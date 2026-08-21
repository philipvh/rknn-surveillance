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

"""Tests for the PTZ driver.

These exist because of one fact about this camera: every move command runs
until something stops it. The tests below are the evidence that "something"
always exists.
"""
import sys, tempfile, threading, time, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config, ptz  # noqa: E402
from ptz import PTZ, MotorBudget, BudgetExceeded, PTZError  # noqa: E402

CFG = """
camera: {{host: 10.0.0.5, http_port: 88, user: admin,
         main_path: videoMain, sub_path: videoSub}}
detection: {{source: sub, trigger_classes: [person]}}
trigger: {{quiet_period_s: 15, pre_roll_s: 2, post_roll_s: 15, max_duration_min: 10}}
recording:
  segment_seconds: 60
  tiers:
    - {{name: main, stream: main, path: {root}/recordings/main, max_age_days: 2}}
    - {{name: events, stream: null, path: {root}/events, max_age_days: 730, protected: true}}
retention: {{target_free_percent: 20}}
paths: {{events_root: {root}/events, detections_root: {root}/detections}}
ptz:
  home_preset: Home
  move_deadline_s: 0.2
  watchdog_interval_s: 0.02
  max_continuous_move_s: 1.0
  preset_move_estimate_s: 3.0
  settle_s: 0.5
  budget: {{auto_seconds_per_hour: 10, manual_seconds_per_hour: 30,
           min_scan_interval_s: 60}}
  http: {{timeout_s: 1.0, stop_timeout_s: 0.5, stop_retries: 3,
         stop_backoff_base_s: 0.05, stop_backoff_max_s: 0.2}}
"""

PRESET_XML = (b"<CGI_Result><result>0</result><cnt>3</cnt>"
              b"<point0>Home</point0><point1>Court1</point1>"
              b"<point2>Gate</point2></CGI_Result>")
OK_XML = b"<CGI_Result><result>0</result></CGI_Result>"


class FakeCamera:
    """Records every command, and can be told to fail or stall."""

    def __init__(self):
        self.calls = []
        self.fail_cmds = set()
        self.fail_times = {}          # cmd -> how many more times to fail
        self.lock = threading.Lock()
        self.delay = 0.0

    def get(self, url, params, timeout):
        cmd = params.get("cmd")
        with self.lock:
            self.calls.append(cmd)
            n = self.fail_times.get(cmd, 0)
            if n:
                self.fail_times[cmd] = n - 1
                fail = True
            else:
                fail = cmd in self.fail_cmds
        if self.delay:
            time.sleep(self.delay)
        if fail:
            raise OSError(f"simulated network failure on {cmd}")
        if cmd == "getPTZPresetPointList":
            return PRESET_XML
        if cmd == "snapPicture2":
            return b"\xff\xd8\xff\xe0JFIF-ish"
        return OK_XML

    def count(self, cmd):
        with self.lock:
            return self.calls.count(cmd)

    def clear(self):
        with self.lock:
            self.calls.clear()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "config.yaml").write_text(CFG.format(root=root))
        sec = root / "secrets.yaml"
        sec.write_text('camera:\n  password: "pw"\n')
        sec.chmod(0o600)
        self.cfg = config.load(root / "config.yaml", sec)
        self.cam = FakeCamera()

    def tearDown(self):
        self.tmp.cleanup()

    def ptz(self, **kw):
        kw.setdefault("install_signal_handlers", False)
        p = PTZ(self.cfg, transport=self.cam, **kw)
        self.addCleanup(lambda: setattr(p, "_closed", True))
        return p


class TestStartupRescue(Base):
    def test_stops_the_camera_before_doing_anything_else(self):
        p = self.ptz()
        self.assertEqual(self.cam.calls[0], "ptzStopRun",
                         "a previous instance may have been killed mid-move; "
                         "the first thing a new one must do is stop the camera")

    def test_startup_stop_failure_is_not_fatal(self):
        self.cam.fail_cmds.add("ptzStopRun")
        p = self.ptz()                       # must not raise
        self.cam.fail_cmds.clear()
        time.sleep(0.6)
        self.assertGreater(self.cam.count("ptzStopRun"), 3,
                           "the watchdog should keep retrying an unconfirmed "
                           "stop, albeit with a growing backoff")


