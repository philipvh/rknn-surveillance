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

"""The check that would have caught twelve days of filming a ceiling."""

import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aimwatch                                                # noqa: E402
from aimwatch import AimWatch, compare                         # noqa: E402

np = aimwatch.np
needs_numpy = unittest.skipUnless(aimwatch.available(),
                                  "numpy is not installed here")


# A scene with enough structure to correlate: posts, a fence line, a bench.
def scene(w=480, h=320, seed=4):
    rng = np.random.RandomState(seed)
    a = np.full((h, w), 40.0)
    a += rng.rand(h, w) * 6.0               # sensor noise and gravel
    a[int(h * 0.45):int(h * 0.5), :] = 190  # the fence rail
    for x in range(40, w - 40, 90):         # posts
        a[int(h * 0.2):int(h * 0.75), x:x + 5] = 210
    a[int(h * 0.7):int(h * 0.8), int(w * 0.6):int(w * 0.85)] = 150   # bench
    return a


def panned(camera_dx, camera_dy, w=480, h=320, seed=4):
    """Two crops of a wider world: what a camera that PANS actually sees.

    np.roll wraps the picture around on itself, which is tidy and unlike any
    camera. A real pan lets new scenery in at one edge and loses it at the
    other, and that discontinuity is what makes the window in _prepare earn
    its place -- without it a real 2.4-second pan off the club camera scored
    shift 0.0 and a confidence that said the view was unchanged.
    """
    big = scene(w=w * 2, h=h * 2, seed=seed)
    y0, x0 = h // 2, w // 2
    a = big[y0:y0 + h, x0:x0 + w]
    b = big[y0 + camera_dy:y0 + camera_dy + h,
            x0 + camera_dx:x0 + camera_dx + w]
    # Note the physics: the camera turning right slides the world LEFT across
    # the sensor, so the measured content shift is the negative of the pan.
    return a, b


def with_infrared_falloff(a):
    """Bright close, black far -- and fixed to the SENSOR, not the scene.

    This is what the club's night frames look like: the lamps light the near
    concrete and nothing reaches the far court. It is the part of a frame
    that does NOT move when the camera does, which is exactly what the window
    in _prepare is there to keep out of the measurement.
    """
    h, w = a.shape
    return a * (np.linspace(2.2, 0.08, h)[:, None] * np.ones((1, w))) + 12


def shifted(a, dx, dy):
    """The same scene, the camera moved. Edges roll round; the measurement
    is of the scene's content, not of what fell off the frame."""
    return np.roll(np.roll(a, dy, axis=0), dx, axis=1)


def jpeg_of(a):
    """Stand-in for an encoded frame: the fake decoder hands the array back."""
    return a.tobytes() + b"|%d|%d" % a.shape


class Decoder:
    """A decode() that pairs bytes with the array they came from."""

    def __init__(self):
        self.frames = {}

    def put(self, a):
        key = jpeg_of(a)
        self.frames[key] = a
        return key

    def __call__(self, blob):
        return self.frames[bytes(blob)]


