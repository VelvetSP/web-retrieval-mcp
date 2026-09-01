"""Optional Claude Code registration and built-in web-tool hook installer."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MATCHER = "WebSearch|WebFetch"
HOOK_FILENAME = "deny-web-builtins.sh"
MCP_NAME = "web-retrieval"


def _hook_path() -> Path:
    return Path(__file__).resolve().parent / "hooks" / HOOK_FILENAME


def _default_settings() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return Path(base) / "settings.json"


def _hook_command(hook: Path) -> str:
    return f"sh {shlex.quote(str(hook))}"


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: cannot parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"error: {path} is not a JSON object")
    return data


def _write_settings(path: Path, settings: dict) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    mode = 0o600
    if path.exists():
        mode = path.stat().st_mode & 0o777
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
    payload = json.dumps(settings, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return backup


def _entry_hooks(entry: dict, settings_path: Path) -> list:
    value = entry.get("hooks", [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise SystemExit(
            f"error: matching hooks.PreToolUse entry has non-list 'hooks' "
            f"in {settings_path}"
        )
    return value


def _entry_has_hook(entry: dict, settings_path: Path | None = None) -> bool:
    if settings_path is None:
        value = entry.get("hooks", [])
        hooks = value if isinstance(value, list) else []
    else:
        hooks = _entry_hooks(entry, settings_path)
    return any(
        isinstance(hook, dict) and HOOK_FILENAME in str(hook.get("command", ""))
        for hook in hooks
    )


def _install(settings_path: Path, *, dry_run: bool) -> bool:
    hook = _hook_path()
    if not hook.exists():
        raise SystemExit(f"error: bundled hook missing at {hook}")
    settings = _load_settings(settings_path)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit(f"error: settings 'hooks' is not an object in {settings_path}")
    entries = hooks.setdefault("PreToolUse", [])
    if not isinstance(entries, list):
        raise SystemExit(f"error: hooks.PreToolUse is not a list in {settings_path}")
    target = next(
        (entry for entry in entries
         if isinstance(entry, dict) and entry.get("matcher") == MATCHER),
        None,
    )
    if target and _entry_has_hook(target, settings_path):
        print(f"already installed: PreToolUse {MATCHER!r} -> {HOOK_FILENAME}")
        return False
    if target is None:
        target = {"matcher": MATCHER, "hooks": []}
        entries.append(target)
    target_hooks = _entry_hooks(target, settings_path)
    target["hooks"] = target_hooks
    target_hooks.append({
        "type": "command",
        "command": _hook_command(hook),
    })
    if dry_run:
        print(json.dumps(settings, indent=2))
        return False
    backup = _write_settings(settings_path, settings)
    if backup:
        print(f"backup: {backup}")
    print(f"installed hook in {settings_path}")
    return True


def _uninstall(settings_path: Path, *, dry_run: bool) -> bool:
    settings = _load_settings(settings_path)
    hooks = settings.get("hooks")
    entries = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    if not isinstance(entries, list):
        print("nothing to uninstall")
        return False
    changed = False
    updated: list = []
    for entry in entries:
        if not isinstance(entry, dict):
            updated.append(entry)
            continue
        present = entry.get("hooks")
        if not isinstance(present, list):
            updated.append(entry)
            continue
        kept = [
            hook for hook in present
            if not (isinstance(hook, dict)
                    and HOOK_FILENAME in str(hook.get("command", "")))
        ]
        if len(kept) == len(present):
            updated.append(entry)
        else:
            changed = True
        if kept and len(kept) != len(present):
            copied = dict(entry)
            copied["hooks"] = kept
            updated.append(copied)
    if not changed:
        print("nothing to uninstall")
        return False
    hooks["PreToolUse"] = updated
    if dry_run:
        print(json.dumps(settings, indent=2))
        return False
    backup = _write_settings(settings_path, settings)
    if backup:
        print(f"backup: {backup}")
    print(f"removed hook from {settings_path}")
    return True


def _register_mcp(*, run: bool) -> None:
    command = ["claude", "mcp", "add", MCP_NAME, "--", "web-retrieval-mcp"]
    rendered = " ".join(shlex.quote(part) for part in command)
    if not run or shutil.which("claude") is None:
        print(f"Claude Code registration command:\n  {rendered}")
        return
    completed = subprocess.run(command, check=False)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="web-retrieval-mcp-install")
    parser.add_argument("--settings", type=Path, default=_default_settings())
    parser.add_argument("--print", dest="dry_run", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--register-mcp", action="store_true")
    args = parser.parse_args(argv)
    if args.uninstall:
        _uninstall(args.settings, dry_run=args.dry_run)
    else:
        _install(args.settings, dry_run=args.dry_run)
        _register_mcp(run=args.register_mcp and not args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
