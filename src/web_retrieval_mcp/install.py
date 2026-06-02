"""Installer for the Claude Code "deny built-in web tools" hook.

`web-retrieval-mcp-install` wires the bundled PreToolUse hook into your Claude
Code settings so the built-in WebSearch / WebFetch are denied and agents use
this server's mcp__web-retrieval__* tools instead. It is idempotent and backs up
settings.json before writing. Run with --print to preview without changing
anything, or --uninstall to remove the hook.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

MATCHER = "WebSearch|WebFetch"
HOOK_FILENAME = "deny-web-builtins.sh"
MCP_NAME = "web-retrieval"


def _hook_path() -> Path:
    """Absolute path to the hook shipped inside this installed package."""
    return Path(__file__).resolve().parent / "hooks" / HOOK_FILENAME


def _default_settings() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return Path(base) / "settings.json"


def _hook_command(hook: Path) -> str:
    # `sh <path>` so an exec bit isn't required and Windows (Git Bash/WSL) works.
    return f"sh {shlex.quote(str(hook))}"


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"error: cannot parse {path}: {e}")
    if not isinstance(data, dict):
        raise SystemExit(f"error: {path} is not a JSON object")
    return data


def _entry_has_hook(entry: dict) -> bool:
    for h in entry.get("hooks", []) or []:
        if isinstance(h, dict) and HOOK_FILENAME in str(h.get("command", "")):
            return True
    return False


def _install(settings_path: Path, *, dry_run: bool) -> bool:
    hook = _hook_path()
    if not hook.exists():
        raise SystemExit(f"error: bundled hook missing at {hook}")
    command = _hook_command(hook)
    settings = _load_settings(settings_path)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit(f"error: settings 'hooks' is not an object in {settings_path}")
    pre = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        raise SystemExit(f"error: settings hooks.PreToolUse is not a list in {settings_path}")

    target = next((e for e in pre if isinstance(e, dict) and e.get("matcher") == MATCHER), None)
    if target and _entry_has_hook(target):
        print(f"already installed: PreToolUse '{MATCHER}' -> {HOOK_FILENAME}")
        return False
    if target is None:
        target = {"matcher": MATCHER, "hooks": []}
        pre.append(target)
    target.setdefault("hooks", []).append({"type": "command", "command": command})

    if dry_run:
        print(f"[--print] would write to {settings_path}:")
        print(json.dumps({"hooks": {"PreToolUse": pre}}, indent=2))
        return False

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        backup = settings_path.with_suffix(settings_path.suffix + ".bak")
        shutil.copy2(settings_path, backup)
        print(f"backed up {settings_path} -> {backup}")
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"installed: PreToolUse '{MATCHER}' -> {command}")
    print(f"  in {settings_path}")
    return True


def _uninstall(settings_path: Path, *, dry_run: bool) -> bool:
    settings = _load_settings(settings_path)
    pre = (settings.get("hooks") or {}).get("PreToolUse")
    if not isinstance(pre, list):
        print("nothing to uninstall")
        return False
    changed = False
    for entry in pre:
        if not isinstance(entry, dict):
            continue
        kept = [h for h in entry.get("hooks", []) or []
                if not (isinstance(h, dict) and HOOK_FILENAME in str(h.get("command", "")))]
        if len(kept) != len(entry.get("hooks", []) or []):
            entry["hooks"] = kept
            changed = True
    # drop now-empty matcher entries we may have emptied
    settings["hooks"]["PreToolUse"] = [e for e in pre if (e.get("hooks") if isinstance(e, dict) else True)]
    if not changed:
        print("nothing to uninstall")
        return False
    if dry_run:
        print(f"[--print] would remove the {HOOK_FILENAME} hook from {settings_path}")
        return False
    backup = settings_path.with_suffix(settings_path.suffix + ".bak")
    shutil.copy2(settings_path, backup)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"uninstalled {HOOK_FILENAME} (backup: {backup})")
    return True


def _register_mcp(*, run: bool) -> None:
    cmd = ["claude", "mcp", "add", MCP_NAME, "--", "web-retrieval-mcp"]
    pretty = " ".join(shlex.quote(c) for c in cmd)
    if not run:
        print("\nRegister the MCP server with Claude Code:")
        print(f"  {pretty}")
        print("  (then export EXA_API_KEY and FIRECRAWL_API_KEY, or store them via keyring / a key file)")
        return
    if shutil.which("claude") is None:
        print("\n'claude' CLI not on PATH — register manually:")
        print(f"  {pretty}")
        return
    print(f"\nrunning: {pretty}")
    subprocess.run(cmd, check=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="web-retrieval-mcp-install",
        description="Install the Claude Code hook that denies built-in WebSearch/WebFetch "
                    "and routes agents to the web-retrieval MCP server.")
    ap.add_argument("--settings", type=Path, default=_default_settings(),
                    help="path to Claude Code settings.json (default: ~/.claude/settings.json)")
    ap.add_argument("--print", dest="dry_run", action="store_true",
                    help="preview changes without writing")
    ap.add_argument("--uninstall", action="store_true", help="remove the hook")
    ap.add_argument("--register-mcp", action="store_true",
                    help="also run `claude mcp add` to register the server")
    args = ap.parse_args(argv)

    if args.uninstall:
        _uninstall(args.settings, dry_run=args.dry_run)
        return 0

    print(f"hook: {_hook_path()}")
    _install(args.settings, dry_run=args.dry_run)
    _register_mcp(run=args.register_mcp and not args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
