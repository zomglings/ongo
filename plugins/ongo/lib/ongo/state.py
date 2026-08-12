"""Durable agent-state migration helpers."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from .errors import OngoError


LEGACY_STATE_PATH = Path("/tmp/ongo_state.json")
LEGACY_TOMBSTONE_SCHEMA = "ongo-state-migrated-v1"


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


def _normal_interval_minutes(expression) -> int:
    field = str(expression or "").split(maxsplit=1)[0]
    if field.startswith("*/"):
        try:
            value = int(field[2:])
            return value if value > 0 else 30
        except ValueError:
            return 30
    try:
        minutes = sorted({int(value) for value in field.split(",")})
    except ValueError:
        return 30
    if len(minutes) == 1:
        return 60
    if len(minutes) > 1 and all(0 <= minute < 60 for minute in minutes):
        gaps = [
            (minutes[(index + 1) % len(minutes)] - minute) % 60
            for index, minute in enumerate(minutes)
        ]
        if gaps and len(set(gaps)) == 1 and gaps[0] > 0:
            return gaps[0]
    return 30


def migrate_legacy_agent_state(target: Path, *, legacy: Path | None = None, now=None):
    """Migrate the 0.5.x flat state once and tombstone its old path."""
    target = Path(target)
    legacy = Path(legacy) if legacy is not None else legacy_state_path()
    if target.exists():
        return {"status": "existing", "from": str(legacy), "to": str(target)}
    if not legacy.exists():
        return {"status": "none", "from": str(legacy), "to": str(target)}

    old = _read_json(legacy)
    if old.get("schema") == LEGACY_TOMBSTONE_SCHEMA:
        return {"status": "tombstone", "from": str(legacy), "to": str(target)}
    if "channel" not in old or "last_user_ts" not in old:
        raise OngoError(
            "legacy Ongo state is missing required loop fields",
            code="legacy-state-invalid",
            exit_code=3,
            details={"path": str(legacy)},
        )

    scheduler_id = old.get("cron_id") or None
    previous_id = old.get("prev_cron_id") or None
    normal_cron = old.get("normal_cron") or "7,37 * * * *"
    migrated = {
        "channel": old["channel"],
        "last_user_ts": old["last_user_ts"],
        "last_self_improve": old.get("last_self_improve", 0),
        "last_arxiv_daily": old.get("last_arxiv_daily", 0),
        "rotation": old.get("rotation", "reference"),
        "idle": bool(old.get("idle", False)),
        "ken": old.get("ken"),
        "speaker_prefix": "",
        "speaker_user_id": "",
        "scheduler": {
            "host": "claude",
            "id": scheduler_id,
            "previous_id": previous_id,
            "created": old.get("cron_created", 0),
            "normal_interval_minutes": _normal_interval_minutes(normal_cron),
            "normal_cron": normal_cron,
            "mode": old.get("mode", "normal"),
            "fast_idle_polls": old.get("fast_idle_polls", 0),
            "needs_prompt_upgrade": bool(scheduler_id),
        },
    }
    migrated_at = int(time.time() if now is None else now)
    try:
        _atomic_json(target, migrated)
        _atomic_json(
            legacy,
            {
                "schema": LEGACY_TOMBSTONE_SCHEMA,
                "migrated_at": migrated_at,
                "migrated_to": str(target),
                "scheduler_id": scheduler_id,
            },
        )
    except OngoError:
        raise
    except OSError as error:
        raise OngoError(
            "failed to migrate legacy Ongo state atomically",
            code="legacy-state-migration-failed",
            exit_code=3,
            details={
                "from": str(legacy),
                "to": str(target),
                "error": str(error),
            },
        ) from error
    return {
        "status": "migrated",
        "from": str(legacy),
        "to": str(target),
        "scheduler_id": scheduler_id,
    }
