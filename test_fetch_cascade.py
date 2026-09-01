"""web_fetch cascade + truncation markers + routing — unit tests (mocked, no network).
Run with a Python environment containing the project dependencies.

Mocking strategy (MCPServer's tool() returns the undecorated function and module
globals are late-bound): monkeypatch _exa_contents_sync, _firecrawl_sync, and _camoufox_render
(async stub) and m._validate_public_url (no-op, offline determinism — real DNS is
covered by the SSRF suites), then drive asyncio.run(m.web_fetch(...)).

Event-loop hygiene: _render_sem is a module-level asyncio.Semaphore bound to the first
contended loop; each asyncio.run() is a fresh loop. These tests stub _camoufox_render so
the real render (and its semaphore) never runs — no recreation needed here.
"""
import asyncio, sys, tempfile
from pathlib import Path

import os as _os
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
import web_retrieval_mcp.server as m

# This is a tier-routing suite, not a completed-cache integration test. Keep it
# isolated from any configured cache when run directly.
m._completed_fetch_cache = m.CompletedFetchCache(
    f"/tmp/webret-unit-cache-disabled-cascade-{_os.getpid()}.sock"
)

fails = 0
def check(label, cond):
    global fails
    if not cond: fails += 1
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")

m._validate_public_url = lambda url: None   # offline determinism
_REAL_FC = m._firecrawl_sync                 # saved before set_tiers() overwrites them
_REAL_EXA = m._exa_contents_sync
_REAL_CAM = m._camoufox_render

URL = "https://example.com/page"
calls = []   # ordered record of which tier ran

def set_tiers(exa=None, firecrawl=None, camoufox=None):
    """Install per-test tier stubs. Each arg is (behavior). None = raise (tier down).
    Stubs accept the  mode/question args and record them in `calls`."""
    calls.clear()
    def _exa(url, max_chars, max_age_hours=None, mode="full", question=None):
        calls.append(("exa", max_chars, max_age_hours, mode, question))
        if exa is None:
            raise m.RetrievalError("exa down")
        return exa(url, max_chars, max_age_hours)
    def _fc(url, mode="full", question=None, max_age_hours=None):
        calls.append(("firecrawl", url, mode, question, max_age_hours))
        if firecrawl is None:
            raise m.RetrievalError("firecrawl down")
        return firecrawl(url)
    async def _cam(url, max_chars=None):
        calls.append(("camoufox", url, max_chars))
        if camoufox is None:
            raise m.RetrievalError("camoufox down")
        return camoufox(url)
    m._exa_contents_sync = _exa
    m._firecrawl_sync = _fc
    m._camoufox_render = _cam

def run(**kw):
    return asyncio.run(m.web_fetch(URL, **kw))

# ------------------------------------------------------------------ finalize helper unit
print("== _finalize marker helper ==")
check("no clip -> no marker", "[TRUNCATED" not in m._finalize("short body text", 20000, 10000))
long = "x" * 6000
out = m._finalize(long, 5000)          # local clip
check("local clip -> marker at max_chars", "[TRUNCATED at 5000 chars" in out)
check("marker after a blank line", out.endswith("more]") and "\n\n[TRUNCATED" in out)
check("body sliced to max_chars", out.split("\n\n[TRUNCATED")[0] == "x"*5000)
atcap = "y" * 5000
check("upstream at-cap -> marker (len==cap)", "[TRUNCATED at 5000 chars" in m._finalize(atcap, 5000, 5000))
check("upstream 0.98 threshold -> marker", "[TRUNCATED" in m._finalize("z"*9800, 20000, 10000))
check("below 0.98 -> no marker", "[TRUNCATED" not in m._finalize("z"*9000, 20000, 10000))
check("requested_cap None -> no upstream marker", "[TRUNCATED" not in m._finalize("z"*9800, 20000, None))

# ------------------------------------------------------------------ routing + cascade
print("== routing (default / <=10k / >10k / never / always) ==")
body = lambda n: (lambda u, mc, ma: (("A"*n), "crawled"))
set_tiers(exa=body(3000), firecrawl=lambda u: "FC"*2000)
out = run()                            # default None
check("default None -> Exa-first", calls[0][0] == "exa")
check("default None -> effective 20000", calls[0][1] == 20000)
check("provenance header byte-identical", out.startswith("[served by: exa]  " + URL + "\n\n"))
check("tuple return unpacked (text used)", "AAA" in out)

set_tiers(exa=body(3000), firecrawl=lambda u: "FC"*2000)
run(max_chars=5000)                    # explicit <=10k
check("<=10k -> Exa-first", calls[0][0] == "exa" and calls[0][1] == 5000)

