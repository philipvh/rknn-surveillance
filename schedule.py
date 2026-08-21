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

"""When the system is armed.

The single most important false-alarm gate. During club hours the courts are
full of people and every one of them is a valid person detection, so alerting
only exists outside those hours. Recording is unaffected -- it runs regardless,
because disk is cheap and a clip nobody was notified about is still evidence.

Windows name the day they *start* on, so a Monday 23:00-07:00 window covers
Monday night into Tuesday morning.
"""

import datetime as dt
import logging

log = logging.getLogger("schedule")

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
DAY_NAMES = {v: k for k, v in DAYS.items()}


class ScheduleError(Exception):
    pass


def _parse_time(s):
    try:
        h, m = str(s).split(":")
        t = dt.time(int(h), int(m))
    except (ValueError, AttributeError):
        raise ScheduleError(f"bad time {s!r}, expected HH:MM")
    return t


def _parse_days(spec):
    if spec in (None, "all", "daily"):
        return set(DAYS.values())
    if isinstance(spec, str):
        spec = [spec]
    out = set()
    for d in spec:
        key = str(d).strip().lower()[:3]
        if key not in DAYS:
            raise ScheduleError(f"unknown day {d!r}, expected one of {sorted(DAYS)}")
        out.add(DAYS[key])
    if not out:
        raise ScheduleError("a schedule window lists no days")
    return out


class Window:
    def __init__(self, days, start, end):
        self.days = _parse_days(days)
        self.start = _parse_time(start)
        self.end = _parse_time(end)

    @property
    def wraps(self):
        """True if the window runs past midnight."""
        return self.end <= self.start

    @property
    def minutes(self):
        s = self.start.hour * 60 + self.start.minute
        e = self.end.hour * 60 + self.end.minute
        return (1440 - s + e) if self.wraps else (e - s)

    def contains(self, when):
        # A wrapping window started yesterday, so look back a day as well.
        for back in (0, 1):
            day = when - dt.timedelta(days=back)
            if day.weekday() not in self.days:
                continue
            start = day.replace(hour=self.start.hour, minute=self.start.minute,
                                second=0, microsecond=0)
            if start <= when < start + dt.timedelta(minutes=self.minutes):
                return True
        return False

    def __repr__(self):
        days = ",".join(DAY_NAMES[d] for d in sorted(self.days))
        return (f"<{days} {self.start:%H:%M}-{self.end:%H:%M}"
                f"{' +1d' if self.wraps else ''}>")


class Schedule:
    def __init__(self, windows=(), always_armed=False):
        self.windows = list(windows)
        self.always_armed = bool(always_armed)
        self._override_until = None
        self._override_state = None

    @classmethod
    def from_config(cls, cfg):
        c = (cfg._get("schedule", default={}) or {}) if hasattr(cfg, "_get") else (cfg or {})
        windows = []
        for w in (c.get("armed") or []):
            windows.append(Window(w.get("days"), w.get("from"), w.get("to")))
        sched = cls(windows, always_armed=c.get("always_armed", False))
        if not windows and not sched.always_armed:
            log.warning("no armed windows configured -- alerting will never "
                        "fire. Set schedule.armed in config.yaml.")
        return sched

    # ---------------------------------------------------------------- override
    def override(self, armed, until):
        """Arm or disarm by hand from the wall panel, expiring automatically.

        Someone working late needs to silence it without being able to leave
        it silenced for good.
        """
        self._override_state = bool(armed)
        self._override_until = until
        log.info("schedule overridden to %s until %s",
                 "ARMED" if armed else "DISARMED", until)

    def clear_override(self):
        self._override_state = self._override_until = None

    def override_active(self, when):
        return (self._override_until is not None and when < self._override_until)

    # -------------------------------------------------------------------- api
    def is_armed(self, when=None):
        when = when or dt.datetime.now()
        if self.override_active(when):
            return self._override_state
        if self.always_armed:
            return True
        return any(w.contains(when) for w in self.windows)

    def describe(self, when=None):
        when = when or dt.datetime.now()
        if self.override_active(when):
            return (f"{'armed' if self._override_state else 'disarmed'} by hand "
                    f"until {self._override_until:%H:%M}")
        if self.always_armed:
            return "armed (always_armed is set)"
        if self.is_armed(when):
            return "armed (inside a scheduled window)"
        return "disarmed (club hours)"

    def next_change(self, when=None, horizon_h=48):
        """When the armed state next flips. Used for the panel's status line."""
        when = when or dt.datetime.now()
        state = self.is_armed(when)
        probe = when.replace(second=0, microsecond=0)
        for _ in range(horizon_h * 60):
            probe += dt.timedelta(minutes=1)
            if self.is_armed(probe) != state:
                return probe
        return None
