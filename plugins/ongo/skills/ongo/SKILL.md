---
name: ongo
description: Research agent, deterministic experiment manager, and encrypted static publisher. Use for starting or operating the Ongo Slack research loop; planning, approving, executing, or verifying experiments; managing Ken research records; running arXiv expansion; or publishing public and protected Ongo content.
---

# Ongo

Use the bundled CLI for deterministic state changes and the active host's agent
tools for reasoning, delegation, and scheduling.

Recognize these invocation parameters:

- `--channel <id>`: use an explicit Slack channel instead of a self-DM.
- `--interval <minutes>`: set the normal cadence; default to 30.
- `--idle`: respond to Slack messages without passive expansion.

## Choose the host adapter

Before creating, updating, or deleting a scheduled loop, read exactly one host
adapter completely:

- Claude Code: [references/claude-code.md](references/claude-code.md)
- Codex: [references/codex.md](references/codex.md)

For one-shot CLI, experiment, or publishing work, no scheduler adapter is
required. If neither host exposes a supported scheduler, complete the one-shot
work and explain that recurring operation is unavailable in that surface.

## Resolve the runtime

Resolve this skill's absolute directory from the skill metadata supplied by the
host. Claude Code may expose it as `CLAUDE_SKILL_DIR`; Codex includes the skill
path in its available-skills metadata. Never reconstruct a cache path.

Use the wrapper beside this file for every Ongo command:

```bash
ONGO="<absolute-skill-directory>/scripts/ongo"
"$ONGO" version
```

The wrapper locates the plugin root without relying on host PATH injection.

Require `jq`. Require `clacks >= 0.14.1` only for Slack-loop or arXiv-digest
work. If clacks is missing, install `slack-clacks>=0.14.1` only with the user's
authorization. If Slack is required and `clacks auth status` has no `user_id`,
ask the user to run `clacks auth login` and stop setup.

```bash
AUTH_INFO=$(clacks auth status)
USER_ID=$(printf '%s' "$AUTH_INFO" | jq -r '.user_id // empty')
```

## Initialize Ongo

Run setup and verify every reported check:

```bash
SETUP=$("$ONGO" setup)
KEN=$(printf '%s' "$SETUP" | jq -r '.ken')
STATE_PATH=$(printf '%s' "$SETUP" | jq -r '.state')
STATE_MIGRATION=$(printf '%s' "$SETUP" | jq -r '.state_migration.status')
```

If `STATE_MIGRATION` is `invalid`, setup of the CLI and research database still
succeeded, but recurring-loop startup must stop. Report the legacy path and
error from `state_migration`; ask the user to inspect and move or remove that
file after confirming no 0.5.x loop still needs it. Never overwrite, delete, or
silently ignore invalid legacy state.

For a Slack loop, run `"$ONGO" doctor --json`. For one-shot experiment,
publishing, or Ken work that does not use Slack, run
`"$ONGO" doctor --json --no-slack`. Verify every reported check.

`ongo setup` installs checksum-pinned Ken v3 and the pinned `cryptography`
dependency in Ongo's writable data directory. Data-directory precedence is
`ONGO_DATA_DIR`, `CLAUDE_PLUGIN_DATA`, `PLUGIN_DATA`, `XDG_DATA_HOME/ongo`, then
`~/.local/share/ongo`.

For a Slack loop, send the startup message to the requested channel or discover
the self-DM with `clacks send -u "$USER_ID"`. Capture both the returned channel
and timestamp. A host or controlling skill may require a leading identity
prefix for every Slack message. Store that exact prefix in `speaker_prefix`;
otherwise store an empty string. The Ongo marker always follows it, for example
`` `[rex]` [ongo] ...`` when `speaker_prefix` is `` `[rex]` `` followed by one
space. Preserve Markdown or other formatting in the prefix exactly as it appears
on Slack; a visually similar plain-text prefix is not equivalent.

If `STATE_MIGRATION` is `migrated`, or the state has
`scheduler.needs_prompt_upgrade: true`, follow the selected harness adapter's
legacy-state migration guidance before creating or changing any scheduler. If
`STATE_PATH` already exists, validate and resume it; never overwrite an existing
cursor or scheduler ID during startup. Refresh `speaker_user_id` from the
authenticated `USER_ID`.
Only when no state exists, initialize the durable JSON state at `STATE_PATH`:

```json
{
  "channel": "<channel>",
  "last_user_ts": "<startup timestamp>",
  "last_self_improve": 0,
  "last_arxiv_daily": 0,
  "rotation": "reference",
  "idle": false,
  "ken": "<absolute Ken path>",
  "speaker_prefix": "",
  "speaker_user_id": "<authenticated Slack user ID>",
  "scheduler": {
    "host": "<claude|codex>",
    "id": null,
    "previous_id": null,
    "created": 0,
    "normal_interval_minutes": 30,
    "mode": "normal",
    "fast_idle_polls": 0
  }
}
```

