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

"""Tests for the tunnel status panel.

No network is touched: the point of the module is that it reports facts it can
read locally, and only reaches out when somebody presses the button.
"""
import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import vpn  # noqa: E402

ADDR = json.dumps([
    {"ifname": "lo", "operstate": "UNKNOWN",
     "addr_info": [{"family": "inet", "local": "127.0.0.1"}]},
    {"ifname": "enP4p65s0", "operstate": "UP",
     "addr_info": [{"family": "inet", "local": "192.168.90.131"}]},
    {"ifname": "tun0", "operstate": "UNKNOWN",
     "addr_info": [{"family": "inet", "local": "10.8.0.6"}]},
])
NO_TUN = json.dumps([
    {"ifname": "enP4p65s0", "operstate": "UP",
     "addr_info": [{"family": "inet", "local": "192.168.90.131"}]},
])
ROUTE = json.dumps([{"dst": "default", "gateway": "192.168.90.1",
                     "dev": "enP4p65s0"}])
UNITS = ("openvpn-client@club.service loaded active running OpenVPN tunnel\n")


class Base(unittest.TestCase):
    def fake_run(self, table):
        def run(args, timeout=5):
            key = " ".join(args)
            for needle in sorted(table, key=len, reverse=True):
                if needle in key:
                    return table[needle]
            return 1, "", ""
        self._real = vpn._run
        vpn._run = run
        self.addCleanup(setattr, vpn, "_run", self._real)


class TestTunnels(Base):
    def test_a_tun_interface_with_an_address_is_found(self):
        self.fake_run({"addr show": (0, ADDR, "")})
        self.assertEqual(vpn.tunnels(),
                         [{"iface": "tun0", "up": True, "ip": "10.8.0.6"}])

    def test_ordinary_interfaces_are_not_mistaken_for_tunnels(self):
        self.fake_run({"addr show": (0, ADDR, "")})
        names = [t["iface"] for t in vpn.tunnels()]
        self.assertNotIn("enP4p65s0", names)
        self.assertNotIn("lo", names)

    def test_no_tunnel_is_reported_as_down_not_as_an_error(self):
        self.fake_run({"addr show": (0, NO_TUN, ""),
                       "route show": (0, ROUTE, "")})
        st = vpn.status()
        self.assertFalse(st["up"])
        self.assertEqual(st["address"], "")

    def test_broken_json_does_not_raise(self):
        self.fake_run({"addr show": (0, "not json", "")})
        self.assertEqual(vpn.tunnels(), [])

    def test_ip_missing_entirely_does_not_raise(self):
        self.fake_run({})
        self.assertEqual(vpn.tunnels(), [])
        self.assertEqual(vpn.default_route(), {})


class TestUnits(Base):
    def test_a_running_client_is_reported(self):
        self.fake_run({"list-units": (0, UNITS, ""),
                       "is-enabled": (0, "enabled\n", ""),
                       "is-active": (0, "active\n", "")})
        u = vpn.units()
        self.assertEqual(len(u), 1)
        self.assertEqual(u[0]["name"], "club")
        self.assertEqual(u[0]["active"], "active")
        self.assertEqual(u[0]["enabled"], "enabled")

    def test_a_config_with_no_unit_still_appears(self):
        # The normal state before anyone has enabled it. Saying so beats
        # showing an empty table and letting someone wonder.
        self.fake_run({"list-units": (0, "", ""),
                       "is-enabled": (1, "disabled\n", ""),
                       "is-active": (1, "inactive\n", "")})
        real = vpn.configs
        vpn.configs = lambda: ["club"]
        self.addCleanup(setattr, vpn, "configs", real)
        u = vpn.units()
        self.assertEqual([x["name"] for x in u], ["club"])
        self.assertEqual(u[0]["enabled"], "disabled")


class TestStatus(Base):
    def test_it_reports_the_route_traffic_leaves_by(self):
        self.fake_run({"addr show": (0, ADDR, ""),
                       "route show": (0, ROUTE, ""),
                       "list-units": (0, "", ""),
                       "is-enabled": (0, "enabled\n", ""),
                       "is-active": (0, "active\n", "")})
        st = vpn.status()
        self.assertTrue(st["up"])
        self.assertEqual(st["address"], "10.8.0.6")
        self.assertEqual(st["route"]["dev"], "enP4p65s0")


