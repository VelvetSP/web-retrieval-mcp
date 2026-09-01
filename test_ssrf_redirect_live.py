""" LIVE end-to-end test — calls the REAL _camoufox_render.
Proves redirect-SSRF is blocked (body never returned) while legit public
redirects still render. Run with camoufox venv python."""
import asyncio, os as _os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
import web_retrieval_mcp.server as m

fails = 0
def check(label, cond, detail=""):
    global fails
    if not cond: fails += 1
    print(f"{'OK  ' if cond else 'FAIL'}  {label}  {detail}")

async def call(url):
    try:
        body = await m._camoufox_render(url)
        return ("ok", body)
    except Exception as e:
        return ("raised", f"{e.__class__.__name__}: {str(e)[:80]}")

async def main():
    # A — regression: legit public redirect must still render.
    kindA, valA = await call("https://httpbin.org/redirect-to?url=https://example.com/")
    check("A: legit public redirect renders (not broken)",
          kindA == "ok" and "example" in valA.lower(), f"[{kindA}] {valA[:60]!r}")

    # B — redirect -> loopback must NOT return localhost content (must raise).
    kindB, valB = await call("https://httpbin.org/redirect-to?url=http://127.0.0.1:80/")
    leaked_local = (kindB == "ok")  # any returned body for a loopback target = SSRF leak
    check("B: redirect->127.0.0.1 blocked (render raised, body NOT returned)",
          kindB == "raised", f"[{kindB}] {valB[:60]!r}")
    check("B: no localhost content leaked to caller", not leaked_local)

    # C — redirect -> cloud metadata IP must NOT return content (must raise).
    kindC, valC = await call("https://httpbin.org/redirect-to?url=http://169.254.169.254/latest/meta-data/")
    check("C: redirect->169.254.169.254 blocked (render raised)",
          kindC == "raised", f"[{kindC}] {valC[:60]!r}")

    # D — direct public fetch still works (baseline, no redirect).
    kindD, valD = await call("https://example.com/")
    check("D: direct public render works", kindD == "ok" and "example" in valD.lower(),
          f"[{kindD}] {valD[:40]!r}")

asyncio.run(asyncio.wait_for(main(), timeout=240))
print(f"\n{'=== ALL  LIVE TESTS PASS ===' if fails==0 else '=== '+str(fails)+' FAILURE(S) ==='}")
