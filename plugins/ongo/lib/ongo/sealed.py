"""Protected static-site projection and AES-GCM resource envelopes."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import shutil
import sys
from pathlib import Path

from .access import (
    base64url_decode,
    base64url_encode,
    load_keyring,
)
from .errors import OngoError


SCHEMA_VERSION = 1
AAD_PREFIX = "ongo-sealed-v1:"


SEALED_CSS = """
button.keys{font-family:var(--sans);cursor:pointer;color:var(--muted);
background:var(--surface);border:1px solid var(--border);border-radius:999px;
padding:.42rem .85rem;font-size:.8rem;font-weight:600}
button.keys:hover{color:var(--accent);border-color:var(--accent)}
.keyring-panel{background:var(--surface);border:1px solid var(--border);
border-radius:10px;padding:1rem 1.2rem;margin:-1rem 0 2rem;box-shadow:var(--shadow)}
.keyring-panel h2{font-size:1.05rem;margin:.1rem 0 .4rem;border:0;padding:0}
.keyring-panel .key-help{font-size:.85rem;color:var(--muted);margin:.3rem 0 1rem}
.key-form{display:grid;grid-template-columns:1fr 2fr auto;gap:.6rem;align-items:end}
.key-form label{font-size:.75rem;text-transform:uppercase;letter-spacing:.04em;
color:var(--muted);font-weight:700}
.key-form input{display:block;width:100%;margin-top:.25rem;padding:.55rem .65rem;
border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg);
font-family:var(--sans)}
.key-form button,.key-remove{padding:.55rem .8rem;border-radius:6px;
border:1px solid var(--border);background:var(--surface);color:var(--fg);cursor:pointer}
.key-form button:hover,.key-remove:hover{border-color:var(--accent);color:var(--accent)}
#key-message{font-size:.82rem;color:var(--muted);min-height:1.3rem}
#key-list{margin-top:.7rem}.key-row{display:flex;justify-content:space-between;
align-items:center;padding:.45rem 0;border-top:1px solid var(--border-soft)}
.key-details{display:flex;gap:.7rem;align-items:center}.key-details code{font-size:.72rem}
.key-remove{font-size:.72rem;padding:.3rem .55rem}.key-empty{font-size:.85rem;color:var(--muted)}
.locked-resource{padding:.8rem .25rem;color:var(--muted);font-style:italic}
.locked-resource::before{content:"🔒";font-style:normal;margin-right:.55rem}
.error-resource{color:#a33}.resource-main{display:flex;flex:1;flex-wrap:wrap;
align-items:center;gap:.5rem}.key-badges,.resource-tags{display:inline-flex;gap:.35rem;
flex-wrap:wrap}.key-badge,.resource-tag{font-family:var(--sans);font-size:.67rem;
line-height:1.2;border-radius:999px;padding:.2rem .48rem;background:var(--code-bg);
color:var(--muted);border:1px solid var(--border-soft)}
.key-badge::before{content:"🔑 ";font-size:.62rem}.resource-tag::before{content:"#"}
.item-access{display:flex;gap:.5rem;flex-wrap:wrap;margin:0 0 1.3rem}
@media(max-width:700px){.key-form{grid-template-columns:1fr}.key-form button{width:100%}}
"""


def canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def require_aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as error:
        raise OngoError(
            "protected site builds require the pinned cryptography package; run `ongo setup`",
            code="cryptography-missing",
            exit_code=3,
        ) from error
    return AESGCM


def public_resource_id(publication_id):
    value = f"ongo-sealed-v1:{publication_id}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:32]


def collection_for(item):
    if item.get("is_experiment"):
        return "experiment"
    if item.get("is_digest"):
        return "digest"
    return "article"


def tags_for(view, publication_id):
    record = view.show(publication_id) or {}
    tags = set()
    for relationship in record.get("relationships", []):
        if relationship.get("role") != "subject" or relationship.get("relkind") not in {
            "related-to",
            "cites",
            "derives-from",
        }:
            continue
        related = view.publication(relationship.get("publication"))
        if related is None:
            continue
        if related.get("kind") == "topic" or related.get("kind") == "ongo-arxiv-topic":
            title = (related.get("title") or "").strip()
            if title:
                tags.add(title)
    return sorted(tags, key=str.casefold)


def applicable_descriptor_ids(view, item, descriptors, all_descriptor_ids):
    record = view.show(item["pub_id"]) or {}
    descriptor_by_record = {record["id"]: metadata for record, metadata in descriptors}
    applicable = set(all_descriptor_ids)
    for relationship in record.get("relationships", []):
        if (
            relationship.get("role") == "subject"
            and relationship.get("relkind") == "ongo-readable-by"
        ):
            descriptor_id = relationship.get("publication")
            if descriptor_id not in descriptor_by_record:
                raise OngoError(
                    "ongo-readable-by points to a non-key publication",
                    code="invalid-access-policy",
                    exit_code=3,
                    details={"publication_id": item["pub_id"]},
                )
            applicable.add(descriptor_id)
    return sorted(applicable)


def access_material(view, item, descriptors, local_by_id, all_descriptor_ids):
    descriptor_by_record = {record["id"]: metadata for record, metadata in descriptors}
    result = []
    for descriptor_id in applicable_descriptor_ids(
        view, item, descriptors, all_descriptor_ids
    ):
        metadata = descriptor_by_record[descriptor_id]
        local = local_by_id.get(metadata["key_id"])
        if local is None:
            raise OngoError(
                "assigned access-key material is missing from the admin keyring",
                code="access-key-material-missing",
                exit_code=3,
                details={"key_id": metadata["key_id"]},
            )
        result.append(base64url_decode(local["secret"]))
    return result


def encrypt_payload(resource_id, payload, secrets):
    AESGCM = require_aesgcm()
    cleartext = canonical_json(payload).encode("utf-8")
    aad = (AAD_PREFIX + resource_id).encode("utf-8")
    variants = []
    for secret in secrets:
        nonce = os.urandom(12)
        ciphertext = AESGCM(secret).encrypt(nonce, cleartext, aad)
        variants.append(
            {
                "nonce": base64url_encode(nonce),
                "ciphertext": base64url_encode(ciphertext),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "resource_id": resource_id,
        "collection": payload["collection"],
        "variants": variants,
    }


def safe_markdown_html(
    raw, display, resolver, katex_ok, *, allow_remote_images=True
):
    from .site import extract_math, markdown_to_html, reinsert_math, render_toc

    if katex_ok:
        raw, math_map = extract_math(raw)
    else:
        math_map = {}
    html_body, toc = markdown_to_html(
        raw,
        resolver,
        collect_toc=True,
        allow_raw_html=True,
        allow_remote_images=allow_remote_images,
    )
    if math_map:
        html_body = reinsert_math(html_body, math_map)
    if "<h1" not in html_body:
        html_body = (
            '<h1 class="page-title" id="title">'
            + html.escape(display)
            + "</h1>\n"
            + html_body
        )
    toc_for_nav = [
        (level, anchor_id, text)
        for level, anchor_id, text in toc
        if not (level == 1 and toc and toc[0] == (level, anchor_id, text))
    ] or toc
    toc_html = render_toc(toc_for_nav, "On this page")
    byline = '<p class="byline">A published Ongo resource</p>'
    article = byline + "\n" + (toc_html + "\n" if toc_html else "") + html_body
    return "<article>\n" + article + "\n</article>"


def resource_payload(
    view, item, resource_id, resolver, katex_ok, *, allow_remote_images=True
):
    source = item["source"]
    common = {
        "schema_version": SCHEMA_VERSION,
        "resource_id": resource_id,
        "collection": collection_for(item),
        "title": item["display"],
        "date": item["date_label"],
        "tags": tags_for(view, item["pub_id"]),
    }
    if source["kind"] == "pdf":
        path = Path(source["path"])
        try:
            data = path.read_bytes()
        except OSError as error:
            raise OngoError(
                "could not read a protected PDF",
                code="protected-source-read-failed",
                exit_code=3,
                details={"publication_id": item["pub_id"], "error": str(error)},
            ) from error
        return {
            **common,
            "format": "pdf",
            "filename": path.name,
            "data_base64": base64.b64encode(data).decode("ascii"),
        }
    return {
        **common,
        "format": "html",
        "html": safe_markdown_html(
            source["body"],
            item["display"],
            resolver,
            katex_ok,
            allow_remote_images=allow_remote_images,
        ),
    }


def mixed_shell(site_title, *, page, asset_prefix="", resource_id="", katex=False):
    katex_assets = ""
    if katex:
        katex_assets = (
            f'<link rel="stylesheet" href="{asset_prefix}assets/katex/katex.min.css">\n'
            f'<script defer src="{asset_prefix}assets/katex/katex.min.js"></script>\n'
            f'<script defer src="{asset_prefix}assets/ongo-katex.js"></script>'
        )
    title = site_title
    lede = {
        "index": "Research notes",
        "experiments": "Experiments",
        "digests": "Digests",
        "item": "Research resource",
    }[page]
    resource_attribute = html.escape(resource_id, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="generator" content="ongo site build">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; img-src 'self' blob: data: http: https:; frame-src blob:; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="{asset_prefix}assets/ongo.css">
{katex_assets}
<script type="module" src="{asset_prefix}assets/ongo-sealed.js"></script>
</head>
<body data-page="{page}" data-resource="{resource_attribute}" data-asset-prefix="{asset_prefix}">
<div class="wrap">
<header class="site">
<div class="brand">
<a class="home" href="{asset_prefix}index.html">{html.escape(site_title)}</a>
<div class="tag">Research notes</div>
</div>
<nav class="top">
<a href="{asset_prefix}index.html">Index</a>
<a href="{asset_prefix}experiments.html">Experiments</a>
<a href="{asset_prefix}digests.html">Digests</a>
<button id="keyring-toggle" class="keys" type="button" aria-expanded="false">Keys</button>
<button id="rand-article" class="rand" type="button" aria-label="Go to a random accessible resource">&#127922;</button>
<button id="theme-toggle" class="theme" type="button" aria-label="Toggle color theme">&#9728;</button>
</nav>
</header>
<section id="keyring-panel" class="keyring-panel" hidden>
<h2>Browser keyring</h2>
<p class="key-help">Keys remain in this browser's local storage and are never sent to the static host.</p>
<form id="key-form" class="key-form">
<label>Local label<input id="key-label" autocomplete="off" required></label>
<label>Ongo access key<input id="key-capability" type="password" autocomplete="off" spellcheck="false" required></label>
<button type="submit">Register key</button>
</form>
<p id="key-message" aria-live="polite"></p>
<div id="key-list"></div>
</section>
<p class="lede">{lede}</p>
<h1 class="page-title">{lede}</h1>
<main id="ongo-content"><p class="intro">Loading encrypted resources…</p></main>
<footer class="site">
<p>Generated by <strong>ongo site build</strong>. Public resources are readable immediately; protected resources are AES-256-GCM encrypted and decrypted locally with registered access keys.</p>
</footer>
</div>
</body>
</html>
"""


