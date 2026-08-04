#!/usr/bin/env python3
"""Integration tests for the Ken-backed experiment protocol."""

from __future__ import annotations

import json
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "lib"))

from ongo.errors import OngoError
from ongo import site
from ongo.experiments import (
    approve_experiment,
    artifact_envelope,
    begin_experiment,
    cancel_attempt,
    create_delegation,
    create_experiment,
    current_spend,
    finish_attempt,
    markdown_view,
    read_artifact_specs,
    retry_condition,
    run_local,
    state_for_experiment,
    verify_experiment,
)
from ongo.ken import KenClient
from ongo.site import KenView, fetch_publication, resolve_source


class Args:
    pass


@unittest.skipUnless(shutil.which("ken"), "Ken v3 is required")
class ExperimentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.client = KenClient(binary=shutil.which("ken"), db=str(self.root / "ken.db"))
        self.client.initialize()
        self.client.ensure_kinds()

    def tearDown(self):
        self.temporary.cleanup()

    def write_plan(self, conditions, title="Protocol"):
        document = self.root / "plan.md"
        manifest = self.root / "manifest.json"
        document.write_text(f"# {title}\n\nRun every declared condition.\n", encoding="utf-8")
        manifest.write_text(
            json.dumps({"schema_version": 1, "title": title, "conditions": conditions}),
            encoding="utf-8",
        )
        return document, manifest

    def manual_condition(self, condition_id="manual", cost="0", runs=1):
        return {
            "id": condition_id,
            "description": f"Run {condition_id}",
            "required_runs": runs,
            "expected_cost_usd": cost,
            "required_artifacts": ["observation"],
            "execution": {"mode": "manual"},
        }

    def create(self, conditions, title="Protocol"):
        document, manifest = self.write_plan(conditions, title)
        return create_experiment(self.client, str(document), str(manifest))

    def approve_free(self, experiment_id):
        return approve_experiment(self.client, experiment_id, None, "driver", "driver")

    def delegation(
        self,
        *,
        max_per="10",
        max_total=None,
        modes=None,
        experiment=None,
        seconds=86400,
        evidence="claude-task:explicit-grant",
    ):
        args = Args()
        args.max_per_experiment_usd = max_per
        args.max_total_usd = max_total
        args.expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=seconds)
        ).isoformat()
        args.mode = modes
        args.experiment = experiment
        args.granted_by = "human-owner"
        args.evidence = evidence
        return create_delegation(self.client, args)["delegation"]

    def test_manual_lifecycle_is_idempotent_and_exact(self):
        created = self.create([self.manual_condition(runs=2)])
        experiment_id = created["experiment"]["experiment_id"]
        repeated = self.create([self.manual_condition(runs=2)])
        self.assertEqual(repeated["experiment"]["experiment_id"], experiment_id)
        self.approve_free(experiment_id)

        first = begin_experiment(self.client, experiment_id, "worker-a")
        recovered = begin_experiment(self.client, experiment_id, "worker-b")
        self.assertEqual(first["attempt"]["attempt_id"], recovered["attempt"]["attempt_id"])

        artifact = artifact_envelope(
            "observation", "observation.txt", "text/plain", b"ok"
        )
        result = {
            "schema_version": 1,
            "status": "completed",
            "valid_observation": True,
            "summary": "observed",
            "metrics": {"score": 1},
            "actual_cost_usd": "0",
        }
        finished = finish_attempt(
            self.client, first["attempt"]["attempt_id"], result, {"observation": artifact}
        )
        repeated_finish = finish_attempt(
            self.client, first["attempt"]["attempt_id"], result, {"observation": artifact}
        )
        self.assertEqual(finished["record_id"], repeated_finish["record_id"])

        second = begin_experiment(self.client, experiment_id, "worker-b")
        finish_attempt(
            self.client, second["attempt"]["attempt_id"], result, {"observation": artifact}
        )
        status, exit_code = verify_experiment(self.client, experiment_id)
        self.assertEqual(exit_code, 0)
        self.assertTrue(status["complete"])
        self.assertEqual(status["valid_runs"], 2)

    def test_manifest_render_includes_exact_local_execution_contract(self):
        condition = {
            "id": "local",
            "description": "Capture the exact invocation",
            "required_runs": 1,
            "expected_cost_usd": "0",
            "required_artifacts": ["stdout", "evidence"],
            "execution": {
                "mode": "local",
                "argv": [sys.executable, "-c", "print('exact')"],
                "cwd": str(self.root),
                "env": {"EXACT_VALUE": "yes"},
                "timeout_seconds": 7,
                "accepted_exit_codes": [0, 3],
                "output_files": [{"name": "evidence", "path": "out.txt"}],
            },
        }
        created = self.create([condition])
        rendered = markdown_view(self.client, created["experiment"]["experiment_id"])
        self.assertIn("## Canonical manifest", rendered)
        self.assertIn('"EXACT_VALUE": "yes"', rendered)
        self.assertIn('"accepted_exit_codes": [', rendered)
        self.assertIn('"evidence"', rendered)

    def test_invalid_manifest_is_atomic(self):
        condition = self.manual_condition()
        condition["unknown"] = True
        document, manifest = self.write_plan([condition])
        before = len(self.client.list_kind("ongo-experiment"))
        with self.assertRaises(OngoError) as raised:
            create_experiment(self.client, str(document), str(manifest))
        self.assertEqual(raised.exception.exit_code, 2)
        self.assertEqual(len(self.client.list_kind("ongo-experiment")), before)

    def test_non_finite_json_numbers_are_rejected_before_creation(self):
        condition = {
            "id": "local",
            "description": "Must never begin",
            "required_runs": 1,
            "expected_cost_usd": "0",
            "required_artifacts": ["stdout", "stderr"],
            "execution": {
                "mode": "local",
                "argv": [sys.executable, "-c", "print('should not run')"],
                "cwd": str(self.root),
                "env": {},
                "timeout_seconds": 7,
                "accepted_exit_codes": [0],
                "output_files": [],
            },
        }
        document, manifest = self.write_plan([condition])
        base = manifest.read_text(encoding="utf-8")
        for literal in (
            "1e999",
            "NaN",
            "Infinity",
            "-Infinity",
            "1" + ("0" * 400),
        ):
            with self.subTest(literal=literal):
                manifest.write_text(
                    base.replace('"timeout_seconds": 7', f'"timeout_seconds": {literal}'),
                    encoding="utf-8",
                )
                with self.assertRaises(OngoError) as raised:
                    create_experiment(self.client, str(document), str(manifest))
                self.assertEqual(raised.exception.exit_code, 2)
                self.assertEqual(self.client.list_kind("ongo-experiment"), [])

    def test_changed_protocol_is_a_successor_and_does_not_reuse_approval(self):
        first = self.create([self.manual_condition()], "First")
        first_id = first["experiment"]["experiment_id"]
        self.approve_free(first_id)
        attempt = begin_experiment(self.client, first_id, "worker")
        self.assertTrue(state_for_experiment(self.client, first_id)["plan_frozen"])

        document, manifest = self.write_plan(
            [self.manual_condition(condition_id="changed")], "Changed"
        )
        successor = create_experiment(
            self.client, str(document), str(manifest), successor_of=first_id
        )
        successor_body = successor["experiment"]
        self.assertEqual(successor_body["successor_of"], first_id)
        self.assertFalse(successor["status"]["approved"])
        with self.assertRaises(OngoError) as raised:
            begin_experiment(self.client, successor_body["experiment_id"], "worker")
        self.assertEqual(raised.exception.exit_code, 5)
        cancel_attempt(self.client, attempt["attempt"]["attempt_id"], "test cleanup")

    def test_invalid_initial_attempt_requires_explicit_retry(self):
        created = self.create([self.manual_condition()])
        experiment_id = created["experiment"]["experiment_id"]
        self.approve_free(experiment_id)
        attempt = begin_experiment(self.client, experiment_id, "worker")
        finish_attempt(
            self.client,
            attempt["attempt"]["attempt_id"],
            {
                "schema_version": 1,
                "status": "failed",
                "valid_observation": False,
                "summary": "failed",
                "metrics": {},
                "actual_cost_usd": "0",
            },
            {},
        )
        with self.assertRaises(OngoError) as raised:
            begin_experiment(self.client, experiment_id, "worker")
        self.assertEqual(raised.exception.exit_code, 6)
        retry = retry_condition(self.client, experiment_id, "manual", "worker")
        self.assertTrue(retry["attempt"]["retry"])

    def test_condition_order_cancellation_retry_and_invalid_counts(self):
        created = self.create(
            [
                self.manual_condition("first"),
                self.manual_condition("second"),
            ]
        )
        experiment_id = created["experiment"]["experiment_id"]
        self.approve_free(experiment_id)
        first = begin_experiment(self.client, experiment_id, "worker")
        self.assertEqual(first["attempt"]["condition_id"], "first")
        cancel_attempt(self.client, first["attempt"]["attempt_id"], "operator cancelled")
        second = begin_experiment(self.client, experiment_id, "worker")
        self.assertEqual(second["attempt"]["condition_id"], "second")
        finish_attempt(
            self.client,
            second["attempt"]["attempt_id"],
            {
                "schema_version": 1,
                "status": "completed",
                "valid_observation": False,
                "summary": "ran but invalid",
                "metrics": {},
                "actual_cost_usd": "0",
            },
            {},
        )
        status, exit_code = verify_experiment(self.client, experiment_id)
        self.assertEqual(exit_code, 6)
        self.assertEqual(status["cancelled_attempts"], 1)
        self.assertEqual(status["invalid_observations"], 2)
        self.assertEqual(status["remaining_runs"], 2)
        retry = retry_condition(self.client, experiment_id, "first", "worker")
        self.assertTrue(retry["attempt"]["retry"])

    def test_finish_conflict_and_missing_artifact_leave_one_open_attempt(self):
        created = self.create([self.manual_condition()])
        experiment_id = created["experiment"]["experiment_id"]
        self.approve_free(experiment_id)
        attempt = begin_experiment(self.client, experiment_id, "worker")["attempt"]
        with self.assertRaises(OngoError) as raised:
            finish_attempt(
                self.client,
                attempt["attempt_id"],
                {
                    "schema_version": 1,
                    "status": "completed",
                    "valid_observation": True,
                    "summary": "missing evidence",
                    "metrics": {},
                    "actual_cost_usd": "0",
                },
                {},
            )
        self.assertEqual(raised.exception.exit_code, 2)
        self.assertEqual(len(self.client.list_kind("ongo-experiment-result")), 0)
        self.assertEqual(state_for_experiment(self.client, experiment_id)["open_attempts"], 1)

        artifact = artifact_envelope("observation", "observation.txt", None, b"ok")
        result = {
            "schema_version": 1,
            "status": "completed",
            "valid_observation": True,
            "summary": "first terminal result",
            "metrics": {},
            "actual_cost_usd": "0",
        }
        finish_attempt(self.client, attempt["attempt_id"], result, {"observation": artifact})
        changed = {**result, "summary": "different terminal result"}
        with self.assertRaises(OngoError) as raised:
            finish_attempt(self.client, attempt["attempt_id"], changed, {"observation": artifact})
        self.assertEqual(raised.exception.exit_code, 4)
        self.assertEqual(len(self.client.list_kind("ongo-experiment-result")), 1)

    def test_unreadable_artifact_spec_does_not_mutate_ken(self):
        created = self.create([self.manual_condition()])
        experiment_id = created["experiment"]["experiment_id"]
        self.approve_free(experiment_id)
        begin_experiment(self.client, experiment_id, "worker")
        with self.assertRaises(OngoError):
            read_artifact_specs([f"observation={self.root / 'missing.bin'}"])
        self.assertEqual(len(self.client.list_kind("ongo-experiment-result")), 0)
        self.assertEqual(len(self.client.list_kind("ongo-experiment-artifact")), 0)
        self.assertEqual(state_for_experiment(self.client, experiment_id)["open_attempts"], 1)

    def test_paid_plan_uses_driver_delegation_and_budget(self):
        created = self.create([self.manual_condition(cost="2.5")])
        experiment_id = created["experiment"]["experiment_id"]
        with self.assertRaises(OngoError) as missing:
            approve_experiment(self.client, experiment_id, None, "driver", "driver")
        self.assertEqual(missing.exception.exit_code, 5)

        args = Args()
        args.max_per_experiment_usd = "3"
        args.max_total_usd = "10"
        args.expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        args.mode = ["manual"]
        args.experiment = None
        args.granted_by = "human-owner"
        args.evidence = "claude-task:approval-message"
        delegation = create_delegation(self.client, args)["delegation"]

        with self.assertRaises(OngoError) as worker:
            approve_experiment(
                self.client, experiment_id, delegation["delegation_id"], "worker", "worker"
            )
        self.assertEqual(worker.exception.exit_code, 5)
        approved = approve_experiment(
            self.client, experiment_id, delegation["delegation_id"], "driver", "driver"
        )
        self.assertEqual(approved["approval"]["delegation_id"], delegation["delegation_id"])
        begin_experiment(self.client, experiment_id, "worker")

    def test_expired_restricted_and_excess_delegations_are_rejected(self):
        first = self.create([self.manual_condition(cost="2")], "Paid first")
        first_id = first["experiment"]["experiment_id"]
        second = self.create([self.manual_condition(cost="1")], "Paid second")
        second_id = second["experiment"]["experiment_id"]

        excess = self.delegation(max_per="1")
        with self.assertRaises(OngoError) as raised:
            approve_experiment(
                self.client, first_id, excess["delegation_id"], "driver", "driver"
            )
        self.assertEqual(raised.exception.exit_code, 5)

        restricted = self.delegation(experiment=first_id)
        with self.assertRaises(OngoError) as raised:
            approve_experiment(
                self.client, second_id, restricted["delegation_id"], "driver", "driver"
            )
        self.assertEqual(raised.exception.exit_code, 5)

        expiring = self.delegation(seconds=0.05)
        import time

        time.sleep(0.08)
        with self.assertRaises(OngoError) as raised:
            approve_experiment(
                self.client, first_id, expiring["delegation_id"], "driver", "driver"
            )
        self.assertEqual(raised.exception.exit_code, 5)

    def test_cumulative_budget_uses_expected_cost_when_actual_is_omitted(self):
        first = self.create([self.manual_condition(cost="1")], "Budget one")
        second = self.create([self.manual_condition(cost="1")], "Budget two")
        delegation = self.delegation(max_per="2", max_total="1.5")
        for experiment in (first, second):
            approve_experiment(
                self.client,
                experiment["experiment"]["experiment_id"],
                delegation["delegation_id"],
                "driver",
                "driver",
            )

        first_id = first["experiment"]["experiment_id"]
        attempt = begin_experiment(self.client, first_id, "worker-a")
        finish_attempt(
            self.client,
            attempt["attempt"]["attempt_id"],
            {
                "schema_version": 1,
                "status": "completed",
                "valid_observation": True,
                "summary": "actual unavailable",
                "metrics": {},
            },
            {"observation": artifact_envelope("observation", "one.txt", None, b"one")},
        )
        self.assertEqual(current_spend(self.client, first_id), Decimal("1"))
        with self.assertRaises(OngoError) as raised:
            begin_experiment(
                self.client, second["experiment"]["experiment_id"], "worker-b"
            )
        self.assertEqual(raised.exception.exit_code, 5)

    def test_open_attempt_reserves_cumulative_delegation_budget(self):
        first = self.create([self.manual_condition(cost="1")], "Open one")
        second = self.create([self.manual_condition(cost="1")], "Open two")
        delegation = self.delegation(max_per="2", max_total="1.5")
        for experiment in (first, second):
            approve_experiment(
                self.client,
                experiment["experiment"]["experiment_id"],
                delegation["delegation_id"],
                "driver",
                "driver",
            )
        begin_experiment(
            self.client, first["experiment"]["experiment_id"], "worker-a"
        )
        with self.assertRaises(OngoError) as raised:
            begin_experiment(
                self.client, second["experiment"]["experiment_id"], "worker-b"
            )
        self.assertEqual(raised.exception.exit_code, 5)

    def test_cancelled_attempt_retains_expected_budget_reservation(self):
        created = self.create([self.manual_condition(cost="1")], "Cancelled spend")
        experiment_id = created["experiment"]["experiment_id"]
        delegation = self.delegation(max_per="1.5", max_total="1.5")
        approved = approve_experiment(
            self.client,
            experiment_id,
            delegation["delegation_id"],
            "driver",
            "driver",
        )
        attempt = begin_experiment(self.client, experiment_id, "worker")
        cancelled = cancel_attempt(
            self.client,
            attempt["attempt"]["attempt_id"],
            "interrupted after paid work",
        )
        self.assertIsNone(cancelled["result"]["actual_cost_usd"])
        self.assertEqual(current_spend(self.client, experiment_id), Decimal("1"))
        repeated = approve_experiment(
            self.client,
            experiment_id,
            delegation["delegation_id"],
            "driver",
            "driver",
        )
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(
            repeated["approval"]["approval_id"],
            approved["approval"]["approval_id"],
        )
        with self.assertRaises(OngoError) as raised:
            retry_condition(self.client, experiment_id, "manual", "worker")
        self.assertEqual(raised.exception.exit_code, 5)

    def test_approver_and_worker_labels_are_separated(self):
        created = self.create([self.manual_condition()])
        experiment_id = created["experiment"]["experiment_id"]
        self.approve_free(experiment_id)
        with self.assertRaises(OngoError) as raised:
            begin_experiment(self.client, experiment_id, "driver")
        self.assertEqual(raised.exception.exit_code, 5)
        with self.assertRaises(OngoError) as raised:
            approve_experiment(self.client, experiment_id, None, "driver-two", "driver")
        self.assertEqual(raised.exception.exit_code, 4)

        other = self.create([self.manual_condition()], "Already worked")
        other_id = other["experiment"]["experiment_id"]
        # A manually injected zero-cost approval permits a worker to establish
        # the reverse ordering needed to test approval-after-work separation.
        self.client.load(
            {
                "publications": [
                    {
                        "ref": "approval",
                        "kind": "ongo-experiment-approval",
                        "key": f"test-approval:{other_id}",
                        "title": "Test approval",
                    }
                ],
                "relationships": [],
                "notes": [
                    {
                        "publication": "approval",
                        "body": json.dumps(
                            {
                                "schema_version": 1,
                                "approval_id": "test",
                                "experiment_id": other_id,
                                "manifest_sha256": other["experiment"]["manifest_sha256"],
                                "authority": "zero-cost-policy",
                                "actor": "initial-driver",
                            }
                        ),
                    }
                ],
            }
        )
        begin_experiment(self.client, other_id, "later-driver")
        with self.assertRaises(OngoError) as raised:
            approve_experiment(self.client, other_id, None, "later-driver", "driver")
        self.assertEqual(raised.exception.exit_code, 5)

    def test_local_runner_continues_after_failure(self):
        conditions = [
            {
                "id": "fails",
                "description": "Exit unsuccessfully",
                "required_runs": 1,
                "expected_cost_usd": "0",
                "required_artifacts": ["stdout", "stderr"],
                "execution": {
                    "mode": "local",
                    "argv": [sys.executable, "-c", "import sys; print('first'); sys.exit(7)"],
                    "cwd": str(self.root),
                    "env": {},
                    "timeout_seconds": 10,
                    "accepted_exit_codes": [0],
                    "output_files": [],
                },
            },
            {
                "id": "passes",
                "description": "Exit successfully",
                "required_runs": 1,
                "expected_cost_usd": "0",
                "required_artifacts": ["stdout", "stderr"],
                "execution": {
                    "mode": "local",
                    "argv": [sys.executable, "-c", "print('second')"],
                    "cwd": str(self.root),
                    "env": {"ONGO_TEST": "yes"},
                    "timeout_seconds": 10,
                    "accepted_exit_codes": [0],
                    "output_files": [],
                },
            },
        ]
        created = self.create(conditions)
        experiment_id = created["experiment"]["experiment_id"]
        self.approve_free(experiment_id)
        status, exit_code = run_local(self.client, experiment_id)
        self.assertEqual(exit_code, 6)
        self.assertEqual(status["failed_attempts"], 1)
        self.assertEqual(status["conditions"][1]["valid_runs"], 1)

        artifacts = self.client.records("ongo-experiment-artifact")
        names = [json.loads(record["body"])["name"] for record in artifacts]
        self.assertEqual(names.count("stdout"), 2)
        self.assertEqual(names.count("stderr"), 2)

    def test_local_runner_preserves_order_cwd_env_outputs_and_accepted_codes(self):
        record = self.root / "order.txt"
        conditions = []
        for condition_id, value, exit_code, accepted in (
            ("first", "A", 3, [3]),
            ("second", "B", 0, [0]),
        ):
            code = (
                "import os,pathlib,sys; "
                "p=pathlib.Path('order.txt'); "
                "p.write_text((p.read_text() if p.exists() else '')+os.environ['VALUE']); "
                "pathlib.Path('evidence-" + condition_id + ".txt').write_text(os.getcwd()); "
                f"sys.exit({exit_code})"
            )
            conditions.append(
                {
                    "id": condition_id,
                    "description": condition_id,
                    "required_runs": 1,
                    "expected_cost_usd": "0",
                    "required_artifacts": ["stdout", "stderr", "evidence"],
                    "execution": {
                        "mode": "local",
                        "argv": [sys.executable, "-c", code],
                        "cwd": str(self.root),
                        "env": {"VALUE": value},
                        "timeout_seconds": 5,
                        "accepted_exit_codes": accepted,
                        "output_files": [
                            {
                                "name": "evidence",
                                "path": f"evidence-{condition_id}.txt",
                            }
                        ],
                    },
                }
            )
        created = self.create(conditions)
        experiment_id = created["experiment"]["experiment_id"]
        self.approve_free(experiment_id)
        status, exit_code = run_local(self.client, experiment_id)
        self.assertEqual(exit_code, 0)
        self.assertTrue(status["complete"])
        self.assertEqual(record.read_text(encoding="utf-8"), "AB")
        results = [json.loads(row["body"]) for row in self.client.records("ongo-experiment-result")]
        by_condition = {row["condition_id"]: row for row in results}
        self.assertEqual(by_condition["first"]["execution"]["exit_code"], 3)
        self.assertEqual(
            by_condition["first"]["execution"]["cwd"], str(self.root.resolve())
        )
        self.assertEqual(by_condition["first"]["execution"]["env_additions"], {"VALUE": "A"})

    def test_local_runner_records_timeout_and_launch_failure_without_using_shell(self):
        sentinel = self.root / "must-not-exist"
        conditions = [
            {
                "id": "timeout",
                "description": "Times out",
                "required_runs": 1,
                "expected_cost_usd": "0",
                "required_artifacts": ["stdout", "stderr"],
                "execution": {
                    "mode": "local",
                    "argv": [sys.executable, "-c", "import time; time.sleep(1)"],
                    "cwd": str(self.root),
                    "env": {},
                    "timeout_seconds": 0.05,
                    "accepted_exit_codes": [0],
                    "output_files": [],
                },
            },
            {
                "id": "no-shell",
                "description": "A shell-looking argv remains a literal executable",
                "required_runs": 1,
                "expected_cost_usd": "0",
                "required_artifacts": ["stdout", "stderr"],
                "execution": {
                    "mode": "local",
                    "argv": [f"touch {sentinel}"],
                    "cwd": str(self.root),
                    "env": {},
                    "timeout_seconds": 1,
                    "accepted_exit_codes": [0],
                    "output_files": [],
                },
            },
            {
                "id": "invalid-launch",
                "description": "Unexpected subprocess setup errors become results",
                "required_runs": 1,
                "expected_cost_usd": "0",
                "required_artifacts": ["stdout", "stderr"],
                "execution": {
                    "mode": "local",
                    "argv": [f"{sys.executable}\0"],
                    "cwd": str(self.root),
                    "env": {},
                    "timeout_seconds": 1,
                    "accepted_exit_codes": [0],
                    "output_files": [],
                },
            },
            {
                "id": "untouched",
                "description": "Still runs after both failures",
                "required_runs": 1,
                "expected_cost_usd": "0",
                "required_artifacts": ["stdout", "stderr"],
                "execution": {
                    "mode": "local",
                    "argv": [sys.executable, "-c", "print('continued')"],
                    "cwd": str(self.root),
                    "env": {},
                    "timeout_seconds": 1,
                    "accepted_exit_codes": [0],
                    "output_files": [],
                },
            },
        ]
        created = self.create(conditions)
        experiment_id = created["experiment"]["experiment_id"]
        self.approve_free(experiment_id)
        status, exit_code = run_local(self.client, experiment_id)
        self.assertEqual(exit_code, 6)
        self.assertFalse(sentinel.exists())
        self.assertEqual(status["failed_attempts"], 3)
        self.assertEqual(status["open_attempts"], 0)
        self.assertEqual(status["conditions"][3]["valid_runs"], 1)

    def test_local_runner_records_missing_outputs_and_continues(self):
        marker = self.root / "continued.txt"
        conditions = [
            {
                "id": "missing-output",
                "description": "Fails before producing its output",
                "required_runs": 1,
                "expected_cost_usd": "0",
                "required_artifacts": ["stdout", "stderr", "declared"],
                "execution": {
                    "mode": "local",
                    "argv": [sys.executable, "-c", "import sys; sys.exit(9)"],
                    "cwd": str(self.root),
                    "env": {},
                    "timeout_seconds": 1,
                    "accepted_exit_codes": [0],
                    "output_files": [{"name": "declared", "path": "missing.txt"}],
                },
            },
            {
                "id": "continues",
                "description": "Runs after missing evidence",
                "required_runs": 1,
                "expected_cost_usd": "0",
                "required_artifacts": ["stdout", "stderr"],
                "execution": {
                    "mode": "local",
                    "argv": [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('yes')"],
                    "cwd": str(self.root),
                    "env": {},
                    "timeout_seconds": 1,
                    "accepted_exit_codes": [0],
                    "output_files": [],
                },
            },
        ]
        created = self.create(conditions)
        experiment_id = created["experiment"]["experiment_id"]
        self.approve_free(experiment_id)
        status, exit_code = run_local(self.client, experiment_id)
        self.assertEqual(exit_code, 6)
        self.assertEqual(status["open_attempts"], 0)
        self.assertEqual(status["failed_attempts"], 1)
        self.assertTrue(marker.exists())
        result = next(
            json.loads(record["body"])
            for record in self.client.records("ongo-experiment-result")
            if json.loads(record["body"])["condition_id"] == "missing-output"
        )
        self.assertEqual(result["execution"]["output_file_errors"][0]["name"], "declared")

    def test_local_runner_stops_at_an_earlier_manual_condition(self):
        marker = self.root / "should-wait.txt"
        local = {
            "id": "later-local",
            "description": "Must wait for the manual slot",
            "required_runs": 1,
            "expected_cost_usd": "0",
            "required_artifacts": ["stdout", "stderr"],
            "execution": {
                "mode": "local",
                "argv": [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"],
                "cwd": str(self.root),
                "env": {},
                "timeout_seconds": 1,
                "accepted_exit_codes": [0],
                "output_files": [],
            },
        }
        created = self.create([self.manual_condition("manual-first"), local])
        experiment_id = created["experiment"]["experiment_id"]
        self.approve_free(experiment_id)
        status, exit_code = run_local(self.client, experiment_id)
        self.assertEqual(exit_code, 6)
        self.assertFalse(marker.exists())
        self.assertEqual(status["conditions"][1]["attempts"], 0)

    def test_binary_artifact_round_trips_inside_ken(self):
        created = self.create([self.manual_condition()])
        experiment_id = created["experiment"]["experiment_id"]
        self.approve_free(experiment_id)
        attempt = begin_experiment(self.client, experiment_id, "worker")
        content = b"\x00\xff\x10binary"
        artifact = artifact_envelope(
            "observation", "observation.bin", "application/octet-stream", content
        )
        finish_attempt(
            self.client,
            attempt["attempt"]["attempt_id"],
            {
                "schema_version": 1,
                "status": "completed",
                "valid_observation": True,
                "summary": "binary evidence",
                "metrics": {},
                "actual_cost_usd": "0",
            },
            {"observation": artifact},
        )
        stored = json.loads(self.client.records("ongo-experiment-artifact")[0]["body"])
        self.assertEqual(stored["encoding"], "base64")
        import base64

        self.assertEqual(base64.b64decode(stored["data_base64"]), content)

    def test_utf8_artifact_round_trips_and_tampering_is_rejected(self):
        created = self.create([self.manual_condition()])
        experiment_id = created["experiment"]["experiment_id"]
        self.approve_free(experiment_id)
        attempt = begin_experiment(self.client, experiment_id, "worker")["attempt"]
        artifact = artifact_envelope(
            "observation", "observation.txt", "text/plain", "café".encode()
        )
        tampered = {**artifact, "sha256": "0" * 64}
        result = {
            "schema_version": 1,
            "status": "completed",
            "valid_observation": True,
            "summary": "text evidence",
            "metrics": {},
            "actual_cost_usd": "0",
        }
        with self.assertRaises(OngoError):
            finish_attempt(
                self.client,
                attempt["attempt_id"],
                result,
                {"observation": tampered},
            )
        self.assertEqual(len(self.client.list_kind("ongo-experiment-result")), 0)
        finish_attempt(
            self.client,
            attempt["attempt_id"],
            result,
            {"observation": artifact},
        )
        stored = json.loads(self.client.records("ongo-experiment-artifact")[0]["body"])
        self.assertEqual(stored["encoding"], "utf-8")
        self.assertEqual(stored["content"], "café")

    def test_experiment_is_private_until_explicit_web_marker(self):
        created = self.create([self.manual_condition()])
        experiment = created["experiment"]
        self.assertEqual(self.client.list_kind("ongo-web"), [])

        class SiteArgs:
            ken = self.client.binary
            db = self.client.db
            out = str(self.root / "site")
            site_title = "Experiment site"
            base_url = ""

        with mock.patch.object(site, "vendor_katex", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            site.build(SiteArgs)
        private_tab = (self.root / "site" / "experiments.html").read_text(encoding="utf-8")
        self.assertNotIn(experiment["title"], private_tab)

        self.client.command(
            "add",
            "ongo-web",
            "-k",
            created["record_id"],
            "--title",
            experiment["title"],
        )
        view = KenView(self.client.binary, self.client.db)
        publication = fetch_publication(view, created["record_id"])
        log = []
        source = resolve_source(
            view, publication, log, self.client.binary, self.client.db
        )
        self.assertEqual(source["kind"], "markdown")
        self.assertIn("Authoritative condition matrix", source["body"])
        self.assertIn("`manual`", source["body"])
        with mock.patch.object(site, "vendor_katex", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            site.build(SiteArgs)
        published_tab = (self.root / "site" / "experiments.html").read_text(encoding="utf-8")
        self.assertIn(experiment["title"], published_tab)
        self.assertNotIn("ongo-experiment-artifact", published_tab)


if __name__ == "__main__":
    unittest.main()
