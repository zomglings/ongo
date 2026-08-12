# Contributing to ongo

## Setup

Ongo is a Claude Code and Codex plugin. Its shared user-facing skill lives in
`plugins/ongo/skills/ongo/`; the plugin-root `bin/ongo` entry point imports the
stdlib-only runtime from `plugins/ongo/lib/ongo/`.

```bash
git clone https://github.com/zomglings/ongo.git
cd ongo
```

## Checks

Before opening a PR, run all checks:

```bash
python3 -m py_compile plugins/ongo/bin/ongo plugins/ongo/lib/ongo/*.py
python3 -m unittest discover -s plugins/ongo/tests -p 'test_*.py' -v
claude plugin validate --strict plugins/ongo
```

Also validate `plugins/ongo` with Codex's built-in `plugin-creator` validator
and validate `plugins/ongo/skills/ongo` with the built-in `skill-creator`
validator. The repository tests enforce cross-host version, marketplace,
wrapper, frontmatter, and scheduler-adapter invariants even when those built-in
validators are unavailable.

If you change the skill, re-read `SKILL.md` and every affected reference start
to finish for internal consistency. The host adapters must not contradict the
shared loop invariants, experiment guard, or publishing boundaries.

## Version bumps

Every non-documentation PR must synchronize the version in
`plugins/ongo/lib/ongo/__init__.py`, both host manifests, and the Claude
marketplace entry. `SKILL.md` intentionally has no private version field.

## Pull requests

- Branch from `main`.
- Keep PRs focused — one logical change per PR.
- Make sure all checks pass before requesting review.