class TestWatchdog(Base):
    def test_unrefreshed_move_is_stopped(self):
        p = self.ptz(); self.cam.clear()
        p.move("left", source="manual")
        self.assertTrue(p.moving)
        time.sleep(0.5)                      # deadline is 0.2s
        self.assertFalse(p.moving)
        self.assertGreaterEqual(self.cam.count("ptzStopRun"), 1)
        self.assertGreaterEqual(p.watchdog_stops, 1)

    def test_refreshing_keeps_it_moving(self):
        p = self.ptz(); self.cam.clear()
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            p.move("left", source="manual")
            time.sleep(0.05)
        self.assertTrue(p.moving, "held button should still be moving")
        self.assertEqual(self.cam.count("ptzMoveLeft"), 1,
                         "refreshes must extend the deadline, not re-send")
        p.stop()

    def test_hard_ceiling_beats_refreshes(self):
        """A stuck keepalive loop must not pan forever."""
        p = self.ptz(); self.cam.clear()
        start = time.monotonic()
        stopped_by_ceiling = False
        while time.monotonic() - start < 2.0:
            try:
                p.move("right", source="manual")
            except BudgetExceeded as e:
                if "single move" in str(e):
                    stopped_by_ceiling = True
                    break
            time.sleep(0.05)
        self.assertTrue(stopped_by_ceiling or not p.moving,
                        "max_continuous_move_s must terminate an endless hold")
        self.assertFalse(p.moving)

    def test_failed_stop_is_retried_until_it_works(self):
        p = self.ptz(); self.cam.clear()
        self.cam.fail_times["ptzStopRun"] = 4      # more than stop_retries
        p.move("up", source="manual")
        with self.assertRaises(PTZError):
            p.stop()
        self.assertTrue(p._stop_pending, "an unconfirmed stop must stay pending")
        time.sleep(1.0)
        self.assertFalse(p._stop_pending, "watchdog should have completed the stop")


class TestShutdown(Base):
    def test_context_manager_stops_on_exit(self):
        with PTZ(self.cfg, transport=self.cam,
                 install_signal_handlers=False) as p:
            self.cam.clear()
            p.move("left", source="manual")
            self.assertTrue(p.moving)
        self.assertGreaterEqual(self.cam.count("ptzStopRun"), 1,
                                "leaving the block must stop the camera")

    def test_close_is_idempotent(self):
        p = self.ptz()
        p.close(); p.close()


class TestBudget(Base):
    def test_refuses_when_the_hour_is_spent(self):
        b = MotorBudget(auto_s_per_hour=10, clock=lambda: 0.0)
        b.record(9.5, "auto")
        self.assertTrue(b.check(0.4, "auto").allowed)
        d = b.check(1.0, "auto")
        self.assertFalse(d.allowed)
        self.assertIn("budget spent", d.reason)

    def test_manual_has_its_own_larger_ceiling(self):
        b = MotorBudget(auto_s_per_hour=10, manual_s_per_hour=30,
                        clock=lambda: 0.0)
        b.record(10, "auto")
        self.assertFalse(b.check(1, "auto").allowed)
        self.assertTrue(b.check(1, "manual").allowed,
                        "an operator at the panel must not be locked out by "
                        "automatic scanning having used its quota")

    def test_window_rolls_forward(self):
        now = [0.0]
        b = MotorBudget(auto_s_per_hour=10, window_s=3600, clock=lambda: now[0])
        b.record(10, "auto")
        self.assertFalse(b.check(1, "auto").allowed)
        now[0] = 3601
        self.assertTrue(b.check(1, "auto").allowed)

    def test_scan_interval_floor(self):
        now = [1000.0]
        b = MotorBudget(min_scan_interval_s=60, clock=lambda: now[0])
        b.record(1, "auto", is_scan_start=True)
        d = b.check(1, "auto", is_scan_start=True)
        self.assertFalse(d.allowed)
        self.assertIn("minimum interval", d.reason)
        now[0] += 61
        self.assertTrue(b.check(1, "auto", is_scan_start=True).allowed)

    def test_over_budget_move_is_refused_and_camera_not_commanded(self):
        p = self.ptz(); self.cam.clear()
        p.budget.record(10, "auto")               # auto ceiling is 10s
        with self.assertRaises(BudgetExceeded):
            p.move("left", source="auto")
        self.assertEqual(self.cam.count("ptzMoveLeft"), 0,
                         "a refused move must never reach the camera")

    def test_budget_exhausted_mid_hold_stops_the_camera(self):
        p = self.ptz(); self.cam.clear()
        p.move("left", source="manual")
        p.budget.record(30, "manual")             # manual ceiling is 30s
        with self.assertRaises(BudgetExceeded):
            p.move("left", source="manual")
        self.assertFalse(p.moving)
        self.assertGreaterEqual(self.cam.count("ptzStopRun"), 1)


