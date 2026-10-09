# Copyright 2026 Philip van Houtte, magicview.tv, the Netherlands
# SPDX-License-Identifier: Apache-2.0
"""Waking the wall panel when the camera turns to look at somebody.

The screen is a deterrent, not a display: dark until something triggers, lit
while the camera is turning. That also stops the tablet outlasting its charger
-- it managed 28 hours and then 41 before dying, both times with the screen on.
"""

import datetime as dt
import json
import tempfile
import sys
import threading
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kiosk  # noqa: E402


class FakeHTTP:
    def __init__(self, *errors):
        self.errors = list(errors)
        self.urls = []
        self.done = threading.Event()

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        self.done.set()
        if self.errors:
            e = self.errors.pop(0)
            if e is not None:
                raise e

        class R:
            def read(self_inner):
                return b"OK"

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False
        return R()


class Base(unittest.TestCase):
    def build(self, http=None, **over):
        self.now = [1000.0]
        c = {"enabled": True, "host": "10.0.0.5", "password": "pw",
             "wake_seconds": 120, "min_interval_s": 20}
        c.update(over)
        self.http = http or FakeHTTP()
        return kiosk.Kiosk(c, opener=self.http, clock=lambda: self.now[0])

    def settle(self, k):
        """The calls are fired on their own thread; wait for the socket."""
        self.http.done.wait(2)
        for t in threading.enumerate():
            if t.name.startswith("kiosk-"):
                t.join(2)


class TestItOnlyActsWhenConfigured(Base):
    def test_disabled_sends_nothing(self):
        k = self.build(enabled=False)
        self.assertFalse(k.wake("test"))
        self.assertEqual(self.http.urls, [])

    def test_no_password_sends_nothing(self):
        """Fully Kiosk refuses an unauthenticated command anyway; not even
        trying keeps a pointless request off the wire every trigger."""
        k = self.build(password="")
        self.assertFalse(k.wake("test"))
        self.assertEqual(self.http.urls, [])

    def test_no_host_sends_nothing(self):
        k = self.build(host="")
        self.assertFalse(k.wake("test"))
        self.assertEqual(self.http.urls, [])


class TestWaking(Base):
    def test_it_turns_the_screen_on_and_up(self):
        """Brightness means nothing to a blanked panel, so screenOn has to
        come first -- something other than us may have turned it off."""
        k = self.build()
        self.assertTrue(k.wake("sweep on trigger"))
        self.settle(k)
        joined = " ".join(self.http.urls)
        self.assertIn("cmd=screenOn", joined)
        self.assertIn("screenBrightness", joined)
        self.assertIn("value=255", joined)
        self.assertIn("10.0.0.5:2323", self.http.urls[0])
        self.assertLess(self.http.urls.index([u for u in self.http.urls
                                              if "screenOn" in u][0]),
                        self.http.urls.index([u for u in self.http.urls
                                              if "screenBrightness" in u][0]),
                        "brightness was set before the screen was on")

    def test_the_password_is_not_in_the_status(self):
        k = self.build()
        self.assertNotIn("pw", repr(k.status()))

    def test_repeated_triggers_do_not_hammer_the_tablet(self):
        """An incident fires this many times a second; the tablet should hear
        about it once."""
        k = self.build()
        k.wake("first")
        for _ in range(50):
            k.wake("again")
        self.settle(k)
        self.assertEqual(len([u for u in self.http.urls if "screenOn" in u]), 1)

    def test_a_later_trigger_wakes_it_again(self):
        k = self.build()
        k.wake("first")
        self.settle(k)
        self.now[0] += 25          # past min_interval_s
        self.assertTrue(k.wake("second"))

    def test_a_rate_limited_repeat_still_extends_the_window(self):
        """Somebody hanging around must not let the screen go dark just
        because the first trigger has aged out.

        The repeat has to fall INSIDE min_interval_s or this exercises the
        ordinary wake path and proves nothing -- which is what the first
        version of this test did, and it passed with the extension deleted.
        """
        k = self.build()
        k.wake("first")                 # t=1000, dark at 1120
        self.now[0] += 10               # inside min_interval_s of 20
        self.assertFalse(k.wake("still there"), "not the rate-limited path")
        self.now[0] += 115              # t=1125: past the ORIGINAL window
        self.assertFalse(k.tick(),
                         "the screen went dark while somebody was still there")
        self.now[0] += 10               # t=1135: past the extended one too
        self.assertTrue(k.tick())