set_tiers(exa=body(3000), firecrawl=lambda u: "F"*3000, camoufox=lambda u: "C"*3000)
out = run(max_chars=20000)
check(">10k: Camoufox first because Exa cannot satisfy requested size",
      [c[0] for c in calls] == ["camoufox"])
check(">10k: usable Camoufox preserves Firecrawl budget and skips Exa",
      out.startswith("[served by: camoufox]  ")
      and not any(c[0] in ("firecrawl", "exa") for c in calls))

set_tiers(exa=body(3000), firecrawl=lambda u: "F"*3000, camoufox=None)
out = run(max_chars=20000)
check(">10k: Firecrawl only after Camoufox fails",
      [c[0] for c in calls] == ["camoufox", "firecrawl"]
      and out.startswith("[served by: firecrawl]  "))

set_tiers(exa=body(3000), firecrawl=None, camoufox=None)
out = run(max_chars=20000)
check(">10k: Exa is final truncated salvage after local + paid tiers fail",
      [c[0] for c in calls] == ["camoufox", "firecrawl", "exa"]
      and out.startswith("[served by: exa]  "))

set_tiers(exa=None, firecrawl=lambda u: "F"*3000, camoufox=lambda u: "C"*3000)
out = run(render="never")
check("never -> Exa then Firecrawl, Camoufox skipped",
      [c[0] for c in calls] == ["exa", "firecrawl"]
      and out.startswith("[served by: firecrawl]  "))

set_tiers(exa=body(3000), firecrawl=lambda u: "F"*3000, camoufox=lambda u: "C"*3000)
out = run(render="always")
check("always -> camoufox first", calls[0][0] == "camoufox")
check("always -> exa never called", not any(c[0] == "exa" for c in calls))
check("always -> served by camoufox", out.startswith("[served by: camoufox]  "))

# ------------------------------------------------------------------ thin + all-fail
print("== thin cascade + all-tiers-fail ==")
set_tiers(exa=lambda u, mc, ma: ("tiny", "crawled"), firecrawl=lambda u: "F"*3000)
out = run()
check("thin exa -> cascade to firecrawl", out.startswith("[served by: firecrawl]  "))
check("thin exa recorded in cascade", any(c[0] == "exa" for c in calls) and any(c[0] == "firecrawl" for c in calls))

set_tiers(exa=None, firecrawl=None)
out = run()
check("all-fail -> RETRIEVAL_FAILED prefix", out.startswith(f"RETRIEVAL_FAILED: {URL} — "))
check("all-fail -> exa | camoufox | firecrawl trail",
      "exa: exa down" in out and "camoufox:" in out and "firecrawl: firecrawl down" in out
      and out.index("exa:") < out.index("camoufox:") < out.index("firecrawl:"))

# ------------------------------------------------------------------ markers per tier
print("== truncation markers per tier + at-cap ==")
set_tiers(exa=lambda u, mc, ma: ("E"*5000, "crawled"))   # exa returns exactly cap
out = run(max_chars=5000)
check("exa at-cap 5000 -> marker", "[TRUNCATED at 5000 chars" in out)
set_tiers(exa=lambda u, mc, ma: ("E"*10000, "crawled"))
out = run(max_chars=10000)
check("exa at-cap 10000 -> marker", "[TRUNCATED at 10000 chars" in out)
set_tiers(exa=lambda u, mc, ma: ("E"*3000, "crawled"))
out = run()                                              # default, complete content
check("exa complete (3000<9800) -> NO marker", "[TRUNCATED" not in out)

set_tiers(exa=None, firecrawl=lambda u: "F"*25000)       # firecrawl local clip
out = run(max_chars=20000)
check("firecrawl local clip -> marker at 20000", "[TRUNCATED at 20000 chars" in out)
check("marker AFTER body (firecrawl)", out.index("[TRUNCATED") > out.index("FFFF"))

set_tiers(camoufox=lambda u: "C"*30000)                  # camoufox local clip (default effective 20000)
out = run(render="always")
check("camoufox local clip -> marker at 20000", "[TRUNCATED at 20000 chars" in out)

# ------------------------------------------------------------------ clamp
print("== max_chars clamp ==")
set_tiers(exa=lambda u, mc, ma: ("E"*500, "crawled"), firecrawl=lambda u: "F"*3000)
run(max_chars=10)
check("clamp low: effective->1000 (still Exa-first, 10<=10000)", calls[0] == ("exa", 1000, None, "full", None))
set_tiers(exa=None, firecrawl=lambda u: "F"*3000)
run(max_chars=999999)
check("clamp high: Camoufox -> Firecrawl, Camoufox cap->100000",
      [c[0] for c in calls] == ["camoufox", "firecrawl"]
      and calls[0][2] == 100000)

