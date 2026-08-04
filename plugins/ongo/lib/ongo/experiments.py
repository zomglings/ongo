"""Deterministic, append-only experiment management backed by Ken v3."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import html
import json
import math
import mimetypes
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .errors import OngoArgumentParser, OngoError, emit_json
from .ken import KenClient


SCHEMA_VERSION = 1
CONDITION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EXPERIMENT_KINDS = (
    "ongo-experiment",
    "ongo-experiment-plan",
    "ongo-experiment-manifest",
    "ongo-experiment-condition",
    "ongo-experiment-delegation",
    "ongo-experiment-approval",
    "ongo-experiment-attempt",
    "ongo-experiment-result",
    "ongo-experiment-artifact",
    "ongo-experiment-note",
)


def canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def check_finite_json_numbers(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if isinstance(value, dict):
        for item in value.values():
            check_finite_json_numbers(item)
    elif isinstance(value, list):
        for item in value:
            check_finite_json_numbers(item)


def strict_json_loads(text):
    def reject_constant(value):
        raise ValueError(f"non-standard JSON constant {value}")

    value = json.loads(text, parse_constant=reject_constant)
    check_finite_json_numbers(value)
    return value


def hash_bytes(value):
    return hashlib.sha256(value).hexdigest()


def hash_text(value):
    return hash_bytes(value.encode("utf-8"))


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value, field):
    if not isinstance(value, str) or not value:
        invalid(f"{field} must be an ISO-8601 timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        invalid(f"{field} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        invalid(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def money(value, field):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        invalid(f"{field} must be a decimal USD amount")
    if not parsed.is_finite() or parsed < 0:
        invalid(f"{field} must be a non-negative decimal USD amount")
    return parsed


def money_text(value):
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def invalid(message, details=None):
    raise OngoError(
        message,
        code="invalid-input",
        exit_code=2,
        details=details,
    )


def conflict(message, details=None):
    raise OngoError(
        message,
        code="protocol-conflict",
        exit_code=4,
        details=details,
    )


def unauthorized(message, details=None):
    raise OngoError(
        message,
        code="authorization-required",
        exit_code=5,
        details=details,
    )


def incomplete(message, details=None):
    raise OngoError(
        message,
        code="experiment-incomplete",
        exit_code=6,
        details=details,
    )


def require_object(value, field):
    if not isinstance(value, dict):
        invalid(f"{field} must be an object")


def reject_unknown(value, allowed, field):
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        invalid(f"{field} has unknown fields", {"fields": unknown})


def load_json_file(path, field):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        invalid(f"could not read {field}", {"path": path, "error": str(error)})
    try:
        return strict_json_loads(text)
    except (json.JSONDecodeError, ValueError) as error:
        invalid(f"{field} is not valid JSON", {"path": path, "error": str(error)})


def normalize_output_files(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        invalid(f"{field} must be a list")
    outputs = []
    names = set()
    for index, item in enumerate(value):
        if isinstance(item, str):
            item = {"name": Path(item).name, "path": item}
        require_object(item, f"{field}[{index}]")
        reject_unknown(item, {"name", "path", "media_type"}, f"{field}[{index}]")
        name = item.get("name")
        path = item.get("path")
        if not isinstance(name, str) or not name or not isinstance(path, str) or not path:
            invalid(f"{field}[{index}] requires non-empty name and path")
        if name in names:
            invalid(f"{field} contains duplicate artifact name", {"name": name})
        names.add(name)
        output = {"name": name, "path": path}
        if item.get("media_type") is not None:
            if not isinstance(item["media_type"], str) or not item["media_type"]:
                invalid(f"{field}[{index}].media_type must be a non-empty string")
            output["media_type"] = item["media_type"]
        outputs.append(output)
    return outputs


def validate_manifest(value):
    try:
        check_finite_json_numbers(value)
    except ValueError as error:
        invalid("manifest contains a non-finite JSON number", {"error": str(error)})
    require_object(value, "manifest")
    reject_unknown(value, {"schema_version", "title", "conditions"}, "manifest")
    if value.get("schema_version") != SCHEMA_VERSION:
        invalid("manifest.schema_version must be 1")
    title = value.get("title")
    if not isinstance(title, str) or not title.strip():
        invalid("manifest.title must be a non-empty string")
    conditions = value.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        invalid("manifest.conditions must be a non-empty list")
    normalized = []
    condition_ids = set()
    for index, condition in enumerate(conditions):
        field = f"manifest.conditions[{index}]"
        require_object(condition, field)
        reject_unknown(
            condition,
            {
                "id",
                "description",
                "required_runs",
                "expected_cost_usd",
                "required_artifacts",
                "execution",
            },
            field,
        )
        condition_id = condition.get("id")
        if not isinstance(condition_id, str) or not CONDITION_ID.fullmatch(condition_id):
            invalid(f"{field}.id has an invalid format")
        if condition_id in condition_ids:
            invalid("manifest condition IDs must be unique", {"id": condition_id})
        condition_ids.add(condition_id)
        description = condition.get("description")
        if not isinstance(description, str) or not description.strip():
            invalid(f"{field}.description must be a non-empty string")
        required_runs = condition.get("required_runs")
        if not isinstance(required_runs, int) or isinstance(required_runs, bool) or required_runs < 1:
            invalid(f"{field}.required_runs must be a positive integer")
        expected_cost = money(condition.get("expected_cost_usd"), f"{field}.expected_cost_usd")
        required_artifacts = condition.get("required_artifacts")
        if not isinstance(required_artifacts, list) or any(
            not isinstance(item, str) or not item for item in required_artifacts
        ):
            invalid(f"{field}.required_artifacts must be a list of non-empty strings")
        if len(set(required_artifacts)) != len(required_artifacts):
            invalid(f"{field}.required_artifacts contains duplicates")
        execution = condition.get("execution")
        require_object(execution, f"{field}.execution")
        mode = execution.get("mode")
        if mode == "manual":
            reject_unknown(execution, {"mode"}, f"{field}.execution")
            normalized_execution = {"mode": "manual"}
        elif mode == "local":
            reject_unknown(
                execution,
                {
                    "mode",
                    "argv",
                    "cwd",
                    "env",
                    "timeout_seconds",
                    "accepted_exit_codes",
                    "output_files",
                },
                f"{field}.execution",
            )
            argv = execution.get("argv")
            if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
                invalid(f"{field}.execution.argv must be a non-empty string list")
            cwd = execution.get("cwd", ".")
            if not isinstance(cwd, str) or not cwd:
                invalid(f"{field}.execution.cwd must be a non-empty string")
            env = execution.get("env", {})
            if not isinstance(env, dict) or any(
                not isinstance(key, str) or not isinstance(item, str)
                for key, item in env.items()
            ):
                invalid(f"{field}.execution.env must map strings to strings")
            timeout = execution.get("timeout_seconds", 3600)
            try:
                timeout_is_finite = math.isfinite(timeout)
            except (TypeError, OverflowError):
                timeout_is_finite = False
            if (
                not isinstance(timeout, (int, float))
                or isinstance(timeout, bool)
                or not timeout_is_finite
                or timeout <= 0
            ):
                invalid(
                    f"{field}.execution.timeout_seconds must be a finite positive number"
                )
            accepted = execution.get("accepted_exit_codes", [0])
            if not isinstance(accepted, list) or not accepted or any(
                not isinstance(item, int) or isinstance(item, bool) for item in accepted
            ):
                invalid(f"{field}.execution.accepted_exit_codes must be an integer list")
            normalized_execution = {
                "mode": "local",
                "argv": argv,
                "cwd": cwd,
                "env": dict(sorted(env.items())),
                "timeout_seconds": timeout,
                "accepted_exit_codes": accepted,
                "output_files": normalize_output_files(
                    execution.get("output_files"), f"{field}.execution.output_files"
                ),
            }
            available_artifacts = {"stdout", "stderr"} | {
                item["name"] for item in normalized_execution["output_files"]
            }
            unavailable = sorted(set(required_artifacts) - available_artifacts)
            if unavailable:
                invalid(
                    f"{field}.required_artifacts names outputs the local runner cannot capture",
                    {"unavailable": unavailable},
                )
            if any(
                item["name"] in {"stdout", "stderr"}
                for item in normalized_execution["output_files"]
            ):
                invalid(
                    f"{field}.execution.output_files cannot redefine stdout or stderr"
                )
        else:
            invalid(f"{field}.execution.mode must be manual or local")
        normalized.append(
            {
                "id": condition_id,
                "description": description.strip(),
                "required_runs": required_runs,
                "expected_cost_usd": money_text(expected_cost),
                "required_artifacts": required_artifacts,
                "execution": normalized_execution,
            }
        )
    return {"schema_version": SCHEMA_VERSION, "title": title.strip(), "conditions": normalized}


def parse_body(record, *, expected_object=True):
    body = record.get("body", "")
    try:
        value = strict_json_loads(body)
    except (json.JSONDecodeError, ValueError) as error:
        raise OngoError(
            "an Ongo publication contains invalid JSON",
            code="corrupt-experiment-record",
            exit_code=3,
            details={"id": record.get("id"), "kind": record.get("kind"), "error": str(error)},
        ) from error
    if expected_object and not isinstance(value, dict):
        raise OngoError(
            "an Ongo publication contains an invalid record shape",
            code="corrupt-experiment-record",
            exit_code=3,
            details={"id": record.get("id"), "kind": record.get("kind")},
        )
    return value


def records_with_bodies(client, kind):
    return [(record, parse_body(record)) for record in client.records(kind)]


def find_experiment(client, identifier):
    matches = []
    for record, body in records_with_bodies(client, "ongo-experiment"):
        if identifier in {record.get("id"), record.get("key"), body.get("experiment_id")}:
            matches.append((record, body))
    if not matches:
        invalid("experiment not found", {"experiment": identifier})
    if len(matches) > 1:
        conflict("experiment identifier is ambiguous", {"experiment": identifier})
    return matches[0]


def find_delegation(client, identifier):
    matches = []
    for record, body in records_with_bodies(client, "ongo-experiment-delegation"):
        if identifier in {record.get("id"), record.get("key"), body.get("delegation_id")}:
            matches.append((record, body))
    if not matches:
        invalid("delegation not found", {"delegation": identifier})
    if len(matches) > 1:
        conflict("delegation identifier is ambiguous", {"delegation": identifier})
    return matches[0]


def find_attempt(client, identifier):
    matches = []
    for record, body in records_with_bodies(client, "ongo-experiment-attempt"):
        if identifier in {record.get("id"), record.get("key"), body.get("attempt_id")}:
            matches.append((record, body))
    if not matches:
        invalid("attempt not found", {"attempt": identifier})
    if len(matches) > 1:
        conflict("attempt identifier is ambiguous", {"attempt": identifier})
    return matches[0]


def find_topics(client, identifiers):
    rows = client.list_kind("topic")
    resolved = {}
    for identifier in identifiers or []:
        if not isinstance(identifier, str) or not identifier.strip():
            invalid("topic references must be non-empty strings")
        identifier = identifier.strip()
        matches = {
            row["id"]: row
            for row in rows
            if identifier in {row.get("id"), row.get("key")}
        }
        if not matches:
            invalid("topic not found", {"topic": identifier})
        if len(matches) > 1:
            conflict("topic identifier is ambiguous", {"topic": identifier})
        row = next(iter(matches.values()))
        resolved[row["id"]] = row
    return [resolved[record_id] for record_id in sorted(resolved)]


def corrupt_note(record, message, details=None):
    raise OngoError(
        message,
        code="corrupt-experiment-record",
        exit_code=3,
        details={"id": record.get("id"), **(details or {})},
    )


def validate_note_body(record, body):
    required = {
        "schema_version",
        "note_id",
        "experiment_id",
        "target_type",
        "target_id",
        "target_record_id",
        "actor",
        "markdown",
        "operation_key",
        "created_at",
    }
    if not isinstance(body, dict) or set(body) != required:
        corrupt_note(record, "an experiment note has an invalid schema")
    if body.get("schema_version") != SCHEMA_VERSION:
        corrupt_note(record, "an experiment note has an unsupported schema")
    for field in (
        "note_id",
        "experiment_id",
        "target_id",
        "target_record_id",
        "actor",
        "markdown",
        "created_at",
    ):
        if not isinstance(body.get(field), str) or not body[field]:
            corrupt_note(record, "an experiment note has an invalid field", {"field": field})
    if body.get("target_type") not in {"experiment", "condition", "attempt"}:
        corrupt_note(record, "an experiment note has an invalid target type")
    operation_key = body.get("operation_key")
    if operation_key is not None and (
        not isinstance(operation_key, str) or not operation_key
    ):
        corrupt_note(record, "an experiment note has an invalid operation key")
    try:
        parse_time(body["created_at"], "note.created_at")
    except OngoError as error:
        corrupt_note(record, "an experiment note has an invalid timestamp", {"error": str(error)})
    return body


def note_topics(client, record):
    topics = {}
    for relationship in record.get("relationships", []):
        if (
            relationship.get("role") != "subject"
            or relationship.get("relkind") != "related-to"
        ):
            continue
        related = client.show(relationship.get("publication"), check=False)
        if related is not None and related.get("kind") == "topic":
            topics[related["id"]] = {
                "record_id": related["id"],
                "key": related.get("key"),
                "title": related.get("title") or related.get("key") or related["id"],
            }
    return sorted(topics.values(), key=lambda item: (item["title"].casefold(), item["record_id"]))


def note_view(client, record, body):
    validate_note_body(record, body)
    attachments = [
        relationship
        for relationship in record.get("relationships", [])
        if relationship.get("role") == "subject"
        and relationship.get("relkind") == "ongo-note-for"
    ]
    if (
        len(attachments) != 1
        or attachments[0].get("publication") != body["target_record_id"]
    ):
        corrupt_note(record, "an experiment note has an invalid target relationship")
    return {
        **body,
        "record_id": record["id"],
        "key": record.get("key"),
        "topics": note_topics(client, record),
    }


def experiment_notes(client, identifier):
    root_record, root = find_experiment(client, identifier)
    experiment_id = root["experiment_id"]
    conditions = {
        record["id"]: body
        for record, body in experiment_records(
            client, experiment_id, "ongo-experiment-condition"
        )
    }
    attempts = {
        record["id"]: body
        for record, body in experiment_records(
            client, experiment_id, "ongo-experiment-attempt"
        )
    }
    targets = {
        root_record["id"]: ("experiment", experiment_id),
        **{
            record_id: ("condition", body["id"])
            for record_id, body in conditions.items()
        },
        **{
            record_id: ("attempt", body["attempt_id"])
            for record_id, body in attempts.items()
        },
    }
    notes = []
    for record, body in experiment_records(
        client, experiment_id, "ongo-experiment-note"
    ):
        note = note_view(client, record, body)
        expected = targets.get(note["target_record_id"])
        if expected != (note["target_type"], note["target_id"]):
            corrupt_note(
                record,
                "an experiment note targets a record outside its experiment",
            )
        notes.append(note)
    return sorted(notes, key=lambda item: (item["created_at"], item["record_id"]))


def add_experiment_note(
    client,
    identifier,
    *,
    actor,
    markdown,
    condition_id=None,
    attempt_identifier=None,
    topic_identifiers=None,
    operation_key=None,
):
    if not isinstance(actor, str) or not actor.strip():
        invalid("actor must be a non-empty label")
    if not isinstance(markdown, str) or not markdown.strip():
        invalid("experiment note text must not be empty")
    actor = actor.strip()
    if condition_id is not None and attempt_identifier is not None:
        invalid("an experiment note may target a condition or an attempt, not both")
    if operation_key is not None:
        if not isinstance(operation_key, str) or not operation_key.strip():
            invalid("operation_key must be a non-empty string")
        operation_key = operation_key.strip()
        if len(operation_key) > 256:
            invalid("operation_key must not exceed 256 characters")

    root_record, root = find_experiment(client, identifier)
    target_record = root_record
    target_type = "experiment"
    target_id = root["experiment_id"]
    if condition_id is not None:
        matches = [
            pair
            for pair in condition_pairs(client, root)
            if pair[1]["id"] == condition_id
        ]
        if not matches:
            invalid("condition not found", {"condition": condition_id})
        target_record, condition = matches[0]
        target_type = "condition"
        target_id = condition["id"]
    elif attempt_identifier is not None:
        target_record, attempt = find_attempt(client, attempt_identifier)
        if attempt.get("experiment_id") != root["experiment_id"]:
            invalid(
                "attempt does not belong to the experiment",
                {"attempt": attempt_identifier},
            )
        target_type = "attempt"
        target_id = attempt["attempt_id"]

    topics = find_topics(client, topic_identifiers)
    semantic = {
        "experiment_id": root["experiment_id"],
        "target_type": target_type,
        "target_id": target_id,
        "target_record_id": target_record["id"],
        "actor": actor,
        "markdown": markdown,
        "operation_key": operation_key,
    }
    if operation_key is not None:
        key = f"{root_record['key']}:note:operation:{hash_text(operation_key)}"
        existing = client.unique_by_key("ongo-experiment-note", key)
        if existing is not None:
            existing_note = note_view(client, existing, parse_body(existing))
            existing_semantic = {
                field: existing_note[field] for field in semantic
            }
            existing_topics = sorted(
                topic["record_id"] for topic in existing_note["topics"]
            )
            expected_topics = sorted(topic["id"] for topic in topics)
            if existing_semantic != semantic or existing_topics != expected_topics:
                conflict(
                    "operation key already identifies a different experiment note",
                    {"operation_key": operation_key},
                )
            return {
                "ok": True,
                "note": existing_note,
                "idempotent": True,
            }
    else:
        key = None

    note_id = str(uuid.uuid4())
    if key is None:
        key = f"{root_record['key']}:note:{note_id}"
    body = {
        "schema_version": SCHEMA_VERSION,
        "note_id": note_id,
        **semantic,
        "created_at": utc_now(),
    }
    relationships = [
        {"subject": "note", "object": target_record["id"], "kind": "ongo-note-for"},
        *[
            {"subject": "note", "object": topic["id"], "kind": "related-to"}
            for topic in topics
        ],
    ]
    loaded = client.load(
        {
            "publications": [
                {
                    "ref": "note",
                    "kind": "ongo-experiment-note",
                    "key": key,
                    "title": "Experiment note",
                }
            ],
            "relationships": relationships,
            "notes": [{"publication": "note", "body": canonical_json(body)}],
        }
    )
    record = client.show(loaded["refs"]["note"])
    return {
        "ok": True,
        "note": note_view(client, record, body),
        "idempotent": False,
    }


def experiment_records(client, experiment_id, kind):
    return [
        (record, body)
        for record, body in records_with_bodies(client, kind)
        if body.get("experiment_id") == experiment_id
    ]


def total_expected_cost(manifest):
    total = Decimal("0")
    for condition in manifest["conditions"]:
        total += money(condition["expected_cost_usd"], "expected_cost_usd") * condition["required_runs"]
    return total


def create_experiment(client, document_path, manifest_path, successor_of=None):
    try:
        document = Path(document_path).read_text(encoding="utf-8")
    except OSError as error:
        invalid("could not read plan document", {"path": document_path, "error": str(error)})
    if not document.strip():
        invalid("plan document must not be empty")
    manifest = validate_manifest(load_json_file(manifest_path, "manifest"))
    manifest_text = canonical_json(manifest)
    document_hash = hash_text(document)
    manifest_hash = hash_text(manifest_text)
    successor_record = None
    successor_body = None
    if successor_of:
        successor_record, successor_body = find_experiment(client, successor_of)
    expected_successor = successor_body.get("experiment_id") if successor_body else None
    for record, body in records_with_bodies(client, "ongo-experiment"):
        if body.get("document_sha256") == document_hash and body.get("manifest_sha256") == manifest_hash:
            if body.get("successor_of") != expected_successor:
                conflict(
                    "identical plan content already exists with different lineage",
                    {"experiment_id": body.get("experiment_id")},
                )
            return experiment_view(client, body["experiment_id"])
    experiment_id = str(uuid.uuid4())
    root_key = f"ongo-experiment:{experiment_id}"
    plan_key = f"{root_key}:plan:{document_hash[:16]}"
    manifest_key = f"{root_key}:manifest:{manifest_hash[:16]}"
    created_at = utc_now()
    root = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "title": manifest["title"],
        "created_at": created_at,
        "document_sha256": document_hash,
        "manifest_sha256": manifest_hash,
        "plan_key": plan_key,
        "manifest_key": manifest_key,
        "expected_cost_usd": money_text(total_expected_cost(manifest)),
        "successor_of": expected_successor,
    }
    publications = [
        {"ref": "root", "kind": "ongo-experiment", "key": root_key, "title": manifest["title"]},
        {"ref": "plan", "kind": "ongo-experiment-plan", "key": plan_key, "title": f"{manifest['title']} — plan"},
        {"ref": "manifest", "kind": "ongo-experiment-manifest", "key": manifest_key, "title": f"{manifest['title']} — manifest"},
    ]
    notes = [
        {"publication": "root", "body": canonical_json(root)},
        {"publication": "plan", "body": document},
        {"publication": "manifest", "body": manifest_text},
    ]
    relationships = [
        {"subject": "root", "object": "plan", "kind": "ongo-has-plan"},
        {"subject": "plan", "object": "manifest", "kind": "ongo-compiled-as"},
    ]
    for index, condition in enumerate(manifest["conditions"]):
        reference = f"condition-{index}"
        condition_key = f"{root_key}:condition:{condition['id']}"
        condition_body = {
            **condition,
            "schema_version": SCHEMA_VERSION,
            "experiment_id": experiment_id,
            "manifest_sha256": manifest_hash,
            "order": index,
        }
        publications.append(
            {
                "ref": reference,
                "kind": "ongo-experiment-condition",
                "key": condition_key,
                "title": f"{condition['id']}: {condition['description']}",
            }
        )
        notes.append({"publication": reference, "body": canonical_json(condition_body)})
        relationships.append(
            {"subject": "manifest", "object": reference, "kind": "ongo-has-condition"}
        )
    if successor_record:
        relationships.append(
            {"subject": "root", "object": successor_record["id"], "kind": "ongo-successor-of"}
        )
    client.load(
        {"publications": publications, "relationships": relationships, "notes": notes}
    )
    return experiment_view(client, experiment_id)


def current_plan(client, root_body):
    plan = client.unique_by_key("ongo-experiment-plan", root_body["plan_key"])
    manifest_record = client.unique_by_key(
        "ongo-experiment-manifest", root_body["manifest_key"]
    )
    if not plan or not manifest_record:
        raise OngoError(
            "experiment plan graph is incomplete",
            code="corrupt-experiment-record",
            exit_code=3,
            details={"experiment_id": root_body["experiment_id"]},
        )
    manifest = validate_manifest(parse_body(manifest_record))
    return plan, manifest_record, manifest


def state_for_experiment(client, identifier):
    root_record, root = find_experiment(client, identifier)
    plan_record, manifest_record, manifest = current_plan(client, root)
    experiment_id = root["experiment_id"]
    condition_pairs = sorted(
        experiment_records(client, experiment_id, "ongo-experiment-condition"),
        key=lambda pair: pair[1]["order"],
    )
    attempts = experiment_records(client, experiment_id, "ongo-experiment-attempt")
    results = experiment_records(client, experiment_id, "ongo-experiment-result")
    results_by_attempt = {body["attempt_id"]: (record, body) for record, body in results}
    conditions = []
    planned = valid = failed = cancelled = invalid_count = open_count = extra_valid = 0
    for condition_record, condition in condition_pairs:
        condition_attempts = [pair for pair in attempts if pair[1]["condition_id"] == condition["id"]]
        terminal = [results_by_attempt[pair[1]["attempt_id"]] for pair in condition_attempts if pair[1]["attempt_id"] in results_by_attempt]
        valid_count = sum(1 for _, body in terminal if body.get("valid_observation") is True)
        failed_count = sum(1 for _, body in terminal if body.get("status") == "failed")
        cancelled_count = sum(1 for _, body in terminal if body.get("status") == "cancelled")
        invalid_observations = sum(
            1 for _, body in terminal if body.get("valid_observation") is not True
        )
        opens = [pair for pair in condition_attempts if pair[1]["attempt_id"] not in results_by_attempt]
        initial_count = sum(1 for _, body in condition_attempts if not body.get("retry"))
        needed = max(condition["required_runs"] - valid_count, 0)
        planned += condition["required_runs"]
        valid += valid_count
        failed += failed_count
        cancelled += cancelled_count
        invalid_count += invalid_observations
        open_count += len(opens)
        extra_valid += max(valid_count - condition["required_runs"], 0)
        conditions.append(
            {
                "id": condition["id"],
                "description": condition["description"],
                "order": condition["order"],
                "required_runs": condition["required_runs"],
                "valid_runs": valid_count,
                "remaining_runs": needed,
                "initial_attempts": initial_count,
                "attempts": len(condition_attempts),
                "failed": failed_count,
                "cancelled": cancelled_count,
                "invalid_observations": invalid_observations,
                "open": len(opens),
                "execution_mode": condition["execution"]["mode"],
                "expected_cost_usd": condition["expected_cost_usd"],
                "record_id": condition_record["id"],
            }
        )
    approvals = [
        body
        for _, body in experiment_records(client, experiment_id, "ongo-experiment-approval")
        if body.get("manifest_sha256") == root["manifest_sha256"]
    ]
    notes = experiment_notes(client, experiment_id)
    note_counts = {
        target_type: sum(
            1 for note in notes if note["target_type"] == target_type
        )
        for target_type in ("experiment", "condition", "attempt")
    }
    return {
        "ok": True,
        "experiment_id": experiment_id,
        "record_id": root_record["id"],
        "key": root_record["key"],
        "title": root["title"],
        "document_sha256": root["document_sha256"],
        "manifest_sha256": root["manifest_sha256"],
        "expected_cost_usd": root["expected_cost_usd"],
        "approved": bool(approvals),
        "approvals": approvals,
        "plan_frozen": bool(attempts),
        "planned_runs": planned,
        "valid_runs": valid,
        "failed_attempts": failed,
        "cancelled_attempts": cancelled,
        "invalid_observations": invalid_count,
        "open_attempts": open_count,
        "remaining_runs": max(planned - valid, 0),
        "extra_valid_runs": extra_valid,
        "complete": valid == planned and open_count == 0 and extra_valid == 0,
        "note_count": len(notes),
        "note_counts": note_counts,
        "conditions": conditions,
        "plan_record_id": plan_record["id"],
        "manifest_record_id": manifest_record["id"],
    }


def markdown_view(client, identifier):
    root_record, root = find_experiment(client, identifier)
    plan_record, manifest_record, manifest = current_plan(client, root)
    status = state_for_experiment(client, identifier)
    notes = experiment_notes(client, identifier)
    lines = [
        f"# {root['title']}",
        "",
        f"Experiment: `{root['experiment_id']}`  ",
        f"Plan SHA-256: `{root['document_sha256']}`  ",
        f"Manifest SHA-256: `{root['manifest_sha256']}`  ",
        f"Expected cost: `${root['expected_cost_usd']}`  ",
        f"Approved: `{'yes' if status['approved'] else 'no'}`  ",
        f"Coverage: `{status['valid_runs']}/{status['planned_runs']}`",
    ]
    if notes:
        lines.extend(["", "## Experiment notes"])
        lines.extend(note_markdown_entries(notes, heading_level=3))
    lines.extend(
        [
            "",
            "## Authoritative condition matrix",
            "",
            "| Order | ID | Runs | Mode | Expected USD/run | Required artifacts | Description |",
            "|---:|---|---:|---|---:|---|---|",
        ]
    )
    for index, condition in enumerate(manifest["conditions"], start=1):
        artifacts = ", ".join(condition["required_artifacts"]) or "—"
        description = condition["description"].replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {index} | `{condition['id']}` | {condition['required_runs']} | "
            f"{condition['execution']['mode']} | {condition['expected_cost_usd']} | "
            f"{artifacts} | {description} |"
        )
    lines.extend(["", "## Protocol document", "", plan_record.get("body", "")])
    lines.extend(
        [
            "",
            "## Canonical manifest",
            "",
            "```json",
            json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False),
            "```",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def experiment_view(client, identifier):
    root_record, root = find_experiment(client, identifier)
    plan_record, manifest_record, manifest = current_plan(client, root)
    return {
        "ok": True,
        "experiment": root,
        "record_id": root_record["id"],
        "document": plan_record.get("body", ""),
        "manifest": manifest,
        "status": state_for_experiment(client, identifier),
        "notes": experiment_notes(client, identifier),
    }


def markdown_inline(value):
    return str(value).replace("\\", "\\\\").replace("`", "\\`")


def note_target_label(note):
    if note["target_type"] == "experiment":
        return "Experiment"
    if note["target_type"] == "condition":
        return f"Condition `{markdown_inline(note['target_id'])}`"
    return f"Attempt `{markdown_inline(note['target_id'])}`"


def note_markdown_entries(notes, heading_level=2):
    lines = []
    heading = "#" * heading_level
    for note in notes:
        actor = markdown_inline(note["actor"])
        lines.extend(
            [
                "",
                f"{heading} {note['created_at']} — {actor}",
                "",
                f"Target: {note_target_label(note)}  ",
            ]
        )
        if note["topics"]:
            topics = ", ".join(
                f"`{markdown_inline(topic['title'])}`" for topic in note["topics"]
            )
            lines.append(f"Topics: {topics}  ")
        lines.extend(["", note["markdown"].rstrip()])
    return lines


def experiment_notes_markdown(client, identifier):
    _record, root = find_experiment(client, identifier)
    notes = experiment_notes(client, identifier)
    lines = [f"# Notes for {root['title']}", ""]
    if not notes:
        lines.append("No experiment notes have been recorded.")
    else:
        lines.extend(note_markdown_entries(notes))
    return "\n".join(lines).rstrip() + "\n"


def experiment_notes_view(client, identifier):
    _record, root = find_experiment(client, identifier)
    notes = experiment_notes(client, identifier)
    return {
        "ok": True,
        "experiment_id": root["experiment_id"],
        "notes": notes,
        "count": len(notes),
    }


def read_note_markdown(text, path):
    if text is not None:
        return text
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as error:
        invalid("could not read experiment note", {"path": path, "error": str(error)})


def render_experiment(client, identifier, output_dir):
    markdown = markdown_view(client, identifier)
    status = state_for_experiment(client, identifier)
    escaped = html.escape(markdown)
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(status['title'])}</title>
<style>body{{font:16px/1.5 system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem}}pre{{white-space:pre-wrap;background:#f6f8fa;padding:1rem;border-radius:.5rem}}</style>
</head><body><pre>{escaped}</pre></body></html>"""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    output = target / "index.html"
    output.write_text(document, encoding="utf-8")
    return {"ok": True, "experiment_id": status["experiment_id"], "output": str(output.resolve())}


