"""Durable agent-state migration helpers."""

from __future__ import annotations

import json
import os
import tempfile
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .errors import OngoError


LEGACY_STATE_PATH = Path("/tmp/ongo_state.json")
LEGACY_TOMBSTONE_SCHEMA = "ongo-state-migrated-v1"
MIGRATION_RECORD_SCHEMA = "ongo-state-migration-v1"


def legacy_state_path() -> Path:
    configured = os.environ.get("ONGO_LEGACY_STATE_PATH")
    return Path(configured).expanduser() if configured else LEGACY_STATE_PATH


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    temporary_path = Path(temporary)
    try:
        os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OngoError(
            "failed to read legacy Ongo state",
            code="legacy-state-invalid",
            exit_code=3,
            details={"path": str(path), "error": str(error)},
        ) from error
    if not isinstance(payload, dict):
        raise OngoError(
            "legacy Ongo state must be a JSON object",
            code="legacy-state-invalid",
            exit_code=3,
            details={"path": str(path)},
        )
    return payload


def _cron_values(field: str, *, minimum: int, maximum: int) -> list[int] | None:
    """Expand the fixed-width cron forms used by Ongo's legacy scheduler."""
    values = set()
    for part in field.split(","):
        if part == "*":
            values.update(range(minimum, maximum + 1))
            continue
        if part.startswith("*/"):
            try:
                step = int(part[2:])
            except ValueError:
                return None
            if step <= 0:
                return None
            values.update(range(minimum, maximum + 1, step))
            continue
        try:
            value = int(part)
        except ValueError:
            return None
        if not minimum <= value <= maximum:
            return None
        values.add(value)
    return sorted(values) or None


def _normal_interval_minutes(expression) -> int:
    """Return a fixed cadence for simple minute/hour cron expressions."""
    fields = str(expression or "").strip().split()
    if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
        return 30
    minutes = _cron_values(fields[0], minimum=0, maximum=59)
    hours = _cron_values(fields[1], minimum=0, maximum=23)
    if minutes is None or hours is None:
        return 30
    occurrences = sorted(hour * 60 + minute for hour in hours for minute in minutes)
    if len(occurrences) == 1:
        return 24 * 60
    gaps = [
        (occurrences[(index + 1) % len(occurrences)] - occurrence) % (24 * 60)
        for index, occurrence in enumerate(occurrences)
    ]
    return gaps[0] if gaps and gaps[0] > 0 and len(set(gaps)) == 1 else 30


