"""Firecrawl Research Index — unit tests (mocked, no network) + opt-in live smoke.
Run with the camoufox venv python so server.py deps import:
    python3.12 test_research_index.py          # unit only (must be green)
    python3.12 test_research_index.py --live    # also hit the real keyless API
"""
import asyncio, sys
from pathlib import Path

import os as _os
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
import web_retrieval_mcp.server as m

fails = 0
def check(label, cond):
    global fails
    if not cond: fails += 1
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")

# --- URL / param construction (capture via monkeypatched _get_json) ---
print("== URL construction ==")
captured = {}
def fake_get_json(url, headers, secret, timeout=m.HTTP_TIMEOUT, deadline=None):
    captured["url"] = url; captured["headers"] = headers; captured["timeout"] = timeout
    #  the helpers now fail closed on the envelope, and every live Research Index
    # endpoint really does return success:true (probed 2026-08-08) — so the fake must too,
    # or these URL-construction calls raise before the assertion runs.
    return {"success": True, "results": [], "paper": {}}
m._get_json = fake_get_json
# force keyless so header assertions are deterministic
m._firecrawl_key_optional = lambda: ""

m._research_papers_sync("mixture of experts", 5)
check("papers path+query+k", captured["url"] ==
      "https://api.firecrawl.dev/v2/search/research/papers?query=mixture+of+experts&k=5")
check("papers uses research timeout", captured["timeout"] == m.RESEARCH_HTTP_TIMEOUT)
check("keyless -> no auth header", captured["headers"] == {})

m._research_paper_sync("arxiv:2606.01509", None)
check("paper metadata encodes ':' in id, no query",
      captured["url"] == "https://api.firecrawl.dev/v2/search/research/papers/arxiv%3A2606.01509")

m._research_paper_sync("arxiv:2606.01509", "how does routing work")
check("paper passages appends ?query",
      captured["url"] ==
      "https://api.firecrawl.dev/v2/search/research/papers/arxiv%3A2606.01509?query=how+does+routing+work")

m._research_similar_sync("pid123", "newer routing methods")
check("similar path + intent + default k/mode",
      captured["url"] ==
      "https://api.firecrawl.dev/v2/search/research/papers/pid123/similar?intent=newer+routing+methods&k=8&mode=similar")

#  k/mode/rerank threading
m._research_similar_sync("pid123", "cite", k=15, mode="citers")
check("similar honors k + mode",
      captured["url"] ==
      "https://api.firecrawl.dev/v2/search/research/papers/pid123/similar?intent=cite&k=15&mode=citers")
m._research_similar_sync("pid123", "cite", k=5, mode="references", rerank=True)
check("similar appends rerank=true when set",
      captured["url"].endswith("&mode=references&rerank=true"))
m._research_similar_sync("pid123", "cite", rerank=False)
check("similar appends rerank=false when set False", captured["url"].endswith("&rerank=false"))
m._research_similar_sync("pid123", "cite")
check("similar omits rerank when None", "rerank" not in captured["url"])

m._research_github_sync("flash attention", 3)
check("github path+query+k",
      captured["url"] == "https://api.firecrawl.dev/v2/search/research/github?query=flash+attention&k=3")

# --- auth header when a key IS present ---
print("== auth header ==")
m._firecrawl_key_optional = lambda: "fc-secret"
hdrs, secret = m._fc_research_headers()
check("key present -> Bearer header", hdrs == {"Authorization": "Bearer fc-secret"} and secret == "fc-secret")

# --- renderers against REAL probed shapes ---
print("== renderers ==")
papers_resp = [
    {"paperId": "p1", "primaryId": "arxiv:2507.11181", "ids": {"arxiv": ["2507.11181"]},
     "title": "Mixture of Experts in LLMs", "abstract": "A review of MoE.", "score": 0.9876},
]
out = m._render_papers("moe", papers_resp)
check("papers render: title", "Mixture of Experts in LLMs" in out)
check("papers render: arxiv id", "arXiv:2507.11181" in out)
check("papers render: score fmt", "score 0.988" in out)
check("papers render: next-step hint", "research_paper(" in out)
check("papers render: empty -> message", m._render_papers("x", []) == "No research papers for: x")

