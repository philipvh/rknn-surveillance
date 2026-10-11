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
        """The third field is the regime: True bright, False dim.

        Both `screen` and `bright` are written, because in dim mode the real
        tablet reports the screen as ON -- a fixture that set screen=False for
        dim would not resemble anything the board ever logs.
        """
        t = start or dt.datetime(2026, 10, 9, 20, 0)
        out = []
        for minutes, battery, lit in spec:
            out.append({"t": (t + dt.timedelta(minutes=minutes)).isoformat(),
                        "ok": True, "battery": battery,
                        "screen": True, "bright": lit})
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


class TestBrightIsWhatTheBoardCommanded(Base):
    """In dim mode the screen stays ON and only brightness changes, so Fully's
    screenOn cannot tell the two regimes apart. Bucketing on it gave two hours
    of readings all labelled 'bright' -- which were simply all of them -- and
    a dim bucket that could never populate."""

    def good(self):
        body = json.dumps({"batteryLevel": 80, "isPlugged": True,
                           "screenOn": True, "batteryTemperature": 21})

        class OK:
            done = threading.Event()

            def __call__(self, url, timeout=None):
                OK.done.set()

                class R:
                    def read(self_inner):
                        return body.encode()

                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *a):
                        return False
                return R()
        return OK()

    def test_inside_the_wake_window_is_bright(self):
        k = self.build(self.good())
        k.wake("trigger")
        self.assertTrue(k.poll()["bright"])

    def test_after_the_window_is_not_bright_though_the_screen_is_still_on(self):
        """The row follows the command, and tick() is what issues it: between
        the window expiring and the next pass of the loop the panel really is
        still bright, because nothing has told it otherwise yet."""
        k = self.build(self.good())
        k.wake("trigger")
        self.now[0] += 121
        k.tick()
        row = k.poll()
        self.assertTrue(row["screen"], "dim mode leaves the screen on")
        self.assertFalse(row["bright"], "dim was recorded as bright")

    def test_never_woken_is_not_bright(self):
        self.assertFalse(self.build(self.good()).poll()["bright"])

    def test_rates_prefers_bright_over_screen(self):
        t = dt.datetime(2026, 10, 9, 20, 0)
        rows = [{"t": (t + dt.timedelta(minutes=m)).isoformat(), "ok": True,
                 "battery": b, "screen": True, "bright": lit}
                for m, b, lit in ((0, 80, True), (30, 78, True),
                                  (60, 78, False), (90, 80, False))]
        r = kiosk.rates(rows)
        self.assertLess(r["bright"]["ma"], 0)
        self.assertGreater(r["dim"]["ma"], 0)

    def test_a_row_written_before_this_existed_is_not_attributed(self):
        """Falling back to `screen` looked harmless and was not. In dim mode
        screen is always true, so every legacy row landed in the bright
        bucket: a real log reported "+43 mA bright" built entirely from
        readings taken while the panel was dim."""
        t = dt.datetime(2026, 10, 9, 20, 0)
        rows = [{"t": (t + dt.timedelta(minutes=m)).isoformat(), "ok": True,
                 "battery": b, "screen": True} for m, b in ((0, 80), (30, 81))]
        r = kiosk.rates(rows)
        self.assertIsNone(r["bright"]["ma"], "a legacy row was called bright")
        self.assertIsNone(r["dim"]["ma"])

    def test_legacy_rows_do_not_bridge_two_real_ones(self):
        t = dt.datetime(2026, 10, 9, 20, 0)
        rows = [{"t": t.isoformat(), "ok": True, "battery": 80,
                 "screen": True, "bright": False},
                {"t": (t + dt.timedelta(minutes=20)).isoformat(), "ok": True,
                 "battery": 70, "screen": True},                  # legacy
                {"t": (t + dt.timedelta(minutes=40)).isoformat(), "ok": True,
                 "battery": 60, "screen": True, "bright": False}]
        self.assertIsNone(kiosk.rates(rows)["dim"]["ma"],
                          "it rated across an unattributable row")


