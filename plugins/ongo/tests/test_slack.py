#!/usr/bin/env python3
"""Slack cursor-pagination regression tests."""

from __future__ import annotations

import json
import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "lib"))

from ongo import slack


def message(index, text=None):
    return {
        "ts": f"1700000000.{index:06d}",
        "text": text if text is not None else f"user {index}",
    }


def page(messages, next_cursor=""):
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(
            {
                "ok": True,
                "messages": messages,
                "has_more": bool(next_cursor),
                "response_metadata": {"next_cursor": next_cursor},
            }
        ),
        stderr="",
    )


class SlackPollTests(unittest.TestCase):
    def test_exact_page_is_complete_without_false_truncation(self):
        with mock.patch.object(
            slack.subprocess, "run", return_value=page([message(i) for i in range(200)])
        ) as run:
            result = slack.poll("C123", "1700000000.000000")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["total_seen"], 200)
        self.assertEqual(result["user_count"], 199)
        self.assertEqual(result["newest_user_ts"], "1700000000.000199")
        self.assertEqual(run.call_count, 1)

    def test_cursor_pages_are_merged_oldest_first_without_gaps(self):
        newest = [message(i) for i in range(50, 250)]
        oldest = [message(i) for i in range(50)]
        with mock.patch.object(
            slack.subprocess,
            "run",
            side_effect=(page(newest, "older-page"), page(oldest)),
        ) as run:
            result = slack.poll("C123", "1699999999.999999")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["total_seen"], 250)
        self.assertEqual(result["user_count"], 250)
        self.assertEqual(
            [item["ts"] for item in result["user_messages"]],
            [message(i)["ts"] for i in range(250)],
        )
        self.assertNotIn("--cursor", run.call_args_list[0].args[0])
        second_command = run.call_args_list[1].args[0]
        self.assertEqual(second_command[second_command.index("--cursor") + 1], "older-page")

    def test_repeated_pagination_cursor_is_an_error(self):
        with mock.patch.object(
            slack.subprocess,
            "run",
            side_effect=(page([message(2)], "same"), page([message(1)], "same")),
        ):
            result = slack.poll("C123", "1700000000.000000")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "pagination cursor repeated")
        self.assertEqual(result["newest_user_ts"], "1700000000.000000")
        self.assertEqual(result["user_messages"], [])

    def test_later_page_failure_returns_no_partial_messages(self):
        failure = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps({"ok": False, "error": "ratelimited"}),
            stderr="",
        )
        with mock.patch.object(
            slack.subprocess,
            "run",
            side_effect=(page([message(2)], "older"), failure),
        ), mock.patch.object(slack, "BACKOFFS", ()):
            result = slack.poll("C123", "1700000000.000000")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "ratelimited")
        self.assertEqual(result["total_seen"], 0)
        self.assertEqual(result["user_messages"], [])

    def test_configured_speaker_prefix_filters_host_identity(self):
        messages = [
            message(1, "`[rex]` [ongo] status"),
            message(2, "`[rex]` [ongo, agent-1] Done"),
            message(3, "a real request"),
        ]
        with mock.patch.object(slack.subprocess, "run", return_value=page(messages)):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    slack.main(
                        [
                            "C123",
                            "1700000000.000000",
                            "--speaker-prefix",
                            "`[rex]` ",
                        ]
                    ),
                    0,
                )
        result = json.loads(output.getvalue())
        self.assertEqual(result["user_count"], 1)
        self.assertEqual(result["user_messages"][0]["text"], "a real request")

    def test_unconfigured_speaker_prefix_is_not_silently_trusted(self):
        messages = [message(1, "`[rex]` [ongo] user-supplied text")]
        with mock.patch.object(slack.subprocess, "run", return_value=page(messages)):
            result = slack.poll("C123", "1700000000.000000")
        self.assertEqual(result["user_count"], 1)


if __name__ == "__main__":
    unittest.main()
