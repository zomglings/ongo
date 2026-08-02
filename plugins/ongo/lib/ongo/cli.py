"""Top-level command dispatcher for the Ongo Claude Code plugin."""

from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import subprocess
import sys

from . import __version__
from . import arxiv, delete, serve, site, slack
from .errors import OngoArgumentParser, OngoError, emit_error, emit_json
from .ken import (
    KEN_DB_VERSION,
    PUBLICATION_KINDS,
    RELATIONSHIP_KINDS,
    KenClient,
    default_data_dir,
    install_ken,
)


HELP = """usage: ongo <command> [options]

Commands:
  setup                 Install and initialize pinned dependencies
  doctor [--json]       Check the Claude Code plugin runtime
  slack poll            Poll Slack using Ongo's gap-free cursor contract
  arxiv sweep           Import the daily arXiv topic sweep
  site build            Build the static Ongo research site
  site serve            Serve a generated site
  ken delete            Delete non-experiment Ken records
  experiment ...        Manage deterministic experiments
  version               Print the Ongo plugin version
"""


def invoke(name, function, argv, *, return_code_map=None, passthrough=()):
    """Adapt finite legacy helpers to the public JSON failure contract."""
    captured = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured):
            result = function(argv)
    except OngoError:
        raise
    except SystemExit as error:
        if error.code in {None, 0}:
            return 0
        raw_code = error.code if isinstance(error.code, int) else 3
        message = captured.getvalue().strip() or str(error) or f"{name} failed"
        mapped = (return_code_map or {}).get(raw_code, raw_code)
        exit_code = mapped if mapped in {2, 3, 4, 5, 6} else 3
        raise OngoError(
            message,
            code="invalid-input" if exit_code == 2 else f"{name}-failed",
            exit_code=exit_code,
        ) from error
    result = 0 if result is None else result
    if result in passthrough:
        sys.stderr.write(captured.getvalue())
        return result
    if result != 0:
        mapped = (return_code_map or {}).get(result, result)
        exit_code = mapped if mapped in {2, 3, 4, 5, 6} else 3
        raise OngoError(
            captured.getvalue().strip() or f"{name} failed",
            code="invalid-input" if exit_code == 2 else f"{name}-failed",
            exit_code=exit_code,
            details={"helper_exit_code": result},
        )
    sys.stderr.write(captured.getvalue())
    return 0


def setup_main(argv):
    import argparse

    parser = OngoArgumentParser(prog="ongo setup")
    parser.add_argument("--db")
    args = parser.parse_args(argv)
    binary = install_ken()
    client = KenClient(binary=binary, db=args.db)
    client.initialize()
    client.ensure_kinds()
    emit_json({"ok": True, "ken": binary, "db": client.db, "version": __version__})
    return 0


def clacks_version():
    binary = shutil.which("clacks")
    if not binary:
        return {"ok": False, "path": None, "version": None}
    result = subprocess.run([binary, "--version"], capture_output=True, text=True)
    text = (result.stdout or result.stderr).strip()
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    version = tuple(int(part) for part in match.groups()) if match else None
    return {
        "ok": result.returncode == 0 and version is not None and version >= (0, 10, 3),
        "path": binary,
        "version": text,
        "minimum": "0.10.3",
    }


def doctor_main(argv):
    import argparse

    parser = OngoArgumentParser(prog="ongo doctor")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--ken")
    parser.add_argument("--db")
    args = parser.parse_args(argv)
    checks = {
        "python": {"ok": sys.version_info >= (3, 10), "version": sys.version.split()[0]},
        "plugin_data": {"ok": False, "path": str(default_data_dir())},
        "ken": {"ok": False},
        "database": {"ok": False},
        "clacks": clacks_version(),
    }
    data_dir = default_data_dir()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        checks["plugin_data"]["ok"] = os.access(data_dir, os.W_OK)
    except OSError as error:
        checks["plugin_data"]["error"] = str(error)
    try:
        client = KenClient(binary=args.ken, db=args.db)
        checks["ken"] = {"ok": True, "path": client.binary, "version": "3"}
        db_version = client.command("dbversion", check=False)
        checks["database"] = {
            "ok": (
                db_version.returncode == 0
                and db_version.stdout.strip() == KEN_DB_VERSION
            ),
            "path": client.db,
            "schema_version": db_version.stdout.strip() or None,
        }
        if checks["database"]["ok"]:
            missing_publications = [
                name
                for name in PUBLICATION_KINDS
                if client.command("pubkind", "show", name, check=False).returncode != 0
            ]
            missing_relationships = [
                name
                for name in RELATIONSHIP_KINDS
                if client.command("relkind", "show", name, check=False).returncode != 0
            ]
            checks["database"]["missing_publication_kinds"] = missing_publications
            checks["database"]["missing_relationship_kinds"] = missing_relationships
            checks["database"]["ok"] = not missing_publications and not missing_relationships
    except OngoError as error:
        checks["ken"]["error"] = str(error)
    ok = all(item.get("ok", False) for item in checks.values())
    payload = {"ok": ok, "checks": checks, "version": __version__}
    if args.json:
        emit_json(payload)
    else:
        for name, check in checks.items():
            print(f"{'ok' if check.get('ok') else 'FAIL':4} {name}: {check}")
    return 0 if ok else 3


def dispatch(argv):
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(HELP, end="")
        return 0
    command = argv[0]
    rest = argv[1:]
    if command == "version":
        print(__version__)
        return 0
    if command == "setup":
        return setup_main(rest)
    if command == "doctor":
        return doctor_main(rest)
    if command == "slack" and rest[:1] == ["poll"]:
        return invoke("slack-poll", slack.main, rest[1:])
    if command == "arxiv" and rest[:1] == ["sweep"]:
        return invoke("arxiv-sweep", arxiv.main, rest[1:], return_code_map={2: 3})
    if command == "site" and rest[:1] == ["build"]:
        return invoke("site-build", site.main, rest[1:])
    if command == "site" and rest[:1] == ["serve"]:
        return serve.main(rest[1:])
    if command == "ken" and rest[:1] == ["delete"]:
        return invoke("ken-delete", delete.main, rest[1:])
    if command == "experiment":
        from .experiments import main as experiments_main

        return invoke(
            "experiment", experiments_main, rest, passthrough={6}
        )
    raise OngoError(
        f"unknown command: {' '.join(argv[:2])}",
        code="invalid-command",
        exit_code=2,
    )


def main(argv=None):
    try:
        return dispatch(list(sys.argv[1:] if argv is None else argv))
    except OngoError as error:
        return emit_error(error)
    except KeyboardInterrupt:
        return emit_error(
            OngoError("interrupted", code="interrupted", exit_code=4)
        )
    except Exception as error:  # defensive public-CLI boundary
        return emit_error(
            OngoError(
                "unexpected command failure",
                code="internal-failure",
                exit_code=3,
                details={"type": type(error).__name__, "message": str(error)},
            )
        )