Set `idle` and the normal interval from the user's arguments. Write state with a
temporary sibling plus atomic rename; never partially overwrite it. Then follow
the selected host adapter to create the scheduler and record its returned ID.

## Run one tick

Treat these steps as the shared correctness contract for every scheduler:

1. Read and validate `STATE_PATH`. If the scheduler ID is null, do not guess;
   re-run startup or report the incomplete setup.
2. Apply the host adapter's stale-scheduler reconciliation or update rules.
3. Poll Slack with `"$ONGO" slack poll "$CHANNEL" "$LAST_USER_TS"`. When
   `speaker_prefix` is non-empty, append `--speaker-prefix "$SPEAKER_PREFIX"`.
   Always append `--speaker-user-id "$SPEAKER_USER_ID"`; the poller uses
   Slack's authenticated sender metadata so another user cannot forge an Ongo
   marker and get silently skipped.
4. Check `status` before interpreting the message count.
   - `error`: post an Ongo-prefixed failure notice, leave `last_user_ts`
     unchanged, skip expansion and cadence counters, apply rate-limit backoff,
     and end the tick.
   - `ok`: process the returned window.
5. If `user_count > 0`, acknowledge processing and handle every message in
   ascending timestamp order. Answer it inline or successfully dispatch it to
   one writer. Do not skip messages.
6. Advance `last_user_ts` only to the last message in the longest fully handled
   ascending prefix. Never advance past a failed or undispatched message, and
   never advance from bot traffic.
7. If the successful poll is empty and the loop is not idle, run at most one
   auto-expansion unit from [references/research.md](references/research.md).
8. When due and not rate-limited or in fast mode, run the daily arXiv sweep and
   the safe maintenance cycle from the research reference.
9. Apply the host adapter's fast-mode rule: activity requests a one-minute
   cadence; five successful empty fast polls request the normal cadence.
10. Atomically persist the updated state.

`ongo slack poll` follows every Slack cursor page, returns no partial window on
failure, filters only configured Ongo speaker forms, deduplicates by timestamp,
and sorts ascending. Do not replace it with direct `clacks read` calls.

## Process messages

Interpret Slack messages as natural language:

- Research: search authoritative sources, add references and analytical notes
  to Ken, relate them, and report failures without hiding partial work.
- Ken management: inspect or update publications and relationships through Ken.
- Exploration strategy: create or revise `ongo-exploration` publications.
- Experiments: read [references/experiments.md](references/experiments.md)
  completely and use only `ongo experiment` for experiment state.
- Publishing or access keys: read
  [references/publishing.md](references/publishing.md) completely.
- Maintenance: use the bounded, non-destructive maintenance rules in the
  research reference.

Quick questions may be answered inline. Delegate heavyweight research when the
host supports subagents, but enumerate write targets first and keep at most one
writer per file, publication key, state file, or Slack response stream. Send new
instructions for an in-flight artifact to its existing writer instead of
starting a competing writer.

If `writing-style.md` exists beside this file, run
`scripts/print-style.sh section` and follow the returned style instructions.
Pass the same style block to every prose-writing subagent.

## Message identity

Every Slack message sent by Ongo begins with the configured `speaker_prefix`
followed immediately by one of:

- `[ongo]` for the main loop.
- `[ongo, <agent-id>]` for a delegated agent.

The marker must be the first content after the optional speaker prefix. Preserve
case and brackets. Use the same `speaker_prefix` in `ongo slack poll`; otherwise
the poller deliberately treats the prefixed message as user input.

For each delegated run, the loop posts a spawn announcement, the agent may post
progress, and the agent posts one final `Done` sign-off. Pass the exact agent ID
to its prompt. Truncate Slack responses over 30,000 characters.

## Stop safely

Treat `/quit`, `/stop`, and `/exit` received through the configured Slack
channel as explicit shutdown requests. Post the shutdown message, follow the
host adapter's scheduler deletion procedure, verify no Ongo scheduler remains,
and preserve the research database and state file for inspection. Never infer a
shutdown from silence, context pressure, or a polling error.

## Non-negotiable invariants

- Never advance the Slack cursor past an unhandled message.
- Never treat a failed poll as an empty successful poll.
- Never let two agents write the same artifact concurrently.
- Never use direct Ken or SQLite mutation for experiment publication kinds.
- Never put access capabilities in Ken, Slack, URLs, logs, or command arguments.
- Preview bulk deletion before executing it.
- Do not upgrade dependencies, edit an installed plugin cache, delete research,
  or create external issues or pull requests without explicit authorization.
- Preserve scheduler continuity: create before delete on Claude Code; update the
  existing heartbeat on Codex.
