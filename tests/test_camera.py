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

"""The camera abstraction, and the Foscam backend behind it.

The test that matters most here is TestSomeoneElsesCamera: a backend written
by someone who has never read ptz.py must still get the deadline watchdog and
the motor budget. If that ever stops being true, the abstraction has become a
way to bypass the safety layer rather than a way to reuse it.
"""
import datetime as dt
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import camera  # noqa: E402
from camera import (Cap, CameraBackend, CameraError, NotSupported,  # noqa: E402
                    FoscamBackend)
from camera.foscam import foscam_time_params  # noqa: E402
from ptz import PTZ, BudgetExceeded  # noqa: E402

OK = b"<CGI_Result><result>0</result></CGI_Result>"


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.fail_cmds = set()

    def get(self, url, params, timeout):
        cmd = params.get("cmd")
        self.calls.append(cmd)
        if cmd in self.fail_cmds:
            raise OSError("simulated failure on " + str(cmd))
        if cmd == "snapPicture2":
            return b"\xff\xd8jpeg"
        if cmd == "getPTZPresetPointList":
            return (b"<CGI_Result><result>0</result><cnt>2</cnt>"
                    b"<point0>Home</point0><point1>Gate</point1></CGI_Result>")
        return OK

    def count(self, c):
        return self.calls.count(c)


class StubCfg:
    """Only what a backend and PTZ actually read."""
    camera_host = "10.0.0.5"
    camera_port = 88
    camera_user = "admin"
    camera_password = "pw"

    def __init__(self, ptz=None):
        self._ptz = ptz if ptz is not None else {
            "enabled": True, "move_deadline_s": 0.2,
            "watchdog_interval_s": 0.02, "max_continuous_move_s": 2.0,
            "settle_s": 0.0,
            "budget": {"auto_seconds_per_hour": 10,
                       "manual_seconds_per_hour": 5},
            "http": {"timeout_s": 1, "stop_timeout_s": 0.3, "stop_retries": 2},
        }

    def _get(self, *keys, default=None):
        if keys and keys[0] == "ptz":
            node = self._ptz
            for k in keys[1:]:
                if not isinstance(node, dict) or k not in node:
                    return default
                node = node[k]
            return node
        return default


# ------------------------------------------------------------ the contract

class TestBackendContract(unittest.TestCase):
    def test_optional_operations_say_they_are_unsupported(self):
        # A minimal backend must not have to implement everything, and callers
        # must be able to tell "cannot" from "failed".
        class Bare(CameraBackend):
            name = "bare"
            def start_move(self, direction): pass
            def stop(self, kind=None, timeout=None): pass

        b = Bare(StubCfg())
        for call in (lambda: b.list_presets(), lambda: b.snapshot(),
                     lambda: b.start_zoom("in"), lambda: b.set_speed(1),
                     lambda: b.set_clock(dt.datetime(2026, 1, 1), 0)):
            with self.assertRaises(NotSupported):
                call()

    def test_not_supported_is_a_camera_error(self):
        # so a caller that only catches CameraError still behaves
        self.assertTrue(issubclass(NotSupported, CameraError))


# ----------------------------------------------------------------- foscam

class TestFoscamProtocol(unittest.TestCase):
    def backend(self):
        self.t = FakeTransport()
        return FoscamBackend(StubCfg(), transport=self.t)

    def test_malformed_xml_still_yields_the_result(self):
        out = FoscamBackend.parse("x", b"junk<result>0</result>trailing")
        self.assertEqual(out["result"], 0)

    def test_nonzero_result_is_an_error(self):
        with self.assertRaises(CameraError) as e:
            FoscamBackend.parse(
                "ptzMoveLeft", b"<CGI_Result><result>-3</result></CGI_Result>")
        self.assertIn("result=-3", str(e.exception))

    def test_move_and_zoom_send_the_vendor_commands(self):
        b = self.backend()
        b.start_move("left")
        b.start_zoom("in")
        self.assertIn("ptzMoveLeft", self.t.calls)
        self.assertIn("zoomIn", self.t.calls)

    def test_stopping_a_zoom_stops_the_zoom_and_the_dome(self):
        b = self.backend()
        b.stop("zoom")
        self.assertEqual(self.t.calls, ["zoomStop", "ptzStopRun"])

    def test_saving_a_preset_deletes_first_so_it_overwrites(self):
        # The camera's own add silently refuses to replace an existing name,
        # which made a panel "Set" button a no-op. Order matters.
        b = self.backend()
        b.save_preset("Home")
        order = [c for c in self.t.calls
                 if c in ("ptzDeletePresetPoint", "ptzAddPresetPoint")]
        self.assertEqual(order, ["ptzDeletePresetPoint", "ptzAddPresetPoint"])

    def test_saving_still_works_when_the_name_was_not_there(self):
        b = self.backend()
        self.t.fail_cmds.add("ptzDeletePresetPoint")
        b.save_preset("Fresh")
        self.assertIn("ptzAddPresetPoint", self.t.calls)

    def test_presets_come_back_in_order(self):
        b = self.backend()
        self.assertEqual(b.list_presets(), ["Home", "Gate"])

    def test_it_admits_it_cannot_report_position(self):
        # A caller wanting a posture readout has to be able to find this out
        # rather than discover it as a -3 at runtime.
        self.assertFalse(FoscamBackend.supports(Cap.ABSOLUTE_POSITION))
        self.assertTrue(FoscamBackend.supports(Cap.PRESETS))

    def test_stream_defaults_are_the_foscam_ones(self):
        self.assertEqual(FoscamBackend.HTTP_PORT, 88)
        self.assertIsNone(FoscamBackend.RTSP_PORT)   # RTSP rides on HTTP port
        self.assertEqual(FoscamBackend.MAIN_PATH, "videoMain")


