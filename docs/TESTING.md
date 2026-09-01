# Testing contract

## Product oracle

Expected behavior comes from, in order:

1. The current explicit product requirement.
2. The public contract in `README.md` and the MCP tool descriptions.
3. Current provider documentation and protocol behavior.

Code and existing tests are evidence, not the definition of expected behavior.

## Feature matrix

| ID | Declared behavior | Public-boundary evidence | Status |
|---|---|---|---|
| WR-INSTALL-001 | Wheel installs in a clean environment; base install starts and lists six tools without optional dependencies | `tests/acceptance_public_install.py` | covered |
| WR-DOCS-001 | Built artifacts expose searchable package/registry metadata, a PyPI-renderable README with portable links and client-neutral onboarding, and an agent-readable discovery index | `tests/acceptance_public_metadata.py` | covered |
| WR-CREDS-001 | Exa, Firecrawl, and Tavily credentials resolve from documented sources without entering command arguments or errors | Installed MCP with loopback provider doubles; resolver unit cases | covered |
| WR-SEARCH-001 | Exa search preserves result provenance and uses Firecrawl on provider failure | Installed MCP Exa/Firecrawl calls plus `test_search_render.py` | covered |
| WR-TAVILY-001 | Tavily is a strict selectable search provider; filter approximations are disclosed | Installed MCP Tavily SDK call plus `test_tavily.py` | covered |
| WR-FETCH-001 | Full-body auto keeps Firecrawl last, with local Camoufox before optional Tavily | MCP routing acceptance plus `test_fetch_cascade.py` and `test_tavily.py` | covered |
| WR-FETCH-002 | `never`, `always`, semantic extraction, freshness, truncation, and provenance retain their contracts | `test_fetch_policy_acceptance.py` and focused suites | covered |
| WR-TAVILY-002 | Tavily Extract is opt-in, strict, cache-distinct, and skipped when it cannot honor semantic/freshness contracts | Installed MCP SDK call plus `test_tavily.py` | covered |
| WR-CACHE-001 | Exact eligible plans replay; privacy bypasses, corruption, outage, TTL, and concurrency preserve retrieval behavior | `test_fetch_policy_acceptance.py` and `test_fetch_cache.py` | covered |
| WR-RESEARCH-001 | Research paper and developer-index tools preserve provider envelopes and fallback provenance | Internal mocked provider coverage only (`test_research_index.py`) | partial |
| WR-SSRF-001 | Initial private targets, changed DNS, redirect hops, and private subresources fail closed | MCP acceptance, unit guard, and live browser suite | covered |

The research tools still lack an independent installed-MCP acceptance oracle. Keep that
row marked partial until one is added.

## Test layers

- `./run-tests.sh` is the release gate. It runs focused suites, builds wheel and sdist,
  renders the packaged README through PyPI's GFM renderer, validates public discovery
  metadata, installs the wheel into a clean virtual environment, and exercises the
  installed MCP command against loopback provider doubles.
- `test_fetch_policy_acceptance.py` starts the real HTTP MCP process, a real isolated
  Valkey process, loopback Exa/Firecrawl doubles, and the installed local browser.
- `test_ssrf_redirect_live.py` is required when browser rendering or SSRF behavior
  changes. It uses real public pages and is kept outside ordinary offline unit tests.
- Provider `--live` modes are opt-in compatibility probes, not deterministic release
  acceptance.

Acceptance-only provider and DNS overrides require `WEBRET_ACCEPTANCE_LAB=1`, loopback
HTTP endpoints, and literal IP sequences. They cannot redirect a real key to an arbitrary
host. The harness uses temporary ports, sockets, directories, dummy credentials, and
non-persistent provider doubles.

## Release gate

```bash
./run-tests.sh
python test_ssrf_redirect_live.py   # additionally for browser/SSRF changes
```
