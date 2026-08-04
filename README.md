# ongo

Claude Code research plugin with a natural-language `/ongo:ongo` skill and a
deterministic `ongo` CLI. It reads research requests from Slack, tracks findings
in [kendb](https://github.com/zomglings/ken), manages auditable experiments,
and publishes mixed public and symmetric-key-protected static research sites.

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
