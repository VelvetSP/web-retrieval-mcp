<!-- mcp-name: io.github.VelvetSP/web-retrieval-mcp -->

# web-retrieval-mcp — reliable MCP web search and web fetch for AI agents

> **An open-source [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for source-grounded web research. Give Claude Code, Claude Desktop, Cursor, or any stdio MCP client six read-only tools for Exa or Tavily search, resilient Exa → Camoufox → Tavily → Firecrawl page retrieval, AI/ML paper discovery, and developer-source search—with explicit provenance, optional local caching, and SSRF guards.**

[![PyPI](https://img.shields.io/pypi/v/web-retrieval-mcp.svg)](https://pypi.org/project/web-retrieval-mcp/)
[![CI](https://github.com/VelvetSP/web-retrieval-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/VelvetSP/web-retrieval-mcp/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/pypi/pyversions/web-retrieval-mcp.svg)](https://pypi.org/project/web-retrieval-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-16a34a.svg)](https://github.com/VelvetSP/web-retrieval-mcp/blob/main/LICENSE)
[![Model Context Protocol](https://img.shields.io/badge/MCP-server-7c3aed.svg)](https://modelcontextprotocol.io)

**[Why use it?](#why-use-web-retrieval-mcp) · [Quick start](#quick-start) · [Tools](#six-read-only-tools) · [Routing](#how-retrieval-works) · [Configuration](#configuration-reference) · [Security](#security-model) · [FAQ](#frequently-asked-questions)**

## Why use web-retrieval-mcp?

Web research is more reliable when an agent can see where every result came from,
choose the right retrieval strategy, and recover when one provider or page-access
method fails. `web-retrieval-mcp` makes those controls part of the tool contract:

- **Keep sources separate.** Default search returns one block per result with its own
  title, URL, highlights, and text, followed by a `Sources` trailer. Exa deep modes
  additionally return a clearly labeled synthesized answer, with grounding when Exa
  provides it, before the source-separated result blocks.
- **Use more than one retrieval path.** Search can use Exa or Tavily and fall back to
  Firecrawl. Full-page fetches can escalate from indexed content to a local Camoufox
  browser, optional Tavily Extract, and Firecrawl.
- **Know what actually served the answer.** Fetch responses identify the serving tier,
  cache state, semantic mode, and truncation instead of hiding the route.
- **Ask for the right shape.** Fetch a readable page body, a concise summary, or a
  grounded answer to a question. Control freshness, rendering, domains, dates, result
  count, and per-result text size.
- **Search specialist corpora.** Discover AI/ML papers, inspect query-relevant
  full-text passages, expand to related papers, and search developer documentation,
  READMEs, issues, and merged pull requests.
- **Own the deployment boundary.** Run locally over stdio or host a loopback HTTP
  service; select providers and secret storage yourself; optionally cache completed
  fetches in a private Valkey sidecar.
- **Fail explicitly.** Unsupported provider choices, missing optional packages, and
  unavailable credentials fail in the tool result. Tavily mappings, Firecrawl fallback
  filter gaps, and Exa category migrations are caller-visible; other Exa filter drops
  are written to server diagnostics.

### How it compares with built-in agent web tools

Built-in capabilities vary by agent and can improve over time. The useful comparison
is therefore not “all defaults are bad”; it is whether you need a portable,
inspectable retrieval layer with controls your current client does not expose.

| Need | Typical bundled search/fetch surface | **web-retrieval-mcp** |
|---|---|---|
| Search output | Client-defined result or synthesis format | Default search returns source-separated blocks plus `Sources`; Exa deep modes add a labeled synthesis |
| Backend choice | Provider and routing are usually managed by the client | Exa or Tavily search, with a disclosed Firecrawl fallback |
| Difficult pages | One client-specific access path | Indexed content, guarded local browser rendering, Tavily Extract, and Firecrawl tiers |
| Retrieval intent | Usually search or page text | Full body, concise summary, or query-grounded page answer |
| Research discovery | General web index | Dedicated paper and developer-source tools in addition to web search |
| Freshness and filters | Whatever the client exposes | Date windows, hour-level recency, domains, categories, result limits, and forced freshness |
| Auditability | Client-specific | Fetch tier/cache/truncation disclosures; Tavily and Firecrawl search-fallback labels; explicit filter notices where supported |
| Operations | Client-defined; often hosted or opaque | Local stdio or self-hosted loopback HTTP, optional private UDS cache |
| Security policy | Client-specific | Initial URL validation plus guarded browser requests and redirects |

Use it as an **independent complementary retrieval lane** by default. If you want it
to replace Claude Code's built-in `WebSearch` and `WebFetch`, the package also includes
an optional, previewable hook installer.

## Quick start

Python 3.10 or newer is required.

### 1. Install

For the complete feature set in an isolated environment:

```bash
pipx install --include-deps 'web-retrieval-mcp[all]'
camoufox fetch
```

`--include-deps` exposes Camoufox's own command from the isolated pipx environment;
the second command downloads its managed browser. If you do not want local rendering,
install the base package or selected non-render extras and skip the browser download.

For a lean Exa + Firecrawl installation:

```bash
pipx install web-retrieval-mcp
```

With [uv](https://docs.astral.sh/uv/guides/tools/) installed, you can run the base
package in a temporary isolated environment without a permanent install:

```bash
uvx web-retrieval-mcp
```

Or install into an existing virtual environment:

```bash
python -m pip install 'web-retrieval-mcp[all]'
python -m camoufox fetch
```

The browser download is required whenever you install the `render` extra and want
Camoufox to serve `render="auto"` or `render="always"` calls.

Optional extras are composable:

| Extra | Adds |
|---|---|
| `tavily` | Tavily Search and Tavily Extract |
| `render` | Local Camoufox/Playwright rendering |
| `cache` | Valkey client for completed-result caching |
| `keyring` | Cross-platform native secret-store access |
| `all` | Every optional runtime capability above |

### 2. Add provider credentials

For the broadest routing coverage, configure Exa and Firecrawl; add Tavily when you
want provider-selectable search or another full-body fetch tier.

```bash
export EXA_API_KEY='<your-exa-api-key>'
export FIRECRAWL_API_KEY='<your-firecrawl-api-key>'
export TAVILY_API_KEY='<your-tavily-api-key>'   # optional
```

You do not need all three keys to start the server. Tools resolve credentials lazily
and report what a selected route is missing. See [Credential storage](#credential-storage)
for a key file and OS secret-store alternatives.

All three providers advertised no-card entry allocations when checked on 2026-09-01;
plans and unit costs can change, so check their live pricing before estimating a
workload:

| Provider | Entry allocation checked 2026-09-01 | Used for |
|---|---|---|
| [Exa](https://exa.ai/pricing?tab=api) | $20 signup credit plus $10 monthly API credits | Default web search and first indexed-content fetch tier |
| [Firecrawl](https://www.firecrawl.dev/pricing) | 1,000 credits per month | Search/fetch fallback and research/developer indexes |
| [Tavily](https://www.tavily.com/pricing) | 1,000 API credits per month | Optional search provider and full-body extract tier |
| [Camoufox](https://github.com/daijro/camoufox) | Local open-source browser | JavaScript-rendered page retrieval on your machine |

### 3. Connect an MCP client

Claude Code:

```bash
claude mcp add web-retrieval -- web-retrieval-mcp
```

With `uvx` and no prior install:

```bash
claude mcp add web-retrieval -- uvx web-retrieval-mcp
```

Claude Desktop, Cursor, and other clients that accept an `mcpServers` object can use:

```json
{
  "mcpServers": {
    "web-retrieval": {
      "command": "web-retrieval-mcp"
    }
  }
}
```

That configuration assumes the executable is on the client's `PATH`. For an `uvx`
launch, use `"command": "uvx"` and `"args": ["web-retrieval-mcp"]`.
If a desktop client does not inherit your shell environment, use the private key file
described below or place literal key values in the client's protected environment
configuration. Do not rely on `${VARIABLE}` interpolation unless your client documents
that behavior.

### 4. Give the agent a real task

Once the MCP server is connected, prompts can stay natural:

```text
Search the web for the latest primary documentation about Python package metadata.
Keep each source separate and preserve its URL.

Fetch https://example.com/report, force a fresh retrieval, and answer only:
What methodology did the authors use?

Find AI/ML papers about retrieval reranking, inspect the strongest paper for its
reported benchmark result, then find newer work that cites it.

Search merged pull requests and documentation for the origin of this exact error:
"transport closed".
```

## Six read-only tools

All tools carry MCP's read-only annotation. MCP clients receive the complete generated
input schema and descriptions when they connect.

| Tool | Compact signature | Best for |
|---|---|---|
| `web_search` | `web_search(query, num_results=8, mode="auto", …, provider=None)` | General web search through Exa or Tavily, with source-separated results and Firecrawl fallback |
| `web_fetch` | `web_fetch(url, render="auto", max_chars=None, max_age_hours=None, mode="full", question=None, tavily=None)` | Readable page bodies, concise summaries, or grounded answers through a tiered fetch cascade |
| `research_papers` | `research_papers(query, k=8)` | Ranked AI/ML paper discovery through Firecrawl's Research Index |
| `research_paper` | `research_paper(paper_id, query=None)` | Paper metadata and optional query-relevant full-text passages for claim verification |
| `research_similar` | `research_similar(paper_id, intent, k=8, mode="similar", …)` | Related papers, citers, or references guided by a natural-language intent |
| `research_github` | `research_github(query, k=8, passages=2, types=None, repos=None)` | Developer documentation, repository READMEs, issues, and merged pull requests |

`web_search` supports relevance or date-oriented filters, domain inclusion/exclusion,
publication windows, generated per-result summaries, and Exa's fast/deep search modes.
Tavily receives equivalent controls where its API supports them; approximations and
dropped controls are disclosed in the output.

The research-paper index is arXiv-oriented and best suited to AI/ML. For scholarly
work outside that scope, use `web_search(category="publication")`.

## How retrieval works

### Search routing

Exa is the default search provider. Select Tavily for one call with
`provider="tavily"`, or globally:

```bash
export WEB_SEARCH_PROVIDER=tavily
```

Only `exa` and `tavily` are accepted. If the selected provider fails, the server tries
Firecrawl and names both the failed primary provider and serving fallback in the
result.

### Fetch routing

For an ordinary full-body request in automatic mode:

```text
Exa indexed contents
        ↓ unusable or unavailable
guarded local Camoufox browser
        ↓ unusable or unavailable
optional Tavily Extract
        ↓ unusable or unavailable
Firecrawl
```

- `render="auto"` uses that adaptive cascade.
- `render="never"` forbids the local browser.
- `render="always"` starts with the local browser and skips Exa; optional Tavily and
  Firecrawl remain backstops.
- `mode="concise"` asks for a compact generated summary.
- `question="…"` asks for a grounded answer rather than the whole page.
- `max_age_hours=0` forces fresh provider work; `-1` requests Exa's always-use-cache
  behavior; positive values set a freshness window.
- Explicit full-body requests above Exa's body-size ceiling start with a tier capable
  of satisfying the requested size rather than silently returning a short body.

Enable Tavily Extract per call with `tavily=true`, or globally:

```bash
export WEB_FETCH_TAVILY_TIER=1
```

Tavily Extract is a full-body tier. The server skips it when it cannot honor a
concise, question-answer, or explicit freshness contract.

## Credential storage

The server resolves each API key lazily in this order:

1. `EXA_API_KEY`, `FIRECRAWL_API_KEY`, or `TAVILY_API_KEY` in the process environment.
2. A dotenv-style private key file.
3. The optional Python `keyring` package.
4. macOS Keychain or Linux Secret Service command-line clients.

The default key file is:

- Linux/macOS: `${XDG_CONFIG_HOME:-~/.config}/web-retrieval-mcp/keys.env`
- Windows: `%APPDATA%\web-retrieval-mcp\keys.env`

Override the file with `WEB_RETRIEVAL_MCP_ENV_FILE`, or its directory with
`WEB_RETRIEVAL_MCP_CONFIG_DIR`. On POSIX systems the file must not be readable by
group or other users:

```bash
install -d -m 700 ~/.config/web-retrieval-mcp
printf '%s\n' \
  'EXA_API_KEY=<your-exa-api-key>' \
  'FIRECRAWL_API_KEY=<your-firecrawl-api-key>' \
  'TAVILY_API_KEY=<your-tavily-api-key>' \
  > ~/.config/web-retrieval-mcp/keys.env
chmod 600 ~/.config/web-retrieval-mcp/keys.env
```

For `keyring`, store each secret under service `web-retrieval-mcp`, using the
environment-variable name as the username:

```bash
keyring set web-retrieval-mcp EXA_API_KEY
keyring set web-retrieval-mcp FIRECRAWL_API_KEY
keyring set web-retrieval-mcp TAVILY_API_KEY
```

Credentials remain in process memory, are redacted from provider errors and displayed
URLs, and are never put in provider command arguments.

## Configuration reference

| Setting | Default | Purpose |
|---|---|---|
| `EXA_API_KEY` | unset | Exa search and indexed page contents |
| `FIRECRAWL_API_KEY` | unset | Firecrawl fallback, research papers, and developer search |
| `TAVILY_API_KEY` | unset | Tavily Search and Extract; also requires the `tavily` extra |
| `WEB_SEARCH_PROVIDER` | `exa` | Default `web_search` provider: `exa` or `tavily` |
| `WEB_FETCH_TAVILY_TIER` | `false` | Globally enable Tavily Extract in eligible fetch cascades |
| `WEB_RETRIEVAL_MCP_ENV_FILE` | platform key-file path | Override the exact dotenv key file |
| `WEB_RETRIEVAL_MCP_CONFIG_DIR` | platform config directory | Override the directory containing `keys.env` |
| `WEB_RETRIEVAL_MCP_CACHE` | `auto` | Completed fetch cache: `auto`, `on`, or `off` |
| `WEB_RETRIEVAL_MCP_VALKEY_SOCKET` | unset | Absolute path to a private Valkey Unix-domain socket |
| `WEB_RETRIEVAL_MCP_HOST` | `127.0.0.1` | Streamable HTTP bind address |
| `WEB_RETRIEVAL_MCP_PORT` | `8100` | Streamable HTTP port |

Boolean settings accept `1/0`, `true/false`, `yes/no`, and `on/off`; invalid values
fail explicitly rather than being guessed.

### Optional completed-result cache

Install the `cache` extra and point the server at a private, non-persistent Valkey
Unix-domain socket:

```bash
export WEB_RETRIEVAL_MCP_CACHE=on
export WEB_RETRIEVAL_MCP_VALKEY_SOCKET=/absolute/private/path/valkey.sock
```

Successful eligible fetches are stored for 24 hours. Credential-bearing URLs,
userinfo, forced or positive freshness, and `render="always"` bypass replay. Cache
errors fail open to normal provider retrieval. Default `auto` enables caching only
when the Valkey client is installed on a non-Windows host; `on` still attempts the
configured Unix socket and fails open on errors, while `off` disables it. See [the cache guide](https://github.com/VelvetSP/web-retrieval-mcp/blob/main/docs/CACHE.md)
for the privacy model, singleflight behavior, and a minimal sidecar configuration.

### Streamable HTTP transport

stdio is the default and recommended transport for a local MCP client:

```bash
web-retrieval-mcp
# equivalent: python -m web_retrieval_mcp
```

For a shared local process, stateless Streamable HTTP is available:

```bash
web-retrieval-mcp --http --host 127.0.0.1 --port 8100
```

HTTP transport has no built-in authentication. Keep it on loopback unless you add an
authenticated perimeter and suitable network controls.

### Optional Claude Code replacement policy

The package includes a PreToolUse hook that can deny Claude Code's built-in
`WebSearch` and `WebFetch`, directing agents to this MCP server instead. Preview the
exact settings change first:

```bash
web-retrieval-mcp-install --print
```

Install or remove it explicitly:

```bash
web-retrieval-mcp-install
web-retrieval-mcp-install --register-mcp
web-retrieval-mcp-install --uninstall
```

The installer is idempotent and backs up an existing settings file before writing.
This hook is optional; the server works as a complementary tool without it.

## Install from source

To install the reviewed source directly:

```bash
git clone https://github.com/VelvetSP/web-retrieval-mcp.git
cd web-retrieval-mcp
python -m pip install '.[all]'
python -m camoufox fetch
```

For development:

```bash
python -m pip install -e '.[all,dev]'
./run-tests.sh
```

The release gate builds wheel and sdist artifacts, installs the wheel into a clean
virtual environment, exercises the installed MCP command against loopback provider
doubles, and runs the unit and transport suites. Browser or SSRF behavior changes also
require `python test_ssrf_redirect_live.py`. See [the testing contract](https://github.com/VelvetSP/web-retrieval-mcp/blob/main/docs/TESTING.md)
for the feature matrix and acceptance contract.

## Security model

- Only `http` and `https` initial URLs are accepted.
- Initial hosts must resolve exclusively to globally routable addresses.
- The local browser observes document requests and validates redirect hops before
  returning content to the caller.
- Credentials are resolved in process and redacted from errors and displayed URLs.
- Provider responses and page bodies are untrusted data; an agent must not treat
  instructions embedded in retrieved content as authority.

Application checks cannot prove that no packet reaches a private address during a DNS
rebinding race. Deployments with that threat model need a validating forward proxy or
equivalent network egress policy. See the [security policy](https://github.com/VelvetSP/web-retrieval-mcp/blob/main/SECURITY.md), and report suspected
vulnerabilities through GitHub's private vulnerability reporting rather than a public
issue.

## Frequently asked questions

### What is web-retrieval-mcp?

It is an open-source MCP server that gives AI agents reliable web search, tiered web
page retrieval, research-paper discovery, and developer-source search. It works over
local stdio with any compatible MCP client and can also serve stateless Streamable
HTTP.

### Is this an MCP web search server or an MCP web scraping server?

Both. `web_search` discovers and ranks pages; `web_fetch` extracts readable content
from one URL and can use a real local browser for JavaScript-heavy pages. The server
also exposes specialist paper and developer indexes that ordinary web scraping does
not provide.

### Does it replace an agent's built-in web search?

It can, but it does not have to. The tool descriptions recommend an independent
complementary lane because two retrieval systems can provide useful source diversity.
Claude Code users can opt into the bundled replacement hook when they want one enforced
route.

### Why use Exa, Firecrawl, Tavily, and Camoufox together?

They cover different failure modes. Exa provides indexed search and contents; Tavily
is an alternate search provider and optional extractor; Camoufox renders pages locally;
Firecrawl provides the final web fallback plus specialist research and developer
indexes. The server chooses among them according to the request and discloses the tier
that succeeded.

### Do I need every provider key?

No. Exa is the default for general search and first-tier contents. Firecrawl enables
fallbacks and all four research tools. Tavily is optional. The server lists all six
tools even when an optional route is not configured and returns an actionable error if
a call selects an unavailable capability.

### Is web-retrieval-mcp free?

The project is MIT-licensed. Provider usage is billed under your own accounts; each
provider currently offers an entry allocation, but quotas and prices can change.
Camoufox runs locally without a metered retrieval API.

### Which operating systems are supported?

The base Python server is platform-independent. Environment and key-file credentials
work across Linux, macOS, and Windows. Valkey UDS caching is non-Windows, and local
browser availability follows the supported Camoufox/Playwright platforms.

### Does it support RAG and autonomous research agents?

Yes. The source-separated result contract, explicit URLs, controllable text budgets,
grounded page questions, specialist indexes, and deterministic provenance make the
tools suitable as a retrieval layer for RAG pipelines and research agents. The server
returns evidence; the calling application remains responsible for evaluation,
citation, and prompt-injection handling.

## Project links

- [Python package on PyPI](https://pypi.org/project/web-retrieval-mcp/)
- [MCP Registry manifest](https://github.com/VelvetSP/web-retrieval-mcp/blob/main/server.json)
- [Documentation index](https://github.com/VelvetSP/web-retrieval-mcp/blob/main/docs/README.md)
- [Testing and acceptance contract](https://github.com/VelvetSP/web-retrieval-mcp/blob/main/docs/TESTING.md)
- [Security policy](https://github.com/VelvetSP/web-retrieval-mcp/blob/main/SECURITY.md)
- [Publishing runbook](https://github.com/VelvetSP/web-retrieval-mcp/blob/main/PUBLISHING.md)
- [Agent-readable project index](https://github.com/VelvetSP/web-retrieval-mcp/blob/main/llms.txt)
- [Issues and feature requests](https://github.com/VelvetSP/web-retrieval-mcp/issues)
- [Release notes](https://github.com/VelvetSP/web-retrieval-mcp/releases)

Contributions are welcome. Keep `stdout` reserved for JSON-RPC, send diagnostics to
`stderr`, add public-boundary acceptance coverage for changed behavior, and run
`./run-tests.sh` before opening a pull request.

## License

[MIT](https://github.com/VelvetSP/web-retrieval-mcp/blob/main/LICENSE) © VelvetSP
