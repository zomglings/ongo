"""Ken v3 discovery and graph operations."""

from __future__ import annotations

import json
import hashlib
import os
import platform
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from .errors import OngoError


KEN_TAG = "v3"
KEN_VERSION = "3"
KEN_DB_VERSION = "1"
KEN_SHA256 = {
    "ken-aarch64-linux": "c47d2f8b633ec107f6d50942dd5fb01f9f3a8f583e4b7ae564bce83ac5a61f8f",
    "ken-aarch64-macos": "5b34212a416725d9b1ee81fe7fb52f46f40012b10e9fbe3437290dc9e334c5e6",
    "ken-x86_64-linux": "895eeb32982704b007a419015da7cb920e267f28b9a8e3fce601e3f1acaf0f9c",
    "ken-x86_64-macos": "75daa41bdac6e16ee7c32af3d70884d3d03d16e149acb997942a64bac17b068d",
}

PUBLICATION_KINDS = {
    "ongo-exploration": "A user preference that shapes Ongo's research expansion strategy.",
    "ongo-self-improvement": "A record of an Ongo self-improvement attempt and its outcome.",
    "ongo-cron-reset": "A record of an Ongo cron renewal or cadence swap.",
    "ongo-web": "An explicit marker publishing another Ken publication on the Ongo site.",
    "ongo-arxiv-topic": "A topic and arXiv API query watched by Ongo.",
    "ongo-digest": "A summary of one Ongo daily arXiv sweep.",
    "ongo-experiment": "The durable root of one Ongo experiment.",
    "ongo-experiment-plan": "A human-readable immutable experiment protocol.",
    "ongo-experiment-manifest": "The canonical machine-readable form of an experiment plan.",
    "ongo-experiment-condition": "One explicitly enumerated condition in an experiment manifest.",
    "ongo-experiment-delegation": "A human grant allowing an Ongo driving agent to approve experiments within a budget.",
    "ongo-experiment-approval": "Approval of one exact experiment plan and manifest hash.",
    "ongo-experiment-attempt": "One append-only execution attempt for an experiment condition.",
    "ongo-experiment-result": "The terminal result reported for one experiment attempt.",
    "ongo-experiment-artifact": "An immutable text or base64-encoded binary artifact produced by an experiment.",
    "ongo-experiment-note": "An append-only free-form Markdown note documenting an experiment, condition, or attempt.",
    "ongo-access-key": "Public metadata for a symmetric key authorized to decrypt selected Ongo site resources; secret material is never stored in Ken.",
}

RELATIONSHIP_KINDS = {
    "related-to": "Connects generally related Ken publications used by Ongo research and digest views.",
    "ongo-has-plan": "Connects an experiment to its human-readable plan.",
    "ongo-compiled-as": "Connects a human plan to its canonical machine manifest.",
    "ongo-has-condition": "Connects a manifest to one enumerated condition.",
    "ongo-delegates-for": "Restricts a delegation to a particular experiment.",
    "ongo-approves": "Connects an approval to the exact manifest it authorizes.",
    "ongo-under-delegation": "Connects an approval to the delegation used by the driving agent.",
    "ongo-attempt-of": "Connects an execution attempt to its condition.",
    "ongo-result-of": "Connects a terminal result to its attempt.",
    "ongo-produced": "Connects an experiment result to an immutable artifact.",
    "ongo-note-for": "Connects an append-only experiment note to its experiment, condition, or attempt.",
    "ongo-successor-of": "Connects a changed protocol to the experiment it supersedes.",
    "ongo-readable-by": "Authorizes an Ongo site resource to be encrypted for an access-key descriptor.",
}


def default_data_dir():
    for variable in ("ONGO_DATA_DIR", "CLAUDE_PLUGIN_DATA", "PLUGIN_DATA"):
        configured = (os.environ.get(variable) or "").strip()
        if configured:
            return Path(configured).expanduser()
    xdg = (os.environ.get("XDG_DATA_HOME") or "").strip()
    if xdg:
        return Path(xdg).expanduser() / "ongo"
    return Path.home() / ".local" / "share" / "ongo"


def agent_state_path():
    return default_data_dir() / "agent-state.json"


def resolve_ken(explicit=None):
    candidates = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("ONGO_KEN"):
        candidates.append(os.environ["ONGO_KEN"])
    candidates.append(str(default_data_dir() / "bin" / "ken"))
    found = shutil.which("ken")
    if found:
        candidates.append(found)
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise OngoError(
        "ken v3 was not found; run `ongo setup`",
        code="ken-not-found",
        exit_code=3,
        details={"searched": candidates},
    )


