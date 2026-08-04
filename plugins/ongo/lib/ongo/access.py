"""Symmetric access-key management for protected static Ongo sites."""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import getpass
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .errors import OngoArgumentParser, OngoError, emit_json
from .ken import KenClient, default_data_dir


SCHEMA_VERSION = 1
CAPABILITY_PREFIX = "ongo-key-v1."
KEY_BYTES = 32
KEYRING_ENV = "ONGO_SITE_KEYRING"


def canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def base64url_encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def base64url_decode(value):
    if not isinstance(value, str) or not value:
        raise OngoError(
            "access capability is empty",
            code="invalid-access-capability",
            exit_code=2,
        )
    if re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise OngoError(
            "access capability must use canonical unpadded base64url",
            code="invalid-access-capability",
            exit_code=2,
        )
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as error:
        raise OngoError(
            "access capability is not valid base64url",
            code="invalid-access-capability",
            exit_code=2,
        ) from error
    if len(raw) != KEY_BYTES:
        raise OngoError(
            "access capability must contain a 256-bit key",
            code="invalid-access-capability",
            exit_code=2,
            details={"decoded_bytes": len(raw)},
        )
    if base64url_encode(raw) != value:
        raise OngoError(
            "access capability must use canonical unpadded base64url",
            code="invalid-access-capability",
            exit_code=2,
        )
    return raw


def parse_capability(value):
    text = value.strip()
    if text.startswith(CAPABILITY_PREFIX):
        text = text[len(CAPABILITY_PREFIX):]
    return base64url_decode(text)


def format_capability(secret):
    return CAPABILITY_PREFIX + base64url_encode(secret)


def key_fingerprint(secret):
    return hashlib.sha256(secret).hexdigest()


def resolve_keyring_path(explicit=None):
    configured = explicit or os.environ.get(KEYRING_ENV)
    if configured:
        path = Path(configured).expanduser()
    else:
        path = default_data_dir() / "site-keys.json"
    try:
        # All readers, locks, and atomic writers must agree on one path. In
        # particular, replacing a symlink alias would otherwise fork the
        # administrator keyring from its canonical target.
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise OngoError(
            "could not resolve the site keyring path",
            code="site-keyring-path-invalid",
            exit_code=3,
            details={"path": str(path), "error": str(error)},
        ) from error


def validate_keyring_location(path, **reserved_paths):
    """Reject administrator keyrings inside any tool-owned site tree."""
    resolved_keyring = Path(path).resolve()
    for label, reserved in reserved_paths.items():
        resolved_reserved = Path(reserved).resolve()
        if resolved_keyring == resolved_reserved or resolved_keyring.is_relative_to(
            resolved_reserved
        ):
            raise OngoError(
                "the administrator keyring must be outside generated site paths",
                code="unsafe-site-keyring-path",
                exit_code=3,
                details={
                    "keyring": str(path),
                    "reserved_path": str(reserved),
                    "reserved_role": label,
                },
            )
    return path


def empty_keyring():
    return {"schema_version": SCHEMA_VERSION, "keys": []}


def validate_keyring(value, path):
    if not isinstance(value, dict):
        raise OngoError(
            "site keyring must be a JSON object",
            code="invalid-site-keyring",
            exit_code=3,
            details={"path": str(path)},
        )
    if value.get("schema_version") != SCHEMA_VERSION or not isinstance(
        value.get("keys"), list
    ):
        raise OngoError(
            "site keyring has an unsupported schema",
            code="invalid-site-keyring",
            exit_code=3,
            details={"path": str(path)},
        )
    seen_ids = set()
    seen_fingerprints = set()
    normalized = empty_keyring()
    for entry in value["keys"]:
        if not isinstance(entry, dict):
            raise OngoError(
                "site keyring contains a non-object entry",
                code="invalid-site-keyring",
                exit_code=3,
                details={"path": str(path)},
            )
        required = {"key_id", "label", "secret", "fingerprint", "created_at"}
        if set(entry) != required or not all(
            isinstance(entry.get(name), str) and entry[name]
            for name in required
        ):
            raise OngoError(
                "site keyring contains an invalid key entry",
                code="invalid-site-keyring",
                exit_code=3,
                details={"path": str(path)},
            )
        try:
            secret = base64url_decode(entry["secret"])
        except OngoError as error:
            raise OngoError(
                "site keyring contains invalid key material",
                code="invalid-site-keyring",
                exit_code=3,
                details={"path": str(path), "key_id": entry["key_id"]},
            ) from error
        fingerprint = key_fingerprint(secret)
        if entry["fingerprint"] != fingerprint:
            raise OngoError(
                "site keyring fingerprint does not match its secret",
                code="invalid-site-keyring",
                exit_code=3,
                details={"path": str(path), "key_id": entry["key_id"]},
            )
        if entry["key_id"] in seen_ids or fingerprint in seen_fingerprints:
            raise OngoError(
                "site keyring contains a duplicate key",
                code="invalid-site-keyring",
                exit_code=3,
                details={"path": str(path), "key_id": entry["key_id"]},
            )
        seen_ids.add(entry["key_id"])
        seen_fingerprints.add(fingerprint)
        normalized["keys"].append(dict(entry))
    return normalized


