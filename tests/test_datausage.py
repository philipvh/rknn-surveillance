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

"""The mobile-data meter.

The failure that matters is under-counting: a meter that reads low is worse
than no meter, because it is believed and the bill arrives anyway. Most of
these tests are about the ways an interface counter lies -- it resets on
reboot, it resets when the link is cycled, and it starts non-zero when the
service restarts.
"""
import datetime as dt
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import datausage  # noqa: E402
from datausage import DataUsage, default_interface  # noqa: E402

GIB = 1073741824


class FakeNet:
    """A /sys/class/net tree we can move."""

    def __init__(self, root, iface="wan0"):
        self.root = Path(root)
        self.iface = iface
        self.stats = self.root / iface / "statistics"
        self.stats.mkdir(parents=True)
        self.set(0, 0)

    def set(self, rx, tx):
        (self.stats / "rx_bytes").write_text(str(rx))
        (self.stats / "tx_bytes").write_text(str(tx))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.net = FakeNet(self.dir / "net")
        self.day = dt.date(2026, 8, 23)

    def meter(self, **kw):
        kw.setdefault("iface", "wan0")
        kw.setdefault("sys_net", str(self.dir / "net"))
        kw.setdefault("today", lambda: self.day)
        return DataUsage(self.dir / "usage.json", **kw)


class TestCounting(Base):
    def test_the_first_sample_does_not_count_history(self):
        # The counter is already large when the service starts; that traffic
        # happened before we were watching and is not ours to claim.
        self.net.set(3 * GIB, 1 * GIB)
        m = self.meter()
        self.assertEqual(m.sample(), 0)
        self.assertEqual(m.today_bytes(), 0)

    def test_it_counts_the_difference(self):
        m = self.meter()
        m.sample()
        self.net.set(100, 50)
        self.assertEqual(m.sample(), 150)
        self.net.set(200, 50)
        self.assertEqual(m.sample(), 100)
        self.assertEqual(m.today_bytes(), 250)

    def test_rx_and_tx_are_both_counted(self):
        # The bundle pays for both directions.
        m = self.meter()
        m.sample()
        self.net.set(10, 90)
        self.assertEqual(m.sample(), 100)


class TestCountersThatLie(Base):
    def test_a_reboot_resets_the_counter_and_is_not_counted_as_negative(self):
        m = self.meter()
        m.sample()
        self.net.set(5 * GIB, 0)
        m.sample()
        self.assertEqual(m.today_bytes(), 5 * GIB)
        # reboot: counter starts again, small
        self.net.set(1000, 0)
        delta = m.sample()
        self.assertEqual(delta, 1000, "post-reset traffic was not counted")
        self.assertEqual(m.today_bytes(), 5 * GIB + 1000)

    def test_a_reset_never_subtracts(self):
        m = self.meter()
        m.sample()
        self.net.set(10 * GIB, 0)
        m.sample()
        before = m.today_bytes()
        self.net.set(1, 0)
        m.sample()
        self.assertGreaterEqual(m.today_bytes(), before,
                                "a counter reset went backwards in the total")

    def test_switching_uplink_does_not_import_the_new_interfaces_history(self):
        # Bench ethernet -> 4G router: the new interface's counter has nothing
        # to do with what we have used.
        m = self.meter()
        m.sample()
        self.net.set(500, 0)
        m.sample()
        other = FakeNet(self.dir / "net", iface="wan1")
        other.set(9 * GIB, 9 * GIB)
        m._iface = "wan1"
        self.assertEqual(m.sample(), 0)
        self.assertEqual(m.today_bytes(), 500)


class TestPersistence(Base):
    def test_totals_survive_a_restart(self):
        m = self.meter()
        m.sample()
        self.net.set(1000, 0)
        m.sample()
        again = self.meter()
        self.assertEqual(again.today_bytes(), 1000)

    def test_counting_continues_across_a_restart_without_a_gap(self):
        m = self.meter()
        m.sample()
        self.net.set(1000, 0)
        m.sample()
        again = self.meter()          # same boot, counter keeps climbing
        self.net.set(1500, 0)
        self.assertEqual(again.sample(), 500)
        self.assertEqual(again.today_bytes(), 1500)

    def test_a_corrupt_file_does_not_take_the_service_down(self):
        (self.dir / "usage.json").write_text("{ not json")
        m = self.meter()
        self.assertEqual(m.today_bytes(), 0)


class TestReporting(Base):
    def test_percent_and_warning_at_eighty(self):
        m = self.meter(limit_gb=5.0, billing_day=1)
        m.sample()
        self.net.set(4 * GIB, 0)
        m.sample()
        r = m.report()
        self.assertEqual(r["used_gb"], 4.0)
        self.assertEqual(r["percent"], 80.0)
        self.assertTrue(r["warn"])
        self.assertFalse(r["over"])

    def test_over_the_bundle(self):
        m = self.meter(limit_gb=5.0, billing_day=1)
        m.sample()
        self.net.set(6 * GIB, 0)
        m.sample()
        self.assertTrue(m.report()["over"])

    def test_no_limit_means_no_percentage_rather_than_a_wrong_one(self):
        m = self.meter(limit_gb=0)
        m.sample()
        r = m.report()
        self.assertIsNone(r["percent"])
        self.assertFalse(r["warn"])

    def test_the_period_starts_on_the_billing_day(self):
        m = self.meter(limit_gb=5.0, billing_day=10)
        self.assertEqual(m.period_start(), dt.date(2026, 8, 10))

    def test_before_the_billing_day_the_period_started_last_month(self):
        self.day = dt.date(2026, 8, 3)
        m = self.meter(limit_gb=5.0, billing_day=10)
        self.assertEqual(m.period_start(), dt.date(2026, 7, 10))

    def test_older_days_are_outside_the_period(self):
        m = self.meter(limit_gb=5.0, billing_day=10)
        m._days[dt.date(2026, 8, 5).isoformat()] = 3 * GIB   # last period
        m._days[dt.date(2026, 8, 20).isoformat()] = 1 * GIB  # this one
        self.assertEqual(m.report()["used_gb"], 1.0)


