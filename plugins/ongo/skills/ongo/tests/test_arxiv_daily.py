#!/usr/bin/env python3
"""Unit tests for ongo-arxiv-daily.

Runs offline. Covers:
  * ``parse_arxiv_feed`` on an embedded Atom fixture (2 entries).
  * dedup filter treats ``2401.00001v2`` as duplicate of ``2401.00001``.
  * ``window_hours`` filter drops entries older than the window.

The ``ongo-arxiv-daily`` script has no ``.py`` extension, so we load it via
``importlib``. Its body is guarded by ``if __name__ == "__main__":`` so
importing is side-effect-free.
"""

import importlib.machinery
import importlib.util
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone


_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.normpath(os.path.join(_HERE, "..", "bin", "ongo-arxiv-daily"))


def _load():
    loader = importlib.machinery.SourceFileLoader("ongo_arxiv_daily", _SCRIPT)
    spec = importlib.util.spec_from_loader("ongo_arxiv_daily", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ATOM_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <title>Distributional
      Reinforcement Learning Redux</title>
    <summary>We revisit distributional RL.</summary>
    <published>2026-07-08T10:00:00Z</published>
    <author><name>Alice Example</name></author>
    <author><name>Bob Example</name></author>
    <arxiv:primary_category term="cs.LG"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.00002v3</id>
    <title>Low-Rank Adapters at Scale</title>
    <summary>LoRA at 70B.</summary>
    <published>2026-07-07T18:30:00Z</published>
    <author><name>Carol Example</name></author>
    <arxiv:primary_category term="cs.CL"/>
  </entry>
</feed>
"""


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_parse_two_entries(self):
        entries = self.mod.parse_arxiv_feed(ATOM_FIXTURE)
        self.assertEqual(len(entries), 2)

        e0 = entries[0]
        self.assertEqual(e0["id"], "2401.00001")
        self.assertEqual(e0["title"], "Distributional Reinforcement Learning Redux")
        self.assertEqual(e0["summary"], "We revisit distributional RL.")
        self.assertEqual(e0["published"], "2026-07-08T10:00:00Z")
        self.assertEqual(e0["authors"], ["Alice Example", "Bob Example"])
        self.assertEqual(e0["primary_category"], "cs.LG")

        e1 = entries[1]
        self.assertEqual(e1["id"], "2401.00002")
        self.assertEqual(e1["primary_category"], "cs.CL")
        self.assertEqual(e1["authors"], ["Carol Example"])


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_strip_arxiv_prefix_and_version(self):
        n = self.mod.normalize_arxiv_id
        self.assertEqual(n("arXiv:2401.00001v3"), "2401.00001")
        self.assertEqual(n("2401.00001v1"), "2401.00001")
        self.assertEqual(n("2401.00001"), "2401.00001")
        # Do NOT strip a non-digit suffix that happens to include "v".
        self.assertEqual(n("cs/0501001"), "cs/0501001")


class DedupTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_versioned_id_matches_bare(self):
        known = {"2401.00001"}
        entries = [
            {"id": self.mod.normalize_arxiv_id("2401.00001v2"),
             "title": "already known"},
            {"id": self.mod.normalize_arxiv_id("2401.00003"),
             "title": "fresh a"},
            {"id": self.mod.normalize_arxiv_id("2401.00004"),
             "title": "fresh b"},
        ]
        fresh = self.mod.filter_new(entries, known)
        self.assertEqual(len(fresh), 2)
        self.assertEqual(
            {e["id"] for e in fresh}, {"2401.00003", "2401.00004"},
        )


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_window_drops_old_entries(self):
        now = time.time()
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        recent = (now_dt - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stale = (now_dt - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")

        self.assertTrue(self.mod.within_window(recent, now, 24))
        self.assertFalse(self.mod.within_window(stale, now, 24))

    def test_window_accepts_iso_with_offset(self):
        now = time.time()
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        recent = (now_dt - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        self.assertTrue(self.mod.within_window(recent, now, 24))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
