"""web_search render + dedup + payload — unit tests (mocked, no network).
Run with a Python environment containing the project dependencies.

Event-loop hygiene: `_render_sem` is a module-level asyncio.Semaphore that binds
to the first contended loop; each asyncio.run() below is a fresh loop. These
tests never contend the render semaphore (search path), so no recreation needed;
tests that DO (fetch/render) must recreate m._render_sem in their own loop.
"""
import asyncio, sys
from pathlib import Path

import os as _os
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
import web_retrieval_mcp.server as m
_REAL_EXA_SEARCH = m._exa_search_sync   # saved before stubs overwrite it
_REAL_POST_JSON = m._post_json          # real POST (retry lives in _http_json it calls)
_REAL_FC_SEARCH = m._firecrawl_search_sync

fails = 0
def check(label, cond):
    global fails
    if not cond: fails += 1
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")

# ----------------------------------------------------------------- canonical URL (conservative — fragment+utm only)
print("== _canonical_url dedup semantics ==")
canon = m._canonical_url
check("lowercase host equivalent",  canon("https://Example.COM/Path") == canon("https://example.com/Path"))
check("drop utm_ equivalent",       canon("https://example.com/a?utm_source=x&id=5") == canon("https://example.com/a?id=5"))
check("keep non-utm query distinct",canon("https://example.com/s?q=1") != canon("https://example.com/s?q=2"))
# these are NOT provable equivalences -> kept DISTINCT (never drop a possibly-distinct resource)
check("fragment NOT collapsed (hash-routed SPAs)", canon("https://example.com/a#/products/1") != canon("https://example.com/a#/products/2"))
check("www NOT collapsed",          canon("https://www.example.com/a") != canon("https://example.com/a"))
check("trailing slash NOT collapsed", canon("https://example.com/a/") != canon("https://example.com/a"))
check("distinct port NOT equivalent", canon("https://example.com:8443/a") != canon("https://example.com/a"))
check("distinct SCHEME NOT equivalent", canon("http://example.com/a") != canon("https://example.com/a"))
check("distinct path NOT equivalent",   canon("https://example.com/a") != canon("https://example.com/b"))

# ----------------------------------------------------------------- dedup + render
print("== _render_search dedup + render ==")
# canonical-URL dedup: same URL reappearing with utm/fragment collapses to one survivor
res_urldup = [
    {"url": "https://example.com/a", "title": "First", "text": "Body one is here."},
    {"url": "https://example.com/a?utm_source=z", "title": "Dup", "text": "Body one is here."},
]
out = m._render_search("q", res_urldup)
check("url-dup: one survivor block", out.count("## 1.") == 1 and "## 2." not in out)
check("url-dup: stub rendered",      "(duplicate of [1]: https://example.com/a?utm_source=z)" in out)
check("url-dup: sources has one",    out.count("[1] ") == 1 and "[2] " not in out)

# distinct hosts with identical bodies are NOT deduped (content-fingerprint removed —
# it could only see truncated text; never drop a possibly-distinct resource)
body = "This is a sufficiently long body text used to be a mirror candidate. " * 3
res_mirror = [
    {"url": "https://origin.kernel.org/doc", "title": "Origin", "text": body},
    {"url": "https://cdn.kernel.org/doc",    "title": "Mirror", "text": body},
]
out = m._render_search("q", res_mirror)
check("distinct-host identical-body: BOTH survive (no content dedup)", "## 1." in out and "## 2." in out)

# distinct URLs are never collapsed regardless of body
res_short = [
    {"url": "https://a.com/x", "title": "A", "text": "Short body."},
    {"url": "https://b.com/y", "title": "B", "text": "Short body."},
]
out = m._render_search("q", res_short)
check("distinct urls: both survive", "## 1." in out and "## 2." in out)

# highlight/body overlap suppression
res_hl = [{
    "url": "https://h.com/p", "title": "H",
    "text": "The quick brown fox jumps over the lazy dog near the river.",
    "highlights": ["the quick brown fox", "an unrelated distinct highlight phrase"],
}]
out = m._render_search("q", res_hl, text_chars=1200)
check("overlap: substring highlight suppressed", "- the quick brown fox" not in out.lower())
check("overlap: distinct highlight kept",        "an unrelated distinct highlight phrase" in out)

# empty + Sources trailer + body truncation ellipsis
check("empty results message",       m._render_search("zzz", []) == "No results for: zzz")
res_one = [{"url": "https://one.com", "title": "One", "text": "x"*3000}]
out = m._render_search("q", res_one, text_chars=1000)
check("body truncated with ellipsis", "…" in out and "Sources:\n[1] https://one.com" in out)

# ----------------------------------------------------------------- payload capture
print("== _exa_search_sync payload ==")
captured = {}
def fake_post_json(url, payload, headers, secret, timeout=m.HTTP_TIMEOUT, deadline=None):
    captured["url"] = url; captured["payload"] = payload
    return {"results": [{"url": "https://z.com", "title": "Z", "text": "body"}]}
m._post_json = fake_post_json
m._exa_key = lambda: "exa-secret"

resp = m._exa_search_sync("mixture of experts", 5, "auto", 1500)
p = captured["payload"]
check("payload text maxChars == text_chars", p["contents"]["text"]["maxCharacters"] == 1500)
check("payload highlights budget == HIGHLIGHT_BUDGET (total, not per-hl)",
      p["contents"]["highlights"]["maxCharacters"] == m.HIGHLIGHT_BUDGET == 1000)
check("payload numResults + type", p["numResults"] == 5 and p["type"] == "auto")
check("_exa_search_sync returns FULL dict", isinstance(resp, dict) and "results" in resp)

# ----------------------------------------------------------------- web_search threading + clamps
print("== web_search threading + clamps ==")
seen = {}
def fake_exa_search_sync(query, num_results, mode, text_chars=1200, filters=None, summary=False):
    seen["num_results"] = num_results; seen["text_chars"] = text_chars; seen["mode"] = mode
    seen["filters"] = filters; seen["summary"] = summary
    return {"results": [{"url": "https://w.com", "title": "W", "text": "web body"}]}
