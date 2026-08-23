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

"""Tests for the state machine and the alert policy.

Everything is driven by an explicit clock and a fake PTZ, so a whole night can
be simulated in milliseconds and every transition is deterministic.
"""
import datetime as dt, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from alerts import AlertPolicy, Incident, ShadowLog  # noqa: E402
from controller import Controller, Detection, State  # noqa: E402
from ptz import BudgetExceeded  # noqa: E402
from schedule import Schedule  # noqa: E402
from trigger import TriggerEvent  # noqa: E402


class FakePTZ:
    preset_estimate_s = 1.0

    def __init__(self):
        self.moving = False
        self._settled = True
        self.gotos = []
        self.scan_starts = []
        self.refuse = False
        self.at = None

    def settled(self):
        return self._settled

    def goto_preset(self, name, source="auto", is_scan_start=False):
        if self.refuse:
            raise BudgetExceeded("motor budget spent")
        self.gotos.append(name)
        self.scan_starts.append(is_scan_start)
        self.at = name


class Cfg:
    """Minimal stand-in for config.Config."""

    def __init__(self, **over):
        self.d = {
            "ptz": {"scan_presets": ["Court1", "Court2", "Gate"],
                    "home_preset": "Home", "dwell_s": 4.0, "settle_s": 0.5},
            "controller": {"lights_settle_s": 1.5, "quiet_period_s": 15.0,
                           "max_hold_s": 600.0, "max_scan_s": 120.0,
                           "manual_timeout_s": 180.0, "return_timeout_s": 30.0,
                           "pir_required_to_scan": True},
            "trigger": {"pre_roll_s": 2.0, "post_roll_s": 15.0},
            "alerts": {"require_pir": True, "min_duration_s": 4.0,
                       "min_sightings": 3, "min_confidence": 0.7,
                       "min_interval_s": 300, "max_per_day": 12},
        }
        for k, v in over.items():
            self.d.setdefault(k, {}).update(v)

    def _get(self, *keys, default=None):
        node = self.d
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


ARMED_NIGHT = dt.datetime(2026, 8, 19, 23, 30)      # Wednesday night


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.wall = ARMED_NIGHT
        self.cfg = Cfg()
        self.ptz = FakePTZ()
        self.sched = Schedule.from_config({"armed": [
            {"days": "all", "from": "22:00", "to": "08:00"}]})
        self.policy = AlertPolicy(self.cfg, self.sched, clock=lambda: self.wall)
        self.shadow = ShadowLog(Path(self.tmp.name) / "shadow")
        self.clips = []
        self.c = Controller(
            self.cfg, self.ptz, self.sched, self.policy, self.shadow,
            clip_fn=lambda s, e: self.clips.append((s, e)) or f"clip_{len(self.clips)}.mp4",
            snapshot_fn=lambda: "snap.jpg",
            clock=lambda: self.now, wall=lambda: self.wall)

    def tearDown(self):
        self.tmp.cleanup()

    def advance(self, seconds, step=0.1):
        end = self.now + seconds
        while self.now < end:
            self.now = round(self.now + step, 4)
            self.wall = self.wall + dt.timedelta(seconds=step)
            self.c.tick()

    def pir(self, kind, duration=0.0):
        self.c.on_pir(TriggerEvent(kind, self.wall.timestamp(), duration))

    def see(self, conf=0.9, count=1, label="person"):
        self.c.on_detection(Detection(self.wall, count, conf, [label]))

    def rows(self):
        return self.shadow.read()


