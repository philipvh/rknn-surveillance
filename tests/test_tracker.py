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

"""Tests for auto-tracking.

The headline test simulates a person walking across the courts with a camera
model that actually pans in response, and asserts the two things the plan
names: the person stays roughly framed, and the camera does not oscillate.
"""
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ptz import BudgetExceeded  # noqa: E402
from tracker import Tracker  # noqa: E402

W, H = 1280, 720


class FakePTZ:
    """A camera whose view pans when told to.

    degrees_per_s is expressed in fractions of a frame width per second, so a
    one-second pulse at 0.5 shifts the view by half a frame.
    """

    def __init__(self, pan_per_s=0.5, refuse=False):
        self.pan_per_s = pan_per_s
        self.refuse = refuse
        self.view_x = 0.0            # centre of the view, in world fractions
        self.view_y = 0.0
        self.moves = []

    def move(self, direction, source="auto", deadline_s=0.3):
        if self.refuse:
            raise BudgetExceeded("motor budget spent")
        self.moves.append((direction, deadline_s))
        shift = self.pan_per_s * deadline_s
        if "left" in direction:
            self.view_x -= shift
        if "right" in direction:
            self.view_x += shift
        if "top" in direction or direction == "up":
            self.view_y -= shift
        if "bottom" in direction or direction == "down":
            self.view_y += shift


class Cfg:
    def __init__(self, **over):
        self.d = {"tracking": {"enabled": True, "dead_zone": 0.25,
                               "min_pulse_s": 0.15, "max_pulse_s": 0.5,
                               "cooldown_s": 1.5, "confirmations": 2,
                               "max_pulses_per_incident": 12,
                               "min_confidence": 0.7, "vertical": True}}
        self.d["tracking"].update(over)

    def _get(self, *keys, default=None):
        node = self.d
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


