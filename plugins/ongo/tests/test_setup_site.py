#!/usr/bin/env python3
"""Setup, doctor, and Ken-backed site integration tests."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(PLUGIN_ROOT / "lib"))

from ongo import cli, site
from ongo.errors import OngoError
from ongo.ken import (
    PUBLICATION_KINDS,
    RELATIONSHIP_KINDS,
    KenClient,
    agent_state_path,
    default_data_dir,
    verify_checksum,
)


class RuntimePathTests(unittest.TestCase):
    def test_claude_data_is_preserved_when_codex_variable_is_also_present(self):
        with mock.patch.dict(
            os.environ,
            {
                "PLUGIN_DATA": "/tmp/codex-ongo-data",
                "CLAUDE_PLUGIN_DATA": "/tmp/claude-ongo-data",
            },
            clear=True,
        ):
            self.assertEqual(default_data_dir(), Path("/tmp/claude-ongo-data"))
            self.assertEqual(
                agent_state_path(), Path("/tmp/claude-ongo-data/agent-state.json")
            )

    def test_codex_plugin_data_is_used_without_claude_data(self):
        with mock.patch.dict(
            os.environ, {"PLUGIN_DATA": "/tmp/codex-ongo-data"}, clear=True
        ):
            self.assertEqual(default_data_dir(), Path("/tmp/codex-ongo-data"))

    def test_whitespace_environment_values_are_ignored(self):
        with mock.patch.dict(
            os.environ,
            {
                "ONGO_DATA_DIR": " ",
                "CLAUDE_PLUGIN_DATA": "\t",
                "PLUGIN_DATA": "  ",
                "XDG_DATA_HOME": "/tmp/xdg-data",
            },
            clear=True,
        ):
            self.assertEqual(default_data_dir(), Path("/tmp/xdg-data/ongo"))

    def test_explicit_ongo_data_precedes_host_data(self):
        with mock.patch.dict(
            os.environ,
            {
                "ONGO_DATA_DIR": "/tmp/explicit-ongo-data",
                "PLUGIN_DATA": "/tmp/codex-ongo-data",
                "CLAUDE_PLUGIN_DATA": "/tmp/claude-ongo-data",
            },
            clear=True,
        ):
            self.assertEqual(default_data_dir(), Path("/tmp/explicit-ongo-data"))


@unittest.skipUnless(shutil.which("ken"), "Ken v3 is required")
class SetupAndSiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "plugin-data"
        (self.data / "bin").mkdir(parents=True)
        shutil.copy2(shutil.which("ken"), self.data / "bin" / "ken")
        (self.data / "bin" / "ken").chmod(0o755)
        self.database = self.root / "ken.db"
        self.environment = mock.patch.dict(
            os.environ,
            {
                "CLAUDE_PLUGIN_DATA": str(self.data),
                "ONGO_KEN_DB": str(self.database),
                "ONGO_LEGACY_STATE_PATH": str(self.root / "legacy-state.json"),
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_setup_is_idempotent_and_preserves_existing_publications(self):
        source = str(self.data / "bin" / "ken")
        client = KenClient(binary=source, db=str(self.database))
        client.initialize()
        existing_id = client.command(
            "add", "note", "-k", "existing", "--title", "Existing"
        ).stdout.strip()

        with mock.patch.object(cli, "install_ken", return_value=source), mock.patch.object(
            cli,
            "install_cryptography",
            return_value={"path": str(self.data / "python"), "version": "49.0.0"},
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.setup_main(["--db", str(self.database)]), 0)
            self.assertEqual(cli.setup_main(["--db", str(self.database)]), 0)

        after = KenClient(binary=source, db=str(self.database))
        self.assertEqual(after.show(existing_id)["title"], "Existing")
        self.assertEqual(len(after.list_kind("note")), 1)
        for kind in PUBLICATION_KINDS:
            self.assertEqual(after.command("pubkind", "show", kind).returncode, 0)
        for kind in RELATIONSHIP_KINDS:
            self.assertEqual(after.command("relkind", "show", kind).returncode, 0)

    def test_setup_reports_durable_agent_state_path(self):
        source = str(self.data / "bin" / "ken")
        output = io.StringIO()
        with mock.patch.object(cli, "install_ken", return_value=source), mock.patch.object(
            cli,
            "install_cryptography",
            return_value={"path": str(self.data / "python"), "version": "49.0.0"},
        ), contextlib.redirect_stdout(output):
            self.assertEqual(cli.setup_main(["--db", str(self.database)]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["state"], str(self.data / "agent-state.json"))
        self.assertEqual(payload["state_migration"]["status"], "none")

    def test_setup_reports_invalid_legacy_state_without_failing_install(self):
        source = str(self.data / "bin" / "ken")
        legacy = self.root / "legacy-state.json"
        legacy.write_text('{"unrelated":true}\n', encoding="utf-8")
        output = io.StringIO()
        with mock.patch.object(cli, "install_ken", return_value=source), mock.patch.object(
            cli,
            "install_cryptography",
            return_value={"path": str(self.data / "python"), "version": "49.0.0"},
        ), contextlib.redirect_stdout(output):
            self.assertEqual(cli.setup_main(["--db", str(self.database)]), 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state_migration"]["status"], "invalid")
        self.assertEqual(payload["state_migration"]["from"], str(legacy))
        self.assertFalse((self.data / "agent-state.json").exists())
        self.assertEqual(json.loads(legacy.read_text(encoding="utf-8")), {"unrelated": True})

    def test_setup_reports_malformed_legacy_json_without_failing_install(self):
        source = str(self.data / "bin" / "ken")
        legacy = self.root / "legacy-state.json"
        legacy.write_text("{unfinished\n", encoding="utf-8")
        output = io.StringIO()
        with mock.patch.object(cli, "install_ken", return_value=source), mock.patch.object(
            cli,
            "install_cryptography",
            return_value={"path": str(self.data / "python"), "version": "49.0.0"},
        ), contextlib.redirect_stdout(output):
            self.assertEqual(cli.setup_main(["--db", str(self.database)]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["state_migration"]["status"], "invalid")
        self.assertEqual(legacy.read_text(encoding="utf-8"), "{unfinished\n")

    def test_doctor_reports_compatible_database_and_required_kinds(self):
        source = str(self.data / "bin" / "ken")
        with mock.patch.object(cli, "install_ken", return_value=source), mock.patch.object(
            cli,
            "install_cryptography",
            return_value={"path": str(self.data / "python"), "version": "49.0.0"},
        ), contextlib.redirect_stdout(io.StringIO()):
            cli.setup_main(["--db", str(self.database)])
        output = io.StringIO()
        with mock.patch.object(
            cli,
            "clacks_version",
            return_value={"ok": True, "path": "/test/clacks", "version": "0.14.1"},
        ), contextlib.redirect_stdout(output):
            self.assertEqual(
                cli.doctor_main(
                    [
                        "--json",
                        "--ken",
                        str(self.data / "bin" / "ken"),
                        "--db",
                        str(self.database),
                    ]
                ),
                0,
            )
        report = json.loads(output.getvalue())
        self.assertTrue(report["ok"])
        self.assertEqual(report["checks"]["database"]["missing_publication_kinds"], [])
        self.assertEqual(report["checks"]["database"]["missing_relationship_kinds"], [])

    def test_pinned_binary_checksum_mismatch_is_rejected(self):
        candidate = self.root / "ken-corrupt"
        candidate.write_bytes(b"not the pinned release binary")
        with self.assertRaises(OngoError) as raised:
            verify_checksum(candidate, "0" * 64)
        self.assertEqual(raised.exception.code, "ken-checksum-mismatch")

    def test_cryptography_install_replaces_stale_target_atomically(self):
        target = self.data / "python"
        stale = target / "cryptography-48.0.1.dist-info"
        stale.mkdir(parents=True)
        stale.joinpath("OLD").write_text("stale metadata", encoding="utf-8")

        def fake_run(command, **kwargs):
            if "pip" in command:
                install_target = Path(command[command.index("--target") + 1])
                install_target.joinpath("cryptography").mkdir()
                install_target.joinpath("cryptography", "__init__.py").write_text(
                    "", encoding="utf-8"
                )
                install_target.joinpath(
                    "cryptography-49.0.0.dist-info"
                ).mkdir()
                return subprocess.CompletedProcess(command, 0, "", "")
            probe_target = Path(kwargs["env"]["PYTHONPATH"])
            version = "48.0.1" if probe_target == target else "49.0.0"
            return subprocess.CompletedProcess(
                command,
                0,
                f"{version}\n{version}\n"
                f"{probe_target / 'cryptography' / '__init__.py'}\n",
                "",
            )

        with mock.patch.object(cli.subprocess, "run", side_effect=fake_run):
            installed = cli.install_cryptography()

        self.assertEqual(installed["version"], "49.0.0")
        self.assertFalse(stale.exists())
        self.assertTrue((target / "cryptography-49.0.0.dist-info").is_dir())
        self.assertEqual(list(self.data.glob(".python.*-*")), [])

    def test_doctor_requires_cursor_capable_clacks(self):
        for version, expected in (("0.14.0", False), ("0.14.1", True)):
            with self.subTest(version=version), mock.patch.object(
                cli.shutil, "which", return_value="/test/clacks"
            ), mock.patch.object(
                cli.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=version, stderr=""
                ),
            ):
                report = cli.clacks_version()
            self.assertEqual(report["ok"], expected)
            self.assertEqual(report["minimum"], "0.14.1")

    def test_doctor_can_validate_one_shot_runtime_without_clacks(self):
        source = str(self.data / "bin" / "ken")
        with mock.patch.object(cli, "install_ken", return_value=source), mock.patch.object(
            cli,
            "install_cryptography",
            return_value={"path": str(self.data / "python"), "version": "49.0.0"},
        ), contextlib.redirect_stdout(io.StringIO()):
            cli.setup_main(["--db", str(self.database)])
        output = io.StringIO()
        with mock.patch.object(
            cli, "clacks_version", side_effect=AssertionError("must not probe clacks")
        ), contextlib.redirect_stdout(output):
            self.assertEqual(
                cli.doctor_main(
                    [
                        "--json",
                        "--no-slack",
                        "--ken",
                        source,
                        "--db",
                        str(self.database),
                    ]
                ),
                0,
            )
        report = json.loads(output.getvalue())
        self.assertTrue(report["ok"])
        self.assertNotIn("clacks", report["checks"])

    def test_site_is_private_by_default_and_uses_explicit_markers(self):
        client = KenClient(binary=str(self.data / "bin" / "ken"), db=str(self.database))
        client.initialize()
        client.ensure_kinds()
        note_file = self.root / "private.md"
        note_file.write_text("# Private evidence\n\nsecret text\n", encoding="utf-8")
        note_id = client.command(
            "add", "note", "-k", str(note_file), "--title", "Private evidence"
        ).stdout.strip()

        class Args:
            ken = str(self.data / "bin" / "ken")
            db = str(self.database)
            out = str(self.root / "site")
            site_title = "Test site"
            base_url = ""

        with mock.patch.object(site, "vendor_katex", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(site.build(Args), 0)
        self.assertNotIn("Private evidence", (self.root / "site" / "index.html").read_text())

        client.command(
            "add", "ongo-web", "-k", note_id, "--title", "Reviewed evidence"
        )
        second_file = self.root / "second.md"
        second_file.write_text("# Second\n\nsecond body\n", encoding="utf-8")
        second_id = client.command(
            "add", "note", "-k", str(second_file), "--title", "Second"
        ).stdout.strip()
        client.command(
            "add", "ongo-web", "-k", second_id, "--title", "Reviewed evidence"
        )
        with mock.patch.object(site, "vendor_katex", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(site.build(Args), 0)
        index = (self.root / "site" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Reviewed evidence", index)
        item_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (self.root / "site" / "items").glob("*.html")
        )
        self.assertIn("secret text", item_text)
        self.assertIn("second body", item_text)
        self.assertEqual(
            len(list((self.root / "site" / "items").glob("*.html"))), 2
        )

    def test_site_preserves_ken_creation_dates_and_order(self):
        client = KenClient(binary=str(self.data / "bin" / "ken"), db=str(self.database))
        client.initialize()
        client.ensure_kinds()
        publications = []
        for title, timestamp in (
            ("Older evidence", "2026-07-01 09:00:00"),
            ("Newer evidence", "2026-07-02 14:30:00"),
        ):
            source = self.root / f"{title.lower().replace(' ', '-')}.md"
            source.write_text(f"# {title}\n", encoding="utf-8")
            publication_id = client.command(
                "add", "note", "-k", str(source), "--title", title
            ).stdout.strip()
            client.command("add", "ongo-web", "-k", publication_id, "--title", title)
            publications.append((publication_id, timestamp))
        with sqlite3.connect(self.database) as connection:
            connection.executemany(
                "UPDATE publications SET created_at = ? WHERE id = ?",
                [(timestamp, publication_id) for publication_id, timestamp in publications],
            )

        class Args:
            ken = str(self.data / "bin" / "ken")
            db = str(self.database)
            out = str(self.root / "dated-site")
            site_title = "Dated site"
            base_url = ""

        with mock.patch.object(site, "vendor_katex", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(site.build(Args), 0)
        index = (self.root / "dated-site" / "index.html").read_text(encoding="utf-8")
        self.assertIn("2026-07-01 09:00:00", index)
        self.assertIn("2026-07-02 14:30:00", index)
        self.assertLess(index.index("Newer evidence"), index.index("Older evidence"))


if __name__ == "__main__":
    unittest.main()