class TestHappyPath(Base):
    def test_full_cycle_from_pir_to_home(self):
        self.assertIs(self.c.state, State.PARKED)

        self.pir("active")
        self.assertIs(self.c.state, State.SETTLING)

        self.advance(2.0)                       # lights settle
        self.assertIs(self.c.state, State.SCANNING)
        self.assertEqual(self.ptz.gotos[0], "Court1")

        self.see()                               # someone appears
        self.assertIs(self.c.state, State.HOLDING)
        for _ in range(5):
            self.advance(1.0)
            self.see()

        self.pir("inactive", duration=8.0)       # the PIR's timer runs out
        self.advance(16.0)                       # then the quiet period
        self.assertIs(self.c.state, State.RETURNING)
        self.advance(3.0)
        self.assertIs(self.c.state, State.PARKED)
        self.assertEqual(self.ptz.gotos[-1], "Home")

    def test_a_clip_is_requested_with_pre_and_post_roll(self):
        # No PIR here: the window would then start at the PIR rather than at
        # the first sighting, and this is about the roll either side of the
        # window, not about which source opened it.
        first = self.wall
        self.see(); self.advance(3.0); self.see()
        last = self.wall
        self.advance(16.0)
        self.assertEqual(len(self.clips), 1)
        start, end = self.clips[0]
        self.assertAlmostEqual((first - start).total_seconds(), 2.0, delta=0.3)
        self.assertAlmostEqual((end - last).total_seconds(), 15.0, delta=0.3)

    def test_camera_is_home_within_a_minute(self):
        self.pir("active"); self.advance(2.0)
        self.see()
        self.pir("inactive", duration=8.0)
        t0 = self.now
        self.advance(40.0)
        self.assertIs(self.c.state, State.PARKED)
        self.assertLess(self.now - t0, 60.0)


class TestScanning(Base):
    def test_sweeps_every_preset_then_returns(self):
        self.pir("active"); self.advance(2.0)
        self.advance(30.0)
        self.assertEqual(self.ptz.gotos[:3], ["Court1", "Court2", "Gate"])
        self.assertIn("Home", self.ptz.gotos)
        self.assertIs(self.c.state, State.PARKED)

    def test_pir_released_before_the_scan_starts_cancels_it(self):
        self.pir("active")
        self.pir("inactive", duration=0.5)
        self.advance(2.0)
        self.assertIs(self.c.state, State.PARKED)
        self.assertEqual(self.ptz.gotos, [], "must not move for a blip")

    def test_motor_budget_refusal_parks_instead_of_looping(self):
        self.ptz.refuse = True
        self.pir("active"); self.advance(2.0)
        self.advance(5.0)
        self.assertIn(self.c.state, (State.PARKED, State.RETURNING))

    def test_scan_that_overruns_gives_up(self):
        self.pir("active"); self.advance(2.0)
        self.c.presets = ["A"] * 200
        self.advance(130.0)
        self.assertIn(self.c.state, (State.RETURNING, State.PARKED))


class TestHolding(Base):
    def test_repeated_sightings_extend_the_hold(self):
        self.pir("active"); self.advance(2.0)
        self.see()
        for _ in range(10):
            self.advance(10.0)
            self.see()
            self.assertIs(self.c.state, State.HOLDING)

    def test_camera_does_not_move_while_holding(self):
        self.pir("active"); self.advance(2.0)
        n = len(self.ptz.gotos)
        self.see()
        self.advance(10.0)
        self.assertEqual(len(self.ptz.gotos), n,
                         "holding must keep the view stable for the clip")

    def test_max_hold_closes_the_incident(self):
        self.pir("active"); self.advance(2.0)
        self.see()
        for _ in range(70):
            self.advance(10.0)
            self.see()
        self.assertEqual(len(self.clips), 1)


class TestDetectionGating(Base):
    def test_disabled_while_the_motors_run(self):
        self.ptz.moving = True
        self.assertFalse(self.c.detection_enabled())

    def test_disabled_until_the_camera_settles(self):
        self.ptz.moving = False
        self.ptz._settled = False
        self.assertFalse(self.c.detection_enabled())
        self.ptz._settled = True
        self.assertTrue(self.c.detection_enabled())

    def test_disabled_while_the_floodlights_come_up(self):
        self.pir("active")
        self.assertIs(self.c.state, State.SETTLING)
        self.assertFalse(self.c.detection_enabled(),
                         "exposure and IR-cut are still reacting")


