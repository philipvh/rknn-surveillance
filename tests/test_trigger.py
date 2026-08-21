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

"""Tests for the PIR trigger input.

Driven through poll_once() with a fake clock, so bounce, blips and stuck
relays are exact rather than approximate.
"""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trigger import TriggerInput, TriggerEvent, BackendUnavailable, open_backend  # noqa: E402


class FakeLine:
    """A GPIO line whose raw value the test drives directly."""

    def __init__(self, raw=1):
        self.raw = raw          # 1 = open contact (pulled up) = idle
        self.fail = False
        self.closed = False

    def read(self):
        if self.fail:
            raise OSError("simulated GPIO read failure")
        return self.raw

    def close(self):
        self.closed = True

    # convenience for tests
    def close_contact(self):
        self.raw = 0            # active, with active_low

    def open_contact(self):
        self.raw = 1


CONF = {
    "enabled": True, "chip": "gpiochip0", "line": 17, "active_low": True,
    "poll_interval_s": 0.05, "debounce_s": 0.2, "min_active_s": 0.3,
    "stuck_after_s": 1800,
}


class Base(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.line = FakeLine()
        self.events = []
        self.t = TriggerInput(dict(CONF), backend=self.line,
                              on_event=self.events.append,
                              clock=lambda: self.now,
                              wall_clock=lambda: self.now)

    def tick(self, seconds, step=0.05):
        """Advance time, polling as the real loop would."""
        end = self.now + seconds
        while self.now < end:
            self.now = round(self.now + step, 6)
            self.t.poll_once()

    def kinds(self):
        return [e.kind for e in self.events]


class TestBasicSignal(unittest.TestCase):
    def test_idle_line_produces_nothing(self):
        b = Base("run"); b.setUp()
        b.tick(5)
        self.assertEqual(b.events, [])
        self.assertFalse(b.t.active)


class TestActivation(Base):
    def test_contact_closing_raises_an_active_event(self):
        self.line.close_contact()
        self.tick(0.5)
        self.assertEqual(self.kinds(), ["active"])
        self.assertTrue(self.t.active)

    def test_activation_waits_for_the_debounce_period(self):
        self.line.close_contact()
        self.tick(0.15)                    # debounce is 0.2s
        self.assertEqual(self.events, [], "must not fire before it settles")
        self.tick(0.15)
        self.assertEqual(self.kinds(), ["active"])

    def test_release_reports_how_long_it_was_active(self):
        self.line.close_contact()
        self.tick(0.5)
        self.tick(30)                      # PIR holds the lights on
        self.line.open_contact()
        self.tick(0.5)
        self.assertEqual(self.kinds(), ["active", "inactive"])
        held = self.events[1].duration
        self.assertGreater(held, 29)
        self.assertLess(held, 32)

    def test_active_duration_is_readable_while_active(self):
        self.line.close_contact()
        self.tick(0.5)
        self.tick(10)
        self.assertGreater(self.t.active_duration(), 9.5)


class TestBounce(Base):
    def test_contact_bounce_produces_one_event_not_many(self):
        # A relay chattering for 100ms before settling closed.
        for _ in range(10):
            self.line.raw = 0
            self.tick(0.01, step=0.005)
            self.line.raw = 1
            self.tick(0.01, step=0.005)
        self.line.close_contact()
        self.tick(0.5)
        self.assertEqual(self.kinds(), ["active"],
                         f"bounce should collapse to one event, got {self.kinds()}")

    def test_a_brief_glitch_never_activates(self):
        self.line.close_contact()
        self.tick(0.1)                     # shorter than debounce
        self.line.open_contact()
        self.tick(1.0)
        self.assertEqual(self.events, [])
        self.assertFalse(self.t.active)

    def test_bounce_on_release_produces_one_inactive(self):
        self.line.close_contact()
        self.tick(1.0)
        for _ in range(8):
            self.line.raw = 1
            self.tick(0.01, step=0.005)
            self.line.raw = 0
            self.tick(0.01, step=0.005)
        self.line.open_contact()
        self.tick(0.5)
        self.assertEqual(self.kinds(), ["active", "inactive"])


class TestBlipFiltering(Base):
    def test_short_activation_is_dropped_as_a_blip(self):
        self.line.close_contact()
        self.tick(0.25)                    # settles active at ~0.2s
        self.assertEqual(self.kinds(), ["active"])
        self.line.open_contact()
        self.tick(0.5)
        # active lasted ~0.25s, under min_active_s of 0.3
        self.assertEqual(self.kinds(), ["active"],
                         "a sub-threshold activation should not report a release")
        self.assertEqual(self.t.rejected_blips, 1)

    def test_a_real_activation_is_not_dropped(self):
        self.line.close_contact()
        self.tick(2.0)
        self.line.open_contact()
        self.tick(0.5)
        self.assertEqual(self.kinds(), ["active", "inactive"])
        self.assertEqual(self.t.rejected_blips, 0)


class TestStuckRelay(Base):
    def test_warns_once_when_the_line_never_releases(self):
        self.t.stuck_after_s = 60
        self.line.close_contact()
        self.tick(1.0)
        self.tick(120, step=1.0)
        self.assertEqual(self.kinds().count("stuck"), 1,
                         "a stuck line must warn once, not every poll")
        self.assertIn("stuck relay", self.events[-1].detail)

    def test_stuck_flag_resets_for_the_next_activation(self):
        self.t.stuck_after_s = 60
        self.line.close_contact(); self.tick(1.0); self.tick(120, step=1.0)
        self.line.open_contact(); self.tick(1.0)
        self.line.close_contact(); self.tick(1.0); self.tick(120, step=1.0)
        self.assertEqual(self.kinds().count("stuck"), 2)


class TestActiveHigh(unittest.TestCase):
    def test_active_high_wiring_is_supported(self):
        line = FakeLine(raw=0)
        events = []
        now = [1000.0]
        conf = dict(CONF, active_low=False)
        t = TriggerInput(conf, backend=line, on_event=events.append,
                         clock=lambda: now[0], wall_clock=lambda: now[0])
        for _ in range(20):
            now[0] += 0.05
            t.poll_once()
        self.assertEqual(events, [], "0 must be idle when active_low is false")
        line.raw = 1
        for _ in range(20):
            now[0] += 0.05
            t.poll_once()
        self.assertEqual([e.kind for e in events], ["active"])


class TestFailureModes(Base):
    def test_read_errors_do_not_raise_or_change_state(self):
        self.line.close_contact()
        self.tick(1.0)
        self.assertTrue(self.t.active)
        self.line.fail = True
        self.tick(5.0)                     # must not raise
        self.assertTrue(self.t.active, "a read failure must not fake a release")
        self.assertGreater(self.t.read_errors, 0)

    def test_recovers_when_reads_start_working_again(self):
        self.line.fail = True
        self.tick(1.0)
        self.line.fail = False
        self.line.close_contact()
        self.tick(1.0)
        self.assertEqual(self.kinds(), ["active"])

    def test_missing_hardware_is_not_fatal(self):
        t = TriggerInput(dict(CONF))       # no backend, real open() will fail
        self.assertFalse(t.open(), "opening a nonexistent chip must return False")
        self.assertFalse(t.available)
        self.assertIsNotNone(t.error)
        t.run()                            # must return immediately, not raise

    def test_disabled_in_config_is_quiet(self):
        t = TriggerInput(dict(CONF, enabled=False))
        self.assertFalse(t.open())
        self.assertFalse(t.available)

    def test_open_backend_reports_what_it_tried(self):
        with self.assertRaises(BackendUnavailable) as e:
            open_backend("gpiochip-does-not-exist", 17)
        self.assertTrue(str(e.exception))

    def test_callback_exception_does_not_kill_the_watcher(self):
        def boom(ev):
            raise RuntimeError("subscriber blew up")
        t = TriggerInput(dict(CONF), backend=self.line, on_event=boom,
                         clock=lambda: self.now, wall_clock=lambda: self.now)
        self.line.close_contact()
        for _ in range(20):
            self.now += 0.05
            t.poll_once()                  # must not raise
        self.assertTrue(t.active)


class TestStatus(Base):
    def test_status_reports_what_an_operator_needs(self):
        self.line.close_contact()
        self.tick(2.0)
        s = self.t.status()
        self.assertTrue(s["available"])
        self.assertTrue(s["active"])
        self.assertGreater(s["active_for_s"], 1.0)
        self.assertEqual(s["activations"], 1)




class TestThreadHygiene(unittest.TestCase):
    """Regression: TriggerInput used to store self._stop, which shadows
    threading.Thread._stop -- an internal method that join() calls. Every
    clean shutdown crashed with "'Event' object is not callable", which meant
    every systemd restart would have too. Found on the board, not in the lab.
    """

    def test_start_and_join_do_not_raise(self):
        t = TriggerInput(dict(CONF, enabled=False))
        t.start()
        t.stop()
        t.join(timeout=2)          # this is what used to explode
        self.assertFalse(t.is_alive())

    def test_no_thread_internals_are_shadowed(self):
        import threading
        # Only methods matter: Thread.__init__ legitimately sets instance
        # attributes like _initialized. The bug is replacing a method the
        # machinery calls -- join() calls self._stop() -- with a non-callable.
        reserved = {n for n in dir(threading.Thread)
                    if n.startswith("_") and callable(getattr(threading.Thread, n, None))}
        t = TriggerInput(dict(CONF, enabled=False))
        clashes = sorted(n for n in set(vars(t)) & reserved
                         if not callable(vars(t)[n]))
        self.assertEqual(clashes, [],
                         f"these attributes shadow Thread methods: {clashes}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