class TestTheBrightTimeIsCapped(Base):
    """A tournament must not do what an always-on screen did.

    A trigger extends the wake window, so an afternoon of activity used to
    hold the panel at full brightness for hours -- the exact draw the dimming
    was introduced to avoid. After max_bright_seconds it rests, whatever is
    still happening in front of the camera.
    """

    def cap(self, **over):
        c = {"max_bright_seconds": 300, "cooldown_seconds": 600}
        c.update(over)
        return self.build(**c)

    def dimmed(self):
        return [u for u in self.http.urls if "value=6" in u]

    def lit(self):
        return [u for u in self.http.urls if "cmd=screenOn" in u]

    def test_continuous_activity_dims_the_panel_at_the_cap(self):
        k = self.cap()
        k.wake("first")
        for _ in range(4):                      # a trigger every 60s
            self.now[0] += 60
            k.wake("still going")
        self.settle(k)
        self.assertTrue(k.status()["bright"], "rested before the cap")
        self.http.urls.clear()
        self.now[0] += 60                       # t=1300: the cap
        self.assertFalse(k.wake("and on"), "it stayed bright past the cap")
        self.assertTrue(k.tick(), "tick did not settle the capped panel")
        self.settle(k)
        self.assertTrue(self.dimmed(), "the panel was never told to dim")
        self.assertFalse(k.status()["bright"])
        self.assertEqual(k.status()["rested"], 1)

    def test_tick_enforces_the_cap_without_a_new_trigger(self):
        """One trigger with a long wake window would otherwise outlast the
        cap by a whole window: nothing calls wake() to notice."""
        k = self.cap(wake_seconds=600)
        k.wake("one long look")
        self.settle(k)
        self.http.urls.clear()
        self.now[0] += 299
        self.assertFalse(k.tick(), "it rested before the cap")
        self.now[0] += 2                        # t=1301
        self.assertTrue(k.tick())
        self.settle(k)
        self.assertTrue(self.dimmed())

    def test_a_trigger_during_the_rest_leaves_it_dim(self):
        k = self.cap()
        k.wake("first")
        self.now[0] += 300
        k.wake("the cap")                       # rests here
        k.tick()                                # which tick() carries out
        self.settle(k)
        self.http.urls.clear()
        self.now[0] += 599                      # still resting
        self.assertFalse(k.wake("knocking"))
        self.settle(k)
        self.assertEqual(self.lit(), [], "it brightened during the rest")
        self.assertFalse(k.status()["bright"])
        self.assertGreater(k.status()["resting"], 0)

    def test_it_brightens_again_once_the_rest_is_over(self):
        """The cap is a duty cycle, not an off switch: the panel has to come
        back, or an incident that outlasts one cooldown goes unwitnessed."""
        k = self.cap()
        k.wake("first")
        self.now[0] += 300
        k.wake("the cap")
        k.tick()
        self.settle(k)
        self.http.urls.clear()
        self.now[0] += 601
        self.assertTrue(k.wake("still here"))
        self.settle(k)
        self.assertTrue(self.lit(), "it never came back after the rest")
        self.assertEqual(k.status()["resting"], 0)

    def test_intermittent_triggers_still_reach_the_cap(self):
        """The spell must survive the gaps between triggers.

        Ending it the moment the wake window expired would let activity that
        arrives every few minutes -- which is what a busy afternoon looks
        like -- restart the clock forever and never reach the cap.
        """
        k = self.cap()
        k.wake("first")                         # bright 1000-1120
        self.now[0] += 130
        self.assertTrue(k.tick(), "the window should have expired")
        self.now[0] += 120                      # t=1250, dim for 120s
        self.assertTrue(k.wake("again"), "the gap was treated as a rest")
        self.settle(k)
        self.http.urls.clear()
        self.now[0] += 60                       # t=1310: 310s into the spell
        self.assertTrue(k.tick(), "the gap restarted the spell")
        self.settle(k)
        self.assertTrue(self.dimmed())
        self.assertEqual(k.status()["rested"], 1)

    def test_a_full_cooldown_of_quiet_starts_a_fresh_spell(self):
        """Otherwise the first spell of the day would cap the panel for good:
        an evening trigger would inherit the afternoon's accumulated time."""
        k = self.cap()
        k.wake("afternoon")
        self.now[0] += 130
        k.tick()                                # dim from t=1130
        self.now[0] += 600                      # t=1730: quiet long enough
        self.assertTrue(k.wake("evening"))
        self.settle(k)
        self.http.urls.clear()
        for _ in range(4):                      # four more minutes of it
            self.now[0] += 60
            k.wake("still there")
            k.tick()
        self.settle(k)
        # 240s into the new spell, 970s into the old one. Carrying the old
        # spell forward would have rested the panel at the first repeat.
        self.assertTrue(k.status()["bright"],
                        "the fresh spell inherited the old one")
        self.assertEqual(k.status()["rested"], 0)

    def test_zero_lifts_the_cap(self):
        k = self.cap(max_bright_seconds=0)
        k.wake("first")
        for _ in range(20):                     # 20 minutes of activity
            self.now[0] += 60
            k.wake("still going")
        self.settle(k)
        self.assertTrue(k.status()["bright"], "an uncapped panel rested")
        self.assertEqual(k.status()["rested"], 0)

    def test_a_cap_with_no_rest_is_refused(self):
        """It would dim the panel and brighten it again on the next trigger,
        flipping the screen at tick rate for as long as the activity lasted.
        """
        with self.assertLogs("kiosk", level="WARNING"):
            k = self.cap(cooldown_seconds=0)
        self.assertEqual(k.max_bright_s, 0)
        k.wake("first")
        self.now[0] += 400
        k.wake("past the cap")
        self.settle(k)
        self.assertTrue(k.status()["bright"])

    def test_a_resting_panel_is_logged_as_dim(self):
        """The charge-rate buckets read this field; a rest booked as bright
        would be charged to the regime it exists to avoid."""
        k = self.cap()
        k.wake("first")
        self.now[0] += 300
        k.wake("the cap")
        k.tick()
        self.settle(k)
        row = k.poll()
        self.assertFalse(row["bright"], "a resting panel was booked as bright")


