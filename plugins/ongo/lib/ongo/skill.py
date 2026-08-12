"""Render the Ongo skill for one supported agent harness."""

from __future__ import annotations

import sys
from pathlib import Path

from .errors import OngoArgumentParser, OngoError


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = PLUGIN_ROOT / "skills" / "ongo"
HARNESS_ADAPTERS = {
    "claude": SKILL_ROOT / "references" / "claude-code.md",
    "codex": SKILL_ROOT / "references" / "codex.md",
}
SELECTION_START = "## Choose the host adapter\n"
SELECTION_END = "\n## Resolve the runtime"
CONTINUITY_RULE = (
    "- Preserve scheduler continuity: create before delete on Claude Code; update the\n"
    "  existing heartbeat on Codex."
)
HARNESS_CONTINUITY_RULES = {
    "claude": "- Preserve scheduler continuity: create the replacement cron job before deleting the old one.",
    "codex": "- Preserve scheduler continuity: update the existing heartbeat in place.",
}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise OngoError(
            "failed to read bundled skill content",
            code="skill-content-unavailable",
            exit_code=3,
            details={"path": str(path), "error": str(error)},
        ) from error


def render(harness: str) -> str:
    """Return the shared skill with exactly one harness adapter inlined."""
    core_path = SKILL_ROOT / "SKILL.md"
    core = _read(core_path)
    adapter = _read(HARNESS_ADAPTERS[harness])

    if SELECTION_START not in core or SELECTION_END not in core:
        raise OngoError(
            "the bundled skill is missing its harness selection markers",
            code="skill-render-invalid",
            exit_code=3,
            details={"path": str(core_path)},
        )

    before, remainder = core.split(SELECTION_START, 1)
    _selection, after = remainder.split(SELECTION_END, 1)
    label = "Claude Code" if harness == "claude" else "Codex"
    selected = (
        "## Harness adapter\n\n"
        f"This rendering targets the **{label}** harness. Follow only the inlined\n"
        "harness adapter at the end of this document.\n"
    )
    rendered_core = before + selected + SELECTION_END + after
    rendered_core = rendered_core.replace('"host": "<claude|codex>"', f'"host": "{harness}"')
    rendered_core = rendered_core.replace(
        CONTINUITY_RULE, HARNESS_CONTINUITY_RULES[harness]
    )
    if CONTINUITY_RULE in rendered_core or '"host": "<claude|codex>"' in rendered_core:
        raise OngoError(
            "the bundled skill could not be specialized for the selected harness",
            code="skill-render-invalid",
            exit_code=3,
            details={"path": str(core_path), "harness": harness},
        )

    return (
        rendered_core.rstrip()
        + "\n\n---\n\n"
        + f"## {label} harness adapter\n\n"
        + adapter.removeprefix(f"# {label} adapter\n").lstrip()
    )


def main(argv=None):
    parser = OngoArgumentParser(
        prog="ongo skill",
        description="Render the complete Ongo skill for an agent harness.",
    )
    parser.add_argument("--harness", required=True, choices=["claude", "codex"])
    args = parser.parse_args(argv)
    sys.stdout.write(render(args.harness))
    return 0