def create_delegation(client, args):
    if not isinstance(args.granted_by, str) or not args.granted_by.strip():
        invalid("granted_by must be a non-empty principal label")
    if not isinstance(args.evidence, str) or not args.evidence.strip():
        invalid("evidence must be a non-empty locator")
    max_per = money(args.max_per_experiment_usd, "max_per_experiment_usd")
    max_total = money(args.max_total_usd, "max_total_usd") if args.max_total_usd else None
    expiry = parse_time(args.expires_at, "expires_at")
    if expiry <= datetime.now(timezone.utc):
        invalid("expires_at must be in the future")
    modes = sorted(set(args.mode or ["manual", "local"]))
    if not modes or not set(modes).issubset({"manual", "local"}):
        invalid("mode must contain manual and/or local")
    experiment_id = None
    experiment_record = None
    if args.experiment:
        experiment_record, experiment = find_experiment(client, args.experiment)
        experiment_id = experiment["experiment_id"]
    body = {
        "schema_version": SCHEMA_VERSION,
        "delegation_id": str(uuid.uuid4()),
        "granted_by": args.granted_by.strip(),
        "evidence": args.evidence.strip(),
        "max_per_experiment_usd": money_text(max_per),
        "max_total_usd": money_text(max_total) if max_total is not None else None,
        "expires_at": expiry.isoformat().replace("+00:00", "Z"),
        "allowed_execution_modes": modes,
        "experiment_id": experiment_id,
        "created_at": utc_now(),
    }
    semantic = {key: value for key, value in body.items() if key not in {"delegation_id", "created_at"}}
    semantic_hash = hash_text(canonical_json(semantic))
    key = f"ongo-delegation:{semantic_hash}"
    existing = client.unique_by_key("ongo-experiment-delegation", key)
    if existing:
        return {"ok": True, "delegation": parse_body(existing), "record_id": existing["id"], "idempotent": True}
    reference = "delegation"
    payload = {
        "publications": [
            {"ref": reference, "kind": "ongo-experiment-delegation", "key": key, "title": f"Delegation from {args.granted_by}"}
        ],
        "notes": [{"publication": reference, "body": canonical_json(body)}],
        "relationships": [],
    }
    if experiment_record:
        payload["relationships"].append(
            {"subject": reference, "object": experiment_record["id"], "kind": "ongo-delegates-for"}
        )
    loaded = client.load(payload)
    return {"ok": True, "delegation": body, "record_id": loaded["refs"][reference], "idempotent": False}


