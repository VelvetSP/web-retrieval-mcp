"""Completed-result Valkey cache and process-local singleflight for ``web_fetch``.

The cache is deliberately optional.  Every Valkey/codec failure resolves to a miss;
URL policy and SSRF decisions remain in ``server.py`` and are never handled here.
Values contain only structured retrieval outcomes: never caller URLs or questions.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
from typing import Awaitable, Callable, Generic, TypeVar
from urllib.parse import unquote, urlparse
import zlib

try:
    from valkey import asyncio as valkey_async
except ImportError:  # Cache absence must not make the retrieval server unimportable.
    valkey_async = None


CACHE_KEY_PREFIX = "wr:fetch:v1:"
CACHE_SCHEMA_VERSION = 1
CACHE_TTL_SECONDS = 86_400
MAX_UNCOMPRESSED_BYTES = 16 * 1024 * 1024
MAX_COMPRESSED_BYTES = 16 * 1024 * 1024
_VALUE_MAGIC = b"WRC1"
_VALUE_HEADER = struct.Struct(">4sI")
_SOCKET_TIMEOUT_SECONDS = 0.1
_OPERATION_TIMEOUT_SECONDS = 0.125
_MAX_CONNECTIONS = 16

_CREDENTIAL_NAMES = frozenset({
    "signature",
    "sig",
    "access_token",
    "id_token",
    "refresh_token",
    "api_key",
    "apikey",
    "auth_token",
    "token",
    "jwt",
    "key",
    "secret",
    "password",
    "passwd",
    "credential",
    "client_secret",
    "private_key",
    "authorization",
    "code",
})
_CREDENTIAL_PREFIXES = ("x-amz-", "x-goog-")

_LOG = logging.getLogger("web_retrieval.fetch_cache")
_METRICS: Counter[str] = Counter()
_METRICS_LOCK = threading.Lock()


def _component_has_credential(component: str) -> bool:
    """Recognize secret-bearing names in query or query-shaped fragment text."""
    for token in component.split("&") if component else ():
        raw_name = token.split("=", 1)[0]
        _prefix, question, parameter = raw_name.rpartition("?")
        decoded_name = unquote(parameter if question else raw_name).casefold()
        if (decoded_name in _CREDENTIAL_NAMES
                or decoded_name.startswith(_CREDENTIAL_PREFIXES)):
            return True
    return False


def _key_prefix(key: str | None) -> str:
    if not key or not key.startswith(CACHE_KEY_PREFIX):
        return ""
    return key[len(CACHE_KEY_PREFIX):len(CACHE_KEY_PREFIX) + 12]


def record_cache_event(event: str, key: str | None = None) -> None:
    """Count an operation class and emit bounded, content-free diagnostics.

    The first occurrence and each 1,024th recurrence are INFO-visible; intervening
    events remain DEBUG-only.  Log volume is therefore bounded without hiding rare
    errors, and correlation never contains more than a short plan-hash prefix.
    """
    with _METRICS_LOCK:
        _METRICS[event] += 1
        count = _METRICS[event]
    prefix = _key_prefix(key)
    emit = _LOG.info if count == 1 or count % 1024 == 0 else _LOG.debug
    if prefix:
        emit("fetch-cache event=%s count=%d key=%s", event, count, prefix)
    else:
        emit("fetch-cache event=%s count=%d", event, count)


def cache_metrics_snapshot() -> dict[str, int]:
    with _METRICS_LOCK:
        return dict(_METRICS)


def reset_cache_metrics_for_tests() -> None:
    with _METRICS_LOCK:
        _METRICS.clear()


@dataclass(frozen=True)
class CacheUrlIdentity:
    identity: str
    completed_cache_eligible: bool
    bypass_reason: str | None


def cache_url_identity(url: str) -> CacheUrlIdentity:
    """Return the UTM-only cache identity and privacy eligibility decision.

    Only the query substring is rebuilt.  Non-UTM raw tokens retain byte spelling,
    order, duplicates, blank values, empty tokens, and percent encoding.
    """
    parsed = urlparse(url)
    try:
        has_userinfo = parsed.username is not None or parsed.password is not None
    except ValueError:
        # URL-policy validation owns malformed-authority refusal.  If it ever admits
        # one, completed caching still takes the privacy-safe bypass direction.
        has_userinfo = True

    hash_at = url.find("#")
    query_at = url.find("?")
    has_query = query_at >= 0 and (hash_at < 0 or query_at < hash_at)
    fragment_credential = _component_has_credential(parsed.fragment)
    if not has_query:
        if has_userinfo:
            reason = "userinfo"
        elif fragment_credential:
            reason = "credential-fragment"
        else:
            reason = None
        return CacheUrlIdentity(url, reason is None, reason)

    query_end = len(url) if hash_at < 0 else hash_at
    raw_query = url[query_at + 1:query_end]
    survivors: list[str] = []
    dropped_utm = False
    credential = fragment_credential
    for token in raw_query.split("&"):
        raw_name = token.split("=", 1)[0]
        decoded_name = unquote(raw_name).casefold()
        credential = credential or (
            decoded_name in _CREDENTIAL_NAMES
            or decoded_name.startswith(_CREDENTIAL_PREFIXES)
        )
        if decoded_name.startswith("utm_"):
            dropped_utm = True
            continue
        survivors.append(token)

    if dropped_utm:
        # A query made solely of UTM tokens aliases the no-query URL.  Surviving
        # empty tokens are real distinctions: ``?utm_x=1&`` therefore keeps ``?``.
        if survivors:
            identity = url[:query_at] + "?" + "&".join(survivors) + url[query_end:]
        else:
            identity = url[:query_at] + url[query_end:]
    else:
        identity = url

    if has_userinfo:
        reason = "userinfo"
    elif credential:
        reason = "credential-query"
    else:
        reason = None
    return CacheUrlIdentity(identity, reason is None, reason)


@dataclass(frozen=True)
class FetchPlan:
    cache_url: str
    render: str
    mode: str
    question_sha256: str | None
    effective_max_chars: int
    routing_class: str
    freshness_class: str
    tavily_enabled: bool

    def canonical_bytes(self) -> bytes:
        payload = {
            "cache_url": self.cache_url,
            "effective_max_chars": self.effective_max_chars,
            "freshness_class": self.freshness_class,
            "mode": self.mode,
            "question_sha256": self.question_sha256,
            "render": self.render,
            "routing_class": self.routing_class,
            "tavily_enabled": self.tavily_enabled,
            "version": 2,
        }
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    @property
    def key(self) -> str:
        return CACHE_KEY_PREFIX + hashlib.sha256(self.canonical_bytes()).hexdigest()


def make_fetch_plan(
    *,
    cache_url: str,
    render: str,
    mode: str,
    question: str | None,
    effective_max_chars: int,
    max_chars_omitted: bool,
    explicit_large: bool,
    max_age_hours: int | float | None,
    tavily_enabled: bool = False,
) -> FetchPlan:
    if max_chars_omitted:
        routing_class = "omitted-default"
    elif explicit_large:
        routing_class = "explicit-large"
    else:
        routing_class = "explicit-bounded"

    if max_age_hours is None:
        freshness_class = "default"
    elif max_age_hours == -1:
        freshness_class = "always-cache"
    elif max_age_hours == 0:
        freshness_class = "force-fresh"
    else:
        # Positive freshness calls are completed-cache-ineligible, but exact values
        # remain in the process-local singleflight identity.
        freshness_class = f"positive:{max_age_hours}"

    question_hash = None
    if question is not None:
        question_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()
    return FetchPlan(
        cache_url=cache_url,
        render=render,
        mode=mode,
        question_sha256=question_hash,
        effective_max_chars=effective_max_chars,
        routing_class=routing_class,
        freshness_class=freshness_class,
        tavily_enabled=tavily_enabled,
    )


@dataclass(frozen=True)
class FetchSuccess:
    """Provider success before public URL/header decoration."""

    stored_at: float
    tier: str
    provider_cache: str
    provider_cache_hours: int | float | None
    result_kind: str
    effective_limit: int
    requested_cap: int | None
    body: str


class CacheCodecError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class CacheEntryTooLarge(CacheCodecError):
    pass


def _entry_payload(entry: FetchSuccess, stored_at: float) -> dict[str, object]:
    return {
        "body": entry.body,
        "effective_limit": entry.effective_limit,
        "provider_cache": entry.provider_cache,
        "provider_cache_hours": entry.provider_cache_hours,
        "requested_cap": entry.requested_cap,
        "result_kind": entry.result_kind,
        "schema": CACHE_SCHEMA_VERSION,
        "stored_at": stored_at,
        "tier": entry.tier,
    }


def encode_cache_value(entry: FetchSuccess, *, stored_at: float | None = None) -> bytes:
    timestamp = time.time() if stored_at is None else stored_at
    raw = json.dumps(
        _entry_payload(entry, timestamp),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(raw) > MAX_UNCOMPRESSED_BYTES:
        raise CacheEntryTooLarge("uncompressed-limit")
    compressed = zlib.compress(raw)
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise CacheEntryTooLarge("compressed-limit")
    return _VALUE_HEADER.pack(_VALUE_MAGIC, len(raw)) + compressed


def _bounded_inflate(compressed: bytes, declared_length: int) -> bytes:
    if declared_length < 1 or declared_length > MAX_UNCOMPRESSED_BYTES:
        raise CacheCodecError("declared-length")
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(compressed, declared_length + 1)
    except zlib.error as exc:
        raise CacheCodecError("zlib") from exc
    if len(raw) > declared_length:
        raise CacheCodecError("inflate-overflow")
    # max_length is one byte larger than the declaration, so a valid stream has
    # enough room to reach EOF in the call above.  Any remainder is inconsistent,
    # oversized, incomplete, or has trailing payload.
    if (not inflater.eof or inflater.unconsumed_tail or inflater.unused_data):
        raise CacheCodecError("incomplete-or-trailing")
    try:
        tail = inflater.flush()
    except zlib.error as exc:
        raise CacheCodecError("zlib-flush") from exc
    raw += tail
    if len(raw) != declared_length:
        raise CacheCodecError("length-mismatch")
    return raw


def decode_cache_value(value: bytes) -> FetchSuccess:
    if not isinstance(value, bytes):
        raise CacheCodecError("non-binary")
    if len(value) > _VALUE_HEADER.size + MAX_COMPRESSED_BYTES:
        raise CacheCodecError("compressed-limit")
    if len(value) <= _VALUE_HEADER.size:
        raise CacheCodecError("short-header")
    magic, declared = _VALUE_HEADER.unpack(value[:_VALUE_HEADER.size])
    if magic != _VALUE_MAGIC:
        raise CacheCodecError("magic")
    raw = _bounded_inflate(value[_VALUE_HEADER.size:], declared)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CacheCodecError("json") from exc
    expected_fields = {
        "body", "effective_limit", "provider_cache", "provider_cache_hours",
        "requested_cap", "result_kind", "schema", "stored_at", "tier",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise CacheCodecError("shape")
    if payload.get("schema") != CACHE_SCHEMA_VERSION:
        raise CacheCodecError("schema")
    if payload.get("tier") not in {"exa", "camoufox", "tavily", "firecrawl"}:
        raise CacheCodecError("tier")
    if payload.get("provider_cache") not in {
        "none", "exa-cached", "exa-crawled", "firecrawl-default",
        "firecrawl-window", "firecrawl-always-default", "fresh", "tavily-unspecified",
    }:
        raise CacheCodecError("provider-cache")
    if payload.get("result_kind") not in {"full", "concise", "question", "rendered"}:
        raise CacheCodecError("result-kind")
    stored_at = payload.get("stored_at")
    if (not isinstance(stored_at, (int, float)) or isinstance(stored_at, bool)
            or not math.isfinite(stored_at) or stored_at <= 0):
        raise CacheCodecError("stored-at")
    effective_limit = payload.get("effective_limit")
    if (not isinstance(effective_limit, int) or isinstance(effective_limit, bool)
            or not 1000 <= effective_limit <= 100_000):
        raise CacheCodecError("effective-limit")
    requested_cap = payload.get("requested_cap")
    if requested_cap is not None and (
            not isinstance(requested_cap, int) or isinstance(requested_cap, bool)
            or not 1 <= requested_cap <= 100_000):
        raise CacheCodecError("requested-cap")
    provider_hours = payload.get("provider_cache_hours")
    if provider_hours is not None and (
            not isinstance(provider_hours, (int, float)) or isinstance(provider_hours, bool)
            or not math.isfinite(provider_hours)):
        raise CacheCodecError("provider-cache-hours")
    body = payload.get("body")
    if not isinstance(body, str):
        raise CacheCodecError("body")
    return FetchSuccess(
        stored_at=float(stored_at),
        tier=payload["tier"],
        provider_cache=payload["provider_cache"],
        provider_cache_hours=provider_hours,
        result_kind=payload["result_kind"],
        effective_limit=effective_limit,
        requested_cap=requested_cap,
        body=body,
    )


def default_cache_socket_path() -> str:
    """Return the configured or platform-default Unix-domain socket path."""
    public_override = os.environ.get("WEB_RETRIEVAL_MCP_VALKEY_SOCKET")
    if public_override is not None:
        path = Path(public_override).expanduser()
        if not path.is_absolute() or "\x00" in public_override:
            raise RuntimeError(
                "WEB_RETRIEVAL_MCP_VALKEY_SOCKET must be an absolute path"
            )
        return str(path)
    override = os.environ.get("WEBRET_ACCEPTANCE_VALKEY_SOCKET")
    if override is not None:
        path = Path(override)
        if (os.environ.get("WEBRET_ACCEPTANCE_LAB") != "1"
                or not path.is_absolute() or "\x00" in override):
            raise RuntimeError(
                "WEBRET_ACCEPTANCE_VALKEY_SOCKET is acceptance-only and must be "
                "an absolute path with WEBRET_ACCEPTANCE_LAB=1"
            )
        return str(path)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if sys.platform == "win32":
        return str(Path(tempfile.gettempdir()) / "web-retrieval-mcp" / "valkey.sock")
    if not runtime or not Path(runtime).is_absolute():
        runtime = f"/run/user/{os.getuid()}"
    return str(Path(runtime) / "web-retrieval-valkey" / "valkey.sock")


def cache_enabled() -> bool:
    """Resolve the optional completed-result cache setting."""
    raw = os.environ.get("WEB_RETRIEVAL_MCP_CACHE", "auto").strip().casefold()
    if raw == "auto":
        return sys.platform != "win32" and valkey_async is not None
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(
        "WEB_RETRIEVAL_MCP_CACHE must be auto, on/off, true/false, yes/no, or 1/0"
    )


@dataclass(frozen=True)
class CacheLookup:
    status: str
    entry: FetchSuccess | None = None


class CompletedFetchCache:
    """Small fail-open adapter around valkey-py's asyncio Unix-socket client."""

    def __init__(self, socket_path: str | None = None, *, enabled: bool | None = None) -> None:
        self.socket_path = socket_path or default_cache_socket_path()
        self.enabled = cache_enabled() if enabled is None else enabled
        self._client = None

    def _get_client(self):
        if valkey_async is None:
            raise RuntimeError("valkey-py unavailable")
        if self._client is None:
            self._client = valkey_async.Valkey(
                unix_socket_path=self.socket_path,
                decode_responses=False,
                socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
                socket_timeout=_SOCKET_TIMEOUT_SECONDS,
                retry_on_timeout=False,
                max_connections=_MAX_CONNECTIONS,
                health_check_interval=0,
            )
        return self._client

    async def _command(self, awaitable):
        async with asyncio.timeout(_OPERATION_TIMEOUT_SECONDS):
            return await awaitable

    async def get(self, key: str) -> CacheLookup:
        if not self.enabled:
            record_cache_event("disabled", key)
            return CacheLookup("disabled")
        # Avoid constructing a loop-bound client when the optional sidecar/socket is
        # simply absent.  It is checked again on every call, so a later sidecar start
        # is picked up without an MCP restart.
        if not Path(self.socket_path).exists():
            record_cache_event("error.socket-missing", key)
            return CacheLookup("error")
        try:
            raw = await self._command(self._get_client().get(key))
        except Exception as exc:  # cache transport is fail-open; CancelledError is BaseException
            record_cache_event(f"error.{exc.__class__.__name__}", key)
            return CacheLookup("error")
        if raw is None:
            record_cache_event("miss", key)
            return CacheLookup("miss")
        if not isinstance(raw, bytes):
            record_cache_event("corrupt.non-binary", key)
            await self.delete(key)
            return CacheLookup("corrupt")
        try:
            entry = decode_cache_value(raw)
        except CacheCodecError as exc:
            record_cache_event(f"corrupt.{exc.reason}", key)
            await self.delete(key)
            return CacheLookup("corrupt")
        except Exception as exc:
            # Decoder implementation/runtime failures are still cache corruption,
            # never a reason to fail the retrieval tool. CancelledError remains a
            # BaseException and therefore keeps its process-control semantics.
            record_cache_event(f"corrupt.decode-{exc.__class__.__name__}", key)
            await self.delete(key)
            return CacheLookup("corrupt")
        record_cache_event("hit", key)
        return CacheLookup("hit", entry)

    async def set(self, key: str, entry: FetchSuccess) -> bool:
        if not self.enabled:
            return False
        try:
            value = encode_cache_value(entry)
        except CacheEntryTooLarge as exc:
            record_cache_event(f"skipped.{exc.reason}", key)
            return False
        except Exception as exc:
            record_cache_event(f"error.encode-{exc.__class__.__name__}", key)
            return False
        if not Path(self.socket_path).exists():
            record_cache_event("error.socket-missing", key)
            return False
        try:
            # Exact product contract: SET <key> <value> EX 86400.
            stored = await self._command(
                self._get_client().set(key, value, ex=CACHE_TTL_SECONDS)
            )
        except Exception as exc:  # fail-open, including timeout/OOM/connection errors
            record_cache_event(f"error.{exc.__class__.__name__}", key)
            return False
        if stored:
            record_cache_event("stored", key)
            return True
        record_cache_event("error.set-refused", key)
        return False

    async def delete(self, key: str) -> bool:
        if not self.enabled:
            return False
        if not Path(self.socket_path).exists():
            return False
        try:
            await self._command(self._get_client().delete(key))
            return True
        except Exception as exc:
            record_cache_event(f"error.delete.{exc.__class__.__name__}", key)
            return False

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await self._command(client.aclose())
        except Exception:
            pass


