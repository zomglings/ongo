#!/usr/bin/env python3
r"""`ongo site build` — generate a static site from a Ken publish set.

Research records appear only when an `ongo-web` publication references them;
daily `ongo-digest` publications are the one automatic collection.
An `ongo-web` entry is the *publish marker*:

    key   = the target publication's id  (or its key)
    title = the display title / nav label for the site
    notes = optional section/topic override (read from the kendb notes table)

The generator:
  1. Reads the publish set via `ken list --kind ongo-web`.
  2. Resolves each marker to a source publication and its body. Bodies come
     from (in order): a filesystem .md/.pdf/.tex named by the publication key,
     a slug -> ~/.local/share/ken/notes/<slug>.md or research-notes/**.md
     match, the kendb note body via `ken show --json` (zomglings/ken#8,
     ken >= v3 — the supported CLI read path for ken's first-class `notes`
     table), or finally the publication title.
  3. Emits the index as a SINGLE GLOBAL FLAT LIST of every published item
     (no topic grouping, no per-topic nav): reverse-chronological (newest
     first), ties broken alphabetically by title (A->Z). Each row shows the
     item's `created_at` (printed verbatim).
  4. Renders each article with a per-article table of contents built from
     its Markdown headings (anchor ids + links), auto-links bare http/https
     URLs (external links get target=_blank rel=noopener), and renders
     `$...$` / `$$...$$` / `\(...\)` / `\[...\]` math via KaTeX vendored
     LOCALLY into site/assets/katex/ (build may fetch once; runtime never).
  5. Emits a self-contained static directory (default ./site/) with an
     embedded CSS theme and no external runtime assets. Access is resolved per
     resource: unassigned resources are public and assigned resources are
     encrypted once for each applicable Ongo access key.

Public-only builds are stdlib-only and deterministic. Mixed-access builds use
the plugin-pinned cryptography package and fresh AES-GCM nonces. Both are safe
to run every self-improvement cycle: they rewrite the output directory cleanly.
KaTeX vendoring degrades gracefully (raw math, logged to build.log) and never
crashes the build.

Usage:
    ongo site build [--ken PATH] [--db PATH] [--out DIR] [--site-title TITLE]
              [--base-url URL]

The Ken binary and database use the same resolution rules as the rest of the
plugin (`ONGO_KEN`, `ONGO_KEN_DB`, plugin data, then Ken's own default).
"""

import argparse
import glob
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from .errors import OngoArgumentParser, OngoError


DEFAULT_KEN = "ken"

# KaTeX is vendored LOCALLY into site/assets/katex/ so the published site
# fetches no external assets at runtime. The build may fetch the tarball
# once (into a work dir) if no local copy is available; the runtime site
# never does. Pinned version + sha keeps the build deterministic.
KATEX_VERSION = "0.16.11"
KATEX_TARBALL_URL = (
    "https://registry.npmjs.org/katex/-/katex-%s.tgz" % KATEX_VERSION
)
# Local cache dirs probed before any network fetch (build-time only).
KATEX_LOCAL_CANDIDATES = (
    "~/.cache/ongo/katex",
    "~/.local/share/ongo/katex",
    "/usr/local/share/katex",
    "/usr/share/katex",
)

# Relationship kinds that connect an item to a topic / to another item.
TOPIC_REL_KINDS = ("related-to", "cites", "derives-from")

# Filesystem roots that may hold note markdown bodies, in search order.
NOTE_SEARCH_ROOTS = (
    "~/.local/share/ken/notes",
    "~/research-notes",
    "/home/claude/research-notes",
)


# --------------------------------------------------------------------------- #
# kendb access
# --------------------------------------------------------------------------- #

def resolve_ken(arg_ken):
    """Resolve Ken using the plugin-wide precedence rules."""
    from .ken import resolve_ken as resolve_plugin_ken

    return resolve_plugin_ken(arg_ken)


def resolve_db_path(ken, arg_db):
    from .ken import resolve_db

    return resolve_db(ken, arg_db)


def ken_list(ken, db_path, kind=None):
    """Read every matching publication through Ken's paginated CLI."""
    rows = []
    offset = 0
    while True:
        cmd = [ken, "-D", db_path, "list"]
        if kind is not None:
            cmd.extend(["--kind", kind])
        cmd.extend(["--limit", "500", "--offset", str(offset)])
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, check=True
            ).stdout
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            stderr = getattr(exc, "stderr", "") or str(exc)
            label = f" --kind {kind}" if kind is not None else ""
            raise SystemExit(
                f"error: `ken list{label}` failed: {stderr.strip()}"
            )
        try:
            page = json.loads(out or "[]")
        except json.JSONDecodeError as exc:
            raise SystemExit(f"error: could not parse ken list output: {exc}")
        if not isinstance(page, list):
            raise SystemExit("error: ken list returned a non-array JSON value")
        rows.extend(page)
        if len(page) < 500:
            return rows
        offset += len(page)


def ken_show_record(ken, db_path, pub_id, key):
    """Read a publication through `ken show ... --json` (Ken v3).

    Prefer the full UUID; fall back to ``--key`` only if no id is available.
    """
    if pub_id:
        cmd = [ken, "-D", db_path, "show", pub_id, "--json"]
    elif key:
        cmd = [ken, "-D", db_path, "show", "--key", key, "--json"]
    else:
        return None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as error:
        raise OngoError(
            "could not read a Ken publication",
            code="ken-command-failed",
            exit_code=3,
            details={"publication_id": pub_id, "error": str(error)},
        ) from error
    if proc.returncode != 0:
        raise OngoError(
            "could not read a Ken publication",
            code="ken-command-failed",
            exit_code=3,
            details={
                "publication_id": pub_id,
                "returncode": proc.returncode,
                "stderr": proc.stderr.strip(),
            },
        )
    out = (proc.stdout or "").strip()
    if not out:
        raise OngoError(
            "Ken returned an empty publication record",
            code="ken-json-invalid",
            exit_code=3,
            details={"publication_id": pub_id},
        )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as error:
        raise OngoError(
            "Ken returned an invalid publication record",
            code="ken-json-invalid",
            exit_code=3,
            details={"publication_id": pub_id, "error": str(error)},
        ) from error
    if not isinstance(data, dict):
        raise OngoError(
            "Ken returned an invalid publication record",
            code="ken-json-invalid",
            exit_code=3,
            details={"publication_id": pub_id},
        )
    return data


def ken_show_body(ken, db_path, pub_id, key):
    """Return a publication note body through Ken's supported CLI."""
    data = ken_show_record(ken, db_path, pub_id, key)
    if data is None:
        return None
    body = data.get("body")
    if not isinstance(body, str):
        return None
    return body


def ken_publication_timestamps(db_path):
    """Read the one publication field that Ken v3's JSON API omits.

    All publication content and relationships still flow through Ken's CLI.
    The site uses a read-only SQLite connection solely for ``created_at`` so
    it can preserve the established date labels and chronological ordering
    while the pinned Ken v3 interface remains unchanged.
    """
    uri = f"{Path(db_path).expanduser().resolve().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            rows = connection.execute(
                "SELECT id, created_at FROM publications"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise SystemExit(
            f"error: could not read publication timestamps from Ken database: {exc}"
        )
    return {publication_id: created_at for publication_id, created_at in rows}


class KenView:
    """Read-only site projection over Ken v3 records."""

    def __init__(self, ken, db_path):
        self.ken = ken
        self.db_path = db_path
        self.rows = []
        self.by_id = {}
        self.by_key = {}
        self._shown = {}
        timestamps = ken_publication_timestamps(db_path)
        for rank, raw in enumerate(ken_list(ken, db_path)):
            row = dict(raw)
            row["created_at"] = row.get("created_at") or timestamps.get(row["id"])
            row["list_rank"] = rank
            self.rows.append(row)
            self.by_id[row["id"]] = row
            if row.get("kind") != "ongo-web" and row.get("key"):
                self.by_key.setdefault(row["key"], []).append(row)

    def publication(self, identifier):
        return self.by_id.get(identifier)

    def publication_by_key(self, key):
        matches = self.by_key.get(key, [])
        if len(matches) > 1:
            raise OngoError(
                "Ken publication identifier is ambiguous",
                code="publication-conflict",
                exit_code=4,
                details={"identifier": key, "count": len(matches)},
            )
        return matches[0] if matches else None

    def resolve_publication(self, identifier):
        matches = {}
        by_id = self.publication(identifier)
        if by_id is not None:
            matches[by_id["id"]] = by_id
        for row in self.by_key.get(identifier, []):
            matches[row["id"]] = row
        if len(matches) > 1:
            raise OngoError(
                "Ken publication identifier is ambiguous",
                code="publication-conflict",
                exit_code=4,
                details={"identifier": identifier, "count": len(matches)},
            )
        return next(iter(matches.values()), None)

    def show(self, pub_id, *, strict=True):
        if pub_id not in self._shown:
            row = self.by_id.get(pub_id)
            try:
                record = (
                    ken_show_record(
                        self.ken,
                        self.db_path,
                        pub_id,
                        row.get("key") if row else None,
                    )
                    if row is not None
                    else None
                )
            except OngoError:
                if strict:
                    raise
                return None
            if row is not None and record is None:
                if strict:
                    raise OngoError(
                        "could not read a listed Ken publication",
                        code="ken-record-unreadable",
                        exit_code=3,
                        details={"publication_id": pub_id},
                    )
                return None
            self._shown[pub_id] = record
        return self._shown[pub_id]


def fetch_publication(view, pub_id):
    return view.publication(pub_id)


def fetch_publication_by_key(view, key):
    """Resolve a key to a publication, never to an ongo-web marker.

    An ongo-web marker's own key equals the slug/key of its target, so a
    naive `WHERE key = ?` would match the marker itself. Exclude the marker
    kind and prefer a deterministic order.
    """
    return view.publication_by_key(key)


def resolve_publication_reference(view, identifier):
    """Resolve an id or key together, rejecting every ambiguous reference."""
    return view.resolve_publication(identifier)


def fetch_digest_body(view, pub_id):
    """Return a digest body and the note publications it was copied from.

    Digests keep their body on a subject-side ``related-to`` note. Ken v3
    returns those relationships and note bodies through ``show --json``.
    """
    record = view.show(pub_id, strict=False) or {}
    bodies = []
    source_ids = []
    for relation in record.get("relationships", []):
        if relation.get("role") != "subject" or relation.get("relkind") != "related-to":
            continue
        related = view.publication(relation.get("publication"))
        if related is None or related.get("kind") != "note":
            continue
        shown = view.show(related["id"], strict=False) or {}
        body = shown.get("body")
        if isinstance(body, str) and body:
            bodies.append(body)
            source_ids.append(related["id"])
    return "\n\n".join(bodies).strip(), source_ids