def delegation_is_live(body):
    return parse_time(body["expires_at"], "delegation.expires_at") > datetime.now(timezone.utc)


def delegation_usage(client, delegation_id):
    approved_experiments = {
        body["experiment_id"]
        for _, body in records_with_bodies(client, "ongo-experiment-approval")
        if body.get("delegation_id") == delegation_id
    }
    return sum(
        (current_spend(client, experiment_id) for experiment_id in approved_experiments),
        Decimal("0"),
    )


def approve_experiment(client, identifier, delegation_identifier, actor, actor_role):
    if actor_role != "driver":
        unauthorized("only the driving agent may record approval", {"actor_role": actor_role})
    if not isinstance(actor, str) or not actor.strip():
        invalid("actor must be a non-empty label")
    actor = actor.strip()
    root_record, root = find_experiment(client, identifier)
    if any(
        body.get("worker") == actor
        for _, body in experiment_records(
            client, root["experiment_id"], "ongo-experiment-attempt"
        )
    ):
        unauthorized("a recorded worker cannot approve the same experiment")
    status = state_for_experiment(client, identifier)
    total = money(root["expected_cost_usd"], "experiment.expected_cost_usd")
    delegation_record = None
    delegation = None
    authority = "zero-cost-policy"
    if delegation_identifier:
        delegation_record, delegation = find_delegation(client, delegation_identifier)
    expected_delegation = delegation["delegation_id"] if delegation else None
    for approval in status["approvals"]:
        if (
            approval.get("actor") == actor
            and approval.get("delegation_id") == expected_delegation
        ):
            return {"ok": True, "approval": approval, "idempotent": True}
        conflict(
            "this exact plan already has a different recorded approval",
            {
                "approval_id": approval.get("approval_id"),
                "actor": approval.get("actor"),
                "delegation_id": approval.get("delegation_id"),
            },
        )
    if total > 0:
        if not delegation_identifier:
            unauthorized("paid experiments require a delegation")
        if not delegation_is_live(delegation):
            unauthorized("delegation has expired", {"delegation_id": delegation["delegation_id"]})
        if delegation.get("experiment_id") not in {None, root["experiment_id"]}:
            unauthorized("delegation is restricted to another experiment")
        modes = {condition["execution"]["mode"] for condition in experiment_view(client, identifier)["manifest"]["conditions"]}
        if not modes.issubset(set(delegation["allowed_execution_modes"])):
            unauthorized("delegation does not permit every execution mode", {"required_modes": sorted(modes)})
        if total > money(delegation["max_per_experiment_usd"], "delegation.max_per_experiment_usd"):
            unauthorized("experiment exceeds the delegation's per-experiment ceiling")
        maximum_total = delegation.get("max_total_usd")
        if maximum_total is not None:
            used = delegation_usage(client, delegation["delegation_id"])
            if used + total > money(maximum_total, "delegation.max_total_usd"):
                unauthorized("experiment exceeds the delegation's cumulative ceiling")
        authority = delegation["delegation_id"]
    elif delegation_identifier:
        if not delegation_is_live(delegation):
            unauthorized("delegation has expired", {"delegation_id": delegation["delegation_id"]})
        if delegation.get("experiment_id") not in {None, root["experiment_id"]}:
            unauthorized("delegation is restricted to another experiment")
        modes = {
            condition["execution"]["mode"]
            for condition in experiment_view(client, identifier)["manifest"]["conditions"]
        }
        if not modes.issubset(set(delegation["allowed_execution_modes"])):
            unauthorized(
                "delegation does not permit every execution mode",
                {"required_modes": sorted(modes)},
            )
        authority = delegation["delegation_id"]
    approval_id = str(uuid.uuid4())
    body = {
        "schema_version": SCHEMA_VERSION,
        "approval_id": approval_id,
        "experiment_id": root["experiment_id"],
        "document_sha256": root["document_sha256"],
        "manifest_sha256": root["manifest_sha256"],
        "expected_cost_usd": root["expected_cost_usd"],
        "actor": actor,
        "actor_role": "driver",
        "delegation_id": expected_delegation,
        "authority": authority,
        "created_at": utc_now(),
    }
    key = f"{root_record['key']}:approval:{root['manifest_sha256'][:16]}:{hash_text(actor)[:12]}"
    existing = client.unique_by_key("ongo-experiment-approval", key)
    if existing:
        existing_body = parse_body(existing)
        if existing_body["manifest_sha256"] == root["manifest_sha256"]:
            return {"ok": True, "approval": existing_body, "record_id": existing["id"], "idempotent": True}
        conflict("approval key already contains different content")
    publications = [
        {"ref": "approval", "kind": "ongo-experiment-approval", "key": key, "title": f"Approval for {root['title']}"}
    ]
    relationships = [
        {"subject": "approval", "object": status["manifest_record_id"], "kind": "ongo-approves"}
    ]
    if delegation_record:
        relationships.append(
            {"subject": "approval", "object": delegation_record["id"], "kind": "ongo-under-delegation"}
        )
    loaded = client.load(
        {"publications": publications, "relationships": relationships, "notes": [{"publication": "approval", "body": canonical_json(body)}]}
    )
    return {"ok": True, "approval": body, "record_id": loaded["refs"]["approval"], "idempotent": False}


