#!/usr/bin/env python3
"""`ongo slack poll` — gap-free Slack poll for the Ongo tick loop.

Usage:
    ongo slack poll <CHANNEL> <LAST_USER_TS>
        [--speaker-prefix <PREFIX>] [--speaker-user-id <USER_ID>]

Prints JSON. On success:
    {"status":"ok","total_seen":N,"user_count":N,
     "newest_user_ts":"<ts>","user_messages":[{"ts","text"},...]}
On read failure (rate limit / API error / unparseable):
    {"status":"error","error":"<msg>","user_count":0,
     "newest_user_ts":"<LAST_USER_TS unchanged>","user_messages":[]}
THE CALLER MUST CHECK status:
  * status=="error"     -> could NOT read. Report the failure and back
    off; never treat it as an idle/all-clear tick. That conflation is
    how the loop goes silently deaf.
  * status=="ok"        -> window fully drained; safe to advance.

Bug history (each fix exposed the next):
  1. Frozen cursor (advanced only on user msgs) -> deaf on idle channel.
  2. Bot-contaminated cursor (advanced on bot sends) -> user msgs before
     a later bot send filtered out.
  3. Three reads/poll (recent+--since+--after) tripled
     conversations.history volume; with 1-min fast-mode polling the
     token hit HTTP-429 'ratelimited'. And every read was try/except ->
     [] so a hard 429 read as "0 msgs, all clear" -> deaf again.
  4. A capped newest slice silently dropped older messages, while the
     attempted rewind replayed the same slice forever. The poller now
     follows Slack's opaque next_cursor until the history window is
     exhausted, merges every page, and only then returns status=="ok".

In force now:
  * Gate strictly on LAST_USER_TS (advances only on processed user
    messages, never on bot traffic).
  * Use `--since LAST_USER_TS` and follow every response_metadata.next_cursor
    page. A Slack ts is a valid --since value; `--after` is not used.
  * Distinguish FAILURE from EMPTY by PARSING, never by substring.
    Earlier this code did `if "ratelimited" in (stdout+stderr)` — but a
    SUCCESSFUL conversations.history response contains message *text*,
    and ongo's own status messages contain the words "ratelimited" /
    "SlackApiError". That made every successful read self-misclassify
    as an error, freezing the loop in fake backoff with real queued
    messages. Detection now: parse JSON; Slack success is
    `{"ok":true,...}` (or any dict with "messages"); only `{"ok":false}`
    -> error(data["error"]); only UNPARSEABLE output -> inspect stderr
    for Traceback/SlackApiError. Message bodies are never scanned.
"""
import json
import subprocess
import sys
import time
from decimal import Decimal, InvalidOperation

from .errors import OngoArgumentParser

LIM = "200"
# Each individual page gets four attempts (initial + three retries).
BACKOFFS = (5, 15, 30)