paper_resp = {"paper": {"title": "ProbMoE", "paperId": "p1", "ids": {"arxiv": ["2606.01509"]},
                        "categories": ["cs.LG", "cs.AI"], "createdDate": "2026-06-01",
                        "authors": ["A. One", {"name": "B. Two"}], "abstract": "Abstract text."},
              "passages": [{"score": 0.42, "text": "Routing works by ..."}]}
out = m._render_paper(paper_resp, "how does routing work")
check("paper render: title+abstract", "# ProbMoE" in out and "Abstract text." in out)
check("paper render: categories", "cs.LG, cs.AI" in out)
check("paper render: author dict+str coerced", "A. One" in out and "B. Two" in out)
check("paper render: passages section", "Top passages for: how does routing work" in out and "Routing works by" in out)
out_nometa = m._render_paper({"paper": {"title": "X", "abstract": "Y"}}, None)
check("paper render: no query -> no passages section", "Top passages" not in out_nometa)

gh_resp = [
    {"resultType": "repo_readme", "repo": "dao-ailab/flash-attention",
     "url": "https://github.com/dao-ailab/flash-attention", "snippet": "FlashAttention readme."},
    {"resultType": "github_history", "repo": "x/y", "pageType": "merged_pr", "number": 2584,
     "url": "https://github.com/x/y/issues/2584", "title": "Conversation", "snippet": "PR discussion."},
]
out = m._render_github("flash attention", gh_resp)
check("github render: repo+readme", "dao-ailab/flash-attention" in out and "FlashAttention readme." in out)
check("github render: PR number", "#2584" in out)
check("github render: empty -> message", m._render_github("x", []) == "No GitHub research results for: x")

# --- defensive: malformed items don't crash ---
print("== defensive parsing ==")
check("papers render skips non-dict item", isinstance(m._render_papers("q", [None, {"title": "T"}]), str))
check("github render skips non-dict item", isinstance(m._render_github("q", ["bad", {"repo": "r"}]), str))

# --- : research_similar k clamp, mode fallback, min_score floor ---
print("== research_similar ==")
sim_seen = {}
def fake_similar_sync(paper_id, intent, k=8, mode="similar", rerank=None):
    sim_seen["k"] = k; sim_seen["mode"] = mode; sim_seen["rerank"] = rerank
    return [
        {"paperId": "a", "title": "High", "abstract": "x", "score": 0.9},
        {"paperId": "b", "title": "Low",  "abstract": "y", "score": 0.02},
    ]
m._research_similar_sync = fake_similar_sync

asyncio.run(m.research_similar("pid", "intent", k=99))
check("k clamp high (->25)", sim_seen["k"] == 25)
asyncio.run(m.research_similar("pid", "intent", k=0))
check("k clamp low (->1)", sim_seen["k"] == 1)
asyncio.run(m.research_similar("pid", "intent", mode="bogus"))
check("invalid mode -> similar", sim_seen["mode"] == "similar")
asyncio.run(m.research_similar("pid", "intent", mode="citers"))
check("valid mode passes through", sim_seen["mode"] == "citers")

out = asyncio.run(m.research_similar("pid", "intent"))
check("default: both papers rendered", "High" in out and "Low" in out)
out = asyncio.run(m.research_similar("pid", "intent", min_score=0.5))
check("min_score floor drops low-score", "High" in out and "Low" not in out)

# min_score filtering directly on the renderer
floor_res = [{"title": "Keep", "score": 0.8}, {"title": "Drop", "score": 0.1},
             {"title": "Unscored"}]
out = m._render_papers("q", floor_res, min_score=0.5)
check("renderer floor keeps >=, drops < and unscored", "Keep" in out and "Drop" not in out and "Unscored" not in out)
check("renderer floor off (0.0) keeps all", "Drop" in m._render_papers("q", floor_res))

# default-call output <= 8 papers (k clamp already asserted; render count sanity)
many = [{"title": f"P{i}", "score": 0.5} for i in range(8)]
check("8 results render 8 blocks", m._render_papers("q", many).count("## ") == 8)

# --- : Developer Index migration ------------------------------------------
print("==  developer index ==")
# Capture the REAL legacy sync BEFORE any spy is installed. Late binding is what makes a
# module-attribute spy work at all, and it is the same property that makes the original
# unreachable afterwards — the envelope-hardening checks below must exercise the real one.
_real_gh_sync = m._research_github_sync

