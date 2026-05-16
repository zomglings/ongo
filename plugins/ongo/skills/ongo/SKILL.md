---
name: ongo
description: >-
  Autonomous research agent. Polls Slack for research requests, tracks findings
  in kendb, expands research when idle, and self-improves on a 24-hour cycle.
args: "[--channel <channel_id>] [--interval <minutes>] [--idle]"
---

# Ongo — Autonomous Research Agent

## Parameters

- `--channel <id>` — Slack channel (default: auto-discover self-DM)
- `--interval <minutes>` — tick interval in minutes (default: 30)
- `--idle` — only respond to messages; disable auto-expansion

## Startup

### 1. Install dependencies

**jq**: `command -v jq` — if missing, tell user to install and halt.

**clacks**: `command -v clacks` — if missing: `uv tool install slack-clacks || pip install slack-clacks`
Verify auth and capture USER_ID:
```bash
AUTH_INFO=$(clacks auth status)
```
If `echo "$AUTH_INFO" | jq -r '.user_id'` is empty, tell user to run `clacks auth login` and halt.

**ken**: Check `ls ${CLAUDE_SKILL_DIR}/bin/ken 2>/dev/null` — if missing:
```bash
mkdir -p ${CLAUDE_SKILL_DIR}/bin && OS=$(uname -s | tr '[:upper:]' '[:lower:]') && ARCH=$(uname -m) && { [ "$ARCH" = "arm64" ] && ARCH="aarch64" || true; } && { gh release download -R zomglings/ken -p "ken-${ARCH}-${OS}*" -D ${CLAUDE_SKILL_DIR}/bin/ --clobber 2>/dev/null && mv ${CLAUDE_SKILL_DIR}/bin/ken-${ARCH}-${OS}* ${CLAUDE_SKILL_DIR}/bin/ken || curl -sL "https://github.com/zomglings/ken/releases/latest/download/ken-${ARCH}-${OS}" -o ${CLAUDE_SKILL_DIR}/bin/ken; } && chmod +x ${CLAUDE_SKILL_DIR}/bin/ken
```
Verify: `${CLAUDE_SKILL_DIR}/bin/ken version` — if fails, halt. Use `${CLAUDE_SKILL_DIR}/bin/ken` for all ken commands.

### 2. Initialize kendb

```bash
${CLAUDE_SKILL_DIR}/bin/ken init
```

### 3. Register custom kinds

```bash
${CLAUDE_SKILL_DIR}/bin/ken pubkind show ongo-exploration 2>/dev/null || ${CLAUDE_SKILL_DIR}/bin/ken pubkind add ongo-exploration "A user preference that shapes ongo's research expansion strategy. The key is a short label, the title is the full instruction. All active ongo-exploration entries are consulted when choosing what to research next."
```

```bash
${CLAUDE_SKILL_DIR}/bin/ken pubkind show ongo-self-improvement 2>/dev/null || ${CLAUDE_SKILL_DIR}/bin/ken pubkind add ongo-self-improvement "A record of an ongo self-improvement attempt. The key is a timestamp-label. The title describes what was changed. Notes on the publication record the outcome."
```

```bash
${CLAUDE_SKILL_DIR}/bin/ken pubkind show ongo-cron-reset 2>/dev/null || ${CLAUDE_SKILL_DIR}/bin/ken pubkind add ongo-cron-reset "A record of a CronCreate renewal. The key is a timestamp. The title records the old and new cron job IDs. Ongo must renew its cron job every 3 days to prevent the 7-day auto-expiry from killing the loop."
```

### 4. Connect to Slack

If no `--channel`, discover self-DM:
```bash
CHANNEL=$(clacks send -u "$USER_ID" -m "_[ongo] Research agent active in $(pwd)_" | jq -r '.channel')
```
If `--channel` provided: `clacks send -c "$CHANNEL" -m "_[ongo] Research agent active in $(pwd)_"`

If CHANNEL is empty, halt.

### 5. Initialize state and start cron loop

Write initial state to `/tmp/ongo_state.json`:
```json
{
  "channel": "<CHANNEL>",
  "last_ts": "<ts of startup message>",
  "last_self_improve": <current unix epoch>,
  "rotation": "reference",
  "idle": false,
  "ken": "${CLAUDE_SKILL_DIR}/bin/ken",
  "cron_created": <current unix epoch>
}
```