m._exa_search_sync = fake_exa_search_sync
# fallback mocked dead from the start so  (Firecrawl search fallback) can't hit
# the network mid-session when it lands — wave-2 failure-path tests stay green.
def _dead_fallback(*a, **k):
    raise m.RetrievalError("firecrawl fallback dead (test)")
m._firecrawl_search_sync = _dead_fallback

out = asyncio.run(m.web_search("q", num_results=3, mode="auto", text_chars=1500))
check("text_chars threaded end-to-end", seen["text_chars"] == 1500)
check("num_results threaded",           seen["num_results"] == 3)
check("render output well-formed",      "# Web search: q" in out and "[1] https://w.com" in out)

asyncio.run(m.web_search("q", num_results=99, text_chars=99999))
check("num_results clamp high (->20)",  seen["num_results"] == 20)
check("text_chars clamp high (->8000)", seen["text_chars"] == 8000)
asyncio.run(m.web_search("q", num_results=0, text_chars=10))
check("num_results clamp low (->1)",    seen["num_results"] == 1)
check("text_chars clamp low (->200)",   seen["text_chars"] == 200)

# SEARCH_FAILED shape
def raising_exa(query, num_results, mode, text_chars=1200, filters=None, summary=False):
    raise m.RetrievalError("boom")
m._exa_search_sync = raising_exa
out = asyncio.run(m.web_search("failq"))
check("SEARCH_FAILED prefix + query", out.startswith("SEARCH_FAILED: failq — "))

# -----------------------------------------------------------------  freshness filters
print("== _build_search_filters ==")
f = m._build_search_filters(recency_days=7)
check("recency_days -> ISO date-time start", f["startPublishedDate"].endswith("T00:00:00.000Z") and len(f["startPublishedDate"]) == 24)
check("recency_days 0 ignored", m._build_search_filters(recency_days=0) == {})
check("recency_days negative ignored", m._build_search_filters(recency_days=-5) == {})
check("recency_days absurd ignored (no OverflowError)", m._build_search_filters(recency_days=10**10) == {})
f = m._build_search_filters(start_published_date="2026-01-15", end_published_date="2026-02-20")
check("start normalized to T00:00:00.000Z", f["startPublishedDate"] == "2026-01-15T00:00:00.000Z")
check("end normalized to T23:59:59.999Z", f["endPublishedDate"] == "2026-02-20T23:59:59.999Z")
check("datetime input -> date part parsed", m._build_search_filters(start_published_date="2026-01-15T10:30:00Z")["startPublishedDate"] == "2026-01-15T00:00:00.000Z")
check("impossible date ignored", "startPublishedDate" not in m._build_search_filters(start_published_date="2026-13-45"))
check("junk date ignored", "endPublishedDate" not in m._build_search_filters(end_published_date="not-a-date"))
check("junk SUFFIX rejected (not [:10]-sliced)", "startPublishedDate" not in m._build_search_filters(start_published_date="2026-01-15garbage"))
check("valid bare date accepted", m._build_search_filters(start_published_date="2026-01-15")["startPublishedDate"] == "2026-01-15T00:00:00.000Z")
# precedence: explicit start wins over recency
f = m._build_search_filters(recency_days=30, start_published_date="2026-01-15")
check("explicit start wins over recency", f["startPublishedDate"] == "2026-01-15T00:00:00.000Z")
# Invalid explicit start falls through to recency rather than discarding both.
f = m._build_search_filters(recency_days=30, start_published_date="2026-13-45")
check("invalid start -> recency still applies", "startPublishedDate" in f and f["startPublishedDate"].endswith("T00:00:00.000Z"))
# category free-string pass-through
check("free-string category passes through", m._build_search_filters(category="news")["category"] == "news")
check("arbitrary category hint passes through", m._build_search_filters(category="cooking blogs")["category"] == "cooking blogs")
# company/people conflict guard
f = m._build_search_filters(category="company", start_published_date="2026-01-15",
                            end_published_date="2026-02-20", exclude_domains=["x.com"])
check("company drops date filters", "startPublishedDate" not in f and "endPublishedDate" not in f)
check("company drops excludeDomains", "excludeDomains" not in f)
check("company keeps category", f["category"] == "company")
# people keeps include_domains
f = m._build_search_filters(category="people", include_domains=["linkedin.com"])
check("people KEEPS includeDomains (no silent broadening)", f["includeDomains"] == ["linkedin.com"])
# domain cap 20
f = m._build_search_filters(include_domains=[f"d{i}.com" for i in range(30)])
check("include_domains capped to 20", len(f["includeDomains"]) == 20)
# Reversed bounds (start > end) drop the end to avoid an Exa 400.
f = m._build_search_filters(start_published_date="2026-06-01", end_published_date="2026-01-01")
check("reversed bounds: start kept", f.get("startPublishedDate") == "2026-06-01T00:00:00.000Z")
check("reversed bounds: end dropped", "endPublishedDate" not in f)
# valid ordered bounds preserved
f = m._build_search_filters(start_published_date="2026-01-01", end_published_date="2026-06-01")
check("ordered bounds: both kept", "startPublishedDate" in f and "endPublishedDate" in f)

print("== web_search filter/summary threading ==")
m._exa_search_sync = fake_exa_search_sync
asyncio.run(m.web_search("q", recency_days=7))
check("web_search threads filters", seen["filters"].get("startPublishedDate", "").endswith("Z"))
asyncio.run(m.web_search("q", summary=True))
check("web_search threads summary=True", seen["summary"] is True)
# summary-mode payload: text + highlights omitted (capture via _post_json)
scap = {}
def fake_post_json2(url, payload, headers, secret, timeout=m.HTTP_TIMEOUT, deadline=None):
    scap["payload"] = payload
    return {"results": [{"url": "https://s.com", "title": "S", "summary": "A generated summary."}]}
