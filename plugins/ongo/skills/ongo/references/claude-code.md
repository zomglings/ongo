# Claude Code adapter

Use this adapter only when Claude Code exposes `CronCreate` and `CronDelete`.
The shared `SKILL.md` remains authoritative for polling, cursor advancement,
experiments, message identity, and safety.

## Contents

- [Create the loop](#create-the-loop)
- [Upgrade a legacy loop](#upgrade-a-legacy-loop)
- [Reconcile and renew](#reconcile-and-renew)
- [Change cadence safely](#change-cadence-safely)
- [Delegate research](#delegate-research)
- [Shut down](#shut-down)

## Create the loop

Compute a cron expression for the requested interval with a small minute offset
to avoid common boundaries. For intervals dividing 60, enumerate the offset
minutes. For example, 30 minutes uses `7,37 * * * *`; 15 minutes uses
`7,22,37,52 * * * *`.

Create a recurring, durable CronCreate job. Its prompt must be self-contained:

1. Identify the installed Ongo skill and its absolute `STATE_PATH`.
2. Instruct the fresh context to read the entire Ongo skill and this adapter.
3. Instruct it to run exactly one shared tick, including the poll status branch,
   longest-handled-prefix cursor rule, scheduled maintenance, and cadence rule.
4. Include shutdown handling and the safe scheduler-swap ordering below.

After CronCreate returns, atomically update state with the new job ID, the
current epoch in `scheduler.created`, `host: "claude"`, and the normal interval.
Do not leave a null scheduler ID after reporting startup success.

Claude Code durable cron jobs expire after seven days. Every tick therefore
checks whether `scheduler.created` is older than three days and renews before
expiry.

## Upgrade a legacy loop

Ongo setup migrates a 0.5.x `/tmp/ongo_state.json` only when the new data-dir
state does not exist. It preserves the cursor, channel, cadence, current and
previous cron IDs, writes the nested state atomically, and replaces the legacy
path with a tombstone. Never initialize a second state or ordinary startup cron
after `state_migration.status` reports `migrated`.

When `scheduler.needs_prompt_upgrade` is true, replace the legacy cron using the
same create-before-delete safety boundary:

1. Capture both legacy `scheduler.id` and `scheduler.previous_id`.
2. CronCreate one replacement whose prompt names the new absolute `STATE_PATH`.
   Preserve fast mode; otherwise use `scheduler.normal_cron` when present or
   derive the cadence from `normal_interval_minutes`.
3. Atomically record the replacement ID and creation time, clear
   `needs_prompt_upgrade`, and set `speaker_user_id` to the authenticated Slack
   user ID.
4. CronDelete both captured legacy IDs when present and distinct. Clear
   `previous_id` only after the deletions succeed.
5. List jobs and delete any remaining prompt beginning exactly
   `Run one ongo research agent tick.`, regardless of which state path it names.

The tombstone prevents the legacy prompt from operating on stale state during
the bounded replacement window. Do not delete the old job before its
replacement exists.

## Reconcile and renew

At the start of every tick, inspect `scheduler.previous_id`. If it is non-null,
different from `scheduler.id`, and still exists, delete it. Clear
`previous_id` only after successful deletion.

Use this create-before-delete sequence for renewal and every cadence change:

1. CronCreate the replacement with the complete tick prompt, `recurring: true`,
   and `durable: true`.
2. Atomically write state with the replacement ID, current creation epoch, and
   the old ID in `previous_id`. Apply the requested mode and counter in the same
   write.
3. CronDelete the old ID.
4. On success, clear `previous_id`. On failure, leave it for the next tick to
   reconcile.
5. Run Ongo setup before recording an `ongo-cron-reset` publication and log the
   old ID, new ID, and reason.

Never delete the current job before its replacement exists. A crash after
create-before-delete leaves a recoverable duplicate; delete-before-create can
leave no job capable of recovery.

## Change cadence safely

When a successful poll returns user messages:

- Reset `fast_idle_polls` to zero.
- If mode is normal, use the safe swap to enter fast mode with a one-minute
  cron expression.
- If already fast, do not churn the scheduler.

For each successful empty fast poll, increment `fast_idle_polls`. After five,
use the safe swap to restore the saved normal cron and reset the counter.

On a Slack `ratelimited` error, leave the cursor unchanged. If mode is fast,
immediately use the safe swap to restore the normal cadence. Do not count the
failed poll as an idle poll and do not run expansion.

Every successful swap creates a fresh seven-day job and resets
`scheduler.created`; it therefore also counts as renewal.

## Delegate research

Use Claude Code's Agent tool with background execution for independent,
disjoint research units. Prefer the most capable available model without
hard-coding a model that may no longer exist.

Before dispatch, run `scripts/available-memory-mib`. When it prints an integer:

- At least 1024 MiB: normal delegation.
- 512–1023 MiB: skip passive expansion; allow user-requested work with a lighter
  available model.
- Below 512 MiB: defer all new delegation for this tick.

When the probe prints `unknown`, do not invent a number. Allow one delegated
unit and rely on the host concurrency limit.

After Agent returns an ID, post the spawn announcement before launching another
agent. Use SendMessage for later instructions targeting that agent's artifact.
Do not wait for independent background work before completing loop bookkeeping.

## Shut down

On an explicit shutdown message:

1. Post the Ongo-prefixed shutdown notice.
2. Read the current and previous scheduler IDs from durable state.
3. CronDelete both when present.
4. List scheduled jobs and delete any orphan whose prompt begins
   `Run one ongo research agent tick.`, including legacy jobs that name
   `/tmp/ongo_state.json` instead of the current state path.
5. Verify no matching job remains. Keep state and Ken data intact.