class TestSleeping(Base):
    def test_the_screen_goes_dark_after_the_window(self):
        k = self.build()
        k.wake("trigger")
        self.settle(k)
        self.http.done.clear()
        self.assertFalse(k.tick(), "it slept while still inside the window")
        self.now[0] += 121
        self.assertTrue(k.tick())
        self.settle(k)
        joined = " ".join(self.http.urls)
        self.assertIn("value=6", joined, "it did not dim")
        self.assertNotIn("cmd=screenOff", joined,
                         "dim mode blanked the screen, which stops the "
                         "page polling and makes the panel look dead")

    def test_tick_is_idle_when_nothing_woke_it(self):
        k = self.build()
        for _ in range(5):
            self.now[0] += 1000
            self.assertFalse(k.tick())
        self.assertEqual(self.http.urls, [])

    def test_it_does_not_keep_sending_screenoff(self):
        k = self.build()
        k.wake("trigger")
        self.now[0] += 121
        k.tick()
        self.settle(k)
        n = len(self.http.urls)
        for _ in range(5):
            self.now[0] += 1000
            self.assertFalse(k.tick())
        self.assertEqual(len(self.http.urls), n)


class TestItNeverCostsTheIncident(Base):
    def test_a_flat_or_absent_tablet_does_not_raise(self):
        """The normal case this exists to work around is a tablet that is
        asleep, flat or off the wifi."""
        k = self.build(FakeHTTP(urllib.error.URLError("no route")))
        self.assertTrue(k.wake("trigger"))     # queued, not yet failed
        self.settle(k)
        self.assertEqual(k.failed, 1)

    def test_an_unexpected_error_does_not_raise_either(self):
        class Boom:
            done = threading.Event()

            def __call__(self, *a, **kw):
                self.done.set()
                raise RuntimeError("something unforeseen")
        k = self.build(Boom())
        k.wake("trigger")
        self.settle(k)
        # A wake is now two commands (screenOn, then brightness), so both
        # fail. The point is that neither reached the caller.
        self.assertGreaterEqual(k.failed, 1)

    def test_the_call_does_not_block_the_caller(self):
        """A tablet that accepts the connection and never answers must not
        hold up an incident for the timeout.

        This has to measure the clock. The first version only waited for the
        request to start, so it passed with the send made synchronous -- it
        merely took five seconds longer to say so.
        """
        import time as _t
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class Slow:
            done = started

            def __call__(self, *a, **kw):
                started.set()
                release.wait(10)
                raise urllib.error.URLError("gave up")
        k = self.build(Slow())
        began = _t.monotonic()
        k.wake("trigger")
        took = _t.monotonic() - began
        self.assertLess(took, 0.5,
                        "wake() blocked the caller for %.1fs" % took)
        self.assertTrue(started.wait(2))


class TestDimRatherThanBlank(Base):
    """A blanked Android screen suspends the page's timers, so the panel stops
    polling and looks dead from the board's side. Dim keeps it alive and
    readable; off is kept for a site that wants it genuinely dark."""

    def urls(self):
        return " ".join(self.http.urls)

    def test_dim_is_the_default(self):
        self.assertEqual(self.build().idle_mode, "dim")

    def test_idle_sets_the_low_brightness_and_leaves_the_screen_on(self):
        k = self.build(dim_level=3)
        k.wake("trigger")
        self.now[0] += 121
        k.tick()
        self.settle(k)
        self.assertIn("value=3", self.urls())
        self.assertNotIn("cmd=screenOff", self.urls())

    def test_off_mode_still_blanks_it(self):
        k = self.build(idle_mode="off")
        k.wake("trigger")
        self.now[0] += 121
        k.tick()
        self.settle(k)
        self.assertIn("cmd=screenOff", self.urls())

    def test_a_nonsense_mode_falls_back_to_dim(self):
        """Not to off: the failure that is easy to miss is a dark panel."""
        self.assertEqual(self.build(idle_mode="sideways").idle_mode, "dim")

    def test_levels_are_clamped_to_what_fully_accepts(self):
        k = self.build(dim_level=-5, bright_level=9999)
        self.assertEqual((k.dim_level, k.bright_level), (0, 255))


