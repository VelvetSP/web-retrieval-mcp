"""Adapter tests for fetch-plan identity, bounded codec, Valkey TTL, and singleflight."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock
import zlib
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from web_retrieval_mcp import fetch_cache as cache
from valkey.exceptions import ConnectionError as ValkeyConnectionError
from valkey.exceptions import ResponseError, TimeoutError as ValkeyTimeoutError
from valkey_test_support import IsolatedValkey, valkey_root


def success(body: str = "body") -> cache.FetchSuccess:
    return cache.FetchSuccess(
        stored_at=0,
        tier="exa",
        provider_cache="exa-crawled",
        provider_cache_hours=None,
        result_kind="full",
        effective_limit=20_000,
        requested_cap=10_000,
        body=body,
    )


class UrlIdentityTests(unittest.TestCase):
    def test_utm_decoding_and_case_collapse_without_rewriting_other_tokens(self):
        baseline = "https://EXAMPLE.com/P%2f?q=a+b&q=%2F&&blank=&x=1#Frag"
        variants = (
            "https://EXAMPLE.com/P%2f?UTM_Source=one&q=a+b&q=%2F&&blank=&x=1#Frag",
            "https://EXAMPLE.com/P%2f?utm%5Fsource=two&q=a+b&q=%2F&&blank=&x=1#Frag",
            "https://EXAMPLE.com/P%2f?%75tm_source=three&q=a+b&q=%2F&&blank=&x=1#Frag",
        )
        for variant in variants:
            with self.subTest(variant=variant):
                identity = cache.cache_url_identity(variant)
                self.assertEqual(identity.identity, baseline)
                self.assertTrue(identity.completed_cache_eligible)

    def test_only_utm_tokens_alias_no_query_but_empty_survivors_are_preserved(self):
        self.assertEqual(
            cache.cache_url_identity("https://e.test/p?utm_a=1#f").identity,
            "https://e.test/p#f",
        )
        self.assertEqual(
            cache.cache_url_identity("https://e.test/p?utm_a=1&#f").identity,
            "https://e.test/p?#f",
        )

    def test_non_utm_distinctions_remain_exact(self):
        values = (
            "https://e.test/p?a=1&b=2",
            "https://e.test/p?b=2&a=1",
            "https://e.test/p?a=1&a=1",
            "https://e.test/p?a=1&",
            "https://e.test/p/?a=1",
            "https://e.test/p?a=%2f",
            "https://e.test/p?a=%2F",
            "https://e.test/p?a=1#one",
            "https://e.test/p?a=1#two",
            "https://e.test/p?a=1;utm_source=x",
            "https://e.test/p?utm+source=x",
        )
        identities = [cache.cache_url_identity(value).identity for value in values]
        self.assertEqual(len(identities), len(set(identities)))

    def test_userinfo_and_credential_matrix_bypass(self):
        exact_names = (
            "signature", "sig", "access_token", "id_token", "refresh_token",
            "api_key", "apikey", "auth_token", "token", "jwt", "key", "secret",
            "password", "passwd", "credential", "client_secret", "private_key",
            "authorization", "code",
        )
        bypass = tuple(f"https://e.test/p?{name}=x" for name in exact_names) + (
            "https://user@example.com/p",
            "https://user:pass@example.com/p",
            "https://e.test/p?SIG=x",
            "https://e.test/p?access%5Ftoken=x",
            "https://e.test/p?x-amz-signature=x",
            "https://e.test/p?X%2dGOOG-Credential=x",
            "https://e.test/p#access_token=x",
            "https://e.test/p#/route?token=x",
        )
        allowed = (
            "https://e.test/p?hash=x",
            "https://e.test/p?expires=x",
            "https://e.test/p?utm_signature=x",
            "https://e.test/p?signature_extra=x",
            "https://e.test/p?x-amz=x",
            "https://e.test/p?x-googx=x",
        )
        for value in bypass:
            with self.subTest(value=value):
                self.assertFalse(cache.cache_url_identity(value).completed_cache_eligible)
        for value in allowed:
            with self.subTest(value=value):
                self.assertTrue(cache.cache_url_identity(value).completed_cache_eligible)


class PlanTests(unittest.TestCase):
    def make(self, **changes):
        values = dict(
            cache_url="https://example.com/page?x=1",
            render="auto",
            mode="full",
            question=None,
            effective_max_chars=20_000,
            max_chars_omitted=True,
            explicit_large=False,
            max_age_hours=None,
        )
        values.update(changes)
        return cache.make_fetch_plan(**values)

    def test_hash_is_deterministic_and_key_contains_only_prefix_and_digest(self):
        plan = self.make(question="What is the secret answer?")
        self.assertEqual(plan.key, self.make(question="What is the secret answer?").key)
        self.assertRegex(plan.key, r"^wr:fetch:v1:[0-9a-f]{64}$")
        self.assertNotIn("example", plan.key)
        self.assertNotIn("secret", plan.key)

    def test_every_routing_identity_dimension_is_distinct(self):
        base = self.make().key
        variants = (
            self.make(cache_url="https://example.com/page?x=2"),
            self.make(render="never"),
            self.make(mode="concise"),
            self.make(question="exact question"),
            self.make(effective_max_chars=19_999),
            self.make(max_chars_omitted=False, explicit_large=True),
            self.make(max_age_hours=-1),
            self.make(max_age_hours=0),
            self.make(max_age_hours=12),
        )
        self.assertTrue(all(value.key != base for value in variants))
        self.assertEqual(len({value.key for value in variants}), len(variants))

    def test_omitted_and_explicit_20000_are_not_equivalent(self):
        self.assertNotEqual(
            self.make(max_chars_omitted=True, explicit_large=False).key,
            self.make(max_chars_omitted=False, explicit_large=True).key,
        )

    def test_diagnostics_are_rate_bounded_and_content_free(self):
        cache.reset_cache_metrics_for_tests()
        key = "wr:fetch:v1:" + "a" * 64
        with self.assertLogs("web_retrieval.fetch_cache", level="DEBUG") as captured:
            cache.record_cache_event("miss", key)
            cache.record_cache_event("miss", key)
        self.assertIn("count=1 key=aaaaaaaaaaaa", captured.output[0])
        self.assertIn("INFO", captured.output[0])
        self.assertIn("count=2 key=aaaaaaaaaaaa", captured.output[1])
        self.assertIn("DEBUG", captured.output[1])
        self.assertNotIn("http", "\n".join(captured.output))
        self.assertEqual(cache.cache_metrics_snapshot(), {"miss": 2})


class CodecTests(unittest.TestCase):
    def test_deterministic_round_trip(self):
        encoded = cache.encode_cache_value(success("héllo"), stored_at=1234.5)
        self.assertEqual(encoded, cache.encode_cache_value(success("héllo"), stored_at=1234.5))
        decoded = cache.decode_cache_value(encoded)
        self.assertEqual(decoded.body, "héllo")
        self.assertEqual(decoded.stored_at, 1234.5)

    def test_uncompressed_and_compressed_write_limits(self):
        with self.assertRaises(cache.CacheEntryTooLarge):
            cache.encode_cache_value(success("x" * (cache.MAX_UNCOMPRESSED_BYTES + 1)))
        with mock.patch.object(
            cache.zlib, "compress", return_value=b"x" * cache.MAX_COMPRESSED_BYTES
        ):
            encoded = cache.encode_cache_value(success("small"), stored_at=1)
            self.assertEqual(len(encoded), 8 + cache.MAX_COMPRESSED_BYTES)
        with mock.patch.object(
            cache.zlib, "compress", return_value=b"x" * (cache.MAX_COMPRESSED_BYTES + 1)
        ):
            with self.assertRaises(cache.CacheEntryTooLarge):
                cache.encode_cache_value(success("small"), stored_at=1)

    def test_bomb_corruption_unknown_schema_and_trailing_payload_are_rejected(self):
        bomb = zlib.compress(b"x" * 100_000)
        with self.assertRaises(cache.CacheCodecError):
            cache.decode_cache_value(struct.pack(">4sI", b"WRC1", 10) + bomb)

        encoded = cache.encode_cache_value(success(), stored_at=1)
        magic, length = struct.unpack(">4sI", encoded[:8])
        raw = zlib.decompress(encoded[8:])
        payload = json.loads(raw)
        payload["schema"] = 999
        unknown = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        with self.assertRaises(cache.CacheCodecError):
            cache.decode_cache_value(struct.pack(">4sI", magic, len(unknown)) + zlib.compress(unknown))
        with self.assertRaises(cache.CacheCodecError):
            cache.decode_cache_value(encoded + b"trailing")
        with self.assertRaises(cache.CacheCodecError):
            cache.decode_cache_value(encoded[:-2])
        with self.assertRaises(cache.CacheCodecError):
            cache.decode_cache_value(struct.pack(">4sI", b"WRC1", length + 1) + encoded[8:])

    def test_invalid_json_and_declared_overflow_are_rejected(self):
        raw = b"not json"
        with self.assertRaises(cache.CacheCodecError):
            cache.decode_cache_value(struct.pack(">4sI", b"WRC1", len(raw)) + zlib.compress(raw))
        oversized = struct.pack(">4sI", b"WRC1", cache.MAX_UNCOMPRESSED_BYTES + 1)
        with self.assertRaises(cache.CacheCodecError):
            cache.decode_cache_value(oversized + zlib.compress(b"x"))


class SingleFlightTests(unittest.IsolatedAsyncioTestCase):
    async def test_identical_waiters_share_one_process_owned_producer(self):
        gate = asyncio.Event()
        calls = 0

        async def producer(_flight):
            nonlocal calls
            calls += 1
            await gate.wait()
            return "shared"

        flight = cache.SingleFlight[str]()
        one = asyncio.create_task(flight.run("wr:fetch:v1:" + "1" * 64, producer))
        two = asyncio.create_task(flight.run("wr:fetch:v1:" + "1" * 64, producer))
        await asyncio.sleep(0)
        gate.set()
        self.assertEqual(await asyncio.gather(one, two), ["shared", "shared"])
        self.assertEqual(calls, 1)

    async def test_one_cancelled_waiter_does_not_cancel_remaining_waiter(self):
        gate = asyncio.Event()
        cancelled = False

        async def producer(_flight):
            nonlocal cancelled
            try:
                await gate.wait()
                return "kept"
            except asyncio.CancelledError:
                cancelled = True
                raise

        flight = cache.SingleFlight[str]()
        one = asyncio.create_task(flight.run("wr:fetch:v1:" + "2" * 64, producer))
        two = asyncio.create_task(flight.run("wr:fetch:v1:" + "2" * 64, producer))
        await asyncio.sleep(0)
        one.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await one
        gate.set()
        self.assertEqual(await two, "kept")
        self.assertFalse(cancelled)

    async def test_all_waiters_gone_marks_noncacheable_and_cancels_before_fallback(self):
        started = asyncio.Event()
        token = None
        cancelled = asyncio.Event()
        fallback = False

        async def producer(flight_token):
            nonlocal token, fallback
            token = flight_token
            started.set()
            try:
                await asyncio.Event().wait()
                fallback = True
            except asyncio.CancelledError:
                cancelled.set()
                raise

        flight = cache.SingleFlight[None]()
        waiter = asyncio.create_task(flight.run("wr:fetch:v1:" + "3" * 64, producer))
        await started.wait()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        self.assertIsNotNone(token)
        self.assertFalse(token.cacheable)
        self.assertFalse(fallback)

    async def test_failure_is_shared_but_not_retained(self):
        calls = 0
        gate = asyncio.Event()

        async def producer(_flight):
            nonlocal calls
            calls += 1
            if calls == 1:
                await gate.wait()
                raise RuntimeError("same failure")
            return "retry success"

        flight = cache.SingleFlight[str]()
        key = "wr:fetch:v1:" + "4" * 64
        one = asyncio.create_task(flight.run(key, producer))
        two = asyncio.create_task(flight.run(key, producer))
        await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(one, two, return_exceptions=True)
        self.assertEqual([str(item) for item in results], ["same failure", "same failure"])
        self.assertEqual(await flight.run(key, producer), "retry success")
        self.assertEqual(calls, 2)

    async def test_last_waiter_cancelling_during_set_removes_a_possible_commit(self):
        class CommittingCache:
            def __init__(self):
                self.present = False
                self.set_count = 0
                self.set_started = asyncio.Event()
                self.delete_started = asyncio.Event()
                self.allow_delete = asyncio.Event()
                self.deleted = asyncio.Event()

            async def set(self, _key, _entry):
                # Model a SET that reached Valkey immediately before its caller was
                # cancelled: local await cancellation cannot prove it was not applied.
                self.set_count += 1
                self.present = True
                if self.set_count > 1:
                    return True
                self.set_started.set()
                await asyncio.Event().wait()

            async def delete(self, _key):
                self.delete_started.set()
                await self.allow_delete.wait()
                self.present = False
                self.deleted.set()
                return True

        adapter = CommittingCache()
        tokens = []
        key = "wr:fetch:v1:" + "5" * 64

        async def producer(flight_token):
            tokens.append(flight_token)
            await cache.store_for_flight(adapter, key, success(), flight_token)
            return "stored"

        flights = cache.SingleFlight[str]()
        waiter = asyncio.create_task(flights.run(key, producer))
        await adapter.set_started.wait()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        await asyncio.wait_for(adapter.delete_started.wait(), timeout=1)

        # A new independent caller arriving while the abandoned producer is still
        # deleting must wait for that cleanup, then run a fresh producer.  Joining the
        # doomed task would leak CancelledError; starting immediately could let the old
        # DEL erase the new result.
        replacement = asyncio.create_task(flights.run(key, producer))
        await asyncio.sleep(0)
        self.assertFalse(replacement.done())
        self.assertEqual(adapter.set_count, 1)
        adapter.allow_delete.set()
        await asyncio.wait_for(adapter.deleted.wait(), timeout=1)
        self.assertEqual(await replacement, "stored")
        self.assertEqual(len(tokens), 2)
        self.assertFalse(tokens[0].cacheable)
        self.assertTrue(tokens[1].cacheable)
        self.assertEqual(adapter.set_count, 2)
        self.assertTrue(adapter.present)


class RealValkeyAdapterTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._root_context = valkey_root()
        cls.root = cls._root_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._root_context.__exit__(None, None, None)

    async def test_exact_ttl_hit_corrupt_delete_and_oversize_skip(self):
        with IsolatedValkey(self.root) as real:
            adapter = cache.CompletedFetchCache(str(real.socket))
            key = "wr:fetch:v1:" + "a" * 64
            self.assertTrue(await adapter.set(key, success("stored body")))
            ttl = int(real.cli("TTL", key).stdout.strip())
            self.assertGreaterEqual(ttl, cache.CACHE_TTL_SECONDS - 5)
            self.assertLessEqual(ttl, cache.CACHE_TTL_SECONDS)
            lookup = await adapter.get(key)
            self.assertEqual((lookup.status, lookup.entry.body), ("hit", "stored body"))

            corrupt = "wr:fetch:v1:" + "b" * 64
            self.assertEqual(real.cli("SET", corrupt, "not-zlib").returncode, 0)
            self.assertEqual((await adapter.get(corrupt)).status, "corrupt")
            self.assertEqual(real.cli("EXISTS", corrupt).stdout.strip(), "0")

            unexpected = "wr:fetch:v1:" + "f" * 64
            self.assertEqual(real.cli("SET", unexpected, "decoder-input").returncode, 0)
            with mock.patch.object(
                cache, "decode_cache_value", side_effect=RecursionError("lab nesting")
            ):
                self.assertEqual((await adapter.get(unexpected)).status, "corrupt")
            self.assertEqual(real.cli("EXISTS", unexpected).stdout.strip(), "0")

            too_large = success("x" * (cache.MAX_UNCOMPRESSED_BYTES + 1))
            self.assertFalse(await adapter.set("wr:fetch:v1:" + "c" * 64, too_large))
            await adapter.close()

    async def test_absent_socket_is_a_bounded_fail_open_miss(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = cache.CompletedFetchCache(str(Path(temporary) / "missing.sock"))
            key = "wr:fetch:v1:" + "d" * 64
            self.assertEqual((await adapter.get(key)).status, "error")
            self.assertFalse(await adapter.set(key, success()))
            with mock.patch.object(
                cache, "encode_cache_value", side_effect=RuntimeError("lab codec failure")
            ):
                self.assertFalse(await adapter.set(key, success()))

    async def test_transport_timeout_and_oom_are_fail_open_but_cancellation_propagates(self):
        class FailingClient:
            async def get(self, _key):
                raise ValkeyConnectionError("lab connection failure")

            async def set(self, _key, _value, **_kwargs):
                raise ResponseError("OOM command not allowed")

            async def delete(self, _key):
                raise ValkeyTimeoutError("lab timeout")

        class CancelledClient:
            async def get(self, _key):
                raise asyncio.CancelledError

        with tempfile.TemporaryDirectory() as temporary:
            socket_marker = Path(temporary) / "present.sock"
            socket_marker.touch()
            adapter = cache.CompletedFetchCache(str(socket_marker))
            adapter._client = FailingClient()
            key = "wr:fetch:v1:" + "e" * 64
            self.assertEqual((await adapter.get(key)).status, "error")
            self.assertFalse(await adapter.set(key, success()))
            self.assertFalse(await adapter.delete(key))

            adapter._client = CancelledClient()
            with self.assertRaises(asyncio.CancelledError):
                await adapter.get(key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
