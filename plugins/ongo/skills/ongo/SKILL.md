---
name: ongo
version: 0.3.14
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

```bash
${CLAUDE_SKILL_DIR}/bin/ken pubkind show ongo-web 2>/dev/null || ${CLAUDE_SKILL_DIR}/bin/ken pubkind add ongo-web "Publish marker for the static site. The key is the target publication's id (or its key), the title is the display/nav label, and an optional notes body overrides the section/topic heading. Only items referenced by an ongo-web entry appear on the generated site (see bin/ongo-site)."
```

**Registration is not durable — every `ken add <customkind>` must be idempotent.** Startup registration above happens once, but each tick and each subagent runs in a fresh context and may operate against a kendb instance where the custom kind is not registered (this was observed in production: `ken add ongo-cron-reset` failed mid-run because the pubkind was missing from that kendb instance and had to be re-registered by hand). Therefore **never call `ken add <customkind> …` bare.** Always ensure the pubkind first, in the same step:

```bash
# Reusable pattern — use this everywhere a custom kind is written (ongo-exploration, ongo-self-improvement, ongo-cron-reset, ongo-web):
${CLAUDE_SKILL_DIR}/bin/ken pubkind show <kind> 2>/dev/null || ${CLAUDE_SKILL_DIR}/bin/ken pubkind add <kind> "<description from startup>"
${CLAUDE_SKILL_DIR}/bin/ken add <kind> -k "<key>" --title "<title>"
```