m._post_json = fake_post_json2
m._exa_search_sync = _REAL_EXA_SEARCH   # restore real (stub was active)
m._exa_key = lambda: "exa-secret"
m._exa_search_sync("q", 5, "auto", 1500, None, True)
check("summary payload has summary contents", scap["payload"]["contents"] == {"summary": {}})
check("summary payload omits text + highlights", "text" not in scap["payload"]["contents"] and "highlights" not in scap["payload"]["contents"])
# render summary as body
out = m._render_search("q", [{"url": "https://s.com", "title": "S", "summary": "A generated summary body."}])
check("render shows summary as body", "A generated summary body." in out)

# people + include_domains -> Exa 400 surfaces as SEARCH_FAILED (fallback mocked dead)
def raising_400(query, num_results, mode, text_chars=1200, filters=None, summary=False):
    raise m.RetrievalError("HTTP 400 from https://api.exa.ai/search: unsupported profile domain")
m._exa_search_sync = raising_400
out = asyncio.run(m.web_search("someone", category="people", include_domains=["example.com"]))
check("people+include 400 surfaces (HTTP 400 in failure)", "HTTP 400" in out and out.startswith("SEARCH_FAILED:"))

# -----------------------------------------------------------------  mode enum + deep output
print("==  mode enum + deep output ==")
mseen = {}
def capturing_exa(query, num_results, mode, text_chars=1200, filters=None, summary=False):
    mseen["mode"] = mode
    return {"results": [{"url": "https://x.com", "title": "X", "text": "body"}]}
m._exa_search_sync = capturing_exa
for want in ("auto", "fast", "instant", "deep-lite", "deep", "deep-reasoning"):
    asyncio.run(m.web_search("q", mode=want))
    check(f"mode {want} passes through", mseen["mode"] == want)
asyncio.run(m.web_search("q", mode="neural"))
check("legacy neural -> auto", mseen["mode"] == "auto")
asyncio.run(m.web_search("q", mode="keyword"))
check("legacy keyword -> auto", mseen["mode"] == "auto")
asyncio.run(m.web_search("q", mode="bogus"))
check("unknown mode -> auto", mseen["mode"] == "auto")

# inner _post_json timeout selection (deep vs non-deep)
tcap = {}
def timeout_post(url, payload, headers, secret, timeout=m.HTTP_TIMEOUT, deadline=None):
    tcap["timeout"] = timeout; tcap["type"] = payload.get("type"); tcap["payload"] = payload
    return {"results": []}
m._post_json = timeout_post
m._exa_search_sync = _REAL_EXA_SEARCH
m._exa_key = lambda: "k"
m._exa_search_sync("q", 5, "deep-reasoning")
check("deep -> inner DEEP_HTTP_TIMEOUT", tcap["timeout"] == m.DEEP_HTTP_TIMEOUT)
check("payload type captured", tcap["type"] == "deep-reasoning")
m._exa_search_sync("q", 5, "auto")
check("non-deep -> inner HTTP_TIMEOUT", tcap["timeout"] == m.HTTP_TIMEOUT)

# -----------------------------------------------------------------  T1/T2: Exa outputSchema on deep modes only
print("==  outputSchema request-side ==")
m._exa_search_sync("q", 5, "deep")
_eos = getattr(m, "EXA_OUTPUT_SCHEMA", None)
check("T1 deep mode sends outputSchema", tcap["payload"].get("outputSchema") == _eos and _eos is not None)
m._exa_search_sync("q", 5, "auto")
check("T2 non-deep mode omits outputSchema (scoping pin)", "outputSchema" not in tcap["payload"])

# outer wait_for timeout selection
import asyncio as _aio
_orig_wf = _aio.wait_for
_wf_t = []
async def _rec_wf(coro, timeout=None):
    _wf_t.append(timeout)
    return await _orig_wf(coro, timeout=timeout)
_aio.wait_for = _rec_wf
m._exa_search_sync = capturing_exa
asyncio.run(m.web_search("q", mode="deep"))
check("deep -> outer DEEP_TIER_TIMEOUT", _wf_t[-1] == m.DEEP_TIER_TIMEOUT)
asyncio.run(m.web_search("q", mode="auto"))
check("non-deep -> outer TIER_TIMEOUT", _wf_t[-1] == m.TIER_TIMEOUT)
_aio.wait_for = _orig_wf

# deep-output rendering — T3/T5a: grounding in its DOCUMENTED Exa shape
# (grounding[] -> citations[] -> {url,title}; field/confidence are required
# siblings, present here so the fixture matches the live schema, not just the
# fields the renderer reads). The pre-fix renderer looked for top-level
# url/id/title, none of which exist at this nesting, so it produced no `cites`
# and never emitted a Grounding: line at all — this is a RE-POINT of what used
# to be a flat/synthetic (API-impossible) shape, not a new fixture.
out = m._render_search("q", [{"url": "u", "title": "T", "text": "body text here"}],
                       output={"content": "The synthesized deep answer.",
                               "grounding": [{"field": "content",
                                              "citations": [{"url": "https://g1.com", "title": "T1"},
                                                            {"url": "https://g2.com", "title": "T2"}],
                                              "confidence": "high"}]})
check("T3/T5a deep output: synthesized block", "## Synthesized answer" in out and "The synthesized deep answer." in out)
check("T3/T5a deep output: grounding cited (nested citations[].url)", "https://g1.com" in out and "https://g2.com" in out)
check("T3/T5a deep output: per-result blocks follow", "## 1. T" in out)
out2 = m._render_search("q", [{"url": "u", "title": "T", "text": "body"}])
check("no output -> no synthesized block (regression pin)", "## Synthesized answer" not in out2)
out3 = m._render_search("q", [], output={"content": "Answer only, no results."})
check("output only (no results) renders answer", "Answer only" in out3 and "## Synthesized answer" in out3)

# T4: duplicate citation URLs across TWO grounding entries collapse to one,
# first-seen order preserved. De-duplication across the citations[] nesting
# level does not exist pre-fix.
out4 = m._render_search("q", [{"url": "u", "title": "T", "text": "body"}],
                        output={"content": "Answer.",
                                "grounding": [
                                    {"field": "content", "citations": [{"url": "https://dup.com", "title": "A"}], "confidence": "high"},
                                    {"field": "content", "citations": [{"url": "https://dup.com", "title": "A2"},
                                                                       {"url": "https://uniq.com", "title": "B"}], "confidence": "medium"},
                                ]})
