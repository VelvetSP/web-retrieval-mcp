# web-retrieval-mcp

A small [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that
gives an LLM agent two high-fidelity web tools:

| Tool | What it does | Backed by |
|---|---|---|
| `web_search(query, num_results, mode)` | Web search. Returns **one block per result** — each with its own title, URL, highlights and text, never a merged summary — plus a `Sources` trailer. | [Exa](https://exa.ai) `/search` (neural / keyword / auto) |
| `web_fetch(url, render, max_chars, max_age_hours)` | Fetch one URL's readable content through a tiered chain, with a `[served by: …]` provenance header. | Exa `/contents` → *(optional local browser)* → [Firecrawl](https://firecrawl.dev) |

It's meant as a drop-in replacement for an agent's built-in web tools, which often
return source-conflating snippets. Each search result keeps its own provenance, and
fetches report which tier served them.

## How `web_fetch` tiering works

```
web_fetch(url, render="auto")   →  Exa /contents  →  Firecrawl          (default)
web_fetch(url, render="never")  →  Exa /contents  →  Firecrawl          (same; no browser)
web_fetch(url, render="always") →  camoufox (local headless browser)  →  Firecrawl
```

The local [camoufox](https://github.com/daijro/camoufox) browser render is **opt-in
(`render="always"`) only**. It is the one tier that runs a real browser on the host
machine and can therefore reach the local network, so it is deliberately kept out of
the default path to shrink [SSRF](https://owasp.org/www-community/attacks/Server_Side_Request_Forgery)
exposure. A `_validate_public_url` guard rejects non-public / loopback / link-local /
multicast hosts up front, so internal URLs never reach the external APIs either.

> **SSRF residual:** the camoufox tier follows redirects and re-resolves DNS, so the
> up-front host check covers the *initial* URL only, not post-redirect hops. The
> default path (`auto`/`never`) never runs the browser, so this only matters if you
> explicitly pass `render="always"` with an untrusted, hostile-redirecting URL. Full
> closure would need a validating forward proxy.

## Requirements

- Python 3.10+
- An [Exa](https://exa.ai) API key and a [Firecrawl](https://firecrawl.dev) API key
- Core deps: `mcp`, `anyio`
- **Optional** (only for `render="always"`): `camoufox`, `playwright` — a ~hundreds-of-MB
  browser stack. Search and the default fetch path work **without** them.

```bash
pip install -r requirements.txt          # core: search + Exa/Firecrawl fetch
pip install -r requirements-render.txt   # optional: local browser render
python -m camoufox fetch                 # one-time: download the camoufox browser
```

## Configuration — API keys

Keys are resolved **in-process** (never on a command line, which is visible via `ps`):

1. **Environment variables** (recommended, and required for headless/cron use):
   - `EXA_API_KEY`
   - `FIRECRAWL_API_KEY`
2. **macOS login Keychain** fallback (interactive use), under generic-password
   service names `EXA_API_KEY` and `FIRECRAWL_API_KEY`:
   ```bash
   security add-generic-password -s EXA_API_KEY       -a "$USER" -w 'your-exa-key'
   security add-generic-password -s FIRECRAWL_API_KEY -a "$USER" -w 'your-firecrawl-key'
   ```

An unexpanded `${...}` config literal is treated as absent. For headless / scheduled
runs the login Keychain may be locked — supply keys via env there.

## Register with an MCP client

The server speaks MCP over stdio. Point your client (e.g. Claude Code, Claude Desktop)
at `server.py` and pass the keys via `env`. Example (replace the placeholder path with
wherever you cloned this repo):

```json
{
  "mcpServers": {
    "web-retrieval": {
      "command": "python3",
      "args": ["/path/to/web-retrieval-mcp/server.py"],
      "env": {
        "EXA_API_KEY": "your-exa-key",
        "FIRECRAWL_API_KEY": "your-firecrawl-key"
      }
    }
  }
}
```

For Claude Code you can also register it from the CLI:

```bash
claude mcp add web-retrieval -- python3 /path/to/web-retrieval-mcp/server.py
```

(then set `EXA_API_KEY` / `FIRECRAWL_API_KEY` in your environment or Keychain).

## Run / smoke-test directly

```bash
EXA_API_KEY=... FIRECRAWL_API_KEY=... python3 server.py   # serves MCP over stdio
```

## Design constraints (for contributors)

- **stdout is JSON-RPC only.** Tools return strings; nothing prints to stdout — a
  stray print corrupts the protocol. All diagnostics go to stderr.
- Blocking I/O (Keychain subprocess + `urllib` POSTs) runs in a worker thread via
  `anyio.to_thread.run_sync`, so it never stalls the event loop under concurrent
  calls. Each tier is wrapped in `asyncio.wait_for`.
- The camoufox render is bounded by a semaphore + timeout (one browser per fetch)
  and renders in-process via the native `AsyncCamoufox` API. Its imports are local,
  so the browser stack is required only for `render="always"`.
- Errors are caught and returned as `SEARCH_FAILED:` / `RETRIEVAL_FAILED:` strings —
  a tool failure can't kill the server. Secrets are scrubbed from any error text.

## License

[MIT](./LICENSE)
