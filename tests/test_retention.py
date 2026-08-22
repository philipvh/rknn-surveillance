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

"""Tests for the retention policy.

The claim these exist to defend: filling the disk deletes continuous footage
but never an event clip.
"""
import sys, time, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from retention import Candidate, plan_deletions   # noqa: E402

GB = 1024 ** 3
DAY = 86400.0
NOW = 1_700_000_000.0

# tier order = sacrifice order, matching config.yaml
MAIN, SUB, DETECTIONS, EVENTS = 0, 1, 2, 3


def c(tier_index, age_days, size=GB, name=None, protected=False, max_age_days=None):
    names = {MAIN: "main", SUB: "sub", DETECTIONS: "detections", EVENTS: "events"}
    tname = names[tier_index]
    return Candidate(
        path=Path(f"/{tname}/{name or f'{age_days}d-{size}'}.mp4"),
        mtime=NOW - age_days * DAY, size=size,
        tier_name=tname, tier_index=tier_index,
        max_age_s=None if max_age_days is None else max_age_days * DAY,
        protected=protected,
    )


def plan(cands, free_gb=100, total_gb=1000, target=0.20, **kw):
    return plan_deletions(cands, now=NOW, free_bytes=free_gb * GB,
                          total_bytes=total_gb * GB, target_free_ratio=target, **kw)


class TestAge(unittest.TestCase):
    def test_deletes_past_max_age(self):
        old = c(SUB, age_days=61, max_age_days=60)
        new = c(SUB, age_days=59, max_age_days=60)
        d, _ = plan([old, new], free_gb=500)
        self.assertEqual([x.path for x in d], [old.path])
        self.assertEqual(d[0].reason, "age")

    def test_age_applies_to_protected_tiers_too(self):
        ancient = c(EVENTS, age_days=800, protected=True, max_age_days=730)
        recent = c(EVENTS, age_days=700, protected=True, max_age_days=730)
        d, _ = plan([ancient, recent], free_gb=500)
        self.assertEqual([x.path for x in d], [ancient.path])

    def test_no_max_age_means_keep(self):
        d, _ = plan([c(SUB, age_days=9999, max_age_days=None)], free_gb=500)
        self.assertEqual(d, [])


class TestPressure(unittest.TestCase):
    def test_sacrifices_tiers_in_config_order(self):
        cands = [c(SUB, 10, name="sub"), c(MAIN, 1, name="main")]
        # need 200GB free, have 199GB -> one file must go, and it must be main
        d, _ = plan(cands, free_gb=199, total_gb=1000)
        self.assertEqual([x.tier_name for x in d], ["main"])
        self.assertEqual(d[0].reason, "pressure")

    def test_oldest_first_within_a_tier(self):
        newest = c(MAIN, 1, name="new")
        oldest = c(MAIN, 5, name="old")
        d, _ = plan([newest, oldest], free_gb=199, total_gb=1000)
        self.assertEqual([x.path for x in d], [oldest.path])

    def test_stops_once_target_is_met(self):
        cands = [c(MAIN, i, name=f"f{i}") for i in range(1, 11)]
        d, _ = plan(cands, free_gb=197, total_gb=1000)   # 3GB short
        self.assertEqual(len(d), 3)

    def test_protected_survives_a_full_disk_when_asked_to(self):
        # The original policy, still available: a full disk stops the system
        # rather than dropping any evidence.
        clips = [c(EVENTS, 30 + i, size=10 * GB, name=f"clip{i}",
                   protected=True, max_age_days=730) for i in range(5)]
        d, w = plan(clips, free_gb=0, total_gb=1000, purge_protected=False)
        self.assertEqual(d, [], "event clips must survive a completely full disk")
        self.assertTrue(w, "a full disk with nothing deletable must warn")
        self.assertIn("protected", w[0])

    def test_by_default_a_full_disk_costs_the_oldest_evidence(self):
        # The policy now: the camera keeps recording, and the cost is the
        # oldest clips rather than every future one.
        clips = [c(EVENTS, 30 + i, size=10 * GB, name=f"clip{i}",
                   protected=True, max_age_days=730) for i in range(5)]
        d, w = plan(clips, free_gb=0, total_gb=1000)
        self.assertTrue(d, "something must give, or nothing records again")
        self.assertEqual({x.reason for x in d}, {"pressure (protected)"})
        self.assertIn("clip4", str(d[0].path), "the oldest one first")
        self.assertTrue(any("oldest protected" in x for x in w))

    def test_unprotected_is_always_spent_before_evidence(self):
        cands = [c(MAIN, 1, size=GB, name="m"),
                 c(EVENTS, 400, size=500 * GB, protected=True, max_age_days=730)]
        d, w = plan(cands, free_gb=0, total_gb=1000)
        self.assertEqual([x.tier_name for x in d], ["main", "events"],
                         "the working buffer goes first, every time")
        self.assertTrue(w)

    def test_and_kept_entirely_when_purging_is_off(self):
        cands = [c(MAIN, 1, size=GB, name="m"),
                 c(EVENTS, 400, size=500 * GB, protected=True, max_age_days=730)]
        d, w = plan(cands, free_gb=0, total_gb=1000, purge_protected=False)
        self.assertEqual([x.tier_name for x in d], ["main"])
        self.assertTrue(w)