# --------------------------------------------------------------------------- #
# source body resolution
# --------------------------------------------------------------------------- #

def find_slug_file(slug):
    """Locate a markdown file for a slug-style key under known note roots."""
    if not slug or "/" in slug or slug.endswith(".pdf"):
        return None
    for root in NOTE_SEARCH_ROOTS:
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        direct = os.path.join(root, slug + ".md")
        if os.path.isfile(direct):
            return direct
        # recursive match: any *<slug>*.md (deterministic: sorted, first hit)
        matches = sorted(
            glob.glob(os.path.join(root, "**", "*.md"), recursive=True)
        )
        for m in matches:
            stem = os.path.splitext(os.path.basename(m))[0]
            if stem == slug:
                return m
    return None


def resolve_source(view, pub, log, ken, db_path):
    """Resolve a publication to its renderable source.

    Returns a dict: {kind: 'markdown'|'pdf'|'text', body|path}
    or None if it cannot be resolved (caller emits a warning + skips).

    Resolution order: a filesystem .md/.pdf/.tex named by the publication
    key, a slug match under the note roots, the kendb note body read via
    `ken show --json` (the supported Ken v3 read path), and finally the title.
    """
    key = pub["key"]
    title = pub["title"] or ""

    # Experiment roots remain private unless an explicit ongo-web marker
    # selects them. When selected, render the reviewed protocol and exact
    # condition matrix rather than exposing the root publication's JSON.
    if pub["kind"] == "ongo-experiment":
        try:
            from .experiments import markdown_view
            from .ken import KenClient

            client = KenClient(binary=ken, db=db_path)
            return {"kind": "markdown", "body": markdown_view(client, pub["id"])}
        except Exception as error:
            log.append(
                f"  WARNING: could not render experiment {pub['id']}: {error}"
            )
            return None

    # 1. key is a filesystem path.
    if key and key.startswith("/"):
        if os.path.isfile(key):
            ext = os.path.splitext(key)[1].lower()
            if ext == ".pdf":
                return {"kind": "pdf", "path": key}
            if ext in (".md", ".markdown", ".tex", ".txt"):
                with open(key, "r", encoding="utf-8", errors="replace") as fh:
                    return {"kind": "markdown", "body": fh.read(),
                            "is_tex": ext == ".tex"}
        # fall through to other strategies if the path is missing

    # 2. key is a slug -> filesystem markdown.
    if key:
        slug_file = find_slug_file(key)
        if slug_file:
            with open(slug_file, "r", encoding="utf-8",
                      errors="replace") as fh:
                return {"kind": "markdown", "body": fh.read()}

    # 3. Ken v3's supported publication body read path.
    shown = view.show(pub["id"], strict=False)
    show_body = shown.get("body") if shown else None
    if show_body and show_body.strip():
        return {"kind": "markdown", "body": show_body}

    # 4. fall back to the title as the body.
    if title.strip():
        log.append(
            f"  note: {pub['id']} resolved to title-only body "
            f"(no file or notes body found)"
        )
        return {"kind": "markdown", "body": "# " + title.strip()}

    return None


# --------------------------------------------------------------------------- #
# KaTeX vendoring (build-time only; runtime is fully self-contained)
# --------------------------------------------------------------------------- #

def _katex_dir_is_complete(path):
    """A vendored KaTeX dir is usable iff it has the css, js and a fonts dir."""
    return (
        os.path.isfile(os.path.join(path, "katex.min.css"))
        and os.path.isfile(os.path.join(path, "katex.min.js"))
        and os.path.isdir(os.path.join(path, "fonts"))
    )


def _find_local_katex():
    """Return a path to an already-available KaTeX dist dir, or None.

    Probed before any network fetch so the build prefers a local copy and
    only downloads when nothing is cached. Never raises.
    """
    for cand in KATEX_LOCAL_CANDIDATES:
        cand = os.path.expanduser(cand)
        if _katex_dir_is_complete(cand):
            return cand
        dist = os.path.join(cand, "dist")
        if _katex_dir_is_complete(dist):
            return dist
    return None


def _download_katex_into(work_dir, log):
    """Fetch + extract the pinned KaTeX tarball into work_dir/_katex/dist.

    Build-time only. Returns the dist path on success or None on any
    failure (logged); the caller then degrades gracefully (raw math).
    """
    extract_root = os.path.join(work_dir, "_katex")
    try:
        os.makedirs(extract_root, exist_ok=True)
        tgz_path = os.path.join(extract_root, "katex.tgz")
        with urllib.request.urlopen(KATEX_TARBALL_URL, timeout=30) as resp:
            data = resp.read()
        with open(tgz_path, "wb") as fh:
            fh.write(data)
        with tarfile.open(tgz_path, "r:gz") as tf:
            members = [
                mi
                for mi in tf.getmembers()
                if mi.name.startswith("package/dist/")
                and (mi.isfile() or mi.isdir())
                and ".." not in mi.name.split("/")
            ]
            for mi in members:
                # Strip the leading "package/" so files land in dist/.
                mi_name = mi.name[len("package/"):]
                target = os.path.join(extract_root, mi_name)
                if mi.isdir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(mi)
                if src is None:
                    continue
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        dist = os.path.join(extract_root, "dist")
        if _katex_dir_is_complete(dist):
            log.append(
                f"katex: downloaded {KATEX_VERSION} from npm (build-time)"
            )
            return dist
        log.append("katex: download incomplete — degrading to raw math")
        return None
    except Exception as exc:  # noqa: BLE001 — never crash the build
        log.append(f"katex: download failed ({exc}) — degrading to raw math")
        return None


def vendor_katex(work_dir, log):
    """Copy a usable KaTeX dist into work_dir/assets/katex/.

    Order: a local cached copy first; otherwise a one-time build-time
    download. Returns True if KaTeX assets were vendored (math should be
    rendered), False to degrade gracefully (raw `$...$` left in place,
    reason logged — the build never crashes on KaTeX problems).
    """
    src = _find_local_katex()
    if src:
        log.append(f"katex: using local copy at {src}")
    else:
        src = _download_katex_into(work_dir, log)
    if not src:
        return False
    dest = os.path.join(work_dir, "assets", "katex")
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # Copy only the three things the site needs (css, js, fonts/) so
        # the output is minimal and deterministic.
        os.makedirs(dest, exist_ok=True)
        shutil.copyfile(
            os.path.join(src, "katex.min.css"),
            os.path.join(dest, "katex.min.css"),
        )
        shutil.copyfile(
            os.path.join(src, "katex.min.js"),
            os.path.join(dest, "katex.min.js"),
        )
        fonts_src = os.path.join(src, "fonts")
        fonts_dest = os.path.join(dest, "fonts")
        if os.path.isdir(fonts_dest):
            shutil.rmtree(fonts_dest)
        # Deterministic copy: sorted file list.
        os.makedirs(fonts_dest, exist_ok=True)
        for name in sorted(os.listdir(fonts_src)):
            sp = os.path.join(fonts_src, name)
            if os.path.isfile(sp):
                shutil.copyfile(sp, os.path.join(fonts_dest, name))
        log.append("katex: vendored css+js+fonts into assets/katex/")
        return True
    except Exception as exc:  # noqa: BLE001 — never crash the build
        log.append(f"katex: vendoring failed ({exc}) — degrading to raw math")
        return False


# Client-side renderer (deterministic, no contrib auto-render dependency):
# it walks for our build-emitted placeholder spans and calls katex.render.
KATEX_RUNTIME_JS = """\
(function(){
  // NOTE: an inline <script> ignores the `defer` attribute, so this must
  // not run at parse time (KaTeX, a deferred external script, isn't loaded
  // yet -> spans would be left blank). Run on DOMContentLoaded (by which
  // point the deferred katex.min.js has executed); also re-run on load,
  // and if KaTeX is still unavailable, fall back to the raw TeX source so
  // math is never invisible.
  function render(){
    var nodes=document.querySelectorAll("span.ongo-math,div.ongo-math");
    var hasK=(typeof katex!=="undefined");
    for(var i=0;i<nodes.length;i++){
      var el=nodes[i];
      if(el.getAttribute("data-done")==="1")continue;
      var tex=el.getAttribute("data-tex")||"";
      var display=el.getAttribute("data-display")==="1";
      if(hasK){
        try{
          katex.render(tex,el,{displayMode:display,throwOnError:false});
        }catch(e){
          el.textContent=(display?"$$":"$")+tex+(display?"$$":"$");
        }
        el.setAttribute("data-done","1");
      }else{
        el.textContent=(display?"$$":"$")+tex+(display?"$$":"$");
      }
    }
  }
  window.__ongoRenderMath=render;
  if(document.readyState==="loading"){
    document.addEventListener("DOMContentLoaded",render);
  }else{
    render();
  }
  window.addEventListener("load",render);
})();
"""


# Light/dark theme toggle (pure vanilla, no deps). On load: saved choice in
# localStorage("ongo-theme") wins; else fall back to the OS preference
# (prefers-color-scheme). Clicking the header control flips and persists it.
# The early inline part (THEME_INIT_JS) runs in <head> before paint to set
# data-theme and avoid a flash; THEME_TOGGLE_JS wires the button at end.
THEME_INIT_JS = """\
(function(){try{
var s=localStorage.getItem("ongo-theme");
var m=window.matchMedia&&window.matchMedia("(prefers-color-scheme:dark)").matches;
var t=(s==="light"||s==="dark")?s:(m?"dark":"light");
document.documentElement.setAttribute("data-theme",t);
}catch(e){}})();
"""

THEME_TOGGLE_JS = """\
(function(){
var b=document.getElementById("theme-toggle");if(!b)return;
function cur(){return document.documentElement.getAttribute("data-theme")||"light";}
function paint(){b.setAttribute("aria-label","Switch to "+(cur()==="dark"?"light":"dark")+" mode");
b.textContent=cur()==="dark"?"\\u2600":"\\u263E";}
paint();
b.addEventListener("click",function(){
var t=cur()==="dark"?"light":"dark";
document.documentElement.setAttribute("data-theme",t);
try{localStorage.setItem("ongo-theme",t);}catch(e){}
paint();});
})();
"""