def resolve_db(ken_binary, explicit=None):
    configured = explicit or os.environ.get("ONGO_KEN_DB")
    if configured:
        return str(Path(configured).expanduser())
    result = subprocess.run(
        [ken_binary, "initpath"], capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise OngoError(
            "ken could not determine its default database path",
            code="ken-initpath-failed",
            exit_code=3,
            details={"stderr": result.stderr.strip()},
        )
    return result.stdout.strip()


def verify_ken(binary):
    result = subprocess.run([binary, "version"], capture_output=True, text=True)
    if result.returncode != 0 or result.stdout.strip() != KEN_VERSION:
        raise OngoError(
            "Ongo requires Ken v3",
            code="ken-version-mismatch",
            exit_code=3,
            details={"path": binary, "version": result.stdout.strip()},
        )


def verify_checksum(path, expected):
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if digest != expected:
        raise OngoError(
            "the downloaded Ken v3 binary failed checksum verification",
            code="ken-checksum-mismatch",
            exit_code=3,
            details={"expected": expected, "actual": digest},
        )


def install_ken():
    system = platform.system().lower()
    machine = platform.machine().lower()
    system_name = {"darwin": "macos", "linux": "linux"}.get(system)
    machine_name = {
        "arm64": "aarch64",
        "aarch64": "aarch64",
        "x86_64": "x86_64",
        "amd64": "x86_64",
    }.get(machine)
    if not system_name or not machine_name:
        raise OngoError(
            "no pinned Ken v3 binary is available for this platform",
            code="unsupported-platform",
            exit_code=3,
            details={"system": system, "machine": machine},
        )
    asset = f"ken-{machine_name}-{system_name}"
    expected_checksum = KEN_SHA256[asset]
    target_dir = default_data_dir() / "bin"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "ken"
    if target.is_file() and os.access(target, os.X_OK):
        try:
            verify_checksum(target, expected_checksum)
            verify_ken(str(target))
            return str(target)
        except OngoError:
            pass
    url = (
        "https://github.com/zomglings/ken/releases/download/"
        f"{KEN_TAG}/{asset}"
    )
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            with tempfile.NamedTemporaryFile(dir=target_dir, delete=False) as handle:
                shutil.copyfileobj(response, handle)
                temporary = Path(handle.name)
    except (OSError, urllib.error.URLError) as error:
        raise OngoError(
            "failed to download the pinned Ken v3 binary",
            code="ken-download-failed",
            exit_code=3,
            details={"url": url, "error": str(error)},
        ) from error
    try:
        verify_checksum(temporary, expected_checksum)
        temporary.chmod(0o755)
        verify_ken(str(temporary))
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return str(target)


class KenClient:
    def __init__(self, binary=None, db=None):
        self.binary = resolve_ken(binary)
        verify_ken(self.binary)
        self.db = resolve_db(self.binary, db)

    def command(self, *args, check=True):
        result = subprocess.run(
            [self.binary, "-D", self.db, *args],
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            raise OngoError(
                f"ken {' '.join(args)} failed",
                code="ken-command-failed",
                exit_code=3,
                details={
                    "command": list(args),
                    "returncode": result.returncode,
                    "stderr": result.stderr.strip(),
                },
            )
        return result

    def initialize(self):
        Path(self.db).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.command("init")

    def ensure_kinds(self):
        for name, description in PUBLICATION_KINDS.items():
            shown = self.command("pubkind", "show", name, check=False)
            if shown.returncode != 0:
                self.command("pubkind", "add", name, description)
        for name, description in RELATIONSHIP_KINDS.items():
            shown = self.command("relkind", "show", name, check=False)
            if shown.returncode != 0:
                self.command("relkind", "add", name, description)

    def list_kind(self, kind):
        rows = []
        offset = 0
        while True:
            result = self.command(
                "list", "--kind", kind, "--limit", "500", "--offset", str(offset)
            )
            try:
                page = json.loads(result.stdout or "[]")
            except json.JSONDecodeError as error:
                raise OngoError(
                    "ken returned invalid JSON",
                    code="ken-json-invalid",
                    exit_code=3,
                    details={"kind": kind, "error": str(error)},
                ) from error
            if not isinstance(page, list):
                raise OngoError(
                    "ken list returned an unexpected JSON shape",
                    code="ken-json-invalid",
                    exit_code=3,
                    details={"kind": kind},
                )
            rows.extend(page)
            if len(page) < 500:
                return rows
            offset += len(page)

    def show(self, identifier=None, *, key=None, check=True):
        if key is not None:
            result = self.command("show", "--key", key, "--json", check=check)
        else:
            result = self.command("show", identifier, "--json", check=check)
        if result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise OngoError(
                "ken show returned invalid JSON",
                code="ken-json-invalid",
                exit_code=3,
                details={"error": str(error)},
            ) from error

    def records(self, kind):
        records = []
        for row in self.list_kind(kind):
            record = self.show(row["id"])
            if record is not None:
                records.append(record)
        return records

    def unique_by_key(self, kind, key):
        matches = [row for row in self.list_kind(kind) if row.get("key") == key]
        if len(matches) > 1:
            raise OngoError(
                "duplicate Ken keys violate the Ongo protocol",
                code="duplicate-ken-key",
                exit_code=4,
                details={"kind": kind, "key": key, "count": len(matches)},
            )
        return self.show(matches[0]["id"]) if matches else None

    def load(self, payload):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False
        ) as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            path = handle.name
        try:
            result = self.command("load", path)
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError as error:
                raise OngoError(
                    "ken load returned invalid JSON",
                    code="ken-json-invalid",
                    exit_code=3,
                    details={"error": str(error)},
                ) from error
        finally:
            Path(path).unlink(missing_ok=True)
