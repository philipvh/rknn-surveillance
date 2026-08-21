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

"""The spoken warning: rung three of the deterrence ladder.

A short recorded announcement removes any doubt that someone might be watching
in real time, which is remarkably effective against opportunistic vandalism.
It is also the only part of this system that acts on a bystander rather than
recording one, so it is gated harder than anything else here: armed hours
only, once per incident, a strict daily ceiling, and a mute switch on the wall
panel that a person can reach.

On getting the sound out
------------------------
The SD2X manual confirms an Audio Out jack on the pigtail for an external
powered speaker (a bare speaker will not drive it). What the manual does not
document is how to *push* audio to it: that is Foscam's two-way talk feature,
and the protocol behind it is not a documented CGI call. Until that is
verified against the real camera, the certain option is a powered speaker on
the Rock 5B's own audio output, which needs no protocol at all.

So the player is pluggable and the default is a local command. `player_cmd`
takes {file}; anything that can be spoken from a shell can be the backend.
"""

import datetime as dt
import logging
import shlex
import subprocess
import threading
from pathlib import Path

log = logging.getLogger("announcer")


class CommandPlayer:
    """Plays a file with a configured command. The general escape hatch."""

    def __init__(self, template="aplay -q {file}", timeout=30):
        self.template = template
        self.timeout = timeout

    def play(self, path):
        cmd = shlex.split(self.template.format(file=shlex.quote(str(path))))
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=self.timeout)
        except FileNotFoundError:
            log.error("player command not found: %s. Set announce.player_cmd "
                      "to something this box actually has.", cmd[0])
            return False
        except subprocess.SubprocessError as e:
            log.error("playback failed: %s", e)
            return False
        if r.returncode != 0:
            log.error("playback exited %s: %s", r.returncode,
                      r.stderr.decode("utf-8", "replace")[:200])
            return False
        return True


class NullPlayer:
    """Logs what would have been said. The default until a speaker exists."""

    def __init__(self):
        self.played = []

    def play(self, path):
        self.played.append(path)
        log.info("[no speaker configured] would have played %s", path)
        return True


class Announcer:
    def __init__(self, cfg, schedule, player=None, clock=None):
        c = (cfg._get("announce", default={}) or {}) if hasattr(cfg, "_get") else (cfg or {})
        self.cfg = cfg
        self.schedule = schedule
        self.enabled = bool(c.get("enabled", False))
        self.sound = c.get("sound", "sounds/warning.wav")
        self.min_interval_s = float(c.get("min_interval_s", 120))
        self.max_per_day = int(c.get("max_per_day", 6))
        self.require_armed = bool(c.get("require_armed", True))
        self.min_confidence = float(c.get("min_confidence", 0.8))

        if player is not None:
            self.player = player
        elif c.get("player_cmd"):
            self.player = CommandPlayer(c["player_cmd"])
        else:
            self.player = NullPlayer()

        self._clock = clock or dt.datetime.now
        # Reentrant: status() reads muted, which locks again. A plain Lock
        # self-deadlocks there, and because Controller.status() calls this
        # while holding its own lock, that wedges the detection loop too.
        self._lock = threading.RLock()
        self._last_at = None
        self._day = None
        self._today = 0
        self._muted_until = None
        self.spoken = 0
        self.refusals = {}

    # ---------------------------------------------------------------- mute
    def mute(self, minutes=120):
        until = self._clock() + dt.timedelta(minutes=max(1, min(minutes, 1440)))
        with self._lock:
            self._muted_until = until
        log.info("speaker muted until %s", until.strftime("%H:%M"))
        return until

    def unmute(self):
        with self._lock:
            self._muted_until = None
        log.info("speaker unmuted")

    @property
    def muted(self):
        with self._lock:
            return (self._muted_until is not None
                    and self._clock() < self._muted_until)

    # -------------------------------------------------------------- speaking
    def _refuse(self, reason):
        self.refusals[reason] = self.refusals.get(reason, 0) + 1
        log.info("not announcing: %s", reason)
        return False

    def maybe_announce(self, incident=None, now=None):
        """Speak, if every gate agrees. Returns True if it played."""
        now = now or self._clock()
        if not self.enabled:
            return self._refuse("announcements are disabled")
        if self.muted:
            return self._refuse("muted from the wall panel")
        if self.require_armed and not self.schedule.is_armed(now):
            # A voice at 2pm addressed to a member is worse than no voice.
            return self._refuse("not armed -- the club is open")
        if incident is not None and incident.max_confidence < self.min_confidence:
            return self._refuse(
                f"confidence {incident.max_confidence:.2f} below "
                f"{self.min_confidence:.2f}")

        with self._lock:
            if self._day != now.date():
                self._day, self._today = now.date(), 0
            if self._today >= self.max_per_day:
                return self._refuse(f"already spoken {self._today} times today")
            if self._last_at is not None:
                since = (now - self._last_at).total_seconds()
                if since < self.min_interval_s:
                    return self._refuse(f"spoke {since:.0f}s ago")
            self._last_at = now
            self._today += 1

        path = Path(self.sound)
        if not path.is_absolute():
            path = (Path(self.cfg.path).parent if hasattr(self.cfg, "path")
                    else Path(".")) / path
        if not path.exists():
            log.error("announcement sound not found: %s. Record one, or set "
                      "announce.sound.", path)
            return False

        log.info("ANNOUNCING at %s", now.strftime("%H:%M:%S"))
        if self.player.play(path):
            self.spoken += 1
            return True
        return False

    def status(self):
        with self._lock:
            muted = (self._muted_until is not None
                     and self._clock() < self._muted_until)
            return {"announce_enabled": self.enabled,
                    "muted": muted,
                    "muted_until": self._muted_until.strftime("%H:%M")
                    if self._muted_until else None,
                    "spoken_today": self._today,
                    "max_per_day": self.max_per_day,
                    "spoken_total": self.spoken}