def install_tree(work_dir, out_dir, old_dir):
    # ``build`` reserves a unique, same-filesystem backup directory. Remove
    # only that empty reservation; never recursively delete a guessed path.
    if os.path.islink(old_dir) or not os.path.isdir(old_dir):
        raise OngoError(
            "reserved site backup path is invalid",
            code="site-backup-invalid",
            exit_code=3,
            details={"backup": str(old_dir)},
        )
    try:
        os.rmdir(old_dir)
    except OSError as error:
        raise OngoError(
            "reserved site backup path is not empty",
            code="site-backup-invalid",
            exit_code=3,
            details={"backup": str(old_dir), "error": str(error)},
        ) from error
    moved_previous = False
    try:
        if os.path.lexists(out_dir):
            os.replace(out_dir, old_dir)
            moved_previous = True
        os.replace(work_dir, out_dir)
    except Exception:
        if (
            moved_previous
            and not os.path.lexists(out_dir)
            and os.path.lexists(old_dir)
        ):
            os.replace(old_dir, out_dir)
        raise
    try:
        if os.path.islink(old_dir):
            os.unlink(old_dir)
        elif os.path.isdir(old_dir):
            shutil.rmtree(old_dir)
        elif os.path.lexists(old_dir):
            os.unlink(old_dir)
    except OSError:
        # The installed site is complete. A uniquely named backup is safer to
        # leave for manual recovery than to turn cleanup into a failed build.
        pass


