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

"""Wake the wall panel when the camera turns to look at somebody.

Two problems with one answer. The tablet draws more than its charger supplies
and dies after a day or two -- measured: 28 hours, then 41 hours, then silence
until it had charged enough to boot itself. And a panel that is always lit is
a worse deterrent than one that lights up the moment the camera moves, because
the second one is obviously *about you*.

So the screen stays dark, and the same trigger that starts the sweep and rings
the speaker also turns the screen on. Rung zero of the deterrence ladder: the
wall says "you are on camera" before the camera has finished turning.

It talks to Fully Kiosk's remote administration API on the tablet itself
(http://<tablet>:2323/?cmd=screenOn&password=...), which has to be enabled on
the tablet with a password. That password is a secret, so it lives in
secrets.yaml and never appears in status output.

Nothing here can fail into the controller. A tablet that is asleep, flat,
unplugged or off the wifi must not delay an incident by so much as the connect
timeout, so every call is fired on its own thread and every exception is
swallowed with a log line.
"""

import logging
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
        # Do not re-issue screenOn for every frame of an incident.
        self.min_interval_s = float(c.get("min_interval_s", 20))
        self.timeout_s = float(c.get("timeout_s", TIMEOUT_S))
        self._opener = opener or urllib.request.urlopen
        self._clock = clock
        self._lock = threading.Lock()
        self._awake_until = 0.0
        self._last_cmd_at = None
        self.woken = 0
        self.failed = 0
        self.last_error = ""

    @property
    def configured(self):
        return bool(self.enabled and self.host and self.password)

    def status(self):
        with self._lock:
            return {"enabled": self.enabled, "configured": self.configured,
                    "host": self.host,          # not a secret; the password is
                    "screen_on": self._awake_until > self._clock(),
                    "woken": self.woken, "failed": self.failed,
                    "last_error": self.last_error}

    # ----------------------------------------------------------------- wire
    def _url(self, cmd):
        q = urllib.parse.urlencode({"cmd": cmd, "password": self.password})
        return "http://%s:%d/?%s" % (self.host, self.port, q)

    def _send(self, cmd):
        try:
            with self._opener(self._url(cmd), timeout=self.timeout_s) as r:
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

    def _in_background(self, cmd):
        threading.Thread(target=self._send, args=(cmd,), daemon=True,
                         name="kiosk-%s" % cmd).start()

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
        log.info("waking the wall panel%s", (" (%s)" % why) if why else "")
        self._in_background("screenOn")
        return True

    def tick(self):
        """Let the screen sleep again once the wake window has passed."""
        if not self.configured:
            return False
        with self._lock:
            if not self._awake_until or self._awake_until > self._clock():
                return False
            self._awake_until = 0.0
        log.info("letting the wall panel sleep")
        self._in_background("screenOff")
        return True


def from_config(cfg):
    try:
        c = dict(cfg._get("kiosk", default={}) or {})
    except AttributeError:
        c = dict(cfg or {})
    return Kiosk(c)
