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

"""Tests for segment retention.

Deleting the wrong segment loses footage that cannot be recovered, so the
decision is a pure function and this exercises it exhaustively.
"""
import datetime as dt, os, sys, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from capture import READY, TRIGGERED, plan_pruning  # noqa: E402

NOW = 1_700_000_000.0
T0 = dt.datetime(2026, 8, 20, 23, 0, 0)


def seg(minute, age_s=300):
    """A settled segment starting `minute` minutes past T0."""
    return (Path(f"/rec/{minute:02d}.mp4"),
            T0 + dt.timedelta(minutes=minute),
            NOW - age_s)


def live(minute):
    """The segment ffmpeg is writing right now."""
    return (Path(f"/rec/{minute:02d}.mp4"),
            T0 + dt.timedelta(minutes=minute), NOW - 1)


def names(paths):
    return sorted(p.name for p in paths)


class TestReady(unittest.TestCase):
    def test_the_segment_being_written_is_never_deleted(self):
        d = plan_pruning([live(5)], READY, ready_keep=1, now=NOW)
        self.assertEqual(d, [], "ffmpeg still owns that file")

    def test_one_completed_segment_is_held_as_pre_roll(self):
        d = plan_pruning([seg(1), seg(2), seg(3), live(4)], READY,
                         ready_keep=1, now=NOW)
        self.assertEqual(names(d), ["01.mp4", "02.mp4"],
                         "the newest completed one stays as pre-roll")

    def test_ready_keep_two_holds_two(self):
        d = plan_pruning([seg(1), seg(2), seg(3), live(4)], READY,
                         ready_keep=2, now=NOW)
        self.assertEqual(names(d), ["01.mp4"])

    def test_nothing_to_delete_when_only_the_kept_ones_exist(self):
        self.assertEqual(plan_pruning([seg(3), live(4)], READY,
                                      ready_keep=1, now=NOW), [])

    def test_ready_keep_zero_deletes_every_completed_segment(self):
        d = plan_pruning([seg(1), seg(2), live(3)], READY,
                         ready_keep=0, now=NOW)
        self.assertEqual(names(d), ["01.mp4", "02.mp4"])

    def test_an_empty_directory_is_fine(self):
        self.assertEqual(plan_pruning([], READY, now=NOW), [])


class TestTriggered(unittest.TestCase):
    def test_nothing_from_the_incident_is_deleted(self):
        start = T0 + dt.timedelta(minutes=2)
        d = plan_pruning([seg(2), seg(3), seg(4), live(5)], TRIGGERED,
                         keep_from=start, now=NOW)
        self.assertEqual(d, [], "an incident's footage must survive intact")

    def test_segments_from_before_the_incident_may_still_go(self):
        start = T0 + dt.timedelta(minutes=3)
        d = plan_pruning([seg(1), seg(2), seg(3), seg(4), live(5)], TRIGGERED,
                         keep_from=start, now=NOW)
        self.assertEqual(names(d), ["01.mp4", "02.mp4"],
                         "those belong to the quiet period before it started")

    def test_the_segment_containing_the_trigger_is_kept(self):
        # The incident began 30 seconds into segment 3.
        start = T0 + dt.timedelta(minutes=3, seconds=30)
        d = plan_pruning([seg(3), seg(4), live(5)], TRIGGERED,
                         keep_from=start, now=NOW)
        self.assertNotIn("03.mp4", names(d),
                         "the pre-roll lives in the segment the trigger fell in")

    def test_a_segment_that_ends_exactly_at_the_window_start_may_go(self):
        start = T0 + dt.timedelta(minutes=3)
        d = plan_pruning([seg(2), seg(3), live(4)], TRIGGERED,
                         keep_from=start, segment_seconds=60, now=NOW)
        self.assertEqual(names(d), ["02.mp4"],
                         "segment 2 ends exactly when the incident begins")

    def test_a_long_incident_keeps_everything(self):
        start = T0
        segs = [seg(i) for i in range(30)] + [live(30)]
        self.assertEqual(plan_pruning(segs, TRIGGERED, keep_from=start,
                                      now=NOW), [])

    def test_no_keep_from_means_keep_everything(self):
        d = plan_pruning([seg(1), seg(2), live(3)], TRIGGERED,
                         keep_from=None, now=NOW)
        self.assertEqual(d, [], "without a window, err towards keeping")


