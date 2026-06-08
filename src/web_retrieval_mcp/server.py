#!/usr/bin/env python3
"""web-retrieval MCP server — a drop-in web search/fetch tool stack.

Exposes two MCP tools, designed to be a higher-fidelity replacement for an
agent's built-in web tools (which often return source-conflating snippets):

  web_search(query, num_results, mode)  -> Exa /search (neural/keyword/auto)
  web_fetch(url, render, max_chars)     -> Exa /contents -> [camoufox] -> Firecrawl

Design notes:
  - web_fetch is a tiered fetch chain. Tier 1 is Exa /contents; the final
    backstop is Firecrawl. An optional local headless-browser render (camoufox)
    sits between them but is OPT-IN ONLY (render="always"), because it is the one
    tier that runs a real browser on this machine and can therefore reach the
    local network — keeping it out of the default path shrinks SSRF exposure.
  - Keys are resolved IN-PROCESS and cross-platform, cheapest source first:
    env vars → dotenv-style key file → the optional `keyring` library (native
    secret store on macOS/Windows/Linux) → OS-native secret CLI (`security` on
    macOS, `secret-tool` on Linux). Never passed on a command line — argv is
    world-visible via `ps`. An unexpanded ${...} config literal counts as absent.
  - stdout is JSON-RPC ONLY. Tools RETURN strings; nothing here prints to stdout.
    All diagnostics go to stderr. (A stray stdout print corrupts the protocol.)
  - Blocking I/O (Keychain subprocess + urllib POSTs) runs in a worker thread via
    anyio.to_thread.run_sync so it never stalls the async event loop under
    concurrent calls. Each tier is wrapped in asyncio.wait_for.
  - The camoufox render is bounded by a semaphore + timeout (one browser per
    fetch) and renders in-process via the native AsyncCamoufox API. Its imports
    are lazy, so search + Exa/Firecrawl fetch work without the browser stack
    installed — you only need `camoufox` + `playwright` for render="always".
  - Headless/cron caveat: a desktop secret store may be locked or absent. For
    scheduled / non-interactive runs, supply keys via env (EXA_API_KEY /
    FIRECRAWL_API_KEY) or a key file instead of relying on a secret store.

API shapes (verified live 2026-06-02):
  Exa search   POST https://api.exa.ai/search    header x-api-key
               body {query,type,numResults,contents:{text:{maxCharacters},highlights}}
               resp {results:[{title,url,publishedDate,highlights,text}]}
  Exa contents POST https://api.exa.ai/contents   header x-api-key
               body {urls:[url],text:{maxCharacters}[,maxAgeHours]}
               resp {results:[{text,...}]}   (livecrawl deprecated -> maxAgeHours)
  Firecrawl    POST https://api.firecrawl.dev/v2/scrape  header Authorization: Bearer
               body {url,formats:["markdown"]}
               resp {success,data:{markdown,metadata:{statusCode,error?}}}
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import anyio
from mcp.server.fastmcp import FastMCP

EXA_SEARCH_URL = "https://api.exa.ai/search"
EXA_CONTENTS_URL = "https://api.exa.ai/contents"
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"

HTTP_TIMEOUT = 30          # per-call socket timeout (s)
TIER_TIMEOUT = 45          # per-tier wall-clock cap (s)
RENDER_TIMEOUT = 40        # camoufox navigation+render cap (s)
RENDER_CONCURRENCY = 2     # max simultaneous headless browsers
MIN_USEFUL_CHARS = 200     # below this a tier is "empty/boilerplate" -> next tier

_render_sem = asyncio.Semaphore(RENDER_CONCURRENCY)
mcp = FastMCP("web-retrieval")


class RetrievalError(Exception):
    """Raised (never sys.exit) so a tool failure can't kill the server."""