def results_for_attempts(client, experiment_id):
    return {
        body["attempt_id"]: (record, body)
        for record, body in experiment_records(client, experiment_id, "ongo-experiment-result")
    }


def current_spend(client, experiment_id):
    attempts = [
        body
        for _, body in experiment_records(
            client, experiment_id, "ongo-experiment-attempt"
        )
    ]
    results = {
        body["attempt_id"]: body
        for _, body in experiment_records(
            client, experiment_id, "ongo-experiment-result"
        )
    }
    total = Decimal("0")
    for attempt in attempts:
        result = results.get(attempt["attempt_id"])
        actual = result.get("actual_cost_usd") if result else None
        if actual is None:
            actual = attempt.get("expected_cost_usd", "0")
        total += money(actual, "result.actual_cost_usd")
    return total


def active_approval(client, root):
    matches = [
        (record, body)
        for record, body in experiment_records(client, root["experiment_id"], "ongo-experiment-approval")
        if body.get("manifest_sha256") == root["manifest_sha256"]
    ]
    if not matches:
        unauthorized("the current plan has not been approved")
    if len(matches) > 1:
        conflict(
            "the exact plan has multiple approvals despite the serial-controller contract",
            {"count": len(matches)},
        )
    return matches[0]


def authorize_attempt(client, root, condition):
    approval_record, approval = active_approval(client, root)
    next_cost = money(condition["expected_cost_usd"], "condition.expected_cost_usd")
    if approval.get("authority") == "zero-cost-policy":
        if next_cost != 0:
            unauthorized("zero-cost approval cannot authorize a paid attempt")
        return approval
    delegation_record, delegation = find_delegation(client, approval["delegation_id"])
    if not delegation_is_live(delegation):
        unauthorized("delegation expired before the attempt began")
    if condition["execution"]["mode"] not in delegation["allowed_execution_modes"]:
        unauthorized("delegation does not permit this execution mode")
    spent = current_spend(client, root["experiment_id"])
    per_limit = money(delegation["max_per_experiment_usd"], "delegation.max_per_experiment_usd")
    if spent + next_cost > per_limit:
        unauthorized("next attempt would exceed the per-experiment budget")
    if delegation.get("max_total_usd") is not None:
        used = delegation_usage(client, delegation["delegation_id"])
        total_limit = money(delegation["max_total_usd"], "delegation.max_total_usd")
        if used + next_cost > total_limit:
            unauthorized("next attempt would exceed the cumulative budget")
    return approval