@needs_numpy
class TestItMeasuresTheShift(unittest.TestCase):
    """Phase correlation, because the question is 'how far has it moved',
    not 'how different is it'. Two pictures of the same place differ wildly
    between noon and midnight; the geometry does not move."""

    def test_an_unmoved_camera_reads_zero(self):
        a = scene()
        dx, dy, shift, conf = compare(a, a)
        self.assertEqual((dx, dy), (0.0, 0.0))
        self.assertLess(shift, 0.001)

    def test_it_recovers_the_shift_it_was_given(self):
        """Sign included: a message that says the view moved left when it
        moved right sends somebody to the wrong side of the clubhouse."""
        a = scene(w=480, h=320)
        for want_dx, want_dy in ((24, 0), (0, -18), (-40, 28), (64, 48)):
            b = shifted(a, want_dx, want_dy)
            dx, dy, shift, conf = compare(a, b)
            # the comparison runs on a 256-pixel square, so the scene's own
            # pixels scale into it
            self.assertAlmostEqual(dx, want_dx * 256.0 / 480.0, delta=2.0)
            self.assertAlmostEqual(dy, want_dy * 256.0 / 320.0, delta=2.0)
            self.assertGreater(conf, 20.0)

    def test_a_real_pan_is_measured_and_not_missed(self):
        """New scenery enters the frame, as it does on a camera that moved.
        This is the case that matters: the club's 2.4-second pan right."""
        for pan_x, pan_y in ((40, 0), (0, 30), (-56, -24)):
            a, b = panned(pan_x, pan_y)
            dx, dy, shift, conf = compare(a, b)
            self.assertGreater(shift, 10.0)
            # negative of the pan: turning right slides the world left
            self.assertAlmostEqual(dx, -pan_x * 256.0 / 480.0, delta=4.0)
            self.assertAlmostEqual(dy, -pan_y * 256.0 / 320.0, delta=4.0)

    def test_a_pan_is_not_mistaken_for_a_steady_view(self):
        """The failure the window in _prepare prevents, and the reason it is
        there at all: without it the correlation of a panned pair peaks at
        zero, on the frame's own border rather than on the scene, and a
        camera that has moved is reported as a camera that has not. Measured
        on the club's real 2.4-second pan: shift 0.0 and a confidence saying
        the view was unchanged."""
        a, b = panned(48, 20)
        _, _, moved, _ = compare(a, b)
        _, _, still, _ = compare(a, a)
        self.assertGreater(moved, 10.0)
        self.assertLess(still, 0.001)

    def test_the_frame_border_stays_out_of_the_measurement(self):
        """A lit foreground that belongs to the lens rather than to the
        scene is a feature the correlation will happily lock onto, and then
        a camera that has moved reads as one that has not. On the club's own
        2.4-second pan, dropping the window in _prepare turned shift 24 into
        shift 0 with a confidence that called the view unchanged.

        Asserted as an absolute: a panned pair under heavy infrared falloff
        still has to come back with a confident reading."""
        a, b = panned(48, 20)
        dx, dy, shift, conf = compare(with_infrared_falloff(a),
                                      with_infrared_falloff(b))
        self.assertGreater(shift, 10.0)
        self.assertGreater(conf, 80.0)

    def test_darkness_is_not_movement(self):
        """The reference was taken at noon and the check runs at night. The
        club's night frames are a quarter the brightness with the infrared
        lamps lighting only the near concrete -- if that reads as drift, the
        alarm cries wolf every single night and gets switched off."""
        a = scene()
        night = a * 0.25 + 8.0
        night[:60, :] *= 2.4                    # IR glare on the near soffit
        dx, dy, shift, conf = compare(a, night)
        self.assertLess(shift, 4.0)
        self.assertGreater(conf, 4.0)

    def test_somebody_walking_through_is_not_movement(self):
        a = scene()
        b = a.copy()
        b[150:300, 200:260] = 230               # a person, close and bright
        dx, dy, shift, conf = compare(a, b)
        self.assertLess(shift, 4.0)

    def test_a_different_place_is_not_a_measurable_shift(self):
        """What the club's camera actually did: it ended up looking at its
        own mount. There is no offset that maps a fence onto a ceiling, and
        reporting one would be a lie -- the confidence has to collapse so the
        alarm says 'unrecognisable' instead of inventing a number."""
        a = scene()
        rng = np.random.RandomState(9)
        mount = np.full((320, 480), 20.0) + rng.rand(320, 480) * 4
        mount[:90, :] = 240                     # the bright housing rim
        _, _, _, conf = compare(a, mount)
        _, _, _, same_conf = compare(a, shifted(a, 10, 4))
        self.assertLess(conf, same_conf / 2.0)


