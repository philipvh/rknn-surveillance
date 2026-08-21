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

"""Tests for clock sanity, the systemd watchdog, and health aggregation."""
import datetime as dt, json, socket, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from health import ClockGuard, HealthMonitor, SystemdNotifier, EPOCH_SANITY  # noqa: E402


class FakeRecorder:
    def __init__(self, healthy=True):
        self.healthy = healthy
        self.name_ = "fake"


class Cfg:
    def __init__(self, d=None):
        self.d = d or {"health": {"stale_frames_s": 120, "disk_warn_percent": 90}}

    def _get(self, *keys, default=None):
        node = self.d
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


class TestClockGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "clock.json"

    def tearDown(self):
        self.tmp.cleanup()

    def guard(self, now):
        return ClockGuard(self.path, write_interval_s=0.0, now=lambda: now)

    def test_first_run_is_clean(self):
        g = self.guard(dt.datetime(2026, 8, 19, 12, 0))
        self.assertTrue(g.ok)
        self.assertIsNone(g.last_known)

    def test_it_records_the_time(self):
        g = self.guard(dt.datetime(2026, 8, 19, 12, 0))
        g.tick()
        self.assertTrue(self.path.exists())
        self.assertIn("last_seen", json.loads(self.path.read_text()))

    def test_a_clock_that_went_backwards_is_detected(self):
        g = self.guard(dt.datetime(2026, 8, 19, 12, 0))
        g.tick()
        later = self.guard(dt.datetime(2026, 8, 18, 9, 0))    # a day earlier
        self.assertFalse(later.ok)
        self.assertGreater(later.regressed_by_s, 3600)

    def test_a_clock_that_never_got_set_is_detected(self):
        g = self.guard(dt.datetime(1970, 1, 2))
        self.assertTrue(g.implausible)
        self.assertFalse(g.ok)

    def test_normal_forward_progress_is_fine(self):
        self.guard(dt.datetime(2026, 8, 19, 12, 0)).tick()
        g = self.guard(dt.datetime(2026, 8, 19, 13, 0))
        self.assertTrue(g.ok)

    def test_it_never_records_a_time_earlier_than_one_it_has(self):
        self.guard(dt.datetime(2026, 8, 19, 12, 0)).tick()
        bad = self.guard(dt.datetime(2020, 1, 1))
        bad.tick()
        saved = dt.datetime.fromisoformat(
            json.loads(self.path.read_text())["last_seen"])
        self.assertEqual(saved.year, 2026,
                         "a bad clock must not destroy the last good reading")

    def test_a_corrupt_state_file_is_survivable(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ not json")
        g = self.guard(dt.datetime(2026, 8, 19, 12, 0))
        self.assertIsNone(g.last_known)
        self.assertTrue(g.ok)

    def test_status_is_reportable(self):
        self.guard(dt.datetime(2026, 8, 19, 12, 0)).tick()
        s = self.guard(dt.datetime(2026, 8, 18)).status()
        self.assertFalse(s["clock_ok"])
        self.assertGreater(s["clock_regressed_s"], 0)


class TestSystemdNotifier(unittest.TestCase):
    def test_inert_outside_systemd(self):
        n = SystemdNotifier(env={})
        self.assertFalse(n.enabled)
        self.assertFalse(n.ready())
        self.assertFalse(n.watchdog())
        self.assertIsNone(n.watchdog_interval_s)

    def test_it_actually_sends_a_datagram(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "notify.sock")
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            srv.bind(path)
            srv.settimeout(2)
            try:
                n = SystemdNotifier(env={"NOTIFY_SOCKET": path,
                                         "WATCHDOG_USEC": "30000000"})
                self.assertTrue(n.enabled)
                self.assertTrue(n.ready())
                self.assertEqual(srv.recv(64), b"READY=1")
                self.assertTrue(n.watchdog())
                self.assertEqual(srv.recv(64), b"WATCHDOG=1")
            finally:
                srv.close()

    def test_ping_interval_is_half_the_configured_watchdog(self):
        n = SystemdNotifier(env={"NOTIFY_SOCKET": "/nonexistent",
                                 "WATCHDOG_USEC": "30000000"})
        self.assertAlmostEqual(n.watchdog_interval_s, 15.0)

    def test_a_bad_watchdog_value_does_not_crash(self):
        n = SystemdNotifier(env={"NOTIFY_SOCKET": "/nonexistent",
                                 "WATCHDOG_USEC": "not a number"})
        self.assertIsNone(n.watchdog_interval_s)


class FakeNotifier:
    def __init__(self, interval=1.0):
        self.watchdog_interval_s = interval
        self.pings = 0
        self.enabled = True

    def watchdog(self):
        self.pings += 1
        return True

    def ready(self):
        return True

    def status(self, t):
        return True


class TestHealthMonitor(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.notifier = FakeNotifier()
        self.rec = FakeRecorder()
        self.h = HealthMonitor(Cfg(), recorders=[self.rec],
                               notifier=self.notifier,
                               clock=lambda: self.now[0])

    def test_pings_while_frames_arrive(self):
        for _ in range(5):
            self.h.note_frame()
            self.now[0] += 2
            self.h.tick()
        self.assertGreater(self.notifier.pings, 0)

    def test_stops_pinging_when_frames_stop(self):
        """A watchdog that always pings is a watchdog that never fires."""
        self.h.note_frame()
        self.now[0] += 2
        self.h.tick()
        before = self.notifier.pings
        self.now[0] += 300                       # stale_frames_s is 120
        for _ in range(5):
            self.now[0] += 2
            self.h.tick()
        self.assertEqual(self.notifier.pings, before,
                         "no frames must mean no ping, so systemd restarts us")
        self.assertFalse(self.h.alive())

    def test_recovers_when_frames_return(self):
        self.now[0] += 300
        self.h.tick()
        self.assertFalse(self.h.alive())
        self.h.note_frame()
        self.assertTrue(self.h.alive())

    def test_a_grace_period_after_start(self):
        self.assertTrue(self.h.frames_fresh(),
                        "starting up is not the same as being wedged")
        self.now[0] += 300
        self.assertFalse(self.h.frames_fresh())

    def test_a_full_disk_does_not_trigger_restarts(self):
        """Restarting will not empty a disk; it should be reported instead."""
        self.h.note_frame()
        self.h.disk_warn_percent = 0             # everything counts as full
        self.assertTrue(self.h.alive())
        self.assertFalse(self.h.status()["healthy"])

    def test_an_unhealthy_recorder_shows_in_status(self):
        self.h.note_frame()
        self.rec.healthy = False
        s = self.h.status()
        self.assertFalse(s["recorders_ok"])
        self.assertFalse(s["healthy"])

    def test_a_bad_clock_makes_status_unhealthy(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            ClockGuard(p, 0.0, now=lambda: dt.datetime(2026, 8, 19, 12)).tick()
            g = ClockGuard(p, 0.0, now=lambda: dt.datetime(2026, 8, 18))
            h = HealthMonitor(Cfg(), clock_guard=g, notifier=self.notifier,
                              clock=lambda: self.now[0])
            h.note_frame()
            s = h.status()
            self.assertFalse(s["clock_ok"])
            self.assertFalse(s["healthy"])

    def test_status_includes_what_the_panel_needs(self):
        self.h.note_frame()
        s = self.h.status()
        for k in ("frames_fresh", "recorders_ok", "disk_percent",
                  "disk_free_gb", "uptime_s", "healthy"):
            self.assertIn(k, s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