class TestSafety(unittest.TestCase):
    def test_pinned_files_are_untouchable(self):
        f = c(MAIN, 99, max_age_days=2)
        d, _ = plan([f], free_gb=0, pinned={f.path})
        self.assertEqual(d, [])

    def test_in_flight_segment_is_not_deleted(self):
        # being written right now, but its tier's max_age is 0.00001 days
        writing = c(MAIN, age_days=0, max_age_days=0.00001)
        d, _ = plan([writing], free_gb=0, min_age_s=75)
        self.assertEqual(d, [], "must not delete the segment ffmpeg is writing")

    def test_a_file_is_never_planned_twice(self):
        f = c(MAIN, 99, max_age_days=2)
        d, _ = plan([f], free_gb=0, total_gb=1000)
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0].reason, "age")

    def test_empty_input_is_fine(self):
        d, w = plan([], free_gb=0)
        self.assertEqual(d, [])
        self.assertTrue(w)


class TestRealisticNight(unittest.TestCase):
    """A full disk on a 1TB drive with a normal mix of data."""

    def test_evidence_survives_a_full_disk(self):
        cands = []
        cands += [c(MAIN, i / 24, size=1 * GB, name=f"main{i}", max_age_days=2)
                  for i in range(48)]                      # 48GB, 2 days
        cands += [c(SUB, i, size=5 * GB, name=f"sub{i}", max_age_days=60)
                  for i in range(60)]                      # 300GB, 60 days
        cands += [c(EVENTS, i * 3, size=60 * 1024 ** 2, name=f"clip{i}",
                    protected=True, max_age_days=730) for i in range(200)]

        d, w = plan(cands, free_gb=5, total_gb=1000, target=0.20)
        deleted_tiers = {x.tier_name for x in d}

        self.assertNotIn("events", deleted_tiers,
                         "an event clip was deleted to make room -- the one thing "
                         "this policy exists to prevent")
        self.assertIn("main", deleted_tiers)
        freed = sum(x.size for x in d)
        self.assertGreaterEqual(freed + 5 * GB, 200 * GB, "should reach the target")
        self.assertEqual(w, [], "target was reachable, so no warning expected")




class TestFullDiskTakesTheOldestProtected(unittest.TestCase):
    """A full disk must not stop the camera recording.

    Without this pass the recorder simply stops: no clips, no stills, and the
    only sign is a warning in a log nobody reads. Losing the oldest footage is
    bounded and visible; recording nothing from today onwards is neither.
    """

    def test_protected_is_taken_only_after_everything_else(self):
        cands = [c(EVENTS, 30, protected=True), c(EVENTS, 20, protected=True),
                 c(DETECTIONS, 5), c(MAIN, 1)]
        # 100 GB free of 1000, want 200 -> 100 GB short; two unprotected GB
        # cannot cover it, so protected has to give.
        dels, warns = plan(cands, free_gb=100, total_gb=1000)
        order = [d.reason for d in dels]
        self.assertEqual(order.count("pressure"), 2, "unprotected goes first")
        self.assertIn("pressure (protected)", order)
        self.assertLess(order.index("pressure"),
                        order.index("pressure (protected)"))

    def test_the_oldest_protected_goes_first(self):
        cands = [c(EVENTS, 5, protected=True, name="new"),
                 c(EVENTS, 60, protected=True, name="old"),
                 c(EVENTS, 30, protected=True, name="mid")]
        dels, _ = plan(cands, free_gb=199, total_gb=1000)   # 1 GB short
        self.assertEqual(len(dels), 1)
        self.assertIn("old", str(dels[0].path))

    def test_it_stops_as_soon_as_the_target_is_met(self):
        cands = [c(EVENTS, d, protected=True, name=f"e{d}")
                 for d in (10, 20, 30, 40, 50)]
        dels, _ = plan(cands, free_gb=198, total_gb=1000)   # 2 GB short
        self.assertEqual(len(dels), 2, "no more than the shortfall requires")

    def test_nothing_protected_is_touched_when_there_is_room(self):
        cands = [c(EVENTS, 30, protected=True), c(DETECTIONS, 5)]
        dels, warns = plan(cands, free_gb=500, total_gb=1000)
        self.assertEqual(dels, [])
        self.assertEqual(warns, [])

    def test_it_says_so_loudly(self):
        cands = [c(EVENTS, 30, protected=True)]
        _, warns = plan(cands, free_gb=199, total_gb=1000)
        self.assertTrue(warns)
        self.assertIn("oldest protected", warns[0])

    def test_the_old_behaviour_is_still_reachable(self):
        cands = [c(EVENTS, 30, protected=True)]
        dels, warns = plan(cands, free_gb=100, total_gb=1000,
                           purge_protected=False)
        self.assertEqual(dels, [], "nothing protected may be deleted")
        self.assertTrue(any("left alone" in w for w in warns))

    def test_a_file_still_being_written_is_never_taken(self):
        cands = [c(EVENTS, 0, protected=True)]      # mtime = now
        dels, _ = plan(cands, free_gb=100, total_gb=1000, min_age_s=120)
        self.assertEqual(dels, [], "ffmpeg still owns it")

    def test_a_pinned_file_is_never_taken(self):
        keep = c(EVENTS, 90, protected=True)
        dels, _ = plan([keep], free_gb=100, total_gb=1000,
                       pinned={keep.path})
        self.assertEqual(dels, [], "a queued cut still needs it")

    def test_age_still_applies_to_protected_tiers_first(self):
        cands = [c(EVENTS, 40, protected=True, max_age_days=30)]
        dels, _ = plan(cands, free_gb=500, total_gb=1000)
        self.assertEqual([d.reason for d in dels], ["age"],
                         "over its age it goes regardless of disk pressure")


if __name__ == "__main__":
    unittest.main(verbosity=2)