@needs_numpy
class TestTheDailyQuestion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dec = Decoder()
        self.now = dt.datetime(2026, 11, 3, 13, 0, 0).timestamp()
        self.ref = scene()

    def watch(self, **kw):
        kw.setdefault("decode", self.dec)
        kw.setdefault("clock", lambda: self.now)
        kw.setdefault("confirm_seconds", 120)
        return AimWatch(os.path.join(self.tmp.name, "aim-reference.jpg"), **kw)

    def arm(self, w):
        w.set_reference(self.dec.put(self.ref))
        return w

    def frame(self, a):
        return self.dec.put(a)

    # -------------------------------------------------------------- timing
    def test_nothing_happens_without_a_reference(self):
        """A camera nobody has aimed yet has no wrong view to be in."""
        w = self.watch()
        self.assertFalse(w.due())
        self.assertIsNone(w.check(self.frame(self.ref)))

    def test_it_waits_a_day(self):
        w = self.arm(self.watch())
        self.now += 3600
        self.assertFalse(w.due())
        self.now += 23 * 3600
        self.assertTrue(w.due())

    def test_it_asks_at_the_hour_the_reference_was_taken(self):
        """A reference shot at one in the afternoon, checked at three in the
        morning, is two pictures of different places as far as the light is
        concerned."""
        w = self.arm(self.watch())
        self.now += 14 * 3600           # a day later but the small hours
        self.assertFalse(w.due())
        self.now += 10 * 3600           # round to the same hour again
        self.assertTrue(w.due())

    def test_saving_a_new_home_restarts_the_clock(self):
        w = self.arm(self.watch())
        self.now += 25 * 3600
        self.assertTrue(w.due())
        w.set_reference(self.frame(scene(seed=7)))
        self.assertFalse(w.due())

    # ------------------------------------------------------------- verdicts
    def test_the_same_view_a_day_later_is_quiet(self):
        w = self.arm(self.watch())
        self.now += 24 * 3600
        r = w.check(self.frame(self.ref))
        self.assertFalse(r.drifted)
        self.assertLess(r.shift, 2.0)

    def test_a_moved_camera_is_reported_once_confirmed(self):
        w = self.arm(self.watch())
        self.now += 24 * 3600
        moved = shifted(self.ref, 90, 60)
        first = w.check(self.frame(moved))
        self.assertFalse(first.drifted)     # held, not believed yet
        self.assertFalse(w.due())           # not this instant
        self.now += 130
        self.assertTrue(w.due())            # but in two minutes, not tomorrow
        second = w.check(self.frame(moved))
        self.assertTrue(second.drifted)
        self.assertGreater(second.shift, 20)

    def test_a_van_parked_in_front_for_a_minute_is_not_an_alarm(self):
        """The whole reason for the second look. One frame cannot tell a
        camera that moved from a lorry that stopped."""
        w = self.arm(self.watch())
        self.now += 24 * 3600
        blocked = self.ref.copy()
        blocked[:, :] = 15                      # the lens, obscured
        self.assertFalse(w.check(self.frame(blocked)).drifted)
        self.now += 130
        self.assertFalse(w.check(self.frame(self.ref)).drifted)
        self.assertEqual(w.alerts, 0)

    def test_an_unrecognisable_view_is_an_alarm_of_its_own(self):
        w = self.arm(self.watch())
        self.now += 24 * 3600
        rng = np.random.RandomState(3)
        ceiling = np.full((320, 480), 18.0) + rng.rand(320, 480) * 3
        ceiling[:100, :] = 245
        w.check(self.frame(ceiling))
        self.now += 130
        r = w.check(self.frame(ceiling))
        self.assertTrue(r.drifted)
        self.assertFalse(r.recognisable)
        self.assertIn("does not resemble", w.message(r))

    def test_it_reminds_once_a_day_and_not_once_a_check(self):
        """Until somebody re-aims the camera every check finds the same wrong
        view. Staying silent after the first message is how a camera ends up
        filming a ceiling for twelve days; a message per check is how the ntfy
        topic gets muted. One a day keeps saying it without becoming noise."""
        w = self.arm(self.watch())
        moved = shifted(self.ref, 90, 60)
        self.now += 24 * 3600
        w.check(self.frame(moved))
        self.now += 130
        self.assertTrue(w.check(self.frame(moved)).drifted)
        # a second look inside the same day says nothing more
        self.now += 130
        w.check(self.frame(moved))
        self.now += 130
        self.assertFalse(w.check(self.frame(moved)).drifted)
        self.assertEqual(w.alerts, 1)
        # tomorrow it says it again
        self.now += 24 * 3600
        w.check(self.frame(moved))
        self.now += 130
        self.assertTrue(w.check(self.frame(moved)).drifted)
        self.assertEqual(w.alerts, 2)

    def test_fixing_the_aim_clears_the_alarm(self):
        w = self.arm(self.watch())
        moved = shifted(self.ref, 90, 60)
        self.now += 24 * 3600
        w.check(self.frame(moved))
        self.now += 130
        w.check(self.frame(moved))
        # somebody re-aims and presses Set Home
        w.set_reference(self.frame(moved))
        self.now += 24 * 3600
        self.assertFalse(w.check(self.frame(moved)).drifted)
        self.assertIsNone(w.status()["last_alert"])

    # -------------------------------------------------------------- restart
    def test_a_restart_does_not_re_arm_the_check(self):
        """The board restarts for every deploy. If the schedule lived only in
        memory, a busy week of deploys would check -- and alert -- every time."""
        w = self.arm(self.watch())
        self.now += 3600
        again = self.watch()
        self.assertFalse(again.due())
        self.assertTrue(again.has_reference)

    def test_a_restart_does_not_repeat_an_alert(self):
        w = self.arm(self.watch())
        moved = shifted(self.ref, 90, 60)
        self.now += 24 * 3600
        w.check(self.frame(moved))
        self.now += 130
        w.check(self.frame(moved))
        again = self.watch()        # a deploy, minutes later
        self.now += 130
        again.check(self.frame(moved))
        self.now += 130
        self.assertFalse(again.check(self.frame(moved)).drifted)
        self.assertEqual(again.alerts, 0)

    # --------------------------------------------------------------- safety
    def test_an_undecodable_frame_is_not_a_crash(self):
        """The camera answers with half a JPEG now and then. A watchdog that
        falls over on a bad frame is worse than no watchdog."""
        w = self.arm(self.watch())
        self.now += 24 * 3600
        self.assertIsNone(w.check(b"not a picture at all"))

    def test_it_switches_itself_off_without_numpy(self):
        real = aimwatch.np
        try:
            aimwatch.np = None
            w = AimWatch(os.path.join(self.tmp.name, "ref.jpg"), enabled=True)
            self.assertFalse(w.enabled)
            self.assertFalse(w.due())
        finally:
            aimwatch.np = real


if __name__ == "__main__":
    unittest.main()