def _validate_public_url(url: str) -> None:
    """SSRF guard. Reject non-http(s) schemes and any host that resolves to a
    non-public IP (loopback, private/RFC-1918, link-local, reserved). The local
    camoufox tier runs a real browser on this machine, so without this an agent
    — or injected page content steering web_fetch — could reach file://,
    localhost, or LAN/cloud-metadata endpoints. Validated up front so internal
    URLs never reach the external Exa/Firecrawl APIs either."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise RetrievalError(f"refused non-http(s) URL scheme: {parsed.scheme or '(none)'}")
    host = parsed.hostname
    if not host:
        raise RetrievalError("refused URL with no host")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise RetrievalError(f"cannot resolve host '{host}': {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # is_global=False catches loopback/private/link-local/unspecified;
        # is_reserved catches reserved + NAT64; is_multicast is NOT covered by
        # either (e.g. SSDP 239.255.255.250, 224.0.0.1, ff02::1 are
        # is_global=True, is_reserved=False) — reject it explicitly.
        if not ip.is_global or ip.is_reserved or ip.is_multicast:
            raise RetrievalError(f"refused non-public address for '{host}': {ip}")


# ----------------------------------------------------------------------------- keys
def _looks_like_secret(v: str | None) -> bool:
    """True if v is a real secret, not an unexpanded ${...} config literal.
    Strips first so a whitespace-padded literal can't slip through."""
    if not v:
        return False
    s = v.strip()
    return bool(s) and not (s.startswith("${") and s.endswith("}"))


