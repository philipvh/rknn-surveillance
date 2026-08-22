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

"""Tests for the mode that survives a reboot.

wifi_mode.sh writes the chosen mode into NetworkManager's autoconnect,
because autoconnect is the only thing consulted at boot. Getting this wrong
is expensive in a specific way: set every wireless connection to `no` and the
board comes back at the club with a dead radio, a wall tablet showing
nothing, and no way in that does not involve carrying a laptop to the site.

nmcli is a stub, so this runs anywhere. Only the prefer() function is
extracted and exercised -- the rest of the script needs a real radio.
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "wifi_mode.sh"

# A name with a space and a name with an escaped colon: both are real, and
# both have already been got wrong once -- the space by shell word splitting,
# the colon by nmcli's own terse escaping.
CONNECTIONS = [
    r"rknn-ap:802-11-wireless",
    r"phone hotspot 1:802-11-wireless",
    r"Bar \: Grill:802-11-wireless",
    r"Wired connection 1:802-3-ethernet",
    r"tun0:tun",
]

STUB = r"""#!/usr/bin/env bash
if [ "$1" = "-t" ] && [ "$3" = "NAME,TYPE" ]; then
  cat "$CONNS"
  exit 0
fi
if [ "$1" = "connection" ] && [ "$2" = "modify" ]; then
  echo "$3|$5|$7" >> "$LOGF"
fi
exit 0
"""


def run_prefer(mode, want=""):
    """Run prefer() from the real script against a stub nmcli."""
    body = re.search(r"^prefer\(\) \{.*?^\}", SCRIPT.read_text(), re.S | re.M)
    assert body, "prefer() not found in wifi_mode.sh"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "bin").mkdir()
        stub = tmp / "bin" / "nmcli"
        stub.write_text(STUB)
        stub.chmod(0o755)
        (tmp / "conns").write_text("\n".join(CONNECTIONS) + "\n")
        log = tmp / "log"
        log.touch()
        env = dict(os.environ)
        env.update(PATH="%s:%s" % (tmp / "bin", env["PATH"]),
                   CONNS=str(tmp / "conns"), LOGF=str(log), AP_CON="rknn-ap")
        script = "say(){ :; }\n%s\nprefer %s %s\n" % (
            body.group(0), mode, want and "'%s'" % want)
        p = subprocess.run(["bash", "-c", script], env=env,
                           capture_output=True, text=True, timeout=30)
        assert p.returncode == 0, p.stderr
        out = {}
        for line in log.read_text().splitlines():
            name, auto, prio = line.split("|")
            out[name] = (auto, prio)
        return out


class TestModeSurvivesReboot(unittest.TestCase):

    def test_ap_mode_wins_the_radio_at_boot(self):
        got = run_prefer("ap")
        self.assertEqual(got["rknn-ap"], ("yes", "100"))
        self.assertEqual(got["phone hotspot 1"], ("no", "0"))

    def test_client_mode_gives_the_radio_back(self):
        got = run_prefer("client", "phone hotspot 1")
        self.assertEqual(got["rknn-ap"], ("no", "0"))
        self.assertEqual(got["phone hotspot 1"], ("yes", "100"))

    def test_the_wired_connection_is_never_touched(self):
        # It is how anyone gets in to fix a wireless setting that went wrong.
        for mode in ("ap", "client"):
            got = run_prefer(mode)
            self.assertNotIn("Wired connection 1", got, mode)
            self.assertNotIn("tun0", got, mode)

    def test_a_name_with_a_space_is_one_connection(self):
        # Word splitting turned this into 'phone hotspot' and '1' once already.
        got = run_prefer("ap")
        self.assertIn("phone hotspot 1", got)
        self.assertNotIn("1", got)

    def test_the_escaped_separator_is_undone(self):
        # nmcli -t escapes the colon on the way out; passing it back with the
        # backslash still in it names a connection that does not exist, and
        # that connection then keeps whatever autoconnect it had.
        got = run_prefer("ap")
        self.assertIn("Bar : Grill", got)
        self.assertNotIn(r"Bar \: Grill", got)

    def test_never_leaves_every_radio_off(self):
        # The failure that strands the board.
        for mode in ("ap", "client"):
            got = run_prefer(mode, "phone hotspot 1")
            on = [n for n, (a, _) in got.items() if a == "yes"]
            self.assertTrue(on, "%s mode turned every wireless connection off"
                            % mode)

    def test_exactly_one_connection_is_preferred(self):
        for mode, want in (("ap", ""), ("client", "phone hotspot 1")):
            got = run_prefer(mode, want)
            top = [n for n, (a, p) in got.items() if a == "yes" and p == "100"]
            self.assertEqual(len(top), 1, "%s: %r" % (mode, top))


if __name__ == "__main__":
    unittest.main()
