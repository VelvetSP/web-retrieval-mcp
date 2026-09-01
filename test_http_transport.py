""" — streamable-http serving mode smoke test (server-side proof).

Boots server.py with `--http` on an ephemeral port, connects with the mcp SDK's
streamable-http client, and asserts `initialize` + `tools/list` return exactly the
6 expected tool names. No API keys / network needed — the daemon starts without
resolving any provider key (keys resolve lazily per tool call).

Runs in the unit suite globbed by `run-tests.sh`.

Scope note: this proves the SERVER serves over http. It does NOT exercise Claude
Code's client-side 60 s HTTP timer (the `"timeout": 300000` fix) — that is verified
separately with a fresh session and a deliberate >60 s call (plan WP1.6 / WP4).
"""
import asyncio
import os
import socket
import subprocess
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client   #  v2 dropped the
# `streamablehttp_client` alias, and the context manager now yields a 2-TUPLE — the third
# element (get_session_id) was removed.

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_ROOT = os.path.join(HERE, "src")
EXPECTED_TOOLS = {
    "web_search", "web_fetch",
    "research_papers", "research_paper", "research_similar", "research_github",
}

fails = 0
def check(label, cond, detail=""):
    global fails
    if not cond:
        fails += 1
    print(f"{'OK  ' if cond else 'FAIL'}  {label}  {detail}")


def free_port():
    """Bind-then-release an ephemeral port so the daemon can claim it next."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_tcp(port, deadline):
    """Poll TCP connect until the daemon's listener is up or the deadline passes."""
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.2)
    return False


async def fetch_tool_names(url, deadline):
    """initialize + tools/list, retrying past app-mount races until the deadline."""
    last = None
    while time.monotonic() < deadline:
        try:
            async with streamable_http_client(url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    resp = await session.list_tools()
                    return {t.name for t in resp.tools}
        except Exception as e:  # noqa: BLE001 — pre-ready connection errors are retryable
            last = e
            await asyncio.sleep(0.3)
    raise RuntimeError(f"never became ready before deadline; last error: {last!r}")


def main():
    port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    env = dict(os.environ, WEB_RETRIEVAL_MCP_PORT=str(port), WEB_RETRIEVAL_MCP_CACHE="off")
    env["PYTHONPATH"] = SOURCE_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "web_retrieval_mcp", "--http"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15.0
        if not wait_tcp(port, deadline):
            # daemon never bound — surface its stderr for diagnosis
            proc.terminate()
            out, err = proc.communicate(timeout=5)
            print("     daemon stderr:\n" + err.decode("utf-8", "replace")[-2000:])
            check("daemon bound the port within 15s", False, f"port {port}")
            return
        check("daemon bound the port", True, f"127.0.0.1:{port}")

        names = asyncio.run(fetch_tool_names(url, deadline))
        check("tools/list returned exactly the 6 expected tools", names == EXPECTED_TOOLS,
              f"got {sorted(names)}")
        missing = EXPECTED_TOOLS - names
        extra = names - EXPECTED_TOOLS
        if missing:
            check("no missing tools", False, f"missing {sorted(missing)}")
        if extra:
            check("no unexpected tools", False, f"extra {sorted(extra)}")

        #  DNS-rebinding protection moved from the constructor into
        # run()/streamable_http_app() under SDK v2. Until now it was asserted only by a
        # COMMENT in server.py — and it is exactly the mechanism the migration relocated,
        # so pin it with a live probe.
        # The auto-allowlist entries are host:PORT patterns, so the positive control MUST
        # keep the port: a bare `Host: 127.0.0.1` is itself rejected with 421.
        import http.client
        def _post_with_host(host_header):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.request("POST", "/mcp", body=b"{}", headers={
                    "Host": host_header, "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream"})
                return conn.getresponse().status
            finally:
                conn.close()
        check("bogus Host -> 421", _post_with_host("evil.example.com") == 421)
        check("bare Host without port -> 421 (allowlist is host:port)",
              _post_with_host("127.0.0.1") == 421)
        check("Host with port -> not 421 (positive control)",
              _post_with_host(f"127.0.0.1:{port}") != 421)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    print(f"\n{'ALL HTTP-TRANSPORT TESTS PASS' if fails == 0 else str(fails) + ' FAILURE(S)'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
