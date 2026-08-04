# Deterministic Experiment Management Pilot Contract

Status: implemented pilot contract for Ongo 0.4.0

## Claim

Ongo's plugin-shipped CLI can keep a cooperative but fallible agent from
silently changing or incompletely executing an approved experiment protocol.
It freezes an explicit condition list, issues work in plan order, preserves
every attempt and artifact in Ken, and derives completion from stored coverage.

The pilot fails this claim if a supported workflow can silently omit a required
run, select an unplanned condition, reuse approval for changed plan content,
overwrite failed evidence, or make `ongo experiment verify` succeed without
the declared number of valid observations.

## Boundaries

- `/ongo:ongo` owns conversation, scientific judgment, and the human-readable
  protocol.
- `ongo experiment` owns validation, approval, assignment, execution state,
  artifact ingestion, budgets, and completion checks.
- Ken v3 is used unchanged. There is no Ken schema migration, separate
  database, lock, lease, TTL, or worker identity service.
- `ongo setup` downloads the released platform binary only after its pinned
  SHA-256 matches and registers both experiment kinds and legacy research kinds
  required by the consolidated commands.
- One driving controller operates serially. Authorization is an auditable
  cooperation protocol, not a security boundary against a malicious process
  with database access.
- Plans enumerate conditions explicitly. The user reviews Markdown or a local
  web view; the driving agent creates the JSON manifest on the user's behalf.
- Every artifact is a Ken publication. UTF-8 is stored directly and binary data
  is base64 encoded. Practical SQLite size limits are accepted during the
  pilot.

## Workflow

1. The driving agent writes a Markdown protocol and schema-v1 JSON manifest.
2. `ongo experiment create` validates, hashes, and atomically stores the
   experiment, plan, manifest, and conditions.
3. The user reviews `ongo experiment show --format markdown` or a rendered web
   view containing the authoritative condition matrix.
4. The user may create a time- and budget-bounded delegation. The driving agent
   approves an exact plan hash within it; zero-cost plans use the zero-cost
   policy.
5. `begin` chooses the next initial slot, or `run` executes eligible local argv
   conditions serially without a shell. Workers cannot select another
   condition.
6. `finish` atomically records a terminal result and every artifact. Invalid or
   failed observations remain visible and do not satisfy coverage.
7. Retries are explicit and budget checked. They never replace prior attempts.
8. `verify` succeeds only when every required run has a valid observation and
   there is no open or excess valid work.

Once the first attempt starts, the plan is frozen. A changed protocol is a new
successor experiment with its own approval.

The generated Ongo site has an Experiments tab. It lists only experiment roots
with an explicit `ongo-web` marker. Results and artifacts remain absent unless
separately marked. Each published experiment resource independently remains
public or is AES-GCM encrypted for its effective `ongo-readable-by` access
keys; protection never implicitly spreads from a root to an artifact.

## Pilot interfaces

```text
ongo setup
ongo doctor --json

ongo experiment create --document PLAN.md --manifest MANIFEST.json
ongo experiment show ID --format json|markdown
ongo experiment render ID --out DIR
ongo experiment status ID --json
ongo experiment delegate create ...
ongo experiment approve ID [--delegation ID] --actor LABEL
ongo experiment begin ID --worker LABEL
ongo experiment finish ATTEMPT --result RESULT.json [--artifact NAME=PATH ...]
ongo experiment cancel ATTEMPT --reason TEXT
ongo experiment retry ID --condition CONDITION --worker LABEL
ongo experiment run ID
ongo experiment verify ID --json
```

The CLI also consolidates existing behavior under `ongo slack poll`,
`ongo arxiv sweep`, `ongo site build`, `ongo site serve`, and
`ongo ken delete`.

## Evidence collected during use

Retain document and manifest hashes; delegation and approval records; expected
and reported actual cost; every condition, attempt, retry, and cancellation;
local argv, cwd, environment additions, timing, exit code, stdout, and stderr;
artifact media type, encoding, byte size, and SHA-256; and the final coverage
report with every discrepancy.

## Upgrade triggers

Change Ken or add another store only after observing one of these failures:

- multiple controllers genuinely need concurrent assignment;
- a crash cannot be resolved from the append-only attempt record;
- duplicate Ken keys occur despite the single-controller contract;
- required queries are too slow or ambiguous through Ken v3;
- binary artifacts make the Ken database operationally impractical;
- authorization must resist a malicious worker rather than audit a cooperative
  one;
- actual costs cannot be bounded well enough by declared expected cost.

Those observations determine whether the next addition is uniqueness,
optimistic concurrency, stronger identity, blob storage, or a new database.
They are not assumed requirements of this pilot.