class TestInterfaceDiscovery(Base):
    def test_finds_the_default_route(self):
        route = self.dir / "route"
        route.write_text(
            "Iface\tDestination\tGateway\tFlags\n"
            "enP4p65s0\t0000A8C0\t00000000\t0001\n"
            "wlan0\t00000000\t0108A8C0\t0003\n")
        self.assertEqual(default_interface(str(route)), "wlan0")

    def test_missing_file_is_not_fatal(self):
        self.assertIsNone(default_interface(str(self.dir / "nope")))


if __name__ == "__main__":
    unittest.main()


def _published(tmp, text, age_s=0.0):
    """A /run/rknn-meter as the timer would leave it."""
    import os, time as _t
    f = Path(tmp) / "rknn-meter"
    f.write_text(text)
    if age_s:
        t = _t.time() - age_s
        os.utime(f, (t, t))
    return str(f)


class TestWanCounter(unittest.TestCase):
    """Only the bytes that crossed the mobile link should be billed.

    The wall panel lives on the 4G router's LAN because the USB wifi adapter
    cannot carry video, so its stream crosses the uplink NIC without ever
    leaving the building -- about 0.8 GB an hour of phantom mobile data if the
    interface counter is believed.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_it_adds_the_two_directions(self):
        w = datausage.WanCounter(_published(self.tmp.name, "1000 2345\n"))
        self.assertEqual(w.read(), 3345)

    def test_a_missing_file_reports_nothing_rather_than_guessing(self):
        w = datausage.WanCounter(str(Path(self.tmp.name) / "absent"))
        self.assertIsNone(w.read())

    def test_garbage_is_not_taken_as_a_number(self):
        w = datausage.WanCounter(_published(self.tmp.name, "banana\n"))
        self.assertIsNone(w.read())

    def test_a_stale_file_is_refused_rather_than_frozen(self):
        """If the timer dies the counter stops moving. Reporting that as
        'no data used' would be worse than over-reporting."""
        w = datausage.WanCounter(_published(self.tmp.name, "9 9\n", age_s=3600))
        self.assertIsNone(w.read())

    def test_a_fresh_file_just_under_the_limit_is_accepted(self):
        w = datausage.WanCounter(_published(self.tmp.name, "9 9\n", age_s=60),
                                 stale_s=300)
        self.assertEqual(w.read(), 18)

    def test_it_warns_once_per_cause_not_once_per_sample(self):
        w = datausage.WanCounter(str(Path(self.tmp.name) / "absent"))
        with self.assertLogs("usage", level="WARNING") as got:
            for _ in range(10):
                w.read()
        self.assertEqual(len(got.records), 1)

    def test_it_says_so_when_the_meter_comes_back(self):
        path = str(Path(self.tmp.name) / "later")
        w = datausage.WanCounter(path)
        w.read()                                  # missing: warns
        Path(path).write_text("4 4\n")
        with self.assertLogs("usage", level="INFO") as got:
            self.assertEqual(w.read(), 8)
        self.assertTrue(any("readable again" in r.getMessage()
                            for r in got.records))


class TestSamplerPrefersTheWanCounter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "usage.json"

    def _usage(self, wan):
        net = FakeNet(Path(self.tmp.name) / "net")
        net.set(10 ** 9, 10 ** 9)           # a big interface counter to ignore
        return datausage.DataUsage(self.path, iface="wan0", wan=wan,
                                   sys_net=str(Path(self.tmp.name) / "net"))

    def test_the_wan_counter_wins_when_it_is_available(self):
        path = _published(self.tmp.name, "100 100\n")
        u = self._usage(datausage.WanCounter(path))
        u.sample()                          # first sample only sets the base
        Path(path).write_text("300 300\n")
        self.assertEqual(u.sample(), 400,
                         "it billed the interface counter, not the uplink one")

    def test_it_falls_back_to_the_interface_when_the_meter_is_gone(self):
        wan = datausage.WanCounter(str(Path(self.tmp.name) / "absent"))
        u = self._usage(wan)
        self.assertEqual(u.sample(), 0)     # base
        self.assertEqual(u._source, "iface")

    def test_switching_source_does_not_invent_traffic(self):
        """The two counters measure different things; a delta across the
        switch would book the whole of one as if it had just been used."""
        path = _published(self.tmp.name, "5 5\n")
        u = self._usage(datausage.WanCounter(path))
        u.sample()                          # base, from the wan counter
        Path(path).unlink()                 # the timer stopped
        added = u.sample()                  # falls back to the interface
        self.assertEqual(added, 0,
                         "a billion interface bytes were booked as new usage")