# ------------------------------------------------------------------  concise/question
print("== : token-efficiency formats + [mode:] line ==")
# payload capture via _post_json (call the real sync funcs)
pcap = {}
def fake_post(url, payload, headers, secret, timeout=None, deadline=None):
    pcap["payload"] = payload; pcap["url"] = url
    # return shapes for whichever field was requested
    return {"success": True,
            "data": {"markdown": "MD"*200, "summary": "SUM"*40, "answer": "ANS"*40,
                     "metadata": {"statusCode": 200}},
            "results": [{"text": "TX"*200, "summary": "SM"*40}], "statuses": [{"source": "crawled"}]}
m._post_json = fake_post
m._firecrawl_key = lambda: "fc"
m._exa_key = lambda: "exa"
m._firecrawl_sync = _REAL_FC                  # restore real funcs (set_tiers had stubbed them)
m._exa_contents_sync = _REAL_EXA

m._firecrawl_sync(URL)                       # full
check("firecrawl full: onlyMainContent True", pcap["payload"].get("onlyMainContent") is True)
check("firecrawl full: formats markdown", pcap["payload"]["formats"] == ["markdown"])
m._firecrawl_sync(URL, "concise")
check("firecrawl concise: formats summary (bare string)", pcap["payload"]["formats"] == ["summary"])
m._firecrawl_sync(URL, "full", "what is X?")
check("firecrawl question: formats question object (both keys)",
      pcap["payload"]["formats"] == [{"type": "question", "question": "what is X?"}])
check("firecrawl question read path -> data.answer", m._firecrawl_sync(URL, "full", "q?").startswith("ANS"))
check("firecrawl concise read path -> data.summary", m._firecrawl_sync(URL, "concise").startswith("SUM"))

t, _ = m._exa_contents_sync(URL, 5000)       # full
check("exa full: requests text, no summary", "text" in pcap["payload"] and "summary" not in pcap["payload"])
m._exa_contents_sync(URL, 5000, None, "concise")
check("exa concise: requests summary {}, no text", pcap["payload"].get("summary") == {} and "text" not in pcap["payload"])
m._exa_contents_sync(URL, 5000, None, "full", "how?")
check("exa question: summary {query}", pcap["payload"].get("summary") == {"query": "how?"})
sm, _ = m._exa_contents_sync(URL, 5000, None, "concise")
check("exa concise read path -> results[0].summary", sm.startswith("SM"))

# [mode:] line placement via web_fetch with mocked tiers
set_tiers(exa=lambda u, mc, ma: ("A concise summary of the page, comfortably longer than the sixty-character extract floor here.", "crawled"))
out = run(mode="concise")
check("concise: [mode: concise] after served-by, before blank line",
      out.startswith("[served by: exa]  " + URL + "\n[mode: concise]\n\n"))
check("concise: exa got mode=concise", calls[0][3] == "concise")

set_tiers(exa=lambda u, mc, ma: ("A grounded answer to the question, comfortably past the sixty-character extract floor here.", "crawled"))
out = run(question="what?")
check("question: [mode: question] line", "\n[mode: question]\n\n" in out and out.startswith("[served by: exa]"))
check("question: exa got question arg", calls[0][4] == "what?")

# full mode -> NO [mode:] line
set_tiers(exa=lambda u, mc, ma: ("Full body text content that is long enough to pass the floor easily.", "crawled"))
out = run()
check("full: no [mode:] line", "[mode:" not in out)

# A terse answer is valid; only an empty answer cascades.
set_tiers(exa=lambda u, mc, ma: ("x"*120, "crawled"))   # 120-char answer
out = run(question="q?")
check("question floor: 120-char answer accepted", out.startswith("[served by: exa]"))
set_tiers(exa=lambda u, mc, ma: ("Paris", "crawled"))   # 5-char correct answer
out = run(question="q?")
check("question floor: terse 5-char answer accepted (no cascade)", out.startswith("[served by: exa]") and "Paris" in out)
# real _exa_contents_sync strips -> a whitespace answer arrives as "" (len 0) and cascades
set_tiers(exa=lambda u, mc, ma: ("", "crawled"), firecrawl=lambda u: "FCFALLBACK"*30)
out = run(question="q?")
check("question floor: empty answer cascades without full browser",
      out.startswith("[served by: firecrawl]")
      and [c[0] for c in calls] == ["exa", "firecrawl"])