class TestFoscamClock(unittest.TestCase):
    """A sign error here puts a wrong timestamp on every recording."""

    def test_offset_sign_is_inverted(self):
        p = foscam_time_params(dt.datetime(2026, 1, 15, 8, 30, 0), 3600)
        self.assertEqual(p["timeZone"], "-3600")

    def test_summer_offset_carries_dst_not_the_flag(self):
        p = foscam_time_params(dt.datetime(2026, 7, 15, 8, 30, 0), 7200)
        self.assertEqual(p["timeZone"], "-7200")
        self.assertEqual(p["isDst"], "0")   # firmware ignores its own flag

    def test_carries_utc_not_local(self):
        p = foscam_time_params(dt.datetime(2026, 7, 15, 8, 47, 9), 7200)
        self.assertEqual((p["hour"], p["minute"], p["sec"]), ("8", "47", "9"))
        self.assertEqual(p["timeSource"], "1")

    def test_west_of_utc(self):
        p = foscam_time_params(dt.datetime(2026, 1, 1, 0, 0, 0), -18000)
        self.assertEqual(p["timeZone"], "18000")


# --------------------------------------------------------------- registry

class TestRegistry(unittest.TestCase):
    def test_foscam_is_the_default(self):
        self.assertIs(camera.backend_class(None), FoscamBackend)
        self.assertIs(camera.backend_class("foscam"), FoscamBackend)

    def test_names_are_case_insensitive(self):
        self.assertIs(camera.backend_class("FosCam"), FoscamBackend)

    def test_unknown_name_says_what_is_known(self):
        with self.assertRaises(CameraError) as e:
            camera.backend_class("nosuchcam")
        self.assertIn("foscam", str(e.exception))

    def test_a_backend_can_live_outside_this_repo(self):
        # The dotted form is what keeps someone with an odd camera from
        # having to fork. Point it at this very module.
        cls = camera.backend_class("tests.test_camera:ExampleBackend")
        # identity, not `is`: pytest imports this module under two names, so
        # the loaded class is a distinct object with the same definition.
        self.assertTrue(issubclass(cls, CameraBackend))
        self.assertEqual(cls.__name__, "ExampleBackend")

    def test_a_dotted_path_to_something_else_is_refused(self):
        with self.assertRaises(CameraError):
            camera.backend_class("tests.test_camera:StubCfg")

    def test_registering_a_non_backend_is_refused(self):
        with self.assertRaises(TypeError):
            camera.register(StubCfg, name="bogus")


# ------------------------------------------ the point of the whole exercise

class ExampleBackend(CameraBackend):
    """A camera written by someone who never read ptz.py."""
    name = "example"
    RTSP_PORT = 554
    MAIN_PATH = "stream1"
    SUB_PATH = "stream2"
    CAPABILITIES = frozenset({Cap.PRESETS})

    def __init__(self, cfg, transport=None, timeout=5.0):
        super().__init__(cfg, transport=transport, timeout=timeout)
        self.moves = []
        self.stops = 0
        self.presets = {}
        self.lock = threading.Lock()

    def start_move(self, direction):
        with self.lock:
            self.moves.append(direction)

    def stop(self, kind=None, timeout=None):
        with self.lock:
            self.stops += 1

    def goto_preset(self, name):
        with self.lock:
            self.presets.setdefault(name, 0)
            self.presets[name] += 1


class TestSomeoneElsesCamera(unittest.TestCase):
    """A third-party backend inherits every safety property for free."""

    def drive(self, **ptzcfg):
        cfg = StubCfg()
        cfg._ptz.update(ptzcfg)
        back = ExampleBackend(cfg)
        p = PTZ(cfg, backend=back, install_signal_handlers=False)
        self.addCleanup(lambda: setattr(p, "_closed", True))
        return p, back

    def test_it_moves_through_the_new_backend(self):
        p, back = self.drive()
        p.move("left", source="manual")
        self.assertEqual(back.moves, ["left"])

    def test_the_watchdog_stops_a_camera_it_has_never_seen_before(self):
        # No refresh after the deadline -> the watchdog must stop it, with no
        # cooperation from the backend beyond implementing stop().
        p, back = self.drive()
        before = back.stops
        p.move("right", source="manual")
        time.sleep(0.5)                       # deadline is 0.2s
        self.assertFalse(p.moving)
        self.assertGreater(back.stops, before)

    def test_the_motor_budget_applies_to_it_too(self):
        p, back = self.drive()
        p.budget.record(5, "manual")          # manual ceiling is 5s
        with self.assertRaises(BudgetExceeded):
            p.move("left", source="manual")
        self.assertEqual(back.moves, [], "a refused move reached the camera")

    def test_a_direction_this_camera_lacks_is_refused_before_the_wire(self):
        p, back = self.drive()
        with self.assertRaises(CameraError):
            p.move("topleft", source="manual")   # base has no diagonals
        self.assertEqual(back.moves, [])

    def test_unsupported_features_surface_as_not_supported(self):
        p, _ = self.drive()
        with self.assertRaises(NotSupported):
            p.snapshot()
        self.assertFalse(p.supports(Cap.SNAPSHOT))
        self.assertTrue(p.supports(Cap.PRESETS))

    def test_startup_sends_a_rescue_stop(self):
        # The layer that covers a previous process killed mid-move.
        cfg = StubCfg()
        back = ExampleBackend(cfg)
        p = PTZ(cfg, backend=back, install_signal_handlers=False,
                stop_on_start=True)
        self.addCleanup(lambda: setattr(p, "_closed", True))
        self.assertGreaterEqual(back.stops, 1)


if __name__ == "__main__":
    unittest.main()
