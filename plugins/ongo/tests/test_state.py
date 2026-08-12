#!/usr/bin/env python3
"""Durable loop-state migration regression tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "lib"))

from ongo.errors import OngoError
from ongo import state as state_module
from ongo.state import (
    LEGACY_TOMBSTONE_SCHEMA,
    MIGRATION_RECORD_SCHEMA,
    _normal_interval_minutes,
    migrate_legacy_agent_state,
)


class StateMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.legacy = self.root / "ongo_state.json"
        self.target = self.root / "plugin-data" / "agent-state.json"

    def tearDown(self):
        self.temporary.cleanup()

    def test_flat_claude_state_is_migrated_and_tombstoned(self):
        self.legacy.write_text(
            json.dumps(
                {
                    "channel": "C123",
                    "last_user_ts": "1700000000.000001",
                    "last_self_improve": 11,
                    "last_arxiv_daily": 12,
                    "rotation": "survey",
                    "idle": True,
                    "ken": "/data/ken",
                    "cron_id": "cron-current",
                    "prev_cron_id": "cron-previous",
                    "cron_created": 13,
                    "normal_cron": "7,37 * * * *",
                    "mode": "fast",
                    "fast_idle_polls": 2,
                }
            ),
            encoding="utf-8",
        )

        result = migrate_legacy_agent_state(
            self.target, legacy=self.legacy, now=99
        )

        self.assertEqual(result["status"], "migrated")
        state = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(state["channel"], "C123")
        self.assertEqual(state["last_user_ts"], "1700000000.000001")
        self.assertEqual(state["scheduler"]["id"], "cron-current")
        self.assertEqual(state["scheduler"]["previous_id"], "cron-previous")
        self.assertEqual(state["scheduler"]["normal_interval_minutes"], 30)
        self.assertEqual(state["scheduler"]["normal_cron"], "7,37 * * * *")
        self.assertTrue(state["scheduler"]["needs_prompt_upgrade"])
        tombstone = json.loads(self.legacy.read_text(encoding="utf-8"))
        self.assertEqual(tombstone["schema"], LEGACY_TOMBSTONE_SCHEMA)
        self.assertEqual(tombstone["migrated_to"], str(self.target))

    def test_fixed_cron_cadence_uses_minute_and_hour_fields(self):
        cases = {
            "* * * * *": 1,
            "7,37 * * * *": 30,
            "0 */2 * * *": 120,
            "0 9 * * *": 1440,
        }
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assertEqual(_normal_interval_minutes(expression), expected)

    def test_legacy_numeric_strings_are_coerced_and_blank_cron_defaults(self):
        self.legacy.write_text(
            json.dumps(
                {
                    "channel": "C123",
                    "last_user_ts": "1.0",
                    "last_self_improve": "11",
                    "last_arxiv_daily": "12",
                    "cron_created": "13",
                    "fast_idle_polls": "2",
                    "normal_cron": "   ",
                }
            ),
            encoding="utf-8",
        )
        migrate_legacy_agent_state(self.target, legacy=self.legacy)
        migrated = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(migrated["last_self_improve"], 11)
        self.assertEqual(migrated["last_arxiv_daily"], 12)
        self.assertEqual(migrated["scheduler"]["created"], 13)
        self.assertEqual(migrated["scheduler"]["fast_idle_polls"], 2)
        self.assertEqual(migrated["scheduler"]["normal_cron"], "7,37 * * * *")

    def test_tombstone_failure_rolls_back_new_target(self):
        self.legacy.write_text(
            '{"channel":"C123","last_user_ts":"1.0"}\n', encoding="utf-8"
        )
        atomic_json = state_module._atomic_json

        def fail_tombstone(path, payload):
            if Path(path) == self.legacy:
                raise OSError("read only")
            atomic_json(path, payload)

        with mock.patch.object(state_module, "_atomic_json", side_effect=fail_tombstone):
            with self.assertRaises(OngoError) as raised:
                migrate_legacy_agent_state(self.target, legacy=self.legacy)
        self.assertEqual(raised.exception.code, "legacy-state-migration-failed")
        self.assertFalse(self.target.exists())
        self.assertEqual(
            json.loads(self.legacy.read_text(encoding="utf-8"))["channel"], "C123"
        )

    def test_interrupted_migration_is_recovered_from_target_record(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text(
            json.dumps(
                {
                    "migration": {
                        "schema": MIGRATION_RECORD_SCHEMA,
                        "from": str(self.legacy),
                        "migrated_at": 41,
                    },
                    "scheduler": {"id": "cron-current"},
                }
            ),
            encoding="utf-8",
        )
        self.legacy.write_text(
            '{"channel":"C123","last_user_ts":"1.0"}\n', encoding="utf-8"
        )
        result = migrate_legacy_agent_state(self.target, legacy=self.legacy)
        self.assertEqual(result["status"], "migrated")
        self.assertTrue(result["recovered"])
        tombstone = json.loads(self.legacy.read_text(encoding="utf-8"))
        self.assertEqual(tombstone["schema"], LEGACY_TOMBSTONE_SCHEMA)
        self.assertEqual(tombstone["scheduler_id"], "cron-current")

    def test_existing_new_state_is_never_overwritten(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('{"sentinel":true}\n', encoding="utf-8")
        self.legacy.write_text(
            '{"channel":"C123","last_user_ts":"1.0"}\n', encoding="utf-8"
        )
        result = migrate_legacy_agent_state(self.target, legacy=self.legacy)
        self.assertEqual(result["status"], "existing")
        self.assertEqual(
            json.loads(self.target.read_text(encoding="utf-8")),
            {"sentinel": True},
        )

    def test_invalid_legacy_state_is_not_tombstoned(self):
        self.legacy.write_text("[]\n", encoding="utf-8")
        with self.assertRaises(OngoError) as raised:
            migrate_legacy_agent_state(self.target, legacy=self.legacy)
        self.assertEqual(raised.exception.code, "legacy-state-invalid")
        self.assertFalse(self.target.exists())
        self.assertEqual(self.legacy.read_text(encoding="utf-8"), "[]\n")


if __name__ == "__main__":
    unittest.main()
