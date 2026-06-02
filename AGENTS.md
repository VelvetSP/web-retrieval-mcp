# AGENTS.md — web-retrieval-mcp

Machine-readable guide for AI coding agents (Claude Code, Codex, Cursor, etc.) working with or installing this repository.

## What this project is

An MCP (Model Context Protocol) server, written in Python, exposing two tools over stdio:

- `web_search(query, num_results=8, mode="auto")` — Exa neural/keyword/auto web search; one block per result with provenance.
- `web_fetch(url, render="auto", max_chars=20000, max_age_hours=None)` — tiered single-URL fetch: Exa contents → (opt-in) local camoufox browser → Firecrawl; returns a `[served by: …]` header.

It is a drop-in replacement for built-in `WebSearch`/`WebFetch`. The package also ships a Claude Code hook that disables those built-ins.

## How to install it for an end user

```bash
# 1. Install (pick one) — on PyPI
uvx web-retrieval-mcp                 # run without installing
pipx install web-retrieval-mcp        # or: pip install web-retrieval-mcp

# 2. Provide free API keys (env is the universal, headless-safe method)
export EXA_API_KEY="..."        # free key at https://exa.ai
export FIRECRAWL_API_KEY="..."  # free key at https://firecrawl.dev

# 3. Register with Claude Code
claude mcp add web-retrieval -- web-retrieval-mcp

# 4. (Optional) disable the built-in web tools so agents use this server
web-retrieval-mcp-install             # idempotent; backs up ~/.claude/settings.json
```

Optional extras: `[render]` adds the local headless browser (camoufox/playwright) for `render="always"`; `[keyring]` adds a cross-platform native secret store.

## Repository layout

- `src/web_retrieval_mcp/server.py` — the entire server (tools, tiers, SSRF guard, key resolution).
- `src/web_retrieval_mcp/install.py` — the `web-retrieval-mcp-install` hook installer.
- `src/web_retrieval_mcp/hooks/deny-web-builtins.sh` — the PreToolUse deny hook (pure POSIX sh).
- `pyproject.toml` — packaging, two console scripts, `render` + `keyring` extras.

## Conventions for code changes

- **`stdout` is JSON-RPC only.** Tools return strings; never `print()` to stdout — it corrupts the MCP protocol. Send diagnostics to `stderr`.
- Blocking I/O (HTTP, secret lookups) runs in a worker thread via `anyio.to_thread.run_sync`; each tier is wrapped in `asyncio.wait_for`.
- Secrets resolve in-process only — never place an API key on a command line (argv is world-visible via `ps`).
- Keep camoufox/playwright imports local to `_camoufox_render` so the browser stack stays optional.
- After any change, verify: `python -c "import ast; ast.parse(open('src/web_retrieval_mcp/server.py').read())"` and an install smoke test.

## Keywords

MCP server, Model Context Protocol, AI agent web search, LLM web fetch, Exa, Firecrawl, camoufox, Claude Code, RAG retrieval, web scraping, SSRF-safe, cross-platform, free web search API.
