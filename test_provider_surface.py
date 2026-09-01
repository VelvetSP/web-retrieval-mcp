""" — provider-surface drift coverage NOT already covered by
test_search_render.py: Firecrawl `highlights`, and the Exa `maxAgeHours` -1..720
range across web_fetch's two tiers. New file (this cluster owns no other web_fetch
test file — test_fetch_cascade.py etc. belong to other, unrelated work).

Run with a Python environment containing the project dependencies.

RED-first discipline (same as test_search_render.py): a call that would raise
against unmodified code goes inside try/except so it reports as a check() FAIL,
never an uncaught traceback that would silently stop the rest of this flat script.
"""
import asyncio
import os as _os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
import web_retrieval_mcp.server as m

# Keep this provider-payload unit suite independent of a future live Valkey socket.
m._completed_fetch_cache = m.CompletedFetchCache(
    f"/tmp/webret-unit-cache-disabled-provider-{_os.getpid()}.sock"
)

fails = 0
def check(label, cond, detail=""):
    global fails
    if not cond: fails += 1
    print(f"{'OK  ' if cond else 'FAIL'}  {label}  {detail}")

m._validate_public_url = lambda url: None   # no DNS needed for these tests

# ----------------------------------------------------------------- T13: Firecrawl search highlights
print("== T13 Firecrawl /v2/search highlights ==")
_fscap = {}
def _fs_post(url, payload, headers, secret, timeout=None, deadline=None):
    _fscap["payload"] = payload
    return {"success": True, "data": {"web": []}}
m._post_json = _fs_post
m._firecrawl_key = lambda: "fc-secret"
m._firecrawl_search_sync("q", 8)
check("T13 highlights sent explicitly", _fscap["payload"].get("highlights") is True)

# ----------------------------------------------------------------- T14/T15/T16/T17/T18/T19: web_fetch max_age_hours
print("==  web_fetch max_age_hours -1..720 ==")

def _make_exa_stub(cap):
    def _stub(url, max_chars, max_age_hours=None, mode="full", question=None):
        cap["max_age_hours"] = max_age_hours
        return ("a sufficiently long body of text to clear MIN_USEFUL_CHARS " * 5, None)
    return _stub

def _make_fc_stub(cap, raise_instead=False):
    def _stub(url, mode="full", question=None, max_age_hours=None):
        cap["max_age_hours"] = max_age_hours
        if raise_instead:
            raise m.RetrievalError("firecrawl unreachable in this stub")
        return "a sufficiently long body of text to clear MIN_USEFUL_CHARS " * 5
    return _stub

_saved_exa_contents = m._exa_contents_sync
_saved_fc_sync = m._firecrawl_sync

# T14 (RED): web_fetch(url, max_age_hours=-1) reaches _exa_contents_sync with -1.
_t14 = {}
m._exa_contents_sync = _make_exa_stub(_t14)
asyncio.run(m.web_fetch("https://example.com/x", max_age_hours=-1))
check("T14 web_fetch(-1) reaches _exa_contents_sync as -1", _t14.get("max_age_hours") == -1)

# T15 (RED): _exa_contents_sync itself, called directly, puts maxAgeHours: -1 in
# the real /contents payload (unreachable pre-fix — -1 was normalized to None
# before ever reaching this function).
m._exa_contents_sync = _saved_exa_contents
_t15cap = {}
def _t15_post(url, payload, headers, secret, timeout=None, deadline=None):
    _t15cap["payload"] = payload
    return {"results": [{"text": "body"}], "statuses": [{"status": "success", "source": "crawled"}]}
m._post_json = _t15_post
m._exa_key = lambda: "exa-secret"
m._exa_contents_sync("https://example.com/x", 2000, max_age_hours=-1)
# PIN, not RED (verified 2026-08-01 against pre-fix code, and consistent with the
# plan's own §2: "_exa_contents_sync needs no change — it passes the value
# straight through"): called DIRECTLY (bypassing web_fetch's normalization), -1
# already reached the payload before this cluster's changes — the whole
# defect lived in web_fetch's upstream guard (T14), not here. This assertion pins
# the pass-through so a future "fix" doesn't add a redundant/wrong guard here too.
check("T15 PIN: _exa_contents_sync payload carries maxAgeHours: -1 (pass-through)",
      _t15cap["payload"].get("maxAgeHours") == -1)

