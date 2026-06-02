# Publishing & discoverability runbook

How to take `web-retrieval-mcp` from "on GitHub" to "agents find, install, and cite it."
Ordered by leverage. Steps 1–2 are gating; the rest are amplification.

## Requirements → action map

| Discovery channel | What it indexes / requires | Action |
|---|---|---|
| **GitHub search & Google** | repo **name**, **About/description**, **topics** (highest weight), README H1/H2 + first paragraph, Releases | Done: keyword name, description, 18 topics, structured README. Cut a Release (step 3). |
| **Official MCP Registry** (`registry.modelcontextprotocol.io`) | `server.json`, a package on a trusted registry (**PyPI**), GitHub OAuth namespace ownership | Steps 1–2. Aggregators ingest from here. |
| **Glama** (`glama.ai/mcp`) | auto-indexes from **GitHub topics + README**; scores the worst-described tool at 40% weight | Topics set; tool docstrings are descriptive. Auto-picks up. |
| **mcp.so** | auto-pulls README: first bash block with `claude mcp add`, `## Tools` heading + table, first image | README has both. Optional: submit at https://mcp.so/submit. |
| **PulseMCP** | syncs from the official Registry + GitHub topics | Automatic after step 2. |
| **Smithery** (`smithery.ai`) | auto-crawls GitHub; `smithery.yaml` tunes ranking (name / description / categories) | Optional: add `smithery.yaml` or submit the repo URL. |
| **awesome-mcp-servers** | hand-curated; pulls your README's first paragraph | Optional: PR one line to `punkpeye/awesome-mcp-servers`. |
| **LLM citation (GEO)** | 200–400-token answer-first passages, tables, FAQ, entity/brand signals | README + `llms.txt` + `AGENTS.md` are answer-first and structured. |

## Step 1 — Publish to PyPI (gating) — ✅ DONE (v0.1.0)

Live at https://pypi.org/project/web-retrieval-mcp/ — `pip install web-retrieval-mcp` resolves.
For the next version, bump `version` in `pyproject.toml`, then:

```
python -m build                 # builds dist/*.whl + *.tar.gz
uv publish                      # or: twine upload dist/*   (needs a PyPI API token)
```

## Step 2 — Official MCP Registry (gating for aggregators)

Requires PyPI (step 1) + GitHub OAuth. **Namespace caveat:** the repo lives under the
`VelvetSP` org, and the registry verifies namespace ownership casing-exact via OAuth —
authorize the publisher app for the `VelvetSP` org so `io.github.velvetsp/...` validates.

```
brew install mcp-publisher       # or download from the registry releases
mcp-publisher login              # GitHub OAuth (authorize the VelvetSP org)
mcp-publisher publish            # reads ./server.json
```

`server.json` is already in the repo — validate it with `mcp-publisher` before publishing
(schema fields evolve; confirm against the `$schema` URL in the file).

## Step 3 — Tag a GitHub Release (freshness + Google index)

```
git tag v0.1.0
git push origin v0.1.0
gh release create v0.1.0 --title "v0.1.0" --notes "First release."
```

## Step 4 — Amplify (optional, low effort, slow burn)

- mcp.so: submit at https://mcp.so/submit (re-fetches README weekly).
- Smithery: submit the GitHub URL; optionally add `smithery.yaml`.
- awesome-mcp-servers: PR one line to `punkpeye/awesome-mcp-servers` (DoFollow backlink).