This guard is cheap (a `pubkind show` is local) and is the only thing that keeps cron-reset / self-improvement logging from silently failing on a re-initialized kendb. The standard `note`/`topic`/`arxiv`/`web` kinds are built into ken and do not need this guard. (This relies on `ken pubkind show` exiting non-zero for an absent kind — fixed in ken via zomglings/ken#7; ensure the deployed ken includes that fix.)

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
  "last_user_ts": "<ts of startup message>",
  "last_self_improve": <current unix epoch>,
  "rotation": "reference",
  "idle": false,
  "ken": "${CLAUDE_SKILL_DIR}/bin/ken",
  "cron_id": null,
  "prev_cron_id": "",
  "cron_created": <current unix epoch>,
  "normal_cron": "<computed cron expression from --interval>",
  "mode": "normal",
  "fast_idle_polls": 0
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
> 2. Poll Slack with the robust poller: `$SKILL_DIR/bin/ongo-poll "$CHANNEL" "$LAST_USER_TS"` — returns JSON `{status, total_seen, user_count, newest_user_ts, user_messages[]}`. Do **not** call `clacks read --after` directly (see "Polling correctly" below). **Check `status` first** (load-bearing — see "Polling correctly"):
>    - `status == "error"`: the read **failed** (rate limit / API error). This is **not** an idle tick. Send `_[ongo] Poll failed (<error>) — backing off, will retry next tick._`, **leave `last_user_ts` unchanged**, do **not** run auto-expansion, do **not** increment `fast_idle_polls`, and if `error == "ratelimited"` and `mode == "fast"` revert to `normal_cron` (see "Slack API rate-limit budget"), then end the tick.
>    - `status == "truncated"`: the read succeeded but the window was **saturated** (>200 msgs since last poll; Slack dropped the oldest). Process the returned messages as in step 4, then set `last_user_ts = newest_user_ts` (a safe earlier anchor the poller chose, **not** the true newest) and **immediately schedule another poll** (treat as fast-mode / re-poll next tick) until a `status == "ok"` poll confirms the channel is fully drained. Do **not** treat as idle.
>    - `status == "ok"`: window fully drained; proceed normally.
> 3. The poller filters out `[ongo]`/`_[ongo]` bot messages and returns only user messages with `ts > LAST_USER_TS`, ascending by `ts`.
> 4. **If `status` is `ok`/`truncated` and `user_count > 0`**: send `_[ongo] Processing..._`, process **every** returned user message in ascending order (do not skip any, even if you also spawn a background agent for one).
> 5. **If `status == "ok"` AND no user messages AND not idle**: run auto-expansion — pick a topic from kendb weighted by exploration directives, launch a background research subagent (rotate: reference/Sonnet → deep notes/Opus → survey/Opus). There are **no per-topic refresh fields**; a frequently-prioritized topic stays fresh purely via its directive weight. (A `status == "error"` poll is never an idle tick — do not expand on it.)
> 6. **If 24h since last_self_improve**: run self-improvement cycle (layers A–E per SKILL.md).
> 7. **On `/quit`, `/stop`, `/exit`**: send `_[ongo] Shutting down._`, then CronDelete `cron_id` **and** sweep for orphan crons (any cron whose prompt begins `Run one ongo research agent tick.` — fast-mode/renewal swaps may have left a stale one), and stop. See "Shutdown".
> 8. **Fast-mode transition** (see "Fast mode"), only when `status != "error"`: if `user_count > 0` (or `status == "truncated"`), reset `fast_idle_polls=0` and enter fast mode (1-min cron) if currently normal via the **safe cron-swap procedure**; if `user_count == 0` and `status == "ok"` and in fast mode, increment `fast_idle_polls` and exit to `normal_cron` after it reaches 5.
> 9. **Only after every returned user message has been handled/dispatched**, advance `last_user_ts` to the **longest fully-handled ascending prefix** of `user_messages` (the last message such that it and every earlier message was handled/dispatched-OK) and write state back — this equals `newest_user_ts` only if the whole batch succeeded; if a middle message failed, stop at the last good one *before the hole*. If `status == "error"` or `user_count == 0`, leave `last_user_ts` unchanged. On `status == "truncated"` advance to the poller's (deliberately earlier) `newest_user_ts` and re-poll until `ok`. **Never** advance past an unprocessed/failed user message, and **never** advance because the bot sent a message. A re-surfaced already-answered message is the accepted cost of never losing one. Load-bearing: see "Polling correctly".
>
> Always prepend `[ongo]` to every Slack message. Ken binary at: $KEN. Truncate responses over 30000 chars.

After creating the cron job, report:
```
_[ongo] Research agent active — cron loop every N min. Session-only, auto-expires after 7 days._
```

The initial state above writes `"cron_id": null` (the cron does not exist yet at state-init time) and `"prev_cron_id": ""` (no swap has happened yet). Immediately after **CronCreate** succeeds, overwrite `"cron_id"` with the returned job ID and refresh `"cron_created"` to the creation epoch, then write state back, so the job can be renewed and cancelled. A tick that reads state and finds `cron_id == null` means CronCreate never completed — re-run startup step 5 rather than attempting renewal/shutdown against a nonexistent job.

## Main Loop

The main loop is driven by **CronCreate** — each tick fires as an independent cron job when the REPL is idle. There is no `sleep` or blocking wait. This means:

- **Context is freed between ticks** — the agent is not consuming resources while waiting.
- **User can interact normally** between ticks — the REPL remains responsive.
- **Ticks fire at consistent wall-clock times** regardless of how long the previous tick took.
- **Session-only** — the cron job dies when Claude exits. Auto-expires after 7 days.

**CRITICAL — Cron renewal**: CronCreate jobs auto-expire after 7 days. To ensure ongo **never stops looping**, every tick must check `cron_created` in state. If 3 days (259200 seconds) have passed since cron creation, **renew the cron job**. Track each renewal in kendb as an `ongo-cron-reset` publication — **using the idempotent ensure-pubkind-then-add pattern from Startup step 3** (a fresh tick context may hit an uninitialized kendb; a bare `ken add ongo-cron-reset` will silently fail there, losing the renewal audit trail). **The loop must never be allowed to expire.**

**Safe cron-swap procedure (the canonical primitive — used by renewal AND every fast-mode mode change):** the swap is **create-then-delete**, in this exact order:

1. `CronCreate` the new job (same tick prompt; new expression).
2. **Only after** the new `cron_id` is in hand, write state to `/tmp/ongo_state.json`: set `cron_id` to the new id, set `cron_created`, copy the *previous* `cron_id` into `prev_cron_id`, and update `mode`/`fast_idle_polls`/`normal_cron` as applicable.
3. `CronDelete` the **old** id. On success, clear `prev_cron_id` (write state). On failure, leave `prev_cron_id` set so the next tick retries.
4. Log the `ongo-cron-reset` kendb entry (old id → new id, reason), using the idempotent ensure-pubkind-then-add pattern from Startup step 3.

**Stale-cron reconciliation (tick step 2, runs every tick before the renewal check):** if `prev_cron_id` is non-empty and not equal to the current `cron_id`, attempt `CronDelete prev_cron_id`; on success clear it. This guarantees that a tick which died mid-swap (leaving a duplicate) is self-healed by the *next* fire of either surviving cron — the transient duplicate is bounded to one tick interval.

Rationale: each tick is an independent, killable context. **Never `CronDelete` before the replacement exists.** Delete-then-create has a fatal window: if the tick dies (or `CronCreate` fails) after the delete, there is **zero** cron left and nothing can recreate it — the loop is permanently dead. Create-then-delete fails safe: a crash in the window leaves a transient *duplicate* cron (both fire; reconciliation above deletes the stale id on the next tick), which is recoverable. A brief duplicate is acceptable; a gap is not.

**Durability is uniform**: every `CronCreate` ongo issues — startup, renewal, fast-mode enter, fast-mode exit — uses the **same** `recurring: true` setting and makes **no** `durable` request. The harness treats these as session-scoped (they die when Claude exits and auto-expire after 7 days); the renewal logic depends only on the 7-day expiry, not on any cross-session durability guarantee. Do not request `durable` for some swaps and not others — mixed durability across the swap sites makes the renewal window non-uniform and the guarantee unanalyzable.

Do NOT preemptively shut down for context concerns — each tick is a fresh context. Only shut down on explicit user command (`/quit`, `/stop`, `/exit`).

### Concurrency — parallelize independent work

When a tick (or a user request) implies more than one **independent** unit of work — e.g. a user-facing deliverable plus loop/skill maintenance plus research expansion — launch a background subagent **per unit, in the same turn**, and continue immediately. Do **not** serialize independent tasks, and never let the loop's own bookkeeping (polling, state writes, repo/PR work, self-improvement layers) block or delay a user-facing deliverable.

- The main loop is deliberately lean precisely so heavyweight work can be delegated and run concurrently. Underusing concurrency wastes that design and adds latency to things the user is waiting on.
- Serialize for **two** reasons only: a genuine **data dependency** (B needs A's output) **or** a **write conflict** (A and B mutate the same artifact). Absent both, fan out.
- All spawns remain subject to the memory-tier gate (see Auto-Expansion). Within the allowed tier, prefer launching the user deliverable first, then the maintenance work, both in the background.
- A user deliverable must never wait on unrelated maintenance. If both are due in one tick, the deliverable subagent is launched first and does not block on the rest.

**Same-artifact work is NOT independent — never fan it out.** Two agents writing the same file/note/document/kendb publication concurrently is a race: the later writer silently clobbers the earlier, or you get a half-merged artifact. A data dependency is not required for this to be unsafe — two blind writers need nothing from each other and still corrupt the result. "Independent" means *disjoint write targets*.

**Operational test — apply before every fan-out.** For each pair of units about to be launched in the same turn, enumerate the concrete artifacts each will *write*: file paths it will edit, kendb publication IDs/keys it will `add`/relate/`ongo-delete`, the SKILL.md self-modification file, the Slack channel state, `/tmp/ongo_state.json`. If the write-sets intersect (same path, same publication ID/key, same file), the units are **not** independent — collapse them into one agent. When in doubt, treat the artifact as shared and serialize; a missed parallelization costs latency, a missed conflict costs the artifact. Read-only overlap (both agents `ken list` the same kind) is fine; only overlapping **writes** force serialization.

**Follow-ups to in-flight work.** If new instructions arrive for work an agent is already doing (e.g. the user adds a requirement to a document being revised, or a second message targets a note another tick's agent is writing), **send them to the existing agent via SendMessage — do not spawn a second agent on the same artifact.** If the agent has already finished, launch a *single* successor that reads the current on-disk/kendb state and edits forward; never launch two successors. One artifact ⇒ at most one writer in flight. Concurrency parallelizes across artifacts, never within one. This is the only safe way to honor "process every returned user message" (Tick step 6) when two messages in one batch touch the same artifact: they are processed in ascending order by the *same* writer, not by racing agents.

### Tick (cron-fired)

Each tick is self-contained. It reads state from `/tmp/ongo_state.json`, executes, and writes state back.

1. Read `/tmp/ongo_state.json` to recover CHANNEL, LAST_USER_TS, rotation, idle, ken path, last_self_improve, cron_id, cron_created, prev_cron_id.
2. **Stale-cron reconciliation**: if `prev_cron_id` is non-empty and ≠ `cron_id`, `CronDelete prev_cron_id`; on success clear `prev_cron_id` and write state. (Self-heals a duplicate left by a tick that died mid-swap; see "Cron renewal".)
3. **Cron renewal check**: if current time minus `cron_created` > 259200 (3 days), renew the cron job via the **safe cron-swap procedure** (CronCreate new → write state → CronDelete old → log `ongo-cron-reset`; see "Cron renewal"). Never delete the old cron before the new one exists.
4. Poll: `$SKILL_DIR/bin/ongo-poll "$CHANNEL" "$LAST_USER_TS"`. Parse its JSON and **branch on `status`** (this check is load-bearing — a missing-status check is exactly how the loop went silently deaf historically):
   - `status == "error"`: read **failed** (rate limit / API error). Send `_[ongo] Poll failed (<error>) — backing off._`, leave `LAST_USER_TS` unchanged, **skip steps 6–9** (no expansion, no fast-mode counter change). If `error == "ratelimited"`, additionally follow the cadence back-off in "Slack API rate-limit budget" (revert fast mode if active). End tick.
   - `status == "truncated"`: read succeeded but window saturated (>200 msgs; Slack dropped oldest). Handle returned messages (step 6), set `LAST_USER_TS = newest_user_ts` (the poller's safe earlier anchor), and force a re-poll (stay in / enter fast mode) until a `status == "ok"` poll. Never treat as idle.
   - `status == "ok"`: proceed normally.
5. `user_messages` (bot-filtered, `ts > LAST_USER_TS`, ascending) are the messages to handle.
6. **`user_count > 0`** (status `ok` or `truncated`): send `_[ongo] Processing..._`, then for **every** message in ascending order: process it (or dispatch a background agent for it), respond via `clacks send -c "$CHANNEL" -m "[ongo] <response>"`. Do not skip any, even partially-handled ones. **A message is "handled" only once it has either been answered inline or successfully dispatched to a background agent that acknowledged start.** If processing or dispatch of message *i* fails (exception, agent failed to launch, dispatch errored), stop treating later messages as gating: record the *highest ts that was fully handled with no unhandled message before it* — this, not `newest_user_ts`, is what step 10 may advance to. A dispatched agent that later *fails mid-work* must post the failure to Slack so the user can re-ask.
7. **`status == "ok"` AND no user messages AND not idle**: run auto-expansion (see Auto-Expansion section). (Never on `status == "error"`.)
8. **24h since last_self_improve** (or user requested): run self-improvement, update last_self_improve.
9. **Fast-mode transition** (see "Fast mode"), only when `status != "error"`: `user_count > 0` or `status == "truncated"` → `fast_idle_polls=0`, enter fast mode if normal (no swap if already fast); `user_count == 0` and `status == "ok"` and fast → `fast_idle_polls++`, exit to `normal_cron` at 5. Every mode change uses the **safe cron-swap procedure** (create-then-delete; see "Cron renewal").
10. Advance the gate to the **longest fully-handled ascending prefix**, never blindly to `newest_user_ts`. Concretely: walk `user_messages` ascending; set `LAST_USER_TS` to the ts of the last message such that *it and every message before it* was handled (step 6 definition). If the whole batch succeeded this equals `newest_user_ts`. If a middle message failed, the gate stays at the last good message *before the hole* — the failed message and everything after it stay outstanding and are re-polled next tick. **Never advance past a hole**, even though that means already-answered later messages in the same batch will be re-surfaced and re-answered (accepted: see "Reprocessing"). If `status == "error"` or `user_count == 0`, leave `LAST_USER_TS` unchanged. On `status == "truncated"` advance to the poller's deliberately-earlier `newest_user_ts`. Then write state back.

### Polling correctly

`clacks read -c $CHANNEL --after $ts` is **not** a safe primitive for the loop and must not be used directly. Four bugs were found and fixed in sequence (each fix exposed the next):

- **Capped slice.** `clacks read --after` returns a bounded *oldest-first* slice (~15–20 msgs) anchored at `--after`, not a stream of everything since. Once the bot's own `[ongo]` messages exceed that slice it is pure bot chatter and real user messages further ahead are never returned — the filter then truthfully reports "0 user messages" of a window that structurally cannot contain them.
- **Bot-contaminated cursor.** The first fix advanced the cursor to the newest message *including the bot's own sends*. A user message timestamped before one of the bot's sends but after the previous cursor was then excluded by the `> cursor` filter next poll — silently missed because the bot "spoke later." This actually happened (four user messages lost in one busy turn).
- **Three reads / poll → masked rate limit.** A later design issued three reads per poll (`recent` + `--since` + `--after`); under 1-min fast-mode polling this tripped Slack HTTP-429 `ratelimited`, and because every read was `try/except -> []` a hard 429 was read as "0 messages, all clear" — deaf again.
- **Single capped read silently truncates.** The current poller issues exactly **one** `clacks read --since LAST_USER_TS -l 200`, which becomes a single non-paginated Slack `conversations.history(oldest=LAST_USER_TS, limit=200, inclusive=True)`. Slack returns the **newest 200** in the window and **silently drops the oldest** when more exist. If >200 messages land between polls the oldest unprocessed *user* messages disappear while the cursor still advances — permanently skipped. The poller now detects saturation (`total_seen >= 200`) and returns `status == "truncated"` with a deliberately *earlier* `newest_user_ts` anchor (just below the oldest message actually seen), so the next poll's `--since` window re-covers the dropped older slice.

**The poller contract.** `bin/ongo-poll` takes `LAST_USER_TS`, issues **one** `--since LAST_USER_TS` read (a Slack `ts` is a valid `--since` value; clacks relative strings like `"2 hours ago"` silently return nothing — only a `ts`/epoch works, and `--after` is unsafe per the bugs above), filters out `[ongo]`/`_[ongo]` bot messages, returns user messages with `ts > LAST_USER_TS` ascending, and a `status` of `ok` / `truncated` / `error` (with exponential back-off 5/15/30s — under the 60s fast cron — on transient read failure before reporting `error`). **The caller MUST branch on `status`** (see Tick steps): `error` ≠ idle (back off, do not advance, do not expand); `truncated` ≠ drained (process, advance to the safe anchor, re-poll until `ok`); only `ok` means the window is fully drained. Conflating `error`/`truncated` with "0 user messages → idle" is precisely how the loop goes silently deaf.

**The invariant.** The gate is `LAST_USER_TS` = ts of the most recent USER message *actually processed*. It advances **only** when user messages are handled, **never** because the bot sent something, and **never** past an unprocessed user message. There is no separate bot-influenced cursor — bot/loop traffic is irrelevant to whether a user message is outstanding. Process the whole returned batch each tick in ascending order; only then advance `LAST_USER_TS` over the fully-handled prefix (Tick step 10). The delivery guarantee is **at-least-once, never-lost**: "every user message is eventually processed; under saturation or a mid-batch failure a small newer slice may be re-seen" — *not* "exactly once," which is impossible without pagination, and *not* "ride the head of the channel." At-least-once with bounded duplication is strictly safer than the silent drop it replaces. Slack `ts` are canonical `<10-digit-seconds>.<6-digit-micros>` strings; in the current epoch era (2001–2286) integer width is fixed, so the poller's lexicographic `ts` comparison is equivalent to numeric ordering for all genuine message timestamps.

**Reprocessing (accepted tradeoff).** Because the gate may only advance over a contiguous handled prefix and **never past an unprocessed (failed/held) message**, a later message in the same batch that *was* answered will be re-surfaced and re-answered on the next poll if an earlier message in that batch failed. This is not a bug — it is the chosen behavior, and it has been observed (a Markov-chain question got a second answer after an earlier message in its batch errored). Loss is unacceptable; a duplicate answer is merely noisy. Mitigations, in order: (a) make message handling **idempotent where cheap** — before answering, a handler may scan recent `[ongo]` sends for an answer it already posted to the same question and skip/acknowledge instead of recomputing; (b) keep batches small (fast mode shortens the poll window, shrinking batches and thus the reprocess blast radius); (c) when re-answering a known duplicate, prefix `_[ongo] (re-sending — earlier sibling in this batch failed)_` so the user understands the repeat. None of these may weaken the rule: **the gate still never advances past the failed message.**

There are deliberately **no per-topic scheduling fields** in state (e.g. no `last_<topic>_refresh`). Keeping any one topic current is the job of the directive-weighted auto-expansion (Tick step 7), not bespoke per-topic timers — those don't generalize and put scheduling policy in state instead of in the loop.

### Fast mode — responsive polling during active conversations

The base cadence (`--interval`, default 30 min) is fine when idle but far too slow when the user is actively talking. **Fast mode** makes the loop poll every 1 minute while a conversation is live, then fall back automatically.

State carries: `"mode"` (`"normal"` | `"fast"`, default `"normal"`), `"fast_idle_polls"` (int, default `0`), and `"normal_cron"` (the base cron expression computed at startup from `--interval`, stored so it can be restored).

Cron expressions: normal = `normal_cron`; fast = `"* * * * *"` (every minute).

Transition logic, evaluated every tick **after** polling and message handling, **before** the state write:

- **`user_count > 0`** (the user said something) or `status == "truncated"`: set `fast_idle_polls = 0`. If `mode == "normal"`, **enter fast mode**: perform the **safe cron-swap procedure** (see "Cron renewal") with the new expression `"* * * * *"` and the same tick prompt; also set `mode = "fast"` in the same state write. (`recurring: true`, no `durable` — same as every other CronCreate.) If `mode == "fast"` already, do **not** swap — only the `fast_idle_polls = 0` reset applies (no cron churn for an ongoing back-and-forth).
- **`user_count == 0` and `status == "ok"` and `mode == "fast"`**: increment `fast_idle_polls`. If `fast_idle_polls >= 5`, **exit fast mode**: perform the **safe cron-swap procedure** with the new expression `normal_cron`; in the same state write set `mode = "normal"` and `fast_idle_polls = 0`.
- **`mode == "normal"` and `user_count == 0`**: nothing.
- **`status == "error"`**: no fast-mode transition counter change; if `error == "ratelimited"` and `mode == "fast"`, revert to `normal_cron` per "Slack API rate-limit budget".

Properties: entering fast mode is immediate on the first detected user message; a back-and-forth keeps resetting `fast_idle_polls` (no extra cron swap — only the normal→fast and fast→normal edges swap) so the loop stays at 1-min cadence for the whole exchange; after 5 consecutive 1-min polls (~5 min) with silence it reverts. The cron-renewal check still applies to whichever cron is active.

**Swap churn is bounded.** At most one cron swap per fast/normal *edge*: a sustained conversation triggers exactly one normal→fast swap, and reversion triggers exactly one fast→normal swap. Even a pathological pattern (one user message every ~6 min forever) is bounded to one pair of swaps per ~5-min reversion cycle — not per message — because the `mode == "fast"` guard above suppresses redundant swaps. This is safe given the create-then-delete primitive.

**Renewal interaction (load-bearing).** Every swap *is* a fresh `CronCreate`, so it resets the new cron's own 7-day expiry clock — and `cron_created` is updated to match. A long-lived flapping session therefore keeps resetting `cron_created`, so the explicit 3-day renewal check may *never* fire. That is acceptable **only because** each swap is itself a real fresh cron that restarts the 7-day clock — i.e. swaps double as renewals. This makes the safe-swap primitive's create-then-delete ordering not merely advisory but **required**: a swap that deletes the old cron and then fails to create the new one leaves zero cron *and* a `cron_created` that no longer reflects reality, with no surviving cron to ever run the renewal check — unrecoverable. Never weaken the swap ordering.

### Slack API rate-limit budget

Slack rate-limits **per method, per workspace**. The two methods ongo uses:

- `clacks read` → `conversations.history` — **Tier 3, ~50 requests/minute**.
- `clacks send` → `chat.postMessage` — roughly **1 message/second sustained** per channel.

**Calls per tick** (count them — this is load-bearing):

| Operation | `read` calls | `send` calls |
|---|---|---|
| `ongo-poll` | 1 (up to 4 with internal back-off retries on 429/error) | 0 |
| `_[ongo] Processing..._` | 0 | 1 (only if `user_count > 0`) |
| Per user message reply | 0 | 1 × `user_count` |
| Fast-mode / cron-reset status post | 0 | 0–1 |
| Auto-expansion subagent report | 0 | 1 (idle ticks, async) |
| Self-improvement reports (every 24h) | 0 | up to ~6 |

A normal idle tick is **1 read + 0 sends**. A busy fast-mode tick with a 3-message burst is **1 read + 4 sends**.

**The real incident**: an earlier design did 3 reads/poll and fast mode polled every 60s unconditionally; `3 reads × 60 polls/hr = 180 reads/hr` plus ad-hoc reads pushed `conversations.history` over Tier 3 and the token got HTTP-429 `ratelimited`, which the loop then mis-read as "0 messages, all clear" and went deaf. The poller is now **1 read/poll** with bounded exponential back-off (5/15/30s), which fixes the volume. The remaining exposure is **cadence**, not volume:

**Safe ceiling**: at 1 read/poll, even sustained 1-min fast-mode polling is `60 reads/hr` — well under Tier 3's ~3000/hr. The budget is comfortable **as long as the poller stays at one read per poll and the back-off is respected**. Do not reintroduce multi-read polling, and do not add ad-hoc `clacks read` calls outside `bin/ongo-poll`.

**Rate-limit-aware back-off (cadence)**: if `ongo-poll` returns `status == "error"` with `error == "ratelimited"` (the poller has already exhausted its internal ~50s back-off), the tick **must not** treat it as idle and **must not** keep hammering at the current cadence:

1. Report `_[ongo] Slack rate-limited — backing off, will retry next tick._` (a single `send`; skip even this if the rate limit is on `chat.postMessage`).
2. Leave `LAST_USER_TS` unchanged (unread window is unknown — see "Polling correctly").
3. **If `mode == "fast"`**: immediately revert to `normal_cron` for this back-off via the **safe cron-swap procedure** (CronCreate `normal_cron` → write state with `mode = "normal"`, `fast_idle_polls = 0` → CronDelete old → log `ongo-cron-reset`). A 60s cadence is what produced the 429 in the first place; backing the cadence off to the normal interval is the correct response, and a genuine subsequent user message will re-enter fast mode normally.
4. Do not advance any cron-renewal or self-improvement work this tick.

This makes fast mode rate-limit-aware: it accelerates for live conversations but yields the moment Slack signals overload, instead of compounding the problem.

### Shutdown

On `/quit`, `/stop`, or `/exit` in a user message:
1. Send `_[ongo] Shutting down._`
2. Read `cron_id` from `/tmp/ongo_state.json`.
3. Cancel the cron job via **CronDelete** with that ID.
4. **Defensively sweep for orphans.** `cron_id` is rewritten on every fast-mode swap and every 3-day renewal (create-then-delete). If a previous swap was interrupted between the create and the `prev_cron_id` cleanup, a stale ongo cron can still be live with an ID no longer in `cron_id` (check `prev_cron_id` too). List all cron jobs and CronDelete any whose prompt is the ongo tick prompt (it begins with `Run one ongo research agent tick.`), not only the one recorded in state. Shutting down while leaving an orphan cron alive means the loop never actually stops.
5. Stop processing.

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
- **Regenerate the published site** — run `${CLAUDE_SKILL_DIR}/bin/ongo-site` to rebuild the static site from the `ongo-web` publish set. It is idempotent, deterministic, and rewrites the output dir cleanly, so it is safe to run every cycle. Hosting is self-served (`bin/ongo-serve`) on the user's own server; **DNS is the user's manual step — ongo never performs DNS or deploys.**

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

(Every `ken add ongo-self-improvement` and `ken add ongo-cron-reset` below — and the upstream-sync record in layer C — must use the idempotent ensure-pubkind-then-add pattern from Startup step 3, since this layer also runs in a fresh tick context.)

1. Record plan: `${CLAUDE_SKILL_DIR}/bin/ken add ongo-self-improvement -k "<timestamp>-<label>" --title "<what will change>"`
2. Backup: `cp ${CLAUDE_SKILL_DIR}/SKILL.md ${CLAUDE_SKILL_DIR}/SKILL.md.bak`
3. Reflect and edit. Only modify `${CLAUDE_SKILL_DIR}/SKILL.md`.
4. Record outcome as a note on the publication.
5. Report: `_[ongo] Self-update: <what changed>_`
6. If next tick fails to parse, restore from backup.

### E. Upstream contributions

File issues/PRs against tools (ken, clacks, etc.) when you hit bugs or missing features. Track as `ongo-self-improvement` entries keyed by issue/PR URL. On subsequent cycles, check status via `gh issue view`/`gh pr view` and update notes. Record rejection reasons to inform future attempts.

**Constraints**: Do not remove shutdown commands, remove error handling, weaken the `ts > LAST_USER_TS` user-message gate or advance it on bot traffic, bypass `bin/ongo-poll`, or modify these constraints.

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

### Static site

The `ongo-web` publication kind is the **publish marker**: an `ongo-web`
entry's key = the target note/publication id (or its key), title = the
display/nav label, and an optional notes body = a section heading override.
**Nothing appears on the site unless an `ongo-web` entry references it**, so
the published surface is always explicit and queryable. Mark a note for
publish with the idempotent ensure-pubkind-then-add pattern from Startup
step 3:

```bash
${CLAUDE_SKILL_DIR}/bin/ken pubkind show ongo-web 2>/dev/null || ${CLAUDE_SKILL_DIR}/bin/ken pubkind add ongo-web "<description from startup>"
${CLAUDE_SKILL_DIR}/bin/ken add ongo-web -k "<note-id>" --title "<nav title>"
```

`${CLAUDE_SKILL_DIR}/bin/ongo-site` generates a self-contained static site
(default `./site/`) from the publish set: a top index grouping items by
their kendb topic graph, plus per-item HTML pages with an embedded CSS
theme and no external assets. It is stdlib-only, idempotent, and
deterministic — it **regenerates each self-improvement cycle** (see
Self-Improvement layer A). Source bodies resolve in order: a filesystem
`.md`/`.pdf`/`.tex` named by the publication key, a slug match under the
note roots, the kendb note body read via `ken show --json` (zomglings/ken#8,
**ken ≥ v3** — analogous to the ken#7 dependency in Startup step 3; the
supported CLI read path for ken's first-class `notes` table. On older ken
`ken show` is absent and resolution degrades gracefully to a compatibility
fallback that reads the same first-class `notes` data via direct SQL against
ken's internal schema from outside the tool), or finally the title. Cross-links between
published notes resolve to their pages; links to unpublished notes degrade
to plain text so unpublished content is never leaked.

**Regeneration is atomic.** The generator builds into a sibling temp dir
and then swaps it into place with a single `os.replace` (same filesystem,
true atomic rename). A request that arrives mid-regeneration always reads
a **complete** tree — either the previous site or the new one, never a
half-written mix — so `bin/ongo-serve` / `http.server` **needs no restart**
across a regeneration and there is no read-during-write window. The temp
dir is removed on any failure (no `.tmp`/`.old` is ever left behind), so a
crashed or interrupted build leaves the previously published site intact.

`${CLAUDE_SKILL_DIR}/bin/ongo-serve` serves the generated directory via
`python3 -m http.server` (default `0.0.0.0:80`). Hosting is
**self-served on the user's own server** (see `deploy/README.md` and the
`deploy/ongo-site.service` systemd template). The user points DNS
(`ongo.ergodic.xyz`) at their server **manually** — the skill must never
perform DNS changes or deploys.