class TestTransitions(unittest.TestCase):
    def test_going_back_to_ready_prunes_what_the_incident_held(self):
        segs = [seg(1), seg(2), seg(3), live(4)]
        self.assertEqual(plan_pruning(segs, TRIGGERED,
                                      keep_from=T0 + dt.timedelta(minutes=1),
                                      now=NOW), [])
        # after the clip has been cut and the state released
        d = plan_pruning(segs, READY, ready_keep=1, now=NOW)
        self.assertEqual(names(d), ["01.mp4", "02.mp4"])

    def test_in_flight_window_is_respected_in_both_states(self):
        for state, kw in ((READY, {"ready_keep": 0}),
                          (TRIGGERED, {"keep_from": T0 + dt.timedelta(hours=9)})):
            d = plan_pruning([live(1)], state, now=NOW, **kw)
            self.assertEqual(d, [], f"{state}: must not delete a live segment")




class TestPinnedByAPendingCut(unittest.TestCase):
    """Going back to ready does not mean the clip has been cut.

    Observed on the board: an incident closed, the state went to ready, the
    sweep deleted the segments, and the concat then failed with "No such file
    or directory". Three events lost their full-resolution footage that way.
    """

    def test_a_queued_cut_protects_its_segments(self):
        segs = [seg(1), seg(2), seg(3), live(4)]
        pinned = {seg(1)[0], seg(2)[0]}
        d = plan_pruning(segs, READY, ready_keep=1, pinned=pinned, now=NOW)
        self.assertEqual(d, [],
                         "these are the only copy of that event until the "
                         "concat finishes")

    def test_unpinned_segments_are_still_pruned(self):
        segs = [seg(1), seg(2), seg(3), live(4)]
        d = plan_pruning(segs, READY, ready_keep=1, pinned={seg(1)[0]}, now=NOW)
        self.assertEqual(names(d), ["02.mp4"])

    def test_pinning_works_in_the_triggered_state_too(self):
        segs = [seg(1), seg(2), seg(3), live(4)]
        d = plan_pruning(segs, TRIGGERED,
                         keep_from=T0 + dt.timedelta(minutes=3),
                         pinned={seg(1)[0]}, segment_seconds=60, now=NOW)
        self.assertEqual(names(d), ["02.mp4"],
                         "1 is pinned, 2 predates the incident")

    def test_string_and_path_forms_both_pin(self):
        segs = [seg(1), seg(2), live(3)]
        d = plan_pruning(segs, READY, ready_keep=0,
                         pinned={str(seg(1)[0]), seg(2)[0]}, now=NOW)
        self.assertEqual(d, [], "the queue holds Paths, the log holds strings")


class TestTheBoardRegression(unittest.TestCase):
    """The 2026-08-20 footage loss, reproduced with real files on disk.

    An incident closed at 13:13:21, the clip was queued at 13:13:17, and the
    sweep deleted the sources before the worker had finished waiting for them
    to settle. ffmpeg then said "No such file or directory" and three events
    lost their full-resolution footage for good. Only the annotated companion
    and the stills survived.
    """

    def setUp(self):
        import tempfile, shutil
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.day = self.tmp / "2026-08-20"
        self.day.mkdir()
        self.files = []
        for m in range(9, 13):                    # 13:09 .. 13:12
            p = self.day / f"13-{m:02d}-23.mp4"
            p.write_bytes(b"x" * 1024)
            self.files.append(p)
        old = time.time() - 300                   # all settled
        for p in self.files:
            os.utime(p, (old, old))

    def _manager(self):
        from capture import CaptureManager

        class Tier:
            pass
        tier = Tier()
        tier.path = self.tmp
        return CaptureManager({"ready_keep_clips": 1, "in_flight_s": 75.0},
                              tier)

    def test_a_queued_cut_survives_the_sweep_that_follows_set_ready(self):
        from concat_mgr import ConcatJob, ConcatManager
        from segments import pinned_paths

        cm = ConcatManager()                      # not started: nothing drains
        cm.submit(ConcatJob(list(self.files),
                            self.tmp / "clip.mp4", delete_sources=True))

        cap = self._manager()
        cap.set_ready()                           # the incident just closed

        pinned = pinned_paths(cm, self.tmp, None, 60)
        cap.sweep(pinned=pinned)

        alive = sorted(p.name for p in self.day.glob("*.mp4"))
        self.assertEqual(
            alive,
            ["13-09-23.mp4", "13-10-23.mp4", "13-11-23.mp4", "13-12-23.mp4"],
            "the queued cut still needs every one of these")

    def test_without_the_pin_the_sweep_eats_them(self):
        """The old behaviour, kept so the fix cannot silently regress."""
        cap = self._manager()
        cap.set_ready()
        cap.sweep()                               # no pinned set
        alive = sorted(p.name for p in self.day.glob("*.mp4"))
        self.assertEqual(alive, ["13-12-23.mp4"],
                         "ready keeps one; the other three are the loss")


if __name__ == "__main__":
    unittest.main(verbosity=2)
