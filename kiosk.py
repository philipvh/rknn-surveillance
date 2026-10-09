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

"""Brighten the wall panel when the camera turns to look at somebody.

Two problems with one answer. The tablet draws more than its charger supplies
and dies after a day or two -- measured: 28 hours, then 41 hours, then silence
until it had charged enough to boot itself. And a panel that is always lit is
a worse deterrent than one that lights up the moment the camera moves, because
the second one is obviously *about you*.

So the screen idles dim and goes to full brightness on a trigger. Rung zero of
the deterrence ladder: the wall says "you are on camera" before the camera has
finished turning, and a panel that brightens as you approach reads as a
response rather than as scenery.

Dimming rather than blanking, which was the first attempt: a blanked Android
screen suspends the page's timers, so the panel stops polling and looks dead
from the board's side -- and a dim panel is still a panel somebody can read.
Fully's own screen-off timer (timeToScreenOffV2) has to be 0 or it blanks the
screen underneath all this; `idle_mode: off` is kept for a site that would
rather have the screen genuinely dark.

It talks to Fully Kiosk's remote administration API on the tablet itself
(http://<tablet>:2323/?cmd=screenOn&password=...), which has to be enabled on
the tablet with a password. That password is a secret, so it lives in
secrets.yaml and never appears in status output.

Nothing here can fail into the controller. A tablet that is asleep, flat,
unplugged or off the wifi must not delay an incident by so much as the connect
timeout, so every call is fired on its own thread and every exception is
swallowed with a log line.
"""

import datetime as dt
import json
import logging
import os
import pathlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("kiosk")

DEFAULT_PORT = 2323
TIMEOUT_S = 4


