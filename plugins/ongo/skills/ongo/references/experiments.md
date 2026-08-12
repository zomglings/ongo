# Deterministic experiments

Read this reference before planning, approving, executing, changing, or
declaring completion of an Ongo experiment. The `ongo experiment` CLI is the
sole authority for experiment state. Never write experiment publication kinds
through direct Ken, SQLite, or deletion commands.

## Contents

- [Plan and review](#plan-and-review)
- [Approve](#approve)
- [Execute](#execute)
- [Record notes](#record-notes)
- [Verify and change](#verify-and-change)

## Plan and review

Write a Markdown protocol containing the hypothesis, method, exact conditions,
repetitions, stopping rule, expected cost, required evidence, and exclusions.
Generate a JSON manifest on the user's behalf:

```json
{
  "schema_version": 1,
  "title": "<title>",
  "conditions": [
    {
      "id": "<condition-id>",
      "description": "<what runs>",
      "required_runs": 1,
      "expected_cost_usd": 0,
      "required_artifacts": [],
      "execution": {"mode": "manual"}
    }
  ]
}
```

For local execution, use an explicit argv array, cwd, string environment
additions, timeout, accepted exit codes, and declared output files. Never encode
a shell command string.

Register and review the canonical form:

```bash
"$ONGO" experiment create --document PLAN.md --manifest MANIFEST.json
"$ONGO" experiment show <experiment-id> --format markdown
```

The manifest, not the prose alone, is what executes. Once the first attempt
begins, the plan is frozen.

## Approve

Zero-cost plans may be approved without delegation. A costed plan requires an
explicit user grant containing a principal, evidence pointer, per-experiment
ceiling, optional cumulative ceiling, expiry, and allowed modes:

```bash
"$ONGO" experiment delegate create \
  --granted-by <principal> --evidence <pointer> \
  --max-per-experiment-usd <amount> [--max-total-usd <amount>] \
  --expires-at <ISO-8601> [--mode manual] [--mode local] \
  --experiment <experiment-id>
"$ONGO" experiment approve <experiment-id> \
  --delegation <delegation-id> --actor <driver-label>
```

Never invent or widen a grant. Keep delegation experiment-scoped; a successor
requires a fresh delegation. The approver and worker must be distinct. Exit
code 5 is an authorization or budget discrepancy: stop and show it to the user.

## Execute

For manual work, let the ledger assign the next condition:

```bash
"$ONGO" experiment begin <experiment-id> --worker <label>
```

Give that exact assignment to the worker. Do not self-select a condition. Submit
the result and every required artifact:

```bash
"$ONGO" experiment finish <attempt-id> --result RESULT.json \
  --artifact NAME=PATH
```

`RESULT.json` accepts only:

```json
{
  "schema_version": 1,
  "status": "completed",
  "valid_observation": true,
  "summary": "what happened, with key measurements"
}
```

Status may also be `failed` or `cancelled`. Put detail in the summary and
artifacts; unknown fields are rejected. For eligible local conditions,
`"$ONGO" experiment run <experiment-id>` executes argv conditions serially and
records complete outputs.

Cancel interruptions explicitly. Retry only after a deliberate decision:

```bash
"$ONGO" experiment cancel <attempt-id> --reason <text>
"$ONGO" experiment retry <experiment-id> --condition <condition-id> \
  --worker <label>
```

Failed or invalid observations never satisfy coverage.

## Record notes

Record discrepancies, probe results, deviations, and interpretation as
append-only Markdown:

```bash
"$ONGO" experiment note add <experiment-id> --actor <label> \
  --text <markdown> \
  [--condition <condition-id> | --attempt <attempt-id>]
"$ONGO" experiment note add <experiment-id> --actor <label> \
  --file NOTE.md \
  [--condition <condition-id> | --attempt <attempt-id>]
"$ONGO" experiment note list <experiment-id> --format markdown
```

Use `--topic` only for an existing, unambiguous Ken topic. Use
`--operation-key` when the caller has a retry-safe operation key. Notes are
documentary: they never change the frozen plan, approval, budget, result, or
coverage. If a deviation invalidates an observation, set
`valid_observation: false` in its result as well as documenting why.

## Verify and change

Always finish with:

```bash
"$ONGO" experiment verify <experiment-id> --json
```

Exit code 6 means coverage is incomplete. Report the stored discrepancy rather
than summarizing completion from memory.

Create any changed protocol with `--successor-of <old-id>`. Review and approve
the successor independently; never edit or reuse the frozen predecessor's
approval.