# Random-article button. Reads the build-time-emitted global
# window.__ONGO_ITEMS__ (an array of relative item-page URLs, correct
# depth for the current page) and on click navigates to a uniformly
# random entry. Pure client-side randomness; build stays deterministic.
RANDOM_JS = """\
(function(){
var b=document.getElementById("rand-article");if(!b)return;
var L=window.__ONGO_ITEMS__;
if(!L||!L.length){b.disabled=true;return;}
b.addEventListener("click",function(){
var i=Math.floor(Math.random()*L.length);
if(i>=L.length)i=L.length-1;
window.location.href=L[i];});
})();
"""


# --------------------------------------------------------------------------- #
# math extraction ($...$, $$...$$, \(...\), \[...\])
# --------------------------------------------------------------------------- #

def extract_math(md):
    """Pull math spans out of raw Markdown before any HTML escaping.

    Replaces each occurrence with an opaque placeholder token so the
    Markdown renderer never mangles TeX, then the placeholders are swapped
    for KaTeX-target spans after rendering. Returns (text, mapping) where
    mapping[token] = (tex, is_display).

    Display: $$...$$ and \\[...\\].  Inline: $...$ and \\(...\\).
    Fenced/indented code is left alone because extraction runs on the raw
    source and code spans rarely contain `$…$`; KaTeX is also told not to
    throw, so a stray match degrades to literal text rather than breaking.
    """
    mapping = {}

    def _store(tex, is_display):
        token = "\x00MATH%d\x00" % len(mapping)
        mapping[token] = (tex.strip(), is_display)
        return token

    # Order matters: display delimiters before inline so $$ is not eaten
    # as two empty $ … $.
    patterns = [
        (re.compile(r"\$\$(.+?)\$\$", re.DOTALL), True),
        (re.compile(r"\\\[(.+?)\\\]", re.DOTALL), True),
        (re.compile(r"\\\((.+?)\\\)", re.DOTALL), False),
        # Inline $...$: not $$, no newline inside, not an escaped \$.
        (re.compile(r"(?<!\\)\$(?!\$)([^\n$]+?)(?<!\\)\$(?!\$)"), False),
    ]
    for rx, is_display in patterns:
        md = rx.sub(lambda m: _store(m.group(1), is_display), md)
    return md, mapping


def reinsert_math(html_text, mapping):
    """Swap math placeholders for KaTeX-target spans in rendered HTML."""
    for token, (tex, is_display) in mapping.items():
        tag = "div" if is_display else "span"
        span = (
            '<%s class="ongo-math" data-display="%s" data-tex="%s"></%s>'
            % (
                tag,
                "1" if is_display else "0",
                html.escape(tex, quote=True),
                tag,
            )
        )
        html_text = html_text.replace(token, span)
    return html_text


# --------------------------------------------------------------------------- #
# minimal Markdown -> HTML
# --------------------------------------------------------------------------- #

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)([^*]+)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_HTML_TAG_LINE = re.compile(r"^\s*<(/?)([a-zA-Z][\w-]*)")

# A whole Markdown link span `[ ... ]( ... )` that may be soft-wrapped: the
# `[text]` and/or the `(url)` can straddle newlines or a list-item boundary.
# `[^\[\]]` (no nested brackets) keeps it from running away across the doc;
# DOTALL lets the inner text and url contain the wrapping newlines. The url
# part forbids whitespace AFTER collapsing (caught in the substitution).
_WRAPPED_LINK = re.compile(
    r"\[([^\[\]]+?)\]\s*\(\s*([^()\s][^()]*?)\s*\)", re.DOTALL
)


def _join_wrapped_links(md):
    """Collapse internal whitespace/newlines inside Markdown link spans.

    A `[text](url)` whose `text` (or the whole token) is soft-wrapped across
    lines — or across a list-item boundary — would otherwise never reach the
    inline renderer intact: the line-based block parser splits the `[` into
    bare list/paragraph text and the `](url)` leaks as literal text into a
    later block. Run this on the RAW markdown (before block parsing): for
    every bracket-paren link span, replace runs of internal whitespace
    (incl. newlines) in the link *text* with single spaces and strip
    whitespace from the *url*, so `[ ... \\n ... ]( ... )` becomes a
    single-line `[...](...)` the normal `_LINK` pass recognizes. Real
    Markdown links on one line are unaffected (no internal newline to
    collapse). Code fences are protected so `[x](y)` inside a code block
    is left byte-for-byte intact.
    """
    # Protect fenced code blocks: never rewrite link-looking text in code.
    fences = {}

    def _stash_fence(m):
        tok = "\x00FENCE%d\x00" % len(fences)
        fences[tok] = m.group(0)
        return tok

    protected = re.sub(
        r"(^|\n)([ \t]*)(```|~~~)[^\n]*\n.*?(?:\n[ \t]*\3[^\n]*|\Z)",
        _stash_fence,
        md,
        flags=re.DOTALL,
    )

    def _collapse(m):
        label = re.sub(r"\s+", " ", m.group(1)).strip()
        url = re.sub(r"\s+", "", m.group(2)).strip()
        if not label or not url:
            return m.group(0)
        return "[%s](%s)" % (label, url)

    joined = _WRAPPED_LINK.sub(_collapse, protected)
    for tok, original in fences.items():
        joined = joined.replace(tok, original)
    return joined


# Collapse an over-escaped HTML entity back to a single, valid entity:
# `&amp;amp;` -> `&amp;`, `&amp;quot;` -> `&quot;`, `&amp;lt;` -> `&lt;`,
# `&amp;#x27;` -> `&#x27;`, etc. Applied AFTER html.escape so a source
# `&amp;`/`&quot;`/`&#x27;` that was escaped a second time (or a link label
# escaped twice) is healed; a single real `&` stays `&amp;` (it has no
# trailing entity name to fold). Idempotent: re-running changes nothing.
_OVER_ESCAPED_ENTITY = re.compile(
    r"&amp;(amp|lt|gt|quot|apos|#x?[0-9a-fA-F]+);"
)


def _unescape_double_entities(text):
    """Fold `&amp;(amp|lt|gt|quot|#x?\\d+);` -> `&\\1;` (repeatedly).

    Handles arbitrarily deep double-escaping (`&amp;amp;amp;` -> `&amp;`)
    by iterating to a fixed point. Leaves a lone escaped `&` (`&amp;`)
    untouched because it is not followed by another entity name.
    """
    prev = None
    while prev != text:
        prev = text
        text = _OVER_ESCAPED_ENTITY.sub(r"&\1;", text)
    return text
# Bare http/https URL not already inside a Markdown link target. Trailing
# sentence punctuation is trimmed so "see https://x.org." links cleanly.
_BARE_URL = re.compile(r"(?<![\"'(\]=])\bhttps?://[^\s<>\)\]]+")
_URL_TRAILING = ".,;:!?’')\""


def _is_external(href):
    return bool(re.match(r"^[a-z][a-z0-9+.-]*://", href)) and not (
        href.startswith("/")
    )


def _anchor(href, label, external):
    extra = ' target="_blank" rel="noopener"' if external else ""
    return '<a href="%s"%s>%s</a>' % (
        html.escape(href, quote=True),
        extra,
        label,
    )


def safe_link_href(href):
    """Allow ordinary links while rejecting executable URL schemes."""
    if not isinstance(href, str) or href != href.strip():
        return False
    if re.search(r"[\\\x00-\x20\x7f]", href):
        return False
    if href.startswith("//"):
        return False
    scheme = re.match(r"^([a-z][a-z0-9+.-]*):", href, re.IGNORECASE)
    if scheme:
        return scheme.group(1).lower() in {"http", "https", "mailto"}
    return True