class Kiosk:
    """Turns the wall panel's screen on for a while, then lets it sleep."""

    def __init__(self, cfg=None, opener=None, clock=time.monotonic):
        c = dict(cfg or {})
        self.enabled = bool(c.get("enabled", False))
        self.host = str(c.get("host") or "").strip()
        self.port = int(c.get("port", DEFAULT_PORT))
        self.password = str(c.get("password") or "")
        # How long the screen stays on after the last trigger. Short enough to
        # matter for the battery, long enough that somebody walking past reads
        # it and thinks better of whatever they were about to do.
        self.wake_s = float(c.get("wake_seconds", 120))
        # dim: idle at a low brightness, full brightness on a trigger.
        # off:  idle with the screen blanked (the old behaviour).
        self.idle_mode = str(c.get("idle_mode", "dim")).strip().lower()
        if self.idle_mode not in ("dim", "off"):
            log.warning("kiosk.idle_mode %r is not 'dim' or 'off'; using dim",
                        self.idle_mode)
            self.idle_mode = "dim"
        # Fully takes 0-255. 0 is not off, it is the dimmest the panel goes.
        self.dim_level = max(0, min(255, int(c.get("dim_level", 6))))
        self.bright_level = max(0, min(255, int(c.get("bright_level", 255))))
        # Do not re-issue screenOn for every frame of an incident.
        self.min_interval_s = float(c.get("min_interval_s", 20))
        self.timeout_s = float(c.get("timeout_s", TIMEOUT_S))
        self.capacity_mah = int(c.get("battery_mah", 7900))
        self._opener = opener or urllib.request.urlopen
        self._clock = clock
        self._lock = threading.Lock()
        self._awake_until = 0.0
        self._last_cmd_at = None
        self.woken = 0
        self.failed = 0
        self.last_error = ""
        self.log = None                 # set by from_config when a path is given
        self.last_reading = None

    @property
    def configured(self):
        return bool(self.enabled and self.host and self.password)

    def status(self):
        with self._lock:
            return {"enabled": self.enabled, "configured": self.configured,
                    "host": self.host,          # not a secret; the password is
                    "idle_mode": self.idle_mode,
                    "bright": self._awake_until > self._clock(),
                    "woken": self.woken, "failed": self.failed,
                    "last_error": self.last_error,
                    "reading": self.last_reading}

    # ----------------------------------------------------------------- wire
    def _url(self, cmd, **extra):
        q = {"cmd": cmd, "password": self.password}
        q.update(extra)
        return "http://%s:%d/?%s" % (self.host, self.port,
                                     urllib.parse.urlencode(q))

    def _brightness(self, level):
        return ("setStringSetting",
                {"key": "screenBrightness", "value": str(int(level))})

    def _send(self, cmd, **extra):
        try:
            with self._opener(self._url(cmd, **extra), timeout=self.timeout_s) as r:
                getattr(r, "read", lambda: b"")()
            with self._lock:
                self.last_error = ""
            return True
        except (urllib.error.URLError, OSError, ValueError) as e:
            with self._lock:
                self.failed += 1
                # A wall tablet that is flat or asleep is the normal case this
                # exists to work around, so this is info, not alarm.
                self.last_error = str(e)
            log.info("could not reach the wall panel (%s): %s", cmd, e)
            return False
        except Exception as e:                      # never reach the caller
            with self._lock:
                self.failed += 1
                self.last_error = repr(e)
            log.exception("wall panel command failed unexpectedly")
            return False

    def _in_background(self, cmd, **extra):
        threading.Thread(target=self._send, args=(cmd,), kwargs=extra,
                         daemon=True, name="kiosk-%s" % cmd).start()

    def _apply(self, bright):
        """One thread, both commands, in order.

        screenOn first: brightness means nothing to a blanked panel, and the
        screen may have been turned off by something other than us.
        """
        def run():
            if bright:
                self._send("screenOn")
            cmd, extra = self._brightness(
                self.bright_level if bright else self.dim_level)
            self._send(cmd, **extra)
            if not bright and self.idle_mode == "off":
                self._send("screenOff")
        threading.Thread(target=run, daemon=True,
                         name="kiosk-%s" % ("bright" if bright else "dim")).start()

    # ---------------------------------------------------------------- read
    def poll(self):
        """Ask the tablet how it is. Returns a row, reachable or not.

        Synchronous on purpose -- this runs on the sampler's own thread, not
        the controller's, and a reading that silently never arrives is worse
        than one that takes four seconds.
        """
        if not self.configured:
            return None
        row = {"ok": False}
        try:
            with self._opener(self._url("deviceInfo", type="json"),
                              timeout=self.timeout_s) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            row = {"ok": True, "battery": d.get("batteryLevel"),
                   "plugged": bool(d.get("isPlugged")),
                   "screen": bool(d.get("screenOn")),
                   "temp": d.get("batteryTemperature")}
        except Exception as e:
            # Unreachable is a reading, not an error: it is what distinguishes
            # a flat tablet from one that dropped the wifi.
            row["why"] = type(e).__name__
        # What the board COMMANDED, not what Fully reports. In dim mode the
        # screen stays on and only its brightness changes, so screenOn cannot
        # tell bright from dim -- bucketing on it produced two hours of
        # readings labelled "bright" that were simply all of them, and a dim
        # bucket that could never populate.
        row["bright"] = bool(self._awake_until > self._clock())
        self.last_reading = dict(row, t=dt.datetime.now().isoformat(
            timespec="seconds"))
        if self.log is not None:
            self.log.record(row)
        return self.last_reading

    # ---------------------------------------------------------------- calls
    def wake(self, why=""):
        """Turn the screen on. Cheap to call repeatedly; it rate-limits."""
        if not self.configured:
            return False
        now = self._clock()
        with self._lock:
            if (self._last_cmd_at is not None
                    and now - self._last_cmd_at < self.min_interval_s):
                # Already on and recently told so; just extend the window.
                self._awake_until = now + self.wake_s
                return False
            self._last_cmd_at = now
            self._awake_until = now + self.wake_s
            self.woken += 1
        log.info("brightening the wall panel%s", (" (%s)" % why) if why else "")
        self._apply(True)
        return True

    def tick(self):
        """Let the screen sleep again once the wake window has passed."""
        if not self.configured:
            return False
        with self._lock:
            if not self._awake_until or self._awake_until > self._clock():
                return False
            self._awake_until = 0.0
        log.info("letting the wall panel go %s",
                 "dim" if self.idle_mode == "dim" else "dark")
        self._apply(False)
        return True


