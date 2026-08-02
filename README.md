# ongo

Claude Code research plugin with a natural-language `/ongo:ongo` skill and a
deterministic `ongo` CLI. It reads research requests from Slack, tracks findings
in [kendb](https://github.com/zomglings/ken), and manages auditable experiments.

## Prerequisites

- Slack authentication via clacks: run `clacks auth login` before first use
- [clacks](https://github.com/downstairs-dawgs/clacks) and [ken](https://github.com/zomglings/ken) are installed automatically on first run

## Install

```
/plugin marketplace add zomglings/ongo
/plugin install ongo@ongo
```

## Usage

```
/ongo:ongo
```

Claude Code exposes the installed skill as `/ongo:ongo`. The plugin also adds
its implementation CLI to Bash's `PATH`:

```bash
ongo setup
ongo doctor --json
ongo experiment --help
```

Options:
- `--channel <id>` — Slack channel to listen on (default: self-DM)
- `--interval <minutes>` — tick interval in minutes (default: 30)
- `--idle` — only respond to messages; do not expand research autonomously

## How it works

Ongo runs as a durable scheduled loop managed by Claude Code:

1. **Poll** — checks Slack for new messages on the configured cadence
2. **Process** — interprets messages as natural language (research requests, strategy updates, maintenance triggers)
3. **Expand** — when idle, picks a random topic from kendb and researches it further
4. **Self-improve** — every 24 hours, runs kendb maintenance, checks for dependency updates, and reflects on its own behavior

All research is tracked in kendb with publications, relationships, and notes.
Experiment plans, approvals, attempts, results, and artifacts are append-only
Ken publications managed exclusively through `ongo experiment`.

### Exploration strategy

Tell ongo how to focus its research via Slack:

- "Focus more on cryptography"
- "Prefer papers published after 2020"
- "Ignore machine learning"

These are stored as `ongo-exploration` entries in kendb and consulted during idle expansion.

### Self-improvement

Every 24 hours (or on request), ongo:

- **Maintains kendb** — deduplicates, fills relationship gaps, produces surveys, evolves kinds
- **Checks dependencies** — verifies the plugin-pinned Ken v3 and the clacks version floor
- **Modifies itself** — edits its own skill instructions based on what's working

All self-improvement attempts are tracked in kendb via the `ongo-self-improvement` kind.

## Development

Test locally:
```
claude --plugin-dir /path/to/ongo
```

Design documents:

- [Deterministic experiment management pilot contract](docs/experiment-management-requirements.md)

## License

MIT