_grounding_lines = [l for l in out4.splitlines() if l.startswith("Grounding:")]
_grounding_line = _grounding_lines[0] if _grounding_lines else ""
check("T4 duplicate citation URL collapses to one", _grounding_line.count("https://dup.com") == 1)
check("T4 first-seen order preserved",
      "https://dup.com" in _grounding_line and "https://uniq.com" in _grounding_line
      and _grounding_line.index("https://dup.com") < _grounding_line.index("https://uniq.com"))

# T5b (PIN, kept SEPARATE from T3/T5a on purpose): the flat-string / flat-`url`
# tolerance branch stays green before and after — it is the only thing pinning
# a fallback the fix keeps for an unannounced shape change to degrade into
# rather than crash on.
out5b = m._render_search("q", [{"url": "u", "title": "T", "text": "body"}],
                         output={"content": "Answer.",
                                 "grounding": [{"url": "https://flat.com"}, "https://bare-string.com"]})
check("T5b PIN: flat-url and bare-string grounding forms still render",
      "https://flat.com" in out5b and "https://bare-string.com" in out5b)

# T5 (RED): the PRODUCTION PATH this behavior is actually about — outputSchema in,
# `output` populated on the response, block rendered — not the renderer tested
# in isolation. Deleting `output=resp.get("output")` from the web_search call
# site after this cluster lands would leave T3/T4/T5a/T5b green and only T5 red;
# that is the exact masking-one-level-up hole  is filed about.
_t5_saved_exa = m._exa_search_sync
def _t5_stub(query, num_results, mode, text_chars=1200, filters=None, summary=False):
    return {"results": [{"url": "https://r.com", "title": "R", "text": "body"}],
            "output": {"content": "The synthesized deep answer.",
                       "grounding": [{"field": "content",
                                      "citations": [{"url": "https://cite.example", "title": "C"}],
                                      "confidence": "high"}]}}
m._exa_search_sync = _t5_stub
_t5_out = asyncio.run(m.web_search("q", mode="deep"))
check("T5 production path: synthesized block reaches web_search output",
      "## Synthesized answer" in _t5_out)
check("T5 production path: synthesized content reaches web_search output",
      "The synthesized deep answer." in _t5_out)
check("T5 production path: grounding citation reaches web_search output",
      "https://cite.example" in _t5_out)
m._exa_search_sync = _t5_saved_exa

# -----------------------------------------------------------------  fallback tier
print("==  Firecrawl search fallback ==")
def failing_exa(query, num_results, mode, text_chars=1200, filters=None, summary=False):
    raise m.RetrievalError("exa 503")
m._exa_search_sync = failing_exa
fbcap = {}
def fake_fc_search(query, num_results, include_domains=None, exclude_domains=None, tbs=None):
    fbcap["query"] = query; fbcap["num_results"] = num_results
    fbcap["include"] = include_domains; fbcap["exclude"] = exclude_domains; fbcap["tbs"] = tbs
    return [{"title": "FB", "url": "https://fb.com", "text": "a fallback description"}]
m._firecrawl_search_sync = fake_fc_search

out = asyncio.run(m.web_search("q"))
check("fallback engaged on exa failure", "[served by: firecrawl search — exa unavailable: exa 503]" in out)
check("fallback renders results", "FB" in out and "https://fb.com" in out)
check("fallback provenance directly under header",
      out.startswith("# Web search: q\n[served by: firecrawl search — exa unavailable: exa 503]"))

asyncio.run(m.web_search("q", recency_days=5))
check("recency_days 5 -> tbs qdr:w", fbcap["tbs"] == "qdr:w")
#  precedence REVERSAL: an explicit VALID start_published_date used to
# SUPPRESS the fallback's time filter entirely (the fallback could not express an
# explicit start at all). Now that cdr:1,cd_min:… can express it, the explicit
# bound is APPLIED instead — a narrower, more correct fallback result set.
asyncio.run(m.web_search("q", recency_days=5, start_published_date="2026-01-15"))
check("valid explicit start -> fallback cdr range ( reversal)",
      fbcap["tbs"] == "cdr:1,cd_min:01/15/2026")
# invalid start falls through -> recency->tbs still applied
asyncio.run(m.web_search("q", recency_days=5, start_published_date="2026-13-45"))
check("invalid start -> fallback recency->tbs still applies", fbcap["tbs"] == "qdr:w")

# -----------------------------------------------------------------  T6-T12a: provider-surface fallback coverage
print("==  fallback tbs coverage (hours/cdr/sbd) ==")
check("T6 'personal site' documented in web_search docstring", "personal site" in (m.web_search.__doc__ or ""))

def _tolerant_run(label, kwargs, cond_fn):
    """RED-safe helper (see file header discipline): a pre-fix web_search() with an
    unknown kwarg raises TypeError immediately (before any coroutine runs), which
    must show as a check() FAIL, not an aborted script. fbcap is only trustworthy
    when the call actually ran, so cond_fn is evaluated ONLY on success."""
    try:
        asyncio.run(m.web_search("q", **kwargs))
    except TypeError:
        check(label, False)
        return
    check(label, cond_fn())

_tolerant_run("T7 sort_by_date=True -> fallback tbs contains sbd:1",
             {"sort_by_date": True}, lambda: "sbd:1" in (fbcap.get("tbs") or ""))

_tolerant_run("T8 recency_hours=1 -> fallback tbs qdr:h",
             {"recency_hours": 1}, lambda: fbcap.get("tbs") == "qdr:h")

# T8a — the EXA side of the same parameter (direct call, no stubs needed): a
# malformed timestamp here would ship silently and only surface as a live Exa 400
# well after this cluster's push, so it is pinned at the unit level too.
_bsf = getattr(m, "_build_search_filters", None)
try:
    _t8a = _bsf(recency_hours=6)["startPublishedDate"] if _bsf else None
