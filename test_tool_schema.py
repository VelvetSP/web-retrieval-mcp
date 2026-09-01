"""Tool-schema smoke: module loads and all six tools expose
annotations.read_only_hint == True. Also catches MCPServer schema-generation breakage
from the new optional params (a real-MCP-only failure direct calls miss), and pins the
cross-client complementary/replacement retrieval guidance.
The attribute is snake_case under SDK v2; the camelCase constructor argument in
server.py (`ToolAnnotations(readOnlyHint=True)`) still works and was left alone.
Run with a Python environment containing the project dependencies.
"""
import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
import web_retrieval_mcp.server as m

fails = 0
def check(label, cond):
    global fails
    if not cond: fails += 1
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")

tools = asyncio.run(m.mcp.list_tools())
names = {t.name for t in tools}
by_name = {t.name: t for t in tools}
check("6 tools registered", len(tools) == 6)
for want in ("web_search", "web_fetch", "research_papers", "research_paper",
             "research_similar", "research_github"):
    check(f"tool present: {want}", want in names)
for t in tools:
    ann = getattr(t, "annotations", None)
    check(f"{t.name}: read_only_hint is True", ann is not None and ann.read_only_hint is True)

for name in ("web_search", "web_fetch"):
    description = " ".join((by_name[name].description or "").lower().split())
    check(f"{name}: advertises a complementary retrieval lane",
          "complementary retrieval lane" in description)
    check(f"{name}: has no replacement-only directive",
          "use this instead" not in description)

print(f"\n{'ALL TOOL-SCHEMA TESTS PASS' if fails == 0 else str(fails) + ' FAILURE(S)'}")
sys.exit(1 if fails else 0)
