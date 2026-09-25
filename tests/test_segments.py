# Copyright 2026 Philip van Houtte, magicview.tv, the Netherlands
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. This
# software aids surveillance; it does not guarantee it, and no liability is
# accepted for any failure to detect, record, retain or report an event.
# See the NOTICE file for the full disclaimer.

"""Capping a clip's window.

A clip is named for the window it covers, and the media browser files each
still under the first clip whose window contains it. So a name that overstates
the window is not cosmetic: one capped clip claimed sixteen hours while holding
ten minutes, and every still of that day disappeared into it, leaving the four
real clips showing none.
"""
import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import segments  # noqa: E402
from segments import cap_window  # noqa: E402

DAY = dt.datetime(2026, 9, 25)


def segs(start_min, count, root=Path("/rec/2026-09-25")):
    out = []
    for i in range(count):
        t = DAY + dt.timedelta(minutes=start_min + i)
        out.append(root / t.strftime("%H-%M-%S.mp4"))
    return out


class TestCapWindow(unittest.TestCase):
    def test_a_short_window_is_untouched(self):
        s = segs(115, 5)                     # 01:55 .. 01:59
        end = DAY + dt.timedelta(minutes=120)
        got, got_end, capped = cap_window(s, 600.0, 60, end)
        self.assertEqual(got, s)
        self.assertEqual(got_end, end)
        self.assertFalse(capped)

    def test_a_long_window_is_trimmed_to_the_cap(self):
        got, _, capped = cap_window(segs(115, 967), 600.0, 60, DAY)
        self.assertTrue(capped)
        self.assertEqual(len(got), 10)       # 600s / 60s

    def test_the_end_follows_the_content_not_the_request(self):
        # The bug: 967 segments from 01:55, capped to 10, but named as ending
        # at 18:01 -- a file claiming 16 hours and holding 10 minutes.
        requested_end = DAY + dt.timedelta(hours=18, minutes=1, seconds=28)
        _, end, _ = cap_window(segs(115, 967), 600.0, 60, requested_end)
        self.assertEqual(end, DAY + dt.timedelta(minutes=125),
                         "the clip still claims a window it does not contain")
        self.assertLess((end - DAY).total_seconds(), 8000,
                        "a capped clip must not span most of a day")

    def test_the_named_span_matches_the_content_length(self):
        first = DAY + dt.timedelta(minutes=115)
        _, end, _ = cap_window(segs(115, 500), 600.0, 60,
                               DAY + dt.timedelta(hours=20))
        self.assertAlmostEqual((end - first).total_seconds(), 600.0, delta=1)

    def test_a_cap_smaller_than_one_segment_still_keeps_one(self):
        got, _, capped = cap_window(segs(115, 5), 10.0, 60, DAY)
        self.assertEqual(len(got), 1)
        self.assertTrue(capped)

    def test_an_unparseable_name_leaves_the_end_alone(self):
        odd = [Path("/rec/not-a-timestamp.mp4")] * 40
        end = DAY + dt.timedelta(hours=3)
        _, got_end, capped = cap_window(odd, 600.0, 60, end)
        self.assertTrue(capped)
        self.assertEqual(got_end, end)       # nothing better to say


if __name__ == "__main__":
    unittest.main()


class TestSplitWindowKeepsEverything(unittest.TestCase):
    """The 2026-09-24 data loss: a four-day window, ten minutes kept.

    cap_window() is still the right answer for naming a single clip, but the
    caller must not throw the remainder away. These pin the property that
    matters: every segment handed in comes back out in exactly one clip.
    """

    def _segs(self, minutes, day="2026-09-20"):
        return [f"/t/{day}/{m // 60:02d}-{m % 60:02d}-00.mp4"
                for m in range(minutes)]

    def test_a_window_under_the_cap_stays_one_clip(self):
        segs = self._segs(7)
        end = dt.datetime(2026, 9, 20, 0, 7, 0)
        parts, surplus = segments.split_window(segs, 600.0, 60, end)
        self.assertEqual(len(parts), 1)
        self.assertEqual(surplus, 0)
        self.assertEqual(parts[0], (segs, end))

    def test_no_segment_is_dropped(self):
        segs = self._segs(180)                      # three hours
        end = dt.datetime(2026, 9, 20, 3, 0, 0)
        parts, surplus = segments.split_window(segs, 600.0, 60, end)
        self.assertEqual(surplus, 0)
        rebuilt = [s for chunk, _ in parts for s in chunk]
        self.assertEqual(rebuilt, segs, "every segment must land in one clip")
        self.assertEqual(len(parts), 18)

    def test_each_clip_is_named_for_what_it_holds(self):
        segs = self._segs(25)
        end = dt.datetime(2026, 9, 20, 0, 25, 0)
        parts, _ = segments.split_window(segs, 600.0, 60, end)
        for chunk, chunk_end in parts:
            first = segments.parse_seg_start(chunk[0])
            held = (chunk_end - first).total_seconds()
            self.assertLessEqual(held, 600.0)
            self.assertAlmostEqual(held, len(chunk) * 60, delta=1)

    def test_the_four_day_window_would_have_been_kept(self):
        """5976 segments is what the real incident held."""
        segs = [f"/t/d/{i}.mp4" for i in range(5976)]
        end = dt.datetime(2026, 9, 24, 21, 1, 20)
        parts, surplus = segments.split_window(segs, 600.0, 60, end,
                                               max_parts=1000)
        self.assertEqual(surplus, 0)
        self.assertEqual(sum(len(c) for c, _ in parts), 5976)

    def test_max_parts_reports_the_surplus_instead_of_hiding_it(self):
        segs = self._segs(180)
        end = dt.datetime(2026, 9, 20, 3, 0, 0)
        parts, surplus = segments.split_window(segs, 600.0, 60, end,
                                               max_parts=5)
        self.assertEqual(len(parts), 5)
        self.assertEqual(surplus, 180 - 50)
