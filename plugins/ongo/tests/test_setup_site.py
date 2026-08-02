#!/usr/bin/env python3
"""Setup, doctor, and Ken-backed site integration tests."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
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
    verify_checksum,
)


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

        with mock.patch.object(cli, "install_ken", return_value=source), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.setup_main(["--db", str(self.database)]), 0)
            self.assertEqual(cli.setup_main(["--db", str(self.database)]), 0)

        after = KenClient(binary=source, db=str(self.database))
        self.assertEqual(after.show(existing_id)["title"], "Existing")
        self.assertEqual(len(after.list_kind("note")), 1)
        for kind in PUBLICATION_KINDS:
            self.assertEqual(after.command("pubkind", "show", kind).returncode, 0)
        for kind in RELATIONSHIP_KINDS:
            self.assertEqual(after.command("relkind", "show", kind).returncode, 0)

    def test_doctor_reports_compatible_database_and_required_kinds(self):
        source = str(self.data / "bin" / "ken")
        with mock.patch.object(cli, "install_ken", return_value=source), contextlib.redirect_stdout(io.StringIO()):
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


if __name__ == "__main__":
    unittest.main()