except Exception:
    _t8a = None
check("T8a recency_hours reaches Exa as a .000Z timestamp",
      bool(_t8a) and _t8a.endswith(".000Z"))
check("T8a recency_hours is NOT midnight-anchored (hour granularity)",
      bool(_t8a) and "T00:00:00" not in _t8a)
check("T8a recency_hours timestamp round-trips through _parse_date",
      bool(_t8a) and m._parse_date(_t8a.replace("Z", "+00:00")) is not None)

_tolerant_run("T9 recency_days=400 -> fallback tbs starts cdr:1,cd_min:",
             {"recency_days": 400}, lambda: (fbcap.get("tbs") or "").startswith("cdr:1,cd_min:"))

_tolerant_run("T10 explicit start+end -> fallback cdr range with cd_max",
             {"start_published_date": "2026-01-15", "end_published_date": "2026-02-15"},
             lambda: fbcap.get("tbs") == "cdr:1,cd_min:01/15/2026,cd_max:02/15/2026")

_tolerant_run("T11 sort_by_date + recency_days=5 -> fallback tbs sbd:1,qdr:w (join order)",
             {"sort_by_date": True, "recency_days": 5},
             lambda: fbcap.get("tbs") == "sbd:1,qdr:w")

# T12: sort_by_date served by EXA (not the fallback) -> exactly-once-bracketed
# header notice, since Exa has no sort_by_date equivalent. Needs a WORKING Exa
# stub (this section's ambient stub is failing_exa) — save/restore around it.
_t12_saved_exa = m._exa_search_sync
def _t12_exa(query, num_results, mode, text_chars=1200, filters=None, summary=False):
    return {"results": [{"url": "https://x.com", "title": "X", "text": "body"}]}
m._exa_search_sync = _t12_exa
try:
    _t12_out = asyncio.run(m.web_search("q", sort_by_date=True))
    check("T12 sort_by_date-on-exa notice present, exactly-once-bracketed",
          "[sort_by_date not supported by exa — results are relevance-ranked]" in _t12_out
          and "[[" not in _t12_out)
except TypeError:
    check("T12 sort_by_date-on-exa notice present, exactly-once-bracketed", False)
m._exa_search_sync = _t12_saved_exa   # restore failing_exa for the rest of this section

# T12a: end-bound-only (no start, no recency) -> NOT invented as a cdr: form; the
# header discloses the drop rather than staying silent.
try:
    _t12a_out = asyncio.run(m.web_search("q", end_published_date="2026-02-15"))
    check("T12a end-bound-only -> fallback tbs stays None (no invented cd_max-only form)",
          fbcap.get("tbs") is None)
    check("T12a end-bound-only -> header discloses the drop", "end bound only" in _t12a_out)
except TypeError:
    check("T12a end-bound-only -> fallback tbs stays None (no invented cd_max-only form)", False)
    check("T12a end-bound-only -> header discloses the drop", False)

# T12b (RED, review MEDIUM finding 2026-08-01 — fallback-recency-precedence-divergence):
# recency_days/recency_hours must NOT be silently discarded just because an
# end_published_date is ALSO present, on either tier. Pre-fix, ANY parseable
# end date routed into the "explicit date bounds" branch even with no start,
# _dates_to_tbs(None, end_d) returned None (a cd_max-only range is not a real
# shape), and the valid recency_days was never even looked at — contradicting
# this function's own documented "SAME precedence order as
# _build_search_filters" guarantee. Reversed case (recency-derived start
# AFTER the explicit end) mirrors _build_search_filters's own
# drop-the-end-bound handling: keep the recency window, drop the end.
try:
    asyncio.run(m.web_search("q", recency_days=5, end_published_date="2026-02-15"))
    check("T12b recency_days survives a PAST end_published_date (reversed bounds -> end dropped, recency kept)",
          fbcap.get("tbs") == "qdr:w")
except TypeError:
    check("T12b recency_days survives a PAST end_published_date (reversed bounds -> end dropped, recency kept)", False)

# review MEDIUM (fixed-future-date-test-expiry, 2026-08-01): a
# LITERAL "2026-08-05" here was only future-relative to a 5-day recency window
# on the day this was written. From 2026-08-11 UTC onward, today - 5 days would
# be AFTER that literal date, tripping the REVERSED branch (T12b's first check)
# instead of the combined-range branch this second check exists to cover, and
# the assertion would fail forever, not just once. Derive the end bound from
# "now" at RUN time, comfortably (30d) past any 5-day recency window, so the
# combined-range case stays reachable no matter when the suite runs.
_t12b2_end = (m.datetime.now(m.timezone.utc) + m.timedelta(days=30)).date()
_t12b2_end_mdY = _t12b2_end.strftime("%m/%d/%Y")
try:
    asyncio.run(m.web_search("q", recency_days=5, end_published_date=_t12b2_end.isoformat()))
    _t12b2 = fbcap.get("tbs") or ""
    check("T12b recency_days + a FUTURE-relative end -> combined cdr range with cd_max",
          _t12b2.startswith("cdr:1,cd_min:") and _t12b2.endswith(f",cd_max:{_t12b2_end_mdY}"))
except TypeError:
    check("T12b recency_days + a FUTURE-relative end -> combined cdr range with cd_max", False)

# T12c (RED, same review finding, second half): an INVALID recency_hours
# (negative, or over the 876,000h absurd-value ceiling) must fall through to
# a valid recency_days, not silently consume the precedence slot and produce
# no filter at all.
try:
    asyncio.run(m.web_search("q", recency_hours=-5, recency_days=7))
    check("T12c invalid (negative) recency_hours falls through to recency_days",
          fbcap.get("tbs") == "qdr:w")
except TypeError:
    check("T12c invalid (negative) recency_hours falls through to recency_days", False)

try:
    asyncio.run(m.web_search("q", recency_hours=9_000_000, recency_days=7))
    check("T12c invalid (>876000h) recency_hours falls through to recency_days",
          fbcap.get("tbs") == "qdr:w")