class TestManual(Base):
    def test_panel_takes_over_and_expires(self):
        self.pir("active"); self.advance(2.0)
        self.c.on_manual(True)
        self.assertIs(self.c.state, State.MANUAL)
        self.advance(100.0)
        self.assertIs(self.c.state, State.MANUAL)
        self.advance(120.0)
        self.assertIn(self.c.state, (State.RETURNING, State.PARKED))

    def test_panel_release_returns_home(self):
        self.c.on_manual(True)
        self.c.on_manual(False)
        self.assertIs(self.c.state, State.RETURNING)


class TestStuckPir(Base):
    def test_stuck_line_does_not_hold_the_camera_scanning(self):
        self.pir("active"); self.advance(2.0)
        self.c.on_pir(TriggerEvent("stuck", self.wall.timestamp(), 1800))
        self.advance(30.0)
        self.assertIn(self.c.state, (State.PARKED, State.RETURNING))


class TestAlertGates(Base):
    def full_incident(self, sightings=6, conf=0.9, gap=1.0):
        self.pir("active"); self.advance(2.0)
        self.see(conf=conf)
        for _ in range(sightings - 1):
            self.advance(gap)
            self.see(conf=conf)
        # The PIR's own timer runs out. Until it does, the window stays open:
        # the sensor still seeing movement is a reason to keep recording.
        self.pir("inactive", duration=10.0)
        self.advance(16.0)

    def test_a_real_intruder_would_alert(self):
        self.full_incident()
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["would_alert"], rows[0])
        self.assertEqual(rows[0]["passed"],
                         ["schedule", "corroboration", "persistence", "rate limit"])

    def test_club_hours_block_the_alert(self):
        self.wall = dt.datetime(2026, 8, 19, 14, 0)      # afternoon
        self.full_incident()
        row = self.rows()[0]
        self.assertFalse(row["would_alert"])
        self.assertEqual(row["failed"], "schedule")

    def test_camera_without_pir_is_not_enough(self):
        self.see()                                        # no PIR at all
        for _ in range(5):
            self.advance(1.0); self.see()
        self.advance(16.0)
        row = self.rows()[0]
        self.assertFalse(row["would_alert"])
        self.assertEqual(row["failed"], "corroboration")

    def test_a_single_glimpse_fails_persistence(self):
        self.pir("active"); self.advance(2.0)
        self.see()
        self.pir("inactive", duration=8.0)
        self.advance(16.0)
        row = self.rows()[0]
        self.assertFalse(row["would_alert"])
        self.assertEqual(row["failed"], "persistence")

    def test_low_confidence_fails_persistence(self):
        self.full_incident(conf=0.5)
        row = self.rows()[0]
        self.assertFalse(row["would_alert"])
        self.assertEqual(row["failed"], "persistence")

    def test_second_incident_is_rate_limited(self):
        self.full_incident()
        self.advance(30.0)
        self.full_incident()
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["would_alert"])
        self.assertFalse(rows[1]["would_alert"])
        self.assertEqual(rows[1]["failed"], "rate limit")

    def test_daily_ceiling_holds(self):
        self.policy.max_per_day = 2
        self.policy.min_interval_s = 0
        for _ in range(4):
            self.full_incident()
            self.advance(5.0)
        rows = self.rows()
        self.assertEqual(sum(r["would_alert"] for r in rows), 2,
                         "a total logic failure must still cost only a handful "
                         "of messages")

    def test_nothing_is_actually_sent(self):
        self.full_incident()
        self.assertTrue(self.rows()[0]["would_alert"])
        # The only output is the shadow log; there is no sender wired in yet.
        self.assertTrue(any(self.shadow.root.rglob("*.jsonl")))


