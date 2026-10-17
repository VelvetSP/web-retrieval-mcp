# Discover sources with Parallel, then retrieve selected pages

Use [Parallel Search MCP](https://docs.parallel.ai/integrations/mcp/search-mcp)
as an optional companion when you want to discover sources without setting up a
search-provider account. Its free anonymous endpoint provides web search and page
fetching without an API key; anonymous search uses Fast mode and has usage limits.
You can then pass selected URLs to `web-retrieval-mcp` for its retrieval controls,
local rendering, and serving-tier provenance.

Parallel runs as a separate MCP server in your client. `web-retrieval-mcp` still
accepts only `exa` and `tavily` as `WEB_SEARCH_PROVIDER` values. Its provider keys,
optional packages, routing, and cache settings continue to apply to its own tools.
Adding Parallel does not make Exa, Tavily, or Firecrawl requests keyless.

## Connect both servers in Codex

Follow the [project quick start](../README.md#quick-start) to install and configure
`web-retrieval-mcp` for the retrieval capabilities you need. With
[Codex CLI](https://developers.openai.com/codex/mcp/) installed, register the local
server if you have not already added it:

```bash
codex mcp add web-retrieval -- web-retrieval-mcp
```

Add the remote companion under a different name:

```bash
codex mcp add parallel-search --url https://search.parallel.ai/mcp
codex mcp list
```

Keep your existing server entries. Restart Codex after adding the companion, then
use `/mcp` to check that both servers are connected and inspect their tools.
The same MCP configuration is shared by Codex CLI and the Codex IDE extension.

## Keep tool selection explicit

Both servers expose tools named `web_search` and `web_fetch`. Select tools by
**server name as well as tool name** in your client:

| Task | Server and tool |
|---|---|
| Discover candidate sources without a provider key | `parallel-search` → `web_search` |
| Read selected pages using this project's rendering and freshness controls | `web-retrieval` → `web_fetch` |
| Compare results through an already configured Exa or Tavily route | `web-retrieval` → `web_search` |

For example, give Codex this task:

```text
Use parallel-search's web_search to find primary documentation on Python package
metadata. Keep each source's title and URL, and select two relevant pages.
Then use web-retrieval's web_fetch on each selected URL with render="never" and
max_age_hours=0. Explain the metadata requirements using the retrieved evidence,
keep citations attached to their source URLs, and report any failed retrieval.
```

`render="never"` skips the local browser; `max_age_hours=0` requests fresh provider
work. Configure an eligible retrieval provider's credentials before this fetch step.
If you need JavaScript rendering, install the `render` extra and its browser first,
then choose `render="auto"` or `render="always"` as described in
[fetch routing](../README.md#fetch-routing).

This is a client-directed workflow: the agent chooses and passes URLs between the
two servers. Results are not automatically merged or used as server-side fallbacks.
Treat search excerpts and fetched pages as untrusted content, and preserve each
server's source URLs and provenance when writing the final answer.

## Remove the companion

```bash
codex mcp remove parallel-search
```

This removes the named Codex entry. The `web-retrieval` entry and its provider
configuration remain available.