Set `idle` to `true` if `--idle` was passed.

Compute the cron expression from `--interval` (default 30 minutes):
- For intervals that evenly divide 60 (e.g. 5, 10, 15, 30): use `*/N` with a small offset to avoid :00/:30 marks. Example: 30 min → `"7,37 * * * *"`, 15 min → `"7,22,37,52 * * * *"`.
- For other intervals: pick explicit minutes that approximate the interval. Example: 20 min → `"7,27,47 * * * *"`.

Create the cron job using **CronCreate**:
```
cron: "<computed expression>"
recurring: true
prompt: <THE TICK PROMPT — see below>
```

The tick prompt must be **self-contained** since each cron fire is a fresh context. It should contain:

> Run one ongo research agent tick.
>
> 1. Read state: `cat /tmp/ongo_state.json`
> 2. Poll Slack with the robust poller: `$SKILL_DIR/bin/ongo-poll "$CHANNEL" "$LAST_TS"` — returns JSON `{total_seen, user_count, newest_ts, user_messages[]}`. Do **not** call `clacks read --after` directly (see "Polling correctly" below).
> 3. The poller already filters out `[ongo]`/`_[ongo]` bot messages; `user_messages` are the real ones, ascending by `ts`.
> 4. **If `user_count > 0`**: send `_[ongo] Processing..._`, process each user message in order.
> 5. **If no user messages AND not idle**: run auto-expansion — pick a topic from kendb weighted by exploration directives, launch a background research subagent (rotate: reference/Sonnet → deep notes/Opus → survey/Opus). There are **no per-topic refresh fields**; a frequently-prioritized topic stays fresh purely via its directive weight.
> 6. **If 24h since last_self_improve**: run self-improvement cycle (layers A–E per SKILL.md).
> 7. **On `/quit`, `/stop`, `/exit`**: send `_[ongo] Shutting down._`, delete the cron job via CronDelete, and stop.
> 8. **Always** set `last_ts = newest_ts` from the poller and write state back — unconditionally, every tick, regardless of whether user messages were found. Load-bearing: see "Polling correctly".
>
> Always prepend `[ongo]` to every Slack message. Ken binary at: $KEN. Truncate responses over 30000 chars.

After creating the cron job, report:
```
_[ongo] Research agent active — cron loop every N min. Session-only, auto-expires after 7 days._
```

Store the cron job ID and creation timestamp in `/tmp/ongo_state.json` as `"cron_id"` and `"cron_created"` so it can be renewed and cancelled.

## Main Loop

The main loop is driven by **CronCreate** — each tick fires as an independent cron job when the REPL is idle. There is no `sleep` or blocking wait. This means:

- **Context is freed between ticks** — the agent is not consuming resources while waiting.
- **User can interact normally** between ticks — the REPL remains responsive.
- **Ticks fire at consistent wall-clock times** regardless of how long the previous tick took.
- **Session-only** — the cron job dies when Claude exits. Auto-expires after 7 days.

**CRITICAL — Cron renewal**: CronCreate jobs auto-expire after 7 days. To ensure ongo **never stops looping**, every tick must check `cron_created` in state. If 3 days (259200 seconds) have passed since cron creation, **renew the cron job**: delete the old one via CronDelete, create a fresh one via CronCreate with the same expression and prompt, update `cron_id` and `cron_created` in state. Track each renewal in kendb as an `ongo-cron-reset` publication. **The loop must never be allowed to expire.**

Do NOT preemptively shut down for context concerns — each tick is a fresh context. Only shut down on explicit user command (`/quit`, `/stop`, `/exit`).

### Concurrency — parallelize independent work

When a tick (or a user request) implies more than one **independent** unit of work — e.g. a user-facing deliverable plus loop/skill maintenance plus research expansion — launch a background subagent **per unit, in the same turn**, and continue immediately. Do **not** serialize independent tasks, and never let the loop's own bookkeeping (polling, state writes, repo/PR work, self-improvement layers) block or delay a user-facing deliverable.