class TestAHandOnTheGlass(Base):
    """A touch is its own reason to be bright.

    The cap is there because a tournament produces triggers for hours with
    nobody looking at the wall. Somebody putting a hand on the glass is the
    opposite case: refusing them because the courts were busy would make the
    panel look broken at the one moment it has an audience.
    """

    def cap(self, **over):
        c = {"max_bright_seconds": 300, "cooldown_seconds": 600,
             "touch_seconds": 60}
        c.update(over)
        return self.build(**c)

    def lit(self):
        return [u for u in self.http.urls if "cmd=screenOn" in u]

    def test_a_touch_brightens_the_panel(self):
        k = self.cap()
        self.assertTrue(k.touch())
        self.settle(k)
        self.assertTrue(self.lit())
        self.assertIn("value=255", " ".join(self.http.urls))
        self.assertEqual(k.status()["touched"], 1)

    def test_a_touch_brightens_a_panel_that_is_resting(self):
        """The whole point: the camera's budget must not gag the person
        standing in front of the thing."""
        k = self.cap()
        k.wake("first")
        self.now[0] += 300
        k.wake("the cap")
        k.tick()                                # resting now
        self.settle(k)
        self.assertGreater(k.status()["resting"], 0)
        self.http.urls.clear()
        self.assertTrue(k.touch(), "a resting panel ignored a hand on it")
        self.settle(k)
        self.assertTrue(self.lit())
        self.assertTrue(k.status()["bright"])

    def test_the_touch_window_expires(self):
        k = self.cap()
        k.touch()
        self.settle(k)
        self.now[0] += 59
        self.assertFalse(k.tick(), "it dimmed while the hand was still there")
        self.now[0] += 2
        self.assertTrue(k.tick())
        self.assertFalse(k.status()["bright"])

    def test_a_touch_holds_the_panel_through_the_camera_cap(self):
        """Two budgets, not one: the camera's running out must not pull the
        screen out from under somebody reading it."""
        k = self.cap()
        k.wake("first")
        for _ in range(4):                      # play keeps the window alive
            self.now[0] += 60
            k.wake("still going")
        self.now[0] += 50                       # t=1290, 10s before the cap
        k.touch()
        self.settle(k)
        self.now[0] += 20                       # t=1310: past the cap
        self.assertFalse(k.tick(), "the cap dimmed a panel being touched")
        self.assertTrue(k.status()["bright"])
        self.assertEqual(k.status()["rested"], 1, "the camera was not capped")
        self.now[0] += 60                       # the hand is gone too
        self.assertTrue(k.tick())
        self.assertFalse(k.status()["bright"])

    def test_a_touch_does_not_hand_the_camera_its_budget_back(self):
        """Otherwise one tap would reopen the tournament hole: the camera
        would resume extending the window for as long as play lasted."""
        k = self.cap()
        k.wake("first")
        self.now[0] += 300
        k.wake("the cap")
        k.tick()
        k.touch()
        self.settle(k)
        self.now[0] += 30
        self.assertFalse(k.wake("play continues"), "the camera got back in")
        self.now[0] += 60                       # the touch window runs out
        self.assertTrue(k.tick(), "it stayed bright on the camera's account")

    def test_repeated_touching_does_not_hammer_the_tablet(self):
        k = self.cap()
        k.touch()
        for _ in range(20):
            self.now[0] += 1
            k.touch()
        self.settle(k)
        self.assertEqual(len(self.lit()), 1)
        self.assertTrue(k.status()["bright"])

    def test_touching_keeps_it_up(self):
        k = self.cap()
        k.touch()
        self.settle(k)
        for _ in range(6):                      # a tap every 30s for 3 min
            self.now[0] += 30
            k.touch()
            self.assertFalse(k.tick(), "it dimmed under a hand")
        self.assertTrue(k.status()["bright"])

    def test_zero_turns_touch_waking_off(self):
        k = self.cap(touch_seconds=0)
        self.assertFalse(k.touch())
        self.settle(k)
        self.assertEqual(self.http.urls, [])