class TestReachability(unittest.TestCase):
    def test_an_unroutable_address_fails_quickly_and_says_so(self):
        # 198.51.100.0/24 is reserved for documentation: nothing answers.
        ok, msg = vpn.reach("198.51.100.1", 443, timeout=0.6)
        self.assertFalse(ok)
        self.assertTrue(msg)

    def test_it_checks_an_address_not_a_name(self):
        # A name would conflate "no route" with "no DNS", which need
        # different fixes -- that distinction is the value of the check.
        import inspect
        self.assertIn('host="1.1.1.1"', inspect.signature(vpn.reach).__str__()
                      .replace("'", '"'))




class TestControl(Base):
    """Working the tunnel from the panel, which means running as root."""

    def setUp(self):
        self._helper = vpn.HELPER
        self.addCleanup(setattr, vpn, "HELPER", self._helper)

    def with_helper(self, answer=None, delay=0.0):
        """A writable request directory, and root answering (or not)."""
        import shutil
        import tempfile
        import threading
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        vpn.HELPER = str(Path(d) / "rknn-vpnctl")
        Path(vpn.HELPER).write_text("#!/bin/sh\n")
        self._req_dir = vpn.REQ_DIR
        self.addCleanup(setattr, vpn, "REQ_DIR", self._req_dir)
        vpn.REQ_DIR = d
        self.dir = Path(d)

        if answer is not None:
            # Stand in for the .path unit noticing the request.
            def responder():
                for _ in range(200):
                    if (self.dir / "request").exists():
                        self.written = (self.dir / "request").read_text()
                        (self.dir / "request").unlink()
                        (self.dir / "result").write_text(answer)
                        return
                    time.sleep(0.01)
            t = threading.Thread(target=responder, daemon=True)
            t.start()
            self.addCleanup(t.join, 1)

    def test_a_bad_name_never_becomes_a_request(self):
        # The name is checked here and again inside the helper. This side
        # exists so nothing malformed is ever written down at all.
        self.with_helper()
        for bad in ("../../etc/passwd", "club.service", "club;reboot", ""):
            ok, _ = vpn.control("enable", bad)
            self.assertFalse(ok, bad)
        self.assertFalse((self.dir / "request").exists())

    def test_an_unknown_action_is_refused(self):
        self.with_helper()
        ok, _ = vpn.control("uninstall", "club")
        self.assertFalse(ok)
        self.assertFalse((self.dir / "request").exists())

    def test_a_request_is_written_for_root_to_pick_up(self):
        self.with_helper(answer="ok done")
        vpn.control("enable", "club")
        self.assertEqual(self.written.strip(), "enable club")

    def test_enabling_says_it_covers_boot_and_now(self):
        self.with_helper(answer="ok done")
        ok, msg = vpn.control("enable", "club")
        self.assertTrue(ok)
        self.assertIn("boot", msg)

    def test_a_refusal_from_root_is_reported(self):
        self.with_helper(answer="fail no such tunnel configuration")
        ok, msg = vpn.control("enable", "club")
        self.assertFalse(ok)
        self.assertIn("no such tunnel", msg)

    def test_no_watcher_running_is_a_message_not_a_hang(self):
        self.with_helper()                 # nobody answers
        started = time.time()
        ok, msg = vpn.control("enable", "club", timeout=1.0)
        self.assertFalse(ok)
        self.assertIn("No answer", msg)
        self.assertLess(time.time() - started, 5,
                        "it must give up, not wait for the page to time out")

    def test_a_stale_answer_is_not_read_as_this_one(self):
        self.with_helper(answer="ok done")
        (self.dir / "result").write_text("ok from an earlier request")
        vpn.control("enable", "club")
        self.assertEqual(self.written.strip(), "enable club")

    def test_a_missing_helper_says_what_to_run(self):
        vpn.HELPER = "/nonexistent/rknn-vpnctl"
        ok, msg = vpn.control("enable", "club")
        self.assertFalse(ok)
        self.assertIn("install.sh", msg)

    def test_can_control_needs_both_ends_present(self):
        vpn.HELPER = "/nonexistent/rknn-vpnctl"
        self.assertFalse(vpn.can_control())


if __name__ == "__main__":
    unittest.main(verbosity=2)
