# Optional completed-result cache

`web_fetch` has a process-local exact-plan singleflight and an optional 24-hour
completed-result cache backed by Valkey over a Unix-domain socket. The cache is a
performance and provider-cost optimization, never an availability dependency. Missing,
slow, corrupt, or full cache state fails open to the normal retrieval cascade.

`web_search` and the research tools are not cached.

## Enable it

Install the client extra and run a private Valkey instance with persistence disabled:

```bash
python -m pip install 'web-retrieval-mcp[cache]'
export WEB_RETRIEVAL_MCP_CACHE=on
export WEB_RETRIEVAL_MCP_VALKEY_SOCKET=/absolute/private/path/valkey.sock
```

Minimal Valkey settings:

```text
port 0
protected-mode yes
unixsocket /absolute/private/path/valkey.sock
unixsocketperm 600
save ""
appendonly no
maxmemory 512mb
maxmemory-policy allkeys-lfu
databases 1
```

The socket's parent directory should be mode `0700`. Choose memory limits for your
host. `WEB_RETRIEVAL_MCP_CACHE=auto` (the default) enables caching only when the Valkey
Python client is installed on a non-Windows system; `off` disables it explicitly.

## Identity and eligibility

Keys are `wr:fetch:v1:<sha256>`. The digest covers the effective fetch plan, including
URL identity, render/mode, a hash of a non-empty question, character limit, routing
class, freshness class, and Tavily-tier state. Raw URLs and questions never appear in
keys or diagnostics.

Only decoded, case-insensitive `utm_*` query tokens are removed from cache identity.
Providers still receive the caller's original URL. All other query spelling, ordering,
duplicates, blank values, path spelling, and fragments remain distinct.

Completed replay is bypassed for:

- `render="always"`;
- forced or positive freshness;
- URLs containing userinfo;
- recognized credential/signature names in queries or query-shaped fragments;
- failures and unusably thin intermediate results.

Every request is URL-validated before cache access. A hit is validated again before
its body is returned, so a host that now resolves privately cannot expose a planted
cached body.

## Values and privacy

Values are bounded zlib-compressed JSON success envelopes. They include the fetched
body, result kind, serving tier, provider cache state, limits, and local storage time.
They do not include a separate URL or question field. Both compressed and uncompressed
forms are capped at 16 MiB; corrupt or oversized entries are treated as misses.

Successful writes use a 86,400-second TTL. Valkey persistence should remain disabled;
entries may disappear on sidecar or host restart. A replay preserves original provider
provenance and adds a separate local-cache age line.

## Singleflight

Concurrent exact-plan calls in one MCP process share one producer. One cancelled HTTP
waiter does not cancel work needed by other waiters. If all waiters leave, the producer
becomes non-cacheable and is cancelled before advancing to another paid tier.

Run a single HTTP worker. Multiple workers require distributed miss coalescing, which
this implementation intentionally does not provide.
