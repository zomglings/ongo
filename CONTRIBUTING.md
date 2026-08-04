# Contributing to ongo

## Setup

Ongo is a Claude Code plugin. Its user-facing skill lives in
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

If you change `SKILL.md`, re-read it start to finish for internal
consistency (the loop invariants, the pubkind guard pattern, and the
static-site sections must not contradict each other).

## Version bumps

Every non-documentation PR must bump `plugins/ongo/.claude-plugin/plugin.json`
and keep `.claude-plugin/marketplace.json` synchronized. The plugin manifest is
the release-version source of truth; `SKILL.md` intentionally has no private
version field.

## Pull requests

- Branch from `main`.
- Keep PRs focused — one logical change per PR.
- Make sure all checks pass before requesting review.