except TypeError:
    check("T12c invalid (>876000h) recency_hours falls through to recency_days", False)

# T12d (RED, review MEDIUM finding, 2026-08-01 —
# fallback-inverted-explicit-date-range): an EXPLICIT start+end pair where the
# end is BEFORE the start must not produce an inverted cd_min>cd_max clause —
# mirrors _build_search_filters's existing reversed-bounds handling for the
# Exa tier (drop the end bound, keep the start).
try:
    asyncio.run(m.web_search("q", start_published_date="2026-02-15", end_published_date="2026-01-15"))
    _t12d = fbcap.get("tbs") or ""
    check("T12d inverted explicit start>end -> end dropped, start kept, NOT inverted",
          _t12d == "cdr:1,cd_min:02/15/2026")
except TypeError:
    check("T12d inverted explicit start>end -> end dropped, start kept, NOT inverted", False)

# T12e (PIN): the non-inverted explicit start+end case (T10's shape) still
# combines normally — this fix must not touch the ordinary path.
try:
    asyncio.run(m.web_search("q", start_published_date="2026-01-15", end_published_date="2026-02-15"))
    check("T12e PIN: non-inverted explicit start+end still combines normally",
          fbcap.get("tbs") == "cdr:1,cd_min:01/15/2026,cd_max:02/15/2026")
except TypeError:
    check("T12e PIN: non-inverted explicit start+end still combines normally", False)

out = asyncio.run(m.web_search("q", include_domains=["a.com"], exclude_domains=["b.com"]))
check("both domains: only includeDomains sent to fallback", fbcap["include"] == ["a.com"] and fbcap["exclude"] is None)
_lines = out.splitlines()
check("two-line placement: provenance then partial-filters", _lines[1].startswith("[served by: firecrawl search") and _lines[2].startswith("[filters partially applied on fallback: excludeDomains"))

out = asyncio.run(m.web_search("q", summary=True))
#  assert the STABLE fragment, not the full sentence — the wording has already
# churned once (2026-07-22 Firecrawl highlights flip) and a literal match re-breaks here.
check("summary fallback degradation note", "not generated summaries" in out)

# Long query -> fallback discloses the 500-character truncation.
longq = "x" * 600
out = asyncio.run(m.web_search(longq))
check("fallback discloses query truncation", "[query truncated to 500 chars on fallback (was 600)]" in out)

# Empty fallback -> explicit no-results signal, not a bare header.
def empty_fc_search(query, num_results, include_domains=None, exclude_domains=None, tbs=None):
    return []
m._firecrawl_search_sync = empty_fc_search
out = asyncio.run(m.web_search("q"))
check("empty fallback still shows provenance", "[served by: firecrawl search" in out)
check("empty fallback shows explicit no-results", "(no results for: q)" in out)
m._firecrawl_search_sync = fake_fc_search   # restore for later checks

def failing_fc(*a, **k):
    raise m.RetrievalError("fc 500")
m._firecrawl_search_sync = failing_fc
out = asyncio.run(m.web_search("q"))
check("both fail -> combined SEARCH_FAILED trail", out == "SEARCH_FAILED: q — exa: exa 503 | firecrawl: fc 500")

# _firecrawl_search_sync payload/mapping (real fn + mocked _post_json)
print("== _firecrawl_search_sync ==")
m._firecrawl_search_sync = _REAL_FC_SEARCH   # restore real (stubs were active above)
fscap = {}
def fs_post(url, payload, headers, secret, timeout=None, deadline=None):
    fscap["payload"] = payload; fscap["url"] = url
    return {"success": True, "data": {"web": [{"title": "T", "url": "https://t.com", "description": "desc"}]}}
m._post_json = fs_post
m._firecrawl_key = lambda: "fc"
res = m._firecrawl_search_sync("x"*600, 8, include_domains=["a.com"], exclude_domains=["b.com"], tbs="qdr:w")
check("query truncated to 500", len(fscap["payload"]["query"]) == 500)
check("fallback hits /v2/search", fscap["url"] == m.FIRECRAWL_SEARCH_URL)
check("both set -> only includeDomains in payload", fscap["payload"].get("includeDomains") == ["a.com"] and "excludeDomains" not in fscap["payload"])
check("tbs in payload", fscap["payload"]["tbs"] == "qdr:w")
check("maps data.web -> result shape", res == [{"title": "T", "url": "https://t.com", "text": "desc"}])
def fs_post_fail(url, payload, headers, secret, timeout=None, deadline=None):
    return {"success": False, "error": "quota"}
m._post_json = fs_post_fail
_ok = False
try:
    m._firecrawl_search_sync("q", 8)
except m.RetrievalError:
    _ok = True
check("success:false -> raises (fail-closed)", _ok)
#  RE-POINTED from the old days-based helper to _recency_hours_to_tbs(hours).
# The rename is load-bearing (the UNIT changed) — getattr-guarded so a pre-fix run
# reports these as check() FAILs, not an AttributeError that aborts the script.
_rhtt = getattr(m, "_recency_hours_to_tbs", None)
if _rhtt is not None:
    check("recency->tbs buckets (hour-based; behaviour-preserving for every old day value)",
          (_rhtt(24), _rhtt(168), _rhtt(744), _rhtt(8760), _rhtt(0)) == ("qdr:d", "qdr:w", "qdr:m", "qdr:y", None))
    check("recency->tbs new qdr:h bucket (<=1h)", _rhtt(1) == "qdr:h")
    check("recency >1y -> cdr range (not dropped)",
          (_rhtt(730 * 24) or "").startswith("cdr:1,cd_min:"))
else:
    check("recency->tbs buckets (hour-based; behaviour-preserving for every old day value)", False)
    check("recency->tbs new qdr:h bucket (<=1h)", False)
    check("recency >1y -> cdr range (not dropped)", False)

# -----------------------------------------------------------------  deadline-aware retry
print("==  deadline-aware retry ==")
import urllib.request as _ur, urllib.error as _ue
_REAL_URLOPEN = _ur.urlopen
m._post_json = _REAL_POST_JSON
m._exa_key = lambda: "k"

