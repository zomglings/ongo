# ongo

Research plugin for Claude Code and Codex with a natural-language Ongo skill
and a deterministic CLI. It reads research requests from Slack, tracks findings
in [kendb](https://github.com/zomglings/ken), manages auditable experiments,
and publishes mixed public and symmetric-key-protected static research sites.

## Prerequisites

- Slack authentication via clacks: run `clacks auth login` before first use
- [clacks](https://github.com/downstairs-dawgs/clacks) 0.14.1 or newer for Slack workflows; the skill can install it after authorization
- [ken](https://github.com/zomglings/ken) v3 and the pinned encryption dependency are installed by `ongo setup`

## Install in Claude Code

```
/plugin marketplace add zomglings/ongo
/plugin install ongo@ongo
```

Invoke `/ongo:ongo` after installation. Claude Code also adds the plugin's
`bin/` directory to Bash PATH.

## Install in Codex

```bash
codex plugin marketplace add zomglings/ongo
codex plugin add ongo@ongo
```

Start a new Codex task after installation, then invoke `$ongo`. Codex uses the
plugin's bundled wrapper and does not depend on plugin PATH injection.

Codex recurring operation uses a heartbeat automation attached to the current
task. The ChatGPT desktop app must remain running for local scheduled work, and
the scheduled environment needs network access, clacks credentials, and write
access to the Ongo data directory.

### Upgrading an active Claude loop

The first `ongo setup` after upgrading from 0.5.x migrates
`/tmp/ongo_state.json` into the plugin data directory without resetting the
Slack cursor. It tombstones the old path, then the Claude adapter replaces the
legacy cron with a new-state prompt using create-before-delete and sweeps any
older Ongo tick jobs. Do not manually start a second loop while that one-time
migration is being reconciled.

If the legacy path contains corrupt or unrelated data, setup reports
`state_migration.status: invalid` without blocking one-shot CLI use. Recurring
startup remains blocked until the user inspects that path, confirms no legacy
loop needs it, and moves or removes it.

## Usage

```
/ongo:ongo   # Claude Code
$ongo        # Codex
```

The skill resolves the bundled CLI portably. From a source checkout, invoke it
directly:

```bash
plugins/ongo/bin/ongo setup
plugins/ongo/bin/ongo doctor --json
plugins/ongo/bin/ongo skill --harness codex
plugins/ongo/bin/ongo skill --harness claude
plugins/ongo/bin/ongo experiment --help
```

`ongo skill --harness {claude,codex}` writes a complete skill document to
standard output. It combines the shared workflow with exactly one inlined
harness adapter, which is useful for inspection, testing, or passing Ongo to an
agent surface that does not load plugin skills directly.

Options:
- `--channel <id>` — Slack channel to listen on (default: self-DM)
- `--interval <minutes>` — tick interval in minutes (default: 30)
- `--idle` — only respond to messages; do not expand research autonomously

## How it works

Ongo runs as a durable scheduled loop managed by the active host:

1. **Poll** — checks Slack for new messages on the configured cadence
2. **Process** — interprets messages as natural language (research requests, strategy updates, maintenance triggers)
3. **Expand** — when idle, picks a random topic from kendb and researches it further
4. **Maintain** — every 24 hours, checks the database, identifies bounded maintenance, and reports dependency updates

Claude Code uses durable cron jobs with create-before-delete renewal. Codex uses
one heartbeat automation that is updated in place for cadence changes. Both
hosts share the same polling, cursor, experiment, and publishing invariants.

All research is tracked in kendb with publications, relationships, and notes.
Experiment plans, approvals, attempts, results, and artifacts are append-only
Ken publications managed exclusively through `ongo experiment`. Free-form
Markdown notes document protocol difficulties and deviations without changing
the protocol or verification state:

```bash
ongo experiment note add <experiment-id> --actor <label> --file deviation.md \
  --attempt <attempt-id> --topic <existing-topic-key> --operation-key <key>
ongo experiment note list <experiment-id> --format markdown
```

### Static publishing and access keys

`ongo site build` resolves access per published resource. Content with no
effective access key is emitted publicly, preserving the existing static site.
Content related to one or more `ongo-access-key` descriptors is emitted as one
AES-256-GCM ciphertext per key. One site can contain both public and protected
articles, experiments, results, and explicitly published artifacts.

```bash
ongo key create --label "Current team" --scope all
ongo key create --label "Published snapshot" --scope published
ongo key create --label "Project reader" --scope empty
printf '%s\n' "$ONGO_SHARED_CAPABILITY" | ongo key import --label "Imported" --scope empty
ongo key grant <key-id> <publication-id>
ongo site build --out ./site
ongo site serve --dir ./site --host 127.0.0.1 --port 8000
```

The key command prints a shareable `ongo-key-v1.…` capability. Readers paste
capabilities into the site's **Keys** panel and name them locally. The browser
stores them in local storage, decrypts matching resources with Web Crypto, and
deduplicates resources available under several registered keys. Protected
titles, dates, tags, bodies, experiment protocols, and artifacts are all
encrypted. If local storage is blocked, entered keys remain available only for
the current page session; public resources still render when storage or Web
Crypto is unavailable. Public output uses the legacy deterministic generator
when no published resource has an effective key.
Presentation-oriented raw HTML such as `<details>`, tables, and images remains
available through the same allowlist in both renderers; scripts, forms, event
handlers, inline styles, and executable URLs are removed.
HTTP(S) images remain available in public resources. They are removed from
protected resources so decrypting a page cannot disclose access to an image
host; use relative or embedded data images for protected content.
Digest bodies are derived from related note publications. If a source note is
protected, the digest must be granted a non-broader subset of those keys; the
builder rejects an incompatible public or more widely shared digest instead of
copying protected source text into it.
Experiment notes are similarly derived into their experiment page. The builder
encrypts note bodies, actors, and topic labels with the experiment and rejects
access rules that would expose a protected note or topic through a broader
experiment resource. An experiment note cannot be published independently.

The administrator keyring defaults to the plugin data directory and is written
with mode `0600`; Ken contains only descriptors and access relationships.
Symlink aliases resolve to the canonical keyring, while hard-linked keyring
files are rejected because atomic replacement cannot update every hard link.
`ongo key import` reads the capability from standard input, or uses a hidden
prompt when run interactively, so key material does not appear in process
arguments or shell history. Avoid literal capabilities in shell command text.
Build on a trusted machine and deploy only the generated site tree. Production
sites containing protected resources require HTTPS. Static capabilities cannot
reliably expire or prevent an authorized reader from retaining plaintext. The
static host can still observe resource counts, article/experiment collection,
ordering, stable opaque resource IDs, ciphertext sizes, and rebuild timing.

### Exploration strategy

Tell ongo how to focus its research via Slack:

- "Focus more on cryptography"
- "Prefer papers published after 2020"
- "Ignore machine learning"

These are stored as `ongo-exploration` entries in kendb and consulted during idle expansion.

### Self-improvement

Every 24 hours (or on request), Ongo:

- **Maintains kendb** — deduplicates, fills relationship gaps, produces surveys, evolves kinds
- **Checks dependencies** — verifies the plugin-pinned Ken v3 and the clacks version floor
- **Proposes improvements** — records findings without modifying an installed plugin cache

Maintenance attempts are tracked in kendb via the `ongo-self-improvement` kind.
Dependency upgrades, research deletion, source changes, and external issues or
pull requests require explicit authorization.

## Development

Test locally:
```
claude --plugin-dir /path/to/ongo
codex plugin marketplace add /path/to/ongo
```

Design documents:

- [Deterministic experiment management pilot contract](docs/experiment-management-requirements.md)

## License

MIT