def validate_access_relationships(view, descriptors, global_order, *, strict=True):
    """Fail closed on malformed access edges touching published resources."""
    descriptor_ids = {record["id"] for record, _metadata in descriptors}
    relevant = [
        (view.show(item["pub_id"], strict=strict) or {}, "resource")
        for item in global_order
    ] + [(record, "descriptor") for record, _metadata in descriptors]
    for record, endpoint_role in relevant:
        for relationship in record.get("relationships", []):
            if relationship.get("relkind") != "ongo-readable-by":
                continue
            related_id = relationship.get("publication")
            actual_role = relationship.get("role")
            related_publication = view.publication(related_id)
            valid = (
                endpoint_role == "resource"
                and actual_role == "subject"
                and related_id in descriptor_ids
            ) or (
                endpoint_role == "descriptor"
                and actual_role == "object"
                and related_id not in descriptor_ids
                and related_publication is not None
                and related_publication.get("kind") != "ongo-web"
            )
            if not valid:
                raise OngoError(
                    "ongo-readable-by must connect a resource subject to an access-key object",
                    code="invalid-access-policy",
                    exit_code=3,
                    details={
                        "publication_id": record.get("id"),
                        "endpoint_role": endpoint_role,
                        "relationship_role": actual_role,
                        "related_publication": related_id,
                    },
                )


