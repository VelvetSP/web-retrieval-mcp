#!/bin/sh
# deny-web-builtins.sh — Claude Code PreToolUse hook (matcher: "WebSearch|WebFetch").
#
# Hard-denies Claude Code's built-in WebSearch/WebFetch for every agent and
# subagent, redirecting them to this MCP server's higher-fidelity tools
# (mcp__web-retrieval__web_search / mcp__web-retrieval__web_fetch). A PreToolUse
# "deny" fires on subagent-issued calls too, so coverage is total.
#
# Why: the built-ins return source-conflating snippets; these tools keep every
# result's own provenance and run a tiered fetch (Exa -> optional local browser
# -> Firecrawl). See the project README.
#
# Break-glass: `touch ~/.claude/.web-builtins-allow` re-enables the built-ins for
# the rest of the session (the file is stat-ed per call); remove it to re-arm.
#
# Pure POSIX sh, no jq dependency — the deny payload is a fixed JSON literal.

[ -f "$HOME/.claude/.web-builtins-allow" ] && exit 0

cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Built-in web tools are disabled on this machine. Use the web-retrieval MCP tools instead: mcp__web-retrieval__web_search (search) and mcp__web-retrieval__web_fetch (fetch a URL; pass render=\"always\" for a JS/anti-bot page). If they are not in your tool list, load them first: ToolSearch select:mcp__web-retrieval__web_search,mcp__web-retrieval__web_fetch"}}
JSON
exit 0