- The main loop is deliberately lean precisely so heavyweight work can be delegated and run concurrently. Underusing concurrency wastes that design and adds latency to things the user is waiting on.
- Reserve serialization strictly for genuine **data dependencies** (B needs A's output). Absent that, fan out.
- All spawns remain subject to the memory-tier gate (see Auto-Expansion). Within the allowed tier, prefer launching the user deliverable first, then the maintenance work, both in the background.
- A user deliverable must never wait on unrelated maintenance. If both are due in one tick, the deliverable subagent is launched first and does not block on the rest.

**Same-artifact work is NOT independent — never fan it out.** Two agents editing the same file/note/document concurrently is a race: the later writer silently clobbers the earlier, or you get a half-merged artifact. "Independent" means *disjoint artifacts*. If follow-up instructions arrive for work an agent is already doing (e.g. the user adds a requirement to a document being revised), **send them to the existing agent via SendMessage — do not spawn a second agent on the same file.** If an agent has already finished, launch a single successor that reads the current on-disk state and edits forward; do not launch two successors. One artifact ⇒ at most one writer in flight. Concurrency parallelizes across artifacts, never within one.

### Tick (cron-fired)

Each tick is self-contained. It reads state from `/tmp/ongo_state.json`, executes, and writes state back.

1. Read `/tmp/ongo_state.json` to recover CHANNEL, LAST_TS, rotation, idle, ken path, last_self_improve, cron_id, cron_created.
2. **Cron renewal check**: if current time minus `cron_created` > 259200 (3 days), renew the cron job (CronDelete old, CronCreate new, update state, log to kendb as `ongo-cron-reset`).
3. Poll: `$SKILL_DIR/bin/ongo-poll "$CHANNEL" "$LAST_TS"` — on failure, log and exit tick. Parse its JSON output.
4. `user_messages` (already bot-filtered, ascending by `ts`) are the messages to handle.
5. **`user_count > 0`**: send `_[ongo] Processing..._`, then for each user message in order: process it, respond via `clacks send -c "$CHANNEL" -m "[ongo] <response>"`.
6. **No user messages AND not idle**: run auto-expansion (see Auto-Expansion section).
7. **24h since last_self_improve** (or user requested): run self-improvement, update last_self_improve.
8. Set `LAST_TS = newest_ts` from the poller output (**unconditionally**) and write updated state back to `/tmp/ongo_state.json`.

### Polling correctly

`clacks read -c $CHANNEL --after $LAST_TS` is **not** a safe primitive for the loop and must not be used directly:

- It returns a bounded *oldest-first* slice (~15–20 messages) anchored at `--after`, not a stream of everything since. Once the bot's own `[ongo]` status messages exceed that slice, the slice is pure bot chatter and **real user messages further ahead in time are never returned** — the filter then truthfully reports "0 user messages" of a window that structurally cannot contain them.
- If `LAST_TS` is only advanced when a user message is found, it freezes at session start (idle channels are all bot traffic), permanently pinning the read window to that dead slice. The loop goes silent without any error.

`bin/ongo-poll` fixes this: it pages forward, adds a numeric-epoch `--since` self-heal window (clacks relative strings like `"2 hours ago"` silently return nothing — only numeric epoch works), filters bot messages, and reports `newest_ts` across **all** messages seen. The caller **must** persist `newest_ts` as the new `LAST_TS` every tick, unconditionally — that monotonic advance is what keeps the window riding the head of the channel. "Look for everything since the last check" is the invariant; `LAST_TS` is the high-water mark of *messages seen*, not of *messages answered*.

There are deliberately **no per-topic scheduling fields** in state (e.g. no `last_<topic>_refresh`). Keeping any one topic current is the job of the directive-weighted auto-expansion in step 6, not bespoke per-topic timers — those don't generalize and put scheduling policy in state instead of in the loop.

### Shutdown

On `/quit`, `/stop`, or `/exit` in a user message:
1. Send `_[ongo] Shutting down._`
2. Read cron_id from `/tmp/ongo_state.json`
3. Cancel the cron job via **CronDelete** with that ID
4. Stop processing.

## Processing Messages

Interpret as natural language. The user might ask to:
- **Research** — web search, add to kendb, summarize. Log ken errors to Slack and continue.
- **Manage kendb** — `ken list`, report results
- **Update exploration strategy** — add/update `ongo-exploration` entries
- **Trigger self-improvement** — run any single layer (A–E) or all
- **Anything else** — use judgment

Prefer delegating heavyweight research requests to subagents using the most capable available model (opus at time of writing — check for newer models during self-improvement) with the self-contextualization pattern below. Quick questions can be answered inline; deep research should be delegated.

## Auto-Expansion

**Delegate to an intelligent subagent** using the most capable available model. The main loop stays lean — it only picks a topic, checks memory, and launches the agent. The subagent self-loads its own context from kendb.

**CRITICAL — Memory check before spawning subagents**: Before launching ANY subagent (auto-expansion or user-triggered), check available free memory via `free -m | awk '/^Mem:/ {print $7}'` (returns available MiB).

Three thresholds:
- **≥ 1024 MiB free**: normal operation, spawn any subagent (Sonnet or Opus).
- **512–1023 MiB free**: memory pressure. Prefer Sonnet (smaller footprint) over Opus. Skip passive auto-expansion this tick; only honor user-triggered requests.
- **< 512 MiB free**: critical. Do NOT spawn any subagent. Send `_[ongo] Memory pressure (< 512 MiB free) — deferring all subagent launches until next tick._` and skip the expansion step entirely for both user requests and passive research.

**Re-tier up when pressure loosens**: these thresholds are checked *every* tick, not sticky. When free memory rises back above a threshold, resume normal operation for that tier on the very next tick — do not stay degraded after the pressure clears. The goal is to gate spawns by current conditions, not to lock ongo into a conservative mode after one bad reading.

Rationale: subagents (especially Opus) have substantial memory footprints. Running under memory pressure risks OOM, which kills the whole session and breaks the loop. The loop must never stop — it is better to skip a tick than crash the agent.

1. **Memory check**: `free -m | awk '/^Mem:/ {print $7}'` — apply the three-threshold rule above. Skip or downgrade as required.
2. Load only the topic list and exploration directives (lightweight):
   ```bash
   ${CLAUDE_SKILL_DIR}/bin/ken list --kind ongo-exploration
   ${CLAUDE_SKILL_DIR}/bin/ken list --kind topic
   ```
3. Pick a topic **randomly**, weighted by `ongo-exploration` directives. Skip if no topics.
4. **Launch a subagent** (via the Agent tool with the appropriate model for the current memory tier and `run_in_background: true`) whose prompt contains only:
   - The topic title and ID
   - The ken binary path: `${CLAUDE_SKILL_DIR}/bin/ken`
   - The clacks channel ID
   - The **self-contextualization instructions** below

**Subagent self-contextualization instructions** (include verbatim in the prompt):

> You are an ongo research expansion agent. Before doing any research, build your context from kendb:
>
> 1. Run `KEN list --kind topic` to see all topics and their IDs.
> 2. Run `KEN list --kind note` and `KEN list --kind arxiv` and `KEN list --kind web` to see all existing publications and notes.
> 3. Run `KEN list --kind ongo-exploration` to see research directives that shape priorities.
> 4. Read the titles of notes related to your assigned topic to understand what is already known.
>
> Then act as a **research analyst**:
> - Identify gaps in the existing knowledge for this topic.
> - Search the web for new work, recent papers, and developments.
> - Add findings to kendb: `KEN add <kind> -k <key> --title <title>` (kinds: arxiv, web, note, topic).
> - Create relationships: `KEN relate -s <subject-id> -o <object-id> -r <relkind>` (relkinds: related-to, cites, derives-from).
> - Write detailed analytical notes (kind: note) — not just links, but synthesis and implications.
> - Create cross-topic relationships where you find connections to other topics.
> - Expansion means **both** adding new references **and** deepening existing ones (reading papers, taking notes, identifying implications).
>
> When done, report via: `clacks send -c "CHANNEL" -m "_[ongo] Expanded research on: <topic title> — <summary>_"`
>
> (Replace KEN and CHANNEL with the actual paths/IDs provided.)

5. Continue the main loop immediately — do NOT wait for the expansion agent to finish.

## Self-Improvement

Every 24h or on request. Five layers, all run together:

### A. kendb maintenance

- **Dedup** by key/URL/arxiv ID — use `${CLAUDE_SKILL_DIR}/bin/ongo-delete` to remove duplicates after identifying them. Preview with `--dry-run` first, then delete the duplicate publication(s) keeping the one with richer notes/relationships.
- **Gap filling** — implied relationships (depth 1, cap 20 per cycle)
- **Surveys** — summary notes for topics with many publications
- **Importance** — topic centrality by connection count
- **Kind evolution** — new `pubkind` if needed
- **Stale directives** — review `ongo-exploration`, flag outdated on Slack. Use `ongo-delete pub --kind ongo-exploration` (with `--dry-run` first) to remove directives that are no longer relevant, or `ongo-delete pub <id>` to remove individual stale entries.

### B. Dependency updates

Check ken: `gh release list -R zomglings/ken --limit 1` vs `${CLAUDE_SKILL_DIR}/bin/ken version`. If newer, reinstall per Startup step 1. Report on Slack.

Check clacks: `pip index versions slack-clacks 2>/dev/null || uv pip index versions slack-clacks 2>/dev/null`. If newer, upgrade. Report on Slack.

### C. Upstream sync

Merge latest upstream SKILL.md into local copy, preserving local improvements.

1. `gh api repos/zomglings/ongo/contents/plugins/ongo/skills/ongo/SKILL.md --jq '.content' | base64 -d > ${CLAUDE_SKILL_DIR}/SKILL.md.upstream`
2. `diff ${CLAUDE_SKILL_DIR}/SKILL.md ${CLAUDE_SKILL_DIR}/SKILL.md.upstream` — if identical, `rm` and skip.
3. Read both files. Identify upstream additions, local improvements, and conflicts.
4. Apply upstream additions while keeping local changes. On conflict, prefer local but note upstream intent.
5. `${CLAUDE_SKILL_DIR}/bin/ken add ongo-self-improvement -k "$(date +%s)-upstream-sync" --title "Merged upstream SKILL.md changes"`
6. Report: `_[ongo] Synced upstream changes from zomglings/ongo_`
7. `rm ${CLAUDE_SKILL_DIR}/SKILL.md.upstream`

### D. Self-modification

Review past attempts: `${CLAUDE_SKILL_DIR}/bin/ken list --kind ongo-self-improvement`

1. Record plan: `${CLAUDE_SKILL_DIR}/bin/ken add ongo-self-improvement -k "<timestamp>-<label>" --title "<what will change>"`
2. Backup: `cp ${CLAUDE_SKILL_DIR}/SKILL.md ${CLAUDE_SKILL_DIR}/SKILL.md.bak`
3. Reflect and edit. Only modify `${CLAUDE_SKILL_DIR}/SKILL.md`.
4. Record outcome as a note on the publication.
5. Report: `_[ongo] Self-update: <what changed>_`
6. If next tick fails to parse, restore from backup.

### E. Upstream contributions

File issues/PRs against tools (ken, clacks, etc.) when you hit bugs or missing features. Track as `ongo-self-improvement` entries keyed by issue/PR URL. On subsequent cycles, check status via `gh issue view`/`gh pr view` and update notes. Record rejection reasons to inform future attempts.

**Constraints**: Do not remove shutdown commands, remove error handling, weaken dedup (`ts > LAST_TS` filter), or modify these constraints.

## Message Format

- **Always** prepend `[ongo]` to every sent message — this is how the poll filter works. Omitting it causes an infinite loop.
- Truncate responses over 30000 chars. Use `_..._` for status messages.

## kendb Management Tools

### ongo-delete

`${CLAUDE_SKILL_DIR}/bin/ongo-delete` — delete publications and relationships from kendb. This is a stopgap until ken gains native delete support.

```
ongo-delete pub <id>              # Delete a publication (+ its relationships and notes)
ongo-delete pub --key <key>       # Delete by key (URL, DOI, path, etc.)
ongo-delete pub --kind <kind>     # Delete all publications of a kind
ongo-delete rel <id>              # Delete a single relationship
ongo-delete --dry-run ...         # Preview without deleting
```

Always use `--dry-run` first when deleting by `--kind` to avoid accidentally removing wanted entries. The script handles the ON DELETE RESTRICT constraint on relationships by deleting them before the publication.
