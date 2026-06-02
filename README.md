<!-- mcp-name: io.github.VelvetSP/web-retrieval-mcp -->

# web-retrieval-mcp — MCP web search & web fetch for AI agents (Exa + Firecrawl)

> **web-retrieval-mcp is an open-source [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that gives AI agents two web tools — neural web search (Exa) and a tiered web fetch (Exa → optional local browser → Firecrawl) — as a drop-in replacement for built-in WebSearch/WebFetch.** It preserves per-source provenance, guards against SSRF, runs cross-platform (macOS/Linux/Windows), and works with **Claude Code, Claude Desktop, Cursor, and any MCP client**. **Runs on free API tiers.**

[![PyPI](https://img.shields.io/pypi/v/web-retrieval-mcp.svg)](https://pypi.org/project/web-retrieval-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/Model_Context_Protocol-server-7c3aed.svg)](https://modelcontextprotocol.io)
[![Cross-platform](https://img.shields.io/badge/macOS%20%7C%20Linux%20%7C%20Windows-supported-informational.svg)](#cross-platform-api-keys)

---

## Why replace the built-in web tools?

An agent's stock `WebSearch` / `WebFetch` tend to flatten many sources into one blurry summary, drop provenance, and silently fail on JavaScript-heavy or anti-bot pages. This server fixes that:

| | Built-in web tools | **web-retrieval-mcp** |
|---|---|---|
| **Search results** | One merged summary, sources conflated | **One block per result** — each keeps its own title, URL, highlights, and text, plus a `Sources` trailer |
| **Fetch reliability** | Single attempt, gives up on hard pages | **Tiered fallback**: Exa contents → optional local browser → Firecrawl, with a `[served by: …]` provenance header |
| **JS / anti-bot pages** | Usually fails | Opt-in real headless browser ([camoufox](https://github.com/daijro/camoufox)) on demand |
| **Safety** | — | **SSRF guard** rejects loopback / private / link-local / multicast hosts before any request |
| **Cost** | Bundled / metered by your model vendor | **Free** on Exa + Firecrawl free tiers (see below) |

## Runs on free API tiers — and the free tiers are more than enough

Both providers have a genuinely usable **free, no-credit-card** tier, and because fetches hit **Exa first** (Firecrawl is only the fallback), a single developer or agent rarely touches the Firecrawl quota at all:

| Provider | Free tier (verified 2026) | Role in this server |
|---|---|---|
| **Exa** | **1,000 requests / month**, no card | Powers `web_search` **and** the first `web_fetch` tier |
| **Firecrawl** | **1,000 pages / month**, no card | Fallback fetch tier only — rarely reached |
| **camoufox** (local browser) | **Unlimited & free** — runs on your machine | Opt-in `render="always"` tier for JS/anti-bot pages |

For a personal agent that's ~33 searches **and** 33 hard-page fetches **every day**, indefinitely, for **$0/month**. Heavy production workloads can upgrade either provider independently — the tiering and code don't change.

## Features

- 🔎 **`web_search`** — neural / keyword / auto search via Exa, one provenance-preserving block per result.
- 🌐 **`web_fetch`** — single-URL readable content through a resilient tier chain with provenance headers.
- 🧱 **Tiered fallback** — Exa contents → (opt-in) local camoufox browser → Firecrawl, so hard pages still resolve.
- 🛡️ **SSRF guard** — non-public hosts (loopback, RFC-1918, link-local, multicast, NAT64) are refused up front.
- 🔑 **Cross-platform secrets** — env vars, a key file, the `keyring` library, or an OS secret tool. No keys on the command line.
- 🚫 **Hook to disable the built-ins** — bundled PreToolUse hook + one-command installer so agents *must* use these tools.
- 📦 **One-command install** — `uvx`, `pipx`, or `pip`; ships two console scripts.

## Quickstart

```bash
# Run with no install — uvx fetches and runs it on demand:
uvx web-retrieval-mcp

# Or install the CLI (isolated, recommended):
pipx install web-retrieval-mcp          # or: pip install web-retrieval-mcp

# Optional extras:
pip install "web-retrieval-mcp[render]"    # local headless-browser tier (render="always")
pip install "web-retrieval-mcp[keyring]"   # cross-platform native secret store
python -m camoufox fetch                   # one-time browser download (only if you use [render])
```

> On [PyPI](https://pypi.org/project/web-retrieval-mcp/). Prefer the bleeding edge? Install from source: `pipx install git+https://github.com/VelvetSP/web-retrieval-mcp`.

Get free API keys: **Exa** → https://exa.ai · **Firecrawl** → https://firecrawl.dev — then:

```bash
export EXA_API_KEY="exa-..."
export FIRECRAWL_API_KEY="fc-..."
```

### Register with Claude Code

```bash
# After `pipx install web-retrieval-mcp` puts the script on your PATH:
claude mcp add web-retrieval -- web-retrieval-mcp

# Or with no prior install, via uvx:
claude mcp add web-retrieval -- uvx web-retrieval-mcp
```

### Register with Claude Desktop / any MCP client

```json
{
  "mcpServers": {
    "web-retrieval": {
      "command": "web-retrieval-mcp",
      "env": {
        "EXA_API_KEY": "exa-...",
        "FIRECRAWL_API_KEY": "fc-..."
      }
    }
  }
}
```

`command` above assumes `web-retrieval-mcp` is on PATH (after `pipx install`). Otherwise set `command` to `uvx` with `args: ["--from", "git+https://github.com/VelvetSP/web-retrieval-mcp", "web-retrieval-mcp"]`.

## Tools

| Tool | Signature | What it returns |
|---|---|---|
| **`web_search`** | `web_search(query, num_results=8, mode="auto")` | Neural web search via Exa. One block per result — each with its own title, URL, published date, highlights, and text — plus a `Sources` list. `mode` ∈ `auto` \| `neural` \| `keyword`. |
| **`web_fetch`** | `web_fetch(url, render="auto", max_chars=20000, max_age_hours=None)` | One URL's readable content through the tier chain, with a `[served by: …]` provenance header. |

### `web_fetch` details
Fetch one URL's readable content through the tier chain, returned with a `[served by: …]` header.

```
render="auto"   (default) →  Exa /contents  →  Firecrawl                 # no local browser
render="never"            →  Exa /contents  →  Firecrawl                 # same, explicit
render="always"           →  camoufox (local browser)  →  Firecrawl      # for JS / anti-bot pages
```

`max_age_hours` controls Exa's freshness window (`0` = force fresh; `None` = Exa default cache).

## Cross-platform API keys

Keys are resolved **in-process** (never on the command line, which is visible via `ps`), cheapest/safest source first — the **same code path on macOS, Linux, and Windows**:

1. **Environment variables** — `EXA_API_KEY`, `FIRECRAWL_API_KEY`. Universal; **required for headless / CI**.
2. **Key file** — a dotenv-style `KEY=value` file at `$WEB_RETRIEVAL_MCP_ENV_FILE` or `<config-dir>/keys.env`
   (`~/.config/web-retrieval-mcp/` on Linux/macOS, `%APPDATA%\web-retrieval-mcp\` on Windows).
3. **`keyring` library** — native store on every OS: macOS Keychain, Windows Credential Locker, Linux Secret Service / KWallet. Install the `[keyring]` extra, then store under service `web-retrieval-mcp`:
   ```bash
   keyring set web-retrieval-mcp EXA_API_KEY
   keyring set web-retrieval-mcp FIRECRAWL_API_KEY
   ```
4. **OS-native secret CLI** — macOS `security`, Linux `secret-tool` (libsecret), if present.

An unexpanded `${...}` config literal is treated as absent.

## Block the built-in web tools (Claude Code)

So agents and subagents can't silently fall back to the lower-fidelity built-ins, this repo ships a PreToolUse hook that **denies** `WebSearch` / `WebFetch` and points the agent here. Install it idempotently:

```bash
web-retrieval-mcp-install              # patch ~/.claude/settings.json (backs it up first)
web-retrieval-mcp-install --print      # preview only, write nothing
web-retrieval-mcp-install --register-mcp   # also run `claude mcp add`
web-retrieval-mcp-install --uninstall  # remove the hook
```

Break-glass: `touch ~/.claude/.web-builtins-allow` re-enables the built-ins for the session; remove the file to re-arm. The hook is pure POSIX `sh` (no `jq`).

## Security — SSRF

`web_fetch` validates every URL before any request: non-`http(s)` schemes and any host resolving to a non-public IP (loopback, private/RFC-1918, link-local, reserved/NAT64, multicast) are refused. The only tier that runs a real browser on your machine (camoufox) is **opt-in** (`render="always"`), so the default path never exposes it. Residual: the camoufox tier follows redirects, so the up-front check covers the initial URL only — full closure would need a validating forward proxy. The default `auto`/`never` path never runs the browser.

## FAQ

**What is web-retrieval-mcp?**
An open-source MCP (Model Context Protocol) server that gives AI agents two web tools — `web_search` (Exa) and `web_fetch` (Exa → local browser → Firecrawl) — as a drop-in replacement for built-in web access, with provenance preservation and an SSRF guard.

**Does it work with Claude Code?**
Yes. Register with `claude mcp add web-retrieval -- web-retrieval-mcp`, and optionally install the bundled hook so the built-in `WebSearch`/`WebFetch` are disabled in favor of these tools.

**Is it free?**
Yes. The code is MIT-licensed, and it runs on the free tiers of Exa (1,000 requests/month) and Firecrawl (1,000 pages/month), neither of which requires a credit card. The local browser tier is free and unlimited.

**Which platforms are supported?**
macOS, Linux, and Windows. Key resolution and the server are cross-platform; the local browser tier needs the optional `[render]` extra.

**How is it better than built-in WebSearch/WebFetch?**
It returns one result block per source (no conflated summaries), preserves provenance, falls back across multiple fetch backends so hard/JS pages still resolve, and guards against SSRF.

**Do I need the browser stack?**
No. Search and the default fetch path need only `mcp` + `anyio`. The camoufox/playwright browser is the optional `[render]` extra, used only for `render="always"`.

## Publishing

> **Status:** ✅ on [PyPI](https://pypi.org/project/web-retrieval-mcp/) · ✅ [GitHub Release](https://github.com/VelvetSP/web-retrieval-mcp/releases) · ✅ listed on the [official MCP Registry](https://registry.modelcontextprotocol.io/v0/servers?search=web-retrieval) as `io.github.VelvetSP/web-retrieval-mcp`.

Aggregators (PulseMCP, Glama, mcp.so, Smithery) ingest from the official registry, so the listing propagates to them automatically. The [`server.json`](./server.json) manifest and the full runbook live in [`PUBLISHING.md`](./PUBLISHING.md). For a new version: bump the version, `python -m build`, `twine upload`, then `mcp-publisher publish`.

## Contributing

Issues and PRs welcome at https://github.com/VelvetSP/web-retrieval-mcp. The server is a single module (`src/web_retrieval_mcp/server.py`); `stdout` is JSON-RPC only — keep all diagnostics on `stderr`.

## License

[MIT](./LICENSE) © VelvetSP

---

<sub>Keywords: MCP server, Model Context Protocol, AI agent web search, LLM web fetch, Exa API, Firecrawl API, camoufox, Claude Code MCP, web scraping for agents, RAG retrieval, SSRF-safe fetch, cross-platform, free web search API.</sub>
