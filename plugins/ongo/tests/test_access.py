#!/usr/bin/env python3
"""Access-key CLI and Ken policy tests."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(PLUGIN_ROOT / "lib"))

from ongo.access import (
    database_identity,
    create_key,
    export_key,
    format_capability,
    grant_key,
    list_keys,
    load_keyring,
    pending_create_path,
    parse_capability,
)
from ongo.errors import OngoError
from ongo.ken import KenClient


@unittest.skipUnless(shutil.which("ken"), "Ken v3 is required")
class AccessKeyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "ken.db"
        self.keyring = self.root / "site-keys.json"
        self.client = KenClient(binary=shutil.which("ken"), db=str(self.database))
        self.client.initialize()
        self.client.ensure_kinds()

    def tearDown(self):
        self.temporary.cleanup()

    def add_published_note(self, key):
        note_id = self.client.command(
            "add", "note", "--key", key, "--title", f"Title {key}"
        ).stdout.strip()
        self.client.command(
            "add", "ongo-web", "--key", note_id, "--title", f"Published {key}"
        )
        return note_id

    def test_key_material_stays_out_of_ken_and_export_round_trips(self):
        created = create_key(
            self.client,
            label="All research",
            scope="all",
            keyring_path=self.keyring,
        )
        self.assertTrue(created["created"])
        self.assertEqual(len(parse_capability(created["capability"])), 32)
        self.assertEqual(self.keyring.stat().st_mode & 0o777, 0o600)
        raw_keyring = json.loads(self.keyring.read_text(encoding="utf-8"))
        secret = raw_keyring["keys"][0]["secret"]
        self.assertNotIn(secret.encode("ascii"), self.database.read_bytes())

        record = self.client.show(created["record_id"])
        self.assertNotIn(secret, json.dumps(record))
        metadata = json.loads(record["body"])
        self.assertEqual(metadata["scope"], "all")
        self.assertEqual(metadata["label"], "All research")

        exported = export_key(
            self.client, created["key_id"], keyring_path=self.keyring
        )
        self.assertEqual(exported["capability"], created["capability"])
        listed = list_keys(self.client, self.keyring)
        self.assertEqual(len(listed["keys"]), 1)
        self.assertTrue(listed["keys"][0]["key_material_available"])
        self.assertNotIn("capability", listed["keys"][0])

    def test_published_scope_snapshots_current_resources_only(self):
        first = self.add_published_note("first")
        created = create_key(
            self.client,
            label="Published snapshot",
            scope="published",
            keyring_path=self.keyring,
        )
        second = self.add_published_note("second")
        descriptor = self.client.show(created["record_id"])
        related = {
            relationship["publication"]
            for relationship in descriptor["relationships"]
            if relationship["role"] == "object"
            and relationship["relkind"] == "ongo-readable-by"
        }
        self.assertIn(first, related)
        self.assertNotIn(second, related)
        self.assertEqual(json.loads(descriptor["body"])["scope"], "explicit")

    def test_published_scope_rejects_ambiguous_marker_without_saving_key(self):
        for title in ("First", "Second"):
            self.client.command(
                "add", "note", "--key", "ambiguous", "--title", title
            )
        self.client.command(
            "add", "ongo-web", "--key", "ambiguous", "--title", "Published"
        )

        with self.assertRaises(OngoError) as raised:
            create_key(
                self.client,
                label="Unsafe snapshot",
                scope="published",
                keyring_path=self.keyring,
            )

        self.assertEqual(raised.exception.code, "publication-conflict")
        self.assertFalse(self.keyring.exists())
        self.assertEqual(self.client.records("ongo-access-key"), [])

    def test_grant_is_retry_safe(self):
        note_id = self.add_published_note("selected")
        created = create_key(
            self.client,
            label="Selected",
            scope="empty",
            keyring_path=self.keyring,
        )
        first = grant_key(self.client, created["key_id"], note_id)
        second = grant_key(self.client, created["key_id"], note_id)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        record = self.client.show(note_id)
        self.assertEqual(
            sum(
                relationship["relkind"] == "ongo-readable-by"
                for relationship in record["relationships"]
            ),
            1,
        )

    def test_concurrent_grants_create_one_policy_edge(self):
        note_id = self.add_published_note("concurrent-grant")
        created = create_key(
            self.client,
            label="Concurrent grant",
            scope="empty",
            keyring_path=self.keyring,
        )
        command = [
            str(PLUGIN_ROOT / "bin" / "ongo"),
            "key",
            "--ken",
            shutil.which("ken"),
            "--db",
            str(self.database),
            "grant",
            created["key_id"],
            note_id,
        ]
        processes = [
            subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _index in range(20)
        ]
        results = [process.communicate(timeout=30) for process in processes]
        decoded = []
        for process, (stdout, stderr) in zip(processes, results):
            self.assertEqual(process.returncode, 0, stdout + stderr)
            decoded.append(json.loads(stdout))
        self.assertEqual(sum(item["created"] for item in decoded), 1)
        record = self.client.show(note_id)
        self.assertEqual(
            sum(
                relationship["relkind"] == "ongo-readable-by"
                and relationship["publication"] == created["record_id"]
                for relationship in record["relationships"]
            ),
            1,
        )

    def test_grant_rejects_identifier_shared_by_an_id_and_another_key(self):
        first_id = self.client.command(
            "add", "note", "--key", "first", "--title", "First"
        ).stdout.strip()
        second_id = self.client.command(
            "add", "note", "--key", first_id, "--title", "Second"
        ).stdout.strip()
        created = create_key(
            self.client,
            label="Selected",
            scope="empty",
            keyring_path=self.keyring,
        )

        with self.assertRaises(OngoError) as raised:
            grant_key(self.client, created["key_id"], first_id)

        self.assertEqual(raised.exception.code, "publication-conflict")
        for publication_id in (first_id, second_id):
            record = self.client.show(publication_id)
            self.assertFalse(
                any(
                    relationship["relkind"] == "ongo-readable-by"
                    for relationship in record["relationships"]
                )
            )

    def test_concurrent_creates_preserve_every_keyring_entry(self):
        processes = []
        for index in range(12):
            command = [
                str(PLUGIN_ROOT / "bin" / "ongo"),
                "key",
                "--ken",
                shutil.which("ken"),
                "--db",
                str(self.database),
                "--keyring",
                str(self.keyring),
                "create",
                "--label",
                f"concurrent-{index}",
                "--scope",
                "all",
            ]
            processes.append(
                subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )

        results = [process.communicate(timeout=30) for process in processes]
        for process, (stdout, stderr) in zip(processes, results):
            self.assertEqual(process.returncode, 0, stdout + stderr)

        _, keyring = load_keyring(self.keyring)
        self.assertEqual(len(keyring["keys"]), 12)
        descriptors = list(self.client.records("ongo-access-key"))
        self.assertEqual(len(descriptors), 12)
        self.assertEqual(
            {entry["key_id"] for entry in keyring["keys"]},
            {json.loads(record["body"])["key_id"] for record in descriptors},
        )

    def test_import_is_idempotent_by_secret(self):
        original = create_key(
            self.client,
            label="Original",
            scope="empty",
            keyring_path=self.keyring,
        )
        repeated = create_key(
            self.client,
            label="Ignored replacement",
            scope="all",
            keyring_path=self.keyring,
            capability=original["capability"],
        )
        self.assertFalse(repeated["created"])
        self.assertEqual(repeated["key_id"], original["key_id"])
        self.assertEqual(repeated["scope"], "explicit")
        _, keyring = load_keyring(self.keyring)
        self.assertEqual(len(keyring["keys"]), 1)

    def test_symlinked_keyring_updates_canonical_target(self):
        first = create_key(
            self.client,
            label="Canonical",
            scope="empty",
            keyring_path=self.keyring,
        )
        alias = self.root / "keyring-alias.json"
        alias.symlink_to(self.keyring)

        second = create_key(
            self.client,
            label="Through alias",
            scope="empty",
            keyring_path=alias,
        )

        self.assertTrue(alias.is_symlink())
        resolved, keyring = load_keyring(alias)
        self.assertEqual(resolved, self.keyring.resolve())
        self.assertEqual(
            {entry["key_id"] for entry in keyring["keys"]},
            {first["key_id"], second["key_id"]},
        )
        _, canonical = load_keyring(self.keyring)
        self.assertEqual(canonical, keyring)

    def test_hard_linked_keyring_is_rejected_before_mutation(self):
        created = create_key(
            self.client,
            label="Original",
            scope="empty",
            keyring_path=self.keyring,
        )
        alias = self.root / "hard-link-keyring.json"
        os.link(self.keyring, alias)
        original = self.keyring.read_bytes()

        with self.assertRaises(OngoError) as raised:
            create_key(
                self.client,
                label="Must not fork",
                scope="empty",
                keyring_path=alias,
            )

        self.assertEqual(
            raised.exception.code, "site-keyring-hardlink-unsupported"
        )
        self.assertEqual(self.keyring.read_bytes(), original)
        self.assertEqual(alias.read_bytes(), original)
        descriptors = self.client.records("ongo-access-key")
        self.assertEqual(len(descriptors), 1)
        self.assertEqual(descriptors[0]["id"], created["record_id"])

    def test_cli_import_reads_secret_from_stdin_and_rejects_secret_argv(self):
        capability = format_capability(b"i" * 32)
        command = [
            str(PLUGIN_ROOT / "bin" / "ongo"),
            "key",
            "--ken",
            shutil.which("ken"),
            "--db",
            str(self.database),
            "--keyring",
            str(self.keyring),
            "import",
            "--label",
            "Imported",
            "--scope",
            "empty",
        ]
        imported = subprocess.run(
            command,
            input=capability + "\n",
            capture_output=True,
            text=True,
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertNotIn(capability, " ".join(command))
        self.assertEqual(json.loads(imported.stdout)["label"], "Imported")

        rejected = subprocess.run(
            [*command, "--capability", capability],
            input="",
            capture_output=True,
            text=True,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertNotIn(capability, rejected.stderr)

    def test_existing_local_key_recovers_a_missing_descriptor(self):
        original = create_key(
            self.client,
            label="Reusable local key",
            scope="empty",
            keyring_path=self.keyring,
        )
        second_database = self.root / "second.db"
        second_client = KenClient(binary=shutil.which("ken"), db=str(second_database))
        second_client.initialize()
        second_client.ensure_kinds()

        recovered = create_key(
            second_client,
            label="Ignored because local metadata is authoritative",
            scope="all",
            keyring_path=self.keyring,
            capability=original["capability"],
        )

        self.assertTrue(recovered["created"])
        self.assertEqual(recovered["key_id"], original["key_id"])
        self.assertEqual(recovered["scope"], "all")
        metadata = json.loads(second_client.show(recovered["record_id"])["body"])
        self.assertEqual(metadata["label"], "Reusable local key")
        self.assertEqual(metadata["scope"], "all")
        _, keyring = load_keyring(self.keyring)
        self.assertEqual(len(keyring["keys"]), 1)

    def test_committed_key_survives_lost_load_response_and_retries(self):
        real_load = self.client.load

        def commit_then_fail(payload):
            real_load(payload)
            raise OngoError(
                "simulated lost Ken response",
                code="ken-json-invalid",
                exit_code=3,
            )

        with mock.patch.object(self.client, "load", side_effect=commit_then_fail):
            created = create_key(
                self.client,
                label="Recovered commit",
                scope="all",
                keyring_path=self.keyring,
            )

        self.assertTrue(created["created"])
        _, keyring = load_keyring(self.keyring)
        self.assertEqual(len(keyring["keys"]), 1)
        self.assertEqual(len(self.client.records("ongo-access-key")), 1)
        repeated = create_key(
            self.client,
            label="Ignored retry label",
            scope="empty",
            keyring_path=self.keyring,
            capability=created["capability"],
        )
        self.assertFalse(repeated["created"])
        self.assertEqual(repeated["record_id"], created["record_id"])

    def test_hard_crash_pending_create_is_replayed_by_plain_retry(self):
        with mock.patch.object(self.client, "load", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                create_key(
                    self.client,
                    label="Interrupted create",
                    scope="all",
                    keyring_path=self.keyring,
                )

        pending = pending_create_path(
            self.keyring, database_identity(self.client)
        )
        self.assertTrue(pending.is_file())
        _, keyring = load_keyring(self.keyring)
        self.assertEqual(len(keyring["keys"]), 1)
        self.assertEqual(self.client.records("ongo-access-key"), [])

        recovered = create_key(
            self.client,
            label="Retry does not need the lost capability",
            scope="empty",
            keyring_path=self.keyring,
        )

        self.assertTrue(recovered["created"])
        self.assertEqual(recovered["label"], "Interrupted create")
        self.assertEqual(recovered["scope"], "all")
        self.assertFalse(pending.exists())
        self.assertEqual(len(self.client.records("ongo-access-key")), 1)
        _, keyring = load_keyring(self.keyring)
        self.assertEqual(len(keyring["keys"]), 1)

    def test_pending_create_is_scoped_to_its_database(self):
        self.add_published_note("first-database")
        with mock.patch.object(self.client, "load", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                create_key(
                    self.client,
                    label="Shared administrator key",
                    scope="published",
                    keyring_path=self.keyring,
                )

        first_pending = pending_create_path(
            self.keyring, database_identity(self.client)
        )
        self.assertTrue(first_pending.is_file())
        _, keyring = load_keyring(self.keyring)
        capability = "ongo-key-v1." + keyring["keys"][0]["secret"]

        second_database = self.root / "second.db"
        second = KenClient(binary=shutil.which("ken"), db=str(second_database))
        second.initialize()
        second.ensure_kinds()
        created_second = create_key(
            second,
            label="Ignored in favor of durable keyring metadata",
            scope="empty",
            keyring_path=self.keyring,
            capability=capability,
        )

        self.assertTrue(created_second["created"])
        self.assertTrue(first_pending.is_file())
        self.assertEqual(len(second.records("ongo-access-key")), 1)
        self.assertEqual(self.client.records("ongo-access-key"), [])

        recovered_first = create_key(
            self.client,
            label="Retry",
            scope="empty",
            keyring_path=self.keyring,
        )
        self.assertTrue(recovered_first["created"])
        self.assertFalse(first_pending.exists())
        self.assertEqual(len(self.client.records("ongo-access-key")), 1)
        self.assertEqual(len(second.records("ongo-access-key")), 1)

    def test_invalid_capability_is_rejected(self):
        invalid = (
            "not-a-key",
            "ongo-key-v1." + base64.b64encode(b"\xfb" * 32).decode("ascii"),
        )
        for capability in invalid:
            with self.subTest(capability=capability), self.assertRaises(
                OngoError
            ) as raised:
                create_key(
                    self.client,
                    label="Bad",
                    scope="empty",
                    keyring_path=self.keyring,
                    capability=capability,
                )
            self.assertEqual(raised.exception.code, "invalid-access-capability")
        self.assertFalse(self.keyring.exists())

    def test_corrupt_stored_capability_is_a_storage_failure(self):
        self.keyring.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "keys": [
                        {
                            "key_id": "key-id",
                            "label": "Broken",
                            "secret": "not-a-key",
                            "fingerprint": "broken",
                            "created_at": "2026-08-03T00:00:00Z",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(OngoError) as raised:
            load_keyring(self.keyring)
        self.assertEqual(raised.exception.code, "invalid-site-keyring")
        self.assertEqual(raised.exception.exit_code, 3)


if __name__ == "__main__":
    unittest.main()