def validate_derived_content_access(
    view, descriptors, global_order, all_descriptor_ids
):
    """Ensure a derived resource never has more readers than its sources."""
    for item in global_order:
        source_ids = item.get("content_source_ids", [])
        if not source_ids:
            continue
        derived_access = set(
            applicable_descriptor_ids(
                view, item, descriptors, all_descriptor_ids
            )
        )
        for source_id in source_ids:
            source_access = set(
                applicable_descriptor_ids(
                    view,
                    {"pub_id": source_id},
                    descriptors,
                    all_descriptor_ids,
                )
            )
            if source_access and (
                not derived_access or not derived_access.issubset(source_access)
            ):
                raise OngoError(
                    "derived content has broader access than its protected source",
                    code="derived-access-policy-conflict",
                    exit_code=3,
                    details={
                        "publication_id": item["pub_id"],
                        "source_publication_id": source_id,
                    },
                )


def mixed_site_required(view, global_order):
    descriptors = access_key_records_for_view(view)
    all_descriptor_ids = {
        record["id"]
        for record, metadata in descriptors
        if metadata["scope"] == "all"
    }
    # Validate dangling or reversed access edges even when no descriptor was
    # registered. Best-effort reads preserve the legacy title fallback only in
    # that descriptor-free case; once a key exists, policy reads fail closed.
    if not descriptors:
        validate_access_relationships(
            view, descriptors, global_order, strict=False
        )
        validate_derived_content_access(
            view, descriptors, global_order, all_descriptor_ids
        )
        return False
    validate_access_relationships(view, descriptors, global_order)
    validate_derived_content_access(
        view, descriptors, global_order, all_descriptor_ids
    )
    return any(
        applicable_descriptor_ids(view, item, descriptors, all_descriptor_ids)
        for item in global_order
    )


