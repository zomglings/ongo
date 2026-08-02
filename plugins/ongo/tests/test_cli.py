#!/usr/bin/env python3
"""Public CLI surface tests."""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
ONGO = PLUGIN_ROOT / "bin" / "ongo"


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([str(ONGO), *args], capture_output=True, text=True)

    def test_root_help_lists_plugin_commands(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("experiment", result.stdout)
        self.assertIn("site build", result.stdout)

    def test_every_public_subcommand_has_help(self):
        commands = (
            ("setup",),
            ("doctor",),
            ("slack", "poll"),
            ("arxiv", "sweep"),
            ("site", "build"),
            ("site", "serve"),
            ("ken", "delete"),
            ("experiment",),
            ("experiment", "create"),
            ("experiment", "show"),
            ("experiment", "render"),
            ("experiment", "status"),
            ("experiment", "delegate", "create"),
            ("experiment", "approve"),
            ("experiment", "begin"),
            ("experiment", "finish"),
            ("experiment", "cancel"),
            ("experiment", "retry"),
            ("experiment", "run"),
            ("experiment", "verify"),
        )
        for command in commands:
            with self.subTest(command=command):
                result = self.run_cli(*command, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout.lower())

    def test_unknown_command_is_structured(self):
        result = self.run_cli("does-not-exist")
        self.assertEqual(result.returncode, 2)
        self.assertIn('"code": "invalid-command"', result.stderr)

    def test_invalid_helper_input_is_structured(self):
        result = self.run_cli("slack", "poll")
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "invalid-input")

    def test_old_entry_points_are_absent(self):
        old = (
            "ongo-poll",
            "ongo-arxiv-daily",
            "ongo-site",
            "ongo-serve",
            "ongo-delete",
        )
        for name in old:
            self.assertFalse((PLUGIN_ROOT / "bin" / name).exists())
            self.assertFalse((PLUGIN_ROOT / "skills" / "ongo" / "bin" / name).exists())

    def test_public_cli_creates_and_approves_zero_cost_experiment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "ken.db"
            subprocess.run(["ken", "-D", str(database), "init"], check=True, capture_output=True)
            plan = root / "plan.md"
            manifest = root / "manifest.json"
            plan.write_text("# CLI protocol\n", encoding="utf-8")
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "title": "CLI protocol",
                        "conditions": [
                            {
                                "id": "one",
                                "description": "One manual observation",
                                "required_runs": 1,
                                "expected_cost_usd": "0",
                                "required_artifacts": [],
                                "execution": {"mode": "manual"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["ONGO_KEN"] = shutil.which("ken") or "ken"
            environment["ONGO_KEN_DB"] = str(database)
            created = subprocess.run(
                [str(ONGO), "experiment", "create", "--document", str(plan), "--manifest", str(manifest)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            experiment_id = json.loads(created.stdout)["experiment"]["experiment_id"]
            approved = subprocess.run(
                [str(ONGO), "experiment", "approve", experiment_id, "--actor", "driver"],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            self.assertEqual(json.loads(approved.stdout)["approval"]["authority"], "zero-cost-policy")

            guarded = subprocess.run(
                [
                    str(ONGO),
                    "ken",
                    "delete",
                    "--dry-run",
                    "pub",
                    "--kind",
                    "ongo-experiment",
                ],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(guarded.returncode, 4)
            self.assertFalse(json.loads(guarded.stderr)["ok"])
            self.assertEqual(
                len(
                    json.loads(
                        subprocess.run(
                            ["ken", "-D", str(database), "list", "--kind", "ongo-experiment"],
                            capture_output=True,
                            text=True,
                            check=True,
                        ).stdout
                    )
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()
