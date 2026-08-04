"""Shared CLI errors and machine-readable reporting."""

from __future__ import annotations

import argparse
import json
import sys


class OngoError(Exception):
    def __init__(self, message, *, code="ongo-error", exit_code=3, details=None):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.details = details or {}


class OngoArgumentParser(argparse.ArgumentParser):
    """Argument parser that participates in the CLI's JSON error contract."""

    def error(self, message):
        raise OngoError(
            message,
            code="invalid-input",
            exit_code=2,
            details={"usage": self.format_usage().strip()},
        )


def emit_error(error):
    payload = {
        "ok": False,
        "error": {
            "code": error.code,
            "message": str(error),
            "details": error.details,
        },
    }
    sys.stderr.write(json.dumps(payload, sort_keys=True) + "\n")
    return error.exit_code


def emit_json(payload):
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
