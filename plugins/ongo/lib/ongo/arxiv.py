#!/usr/bin/env python3
"""`ongo arxiv sweep` — import new preprints matching seeded topics.

Reads user-seeded topics of kind ``ongo-arxiv-topic`` (key = short slug,
title = arxiv API ``search_query`` expression), queries the arxiv Atom API
for each, dedups against already-published ``arxiv`` entries in kendb,
publishes new hits, links them to the topic, and (optionally) posts a
Slack digest.

Stdlib-only. See SKILL.md ("Daily arxiv sweep") for the tick-loop wiring.

Exit codes:
    0  success (including "zero new")
    2  every topic errored (network hard-down, all queries failed)
    3  ken binary missing or ``ken list`` failed
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .errors import OngoArgumentParser


ARXIV_API = "http://export.arxiv.org/api/query"
USER_AGENT = "ongo-arxiv-sweep/0.4 (+http://ongo.ergodic.xyz)"
HTTP_TIMEOUT = 30
DIGEST_PAPER_CAP = 30

ATOM_NS = "http://www.w3.org/2005/Atom"
ARXIV_NS = "http://arxiv.org/schemas/atom"
NS = {"atom": ATOM_NS, "arxiv": ARXIV_NS}

PUBKIND_DESCRIPTION = (
    "A topic ongo watches for new arxiv preprints. The key is a short "
    "slug; the title is an arxiv-API search_query expression; notes are "
    "free-form context."
)

ONGO_DIGEST_DESCRIPTION = (
    "A summary of one ongo daily-arxiv-sweep run. The key is an ISO date. "
    "The title lists the paper counts by topic. Notes contain the paper "
    "list with links."
)


# ---------- ken discovery ---------------------------------------------------

def find_ken(explicit=None, db=None) -> list[str]:
    """Resolve the selected Ken v3 binary and database."""
    from .ken import resolve_db, resolve_ken

    binary = resolve_ken(explicit)
    return [binary, "-D", resolve_db(binary, db)]


def ken(ken_bin: list[str], *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*ken_bin, *args], capture_output=True, text=True, check=check
    )


def ken_load(ken_bin: list[str], payload: dict) -> dict:
    """Commit a publication graph through one Ken transaction."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", encoding="utf-8", delete=False
    ) as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        path = handle.name
    try:
        result = ken(ken_bin, "load", path)
        return json.loads(result.stdout)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def ken_ensure_pubkind(ken_bin: list[str], kind: str, description: str) -> None:
    """Idempotently ensure a supporting publication kind exists."""
    r = subprocess.run(
        [*ken_bin, "pubkind", "show", kind], capture_output=True, text=True
    )
    if r.returncode == 0:
        return
    subprocess.run(
        [*ken_bin, "pubkind", "add", kind, description],
        capture_output=True, text=True, check=True,
    )


# ---------- arxiv id normalisation & dedup ----------------------------------

def normalize_arxiv_id(raw: str) -> str:
    """Strip ``arXiv:`` prefix and any trailing ``vN`` version suffix.

    ``arXiv:2401.00001v3`` -> ``2401.00001``; ``2401.00001`` -> ``2401.00001``.
    """
    s = raw.strip()
    if s.lower().startswith("arxiv:"):
        s = s[6:]
    # Drop trailing vN (v1..v99): only when the digits after 'v' are all digits.
    if "v" in s:
        head, _, tail = s.rpartition("v")
        if tail.isdigit():
            s = head
    return s


def load_known_arxiv_ids(ken_bin: list[str]) -> set[str]:
    r = subprocess.run(
        [*ken_bin, "list", "--kind", "arxiv"], capture_output=True, text=True
    )
    if r.returncode != 0:
        raise RuntimeError(f"ken list --kind arxiv failed: {r.stderr.strip()}")
    try:
        items = json.loads(r.stdout or "[]")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ken list returned unparseable JSON: {e}")
    return {normalize_arxiv_id(item.get("key", "")) for item in items
            if item.get("key")}


# ---------- Atom parsing ----------------------------------------------------

def _text(elem, path: str) -> str:
    e = elem.find(path, NS)
    if e is None or e.text is None:
        return ""
    return " ".join(e.text.split())


