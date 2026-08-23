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

"""A simulated night, driven through the real config.yaml.

Constructs the same object graph surveillance_main.py builds (minus the camera
and the NPU) and plays a plausible evening through it, to check that the
shipped configuration actually produces sensible behaviour -- not just that the
classes work when handed test fixtures.
"""
import datetime as dt, os, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
from alerts import AlertPolicy, ShadowLog  # noqa: E402
from controller import Controller, Detection, State  # noqa: E402
from schedule import Schedule  # noqa: E402
from trigger import TriggerEvent  # noqa: E402


class FakePTZ:
    preset_estimate_s = 1.0

    def __init__(self):
        self.moving = False
        self.gotos = []

    def settled(self):
        return True

    def goto_preset(self, name, source="auto", is_scan_start=False,
                    essential=False):
        self.gotos.append(name)


class TestShippedConfig(unittest.TestCase):
    def setUp(self):
        os.environ["RKNN_CAMERA_PASSWORD"] = "x"
        self.cfg = config.load(ROOT / "config.yaml", ROOT / "does-not-exist.yaml")
        self.tmp = tempfile.TemporaryDirectory()
        self.ptz = FakePTZ()
        self.sched = Schedule.from_config(self.cfg)
        self.wall = dt.datetime(2026, 8, 19, 23, 45)     # Wednesday, armed
        self.policy = AlertPolicy(self.cfg, self.sched, clock=lambda: self.wall)
        self.shadow = ShadowLog(Path(self.tmp.name) / "shadow")
        self.clips = []
        self.now = 0.0
        self.c = Controller(self.cfg, self.ptz, self.sched, self.policy,
                            self.shadow,
                            clip_fn=lambda s, e: self.clips.append((s, e)) or "c.mp4",
                            snapshot_fn=lambda: "s.jpg",
                            clock=lambda: self.now, wall=lambda: self.wall)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("RKNN_CAMERA_PASSWORD", None)

    def advance(self, seconds, step=0.1):
        end = self.now + seconds
        while self.now < end:
            self.now = round(self.now + step, 4)
            self.wall += dt.timedelta(seconds=step)
            self.c.tick()

    def test_shipped_schedule_is_armed_at_midnight_and_not_at_noon(self):
        self.assertTrue(self.sched.is_armed(dt.datetime(2026, 8, 19, 23, 45)))
        self.assertFalse(self.sched.is_armed(dt.datetime(2026, 8, 19, 12, 0)))

    def test_shipped_presets_are_swept_in_order(self):
        self.c.on_pir(TriggerEvent("active", 0))
        self.advance(2.0)
        self.assertIs(self.c.state, State.SCANNING)
        expected = self.cfg._get("ptz", "scan_presets")
        self.advance(40.0)
        self.assertEqual(self.ptz.gotos[:len(expected)], expected)

    def test_an_intruder_at_midnight_would_alert(self):
        self.c.on_pir(TriggerEvent("active", 0))
        self.advance(2.0)
        for _ in range(8):
            self.c.on_detection(Detection(self.wall, 1, 0.91, ["person"]))
            self.advance(1.0)
        # The PIR's timer runs out; then a minute with no trigger closes it.
        self.c.on_pir(TriggerEvent("inactive", 0, 20.0))
        self.advance(65.0)
        rows = self.shadow.read()
        self.assertEqual(len(rows), 1, rows)
        self.assertTrue(rows[0]["would_alert"],
                        f"shipped thresholds rejected a real intruder: {rows[0]}")
        self.assertEqual(len(self.clips), 1)

    def test_the_same_intruder_during_club_hours_would_not(self):
        self.wall = dt.datetime(2026, 8, 19, 14, 0)
        self.c.on_pir(TriggerEvent("active", 0))
        self.advance(2.0)
        for _ in range(8):
            self.c.on_detection(Detection(self.wall, 1, 0.91, ["person"]))
            self.advance(1.0)
        # The PIR's timer runs out; then a minute with no trigger closes it.
        self.c.on_pir(TriggerEvent("inactive", 0, 20.0))
        self.advance(65.0)
        rows = self.shadow.read()
        self.assertFalse(rows[0]["would_alert"])
        self.assertEqual(rows[0]["failed"], "schedule")
        self.assertEqual(len(self.clips), 1,
                         "recording is unaffected by the schedule -- only "
                         "alerting is gated")

    def test_a_quiet_night_produces_nothing_at_all(self):
        self.advance(600.0)
        self.assertIs(self.c.state, State.PARKED)
        self.assertEqual(self.ptz.gotos, [], "the motors must not run all night")
        self.assertEqual(self.shadow.read(), [])

    def test_a_cat_sized_false_trigger_costs_one_sweep_and_no_alert(self):
        self.c.on_pir(TriggerEvent("active", 0))
        self.advance(2.0)
        self.advance(60.0)                    # sweeps, sees nobody
        self.c.on_pir(TriggerEvent("inactive", 0, 30.0))
        self.advance(65.0)
        self.assertIs(self.c.state, State.PARKED)
        self.assertLessEqual(len(self.ptz.gotos), 5)

        # The PIR triggers a capture on its own, so there is a row -- and it
        # is blocked by persistence, having no sightings at all. That is worth
        # recording: it is exactly how often the sensor fires at nothing,
        # which is what the corroboration gate exists for.
        rows = self.shadow.read()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["would_alert"])
        self.assertEqual(rows[0]["failed"], "persistence")
        self.assertEqual(rows[0]["sightings"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