# T16 (RED): _firecrawl_sync with max_age_hours=-1 OMITS maxAge (no Firecrawl
# equivalent for "always use cache" — sending -1*3_600_000 would be nonsense).
_t16cap = {}
def _t16_post(url, payload, headers, secret, timeout=None, deadline=None):
    _t16cap["payload"] = payload
    return {"success": True, "data": {"markdown": "a body long enough to clear the two-hundred-char floor. " * 5,
                                       "metadata": {"statusCode": 200}}}
m._post_json = _t16_post
m._firecrawl_key = lambda: "fc-secret"
m._firecrawl_sync("https://example.com/x", max_age_hours=-1)
check("T16 _firecrawl_sync(-1) omits maxAge", "maxAge" not in _t16cap["payload"])

# T17 (RED): web_fetch(..., max_age_hours=1000) reaches the tiers with 720
# (Exa's documented ceiling) and the response header names the clamp.
m._exa_contents_sync = _saved_exa_contents
_t17 = {}
m._exa_contents_sync = _make_exa_stub(_t17)
_t17_out = asyncio.run(m.web_fetch("https://example.com/x", max_age_hours=1000))
_max_val = getattr(m, "EXA_MAX_AGE_HOURS_MAX", None)
check("T17 web_fetch(1000) clamps to EXA_MAX_AGE_HOURS_MAX at the tier",
      _max_val is not None and _t17.get("max_age_hours") == _max_val)
check("T17 response header names the clamp",
      _max_val is not None and str(_max_val) in _t17_out and "clamp" in _t17_out.lower())

# T18 (PIN, green before and after): max_age_hours=-5 still degrades to None
# (the surviving < -1 guard).
_t18 = {}
m._exa_contents_sync = _make_exa_stub(_t18)
asyncio.run(m.web_fetch("https://example.com/x", max_age_hours=-5))
check("T18 PIN: max_age_hours=-5 still degrades to None", _t18.get("max_age_hours") is None)

# T19 (RED): Exa fails -> Firecrawl serves -> the cache-disclosure line carries
# the NEW -1 wording, and — this must be the exact assertion, not an absence
# check — contains NEITHER "-1h" NOR "48h". Against UNMODIFIED code, -1 is
# normalized to None BEFORE the disclosure is built, so the line reads
# "up to 48h-old content" and an absence-of-"-1h" assertion would pass for the
# WRONG reason (a false RED). Asserting the new wording is present fails today
# for the right reason.
def _raising_exa(url, max_chars, max_age_hours=None, mode="full", question=None):
    raise m.RetrievalError("exa down (forced for T19)")
m._exa_contents_sync = _raising_exa
_t19cap = {}
m._firecrawl_sync = _make_fc_stub(_t19cap)
_t19_out = asyncio.run(m.web_fetch("https://example.com/x", max_age_hours=-1))
check("T19 firecrawl received -1 (not normalized away)", _t19cap.get("max_age_hours") == -1)
check("T19 cache-disclosure carries the new -1 wording, no -1h/48h substrings",
      "always use" in _t19_out.lower() and "-1h" not in _t19_out and "48h" not in _t19_out)

m._exa_contents_sync = _saved_exa_contents
m._firecrawl_sync = _saved_fc_sync

# T20 (RED): web_fetch docstring mentions -1 and 720.
_wf_doc = m.web_fetch.__doc__ or ""
check("T20 docstring mentions -1", "-1" in _wf_doc)
check("T20 docstring mentions 720", "720" in _wf_doc)

print(f"\n{'ALL OK' if fails == 0 else f'{fails} FAILURE(S)'}")
import sys
sys.exit(1 if fails else 0)