_RAW_HTML_ALLOWED_TAGS = frozenset(
    {
        "a", "abbr", "article", "b", "blockquote", "br", "code", "dd",
        "del", "details", "div", "dl", "dt", "em", "figcaption", "figure",
        "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "kbd",
        "li", "mark", "nav", "ol", "p", "pre", "s", "section", "small",
        "span", "strong", "sub", "summary", "sup", "table", "tbody", "td",
        "tfoot", "th", "thead", "time", "tr", "u", "ul",
    }
)
_RAW_HTML_VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }
)
_RAW_HTML_DISCARDED_TAGS = frozenset(
    {
        "applet", "area", "audio", "base", "button", "col", "embed", "form",
        "frame", "frameset", "iframe", "input", "link", "math", "meta",
        "noscript", "object", "option", "param", "script", "select", "source",
        "style", "svg", "template", "textarea", "track", "video", "wbr",
    }
)
_RAW_HTML_GLOBAL_ATTRIBUTES = frozenset(
    {"aria-hidden", "aria-label", "class", "id", "role", "title"}
)
_RAW_HTML_TAG_ATTRIBUTES = {
    "a": frozenset({"href", "rel", "target"}),
    "blockquote": frozenset({"cite"}),
    "details": frozenset({"open"}),
    "img": frozenset({"alt", "height", "loading", "src", "width"}),
    "li": frozenset({"value"}),
    "ol": frozenset({"reversed", "start"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan", "scope"}),
    "time": frozenset({"datetime"}),
}
_SAFE_DATA_IMAGE = re.compile(
    r"^data:image/(?:avif|gif|jpeg|png|webp);base64,[A-Za-z0-9+/=]+$",
    re.IGNORECASE,
)


def safe_media_src(value, allow_remote=True):
    if _SAFE_DATA_IMAGE.fullmatch(value):
        return True
    if not safe_link_href(value):
        return False
    scheme = re.match(r"^([a-z][a-z0-9+.-]*):", value, re.IGNORECASE)
    return scheme is None or (
        allow_remote and scheme.group(1).lower() in {"http", "https"}
    )


def _sanitize_raw_attributes(tag, attributes, allow_remote_images):
    allowed = _RAW_HTML_GLOBAL_ATTRIBUTES | _RAW_HTML_TAG_ATTRIBUTES.get(
        tag, frozenset()
    )
    result = []
    for name, value in attributes:
        name = name.lower()
        if name not in allowed:
            continue
        value = "" if value is None else value
        if name in {"href", "cite"} and not safe_link_href(value):
            continue
        if name == "src" and not safe_media_src(value, allow_remote_images):
            continue
        if name == "target" and value != "_blank":
            continue
        if name == "rel":
            continue
        if name in {"open", "reversed"}:
            result.append((name, None))
        else:
            result.append((name, value))
    if tag == "a":
        has_href = any(name == "href" for name, _value in result)
        if not has_href:
            result = [item for item in result if item[0] != "target"]
        elif any(name == "target" for name, _value in result):
            result.append(("rel", "noopener noreferrer"))
    return result


class _RawHTMLSanitizer(HTMLParser):
    def __init__(self, allow_remote_images=True):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.discard_depth = 0
        self.allow_remote_images = allow_remote_images

    def handle_starttag(self, tag, attributes):
        tag = tag.lower()
        if self.discard_depth:
            if tag not in _RAW_HTML_VOID_TAGS:
                self.discard_depth += 1
            return
        if tag in _RAW_HTML_DISCARDED_TAGS:
            if tag not in _RAW_HTML_VOID_TAGS:
                self.discard_depth = 1
            return
        if tag not in _RAW_HTML_ALLOWED_TAGS:
            return
        rendered = []
        for name, value in _sanitize_raw_attributes(
            tag, attributes, self.allow_remote_images
        ):
            if value is None:
                rendered.append(name)
            else:
                rendered.append(f'{name}="{html.escape(value, quote=True)}"')
        suffix = (" " + " ".join(rendered)) if rendered else ""
        self.output.append(f"<{tag}{suffix}>")

    def handle_startendtag(self, tag, attributes):
        if self.discard_depth:
            return
        lowered = tag.lower()
        if lowered in _RAW_HTML_DISCARDED_TAGS:
            return
        self.handle_starttag(tag, attributes)
        if (
            lowered in _RAW_HTML_ALLOWED_TAGS
            and lowered not in _RAW_HTML_VOID_TAGS
        ):
            self.output.append(f"</{lowered}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.discard_depth:
            self.discard_depth -= 1
            return
        if tag in _RAW_HTML_ALLOWED_TAGS and tag not in _RAW_HTML_VOID_TAGS:
            self.output.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.discard_depth:
            self.output.append(html.escape(data))


def sanitize_raw_html(value, allow_remote_images=True):
    """Preserve inert presentation markup while removing executable HTML."""
    parser = _RawHTMLSanitizer(allow_remote_images=allow_remote_images)
    parser.feed(value)
    parser.close()
    return "".join(parser.output)


def _render_inline(text, link_resolver):
    """Render inline markdown within an already-escaped run is wrong; we
    escape here and re-insert safe markup."""
    # Protect inline code spans first (no further processing inside).
    placeholders = {}

    def _stash(m):
        token = f"\x00CODE{len(placeholders)}\x00"
        placeholders[token] = "<code>" + html.escape(m.group(1)) + "</code>"
        return token

    text = _INLINE_CODE.sub(_stash, text)
    text = html.escape(text)
    # `html.escape` turns a source `&` into `&amp;`; if that `&` was the
    # start of an entity the source ALREADY contained (`&amp;`, `&quot;`,
    # `&#x27;`, `&lt;`, `&gt;`) it is now double-escaped (`&amp;amp;` …).
    # Fold those back so a pre-existing valid entity passes through exactly
    # once while a real lone `&`/`<`/`>` in prose stays escaped.
    text = _unescape_double_entities(text)
    # Currency escaped in the source as `\$` (so KaTeX won't treat it as
    # math) must render as a literal `$`, not show the backslash. Math has
    # already been pulled out to opaque tokens by extract_math(), so any
    # `\$` remaining here is genuine escaped currency — strip the slash.
    text = text.replace("\\$", "$")

    def _link_sub(m):
        label, target = m.group(1), m.group(2)
        # `target` was captured from already-escaped text, so a `&` in a
        # query string is now `&amp;`. `_anchor` re-escapes the href via
        # html.escape(quote=True), which would double-escape it. Unescape
        # the captured target once here so the single canonical escape in
        # `_anchor` is the only one applied.
        target = html.unescape(target)
        href, is_plain = link_resolver(target)
        # `label` came from `text`, which is ALREADY html-escaped (and
        # entity-folded) above. Re-escaping here is the double-escape bug
        # (`&` -> `&amp;` -> `&amp;amp;`). Use the label as-is.
        if is_plain or not safe_link_href(href):
            # Unpublished target: degrade to plain text, no link, no leak.
            return label
        return _anchor(href, label, _is_external(href))

    # _LINK runs against escaped text; brackets/parens are not escaped by
    # html.escape, so the pattern still matches. Stash each Markdown link
    # as a placeholder so the bare-URL pass below cannot re-link the URL
    # already inside it.
    def _link_stash(m):
        token = f"\x00LINK{len(placeholders)}\x00"
        placeholders[token] = _link_sub(m)
        return token

    text = _LINK.sub(_link_stash, text)

    # Auto-link bare http/https URLs into clickable anchors. External by
    # definition (scheme present) -> target=_blank rel=noopener.
    def _bare_sub(m):
        raw = m.group(0)
        trail = ""
        while raw and raw[-1] in _URL_TRAILING:
            trail = raw[-1] + trail
            raw = raw[:-1]
        if not raw:
            return m.group(0)
        token = f"\x00LINK{len(placeholders)}\x00"
        placeholders[token] = _anchor(raw, html.escape(raw), True)
        return token + trail

    text = _BARE_URL.sub(_bare_sub, text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    for token, value in placeholders.items():
        text = text.replace(token, value)
    return text


def markdown_to_html(
    md,
    link_resolver,
    collect_toc=False,
    allow_raw_html=True,
    allow_remote_images=True,
):
    """Convert a useful subset of Markdown to HTML.

    Supports: ATX headings, paragraphs, ordered/unordered lists, fenced and
    indented code blocks, blockquotes, horizontal rules, inline code,
    bold/italic, and links. Raw HTML blocks use a presentation-only allowlist;
    executable elements, event handlers, inline styles, and unsafe URLs are
    removed. The private ``allow_raw_html`` switch can disable raw HTML, and
    ``allow_remote_images`` can suppress HTTP(S) images for protected content.

    When collect_toc is True, returns (html, toc) where toc is a list of
    (level, anchor_id, text) for every heading (h1-h6), and each emitted
    heading carries a unique id="" so a per-article table of contents can
    link to it. Otherwise returns just the html string.
    """
    md = md.replace("\r\n", "\n").replace("\r", "\n")
    # Join soft-wrapped `[text](url)` spans BEFORE the line-based block
    # parser runs, so a link whose text/token straddles a newline or a
    # list-item boundary still renders as a single `<a>` instead of leaking
    # `](url)` into a later block.
    md = _join_wrapped_links(md)
    lines = md.split("\n")
    out = []
    toc = []
    seen_ids = {}
    i = 0
    n = len(lines)

    def _heading_id(text):
        base = slugify(re.sub(r"<[^>]+>", "", text), "section") or "section"
        if base in seen_ids:
            seen_ids[base] += 1
            return "%s-%d" % (base, seen_ids[base])
        seen_ids[base] = 0
        return base

    def close_list(stack):
        while stack:
            out.append("</%s>" % stack.pop())

    list_stack = []

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Fenced code block.
        if stripped.startswith("```") or stripped.startswith("~~~"):
            close_list(list_stack)
            fence = stripped[:3]
            i += 1
            buf = []
            while i < n and lines[i].strip()[:3] != fence:
                buf.append(lines[i])
                i += 1
            i += 1  # consume closing fence
            out.append(
                "<pre><code>" + html.escape("\n".join(buf)) + "</code></pre>"
            )
            continue

        # Preserve presentation-only raw HTML through the shared sanitizer.
        if allow_raw_html and _HTML_TAG_LINE.match(line):
            close_list(list_stack)
            buf = [line]
            i += 1
            while i < n and lines[i].strip() != "":
                buf.append(lines[i])
                i += 1
            sanitized = sanitize_raw_html(
                "\n".join(buf), allow_remote_images=allow_remote_images
            )
            if sanitized:
                out.append(sanitized)
            continue

        # Blank line.
        if stripped == "":
            close_list(list_stack)
            i += 1
            continue

        # Horizontal rule.
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            close_list(list_stack)
            out.append("<hr>")
            i += 1
            continue

        # Heading.
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            close_list(list_stack)
            level = len(m.group(1))
            inner = _render_inline(m.group(2), link_resolver)
            hid = _heading_id(m.group(2))
            out.append(
                '<h%d id="%s">%s'
                '<a class="anchor" href="#%s" aria-hidden="true">#</a>'
                "</h%d>" % (level, hid, inner, hid, level)
            )
            # `inner` is already-rendered (html-escaped) HTML. Strip tags,
            # then html.unescape so the TOC stores PLAIN text; render_toc
            # applies the single canonical escape. Without the unescape the
            # heading's `&quot;`/`&#x27;`/`&amp;` would be escaped a second
            # time there (`&amp;quot;` …) — the TOC double-escape bug.
            toc.append(
                (
                    level,
                    hid,
                    html.unescape(
                        re.sub(r"<[^>]+>", "", inner)
                    ).strip(),
                )
            )
            i += 1
            continue

        # Blockquote.
        if stripped.startswith(">"):
            close_list(list_stack)
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append(
                "<blockquote>%s</blockquote>"
                % _render_inline(" ".join(buf), link_resolver)
            )
            continue

        # Lists (unordered / ordered). Single level is enough for the corpus.
        m_ul = re.match(r"^(\s*)([-*+])\s+(.*)$", line)
        m_ol = re.match(r"^(\s*)(\d+)[.)]\s+(.*)$", line)
        if m_ul or m_ol:
            tag = "ul" if m_ul else "ol"
            if not list_stack or list_stack[-1] != tag:
                close_list(list_stack)
                out.append("<%s>" % tag)
                list_stack.append(tag)
            content = (m_ul or m_ol).group(3)
            out.append(
                "<li>%s</li>" % _render_inline(content, link_resolver)
            )
            i += 1
            continue

        # Indented code block (4 spaces / tab).
        if line.startswith("    ") or line.startswith("\t"):
            close_list(list_stack)
            buf = []
            while i < n and (
                lines[i].startswith("    ")
                or lines[i].startswith("\t")
                or lines[i].strip() == ""
            ):
                if lines[i].strip() == "" and not (
                    i + 1 < n
                    and (
                        lines[i + 1].startswith("    ")
                        or lines[i + 1].startswith("\t")
                    )
                ):
                    break
                buf.append(re.sub(r"^(\t|    )", "", lines[i]))
                i += 1
            out.append(
                "<pre><code>"
                + html.escape("\n".join(buf).rstrip())
                + "</code></pre>"
            )
            continue

        # Paragraph (gather consecutive plain lines).
        close_list(list_stack)
        buf = [line]
        i += 1
        while i < n:
            nxt = lines[i]
            ns = nxt.strip()
            if ns == "" or ns.startswith(("#", ">", "```", "~~~")):
                break
            if re.match(r"^\s*([-*+]|\d+[.)])\s+", nxt):
                break
            if _HTML_TAG_LINE.match(nxt):
                break
            buf.append(nxt)
            i += 1
        out.append(
            "<p>%s</p>" % _render_inline(" ".join(buf), link_resolver)
        )

    close_list(list_stack)
    rendered = "\n".join(out)
    if collect_toc:
        return rendered, toc
    return rendered


# --------------------------------------------------------------------------- #
# site assembly
# --------------------------------------------------------------------------- #

CSS = """\
:root{
--fg:#22232b;--fg-soft:#3c3d47;--bg:#fcfcfb;--surface:#ffffff;
--muted:#6a6b78;--accent:#3a5a99;--accent-hover:#284077;
--border:#e5e5e1;--border-soft:#eeeeea;--code-bg:#f4f4f1;
--code-fg:#1f2430;--mark:#fff3bf;--shadow:0 1px 2px rgba(20,20,30,.04),
0 8px 24px rgba(20,20,30,.05);
--serif:Charter,"Iowan Old Style","Source Serif Pro",
"Apple Garamond",Georgia,Cambria,"Times New Roman",serif;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,
Arial,"Helvetica Neue",sans-serif;
--mono:"SFMono-Regular",ui-monospace,"JetBrains Mono","Cascadia Code",
Menlo,Consolas,"Liberation Mono",monospace}
/* Dark palette. Applied when the user explicitly chose dark
(html[data-theme=dark]) OR when the OS prefers dark AND the user has not
explicitly chosen light (html[data-theme=light] opts out of the media
query). The toggle persists the choice in localStorage; an early inline
script sets data-theme before paint so there is no flash. */
:root[data-theme=dark]{
--fg:#e7e7ea;--fg-soft:#c6c7cf;--bg:#16171c;--surface:#1e1f26;
--muted:#9698a6;--accent:#8fb0e8;--accent-hover:#b3c9f2;
--border:#30323b;--border-soft:#26272f;--code-bg:#23242c;
--code-fg:#dfe2ea;--mark:#5a4c1f;
--shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.35)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
--fg:#e7e7ea;--fg-soft:#c6c7cf;--bg:#16171c;--surface:#1e1f26;
--muted:#9698a6;--accent:#8fb0e8;--accent-hover:#b3c9f2;
--border:#30323b;--border-soft:#26272f;--code-bg:#23242c;
--code-fg:#dfe2ea;--mark:#5a4c1f;
--shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.35)}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;font-family:var(--sans);font-size:18px;line-height:1.7;
color:var(--fg);background:var(--bg);
font-feature-settings:"kern" 1,"liga" 1;
text-rendering:optimizeLegibility;
-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
.wrap{max-width:46rem;margin:0 auto;padding:2.5rem 1.4rem 6rem}
::selection{background:var(--mark);color:var(--fg)}
a{color:var(--accent);text-decoration:none;
text-underline-offset:.16em;text-decoration-thickness:.06em}
a:hover{color:var(--accent-hover);text-decoration:underline}

header.site{display:flex;align-items:baseline;justify-content:space-between;
gap:1rem;flex-wrap:wrap;border-bottom:1px solid var(--border);
margin-bottom:2.5rem;padding-bottom:1.1rem}
header.site .brand{display:flex;flex-direction:column;gap:.15rem}
header.site a.home{color:var(--fg);font-weight:700;font-size:1.2rem;
letter-spacing:-.01em}
header.site a.home:hover{color:var(--accent);text-decoration:none}
header.site .tag{color:var(--muted);font-size:.82rem;
letter-spacing:.02em;text-transform:uppercase}
header.site nav.top{display:flex;align-items:center;gap:1rem}
header.site nav.top a{color:var(--muted);font-size:.85rem;
font-weight:500}
header.site nav.top a:hover{color:var(--accent)}
button.theme,button.rand{font-family:var(--sans);cursor:pointer;
color:var(--muted);background:var(--surface);border:1px solid var(--border);
border-radius:999px;width:2rem;height:2rem;font-size:.95rem;line-height:1;
padding:0;display:inline-flex;align-items:center;justify-content:center;
transition:color .12s ease,border-color .12s ease}
button.theme:hover,button.rand:hover{color:var(--accent);
border-color:var(--accent)}
button.theme:focus-visible,button.rand:focus-visible{
outline:2px solid var(--accent);outline-offset:2px}

ul.index{list-style:none;padding:0;margin:1.4rem 0 0;
border-top:1px solid var(--border-soft)}
ul.index li{margin:0;border-bottom:1px solid var(--border-soft)}
ul.index li a{display:flex;align-items:baseline;gap:1rem;
justify-content:space-between;padding:.7rem .2rem;color:var(--fg);
font-family:var(--sans);font-size:1.02rem;font-weight:500;
transition:padding-left .12s ease,color .12s ease}
ul.index li a:hover{color:var(--accent);text-decoration:none;
padding-left:.5rem}
ul.index .idate{color:var(--muted);font-size:.8rem;font-weight:600;
font-variant-numeric:tabular-nums;white-space:nowrap;flex:none}

article{font-family:var(--serif)}
article > h1:first-child,h1.page-title{font-family:var(--sans);
font-size:2.35rem;line-height:1.15;letter-spacing:-.02em;
margin:.2rem 0 .4rem;font-weight:800}
.byline{font-family:var(--sans);color:var(--muted);font-size:.85rem;
margin:0 0 2rem;padding-bottom:1.4rem;border-bottom:1px solid var(--border-soft)}
h1,h2,h3,h4,h5,h6{font-family:var(--sans);line-height:1.25;
letter-spacing:-.015em;color:var(--fg);scroll-margin-top:1.5rem;
position:relative}
h1{font-size:2.1rem;margin:2.6rem 0 1rem;font-weight:800}
h2{font-size:1.6rem;margin:2.6rem 0 .9rem;font-weight:700;
padding-bottom:.3rem;border-bottom:1px solid var(--border-soft)}
h3{font-size:1.28rem;margin:2rem 0 .7rem;font-weight:700}
h4{font-size:1.08rem;margin:1.7rem 0 .6rem;font-weight:700}
h5{font-size:.95rem;margin:1.5rem 0 .5rem;font-weight:700;
text-transform:uppercase;letter-spacing:.05em;color:var(--fg-soft)}
h6{font-size:.85rem;margin:1.4rem 0 .5rem;font-weight:700;
text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
p{margin:1.05rem 0;color:var(--fg-soft)}
article p{font-size:1.06rem}
strong{color:var(--fg);font-weight:700}
ul,ol{margin:1.05rem 0;padding-left:1.5rem}
li{margin:.4rem 0;color:var(--fg-soft)}
li::marker{color:var(--muted)}
img{max-width:100%;height:auto;border-radius:6px}
figure{margin:1.8rem 0;text-align:center}
figcaption{font-family:var(--sans);font-size:.85rem;color:var(--muted);
margin-top:.5rem}

code{font-family:var(--mono);font-size:.86em;background:var(--code-bg);
color:var(--code-fg);padding:.15em .4em;border-radius:4px;
border:1px solid var(--border-soft)}
pre{background:var(--code-bg);color:var(--code-fg);padding:1.1rem 1.25rem;
border-radius:8px;border:1px solid var(--border-soft);overflow:auto;
font-size:.86rem;line-height:1.6;margin:1.5rem 0}
pre code{background:none;border:none;padding:0;font-size:1em}
blockquote{margin:1.6rem 0;padding:.3rem 0 .3rem 1.4rem;
border-left:3px solid var(--accent);color:var(--muted);
font-style:italic}
blockquote p{color:var(--muted)}
hr{border:none;border-top:1px solid var(--border);margin:2.6rem 0}
table{width:100%;border-collapse:collapse;margin:1.6rem 0;
font-family:var(--sans);font-size:.92rem}
th,td{text-align:left;padding:.55rem .8rem;border-bottom:1px solid var(--border)}
th{font-weight:700;color:var(--fg);border-bottom:2px solid var(--border)}
tr:hover td{background:var(--code-bg)}

.lede{font-family:var(--sans);color:var(--muted);font-size:.85rem;
margin:0 0 .6rem;letter-spacing:.04em;text-transform:uppercase}
.intro{font-size:1.05rem;color:var(--muted);max-width:38rem;
margin:.4rem 0 2.6rem}
ul.index li:last-child{border-bottom:none}
ul.index .ititle{flex:1 1 auto}

.crumb{font-family:var(--sans);font-size:.85rem;color:var(--muted);
margin:0 0 1.8rem}
.crumb a{color:var(--muted);font-weight:500}
.crumb a:hover{color:var(--accent)}

nav.toc{background:var(--surface);border:1px solid var(--border);
border-radius:10px;padding:1.1rem 1.4rem;margin:0 0 2.4rem;
font-family:var(--sans);box-shadow:var(--shadow)}
nav.toc .toc-title{font-weight:700;font-size:.78rem;
text-transform:uppercase;letter-spacing:.07em;color:var(--muted);
margin-bottom:.7rem}
nav.toc ul,nav.toc ol{list-style:none;margin:0;padding:0}
nav.toc li{margin:.12rem 0;font-size:.92rem}
nav.toc a{display:block;padding:.22rem .5rem;border-radius:5px;
color:var(--fg-soft);border-left:2px solid transparent}
nav.toc a:hover{background:var(--code-bg);color:var(--accent);
border-left-color:var(--accent);text-decoration:none}
nav.toc .lvl-1{font-weight:600}
nav.toc .lvl-2{padding-left:.9rem}nav.toc .lvl-3{padding-left:1.9rem}
nav.toc .lvl-4{padding-left:2.9rem}nav.toc .lvl-5{padding-left:3.9rem}
nav.toc .lvl-6{padding-left:4.9rem}

.anchor{position:absolute;left:-1.1rem;color:var(--border);
text-decoration:none;font-weight:400;opacity:0;font-size:.85em;
padding:0 .25rem;transition:opacity .12s ease}
h1:hover .anchor,h2:hover .anchor,h3:hover .anchor,h4:hover .anchor,
h5:hover .anchor,h6:hover .anchor{opacity:1}
.anchor:hover{color:var(--accent)}
@media (max-width:640px){.anchor{display:none}}

.pager{display:flex;justify-content:space-between;gap:1rem;
flex-wrap:wrap;margin:3rem 0 0;padding-top:1.6rem;
border-top:1px solid var(--border);font-family:var(--sans)}
.pager a{display:inline-flex;flex-direction:column;max-width:48%;
font-size:.95rem;font-weight:600}
.pager a.next{text-align:right;margin-left:auto}
.pager a span{font-size:.72rem;font-weight:600;color:var(--muted);
text-transform:uppercase;letter-spacing:.05em}

div.ongo-math{overflow-x:auto;overflow-y:hidden;margin:1.5rem 0;
padding:.2rem 0}
.katex{font-size:1.05em}
footer.site{margin-top:4.5rem;padding-top:1.3rem;
border-top:1px solid var(--border);color:var(--muted);
font-size:.8rem;font-family:var(--sans);line-height:1.6}
.embed{width:100%;height:82vh;border:1px solid var(--border);
border-radius:8px;background:var(--surface)}

html{scroll-behavior:smooth}
@media (prefers-reduced-motion:reduce){
html{scroll-behavior:auto}
*{transition:none!important}}

@media print{
body{background:#fff;color:#000;font-size:11pt}
.wrap{max-width:100%;padding:0}
header.site nav.top,nav.toc,.pager,.anchor,.crumb{display:none}
a{color:#000;text-decoration:underline}
pre,code{background:#f5f5f5;border:1px solid #ddd}
h2,h3{page-break-after:avoid}
pre,blockquote,figure,table{page-break-inside:avoid}}
"""


def slugify(text, fallback):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:80] if s else fallback


def _invert_date(iso):
    """Map a `YYYY-MM-DD HH:MM:SS` string to a key that sorts DESCENDING.

    Each digit d is replaced by (9 - d); non-digits pass through. A later
    timestamp thus yields a lexicographically smaller key, so a single
    ascending sort places newer items first. Inputs are ken's 19-char
    `YYYY-MM-DD HH:MM:SS` `created_at` (or the `0000-00-00 00:00:00`
    sentinel for missing rows), so the mapping is length-uniform and
    ordering is total.
    """
    return "".join(
        str(9 - int(ch)) if ch.isdigit() else ch for ch in (iso or "")
    )


def render_toc(toc, title="Contents"):
    """Render a heading list (level, anchor_id, text) as an in-page nav.

    Used both for the per-article TOC (built from Markdown headings) and,
    with synthetic entries, for the index "Contents" topic nav. Returns
    "" for an empty / single-heading list (a TOC of one is just noise).
    """
    if len(toc) < 2:
        return ""
    parts = ['<nav class="toc">',
             '<div class="toc-title">%s</div>' % html.escape(title), "<ul>"]
    for level, anchor_id, text in toc:
        parts.append(
            '<li class="lvl-%d"><a href="#%s">%s</a></li>'
            % (level, html.escape(anchor_id, quote=True), html.escape(text))
        )
    parts.append("</ul></nav>")
    return "\n".join(parts)


def page_shell(site_title, body, crumb_html, depth, with_katex=False,
               page_title=None, item_paths=None):
    up = "../" * depth
    # Build-time-emitted list of every published item page URL, made
    # relative to THIS page (depth-correct prefix). Order is whatever the
    # caller passes (global_order — deterministic), so the emitted array is
    # byte-stable across runs; the random pick happens client-side at click
    # time. json.dumps gives a safe, properly-escaped JS array literal.
    rel_items = [up + p for p in (item_paths or [])]
    items_json = json.dumps(rel_items, separators=(",", ":"), sort_keys=False)
    rand_init = (
        '<script>window.__ONGO_ITEMS__=%s;</script>' % items_json
    )
    katex_head = ""
    katex_tail = ""
    if with_katex:
        katex_head = (
            '<link rel="stylesheet" href="%sassets/katex/katex.min.css">'
            % up
        )
        katex_tail = (
            '<script defer src="%sassets/katex/katex.min.js"></script>\n'
            '<script defer>%s</script>' % (up, KATEX_RUNTIME_JS)
        )
    doc_title = site_title
    if page_title and page_title != site_title:
        doc_title = "%s — %s" % (page_title, site_title)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="generator" content="ongo site build">
<title>{html.escape(doc_title)}</title>
<script>{THEME_INIT_JS}</script>
{rand_init}
<style>{CSS}</style>
{katex_head}
</head>
<body>
<div class="wrap">
<header class="site">
<div class="brand">
<a class="home" href="{up}index.html">{html.escape(site_title)}</a>
<div class="tag">Research notes</div>
</div>
<nav class="top">
<a href="{up}index.html">Index</a>
<a href="{up}experiments.html">Experiments</a>
<a href="{up}digests.html">Digests</a>
<button id="rand-article" class="rand" type="button"
aria-label="Go to a random article">&#127922;</button>
<button id="theme-toggle" class="theme" type="button"
aria-label="Toggle color theme">&#9728;</button>
</nav>
</header>
{crumb_html}
{body}
<footer class="site">
<p>This is an <strong>ongo</strong>. There are many like it, but this one is
mine. ongo is an open, self-hostable autonomous research agent — anyone can
run their own, with their own topics, sources, and publishing rules. Source
&amp; documentation:
<a href="https://github.com/zomglings/ongo" target="_blank" rel="noopener">github.com/zomglings/ongo</a>.</p>
<p>Generated by <strong>ongo site build</strong> from the kendb publish set
(kind:&nbsp;<code>ongo-web</code>). Static and self-contained — no external
runtime assets. Math typeset with locally vendored KaTeX.</p>
</footer>
</div>
<script>{THEME_TOGGLE_JS}</script>
<script>{RANDOM_JS}</script>
{katex_tail}
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# QA self-check (post-generation defect scan)
# --------------------------------------------------------------------------- #

# An unrendered Markdown link left as literal text: a `[...]` immediately
# followed by `(...)` that was NOT turned into an <a> (a real anchor never
# leaves the literal `](` sequence in the output).
_UNRENDERED_LINK = re.compile(r"\[[^\]\n]{0,300}\]\([^)\n]{1,400}\)")
# A raw Markdown block line that leaked verbatim into the HTML body: a line
# that still starts with `#` (ATX heading) or `**` (bold) unprocessed. Only
# flagged outside <pre>/<code>, checked on the raw file with a coarse rule.
_RAW_MD_LINE = re.compile(r"(?m)^(#{1,6}\s+\S|\*\*\S)")

# Each QA signature: (label, test) where test(text) -> count of offences.
def _qa_signatures():
    def _count_substr(s):
        return lambda t: t.count(s)

    def _count_re(rx):
        return lambda t: len(rx.findall(t))

    return [
        ("leaked-link-target `](http`", _count_substr("](http")),
        ("double-escaped `&amp;amp;`", _count_substr("&amp;amp;")),
        ("double-escaped `&amp;quot;`", _count_substr("&amp;quot;")),
        ("double-escaped `&amp;lt;`", _count_substr("&amp;lt;")),
        ("double-escaped `&amp;gt;`", _count_substr("&amp;gt;")),
        ("double-escaped `&amp;#`", _count_substr("&amp;#")),
        ("literal escaped currency `\\$`", _count_substr("\\$")),
        ("unrendered markdown link `[..](..)`",
         _count_re(_UNRENDERED_LINK)),
        ("raw leading `#`/`**` markdown line", _count_re(_RAW_MD_LINE)),
    ]


def _qa_scan_html(text):
    """Return a list of (label, count) for every defect signature present.

    Operates on the *body* of a generated page only (between <body> and
    </body>) so vendored CSS/JS or the doctype can never trip a signature.
    Inline <code>/<pre> spans are blanked first so a code sample that
    legitimately shows `](http` or `\\$` is not a false positive.
    """
    m = re.search(r"<body[^>]*>(.*)</body>", text, re.DOTALL)
    body = m.group(1) if m else text
    # Blank code/pre content (keep tags so offsets/structure are sane).
    body = re.sub(
        r"<pre\b[^>]*>.*?</pre>", "<pre></pre>", body, flags=re.DOTALL
    )
    body = re.sub(
        r"<code\b[^>]*>.*?</code>", "<code></code>", body, flags=re.DOTALL
    )
    hits = []
    for label, test in _qa_signatures():
        c = test(body)
        if c:
            hits.append((label, c))
    return hits


def run_qa(site_dir, log):
    """Scan every produced HTML page for defect signatures.

    Writes site_dir/qa-report.txt listing each offending file + per-signature
    counts (or a clean PASS line), appends a single PASS/FAIL summary to
    `log`, and returns (clean_pages, total_pages, total_defects). Never
    raises and never fails the build — the report just makes regressions
    visible.
    """
    pages = []
    idx = os.path.join(site_dir, "index.html")
    if os.path.isfile(idx):
        pages.append(idx)
    dig = os.path.join(site_dir, "digests.html")
    if os.path.isfile(dig):
        pages.append(dig)
    experiments = os.path.join(site_dir, "experiments.html")
    if os.path.isfile(experiments):
        pages.append(experiments)
    items_dir = os.path.join(site_dir, "items")
    if os.path.isdir(items_dir):
        pages.extend(
            sorted(
                os.path.join(items_dir, f)
                for f in os.listdir(items_dir)
                if f.endswith(".html")
            )
        )
    report = []
    total_defects = 0
    offending = 0
    for path in pages:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:  # pragma: no cover — defensive
            report.append("ERROR reading %s: %s" % (path, exc))
            continue
        hits = _qa_scan_html(text)
        if hits:
            offending += 1
            rel = os.path.relpath(path, site_dir)
            for label, count in hits:
                total_defects += count
                report.append("%s : %s x%d" % (rel, label, count))
    total = len(pages)
    clean = total - offending
    header = (
        "ongo site build QA report — %d pages scanned, %d clean, %d with defects, "
        "%d total defect hits\n" % (total, clean, offending, total_defects)
    )
    if offending == 0:
        body = "PASS: no markdown-render defect signatures found.\n"
    else:
        body = "FAIL: defect signatures present —\n" + "\n".join(
            report
        ) + "\n"
    try:
        with open(
            os.path.join(site_dir, "qa-report.txt"), "w", encoding="utf-8"
        ) as fh:
            fh.write(header + "\n" + body)
    except OSError:  # pragma: no cover — never fail the build on report I/O
        pass
    summary = "QA: %s (%d/%d pages clean, %d defect hits)" % (
        "PASS" if offending == 0 else "FAIL",
        clean,
        total,
        total_defects,
    )
    log.append(summary)
    return clean, total, total_defects, (offending == 0)


def build(args):
    """Generate a complete site tree and replace the previous tree safely.

    Build into a sibling directory, then use same-filesystem renames for a
    staged replacement. If installation of the new tree fails after moving the
    old tree, ``_build_into`` restores the old tree. There is a tiny name gap
    between the two renames, but no half-written published tree.
    """
    out_dir = os.path.abspath(args.out)
    parent = os.path.dirname(out_dir) or "."
    os.makedirs(parent, exist_ok=True)
    work_dir = tempfile.mkdtemp(
        prefix=f".{os.path.basename(out_dir)}.tmp-", dir=parent
    )
    # mkdtemp intentionally creates 0700 directories. The completed tree is
    # static publication output and may be served by a different principal,
    # so preserve the legacy generator's traversable root mode.
    os.chmod(work_dir, 0o755)
    backup_dir = tempfile.mkdtemp(
        prefix=f".{os.path.basename(out_dir)}.old-", dir=parent
    )
    try:
        return _build_into(args, out_dir, work_dir, backup_dir)
    finally:
        # Never leave the temp dir behind. On success os.replace already
        # consumed work_dir (this is a no-op); on any exception it cleans
        # the partial tree. ignore_errors so cleanup never masks the real
        # failure being propagated.
        if os.path.isdir(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
        # An empty directory is the unused reservation. A nonempty directory
        # or symlink can be the prior live site after a failed rollback; leave
        # it in place rather than risking data loss while masking the failure.
        if os.path.isdir(backup_dir) and not os.path.islink(backup_dir):
            try:
                os.rmdir(backup_dir)
            except OSError:
                pass


def _build_into(args, out_dir, work_dir, old_dir):
    ken = resolve_ken(args.ken)
    db_path = resolve_db_path(ken, args.db)

    # These trees are recursively removed during staged replacement. Validate
    # the admin keyring before choosing the legacy or mixed-access renderer so
    # a public-only build cannot bypass the data-loss guard.
    from .access import resolve_keyring_path, validate_keyring_location

    validate_keyring_location(
        resolve_keyring_path(getattr(args, "keyring", None)),
        output=out_dir,
        backup=old_dir,
        staging=work_dir,
    )

    log = []
    log.append(f"ken: {ken}")
    log.append(f"kendb: {db_path}")
    log.append(f"out: {out_dir}")
    log.append(f"work: {work_dir}")

    markers = ken_list(ken, db_path, "ongo-web")
    log.append(f"ongo-web markers: {len(markers)}")

    view = KenView(ken, db_path)

    # Resolve every marker to a published item.
    items = {}  # source_pub_id -> item dict
    used_slugs = set()

    def claim_slug(display, pub_id, prefix=""):
        base = slugify(prefix + display, pub_id[:8])
        candidate = base
        if candidate in used_slugs:
            candidate = f"{base}-{pub_id[:8]}"
        suffix = 2
        while candidate in used_slugs:
            candidate = f"{base}-{pub_id[:8]}-{suffix}"
            suffix += 1
        used_slugs.add(candidate)
        return candidate

    for m in markers:
        ref = m.get("key") or ""
        nav_title = (m.get("title") or "").strip()
        if not ref:
            log.append(
                f"  WARNING: ongo-web {m['id']} has no key — skipping"
            )
            continue

        pub = resolve_publication_reference(view, ref)
        if pub is None:
            log.append(
                f"  WARNING: ongo-web key {ref!r} resolves to no "
                f"publication — skipping"
            )
            continue
        if pub["kind"] == "ongo-web":
            log.append(
                f"  WARNING: ongo-web {m['id']} points at another "
                f"ongo-web marker — skipping"
            )
            continue
        if pub["kind"] == "ongo-access-key":
            log.append(
                f"  WARNING: ongo-web {m['id']} points at access-key "
                "metadata, which is never publishable — skipping"
            )
            continue
        if pub["id"] in items:
            log.append(
                f"  WARNING: multiple ongo-web markers reference {pub['id']} — "
                "using the first marker"
            )
            continue

        source = resolve_source(view, pub, log, ken, db_path)
        if source is None:
            log.append(
                f"  WARNING: could not resolve a body for "
                f"{pub['id']} (key={pub['key']!r}) — skipping"
            )
            continue

        display = nav_title or (pub["title"] or "Untitled")[:120]
        slug = claim_slug(display, pub["id"])
        # Derive a deterministic date from `created_at`. The column
        # may be absent from a Row when keyed differently; access
        # defensively.
        try:
            created_at = pub["created_at"]
        except (IndexError, KeyError):
            created_at = None
        # sort_ts == display label == created_at unchanged: ken's
        # `YYYY-MM-DD HH:MM:SS` already sorts lexicographically and is
        # what we want shown in the index. The fallback string sinks
        # missing-created_at rows to the bottom of the global order.
        sort_ts = created_at or "0000-00-00 00:00:00"
        date_label = created_at or ""
        items[pub["id"]] = {
            "pub_id": pub["id"],
            "display": display,
            "slug": slug,
            "source": source,
            "date_label": date_label,
            "sort_ts": sort_ts,
            "page_path": f"items/{slug}.html",
            "list_rank": pub.get("list_rank", len(view.rows)),
            "is_experiment": pub["kind"] == "ongo-experiment",
        }

    # ongo-digest publications are a first-class site view — surfaced on
    # the Digests tab (digests.html) without needing an ongo-web marker.
    # Each digest gets its own item page under items/<slug>.html via the
    # normal render pipeline. The body lives on a related-to `note`
    # (fetch_digest_body); if no related note is found we fall back to
    # the standard resolver chain (title-only body).
    digest_pubs = ken_list(ken, db_path, "ongo-digest")
    log.append(f"ongo-digest publications: {len(digest_pubs)}")
    digest_pub_ids = set()
    for d in digest_pubs:
        pub_id = d.get("id")
        if not pub_id:
            continue
        pub = fetch_publication(view, pub_id)
        if pub is None:
            continue
        # Skip a digest already published via an ongo-web marker (unlikely,
        # but avoids clobbering the marker-driven item).
        if pub["id"] in items:
            continue
        body, content_source_ids = fetch_digest_body(view, pub["id"])
        if not body:
            # Fall back to the standard chain, but keep the digest visible
            # even title-only rather than skipping it entirely.
            source = resolve_source(view, pub, log, ken, db_path)
            if source is None:
                log.append(
                    f"  WARNING: ongo-digest {pub['id']} has no body "
                    f"(no related-to note, no fallback) — skipping"
                )
                continue
        else:
            source = {"kind": "markdown", "body": body}
        display = (pub["title"] or pub["key"] or "Untitled digest")[:200]
        # Digest slug is `digest-<iso-date>` so URLs are predictable and
        # per-day; the title's long per-topic breakdown would otherwise
        # produce a slug truncated at 80 chars.
        digest_slug_base = pub["key"] or slugify(display, pub["id"][:8])
        slug = claim_slug(digest_slug_base, pub["id"], prefix="digest-")
        try:
            created_at = pub["created_at"]
        except (IndexError, KeyError):
            created_at = None
        sort_ts = created_at or "0000-00-00 00:00:00"
        items[pub["id"]] = {
            "pub_id": pub["id"],
            "display": display,
            "slug": slug,
            "source": source,
            "date_label": created_at or "",
            "sort_ts": sort_ts,
            "page_path": f"items/{slug}.html",
            "is_digest": True,
            "digest_key": pub["key"] or "",
            "content_source_ids": content_source_ids,
            "list_rank": pub.get("list_rank", len(view.rows)),
        }
        digest_pub_ids.add(pub["id"])

    if not items:
        log.append("  no resolvable published items — site will be empty")

    # Single global reverse-chronological order (newest first); ordered by
    # full `YYYY-MM-DD HH:MM:SS` timestamp so multiple items published on
    # the same date sort by time-of-day (newest first). Ties at the same
    # second are broken alphabetically by title (A->Z). This ONE ordering
    # drives both the flat index and the inter-article prev/next pager.
    # `_invert_date` maps the timestamp to a string that sorts in
    # DESCENDING chronological order, so a single ascending sort gives
    # newest-first while the forward (lowercased) title keeps same-second
    # ties A->Z. Deterministic and stable across runs.
    global_order = sorted(
        items.values(),
        key=lambda x: (
            0 if x["sort_ts"] != "0000-00-00 00:00:00" else 1,
            _invert_date(x["sort_ts"])
            if x["sort_ts"] != "0000-00-00 00:00:00"
            else f"{x['list_rank']:012d}",
            x["display"].lower(),
        ),
    )

    from .sealed import build_mixed_site, mixed_site_required

    if mixed_site_required(view, global_order):
        return build_mixed_site(
            args=args,
            out_dir=out_dir,
            old_dir=old_dir,
            work_dir=work_dir,
            log=log,
            view=view,
            items=items,
            global_order=global_order,
            ken=ken,
            db_path=db_path,
        )

    # Build a published-set lookup for cross-link resolution.
    published_ids = set(items.keys())

    def make_link_resolver(depth):
        up = "../" * depth

        def resolver(target):
            # Cross-link to another published note by id/key -> its page.
            tpub = resolve_publication_reference(view, target)
            if tpub is not None and tpub["id"] in published_ids:
                page = items[tpub["id"]]["page_path"]
                return (up + page, False)
            if tpub is not None:
                # Resolves to a real but UNPUBLISHED note: do not leak.
                return ("", True)
            # External link (http/https/mailto) -> keep as-is.
            if re.match(r"^[a-z][a-z0-9+.-]*://", target) or \
                    target.startswith("mailto:"):
                return (target, False)
            # Relative/anchor links -> keep.
            if target.startswith("#") or target.startswith("/"):
                return (target, False)
            return (target, False)

        return resolver

    # --- write output (into the temp dir, then atomic swap) ------------- #
    # Start from a clean temp dir (a stale one from a previously killed
    # build with the same pid would otherwise contaminate the output).
    os.makedirs(os.path.join(work_dir, "items"), exist_ok=True)

    # Vendor KaTeX once into assets/katex/ (build-time fetch allowed; the
    # published site references it via relative paths only). On any failure
    # katex_ok is False and pages degrade to raw `$...$` (logged, no crash).
    katex_ok = vendor_katex(work_dir, log)

    # Per-item pages. prev/next follows the SAME global reverse-chron
    # order as the index (newest first, alpha tie-break), so "Previous"
    # is the newer neighbour and "Next" the older one.
    ordered = global_order
    # One deterministic list of every published item page (build-time
    # order = global_order); page_shell rewrites these to the right
    # relative depth and the random button picks one client-side.
    item_paths = [it["page_path"] for it in global_order]
    for pos, item in enumerate(ordered):
        src = item["source"]
        crumb = (
            '<div class="crumb"><a href="../index.html">Index</a>'
            ' <span aria-hidden="true">/</span> '
            + html.escape(item["display"])
            + "</div>"
        )
        page_uses_katex = False
        if src["kind"] == "pdf":
            pdf_name = os.path.basename(src["path"])
            assets_dir = os.path.join(work_dir, "items", "assets")
            os.makedirs(assets_dir, exist_ok=True)
            shutil.copyfile(src["path"], os.path.join(assets_dir, pdf_name))
            article = (
                f'<h1 class="page-title">{html.escape(item["display"])}</h1>'
                f'<p class="byline">PDF document</p>'
                f'<p><a href="assets/{html.escape(pdf_name)}">'
                f"↓ Download PDF</a></p>"
                f'<iframe class="embed" title='
                f'"{html.escape(item["display"], quote=True)}" '
                f'src="assets/{html.escape(pdf_name)}"></iframe>'
            )
        else:
            resolver = make_link_resolver(depth=1)
            raw = src["body"]
            # Extract math BEFORE Markdown/HTML so TeX is never mangled.
            if katex_ok:
                raw, math_map = extract_math(raw)
            else:
                math_map = {}
            html_body, toc = markdown_to_html(raw, resolver, collect_toc=True)
            if math_map:
                html_body = reinsert_math(html_body, math_map)
                page_uses_katex = "ongo-math" in html_body
            # Ensure a visible title even if body lacks an H1.
            if "<h1" not in html_body:
                html_body = (
                    f'<h1 class="page-title" id="title">'
                    f'{html.escape(item["display"])}</h1>\n'
                    + html_body
                )
            # Per-article TOC from the collected headings (skip a lone H1
            # that just repeats the title — render_toc drops <2 entries).
            toc_for_nav = [
                (lvl, aid, txt)
                for (lvl, aid, txt) in toc
                if not (lvl == 1 and len(toc) and toc[0] == (lvl, aid, txt))
            ] or toc
            toc_html = render_toc(toc_for_nav, "On this page")
            byline = (
                '<p class="byline">A published research note</p>'
            )
            article = (
                byline + "\n" + toc_html + "\n" + html_body
                if toc_html
                else byline + "\n" + html_body
            )
        # Prev / next inter-article navigation.
        pager_parts = []
        if pos > 0:
            prev = ordered[pos - 1]
            pager_parts.append(
                '<a class="prev" href="../%s"><span>← Previous</span>%s</a>'
                % (
                    html.escape(prev["page_path"], quote=True),
                    html.escape(prev["display"]),
                )
            )
        if pos < len(ordered) - 1:
            nxt = ordered[pos + 1]
            pager_parts.append(
                '<a class="next" href="../%s"><span>Next →</span>%s</a>'
                % (
                    html.escape(nxt["page_path"], quote=True),
                    html.escape(nxt["display"]),
                )
            )
        pager = (
            '<nav class="pager">' + "".join(pager_parts) + "</nav>"
            if pager_parts
            else ""
        )
        body = "<article>\n" + article + "\n</article>\n" + pager
        page = page_shell(
            args.site_title, body, crumb, depth=1,
            with_katex=(katex_ok and page_uses_katex),
            page_title=item["display"],
            item_paths=item_paths,
        )
        with open(
            os.path.join(work_dir, item["page_path"]),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write(page)

    # Index page: a SINGLE GLOBAL FLAT LIST of every published item.
    # No topic grouping, no per-topic nav. Rows are in `global_order`
    # (reverse-chronological, newest first; ties A->Z by title) and each
    # row shows the item's derived date.
    total_items = len(items)
    index_body = [
        '<p class="lede">Research notes</p>',
        '<h1 class="page-title">' + html.escape(args.site_title) + "</h1>",
    ]
    if not items:
        index_body.append(
            '<p class="intro">No published items yet. Mark content with '
            "<code>ken add ongo-web -k &lt;note-id&gt; "
            "--title &quot;…&quot;</code>.</p>"
        )
    else:
        index_body.append(
            '<p class="intro">%d published note%s, newest first.</p>'
            % (total_items, "" if total_items == 1 else "s")
        )
        index_body.append('<ul class="index">')
        for item in global_order:
            index_body.append(
                '<li><a href="%s"><span class="ititle">%s</span>'
                '<span class="idate">%s</span></a></li>'
                % (
                    html.escape(item["page_path"]),
                    html.escape(item["display"]),
                    html.escape(item["date_label"]),
                )
            )
        index_body.append("</ul>")

    with open(
        os.path.join(work_dir, "index.html"), "w", encoding="utf-8"
    ) as fh:
        fh.write(
            page_shell(
                args.site_title, "\n".join(index_body), "", depth=0,
                item_paths=item_paths,
            )
        )

    # Experiments tab: only explicitly marked experiment roots. Attempt,
    # result, and artifact records never inherit the root marker.
    experiment_items = [it for it in global_order if it.get("is_experiment")]
    experiments_body = [
        '<p class="lede">Reviewed experiment protocols</p>',
        '<h1 class="page-title">Experiments</h1>',
    ]
    if not experiment_items:
        experiments_body.append(
            '<p class="intro">No experiments are published. Add an '
            '<code>ongo-web</code> marker referencing an experiment root to '
            'publish its protocol and canonical manifest.</p>'
        )
    else:
        experiments_body.append(
            '<p class="intro">%d explicitly published experiment%s.</p>'
            % (
                len(experiment_items),
                "" if len(experiment_items) == 1 else "s",
            )
        )
        experiments_body.append('<ul class="index">')
        for item in experiment_items:
            experiments_body.append(
                '<li><a href="%s"><span class="ititle">%s</span>'
                '<span class="idate">%s</span></a></li>'
                % (
                    html.escape(item["page_path"]),
                    html.escape(item["display"]),
                    html.escape(item["date_label"]),
                )
            )
        experiments_body.append("</ul>")

    with open(
        os.path.join(work_dir, "experiments.html"), "w", encoding="utf-8"
    ) as fh:
        fh.write(
            page_shell(
                args.site_title,
                "\n".join(experiments_body),
                "",
                depth=0,
                page_title="Experiments",
                item_paths=item_paths,
            )
        )

    # Digests tab: reverse-chron flat list of every ongo-digest item.
    # Rendered even when empty so the nav link always resolves; the page
    # then shows a friendly empty state instead of a 404.
    digest_items = [it for it in global_order if it.get("is_digest")]
    digests_body = [
        '<p class="lede">Daily arxiv digests</p>',
        '<h1 class="page-title">Digests</h1>',
    ]
    if not digest_items:
        digests_body.append(
            '<p class="intro">No digests yet. Seed an '
            '<code>ongo-arxiv-topic</code> and wait for '
            '<code>ongo arxiv sweep</code> to publish one.</p>'
        )
    else:
        digests_body.append(
            '<p class="intro">%d digest%s, newest first. One per daily '
            'arxiv sweep with new preprints.</p>'
            % (len(digest_items), "" if len(digest_items) == 1 else "s")
        )
        digests_body.append('<ul class="index">')
        for item in digest_items:
            digests_body.append(
                '<li><a href="%s"><span class="ititle">%s</span>'
                '<span class="idate">%s</span></a></li>'
                % (
                    html.escape(item["page_path"]),
                    html.escape(item["display"]),
                    html.escape(item["date_label"]),
                )
            )
        digests_body.append("</ul>")

    with open(
        os.path.join(work_dir, "digests.html"), "w", encoding="utf-8"
    ) as fh:
        fh.write(
            page_shell(
                args.site_title, "\n".join(digests_body), "", depth=0,
                page_title="Digests",
                item_paths=item_paths,
            )
        )

    # Drop the KaTeX download scratch dir so it never reaches the published
    # tree (the vendored copy already lives at assets/katex/). Build-time
    # only artifact; runtime stays self-contained and minimal.
    katex_scratch = os.path.join(work_dir, "_katex")
    if os.path.isdir(katex_scratch):
        shutil.rmtree(katex_scratch, ignore_errors=True)

    # Build log (also written into the site dir for auditability).
    log.append(f"published items: {len(items)}")
    log.append("index: single global reverse-chron flat list")
    log.append(f"katex: {'vendored' if katex_ok else 'unavailable (raw)'}")

    # QA self-check: scan the freshly generated tree (in work_dir, so the
    # report ships in the staged replacement tree) for markdown-render
    # defect signatures. Writes qa-report.txt, appends a PASS/FAIL line to
    # the log. Never fails the build — visibility only.
    qa_clean, qa_total, qa_defects, qa_pass = run_qa(work_dir, log)

    log_text = "\n".join(log) + "\n"
    with open(
        os.path.join(work_dir, "build.log"), "w", encoding="utf-8"
    ) as fh:
        fh.write(log_text)

    # --- staged replacement with rollback ------------------------------ #
    from .sealed import install_tree

    install_tree(work_dir, out_dir, old_dir)

    sys.stdout.write(log_text)
    # Clear, unmissable PASS/FAIL line on stdout (also in build.log via the
    # appended QA summary). The build still succeeds either way.
    sys.stdout.write(
        "QA SELF-CHECK: %s — %d/%d pages clean, %d defect hits "
        "(see qa-report.txt)\n"
        % (
            "PASS" if qa_pass else "FAIL",
            qa_clean,
            qa_total,
            qa_defects,
        )
    )
    return 0


def main(argv=None):
    p = OngoArgumentParser(
        prog="ongo site build",
        description="Generate a static site from the kendb ongo-web "
        "publish set.",
    )
    p.add_argument(
        "--ken",
        default=None,
        help="path to Ken (then ONGO_KEN, plugin data, PATH)",
    )
    p.add_argument(
        "--db",
        default=None,
        help="path to Ken database (then ONGO_KEN_DB, Ken default)",
    )
    p.add_argument(
        "--out",
        default="./site",
        help="output directory (default: ./site)",
    )
    p.add_argument(
        "--site-title",
        default="Ongo Research",
        help='site title / header (default: "Ongo Research")',
    )
    p.add_argument(
        "--base-url",
        default="",
        help="optional base URL (informational; pages use relative links)",
    )
    p.add_argument(
        "--keyring",
        default=None,
        help="admin keyring path (then ONGO_SITE_KEYRING, plugin data)",
    )
    args = p.parse_args(argv)
    return build(args)


if __name__ == "__main__":
    raise SystemExit(main())