def build_mixed_site(
    *, args, out_dir, old_dir, work_dir, log, view, items, global_order, ken, db_path
):
    del items, ken, db_path
    from .site import CSS, resolve_publication_reference, run_qa, vendor_katex

    require_aesgcm()
    descriptors = access_key_records_for_view(view)
    validate_access_relationships(view, descriptors, global_order)
    keyring_path, keyring = load_keyring(getattr(args, "keyring", None))
    local_by_id = {entry["key_id"]: entry for entry in keyring["keys"]}
    all_descriptor_ids = {
        record["id"]
        for record, metadata in descriptors
        if metadata["scope"] == "all"
    }
    os.makedirs(os.path.join(work_dir, "assets", "sealed"), exist_ok=True)
    os.makedirs(os.path.join(work_dir, "items"), exist_ok=True)
    katex_ok = vendor_katex(work_dir, log)

    public_ids = {
        item["pub_id"]: public_resource_id(item["pub_id"])
        for item in global_order
    }

    def resolver(target):
        publication = resolve_publication_reference(view, target)
        if publication is not None and publication["id"] in public_ids:
            return ("../items/" + public_ids[publication["id"]] + ".html", False)
        if publication is not None:
            return ("", True)
        if re.match(r"^[a-z][a-z0-9+.-]*://", target) or target.startswith("mailto:"):
            return (target, False)
        if target.startswith("#") or target.startswith("/"):
            return (target, False)
        return (target, False)

    manifest_resources = []
    protected_count = 0
    for item in global_order:
        resource_id = public_ids[item["pub_id"]]
        secrets = access_material(
            view, item, descriptors, local_by_id, all_descriptor_ids
        )
        payload = resource_payload(
            view,
            item,
            resource_id,
            resolver,
            katex_ok,
            allow_remote_images=not bool(secrets),
        )
        if secrets:
            envelope = encrypt_payload(resource_id, payload, secrets)
            protected_count += 1
        else:
            envelope = {
                "schema_version": SCHEMA_VERSION,
                "resource_id": resource_id,
                "collection": payload["collection"],
                "public": payload,
            }
        envelope_path = Path(work_dir) / "assets" / "sealed" / f"{resource_id}.json"
        envelope_path.write_text(canonical_json(envelope) + "\n", encoding="utf-8")
        page_path = f"items/{resource_id}.html"
        manifest_resources.append(
            {
                "resource_id": resource_id,
                "collection": payload["collection"],
                "envelope": f"assets/sealed/{resource_id}.json",
                "page": page_path,
            }
        )
        (Path(work_dir) / page_path).write_text(
            mixed_shell(
                args.site_title,
                page="item",
                asset_prefix="../",
                resource_id=resource_id,
                katex=katex_ok,
            ),
            encoding="utf-8",
        )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "site_title": args.site_title,
        "resources": manifest_resources,
    }
    assets = Path(work_dir) / "assets"
    assets.joinpath("ongo-sealed.json").write_text(
        canonical_json(manifest) + "\n", encoding="utf-8"
    )
    assets.joinpath("ongo.css").write_text(CSS + SEALED_CSS, encoding="utf-8")
    source_js = Path(__file__).parent / "assets" / "sealed.js"
    shutil.copyfile(source_js, assets / "ongo-sealed.js")
    if katex_ok:
        from .site import KATEX_RUNTIME_JS

        assets.joinpath("ongo-katex.js").write_text(
            KATEX_RUNTIME_JS, encoding="utf-8"
        )
    for page in ("index", "experiments", "digests"):
        Path(work_dir, f"{page}.html").write_text(
            mixed_shell(args.site_title, page=page, katex=katex_ok),
            encoding="utf-8",
        )

    katex_scratch = Path(work_dir) / "_katex"
    if katex_scratch.is_dir():
        shutil.rmtree(katex_scratch, ignore_errors=True)

    log.append(f"site resources: {len(manifest_resources)}")
    log.append(f"protected resources: {protected_count}")
    log.append(f"public resources: {len(manifest_resources) - protected_count}")
    log.append(f"access key descriptors: {len(descriptors)}")
    log.append(f"admin keyring entries: {len(keyring['keys'])}")
    log.append(f"admin keyring: {keyring_path}")
    clean, total, defects, passed = run_qa(work_dir, log)
    public_log = (
        "ongo mixed-access static build\n"
        f"resources: {len(manifest_resources)}\n"
        f"protected: {protected_count}\n"
        f"public: {len(manifest_resources) - protected_count}\n"
        f"qa: {'PASS' if passed else 'FAIL'} ({clean}/{total} pages clean, {defects} defects)\n"
    )
    Path(work_dir, "build.log").write_text(public_log, encoding="utf-8")
    install_tree(work_dir, out_dir, old_dir)
    log_text = "\n".join(log) + "\n"
    sys.stdout.write(log_text)
    sys.stdout.write(
        "QA SELF-CHECK: %s — %d/%d pages clean, %d defect hits (see qa-report.txt)\n"
        % ("PASS" if passed else "FAIL", clean, total, defects)
    )
    return 0


def access_key_records_for_view(view):
    """Read key descriptors through the same Ken client backing the site view."""
    from .access import parse_key_metadata

    descriptors = []
    seen = set()
    for row in view.rows:
        if row.get("kind") != "ongo-access-key":
            continue
        record = view.show(row["id"])
        metadata = parse_key_metadata(record)
        if metadata["key_id"] in seen:
            raise OngoError(
                "duplicate access-key descriptors violate the Ongo protocol",
                code="duplicate-access-key",
                exit_code=4,
                details={"key_id": metadata["key_id"]},
            )
        seen.add(metadata["key_id"])
        descriptors.append((record, metadata))
    return descriptors