def _legacy_int(value, default=0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _validate_legacy_loop_state(payload: dict, path: Path) -> tuple[str, str, Decimal]:
    channel = payload.get("channel")
    cursor = payload.get("last_user_ts")
    if not isinstance(channel, str) or not channel.strip() or cursor is None:
        raise OngoError(
            "legacy Ongo state is missing valid loop fields",
            code="legacy-state-invalid",
            exit_code=3,
            details={"path": str(path)},
        )
    cursor_text = str(cursor).strip()
    try:
        cursor_value = Decimal(cursor_text)
    except (InvalidOperation, ValueError):
        cursor_value = Decimal("NaN")
    if not cursor_text or not cursor_value.is_finite():
        raise OngoError(
            "legacy Ongo state has an invalid Slack cursor",
            code="legacy-state-invalid",
            exit_code=3,
            details={"path": str(path), "last_user_ts": cursor_text},
        )
    return channel.strip(), cursor_text, cursor_value


def _merge_live_legacy_state(
    target_payload: dict,
    legacy_payload: dict,
    *,
    target: Path,
    legacy: Path,
    include_scheduler: bool,
) -> tuple[dict, bool]:
    """Merge progress committed by a legacy tick before tombstoning it."""
    legacy_channel, legacy_cursor, legacy_cursor_value = _validate_legacy_loop_state(
        legacy_payload, legacy
    )
    target_channel = target_payload.get("channel")
    try:
        target_cursor_value = Decimal(str(target_payload.get("last_user_ts", "")))
    except (InvalidOperation, ValueError):
        target_cursor_value = Decimal("NaN")
    if (
        not isinstance(target_channel, str)
        or not target_channel.strip()
        or not target_cursor_value.is_finite()
    ):
        raise OngoError(
            "migrated Ongo state has invalid loop fields",
            code="legacy-state-migration-failed",
            exit_code=3,
            details={"from": str(legacy), "to": str(target)},
        )
    if target_channel.strip() != legacy_channel:
        raise OngoError(
            "legacy Ongo state changed channels during migration",
            code="legacy-state-migration-conflict",
            exit_code=3,
            details={
                "from": str(legacy),
                "to": str(target),
                "target_channel": target_channel,
                "legacy_channel": legacy_channel,
            },
        )
    if legacy_cursor_value < target_cursor_value:
        return target_payload, False

    merged = dict(target_payload)
    merged["channel"] = legacy_channel
    merged["last_user_ts"] = legacy_cursor
    for field in ("last_self_improve", "last_arxiv_daily"):
        merged[field] = max(
            _legacy_int(merged.get(field)), _legacy_int(legacy_payload.get(field))
        )
    for field in ("rotation", "ken"):
        if legacy_payload.get(field) is not None:
            merged[field] = legacy_payload[field]
    if "idle" in legacy_payload:
        merged["idle"] = bool(legacy_payload["idle"])

    if include_scheduler:
        scheduler = dict(merged.get("scheduler") or {})
        scheduler_id = legacy_payload.get("cron_id") or scheduler.get("id")
        normal_cron = (
            str(legacy_payload.get("normal_cron") or "").strip()
            or scheduler.get("normal_cron")
            or "7,37 * * * *"
        )
        scheduler.update(
            {
                "host": "claude",
                "id": scheduler_id,
                "previous_id": legacy_payload.get("prev_cron_id")
                or scheduler.get("previous_id"),
                "created": _legacy_int(
                    legacy_payload.get("cron_created"),
                    _legacy_int(scheduler.get("created")),
                ),
                "normal_interval_minutes": _normal_interval_minutes(normal_cron),
                "normal_cron": normal_cron,
                "mode": legacy_payload.get("mode", scheduler.get("mode", "normal")),
                "fast_idle_polls": _legacy_int(
                    legacy_payload.get("fast_idle_polls"),
                    _legacy_int(scheduler.get("fast_idle_polls")),
                ),
                "needs_prompt_upgrade": bool(scheduler_id),
            }
        )
        merged["scheduler"] = scheduler
    return merged, merged != target_payload


def _tombstone_payload(target: Path, scheduler_id, migrated_at: int) -> dict:
    return {
        "schema": LEGACY_TOMBSTONE_SCHEMA,
        "migrated_at": migrated_at,
        "migrated_to": str(target),
        "scheduler_id": scheduler_id,
    }


def _recover_interrupted_migration(target: Path, legacy: Path, now=None):
    """Finish a migration that crashed after the durable target was written."""
    try:
        target_payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(target_payload, dict):
        return None
    record = target_payload.get("migration")
    if not isinstance(record, dict) or record.get("schema") != MIGRATION_RECORD_SCHEMA:
        return None
    if record.get("from") != str(legacy):
        return None

    old = _read_json(legacy)
    if old.get("schema") == LEGACY_TOMBSTONE_SCHEMA:
        return None
    scheduler = target_payload.get("scheduler")
    if isinstance(scheduler, dict) and scheduler.get("needs_prompt_upgrade") is False:
        return None
    scheduler_id = scheduler.get("id") if isinstance(scheduler, dict) else None
    migrated_at = _legacy_int(
        record.get("migrated_at"), int(time.time() if now is None else now)
    )
    try:
        target_payload, changed = _merge_live_legacy_state(
            target_payload,
            old,
            target=target,
            legacy=legacy,
            include_scheduler=True,
        )
        if changed:
            _atomic_json(target, target_payload)
        scheduler = target_payload.get("scheduler")
        scheduler_id = scheduler.get("id") if isinstance(scheduler, dict) else None
        _atomic_json(legacy, _tombstone_payload(target, scheduler_id, migrated_at))
    except OSError as error:
        raise OngoError(
            "failed to finish legacy Ongo state migration",
            code="legacy-state-migration-failed",
            exit_code=3,
            details={"from": str(legacy), "to": str(target), "error": str(error)},
        ) from error
    return {
        "status": "migrated",
        "from": str(legacy),
        "to": str(target),
        "scheduler_id": scheduler_id,
        "recovered": True,
    }


def migrate_legacy_agent_state(target: Path, *, legacy: Path | None = None, now=None):
    """Migrate the 0.5.x flat state once and tombstone its old path."""
    target = Path(target)
    legacy = Path(legacy) if legacy is not None else legacy_state_path()
    if target.exists():
        if legacy.exists():
            recovered = _recover_interrupted_migration(target, legacy, now=now)
            if recovered is not None:
                return recovered
        return {"status": "existing", "from": str(legacy), "to": str(target)}
    if not legacy.exists():
        return {"status": "none", "from": str(legacy), "to": str(target)}

    old = _read_json(legacy)
    if old.get("schema") == LEGACY_TOMBSTONE_SCHEMA:
        return {"status": "tombstone", "from": str(legacy), "to": str(target)}
    channel, last_user_ts, _cursor_value = _validate_legacy_loop_state(old, legacy)

    scheduler_id = old.get("cron_id") or None
    previous_id = old.get("prev_cron_id") or None
    normal_cron = str(old.get("normal_cron") or "").strip() or "7,37 * * * *"
    migrated_at = int(time.time() if now is None else now)
    migrated = {
        "channel": channel,
        "last_user_ts": last_user_ts,
        "last_self_improve": _legacy_int(old.get("last_self_improve")),
        "last_arxiv_daily": _legacy_int(old.get("last_arxiv_daily")),
        "rotation": old.get("rotation", "reference"),
        "idle": bool(old.get("idle", False)),
        "ken": old.get("ken"),
        "speaker_prefix": "",
        "speaker_user_id": "",
        "migration": {
            "schema": MIGRATION_RECORD_SCHEMA,
            "from": str(legacy),
            "migrated_at": migrated_at,
        },
        "scheduler": {
            "host": "claude",
            "id": scheduler_id,
            "previous_id": previous_id,
            "created": _legacy_int(old.get("cron_created")),
            "normal_interval_minutes": _normal_interval_minutes(normal_cron),
            "normal_cron": normal_cron,
            "mode": old.get("mode", "normal"),
            "fast_idle_polls": _legacy_int(old.get("fast_idle_polls")),
            "needs_prompt_upgrade": bool(scheduler_id),
        },
    }
    target_written = False
    try:
        _atomic_json(target, migrated)
        target_written = True
        latest = _read_json(legacy)
        if latest.get("schema") != LEGACY_TOMBSTONE_SCHEMA:
            migrated, changed = _merge_live_legacy_state(
                migrated,
                latest,
                target=target,
                legacy=legacy,
                include_scheduler=True,
            )
            if changed:
                _atomic_json(target, migrated)
            scheduler_id = migrated["scheduler"]["id"]
        _atomic_json(legacy, _tombstone_payload(target, scheduler_id, migrated_at))
    except OngoError:
        if target_written:
            try:
                target.unlink()
            except OSError:
                pass
        raise
    except OSError as error:
        rollback_error = None
        if target_written:
            try:
                target.unlink()
            except OSError as rollback:
                rollback_error = str(rollback)
        raise OngoError(
            "failed to migrate legacy Ongo state atomically",
            code="legacy-state-migration-failed",
            exit_code=3,
            details={
                "from": str(legacy),
                "to": str(target),
                "error": str(error),
                "rollback_error": rollback_error,
            },
        ) from error
    return {
        "status": "migrated",
        "from": str(legacy),
        "to": str(target),
        "scheduler_id": scheduler_id,
    }