dev_cap = {}
def fake_post_json(url, payload, headers, secret, timeout=m.HTTP_TIMEOUT, deadline=None):
    dev_cap["url"] = url; dev_cap["payload"] = payload; dev_cap["timeout"] = timeout
    return dev_cap.get("resp", {"success": True, "results": [], "reranked": True,
                                "coverage": {t: "ok" for t in m.DEV_TYPES}})
m._post_json = fake_post_json

def run_gh(**kw):
    return asyncio.run(m.research_github(kw.pop("query", "q"), **kw))

# 1/2/3 — request construction, filter threading, clamps
run_gh()
check("developer POST url", dev_cap["url"] == "https://api.firecrawl.dev/v2/search/developer")
check("payload has query/k/passages", set(dev_cap["payload"]) == {"query", "k", "passages"})
check("passages sent as int", isinstance(dev_cap["payload"]["passages"], int))
check("developer uses research timeout", dev_cap["timeout"] == m.RESEARCH_HTTP_TIMEOUT)
check("types/repos omitted when unset", "types" not in dev_cap["payload"] and "repos" not in dev_cap["payload"])
run_gh(types=["doc"], repos=None)
check("types sent when supplied", dev_cap["payload"]["types"] == ["doc"])
run_gh(k=99, passages=99)
check("k clamped to 25 at the tool", dev_cap["payload"]["k"] == 25)
check("passages clamped to 5", dev_cap["payload"]["passages"] == 5)
run_gh(k=0, passages=0)
check("k/passages clamped low", dev_cap["payload"]["k"] == 1 and dev_cap["payload"]["passages"] == 1)

# 4/5 — semantic refusals happen BEFORE any request, and carry their own prefix
dev_cap.pop("url", None)
out = run_gh(types=["docs"])
check("unknown type refused with RESEARCH_REFUSED", out.startswith("RESEARCH_REFUSED:"))
check("unknown type echoes expected values", "expected doc, issue, pull_request, readme" in out)
check("unknown type issued NO request", "url" not in dev_cap)
out = run_gh(types=["doc"], repos=["a/b"])
check("repos/types conflict refused", out.startswith("RESEARCH_REFUSED:") and "drop repos" in out)
check("conflict issued NO request", "url" not in dev_cap)

# 6/7/8 — renderer
env = {"success": True, "reranked": True, "coverage": {t: "ok" for t in m.DEV_TYPES},
       "results": [{"id": "doc:1", "type": "doc", "url": "https://ex.com/a",
                    "passages": [{"text": "y" * 900, "citation_url": "https://ex.com/raw"}]}]}
out = m._render_developer("q", env)
check("renderer: title-less doc falls back to url", "## 1. https://ex.com/a" in out)
check("renderer: passage clip marks with ellipsis", "…" in out)
check("renderer: citation_url shown", "(source: https://ex.com/raw)" in out)
check("renderer: all-ok -> no coverage line", "[coverage:" not in out)
out = m._render_developer("q", {**env, "results": [], "coverage": {t: "ok" for t in m.DEV_TYPES}})
check("renderer: empty-success message + forced coverage",
      out.startswith("No developer-index results for: q") and "[coverage:" in out)
check("renderer: empty-success has no fallback text", "fallback" not in out)
out = m._render_developer("q", {**env, "coverage": {"doc": "skipped", "issue": "ok",
                                                    "pull_request": "ok", "readme": "ok"}})
check("renderer: skipped alone -> no coverage line", "[coverage:" not in out)
out = m._render_developer("q", {**env, "coverage": {"doc": "degraded", "issue": "ok",
                                                    "pull_request": "ok", "readme": "ok"}})
check("renderer: degraded -> coverage line", "[coverage: doc=degraded" in out)

# 12 — output cap: <= cap AND the note is separated by a blank line
big = {"success": True, "reranked": True, "coverage": {t: "ok" for t in m.DEV_TYPES},
       "results": [{"id": f"r{i}", "type": "readme", "url": f"https://e/{i}", "title": f"t{i}",
                    "passages": [{"text": "z" * 800}, {"text": "w" * 800}]} for i in range(25)]}
out = m._render_developer("q", big)
check("cap: output <= DEV_MAX_OUTPUT_CHARS", len(out) <= m.DEV_MAX_OUTPUT_CHARS)
check("cap: clip note present and separated", "\n\n[output clipped at" in out)

# 14 — legacy envelope hardening (uses the REAL sync captured above)
def _legacy_raises(resp, label):
    m._get_json = lambda *a, **k: resp
    try:
        _real_gh_sync("q", 2)
    except m.RetrievalError:
        return True
    except Exception:  # noqa: BLE001 — anything else is the bug this guards
        return False
    return False