set_tiers(exa=lambda u, mc, ma: ("Paris", "crawled"), firecrawl=lambda u: "FCFALLBACK"*30)
out = run(question="q?", max_chars=20000)
check("question explicit >10k: Exa remains first and preserves Firecrawl budget",
      out.startswith("[served by: exa]")
      and [c[0] for c in calls] == ["exa"])
# concise mode keeps the 60-char thin-summary guard
set_tiers(exa=lambda u, mc, ma: ("tiny", "crawled"), firecrawl=lambda u: "A sufficiently long concise summary body that clears the sixty-character floor easily here.")
out = run(mode="concise")
check("concise floor: <60 summary cascades without full browser",
      out.startswith("[served by: firecrawl]")
      and [c[0] for c in calls] == ["exa", "firecrawl"])
set_tiers(exa=lambda u, mc, ma: ("A sufficiently long Exa summary that clears the sixty-character extraction floor without paid fallback.", "crawled"),
          firecrawl=lambda u: "FCFALLBACK"*30)
out = run(mode="concise", max_chars=20000)
check("concise explicit >10k: Exa remains first and preserves Firecrawl budget",
      out.startswith("[served by: exa]")
      and [c[0] for c in calls] == ["exa"])

# camoufox ignores mode -> no [mode:] line even under concise
set_tiers(camoufox=lambda u: "Full rendered body content, long enough to clear the 200-char floor easily. "*5)
out = run(render="always", mode="concise")
check("camoufox ignores mode: no [mode:] line", out.startswith("[served by: camoufox]") and "[mode:" not in out)

# Empty/whitespace question is full-body mode, with no mode line or question argument.
q_seen = {}
set_tiers(exa=lambda u, mc, ma: (q_seen.__setitem__("called", True) or ("A full body of sufficient length to clear the two-hundred char full-mode floor here. "*3), "crawled"))
out = run(question="   ")
check("empty question -> not question mode (no [mode:] line)", "[mode:" not in out)
check("empty question -> exa got question=None", calls[0][4] is None)

# ------------------------------------------------------------------  max_age_hours + cache line
print("== Firecrawl max_age_hours + cache disclosure ==")
# maxAge payload conversion via real _firecrawl_sync
acap = {}
def fake_post_age(url, payload, headers, secret, timeout=None, deadline=None):
    acap["payload"] = payload
    return {"success": True, "data": {"markdown": "MD"*200, "metadata": {"statusCode": 200}}}
m._post_json = fake_post_age
m._firecrawl_key = lambda: "fc"
m._firecrawl_sync = _REAL_FC
m._firecrawl_sync(URL, max_age_hours=6)
check("maxAge = hours*3_600_000 ms", acap["payload"]["maxAge"] == 6 * 3_600_000)
m._firecrawl_sync(URL, max_age_hours=0)
check("maxAge 0 -> force fresh (present, 0)", acap["payload"]["maxAge"] == 0)
m._firecrawl_sync(URL)
check("maxAge omitted when None", "maxAge" not in acap["payload"])

# cache disclosure line present/absent after Camoufox fails on the large-body route
set_tiers(exa=None, firecrawl=lambda u: "F"*3000)
out = run(max_chars=20000)                       # default max_age_hours None -> 48h cache permitted
check("cache line present (default 48h)", "[cache: firecrawl may serve up to 48h-old content" in out)
check("cache line after served-by, before body", out.index("[cache:") < out.index("FFFF"))
set_tiers(exa=None, firecrawl=lambda u: "F"*3000)
out = run(max_chars=20000, max_age_hours=6)
check("cache line reflects custom window", "up to 6h-old content" in out)
check("firecrawl got max_age_hours=6", calls[-1][4] == 6)
set_tiers(exa=None, firecrawl=lambda u: "F"*3000)
out = run(max_chars=20000, max_age_hours=0)
check("max_age_hours=0 -> NO cache line (fresh forced)", "[cache:" not in out)
check("provenance header still byte-identical", out.startswith("[served by: firecrawl]  " + URL))

# ------------------------------------------------------------------  statuses[] + cache
print("== : Exa statuses[] error raise + cache provenance ==")
# error status -> raise the tag ONLY (no exa: prefix) via real _exa_contents_sync
def err_post(url, payload, headers, secret, timeout=None, deadline=None):
    return {"results": [], "statuses": [{"id": url, "status": "error",
                                         "error": {"tag": "CRAWL_TIMEOUT"}}]}
m._post_json = err_post
m._exa_key = lambda: "k"
m._exa_contents_sync = _REAL_EXA
_raised = ""
try:
    m._exa_contents_sync(URL, 5000)
except m.RetrievalError as e:
    _raised = str(e)