def _read_once(channel, last_user_ts, cursor=None):
    # `--order asc` is the canonical ascending-by-ts shape that ongo's
    # tick loop expects. Cursor pagination requires slack-clacks >= 0.14.1.
    command = [
        "clacks", "read", "-c", channel, "--since", last_user_ts,
        "-l", LIM, "--order", "asc",
    ]
    if cursor:
        command.extend(("--cursor", cursor))
    try:
        p = subprocess.run(
            command,
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        return None, None, f"subprocess: {e}"
    out, err = p.stdout.strip(), p.stderr.strip()
    # Parse first. NEVER substring-scan the blob — message bodies (incl.
    # ongo's own status posts) legitimately contain "ratelimited" etc.
    try:
        data = json.loads(out)
    except Exception:
        # Not JSON at all -> a real failure. Inspect stderr only.
        if "ratelimited" in err:
            return None, None, "ratelimited"
        if "SlackApiError" in err or "Traceback" in err:
            return None, None, "api-error"
        return None, None, f"unparseable: {(err or out)[:120]}"
    if isinstance(data, dict) and data.get("ok") is False:
        return None, None, data.get("error", "api-error")
    if isinstance(data, dict) and "messages" in data:
        metadata = data.get("response_metadata") or {}
        next_cursor = metadata.get("next_cursor") or ""
        if data.get("has_more") and not next_cursor:
            return None, None, "pagination response omitted next_cursor"
        return data["messages"], next_cursor, None
    if isinstance(data, list):
        return data, "", None
    return None, None, "unexpected JSON response"


def _read_page(channel, last_user_ts, cursor):
    messages, next_cursor, error = _read_once(channel, last_user_ts, cursor)
    for delay in BACKOFFS:
        if error is None:
            break
        time.sleep(delay)
        messages, next_cursor, error = _read_once(channel, last_user_ts, cursor)
    return messages, next_cursor, error


def poll(channel, last_user_ts, speaker_prefix="", speaker_user_id=""):
    messages = []
    cursor = None
    seen_cursors = set()
    while True:
        page, next_cursor, error = _read_page(channel, last_user_ts, cursor)
        if error is not None:
            return {
                "status": "error",
                "error": error,
                "total_seen": 0,
                "user_count": 0,
                "newest_user_ts": last_user_ts,
                "user_messages": [],
            }
        messages.extend(page)
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            return {
                "status": "error",
                "error": "pagination cursor repeated",
                "total_seen": 0,
                "user_count": 0,
                "newest_user_ts": last_user_ts,
                "user_messages": [],
            }
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    def is_bot(m):
        # Accept both the loop's [ongo] prefix and the subagent's
        # [ongo, <id>] / [ongo:<id>] prefix; the italics-wrapped _[ongo…
        # variants are also bot-spoken. Text is forgeable, so a configured
        # speaker identity is trusted only when Slack also attributes the
        # message to that exact authenticated user. Third-party bot/app
        # metadata does not identify Ongo and must not suppress a message.
        t = m.get("text", "").lstrip()
        if speaker_prefix:
            if not t.startswith(speaker_prefix):
                return False
            t = t[len(speaker_prefix) :].lstrip()
        marker = t.startswith((
            "[ongo]", "_[ongo]",
            "[ongo,", "_[ongo,",
            "[ongo:", "_[ongo:",
        ))
        if not marker:
            return False
        if speaker_user_id:
            return str(m.get("user") or "") == speaker_user_id
        return not speaker_prefix

    by_timestamp = {
        str(message["ts"]): message for message in messages if message.get("ts")
    }
    try:
        gate = Decimal(str(last_user_ts))
        allm = sorted(
            by_timestamp.values(), key=lambda message: Decimal(str(message["ts"]))
        )
    except (InvalidOperation, TypeError, ValueError):
        return {
            "status": "error",
            "error": "invalid Slack timestamp",
            "total_seen": 0,
            "user_count": 0,
            "newest_user_ts": last_user_ts,
            "user_messages": [],
        }
    # --since is inclusive; strictly exclude the gate message itself.
    users = [
        message
        for message in allm
        if not is_bot(message) and Decimal(str(message["ts"])) > gate
    ]
    newest_user = users[-1]["ts"] if users else last_user_ts

    return {
        "status": "ok",
        "total_seen": len(allm),
        "user_count": len(users),
        "newest_user_ts": newest_user,
        "user_messages": [
            {"ts": m["ts"], "text": m.get("text", "")} for m in users
        ],
    }


def main(argv=None):
    parser = OngoArgumentParser(prog="ongo slack poll")
    parser.add_argument("channel")
    parser.add_argument("last_user_ts")
    parser.add_argument("--speaker-prefix", default="")
    parser.add_argument("--speaker-user-id", default="")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.speaker_prefix and not args.speaker_user_id:
        parser.error("--speaker-user-id is required with --speaker-prefix")
    print(
        json.dumps(
            poll(
                args.channel,
                args.last_user_ts,
                speaker_prefix=args.speaker_prefix,
                speaker_user_id=args.speaker_user_id,
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
