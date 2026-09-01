#!/bin/sh
# Optional Claude Code PreToolUse hook installed by web-retrieval-mcp-install.

config_dir=${CLAUDE_CONFIG_DIR:-"$HOME/.claude"}
[ -f "$config_dir/.web-builtins-allow" ] && exit 0

cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Use the web-retrieval MCP tools: mcp__web-retrieval__web_search and mcp__web-retrieval__web_fetch. Create .web-builtins-allow in the Claude config directory to temporarily allow the built-ins."}}
JSON
