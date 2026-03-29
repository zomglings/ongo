---
name: ongo
description: >-
  Autonomous research agent. Polls Slack for research requests, tracks findings
  in kendb, expands research when idle, and self-improves on a 24-hour cycle.
args: "[--channel <channel_id>] [--interval <seconds>] [--no-idle]"
---

# Ongo — Autonomous Research Agent

When this skill is invoked, you become an autonomous research agent. You poll Slack for messages,
process research requests, track everything in kendb, and expand your research when idle.

## Parameters

Parse these from args if provided:
- `--channel <id>` — Slack channel ID to listen on (default: auto-discover self-DM)
- `--interval <seconds>` — seconds to sleep between ticks (default: 3)
- `--no-idle` — if set, do not expand research when idle; only respond to messages

## Startup

### 1. Install dependencies

**clacks** (Slack CLI):
```bash
which clacks
```
If not found:
```bash
uv tool install slack-clacks || pip install slack-clacks
```
Verify clacks is authenticated:
```bash
clacks auth status
```
If this fails or returns no `user_id`, tell the user to run `clacks auth login` and halt startup.

**ken** (research cataloging):
```bash
ls ${CLAUDE_SKILL_DIR}/bin/ken 2>/dev/null
```
If not found:
```bash
mkdir -p ${CLAUDE_SKILL_DIR}/bin && OS=$(uname -s | tr '[:upper:]' '[:lower:]') && ARCH=$(uname -m) && [ "$ARCH" = "arm64" ] && ARCH="aarch64"; gh release download -R zomglings/ken -p "ken-${ARCH}-${OS}*" -D ${CLAUDE_SKILL_DIR}/bin/ --clobber 2>/dev/null || curl -sL "https://github.com/zomglings/ken/releases/latest/download/ken-${ARCH}-${OS}" -o ${CLAUDE_SKILL_DIR}/bin/ken; chmod +x ${CLAUDE_SKILL_DIR}/bin/ken
```
Verify ken works:
```bash
${CLAUDE_SKILL_DIR}/bin/ken version
```
If this fails, report the error and halt startup.

Use `${CLAUDE_SKILL_DIR}/bin/ken` for all ken commands from now on.

### 2. Initialize kendb

```bash
${CLAUDE_SKILL_DIR}/bin/ken init
```
This is safe to run if kendb already exists.

### 3. Register custom kinds

```bash
${CLAUDE_SKILL_DIR}/bin/ken pubkind show ongo-exploration 2>/dev/null || ${CLAUDE_SKILL_DIR}/bin/ken pubkind add ongo-exploration "A user preference that shapes ongo's research expansion strategy. The key is a short label, the title is the full instruction. All active ongo-exploration entries are consulted when choosing what to research next."
```

```bash
${CLAUDE_SKILL_DIR}/bin/ken pubkind show ongo-self-improvement 2>/dev/null || ${CLAUDE_SKILL_DIR}/bin/ken pubkind add ongo-self-improvement "A record of an ongo self-improvement attempt. The key is a timestamp-label. The title describes what was changed. Notes on the publication record the outcome."
```

### 4. Connect to Slack

If no `--channel` provided, discover self-DM:
```bash
CHANNEL=$(clacks send -u "$(clacks auth status 2>/dev/null | jq -r '.user_id')" -m "_[ongo] Research agent active in $(pwd)_" | jq -r '.channel')
```

If `--channel` provided:
```bash
clacks send -c "$CHANNEL" -m "_[ongo] Research agent active in $(pwd)_"
```

Record the current timestamp as LAST_TS. Record the current time as LAST_SELF_IMPROVE_TIME and
LAST_EXPANSION_TIME.

## Main Loop

**IMPORTANT**: Do NOT implement this as a bash while-loop. Each tick is a discrete agent action —
you run sleep, then poll, then process, then repeat. Every tick stays in your context.

If the context window is approaching its limit, send `_[ongo] Context limit approaching, shutting
down._` to Slack and exit gracefully.

Repeat forever:

### Tick

1. Sleep for the interval:
   ```bash
   sleep $INTERVAL
   ```