def open_attempt(client, experiment_id):
    attempts = experiment_records(client, experiment_id, "ongo-experiment-attempt")
    results = results_for_attempts(client, experiment_id)
    opens = [(record, body) for record, body in attempts if body["attempt_id"] not in results]
    if len(opens) > 1:
        conflict("experiment contains multiple open attempts", {"count": len(opens)})
    return opens[0] if opens else None


def condition_pairs(client, root):
    return sorted(
        experiment_records(client, root["experiment_id"], "ongo-experiment-condition"),
        key=lambda pair: pair[1]["order"],
    )


def initial_attempt_count(client, experiment_id, condition_id):
    return sum(
        1
        for _, body in experiment_records(client, experiment_id, "ongo-experiment-attempt")
        if body["condition_id"] == condition_id and not body.get("retry")
    )


def condition_valid_count(client, experiment_id, condition_id):
    attempts = {
        body["attempt_id"]
        for _, body in experiment_records(client, experiment_id, "ongo-experiment-attempt")
        if body["condition_id"] == condition_id
    }
    return sum(
        1
        for _, body in experiment_records(client, experiment_id, "ongo-experiment-result")
        if body["attempt_id"] in attempts and body.get("valid_observation") is True
    )


def create_attempt(client, root_record, root, condition_record, condition, worker, retry):
    if not isinstance(worker, str) or not worker.strip():
        invalid("worker must be a non-empty label")
    worker = worker.strip()
    authorize_attempt(client, root, condition)
    approving_actors = {
        body.get("actor")
        for _, body in experiment_records(
            client, root["experiment_id"], "ongo-experiment-approval"
        )
        if body.get("manifest_sha256") == root["manifest_sha256"]
    }
    if worker in approving_actors:
        unauthorized("an approving driver cannot work an attempt in the same experiment")
    existing_open = open_attempt(client, root["experiment_id"])
    if existing_open:
        return {"ok": True, "attempt": existing_open[1], "record_id": existing_open[0]["id"], "idempotent": True}
    attempts = [
        body
        for _, body in experiment_records(client, root["experiment_id"], "ongo-experiment-attempt")
        if body["condition_id"] == condition["id"]
    ]
    ordinal = len(attempts) + 1
    attempt_id = str(uuid.uuid4())
    key = f"{root_record['key']}:attempt:{condition['id']}:{ordinal}"
    body = {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "experiment_id": root["experiment_id"],
        "manifest_sha256": root["manifest_sha256"],
        "condition_id": condition["id"],
        "ordinal": ordinal,
        "retry": bool(retry),
        "worker": worker,
        "expected_cost_usd": condition["expected_cost_usd"],
        "execution": condition["execution"],
        "started_at": utc_now(),
    }
    loaded = client.load(
        {
            "publications": [
                {"ref": "attempt", "kind": "ongo-experiment-attempt", "key": key, "title": f"{condition['id']} attempt {ordinal}"}
            ],
            "relationships": [
                {"subject": "attempt", "object": condition_record["id"], "kind": "ongo-attempt-of"}
            ],
            "notes": [{"publication": "attempt", "body": canonical_json(body)}],
        }
    )
    return {"ok": True, "attempt": body, "record_id": loaded["refs"]["attempt"], "idempotent": False}