class KioskLog:
    """A reading every few minutes, kept per day.

    The one subsystem here whose failure is a slow drift over days was the one
    nothing recorded, so every answer about it came from querying by hand
    after the fact. Tuesday's outage took three days and a chain of inference
    to explain; with this it would have been a slope on a page.

    A failed read is written too, as ok=false. That is the row that tells
    "powered off" apart from "fell off the wifi", which was exactly the
    distinction unavailable when it mattered.
    """

    KEEP_DAYS = 120

    def __init__(self, root):
        self.root = pathlib.Path(root)
        self._lock = threading.Lock()

    def _path(self, when):
        return self.root / ("%s.jsonl" % when.strftime("%Y-%m-%d"))

    def record(self, row, when=None):
        when = when or dt.datetime.now()
        row = dict(row)
        row.setdefault("t", when.isoformat(timespec="seconds"))
        try:
            with self._lock:
                self.root.mkdir(parents=True, exist_ok=True)
                with open(self._path(when), "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, sort_keys=True) + "\n")
            self._prune()
        except OSError as e:
            log.warning("could not write the kiosk log: %s", e)
        return row

    def _prune(self):
        try:
            days = sorted(f for f in self.root.glob("*.jsonl"))
        except OSError:
            return
        for f in days[:-self.KEEP_DAYS] if len(days) > self.KEEP_DAYS else []:
            try:
                f.unlink()
            except OSError:
                pass

    def read(self, since=None):
        """Rows, oldest first. `since` is a date."""
        out = []
        try:
            files = sorted(self.root.glob("*.jsonl"))
        except OSError:
            return out
        for f in files:
            if since and f.stem < since.isoformat():
                continue
            try:
                for line in f.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        try:
                            out.append(json.loads(line))
                        except ValueError:
                            pass
            except OSError:
                pass
        return out


def rates(rows, capacity_mah=7900):
    """Net milliamps while the screen was bright, and while it was dim.

    Pairs consecutive readings and attributes each interval to the screen
    state it was taken under, so a day that mixes a tournament with a quiet
    night yields both numbers rather than one average that describes neither.

    Gaps longer than an hour are dropped: the tablet was off or unreachable,
    and the level either side of that says nothing about a rate.
    """
    buckets = {True: [0.0, 0.0], False: [0.0, 0.0]}     # [pct, hours]
    prev = None
    for r in rows:
        if not r.get("ok", True) or r.get("battery") is None:
            prev = None                                  # a gap, not a step
            continue
        try:
            t = dt.datetime.fromisoformat(r["t"])
        except (KeyError, ValueError):
            continue
        # "bright" is what the board commanded. Older rows only have
        # "screen", which in dim mode is always True; fall back to it so a
        # log written before this existed still parses, and accept that those
        # rows cannot separate the two regimes.
        lit = r.get("bright")
        if lit is None:
            lit = bool(r.get("screen"))
        cur = (t, r["battery"], bool(lit))
        if prev is not None:
            hours = (cur[0] - prev[0]).total_seconds() / 3600.0
            # Attribute the interval to the state it STARTED in; a change
            # part-way is noise at five-minute sampling.
            if 0 < hours <= 1.0:
                b = buckets[prev[2]]
                b[0] += cur[1] - prev[1]
                b[1] += hours
        prev = cur
    out = {}
    for state, name in ((True, "bright"), (False, "dim")):
        pct, hours = buckets[state]
        out[name] = {
            "percent": round(pct, 1), "hours": round(hours, 2),
            "ma": round(pct / 100.0 * capacity_mah / hours) if hours > 0 else None,
        }
    return out


def from_config(cfg, state_root=None):
    try:
        c = dict(cfg._get("kiosk", default={}) or {})
    except AttributeError:
        c = dict(cfg or {})
    k = Kiosk(c)
    root = state_root
    if root is None and hasattr(cfg, "resolve"):
        root = cfg.resolve(c.get("state", "state/kiosk"))
    if root is not None:
        k.log = KioskLog(root)
    return k
