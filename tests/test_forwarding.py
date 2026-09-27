# Copyright 2026 Philip van Houtte, magicview.tv, the Netherlands
# SPDX-License-Identifier: Apache-2.0
"""Publishing a device's own web page on a board port.

The fence matters more than the feature. A screen that forwards whatever host
it is given is one careless entry away from being a way into something else,
so every entry is checked against the networks the board is really attached
to -- once, in Settings.parse_forwards, which the panel and the CLI both use.
"""

import ipaddress
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from settings import Settings  # noqa: E402

ATTACHED = [ipaddress.ip_network(n) for n in
            ("192.168.8.0/24", "192.168.91.0/24", "192.168.92.0/24")]

# The club board's on-link routes, so Settings.forwards -- which re-reads the
# kernel on purpose, to un-publish a target the site was rewired away from --
# validates against the same networks the tests use.
CLUB_ROUTE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask"
    "\t\tMTU\tWindow\tIRTT\n"
    "enP4p65s0\t00000000\t0108A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    "tun0\t005AA8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
    "enP4p65s0\t0008A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
    "enx0\t005BA8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
    "wlx0\t005CA8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0\n"
)


def use_club_routes(case):
    import netinfo
    d = tempfile.mkdtemp()
    path = Path(d) / "route"
    path.write_text(CLUB_ROUTE)
    real = netinfo.PROC_ROUTE
    netinfo.PROC_ROUTE = str(path)
    case.addCleanup(setattr, netinfo, "PROC_ROUTE", real)


def fwd(**kw):
    base = {"name": "Camera", "port": 8080, "host": "192.168.91.47",
            "target_port": 88, "expose": "tunnel", "enabled": True}
    base.update(kw)
    return base


class Base(unittest.TestCase):
    def setUp(self):
        use_club_routes(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Settings(Path(self.tmp.name) / "settings.json")

    def parse(self, *rows, **kw):
        kw.setdefault("attached", ATTACHED)
        return Settings.parse_forwards(list(rows), **kw)

    def refused(self, *rows, **kw):
        with self.assertRaises(ValueError) as cm:
            self.parse(*rows, **kw)
        return str(cm.exception)


class TestTheFence(Base):
    def test_a_device_on_an_attached_network_is_allowed(self):
        got = self.parse(fwd())
        self.assertEqual(got[0]["host"], "192.168.91.47")

    def test_a_host_off_every_attached_network_is_refused(self):
        why = self.refused(fwd(host="8.8.8.8"))
        self.assertIn("not on a network this board is attached to", why)

    def test_the_far_side_of_the_tunnel_is_refused(self):
        """OpenVPN pushes on-link routes for the networks behind it, but those
        are not attached, and publishing them is somebody else's LAN."""
        self.refused(fwd(host="192.168.90.6"))

    def test_the_board_itself_is_never_a_target(self):
        """Otherwise a forward reaches the panel from the board's own address,
        and the trusted-network check is only as good as the address it sees."""
        why = self.refused(fwd(host="127.0.0.1"))
        self.assertIn("the board itself is not a target", why)

    def test_the_panels_own_port_cannot_be_taken(self):
        why = self.refused(fwd(port=8081), reserved_ports=(8081,))
        self.assertIn("already the panel", why)

    def test_a_port_outside_the_range_is_refused(self):
        self.refused(fwd(port=22))
        self.refused(fwd(port=9999))

    def test_two_forwards_cannot_share_a_port(self):
        why = self.refused(fwd(), fwd(name="Router", host="192.168.8.1"))
        self.assertIn("both want port", why)

    def test_a_nonsense_address_is_refused(self):
        self.refused(fwd(host="the-camera"))

    def test_an_unnamed_forward_is_refused(self):
        self.refused(fwd(name="  "))

    def test_expose_must_be_one_of_two_things(self):
        self.refused(fwd(expose="everywhere"))


class TestStorage(Base):
    def test_saved_forwards_come_back(self):
        self.s.set_forwards([fwd()], attached=ATTACHED)
        self.assertEqual(len(self.s.forwards), 1)
        self.assertEqual(self.s.forwards[0]["port"], 8080)

    def test_a_refused_set_changes_nothing(self):
        self.s.set_forwards([fwd()], attached=ATTACHED)
        with self.assertRaises(ValueError):
            self.s.set_forwards([fwd(host="8.8.8.8")], attached=ATTACHED)
        self.assertEqual(len(self.s.forwards), 1, "a bad save clobbered a good one")

    def test_a_file_that_no_longer_validates_publishes_nothing(self):
        """The site gets rewired and a stored target is suddenly off-net.
        Publishing nothing beats publishing something unintended."""
        self.s.set_forwards([fwd()], attached=ATTACHED)
        raw = json.loads(self.s.path.read_text())
        raw["web_forwards"][0]["host"] = "203.0.113.9"
        self.s.path.write_text(json.dumps(raw))
        self.s.reload()
        self.assertEqual(self.s.forwards, [])


class TestForwarder(unittest.TestCase):
    """The reconciler: what is running should match what is configured."""

    def setUp(self):
        use_club_routes(self)
        import forwarder
        self.mod = forwarder
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Settings(Path(self.tmp.name) / "settings.json")
        self.f = forwarder.Forwarder(self.s, poll_s=0.01)
        self.f.bind_for = lambda fwd: "127.0.0.1"
        self.addCleanup(self.f.stop)

    def ports(self):
        return sorted(k[1] for k in self.f._running)

    def test_it_opens_a_listener_per_enabled_forward(self):
        self.s.set_forwards([fwd(), fwd(name="Router", port=8082,
                                        host="192.168.8.1", target_port=80)],
                            attached=ATTACHED)
        self.f.reconcile()
        self.assertEqual(self.ports(), [8080, 8082])

    def test_a_disabled_forward_is_not_listened_on(self):
        self.s.set_forwards([fwd(enabled=False)], attached=ATTACHED)
        self.f.reconcile()
        self.assertEqual(self.ports(), [])

    def test_removing_one_leaves_the_other_alone(self):
        """Adding the router must not interrupt somebody watching the camera."""
        self.s.set_forwards([fwd(), fwd(name="Router", port=8082,
                                        host="192.168.8.1", target_port=80)],
                            attached=ATTACHED)
        self.f.reconcile()
        kept = [k for k in self.f._running if k[1] == 8080][0]
        srv_before = self.f._running[kept][0]
        self.s.set_forwards([fwd()], attached=ATTACHED)
        self.f.reconcile()
        self.assertEqual(self.ports(), [8080])
        self.assertIs(self.f._running[kept][0], srv_before,
                      "the untouched forward was torn down and rebuilt")

    def test_a_tunnel_forward_waits_rather_than_binding_anywhere(self):
        self.f.bind_for = self.mod.Forwarder.bind_for.__get__(self.f)
        self.f.tun = "nosuchtun0"
        self.s.set_forwards([fwd()], attached=ATTACHED)
        self.f.reconcile()
        self.assertEqual(self.ports(), [],
                         "it bound somewhere the operator did not ask for")