def begin_experiment(client, identifier, worker):
    root_record, root = find_experiment(client, identifier)
    existing = open_attempt(client, root["experiment_id"])
    if existing:
        return {"ok": True, "attempt": existing[1], "record_id": existing[0]["id"], "idempotent": True}
    selected = next_initial_condition(client, root)
    if selected:
        condition_record, condition = selected
        return create_attempt(
            client, root_record, root, condition_record, condition, worker, False
        )
    incomplete("every initial slot has been attempted; explicit retry is required for invalid observations", state_for_experiment(client, identifier))


def next_initial_condition(client, root):
    for condition_record, condition in condition_pairs(client, root):
        if (
            initial_attempt_count(client, root["experiment_id"], condition["id"])
            < condition["required_runs"]
        ):
            return condition_record, condition
    return None


def retry_condition(client, identifier, condition_id, worker):
    root_record, root = find_experiment(client, identifier)
    if open_attempt(client, root["experiment_id"]):
        conflict("finish or cancel the open attempt before retrying")
    matches = [pair for pair in condition_pairs(client, root) if pair[1]["id"] == condition_id]
    if not matches:
        invalid("condition not found", {"condition": condition_id})
    condition_record, condition = matches[0]
    if initial_attempt_count(client, root["experiment_id"], condition_id) < condition["required_runs"]:
        conflict("initial planned attempts must run before a retry")
    if condition_valid_count(client, root["experiment_id"], condition_id) >= condition["required_runs"]:
        conflict("condition already has complete valid coverage")
    return create_attempt(client, root_record, root, condition_record, condition, worker, True)


def safe_artifact_name(name):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    if not safe:
        invalid("artifact name cannot be converted to a stable key", {"name": name})
    return safe


def artifact_envelope(name, filename, media_type, content):
    digest = hash_bytes(content)
    guessed = media_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    try:
        text = content.decode("utf-8")
        encoding = "utf-8"
        data = {"content": text}
    except UnicodeDecodeError:
        encoding = "base64"
        data = {"data_base64": base64.b64encode(content).decode("ascii")}
    return {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "filename": filename,
        "media_type": guessed,
        "byte_length": len(content),
        "sha256": digest,
        "encoding": encoding,
        **data,
    }


