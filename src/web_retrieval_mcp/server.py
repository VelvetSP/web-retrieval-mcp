#!/usr/bin/env python3
"""High-fidelity web search and fetch tools for Model Context Protocol clients.

The server exposes six read-only tools. General search uses Exa by default or
Tavily when selected, with Firecrawl as a search fallback. Full-page retrieval
uses Exa, an optional local Camoufox renderer, optional Tavily Extract, and
Firecrawl as the final backstop. Three additional tools expose Firecrawl's
research-paper and developer indexes.

Provider credentials are resolved in-process from environment variables, an
optional dotenv-style key file, the optional ``keyring`` package, or an OS
secret-store command. Secrets are never placed in subprocess command arguments
or written to logs. Optional browser, cache, keyring, and Tavily dependencies
are imported lazily so the base package remains usable without them.

stdout is reserved for JSON-RPC. Diagnostics are written only to stderr.
"""
from __future__ import annotations

import asyncio
import argparse
import contextvars
import ipaddress
import json
import logging
import os
import random
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urlparse, urlsplit, urlunsplit

from datetime import date, datetime, timedelta, timezone

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from ._version import __version__
from .fetch_cache import (
    CacheLookup,
    CompletedFetchCache,
    FetchSuccess,
    FlightToken,
    SingleFlight,
    cache_url_identity,
    make_fetch_plan,
    record_cache_event,
    store_for_flight,
)
def _acceptance_lab_endpoint(env_name: str, production_url: str) -> str:
    """Allow a loopback-only provider double for the black-box acceptance harness.

    The gate is intentionally import-time and fail-loud: a stray endpoint override must
    never redirect a real provider credential. Production has no override variables set.
    """
    override = os.environ.get(env_name)
    if override is None:
        return production_url
    parsed = urlparse(override)
    if (os.environ.get("WEBRET_ACCEPTANCE_LAB") != "1"
            or parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None):
        raise RuntimeError(
            f"{env_name} is acceptance-only and must be an unauthenticated "
            "http://127.0.0.1 endpoint with WEBRET_ACCEPTANCE_LAB=1")
    return override