class TestPolling(Base):
    def test_a_good_reading_is_recorded(self):
        payload = json.dumps({"batteryLevel": 79, "isPlugged": True,
                              "screenOn": True, "batteryTemperature": 22})

        class OK:
            done = threading.Event()

            def __call__(self, url, timeout=None):
                OK.done.set()

                class R:
                    def read(self_inner):
                        return payload.encode()

                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *a):
                        return False
                return R()
        k = self.build(OK())
        row = k.poll()
        self.assertEqual((row["ok"], row["battery"], row["screen"]),
                         (True, 79, True))
        self.assertIn("t", row)

    def test_an_unreachable_tablet_is_a_reading_not_an_error(self):
        """The row that tells a flat battery apart from a dropped wifi, which
        was exactly the distinction unavailable when it mattered."""
        k = self.build(FakeHTTP(urllib.error.URLError("no route")))
        row = k.poll()
        self.assertFalse(row["ok"])
        self.assertIn("why", row)

    def test_polling_an_unconfigured_kiosk_does_nothing(self):
        self.assertIsNone(self.build(host="").poll())

    def test_the_reading_reaches_the_status(self):
        k = self.build(FakeHTTP(urllib.error.URLError("x")))
        k.poll()
        self.assertFalse(k.status()["reading"]["ok"])


class TestTheLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = kiosk.KioskLog(Path(self.tmp.name) / "kiosk")

    def test_rows_come_back_in_order(self):
        for i in range(3):
            self.log.record({"ok": True, "battery": 50 + i})
        rows = self.log.read()
        self.assertEqual([r["battery"] for r in rows], [50, 51, 52])

    def test_it_writes_one_file_per_day(self):
        d1 = dt.datetime(2026, 10, 9, 21, 0)
        self.log.record({"ok": True, "battery": 80}, when=d1)
        self.log.record({"ok": True, "battery": 79},
                        when=d1 + dt.timedelta(days=1))
        self.assertEqual(len(list((Path(self.tmp.name) / "kiosk").glob("*.jsonl"))), 2)

    def test_a_write_failure_does_not_raise_at_the_caller(self):
        """This runs on a sampler thread beside the recording."""
        bad = kiosk.KioskLog("/proc/nonexistent/kiosk")
        bad.record({"ok": True, "battery": 1})      # must not raise


class TestRates(unittest.TestCase):
    """Bright and dim attributed separately, which is the whole point: a day
    mixing a tournament with a quiet night averages to a number describing
    neither."""

    def rows(self, spec, start=None):
        t = start or dt.datetime(2026, 10, 9, 20, 0)
        out = []
        for minutes, battery, screen in spec:
            out.append({"t": (t + dt.timedelta(minutes=minutes)).isoformat(),
                        "ok": True, "battery": battery, "screen": screen})
        return out

    def test_bright_losing_and_dim_gaining_are_reported_apart(self):
        rows = self.rows([(0, 80, True), (60, 76, True),      # -4% in an hour
                          (120, 76, False), (180, 80, False)])  # +4% in an hour
        r = kiosk.rates(rows, capacity_mah=7900)
        self.assertLess(r["bright"]["ma"], 0)
        self.assertGreater(r["dim"]["ma"], 0)
        self.assertEqual(r["bright"]["percent"], -4)
        self.assertEqual(r["dim"]["percent"], 4)

    def test_a_gap_is_not_counted_as_an_interval(self):
        """The tablet was off for two days. The level either side says nothing
        about a rate.

        Asserting on the resulting mA is useless -- a long gap DILUTES the
        average rather than inflating it, so the first version of this passed
        with the guard removed. Assert on the hours actually counted.
        """
        rows = self.rows([(0, 80, False), (10, 79, False)])
        rows.append({"t": dt.datetime(2026, 10, 11, 9, 0).isoformat(),
                     "ok": True, "battery": 100, "screen": False})
        r = kiosk.rates(rows)
        self.assertAlmostEqual(r["dim"]["hours"], 10 / 60.0, places=2,
                               msg="a two-day gap was counted as an interval")

    def test_an_unreachable_row_breaks_the_chain(self):
        """The readings either side must not be paired across a dead tablet.

        They have to be CLOSE together, or the one-hour guard rejects the pair
        anyway and the test passes whether or not the chain is broken -- which
        is how the first version of this proved nothing.
        """
        rows = self.rows([(0, 80, False)])
        rows.append({"t": dt.datetime(2026, 10, 9, 20, 20).isoformat(),
                     "ok": False, "why": "URLError"})
        rows += self.rows([(40, 40, False)])      # 40 min after the first
        r = kiosk.rates(rows)
        self.assertIsNone(r["dim"]["ma"], "it rated across a dead tablet")

    def test_no_data_is_none_not_zero(self):
        r = kiosk.rates([])
        self.assertIsNone(r["bright"]["ma"])
        self.assertIsNone(r["dim"]["ma"])