class TestCommands(Base):
    def test_presets_are_parsed_in_order(self):
        p = self.ptz()
        self.assertEqual(p.list_presets(), ["Home", "Court1", "Gate"])

    def test_goto_preset_sends_the_name(self):
        p = self.ptz()
        p.goto_preset("Court1")
        self.assertIn("ptzGotoPresetPoint", self.cam.calls)

    def test_go_home_uses_the_configured_preset(self):
        p = self.ptz()
        p.go_home()
        self.assertIn("ptzGotoPresetPoint", self.cam.calls)

    def test_unknown_direction_is_rejected_locally(self):
        p = self.ptz(); self.cam.clear()
        with self.assertRaises(PTZError):
            p.move("sideways")
        self.assertEqual(self.cam.calls, [], "must not ask the camera nonsense")

    def test_concurrent_stops_do_not_double_up(self):
        """The watchdog and a caller must not run two retry loops at once."""
        p = self.ptz(); self.cam.clear()
        p.move("left", source="manual")
        results = []
        threads = [threading.Thread(target=lambda: results.append(p.stop()))
                   for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertLessEqual(self.cam.count("ptzStopRun"), 2,
                             "four concurrent stops should not become four "
                             "rounds of traffic to the camera")
        self.assertTrue(any(r is True for r in results))

    def test_nonzero_result_is_an_error(self):
        p = self.ptz()
        with self.assertRaises(PTZError) as e:
            p._parse("ptzMoveLeft", b"<CGI_Result><result>-3</result></CGI_Result>")
        self.assertIn("result=-3", str(e.exception))

    def test_malformed_xml_still_yields_the_result(self):
        p = self.ptz()
        out = p._parse("x", b"junk<result>0</result>trailing")
        self.assertEqual(out["result"], 0)

    def test_snapshot_returns_bytes(self):
        p = self.ptz()
        self.assertTrue(p.snapshot().startswith(b"\xff\xd8"))

    def test_zoom_stop_also_sends_ptz_stop(self):
        p = self.ptz(); self.cam.clear()
        p.zoom("in", source="manual")
        p.stop()
        self.assertIn("zoomStop", self.cam.calls)
        self.assertIn("ptzStopRun", self.cam.calls)


class TestSettling(Base):
    def test_not_settled_while_moving(self):
        p = self.ptz()
        p.move("left", source="manual")
        self.assertFalse(p.settled(), "frames taken mid-pan are motion blurred")
        p.stop()

    def test_settles_after_the_configured_delay(self):
        clock = [1000.0]
        p = PTZ(self.cfg, transport=self.cam, clock=lambda: clock[0],
                install_signal_handlers=False)
        p.move("left", source="manual")
        p.stop()
        self.assertFalse(p.settled())
        clock[0] += 0.6
        self.assertTrue(p.settled())
        p._closed = True




class TestCLI(Base):
    """Drives ptz_cli end to end against the fake camera.

    'ptz goto Home works' is one of the things Phase 2 is judged on, so it is
    worth exercising the actual entry point rather than the library beneath it.
    """

    def setUp(self):
        super().setUp()
        import os
        self.root = Path(self.tmp.name)
        os.environ["RKNN_CONFIG"] = str(self.root / "config.yaml")
        os.environ["RKNN_SECRETS"] = str(self.root / "secrets.yaml")
        cam = self.cam
        self._real_transport = ptz.UrllibTransport
        ptz.UrllibTransport = lambda: cam
        self.addCleanup(self._restore)

    def _restore(self):
        import os
        ptz.UrllibTransport = self._real_transport
        os.environ.pop("RKNN_CONFIG", None)
        os.environ.pop("RKNN_SECRETS", None)

    def run_cli(self, *argv):
        import io, contextlib, ptz_cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = ptz_cli.main(list(argv))
        return rc, buf.getvalue()

    def test_goto_home(self):
        rc, out = self.run_cli("goto", "Home")
        self.assertEqual(rc, 0, out)
        self.assertIn("ptzGotoPresetPoint", self.cam.calls)

    def test_presets_lists_and_flags_missing_ones(self):
        rc, out = self.run_cli("presets")
        self.assertIn("Home", out)
        self.assertIn("Court1", out)
        # config.yaml in this fixture names no scan_presets, so nothing missing
        self.assertEqual(rc, 0, out)

    def test_move_stops_at_the_end(self):
        rc, out = self.run_cli("move", "left", "--duration", "0.2")
        self.assertEqual(rc, 0, out)
        self.assertIn("ptzMoveLeft", self.cam.calls)
        self.assertIn("ptzStopRun", self.cam.calls)
        self.assertEqual(self.cam.calls[-1], "ptzStopRun",
                         "the last thing a move command does is stop")

    def test_stop_subcommand_does_not_move_first(self):
        rc, out = self.run_cli("stop")
        self.assertEqual(rc, 0, out)
        self.assertTrue(all(c == "ptzStopRun" for c in self.cam.calls),
                        f"'stop' should only ever stop, got {self.cam.calls}")

    def test_move_refused_when_budget_spent_returns_distinct_code(self):
        rc, _ = self.run_cli("move", "left", "--duration", "0.2")
        self.assertEqual(rc, 0)
        # manual ceiling is 30s in the fixture; burn it
        rc, out = self.run_cli("move", "left", "--duration", "40")
        self.assertEqual(rc, 3, "a budget refusal needs its own exit code")
        self.assertIn("refused", out)

    def test_snapshot_writes_a_jpeg(self):
        out_path = self.root / "shot.jpg"
        rc, out = self.run_cli("snapshot", str(out_path))
        self.assertEqual(rc, 0, out)
        self.assertTrue(out_path.read_bytes().startswith(b"\xff\xd8"))

    def test_probe_reports_supported_commands(self):
        rc, out = self.run_cli("probe")
        self.assertEqual(rc, 0, out)
        self.assertIn("getDevState", out)
        self.assertIn("8/8", out)

    def test_selftest_requires_confirmation(self):
        rc, out = self.run_cli("selftest")
        self.assertEqual(rc, 2)
        self.assertIn("--yes", out)

    def test_selftest_passes_against_a_healthy_camera(self):
        rc, out = self.run_cli("selftest", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("SELFTEST PASSED", out)
        self.assertIn("kill -9", out, "must tell the operator what it cannot test")

    def test_selftest_fails_when_stops_do_not_work(self):
        self.cam.fail_cmds.add("ptzStopRun")
        rc, out = self.run_cli("selftest", "--yes")
        self.assertEqual(rc, 1, out)
        self.assertIn("SELFTEST FAILED", out)




class TestDisabledAndUnreachable(Base):
    """Bench finding: a camera that is absent or not a Foscam must not be
    hammered. Before this, an unreachable host produced 173 log lines a minute
    and the stop watchdog never backed off."""

    def test_disabled_ptz_sends_nothing(self):
        self.cfg.raw["ptz"]["enabled"] = False
        p = PTZ(self.cfg, transport=self.cam, install_signal_handlers=False)
        self.addCleanup(lambda: setattr(p, "_closed", True))
        self.assertFalse(p.enabled)
        self.assertEqual(self.cam.calls, [],
                         "a disabled PTZ must not even send the startup stop")
        with self.assertRaises(PTZError):
            p.move("left", source="manual")
        self.assertEqual(self.cam.calls, [])

    def test_repeated_stop_failure_backs_off(self):
        """An unreachable camera cost 173 log lines a minute before this."""
        self.cam.fail_cmds.add("ptzStopRun")
        p = self.ptz()
        self.addCleanup(lambda: setattr(p, "_closed", True))
        time.sleep(1.0)
        streak = p._stop_fail_streak
        self.assertGreater(streak, 0, "failures should be counted")
        self.assertGreater(p._retry_stop_after, 0,
                           "a backoff deadline should be set")
        # With the fixture's 0.05s base and 0.2s cap, an unbounded hammer at
        # the 0.02s watchdog interval would be far more than this.
        self.assertLess(self.cam.count("ptzStopRun"), 120,
                        "retries must thin out rather than run at the "
                        "watchdog interval")

    def test_a_successful_stop_clears_the_backoff(self):
        p = self.ptz()
        p._stop_fail_streak = 5
        p._retry_stop_after = 0
        p.move("left", source="manual")
        p.stop()
        self.assertEqual(p._stop_fail_streak, 0)




class TestDisabledIsQuiet(Base):
    def test_stopping_a_disabled_ptz_is_a_no_op(self):
        """Shutdown used to log five failed attempts and an error for a
        camera that had never been commanded."""
        self.cfg.raw["ptz"]["enabled"] = False
        p = PTZ(self.cfg, transport=self.cam, install_signal_handlers=False)
        self.addCleanup(lambda: setattr(p, "_closed", True))
        self.cam.clear()
        self.assertTrue(p.stop(reason="shutdown"))
        self.assertEqual(self.cam.calls, [])
        self.assertEqual(p.failed_stops, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
