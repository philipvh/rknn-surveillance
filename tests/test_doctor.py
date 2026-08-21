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

"""Smoke tests for the doctor.

It is the tool someone reaches for when things are broken, so it must not
itself break -- including on a machine where nearly everything is missing.
"""
import io, contextlib, os, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import doctor  # noqa: E402


class TestDoctor(unittest.TestCase):
    def run_doctor(self, *argv):
        os.environ["TVW_CAMERA_PASSWORD"] = "x"
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = doctor.main(list(argv))
        finally:
            os.environ.pop("TVW_CAMERA_PASSWORD", None)
        return rc, buf.getvalue()

    def test_it_runs_without_a_camera_and_does_not_raise(self):
        rc, out = self.run_doctor("--no-camera")
        self.assertIn(rc, (0, 1))
        self.assertIn("Configuration", out)
        self.assertIn("Clock", out)

    def test_failures_produce_a_nonzero_exit(self):
        """So it can be run from cron and actually mean something."""
        rc, out = self.run_doctor("--no-camera")
        if "FAIL" in out:
            self.assertEqual(rc, 1)
        else:
            self.assertEqual(rc, 0)

    def test_quiet_hides_passing_checks(self):
        _, loud = self.run_doctor("--no-camera")
        _, quiet = self.run_doctor("--no-camera", "--quiet")
        self.assertLess(len(quiet), len(loud))
        self.assertNotIn("  ok  ", quiet)

    def test_every_failure_carries_a_fix(self):
        d = doctor.Doctor(quiet=True)
        with contextlib.redirect_stdout(io.StringIO()):
            doctor.check_clock(d)
            doctor.check_deps(d)
        for name, status, detail, fix in d.results:
            if status == doctor.FAIL:
                self.assertTrue(fix.strip(),
                                f"the check {name!r} fails without saying what "
                                f"to do about it")

    def test_the_rtc_check_is_present_because_there_is_no_ntp(self):
        d = doctor.Doctor(quiet=True)
        with contextlib.redirect_stdout(io.StringIO()):
            doctor.check_clock(d)
        names = [r[0] for r in d.results]
        self.assertIn("RTC present", names)
        self.assertIn("clock has not gone backwards", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
