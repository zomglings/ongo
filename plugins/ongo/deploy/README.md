# ongo-site — publishing, generating, serving

`ongo site build` turns the **kendb publish set** into a self-contained static
website with access resolved per resource. Research notes, experiments,
results, and artifacts require an
explicit `ongo-web` marker. Daily `ongo-digest` publications are the one
automatic collection.

## 1. Mark content for publish

The `ongo-web` publication kind is the publish marker:

| field   | meaning                                             |
|---------|-----------------------------------------------------|
| `key`   | the target publication's id (or its key)            |
| `title` | the display title / nav label on the site           |

Mark an existing note for publish (the exact command):

```bash
ken add ongo-web -k <note-id> --title "<nav title>"
```

For example, to publish the note with id `66f290ec-...`:

```bash
ken add ongo-web -k 66f290ec-4aa6-463f-8779-6a06a9c428ac \
  --title "AI in Poetry and Literary Arts"
```

`-k` accepts either the publication **id** or its **key** (slug, path, URL).
To unpublish, delete the `ongo-web` marker (not the source note):

```bash
ken list --kind ongo-web                 # find the marker publication id
ongo ken delete pub <marker-id>          # removes only that marker
```

Do not delete by key to unpublish: a marker may intentionally share its key
with the source publication, and key-based deletion would select both.

Items appear in one reverse-chronological index. The marker title supplies the
navigation label.

Experiment roots with a marker also appear on the dedicated **Experiments**
tab, where their reviewed protocol and canonical manifest are rendered.
Attempts, results, and artifacts do not inherit the root marker; an artifact
must have its own `ongo-web` marker to be published.

## 2. Generate the site

```bash
ongo site build                        # writes ./site/ (deterministic)
ongo site build --out /opt/ongo/site   # custom output directory
ongo site build --ken /path/to/ken --db /path/to/ken.db
```

Public-only generation is stdlib-only and deterministic. Every build is
idempotent in meaning and rewrites the output directory cleanly, so it is safe
to run every self-improvement cycle. Source bodies are
resolved from (in order) a filesystem `.md`/`.pdf`/`.tex` named by the
publication key, a slug match under the note roots, the kendb note body via
`ken show --json` (the supported **Ken v3** read path), or finally the title.
Unresolvable references are skipped with a
warning in `site/build.log` (the build never crashes). Cross-links between published
notes resolve to their generated pages; links to **unpublished** notes
degrade to plain text so unpublished content is never leaked.
Presentation-oriented raw HTML is sanitized through a shared allowlist in
public and mixed builds; executable elements, event handlers, inline styles,
and unsafe URLs are removed.
Remote HTTP(S) images remain available in public resources but are stripped
from protected resources to avoid access-dependent requests to third parties.
Use relative or embedded data images for protected content.

When any published resource has an effective access key, the generator uses
the pinned `cryptography` dependency installed by `ongo setup` and emits a
mixed static client. Resources without keys remain plaintext; resources with
keys are AES-256-GCM encrypted independently for every assigned key. Such
builds use fresh nonces and therefore are not byte-deterministic. Configure
keys with `ongo key create`, `ongo key grant`, and `ongo key export`; see the
repository README for scope semantics. The administrator keyring must never be
placed inside the generated site directory or hard-linked. A protected note
used as a digest body also requires the digest to have a non-broader compatible
key set; incompatible derived-content policy fails the build closed.

**Regeneration is staged and recoverable.** `ongo site build` prepares a
complete tree and reserves a uniquely named backup in the output's parent,
moves the previous tree aside, then renames the new tree into place. A failed
second rename restores the previous tree. The server never sees a half-written
tree, although a request in the tiny interval between the two renames may
receive a transient miss. The running `ongo site serve` process does not need
to be restarted.

## 3. Serve the site

```bash
ongo site serve                                   # ./site on 0.0.0.0:80
ongo site serve --dir /opt/ongo/site --port 80
```

For production, run it under systemd:

- Install the templated unit `deploy/ongo-site.service` (edit paths/user
  first). It binds `ongo site serve` directly to `0.0.0.0:80`.
- **Port 80 is privileged (<1024).** The unit grants the unprivileged
  `ongo` user `CAP_NET_BIND_SERVICE` (via `AmbientCapabilities`, which
  works even with `NoNewPrivileges=true` because systemd applies ambient
  capabilities before exec) — so the service binds :80 **without running
  as full root**. No reverse proxy is required for plain HTTP.

A reverse proxy is optional for a public-only site. It is **required for a site
containing protected resources**, because browser Web Crypto requires HTTPS
(localhost is the development exception). Use it if you want HTTPS
on :443. If so, terminate TLS in nginx/caddy and proxy to the `ongo site serve`
backend (point it at a high local port instead of :80 in that case).

nginx TLS example (optional — `ongo site serve` on a local port, proxy adds 443):

```nginx
server {
    listen 443 ssl;
    server_name ongo.ergodic.xyz;
    # ssl_certificate / ssl_certificate_key ...
    location / {
        proxy_pass http://127.0.0.1:80;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

Caddy equivalent (`Caddyfile`, auto-TLS):

```
ongo.ergodic.xyz {
    reverse_proxy 127.0.0.1:80
}
```

## 4. DNS — manual, user-owned step

Hosting is **self-served on the user's own server**. Pointing the domain at
that server is a **manual step the user performs** — the ongo skill never
performs DNS changes or deploys, and contains no DigitalOcean (or other
registrar) credentials.

To go live, the user:

1. Generates the site (`ongo site build`) and serves it (`ongo site serve`,
   ideally via the systemd unit + reverse proxy above).
2. Manually creates a DNS **A record** for `ongo.ergodic.xyz` pointing at
   the server's public IP, at their DNS provider.
3. (Optional) Obtains a TLS certificate (e.g. caddy auto-HTTPS, or certbot
   for nginx).

That is the entire deployment boundary: ongo generates and can self-serve;
the user owns DNS and the server.

## Auto-regeneration (optional)

`ongo-site-regen.service` + `ongo-site-regen.timer` regenerate the site every 15 min so new kendb `ongo-web` markers and topic/relationship edges publish automatically. Install both unit files (see the service file header), then `systemctl enable --now ongo-site-regen.timer`. The staged replacement means `ongo-site.service` needs no restart.

For protected resources, the regeneration user must be able to read the admin
keyring under `ONGO_DATA_DIR` (default `site-keys.json`, mode `0600`). The
serving process needs only the generated directory and should not receive the
keyring or Ken database. Prefer building on a trusted machine and copying only
the completed static tree to the public host. The public tree still reveals
resource counts, collection membership, ordering, stable opaque IDs,
ciphertext sizes, and rebuild timing; protected semantic fields remain inside
the ciphertext envelopes.

Version 0.4.0 replaces the former skill-local `ongo-site` and `ongo-serve`
paths. Existing installed systemd units must be replaced manually. This command
transition does not migrate or alter the Ken database.