def parse_arxiv_feed(xml_bytes: bytes) -> list[dict]:
    """Parse an arxiv Atom feed into a list of paper dicts.

    Each dict: {id, title, summary, published, authors, primary_category}.
    ``id`` is the bare arxiv id (version stripped); ``published`` is ISO-8601.
    """
    root = ET.fromstring(xml_bytes)
    out: list[dict] = []
    for entry in root.findall("atom:entry", NS):
        raw_id = _text(entry, "atom:id")
        # id URL is http://arxiv.org/abs/<id>[vN]
        bare = raw_id.rsplit("/abs/", 1)[-1] if "/abs/" in raw_id else raw_id
        bare = normalize_arxiv_id(bare)
        if not bare:
            continue
        title = _text(entry, "atom:title")
        summary = _text(entry, "atom:summary")
        published = _text(entry, "atom:published")
        authors = [
            _text(a, "atom:name")
            for a in entry.findall("atom:author", NS)
        ]
        authors = [a for a in authors if a]
        primary = ""
        pc = entry.find("arxiv:primary_category", NS)
        if pc is not None:
            primary = pc.attrib.get("term", "")
        out.append({
            "id": bare,
            "title": title,
            "summary": summary,
            "published": published,
            "authors": authors,
            "primary_category": primary,
        })
    return out


# ---------- window & dedup filters ------------------------------------------

def within_window(published_iso: str, now_ts: float, window_hours: float) -> bool:
    """True if ``published_iso`` falls within [now - window, now].

    Accepts ``Z`` and ``+00:00`` suffixed timestamps. On parse failure
    returns True (fail-open: we would rather keep a paper we can't date
    than silently drop it — the dedup filter still protects downstream).
    """
    if not published_iso:
        return True
    s = published_iso
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now_ts - dt.timestamp()) <= window_hours * 3600.0


def filter_new(entries: list[dict], known: set[str]) -> list[dict]:
    """Drop entries whose (normalised) id is already in ``known``."""
    fresh = []
    for e in entries:
        if e["id"] in known:
            continue
        fresh.append(e)
    return fresh


# ---------- HTTP ------------------------------------------------------------