T = TypeVar("T")


@dataclass
class FlightToken:
    key: str
    cacheable: bool = True
    waiters: int = 0
    task: asyncio.Task | None = None


async def store_for_flight(
    cache: CompletedFetchCache,
    key: str,
    entry: FetchSuccess,
    flight: FlightToken,
) -> bool:
    """Store only while a flight still has a waiter.

    A cancelled Valkey coroutine may already have put ``SET`` bytes on the socket.
    If the last waiter disappears during that await, delete the possibly committed
    value before the producer task completes and permits a new same-key flight.
    """
    if not flight.cacheable:
        record_cache_event("skipped.no-waiters", key)
        return False
    try:
        stored = await cache.set(key, entry)
    except asyncio.CancelledError:
        record_cache_event("cleanup.cancelled-store", key)
        await cache.delete(key)
        raise
    if not flight.cacheable:
        record_cache_event("cleanup.no-waiters-after-store", key)
        await cache.delete(key)
        return False
    return stored


class SingleFlight(Generic[T]):
    """Exact-key, process-local coalescing with process-owned producer tasks."""

    def __init__(self) -> None:
        self._flights: dict[str, FlightToken] = {}

    async def run(
        self,
        key: str,
        producer: Callable[[FlightToken], Awaitable[T]],
    ) -> T:
        flight = self._flights.get(key)
        while (flight is not None and flight.task is not None
               and flight.waiters == 0 and not flight.task.done()):
            # The last prior waiter has already abandoned and cancelled this
            # producer, but its cache-write cleanup may still be running.  A later
            # independent caller must not join that doomed task, and a new SET must
            # not race the old flight's compensating DEL.  Wait without becoming an
            # old-flight waiter, then retry normal leader/join selection.
            record_cache_event("singleflight.wait-abandoned-cleanup", key)
            try:
                await asyncio.shield(flight.task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except Exception:
                pass
            flight = self._flights.get(key)
        if flight is None or (flight.task is not None and flight.task.done()
                              and flight.waiters == 0):
            flight = FlightToken(key=key)

            async def _owned_producer() -> T:
                return await producer(flight)

            task = asyncio.create_task(_owned_producer(), name=f"web-fetch-{_key_prefix(key)}")
            flight.task = task
            self._flights[key] = flight
            record_cache_event("singleflight.leader", key)

            def _done(done: asyncio.Task) -> None:
                if flight.waiters == 0 and self._flights.get(key) is flight:
                    self._flights.pop(key, None)
                # Retrieve an orphaned exception after all waiters cancel so asyncio
                # never emits "Task exception was never retrieved".
                if flight.waiters == 0 and not done.cancelled():
                    try:
                        done.exception()
                    except Exception:
                        pass

            task.add_done_callback(_done)
        else:
            record_cache_event("singleflight.join", key)

        flight.waiters += 1
        assert flight.task is not None
        try:
            # shield makes the producer process-owned: one HTTP waiter cancelling
            # cannot cancel work another waiter still needs.
            return await asyncio.shield(flight.task)
        finally:
            flight.waiters -= 1
            if flight.waiters == 0:
                if not flight.task.done():
                    # The producer sees cacheable=False before cancellation reaches
                    # it.  abandon_on_cancel provider threads may finish, but the task
                    # cannot store or advance to a later fallback tier.
                    flight.cacheable = False
                    record_cache_event("singleflight.all-waiters-gone", key)
                    flight.task.cancel()
                elif self._flights.get(key) is flight:
                    self._flights.pop(key, None)
                    # Close the narrow race where the producer finishes with an
                    # exception after shield has delivered waiter cancellation but
                    # before this finally block runs. Other waiters may still inspect
                    # a Task's exception after this retrieval; this only suppresses an
                    # orphaned "never retrieved" warning when there are none.
                    if not flight.task.cancelled():
                        try:
                            flight.task.exception()
                        except Exception:
                            pass

    def active_count(self) -> int:
        return len(self._flights)