def box_at(world_x, view_x, world_y=0.0, view_y=0.0, size=0.12):
    """Where a subject at world_x appears, given the camera is looking at view_x.

    Coordinates are fractions of the frame; returns pixels.
    """
    cx = (world_x - view_x) * W + W / 2.0
    cy = (world_y - view_y) * H + H / 2.0
    w = size * W
    h = size * H * 2
    return [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class TestFraming(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.ptz = FakePTZ()
        self.t = Tracker(Cfg(), self.ptz, clock=self.clock)

    def walk(self, seconds=40.0, world_speed=0.04, fps=2.0):
        """A person crossing the view.

        world_speed is frame-widths per second. 0.04 puts them across a frame
        and a half in forty seconds, which is a walking pace at the sort of
        distance a court camera sees. The camera can pan at 0.5 frame-widths
        per second of pulse, so it can keep up -- the question the test asks
        is whether it does so smoothly.
        """
        offsets = []
        world_x = -0.4
        step = 1.0 / fps
        for _ in range(int(seconds * fps)):
            self.clock.t += step
            world_x += world_speed * step
            self.t.update([box_at(world_x, self.ptz.view_x)], [0.95], W, H)
            offsets.append(world_x - self.ptz.view_x)
        return offsets

    def test_a_person_walking_across_stays_roughly_framed(self):
        offsets = self.walk()
        worst = max(abs(o) for o in offsets)
        self.assertLess(worst, 0.75,
                        f"subject drifted {worst:.2f} frames from centre; "
                        f"they should stay in shot")
        self.assertTrue(self.ptz.moves, "the camera should have followed at all")

    def test_it_does_not_oscillate(self):
        self.walk()
        self.assertLessEqual(
            self.t.reversals, 2,
            f"the camera reversed direction {self.t.reversals} times while "
            f"following someone walking one way -- that is hunting")

    def test_a_stationary_subject_in_the_middle_is_left_alone(self):
        for _ in range(20):
            self.clock.t += 0.5
            self.t.update([box_at(0.0, 0.0)], [0.95], W, H)
        self.assertEqual(self.ptz.moves, [],
                         "a centred subject must not make the motors run")

    def test_a_subject_just_inside_the_dead_zone_is_left_alone(self):
        for _ in range(20):
            self.clock.t += 0.5
            self.t.update([box_at(0.12, 0.0)], [0.95], W, H)
        self.assertEqual(self.ptz.moves, [])

    def test_a_subject_outside_the_dead_zone_is_followed(self):
        for _ in range(6):
            self.clock.t += 0.5
            self.t.update([box_at(0.45, 0.0)], [0.95], W, H)
        self.assertTrue(self.ptz.moves)
        self.assertIn("right", self.ptz.moves[0][0])


class TestAntiHunting(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.ptz = FakePTZ()
        self.t = Tracker(Cfg(), self.ptz, clock=self.clock)

    def test_one_stray_detection_does_not_move_the_camera(self):
        self.clock.t += 0.5
        self.t.update([box_at(0.9, 0.0)], [0.95], W, H)      # one bad frame
        self.assertEqual(self.ptz.moves, [],
                         "a single frame must not be enough to move the dome")

    def test_two_agreeing_frames_do(self):
        for _ in range(2):
            self.clock.t += 0.5
            self.t.update([box_at(0.9, 0.0)], [0.95], W, H)
        self.assertEqual(len(self.ptz.moves), 1)

    def test_disagreeing_frames_never_accumulate(self):
        for i in range(10):
            self.clock.t += 0.5
            x = 0.9 if i % 2 == 0 else -0.9
            self.t.update([box_at(x, 0.0)], [0.95], W, H)
        self.assertEqual(self.ptz.moves, [],
                         "alternating detections must not drive the motors")

    def test_nothing_moves_during_the_cooldown(self):
        for _ in range(2):
            self.clock.t += 0.5
            self.t.update([box_at(0.9, 0.0)], [0.95], W, H)
        self.assertEqual(len(self.ptz.moves), 1)
        for _ in range(4):
            self.clock.t += 0.2                              # inside cooldown
            self.t.update([box_at(0.9, 0.0)], [0.95], W, H)
        self.assertEqual(len(self.ptz.moves), 1)
        self.clock.t += 2.0
        for _ in range(2):
            self.clock.t += 0.5
            self.t.update([box_at(0.9, 0.0)], [0.95], W, H)
        self.assertEqual(len(self.ptz.moves), 2)

    def test_pulses_are_capped_per_incident(self):
        # A subject that never becomes centred, so corrections never stop.
        for i in range(200):
            self.clock.t += 2.0
            self.t.update([box_at(0.9, 0.0)], [0.95], W, H)
        self.assertLessEqual(len(self.ptz.moves), 12,
                             "a bad night must not grind the dome")

    def test_reset_starts_the_next_incident_fresh(self):
        for i in range(60):
            self.clock.t += 2.0
            self.t.update([box_at(0.9, 0.0)], [0.95], W, H)
        n = len(self.ptz.moves)
        self.assertEqual(n, 12, "should have hit the per-incident cap")
        self.t.reset()
        for _ in range(4):
            self.clock.t += 2.0
            self.t.update([box_at(0.9, 0.0)], [0.95], W, H)
        self.assertGreater(len(self.ptz.moves), n,
                           "a new incident gets a fresh allowance")


class TestTargeting(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.ptz = FakePTZ()
        self.t = Tracker(Cfg(), self.ptz, clock=self.clock)

    def test_low_confidence_boxes_are_ignored(self):
        for _ in range(4):
            self.clock.t += 0.5
            self.t.update([box_at(0.9, 0.0)], [0.4], W, H)
        self.assertEqual(self.ptz.moves, [])

    def test_the_largest_confident_box_wins(self):
        near = box_at(-0.6, 0.0, size=0.3)
        far = box_at(0.6, 0.0, size=0.05)
        for _ in range(2):
            self.clock.t += 0.5
            self.t.update([far, near], [0.9, 0.9], W, H)
        self.assertIn("left", self.ptz.moves[0][0],
                      "the nearest person is the one worth framing")

    def test_diagonal_error_produces_a_diagonal_move(self):
        for _ in range(2):
            self.clock.t += 0.5
            self.t.update([box_at(0.9, 0.0, world_y=0.9)], [0.95], W, H)
        self.assertEqual(self.ptz.moves[0][0], "bottomright")

    def test_vertical_can_be_disabled(self):
        t = Tracker(Cfg(vertical=False), self.ptz, clock=self.clock)
        for _ in range(2):
            self.clock.t += 0.5
            t.update([box_at(0.0, 0.0, world_y=0.9)], [0.95], W, H)
        self.assertEqual(self.ptz.moves, [],
                         "with vertical off, a purely vertical error is ignored")

    def test_pulse_length_grows_with_the_error(self):
        small = self.t._pulse_length(0.3, 0.0)
        large = self.t._pulse_length(1.0, 0.0)
        self.assertLess(small, large)
        self.assertGreaterEqual(small, self.t.min_pulse_s)
        self.assertLessEqual(large, self.t.max_pulse_s)


class TestSafety(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.ptz = FakePTZ(refuse=True)
        self.t = Tracker(Cfg(), self.ptz, clock=self.clock)

    def test_the_motor_budget_wins(self):
        for _ in range(6):
            self.clock.t += 0.5
            self.t.update([box_at(0.9, 0.0)], [0.95], W, H)
        self.assertFalse(self.t.enabled,
                         "a budget refusal must stop tracking, not retry it "
                         "every frame")

    def test_disabled_by_config_does_nothing(self):
        t = Tracker(Cfg(enabled=False), FakePTZ(), clock=self.clock)
        for _ in range(10):
            self.clock.t += 0.5
            self.assertIsNone(t.update([box_at(0.9, 0.0)], [0.95], W, H))

    def test_a_degenerate_frame_size_is_ignored(self):
        self.assertIsNone(self.t.update([box_at(0.9, 0.0)], [0.95], 0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
