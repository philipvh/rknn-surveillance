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

"""Tests for the arming schedule -- the most important alert gate.

An error here either floods people during club hours or silently disarms the
system at night, so the wrapping-past-midnight cases get particular attention.
"""
import datetime as dt, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schedule import Schedule, Window, ScheduleError  # noqa: E402

# 2026-08-17 is a Monday.
def when(day, hh, mm=0):
    return dt.datetime(2026, 8, 17, hh, mm) + dt.timedelta(days=day)

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


class TestWindow(unittest.TestCase):
    def test_simple_window(self):
        w = Window(["mon"], "09:00", "17:00")
        self.assertFalse(w.wraps)
        self.assertTrue(w.contains(when(MON, 12)))
        self.assertFalse(w.contains(when(MON, 8)))
        self.assertFalse(w.contains(when(TUE, 12)))

    def test_window_wrapping_midnight_covers_the_next_morning(self):
        w = Window(["mon"], "23:00", "07:00")
        self.assertTrue(w.wraps)
        self.assertEqual(w.minutes, 8 * 60)
        self.assertTrue(w.contains(when(MON, 23, 30)))
        self.assertTrue(w.contains(when(TUE, 3)),
                        "Monday 23:00-07:00 must cover Tuesday 03:00")
        self.assertTrue(w.contains(when(TUE, 6, 59)))
        self.assertFalse(w.contains(when(TUE, 7, 1)))
        self.assertFalse(w.contains(when(MON, 22, 59)))

    def test_boundaries_are_half_open(self):
        w = Window(["mon"], "22:00", "23:00")
        self.assertTrue(w.contains(when(MON, 22, 0)))
        self.assertFalse(w.contains(when(MON, 23, 0)))

    def test_day_names_are_forgiving(self):
        self.assertEqual(Window(["Monday"], "1:00", "2:00").days, {0})
        self.assertEqual(Window("sun", "1:00", "2:00").days, {6})
        self.assertEqual(Window("all", "1:00", "2:00").days, set(range(7)))

    def test_bad_input_is_rejected_loudly(self):
        with self.assertRaises(ScheduleError):
            Window(["funday"], "1:00", "2:00")
        with self.assertRaises(ScheduleError):
            Window(["mon"], "25 past 3", "2:00")
        with self.assertRaises(ScheduleError):
            Window([], "1:00", "2:00")


class TestSchedule(unittest.TestCase):
    def setUp(self):
        self.s = Schedule.from_config({"armed": [
            {"days": ["mon", "tue", "wed", "thu", "fri"],
             "from": "22:30", "to": "07:30"},
            {"days": ["sat", "sun"], "from": "21:00", "to": "08:30"},
        ]})

    def test_disarmed_during_club_hours(self):
        for h in (9, 12, 15, 18, 21):
            self.assertFalse(self.s.is_armed(when(WED, h)),
                             f"must be disarmed at {h}:00 -- the courts are busy")

    def test_armed_overnight_on_a_weekday(self):
        self.assertTrue(self.s.is_armed(when(WED, 23)))
        self.assertTrue(self.s.is_armed(when(THU, 3)))
        self.assertTrue(self.s.is_armed(when(THU, 7, 29)))
        self.assertFalse(self.s.is_armed(when(THU, 7, 31)))

    def test_weekend_arms_earlier_and_disarms_later(self):
        self.assertTrue(self.s.is_armed(when(SAT, 21, 30)))
        self.assertFalse(self.s.is_armed(when(SAT, 20, 30)))
        self.assertTrue(self.s.is_armed(when(SUN, 8, 0)))

    def test_friday_night_into_saturday(self):
        self.assertTrue(self.s.is_armed(when(FRI, 23)))
        self.assertTrue(self.s.is_armed(when(SAT, 5)),
                        "the Friday window must carry into Saturday morning")

    def test_no_windows_means_never_armed(self):
        s = Schedule.from_config({})
        self.assertFalse(s.is_armed(when(WED, 3)))

    def test_always_armed_overrides_everything(self):
        s = Schedule.from_config({"always_armed": True})
        self.assertTrue(s.is_armed(when(WED, 12)))


class TestOverride(unittest.TestCase):
    def setUp(self):
        self.s = Schedule.from_config({"armed": [
            {"days": ["mon"], "from": "22:30", "to": "07:30"}]})

    def test_manual_disarm_expires(self):
        night = when(MON, 23)
        self.assertTrue(self.s.is_armed(night))
        self.s.override(False, until=night + dt.timedelta(hours=1))
        self.assertFalse(self.s.is_armed(night))
        self.assertFalse(self.s.is_armed(night + dt.timedelta(minutes=59)))
        self.assertTrue(self.s.is_armed(night + dt.timedelta(hours=2)),
                        "a manual disarm must not be permanent")

    def test_manual_arm_during_club_hours(self):
        noon = when(MON, 12)
        self.assertFalse(self.s.is_armed(noon))
        self.s.override(True, until=noon + dt.timedelta(hours=2))
        self.assertTrue(self.s.is_armed(noon))

    def test_describe_says_why(self):
        self.assertIn("club hours", self.s.describe(when(MON, 12)))
        self.assertIn("armed", self.s.describe(when(MON, 23)))
        self.s.override(False, until=when(MON, 23, 30))
        self.assertIn("by hand", self.s.describe(when(MON, 23)))


class TestNextChange(unittest.TestCase):
    def test_reports_when_arming_next_flips(self):
        s = Schedule.from_config({"armed": [
            {"days": ["mon"], "from": "22:30", "to": "07:30"}]})
        nxt = s.next_change(when(MON, 21, 0))
        self.assertEqual((nxt.hour, nxt.minute), (22, 30))


if __name__ == "__main__":
    unittest.main(verbosity=2)
