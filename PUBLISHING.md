# Publishing runbook

This repository publishes the `web-retrieval-mcp` Python package and MCP Registry
manifest. Publishing changes external state and must be performed only by an
authorized maintainer.

## Prepare and verify

1. Update the version in `pyproject.toml`, `src/web_retrieval_mcp/_version.py`, and
   `server.json`.
2. Update `README.md`, `llms.txt`, and release notes for user-visible behavior.
3. Start from a branch whose complete reachable history is approved for public
   disclosure. Never publish a branch descended from a private development history.
4. Install the full test environment and run the release gate:

   ```bash
   python3.12 -m pip install -e '.[all,dev]'
   python3.12 -m camoufox fetch
   ./run-tests.sh
   ```

5. When browser or SSRF behavior changed, also run:

   ```bash
   python3.12 test_ssrf_redirect_live.py
   ```

6. Inspect the exact commit and artifacts for credentials, private hosts, local paths,
   generated files, and unexpected history before publication.

## Publish the package

Build fresh artifacts from the reviewed commit and verify their metadata and contents:

```bash
python3.12 -m build
python3.12 -m twine check dist/*
python3.12 tests/acceptance_public_metadata.py dist/
python3.12 -m zipfile -l dist/*.whl
python3.12 -m tarfile -l dist/*.tar.gz
```

Upload with a trusted publisher or a scoped PyPI token held outside the repository.
Never place a token in a command argument, file in the tree, shell history, or log.

## Publish GitHub and MCP Registry metadata

After the package exists on PyPI:

1. Push the reviewed public branch and open/review the public pull request.
2. Tag the merged commit as `v<version>` and create release notes.
3. Validate `server.json` with the current `mcp-publisher` release.
4. Authenticate interactively and run `mcp-publisher publish`.

Do not automate PyPI, GitHub Release, or MCP Registry publication from an untrusted
pull-request workflow. Keep repository workflow permissions read-only unless a separate,
reviewed release workflow genuinely needs more.

## Discoverability checklist

Search engines and package/agent catalogs draw from different metadata surfaces. Keep
them semantically aligned while respecting each surface's length and format limits; do
not add keyword dumps or claims that are broader than the current release.

### GitHub repository settings

GitHub's default repository search uses the repository name, description, and topics;
README text is searchable only when a user explicitly includes README content. After
public push authorization, update the external repository settings to match the release:

- **About description:** `Reliable MCP web search and web fetch for AI agents: Exa or Tavily search, tiered Exa/Camoufox/Tavily/Firecrawl retrieval, research indexes, provenance, caching, and SSRF guards.`
- **Website:** `https://pypi.org/project/web-retrieval-mcp/`
- **Topics** (GitHub allows at most 20): `ai-agents`, `ai-search`, `camoufox`,
  `claude-code`, `cursor`, `exa`, `firecrawl`, `llm-tools`, `mcp`, `mcp-registry`,
  `mcp-server`, `model-context-protocol`, `python`, `rag`, `research-papers`,
  `tavily`, `web-fetch`, `web-retrieval`, `web-scraping`, `web-search`.
- **Social preview:** upload a legible 1280×640 image under 1 MB. Use the project
  name, “MCP web search + tiered fetch,” and a short Exa/Tavily/Camoufox/Firecrawl
  routing motif; verify readability in GitHub's compact preview.

These are GitHub-hosted settings, not repository files. Changing them is a separate
external-state action and is never implied by editing this runbook.

### Search and agent-facing files

- Keep the README's H1, opening paragraph, section headings, and FAQ descriptive and
  people-first. Search snippets are commonly drawn from visible page content.
- Use absolute repository URLs for README file links so the same long description
  works on both GitHub and PyPI; same-document `#anchors` may remain relative.
- Keep `pyproject.toml`'s description, keywords, and well-known project URLs current;
  the README becomes the PyPI long description.
- Keep `server.json` versioned with the package. Publish the package to PyPI before
  the MCP Registry because the registry validates package ownership and availability.
- Keep the registry description capability-focused and at most 100 characters, as
  required by the referenced `server.json` schema.
- Validate `server.json` with `mcp-publisher validate` before publishing a new,
  immutable registry version. Correct published metadata by releasing a new version,
  not by assuming an existing registry entry can be overwritten.
- Keep `llms.txt` short and link-oriented. It follows the llms.txt proposal for
  agent-readable navigation; it is a supplemental discovery surface, not a formal web
  standard or a substitute for the README and MCP Registry metadata.

### Post-publication verification

1. Open the GitHub repository while signed out and verify the About text, topics,
   social preview, README anchors, and relative links.
2. Open the PyPI project and verify the version, one-line summary, rendered README,
   Python requirement, extras, license, and project links.
3. Search the MCP Registry for `io.github.VelvetSP/web-retrieval-mcp` and verify the
   package version, transport, and all three optional provider variables.
4. Install from PyPI in a clean environment and list the six MCP tools.
5. Check the raw URLs in `llms.txt` after the public branch becomes the repository's
   default branch.

Primary guidance used for this checklist:

- [GitHub README guidance](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes)
- [GitHub repository topics](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics)
- [GitHub social previews](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview)
- [Google Search SEO Starter Guide](https://developers.google.com/search/docs/fundamentals/seo-starter-guide)
- [Python project metadata](https://packaging.python.org/specifications/declaring-project-metadata/)
- [PyPI-friendly README guidance](https://packaging.python.org/en/latest/guides/making-a-pypi-friendly-readme/)
- [Well-known Python project URLs](https://packaging.python.org/en/latest/specifications/well-known-project-urls/)
- [MCP Registry publishing quickstart](https://modelcontextprotocol.io/registry/quickstart)
- [llms.txt proposal](https://llmstxt.org/)
