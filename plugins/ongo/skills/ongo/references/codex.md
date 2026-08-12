# Codex adapter

Use this adapter in Codex in the ChatGPT desktop app when the scheduled-task or
automation capability is available. The shared `SKILL.md` remains authoritative
for polling, cursor advancement, experiments, message identity, and safety.

## Contents

- [Create the heartbeat](#create-the-heartbeat)
- [Run and update it](#run-and-update-it)
- [Delegate research](#delegate-research)
- [Permissions and recovery](#permissions-and-recovery)
- [Shut down](#shut-down)

## Create the heartbeat

Create a heartbeat automation attached to the current Codex task. Do not create
a standalone per-run task unless the user explicitly requests independent runs.
Use the automation tool exposed by the Codex app; never edit automation files or
emit raw scheduling directives.

Ask the tool for the user's interval, defaulting to 30 minutes. The durable
heartbeat prompt must:

1. Explicitly invoke `$ongo`.
2. Name the absolute `STATE_PATH` returned by Ongo setup.
3. Instruct the task to read the installed skill and this adapter completely.
4. Run exactly one shared tick and return to the same task.
5. Preserve the poll-status branch and longest-handled-prefix cursor rule.
6. Stop and delete the heartbeat only on an explicit shutdown message.

After creation, atomically store the returned automation ID, current epoch,
`host: "codex"`, mode, and normal interval. Codex heartbeats do not use Claude's
seven-day renewal or create-before-delete swap.

## Run and update it

At the start of each tick, confirm that the state ID identifies the current
heartbeat. If the automation cannot be viewed, report the mismatch rather than
creating a silent duplicate.

For activity-driven fast mode, update the existing heartbeat to a one-minute
interval and then atomically record `mode: "fast"`. For five successful empty
fast polls, update the same heartbeat back to the saved normal interval and
reset the counter. Never create a second heartbeat merely to change cadence.

If an update fails, retain the previous mode and schedule in state. On Slack
rate limiting, leave the cursor unchanged and request the normal interval when
currently fast. A failed poll is not an idle poll.

Use the automation tool's structured create, view, update, and delete operations.
Do not expose raw recurrence-rule strings to the user.

## Delegate research

Use Codex subagents only when the capability is available. Let agents inherit
the current model unless the user explicitly selected another supported model.
Respect the host concurrency limit and dispatch at most one passive expansion
unit per tick.

Enumerate each unit's write set before dispatch. Parallel units may share
read-only Ken queries but must not write the same publication key, file, state
path, or Slack response stream. Send follow-up instructions to the existing
writer for an in-flight artifact.

Include the exact subagent ID, Ken path, Slack channel, speaker prefix, and style
block in its prompt. Post the spawn announcement as soon as the subagent starts.
Require the agent to post its own prefixed final sign-off.

## Permissions and recovery

Local scheduled work requires the ChatGPT desktop app to be running and the
selected project to remain available. Ongo also needs network access, clacks
credentials, and write access to its data directory. Before creating the
heartbeat, verify that the scheduled execution environment grants those
capabilities without interactive approvals. If it does not, explain the exact
missing permission and do not claim the loop is active.

Prefer local execution in the intended project. Do not use an isolated worktree
when the Ongo state or requested artifacts must remain in the main working
directory.

If a controlling Codex skill requires a Slack identity prefix, store it in
`speaker_prefix`, prepend it to every Ongo message, and pass it explicitly to
`ongo slack poll --speaker-prefix`. This prevents Codex from reprocessing its
own messages without baking a user-specific identity into Ongo. For example,
the Rex skill requires the exact prefix `` `[rex]` `` followed by one space;
configure that literal wire text, including the backticks.

## Shut down

On an explicit shutdown message:

1. Post the Ongo-prefixed shutdown notice.
2. Delete the automation whose ID is stored in state.
3. View or list matching automations and verify no duplicate heartbeat targets
   the same Ongo state path.
4. Keep state and Ken data intact for inspection or later restart.