def validate_artifact_envelope(mapping_name, envelope):
    require_object(envelope, f"artifact {mapping_name}")
    reject_unknown(
        envelope,
        {
            "schema_version",
            "name",
            "filename",
            "media_type",
            "byte_length",
            "sha256",
            "encoding",
            "content",
            "data_base64",
        },
        f"artifact {mapping_name}",
    )
    if envelope.get("schema_version") != SCHEMA_VERSION:
        invalid("artifact.schema_version must be 1", {"name": mapping_name})
    if envelope.get("name") != mapping_name:
        invalid(
            "artifact mapping name does not match its envelope",
            {"mapping_name": mapping_name, "envelope_name": envelope.get("name")},
        )
    filename = envelope.get("filename")
    media_type = envelope.get("media_type")
    if not isinstance(filename, str) or not filename:
        invalid("artifact.filename must be non-empty", {"name": mapping_name})
    if not isinstance(media_type, str) or not media_type:
        invalid("artifact.media_type must be non-empty", {"name": mapping_name})
    encoding = envelope.get("encoding")
    if encoding == "utf-8":
        if not isinstance(envelope.get("content"), str) or "data_base64" in envelope:
            invalid("UTF-8 artifact content is malformed", {"name": mapping_name})
        content = envelope["content"].encode("utf-8")
    elif encoding == "base64":
        if not isinstance(envelope.get("data_base64"), str) or "content" in envelope:
            invalid("base64 artifact content is malformed", {"name": mapping_name})
        try:
            content = base64.b64decode(envelope["data_base64"], validate=True)
        except (ValueError, binascii.Error):
            invalid("artifact contains invalid base64", {"name": mapping_name})
    else:
        invalid("artifact.encoding must be utf-8 or base64", {"name": mapping_name})
    if envelope.get("byte_length") != len(content):
        invalid("artifact byte length does not match its content", {"name": mapping_name})
    if envelope.get("sha256") != hash_bytes(content):
        invalid("artifact SHA-256 does not match its content", {"name": mapping_name})
    return envelope


def read_artifact_specs(specs):
    artifacts = {}
    for spec in specs or []:
        if "=" not in spec:
            invalid("artifact must use NAME=PATH syntax", {"artifact": spec})
        name, path_text = spec.split("=", 1)
        if not name or name in artifacts:
            invalid("artifact names must be non-empty and unique", {"name": name})
        path = Path(path_text)
        try:
            content = path.read_bytes()
        except OSError as error:
            invalid("could not read artifact", {"name": name, "path": path_text, "error": str(error)})
        artifacts[name] = artifact_envelope(name, path.name, None, content)
    return artifacts


def validate_result(value):
    try:
        check_finite_json_numbers(value)
    except ValueError as error:
        invalid("result contains a non-finite JSON number", {"error": str(error)})
    require_object(value, "result")
    reject_unknown(
        value,
        {"schema_version", "status", "valid_observation", "summary", "metrics", "actual_cost_usd", "execution"},
        "result",
    )
    if value.get("schema_version") != SCHEMA_VERSION:
        invalid("result.schema_version must be 1")
    status = value.get("status")
    if status not in {"completed", "failed", "cancelled"}:
        invalid("result.status must be completed, failed, or cancelled")
    valid = value.get("valid_observation")
    if not isinstance(valid, bool):
        invalid("result.valid_observation must be boolean")
    if status in {"failed", "cancelled"} and valid:
        invalid("failed or cancelled results cannot be valid observations")
    summary = value.get("summary")
    if not isinstance(summary, str):
        invalid("result.summary must be a string")
    metrics = value.get("metrics", {})
    if not isinstance(metrics, dict):
        invalid("result.metrics must be an object")
    actual = (
        None
        if value.get("actual_cost_usd") is None
        else money(value["actual_cost_usd"], "result.actual_cost_usd")
    )
    execution = value.get("execution")
    if execution is not None and not isinstance(execution, dict):
        invalid("result.execution must be an object")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "valid_observation": valid,
        "summary": summary,
        "metrics": metrics,
        "actual_cost_usd": money_text(actual) if actual is not None else None,
        "execution": execution,
    }


def finish_attempt(client, identifier, result_value, artifacts):
    attempt_record, attempt = find_attempt(client, identifier)
    root_record, root = find_experiment(client, attempt["experiment_id"])
    condition = next(
        body for _, body in condition_pairs(client, root) if body["id"] == attempt["condition_id"]
    )
    result = validate_result(result_value)
    artifacts = {
        name: validate_artifact_envelope(name, envelope)
        for name, envelope in artifacts.items()
    }
    artifact_names = set(artifacts)
    if result["valid_observation"]:
        missing = sorted(set(condition["required_artifacts"]) - artifact_names)
        if missing:
            invalid("valid result is missing required artifacts", {"missing": missing})
    artifact_summary = [
        {key: envelope[key] for key in ("name", "filename", "media_type", "byte_length", "sha256", "encoding")}
        for envelope in sorted(artifacts.values(), key=lambda item: item["name"])
    ]
    submission = {**result, "artifacts": artifact_summary}
    submission_hash = hash_text(canonical_json(submission))
    result_key = f"{attempt_record['key']}:result"
    existing = client.unique_by_key("ongo-experiment-result", result_key)
    if existing:
        existing_body = parse_body(existing)
        if existing_body.get("submission_sha256") == submission_hash:
            return {"ok": True, "result": existing_body, "record_id": existing["id"], "idempotent": True}
        conflict("attempt already has a different terminal result", {"attempt_id": attempt["attempt_id"]})
    result_id = str(uuid.uuid4())
    body = {
        **result,
        "result_id": result_id,
        "experiment_id": root["experiment_id"],
        "attempt_id": attempt["attempt_id"],
        "condition_id": attempt["condition_id"],
        "submission_sha256": submission_hash,
        "artifacts": artifact_summary,
        "finished_at": utc_now(),
    }
    publications = [
        {"ref": "result", "kind": "ongo-experiment-result", "key": result_key, "title": f"Result for {attempt_record['title']}"}
    ]
    notes = [{"publication": "result", "body": canonical_json(body)}]
    relationships = [
        {"subject": "result", "object": attempt_record["id"], "kind": "ongo-result-of"}
    ]
    safe_names = set()
    for index, envelope in enumerate(sorted(artifacts.values(), key=lambda item: item["name"])):
        safe = safe_artifact_name(envelope["name"])
        if safe in safe_names:
            invalid("artifact names collide after key normalization", {"name": envelope["name"]})
        safe_names.add(safe)
        reference = f"artifact-{index}"
        artifact_body = {
            **envelope,
            "artifact_id": str(uuid.uuid4()),
            "experiment_id": root["experiment_id"],
            "attempt_id": attempt["attempt_id"],
            "created_at": utc_now(),
        }
        publications.append(
            {"ref": reference, "kind": "ongo-experiment-artifact", "key": f"{attempt_record['key']}:artifact:{safe}", "title": envelope["name"]}
        )
        notes.append({"publication": reference, "body": canonical_json(artifact_body)})
        relationships.append({"subject": "result", "object": reference, "kind": "ongo-produced"})
    loaded = client.load(
        {"publications": publications, "relationships": relationships, "notes": notes}
    )
    return {"ok": True, "result": body, "record_id": loaded["refs"]["result"], "idempotent": False}


def cancel_attempt(client, identifier, reason):
    attempt_record, attempt = find_attempt(client, identifier)
    value = {
        "schema_version": SCHEMA_VERSION,
        "status": "cancelled",
        "valid_observation": False,
        "summary": reason,
        "metrics": {},
        "actual_cost_usd": None,
    }
    return finish_attempt(client, attempt["attempt_id"], value, {})


def local_artifacts(execution, cwd, stdout, stderr):
    artifacts = {
        "stdout": artifact_envelope("stdout", "stdout.txt", "text/plain", stdout),
        "stderr": artifact_envelope("stderr", "stderr.txt", "text/plain", stderr),
    }
    errors = []
    for output in execution["output_files"]:
        path = Path(output["path"])
        if not path.is_absolute():
            path = Path(cwd) / path
        try:
            content = path.read_bytes()
        except OSError as error:
            errors.append(
                {"name": output["name"], "path": str(path), "error": str(error)}
            )
            continue
        artifacts[output["name"]] = artifact_envelope(
            output["name"], path.name, output.get("media_type"), content
        )
    return artifacts, errors