def _config_dir() -> Path:
    """Cross-platform per-user config dir for the optional key file."""
    override = os.environ.get("WEB_RETRIEVAL_MCP_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(base) / "web-retrieval-mcp"
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "web-retrieval-mcp"


def _key_from_file(env_names: tuple[str, ...]) -> str | None:
    """Optional dotenv-style key file (KEY=value lines), zero dependencies, every
    OS. Path: $WEB_RETRIEVAL_MCP_ENV_FILE, else <config-dir>/keys.env."""
    path_str = os.environ.get("WEB_RETRIEVAL_MCP_ENV_FILE")
    path = Path(path_str) if path_str else _config_dir() / "keys.env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    wanted = set(env_names)
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() in wanted:
            v = v.strip().strip('"').strip("'")
            if _looks_like_secret(v):
                return v
    return None


def _key_from_keyring(service: str, env_names: tuple[str, ...]) -> str | None:
    """Optional `keyring` library — native secret store on macOS (Keychain),
    Windows (Credential Locker), and Linux (Secret Service / KWallet).
    Install with `pip install web-retrieval-mcp[keyring]`. Looked up under
    service name 'web-retrieval-mcp', username = the key name."""
    try:
        import keyring  # noqa: PLC0415 — optional dependency, imported lazily
    except ImportError:
        return None
    try:
        for name in (service, *env_names):
            v = keyring.get_password("web-retrieval-mcp", name)
            if _looks_like_secret(v):
                return v.strip()
    except Exception:  # noqa: BLE001 — a broken keyring backend must not crash lookup
        return None
    return None


def _key_from_os_cli(service: str) -> str | None:
    """OS-native secret CLI fallback, no Python deps:
      macOS → `security find-generic-password -s <service> -w`
      Linux → `secret-tool lookup service <service>` (libsecret), if installed."""
    if sys.platform == "darwin":
        cmd = ["security", "find-generic-password", "-s", service, "-w"]
    elif sys.platform.startswith("linux") and shutil.which("secret-tool"):
        cmd = ["secret-tool", "lookup", "service", service]
    else:
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    return None


def _get_key(*, env_names: tuple[str, ...], service: str) -> str:
    """Resolve a secret cross-platform, cheapest/safest source first:
      1. environment variables (universal; required for headless / CI)
      2. a dotenv-style key file (<config-dir>/keys.env)
      3. the optional `keyring` library (native store on macOS/Windows/Linux)
      4. an OS-native secret CLI (macOS `security`, Linux `secret-tool`)
    An unexpanded ${...} config literal is treated as absent throughout. Secrets
    resolve IN-PROCESS, never on a command line (argv is world-visible via `ps`)."""
    for name in env_names:
        v = os.environ.get(name)
        if _looks_like_secret(v):
            return v.strip()
    for resolver in (lambda: _key_from_file(env_names),
                     lambda: _key_from_keyring(service, env_names),
                     lambda: _key_from_os_cli(service)):
        v = resolver()
        if _looks_like_secret(v):
            return v.strip()
    raise RetrievalError(
        f"no usable secret for '{service}'. Set one of the env vars "
        f"[{', '.join(env_names)}], add it to a keys.env file, or store it via "
        f"`keyring` / your OS secret tool. (Headless? env vars are required — a "
        f"desktop secret store may be locked or absent.)")


def _scrub(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***REDACTED***")
    return text


# ----------------------------------------------------------------------------- http
def _post_json(url: str, payload: dict, headers: dict, secret: str) -> dict:
    """Blocking in-process JSON POST. Runs in a worker thread (see callers)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500] if e.fp else ""
        raise RetrievalError(_scrub(f"HTTP {e.code} from {url}: {detail}", secret))
    except (urllib.error.URLError, OSError) as e:
        raise RetrievalError(_scrub(f"network error calling {url}: {e}", secret))
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise RetrievalError(_scrub(f"non-JSON response from {url}: {body[:300]}", secret))


def _exa_key() -> str:
    return _get_key(env_names=("EXA_API_KEY",), service="EXA_API_KEY")


def _firecrawl_key() -> str:
    return _get_key(env_names=("FIRECRAWL_API_KEY",), service="FIRECRAWL_API_KEY")


def _tavily_key() -> str:
    return _get_key(env_names=("TAVILY_API_KEY",), service="TAVILY_API_KEY")


# --------------------------------------------------------------------------- tiers (blocking)
def _exa_search_sync(query: str, num_results: int, mode: str) -> list[dict]:
    key = _exa_key()
    payload = {
        "query": query, "type": mode, "numResults": num_results,
        "contents": {"text": {"maxCharacters": 4000}, "highlights": True},
    }
    resp = _post_json(EXA_SEARCH_URL, payload, {"x-api-key": key}, key)
    return resp.get("results") or []


def _tavily_search_sync(query: str, num_results: int) -> list[dict]:
    """Call Tavily Search API and map results to the same shape as Exa."""
    from tavily import TavilyClient  # noqa: PLC0415 — optional dependency, imported lazily

    client = TavilyClient(api_key=_tavily_key())
    resp = client.search(query=query, max_results=num_results, search_depth="advanced")
    results: list[dict] = []
    for r in resp.get("results") or []:
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "publishedDate": r.get("published_date", ""),
            "highlights": [],
            "text": r.get("content", ""),
        })
    return results


def _exa_contents_sync(url: str, max_chars: int, max_age_hours: int | None = None) -> str:
    key = _exa_key()
    # Exa /contents OpenAPI declares text.maxCharacters maximum:10000. Exa clamps
    # rather than 400s on larger values, but the schema max is authoritative — cap
    # the request at 10000 so a future strict-validation change can't 400 us. The
    # caller's full max_chars still bounds the tier OUTPUT (camoufox/Firecrawl
    # tiers can return more).
    text_opts = {"maxCharacters": min(max_chars, 10000)}
    payload: dict = {"urls": [url], "text": text_opts}
    # maxAgeHours = Exa's freshness control (replaces deprecated livecrawl).
    # Omit when None to preserve Exa's default caching.
    if max_age_hours is not None:
        payload["maxAgeHours"] = max_age_hours
    resp = _post_json(EXA_CONTENTS_URL, payload, {"x-api-key": key}, key)
    results = resp.get("results") or []
    if not results:
        return ""
    return (results[0].get("text") or "").strip()


def _firecrawl_sync(url: str) -> str:
    key = _firecrawl_key()
    payload = {"url": url, "formats": ["markdown"]}
    resp = _post_json(FIRECRAWL_SCRAPE_URL, payload, {"Authorization": f"Bearer {key}"}, key)
    if not resp.get("success"):
        raise RetrievalError(_scrub(f"firecrawl success=false: {json.dumps(resp)[:300]}", key))
    data = resp.get("data") or {}
    meta = data.get("metadata") or {}
    status = meta.get("statusCode")
    if status is not None:
        code = int(status) if str(status).isdigit() else 0
        if not (200 <= code < 300):
            raise RetrievalError(f"firecrawl upstream status {status} for {url}")
    if meta.get("error"):
        raise RetrievalError(f"firecrawl error for {url}: {meta.get('error')}")
    md = (data.get("markdown") or "").strip()
    if len(md) < MIN_USEFUL_CHARS:
        raise RetrievalError(f"firecrawl markdown too short ({len(md)} chars) for {url}")
    return md


async def _camoufox_render(url: str) -> str:
    """Tier-2: headless camoufox render -> visible body text. Bounded + timed.

    Single navigation (domcontentloaded), then a best-effort wait for network
    idle — so a long-polling page that never goes idle still yields the content
    that loaded, WITHOUT a second goto (which would re-request the page and could
    mask a real navigation failure). Only the idle wait's timeout is swallowed;
    a genuine goto failure (DNS, auth redirect) propagates to the Firecrawl tier.

    Imports are local so the heavy browser stack is only required for this path.
    """
    from camoufox.async_api import AsyncCamoufox
    from playwright.async_api import TimeoutError as PWTimeout

    async def _go() -> str:
        async with _render_sem:
            async with AsyncCamoufox(headless=True) as browser:
                page = await browser.new_page()
                await page.goto(url, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except PWTimeout:
                    pass  # page loaded but never idled; use what's there
                return (await page.inner_text("body")).strip()

    return await asyncio.wait_for(_go(), timeout=RENDER_TIMEOUT)


# --------------------------------------------------------------------------- formatting
def _render_search(query: str, results: list[dict]) -> str:
    if not results:
        return f"No results for: {query}"
    blocks = [f"# Web search: {query}\n"]
    sources = []
    for i, r in enumerate(results, 1):
        url = r.get("url", "")
        title = r.get("title") or "(untitled)"
        pub = r.get("publishedDate") or ""
        hl = r.get("highlights") or []
        text = (r.get("text") or "").strip()
        sources.append(f"[{i}] {url}")
        block = [f"## {i}. {title}", f"URL: {url}" + (f"  ·  {pub}" if pub else "")]
        if hl:
            block.append("Highlights:\n" + "\n".join(f"  - {h.strip()}" for h in hl[:4]))
        if text:
            block.append(text[:1200] + ("…" if len(text) > 1200 else ""))
        blocks.append("\n".join(block))
    blocks.append("Sources:\n" + "\n".join(sources))
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- tools
@mcp.tool()
async def web_search(query: str, num_results: int = 8, mode: str = "auto") -> str:
    """Search the web via Exa or Tavily. Returns one block per result,
    each with its OWN title, URL, highlights, and text — never a merged summary —
    plus a Sources trailer.

    Set WEB_SEARCH_PROVIDER=tavily to use Tavily instead of Exa (default).

    Args:
        query: the search query.
        num_results: how many results (default 8).
        mode: "auto" (default), "neural", or "keyword" (Exa only; ignored by Tavily).
    """
    provider = os.environ.get("WEB_SEARCH_PROVIDER", "exa").strip().lower()
    try:
        if provider == "tavily":
            results = await asyncio.wait_for(
                anyio.to_thread.run_sync(_tavily_search_sync, query, num_results),
                timeout=TIER_TIMEOUT,
            )
        else:
            if mode not in ("auto", "neural", "keyword"):
                mode = "auto"
            results = await asyncio.wait_for(
                anyio.to_thread.run_sync(_exa_search_sync, query, num_results, mode),
                timeout=TIER_TIMEOUT,
            )
    except (RetrievalError, asyncio.TimeoutError) as e:
        return f"SEARCH_FAILED: {query} — {e}"
    return _render_search(query, results)


@mcp.tool()
async def web_fetch(url: str, render: str = "auto", max_chars: int = 20000,
                    max_age_hours: int | None = None) -> str:
    """Fetch a single URL's readable content. Tier chain: Exa contents → Firecrawl.
    The local camoufox headless-browser render is OPT-IN (render="always") only —
    it is the one tier that runs a real browser on this machine and can therefore
    reach the local network, so it is kept out of the default path to shrink SSRF
    exposure. Returns content with a `[served by: …]` provenance header.

    Args:
        url: the URL to fetch.
        render: "auto" (default) and "never" → Exa then Firecrawl, NO local
            browser. "always" → force the local camoufox browser (Firecrawl
            backstop on failure). Only "always" runs the SSRF-exposed tier, and
            then only on the URL the caller explicitly passed.
        max_chars: max characters of text to request (default 20000).
        max_age_hours: Exa freshness window (tier 1). None = Exa default cache;
            0 = force fresh. Only affects the Exa /contents tier.

    SSRF note: the camoufox tier follows redirects and re-resolves DNS, so a
    hostile redirect could point it at a LAN/loopback host. Mitigated by gating it
    to render="always" (no auto-escalation from a hostile page) + reordering
    Firecrawl (server-side, immune) ahead of it. Residual: a caller that passes
    render="always" with a hostile-redirecting URL is still exposed on the camoufox
    tier; the up-front _validate_public_url + multicast block cover the initial URL
    only, not post-redirect hops. Full closure would need a validating forward proxy.
    """
    try:
        _validate_public_url(url)
    except RetrievalError as e:
        return f"RETRIEVAL_FAILED: {url} — {e}"

    errors: list[str] = []

    # Tier 1 — Exa contents (skip only when render=always forces the browser first)
    if render != "always":
        try:
            text = await asyncio.wait_for(
                anyio.to_thread.run_sync(_exa_contents_sync, url, max_chars, max_age_hours),
                timeout=TIER_TIMEOUT)
            if len(text) >= MIN_USEFUL_CHARS:
                return f"[served by: exa]  {url}\n\n{text[:max_chars]}"
            errors.append(f"exa: thin ({len(text)} chars)")
        except (RetrievalError, asyncio.TimeoutError) as e:
            errors.append(f"exa: {e}")

    # camoufox render — OPT-IN ONLY (render="always"). The SSRF-exposed local
    # browser tier is deliberately NOT in the auto/never path: auto/never fall
    # through to Firecrawl (server-side, immune).
    if render == "always":
        try:
            text = await _camoufox_render(url)
            if len(text) >= MIN_USEFUL_CHARS:
                return f"[served by: camoufox]  {url}\n\n{text[:max_chars]}"
            errors.append(f"camoufox: thin ({len(text)} chars)")
        except Exception as e:  # noqa: BLE001 — render is best-effort; CancelledError (BaseException) still propagates
            errors.append(f"camoufox: {e.__class__.__name__}: {e}")

    # Firecrawl — server-side fallback for auto/never AND camoufox-failure backstop
    try:
        md = await asyncio.wait_for(
            anyio.to_thread.run_sync(_firecrawl_sync, url), timeout=TIER_TIMEOUT)
        return f"[served by: firecrawl]  {url}\n\n{md[:max_chars]}"
    except (RetrievalError, asyncio.TimeoutError) as e:
        errors.append(f"firecrawl: {e}")

    return f"RETRIEVAL_FAILED: {url} — " + " | ".join(errors)


def main() -> None:
    """Console-script / `python -m web_retrieval_mcp` entry point."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
