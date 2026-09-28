# Copyright 2026 Philip van Houtte, magicview.tv, the Netherlands
# SPDX-License-Identifier: Apache-2.0
"""Waking the wall panel when the camera turns to look at somebody.

The screen is a deterrent, not a display: dark until something triggers, lit
while the camera is turning. That also stops the tablet outlasting its charger
-- it managed 28 hours and then 41 before dying, both times with the screen on.
"""

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
    def test_it_asks_the_tablet_to_turn_the_screen_on(self):
        k = self.build()
        self.assertTrue(k.wake("sweep on trigger"))
        self.settle(k)
        self.assertEqual(len(self.http.urls), 1)
        self.assertIn("cmd=screenOn", self.http.urls[0])
        self.assertIn("10.0.0.5:2323", self.http.urls[0])

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
        self.assertEqual(len(self.http.urls), 1)

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
        self.assertIn("cmd=screenOff", self.http.urls[-1])

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
        self.assertEqual(k.failed, 1)

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