class TestShadowLogContent(Base):
    def test_row_carries_enough_to_judge_it_later(self):
        self.pir("active"); self.advance(2.0)
        self.see(conf=0.93, count=2)
        for _ in range(4):
            self.advance(1.0); self.see(conf=0.81, count=3)
        self.pir("inactive", duration=10.0)
        self.advance(16.0)
        row = self.rows()[0]
        for key in ("at", "would_alert", "passed", "summary", "duration_s",
                    "sightings", "max_confidence", "max_count", "labels",
                    "pir", "preset", "snapshot", "clip"):
            self.assertIn(key, row)
        self.assertEqual(row["max_count"], 3)
        self.assertAlmostEqual(row["max_confidence"], 0.93, places=2)
        self.assertEqual(row["labels"], ["person"])
        self.assertTrue(row["clip"])
        self.assertEqual(row["snapshot"], "snap.jpg")




class TestFixedCamera(Base):
    """No PTZ -- a camera that cannot move must not be commanded.

    Found on the bench: with ptz.enabled false the driver still ran, and the
    stop watchdog plus the controller's returning state retried against an
    unreachable host forever, at 173 log lines a minute.
    """

    def setUp(self):
        super().setUp()
        self.ptz.enabled = False
        self.c = Controller(
            self.cfg, self.ptz, self.sched, self.policy, self.shadow,
            clip_fn=lambda s, e: self.clips.append((s, e)) or "c.mp4",
            snapshot_fn=lambda: "snap.jpg",
            clock=lambda: self.now, wall=lambda: self.wall)

    def test_a_trigger_watches_instead_of_scanning(self):
        self.pir("active")
        self.advance(3.0)
        self.assertIs(self.c.state, State.HOLDING)
        self.assertEqual(self.ptz.gotos, [], "a fixed camera must not be moved")

    def test_an_incident_still_closes_and_is_recorded(self):
        self.pir("active"); self.advance(3.0)
        for _ in range(6):
            self.see(); self.advance(1.0)
        self.pir("inactive", duration=10.0)
        self.advance(20.0)
        self.assertEqual(len(self.clips), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_it_parks_without_commanding_a_return(self):
        self.pir("active"); self.advance(3.0)
        self.see(); self.pir("inactive", duration=5.0); self.advance(20.0)
        self.assertIn(self.c.state, (State.PARKED, State.HOLDING))
        self.assertEqual(self.ptz.gotos, [])

    def test_no_endless_retry_loop(self):
        """Every tick used to call goto_preset and log a warning."""
        self.pir("active")
        self.advance(60.0)
        self.assertEqual(self.ptz.gotos, [])




class TestStatusCannotWedgeTheLoop(Base):
    """The panel polls status every two seconds. It must never be able to
    block the detection loop, whatever the objects it asks are doing."""

    def _call_with_timeout(self, fn, seconds=3.0):
        import threading
        done = []
        t = threading.Thread(target=lambda: done.append(fn()), daemon=True)
        t.start()
        t.join(timeout=seconds)
        self.assertTrue(done, "status() deadlocked")
        return done[0]

    def test_status_returns_promptly(self):
        s = self._call_with_timeout(self.c.status)
        for k in ("state", "armed", "detection_enabled"):
            self.assertIn(k, s)

    def test_a_broken_component_does_not_break_status(self):
        class Exploding:
            def status(self):
                raise RuntimeError("boom")
        self.c.announcer = Exploding()
        s = self._call_with_timeout(self.c.status)
        self.assertIn("state", s, "one bad component must not lose the rest")

    def test_status_does_not_block_ticking(self):
        """The real failure: a status call held the controller lock while
        waiting on another object, and tick() then blocked on that lock."""
        import threading
        stop = threading.Event()

        class SlowAnnouncer:
            def status(self):
                stop.wait(1.5)          # holds nothing, just slow
                return {"announce_enabled": False}

        self.c.announcer = SlowAnnouncer()
        t = threading.Thread(target=self.c.status, daemon=True)
        t.start()
        ticked = []
        t2 = threading.Thread(target=lambda: (self.c.tick(), ticked.append(1)),
                              daemon=True)
        t2.start()
        t2.join(timeout=2.0)
        stop.set()
        self.assertTrue(ticked, "tick() blocked behind a slow status call")




class TestCaptureStateMachine(Base):
    """ready / triggered, and what each does to the segments on disk."""

    class FakeCapture:
        def __init__(self):
            self.state = "ready"
            self.keep_from = None
            self.transitions = []

        def set_triggered(self, keep_from):
            self.state = "triggered"
            self.keep_from = keep_from
            self.transitions.append("triggered")

        def set_ready(self):
            self.state = "ready"
            self.keep_from = None
            self.transitions.append("ready")

    def setUp(self):
        super().setUp()
        self.cap = self.FakeCapture()
        # The spec's minute, rather than the 15s the other tests use.
        self.cfg = Cfg(controller={"quiet_period_s": 60.0})
        self.c = Controller(
            self.cfg, self.ptz, self.sched, self.policy, self.shadow,
            clip_fn=lambda s, e: self.clips.append((s, e)) or "c.mp4",
            snapshot_fn=lambda: "snap.jpg", capture=self.cap,
            clock=lambda: self.now, wall=lambda: self.wall)

    def test_starts_ready(self):
        self.assertEqual(self.cap.state, "ready")
        self.assertFalse(self.c.triggered)

    def test_an_event_makes_it_triggered(self):
        self.see()
        self.assertEqual(self.cap.state, "triggered")
        self.assertTrue(self.c.triggered)

    def test_the_keep_window_starts_before_the_first_sighting(self):
        first = self.wall
        self.see()
        self.assertLess(self.cap.keep_from, first,
                        "the window must include the pre-roll")

    def test_a_further_event_extends_rather_than_re_triggers(self):
        self.see()
        for _ in range(5):
            self.advance(20.0)
            self.see()
            self.assertEqual(self.cap.state, "triggered")
        self.assertEqual(self.cap.transitions.count("triggered"), 1)

    def test_a_quiet_minute_returns_it_to_ready(self):
        self.see()
        self.advance(30.0)
        self.assertEqual(self.cap.state, "triggered", "30s is not a minute")
        self.advance(35.0)
        self.assertEqual(self.cap.state, "ready")

    def test_the_clip_is_cut_before_the_state_is_released(self):
        self.see()
        self.advance(65.0)
        self.assertEqual(len(self.clips), 1)
        self.assertEqual(self.cap.transitions, ["triggered", "ready"])

    def test_a_second_incident_triggers_again(self):
        self.see(); self.advance(65.0)
        self.see(); self.advance(65.0)
        self.assertEqual(self.cap.transitions,
                         ["triggered", "ready", "triggered", "ready"])
        self.assertEqual(len(self.clips), 2)




class TestPirTriggersCapture(Base):
    """The PIR and the detector do the same job: they say something is
    happening. They must extend one window, not run two."""

    def test_pir_alone_opens_an_incident(self):
        self.pir("active")
        self.assertTrue(self.c.triggered,
                        "a PIR event must start a capture on its own")

    def test_pir_alone_still_cuts_a_clip(self):
        self.pir("active")
        self.pir("inactive", duration=2.0)
        self.advance(20.0)
        self.assertEqual(len(self.clips), 1,
                         "a trigger is for getting a clip, seen or not")

    def test_pir_alone_does_not_alert(self):
        self.pir("active")
        self.pir("inactive", duration=2.0)
        self.advance(20.0)
        row = self.rows()[0]
        self.assertFalse(row["would_alert"])
        self.assertEqual(row["failed"], "persistence")
        self.assertEqual(row["sightings"], 0)

    def test_a_held_pir_keeps_the_window_open(self):
        self.pir("active")
        self.advance(120.0)
        self.assertTrue(self.c.triggered,
                        "while the PIR is still active nothing should close")
        self.pir("inactive", duration=120.0)
        self.advance(20.0)
        self.assertFalse(self.c.triggered)

    def test_detection_and_pir_share_one_window(self):
        self.pir("active")
        self.advance(2.0)
        self.see()
        self.pir("inactive", duration=5.0)
        self.advance(20.0)
        self.assertEqual(len(self.clips), 1, "one window, one clip")
        self.assertEqual(len(self.rows()), 1)

    def test_a_sighting_extends_a_pir_window(self):
        self.pir("active")
        self.pir("inactive", duration=1.0)
        self.advance(10.0)
        self.see()
        self.advance(10.0)
        self.assertTrue(self.c.triggered, "the sighting should have extended it")


class TestAnInterruptedIncident(Base):
    """Stopping mid-incident must not lose the event.

    Observed on the board: 31 stills exist for 13:22:00..13:22:30 and there is
    no clip, no sidecar and no annotated file. The service was restarted about
    40 seconds before the quiet period would have closed the window, and the
    incident only ever lived in memory.
    """

    def setUp(self):
        super().setUp()
        self.marks = []
        self.c.mark_open_fn = lambda start: self.marks.append(("open", start))
        self.c.mark_done_fn = lambda: self.marks.append(("done", None))

    def test_closing_on_shutdown_cuts_the_clip(self):
        self.see()
        self.advance(5)
        self.assertTrue(self.c.triggered, "the window is open")
        self.assertEqual(self.clips, [], "nothing cut while it is still open")

        closed = self.c.close_open_incident("service is stopping")

        self.assertTrue(closed, "there was an incident to close")
        self.assertEqual(len(self.clips), 1,
                         "the interrupted window still gets its clip")
        self.assertFalse(self.c.triggered)

    def test_closing_when_nothing_is_open_is_a_no_op(self):
        self.assertFalse(self.c.close_open_incident("service is stopping"))
        self.assertEqual(self.clips, [], "no phantom clip on a quiet shutdown")

    def test_opening_marks_disk_so_a_kill_is_recoverable(self):
        self.see()
        self.advance(1)
        kinds = [k for k, _ in self.marks]
        self.assertEqual(kinds, ["open"],
                         "a kill -9 here leaves the marker behind")

    def test_the_marker_is_cleared_once_the_clip_is_requested(self):
        self.see()
        self.advance(1)
        self.c.close_open_incident("service is stopping")
        self.assertEqual([k for k, _ in self.marks], ["open", "done"],
                         "the sidecar stands in for the marker from here")

    def test_the_marker_covers_the_pre_roll(self):
        self.see()
        self.advance(1)
        start = self.marks[0][1]
        self.assertLessEqual(
            start, self.c_first_seen(),
            "recovery must reach back far enough to include the approach")

    def c_first_seen(self):
        return self.wall - dt.timedelta(seconds=1)

    def test_a_marker_write_that_fails_does_not_stop_the_incident(self):
        def boom(_start):
            raise OSError("read-only filesystem")
        self.c.mark_open_fn = boom
        self.see()
        self.advance(1)
        self.assertTrue(self.c.triggered,
                        "bookkeeping must never cost us the recording")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class FakeSweepSettings:
    """The slice of Settings the controller reads for the trigger-sweep."""
    def __init__(self, enabled=True, left=True, right=True, dwell=4.0):
        self.sweep_enabled = enabled
        self._left = left
        self._right = right
        self.sweep_dwell_s = dwell

    @property
    def sweep_ready(self):
        return self._left and self._right


class TestTriggerSweep(unittest.TestCase):
    """When configured, a trigger makes the camera oscillate between two saved
    endpoints, covering a scene wider than one view, and comes home when quiet.
    """
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.wall = ARMED_NIGHT
        # No scan_presets: a sweep-only install, which is the real board.
        self.cfg = Cfg(ptz={"scan_presets": [], "home_preset": "Home",
                            "dwell_s": 4.0, "settle_s": 0.5})
        self.ptz = FakePTZ()
        self.sched = Schedule.from_config({"armed": [
            {"days": "all", "from": "22:00", "to": "08:00"}]})
        self.policy = AlertPolicy(self.cfg, self.sched, clock=lambda: self.wall)
        self.shadow = ShadowLog(Path(self.tmp.name) / "shadow")
        self.settings = FakeSweepSettings()
        self.c = Controller(
            self.cfg, self.ptz, self.sched, self.policy, self.shadow,
            clip_fn=lambda s, e: "c.mp4", snapshot_fn=lambda: "snap.jpg",
            settings=self.settings, clock=lambda: self.now,
            wall=lambda: self.wall)

    def tearDown(self):
        self.tmp.cleanup()

    def advance(self, seconds, step=0.1):
        end = self.now + seconds
        while self.now < end:
            self.now = round(self.now + step, 4)
            self.wall = self.wall + dt.timedelta(seconds=step)
            self.c.tick()

    def see(self):
        self.c.on_detection(Detection(self.wall, 1, 0.9, ["person"]))

    def test_detection_starts_a_sweep_not_a_hold(self):
        self.see()
        self.assertIs(self.c.state, State.SWEEPING)

    def test_oscillates_between_the_two_endpoints(self):
        self.see()
        # keep the scene "busy" so the sweep continues
        for _ in range(30):
            self.see()
            self.advance(1.0)
        seq = [g for g in self.ptz.gotos if g in ("SweepLeft", "SweepRight")]
        self.assertGreaterEqual(len(seq), 3)
        self.assertEqual(seq[0], "SweepLeft")
        # it must actually alternate, not sit on one side
        self.assertIn("SweepRight", seq)
        for a, b in zip(seq, seq[1:]):
            self.assertNotEqual(a, b, "sweep repeated the same endpoint")

    def test_returns_home_when_the_scene_goes_quiet(self):
        self.see()
        self.advance(2.0)
        self.assertIs(self.c.state, State.SWEEPING)
        # no more sightings; after the quiet period it should give up and home
        self.advance(20.0)
        self.assertIn(self.c.state, (State.RETURNING, State.PARKED))
        self.assertIn("Home", self.ptz.gotos)

    def test_disabled_falls_back_to_normal_behaviour(self):
        self.settings.sweep_enabled = False
        self.see()
        self.assertIsNot(self.c.state, State.SWEEPING)

    def test_not_ready_does_not_sweep(self):
        # enabled but only one endpoint saved
        self.settings._right = False
        self.see()
        self.assertIsNot(self.c.state, State.SWEEPING)
        self.assertNotIn("SweepLeft", self.ptz.gotos)

    def test_sweep_once_runs_a_single_cycle_and_homes(self):
        ok = self.c.sweep_once()
        self.assertTrue(ok)
        self.assertIs(self.c.state, State.SWEEPING)
        # let the one cycle play out and settle
        self.advance(30.0)
        seq = [g for g in self.ptz.gotos if g in ("SweepLeft", "SweepRight")]
        self.assertEqual(seq, ["SweepLeft", "SweepRight"],
                         "a one-shot must visit each end exactly once")
        self.assertIn("Home", self.ptz.gotos)
        self.assertIn(self.c.state, (State.PARKED, State.RETURNING))

    def test_sweep_once_works_even_when_disabled(self):
        # It is a manual check; the automatic toggle should not gate it.
        self.settings.sweep_enabled = False
        self.assertTrue(self.c.sweep_once())
        self.assertIs(self.c.state, State.SWEEPING)

    def test_manual_sweep_does_not_count_as_a_scan_start(self):
        # else the min-scan-interval throttle would refuse a real trigger that
        # arrives just after a manual test sweep.
        self.c.sweep_once()
        self.advance(30.0)
        self.assertNotIn(True, self.c.ptz.scan_starts,
                         "a manual one-shot must not register a scan start")

    def test_sweep_once_refused_without_endpoints(self):
        self.settings._left = False
        self.assertFalse(self.c.sweep_once())
        self.assertIsNot(self.c.state, State.SWEEPING)

    def test_budget_refusal_ends_the_sweep(self):
        self.ptz.refuse = True
        self.see()
        self.advance(2.0)
        # a refused move must not leave it wedged in SWEEPING
        self.assertIn(self.c.state, (State.RETURNING, State.PARKED))