class _Resp:
    def __init__(self, body): self._b = body.encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False

def make_uo(seq, cap=None):
    st = {"n": 0}
    def _uo(req, timeout=None):
        i = st["n"]; st["n"] += 1
        if cap is not None: cap.append(timeout)
        item = seq[min(i, len(seq)-1)]
        if isinstance(item, BaseException): raise item
        return _Resp(item)
    return _uo, st

err503 = _ue.HTTPError("u", 503, "busy", {}, None)
err400 = _ue.HTTPError("u", 400, "bad", {}, None)

uo, st = make_uo([err503, '{"ok":1}']); _ur.urlopen = uo
m._post_json("http://x", {}, {}, "k", deadline=m.time.monotonic()+30)
check("retry fires once on 503 (2 attempts)", st["n"] == 2)

uo, st = make_uo([TimeoutError("timed out"), '{"ok":1}']); _ur.urlopen = uo
m._post_json("http://x", {}, {}, "k", deadline=m.time.monotonic()+30)
check("retry fires once on socket timeout", st["n"] == 2)

uo, st = make_uo([err400, '{"ok":1}']); _ur.urlopen = uo
try: m._post_json("http://x", {}, {}, "k", deadline=m.time.monotonic()+30)
except m.RetrievalError: pass
check("no retry on 400 (non-transient)", st["n"] == 1)

uo, st = make_uo([err503, '{"ok":1}']); _ur.urlopen = uo
try: m._post_json("http://x", {}, {}, "k", deadline=m.time.monotonic()+2)  # >1s (fires) but <RETRY_FLOOR_S (no retry)
except m.RetrievalError: pass
check("no retry when budget < RETRY_FLOOR_S", st["n"] == 1)

# Less than one second of budget aborts before issuing a request.
uo, st = make_uo(['{"ok":1}']); _ur.urlopen = uo
try: m._post_json("http://x", {}, {}, "k", deadline=m.time.monotonic()+0.3); _aborted = False
except m.RetrievalError: _aborted = True
check("<1s budget aborts before request", _aborted and st["n"] == 0)

caps = []
uo, st = make_uo([err503, '{"ok":1}'], cap=caps); _ur.urlopen = uo
m._post_json("http://x", {}, {}, "k", timeout=30, deadline=m.time.monotonic()+5)
check("retry socket timeout capped to remaining (<30, <=5)", caps[1] < 30 and caps[1] <= 5)

_ur.urlopen = _REAL_URLOPEN   # restore

# _effective_deadline reads the caller's pre-queue context value, or computes one.
print("==  deadline contextvar ==")
_pinned = m.time.monotonic() + 999
_tok = m._TIER_DEADLINE_VAR.set(_pinned)
check("effective_deadline uses the set contextvar", m._effective_deadline(m.TIER_TIMEOUT) == _pinned)
m._TIER_DEADLINE_VAR.reset(_tok)
_fb = m._effective_deadline(m.TIER_TIMEOUT)
check("effective_deadline falls back to budget when unset", _fb <= m.time.monotonic() + m.TIER_TIMEOUT + 1)

# -----------------------------------------------------------------  #2: userinfo in the dedup key
# `p.hostname` DROPS a `user[:pass]@` prefix, so two differently-credentialed URLs
# used to share one canonical key and one of them was silently dropped as a dup.
print("==  #2 userinfo in the canonical key ==")
check("different username NOT collapsed",
      canon("https://alice@example.com/r") != canon("https://bob@example.com/r"))
check("userinfo vs no-userinfo NOT collapsed",
      canon("https://alice@example.com/r") != canon("https://example.com/r"))
check("userinfo case PRESERVED (userinfo is case-sensitive, unlike the host)",
      canon("https://Alice@example.com/r") != canon("https://alice@example.com/r"))
check("host case still folded when userinfo is present",
      canon("https://alice@EXAMPLE.com/r") == canon("https://alice@example.com/r"))
check("different password NOT collapsed",
      canon("https://a:p1@example.com/r") != canon("https://a:p2@example.com/r"))
check("userinfo + explicit port still parses the port off the host",
      canon("https://a@example.com:8443/r") != canon("https://a@example.com/r"))
# the fix must be inert for every URL WITHOUT userinfo (the overwhelming majority)
check("no-userinfo key byte-identical to the pre-fix shape",
      canon("https://example.com/a?id=5") == "https://example.com/a?id=5")
check("'@' inside the PATH is not mistaken for userinfo",
      canon("https://example.com/user@handle") == "https://example.com/user@handle")
# end-to-end: two credentialed URLs must BOTH survive dedup
res_userinfo = [
    {"url": "https://alice@example.com/r", "title": "Alice", "text": "Alice's authenticated page."},
    {"url": "https://bob@example.com/r",   "title": "Bob",   "text": "Bob's authenticated page."},
]
out = m._render_search("q", res_userinfo)
check("userinfo: both credentialed results survive dedup",
      out.count("## 1.") == 1 and out.count("## 2.") == 1)
check("userinfo: no duplicate stub emitted", "(duplicate of" not in out)
# the canonical key is a dict key only — no credential may reach the rendered output
# beyond the ORIGINAL url the caller already passed in.
check("userinfo: password never rendered",
      "p1" not in m._render_search("q", [{"url": "https://a:p1@example.com/r",
                                          "title": "T", "text": "body"}]).replace(
          "https://a:p1@example.com/r", ""))

# -----------------------------------------------------------------  #3: question-floor doc/impl drift
# Round-4 of  lowered the question floor to 1 but left both docstrings
# claiming the 60-char extract floor. Pin doc AND impl so they cannot re-diverge.
print("==  #3 question-floor doc/impl agreement ==")
_wf_doc = m.web_fetch.__doc__ or ""
_fc_doc = m._firecrawl_sync.__doc__ or ""
check("web_fetch doc no longer claims a 60-char question floor",
      "60-char floor" not in _wf_doc)