check("error status raises the tag only (no exa: prefix)", _raised == "CRAWL_TIMEOUT")
# and the web_fetch trail carries a SINGLE exa: prefix
def err_post2(url, payload, headers, secret, timeout=None, deadline=None):
    return {"results": [], "statuses": [{"status": "error", "error": {"tag": "NOT_FOUND"}}]}
m._post_json = err_post2
m._firecrawl_sync = _REAL_FC   # real firecrawl too -> also fails on err_post2 (both tiers down)
out = asyncio.run(m.web_fetch(URL, max_chars=5000, render="never"))
check("trail shows single 'exa: NOT_FOUND' (no double prefix)", "exa: NOT_FOUND" in out and "exa: exa:" not in out)

# cache provenance line present/absent (bodies must clear the 200-char full-mode floor)
_cbody = "A cached body of sufficient length to clear the two-hundred character full-mode floor. " * 4
set_tiers(exa=lambda u, mc, ma: (_cbody, "cached"))
out = run()
check("cached source -> cache line", "[cache: exa served a cached copy" in out)
set_tiers(exa=lambda u, mc, ma: (_cbody, "crawled"))
out = run()
check("crawled source -> no cache line", "[cache:" not in out)
set_tiers(exa=lambda u, mc, ma: (_cbody, "cached"))
out = run(max_age_hours=0)
check("cached but max_age_hours=0 -> no cache line", "[cache:" not in out)
# Negative max_age_hours is ignored, so no invalid maxAge is sent.
seen_ma = {}
set_tiers(exa=lambda u, mc, ma: (seen_ma.__setitem__("ma", ma) or _cbody, "cached"))
out = run(max_age_hours=-5)
check("negative max_age_hours ignored (None passed to tier)", seen_ma.get("ma") is None)

# ------------------------------------------------------------------ render-queue phase
print("== render-queue phase naming ==")
m._camoufox_render = _REAL_CAM   # restore real (set_tiers had stubbed it)
async def f11():
    # recreate the module semaphore in THIS loop (loop-binding pitfall) and exhaust it
    m._render_sem = asyncio.Semaphore(1)
    await m._render_sem.acquire()          # 0 permits left -> next acquire blocks
    m.SEM_ACQUIRE_TIMEOUT = 0.2            # short so the queue wait times out fast
    try:
        await m._camoufox_render("https://example.com/")
        return "no-raise"
    except m.RetrievalError as e:
        return str(e)
_r = asyncio.run(f11())
check("sem-acquire timeout -> phase-named 'render queue busy'", "render queue busy" in _r)
m.SEM_ACQUIRE_TIMEOUT = 60                  # restore
m._render_sem = asyncio.Semaphore(m.RENDER_CONCURRENCY)

print("== configurable credential sources ==")
_orig_keyring = m._key_from_keyring
_orig_os_cli = m._key_from_os_cli
m._key_from_keyring = lambda service: None
m._key_from_os_cli = lambda service: None
with tempfile.TemporaryDirectory() as _key_dir:
    _key_file = Path(_key_dir) / "keys.env"
    _key_file.write_text("TEST_API_KEY=file-value\n", encoding="utf-8")
    _key_file.chmod(0o600)
    _os.environ["WEB_RETRIEVAL_MCP_ENV_FILE"] = str(_key_file)
    _os.environ.pop("TEST_API_KEY", None)
    check("key file resolves provider credential",
          m._get_key(env_names=("TEST_API_KEY",), service="TEST_API_KEY") == "file-value")
    _os.environ["TEST_API_KEY"] = "env-value"
    check("environment overrides key file",
          m._get_key(env_names=("TEST_API_KEY",), service="TEST_API_KEY") == "env-value")
    _os.environ["TEST_API_KEY"] = "${TEST_API_KEY}"
    check("unexpanded environment literal is ignored",
          m._get_key(env_names=("TEST_API_KEY",), service="TEST_API_KEY") == "file-value")
    _key_file.chmod(0o644)
    try:
        m._get_key(env_names=("TEST_API_KEY",), service="TEST_API_KEY")
        _insecure_refused = False
    except m.RetrievalError:
        _insecure_refused = True
    check("insecure key-file permissions are refused", _insecure_refused)
    _os.environ.pop("TEST_API_KEY", None)
    _os.environ.pop("WEB_RETRIEVAL_MCP_ENV_FILE", None)
m._key_from_keyring = _orig_keyring
m._key_from_os_cli = _orig_os_cli

print(f"\n{'ALL FETCH-CASCADE TESTS PASS' if fails == 0 else str(fails) + ' FAILURE(S)'}")
sys.exit(1 if fails else 0)