def _acceptance_dns_sequences() -> dict[str, tuple[str, ...]]:
    """Parse the black-box harness's deterministic DNS sequence, if enabled.

    This is intentionally narrower than a general resolver override: it is accepted
    only in the same explicit lab mode as provider doubles, and every value must be a
    literal IP address.  Production therefore cannot redirect DNS through an arbitrary
    executable, file, or network service.
    """
    raw = os.environ.get("WEBRET_ACCEPTANCE_DNS_JSON")
    if raw is None:
        return {}
    if os.environ.get("WEBRET_ACCEPTANCE_LAB") != "1":
        raise RuntimeError(
            "WEBRET_ACCEPTANCE_DNS_JSON is acceptance-only and requires "
            "WEBRET_ACCEPTANCE_LAB=1"
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("WEBRET_ACCEPTANCE_DNS_JSON must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("WEBRET_ACCEPTANCE_DNS_JSON must be an object")
    sequences: dict[str, tuple[str, ...]] = {}
    for host, values in payload.items():
        if (not isinstance(host, str) or not host or host != host.casefold()
                or not isinstance(values, list) or not values):
            raise RuntimeError("acceptance DNS entries need lowercase hosts and non-empty lists")
        parsed_values: list[str] = []
        for value in values:
            if not isinstance(value, str):
                raise RuntimeError("acceptance DNS values must be literal IP strings")
            try:
                parsed_values.append(str(ipaddress.ip_address(value)))
            except ValueError as exc:
                raise RuntimeError("acceptance DNS values must be literal IP strings") from exc
        sequences[host] = tuple(parsed_values)
    return sequences


EXA_SEARCH_URL = _acceptance_lab_endpoint(
    "WEBRET_ACCEPTANCE_EXA_SEARCH_URL", "https://api.exa.ai/search")
EXA_CONTENTS_URL = _acceptance_lab_endpoint(
    "WEBRET_ACCEPTANCE_EXA_CONTENTS_URL", "https://api.exa.ai/contents")
FIRECRAWL_SCRAPE_URL = _acceptance_lab_endpoint(
    "WEBRET_ACCEPTANCE_FIRECRAWL_SCRAPE_URL", "https://api.firecrawl.dev/v2/scrape")
_ACCEPTANCE_DNS_SEQUENCES = _acceptance_dns_sequences()
_ACCEPTANCE_DNS_COUNTS: dict[str, int] = {}
_ACCEPTANCE_DNS_LOCK = threading.Lock()
FIRECRAWL_SEARCH_URL = _acceptance_lab_endpoint(
    "WEBRET_ACCEPTANCE_FIRECRAWL_SEARCH_URL", "https://api.firecrawl.dev/v2/search")
TAVILY_API_BASE = _acceptance_lab_endpoint(
    "WEBRET_ACCEPTANCE_TAVILY_API_BASE", "https://api.tavily.com")
FC_RESEARCH_BASE = "https://api.firecrawl.dev/v2/search/research"  # Firecrawl Research Index (papers + legacy github)
FC_DEVELOPER_URL = "https://api.firecrawl.dev/v2/search/developer"

# Firecrawl Developer Index request vocabulary.
DEV_TYPES = ("doc", "issue", "pull_request", "readme")
DEV_REPO_TYPES = ("issue", "pull_request", "readme")   # the half `repos` scopes
DEV_PASSAGES_DEFAULT = 2      # API default 1, max 5; 2 gives an agent enough to judge a hit
DEV_PASSAGES_MAX = 5
DEV_K_MAX = 100               # API bound; the tool clamps to 25 first
DEV_MAX_OUTPUT_CHARS = 24000  # total rendered-body cap (research_* bypass _finalize)

RETRYABLE_CODES = frozenset({429, 500, 502, 503, 504})
RETRY_FLOOR_S = 3          # don't start a retry with less than this much tier budget left

HTTP_TIMEOUT = 30          # per-call socket timeout (s)
TIER_TIMEOUT = 45          # per-tier wall-clock cap (s)
DEEP_HTTP_TIMEOUT = 110    # inner socket timeout for Exa deep-family search modes (s)
DEEP_TIER_TIMEOUT = 120    # outer wall-clock cap for deep-family search modes (s)
RESEARCH_HTTP_TIMEOUT = 60 # Research Index full-text passage reads run heavier than general API calls
RESEARCH_TIER_TIMEOUT = 75 # research wall-clock cap (s)

VALID_SEARCH_MODES = ("auto", "fast", "instant", "deep-lite", "deep", "deep-reasoning")
DEEP_SEARCH_MODES = ("deep-lite", "deep", "deep-reasoning")

# Exa /search `outputSchema` request field. When provided, the response gains an
# `output` object whose `content` matches this schema, with added synthesis latency.
# The API accepts outputSchema on every search type; deep-only is
# OUR LATENCY CHOICE, not an API restriction. We send it only when
# `mode in DEEP_SEARCH_MODES` (see _exa_search_sync) because only the deep family budgets
# for it (DEEP_HTTP_TIMEOUT/DEEP_TIER_TIMEOUT above); paying ~2s of synthesis on `instant`
# (~250ms) would defeat that mode's whole purpose. Revisit if the synthesis cost drops.
# The "text" root is LOAD-BEARING, not a default: SearchSynthesisOutputOutput.content
# is `oneOf[string, object]` — string when the root is "text", object when
# outputSchema's root is "object". `_render_search` requires
# `isinstance(content, str)` to emit the block, so switching this to an "object"
# root would make deep modes advertise a synthesized answer that is never rendered.
EXA_OUTPUT_SCHEMA = {"type": "text", "description": "A synthesized answer to the query, grounded in the search results."}

# Exa /search category migrations retained for backward-compatible caller input:
#   "research paper" -> "publication"   RENAMED. Both still return HTTP 200 and, on
#                                       an identical query, the identical top result
# so aliasing forward is behavior-preserving.
#   "pdf", "github"                     announced DEPRECATED but still HTTP 200 today,
#                                       and no replacement category was announced —
#                                       passed through with a stderr deprecation note.
#   "tweet"                             ALREADY REMOVED, not merely deprecated: Exa
#                                       returns HTTP 400 'The "tweet" category is no
#                                       longer supported.' Sending it is a guaranteed
#                                       failure, so it is dropped up front.
# Lookup is case-insensitive; anything unrecognized passes through verbatim (Exa
# accepts free-string hints). Recheck these when Exa's documented categories change.
EXA_CATEGORY_RENAMES = {"research paper": "publication"}
EXA_CATEGORIES_DEPRECATED = ("pdf", "github")
EXA_CATEGORIES_REMOVED = ("tweet",)
RENDER_TIMEOUT = 40        # camoufox navigation+render cap (s)
SEM_ACQUIRE_TIMEOUT = 60   # max wait for a render slot before its render budget starts
                           # Under HTTP this cap is process-global. A longer queue wait
                           # fails explicitly instead of risking memory oversubscription.
RENDER_CONCURRENCY = 2     # max simultaneous headless browsers
                           # Process-global under HTTP, process-local under stdio.
                           # Raise only with memory-headroom evidence — 2 browsers ≈ 0.4–1.5 GB each.
MIN_USEFUL_CHARS = 200     # below this a tier is "empty/boilerplate" -> next tier
MIN_EXTRACT_CHARS = 60     # concise/question outputs (summaries/answers) are short by
                           # design — a correct 120-char answer must NOT cascade-and-fail;
                           # empty/whitespace still cascades
HIGHLIGHT_BUDGET = 1000    # TOTAL highlight text per result (Exa /search semantics:
                           # highlights.maxCharacters is a per-URL budget, NOT per-highlight)

# Request query-relevant Firecrawl highlights explicitly rather than inheriting a
# provider default that may change.
FIRECRAWL_SEARCH_HIGHLIGHTS = True

# Exa /contents `maxAgeHours` documented range is -1 through 720. -1 means
# "always use cache"; values above 720 are clamped here
# rather than sent through (Exa's ceiling), with a caller-visible note.
EXA_MAX_AGE_HOURS_MAX = 720

_render_sem = asyncio.Semaphore(RENDER_CONCURRENCY)
# This is the server version, not the MCP SDK version.
mcp = MCPServer("web-retrieval", version=__version__)


class _FetchFailure:
    __slots__ = ("errors",)

    def __init__(self, errors: tuple[str, ...]) -> None:
        self.errors = errors


_completed_fetch_cache = CompletedFetchCache()
_fetch_singleflight: SingleFlight[tuple[FetchSuccess | _FetchFailure, bool]] = SingleFlight()


class RetrievalError(Exception):
    """Raised (never sys.exit) so a tool failure can't kill the server."""


_DISPLAY_CREDENTIAL_NAMES = frozenset({
    "signature", "sig", "access_token", "id_token", "refresh_token",
    "api_key", "apikey", "auth_token", "token", "jwt", "key", "secret",
    "password", "passwd", "credential", "client_secret", "private_key",
    "authorization", "code",
})
_DISPLAY_CREDENTIAL_PREFIXES = ("x-amz-", "x-goog-")


def _redact_url_pairs(component: str) -> str:
    """Redact recognized credentials in a query or query-shaped fragment."""
    tokens: list[str] = []
    for token in component.split("&") if component else []:
        raw_name, separator, _value = token.partition("=")
        # SPA fragments may look like ``/route?access_token=...``. Preserve the
        # route prefix while checking the actual parameter name.
        prefix, question, parameter = raw_name.rpartition("?")
        name_part = parameter if question else raw_name
        name = unquote(name_part).casefold()
        if (name in _DISPLAY_CREDENTIAL_NAMES
                or name.startswith(_DISPLAY_CREDENTIAL_PREFIXES)):
            kept_name = (prefix + question if question else "") + name_part
            tokens.append(kept_name + (separator or "=") + "***")
        else:
            tokens.append(token)
    return "&".join(tokens)


def _display_url(url: str) -> str:
    """Redact userinfo and recognized credential query values for output/logging."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if host is None:
            return "[invalid URL]"
        rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = rendered_host + (f":{port}" if port is not None else "")
        if parsed.username is not None or parsed.password is not None:
            netloc = "***@" + netloc
        return urlunsplit((
            parsed.scheme,
            netloc,
            parsed.path,
            _redact_url_pairs(parsed.query),
            _redact_url_pairs(parsed.fragment),
        ))
    except (TypeError, ValueError):
        return "[invalid URL]"


def _display_reference(value: str) -> str:
    """Redact URL-shaped references while preserving non-URL citation titles."""
    try:
        return _display_url(value) if urlsplit(value).hostname is not None else value
    except (TypeError, ValueError):
        return "[invalid URL]"


def _ip_forbidden(ip) -> bool:
    """True if an IP must never be reached from the local browser/render tier.
    is_global=False catches loopback/private/RFC-1918/link-local/unspecified;
    is_reserved catches reserved + NAT64; is_multicast is NOT covered by either
    (SSDP 239.255.255.250, 224.0.0.1, ff02::1 are is_global=True,
    is_reserved=False) — reject it explicitly."""
    return (not ip.is_global) or ip.is_reserved or ip.is_multicast


def _host_is_public(host: str) -> bool:
    """Resolve host; return True only if EVERY resolved IP is publicly routable.
    Resolution failure or any forbidden IP -> False (fail-closed). Used by the
    camoufox per-request route guard (/) to re-check redirects and
    subresources by RESOLVED IP — page.goto follows redirects internally, so
    Python never sees the intermediate hops the up-front check validated."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if _ip_forbidden(ip):
            return False
    return True


def _make_route_guard(loop, host_cache: dict):
    """Factory for the camoufox per-request SSRF guard (/). Returns an
    async Playwright route handler that ABORTS any request whose host resolves to
    a non-public IP — the redirect hops + subresources page.goto follows
    internally and Python never otherwise sees — classifying by RESOLVED IP.
    Per-render host cache; getaddrinfo runs off the event loop. A resolution
    failure fails closed (abort). An aborted main navigation propagates out and
    web_fetch cascades to the server-side Firecrawl tier — worst case is 'served
    by firecrawl', never a broken fetch. Used by every Camoufox attempt, whether
    selected automatically or forced with render='always'.
    Residual: DNS-rebind TOCTOU (Chromium re-resolves to connect) — accepted for
    a local single-user box; full closure needs a validating forward proxy."""
    async def _guard(route):
        try:
            host = urlparse(route.request.url).hostname
            if host is None:
                allowed = False
            elif host in host_cache:
                allowed = host_cache[host]
            else:
                allowed = await loop.run_in_executor(None, _host_is_public, host)
                host_cache[host] = allowed
        except Exception:
            allowed = False
        try:
            if allowed:
                await route.continue_()
            else:
                await route.abort()
        except Exception:
            pass  # request already handled or page closing — never crash render
    return _guard


def _make_request_observer(pending_docs: list, host_cache: dict, blocked: list):
    """Factory for the camoufox DOCUMENT-request observer (; was an inline
    closure). The sync callback only RECORDS (url, host) for document navigations —
    it does NOT resolve DNS on the event loop (getaddrinfo would block it). Actual
    resolution is deferred to _flush_pending, off-loop. Fail-closed unchanged: any
    exception building the record trips blocked['<resolve-error>'].

    Background: page.route does NOT fire on a main-frame HTTP redirect
    hop — the browser follows 3xx internally — so the route guard alone misses
    redirect-SSRF (public URL -> 302 -> 127.0.0.1/metadata). This observer sees the
    redirect hops; on a forbidden DOCUMENT navigation the render raises BEFORE any
    body is returned (web_fetch cascades to the SSRF-immune Firecrawl tier).
    Forbidden SUBRESOURCES are aborted individually by the route guard. Residual:
    the request is physically issued before teardown — accepted (local box; the
    exfil-to-caller path, which is what matters, is closed)."""
    def _on_request(req):
        try:
            if req.resource_type != "document":
                return
            host = urlparse(req.url).hostname
            if host is None:
                return
            pending_docs.append((req.url, host))
        except Exception:
            blocked.append("<resolve-error>")  # fail closed
    return _on_request


async def _flush_pending(loop, pending_docs: list, host_cache: dict, blocked: list) -> None:
    """Resolve any pending document hosts OFF the event loop (executor) and move
    forbidden URLs into `blocked`, in request order; per-host cached.
    Called at each pre-existing `if blocked:` check-point in _camoufox_render so
    the raise/not-raise decision is byte-identical to the pre- inline
    observer — only WHERE getaddrinfo runs moves (off the loop). Drains pending_docs."""
    while pending_docs:
        url, host = pending_docs.pop(0)
        allowed = host_cache.get(host)
        if allowed is None:
            allowed = await loop.run_in_executor(None, _host_is_public, host)
            host_cache[host] = allowed
        if not allowed:
            blocked.append(url)


def _validate_public_url(url: str) -> None:
    """SSRF guard. Reject non-http(s) schemes and any host that resolves to a
    non-public IP (loopback, private/RFC-1918, link-local, reserved). The local
    camoufox tier runs a real browser locally, so without this an agent
    — or injected page content steering web_fetch — could reach file://,
    localhost, or LAN/cloud-metadata endpoints. Validated up front so internal
    URLs never reach the external Exa/Firecrawl APIs either."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise RetrievalError(f"refused non-http(s) URL scheme: {parsed.scheme or '(none)'}")
    host = parsed.hostname
    if not host:
        raise RetrievalError("refused URL with no host")
    sequence = _ACCEPTANCE_DNS_SEQUENCES.get(host)
    if sequence is not None:
        with _ACCEPTANCE_DNS_LOCK:
            index = _ACCEPTANCE_DNS_COUNTS.get(host, 0)
            _ACCEPTANCE_DNS_COUNTS[host] = index + 1
        addresses = (sequence[min(index, len(sequence) - 1)],)
    else:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError as e:
            raise RetrievalError(f"cannot resolve host '{host}': {e}")
        addresses = tuple(info[4][0] for info in infos)
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if _ip_forbidden(ip):
            raise RetrievalError(f"refused non-public address for '{host}': {ip}")


async def _validate_public_url_async(url: str) -> str | None:
    """Run the fail-closed URL policy off-loop; return its public error text."""
    try:
        await asyncio.wait_for(
            anyio.to_thread.run_sync(_validate_public_url, url, abandon_on_cancel=True),
            timeout=TIER_TIMEOUT,
        )
    except (RetrievalError, asyncio.TimeoutError) as exc:
        return str(exc)
    return None


# ----------------------------------------------------------------------------- keys
def _looks_like_secret(v: str | None) -> bool:
    """True if v is a real secret, not an unexpanded ${...} config literal.
    Strips first so a whitespace-padded literal can't slip through."""
    if not v:
        return False
    s = v.strip()
    return bool(s) and not (s.startswith("${") and s.endswith("}"))


def _strict_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise RetrievalError(
        f"{name} must be one of 1/0, true/false, yes/no, or on/off; got {value!r}"
    )


def _tavily_fetch_enabled(override: bool | None) -> bool:
    if override is not None:
        return override
    return _strict_bool(
        os.environ.get("WEB_FETCH_TAVILY_TIER", "0"), name="WEB_FETCH_TAVILY_TIER"
    )


def _config_dir() -> Path:
    """Return the cross-platform per-user configuration directory."""
    override = os.environ.get("WEB_RETRIEVAL_MCP_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(base) / "web-retrieval-mcp"
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "web-retrieval-mcp"


def _key_file_path() -> Path:
    override = os.environ.get("WEB_RETRIEVAL_MCP_ENV_FILE")
    return Path(override).expanduser() if override else _config_dir() / "keys.env"


def _key_from_file(env_names: tuple[str, ...]) -> str | None:
    """Read a key from an optional dotenv-style file without loading it into env."""
    path = _key_file_path()
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if os.name != "nt":
            flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RetrievalError(f"credential path is not a regular file: {path}")
        if os.name != "nt" and metadata.st_mode & 0o077:
            raise RetrievalError(
                f"refusing credential file with group/other permissions: {path}; "
                "run chmod 600 on it"
            )
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            lines = stream.read().splitlines()
    except FileNotFoundError:
        return None
    except RetrievalError:
        raise
    except (OSError, UnicodeError) as exc:
        raise RetrievalError(f"cannot read credential file {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    wanted = set(env_names)
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() in wanted:
            value = value.strip().strip('"').strip("'")
            if _looks_like_secret(value):
                return value.strip()
    return None


def _key_from_keyring(service: str) -> str | None:
    """Read from the optional cross-platform ``keyring`` package."""
    try:
        import keyring  # noqa: PLC0415 - optional dependency
    except ImportError:
        return None
    try:
        value = keyring.get_password("web-retrieval-mcp", service)
    except Exception:  # a broken/locked backend must not crash the server
        return None
    return value.strip() if _looks_like_secret(value) else None


def _key_from_os_cli(service: str) -> str | None:
    """Read from macOS Keychain or Linux Secret Service when available."""
    if sys.platform == "darwin":
        command = [
            "security", "find-generic-password", "-s", "web-retrieval-mcp",
            "-a", service, "-w",
        ]
    elif sys.platform.startswith("linux") and shutil.which("secret-tool"):
        command = [
            "secret-tool", "lookup", "service", "web-retrieval-mcp", "key", service,
        ]
    else:
        return None
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip() if completed.returncode == 0 else ""
    return value if _looks_like_secret(value) else None


def _get_key(*, env_names: tuple[str, ...], service: str) -> str:
    """Resolve a credential from env, key file, keyring, or an OS secret store."""
    for name in env_names:
        value = os.environ.get(name)
        if _looks_like_secret(value):
            return value.strip()
    for resolver in (
        lambda: _key_from_file(env_names),
        lambda: _key_from_keyring(service),
        lambda: _key_from_os_cli(service),
    ):
        value = resolver()
        if _looks_like_secret(value):
            return value.strip()
    names = ", ".join(env_names)
    raise RetrievalError(
        f"no usable credential for {service}; set {names}, add it to {_key_file_path()}, "
        "or configure the optional keyring/OS secret store"
    )


def _scrub(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***REDACTED***")
    return text


# ----------------------------------------------------------------------------- http
def _tier_deadline(budget: int) -> float:
    """Monotonic wall-clock deadline for a tier call, computed at call entry — the
    point after which the outer asyncio.wait_for has given up. Passed into the http
    helpers so a retry cannot outlive its tier's budget."""
    return time.monotonic() + budget


# The deadline is set by the ASYNC caller in the task context IMMEDIATELY before
# anyio.to_thread.run_sync (anyio copies the context to the worker thread — verified).
# The worker reads it via _effective_deadline so the deadline is anchored to the
# pre-thread-pool-queue moment: computing it INSIDE the worker would exclude queue
# time and let an abandoned worker's retry run past the outer wait. When unset
# (for example, in direct tests), compute from the budget as a fallback.
_TIER_DEADLINE_VAR: contextvars.ContextVar = contextvars.ContextVar("tier_deadline", default=None)


def _effective_deadline(budget: int) -> float:
    dl = _TIER_DEADLINE_VAR.get()
    return dl if dl is not None else _tier_deadline(budget)


def _retry_ok(deadline: float | None) -> bool:
    """True when a transient retry may fire: unbounded (no deadline) or more than
    RETRY_FLOOR_S of tier budget remains. abandon_on_cancel=True means the outer cap
    does NOT stop this worker thread, so a retry started past the deadline would
    issue an invisible post-cap request; this guard prevents that."""
    return deadline is None or (deadline - time.monotonic()) > RETRY_FLOOR_S


def _http_json(method: str, url: str, headers: dict, secret: str, timeout: int,
               deadline: float | None, data: bytes | None = None) -> dict:
    """Shared blocking JSON request with ONE deadline-aware transient retry.
    Retries once on 429/5xx, URLError, or socket timeout — but only when
    _retry_ok(deadline); the retry's socket timeout is capped to the remaining
    budget. Non-transient HTTP codes (4xx) raise immediately."""
    def _build() -> urllib.request.Request:
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)
        return req

    attempt = 0
    while True:
        sock_timeout = timeout
        if deadline is not None:
            # cap EVERY attempt (not just the retry) to the remaining tier budget, and
            # abort if it is already exhausted — a worker that sat queued past its
            # deadline must not start a fresh full-timeout request the caller has given
            # up on. On the normal unqueued path, remaining is approximately the
            # full budget.
            remaining = deadline - time.monotonic()
            if remaining < 1:
                # <1s left: even a 1s-timeout request would run past the deadline, so
                # abort rather than issue a post-cap request.
                raise RetrievalError(_scrub(f"tier budget exhausted before request to {url}", secret))
            sock_timeout = min(timeout, int(remaining))
        try:
            with urllib.request.urlopen(_build(), timeout=sock_timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500] if e.fp else ""
            if attempt == 0 and e.code in RETRYABLE_CODES and _retry_ok(deadline):
                attempt += 1
                time.sleep(0.5 + random.random() * 1.0)
                continue
            raise RetrievalError(_scrub(f"HTTP {e.code} from {url}: {detail}", secret))
        except (urllib.error.URLError, OSError) as e:
            if attempt == 0 and _retry_ok(deadline):
                attempt += 1
                time.sleep(0.5 + random.random() * 1.0)
                continue
            raise RetrievalError(_scrub(f"network error calling {url}: {e}", secret))
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise RetrievalError(_scrub(f"non-JSON response from {url}: {body[:300]}", secret))


def _post_json(url: str, payload: dict, headers: dict, secret: str,
               timeout: int = HTTP_TIMEOUT, deadline: float | None = None) -> dict:
    """Blocking in-process JSON POST. Runs in a worker thread (see callers).
    `timeout` mirrors _get_json — deep-family Exa search modes pass a longer socket
    timeout (a hardcoded 30s here would abort any deep response and make an
    outer-only tier cap a no-op). `deadline` bounds the retry."""
    data = json.dumps(payload).encode("utf-8")
    return _http_json("POST", url, headers, secret, timeout, deadline, data=data)


def _get_json(url: str, headers: dict, secret: str, timeout: int = HTTP_TIMEOUT,
              deadline: float | None = None) -> dict:
    """Blocking in-process JSON GET (the Research Index endpoints are GET). Runs in a
    worker thread (see callers). Mirrors _post_json error handling + secret scrubbing.
    The query string must already be encoded into `url`."""
    return _http_json("GET", url, headers, secret, timeout, deadline)


def _exa_key() -> str:
    return _get_key(env_names=("EXA_API_KEY",), service="EXA_API_KEY")


def _firecrawl_key() -> str:
    return _get_key(env_names=("FIRECRAWL_API_KEY",), service="FIRECRAWL_API_KEY")


def _tavily_key() -> str:
    return _get_key(env_names=("TAVILY_API_KEY",), service="TAVILY_API_KEY")


def _firecrawl_key_optional() -> str:
    """Like _firecrawl_key but returns "" instead of raising when no key resolves.
    The Research Index works keyless on Firecrawl's free tier, so a locked login
    Keychain (headless/cron) degrades to keyless rather than failing the call."""
    try:
        return _firecrawl_key()
    except RetrievalError:
        return ""


def _fc_research_headers() -> tuple[dict, str]:
    """(headers, secret-for-scrub). Sends Bearer when a key is available, else keyless."""
    key = _firecrawl_key_optional()
    return ({"Authorization": f"Bearer {key}"} if key else {}), key


# --------------------------------------------------------------------------- tiers (blocking)
def _parse_date(s: str) -> date | None:
    """Parse a user-supplied ISO date OR full ISO timestamp, rejecting impossible
    dates (2026-13-45) AND junk suffixes (2026-01-15garbage) — datetime.fromisoformat
    validates the WHOLE string (a [:10] slice would silently accept a trailing junk
    suffix, the exact hole a regex prefix check has). Returns a date, or None."""
    try:
        return datetime.fromisoformat(str(s).strip()).date()
    except (ValueError, TypeError):
        return None


def _build_search_filters(recency_days: int | None = None,
                          recency_hours: int | None = None,
                          start_published_date: str | None = None,
                          end_published_date: str | None = None,
                          category: str | None = None,
                          include_domains: list[str] | None = None,
                          exclude_domains: list[str] | None = None,
                          notices: list[str] | None = None) -> dict:
    """Translate web_search freshness/domain/category params into Exa /search
    TOP-LEVEL filter fields. Invalid or conflicting input is DROPPED with
    a stderr note, never raised. Dates emit full ISO date-time (schema format:
    date-time). company/people categories forbid date filters + excludeDomains
    (documented 400) — dropped here; `people` KEEPS include_domains (dropping would
    silently broaden the query; Exa 400s unless they are its supported profile
    domains — a docstring caveat, not something we can validate).

    `notices` is optional: an
    optional list the CATEGORY-migration messages are appended to, so web_search can
    disclose them to the caller in the rendered header instead of only on stderr.
    Scoped deliberately to the category enum: a renamed or removed category silently
    CHANGES THE SCOPE of the results an agent gets back, which the agent cannot infer
    from the output. The other filter drops stay stderr-only, as before.

    `recency_hours` provides hour-granularity recency, expressed
    on Exa as a full ISO timestamp (not the midnight-anchored date recency_days
    emits) since hour granularity is the whole point of the parameter. Precedence:
    explicit valid start_published_date > recency_hours > recency_days — an invalid
    or absent value at each tier falls through to the next, same as an unparseable
    start_published_date already fell through to recency_days."""
    filters: dict = {}

    def note(msg: str) -> None:
        print(f"web_search filters: {msg}", file=sys.stderr)

    def notice(msg: str) -> None:
        """stderr note AND a caller-visible notice ( category migration)."""
        note(msg)
        if notices is not None:
            notices.append(msg)

    # Three-tier freshness precedence: explicit valid start_published_date wins over
    # recency_hours wins over recency_days. An invalid value at any tier is treated
    # as absent so the next tier still applies — same fall-through the original
    # unparseable-start-date case already relied on.
    start_d = _parse_date(start_published_date) if start_published_date else None
    if start_published_date and start_d is None:
        note(f"ignoring unparseable start_published_date {start_published_date!r}")
    if start_d is not None:
        filters["startPublishedDate"] = f"{start_d.isoformat()}T00:00:00.000Z"
        if recency_hours is not None:
            note("start_published_date set — ignoring recency_hours")
        if recency_days is not None:
            note("start_published_date set — ignoring recency_days")
    else:
        hours_valid = False
        if recency_hours is not None:
            if recency_hours <= 0:
                note(f"ignoring nonpositive recency_hours {recency_hours}")
            elif recency_hours > 876_000:
                # >100 years in hours — mirrors the recency_days absurd-value guard
                # below; a huge int overflows timedelta() (uncaught). Ignore.
                note(f"ignoring absurd recency_hours {recency_hours} (>100y)")
            else:
                hours_valid = True
                if recency_days is not None:
                    note("recency_hours set — ignoring recency_days")
                dt = datetime.now(timezone.utc) - timedelta(hours=recency_hours)
                # strftime, NOT dt.isoformat(): an aware isoformat() emits a
                # "+00:00" offset, and appending the neighbouring "Z" below would
                # produce "...+00:00Z", which Exa rejects. strftime reproduces the
                # exact shape of the existing f"{d.isoformat()}T00:00:00.000Z"
                # string the day-granularity branch below emits.
                filters["startPublishedDate"] = dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if not hours_valid and recency_days is not None:
            if recency_days <= 0:
                note(f"ignoring nonpositive recency_days {recency_days}")
            elif recency_days > 36500:
                # >100 years — effectively no recency filter, and a huge int overflows
                # timedelta() outside web_search's handler. Ignore it safely.
                note(f"ignoring absurd recency_days {recency_days} (>100y)")
            else:
                d = (datetime.now(timezone.utc) - timedelta(days=recency_days)).date()
                filters["startPublishedDate"] = f"{d.isoformat()}T00:00:00.000Z"

    # end date
    if end_published_date:
        d = _parse_date(end_published_date)
        if d is None:
            note(f"ignoring unparseable end_published_date {end_published_date!r}")
        else:
            filters["endPublishedDate"] = f"{d.isoformat()}T23:59:59.999Z"

    # Reversed bounds produce an Exa 400, so keep the start and drop the end.
    # ISO date strings compare lexicographically == chronologically; handles both an
    # explicit start and a recency-derived start.
    if ("startPublishedDate" in filters and "endPublishedDate" in filters
            and filters["startPublishedDate"][:10] > filters["endPublishedDate"][:10]):
        note(f"start {filters['startPublishedDate'][:10]} is after end "
             f"{filters['endPublishedDate'][:10]} — dropping end bound")
        filters.pop("endPublishedDate", None)

    # category enum migration — must run before the conflict guard below so
    # the guard sees the post-rename value. Unrecognized hints pass through verbatim.
    if category:
        key = category.strip().lower()
        if key in EXA_CATEGORIES_REMOVED:
            notice(f"category={category!r} was REMOVED by Exa (it 400s) — dropped; "
                   f"this search is NOT scoped to that category")
            category = None
        elif key in EXA_CATEGORY_RENAMES:
            renamed = EXA_CATEGORY_RENAMES[key]
            notice(f"category={category!r} was renamed by Exa -> {renamed!r} (applied)")
            category = renamed
        elif key in EXA_CATEGORIES_DEPRECATED:
            notice(f"category={category!r} is deprecated by Exa (still accepted today; "
                   f"no replacement announced) — passed through")

    # category + conflict guard (must run before the domains block below)
    if category:
        filters["category"] = category           # any non-empty string (Exa accepts free hints)
        if category in ("company", "people"):
            dropped = [k for k in ("startPublishedDate", "endPublishedDate") if k in filters]
            for k in dropped:
                del filters[k]
            if dropped:
                note(f"category={category!r} forbids date filters — dropped {dropped}")
            if exclude_domains:
                note(f"category={category!r} forbids excludeDomains — dropped")
                exclude_domains = None

    # domains (defensive cap 20; API allows up to 1200)
    def _cap(dl: list[str], label: str) -> list[str]:
        if len(dl) > 20:
            note(f"{label} capped to 20 (had {len(dl)})")
            return dl[:20]
        return dl
    if include_domains:
        filters["includeDomains"] = _cap(list(include_domains), "include_domains")
    if exclude_domains:
        filters["excludeDomains"] = _cap(list(exclude_domains), "exclude_domains")

    return filters


def _exa_search_sync(query: str, num_results: int, mode: str, text_chars: int = 1200,
                     filters: dict | None = None, summary: bool = False) -> dict:
    """Exa /search. Returns the FULL response dict (public response contract) so `web_search`
    reads `output` without a return-shape refactor; `web_search` extracts `results`.
    `text_chars` = request-what-you-render (was a fixed 4000); highlights.maxCharacters
    is a per-URL TOTAL budget (verified Exa semantics). `filters` = top-level
    freshness/domain/category fields. `summary` requests ONLY a summary
    (text + highlights omitted — ContentsOptions are independent siblings; requesting
    text you won't render is waste). : deep-family modes additionally send
    `outputSchema` (EXA_OUTPUT_SCHEMA) so the response's `output.content` is populated —
    see that constant for why scoping is deep-only and why the root must stay "text"."""
    key = _exa_key()
    if summary:
        contents: dict = {"summary": {}}
    else:
        contents = {
            "text": {"maxCharacters": text_chars},
            "highlights": {"maxCharacters": HIGHLIGHT_BUDGET},
        }
    payload = {"query": query, "type": mode, "numResults": num_results, "contents": contents}
    if filters:
        payload.update(filters)
    # deep-family modes are slow (deep-reasoning 12–40s) — a 30s socket timeout would
    # abort them; pass the longer inner timeout so the outer tier cap actually bounds.
    deep = mode in DEEP_SEARCH_MODES
    timeout = DEEP_HTTP_TIMEOUT if deep else HTTP_TIMEOUT
    deadline = _effective_deadline(DEEP_TIER_TIMEOUT if deep else TIER_TIMEOUT)
    # AFTER the filters update, not before: anything assigned earlier is exactly what
    # a future filter key could shadow.
    if deep:
        payload["outputSchema"] = EXA_OUTPUT_SCHEMA
    return _post_json(EXA_SEARCH_URL, payload, {"x-api-key": key}, key,
                      timeout=timeout, deadline=deadline)


def _tavily_search_options(
    *,
    mode: str,
    recency_days: int | None,
    recency_hours: int | None,
    start_published_date: str | None,
    end_published_date: str | None,
    category: str | None,
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
) -> tuple[dict, list[str]]:
    """Map shared search controls to Tavily without claiming false equivalence."""
    notices: list[str] = []
    depth = {
        "instant": "ultra-fast",
        "fast": "fast",
        "auto": "advanced",
        "deep-lite": "advanced",
        "deep": "advanced",
        "deep-reasoning": "advanced",
    }.get(mode, "advanced")
    options: dict = {"search_depth": depth}
    if depth != mode:
        notices.append(f"mode={mode!r} mapped to Tavily search_depth={depth!r}")

    start = _parse_date(start_published_date) if start_published_date else None
    end = _parse_date(end_published_date) if end_published_date else None
    if start_published_date and start is None:
        notices.append("invalid start_published_date was ignored")
    if end_published_date and end is None:
        notices.append("invalid end_published_date was ignored")
    if start is not None:
        options["start_date"] = start.isoformat()
    else:
        if recency_hours and 0 < recency_hours <= 876_000:
            # Tavily exposes date rather than hour bounds. Round outward so a
            # caller's requested window is never accidentally narrowed.
            days = max(1, (recency_hours + 23) // 24)
            options["start_date"] = (datetime.now(timezone.utc).date()
                                     - timedelta(days=days)).isoformat()
            notices.append(
                f"recency_hours={recency_hours} mapped to a {days}-day Tavily date window"
            )
        else:
            if recency_hours and recency_hours > 876_000:
                notices.append(
                    "recency_hours exceeds the supported 100-year bound and was ignored"
                )
            if recency_days and 0 < recency_days <= 36_500:
                options["start_date"] = (datetime.now(timezone.utc).date()
                                         - timedelta(days=recency_days)).isoformat()
            elif recency_days and recency_days > 36_500:
                notices.append(
                    "recency_days exceeds the supported 100-year bound and was ignored"
                )
    if end is not None:
        if "start_date" not in options or end.isoformat() >= options["start_date"]:
            options["end_date"] = end.isoformat()
        else:
            notices.append("end_published_date precedes the effective start date and was dropped")

    if category == "news":
        options["topic"] = "news"
    elif category:
        notices.append(f"category={category!r} has no exact Tavily equivalent and was dropped")
    if include_domains:
        options["include_domains"] = list(include_domains[:20])
        if len(include_domains) > 20:
            notices.append("include_domains was capped at 20 entries")
    if exclude_domains:
        options["exclude_domains"] = list(exclude_domains[:20])
        if len(exclude_domains) > 20:
            notices.append("exclude_domains was capped at 20 entries")
    return options, notices


def _tavily_search_sync(query: str, num_results: int, options: dict) -> dict:
    """Call Tavily Search and map its results to the common renderer shape."""
    try:
        from tavily import TavilyClient  # noqa: PLC0415 - optional dependency
    except ImportError as exc:
        raise RetrievalError(
            "Tavily is selected but its SDK is not installed; "
            "install web-retrieval-mcp[tavily]"
        ) from exc

    key = _tavily_key()
    try:
        with TavilyClient(
            api_key=key,
            api_base_url=TAVILY_API_BASE,
            client_source="web-retrieval-mcp",
        ) as client:
            response = client.search(
                query=query,
                max_results=num_results,
                timeout=HTTP_TIMEOUT,
                **options,
            )
    except Exception as exc:  # SDK publishes several provider-specific exception types
        raise RetrievalError(f"Tavily search failed ({exc.__class__.__name__})") from exc

    mapped: list[dict] = []
    for result in response.get("results") or []:
        if not isinstance(result, dict):
            continue
        mapped.append({
            "title": result.get("title") or "",
            "url": result.get("url") or "",
            "publishedDate": result.get("published_date") or "",
            "highlights": [],
            "text": result.get("content") or "",
        })
    return {"results": mapped}


def _exa_contents_sync(url: str, max_chars: int, max_age_hours: int | None = None,
                       mode: str = "full", question: str | None = None
                       ) -> tuple[str, str | None]:
    """Exa /contents. Returns (text, source) where source is the statuses[] source
    ('cached'|'crawled', or None when absent).

    On /contents, text/summary/highlights are TOP-LEVEL request fields (they nest
    under `contents` only on /search). Request-what-you-render:
      - question -> summary:{"query": question}, read results[0].summary (the answer)
      - mode=="concise" -> summary:{}, read results[0].summary
      - else (full) -> text:{maxCharacters}, read results[0].text  (byte-identical
        to the pre- default path)."""
    key = _exa_key()
    payload: dict = {"urls": [url]}
    if question is not None:
        payload["summary"] = {"query": question}
    elif mode == "concise":
        payload["summary"] = {}
    else:
        # Exa /contents declares text.maxCharacters maximum:10000. Cap the request
        # there so a future strict-validation change cannot reject it. The
        # caller's full max_chars still bounds the tier OUTPUT (camoufox/Firecrawl
        # tiers can return more).
        payload["text"] = {"maxCharacters": min(max_chars, 10000)}
    # maxAgeHours = Exa's freshness control (replaces deprecated livecrawl).
    # Omit when None to preserve Exa's default caching.
    if max_age_hours is not None:
        payload["maxAgeHours"] = max_age_hours
    resp = _post_json(EXA_CONTENTS_URL, payload, {"x-api-key": key}, key,
                      deadline=_effective_deadline(TIER_TIMEOUT))
    results = resp.get("results") or []
    statuses = resp.get("statuses") or []
    # single-URL request -> statuses[0] is this URL's entry.
    st0 = statuses[0] if statuses and isinstance(statuses[0], dict) else None
    #  a non-success status is the real cause — raise the error tag/status ONLY
    # (no "exa:" prefix; web_fetch's trail already formats `exa: {e}`, so a prefixed
    # raise would render "exa: exa: …") so the trail shows it instead of "thin (0 chars)".
    if st0 is not None:
        status = st0.get("status")
        if status and status != "success":
            err = st0.get("error") if isinstance(st0.get("error"), dict) else {}
            raise RetrievalError(str(err.get("tag") or status))
    source = st0.get("source") if st0 is not None else None
    if not results:
        return ("", source)
    r0 = results[0]
    if question is not None or mode == "concise":
        return ((r0.get("summary") or "").strip(), source)
    return ((r0.get("text") or "").strip(), source)


def _firecrawl_sync(url: str, mode: str = "full", question: str | None = None,
                    max_age_hours: int | None = None) -> str:
    """Firecrawl v2 /scrape. Request-what-you-render:
      - question -> formats:[{"type":"question","question":…}] (both keys required),
        read data.answer (Firecrawl's summary format has NO query option, so
        question is the only grounded-extraction route)
      - mode=="concise" -> formats:["summary"] (bare string sanctioned), read data.summary
      - else (full) -> formats:["markdown"], read data.markdown
    onlyMainContent:True is the v2 DEFAULT — set explicitly as a self-documenting
    drift-pin. Short-output floors: a concise SUMMARY uses MIN_EXTRACT_CHARS (60);
    a grounded ANSWER uses 1, so only an empty answer cascades; a full
    markdown body uses MIN_USEFUL_CHARS (200).
    max_age_hours -> maxAge (ms; 0 forces fresh). None omits it, leaving
    Firecrawl's own default cache window (~2 days) in force. : Firecrawl
    has NO "-1 = always use cache" equivalent (that is an Exa-only value), so -1 is
    also treated as omit-maxAge here — an asymmetry disclosed in the caller-facing
    cache note, not silently sent as a nonsensical negative maxAge."""
    key = _firecrawl_key()
    if question is not None:
        formats: list = [{"type": "question", "question": question}]
    elif mode == "concise":
        formats = ["summary"]
    else:
        formats = ["markdown"]
    payload = {"url": url, "formats": formats, "onlyMainContent": True}
    if max_age_hours is not None and max_age_hours >= 0:
        payload["maxAge"] = max_age_hours * 3_600_000   # hours -> ms; 0 => force fresh
    resp = _post_json(FIRECRAWL_SCRAPE_URL, payload, {"Authorization": f"Bearer {key}"}, key,
                      deadline=_effective_deadline(TIER_TIMEOUT))
    if not resp.get("success"):
        raise RetrievalError(_scrub(f"firecrawl success=false: {json.dumps(resp)[:300]}", key))
    data = resp.get("data") or {}
    meta = data.get("metadata") or {}
    status = meta.get("statusCode")
    if status is not None:
        code = int(status) if str(status).isdigit() else 0
        if not (200 <= code < 300):
            raise RetrievalError(
                _scrub(f"firecrawl upstream status {status} for {_display_url(url)}", key)
            )
    if meta.get("error"):
        raise RetrievalError(
            _scrub(f"firecrawl error for {_display_url(url)}: {meta.get('error')}", key)
        )
    if question is not None:
        # a grounded ANSWER can be legitimately terse ("Paris", "No") — only
        # empty/whitespace should cascade; floor = 1.
        body = (data.get("answer") or "").strip()
        floor, label = 1, "answer"
    elif mode == "concise":
        body = (data.get("summary") or "").strip()
        floor, label = MIN_EXTRACT_CHARS, "summary"
    else:
        body = (data.get("markdown") or "").strip()
        floor, label = MIN_USEFUL_CHARS, "markdown"
    if len(body) < floor:
        raise RetrievalError(
            f"firecrawl {label} too short ({len(body)} chars) for {_display_url(url)}"
        )
    return body


def _tavily_extract_sync(url: str) -> str:
    """Fetch one URL with Tavily Extract's basic Markdown mode."""
    try:
        from tavily import TavilyClient  # noqa: PLC0415 - optional dependency
    except ImportError as exc:
        raise RetrievalError(
            "Tavily Extract is enabled but its SDK is not installed; "
            "install web-retrieval-mcp[tavily]"
        ) from exc

    key = _tavily_key()
    try:
        with TavilyClient(
            api_key=key,
            api_base_url=TAVILY_API_BASE,
            client_source="web-retrieval-mcp",
        ) as client:
            response = client.extract(
                urls=[url],
                extract_depth="basic",
                format="markdown",
                timeout=HTTP_TIMEOUT,
            )
    except Exception as exc:
        raise RetrievalError(f"Tavily Extract failed ({exc.__class__.__name__})") from exc

    results = response.get("results") or []
    if not results:
        failures = response.get("failed_results") or []
        detail = "no results"
        if failures and isinstance(failures[0], dict):
            detail = str(failures[0].get("error") or failures[0].get("message") or detail)
        raise RetrievalError(
            _scrub(f"Tavily Extract returned {detail} for {_display_url(url)}", key)
        )
    first = results[0] if isinstance(results[0], dict) else {}
    body = (first.get("raw_content") or first.get("content") or "").strip()
    if len(body) < MIN_USEFUL_CHARS:
        raise RetrievalError(
            f"Tavily Extract content too short ({len(body)} chars) for {_display_url(url)}"
        )
    return body


def _recency_hours_to_tbs(hours: int | None) -> str | None:
    """Map recency hours to the nearest Firecrawl ``tbs`` range bucket.

    1 d=24h->qdr:d, 5 d=120h->qdr:w, 7 d=168h->qdr:w, 31 d=744h->qdr:m,
    365 d=8760h->qdr:y. Above 8760h (1y), a documented Firecrawl custom-date-range
    clause (cdr:1,cd_min:…) REPLACES what the old function did (silently drop it):
    qdr:y no longer has to stand in for "older than a year". A truly absurd value
    (>876,000h / 100y) still degrades to None rather than risk a timedelta overflow,
    mirroring _build_search_filters's own absurd-value guard."""
    if not hours or hours <= 0:
        return None
    if hours <= 1:
        return "qdr:h"
    if hours <= 24:
        return "qdr:d"
    if hours <= 168:
        return "qdr:w"
    if hours <= 744:
        return "qdr:m"
    if hours <= 8760:
        return "qdr:y"
    if hours > 876_000:
        return None
    start_d = (datetime.now(timezone.utc) - timedelta(hours=hours)).date()
    return _dates_to_tbs(start_d, None)


def _dates_to_tbs(start_d: date | None, end_d: date | None) -> str | None:
    """Build Firecrawl's documented custom-date-range `tbs` clause
    (cdr:1,cd_min:MM/DD/YYYY[,cd_max:MM/DD/YYYY]) from already-PARSED date objects
    (from _parse_date, which returns a date — not raw caller strings). Returns None
    when start_d is None: the documented `cdr:1` form always carries a `cd_min`, and
    a cd_max-only range is not a documented shape — the caller records a `partial`
    notice instead of inventing syntax."""
    if start_d is None:
        return None
    tbs = f"cdr:1,cd_min:{start_d.strftime('%m/%d/%Y')}"
    if end_d is not None:
        tbs += f",cd_max:{end_d.strftime('%m/%d/%Y')}"
    return tbs


def _compose_tbs(sort_by_date: bool, range_clause: str | None) -> str | None:
    """Join Firecrawl's documented `tbs` directives in their documented order —
    sbd:1 first, then the range clause, comma-separated (for example,
    "sbd:1,qdr:w"). None when both are absent."""
    parts: list[str] = []
    if sort_by_date:
        parts.append("sbd:1")
    if range_clause:
        parts.append(range_clause)
    return ",".join(parts) if parts else None


def _firecrawl_search_sync(query: str, num_results: int,
                           include_domains: list[str] | None = None,
                           exclude_domains: list[str] | None = None,
                           tbs: str | None = None) -> list[dict]:
    """Firecrawl v2 /search — the web_search fallback tier when Exa is down.
    includeDomains/excludeDomains are MUTUALLY EXCLUSIVE here; the caller sends at
    most one. Fail-closed on the envelope: a schema-valid success:false raises rather
    than rendering 'No results'. Maps data.web[] -> the _render_search result shape
    (description -> text/highlights — : Firecrawl flipped `highlights` to
    default true on 2026-07-22; we now send it explicitly (FIRECRAWL_SEARCH_HIGHLIGHTS)
    so `description` is query-relevant highlight text, not a plain page description).
    : that highlight text may contain MARKDOWN and is passed through unsanitized;
    `_render_search` clips it at `text_chars`, so it can be cut mid-structure."""
    key = _firecrawl_key()
    q = query
    if len(q) > 500:
        print(f"web_search fallback: query truncated to 500 chars (was {len(q)})", file=sys.stderr)
        q = q[:500]
    payload: dict = {"query": q, "limit": num_results,
                     "highlights": FIRECRAWL_SEARCH_HIGHLIGHTS}  # sources omitted (defaults ["web"])
    if include_domains:
        payload["includeDomains"] = list(include_domains)[:20]
    elif exclude_domains:
        payload["excludeDomains"] = list(exclude_domains)[:20]
    if tbs:
        payload["tbs"] = tbs
    resp = _post_json(FIRECRAWL_SEARCH_URL, payload, {"Authorization": f"Bearer {key}"}, key,
                      deadline=_effective_deadline(TIER_TIMEOUT))
    if not resp.get("success"):
        raise RetrievalError(_scrub(f"firecrawl search success=false: {json.dumps(resp)[:300]}", key))
    web = (resp.get("data") or {}).get("web") or []
    return [{"title": r.get("title") or "(untitled)", "url": r.get("url") or "",
             "text": r.get("description") or ""}
            for r in web if isinstance(r, dict)]


# --------------------------------------------------------------------------- Firecrawl Research Index (blocking)
def _fc_envelope(resp, url: str, key: str) -> dict:
    """Fail-closed envelope check shared by every Firecrawl research/developer call.

    : these helpers used to end `return resp.get("results") or`, which turns
    THREE distinct upstream failures into a silent empty result — and a silent empty
    result is indistinguishable from a healthy 'nothing matched'. That matters most on
    research_github, where an undetectable legacy failure would make the both-tiers-fail
    error string unreachable, but the same shape was live on papers/similar/paper too.
    `_http_json` returns `json.loads(body)` unvalidated, so a 200 carrying a bare list
    would reach `.get()` and raise AttributeError — which is NOT RetrievalError and so
    escapes every caller's except clause."""
    if not isinstance(resp, dict):
        raise RetrievalError(_scrub(f"non-object response from {url}: {str(resp)[:200]}", key))
    if not resp.get("success"):
        raise RetrievalError(_scrub(f"success=false from {url}: {json.dumps(resp)[:300]}", key))
    return resp


def _fc_results(resp, url: str, key: str) -> list:
    """_fc_envelope + `results` must be a list, and a non-empty list must hold at least
    one object. A `results: [null]` is non-empty (so it escapes the empty-result path)
    yet every entry is discarded by the renderers -> a header-only 'success' that reads
    exactly like a real one. Treat it as malformed instead."""
    results = _fc_envelope(resp, url, key).get("results")
    if results is None:
        return []
    if not isinstance(results, list):
        raise RetrievalError(_scrub(f"non-list results from {url}: {str(results)[:200]}", key))
    if results and not any(isinstance(r, dict) for r in results):
        raise RetrievalError(_scrub(f"malformed results from {url}: no object entries", key))
    return results


def _research_papers_sync(query: str, k: int) -> list[dict]:
    headers, key = _fc_research_headers()
    url = f"{FC_RESEARCH_BASE}/papers?{urlencode({'query': query, 'k': k})}"
    resp = _get_json(url, headers, key, timeout=RESEARCH_HTTP_TIMEOUT,
                     deadline=_effective_deadline(RESEARCH_TIER_TIMEOUT))
    return _fc_results(resp, url, key)


def _research_paper_sync(paper_id: str, query: str | None) -> dict:
    headers, key = _fc_research_headers()
    pid = quote(paper_id, safe="")  # encode the ':' in "arxiv:2606.01509"
    qs = f"?{urlencode({'query': query})}" if query else ""
    url = f"{FC_RESEARCH_BASE}/papers/{pid}{qs}"
    resp = _get_json(url, headers, key, timeout=RESEARCH_HTTP_TIMEOUT,
                     deadline=_effective_deadline(RESEARCH_TIER_TIMEOUT))
    return _fc_envelope(resp, url, key)   # success:false used to render as "# (untitled)"


def _research_similar_sync(paper_id: str, intent: str, k: int = 8,
                           mode: str = "similar", rerank: bool | None = None) -> list[dict]:
    headers, key = _fc_research_headers()
    pid = quote(paper_id, safe="")
    params = {"intent": intent, "k": k, "mode": mode}
    if rerank is not None:
        params["rerank"] = "true" if rerank else "false"  # API default undocumented — omit unless set
    url = f"{FC_RESEARCH_BASE}/papers/{pid}/similar?{urlencode(params)}"
    resp = _get_json(url, headers, key, timeout=RESEARCH_HTTP_TIMEOUT,
                     deadline=_effective_deadline(RESEARCH_TIER_TIMEOUT))
    return _fc_results(resp, url, key)


def _research_github_sync(query: str, k: int) -> list[dict]:
    """Legacy failure-only tier; the Developer Index is the supported path."""
    headers, key = _fc_research_headers()
    url = f"{FC_RESEARCH_BASE}/github?{urlencode({'query': query, 'k': k})}"
    resp = _get_json(url, headers, key, timeout=RESEARCH_HTTP_TIMEOUT,
                     deadline=_effective_deadline(RESEARCH_TIER_TIMEOUT))
    return _fc_results(resp, url, key)


def _developer_search_sync(query: str, k: int, passages: int = DEV_PASSAGES_DEFAULT,
                           types: list[str] | None = None,
                           repos: list[str] | None = None) -> dict:
    """Call the Firecrawl Developer Index with array filters in a JSON POST.

    Returns the WHOLE envelope (`results` + `coverage` + `reranked`), not just results —
    `coverage` is what distinguishes 'the index could not answer' from 'nothing matched',
    and the caller needs that to decide whether the legacy fallback is warranted.

    `timeout` is passed EXPLICITLY: _post_json defaults to HTTP_TIMEOUT (30 s), so
    inheriting the default would silently cap every research call at 30 s and make the
    75 s outer tier budget a no-op.

    Bounds are CLAMPED, never refused — a clamp cannot surprise the caller into a
    different search. The two SEMANTIC conflicts (unknown `types`, `repos` with no
    repository type) are refused in the tool body instead, before this runs."""
    headers, key = _fc_research_headers()
    payload: dict = {"query": query,
                     "k": max(1, min(int(k), DEV_K_MAX)),
                     "passages": max(1, min(int(passages), DEV_PASSAGES_MAX))}
    if types:
        payload["types"] = list(types)
    if repos:
        payload["repos"] = list(repos)
    resp = _post_json(FC_DEVELOPER_URL, payload, headers, key,
                      timeout=RESEARCH_HTTP_TIMEOUT,
                      deadline=_effective_deadline(RESEARCH_TIER_TIMEOUT))
    _fc_results(resp, FC_DEVELOPER_URL, key)   # fail-closed envelope check; result unused
    return resp


async def _camoufox_render(url: str, max_chars: int | None = None) -> str:
    """Tier-2: headless camoufox render -> visible body text. Bounded + timed.

    Single navigation (domcontentloaded), then a best-effort wait for network
    idle — so a long-polling page that never goes idle still yields the content
    that loaded, WITHOUT a second goto (which would re-request the page and could
    mask a real navigation failure). Only the idle wait's timeout is swallowed;
    a genuine goto failure (DNS, auth redirect) propagates to the Firecrawl tier.
    """
    from camoufox.async_api import AsyncCamoufox
    from playwright.async_api import TimeoutError as PWTimeout

    async def _go_inner() -> str:
        loop = asyncio.get_running_loop()
        host_cache: dict[str, bool] = {}
        guard = _make_route_guard(loop, host_cache)
        blocked: list[str] = []
        pending_docs: list = []
        on_request = _make_request_observer(pending_docs, host_cache, blocked)

        async with AsyncCamoufox(headless=True) as browser:
            page = await browser.new_page()
            await page.route("**/*", guard)   # abort forbidden subresources/initial nav
            page.on("request", on_request)    # RECORD forbidden document/redirect hops (DNS off-loop)
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except Exception:
                await _flush_pending(loop, pending_docs, host_cache, blocked)
                if blocked:
                    raise RetrievalError(f"aborted non-public navigation: {blocked[0]}")
                raise
            await _flush_pending(loop, pending_docs, host_cache, blocked)
            if blocked:
                raise RetrievalError(f"aborted non-public navigation: {blocked[0]}")
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except PWTimeout:
                pass  # page loaded but never idled; use what's there
            await _flush_pending(loop, pending_docs, host_cache, blocked)
            if blocked:
                raise RetrievalError(f"aborted non-public navigation: {blocked[0]}")
            # Bound extracted text inside the browser process. One extra character lets
            # _finalize distinguish an exact-length body from a clipped one and emit its
            # public truncation marker. The page still loads normally; this bounds the
            # renderer-to-server payload and returned content, not network transfer.
            if max_chars is None:
                body = await page.inner_text("body")
            else:
                body = await page.locator("body").evaluate(
                    "(el, limit) => (el.innerText || '').slice(0, limit)", max_chars + 1)
            # A document navigation can fire DURING the inner_text await — flush + check
            # ONCE MORE before returning so a late redirect-to-LAN hop's content can't
            # leak to the caller.
            await _flush_pending(loop, pending_docs, host_cache, blocked)
            if blocked:
                raise RetrievalError(f"aborted non-public navigation: {blocked[0]}")
            return body.strip()

    # The queue wait does not consume the render budget. A separate
    # `except TimeoutError` around the acquire is required for phase naming — in 3.12
    # asyncio.timeout and wait_for BOTH raise the builtin TimeoutError, so a single
    # shared handler could not tell "queue busy" from "render timeout". Do NOT collapse
    # into `async with _render_sem` under one outer timeout. Exception-safety: CPython
    # 3.12 Semaphore.acquire re-releases the permit on cancel-after-grant, and there is
    # no await point between the timeout block's exit and the try/finally.
    try:
        async with asyncio.timeout(SEM_ACQUIRE_TIMEOUT):
            await _render_sem.acquire()
    except TimeoutError:
        raise RetrievalError(
            f"render queue busy (semaphore wait exceeded {SEM_ACQUIRE_TIMEOUT}s)")
    try:
        return await asyncio.wait_for(_go_inner(), timeout=RENDER_TIMEOUT)
    finally:
        _render_sem.release()


# --------------------------------------------------------------------------- formatting
_WS_RE = re.compile(r"\s+")


def _canonical_url(url: str) -> str:
    """Build a conservative deduplication key.

    Only the DNS host is lowercased and ``utm_*`` query parameters are removed.
    Scheme, userinfo, port, path, other query data, and fragment are preserved so
    potentially distinct resources are never collapsed. The raw key stays internal;
    rendered URLs pass through ``_display_url``.
    """
    try:
        p = urlparse(url)
    except Exception:
        return url
    host = (p.hostname or "").lower()
    # verbatim `user[:pass]@` prefix, taken off the raw netloc rather than
    # reassembled from p.username/p.password (which would lose the exact
    # encoding). rsplit: a '@' inside the userinfo itself keeps the LAST one
    # as the delimiter, matching RFC 3986 authority parsing.
    userinfo = ""
    if p.netloc and "@" in p.netloc:
        userinfo = p.netloc.rsplit("@", 1)[0] + "@"
    try:
        port = p.port
    except ValueError:
        port = None
    if port:
        host += f":{port}"
    if p.query:
        kept = [kv for kv in p.query.split("&")
                if kv and not kv.split("=", 1)[0].lower().startswith("utm_")]
        query = "&".join(kept)
    else:
        query = ""
    scheme = (p.scheme or "").lower()
    canon = f"{scheme}://{userinfo}{host}{p.path}"
    if query:
        canon += "?" + query
    if p.fragment:
        canon += "#" + p.fragment   # kept: hash-routed SPA routes are distinct pages
    return canon if host else url


def _render_search(query: str, results: list[dict], text_chars: int = 1200,
                   output: dict | None = None,
                   header_lines: list[str] | None = None) -> str:
    """Render one block per conservative canonical-URL survivor.

    Results differing only by ``utm_*`` parameters collapse; fragments and every
    other potentially meaningful distinction remain separate. The first occurrence
    wins and later duplicates get a one-line stub. Rendered result, duplicate, source,
    and grounding URLs are credential-redacted. Highlights already contained in the
    visible body slice are omitted. Optional Exa synthesized output is rendered before
    result blocks, and documented plus defensively tolerated grounding shapes are
    supported.
    """
    if not results and not output and not header_lines:
        return f"No results for: {query}"

    survivors: list[dict] = []
    dup_stubs: dict[int, list[str]] = {}   # survivor 1-based index -> duplicate URLs
    by_canon: dict[str, int] = {}          # canonical url -> survivor 1-based index
    for r in results:
        url = r.get("url", "")
        canon = _canonical_url(url)
        if canon in by_canon:
            dup_stubs.setdefault(by_canon[canon], []).append(url)
            continue
        idx = len(survivors) + 1
        survivors.append(r)
        by_canon[canon] = idx

    # fallback provenance / partial-filter / degradation lines sit directly
    # under the header title, each on its own line (ground rule 6).
    if header_lines:
        blocks = [f"# Web search: {query}\n" + "\n".join(header_lines)]
    else:
        blocks = [f"# Web search: {query}\n"]
    if output:
        content = output.get("content")
        if isinstance(content, str) and content.strip():
            syn = ["## Synthesized answer", content.strip()]
            cites = []
            seen_cites: set[str] = set()   # first-seen order, across ALL grounding entries
            for g in output.get("grounding") or []:
                found: list[str] = []
                if isinstance(g, dict) and isinstance(g.get("citations"), list):
                    # documented shape: grounding[] -> citations[] -> {url, title}
                    for c in g["citations"]:
                        if isinstance(c, dict):
                            raw_url = c.get("url")
                            fallback_title = c.get("title")
                            if raw_url:
                                found.append(_display_url(str(raw_url)))
                            elif fallback_title:
                                found.append(str(fallback_title))
                elif isinstance(g, dict):
                    # secondary branch: tolerate a flat {"url": ...} entry, a shape
                    # the live API does not produce, so an unannounced future shape
                    # change degrades rather than crashes.
                    raw_url = g.get("url")
                    fallback = g.get("id") or g.get("title") or ""
                    if raw_url:
                        found.append(_display_url(str(raw_url)))
                    elif fallback:
                        found.append(str(fallback))
                elif isinstance(g, str):
                    found.append(_display_reference(g))
                for u in found:
                    if u not in seen_cites:
                        seen_cites.add(u)
                        cites.append(u)
            if cites:
                syn.append("Grounding: " + "; ".join(cites[:20]))
            blocks.append("\n".join(syn))
    sources = []
    for i, r in enumerate(survivors, 1):
        url = r.get("url", "")
        display_url = _display_url(str(url))
        title = r.get("title") or "(untitled)"
        pub = r.get("publishedDate") or ""
        hl = r.get("highlights") or []
        # summary mode omits `text` — render the summary as the body
        text = (r.get("text") or r.get("summary") or "").strip()
        body_slice = text[:text_chars]
        sources.append(f"[{i}] {display_url}")
        block = [
            f"## {i}. {title}",
            f"URL: {display_url}" + (f"  ·  {pub}" if pub else ""),
        ]
        if hl:
            norm_body = _WS_RE.sub(" ", body_slice).strip().lower()
            kept = []
            for h in hl[:4]:
                hs = h.strip()
                if not hs:
                    continue
                norm_h = _WS_RE.sub(" ", hs).strip().lower()
                if norm_h and norm_body and norm_h in norm_body:
                    continue  # already shown in the body slice — suppress overlap
                kept.append(hs)
            if kept:
                block.append("Highlights:\n" + "\n".join(f"  - {h}" for h in kept))
        if text:
            block.append(body_slice + ("…" if len(text) > text_chars else ""))
        for dup_url in dup_stubs.get(i, []):
            block.append(f"(duplicate of [{i}]: {_display_url(str(dup_url))})")
        blocks.append("\n".join(block))
    if not survivors and not output:
        # header_lines path (e.g. an empty Firecrawl fallback) — make zero-results explicit
        # instead of returning a bare provenance header.
        blocks.append(f"(no results for: {query})")
    if sources:
        blocks.append("Sources:\n" + "\n".join(sources))
    return "\n\n".join(blocks)


def _author_name(a) -> str:
    # authors items may be plain strings or {name:...} dicts — coerce defensively
    return (a.get("name") or a.get("fullName") or "").strip() if isinstance(a, dict) else str(a)


def _render_papers(query: str, results: list[dict], abstract_chars: int = 600,
                   min_score: float = 0.0) -> str:
    if min_score > 0.0:
        #  renderer-side score floor: keep only numerically-scored items at
        # or above the floor (drop unscored when a floor is active — conservative).
        results = [r for r in results if isinstance(r, dict)
                   and isinstance(r.get("score"), (int, float)) and r["score"] >= min_score]
    if not results:
        return f"No research papers for: {query}"
    blocks = [f"# Research papers: {query}\n"]
    for i, r in enumerate(results, 1):
        if not isinstance(r, dict):
            continue
        title = r.get("title") or "(untitled)"
        pid = r.get("primaryId") or r.get("paperId") or ""
        ids = r.get("ids") if isinstance(r.get("ids"), dict) else {}
        arxiv = ", ".join(ids.get("arxiv") or []) if isinstance(ids.get("arxiv"), list) else ""
        score = r.get("score")
        ab = (r.get("abstract") or "").strip()
        meta = f"id: {pid}"
        if arxiv:
            meta += f"  ·  arXiv:{arxiv}"
        if isinstance(score, (int, float)):
            meta += f"  ·  score {score:.3f}"
        block = [f"## {i}. {title}", meta]
        if ab:
            block.append(ab[:abstract_chars] + ("…" if len(ab) > abstract_chars else ""))
        blocks.append("\n".join(block))
    blocks.append("(Next: research_paper(paper_id, query=…) reads full-text passages to "
                  "verify a claim before citing; research_similar(paper_id, intent=…) expands related work.)")
    return "\n\n".join(blocks)


def _render_paper(data: dict, query: str | None, passage_chars: int = 1400) -> str:
    paper = data.get("paper") if isinstance(data.get("paper"), dict) else {}
    title = paper.get("title") or "(untitled)"
    pid = paper.get("paperId") or ""
    ids = paper.get("ids") if isinstance(paper.get("ids"), dict) else {}
    arxiv = ", ".join(ids.get("arxiv") or []) if isinstance(ids.get("arxiv"), list) else ""
    cats = paper.get("categories") or []
    created = paper.get("createdDate") or ""
    authors = paper.get("authors") or []
    abstract = (paper.get("abstract") or "").strip()
    out = [f"# {title}"]
    meta = f"id: {pid}"
    if arxiv:
        meta += f"  ·  arXiv:{arxiv}"
    if isinstance(cats, list) and cats:
        meta += "  ·  " + ", ".join(str(c) for c in cats)
    if created:
        meta += f"  ·  created {created}"
    out.append(meta)
    if isinstance(authors, list) and authors:
        names = [n for n in (_author_name(a) for a in authors[:15]) if n]
        if names:
            out.append("Authors: " + ", ".join(names) + ("…" if len(authors) > 15 else ""))
    if abstract:
        out.append("\n## Abstract\n" + abstract)
    passages = data.get("passages") or []
    if isinstance(passages, list) and passages:
        out.append(f"\n## Top passages for: {query}")
        for i, p in enumerate(passages, 1):
            if not isinstance(p, dict):
                continue
            txt = (p.get("text") or "").strip()
            sc = p.get("score")
            hdr = f"### Passage {i}" + (f" (score {sc:.3f})" if isinstance(sc, (int, float)) else "")
            out.append(hdr + "\n" + (txt[:passage_chars] + ("…" if len(txt) > passage_chars else "")))
    return "\n".join(out)


def _render_github(query: str, results: list[dict], snippet_chars: int = 800) -> str:
    if not results:
        return f"No GitHub research results for: {query}"
    blocks = [f"# GitHub research: {query}\n"]
    for i, r in enumerate(results, 1):
        if not isinstance(r, dict):
            continue
        rt = r.get("resultType") or ""
        repo = r.get("repo") or ""
        url = r.get("url") or ""
        label = r.get("title") or r.get("pageType") or rt or "(result)"
        num = r.get("number")
        sn = (r.get("snippet") or "").strip()
        meta = f"type: {rt}" + (f"  ·  #{num}" if num else "")
        block = [f"## {i}. {repo} — {label}", meta, f"URL: {url}"]
        if sn:
            block.append(sn[:snippet_chars] + ("…" if len(sn) > snippet_chars else ""))
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def _dev_coverage_line(cov) -> str | None:
    """`[coverage: ...]` line, or None when it would say nothing.

    Rendered ONLY when some type is `degraded`/`unavailable`. `skipped` is excluded on
    purpose: a non-requested type is ALWAYS reported `skipped`, so gating on 'not all ok'
    would print the line on every scoped call — noise that trains the reader to ignore it.
    Same reason `skipped` is excluded from the fallback predicate: it is caller-caused."""
    if not isinstance(cov, dict) or not cov:
        return None
    if not any(v in ("degraded", "unavailable") for v in cov.values()):
        return None
    return "[coverage: " + ", ".join(f"{t}={cov.get(t)}" for t in DEV_TYPES if t in cov) + "]"


def _render_developer(query: str, envelope: dict, passage_chars: int = 800) -> str:
    """ Developer Index renderer. Deliberately NOT a normalizing shim over
    `_render_github`: the shapes share nothing (`id`/`type`/`passages[]`/`citation_url`
    vs `resultType`/`repo`/`snippet`), and flattening passages back to one snippet would
    discard exactly the information this migration is for. `_render_github` stays
    untouched and keeps serving the legacy fallback."""
    results = envelope.get("results") or []
    cov = envelope.get("coverage")
    cov_line = _dev_coverage_line(cov)

    if not results:
        # Force the coverage line on in the empty case even when every type is `ok`:
        # "the index answered and found nothing" and "the index could not answer" must
        # not render identically.
        detail = cov_line or (f"[coverage: {', '.join(f'{t}={cov.get(t)}' for t in DEV_TYPES if t in cov)}]"
                              if isinstance(cov, dict) and cov else None)
        out = f"No developer-index results for: {query}"
        return out + ("\n" + detail if detail else "")

    head = [f"# Developer index: {query}"]
    if cov_line:
        head.append(cov_line)
    if envelope.get("reranked") is False:
        head.append("[reranked: false]")
    blocks = ["\n".join(head) + "\n"]

    for i, r in enumerate([x for x in results if isinstance(x, dict)], 1):
        url = r.get("url") or ""
        # `title` is frequently absent on `doc` results (live-confirmed) — fall back to
        # the URL, never to "(untitled)": here the URL is the useful identifier.
        label = r.get("title") or url or "(result)"
        meta = f"type: {r.get('type') or '?'}"
        if r.get("id"):
            meta += f"  ·  id: {r['id']}"
        block = [f"## {i}. {label}", meta, f"URL: {url}"]
        for p in (r.get("passages") or []):
            if not isinstance(p, dict):
                continue
            text = (p.get("text") or "").strip()
            if not text:
                continue
            block.append("  " + text[:passage_chars] + ("…" if len(text) > passage_chars else ""))
            cite = p.get("citation_url")
            if cite and cite != url:
                block.append(f"     (source: {cite})")
        blocks.append("\n".join(block))

    body = "\n\n".join(blocks)
    # Own clip, NOT _finalize: _finalize appends its marker AFTER slicing (so it returns
    # cap+len(marker), overshooting), and its marker tells the caller to retry with a
    # larger `max_chars` — a parameter this tool does not have. max(0,…) plus the outer
    # slice keeps the arithmetic correct if DEV_MAX_OUTPUT_CHARS is ever lowered below
    # len(suffix): a negative slice index would otherwise strip from the END of the body.
    if len(body) > DEV_MAX_OUTPUT_CHARS:
        suffix = (f"\n\n[output clipped at {DEV_MAX_OUTPUT_CHARS} chars — lower k, "
                  "or narrow types, for fewer/tighter results]")
        body = (body[:max(0, DEV_MAX_OUTPUT_CHARS - len(suffix))] + suffix)[:DEV_MAX_OUTPUT_CHARS]
    return body


def _finalize(body: str, max_chars: int, requested_cap: int | None = None) -> str:
    """Slice body to max_chars and append a truncation marker on its OWN line
    (after a blank line) when content was, or may have been, clipped.

    requested_cap = the cap actually SENT to the upstream tier (Exa:
    min(max_chars, 10000)). Upstream clipping is invisible client-side, so an
    at-or-near-cap observed length (len >= requested_cap*0.98) must be treated as
    possibly-clipped at EVERY cap value, not only the 10k ceiling. A complete
    article landing exactly at cap gets a spurious-but-honest 'may continue'
    marker — accepted. The marker never recommends render='always' (Camoufox is
    sliced by the same max_chars AND is the SSRF-gated tier) — a larger max_chars
    (up to the 100k clamp; explicit >10k full-body auto starts locally) is the
    correct remedy."""
    truncated = False
    if len(body) > max_chars:
        body = body[:max_chars]
        truncated = True
    elif requested_cap is not None and requested_cap > 0 and len(body) >= requested_cap * 0.98:
        truncated = True
    if truncated:
        marker = (f"[TRUNCATED at {len(body)} chars — content may continue; "
                  f"re-fetch with a larger max_chars for more]")
        return f"{body}\n\n{marker}"
    return body


def _mode_line(mode: str, question: str | None) -> str:
    """The [mode: …] provenance line for concise/question extraction.
    Emitted (own line, after the [served by:] line, before the blank line) only by
    tiers that HONORED the mode — camoufox ignores mode/question, so it never emits."""
    if question is not None:
        return "[mode: question]"
    if mode == "concise":
        return "[mode: concise]"
    return ""


# --------------------------------------------------------------------------- tools
async def _web_search_fallback(query: str, num_results: int, text_chars: int, summary: bool,
                               primary_reason: str, recency_days: int | None,
                               start_published_date: str | None, end_published_date: str | None,
                               category: str | None, include_domains: list[str] | None,
                               exclude_domains: list[str] | None,
                               filter_notices: list[str] | None = None,
                               recency_hours: int | None = None,
                               sort_by_date: bool = False,
                               primary_provider: str = "exa") -> str:
    """Firecrawl /v2/search fallback when the selected search tier fails. Maps
    what it can (domains — mutually exclusive, include wins; dates/recency -> tbs)
    and records everything dropped/approximated on a separate additive line.

    `filter_notices` contains category-migration messages
    already raised while building the Exa filters; re-emitted here so they are not
    lost when the search degrades to the fallback tier.

    Time-filter precedence matches the Exa tier: explicit start, recency_hours,
    then recency_days. ``sort_by_date`` is an independent ``sbd:1`` component.
    An end-only request is disclosed and dropped because Firecrawl's documented
    custom range requires a start.
    """
    partial: list[str] = []
    fb_include = list(include_domains) if include_domains else None
    fb_exclude = None
    if exclude_domains:
        if include_domains:
            partial.append("excludeDomains (mutually exclusive with includeDomains on fallback)")
        else:
            fb_exclude = list(exclude_domains)

    start_d = _parse_date(start_published_date) if start_published_date else None
    end_d = _parse_date(end_published_date) if end_published_date else None

    def _recency_range(hours: int, source_label: str) -> tuple[str | None, str | None]:
        """Combine a recency window in hours with an optional explicit end.

        If the end precedes the recency-derived start, keep the recency window and
        disclose that the inverted end was dropped. Invalid or out-of-range windows
        return ``(None, None)``.
        """
        bucket = _recency_hours_to_tbs(hours)
        if bucket is None:
            return (None, None)
        if end_d is None:
            return (bucket, f"recency≈{bucket}")
        start_from_recency = (datetime.now(timezone.utc) - timedelta(hours=hours)).date()
        if start_from_recency > end_d:
            return (bucket, f"recency≈{bucket} (end bound {source_label} is "
                            f"before the recency window — end dropped)")
        combined = _dates_to_tbs(start_from_recency, end_d)
        return (combined, f"recency≈{combined} ({'hour' if source_label.startswith('recency_hours') else 'day'}-"
                          f"derived start + explicit end bound, day granularity on fallback)")

    range_clause: str | None = None
    if start_d is not None:
        # Explicit valid start wins. Drop and disclose a reversed end rather than
        # sending an inverted range.
        end_for_range = end_d
        if end_d is not None and end_d < start_d:
            note_suffix = " (explicit end is before the explicit start — end dropped)"
            end_for_range = None
        else:
            note_suffix = ""
        range_clause = _dates_to_tbs(start_d, end_for_range)
        partial.append(f"date bounds≈{range_clause} (day granularity on fallback){note_suffix}")
    elif recency_hours and 0 < recency_hours <= 876_000:
        range_clause, note = _recency_range(recency_hours, f"recency_hours={recency_hours}")
        if note:
            partial.append(note)
    elif recency_days and recency_days > 0:
        # Convert days to hours before selecting a Firecrawl recency bucket.
        range_clause, note = _recency_range(recency_days * 24, f"recency_days={recency_days}")
        if note:
            partial.append(note)
    elif end_d is not None:
        # end-only (no start, no valid recency) — Firecrawl's documented cdr:1
        # form always carries a cd_min, so a cd_max-only range is not invented.
        partial.append("end bound only (no fallback cdr: form — dropped)")

    if sort_by_date:
        partial.append("sorted by date (sbd:1)")

    # Built component-wise, NEVER by interpolating the composed tbs: a composed
    # value like "sbd:1,qdr:w" interpolated into "recency≈{tbs}" would call a sort
    # directive a recency window — caller-visible misinformation in the cluster
    # whose other half is about accurate disclosure.
    tbs = _compose_tbs(sort_by_date, range_clause)

    if category:
        partial.append("category (no fallback equivalent)")
    _TIER_DEADLINE_VAR.set(_tier_deadline(TIER_TIMEOUT))
    try:
        fb = await asyncio.wait_for(
            anyio.to_thread.run_sync(_firecrawl_search_sync, query, num_results,
                                     fb_include, fb_exclude, tbs, abandon_on_cancel=True),
            timeout=TIER_TIMEOUT)
    except (RetrievalError, asyncio.TimeoutError) as e2:
        return (f"SEARCH_FAILED: {query} — {primary_provider}: {primary_reason} | "
                f"firecrawl: {e2}")
    header_lines = [
        f"[served by: firecrawl search — {primary_provider} unavailable: {primary_reason}]"
    ]
    header_lines += [f"[{n}]" for n in (filter_notices or [])]
    if len(query) > 500:
        # _firecrawl_search_sync truncates the query to 500 chars — disclose it so the
        # caller knows the results may be for a shortened query.
        header_lines.append(f"[query truncated to 500 chars on fallback (was {len(query)})]")
    if partial:
        header_lines.append(f"[filters partially applied on fallback: {', '.join(partial)}]")
    if summary:
        header_lines.append("[note: fallback results are query-relevant highlight excerpts when "
                            "available, otherwise plain page descriptions; not generated summaries]")
    return _render_search(query, fb, text_chars, header_lines=header_lines)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def web_search(query: str, num_results: int = 8, mode: str = "auto",
                     text_chars: int = 1200,
                     recency_days: int | None = None,
                     start_published_date: str | None = None,
                     end_published_date: str | None = None,
                     category: str | None = None,
                     include_domains: list[str] | None = None,
                     exclude_domains: list[str] | None = None,
                     summary: bool = False,
                     recency_hours: int | None = None,
                     sort_by_date: bool = False,
                     provider: str | None = None) -> str:
    """Search the web via Exa or Tavily. Returns one block per result,
    each with its OWN title, URL, highlights, and text — never a merged summary —
    plus a Sources trailer. In clients with built-in web search, use this as an
    independent complementary retrieval lane; when built-in search is disabled or
    unavailable, use it as the primary search path.

    Args:
        query: the search query.
        num_results: how many results (default 8).
        mode: search mode — "auto" (default), "fast" (~450ms), "instant" (~250ms),
            or the deep-research family "deep-lite" (≈4s), "deep" ($12/1k), or
            "deep-reasoning" (12–40s, $15/1k). Deep modes request a synthesized
            answer via Exa's `outputSchema` and return a
            "## Synthesized answer" block — with a "Grounding:" citation line when
            Exa returns one — plus per-result blocks; this costs ~2s of synthesis
            latency on top of the mode's own search time, which is why it is scoped
            to the deep family only (their timeout budget already covers it).
            Legacy "neural"/"keyword" map to "auto" (deprecated).
        text_chars: per-result body length (default 1200; clamped 200–8000). Now
            governs BOTH the Exa request size (request-what-you-render) and the
            rendered cap — raise it to surface more body text per result.
        recency_days: only results published within the last N days (maps to
            startPublishedDate). Pass this for time-sensitive/"latest" queries —
            the tool does NOT auto-tighten dates on its own.
        recency_hours: like recency_days but HOUR granularity — reaches
            BOTH tiers (Exa via a full ISO timestamp; the Firecrawl fallback via
            tbs qdr:h/cdr:1,cd_min:…). Full precedence ladder, same on both tiers:
            explicit VALID start_published_date > recency_hours > recency_days. An
            invalid/absent value at each tier falls through to the next.
        start_published_date / end_published_date: ISO date bounds ("2026-01-15"
            or a full ISO timestamp). Wins over recency_hours/recency_days per the
            ladder above on both tiers. The fallback represents an explicit start
            with Firecrawl's custom-date-range syntax.
            Unparseable values are ignored (stderr note), never an error.
        category: Exa category hint ("news", "publication", "company", "people",
            "financial report", "personal site", or a free string). NOTE: "company"
            and "people" forbid date filters + excludeDomains (dropped
            automatically), and "people" restricts includeDomains to Exa's
            supported profile domains (it 400s otherwise — surfaced as
            SEARCH_FAILED / fallback).
            Enum migration: "research paper" was
            RENAMED to "publication" and is auto-aliased forward; "tweet" is GONE
            (Exa 400s) and is dropped, leaving the search unscoped; "pdf"/"github"
            are deprecated-but-live and pass through. Each of those emits a
            `[category=… ]` line in the response header, since dropping or renaming
            a category changes which results you get back.
        include_domains / exclude_domains: restrict/exclude result hosts (capped
            at 20 entries each).
        summary: when True, request a generated per-result summary instead of raw
            text/highlights (fewer tokens; text + highlights omitted from the request).
        sort_by_date: sort results by publish date instead of relevance.
            Honoured ONLY on the Firecrawl fallback tier (Firecrawl's `sbd:1`); Exa
            has no equivalent — when the Exa tier serves and this was requested, the
            response header discloses that it was not applied rather than silently
            ignoring it.
        provider: "exa" or "tavily". When omitted, WEB_SEARCH_PROVIDER is used,
            defaulting to Exa. Invalid values fail explicitly. Tavily requires the
            ``tavily`` extra and TAVILY_API_KEY.
    """
    #  mode enum migration: legacy neural/keyword -> auto (deprecated),
    # unknown -> auto; deep-family modes get the longer inner+outer timeouts.
    if mode in ("neural", "keyword"):
        print(f"web_search: mode {mode!r} deprecated -> 'auto'", file=sys.stderr)
        mode = "auto"
    elif mode not in VALID_SEARCH_MODES:
        if mode != "auto":
            print(f"web_search: unknown mode {mode!r} -> 'auto'", file=sys.stderr)
        mode = "auto"
    #  clamp caller-supplied sizes — unbounded num_results means oversized
    # Exa requests; text_chars caps per-result body (default 1200 preserves prior
    # behavior; raise it to surface more of Exa's ~4k text).
    num_results = max(1, min(num_results, 20))
    text_chars = max(200, min(text_chars, 8000))

    configured_provider = provider
    if configured_provider is None:
        configured_provider = os.environ.get("WEB_SEARCH_PROVIDER", "exa")
    if not isinstance(configured_provider, str):
        return "SEARCH_FAILED: provider must be 'exa' or 'tavily'"
    selected_provider = configured_provider.strip().casefold()
    if selected_provider not in {"exa", "tavily"}:
        return ("SEARCH_FAILED: unsupported provider "
                f"{configured_provider!r}; expected 'exa' or 'tavily'")

    if selected_provider == "tavily":
        options, tavily_notices = _tavily_search_options(
            mode=mode,
            recency_days=recency_days,
            recency_hours=recency_hours,
            start_published_date=start_published_date,
            end_published_date=end_published_date,
            category=category,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
        )
        if summary:
            tavily_notices.append(
                "summary has no per-result Tavily equivalent; result excerpts were returned"
            )
        if sort_by_date:
            tavily_notices.append(
                "sort_by_date is not supported by Tavily; results are provider-ranked"
            )
        _TIER_DEADLINE_VAR.set(_tier_deadline(TIER_TIMEOUT))
        try:
            response = await asyncio.wait_for(
                anyio.to_thread.run_sync(
                    _tavily_search_sync,
                    query,
                    num_results,
                    options,
                    abandon_on_cancel=True,
                ),
                timeout=TIER_TIMEOUT,
            )
        except (RetrievalError, asyncio.TimeoutError) as exc:
            return await _web_search_fallback(
                query,
                num_results,
                text_chars,
                summary,
                str(exc),
                recency_days,
                start_published_date,
                end_published_date,
                category,
                include_domains,
                exclude_domains,
                filter_notices=tavily_notices,
                recency_hours=recency_hours,
                sort_by_date=sort_by_date,
                primary_provider="tavily",
            )
        headers = ["[served by: tavily search]"]
        headers.extend(f"[{notice}]" for notice in tavily_notices)
        return _render_search(
            query,
            response.get("results") or [],
            text_chars,
            header_lines=headers,
        )

    #  category-migration messages are surfaced to the CALLER (not just
    # stderr) — a renamed/removed category changes result scope invisibly.
    filter_notices: list[str] = []
    #  keyword args ONLY at this call site — _build_search_filters gained
    # recency_hours as its SECOND positional parameter, so a positional call here
    # would silently bind start_published_date's value to recency_hours instead.
    filters = _build_search_filters(recency_days=recency_days, recency_hours=recency_hours,
                                    start_published_date=start_published_date,
                                    end_published_date=end_published_date,
                                    category=category, include_domains=include_domains,
                                    exclude_domains=exclude_domains,
                                    notices=filter_notices)
    outer_timeout = DEEP_TIER_TIMEOUT if mode in DEEP_SEARCH_MODES else TIER_TIMEOUT
    _TIER_DEADLINE_VAR.set(_tier_deadline(outer_timeout))
    try:
        resp = await asyncio.wait_for(
            anyio.to_thread.run_sync(_exa_search_sync, query, num_results, mode,
                                     text_chars, filters, summary, abandon_on_cancel=True),
            timeout=outer_timeout,
        )
    except (RetrievalError, asyncio.TimeoutError) as e:
        #  try the Firecrawl /v2/search fallback tier before giving up.
        return await _web_search_fallback(
            query, num_results, text_chars, summary, str(e),
            recency_days, start_published_date, end_published_date,
            category, include_domains, exclude_domains,
            filter_notices=filter_notices,
            recency_hours=recency_hours, sort_by_date=sort_by_date)
    results = resp.get("results") or []
    header_lines = [f"[{n}]" for n in filter_notices]
    if sort_by_date:
        # sort_by_date has no Exa equivalent — when Exa served, say so
        # rather than silently ignore the request. UNBRACKETED here: header_lines
        # entries below are bracketed by the render site
        # (header_lines=[f"[{n}]" ...]), so a notice stored WITH brackets would
        # render doubled ("[[...]]") and a plain substring assertion would still
        # match inside it — green tests on wrong output.
        header_lines.append("[sort_by_date not supported by exa — results are relevance-ranked]")
    return _render_search(query, results, text_chars, output=resp.get("output"),
                          header_lines=header_lines or None)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def web_fetch(url: str, render: str = "auto", max_chars: int | None = None,
                    max_age_hours: int | None = None, mode: str = "full",
                    question: str | None = None,
                    tavily: bool | None = None) -> str:
    """Fetch a single URL's readable content. Full-body tier chain:
    Exa contents → local Camoufox → optional Tavily Extract → Firecrawl.
    Firecrawl is the paid last resort:
    automatic full-body retrieval reaches it only after the local browser fails or
    returns a body shorter than the useful-content floor. Camoufox is the one tier
    that runs a real browser locally. Caller-supplied private URLs are refused before
    tier selection in every render mode.
    Eligible successful auto/never calls also use a host-wide 24-hour completed-result
    cache in a private local Valkey sidecar. Local replay is disclosed separately from
    the original provider's cache state; pass max_age_hours=0 to force provider work.
    Signed/credential URLs, userinfo, positive freshness, max_age_hours=0, and
    render="always" bypass completed replay. Every request is SSRF-validated before
    cache access, and a hit is validated again immediately before its body is returned.
    Returns content with a `[served by: …]` provenance header. In clients with
    built-in page retrieval, use this as an independent complementary retrieval
    lane; when built-in retrieval is disabled or unavailable, use it as the primary
    fetch path.

    Args:
        url: the URL to fetch.
        render: "auto" (default) → Exa, then local Camoufox, optional Tavily,
            then Firecrawl. "never" skips Camoufox. "always" forces Camoufox
            first and skips Exa; Tavily and Firecrawl remain backstops.
            For mode="concise" or a question, auto intentionally uses Exa then
            Firecrawl without launching Camoufox: the browser returns a full body
            and cannot satisfy the promised summary/direct-answer shape.
        max_chars: max characters to request/return. None (default) → 20000-char
            budget, Exa-first. An explicit value ≤10000 stays Exa-first. For an
            explicit value >10000 in full-body auto, Exa cannot meet the requested
            size, so the order is Camoufox → optional Tavily → Firecrawl → Exa as a
            final transparently truncated salvage tier. Browser-free full-body mode
            starts with optional Tavily, then Firecrawl and Exa. Semantic requests remain Exa-first
            because they use Exa's summary API rather than its capped body output.
            Clamped to 1000–100000. When output is (or may be) clipped, a
            `[TRUNCATED at N chars — …]` marker is appended on its own line.
        max_age_hours: freshness window for the Exa AND Firecrawl tiers. None =
            each tier's default cache (Exa default; Firecrawl ~2 days); 0 = force
            fresh on both. -1 = "always use cache" (Exa-documented) —
            Firecrawl has NO equivalent, so on that tier -1 is treated as omit
            (its own default cache window applies instead; disclosed in the
            [cache: …] line). Values below -1 are ignored (stderr note, default
            cache used). Values above 720 (Exa's documented ceiling) are CLAMPED to
            720, and the clamp is disclosed — a clamped value changes which content
            can come back. The camoufox render tier is always live. When Firecrawl
            serves with cache permitted, a [cache: …] disclosure line is added.
        mode: "full" (default, whole readable body) or "concise" (a generated
            summary — far fewer tokens). Honored by the Exa and Firecrawl tiers;
            the camoufox render tier ignores it (returns full body, no [mode:]
            line). Concise/question outputs carry a `[mode: …]` provenance line.
        question: optional grounded-extraction query. When set, the tier returns a
            direct ANSWER to the question (Exa summary-with-query / Firecrawl
            question format) instead of the page body; short answers are accepted
            — the floor is 1 char, so only an EMPTY answer cascades to the next
            tier ("Paris"/"No" are legitimate answers). The 60-char
            extract floor applies to mode="concise", and the 200-char floor to a
            full body. Overrides `mode`.
        tavily: enable or disable Tavily Extract for this call. When omitted,
            WEB_FETCH_TAVILY_TIER is parsed strictly (default false). Tavily is a
            full-body tier only and is skipped for concise/question requests and
            whenever max_age_hours is explicit because it cannot honor those
            contracts. Requires the ``tavily`` extra and TAVILY_API_KEY.

    SSRF note: Camoufox follows redirects and re-resolves DNS, so every Camoufox
    attempt (automatic or render="always") is guarded:
      - `_make_route_guard` aborts any request whose host resolves non-public,
        classifying by RESOLVED IP (not the URL string), failing closed on a
        resolution error;
      - `_make_request_observer` + `_flush_pending` exist because `page.route`
        does NOT fire on a main-frame 3xx — they see the redirect hops the route
        guard alone would miss;
      - `_camoufox_render` raises on a `blocked` hop at FOUR checkpoints: after a
        `goto` exception (catches a navigation the guard itself aborted, raising
        the guard's reason instead of an opaque playwright error), after a
        successful `goto`, after the `networkidle` wait, and after `inner_text`
        (a hop recorded during the extraction await, before any body returns).
      - `test_ssrf_redirect_live.py` covers this live.
    The observer detects a forbidden document request after Chromium has emitted it,
    so the application prevents private content from being returned but cannot prove
    that no outbound packet was sent. Chromium may also re-resolve after the guard's
    check. Full closure would need a validating forward proxy or equivalent network
    policy.
    """
    display_url = _display_url(url)
    try:
        tavily_enabled = _tavily_fetch_enabled(tavily)
    except RetrievalError as exc:
        return f"RETRIEVAL_FAILED: {display_url} — {exc}"

    # URL policy always runs before completed-cache access or singleflight.  It also
    # runs again immediately before any completed cached body is returned below.
    validation_error = await _validate_public_url_async(url)
    if validation_error is not None:
        return f"RETRIEVAL_FAILED: {display_url} — {validation_error}"

    # Empty/whitespace question is not question mode; do not send an empty provider
    # extraction request.
    if question is not None and not question.strip():
        question = None

    # -1 is Exa's documented "always use cache" value. Values below -1 are
    # unexpressible and ignored. Values above Exa's documented ceiling are
    # CLAMPED rather than sent through unchanged, with a caller-visible note: a
    # clamped value changes which content comes back, so silence would be wrong.
    arg_notices: list[str] = []
    if max_age_hours is not None and max_age_hours < -1:
        print(f"web_fetch: ignoring negative max_age_hours {max_age_hours} (using default cache)",
              file=sys.stderr)
        max_age_hours = None
    elif max_age_hours is not None and max_age_hours > EXA_MAX_AGE_HOURS_MAX:
        note = (f"max_age_hours {max_age_hours} clamped to {EXA_MAX_AGE_HOURS_MAX} "
                f"(Exa's documented ceiling)")
        print(f"web_fetch: {note}", file=sys.stderr)
        arg_notices.append(note)
        max_age_hours = EXA_MAX_AGE_HOURS_MAX

    # Sentinel default: None -> a 20000-char budget but preserves Exa-first.
    # An EXPLICIT >10000 asks for more than Exa can supply, so full-body auto starts
    # locally; Firecrawl follows only after that local attempt fails.  clamp
    # 1000–100000 preserved.
    max_chars_omitted = max_chars is None
    explicit_large = max_chars is not None and max_chars > 10000
    effective = 20000 if max_chars is None else max_chars
    effective = max(1000, min(effective, 100_000))
    exa_cap = min(effective, 10000)   # the cap actually sent to the Exa tier

    # Concise/question outputs are short by design; use the extraction
    # floor and skip the upstream at-cap marker (nothing was cap-clipped). A grounded
    # ANSWER can be legitimately terse (only empty cascades, floor 1); a concise
    # SUMMARY keeps the 60-character thin-extract guard.
    is_extract = question is not None or mode == "concise"
    tavily_eligible = tavily_enabled and not is_extract and max_age_hours is None
    if tavily_enabled and not tavily_eligible:
        if is_extract:
            arg_notices.append("tavily tier skipped: semantic extraction is unsupported")
        else:
            arg_notices.append("tavily tier skipped: max_age_hours is unsupported")
    if question is not None:
        min_chars = 1
    elif mode == "concise":
        min_chars = MIN_EXTRACT_CHARS
    else:
        min_chars = MIN_USEFUL_CHARS
    ml = _mode_line(mode, question)   # "" unless concise/question

    url_identity = cache_url_identity(url)
    plan = make_fetch_plan(
        cache_url=url_identity.identity,
        render=render,
        mode=mode,
        question=question,
        effective_max_chars=effective,
        max_chars_omitted=max_chars_omitted,
        explicit_large=explicit_large,
        max_age_hours=max_age_hours,
        tavily_enabled=tavily_eligible,
    )
    completed_cache_eligible = (
        url_identity.completed_cache_eligible
        and render in ("auto", "never")
        and max_age_hours in (None, -1)
    )
    if not completed_cache_eligible:
        if url_identity.bypass_reason:
            bypass_reason = url_identity.bypass_reason
        elif render == "always":
            bypass_reason = "render-always"
        elif render not in ("auto", "never"):
            bypass_reason = "render-other"
        elif max_age_hours == 0:
            bypass_reason = "force-fresh"
        else:
            bypass_reason = "positive-freshness"
        record_cache_event(f"bypass.{bypass_reason}", plan.key)

    def _hdr(tier: str, with_mode: bool) -> str:
        h = f"[served by: {tier}]  {display_url}"
        if with_mode and ml:
            h += f"\n{ml}"
        for n in arg_notices:   #  e.g. a max_age_hours>720 clamp
            h += f"\n[{n}]"
        return h

    def _result_kind(tier: str) -> str:
        if tier == "camoufox":
            return "rendered"
        if question is not None:
            return "question"
        if mode == "concise":
            return "concise"
        return "full"

    def _entry_matches_plan(entry: FetchSuccess) -> bool:
        if entry.effective_limit != effective:
            return False
        if (render == "never" or is_extract) and entry.tier == "camoufox":
            return False
        expected_kind = _result_kind(entry.tier)
        if entry.result_kind != expected_kind:
            return False
        if entry.tier == "exa":
            return (
                entry.provider_cache in ("exa-cached", "exa-crawled")
                and entry.provider_cache_hours is None
                and entry.requested_cap == (None if is_extract else exa_cap)
            )
        if entry.tier == "camoufox":
            return (
                entry.provider_cache == "none"
                and entry.provider_cache_hours is None
                and entry.requested_cap is None
            )
        if entry.tier == "tavily":
            return (
                tavily_eligible
                and entry.provider_cache == "tavily-unspecified"
                and entry.provider_cache_hours is None
                and entry.requested_cap is None
            )
        expected_firecrawl_cache = (
            "firecrawl-always-default" if max_age_hours == -1 else "firecrawl-default"
        )
        return (
            entry.provider_cache == expected_firecrawl_cache
            and entry.provider_cache_hours is None
            and entry.requested_cap is None
        )

    async def _completed_lookup() -> FetchSuccess | None:
        lookup: CacheLookup = await _completed_fetch_cache.get(plan.key)
        if lookup.status != "hit" or lookup.entry is None:
            return None
        if not _entry_matches_plan(lookup.entry):
            record_cache_event("corrupt.plan-mismatch", plan.key)
            await _completed_fetch_cache.delete(plan.key)
            return None
        return lookup.entry

    def _local_age(stored_at: float) -> str:
        seconds = max(0, int(time.time() - stored_at))
        if seconds < 60:
            return "<1m"
        if seconds < 3600:
            return f"{seconds // 60}m"
        return f"{seconds // 3600}h"

    def _format_success(entry: FetchSuccess, *, local_replay: bool) -> str:
        header = _hdr(entry.tier, entry.tier != "camoufox")
        if local_replay:
            # Provider-origin cache state is historical on a local replay.  Never
            # repeat the live-provider cache line as though a new provider call ran.
            if entry.provider_cache == "exa-cached":
                header += "\n[source: original exa response was provider-cached]"
            elif entry.provider_cache == "firecrawl-default":
                header += ("\n[source: original firecrawl response may have used its "
                           "48h default cache window]")
            elif entry.provider_cache == "firecrawl-window":
                header += (f"\n[source: original firecrawl response may have used its "
                           f"{entry.provider_cache_hours}h cache window]")
            elif entry.provider_cache == "firecrawl-always-default":
                header += ("\n[source: original firecrawl response used its default "
                           "cache window because firecrawl has no \"always cache\" equivalent]")
            elif entry.provider_cache == "tavily-unspecified":
                header += "\n[source: original response came from Tavily Extract]"
            header += (f"\n[cache: local Valkey replay, age {_local_age(entry.stored_at)} — "
                       "pass max_age_hours=0 to force fresh]")
        else:
            # Existing provider disclosures stay byte-for-byte on live retrievals.
            if entry.provider_cache == "exa-cached" and max_age_hours != 0:
                header += ("\n[cache: exa served a cached copy — "
                           "pass max_age_hours=0 to force fresh]")
            elif entry.provider_cache == "firecrawl-always-default":
                header += ("\n[cache: exa was asked to always use its cache, but "
                           "firecrawl served this instead — firecrawl has no "
                           "\"always cache\" equivalent, so it used its own "
                           "default cache window]")
            elif entry.provider_cache in ("firecrawl-default", "firecrawl-window"):
                hours = 48 if entry.provider_cache_hours is None else entry.provider_cache_hours
                header += (f"\n[cache: firecrawl may serve up to {hours}h-old content — "
                           "pass max_age_hours=0 to force fresh]")
        return f"{header}\n\n{_finalize(entry.body, effective, entry.requested_cap)}"

    # Fast completed-result path.  A corrupt/down/slow cache is already represented
    # as a miss by the adapter and therefore cannot delay the provider cascade by
    # more than the bounded cache operation.
    if completed_cache_eligible:
        cached = await _completed_lookup()
        if cached is not None:
            validation_error = await _validate_public_url_async(url)
            if validation_error is not None:
                return f"RETRIEVAL_FAILED: {display_url} — {validation_error}"
            return _format_success(cached, local_replay=True)

    async def _produce(flight: FlightToken) -> tuple[FetchSuccess | _FetchFailure, bool]:
        # Double-check after becoming the leader: another request/process-local
        # predecessor may have populated Valkey between the first GET and this flight.
        if completed_cache_eligible:
            cached = await _completed_lookup()
            if cached is not None:
                return cached, True

        errors: list[str] = []

        async def attempt_exa() -> FetchSuccess | None:
            _TIER_DEADLINE_VAR.set(_tier_deadline(TIER_TIMEOUT))
            try:
                text, source = await asyncio.wait_for(
                    anyio.to_thread.run_sync(_exa_contents_sync, url, effective, max_age_hours,
                                             mode, question, abandon_on_cancel=True),
                    timeout=TIER_TIMEOUT)
                if len(text) >= min_chars:
                    return FetchSuccess(
                        stored_at=0,
                        tier="exa",
                        provider_cache="exa-cached" if source == "cached" else "exa-crawled",
                        provider_cache_hours=None,
                        result_kind=_result_kind("exa"),
                        effective_limit=effective,
                        requested_cap=None if is_extract else exa_cap,
                        body=text,
                    )
                errors.append(f"exa: thin ({len(text)} chars)")
            except (RetrievalError, asyncio.TimeoutError) as exc:
                errors.append(f"exa: {exc}")
            return None

        async def attempt_firecrawl() -> FetchSuccess | None:
            _TIER_DEADLINE_VAR.set(_tier_deadline(TIER_TIMEOUT))
            try:
                md = await asyncio.wait_for(
                    anyio.to_thread.run_sync(_firecrawl_sync, url, mode, question, max_age_hours,
                                             abandon_on_cancel=True),
                    timeout=TIER_TIMEOUT)
                if max_age_hours == -1:
                    provider_cache = "firecrawl-always-default"
                    provider_hours = None
                elif max_age_hours == 0:
                    provider_cache = "fresh"
                    provider_hours = 0
                elif max_age_hours is None:
                    provider_cache = "firecrawl-default"
                    provider_hours = None
                else:
                    provider_cache = "firecrawl-window"
                    provider_hours = max_age_hours
                return FetchSuccess(
                    stored_at=0,
                    tier="firecrawl",
                    provider_cache=provider_cache,
                    provider_cache_hours=provider_hours,
                    result_kind=_result_kind("firecrawl"),
                    effective_limit=effective,
                    requested_cap=None,
                    body=md,
                )
            except (RetrievalError, asyncio.TimeoutError) as exc:
                errors.append(f"firecrawl: {exc}")
            return None

        async def attempt_tavily() -> FetchSuccess | None:
            _TIER_DEADLINE_VAR.set(_tier_deadline(TIER_TIMEOUT))
            try:
                text = await asyncio.wait_for(
                    anyio.to_thread.run_sync(
                        _tavily_extract_sync, url, abandon_on_cancel=True
                    ),
                    timeout=TIER_TIMEOUT,
                )
                return FetchSuccess(
                    stored_at=0,
                    tier="tavily",
                    provider_cache="tavily-unspecified",
                    provider_cache_hours=None,
                    result_kind="full",
                    effective_limit=effective,
                    requested_cap=None,
                    body=text,
                )
            except (RetrievalError, asyncio.TimeoutError) as exc:
                errors.append(f"tavily: {exc}")
            return None

        async def attempt_camoufox() -> FetchSuccess | None:
            # camoufox ignores mode/question (no extraction model) — full body, no [mode:] line.
            try:
                text = await _camoufox_render(url, effective)
                if len(text) >= MIN_USEFUL_CHARS:
                    return FetchSuccess(
                        stored_at=0,
                        tier="camoufox",
                        provider_cache="none",
                        provider_cache_hours=None,
                        result_kind="rendered",
                        effective_limit=effective,
                        requested_cap=None,
                        body=text,
                    )
                errors.append(f"camoufox: thin ({len(text)} chars)")
            except Exception as exc:  # noqa: BLE001 — CancelledError (BaseException) still propagates
                errors.append(f"camoufox: {exc.__class__.__name__}: {exc}")
            return None

        # Tier order. Full-body auto restores the original budget policy: cheap Exa,
        # guarded local Camoufox, then paid Firecrawl. Semantic extraction skips the
        # browser because a full body cannot satisfy its summary/direct-answer contract.
        # Explicit never/always continue to be hard caller choices.
        tavily_steps = (attempt_tavily,) if tavily_eligible else ()
        if render == "always":
            order = (attempt_camoufox, *tavily_steps, attempt_firecrawl)
        elif is_extract:
            # Summary/question output is not constrained by Exa's 10k BODY limit.
            # Keep the cheap semantic tier first even when max_chars was explicitly
            # large; Camoufox cannot produce the promised answer/summary shape.
            order = (attempt_exa, attempt_firecrawl)
        elif render == "never":
            order = ((*tavily_steps, attempt_firecrawl, attempt_exa) if explicit_large
                     else (attempt_exa, *tavily_steps, attempt_firecrawl))
        elif explicit_large:
            order = (attempt_camoufox, *tavily_steps, attempt_firecrawl, attempt_exa)
        else:
            order = (attempt_exa, attempt_camoufox, *tavily_steps, attempt_firecrawl)

        for attempt in order:
            if not flight.cacheable:
                raise asyncio.CancelledError
            result = await attempt()
            if result is not None:
                if completed_cache_eligible and flight.cacheable:
                    await store_for_flight(_completed_fetch_cache, plan.key, result, flight)
                return result, False

        return _FetchFailure(tuple(errors)), False

    outcome, local_replay = await _fetch_singleflight.run(plan.key, _produce)
    if isinstance(outcome, _FetchFailure):
        trail = " | ".join(outcome.errors)
        if display_url != url:
            trail = trail.replace(url, display_url)
        return f"RETRIEVAL_FAILED: {display_url} — {trail}"
    if local_replay:
        validation_error = await _validate_public_url_async(url)
        if validation_error is not None:
            return f"RETRIEVAL_FAILED: {display_url} — {validation_error}"
    return _format_success(outcome, local_replay=local_replay)


# --------------------------------------------------------------------------- Research Index tools
# Firecrawl Research Index — a specialized AI/ML literature index (3M+ arXiv papers +
# GitHub artifacts) with SOTA recall on arXivQA (+18% over the next provider). Use these
# for AI/ML RESEARCH, not general web queries — general web_search stays on Exa, where
# Firecrawl has no measured advantage.
#  SCOPE NOTE: this index is arXiv-scoped, i.e. effectively AI/ML. For scholarly
# literature OUTSIDE that scope (medicine, law, economics, physics-beyond-arXiv, the
# humanities), use `web_search(category="publication")` instead — Exa's July-2026
# publications index fronts ~350M works and covers what the Research Index cannot.
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def research_papers(query: str, k: int = 8) -> str:
    """Search 3M+ arXiv AI/ML papers via the Firecrawl Research Index (state-of-the-art
    paper recall — far better than general web search for finding the right literature).
    Returns ranked papers: title, arXiv id, relevance score, abstract. Then call
    research_paper(paper_id, query=…) to verify a claim against full text before citing.

    SCOPE: arXiv-scoped, i.e. effectively AI/ML. For scholarly literature outside that
    scope (medicine, law, economics, humanities), use web_search(category="publication")
    — Exa's publications index (~350M works) covers what this one cannot.

    Args:
        query: natural-language research query (topic, method, benchmark, author).
        k: number of papers (1–25, default 8).
    """
    k = max(1, min(k, 25))
    _TIER_DEADLINE_VAR.set(_tier_deadline(RESEARCH_TIER_TIMEOUT))
    try:
        results = await asyncio.wait_for(
            anyio.to_thread.run_sync(_research_papers_sync, query, k, abandon_on_cancel=True),
            timeout=RESEARCH_TIER_TIMEOUT)
    except (RetrievalError, asyncio.TimeoutError) as e:
        return f"RESEARCH_FAILED: {query} — {e}"
    return _render_papers(query, results)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def research_paper(paper_id: str, query: str | None = None) -> str:
    """Inspect ONE paper from the Research Index by id (a paperId, or an arXiv id like
    "arxiv:2606.01509"). Without query → metadata (title, authors, categories, dates,
    abstract). With query → ALSO the top full-text passages answering it — use this to
    VERIFY a paper actually contains a method/dataset/result before relying on it.

    Args:
        paper_id: paperId or primaryId ("arxiv:NNNN.NNNNN") from research_papers.
        query: optional question; when set, returns claim-verification passages.
    """
    _TIER_DEADLINE_VAR.set(_tier_deadline(RESEARCH_TIER_TIMEOUT))
    try:
        data = await asyncio.wait_for(
            anyio.to_thread.run_sync(_research_paper_sync, paper_id, query, abandon_on_cancel=True),
            timeout=RESEARCH_TIER_TIMEOUT)
    except (RetrievalError, asyncio.TimeoutError) as e:
        return f"RESEARCH_FAILED: {paper_id} — {e}"
    return _render_paper(data, query)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def research_similar(paper_id: str, intent: str, k: int = 8,
                           mode: str = "similar", min_score: float = 0.0,
                           rerank: bool | None = None) -> str:
    """Expand from a seed paper to related work via the Research Index. `intent` is a
    REQUIRED natural-language description of the connection you want (e.g. "newer methods
    that improve on this routing", "the work this paper builds on"). Returns ranked
    related papers (same shape as research_papers).

    SCOPE: arXiv-scoped like research_papers — for non-AI/ML literature use
    web_search(category="publication").

    Args:
        paper_id: paperId or "arxiv:…" of the seed paper.
        intent: natural-language description of the kind of related work wanted.
        k: number of related papers (1–25, default 8; API allows up to 500).
        mode: "similar" (default), "citers" (papers citing this one), or
            "references" (papers this one cites). Unknown → "similar".
        min_score: renderer-side relevance floor (default 0.0 = off). k=8 already
            trims the low-score tail; raise this to filter more aggressively.
        rerank: optional bool; omitted from the request when None (API default is
            undocumented). Set True/False to force.
        (anchor — repeatable seed expansion — is not exposed; future work.)
    """
    k = max(1, min(k, 25))
    if mode not in ("similar", "citers", "references"):
        print(f"research_similar: unknown mode {mode!r} -> 'similar'", file=sys.stderr)
        mode = "similar"
    _TIER_DEADLINE_VAR.set(_tier_deadline(RESEARCH_TIER_TIMEOUT))
    try:
        results = await asyncio.wait_for(
            anyio.to_thread.run_sync(_research_similar_sync, paper_id, intent, k, mode, rerank,
                                     abandon_on_cancel=True),
            timeout=RESEARCH_TIER_TIMEOUT)
    except (RetrievalError, asyncio.TimeoutError) as e:
        return f"RESEARCH_FAILED: {paper_id} — {e}"
    return _render_papers(f"related to {paper_id} — {intent}", results, min_score=min_score)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def research_github(query: str, k: int = 8,
                          passages: int = DEV_PASSAGES_DEFAULT,
                          types: list[str] | None = None,
                          repos: list[str] | None = None) -> str:
    """Search developer primary sources via the Firecrawl **Developer Index**: GitHub
    issues, merged pull requests and repository READMEs, PLUS curated documentation
    sites. Returns the matched passages in Markdown, so tables and code blocks survive.
    Use to find the CODE behind a paper, the issue where a bug was reported and fixed,
    an API contract, or the discussion behind an error message.

    Args:
        query: natural-language query (method, kernel, repo topic, error message).
        k: number of results (1–25, default 8).
        passages: matched passages per result (1–5, default 2).
        types: restrict to any of exactly "doc", "issue", "pull_request", "readme".
            NOTE the request spelling is `pull_request` (snake_case). The response's
            `repos[].types` object uses camelCase (`pullRequest`) — echoing a key from
            there back into this argument is refused, not silently ignored.
        repos: "owner/repo" slugs. Scopes only the repository half of the index, so when
            `types` is also given it must contain at least one of issue/pull_request/readme.

    Falls back to the de-documented legacy `/v2/search/research/github` ONLY when the
    Developer Index genuinely fails (transport, HTTP, malformed envelope, or every
    requested type unavailable) — never on a legitimate empty result, and never when
    `types`/`repos` were supplied, since the legacy endpoint accepts only query+k and
    would silently answer a different question than the one asked.
    """
    k = max(1, min(k, 25))
    passages = max(1, min(passages, DEV_PASSAGES_MAX))

    # --- semantic validation, in the TOOL BODY and BEFORE any request ---------------
    # Deliberately not inside _developer_search_sync: a RetrievalError raised there is
    # caught below and routed to the FALLBACK, which would contradict "no request, no
    # fallback" and make the "no request issued" tests unverifiable.
    # A distinct RESEARCH_REFUSED prefix keeps a deterministic refusal (which never
    # heals, and which an agent would otherwise retry forever) separable from the
    # transient RESEARCH_FAILED family.
    if types:
        bad = [t for t in types if t not in DEV_TYPES]
        if bad:
            return (f"RESEARCH_REFUSED: {query} — unknown type: {bad[0]} "
                    f"(expected {', '.join(DEV_TYPES)})")
        if repos and not any(t in DEV_REPO_TYPES for t in types):
            return (f"RESEARCH_REFUSED: {query} — repos cannot match any requested type; "
                    f"add one of {', '.join(DEV_REPO_TYPES)} to types, or drop repos")

    _TIER_DEADLINE_VAR.set(_tier_deadline(RESEARCH_TIER_TIMEOUT))
    dev_error: str | None = None
    try:
        envelope = await asyncio.wait_for(
            anyio.to_thread.run_sync(_developer_search_sync, query, k, passages, types, repos,
                                     abandon_on_cancel=True),
            timeout=RESEARCH_TIER_TIMEOUT)
    except (RetrievalError, asyncio.TimeoutError) as e:
        dev_error = str(e) or e.__class__.__name__
    else:
        cov = envelope.get("coverage")
        print(f"research_github: coverage={cov} reranked={envelope.get('reranked')}", file=sys.stderr)
        # Quantify over DEV_TYPES, not `types or DEV_TYPES`: the filter guard below means
        # every path reaching here with types set never falls back anyway, so the
        # request-scoped form could not change an outcome. The isinstance/bool guard IS
        # load-bearing — `all(...)` over an absent/empty coverage is vacuously True and
        # would fire the fallback on every response missing the key.
        all_unavailable = (isinstance(cov, dict) and bool(cov)
                           and all(cov.get(t) in ("unavailable", "degraded") for t in DEV_TYPES))
        if all_unavailable and not (envelope.get("results") or []):
            dev_error = "coverage: all requested types unavailable"
        else:
            return _render_developer(query, envelope)

    # --- failure path --------------------------------------------------------------
    if types or repos:
        # The legacy endpoint takes only query+k. Broadening the search behind the
        # caller's back is worse than failing: a types=["doc"] request would come back
        # as GitHub repo artifacts, and a repo-scoped one as unrelated repositories.
        return (f"RESEARCH_FAILED: {query} — developer: {dev_error}; "
                "legacy fallback skipped (cannot honor types/repos)")

    print(f"research_github: developer-index fallback: {dev_error}", file=sys.stderr)
    _TIER_DEADLINE_VAR.set(_tier_deadline(RESEARCH_TIER_TIMEOUT))  # re-anchor for the second tier
    try:
        legacy = await asyncio.wait_for(
            anyio.to_thread.run_sync(_research_github_sync, query, k, abandon_on_cancel=True),
            timeout=RESEARCH_TIER_TIMEOUT)
    except (RetrievalError, asyncio.TimeoutError) as e2:
        return f"RESEARCH_FAILED: {query} — developer: {dev_error} | legacy: {e2}"
    header = (f"[fallback: developer index unavailable ({dev_error}) — served by the "
              "de-documented legacy /v2/search/research/github]")
    return header + "\n" + _render_github(query, legacy)


def _configure_http_logging() -> None:
    mcp.settings.log_level = "WARNING"
    logging.getLogger().setLevel(logging.WARNING)
    cache_logger = logging.getLogger("web_retrieval.fetch_cache")
    cache_logger.setLevel(logging.INFO)
    cache_handler = logging.StreamHandler(sys.stderr)
    cache_handler.setFormatter(logging.Formatter("%(message)s"))
    cache_logger.handlers.clear()
    cache_logger.addHandler(cache_handler)
    cache_logger.propagate = False


def main(argv: list[str] | None = None) -> int:
    """Run the MCP server over stdio (default) or stateless Streamable HTTP."""
    parser = argparse.ArgumentParser(prog="web-retrieval-mcp")
    parser.add_argument("--http", action="store_true", help="use Streamable HTTP")
    parser.add_argument(
        "--host",
        default=os.environ.get("WEB_RETRIEVAL_MCP_HOST", "127.0.0.1"),
        help="HTTP bind host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("WEB_RETRIEVAL_MCP_PORT", "8100")),
        help="HTTP bind port (default: 8100)",
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.http:
        _configure_http_logging()
        mcp.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            stateless_http=True,
            json_response=True,
        )
    else:
        mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