check("legacy: non-dict body raises", _legacy_raises(["not", "a", "dict"], "list"))
check("legacy: success=false raises", _legacy_raises({"success": False, "results": []}, "sf"))
check("legacy: non-list results raises", _legacy_raises({"success": True, "results": {"a": 1}}, "nl"))
check("legacy: all-malformed entries raise", _legacy_raises({"success": True, "results": [None]}, "me"))
m._get_json = fake_get_json  # restore for anything after this point

# 9/10/15/16 — fallback activation, measured with a counting spy
gh_calls = {"n": 0, "ret": []}
def spy_gh(query, k):
    gh_calls["n"] += 1
    return gh_calls["ret"]
m._research_github_sync = spy_gh

def fallback_case(resp, **kw):
    gh_calls["n"] = 0
    dev_cap["resp"] = resp
    return run_gh(**kw), gh_calls["n"]

out, n = fallback_case({"success": False, "results": []})
check("fallback fires on success=false", n == 1 and "[fallback: developer index unavailable" in out)
out, n = fallback_case({"success": True, "results": "nope"})
check("fallback fires on non-list results", n == 1)
out, n = fallback_case({"success": True, "results": [None]})
check("fallback fires on all-malformed results", n == 1)
# no `types` argument here on purpose: the filter guard would suppress the fallback first
out, n = fallback_case({"success": True, "results": [],
                        "coverage": {t: "unavailable" for t in m.DEV_TYPES}})
check("fallback fires when every type is unavailable", n == 1)
out, n = fallback_case({"success": True, "results": [],
                        "coverage": {t: "ok" for t in m.DEV_TYPES}})
check("NO fallback on legitimate empty result", n == 0 and "No developer-index results" in out)
out, n = fallback_case({"success": True, "results": []})   # coverage key absent entirely
check("NO fallback when coverage is absent", n == 0)
out, n = fallback_case({"success": False, "results": []}, types=["doc"])
check("filters suppress the fallback", n == 0 and "legacy fallback skipped" in out)
check("filter-suppressed failure uses RESEARCH_FAILED", out.startswith("RESEARCH_FAILED:"))

# 16 — fourth caller-visible family: developer fails, legacy succeeds but is EMPTY
gh_calls["ret"] = []
out, n = fallback_case({"success": False, "results": []})
check("4th family: fallback header + legacy empty message",
      n == 1 and "[fallback: developer index unavailable" in out
      and "No GitHub research results for: q" in out)

# 11 — both tiers fail -> combined trail
def spy_gh_raises(query, k):
    gh_calls["n"] += 1
    raise m.RetrievalError("legacy boom")
m._research_github_sync = spy_gh_raises
out, n = fallback_case({"success": False, "results": []})
check("both tiers fail -> combined trail",
      out.startswith("RESEARCH_FAILED:") and "developer:" in out and "| legacy: legacy boom" in out)
m._research_github_sync = spy_gh

# --- opt-in LIVE smoke (keyless) ---
if "--live" in sys.argv:
    print("== LIVE (keyless) ==")
    importlib.reload  # noqa
    # restore real funcs by re-importing a fresh module
    m2 = importlib.util.module_from_spec(spec); spec.loader.exec_module(m2)
    async def live():
        txt = await m2.research_papers("mixture of experts routing", k=2)
        check("live research_papers returns papers", "Research papers:" in txt and "arXiv:" in txt)
        txt2 = await m2.research_github("flash attention", k=2)
        #  header moved from "# GitHub research:" (legacy) to "# Developer index:".
        # A fallback line here means the Developer Index did NOT serve — that is a failure
        # of this smoke, not a pass, so assert its absence explicitly.
        check("live research_github hits the Developer Index", "Developer index:" in txt2)
        check("live research_github did not fall back", "[fallback:" not in txt2)
        txt3 = await m2.research_github("retries configuration", k=2, types=["doc"])
        check("live research_github honors types=[doc]",
              "Developer index:" in txt3 and "RESEARCH_REFUSED" not in txt3)
    asyncio.run(live())

print(f"\n{'ALL RESEARCH-INDEX TESTS PASS' if fails == 0 else str(fails) + ' FAILURE(S)'}")
sys.exit(1 if fails else 0)
