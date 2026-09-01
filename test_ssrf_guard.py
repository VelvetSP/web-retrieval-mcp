"""Targeted unit test for the Camoufox per-request SSRF guard.
Run with the camoufox venv python so server.py's deps import.
"""
import asyncio, sys, ipaddress
from pathlib import Path

import os as _os
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
import web_retrieval_mcp.server as m

fails = 0
def check(label, got, want):
    global fails
    ok = got == want
    if not ok: fails += 1
    print(f"{'OK  ' if ok else 'FAIL'}  {label}: got={got} want={want}")

# --- _ip_forbidden predicate ---
print("== _ip_forbidden ==")
check("127.0.0.1 forbidden",        m._ip_forbidden(ipaddress.ip_address("127.0.0.1")), True)
check("10.0.0.1 forbidden",         m._ip_forbidden(ipaddress.ip_address("10.0.0.1")), True)
check("192.168.1.1 forbidden",      m._ip_forbidden(ipaddress.ip_address("192.168.1.1")), True)
check("169.254.169.254 forbidden",  m._ip_forbidden(ipaddress.ip_address("169.254.169.254")), True)  # AWS metadata
check("::1 forbidden",              m._ip_forbidden(ipaddress.ip_address("::1")), True)
check("239.255.255.250 forbidden",  m._ip_forbidden(ipaddress.ip_address("239.255.255.250")), True)  # SSDP multicast
check("8.8.8.8 allowed",           m._ip_forbidden(ipaddress.ip_address("8.8.8.8")), False)
check("1.1.1.1 allowed",           m._ip_forbidden(ipaddress.ip_address("1.1.1.1")), False)

# --- _host_is_public (real DNS) ---
print("== _host_is_public ==")
check("localhost not public",       m._host_is_public("localhost"), False)
check("127.0.0.1 not public",       m._host_is_public("127.0.0.1"), False)
check("169.254.169.254 not public", m._host_is_public("169.254.169.254"), False)
check("10.0.0.1 not public",        m._host_is_public("10.0.0.1"), False)
check("empty not public",           m._host_is_public(""), False)
check("nxdomain fail-closed",       m._host_is_public("no-such-host-xyz123.invalid"), False)
check("example.com public",         m._host_is_public("example.com"), True)
check("cloudflare-dns public",      m._host_is_public("1.1.1.1"), True)

# --- route guard decision via fake route ---
print("== route guard (abort vs continue_) ==")
class FakeReq:
    def __init__(self, url): self.url = url
class FakeRoute:
    def __init__(self, url):
        self.request = FakeReq(url); self.action = None
    async def continue_(self): self.action = "continue"
    async def abort(self):     self.action = "abort"

async def drive():
    loop = asyncio.get_running_loop()
    guard = m._make_route_guard(loop, {})
    cases = [
        ("https://example.com/page", "continue"),    # public main
        ("https://1.1.1.1/x",        "continue"),    # public IP literal
        ("http://127.0.0.1:8080/x",  "abort"),       # loopback redirect
        ("http://169.254.169.254/latest/meta-data/", "abort"),  # cloud metadata
        ("http://10.0.0.5/x",        "abort"),       # private subresource
        ("http://[::1]/x",           "abort"),       # ipv6 loopback
        ("http://no-such-host-xyz123.invalid/x", "abort"),  # nxdomain fail-closed
    ]
    for url, want in cases:
        r = FakeRoute(url)
        await guard(r)
        check(f"guard {url}", r.action, want)

asyncio.run(drive())

# --- request observer defers DNS; _flush_pending resolves off-loop ---
print("== observer + flush ==")
class FakeReqRT:
    def __init__(self, url, rt): self.url = url; self.resource_type = rt

# observer records document requests WITHOUT resolving DNS on the loop
resolved_calls = []
_orig_hip = m._host_is_public
m._host_is_public = lambda h: (resolved_calls.append(h), _orig_hip(h))[1]
pending, hcache, blk = [], {}, []
obs = m._make_request_observer(pending, hcache, blk)
obs(FakeReqRT("http://127.0.0.1/x", "document"))
obs(FakeReqRT("https://cdn.example.com/s.js", "script"))   # non-document ignored
check("observer records document url+host", pending, [("http://127.0.0.1/x", "127.0.0.1")])
check("observer ignores non-document", len(pending), 1)
check("observer does NOT resolve DNS on the loop", resolved_calls, [])
m._host_is_public = _orig_hip

# observer fail-closed when building the record raises
class BadReq:
    resource_type = "document"
    @property
    def url(self): raise ValueError("boom")
blk2 = []
m._make_request_observer([], {}, blk2)(BadReq())
check("observer fail-closed -> <resolve-error>", blk2, ["<resolve-error>"])

# _flush_pending resolves off-loop, moves forbidden to blocked, caches, drains.
# IP literals (no external DNS) so the unit runs offline — 8.8.8.8 public, 127.0.0.1 forbidden.
async def drive_flush():
    loop = asyncio.get_running_loop()
    pend = [("http://127.0.0.1/x", "127.0.0.1"), ("https://8.8.8.8/", "8.8.8.8")]
    hc, b = {}, []
    await m._flush_pending(loop, pend, hc, b)
    return pend, hc, b
_pend, _hc, _blk = asyncio.run(drive_flush())
check("flush: forbidden host -> blocked url", _blk, ["http://127.0.0.1/x"])
check("flush: public host not blocked", "https://8.8.8.8/" in _blk, False)
check("flush: 127.0.0.1 cached False", _hc.get("127.0.0.1"), False)
check("flush: 8.8.8.8 cached True", _hc.get("8.8.8.8"), True)
check("flush: drains pending", _pend, [])

# The browser can physically issue a forbidden redirect hop before teardown; only
# the response body is withheld. Pin the disclosure until a validating proxy closes
# that network-layer residual.
print("== network-layer residual disclosure ==")
_obs_doc = m._make_request_observer.__doc__ or ""
_wf_doc = m.web_fetch.__doc__ or ""
check("observer docstring still discloses the issued-before-teardown residual",
      "the request is physically issued before teardown" in _obs_doc, True)
check("web_fetch docstring still discloses the Camoufox SSRF residual",
      "full closure would need a validating forward proxy" in _wf_doc.casefold(), True)

print(f"\n{'ALL  TESTS PASS' if fails==0 else str(fails)+' FAILURE(S)'}")
sys.exit(1 if fails else 0)
