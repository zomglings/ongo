#!/usr/bin/env python3
"""Unit tests for ongo-site's date-time sort ordering.

Verifies:
  * `_invert_date` is monotone-descending across full timestamps.
  * Same-date publications are ordered by time-of-day (newest first).
  * Same-second publications fall back to title (A->Z) as a stable tiebreaker.
  * The "0000-00-00 00:00:00" sentinel (used by the call site when
    `created_at` is missing) inverse-sorts to the BOTTOM of the
    newest-first list, so missing rows always appear last.

The ongo-site script has no `.py` extension, so we load it as a module via
``importlib.util``.
"""

import importlib.util
import importlib.machinery
import os
import sys
import unittest


_HERE = os.path.dirname(os.path.abspath(__file__))
_SITE_SCRIPT = os.path.normpath(os.path.join(_HERE, "..", "bin", "ongo-site"))


def _load():
    # ongo-site has no .py extension, so we need an explicit SourceFileLoader.
    loader = importlib.machinery.SourceFileLoader("ongo_site", _SITE_SCRIPT)
    spec = importlib.util.spec_from_loader("ongo_site", loader)
    mod = importlib.util.module_from_spec(spec)
    # The script's top level is import-safe (all execution is guarded by
    # ``if __name__ == "__main__":``).
    loader.exec_module(mod)
    return mod


class SortByDateTimeTests(unittest.TestCase):
    def setUp(self):
        self.os = _load()

    def test_invert_date_descending_on_full_timestamp(self):
        inv = self.os._invert_date
        # Later timestamp -> smaller inverted key (so ascending sort = newest first).
        self.assertLess(
            inv("2026-05-30 14:30:00"),
            inv("2026-05-30 09:15:00"),
        )
        self.assertLess(
            inv("2026-05-30 00:00:00"),
            inv("2026-05-29 23:59:59"),
        )

    def test_same_day_items_sort_by_time(self):
        items = [
            {"display": "alpha", "sort_ts": "2026-05-30 09:00:00"},
            {"display": "beta",  "sort_ts": "2026-05-30 14:30:00"},
            {"display": "gamma", "sort_ts": "2026-05-30 02:15:00"},
        ]
        ordered = sorted(
            items,
            key=lambda x: (
                self.os._invert_date(x["sort_ts"]),
                x["display"].lower(),
            ),
        )
        self.assertEqual(
            [i["display"] for i in ordered],
            ["beta", "alpha", "gamma"],
        )

    def test_same_second_falls_back_to_title(self):
        items = [
            {"display": "Charlie", "sort_ts": "2026-05-30 10:00:00"},
            {"display": "alpha",   "sort_ts": "2026-05-30 10:00:00"},
            {"display": "Bravo",   "sort_ts": "2026-05-30 10:00:00"},
        ]
        ordered = sorted(
            items,
            key=lambda x: (
                self.os._invert_date(x["sort_ts"]),
                x["display"].lower(),
            ),
        )
        self.assertEqual(
            [i["display"] for i in ordered],
            ["alpha", "Bravo", "Charlie"],
        )

    def test_cross_day_ordering(self):
        items = [
            {"display": "yesterday-late",
             "sort_ts": "2026-05-29 23:59:59"},
            {"display": "today-early",
             "sort_ts": "2026-05-30 00:00:01"},
            {"display": "today-late",
             "sort_ts": "2026-05-30 14:30:00"},
        ]
        ordered = sorted(
            items,
            key=lambda x: (
                self.os._invert_date(x["sort_ts"]),
                x["display"].lower(),
            ),
        )
        self.assertEqual(
            [i["display"] for i in ordered],
            ["today-late", "today-early", "yesterday-late"],
        )

    def test_missing_created_at_sinks_to_bottom(self):
        # The "0000-00-00 00:00:00" sentinel used at the call site for a
        # row whose created_at is missing must inverse-sort to the END
        # of the global newest-first list (i.e. its inverted key is
        # lexicographically LARGEST).
        inv = self.os._invert_date
        sentinel_inv = inv("0000-00-00 00:00:00")
        recent_inv = inv("2026-05-30 14:30:00")
        old_inv = inv("1990-01-01 00:00:00")
        self.assertGreater(sentinel_inv, recent_inv)
        self.assertGreater(sentinel_inv, old_inv)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