class FakePanel:
    """Fully, answering deviceInfo with a page of its own choosing."""

    def __init__(self, page, home="http://panel/", info=True, touched_s=9999):
        self.page, self.home, self.info = page, home, info
        self.touched_s = touched_s      # how long ago the glass was touched
        self.urls = []
        self.done = threading.Event()

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        self.done.set()
        if "deviceInfo" in url:
            if not self.info:
                raise urllib.error.URLError("no answer")
            now_ms = 1791742052829
            body = json.dumps({"batteryLevel": 90, "isPlugged": True,
                               "screenOn": True, "currentPage": self.page,
                               "startUrl": self.home, "timestamp": now_ms,
                               "lastUserInteractionTime":
                                   now_ms - int(self.touched_s * 1000)}).encode()
        else:
            body = b"OK"

        class R:
            def read(self_inner):
                return body

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False
        return R()


class TestItGoesHomeOnTheWayDown(Base):
    """Somebody who left the panel on the clips browser left it on a page
    that is not the panel -- and, on a tablet whose brightness we cannot set,
    on a page with no standby cover, so the screen would stay lit all night.
    """

    def panel(self, page, **over):
        self.http = FakePanel(page)
        c = {"enabled": True, "host": "10.0.0.5", "password": "pw"}
        c.update(over)
        self.now = [1000.0]
        return kiosk.Kiosk(c, opener=self.http, clock=lambda: self.now[0])

    def home_sent(self):
        return [u for u in self.http.urls if "loadStartUrl" in u]

    def test_a_panel_left_elsewhere_is_sent_home(self):
        k = self.panel("http://panel/media")
        k.wake("trigger")
        self.now[0] += 121
        k.tick()
        self.settle(k)
        self.assertTrue(self.home_sent(), "it was left on the clips browser")

    def test_a_panel_already_home_is_left_alone(self):
        """An unconditional reload throws up a white loading page every time
        the panel settles -- a flash of light from the thing just told to go
        dark."""
        k = self.panel("http://panel/")
        k.wake("trigger")
        self.now[0] += 121
        k.tick()
        self.settle(k)
        self.assertEqual(self.home_sent(), [], "it reloaded for nothing")

    def test_a_trailing_slash_is_not_another_page(self):
        k = self.panel("http://panel")
        k.wake("trigger")
        self.now[0] += 121
        k.tick()
        self.settle(k)
        self.assertEqual(self.home_sent(), [])

    def test_an_unreadable_panel_is_sent_home_anyway(self):
        """A flash beats leaving the settings page lit all night."""
        self.http = FakePanel("http://panel/media", info=False)
        k = kiosk.Kiosk({"enabled": True, "host": "10.0.0.5", "password": "pw"},
                        opener=self.http, clock=lambda: self.now[0])
        self.now = [1000.0]
        k.wake("trigger")
        self.now[0] += 121
        k.tick()
        self.settle(k)
        self.assertTrue(self.home_sent())

    def test_it_is_not_sent_home_on_the_way_up(self):
        k = self.panel("http://panel/media")
        k.wake("trigger")
        self.settle(k)
        self.assertEqual(self.home_sent(), [],
                         "it reloaded the page somebody was looking at")

    def test_a_panel_somebody_is_using_is_left_alone(self):
        """A member going through last night's clips is on a page that does
        not report its own touches. Yanking them back to the panel mid-clip
        would be the screen fighting its user."""
        self.http = FakePanel("http://panel/media", touched_s=20)
        self.now = [1000.0]
        k = kiosk.Kiosk({"enabled": True, "host": "10.0.0.5", "password": "pw"},
                        opener=self.http, clock=lambda: self.now[0])
        k.wake("trigger")
        self.now[0] += 121
        k.tick()
        self.settle(k)
        self.assertEqual(self.home_sent(), [], "it interrupted somebody")

    def test_a_panel_abandoned_on_another_page_is_still_sent_home(self):
        self.http = FakePanel("http://panel/media", touched_s=600)
        self.now = [1000.0]
        k = kiosk.Kiosk({"enabled": True, "host": "10.0.0.5", "password": "pw"},
                        opener=self.http, clock=lambda: self.now[0])
        k.wake("trigger")
        self.now[0] += 121
        k.tick()
        self.settle(k)
        self.assertTrue(self.home_sent(), "it was left on the clips browser")

    def test_home_on_dim_off_leaves_the_page_alone(self):
        k = self.panel("http://panel/media", home_on_dim=False)
        k.wake("trigger")
        self.now[0] += 121
        k.tick()
        self.settle(k)
        self.assertEqual(self.home_sent(), [])