def run_local(client, identifier):
    root_record, root = find_experiment(client, identifier)
    if open_attempt(client, root["experiment_id"]):
        conflict("finish or cancel the open attempt before running local conditions")
    while True:
        selected = next_initial_condition(client, root)
        if selected is None:
            break
        condition_record, condition = selected
        if condition["execution"]["mode"] != "local":
            break
        created = create_attempt(
            client, root_record, root, condition_record, condition, "ongo-local-runner", False
        )
        attempt = created["attempt"]
        execution = condition["execution"]
        cwd = str(Path(execution["cwd"]).expanduser().resolve())
        environment = os.environ.copy()
        environment.update(execution["env"])
        started = utc_now()
        start_monotonic = time.monotonic()
        timed_out = False
        launch_error = None
        try:
            process = subprocess.run(
                execution["argv"],
                cwd=cwd,
                env=environment,
                capture_output=True,
                timeout=execution["timeout_seconds"],
            )
            returncode = process.returncode
            stdout = process.stdout
            stderr = process.stderr
        except subprocess.TimeoutExpired as error:
            timed_out = True
            returncode = None
            stdout = error.stdout or b""
            stderr = error.stderr or b""
            if isinstance(stdout, str):
                stdout = stdout.encode()
            if isinstance(stderr, str):
                stderr = stderr.encode()
        except Exception as error:
            returncode = None
            stdout = b""
            stderr = str(error).encode("utf-8", errors="replace")
            launch_error = str(error)
        duration = time.monotonic() - start_monotonic
        accepted = (
            not timed_out
            and launch_error is None
            and returncode in execution["accepted_exit_codes"]
        )
        artifacts, output_errors = local_artifacts(
            execution, cwd, stdout, stderr
        )
        if output_errors:
            accepted = False
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "completed" if accepted else "failed",
            "valid_observation": accepted,
            "summary": (
                "local command completed"
                if accepted
                else (
                    "local command timed out"
                    if timed_out
                    else (
                        "declared output files were missing or unreadable"
                        if output_errors
                        else f"local command could not start: {launch_error}"
                        if launch_error
                        else f"local command exited {returncode}"
                    )
                )
            ),
            "metrics": {},
            "actual_cost_usd": condition["expected_cost_usd"],
            "execution": {
                "argv": execution["argv"],
                "cwd": cwd,
                "env_additions": execution["env"],
                "started_at": started,
                "finished_at": utc_now(),
                "duration_seconds": duration,
                "exit_code": returncode,
                "timed_out": timed_out,
                "launch_error": launch_error,
                "output_file_errors": output_errors,
            },
        }
        finish_attempt(client, attempt["attempt_id"], result, artifacts)
    status = state_for_experiment(client, identifier)
    return status, 0 if status["complete"] else 6


def verify_experiment(client, identifier):
    status = state_for_experiment(client, identifier)
    return status, 0 if status["complete"] else 6


def client_from_args(args):
    client = KenClient(binary=args.ken, db=args.db)
    client.ensure_kinds()
    return client


def add_common(parser):
    parser.add_argument("--ken", help="path to Ken v3")
    parser.add_argument("--db", help="path to the Ken database")


def build_parser():
    parser = OngoArgumentParser(prog="ongo experiment")
    add_common(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    create.add_argument("--document", required=True)
    create.add_argument("--manifest", required=True)
    create.add_argument("--successor-of")

    show = commands.add_parser("show")
    show.add_argument("id")
    show.add_argument("--format", choices=("json", "markdown"), default="json")

    render = commands.add_parser("render")
    render.add_argument("id")
    render.add_argument("--out", required=True)

    status = commands.add_parser("status")
    status.add_argument("id")
    status.add_argument("--json", action="store_true")

    note = commands.add_parser("note")
    note_commands = note.add_subparsers(dest="note_command", required=True)
    note_add = note_commands.add_parser("add")
    note_add.add_argument("id")
    note_add.add_argument("--actor", required=True)
    note_content = note_add.add_mutually_exclusive_group(required=True)
    note_content.add_argument("--text")
    note_content.add_argument("--file")
    note_target = note_add.add_mutually_exclusive_group()
    note_target.add_argument("--condition")
    note_target.add_argument("--attempt")
    note_add.add_argument("--topic", action="append", default=[])
    note_add.add_argument("--operation-key")
    note_list = note_commands.add_parser("list")
    note_list.add_argument("id")
    note_list.add_argument("--format", choices=("json", "markdown"), default="json")

    delegate = commands.add_parser("delegate")
    delegate_commands = delegate.add_subparsers(dest="delegate_command", required=True)
    delegate_create = delegate_commands.add_parser("create")
    delegate_create.add_argument("--granted-by", required=True)
    delegate_create.add_argument("--evidence", required=True)
    delegate_create.add_argument("--max-per-experiment-usd", required=True)
    delegate_create.add_argument("--max-total-usd")
    delegate_create.add_argument("--expires-at", required=True)
    delegate_create.add_argument("--mode", action="append", choices=("manual", "local"))
    delegate_create.add_argument("--experiment")

    approve = commands.add_parser("approve")
    approve.add_argument("id")
    approve.add_argument("--delegation")
    approve.add_argument("--actor", required=True)
    approve.add_argument("--actor-role", default="driver", choices=("driver", "worker"))

    begin = commands.add_parser("begin")
    begin.add_argument("id")
    begin.add_argument("--worker", required=True)

    finish = commands.add_parser("finish")
    finish.add_argument("attempt_id")
    finish.add_argument("--result", required=True)
    finish.add_argument("--artifact", action="append", default=[])

    cancel = commands.add_parser("cancel")
    cancel.add_argument("attempt_id")
    cancel.add_argument("--reason", required=True)

    retry = commands.add_parser("retry")
    retry.add_argument("id")
    retry.add_argument("--condition", required=True)
    retry.add_argument("--worker", required=True)

    run = commands.add_parser("run")
    run.add_argument("id")

    verify = commands.add_parser("verify")
    verify.add_argument("id")
    verify.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    client = client_from_args(args)
    if args.command == "create":
        emit_json(create_experiment(client, args.document, args.manifest, args.successor_of))
        return 0
    if args.command == "show":
        if args.format == "markdown":
            sys.stdout.write(markdown_view(client, args.id))
        else:
            emit_json(experiment_view(client, args.id))
        return 0
    if args.command == "render":
        emit_json(render_experiment(client, args.id, args.out))
        return 0
    if args.command == "status":
        emit_json(state_for_experiment(client, args.id))
        return 0
    if args.command == "note":
        if args.note_command == "add":
            emit_json(
                add_experiment_note(
                    client,
                    args.id,
                    actor=args.actor,
                    markdown=read_note_markdown(args.text, args.file),
                    condition_id=args.condition,
                    attempt_identifier=args.attempt,
                    topic_identifiers=args.topic,
                    operation_key=args.operation_key,
                )
            )
        elif args.format == "markdown":
            sys.stdout.write(experiment_notes_markdown(client, args.id))
        else:
            emit_json(experiment_notes_view(client, args.id))
        return 0
    if args.command == "delegate":
        emit_json(create_delegation(client, args))
        return 0
    if args.command == "approve":
        emit_json(approve_experiment(client, args.id, args.delegation, args.actor, args.actor_role))
        return 0
    if args.command == "begin":
        emit_json(begin_experiment(client, args.id, args.worker))
        return 0
    if args.command == "finish":
        result = validate_result(load_json_file(args.result, "result"))
        artifacts = read_artifact_specs(args.artifact)
        emit_json(finish_attempt(client, args.attempt_id, result, artifacts))
        return 0
    if args.command == "cancel":
        emit_json(cancel_attempt(client, args.attempt_id, args.reason))
        return 0
    if args.command == "retry":
        emit_json(retry_condition(client, args.id, args.condition, args.worker))
        return 0
    if args.command == "run":
        payload, exit_code = run_local(client, args.id)
        emit_json(payload)
        return exit_code
    if args.command == "verify":
        payload, exit_code = verify_experiment(client, args.id)
        emit_json(payload)
        return exit_code
    return 2
