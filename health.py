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

"""Staying alive unattended, and noticing when we have not.

Three separate concerns that all end up here:

  * The clock. With no internet there is no NTP, so nothing corrects a wrong
    clock. Every segment filename and every clip window in this system is wall
    time, so a Rock 5B that boots in 1970 writes footage nobody can find and
    timestamps no insurer would accept. A dead RTC battery is silent unless
    something looks for it, so this looks for it.

  * Liveness. systemd can restart a wedged service, but only if the service
    stops claiming to be well. The watchdog ping here is deliberately
    conditional: if frames have stopped arriving, we stop pinging, and systemd
    restarts the process. A watchdog that pings unconditionally is a watchdog
    that never fires.

  * Aggregation. One place that knows whether the whole thing is working, for
    the panel, the heartbeat and the doctor.
"""

import datetime as dt
import json
import logging
import os
import shutil
import socket
import time
from pathlib import Path

log = logging.getLogger("health")

# Nothing in this project existed before this, so a clock reading earlier than
# this is definitionally wrong rather than merely surprising.
EPOCH_SANITY = dt.datetime(2025, 1, 1)


class ClockGuard:
    """Detects a clock that went backwards or never got set.

    Writes the current time to disk periodically. On start, a clock earlier
    than the last recorded time means the RTC did not hold -- the one failure
    that makes every other timestamp in the system untrustworthy.
    """

    def __init__(self, path, write_interval_s=60.0, now=None):
        self.path = Path(path)
        self.write_interval_s = float(write_interval_s)
        self._now = now or dt.datetime.now
        self._last_write = 0.0
        self.last_known = self._read()
        self.regressed_by_s = 0.0
        self.implausible = False
        self._check_on_start()

    def _read(self):
        try:
            return dt.datetime.fromisoformat(
                json.loads(self.path.read_text())["last_seen"])
        except (OSError, ValueError, KeyError):
            return None

    def _write(self, when):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last_seen": when.isoformat(
                timespec="seconds")}))
            tmp.replace(self.path)
        except OSError as e:
            log.warning("could not record the time: %s", e)

    def _check_on_start(self):
        now = self._now()
        if now < EPOCH_SANITY:
            self.implausible = True
            log.error(
                "THE CLOCK IS WRONG: it reads %s. With no NTP nothing will "
                "correct this. Recordings will be filed under a nonsense date "
                "and clips will not be findable. Fit the RTC battery, then "
                "set the time: sudo date -s '...' && sudo hwclock -w",
                now.isoformat(timespec="seconds"))
        if self.last_known and now < self.last_known:
            self.regressed_by_s = (self.last_known - now).total_seconds()
            log.error(
                "THE CLOCK WENT BACKWARDS by %.0f hours (was %s, now %s). "
                "The RTC battery is probably dead. Footage recorded from here "
                "will overwrite or interleave with older files.",
                self.regressed_by_s / 3600,
                self.last_known.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"))

    def tick(self):
        """Call regularly; cheap, and only writes once a minute."""
        mono = time.monotonic()
        if mono - self._last_write < self.write_interval_s:
            return
        self._last_write = mono
        now = self._now()
        if self.last_known is None or now > self.last_known:
            self.last_known = now
            self._write(now)

    @property
    def ok(self):
        return not self.implausible and self.regressed_by_s == 0

    def status(self):
        return {
            "clock_ok": self.ok,
            "clock_implausible": self.implausible,
            "clock_regressed_s": round(self.regressed_by_s),
            "last_known": self.last_known.isoformat(timespec="seconds")
            if self.last_known else None,
        }


class SystemdNotifier:
    """sd_notify without the systemd python bindings -- it is a datagram.

    Silently inert when not run under systemd, so nothing here changes how the
    service behaves from a shell.
    """

    def __init__(self, env=None):
        env = env if env is not None else os.environ
        addr = env.get("NOTIFY_SOCKET")
        self.enabled = bool(addr)
        self.sock = None
        self.addr = None
        self.watchdog_interval_s = None
        if not self.enabled:
            return
        self.addr = "\0" + addr[1:] if addr.startswith("@") else addr
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        except OSError as e:
            log.warning("could not open the systemd notify socket: %s", e)
            self.enabled = False
            return
        usec = env.get("WATCHDOG_USEC")
        if usec:
            try:
                # Ping at half the interval, as systemd's own documentation
                # recommends, so one slow iteration does not trip a restart.
                self.watchdog_interval_s = int(usec) / 1e6 / 2
            except ValueError:
                pass

    def _send(self, message):
        if not self.enabled or not self.sock:
            return False
        try:
            self.sock.sendto(message.encode("utf-8"), self.addr)
            return True
        except OSError as e:
            log.debug("notify failed: %s", e)
            return False

    def ready(self):
        return self._send("READY=1")

    def watchdog(self):
        return self._send("WATCHDOG=1")

    def status(self, text):
        return self._send(f"STATUS={text[:200]}")

    def stopping(self):
        return self._send("STOPPING=1")


class HealthMonitor:
    """Knows whether the whole thing is working."""

    def __init__(self, cfg, recorders=(), clock_guard=None,
                 notifier=None, clock=time.monotonic):
        c = (cfg._get("health", default={}) or {}) if hasattr(cfg, "_get") else (cfg or {})
        self.cfg = cfg
        self.recorders = list(recorders)
        self.clock_guard = clock_guard
        self.notifier = notifier or SystemdNotifier()
        self._clock = clock
        self.stale_frames_s = float(c.get("stale_frames_s", 120))
        self.disk_warn_percent = float(c.get("disk_warn_percent", 90))
        self.last_frame_at = None
        self.started_at = clock()
        self._last_ping = 0.0
        self._degraded_since = None

    def note_frame(self):
        self.last_frame_at = self._clock()

    def frames_fresh(self):
        if self.last_frame_at is None:
            # Allow one stale window after start before declaring failure.
            return (self._clock() - self.started_at) < self.stale_frames_s
        return (self._clock() - self.last_frame_at) < self.stale_frames_s

    def disk(self, path=None):
        try:
            u = shutil.disk_usage(str(path or Path(".")))
            pct = u.used / u.total * 100
            return {"disk_percent": round(pct), "disk_free_gb": round(u.free / 1e9, 1),
                    "disk_low": pct >= self.disk_warn_percent}
        except OSError:
            return {"disk_percent": 0, "disk_free_gb": 0, "disk_low": False}

    def recorders_ok(self):
        return all(r.healthy for r in self.recorders) if self.recorders else True

    def alive(self):
        """The condition under which we tell systemd we are well.

        Frames are the one thing worth restarting for: a wedged capture is
        invisible from outside and cannot recover on its own, whereas a full
        disk or a bad clock will not be fixed by a restart and should be
        reported instead of looped over.
        """
        return self.frames_fresh()

    def tick(self, disk_path=None):
        if self.clock_guard:
            self.clock_guard.tick()

        alive = self.alive()
        if alive:
            self._degraded_since = None
        elif self._degraded_since is None:
            self._degraded_since = self._clock()
            log.error("no frames for %.0fs -- withholding the watchdog ping so "
                      "systemd restarts us", self.stale_frames_s)

        interval = self.notifier.watchdog_interval_s
        if interval and alive:
            now = self._clock()
            if now - self._last_ping >= interval:
                self._last_ping = now
                self.notifier.watchdog()
        return alive

    def status(self, disk_path=None):
        out = {
            "frames_fresh": self.frames_fresh(),
            "recorders_ok": self.recorders_ok(),
            "uptime_s": round(self._clock() - self.started_at),
            "watchdog": bool(self.notifier.watchdog_interval_s),
        }
        out.update(self.disk(disk_path))
        if self.clock_guard:
            out.update(self.clock_guard.status())
        out["healthy"] = (out["frames_fresh"] and out["recorders_ok"]
                          and not out.get("disk_low")
                          and out.get("clock_ok", True))
        return out