def load_keyring(explicit=None):
    path = resolve_keyring_path(explicit)
    if not path.exists():
        return path, empty_keyring()
    validate_keyring_inode(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OngoError(
            "could not read the site keyring",
            code="site-keyring-read-failed",
            exit_code=3,
            details={"path": str(path), "error": str(error)},
        ) from error
    return path, validate_keyring(value, path)


def validate_keyring_inode(path):
    """Reject aliases that atomic replacement would update independently."""
    try:
        metadata = path.stat()
    except OSError as error:
        raise OngoError(
            "could not inspect the site keyring",
            code="site-keyring-read-failed",
            exit_code=3,
            details={"path": str(path), "error": str(error)},
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise OngoError(
            "the site keyring must be a regular file",
            code="site-keyring-invalid-file",
            exit_code=3,
            details={"path": str(path)},
        )
    if metadata.st_nlink != 1:
        raise OngoError(
            "hard-linked site keyrings are not supported",
            code="site-keyring-hardlink-unsupported",
            exit_code=3,
            details={"path": str(path), "links": metadata.st_nlink},
        )


@contextmanager
def lock_keyring(explicit=None):
    """Serialize keyring read-modify-write transactions across processes."""
    path = resolve_keyring_path(explicit)
    lock_path = path.with_name(f".{path.name}.lock")
    descriptor = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if path.exists():
            validate_keyring_inode(path)
    except OngoError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise OngoError(
            "could not lock the site keyring",
            code="site-keyring-lock-failed",
            exit_code=3,
            details={"path": str(path), "lock_path": str(lock_path), "error": str(error)},
        ) from error
    try:
        yield path
    finally:
        os.close(descriptor)


def save_private_json(path, value, *, message, code):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = None
    temporary = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(canonical_json(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        raise OngoError(
            message,
            code=code,
            exit_code=3,
            details={"path": str(path), "error": str(error)},
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None and temporary.exists():
            temporary.unlink()


def save_keyring(path, value):
    save_private_json(
        path,
        value,
        message="could not write the site keyring",
        code="site-keyring-write-failed",
    )


def database_identity(client):
    """Return the stable local identity used to scope recovery journals."""
    return str(Path(client.db).expanduser().resolve(strict=False))


def pending_create_path(keyring_path, database):
    """Keep independent recovery state for every database sharing a keyring."""
    digest = hashlib.sha256(os.fsencode(database)).hexdigest()[:16]
    return keyring_path.with_name(
        f".{keyring_path.name}.pending-create.{digest}.json"
    )


def save_pending_create(keyring_path, database, value):
    save_private_json(
        pending_create_path(keyring_path, database),
        value,
        message="could not write the pending access-key creation",
        code="access-key-journal-write-failed",
    )


def clear_pending_create(keyring_path, database):
    path = pending_create_path(keyring_path, database)
    try:
        path.unlink(missing_ok=True)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        raise OngoError(
            "could not clear the pending access-key creation",
            code="access-key-journal-write-failed",
            exit_code=3,
            details={"path": str(path), "error": str(error)},
        ) from error


def list_all_publications(client):
    rows = []
    offset = 0
    while True:
        result = client.command(
            "list", "--limit", "500", "--offset", str(offset)
        )
        try:
            page = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as error:
            raise OngoError(
                "ken list returned invalid JSON",
                code="ken-json-invalid",
                exit_code=3,
            ) from error
        if not isinstance(page, list):
            raise OngoError(
                "ken list returned an unexpected JSON shape",
                code="ken-json-invalid",
                exit_code=3,
            )
        rows.extend(page)
        if len(page) < 500:
            return rows
        offset += len(page)


def resolve_publication(client, identifier, *, expected_kind=None):
    rows = list_all_publications(client)
    matches_by_id = {
        row["id"]: row
        for row in rows
        if row.get("id") == identifier
        or (
            row.get("key") == identifier
            and row.get("kind") != "ongo-web"
        )
    }
    matches = list(matches_by_id.values())
    if expected_kind is not None:
        matches = [row for row in matches if row.get("kind") == expected_kind]
    if not matches:
        raise OngoError(
            "Ken publication was not found",
            code="publication-not-found",
            exit_code=2,
            details={"identifier": identifier, "kind": expected_kind},
        )
    if len(matches) > 1:
        raise OngoError(
            "Ken publication identifier is ambiguous",
            code="publication-conflict",
            exit_code=4,
            details={"identifier": identifier, "count": len(matches)},
        )
    return matches[0]


def published_target_ids(client):
    rows = list_all_publications(client)
    by_id = {row["id"]: row for row in rows}
    by_key = {}
    for row in rows:
        if row.get("kind") != "ongo-web" and row.get("key"):
            by_key.setdefault(row["key"], []).append(row)
    targets = set()
    for marker in (row for row in rows if row.get("kind") == "ongo-web"):
        reference = marker.get("key")
        if not reference:
            continue
        matches = {}
        if reference in by_id:
            matches[by_id[reference]["id"]] = by_id[reference]
        for candidate in by_key.get(reference, []):
            matches[candidate["id"]] = candidate
        if len(matches) > 1:
            raise OngoError(
                "Ken publication identifier is ambiguous",
                code="publication-conflict",
                exit_code=4,
                details={
                    "identifier": reference,
                    "count": len(matches),
                    "marker_id": marker["id"],
                },
            )
        target = next(iter(matches.values()), None)
        if target is not None and target.get("kind") not in {
            "ongo-web",
            "ongo-access-key",
        }:
            targets.add(target["id"])
    targets.update(
        row["id"] for row in rows if row.get("kind") == "ongo-digest"
    )
    return sorted(targets)


def parse_key_metadata(record):
    try:
        value = json.loads(record.get("body") or "")
    except json.JSONDecodeError as error:
        raise OngoError(
            "access-key descriptor contains invalid JSON",
            code="invalid-access-key-descriptor",
            exit_code=3,
            details={"record_id": record.get("id")},
        ) from error
    required = {"schema_version", "key_id", "label", "scope", "created_at"}
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("scope") not in {"all", "explicit"}
        or not all(
            isinstance(value.get(name), str) and value[name]
            for name in required - {"schema_version"}
        )
    ):
        raise OngoError(
            "access-key descriptor has an unsupported schema",
            code="invalid-access-key-descriptor",
            exit_code=3,
            details={"record_id": record.get("id")},
        )
    return value


def access_key_records(client):
    records = []
    seen_ids = set()
    for record in client.records("ongo-access-key"):
        metadata = parse_key_metadata(record)
        if metadata["key_id"] in seen_ids:
            raise OngoError(
                "duplicate access-key descriptors violate the Ongo protocol",
                code="duplicate-access-key",
                exit_code=4,
                details={"key_id": metadata["key_id"]},
            )
        seen_ids.add(metadata["key_id"])
        records.append((record, metadata))
    return records


def find_access_key(client, identifier):
    matches = []
    for record, metadata in access_key_records(client):
        if identifier in {record.get("id"), record.get("key"), metadata["key_id"]}:
            matches.append((record, metadata))
    if not matches:
        raise OngoError(
            "access-key descriptor was not found",
            code="access-key-not-found",
            exit_code=2,
            details={"identifier": identifier},
        )
    if len(matches) > 1:
        raise OngoError(
            "access-key identifier is ambiguous",
            code="access-key-conflict",
            exit_code=4,
            details={"identifier": identifier},
        )
    return matches[0]


def load_pending_create(keyring_path, database):
    path = pending_create_path(keyring_path, database)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OngoError(
            "could not read the pending access-key creation",
            code="access-key-journal-invalid",
            exit_code=3,
            details={"path": str(path), "error": str(error)},
        ) from error
    required = {
        "schema_version",
        "database",
        "entry",
        "metadata",
        "requested_scope",
        "published_ids",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("database") != database
        or value.get("requested_scope") not in {"all", "published", "empty"}
        or not isinstance(value.get("published_ids"), list)
        or not all(
            isinstance(publication_id, str) and publication_id
            for publication_id in value.get("published_ids", [])
        )
        or len(set(value.get("published_ids", [])))
        != len(value.get("published_ids", []))
    ):
        raise OngoError(
            "pending access-key creation has an invalid schema",
            code="access-key-journal-invalid",
            exit_code=3,
            details={"path": str(path)},
        )
    entry = validate_keyring(
        {"schema_version": SCHEMA_VERSION, "keys": [value.get("entry")]},
        path,
    )["keys"][0]
    metadata = value.get("metadata")
    expected_metadata = {
        "schema_version",
        "key_id",
        "label",
        "scope",
        "created_at",
    }
    if (
        not isinstance(metadata, dict)
        or set(metadata) != expected_metadata
        or metadata.get("schema_version") != SCHEMA_VERSION
        or metadata.get("scope") not in {"all", "explicit"}
        or metadata.get("key_id") != entry["key_id"]
        or metadata.get("label") != entry["label"]
        or metadata.get("created_at") != entry["created_at"]
        or (
            metadata.get("scope") == "all"
            and value["requested_scope"] != "all"
        )
        or (
            metadata.get("scope") == "explicit"
            and value["requested_scope"] == "all"
        )
    ):
        raise OngoError(
            "pending access-key creation has conflicting metadata",
            code="access-key-journal-invalid",
            exit_code=3,
            details={"path": str(path)},
        )
    value["entry"] = entry
    return value


def pending_load_payload(pending):
    key_id = pending["metadata"]["key_id"]
    return {
        "publications": [
            {
                "ref": "key",
                "kind": "ongo-access-key",
                "key": f"ongo-access-key:{key_id}",
                "title": pending["metadata"]["label"],
            }
        ],
        "relationships": [
            {
                "subject": publication_id,
                "object": "key",
                "kind": "ongo-readable-by",
            }
            for publication_id in pending["published_ids"]
        ],
        "notes": [
            {
                "publication": "key",
                "body": canonical_json(pending["metadata"]),
            }
        ],
    }


def reconcile_pending_create(client, keyring_path, keyring, pending):
    database = database_identity(client)
    if pending["database"] != database:
        raise OngoError(
            "pending access-key creation belongs to another Ken database",
            code="access-key-journal-database-conflict",
            exit_code=4,
            details={"expected": database, "recorded": pending["database"]},
        )
    entry = pending["entry"]
    matches = [
        candidate
        for candidate in keyring["keys"]
        if candidate["key_id"] == entry["key_id"]
        or candidate["fingerprint"] == entry["fingerprint"]
    ]
    if matches and matches != [entry]:
        raise OngoError(
            "pending access-key material conflicts with the keyring",
            code="access-key-conflict",
            exit_code=4,
            details={"key_id": entry["key_id"]},
        )
    if not matches:
        updated = {
            "schema_version": SCHEMA_VERSION,
            "keys": [*keyring["keys"], entry],
        }
        updated["keys"].sort(key=lambda item: item["key_id"])
        save_keyring(keyring_path, updated)
        keyring = updated

    try:
        record, metadata = find_access_key(client, entry["key_id"])
    except OngoError as error:
        if error.code != "access-key-not-found":
            raise
        try:
            loaded = client.load(pending_load_payload(pending))
        except Exception as load_error:
            try:
                record, metadata = find_access_key(client, entry["key_id"])
            except Exception as recovery_error:
                raise load_error from recovery_error
        else:
            record = client.show(loaded["refs"]["key"])
            metadata = parse_key_metadata(record)

    if metadata != pending["metadata"]:
        raise OngoError(
            "committed access-key metadata conflicts with the pending creation",
            code="access-key-conflict",
            exit_code=4,
            details={"key_id": entry["key_id"]},
        )
    expected_subjects = set(pending["published_ids"])
    observed_subjects = {
        relationship.get("publication")
        for relationship in record.get("relationships", [])
        if relationship.get("role") == "object"
        and relationship.get("relkind") == "ongo-readable-by"
    }
    if not expected_subjects.issubset(observed_subjects):
        raise OngoError(
            "committed access-key policy is incomplete",
            code="access-key-conflict",
            exit_code=4,
            details={"key_id": entry["key_id"]},
        )
    clear_pending_create(keyring_path, database)
    return {
        "created": True,
        "record_id": record["id"],
        "key_id": entry["key_id"],
        "label": entry["label"],
        "scope": pending["requested_scope"],
        "capability": format_capability(base64url_decode(entry["secret"])),
        "published_resources": len(pending["published_ids"]),
        "keyring": str(keyring_path),
    }


def create_key(client, *, label, scope, keyring_path=None, capability=None):
    label = label.strip()
    if not label:
        raise OngoError(
            "access-key label must not be empty",
            code="invalid-input",
            exit_code=2,
        )
    if scope not in {"all", "published", "empty"}:
        raise OngoError(
            "access-key scope must be all, published, or empty",
            code="invalid-input",
            exit_code=2,
        )
    secret = parse_capability(capability) if capability else secrets.token_bytes(KEY_BYTES)
    fingerprint = key_fingerprint(secret)
    with lock_keyring(keyring_path) as path:
        database = database_identity(client)
        client.ensure_kinds()
        _, original = load_keyring(path)
        pending = load_pending_create(path, database)
        if pending is not None:
            recovered = reconcile_pending_create(client, path, original, pending)
            if (
                capability is None
                or pending["entry"]["fingerprint"] == fingerprint
            ):
                return recovered
            _, original = load_keyring(path)
        existing = next(
            (
                entry
                for entry in original["keys"]
                if entry["fingerprint"] == fingerprint
            ),
            None,
        )
        if existing is not None:
            try:
                record, metadata = find_access_key(client, existing["key_id"])
            except OngoError as error:
                if error.code != "access-key-not-found":
                    raise
                # Recover a keyring entry left behind by an interrupted load, or
                # deliberately reuse an administrator keyring with another Ken DB.
                key_id = existing["key_id"]
                created_at = existing["created_at"]
                label = existing["label"]
                entry = existing
            else:
                return {
                    "created": False,
                    "record_id": record["id"],
                    "key_id": metadata["key_id"],
                    "label": metadata["label"],
                    "scope": metadata["scope"],
                    "capability": format_capability(secret),
                    "keyring": str(path),
                }
        else:
            key_id = str(uuid.uuid4())
            created_at = utc_now()
        descriptor_scope = "all" if scope == "all" else "explicit"
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "key_id": key_id,
            "label": label,
            "scope": descriptor_scope,
            "created_at": created_at,
        }
        published_ids = published_target_ids(client) if scope == "published" else []
        if existing is None:
            entry = {
                "key_id": key_id,
                "label": label,
                "secret": base64url_encode(secret),
                "fingerprint": fingerprint,
                "created_at": created_at,
            }
        pending = {
            "schema_version": SCHEMA_VERSION,
            "database": database,
            "entry": entry,
            "metadata": metadata,
            "requested_scope": scope,
            "published_ids": published_ids,
        }
        # The journal precedes both durable side effects. A later create can
        # therefore replay any process death between keyring and Ken writes.
        save_pending_create(path, database, pending)
        return reconcile_pending_create(client, path, original, pending)


def list_keys(client, keyring_path=None):
    path, keyring = load_keyring(keyring_path)
    local = {entry["key_id"]: entry for entry in keyring["keys"]}
    result = []
    for record, metadata in access_key_records(client):
        entry = local.get(metadata["key_id"])
        result.append(
            {
                "record_id": record["id"],
                "key_id": metadata["key_id"],
                "label": metadata["label"],
                "scope": metadata["scope"],
                "created_at": metadata["created_at"],
                "key_material_available": entry is not None,
                "fingerprint": entry["fingerprint"][:16] if entry else None,
            }
        )
    return {"keyring": str(path), "keys": result}


def export_key(client, identifier, keyring_path=None):
    path, keyring = load_keyring(keyring_path)
    record, metadata = find_access_key(client, identifier)
    entry = next(
        (item for item in keyring["keys"] if item["key_id"] == metadata["key_id"]),
        None,
    )
    if entry is None:
        raise OngoError(
            "access-key material is not available in the local keyring",
            code="access-key-material-missing",
            exit_code=3,
            details={"key_id": metadata["key_id"], "keyring": str(path)},
        )
    return {
        "record_id": record["id"],
        "key_id": metadata["key_id"],
        "label": metadata["label"],
        "capability": CAPABILITY_PREFIX + entry["secret"],
    }


@contextmanager
def lock_access_policy(client):
    """Serialize idempotent access-policy transitions for one Ken database."""
    database = Path(client.db).expanduser().resolve(strict=False)
    lock_path = database.with_name(f".{database.name}.ongo-access.lock")
    descriptor = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise OngoError(
            "could not lock the access policy",
            code="access-policy-lock-failed",
            exit_code=3,
            details={"database": str(database), "error": str(error)},
        ) from error
    try:
        yield
    finally:
        os.close(descriptor)


def grant_key(client, key_identifier, publication_identifier):
    with lock_access_policy(client):
        key_record, metadata = find_access_key(client, key_identifier)
        publication = resolve_publication(client, publication_identifier)
        if publication["kind"] in {"ongo-access-key", "ongo-web"}:
            raise OngoError(
                "access can only be granted to a publishable resource",
                code="invalid-input",
                exit_code=2,
                details={"kind": publication["kind"]},
            )
        shown = client.show(publication["id"])
        for relationship in shown.get("relationships", []):
            if (
                relationship.get("role") == "subject"
                and relationship.get("relkind") == "ongo-readable-by"
                and relationship.get("publication") == key_record["id"]
            ):
                return {
                    "created": False,
                    "publication_id": publication["id"],
                    "key_id": metadata["key_id"],
                }
        relation_id = client.command(
            "relate",
            "--subject",
            publication["id"],
            "--object",
            key_record["id"],
            "--relation",
            "ongo-readable-by",
        ).stdout.strip()
        return {
            "created": True,
            "relationship_id": relation_id,
            "publication_id": publication["id"],
            "key_id": metadata["key_id"],
        }


def read_import_capability():
    """Read imported secret material without placing it in process argv."""
    if sys.stdin.isatty():
        value = getpass.getpass("Ongo access capability: ")
    else:
        value = sys.stdin.read()
    value = value.strip()
    if not value:
        raise OngoError(
            "an access capability is required on stdin",
            code="invalid-access-capability",
            exit_code=2,
        )
    return value


def build_parser():
    parser = OngoArgumentParser(prog="ongo key")
    parser.add_argument("--ken")
    parser.add_argument("--db")
    parser.add_argument("--keyring")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="generate and register an access key")
    create.add_argument("--label", required=True)
    create.add_argument(
        "--scope",
        choices=("all", "published", "empty"),
        default="all",
        help="all current/future resources, current publish set, or no resources",
    )

    imported = subparsers.add_parser(
        "import",
        help="register a capability read from stdin or a hidden terminal prompt",
    )
    imported.add_argument("--label", required=True)
    imported.add_argument("--capability", help=argparse.SUPPRESS)
    imported.add_argument(
        "--scope", choices=("all", "published", "empty"), default="empty"
    )

    subparsers.add_parser("list", help="list access-key metadata")

    exported = subparsers.add_parser("export", help="reveal a shareable capability")
    exported.add_argument("key")

    grant = subparsers.add_parser("grant", help="grant a key access to one resource")
    grant.add_argument("key")
    grant.add_argument("publication")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    client = KenClient(binary=args.ken, db=args.db)
    if args.command == "create":
        emit_json(
            create_key(
                client,
                label=args.label,
                scope=args.scope,
                keyring_path=args.keyring,
            )
        )
    elif args.command == "import":
        if args.capability is not None:
            raise OngoError(
                "capability arguments are not accepted; use stdin or the hidden prompt",
                code="invalid-input",
                exit_code=2,
            )
        emit_json(
            create_key(
                client,
                label=args.label,
                scope=args.scope,
                keyring_path=args.keyring,
                capability=read_import_capability(),
            )
        )
    elif args.command == "list":
        emit_json(list_keys(client, args.keyring))
    elif args.command == "export":
        emit_json(export_key(client, args.key, args.keyring))
    elif args.command == "grant":
        emit_json(grant_key(client, args.key, args.publication))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