def fetch_topic(search_query: str, limit: int) -> bytes:
    params = urllib.parse.urlencode({
        "search_query": search_query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": str(limit),
    })
    req = urllib.request.Request(
        f"{ARXIV_API}?{params}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


# ---------- ken writes ------------------------------------------------------

def publish_paper(ken_bin: list[str], entry: dict, topic_key: str,
                  topic_id: str) -> tuple[str, str]:
    """Add arxiv publication, attach an abstract-body note, relate both to
    the topic. Returns (arxiv_pub_id, note_pub_id).
    """
    body = (
        f"Authors: {', '.join(entry['authors'])}\n"
        f"Category: {entry['primary_category']}\n"
        f"Published: {entry['published']}\n"
        f"Topic: {topic_key}\n"
        f"URL: http://arxiv.org/abs/{entry['id']}\n"
        f"\n"
        f"{entry['summary']}\n"
    )
    note_title = f"arxiv:{entry['id']} abstract ({topic_key})"
    loaded = ken_load(
        ken_bin,
        {
            "publications": [
                {
                    "ref": "paper",
                    "kind": "arxiv",
                    "key": entry["id"],
                    "title": entry["title"],
                },
                {
                    "ref": "abstract",
                    "kind": "note",
                    "key": f"ongo-arxiv:{entry['id']}:{topic_key}:abstract",
                    "title": note_title,
                },
            ],
            "relationships": [
                {"subject": "paper", "object": "abstract", "kind": "related-to"},
                {"subject": "paper", "object": topic_id, "kind": "related-to"},
            ],
            "notes": [{"publication": "abstract", "body": body}],
        },
    )
    return loaded["refs"]["paper"], loaded["refs"]["abstract"]


# ---------- digest ----------------------------------------------------------

def build_digest(results: dict[str, list[dict]],
                 errors: dict[str, str]) -> str:
    total = sum(len(v) for v in results.values())
    topics_with_hits = sum(1 for v in results.values() if v)
    lines = [
        f"[ongo] Daily arxiv digest — {total} new preprint"
        f"{'s' if total != 1 else ''} across {topics_with_hits} topic"
        f"{'s' if topics_with_hits != 1 else ''}",
    ]
    shown = 0
    for topic_key, papers in results.items():
        if not papers:
            continue
        lines.append(f"* {topic_key}: {len(papers)}")
        for p in papers:
            if shown >= DIGEST_PAPER_CAP:
                break
            lines.append(
                f"    - {p['title']} — http://arxiv.org/abs/{p['id']}"
            )
            shown += 1
        if shown >= DIGEST_PAPER_CAP:
            lines.append(
                "    (digest capped — see http://ongo.ergodic.xyz for more)"
            )
            break
    if errors:
        lines.append("* errors: " + ", ".join(sorted(errors.keys())))
    return "\n".join(lines)


def publish_ongo_digest(
    ken_bin: list[str],
    results: dict,
    errors: dict,
    iso_date: str,
    iso_time_hhmm: str,
) -> tuple[str, str, str]:
    """Publish one ``ongo-digest`` per sweep run summarising the results.

    Idempotently ensures the ``ongo-digest`` pubkind, then adds a digest
    publication keyed by ISO date (``YYYY-MM-DD``). If a digest already
    exists for that date (rare — dev/testing case), an ``HH:MM`` suffix
    is appended to disambiguate. The paper list body and related note are
    committed in one ``ken load`` transaction so ``ongo site build`` can
    render them. Returns (digest_id, digest_key, title).
    """
    ken_ensure_pubkind(ken_bin, "ongo-digest", ONGO_DIGEST_DESCRIPTION)

    total = sum(len(v) for v in results.values())
    hit_topics = [(k, v) for k, v in results.items() if v]
    per_topic = ", ".join(f"{k} ({len(v)})" for k, v in hit_topics)
    if len(per_topic) > 140:
        per_topic = per_topic[:137] + "..."
    title = (
        f"arxiv digest: {total} new preprint"
        f"{'s' if total != 1 else ''} across {len(hit_topics)} topic"
        f"{'s' if len(hit_topics) != 1 else ''} — {per_topic}"
    )

    # Key = ISO date; suffix HH:MM only if the date is already taken.
    r = subprocess.run(
        [*ken_bin, "list", "--kind", "ongo-digest"],
        capture_output=True, text=True,
    )
    existing_keys: set[str] = set()
    if r.returncode == 0 and r.stdout.strip():
        try:
            for it in json.loads(r.stdout):
                k = it.get("key")
                if k:
                    existing_keys.add(k)
        except json.JSONDecodeError:
            pass
    key = iso_date
    if key in existing_keys:
        key = f"{iso_date}-{iso_time_hhmm}"

    # Body — grouped by topic, one line per paper, abstract-linked.
    lines = [
        f"# Daily arxiv digest — {iso_date}",
        "",
        f"**{total} new preprint"
        f"{'s' if total != 1 else ''} across {len(hit_topics)} topic"
        f"{'s' if len(hit_topics) != 1 else ''}.**",
        "",
    ]
    for topic_key, papers in hit_topics:
        lines.append(f"## {topic_key} ({len(papers)})")
        lines.append("")
        for p in papers:
            cat = p.get("category") or ""
            pub = (p.get("published") or "")[:10]
            meta = ", ".join(x for x in (cat, pub) if x)
            suffix = f" — {meta}" if meta else ""
            lines.append(
                f"- [{p['title']}](http://arxiv.org/abs/{p['id']}){suffix}"
            )
        lines.append("")
    if errors:
        lines.append("## errors")
        lines.append("")
        for topic_key, msg in sorted(errors.items()):
            lines.append(f"- {topic_key}: {msg}")
        lines.append("")
    body = "\n".join(lines)

    loaded = ken_load(
        ken_bin,
        {
            "publications": [
                {
                    "ref": "digest",
                    "kind": "ongo-digest",
                    "key": key,
                    "title": title,
                },
                {
                    "ref": "body",
                    "kind": "note",
                    "key": f"ongo-digest:{key}:body",
                    "title": f"arxiv digest body — {key}",
                },
            ],
            "relationships": [
                {"subject": "digest", "object": "body", "kind": "related-to"}
            ],
            "notes": [{"publication": "body", "body": body}],
        },
    )
    return loaded["refs"]["digest"], key, title


def post_digest(channel: str, message: str) -> bool:
    r = subprocess.run(
        ["clacks", "send", "-c", channel, "-m", message],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.stderr.write(
            f"clacks send failed rc={r.returncode}: {r.stderr}\n"
        )
        return False
    return True


# ---------- main ------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = OngoArgumentParser(
        description="Daily arxiv sweep for ongo-arxiv-topic seeds."
    )
    p.add_argument("--channel", default=None,
                   help="Slack channel id for the digest.")
    p.add_argument("--ken", default=None, help="Path to Ken v3.")
    p.add_argument("--db", default=None, help="Path to the Ken database.")
    p.add_argument("--limit", type=int, default=25,
                   help="Max results per topic per query.")
    p.add_argument("--window-hours", type=float, default=26.0,
                   help="Only include papers published in the last N hours. "
                        "26 gives 2h overlap so a late cron does not miss.")
    p.add_argument("--dry-run", action="store_true",
                   help="No ken writes, no Slack post; print JSON summary.")
    p.add_argument("--no-slack", action="store_true",
                   help="Do ken writes but skip Slack digest.")
    args = p.parse_args(argv)

    ken_bin = find_ken(args.ken, args.db)

    if not args.dry_run:
        ken_ensure_pubkind(ken_bin, "ongo-arxiv-topic", PUBKIND_DESCRIPTION)

    # List topics.
    r = subprocess.run(
        [*ken_bin, "list", "--kind", "ongo-arxiv-topic"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.stderr.write(
            f"ken list --kind ongo-arxiv-topic failed: {r.stderr}\n"
        )
        return 3
    try:
        topics = json.loads(r.stdout or "[]")
    except json.JSONDecodeError as e:
        sys.stderr.write(f"unparseable topic JSON: {e}\n")
        return 3

    if not topics:
        sys.stderr.write("no ongo-arxiv-topic seeds; nothing to do\n")
        print(json.dumps({"topics": 0, "new": 0, "posted": False}))
        return 0

    try:
        known = load_known_arxiv_ids(ken_bin)
    except RuntimeError as e:
        sys.stderr.write(f"{e}\n")
        return 3

    now = time.time()
    results: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}

    for topic in topics:
        topic_key = topic.get("key", "")
        topic_id = topic.get("id", "")
        search_query = topic.get("title", "")
        if not (topic_key and topic_id and search_query):
            errors[topic_key or topic_id or "<unknown>"] = "malformed topic row"
            continue
        try:
            xml_bytes = fetch_topic(search_query, args.limit)
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, OSError) as e:
            sys.stderr.write(f"[{topic_key}] fetch failed: {e}\n")
            errors[topic_key] = f"fetch: {e}"
            continue
        try:
            entries = parse_arxiv_feed(xml_bytes)
        except ET.ParseError as e:
            sys.stderr.write(f"[{topic_key}] parse failed: {e}\n")
            errors[topic_key] = f"parse: {e}"
            continue

        fresh = [
            e for e in filter_new(entries, known)
            if within_window(e["published"], now, args.window_hours)
        ]
        results[topic_key] = []
        for entry in fresh:
            if args.dry_run:
                results[topic_key].append({
                    "id": entry["id"],
                    "title": entry["title"],
                    "url": f"http://arxiv.org/abs/{entry['id']}",
                    "category": entry["primary_category"],
                    "published": entry["published"],
                })
                known.add(entry["id"])
                continue
            try:
                publish_paper(ken_bin, entry, topic_key, topic_id)
            except (subprocess.CalledProcessError, OSError) as e:
                sys.stderr.write(
                    f"[{topic_key}] publish failed for {entry['id']}: {e}\n"
                )
                errors.setdefault(topic_key, f"publish: {e}")
                continue
            known.add(entry["id"])
            results[topic_key].append({
                "id": entry["id"],
                "title": entry["title"],
                "url": f"http://arxiv.org/abs/{entry['id']}",
                "category": entry["primary_category"],
                "published": entry["published"],
            })

    total_new = sum(len(v) for v in results.values())

    # Emit one ongo-digest publication per run (before Slack). Only when
    # there is at least one new paper; empty runs don't create a digest.
    digest_key: str | None = None
    if not args.dry_run and total_new > 0:
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        try:
            _dig_id, digest_key, _dig_title = publish_ongo_digest(
                ken_bin,
                results,
                errors,
                now_dt.strftime("%Y-%m-%d"),
                now_dt.strftime("%H:%M"),
            )
        except (subprocess.CalledProcessError, OSError) as e:
            sys.stderr.write(f"ongo-digest publish failed: {e}\n")

    posted = False
    if (not args.dry_run and not args.no_slack and args.channel
            and total_new > 0):
        posted = post_digest(args.channel, build_digest(results, errors))

    summary = {
        "topics": len(topics),
        "new": total_new,
        "posted": posted,
        "digest_key": digest_key,
        "errors": errors,
    }
    print(json.dumps(summary))

    if errors and len(errors) == len(topics) and total_new == 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
