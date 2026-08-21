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

"""Tests for joining a wireless network.

nmcli is faked, so these run on a machine with no radio and no
NetworkManager. What is being tested is the parsing and the decisions -- the
places where a wrong answer either strands the board at a site with no
keyboard, or puts a password somewhere it can be read.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from wifi import WiFi, _split  # noqa: E402

DEVICES = "enP4p65s0:ethernet:connected\nwlan0:wifi:disconnected\nlo:loopback:unmanaged"
NO_RADIO = "enP4p65s0:ethernet:connected\nlo:loopback:unmanaged"
LIST = "\n".join([
    "*:Phone:72:WPA2",
    " :Phone:45:WPA2",          # the same network on the other band
    " :ClubGuest:60:WPA2",
    " :OpenNet:31:",
    " ::20:WPA2",               # hidden, no name
])
CONNS = "Wired connection 1:802-3-ethernet\nPhone:802-11-wireless\ndocker0:bridge"


class Fake:
    """Records what nmcli was asked, and answers from a script."""

    def __init__(self, **replies):
        self.replies = replies
        self.calls = []
        self.stdins = []

    def __call__(self, args, stdin=None, timeout=None):
        self.calls.append(args)
        self.stdins.append(stdin)
        joined = " ".join(args)
        # Longest needle first: "device" is a substring of "device wifi list",
        # and matching the vaguer one would answer the wrong question.
        for needle in sorted(self.replies, key=len, reverse=True):
            if needle in joined:
                return self.replies[needle]
        return 0, "", ""


class TestParsing(unittest.TestCase):
    def test_a_colon_in_a_network_name_survives(self):
        self.assertEqual(_split(r"*:Bar\: Grill:70:WPA2", 4),
                         ["*", "Bar: Grill", "70", "WPA2"])

    def test_missing_fields_are_padded_not_crashed(self):
        self.assertEqual(_split("a:b", 4), ["a", "b", "", ""])


class TestScanning(unittest.TestCase):
    def radio(self):
        return WiFi(runner=Fake(**{
            "device": (0, DEVICES, ""),
            "wifi list": (0, LIST, ""),
            "connection show": (0, CONNS, ""),
        }))

    def test_the_same_network_on_two_bands_appears_once(self):
        names = [n["ssid"] for n in self.radio().scan()]
        self.assertEqual(names.count("Phone"), 1)

    def test_the_stronger_of_the_two_is_kept(self):
        got = [n for n in self.radio().scan() if n["ssid"] == "Phone"][0]
        self.assertEqual(got["signal"], 72)

    def test_a_nameless_network_is_dropped(self):
        # It is real, but there is nothing to click: join it by name instead.
        self.assertNotIn("", [n["ssid"] for n in self.radio().scan()])

    def test_the_connected_one_sorts_first(self):
        self.assertTrue(self.radio().scan()[0]["in_use"])

    def test_the_rest_sort_by_signal(self):
        rest = [n["signal"] for n in self.radio().scan()[1:]]
        self.assertEqual(rest, sorted(rest, reverse=True))

    def test_a_saved_network_is_marked(self):
        by = {n["ssid"]: n for n in self.radio().scan()}
        self.assertTrue(by["Phone"]["saved"])
        self.assertFalse(by["ClubGuest"]["saved"])

    def test_an_unsecured_network_says_open(self):
        by = {n["ssid"]: n for n in self.radio().scan()}
        self.assertEqual(by["OpenNet"]["security"], "open")

    def test_no_radio_means_no_scan_and_no_error(self):
        radio = WiFi(runner=Fake(**{"device": (0, NO_RADIO, "")}))
        self.assertEqual(radio.scan(), [])
        self.assertFalse(radio.status()["present"])


class TestConnecting(unittest.TestCase):
    def test_the_password_goes_in_on_stdin_never_in_the_arguments(self):
        f = Fake(**{"device": (0, DEVICES, "")})
        WiFi(runner=f).connect("Phone", "hunter2hunter2")
        for args in f.calls:
            self.assertNotIn("hunter2hunter2", " ".join(args),
                             "argv is world-readable in /proc while it runs")
        self.assertIn("hunter2hunter2\n", f.stdins)

    def test_a_saved_network_is_rejoined_without_a_password(self):
        f = Fake(**{"device": (0, DEVICES, "")})
        WiFi(runner=f).connect("Phone", None)
        self.assertNotIn("--ask", " ".join(f.calls[-1]))
        self.assertIsNone(f.stdins[-1])

    def test_a_wrong_password_is_reported_as_such(self):
        f = Fake(**{"device": (0, DEVICES, ""),
                    "wifi connect": (4, "", "Error: Secrets were required")})
        ok, msg = WiFi(runner=f).connect("Phone", "nope")
        self.assertFalse(ok)
        self.assertIn("Wrong password", msg)

    def test_a_missing_polkit_rule_says_what_to_run(self):
        f = Fake(**{"device": (0, DEVICES, ""),
                    "wifi connect": (4, "", "Error: not authorized")})
        ok, msg = WiFi(runner=f).connect("Phone", "x")
        self.assertFalse(ok)
        self.assertIn("install.sh", msg)

    def test_an_absent_network_suggests_the_hidden_option(self):
        f = Fake(**{"device": (0, DEVICES, ""),
                    "wifi connect": (10, "", "Error: No network with SSID 'X'")})
        ok, msg = WiFi(runner=f).connect("X", "x")
        self.assertFalse(ok)
        self.assertIn("hidden", msg)

    def test_with_no_adapter_it_says_so_rather_than_failing_oddly(self):
        f = Fake(**{"device": (0, NO_RADIO, "")})
        ok, msg = WiFi(runner=f).connect("Phone", "x")
        self.assertFalse(ok)
        self.assertIn("dongle", msg)

    def test_an_empty_name_is_refused_before_nmcli_is_run(self):
        f = Fake()
        ok, _ = WiFi(runner=f).connect("   ", "x")
        self.assertFalse(ok)
        self.assertEqual(f.calls, [])

    def test_hidden_is_passed_through(self):
        f = Fake(**{"device": (0, DEVICES, "")})
        WiFi(runner=f).connect("Phone", "x", hidden=True)
        self.assertIn("hidden yes", " ".join(f.calls[-1]))

    def test_a_timeout_is_a_message_not_an_exception(self):
        f = Fake(**{"device": (0, DEVICES, ""),
                    "wifi connect": (124, "", "nmcli timed out")})
        ok, msg = WiFi(runner=f).connect("Phone", "x")
        self.assertFalse(ok)
        self.assertTrue(msg)


class TestForgetting(unittest.TestCase):
    def radio(self, **extra):
        r = {"device": (0, DEVICES, ""), "connection show": (0, CONNS, "")}
        r.update(extra)
        self.fake = Fake(**r)
        return WiFi(runner=self.fake)

    def test_forgetting_a_saved_network(self):
        ok, _ = self.radio().forget("Phone")
        self.assertTrue(ok)
        self.assertIn(["connection", "delete", "Phone"], self.fake.calls)

    def test_it_refuses_to_delete_something_that_is_not_wireless(self):
        # The wired connection is how anyone gets in to fix a bad wireless
        # setting. Deleting it from a web page would be unrecoverable.
        ok, msg = self.radio().forget("Wired connection 1")
        self.assertFalse(ok)
        self.assertNotIn(["connection", "delete", "Wired connection 1"],
                         self.fake.calls)

    def test_it_refuses_an_unknown_name(self):
        ok, _ = self.radio().forget("Nonsense")
        self.assertFalse(ok)


class TestStatus(unittest.TestCase):
    SHOW = ("GENERAL.STATE:100 (connected)\n"
            "GENERAL.CONNECTION:Phone\n"
            "IP4.ADDRESS[1]:192.168.43.55/24")

    def test_it_reports_the_network_and_address(self):
        radio = WiFi(runner=Fake(**{
            "device show": (0, self.SHOW, ""),
            "device": (0, DEVICES, ""),
            "wifi list": (0, LIST, ""),
            "connection show": (0, CONNS, ""),
        }))
        st = radio.status()
        self.assertTrue(st["connected"])
        self.assertEqual(st["ssid"], "Phone")
        self.assertEqual(st["ip"], "192.168.43.55/24")
        self.assertEqual(st["signal"], 72)

    def test_an_adapter_with_no_address_is_not_connected(self):
        radio = WiFi(runner=Fake(**{
            "device show": (0, "GENERAL.STATE:30 (disconnected)", ""),
            "device": (0, DEVICES, ""),
            "wifi list": (0, "", ""),
            "connection show": (0, CONNS, ""),
        }))
        self.assertFalse(radio.status()["connected"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
