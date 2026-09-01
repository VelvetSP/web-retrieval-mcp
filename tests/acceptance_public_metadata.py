"""Black-box acceptance for the public package and discovery metadata.

Usage: python tests/acceptance_public_metadata.py DIST_DIR
   or: python tests/acceptance_public_metadata.py DIST.whl DIST.tar.gz

The test reads only built artifacts. It deliberately does not import the project or
derive its expectations from source configuration.
"""

from __future__ import annotations

from email import message_from_bytes, policy
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import zipfile

from readme_renderer.markdown import render, variants


failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        failures.append(label)
    print(f"{'OK  ' if condition else 'FAIL'}  {label}  {detail}")


if len(sys.argv) == 2 and Path(sys.argv[1]).is_dir():
    artifact_dir = Path(sys.argv[1])
    wheels = list(artifact_dir.glob("*.whl"))
    sdists = list(artifact_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit(
            f"expected one wheel and one sdist in {artifact_dir}; "
            f"got wheels={wheels}, sdists={sdists}"
        )
    wheel, sdist = wheels[0], sdists[0]
elif len(sys.argv) == 3:
    wheel, sdist = Path(sys.argv[1]), Path(sys.argv[2])
else:
    raise SystemExit(
        "usage: acceptance_public_metadata.py DIST_DIR | DIST.whl DIST.tar.gz"
    )
if not wheel.is_file() or wheel.suffix != ".whl":
    raise SystemExit(f"wheel not found: {wheel}")
if not sdist.is_file() or not sdist.name.endswith(".tar.gz"):
    raise SystemExit(f"sdist not found: {sdist}")


with zipfile.ZipFile(wheel) as archive:
    metadata_names = [
        name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
    ]
    if len(metadata_names) != 1:
        raise SystemExit(f"expected one wheel METADATA file, got {metadata_names}")
    metadata = message_from_bytes(
        archive.read(metadata_names[0]), policy=policy.default
    )
    entry_point_names = [
        name for name in archive.namelist()
        if name.endswith(".dist-info/entry_points.txt")
    ]
    if len(entry_point_names) != 1:
        raise SystemExit(
            f"expected one wheel entry_points.txt file, got {entry_point_names}"
        )
    entry_points = archive.read(entry_point_names[0]).decode("utf-8")

description = metadata.get_payload()
summary = metadata.get("Summary", "")
keywords = {
    value.strip()
    for value in metadata.get("Keywords", "").split(",")
    if value.strip()
}
project_urls = {}
for entry in metadata.get_all("Project-URL", []):
    label, separator, url = entry.partition(",")
    if separator:
        project_urls[label.strip()] = url.strip()

check("package identity", metadata.get("Name") == "web-retrieval-mcp")
check("README is the Markdown long description",
      metadata.get("Description-Content-Type") == "text/markdown")
check("one-line summary names MCP web search and agents",
      "MCP web search" in summary and "AI agents" in summary)
check(
    "search metadata covers protocol, task, and providers",
    {
        "mcp", "mcp-server", "model-context-protocol", "ai-agents",
        "web-search", "web-fetch", "web-scraping", "rag",
        "exa", "firecrawl", "tavily", "camoufox",
    }.issubset(keywords),
)
check(
    "PyPI exposes canonical project links",
    {"Homepage", "Documentation", "Source", "Issues", "Release Notes"}
    .issubset(project_urls),
)
check(
    "wheel advertises every documented optional capability",
    {"all", "cache", "dev", "keyring", "render", "tavily"}
    == set(metadata.get_all("Provides-Extra", [])),
)
check(
    "wheel exposes the server and optional policy-installer commands",
    "web-retrieval-mcp = web_retrieval_mcp.server:main" in entry_points
    and "web-retrieval-mcp-install = web_retrieval_mcp.install:main" in entry_points,
)

check("PyPI GFM renderer is installed", "GFM" in variants)
markdown_targets = re.findall(r"!?\[[^]]*\]\(([^)]+)\)", description)
source_relative = [
    url for url in markdown_targets
    if not url.startswith(("https://", "http://", "#", "mailto:"))
]
check(
    "packaged README file links are portable across GitHub and PyPI",
    not source_relative,
    f"relative={source_relative[:3]}",
)
check(
    "packaged README carries MCP identity and complete install commands",
    all(value in description for value in (
        "<!-- mcp-name: io.github.VelvetSP/web-retrieval-mcp -->",
        "pipx install --include-deps 'web-retrieval-mcp[all]'",
        "uvx web-retrieval-mcp",
        "python -m pip install 'web-retrieval-mcp[all]'",
        "python -m camoufox fetch",
    )),
)
rendered = render(description) if "GFM" in variants else None
check("packaged README renders through PyPI's GFM renderer", rendered is not None)
if rendered is not None:
    check("render contains the product title and tables",
          "web-retrieval-mcp" in rendered and "<table>" in rendered)
    check("render contains onboarding and security sections",
          "Quick start" in rendered and "Security model" in rendered)
    check(
        "render contains the complete six-tool inventory",
        all(name in rendered for name in (
            "web_search", "web_fetch", "research_papers", "research_paper",
            "research_similar", "research_github",
        )),
    )
    embedded_urls = re.findall(r'(?:href|src)="([^"]+)"', rendered)
    unsafe_relative = [
        url for url in embedded_urls
        if not url.startswith(("https://", "http://", "#", "mailto:"))
    ]
    check(
        "PyPI-rendered file links are absolute and anchors stay local",
        not unsafe_relative,
        f"relative={unsafe_relative[:3]}",
    )


with tarfile.open(sdist) as archive:
    members = {member.name: member for member in archive.getmembers()}

    def sdist_text(relative: str) -> str:
        matches = [
            name for name in members
            if PurePosixPath(name).parts[1:] == PurePosixPath(relative).parts
        ]
        if len(matches) != 1:
            failures.append(f"sdist member: {relative}")
            print(f"FAIL  sdist member: {relative}  matches={matches}")
            return ""
        stream = archive.extractfile(members[matches[0]])
        if stream is None:
            failures.append(f"sdist readable member: {relative}")
            print(f"FAIL  sdist readable member: {relative}")
            return ""
        return stream.read().decode("utf-8")

    required = (
        "README.md", "llms.txt", "server.json", "PUBLISHING.md", "SECURITY.md",
        "docs/README.md", "docs/CACHE.md", "docs/TESTING.md",
        "tests/acceptance_public_metadata.py",
    )
    extracted = {name: sdist_text(name) for name in required}

check("sdist contains the public documentation and discovery set",
      all(extracted.values()))

manifest_text = extracted.get("server.json", "")
manifest = json.loads(manifest_text) if manifest_text else {}
package = (manifest.get("packages") or [{}])[0]
variables = package.get("environmentVariables") or []
check("MCP Registry identity",
      manifest.get("name") == "io.github.VelvetSP/web-retrieval-mcp")
check("MCP Registry description satisfies schema length",
      1 <= len(manifest.get("description", "")) <= 100)
manifest_description = manifest.get("description", "").lower()
check(
    "MCP Registry description names core capabilities",
    all(value in manifest_description for value in (
        "web search", "page retrieval", "research", "provenance", "ssrf",
    )),
)
check("MCP Registry and wheel versions agree",
      manifest.get("version") == metadata.get("Version") == package.get("version"))
check("MCP Registry points to the PyPI stdio package",
      package.get("registryType") == "pypi"
      and package.get("identifier") == "web-retrieval-mcp"
      and package.get("transport") == {"type": "stdio"})
check(
    "MCP Registry declares all provider keys as optional secrets",
    {value.get("name") for value in variables}
    == {"EXA_API_KEY", "FIRECRAWL_API_KEY", "TAVILY_API_KEY"}
    and all(value.get("isSecret") is True for value in variables)
    and all(value.get("isRequired") is False for value in variables),
)

llms = extracted.get("llms.txt", "")
check("llms.txt has proposal-shaped H1 and summary",
      llms.startswith("# web-retrieval-mcp\n\n> "))
check("llms.txt provides multiple absolute-link sections",
      len(re.findall(r"^## ", llms, re.MULTILINE)) >= 2
      and len(re.findall(r"^- \[[^]]+\]\(https://", llms, re.MULTILINE)) >= 8)
check(
    "llms.txt points at the canonical package, source, and registry surfaces",
    all(value in llms for value in (
        "https://pypi.org/project/web-retrieval-mcp/",
        "https://github.com/VelvetSP/web-retrieval-mcp",
        "https://raw.githubusercontent.com/VelvetSP/web-retrieval-mcp/main/server.json",
        "io.github.VelvetSP/web-retrieval-mcp",
    )),
)

print(
    f"\n{'PUBLIC METADATA ACCEPTANCE PASSED' if not failures else str(len(failures)) + ' FAILURE(S)'}"
)
raise SystemExit(1 if failures else 0)
