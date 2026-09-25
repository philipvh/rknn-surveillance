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

"""How much of the mobile bundle has gone, and on what day.

A board on a 4G bundle can spend a month's data in an afternoon of somebody
watching the live view -- that is not hypothetical, it is what happened at the
club. This keeps a running total so the number is visible on the panel before
it arrives on the bill.

Deliberately no vnstat: it would mean a package and a daemon installed as root
on a board whose whole point is to be reproducible from this repo. The kernel
already counts every byte through an interface in /sys/class/net, which any
user can read, so this samples that and keeps its own daily buckets.

The awkward part is that those counters reset -- on reboot, and when an
interface is brought down and up. A counter that went backwards means a reset,
and the new value is then the usage since it happened rather than a huge
negative delta. Getting that wrong in the other direction would silently
under-count, which is the failure that matters here: a meter that reads low is
worse than no meter, because it is believed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tempfile
import threading
import time

log = logging.getLogger("usage")

SYS_NET = "/sys/class/net"
PROC_ROUTE = "/proc/net/route"


def default_interface(proc_route=PROC_ROUTE):
    """The interface carrying the default route -- the one that costs money.

    Looked up rather than configured because the board's uplink has already
    changed once (wired at the bench, a 4G router at the club) and a hard-coded
    name would have quietly measured nothing.
    """
    try:
        with open(proc_route, "r", encoding="utf-8") as fh:
            next(fh, None)                     # header
            for line in fh:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "00000000":
                    return parts[0]
    except (OSError, StopIteration):
        pass
    return None


def _counter(iface, sys_net=SYS_NET):
    """Total bytes in and out since the interface last came up."""
    total = 0
    for direction in ("rx_bytes", "tx_bytes"):
        try:
            with open(os.path.join(sys_net, iface, "statistics", direction),
                      "r", encoding="utf-8") as fh:
                total += int(fh.read().strip() or 0)
        except (OSError, ValueError):
            return None
    return total


class WanCounter:
    """Bytes across the uplink, ignoring what never leaves the site.

    /sys/class/net/<if>/statistics counts every frame on the NIC, which was the
    right measure while the wall panel hung off the board's own access point:
    its video never touched the uplink. The USB adapter cannot carry video, so
    the panel moved onto the 4G router's LAN, and now every frame it pulls
    crosses the uplink NIC on its way to a tablet ten metres away -- about
    0.8 GB an hour of "mobile data" that never went near the mobile network.

    wan_meter.sh puts iptables counters on the uplink that skip every
    directly-attached subnet. Reading them needs root, and this service runs
    with NoNewPrivileges, so it cannot sudo and should not be given the chance:
    a systemd timer publishes the two numbers to a file as root, and all that
    happens here is a read.

    A file that is missing or has gone stale reports None and the caller falls
    back to the interface counter. That direction is deliberate -- over-
    reporting is a bad measurement, but a meter frozen at the moment the timer
    died would quietly report that nothing is being used at all.
    """

    PUBLISHED = "/run/rknn-meter"
    STALE_S = 300.0

    def __init__(self, path=None, stale_s=None, clock=time.time):
        self.path = path or self.PUBLISHED
        self.stale_s = self.STALE_S if stale_s is None else float(stale_s)
        self._clock = clock
        self._warned = None

    def read(self):
        """(in + out) bytes counted on the uplink, or None if unavailable."""
        try:
            st = os.stat(self.path)
            with open(self.path, "r", encoding="utf-8") as fh:
                parts = fh.read().split()
        except (OSError, ValueError):
            return self._unavailable(
                "%s is not there -- run: sudo bash wan_meter.sh install"
                % self.path)
        age = self._clock() - st.st_mtime
        if age > self.stale_s:
            return self._unavailable(
                "%s has not been updated for %.0fs; is rknn-meter.timer "
                "running?" % (self.path, age))
        if len(parts) < 2:
            return self._unavailable("%s holds %r" % (self.path, parts))
        try:
            total = sum(int(x) for x in parts[:2])
        except ValueError:
            return self._unavailable("%s holds %r" % (self.path, parts))
        if self._warned is not None:
            log.info("the uplink meter is readable again; counting only "
                     "traffic that crosses the mobile link")
            self._warned = None
        return total

    def _unavailable(self, why):
        # One line per distinct cause, not one per sample.
        if self._warned != why:
            self._warned = why
            log.warning("counting every byte on the uplink instead of only "
                        "the ones that cost money, so traffic to the wall "
                        "panel is billed as mobile data: %s", why)
        return None


class DataUsage:
    """Daily byte totals for the metered uplink, kept across restarts."""

    KEEP_DAYS = 120

    def __init__(self, path, iface=None, limit_gb=0.0, billing_day=1,
                 sys_net=SYS_NET, clock=None, today=None, wan=None):
        self.path = path
        self._iface = iface
        self.sys_net = sys_net
        # Prefer the counter that knows the difference between a byte that
        # crossed the mobile link and one that went to the tablet on the wall.
        self.wan = WanCounter() if wan is None else wan
        self._source = None
        self.limit_gb = float(limit_gb or 0.0)
        self.billing_day = max(1, min(int(billing_day or 1), 28))
        self._today = today or (lambda: dt.date.today())
        self._lock = threading.RLock()
        self._days = {}
        self._last_raw = None
        self._last_iface = None
        self._load()

    # ---------------------------------------------------------------- state
    @property
    def iface(self):
        return self._iface or default_interface()

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("could not read %s (%s); starting the meter fresh",
                        self.path, e)
            return
        if not isinstance(raw, dict):
            return
        days = raw.get("days")
        if isinstance(days, dict):
            for k, v in days.items():
                try:
                    self._days[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue
        self._last_raw = raw.get("last_raw")
        self._last_iface = raw.get("iface")

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                   prefix=".usage-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"days": self._days, "last_raw": self._last_raw,
                           "iface": self._last_iface}, fh,
                          indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, str(self.path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # --------------------------------------------------------------- sample
    def sample(self):
        """Fold the interface counter into today's bucket. Returns bytes added."""
        iface = self.iface
        if not iface:
            return 0
        raw, source = None, "wan"
        if self.wan:
            raw = self.wan.read()
        if raw is None:
            raw, source = _counter(iface, self.sys_net), "iface"
        if raw is None:
            return 0
        if source != self._source:
            if self._source is not None:
                log.info("the uplink counter changed source (%s -> %s); "
                         "not counting its history as though it just happened",
                         self._source, source)
            self._source = source
        with self._lock:
            prev, prev_iface = self._last_raw, self._last_iface
            # The source is part of the identity: the two counters measure
            # different things, so carrying a delta across a switch between
            # them would invent traffic.
            prev_key = "%s/%s" % (iface, source)
            if prev is None or prev_iface != prev_key:
                # First ever sample, or the uplink changed. Do not count the
                # counter's whole history as though it happened just now.
                delta = 0
            elif raw < prev:
                # The counter went backwards: a reboot, or the link was cycled.
                # Everything it shows now has been used since that happened.
                delta = raw
                log.info("uplink counter reset; counting %d bytes since then",
                         raw)
            else:
                delta = raw - prev
            self._last_raw = raw
            self._last_iface = prev_key
            if delta:
                key = self._today().isoformat()
                self._days[key] = self._days.get(key, 0) + delta
                self._prune()
            self._save()
            return delta

    def _prune(self):
        if len(self._days) <= self.KEEP_DAYS:
            return
        for k in sorted(self._days)[:-self.KEEP_DAYS]:
            del self._days[k]

    # --------------------------------------------------------------- report
    def period_start(self, today=None):
        """First day of the bundle's current month."""
        today = today or self._today()
        day = min(self.billing_day, 28)
        if today.day >= day:
            return today.replace(day=day)
        first = today.replace(day=1)
        prev = first - dt.timedelta(days=1)
        return prev.replace(day=day)

    def bytes_since(self, start):
        with self._lock:
            return sum(v for k, v in self._days.items()
                       if k >= start.isoformat())

    def today_bytes(self):
        with self._lock:
            return self._days.get(self._today().isoformat(), 0)

    def report(self):
        """What the panel shows."""
        start = self.period_start()
        used = self.bytes_since(start)
        gb = used / 1073741824.0
        pct = (gb / self.limit_gb * 100.0) if self.limit_gb else 0.0
        return {
            "iface": self.iface or "",
            "period_start": start.isoformat(),
            "used_gb": round(gb, 2),
            "today_gb": round(self.today_bytes() / 1073741824.0, 2),
            "limit_gb": self.limit_gb,
            "percent": round(pct, 1) if self.limit_gb else None,
            # 80% is where there is still time to change behaviour; at 100 the
            # only useful message is that it has already happened.
            "warn": bool(self.limit_gb and pct >= 80.0),
            "over": bool(self.limit_gb and pct >= 100.0),
            "days": self.recent(14),
        }

    def recent(self, n=14):
        with self._lock:
            keys = sorted(self._days)[-n:]
            return [{"day": k, "gb": round(self._days[k] / 1073741824.0, 2)}
                    for k in keys]
