"""Public-boundary acceptance for page-fetch routing and completed-result caching.

The oracle starts the real HTTP MCP subprocess, an isolated real pinned Valkey, and
loopback wire-level Exa/Firecrawl doubles. It never imports ``server.py`` or
``fetch_cache.py``, never monkeypatches routing, and never touches live services or the
live cache socket.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from valkey import Valkey

from valkey_test_support import IsolatedValkey, valkey_root


HERE = Path(__file__).resolve().parent
SOURCE_ROOT = HERE / "src"
RICH_PAGE = "https://www.rfc-editor.org/rfc/rfc2606.html"
THIN_PAGE = "https://example.com/"
CACHE_HOST = "cache.test"
CACHE_URL = f"https://{CACHE_HOST}/accept-cache"
MAX_VALUE_BYTES = 16 * 1024 * 1024

fails = 0


def check(label, condition, detail=""):
    global fails
    if not condition:
        fails += 1
    print(f"{'OK  ' if condition else 'FAIL'}  {label}  {detail}")


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_tcp(port, deadline):
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            try:
                sock.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.05)
    return False


def _eviction_body(url: str) -> str:
    return "".join(
        hashlib.sha256(f"{url}:{index}".encode()).hexdigest()
        for index in range(2400)
    )


class ProviderLab:
    def __init__(self):
        self.events: list[dict] = []
        self.lock = threading.Lock()
        lab = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/exa/contents":
                    tier = "exa"
                    url = (payload.get("urls") or [""])[0]
                elif self.path == "/firecrawl/scrape":
                    tier = "firecrawl"
                    url = payload.get("url") or ""
                else:
                    self.send_error(404)
                    return
                with lab.lock:
                    lab.events.append({"tier": tier, "url": url, "payload": payload})

                if "slow=1" in url:
                    time.sleep(0.45)
                if "accept-failure" in url:
                    if tier == "exa":
                        body = {
                            "results": [],
                            "statuses": [{"status": "error", "error": {"tag": "lab-failure"}}],
                        }
                    else:
                        body = {"success": False, "error": "lab-failure"}
                elif tier == "exa":
                    source = "cached" if "provider-cached=1" in url else "crawled"
                    if "summary" in payload:
                        query = (payload.get("summary") or {}).get("query") or "concise"
                        marker = hashlib.sha256(query.encode()).hexdigest()[:12]
                        text = (f"LAB EXA SUMMARY [plan:{marker}] " * 8).strip()
                        body = {
                            "results": [{"summary": text}],
                            "statuses": [{"status": "success", "source": source}],
                        }
                    elif CACHE_HOST in url or "rebind-cache.test" in url:
                        if "accept-oversize" in url:
                            text = "0123456789abcdef" * ((17 * 1024 * 1024 // 16) + 1)
                        elif "accept-evict" in url:
                            text = _eviction_body(url)
                        else:
                            marker = hashlib.sha256(url.encode()).hexdigest()[:12]
                            # Long enough for the formatting contract below to exercise
                            # the ordinary 1,000-character truncation marker without
                            # embedding the caller URL in the cached provider body.
                            text = (f"LAB EXA BODY [plan:{marker}] " * 40).strip()
                        body = {
                            "results": [{"text": text}],
                            "statuses": [{"status": "success", "source": source}],
                        }
                    else:
                        body = {
                            "results": [],
                            "statuses": [{"status": "success", "source": source}],
                        }
                else:
                    formats = payload.get("formats") or []
                    data = {"metadata": {"statusCode": 200}}
                    if formats and isinstance(formats[0], dict):
                        data["answer"] = "LAB FIRECRAWL ANSWER"
                    elif formats == ["summary"]:
                        data["summary"] = "LAB FIRECRAWL SUMMARY " * 8
                    else:
                        data["markdown"] = "LAB FIRECRAWL BODY " * 30
                    body = {"success": True, "data": data}
                raw = json.dumps(body).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def port(self):
        return self.httpd.server_port

    def start(self):
        self.thread.start()

    def reset(self):
        with self.lock:
            self.events.clear()

    def snapshot(self):
        with self.lock:
            return list(self.events)

    def tiers(self):
        return [event["tier"] for event in self.snapshot()]

    def close(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()


class StallingUnixServer:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wr-stalling-cache-")
        self.path = Path(self.temporary.name) / "valkey.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.stop_event = threading.Event()
        self.connections: list[socket.socket] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                connection, _ = self.listener.accept()
                self.connections.append(connection)
            except TimeoutError:
                continue
            except OSError:
                return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.stop_event.set()
        self.listener.close()
        for connection in self.connections:
            connection.close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()


def base_environment(lab: ProviderLab, socket_path: Path) -> dict[str, str]:
    environment = dict(os.environ)
    dns = {
        CACHE_HOST: ["8.8.8.8"],
        "rebind-cache.test": ["8.8.8.8", "8.8.8.8", "127.0.0.1"],
    }
    environment.update({
        "WEBRET_ACCEPTANCE_LAB": "1",
        "WEBRET_ACCEPTANCE_EXA_CONTENTS_URL":
            f"http://127.0.0.1:{lab.port}/exa/contents",
        "WEBRET_ACCEPTANCE_FIRECRAWL_SCRAPE_URL":
            f"http://127.0.0.1:{lab.port}/firecrawl/scrape",
        "WEBRET_ACCEPTANCE_VALKEY_SOCKET": str(socket_path),
        "WEBRET_ACCEPTANCE_DNS_JSON": json.dumps(dns, sort_keys=True),
        "EXA_API_KEY": "acceptance-exa-dummy",
        "FIRECRAWL_API_KEY": "acceptance-firecrawl-dummy",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    })
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"
    ):
        environment.pop(name, None)
    return environment


class MCPDaemon:
    def __init__(self, environment: dict[str, str]):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        env = dict(environment)
        env["WEB_RETRIEVAL_MCP_PORT"] = str(self.port)
        env["PYTHONPATH"] = str(SOURCE_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "web_retrieval_mcp", "--http"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.stderr = b""
        if not wait_tcp(self.port, time.monotonic() + 20):
            self.stop()
            raise RuntimeError(f"acceptance MCP did not listen on port {self.port}")

    def stop(self):
        if getattr(self, "process", None) is None:
            return
        self.process.terminate()
        try:
            _out, self.stderr = self.process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            _out, self.stderr = self.process.communicate(timeout=8)
        self.process = None


async def call_fetch(mcp_url, *, rendezvous: asyncio.Barrier | None = None, **arguments):
    async with streamable_http_client(mcp_url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if rendezvous is not None:
                await rendezvous.wait()
            result = await session.call_tool("web_fetch", arguments=arguments)
            return "\n".join(
                block.text
                for block in result.content
                if getattr(block, "type", None) == "text"
            )


def fetch(daemon: MCPDaemon, **arguments):
    return asyncio.run(call_fetch(daemon.url, **arguments))


def cache_keys(client: Valkey) -> set[bytes]:
    return set(client.scan_iter(match=b"wr:fetch:v1:*", count=1000))


def seed_and_find_key(client: Valkey, daemon: MCPDaemon, arguments: dict) -> tuple[str, bytes]:
    before = cache_keys(client)
    output = fetch(daemon, **arguments)
    added = cache_keys(client) - before
    if len(added) != 1:
        raise AssertionError(f"expected exactly one newly stored key, got {len(added)}")
    return output, added.pop()


def routing_contract(daemon: MCPDaemon, lab: ProviderLab):
    lab.reset()
    rich = fetch(daemon, url=RICH_PAGE, render="auto", max_chars=5000)
    check("route: usable local render is served by Camoufox",
          rich.startswith("[served by: camoufox]"), rich[:160])
    check("route: Firecrawl untouched after local success", lab.tiers() == ["exa"], lab.tiers())

    lab.reset()
    large = fetch(daemon, url=RICH_PAGE, render="auto", max_chars=20000)
    check("route: explicit-large auto elects local browser, not paid fallback",
          large.startswith("[served by: camoufox]"), large[:160])
    check("route: explicit-large local success calls no provider", lab.tiers() == [], lab.tiers())

    lab.reset()
    thin = fetch(daemon, url=THIN_PAGE, render="auto", max_chars=5000)
    check("route: thin local render falls back to Firecrawl",
          thin.startswith("[served by: firecrawl]"), thin[:160])
    check("route: fallback provider order remains Exa then Firecrawl",
          lab.tiers() == ["exa", "firecrawl"], lab.tiers())

    lab.reset()
    never = fetch(daemon, url=RICH_PAGE, render="never", max_chars=5000)
    check("route: browser-free mode reaches Firecrawl after Exa",
          never.startswith("[served by: firecrawl]"), never[:160])
    check("route: never order remains Exa then Firecrawl",
          lab.tiers() == ["exa", "firecrawl"], lab.tiers())

    lab.reset()
    concise = fetch(daemon, url=RICH_PAGE, render="auto", mode="concise", max_chars=20000)
    check("route: concise explicit-large remains Exa-first",
          concise.startswith("[served by: exa]") and "[mode: concise]" in concise,
          concise[:160])
    check("route: concise Exa success leaves Firecrawl untouched", lab.tiers() == ["exa"], lab.tiers())

    lab.reset()
    always = fetch(daemon, url=RICH_PAGE, render="always", max_chars=5000)
    check("route: always remains forced local browser",
          always.startswith("[served by: camoufox]"), always[:160])
    check("cache: render=always has no local replay disclosure", "local Valkey replay" not in always)
    lab.reset()
    always_again = fetch(daemon, url=RICH_PAGE, render="always", max_chars=5000)
    check("cache: repeated render=always still bypasses completed cache",
          "local Valkey replay" not in always_again, always_again[:160])


def identity_and_plan_contract(daemon: MCPDaemon, lab: ProviderLab):
    lab.reset()
    variants = [
        f"{CACHE_URL}/utm?UTM_Source=one&x=1",
        f"{CACHE_URL}/utm?utm%5Fsource=two&x=1",
        f"{CACHE_URL}/utm?%75tm_source=three&x=1",
    ]
    outputs = [fetch(daemon, url=url, render="never", max_chars=5000) for url in variants]
    events = lab.snapshot()
    check("cache identity: case/encoded UTM variants make one provider request",
          len(events) == 1, f"calls={len(events)}")
    check("cache identity: provider receives the original first URL",
          bool(events) and events[0]["url"] == variants[0], events[0]["url"] if events else "")
    check("cache identity: replay provenance displays each caller's original URL",
          all(url in output for url, output in zip(variants, outputs, strict=True)))
    check("cache identity: later UTM variants disclose local replay",
          all("local Valkey replay" in output for output in outputs[1:]))

    lab.reset()
    distinctions = [
        f"{CACHE_URL}/distinct?a=1&b=2",
        f"{CACHE_URL}/distinct?b=2&a=1",
        f"{CACHE_URL}/distinct?a=1&a=1",
        f"{CACHE_URL}/distinct?a=1",
        f"{CACHE_URL}/distinct?a=",
        f"{CACHE_URL}/distinct?a",
        f"{CACHE_URL}/distinct?a=1&&b=2",
        f"{CACHE_URL}/distinct/?a=1&b=2",
        f"{CACHE_URL}/distinct?a=%2f",
        f"{CACHE_URL}/distinct?a=%2F",
        f"{CACHE_URL}/distinct?a=1#one",
        f"{CACHE_URL}/distinct?a=1#two",
    ]
    for url in distinctions:
        fetch(daemon, url=url, render="never", max_chars=5000)
    check("cache identity: order/duplicates/blanks/empty/path/fragment/encoding stay distinct",
          len(lab.snapshot()) == len(distinctions), f"calls={len(lab.snapshot())}")

    plan_url = f"{CACHE_URL}/plans"
    plans = [
        {"url": plan_url, "render": "never", "max_chars": 5000},
        {"url": plan_url, "render": "auto", "mode": "concise", "max_chars": 5000},
        {"url": plan_url, "render": "never", "mode": "concise", "max_chars": 5000},
        {"url": plan_url, "render": "never", "question": "Exact alpha?", "max_chars": 5000},
        {"url": plan_url, "render": "never", "question": "Exact beta?", "max_chars": 5000},
        {"url": plan_url, "render": "never", "max_chars": 6000},
        {"url": plan_url, "render": "never"},
        {"url": plan_url, "render": "never", "max_chars": 20000},
        {"url": plan_url, "render": "never", "max_chars": 5000, "max_age_hours": -1},
    ]
    lab.reset()
    first = [fetch(daemon, **plan) for plan in plans]
    first_count = len(lab.snapshot())
    second = [fetch(daemon, **plan) for plan in plans]
    check("cache plan: render/mode/question/max/routing/freshness identities do not collide",
          first_count == len(plans), f"provider calls={first_count}")
    check("cache plan: every exact plan replays without another provider call",
          len(lab.snapshot()) == first_count and all("local Valkey replay" in item for item in second),
          f"provider calls={len(lab.snapshot())}")
    check("cache plan: exact question and concise entries retain their public modes",
          "[mode: question]" in first[3]
          and hashlib.sha256(b"Exact alpha?").hexdigest()[:12] in first[3]
          and "[mode: concise]" in first[1]
          and "[mode: question]" in second[3]
          and hashlib.sha256(b"Exact alpha?").hexdigest()[:12] in second[3])


def persistence_and_singleflight_contract(
    daemon: MCPDaemon, environment: dict[str, str], lab: ProviderLab
) -> MCPDaemon:
    url = f"{CACHE_URL}/restart-survival"
    lab.reset()
    first = fetch(daemon, url=url, render="never", max_chars=5000)
    daemon.stop()
    replacement = MCPDaemon(environment)
    second = fetch(replacement, url=url, render="never", max_chars=5000)
    check("cache persistence: completed entry survives MCP process restart",
          "local Valkey replay" not in first and "local Valkey replay" in second,
          second[:180])
    check("cache persistence: MCP restart causes no second provider request",
          len(lab.snapshot()) == 1, f"calls={len(lab.snapshot())}")

    lab.reset()
    concurrent_url = f"{CACHE_URL}/singleflight?slow=1"

    async def concurrent():
        rendezvous = asyncio.Barrier(6)
        return await asyncio.gather(*(
            call_fetch(
                replacement.url, rendezvous=rendezvous,
                url=concurrent_url, render="never", max_chars=5000,
            )
            for _ in range(6)
        ))

    outputs = asyncio.run(concurrent())
    check("singleflight: independent concurrent MCP clients cause one provider cascade",
          len(lab.snapshot()) == 1, f"calls={len(lab.snapshot())}")
    check("singleflight: all current waiters receive the same successful body",
          len(set(outputs)) == 1 and outputs[0].startswith("[served by: exa]"))

    lab.reset()
    fresh_url = f"{CACHE_URL}/singleflight-fresh?slow=1"

    async def concurrent_fresh():
        rendezvous = asyncio.Barrier(4)
        return await asyncio.gather(*(
            call_fetch(
                replacement.url, rendezvous=rendezvous,
                url=fresh_url, render="never", max_chars=5000,
                max_age_hours=0,
            )
            for _ in range(4)
        ))

    asyncio.run(concurrent_fresh())
    after_concurrent = len(lab.snapshot())
    fetch(replacement, url=fresh_url, render="never", max_chars=5000, max_age_hours=0)
    check("singleflight: force-fresh concurrent calls coalesce but are not completed-cached",
          after_concurrent == 1 and len(lab.snapshot()) == 2,
          f"concurrent={after_concurrent} final={len(lab.snapshot())}")
    return replacement


def eligibility_and_security_contract(daemon: MCPDaemon, lab: ProviderLab):
    cases = [
        ("max_age_hours=0", {"url": f"{CACHE_URL}/bypass-zero", "max_age_hours": 0}),
        ("positive freshness", {"url": f"{CACHE_URL}/bypass-positive", "max_age_hours": 12}),
        ("userinfo", {"url": f"https://user:pass@{CACHE_HOST}/accept-cache/bypass-user"}),
        ("signed query", {"url": f"{CACHE_URL}/bypass-signature?Sig=x"}),
        ("x-amz query", {"url": f"{CACHE_URL}/bypass-amz?X-Amz-Signature=x"}),
    ]
    for label, extra in cases:
        arguments = {"render": "never", "max_chars": 5000, **extra}
        lab.reset()
        outputs = [fetch(daemon, **arguments), fetch(daemon, **arguments)]
        check(f"cache bypass: {label} makes two provider requests",
              len(lab.snapshot()) == 2, f"calls={len(lab.snapshot())}")
        check(f"cache bypass: {label} never claims local replay",
              all("local Valkey replay" not in output for output in outputs))

    lab.reset()
    private = fetch(daemon, url="http://127.0.0.1/private", render="never", max_chars=5000)
    check("SSRF: initially private URL fails before cache/provider traffic",
          private.startswith("RETRIEVAL_FAILED:") and "refused non-public" in private,
          private[:180])
    check("SSRF: initially private URL causes no provider traffic", lab.snapshot() == [])

    lab.reset()
    rebound_url = "https://rebind-cache.test/accept-cache/planted"
    seeded = fetch(daemon, url=rebound_url, render="never", max_chars=5000)
    refused = fetch(daemon, url=rebound_url, render="never", max_chars=5000)
    check("SSRF: a hostname newly resolving private refuses its planted cached body",
          "local Valkey replay" not in seeded
          and refused.startswith("RETRIEVAL_FAILED:")
          and "refused non-public" in refused,
          refused[:200])
    check("SSRF: failed cached-body revalidation makes no provider call",
          len(lab.snapshot()) == 1, f"calls={len(lab.snapshot())}")


def corruption_failure_and_format_contract(
    daemon: MCPDaemon, lab: ProviderLab, client: Valkey
):
    privacy_args = {
        "url": f"{CACHE_URL}/privacy?ordinary=value",
        "render": "never",
        "max_chars": 5000,
        "question": "private exact question text",
    }
    _privacy_output, privacy_key = seed_and_find_key(client, daemon, privacy_args)
    privacy_value = client.get(privacy_key)
    privacy_payload = zlib.decompress(privacy_value[8:])
    privacy_envelope = json.loads(privacy_payload)
    check("cache privacy: structured value has no URL or question metadata",
          privacy_args["url"].encode() not in privacy_payload
          and privacy_args["question"].encode() not in privacy_payload
          and "url" not in privacy_envelope
          and "question" not in privacy_envelope)

    def corrupt_case(label: str, suffix: str, mutate):
        arguments = {"url": f"{CACHE_URL}/corrupt-{suffix}", "render": "never", "max_chars": 5000}
        _first, key = seed_and_find_key(client, daemon, arguments)
        valid = client.get(key)
        client.set(key, mutate(valid))
        before = len(lab.snapshot())
        output = fetch(daemon, **arguments)
        check(f"cache corruption: {label} falls through to normal retrieval",
              output.startswith("[served by: exa]") and "LAB EXA BODY" in output,
              output[:160])
        check(f"cache corruption: {label} causes a provider retry",
              len(lab.snapshot()) == before + 1)

    lab.reset()
    corrupt_case("invalid zlib", "zlib", lambda _value: b"WRC1\0\0\0\x05bad")

    def invalid_json(_value):
        raw = b"{bad json"
        return struct.pack(">4sI", b"WRC1", len(raw)) + zlib.compress(raw)

    corrupt_case("invalid JSON", "json", invalid_json)

    def unknown_schema(value):
        raw = zlib.decompress(value[8:])
        payload = json.loads(raw)
        payload["schema"] = 999
        changed = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return struct.pack(">4sI", b"WRC1", len(changed)) + zlib.compress(changed)

    corrupt_case("unknown schema", "schema", unknown_schema)

    def impossible_plan(value):
        raw = zlib.decompress(value[8:])
        payload = json.loads(raw)
        payload.update({
            "tier": "camoufox",
            "provider_cache": "none",
            "provider_cache_hours": None,
            "result_kind": "rendered",
            "requested_cap": None,
        })
        changed = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return struct.pack(">4sI", b"WRC1", len(changed)) + zlib.compress(changed)

    corrupt_case("impossible plan envelope", "plan-envelope", impossible_plan)
    corrupt_case("oversized stored value", "oversize-value",
                 lambda _value: b"WRC1" + b"\0\0\0\x01" + b"x" * (MAX_VALUE_BYTES + 1))

    lab.reset()
    failure_args = {
        "url": f"{CACHE_URL}/accept-failure", "render": "never", "max_chars": 5000,
    }
    failures = [fetch(daemon, **failure_args), fetch(daemon, **failure_args)]
    check("cache failure: provider/final failures are not stored and later calls retry",
          all(output.startswith("RETRIEVAL_FAILED:") for output in failures)
          and lab.tiers() == ["exa", "firecrawl", "exa", "firecrawl"],
          lab.tiers())

    lab.reset()
    concurrent_failure_args = {
        "url": f"{CACHE_URL}/accept-failure?slow=1",
        "render": "never",
        "max_chars": 5000,
    }
    keys_before_failure = cache_keys(client)

    async def concurrent_failures():
        rendezvous = asyncio.Barrier(4)
        return await asyncio.gather(*(
            call_fetch(daemon.url, rendezvous=rendezvous, **concurrent_failure_args)
            for _ in range(4)
        ))

    concurrent_failure_outputs = asyncio.run(concurrent_failures())
    check("cache failure: concurrent current waiters share one failed provider cascade",
          lab.tiers() == ["exa", "firecrawl"]
          and len(set(concurrent_failure_outputs)) == 1
          and concurrent_failure_outputs[0].startswith("RETRIEVAL_FAILED:"),
          f"tiers={lab.tiers()} outputs={len(set(concurrent_failure_outputs))}")
    check("cache failure: shared failure writes no completed entry",
          cache_keys(client) == keys_before_failure)
    retry_after_shared_failure = fetch(daemon, **concurrent_failure_args)
    check("cache failure: an independent call after shared failure retries both tiers",
          retry_after_shared_failure.startswith("RETRIEVAL_FAILED:")
          and lab.tiers() == ["exa", "firecrawl", "exa", "firecrawl"],
          lab.tiers())

    lab.reset()
    oversized_args = {
        "url": f"{CACHE_URL}/accept-oversize", "render": "never", "max_chars": 5000,
    }
    oversized = [fetch(daemon, **oversized_args), fetch(daemon, **oversized_args)]
    check("cache size: oversized successful results still return with ordinary truncation",
          all(output.startswith("[served by: exa]") and "[TRUNCATED at 5000 chars" in output
              for output in oversized))
    check("cache size: oversized successes skip storage and are fetched again",
          len(lab.snapshot()) == 2 and all("local Valkey replay" not in output for output in oversized),
          f"calls={len(lab.snapshot())}")

    lab.reset()
    format_args = {
        "url": f"{CACHE_URL}/format?provider-cached=1",
        "render": "never",
        "max_chars": 1000,
    }
    live = fetch(daemon, **format_args)
    replay = fetch(daemon, **format_args)
    check("cache format: live response retains provider-cache and truncation notices",
          "[cache: exa served a cached copy" in live and "[TRUNCATED at 1000 chars" in live)
    check("cache format: replay separates original source state from local replay age",
          "[source: original exa response was provider-cached]" in replay
          and "[cache: local Valkey replay, age" in replay
          and "[cache: exa served a cached copy" not in replay
          and "[TRUNCATED at 1000 chars" in replay,
          replay[:300])


def cache_outage_contract(environment: dict[str, str], lab: ProviderLab):
    with tempfile.TemporaryDirectory(prefix="wr-missing-cache-") as temporary:
        missing_env = dict(environment)
        missing_env["WEBRET_ACCEPTANCE_VALKEY_SOCKET"] = str(Path(temporary) / "missing.sock")
        daemon = MCPDaemon(missing_env)
        try:
            lab.reset()
            output = fetch(
                daemon, url=f"{CACHE_URL}/missing-sidecar", render="never", max_chars=5000
            )
            check("cache outage: missing sidecar fails open to ordinary retrieval",
                  output.startswith("[served by: exa]") and len(lab.snapshot()) == 1,
                  output[:160])
        finally:
            daemon.stop()

    with StallingUnixServer() as stalling:
        timeout_env = dict(environment)
        timeout_env["WEBRET_ACCEPTANCE_VALKEY_SOCKET"] = str(stalling.path)
        daemon = MCPDaemon(timeout_env)
        try:
            lab.reset()
            started = time.monotonic()
            output = fetch(
                daemon, url=f"{CACHE_URL}/timeout-sidecar", render="never", max_chars=5000
            )
            elapsed = time.monotonic() - started
            check("cache outage: socket timeout fails open without changing tool output",
                  output.startswith("[served by: exa]") and len(lab.snapshot()) == 1,
                  output[:160])
            check("cache outage: timeout does not consume seconds of provider budget",
                  elapsed < 3.0, f"elapsed={elapsed:.3f}s")
        finally:
            daemon.stop()


def eviction_contract(root: Path, lab: ProviderLab):
    with IsolatedValkey(root, maxmemory="2mb") as tiny:
        environment = base_environment(lab, tiny.socket)
        daemon = MCPDaemon(environment)
        client = Valkey(unix_socket_path=str(tiny.socket), decode_responses=False)
        try:
            lab.reset()
            mapping: dict[bytes, str] = {}
            all_successful = True
            for index in range(36):
                url = f"{CACHE_URL}/accept-evict/{index}"
                before = cache_keys(client)
                output = fetch(daemon, url=url, render="never", max_chars=5000)
                all_successful = all_successful and output.startswith("[served by: exa]")
                added = cache_keys(client) - before
                if len(added) == 1:
                    mapping[added.pop()] = url
            info = tiny.cli("INFO", "stats").stdout
            evicted = int(info.split("evicted_keys:", 1)[1].splitlines()[0])
            remaining = cache_keys(client)
            lost = [(key, url) for key, url in mapping.items() if key not in remaining]
            check("cache capacity: tiny maxmemory never turns eviction into MCP failure", all_successful)
            check("cache capacity: tiny maxmemory demonstrates real Valkey eviction",
                  evicted > 0 and bool(lost), f"evicted_keys={evicted} tracked_lost={len(lost)}")
            if lost:
                before_calls = len(lab.snapshot())
                refetched = fetch(daemon, url=lost[0][1], render="never", max_chars=5000)
                check("cache capacity: an evicted plan misses and re-fetches normally",
                      refetched.startswith("[served by: exa]")
                      and len(lab.snapshot()) == before_calls + 1,
                      f"calls={len(lab.snapshot())}")
        finally:
            client.close()
            daemon.stop()


def cache_diagnostics_contract(stderr_chunks: list[bytes]):
    stderr = b"\n".join(value for value in stderr_chunks if value).decode(
        "utf-8", "replace"
    )
    lines = [line for line in stderr.splitlines() if "fetch-cache event=" in line]
    suffixes = [line.split("fetch-cache ", 1)[1] for line in lines]
    required = {
        "hit",
        "miss",
        "stored",
        "singleflight.leader",
        "singleflight.join",
        "bypass.force-fresh",
        "corrupt.zlib",
        "skipped.uncompressed-limit",
    }
    observed = {
        match.group(1)
        for suffix in suffixes
        if (match := re.fullmatch(
            r"event=([a-z0-9_.-]+) count=[1-9][0-9]*(?: key=[0-9a-f]{12})?",
            suffix,
        ))
    }
    check(
        "cache diagnostics: production HTTP process exposes every operation class",
        required <= observed,
        f"missing={sorted(required - observed)}",
    )
    check(
        "cache diagnostics: emitted records are bounded and request-content-free",
        bool(lines)
        and len(lines) < 100
        and len(lines) == len(suffixes)
        and all(len(suffix) < 160 for suffix in suffixes)
        and all(
            re.fullmatch(
                r"event=[a-z0-9_.-]+ count=[1-9][0-9]*(?: key=[0-9a-f]{12})?",
                suffix,
            )
            for suffix in suffixes
        ),
        f"records={len(lines)}",
    )


def main():
    lab = ProviderLab()
    lab.start()
    daemon = None
    all_stderr = []
    try:
        with valkey_root() as root:
            # This ceiling is test headroom, not pre-allocation.  It must admit a
            # deliberately planted >16 MiB corrupt value so the APPLICATION bound is
            # exercised; eviction behavior has its own 2 MiB fixture below.
            with IsolatedValkey(root, maxmemory="64mb") as real:
                environment = base_environment(lab, real.socket)
                daemon = MCPDaemon(environment)
                client = Valkey(unix_socket_path=str(real.socket), decode_responses=False)
                try:
                    routing_contract(daemon, lab)
                    identity_and_plan_contract(daemon, lab)
                    daemon = persistence_and_singleflight_contract(daemon, environment, lab)
                    eligibility_and_security_contract(daemon, lab)
                    corruption_failure_and_format_contract(daemon, lab, client)
                    cache_outage_contract(environment, lab)
                finally:
                    client.close()
                    if daemon is not None:
                        daemon.stop()
                        all_stderr.append(daemon.stderr)
                        daemon = None
            eviction_contract(root, lab)
    except Exception as exc:
        check("public MCP + real Valkey acceptance completed", False, repr(exc))
    finally:
        if daemon is not None:
            daemon.stop()
            all_stderr.append(daemon.stderr)
        lab.close()

    cache_diagnostics_contract(all_stderr)

    if fails:
        stderr = b"\n".join(value for value in all_stderr if value)
        if stderr:
            print("\n--- acceptance daemon stderr (tail) ---")
            print(stderr.decode("utf-8", "replace")[-8000:])
    print(
        f"\n{'ALL FETCH-POLICY/CACHE ACCEPTANCE TESTS PASS' if fails == 0 else str(fails) + ' FAILURE(S)'}"
    )
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