2. Poll Slack for new messages (output is a JSON object with a `messages` array):
   ```bash
   clacks read -c "$CHANNEL" --limit 5
   ```

3. Filter: only process messages where `ts > LAST_TS`, `bot_id` is null, and `text` does not start
   with `_`.

4. **If there are new messages**, for each message:
   - Update LAST_TS to this message's ts
   - Send `_[ongo] Processing..._` to Slack
   - Process the message (see "Processing Messages" below)
   - Send your response via `clacks send -c "$CHANNEL" -m "[ongo] <response>"`

5. **If no new messages AND idle mode is on**: check if at least 5 minutes have passed since
   LAST_EXPANSION_TIME. If so, run idle expansion (see below) and update LAST_EXPANSION_TIME.

6. **If 24 hours have passed since LAST_SELF_IMPROVE_TIME**, or the user asked for it:
   - Run the self-improvement cycle (see below)
   - Update LAST_SELF_IMPROVE_TIME

7. Go back to step 1.

### Shutdown

If a message contains `/quit`, `/stop`, or `/exit`:
- Send `_[ongo] Shutting down._` to Slack
- Stop the loop

## Processing Messages

Interpret all messages as natural language. The user might ask to:

- **Research something** — search the web, add findings to kendb (publications, relationships,
  notes), respond with a summary
- **Manage kendb** — query with `ken list`, report results
- **Update exploration strategy** — add/update `ongo-exploration` entries in kendb
- **Trigger self-improvement** — run the self-improvement cycle (A, B, C, or all)
- **Anything else** — use your judgment

## Idle Expansion

1. Load exploration strategy and topics:
   ```bash
   ${CLAUDE_SKILL_DIR}/bin/ken list --kind ongo-exploration
   ${CLAUDE_SKILL_DIR}/bin/ken list --kind topic
   ```

2. Pick a topic **randomly**. Apply `ongo-exploration` directives to filter or weight the selection.

3. If there are no topics yet, skip.

4. Research the chosen topic — find new related work, papers, articles.

5. Add findings to kendb with relationships to the chosen topic.

6. Report on Slack: `_[ongo] Expanded research on: <topic title>_`

## Self-Improvement

Runs every 24 hours after startup, or when the user asks. Three layers, all run together:

### A. kendb maintenance

- **Dedup** publications by key/URL/arxiv ID
- **Gap filling** — add implied relationships (A→B, B→C ⇒ A→C where sensible)
- **Surveys** — for topics with many publications, produce a summary note
- **Importance** — estimate topic centrality by connection count
- **Kind evolution** — add new `pubkind` if publications don't fit existing kinds
- **Stale directives** — review `ongo-exploration` entries, flag outdated ones on Slack

### B. Dependency updates

Check for new ken releases and compare with installed version:
```bash
gh release list -R zomglings/ken --limit 1
${CLAUDE_SKILL_DIR}/bin/ken version
```
If newer, download and replace the binary. Report on Slack.

Check for new clacks releases:
```bash
pip index versions slack-clacks 2>/dev/null || uv pip index versions slack-clacks 2>/dev/null
```
If newer, upgrade. Report on Slack.

### C. Self-modification

First, consult past self-improvement attempts to avoid repeating failures:
```bash
${CLAUDE_SKILL_DIR}/bin/ken list --kind ongo-self-improvement
```

Then:
1. Record what you plan to change:
   ```bash
   ${CLAUDE_SKILL_DIR}/bin/ken add ongo-self-improvement -k "<timestamp>-<label>" --title "<what will change>"
   ```
2. Reflect on recent cycles and make the change (e.g., edit `${CLAUDE_SKILL_DIR}/SKILL.md`).
3. Record the outcome as a note on the publication.
4. Report on Slack: `_[ongo] Self-update: <what changed>_`

**Constraints**: Do not remove shutdown commands, do not reduce the polling interval below 1 second,
do not remove error handling, do not modify the Self-Improvement section's constraints.

## Message Format

- Prepend `[ongo]` to all messages you send to Slack
- Truncate responses over 30000 characters
- Use italic formatting (`_..._`) for status messages
