"""Public settings-installer behavior, using only temporary files."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from web_retrieval_mcp import install


class InstallerTests(unittest.TestCase):
    def test_install_is_idempotent_preserves_settings_and_writes_backup(self):
        with tempfile.TemporaryDirectory(prefix="web-retrieval-installer-") as root:
            settings = Path(root) / "settings.json"
            original = {"model": "example", "hooks": {"PreToolUse": [{
                "matcher": "OtherTool",
                "hooks": [{"type": "command", "command": "other-hook"}],
            }]}}
            settings.write_text(json.dumps(original), encoding="utf-8")

            self.assertTrue(install._install(settings, dry_run=False))
            updated = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(updated["model"], "example")
            self.assertEqual(
                updated["hooks"]["PreToolUse"][0],
                original["hooks"]["PreToolUse"][0],
            )
            matches = [
                entry for entry in updated["hooks"]["PreToolUse"]
                if entry.get("matcher") == install.MATCHER
            ]
            self.assertEqual(len(matches), 1)
            self.assertTrue(install._entry_has_hook(matches[0]))
            self.assertEqual(
                json.loads(settings.with_suffix(".json.bak").read_text(encoding="utf-8")),
                original,
            )

            self.assertFalse(install._install(settings, dry_run=False))
            self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), updated)

    def test_uninstall_removes_only_bundled_hook(self):
        with tempfile.TemporaryDirectory(prefix="web-retrieval-installer-") as root:
            settings = Path(root) / "settings.json"
            self.assertTrue(install._install(settings, dry_run=False))
            payload = json.loads(settings.read_text(encoding="utf-8"))
            payload["hooks"]["PreToolUse"].append({
                "matcher": "OtherTool",
                "hooks": [{"type": "command", "command": "keep-me"}],
            })
            settings.write_text(json.dumps(payload), encoding="utf-8")

            self.assertTrue(install._uninstall(settings, dry_run=False))
            updated = json.loads(settings.read_text(encoding="utf-8"))
            rendered = json.dumps(updated)
            self.assertNotIn(install.HOOK_FILENAME, rendered)
            self.assertIn("keep-me", rendered)

    def test_preview_does_not_create_settings(self):
        with tempfile.TemporaryDirectory(prefix="web-retrieval-installer-") as root:
            settings = Path(root) / "settings.json"
            self.assertFalse(install._install(settings, dry_run=True))
            self.assertFalse(settings.exists())

    def test_install_rejects_malformed_matching_hook_list(self):
        with tempfile.TemporaryDirectory(prefix="web-retrieval-installer-") as root:
            settings = Path(root) / "settings.json"
            original = {
                "hooks": {
                    "PreToolUse": [{"matcher": install.MATCHER, "hooks": "bad"}],
                },
            }
            settings.write_text(json.dumps(original), encoding="utf-8")

            with self.assertRaisesRegex(SystemExit, "non-list 'hooks'"):
                install._install(settings, dry_run=False)
            self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), original)

    def test_uninstall_preserves_unrelated_empty_and_malformed_entries(self):
        with tempfile.TemporaryDirectory(prefix="web-retrieval-installer-") as root:
            settings = Path(root) / "settings.json"
            payload = {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Empty", "hooks": []},
                        {"matcher": "Malformed", "hooks": "leave-me"},
                        {
                            "matcher": install.MATCHER,
                            "hooks": [{
                                "type": "command",
                                "command": f"sh /tmp/{install.HOOK_FILENAME}",
                            }],
                        },
                    ],
                },
            }
            settings.write_text(json.dumps(payload), encoding="utf-8")

            self.assertTrue(install._uninstall(settings, dry_run=False))
            entries = json.loads(settings.read_text(encoding="utf-8"))["hooks"][
                "PreToolUse"
            ]
            self.assertEqual(
                entries,
                [
                    {"matcher": "Empty", "hooks": []},
                    {"matcher": "Malformed", "hooks": "leave-me"},
                ],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