check("web_fetch doc states the real 1-char question floor", "floor is 1 char" in _wf_doc)
check("_firecrawl_sync doc no longer lumps question in with the extract floor",
      "Concise/question outputs use the MIN_EXTRACT_CHARS floor" not in _fc_doc)
check("_firecrawl_sync doc names all three floors",
      "MIN_EXTRACT_CHARS" in _fc_doc and "MIN_USEFUL_CHARS" in _fc_doc and "ANSWER uses 1" in _fc_doc)
check("MIN_EXTRACT_CHARS is still the 60 the docs name", m.MIN_EXTRACT_CHARS == 60)
check("MIN_USEFUL_CHARS is still the 200 the docs name", m.MIN_USEFUL_CHARS == 200)
# behavioral pin: a terse ANSWER is accepted, an EMPTY one still cascades
_fc_payload = {"ok": {"success": True, "data": {"answer": "Paris", "metadata": {"statusCode": 200}}},
               "empty": {"success": True, "data": {"answer": "   ", "metadata": {"statusCode": 200}}}}
m._firecrawl_key = lambda: "fc-secret"
m._post_json = lambda url, payload, headers, secret, timeout=m.HTTP_TIMEOUT, deadline=None: _fc_payload["ok"]
check("terse 5-char answer accepted (floor 1, not 60)",
      m._firecrawl_sync("https://x.com", question="Capital of France?") == "Paris")
m._post_json = lambda url, payload, headers, secret, timeout=m.HTTP_TIMEOUT, deadline=None: _fc_payload["empty"]
try:
    m._firecrawl_sync("https://x.com", question="Capital of France?")
    _empty_cascaded = False
except m.RetrievalError:
    _empty_cascaded = True
check("whitespace-only answer still cascades", _empty_cascaded)
m._post_json = _REAL_POST_JSON

# ----------------------------------------------------------------- : Exa category enum migration
print("==  Exa category enum migration ==")
check("'research paper' aliased forward to 'publication'",
      m._build_search_filters(category="research paper")["category"] == "publication")
check("rename lookup is case/whitespace-insensitive",
      m._build_search_filters(category="  Research Paper ")["category"] == "publication")
check("REMOVED 'tweet' is dropped, not sent (Exa 400s on it)",
      "category" not in m._build_search_filters(category="tweet"))
check("deprecated-but-live 'pdf' still passes through",
      m._build_search_filters(category="pdf")["category"] == "pdf")
check("deprecated-but-live 'github' still passes through",
      m._build_search_filters(category="github")["category"] == "github")
check("current category untouched by the migration",
      m._build_search_filters(category="news")["category"] == "news")
check("free-string hint untouched by the migration",
      m._build_search_filters(category="cooking blogs")["category"] == "cooking blogs")
# 'publication' is not company/people, so the conflict guard must NOT eat date filters
f = m._build_search_filters(category="research paper", start_published_date="2026-01-15")
check("renamed category keeps date filters", f["startPublishedDate"] == "2026-01-15T00:00:00.000Z")
# `notices` is an ADDITIVE opt-in: absent -> unchanged behavior, no crash
check("notices default None -> removed category still just dropped",
      m._build_search_filters(category="tweet") == {})
_nz = []
m._build_search_filters(category="news", notices=_nz)
check("no notice for a current category", _nz == [])
_nz = []
m._build_search_filters(category="research paper", notices=_nz)
check("rename raises exactly one notice naming the new value",
      len(_nz) == 1 and "publication" in _nz[0])
_nz = []
m._build_search_filters(category="tweet", notices=_nz)
check("removal raises a notice saying it was dropped",
      len(_nz) == 1 and "dropped" in _nz[0])
_nz = []
m._build_search_filters(category="pdf", notices=_nz)
check("deprecation raises a notice", len(_nz) == 1 and "deprecated" in _nz[0])
# only the CATEGORY migration is caller-visible; other filter drops stay stderr-only
_nz = []
m._build_search_filters(category="company", start_published_date="2026-01-15", notices=_nz)
check("non-category filter drops raise no caller-visible notice", _nz == [])

print("==  web_search threading + header disclosure ==")
cseen = {}
def cat_capturing_exa(query, num_results, mode, text_chars=1200, filters=None, summary=False):
    cseen["filters"] = filters
    return {"results": [{"url": "https://p.com/1", "title": "P", "text": "body"}]}
m._exa_search_sync = cat_capturing_exa
out = asyncio.run(m.web_search("q", category="research paper"))
check("web_search sends the RENAMED category to Exa", cseen["filters"]["category"] == "publication")
check("web_search discloses the rename in the header", "renamed by Exa" in out)
out = asyncio.run(m.web_search("q", category="tweet"))
check("web_search omits the removed category from the Exa payload",
      "category" not in cseen["filters"])
check("web_search discloses the drop in the header", "REMOVED by Exa" in out)
out = asyncio.run(m.web_search("q", category="news"))
check("no header noise for a current category",
      "renamed by Exa" not in out and "REMOVED by Exa" not in out)
check("no-migration render still starts with the bare title line",
      out.startswith("# Web search: q\n"))
# the notice must survive degradation to the Firecrawl fallback tier
def dead_exa(query, num_results, mode, text_chars=1200, filters=None, summary=False):
    raise m.RetrievalError("exa down")
m._exa_search_sync = dead_exa
m._firecrawl_search_sync = lambda q, n, inc=None, exc=None, tbs=None: [
    {"url": "https://f.com/1", "title": "F", "text": "fallback body"}]
out = asyncio.run(m.web_search("q", category="research paper"))
check("fallback header still carries the migration notice", "renamed by Exa" in out)
check("fallback header still carries its own provenance line",
      "[served by: firecrawl search" in out)
m._firecrawl_search_sync = _REAL_FC_SEARCH
m._exa_search_sync = _REAL_EXA_SEARCH

print(f"\n{'ALL SEARCH-RENDER TESTS PASS' if fails == 0 else str(fails) + ' FAILURE(S)'}")
sys.exit(1 if fails else 0)
