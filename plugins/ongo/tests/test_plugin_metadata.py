#!/usr/bin/env python3
"""Cross-host plugin packaging and skill-layout tests."""

from __future__ import annotations

import json
import os
import re
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PLUGIN_ROOT.parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "lib"))

from ongo import __version__


class PluginMetadataTests(unittest.TestCase):
    def load_json(self, relative_path):
        return json.loads((REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8"))

    def test_cross_host_versions_are_synchronized(self):
        claude = json.loads(
            (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        codex = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        marketplace = self.load_json(".claude-plugin/marketplace.json")
        self.assertEqual(claude["version"], __version__)
        self.assertEqual(codex["version"], __version__)
        self.assertEqual(marketplace["plugins"][0]["version"], __version__)

    def test_codex_marketplace_points_to_the_shared_plugin(self):
        marketplace = self.load_json(".agents/plugins/marketplace.json")
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], "ongo")
        self.assertEqual(entry["source"], {"source": "local", "path": "./plugins/ongo"})
        self.assertEqual(entry["policy"]["installation"], "AVAILABLE")
        self.assertEqual(entry["policy"]["authentication"], "ON_INSTALL")

    def test_shared_skill_has_only_portable_frontmatter_and_live_references(self):
        skill = PLUGIN_ROOT / "skills" / "ongo" / "SKILL.md"
        text = skill.read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)[1]
        keys = {
            line.split(":", 1)[0]
            for line in frontmatter.splitlines()
            if ":" in line
        }
        self.assertEqual(keys, {"name", "description"})
        self.assertNotIn("!`", text)
        for relative in re.findall(r"\]\((references/[^)]+)\)", text):
            self.assertTrue((skill.parent / relative).is_file(), relative)

    def test_runtime_wrapper_reaches_shared_cli(self):
        wrapper = PLUGIN_ROOT / "skills" / "ongo" / "scripts" / "ongo"
        result = subprocess.run(
            [str(wrapper), "version"], capture_output=True, text=True, check=True
        )
        self.assertEqual(result.stdout.strip(), __version__)

    def test_runtime_wrapper_resolves_symlink_before_finding_plugin(self):
        wrapper = PLUGIN_ROOT / "skills" / "ongo" / "scripts" / "ongo"
        with tempfile.TemporaryDirectory() as temporary:
            link = Path(temporary) / "ongo"
            link.symlink_to(wrapper)
            result = subprocess.run(
                [str(link), "version"], capture_output=True, text=True, check=True
            )
        self.assertEqual(result.stdout.strip(), __version__)

    def test_entrypoint_preserves_claude_data_when_both_hosts_are_present(self):
        entrypoint = runpy.run_path(str(PLUGIN_ROOT / "bin" / "ongo"))
        with mock.patch.dict(
            os.environ,
            {
                "PLUGIN_DATA": "/tmp/codex-ongo-data",
                "CLAUDE_PLUGIN_DATA": "/tmp/claude-ongo-data",
            },
            clear=True,
        ):
            self.assertEqual(
                entrypoint["plugin_data_dir"](), "/tmp/claude-ongo-data"
            )

    def test_entrypoint_uses_codex_data_and_ignores_whitespace(self):
        entrypoint = runpy.run_path(str(PLUGIN_ROOT / "bin" / "ongo"))
        with mock.patch.dict(
            os.environ,
            {
                "CLAUDE_PLUGIN_DATA": "  ",
                "PLUGIN_DATA": "/tmp/codex-ongo-data",
            },
            clear=True,
        ):
            self.assertEqual(entrypoint["plugin_data_dir"](), "/tmp/codex-ongo-data")

    def test_memory_probe_is_portable_and_machine_readable(self):
        probe = PLUGIN_ROOT / "skills" / "ongo" / "scripts" / "available-memory-mib"
        result = subprocess.run(
            [str(probe)], capture_output=True, text=True, check=True
        )
        output = result.stdout.strip()
        self.assertTrue(output == "unknown" or output.isdigit(), output)

    def test_host_adapters_keep_scheduler_protocols_separate(self):
        references = PLUGIN_ROOT / "skills" / "ongo" / "references"
        claude = references.joinpath("claude-code.md").read_text(encoding="utf-8")
        codex = references.joinpath("codex.md").read_text(encoding="utf-8")
        self.assertIn("CronCreate", claude)
        self.assertIn("create-before-delete", claude)
        self.assertIn("heartbeat automation", codex)
        self.assertIn("update the existing heartbeat", codex)
        self.assertNotIn("CronCreate", codex)


if __name__ == "__main__":
    unittest.main()
