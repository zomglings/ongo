#!/usr/bin/env python3
"""Public CLI surface tests."""

from __future__ import annotations

import os
import json
import shutil
import sqlite3
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
        self.assertIn("skill --harness", result.stdout)
        self.assertIn("site build", result.stdout)

    def test_every_public_subcommand_has_help(self):
        commands = (
            ("setup",),
            ("doctor",),
            ("skill",),
            ("slack", "poll"),
            ("arxiv", "sweep"),
            ("site", "build"),
            ("site", "serve"),
            ("key",),
            ("key", "create"),
            ("key", "import"),
            ("key", "list"),
            ("key", "export"),
            ("key", "grant"),
            ("ken", "delete"),
            ("experiment",),
            ("experiment", "create"),
            ("experiment", "show"),
            ("experiment", "render"),
            ("experiment", "status"),
            ("experiment", "note"),
            ("experiment", "note", "add"),
            ("experiment", "note", "list"),
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

    def test_skill_renders_only_the_selected_harness(self):
        cases = {
            "claude": {
                "included": ("targets the **Claude Code** harness", "CronCreate"),
                "excluded": ("references/codex.md", "heartbeat automation"),
            },
            "codex": {
                "included": (
                    "targets the **Codex** harness",
                    "heartbeat automation",
                    "## Migrate legacy state",
                ),
                "excluded": (
                    "references/claude-code.md",
                    "CronCreate",
                    "read the Claude adapter's legacy-upgrade section",
                ),
            },
        }
        for harness, expected in cases.items():
            with self.subTest(harness=harness):
                result = self.run_cli("skill", "--harness", harness)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.startswith("---\nname: ongo\n"))
                self.assertIn(f'"host": "{harness}"', result.stdout)
                for text in expected["included"]:
                    self.assertIn(text, result.stdout)
                for text in expected["excluded"]:
                    self.assertNotIn(text, result.stdout)

    def test_skill_rejects_unknown_harness(self):
        result = self.run_cli("skill", "--harness", "other")
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["error"]["code"], "invalid-input")
        self.assertIn("invalid choice", payload["error"]["message"])

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
            created_payload = json.loads(created.stdout)
            experiment_id = created_payload["experiment"]["experiment_id"]
            experiment_record_id = created_payload["record_id"]
            approved = subprocess.run(
                [str(ONGO), "experiment", "approve", experiment_id, "--actor", "driver"],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            self.assertEqual(json.loads(approved.stdout)["approval"]["authority"], "zero-cost-policy")

            topic_id = subprocess.run(
                [
                    "ken",
                    "-D",
                    str(database),
                    "add",
                    "topic",
                    "--key",
                    "cli-deviation",
                    "--title",
                    "CLI deviation",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            noted = subprocess.run(
                [
                    str(ONGO),
                    "experiment",
                    "note",
                    "add",
                    experiment_id,
                    "--actor",
                    "driver",
                    "--text",
                    "A free-form **CLI note**.",
                    "--topic",
                    "cli-deviation",
                    "--operation-key",
                    "cli-note",
                ],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(noted.returncode, 0, noted.stderr)
            note_payload = json.loads(noted.stdout)
            note_id = note_payload["note"]["record_id"]
            self.assertEqual(note_payload["note"]["topics"][0]["record_id"], topic_id)
            listed = subprocess.run(
                [
                    str(ONGO),
                    "experiment",
                    "note",
                    "list",
                    experiment_id,
                    "--format",
                    "markdown",
                ],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertIn("free-form **CLI note**", listed.stdout)

            guarded_note = subprocess.run(
                [str(ONGO), "ken", "delete", "pub", note_id],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(guarded_note.returncode, 4)
            self.assertFalse(json.loads(guarded_note.stderr)["ok"])

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

            with sqlite3.connect(database) as connection:
                experiment_relationship_id = connection.execute(
                    "SELECT id FROM relationships WHERE kind = 'ongo-has-plan'"
                ).fetchone()[0]
            for dry_run in (True, False):
                arguments = ["ken", "delete"]
                if dry_run:
                    arguments.append("--dry-run")
                arguments.extend(("rel", experiment_relationship_id))
                guarded_relationship = subprocess.run(
                    [str(ONGO), *arguments],
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                self.assertEqual(guarded_relationship.returncode, 4)
                self.assertFalse(json.loads(guarded_relationship.stderr)["ok"])
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM relationships WHERE id = ?",
                        (experiment_relationship_id,),
                    ).fetchone()[0],
                    1,
                )

            first_note = subprocess.run(
                ["ken", "-D", str(database), "add", "note", "--title", "First"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            second_note = subprocess.run(
                ["ken", "-D", str(database), "add", "note", "--title", "Second"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            experiment_evidence_relationship = subprocess.run(
                [
                    "ken",
                    "-D",
                    str(database),
                    "relate",
                    "--subject",
                    first_note,
                    "--object",
                    experiment_record_id,
                    "--relation",
                    "related-to",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            for arguments in (
                ("pub", first_note),
                ("pub", "--kind", "note"),
            ):
                guarded_publication = subprocess.run(
                    [str(ONGO), "ken", "delete", *arguments],
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                self.assertEqual(guarded_publication.returncode, 4)
                self.assertFalse(json.loads(guarded_publication.stderr)["ok"])
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM publications WHERE id IN (?, ?)",
                        (first_note, second_note),
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM relationships WHERE id = ?",
                        (experiment_evidence_relationship,),
                    ).fetchone()[0],
                    1,
                )
            ordinary_relationship = subprocess.run(
                [
                    "ken",
                    "-D",
                    str(database),
                    "relate",
                    "--subject",
                    first_note,
                    "--object",
                    second_note,
                    "--relation",
                    "related-to",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            deleted_relationship = subprocess.run(
                [str(ONGO), "ken", "delete", "rel", ordinary_relationship],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(deleted_relationship.returncode, 0, deleted_relationship.stderr)
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM relationships WHERE id = ?",
                        (ordinary_relationship,),
                    ).fetchone()[0],
                    0,
                )


if __name__ == "__main__":
    unittest.main()
