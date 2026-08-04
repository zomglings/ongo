#!/usr/bin/env python3
"""Protected static-site integration and leakage tests."""

from __future__ import annotations

import base64
import contextlib
import http.server
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(PLUGIN_ROOT / "lib"))

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ongo import experiments, site
from ongo.access import create_key, grant_key, load_keyring
from ongo.errors import OngoError
from ongo.ken import KenClient


def decode64url(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def find_chrome():
    for cache in (
        Path.home() / "Library" / "Caches" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
    ):
        if cache.is_dir():
            for found in sorted(cache.glob("chromium_headless_shell-*/**/chrome-headless-shell")):
                if found.is_file() and os.access(found, os.X_OK):
                    return str(found)
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    application = Path(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
    return str(application) if application.is_file() else None


@unittest.skipUnless(shutil.which("ken"), "Ken v3 is required")
class ProtectedSiteTests(unittest.TestCase):
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

    def add_note(self, name, title, body):
        path = self.root / f"{name}.md"
        path.write_text(body, encoding="utf-8")
        publication_id = self.client.command(
            "add", "note", "--key", str(path), "--title", title
        ).stdout.strip()
        self.client.command(
            "add", "ongo-web", "--key", publication_id, "--title", title
        )
        return publication_id

    def add_experiment(self):
        document = self.root / "plan.md"
        manifest = self.root / "manifest.json"
        document.write_text(
            "# EXPERIMENT_SECRET_HYPOTHESIS\n\nLorem ipsum controlled protocol.\n",
            encoding="utf-8",
        )
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "title": "EXPERIMENT_SECRET_TITLE",
                    "conditions": [
                        {
                            "id": "lorem",
                            "description": "EXPERIMENT_SECRET_CONDITION",
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
        created = experiments.create_experiment(
            self.client, str(document), str(manifest)
        )
        self.client.command(
            "add",
            "ongo-web",
            "--key",
            created["record_id"],
            "--title",
            "EXPERIMENT_SECRET_NAVIGATION",
        )
        return created["record_id"]

    def args(self, output):
        class Args:
            ken = shutil.which("ken")
            db = str(self.database)
            out = str(output)
            site_title = "Dummy protected Ongo"
            base_url = ""
            keyring = str(self.keyring)

        return Args

    def decrypt(self, envelope, key_id):
        _, keyring = load_keyring(self.keyring)
        entry = next(item for item in keyring["keys"] if item["key_id"] == key_id)
        key = decode64url(entry["secret"])
        aad = ("ongo-sealed-v1:" + envelope["resource_id"]).encode("utf-8")
        for variant in envelope["variants"]:
            try:
                clear = AESGCM(key).decrypt(
                    decode64url(variant["nonce"]),
                    decode64url(variant["ciphertext"]),
                    aad,
                )
                return json.loads(clear)
            except InvalidTag:
                continue
        return None

    def build_fixture(self):
        first = self.add_note(
            "first",
            "ARTICLE_ONE_SECRET_TITLE",
            "# ARTICLE_ONE_SECRET_HEADING\n\nLorem ipsum dolor sit amet.\n\n"
            "<details open><summary>SAFE_DISCLOSURE</summary>\n\n"
            "Disclosure body.\n\n</details>\n\n"
            "<img src=x onerror=ARTICLE_XSS_SENTINEL>\n\n"
            '<img src="https://protected.example/reader-leak.png">\n\n'
            "<script>ARTICLE_SCRIPT_SENTINEL</script>\n\n"
            "<a href=\"javascript:ARTICLE_RAW_LINK_SENTINEL\" "
            "onclick=\"ARTICLE_RAW_EVENT_SENTINEL\">Unsafe raw link</a>\n\n"
            "[unsafe](javascript:ARTICLE_SCHEME_SENTINEL)\n",
        )
        second = self.add_note(
            "second",
            "ARTICLE_TWO_SECRET_TITLE",
            "# ARTICLE_TWO_SECRET_HEADING\n\nConsectetur adipiscing elit.\n",
        )
        topic = self.client.command(
            "add", "topic", "--key", "secret-topic", "--title", "TAG_SECRET_LATIN"
        ).stdout.strip()
        self.client.command(
            "relate", "--subject", first, "--object", topic, "--relation", "related-to"
        )
        experiment_id = self.add_experiment()

        key_a = create_key(
            self.client,
            label="KEY_LABEL_ALPHA_SECRET",
            scope="empty",
            keyring_path=self.keyring,
        )
        key_b = create_key(
            self.client,
            label="KEY_LABEL_BETA_SECRET",
            scope="empty",
            keyring_path=self.keyring,
        )
        for publication_id in (first, experiment_id):
            grant_key(self.client, key_a["key_id"], publication_id)
        for publication_id in (first, second):
            grant_key(self.client, key_b["key_id"], publication_id)
        public = self.add_note(
            "public-mixed",
            "PUBLIC_MIXED_TITLE",
            "# PUBLIC_MIXED_BODY\n\nThis resource remains public.\n\n"
            '<img src="https://public.example/image.png">\n',
        )
        return first, second, experiment_id, public, key_a, key_b

    def test_protected_build_encrypts_semantics_and_assigns_exact_keys(self):
        first, second, experiment_id, public, key_a, key_b = self.build_fixture()
        output = self.root / "site"
        with mock.patch.object(site, "vendor_katex", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(site.build(self.args(output)), 0)
        self.assertEqual(output.stat().st_mode & 0o777, 0o755)

        forbidden = (
            "ARTICLE_ONE_SECRET",
            "ARTICLE_TWO_SECRET",
            "EXPERIMENT_SECRET",
            "TAG_SECRET_LATIN",
            "ARTICLE_XSS_SENTINEL",
            "ARTICLE_SCRIPT_SENTINEL",
            "ARTICLE_RAW_LINK_SENTINEL",
            "ARTICLE_RAW_EVENT_SENTINEL",
            "ARTICLE_SCHEME_SENTINEL",
            "KEY_LABEL_ALPHA_SECRET",
            "KEY_LABEL_BETA_SECRET",
            str(self.root),
        )
        output_bytes = b"\n".join(
            path.read_bytes() for path in output.rglob("*") if path.is_file()
        )
        for sentinel in forbidden:
            with self.subTest(sentinel=sentinel):
                self.assertNotIn(sentinel.encode("utf-8"), output_bytes)

        manifest = json.loads((output / "assets" / "ongo-sealed.json").read_text())
        self.assertEqual(len(manifest["resources"]), 4)
        by_collection = {}
        payloads = {}
        for entry in manifest["resources"]:
            envelope = json.loads((output / entry["envelope"]).read_text())
            by_collection.setdefault(entry["collection"], []).append(envelope)
            if "public" in envelope:
                payload_a = payload_b = envelope["public"]
            else:
                payload_a = self.decrypt(envelope, key_a["key_id"])
                payload_b = self.decrypt(envelope, key_b["key_id"])
            payloads[(payload_a or payload_b)["title"]] = (payload_a, payload_b, envelope)

        first_a, first_b, first_envelope = payloads["ARTICLE_ONE_SECRET_TITLE"]
        self.assertIsNotNone(first_a)
        self.assertIsNotNone(first_b)
        self.assertEqual(len(first_envelope["variants"]), 2)
        self.assertEqual(first_a, first_b)
        self.assertEqual(first_a["tags"], ["TAG_SECRET_LATIN"])
        self.assertIn("<details open>", first_a["html"])
        self.assertIn("SAFE_DISCLOSURE", first_a["html"])
        self.assertIn('<img src="x">', first_a["html"])
        self.assertNotIn("protected.example", first_a["html"])
        self.assertNotIn("onerror", first_a["html"])
        self.assertNotIn("ARTICLE_SCRIPT_SENTINEL", first_a["html"])
        self.assertNotIn("ARTICLE_RAW_LINK_SENTINEL", first_a["html"])
        self.assertNotIn("onclick", first_a["html"])
        self.assertNotIn("javascript:", first_a["html"])

        second_a, second_b, second_envelope = payloads["ARTICLE_TWO_SECRET_TITLE"]
        self.assertIsNone(second_a)
        self.assertIsNotNone(second_b)
        self.assertEqual(len(second_envelope["variants"]), 1)

        experiment_a, experiment_b, experiment_envelope = payloads[
            "EXPERIMENT_SECRET_NAVIGATION"
        ]
        self.assertIsNotNone(experiment_a)
        self.assertIsNone(experiment_b)
        self.assertEqual(experiment_envelope["collection"], "experiment")
        self.assertIn("EXPERIMENT_SECRET_HYPOTHESIS", experiment_a["html"])
        self.assertEqual(len(by_collection["experiment"]), 1)
        public_a, public_b, public_envelope = payloads["PUBLIC_MIXED_TITLE"]
        self.assertEqual(public_a, public_b)
        self.assertIn("public", public_envelope)
        self.assertIn("https://public.example/image.png", public_a["html"])
        item_shell = (
            output / next(
                entry["page"]
                for entry in manifest["resources"]
                if entry["resource_id"] == public_envelope["resource_id"]
            )
        ).read_text(encoding="utf-8")
        self.assertIn("img-src 'self' blob: data: http: https:", item_shell)
        self.assertIn(
            b"PUBLIC_MIXED_BODY",
            (
                output
                / "assets"
                / "sealed"
                / (public_envelope["resource_id"] + ".json")
            ).read_bytes(),
        )

    def test_tampering_wrong_key_and_resource_swap_fail_closed(self):
        self.build_fixture()
        output = self.root / "site"
        with mock.patch.object(site, "vendor_katex", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            site.build(self.args(output))
        manifest = json.loads((output / "assets" / "ongo-sealed.json").read_text())
        envelope = next(
            json.loads((output / entry["envelope"]).read_text())
            for entry in manifest["resources"]
            if "variants" in json.loads((output / entry["envelope"]).read_text())
        )
        wrong = AESGCM.generate_key(bit_length=256)
        variant = envelope["variants"][0]
        with self.assertRaises(InvalidTag):
            AESGCM(wrong).decrypt(
                decode64url(variant["nonce"]),
                decode64url(variant["ciphertext"]),
                ("ongo-sealed-v1:" + envelope["resource_id"]).encode(),
            )
        swapped = dict(envelope)
        swapped["resource_id"] = "0" * 32
        _, keyring = load_keyring(self.keyring)
        self.assertIsNone(self.decrypt(swapped, keyring["keys"][0]["key_id"]))

    def test_experiment_and_artifact_access_are_independent_resources(self):
        experiment_id = self.add_experiment()
        loaded = self.client.load(
            {
                "publications": [
                    {
                        "ref": "artifact",
                        "kind": "ongo-experiment-artifact",
                        "key": "demo:artifact",
                        "title": "ARTIFACT_SECRET_TITLE",
                    }
                ],
                "relationships": [],
                "notes": [
                    {
                        "publication": "artifact",
                        "body": "# ARTIFACT_SECRET_BODY\n\nLorem artifact evidence.",
                    }
                ],
            }
        )
        artifact_id = loaded["refs"]["artifact"]
        self.client.command(
            "add",
            "ongo-web",
            "--key",
            artifact_id,
            "--title",
            "ARTIFACT_SECRET_NAVIGATION",
        )
        experiment_key = create_key(
            self.client,
            label="Experiment reader",
            scope="empty",
            keyring_path=self.keyring,
        )
        artifact_key = create_key(
            self.client,
            label="Artifact reader",
            scope="empty",
            keyring_path=self.keyring,
        )
        grant_key(self.client, experiment_key["key_id"], experiment_id)
        grant_key(self.client, artifact_key["key_id"], artifact_id)

        output = self.root / "site"
        with mock.patch.object(
            site, "vendor_katex", return_value=False
        ), contextlib.redirect_stdout(io.StringIO()):
            site.build(self.args(output))

        manifest = json.loads((output / "assets" / "ongo-sealed.json").read_text())
        observed = {}
        for entry in manifest["resources"]:
            envelope = json.loads((output / entry["envelope"]).read_text())
            experiment_payload = self.decrypt(
                envelope, experiment_key["key_id"]
            )
            artifact_payload = self.decrypt(envelope, artifact_key["key_id"])
            title = (experiment_payload or artifact_payload)["title"]
            observed[title] = (experiment_payload, artifact_payload)

        root_for_experiment, root_for_artifact = observed[
            "EXPERIMENT_SECRET_NAVIGATION"
        ]
        self.assertIsNotNone(root_for_experiment)
        self.assertIsNone(root_for_artifact)
        artifact_for_experiment, artifact_for_artifact = observed[
            "ARTIFACT_SECRET_NAVIGATION"
        ]
        self.assertIsNone(artifact_for_experiment)
        self.assertIsNotNone(artifact_for_artifact)
        self.assertIn("ARTIFACT_SECRET_BODY", artifact_for_artifact["html"])

    def test_experiment_notes_and_topics_follow_experiment_encryption(self):
        experiment_id = self.add_experiment()
        topic_id = self.client.command(
            "add",
            "topic",
            "--key",
            "protected-deviation",
            "--title",
            "EXPERIMENT_NOTE_SECRET_TOPIC",
        ).stdout.strip()
        noted = experiments.add_experiment_note(
            self.client,
            experiment_id,
            actor="WORKER_SECRET_LABEL",
            markdown="# EXPERIMENT_NOTE_SECRET_BODY\n\nLorem ipsum deviation.",
            topic_identifiers=[topic_id],
        )
        note_id = noted["note"]["record_id"]
        self.client.command(
            "add",
            "ongo-web",
            "--key",
            note_id,
            "--title",
            "EXPERIMENT_NOTE_STANDALONE_SECRET",
        )
        key = create_key(
            self.client,
            label="Experiment note readers",
            scope="empty",
            keyring_path=self.keyring,
        )
        for publication_id in (experiment_id, note_id, topic_id):
            grant_key(self.client, key["key_id"], publication_id)

        output = self.root / "experiment-notes-site"
        logs = io.StringIO()
        with mock.patch.object(
            site, "vendor_katex", return_value=False
        ), contextlib.redirect_stdout(logs):
            self.assertEqual(site.build(self.args(output)), 0)

        output_bytes = b"\n".join(
            path.read_bytes() for path in output.rglob("*") if path.is_file()
        )
        for sentinel in (
            "EXPERIMENT_NOTE_SECRET_TOPIC",
            "EXPERIMENT_NOTE_SECRET_BODY",
            "WORKER_SECRET_LABEL",
            "EXPERIMENT_NOTE_STANDALONE_SECRET",
        ):
            self.assertNotIn(sentinel.encode(), output_bytes)
        manifest = json.loads((output / "assets" / "ongo-sealed.json").read_text())
        self.assertEqual(len(manifest["resources"]), 1)
        envelope = json.loads(
            (output / manifest["resources"][0]["envelope"]).read_text()
        )
        payload = self.decrypt(envelope, key["key_id"])
        self.assertIn("EXPERIMENT_NOTE_SECRET_BODY", payload["html"])
        self.assertIn("EXPERIMENT_NOTE_SECRET_TOPIC", payload["html"])
        self.assertIn("WORKER_SECRET_LABEL", payload["html"])
        self.assertIn("only publishable inside its experiment", logs.getvalue())

    def test_public_experiment_cannot_copy_a_protected_experiment_note(self):
        experiment_id = self.add_experiment()
        note_id = experiments.add_experiment_note(
            self.client,
            experiment_id,
            actor="worker",
            markdown="EXPERIMENT_NOTE_PROJECTION_SECRET",
        )["note"]["record_id"]
        key = create_key(
            self.client,
            label="Note-only readers",
            scope="empty",
            keyring_path=self.keyring,
        )
        grant_key(self.client, key["key_id"], note_id)
        output = self.root / "experiment-note-policy-conflict"
        output.mkdir()
        (output / "sentinel.txt").write_text("previous site", encoding="utf-8")

        with self.assertRaises(OngoError) as raised:
            site.build(self.args(output))

        self.assertEqual(raised.exception.code, "derived-access-policy-conflict")
        self.assertEqual((output / "sentinel.txt").read_text(), "previous site")
        self.assertNotIn(
            "EXPERIMENT_NOTE_PROJECTION_SECRET",
            "\n".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in output.rglob("*")
                if path.is_file()
            ),
        )

    def test_public_experiment_cannot_copy_a_protected_note_topic(self):
        experiment_id = self.add_experiment()
        topic_id = self.client.command(
            "add",
            "topic",
            "--key",
            "restricted-topic",
            "--title",
            "EXPERIMENT_TOPIC_PROJECTION_SECRET",
        ).stdout.strip()
        experiments.add_experiment_note(
            self.client,
            experiment_id,
            actor="worker",
            markdown="Public note with a restricted topic label.",
            topic_identifiers=[topic_id],
        )
        key = create_key(
            self.client,
            label="Topic-only readers",
            scope="empty",
            keyring_path=self.keyring,
        )
        grant_key(self.client, key["key_id"], topic_id)
        output = self.root / "experiment-topic-policy-conflict"
        output.mkdir()
        (output / "sentinel.txt").write_text("previous site", encoding="utf-8")

        with self.assertRaises(OngoError) as raised:
            site.build(self.args(output))

        self.assertEqual(raised.exception.code, "derived-access-policy-conflict")
        self.assertEqual((output / "sentinel.txt").read_text(), "previous site")
        self.assertNotIn(
            "EXPERIMENT_TOPIC_PROJECTION_SECRET",
            "\n".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in output.rglob("*")
                if path.is_file()
            ),
        )

    def test_missing_assigned_key_fails_without_replacing_previous_site(self):
        publication = self.add_note(
            "assigned", "ASSIGNED_SECRET", "# ASSIGNED_BODY"
        )
        created = create_key(
            self.client,
            label="Unavailable",
            scope="empty",
            keyring_path=self.keyring,
        )
        grant_key(self.client, created["key_id"], publication)
        self.keyring.write_text(
            '{"schema_version":1,"keys":[]}\n', encoding="utf-8"
        )
        self.keyring.chmod(0o600)
        output = self.root / "site"
        output.mkdir()
        (output / "sentinel.txt").write_text("previous site", encoding="utf-8")
        with mock.patch.object(site, "vendor_katex", return_value=False), self.assertRaises(OngoError) as raised:
            site.build(self.args(output))
        self.assertEqual(raised.exception.code, "access-key-material-missing")
        self.assertEqual((output / "sentinel.txt").read_text(), "previous site")

    def test_unreadable_policy_record_fails_closed(self):
        publication = self.add_note(
            "unreadable", "UNREADABLE_SECRET_TITLE", "# UNREADABLE_SECRET_BODY"
        )
        created = create_key(
            self.client,
            label="Unreadable policy",
            scope="empty",
            keyring_path=self.keyring,
        )
        grant_key(self.client, created["key_id"], publication)
        output = self.root / "site"
        output.mkdir()
        (output / "sentinel.txt").write_text("previous site", encoding="utf-8")

        with mock.patch.object(site, "ken_show_record", return_value=None):
            with self.assertRaises(OngoError) as raised:
                site.build(self.args(output))

        self.assertEqual(raised.exception.code, "ken-record-unreadable")
        self.assertEqual((output / "sentinel.txt").read_text(), "previous site")
        self.assertFalse((output / "assets" / "ongo-sealed.json").exists())

    def test_keyring_inside_output_is_rejected_without_data_loss(self):
        publication = self.add_note(
            "assigned", "ASSIGNED_SECRET", "# ASSIGNED_BODY"
        )
        output = self.root / "site"
        output.mkdir()
        nested_keyring = output / "administrator-keyring.json"
        created = create_key(
            self.client,
            label="Unsafe location",
            scope="empty",
            keyring_path=nested_keyring,
        )
        grant_key(self.client, created["key_id"], publication)
        original_keyring = nested_keyring.read_bytes()

        args = self.args(output)
        args.keyring = str(nested_keyring)
        with mock.patch.object(
            site, "vendor_katex", return_value=False
        ), self.assertRaises(OngoError) as raised:
            site.build(args)

        self.assertEqual(raised.exception.code, "unsafe-site-keyring-path")
        self.assertEqual(nested_keyring.read_bytes(), original_keyring)

    def test_public_build_preserves_keyring_in_conventional_old_sibling(self):
        self.add_note("public", "PUBLIC_TITLE", "# PUBLIC_BODY")
        output = self.root / "site"
        nested_keyring = self.root / "site.old" / "administrator-keyring.json"
        created = create_key(
            self.client,
            label="Unassigned sibling",
            scope="empty",
            keyring_path=nested_keyring,
        )
        self.assertTrue(created["created"])
        original_keyring = nested_keyring.read_bytes()
        args = self.args(output)
        args.keyring = str(nested_keyring)

        with mock.patch.object(
            site, "vendor_katex", return_value=False
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(site.build(args), 0)

        self.assertEqual(nested_keyring.read_bytes(), original_keyring)
        self.assertTrue((output / "index.html").is_file())

    def test_reversed_access_edge_fails_closed_before_public_rendering(self):
        publication = self.add_note(
            "direction", "DIRECTION_SECRET_TITLE", "# DIRECTION_SECRET_BODY"
        )
        created = create_key(
            self.client,
            label="Direction",
            scope="empty",
            keyring_path=self.keyring,
        )
        self.client.command(
            "relate",
            "--subject",
            created["record_id"],
            "--object",
            publication,
            "--relation",
            "ongo-readable-by",
        )
        output = self.root / "site"
        output.mkdir()
        (output / "sentinel.txt").write_text("previous site", encoding="utf-8")

        with self.assertRaises(OngoError) as raised:
            site.build(self.args(output))

        self.assertEqual(raised.exception.code, "invalid-access-policy")
        self.assertEqual((output / "sentinel.txt").read_text(), "previous site")

    def test_dangling_access_edge_without_descriptors_fails_closed(self):
        publication = self.add_note(
            "dangling", "DANGLING_SECRET_TITLE", "# DANGLING_SECRET_BODY"
        )
        topic = self.client.command(
            "add", "topic", "--key", "not-an-access-key", "--title", "Wrong target"
        ).stdout.strip()
        self.client.command(
            "relate",
            "--subject",
            publication,
            "--object",
            topic,
            "--relation",
            "ongo-readable-by",
        )
        output = self.root / "site"
        output.mkdir()
        (output / "sentinel.txt").write_text("previous site", encoding="utf-8")

        with self.assertRaises(OngoError) as raised:
            site.build(self.args(output))

        self.assertEqual(raised.exception.code, "invalid-access-policy")
        self.assertEqual((output / "sentinel.txt").read_text(), "previous site")
        self.assertNotIn(
            "DANGLING_SECRET_BODY",
            "\n".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in output.rglob("*")
                if path.is_file()
            ),
        )

    def test_ambiguous_publish_marker_fails_closed(self):
        for title in ("AMBIGUOUS_FIRST", "AMBIGUOUS_SECOND"):
            self.client.command(
                "add", "note", "--key", "ambiguous", "--title", title
            )
        self.client.command(
            "add",
            "ongo-web",
            "--key",
            "ambiguous",
            "--title",
            "AMBIGUOUS_NAVIGATION",
        )
        output = self.root / "site"
        output.mkdir()
        (output / "sentinel.txt").write_text("previous site", encoding="utf-8")

        with self.assertRaises(OngoError) as raised:
            site.build(self.args(output))

        self.assertEqual(raised.exception.code, "publication-conflict")
        self.assertEqual((output / "sentinel.txt").read_text(), "previous site")

    def test_mixed_build_replaces_output_symlink_and_can_rebuild(self):
        publication = self.add_note(
            "symlink", "SYMLINK_SECRET_TITLE", "# SYMLINK_SECRET_BODY"
        )
        created = create_key(
            self.client,
            label="Symlink",
            scope="empty",
            keyring_path=self.keyring,
        )
        grant_key(self.client, created["key_id"], publication)
        symlink_target = self.root / "original-target"
        symlink_target.mkdir()
        (symlink_target / "sentinel.txt").write_text(
            "external target remains", encoding="utf-8"
        )
        output = self.root / "site-link"
        output.symlink_to(symlink_target, target_is_directory=True)

        for _iteration in range(2):
            with mock.patch.object(
                site, "vendor_katex", return_value=False
            ), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(site.build(self.args(output)), 0)
            self.assertFalse(output.is_symlink())
            self.assertTrue((output / "assets" / "ongo-sealed.json").is_file())

        self.assertEqual(
            (symlink_target / "sentinel.txt").read_text(),
            "external target remains",
        )
        self.assertEqual(list(self.root.glob(".site-link.old-*")), [])

    def test_public_build_remains_plaintext_and_needs_no_keys(self):
        self.add_note(
            "public",
            "PUBLIC_BASELINE_TITLE",
            "# PUBLIC_BASELINE_BODY\n\n"
            "<details><summary>PUBLIC_DISCLOSURE</summary>\n\n"
            "Public details.\n\n</details>\n\n"
            "<img src=x onerror=PUBLIC_XSS_SENTINEL>\n\n"
            "<script>PUBLIC_SCRIPT_SENTINEL</script>\n\n"
            "[unsafe](javascript:PUBLIC_SCHEME_SENTINEL)\n",
        )
        # A registered but unassigned key does not change this resource's
        # access: protection belongs to the resource, not to the whole site.
        create_key(
            self.client,
            label="Unassigned key",
            scope="empty",
            keyring_path=self.keyring,
        )
        output = self.root / "public-site"
        with mock.patch.object(site, "vendor_katex", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(site.build(self.args(output)), 0)
        tree = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in output.rglob("*")
            if path.is_file()
        )
        self.assertIn("PUBLIC_BASELINE_TITLE", tree)
        self.assertIn("PUBLIC_BASELINE_BODY", tree)
        self.assertIn("<details><summary>PUBLIC_DISCLOSURE</summary>", tree)
        self.assertIn('<img src="x">', tree)
        self.assertNotIn("onerror", tree)
        self.assertNotIn("PUBLIC_SCRIPT_SENTINEL", tree)
        self.assertNotIn("javascript:PUBLIC_SCHEME_SENTINEL", tree)
        self.assertFalse((output / "assets" / "ongo-sealed.json").exists())

    def test_public_build_retains_title_fallback_when_body_is_unreadable(self):
        publication = self.client.command(
            "add",
            "note",
            "--key",
            "missing-public-body",
            "--title",
            "PUBLIC_TITLE_FALLBACK",
        ).stdout.strip()
        self.client.command(
            "add",
            "ongo-web",
            "--key",
            publication,
            "--title",
            "PUBLIC_TITLE_FALLBACK",
        )
        output = self.root / "public-fallback"

        with mock.patch.object(
            site, "ken_show_record", return_value=None
        ), mock.patch.object(
            site, "vendor_katex", return_value=False
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(site.build(self.args(output)), 0)

        tree = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in output.rglob("*")
            if path.is_file()
        )
        self.assertIn("PUBLIC_TITLE_FALLBACK", tree)
        self.assertFalse((output / "assets" / "ongo-sealed.json").exists())

    def test_public_digest_retains_title_fallback_when_note_is_unreadable(self):
        digest = self.client.command(
            "add",
            "ongo-digest",
            "--key",
            "unreadable-digest",
            "--title",
            "DIGEST_TITLE_FALLBACK",
        ).stdout.strip()
        self.client.command(
            "add", "ongo-web", "--key", digest, "--title", "DIGEST_TITLE_FALLBACK"
        )
        output = self.root / "digest-fallback"

        with mock.patch.object(
            site, "ken_show_record", return_value=None
        ), mock.patch.object(
            site, "vendor_katex", return_value=False
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(site.build(self.args(output)), 0)

        tree = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in output.rglob("*")
            if path.is_file()
        )
        self.assertIn("DIGEST_TITLE_FALLBACK", tree)
        self.assertFalse((output / "assets" / "ongo-sealed.json").exists())

    def add_digest_with_body(self, key, title, body):
        loaded = self.client.load(
            {
                "publications": [
                    {
                        "ref": "source",
                        "kind": "note",
                        "key": f"{key}:source",
                        "title": f"{title} source",
                    },
                    {
                        "ref": "digest",
                        "kind": "ongo-digest",
                        "key": key,
                        "title": title,
                    },
                ],
                "relationships": [
                    {"subject": "digest", "object": "source", "kind": "related-to"}
                ],
                "notes": [{"publication": "source", "body": body}],
            }
        )
        return loaded["refs"]["digest"], loaded["refs"]["source"]

    def test_public_digest_cannot_copy_a_protected_note_body(self):
        digest, note = self.add_digest_with_body(
            "protected-source-digest",
            "Public digest must not leak",
            "DIGEST_PROJECTION_SECRET_BODY",
        )
        key = create_key(
            self.client,
            label="Digest source readers",
            scope="empty",
            keyring_path=self.keyring,
        )
        grant_key(self.client, key["key_id"], note)
        output = self.root / "digest-policy-conflict"
        output.mkdir()
        (output / "sentinel.txt").write_text("previous site", encoding="utf-8")

        with self.assertRaises(OngoError) as raised:
            site.build(self.args(output))

        self.assertEqual(raised.exception.code, "derived-access-policy-conflict")
        self.assertEqual((output / "sentinel.txt").read_text(), "previous site")
        self.assertNotIn(
            "DIGEST_PROJECTION_SECRET_BODY",
            "\n".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in output.rglob("*")
                if path.is_file()
            ),
        )

    def test_digest_with_compatible_access_encrypts_protected_source(self):
        digest, note = self.add_digest_with_body(
            "safe-protected-digest",
            "Protected digest",
            "DIGEST_ENCRYPTED_SOURCE_BODY",
        )
        key = create_key(
            self.client,
            label="Compatible digest readers",
            scope="empty",
            keyring_path=self.keyring,
        )
        grant_key(self.client, key["key_id"], note)
        grant_key(self.client, key["key_id"], digest)
        output = self.root / "protected-digest"

        with mock.patch.object(
            site, "vendor_katex", return_value=False
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(site.build(self.args(output)), 0)

        tree = b"\n".join(
            path.read_bytes() for path in output.rglob("*") if path.is_file()
        )
        self.assertNotIn(b"DIGEST_ENCRYPTED_SOURCE_BODY", tree)
        manifest = json.loads(
            (output / "assets" / "ongo-sealed.json").read_text(encoding="utf-8")
        )
        entry = next(
            item for item in manifest["resources"] if item["collection"] == "digest"
        )
        envelope = json.loads(
            (output / entry["envelope"]).read_text(encoding="utf-8")
        )
        payload = self.decrypt(envelope, key["key_id"])
        self.assertIn("DIGEST_ENCRYPTED_SOURCE_BODY", payload["html"])

    def test_browser_client_avoids_es2021_only_helpers(self):
        client = (PLUGIN_ROOT / "lib" / "ongo" / "assets" / "sealed.js").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("replaceAll", client)
        self.assertNotIn("Object.hasOwn", client)

    @unittest.skipUnless(shutil.which("node"), "Node is required for Web Crypto interop")
    def test_browser_crypto_decrypts_and_deduplicates_multi_key_resource(self):
        self.build_fixture()
        output = self.root / "site"
        with mock.patch.object(site, "vendor_katex", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            site.build(self.args(output))
        manifest = json.loads((output / "assets" / "ongo-sealed.json").read_text())
        entry = next(
            item
            for item in manifest["resources"]
            if item["collection"] == "article"
            and len(
                json.loads((output / item["envelope"]).read_text()).get(
                    "variants", []
                )
            )
            == 2
        )
        envelope_path = output / entry["envelope"]
        _, keyring = load_keyring(self.keyring)
        capabilities = ["ongo-key-v1." + item["secret"] for item in keyring["keys"]]
        script = r"""
import fs from "node:fs";
import {pathToFileURL} from "node:url";
const client = await import(pathToFileURL(process.env.CLIENT));
const envelope = JSON.parse(fs.readFileSync(process.env.ENVELOPE, "utf8"));
const capabilities = JSON.parse(process.env.CAPABILITIES);
const result = await client.decryptEnvelope(envelope, [
  {label: "Alpha", capability: capabilities[0]},
  {label: "Beta", capability: capabilities[1]},
], {resource_id: envelope.resource_id, collection: envelope.collection});
if (!result || result.payload.title !== "ARTICLE_ONE_SECRET_TITLE") process.exit(10);
if (result.unlockedBy.join(",") !== "Alpha,Beta") process.exit(11);
const wrong = await client.decryptEnvelope(envelope, [
  {label: "Wrong", capability: "ongo-key-v1." + "A".repeat(43)},
], {resource_id: envelope.resource_id, collection: envelope.collection});
if (wrong !== null) process.exit(12);
let mismatchRejected = false;
try {
  await client.decryptEnvelope(envelope, [], {
    resource_id: "0".repeat(32),
    collection: envelope.collection,
  });
} catch (_error) {
  mismatchRejected = true;
}
if (!mismatchRejected) process.exit(13);
Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  value: {
    getItem() { throw new DOMException("Storage is disabled", "SecurityError"); },
    setItem() { throw new DOMException("Storage is disabled", "SecurityError"); },
  },
});
const sessionCapability = capabilities[0];
const persisted = client.saveKnownKeys([
  {label: "Session only", capability: sessionCapability},
]);
const sessionKeys = client.loadKnownKeys();
if (persisted || sessionKeys.length !== 1 || sessionKeys[0].label !== "Session only") {
  process.exit(14);
}
const publicEnvelope = {
  schema_version: 1,
  resource_id: "public-resource",
  collection: "article",
  public: {
    schema_version: 1,
    resource_id: "public-resource",
    collection: "article",
    title: "Public without crypto",
    date: "",
    tags: [],
    format: "html",
    html: "<article><p>public</p></article>",
  },
};
Object.defineProperty(globalThis, "crypto", {configurable: true, value: undefined});
const publicResult = await client.decryptEnvelope(publicEnvelope, [], {
  resource_id: "public-resource",
  collection: "article",
});
if (!publicResult || publicResult.payload.title !== "Public without crypto") {
  process.exit(15);
}
Object.defineProperty(globalThis, "document", {
  configurable: true,
  value: {body: {dataset: {assetPrefix: ""}}},
});
let fetchCalls = 0;
Object.defineProperty(globalThis, "fetch", {
  configurable: true,
  value: async () => {
    fetchCalls += 1;
    if (fetchCalls === 1) return {ok: false, status: 503};
    return {ok: true, json: async () => ({recovered: true})};
  },
});
const retryEntry = {resource_id: "retry-resource", envelope: "retry.json"};
let firstFetchFailed = false;
try {
  await client.fetchEnvelope(retryEntry);
} catch (_error) {
  firstFetchFailed = true;
}
const retriedEnvelope = await client.fetchEnvelope(retryEntry);
if (!firstFetchFailed || fetchCalls !== 2 || !retriedEnvelope.recovered) {
  process.exit(16);
}
console.log(JSON.stringify({
  title: result.payload.title,
  unlockedBy: result.unlockedBy,
  sessionFallback: sessionKeys[0].label,
  publicWithoutCrypto: publicResult.payload.title,
  fetchCalls,
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "CLIENT": str(PLUGIN_ROOT / "lib" / "ongo" / "assets" / "sealed.js"),
                "ENVELOPE": str(envelope_path),
                "CAPABILITIES": json.dumps(capabilities),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["unlockedBy"], ["Alpha", "Beta"])

    @unittest.skipUnless(find_chrome(), "Chrome or Chromium is required")
    def test_protected_display_math_reaches_katex_in_display_mode(self):
        publication_id = self.add_note(
            "display-math",
            "Protected display math",
            "# Protected display math\n\n$$x^2 + y^2 = z^2$$\n",
        )
        created = create_key(
            self.client,
            label="Math reader",
            scope="empty",
            keyring_path=self.keyring,
        )
        grant_key(self.client, created["key_id"], publication_id)
        output = self.root / "math-site"

        def fake_vendor(work_dir, _log):
            katex = Path(work_dir) / "assets" / "katex"
            katex.mkdir(parents=True)
            katex.joinpath("katex.min.css").write_text("", encoding="utf-8")
            katex.joinpath("katex.min.js").write_text(
                "window.katex={render:function(tex,el,opts){"
                "el.setAttribute('data-rendered-tex',tex);"
                "el.setAttribute('data-rendered-display',String(opts.displayMode));"
                "el.textContent='KATEX_STUB:'+tex;}};",
                encoding="utf-8",
            )
            return True

        with mock.patch.object(
            site, "vendor_katex", side_effect=fake_vendor
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(site.build(self.args(output)), 0)

        manifest = json.loads(
            (output / "assets" / "ongo-sealed.json").read_text(encoding="utf-8")
        )
        entry = manifest["resources"][0]
        bootstrap = output / "assets" / "test-key.js"
        bootstrap.write_text(
            "localStorage.setItem('ongo-sealed-keys-v1',"
            + json.dumps(
                json.dumps(
                    [{"label": "Math reader", "capability": created["capability"]}]
                )
            )
            + ");\n",
            encoding="utf-8",
        )
        item = output / entry["page"]
        item.write_text(
            item.read_text(encoding="utf-8").replace(
                '<script type="module" src="../assets/ongo-sealed.js"></script>',
                '<script src="../assets/test-key.js"></script>\n'
                '<script type="module" src="../assets/ongo-sealed.js"></script>',
            ),
            encoding="utf-8",
        )

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, _format, *_arguments):
                return

        handler = lambda *args, **kwargs: QuietHandler(
            *args, directory=str(output), **kwargs
        )
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            profile = self.root / "chrome-profile"
            result = subprocess.run(
                [
                    find_chrome(),
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-background-networking",
                    "--no-first-run",
                    f"--user-data-dir={profile}",
                    "--virtual-time-budget=3000",
                    "--dump-dom",
                    f"http://127.0.0.1:{server.server_port}/{entry['page']}",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('data-rendered-tex="x^2 + y^2 = z^2"', result.stdout)
        self.assertIn('data-rendered-display="true"', result.stdout)


if __name__ == "__main__":
    unittest.main()
