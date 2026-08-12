# Research expansion and maintenance

Use this reference for passive expansion, user-requested deep research, the
daily arXiv sweep, and the bounded maintenance cycle.

## Contents

- [Choose work](#choose-work)
- [Delegate one expansion](#delegate-one-expansion)
- [Run the arXiv sweep](#run-the-arxiv-sweep)
- [Maintain Ongo safely](#maintain-ongo-safely)

## Choose work

Read only the lightweight inputs first:

```bash
"$KEN" list --kind ongo-exploration
"$KEN" list --kind topic
```

Choose a topic randomly, weighted by explicit `ongo-exploration` directives.
Skip passive expansion when no topics exist, the poll failed, the host is in
fast mode, or the loop is idle by user request. Rotate the expansion style in
state among reference collection, deep analytical notes, and cross-topic
survey work.

Run at most one passive expansion per tick. User-requested independent work may
run concurrently only when the host adapter permits it and the write sets are
disjoint.

## Delegate one expansion

Use the host adapter's subagent mechanism. Give the agent:

- Its exact agent ID and assigned topic title and ID.
- The absolute Ken binary path and Slack channel.
- The configured `speaker_prefix` and required
  `[ongo, <agent-id>]` message marker.
- The writing-style block when configured.
- The following self-contextualization contract.

The delegated agent must:

1. Read topic, note, arXiv, web, and exploration publication titles from Ken.
2. Inspect existing material related to the assigned topic before searching.
3. Identify a concrete knowledge gap.
4. Search authoritative primary sources where possible.
5. Add references and detailed analytical notes, not link-only records.
6. Create `related-to`, `cites`, or `derives-from` relationships when supported
   by evidence.
7. Post optional progress and exactly one final Slack sign-off beginning with
   `<speaker_prefix>[ongo, <agent-id>] Done —`.

Use Ken commands for ordinary research records:

```bash
"$KEN" add <kind> -k <key> --title <title>
"$KEN" relate -s <subject-id> -o <object-id> -r <relationship-kind>
```

Do not use those commands for experiment publication kinds. Never let two
agents write the same note, publication key, or relationship operation.

Immediately after dispatch succeeds, the main loop announces:

```text
<speaker_prefix>[ongo] Spawning subagent <agent-id> for: <summary>
```

The loop then continues its bookkeeping without waiting. A dispatched agent
that fails must post a prefixed failure so the user can retry deliberately.

## Run the arXiv sweep

Users seed interests as `ongo-arxiv-topic` publications. The key is a short
slug; the title is an arXiv API search expression:

```bash
"$KEN" add ongo-arxiv-topic -k distributional-rl \
  --title 'all:"distributional reinforcement learning"'
```

When at least 24 hours have elapsed since `last_arxiv_daily`, the last Slack
poll succeeded, and mode is normal, run:

```bash
"$ONGO" arxiv sweep --channel "$CHANNEL"
```

Update `last_arxiv_daily` only after the command completes. Use `--dry-run` for
inspection and `--no-slack` when the caller wants database updates without a
digest message.

The sweep queries every active topic, uses a 26-hour default window, normalizes
arXiv IDs, and commits each paper, abstract, and topic relationship atomically.
Per-topic HTTP failures continue; the command fails when every topic errors or
Ken is unavailable. A non-empty run creates one `ongo-digest` publication and
posts a digest. Empty runs create no digest.

Do not infer topic weighting from `ongo-exploration`; arXiv topics and passive
expansion directives are separate. Retire a stale arXiv topic only after a
dry-run deletion preview and explicit authorization.

## Maintain Ongo safely

Run maintenance at most once per 24 hours unless explicitly requested. Keep it
bounded and observable:

1. Run `"$ONGO" doctor --json` and report dependency or database failures.
2. Inspect duplicates by key, URL, and normalized arXiv ID. Preview any deletion
   with `"$ONGO" ken delete --dry-run`; delete only with explicit authorization.
3. Add at most 20 clearly implied relationship gaps per cycle.
4. Create survey notes for well-populated topics and record topic centrality as
   analysis, not an unstated ranking policy.
5. Review stale exploration directives and report them; do not silently remove
   them.
6. Regenerate an explicitly configured static site with
   `"$ONGO" site build`. Never deploy or change DNS.
7. Record the maintenance plan and outcome as `ongo-self-improvement`
   publications after setup has ensured the kind exists.

Dependency checks are report-only. Do not upgrade clacks, Ken, cryptography, or
the plugin without explicit user authorization. Do not edit an installed plugin
cache. When operating from an authorized source checkout, propose source
changes as a reviewed diff and run the repository checks before applying or
publishing them.

Do not create GitHub issues or pull requests merely because maintenance found a
problem. Report the proposed external action and wait for authorization. Track
an authorized issue or pull request by URL in Ken on later cycles.
