# Copyright 2026 Philip van Houtte, magicview.tv, the Netherlands
# SPDX-License-Identifier: Apache-2.0
"""Which networks are free is read from the kernel, not from a list."""

import ipaddress
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import netinfo  # noqa: E402

HEADER = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask"
          "\t\tMTU\tWindow\tIRTT\n")

# Copied from the club board on 2026-09-25, verbatim. enP4p65s0 is the wire to
# the 4G router, enx.. the camera LAN, wlx.. the board's own access point --
# and tun0 carries on-link routes for the home LAN behind the tunnel.
CLUB = HEADER + "".join(line + "\n" for line in [
    "enP4p65s0\t00000000\t0108A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0",
    "tun0\t0002080A\t0502080A\t0003\t0\t0\t0\t00FFFFFF\t0\t0\t0",
    "tun0\t0502080A\t00000000\t0005\t0\t0\t0\tFFFFFFFF\t0\t0\t0",
    "docker0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0",
    "enP4p65s0\t0008A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0",
    "tun0\t005AA8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0",
    "enx00606ed70c2a\t005BA8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0",
    "wlxe84e067c38f9\t005CA8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0",
])


class Base(unittest.TestCase):
    def route(self, text):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def nets(self, text):
        return [str(n) for n in netinfo.attached_networks(self.route(text))]


class TestAttachedNetworks(Base):
    def test_the_boards_own_wiring_is_found(self):
        got = self.nets(CLUB)
        self.assertIn("192.168.8.0/24", got, "the 4G router's LAN")
        self.assertIn("192.168.91.0/24", got, "the camera LAN")
        self.assertIn("192.168.92.0/24", got, "the board's access point")

    def test_the_network_behind_the_tunnel_is_not_free(self):
        """The one that would quietly hand a remote viewer the full stream."""
        self.assertNotIn("192.168.90.0/24", self.nets(CLUB))

    def test_the_tunnel_subnet_itself_is_not_free(self):
        self.assertNotIn("10.8.2.0/24", self.nets(CLUB))

    def test_the_default_route_is_not_a_network(self):
        self.assertNotIn("0.0.0.0/0", self.nets(CLUB))

    def test_a_host_route_is_not_a_network(self):
        self.assertNotIn("10.8.2.5/32", self.nets(CLUB))

    def test_a_route_via_a_gateway_is_not_on_link(self):
        text = HEADER + "eth0\t0000A8C0\t0108A8C0\t0003\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        self.assertEqual(self.nets(text), [])

    def test_a_missing_route_file_assumes_nothing_is_free(self):
        self.assertEqual(netinfo.attached_networks("/nonexistent/route"), [])

    def test_a_garbled_line_does_not_lose_the_good_ones(self):
        text = CLUB + "junk\n\x00\t\t\n"
        self.assertIn("192.168.8.0/24", self.nets(text))

    def test_addresses_are_decoded_little_endian(self):
        # 0008A8C0 -> C0 A8 08 00 -> 192.168.8.0, not 0.8.168.192
        self.assertEqual(str(netinfo._addr("0008A8C0")), "192.168.8.0")


class TestCache(Base):
    def test_it_rereads_after_the_ttl(self):
        path = self.route(CLUB)
        t = [1000.0]
        a = netinfo.AttachedNetworks(ttl_s=60.0, proc_route=path,
                                     clock=lambda: t[0])
        self.assertIn(ipaddress.ip_network("192.168.8.0/24"), a.get())
        with open(path, "w") as fh:
            fh.write(HEADER)
        self.assertIn(ipaddress.ip_network("192.168.8.0/24"), a.get(),
                      "still cached")
        t[0] += 61.0
        self.assertEqual(a.get(), [], "the ttl expired; it must re-read")
