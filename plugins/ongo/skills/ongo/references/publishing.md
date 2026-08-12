# Ken management and publishing

Read this reference before deleting Ken records, publishing Ongo content,
managing access keys, or serving a generated site.

## Contents

- [Delete records](#delete-records)
- [Select published resources](#select-published-resources)
- [Build and serve](#build-and-serve)
- [Protect resources](#protect-resources)
- [Security boundaries](#security-boundaries)

## Delete records

Ken has no native delete command. Ongo provides a constrained stopgap:

```bash
"$ONGO" ken delete pub <id>
"$ONGO" ken delete pub --key <key>
"$ONGO" ken delete pub --kind <kind>
"$ONGO" ken delete rel <id>
"$ONGO" ken delete --dry-run ...
```

Always preview deletion by kind and other bulk selectors. The command removes
restricting relationships before a publication, but that transactionality does
not replace user authorization. Never use this path for experiment publication
kinds.

## Select published resources

An `ongo-web` publication is the explicit publish marker. Its key identifies the
target publication or note and its title supplies the display label:

```bash
"$KEN" add ongo-web -k "<publication-id-or-key>" --title "<label>"
```

Notes, experiments, results, and artifacts are private by default. An
experiment appears only when its root has a marker. Attempts and artifacts do
not inherit that marker; each artifact requires its own marker. Daily
`ongo-digest` publications are the only automatic site collection.

Experiment notes render only within their published experiment and cannot be
published independently.

## Build and serve

Build the self-contained static site:

```bash
"$ONGO" site build --out ./site
"$ONGO" site serve --dir ./site --host 127.0.0.1 --port 8000
```

Public-only builds are deterministic. A build containing protected resources
uses fresh AES-GCM nonces and is intentionally not byte-deterministic while
remaining equivalent in meaning. The generator stages a complete sibling tree,
backs up the prior tree, and restores it if the final rename fails.

Source bodies resolve from a keyed `.md`, `.pdf`, or `.tex` path; a slug match
under configured note roots; the Ken note body; or the title. Links to
unpublished resources degrade to plain text. Generated protected pages remove
remote images so successful decryption cannot reveal readership to an image
host; use relative or embedded images instead.

Hosting and DNS remain user-managed. Ongo may serve a local or explicitly
configured directory, but it never deploys or changes DNS.

## Protect resources

Protection belongs to each published resource. One generated site may contain
both public and protected notes or experiments. An `ongo-readable-by`
relationship authorizes one registered symmetric key for one resource.

Manage administrator keys only through the CLI:

```bash
"$ONGO" key create --label "Work" --scope all
"$ONGO" key create --label "Snapshot" --scope published
"$ONGO" key create --label "Project" --scope empty
"$ONGO" key import --label "Imported" --scope empty
"$ONGO" key grant <key-id> <publication-id>
"$ONGO" key list
"$ONGO" key export <key-id>
```

Feed an imported capability through standard input or the hidden interactive
prompt. Never place literal capability material in command arguments or shell
history.

Scopes mean:

- `all`: every current and future published resource unless overridden.
- `published`: the resources published at key creation time.
- `empty`: no resource until explicitly granted.

Several grants create several ciphertext variants under one resource identity.
Resources without an effective key remain public.

Digest and experiment-note bodies are derived content. A digest must receive a
non-broader subset of its protected source notes' keys. The builder rejects an
incompatible public or broader policy rather than copying protected text into
it. The same non-broadening rule applies to experiment notes, actors, and topic
labels rendered through an experiment.

## Security boundaries

The administrator keyring defaults to Ongo's writable data directory and uses
mode `0600`; Ken stores only public descriptors and relationships. Symlink
aliases resolve to the canonical keyring. Hard-linked keyrings are rejected
because atomic replacement cannot update every link safely.

Build protected content only on a trusted machine and deploy only the generated
site tree. Production protected sites require HTTPS for Web Crypto. Static
capabilities cannot enforce reliable expiry or prevent an authorized reader
from retaining plaintext. A compromised host can replace the client JavaScript
and steal keys entered by a reader.

The generated protected manifest still exposes resource count, collection,
ordering, stable opaque IDs, ciphertext sizes, and rebuild timing. It does not
expose protected titles, dates, tags, bodies, or key labels.

Never put a capability in Ken, Slack, a URL, generated output, or an Ongo log.
