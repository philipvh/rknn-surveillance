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

"""Deciding what would be worth waking someone for -- and, for now, not doing it.

The previous system was switched off because it flooded people, so the policy
here stacks four independent gates and an alert requires all of them:

  1. the schedule       -- during club hours the courts are full of people
  2. corroboration      -- the PIR and the camera must agree
  3. persistence        -- seen over several seconds, not one lucky frame
  4. rate limiting      -- one message per incident, with a daily ceiling

Every decision is written to a shadow log with the reason, and nothing is
sent. After a fortnight the log answers "would this have flooded us?" with
evidence instead of hope. Phase 7 turns the same decisions into real alerts.
"""

from dataclasses import dataclass, field, asdict
import datetime as dt
import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("alerts")


@dataclass
class Incident:
    """One run of sightings, coalesced."""
    first_seen: dt.datetime
    last_seen: dt.datetime
    frames: int = 0
    sightings: int = 0
    max_confidence: float = 0.0
    max_count: int = 0
    labels: set = field(default_factory=set)
    pir_corroborated: bool = False
    pir_duration_s: float = 0.0
    preset: str = ""
    snapshot: str = ""
    clip: str = ""

    @property
    def duration_s(self):
        return (self.last_seen - self.first_seen).total_seconds()

    def summary(self):
        who = ", ".join(sorted(self.labels)) or "something"
        return (f"{who} x{self.max_count} for {self.duration_s:.0f}s "
                f"at {self.preset or 'the parked view'} "
                f"(confidence {self.max_confidence:.2f})")


    @classmethod
    def from_row(cls, row):
        """Rebuild an incident from a shadow-log row.

        The log deliberately records the raw facts a decision was made from --
        duration, sightings, confidence, corroboration, time -- and not only
        the verdict. That is what lets one fortnight of data answer "what if
        the threshold had been 0.8?" without waiting another fortnight.
        """
        first = dt.datetime.fromisoformat(row["first_seen"])
        # last_seen is written at second precision, but duration_s keeps a
        # decimal. Rebuilding from the duration preserves the precision the
        # log actually recorded, which matters when replaying against a
        # threshold like min_duration_s = 4.0.
        if "duration_s" in row:
            last = first + dt.timedelta(seconds=float(row["duration_s"]))
        else:
            last = dt.datetime.fromisoformat(row["last_seen"])
        inc = cls(
            first_seen=first,
            last_seen=last,
            sightings=int(row.get("sightings", 0)),
            max_confidence=float(row.get("max_confidence", 0.0)),
            max_count=int(row.get("max_count", 0)),
            labels=set(row.get("labels") or []),
            pir_corroborated=bool(row.get("pir", False)),
            pir_duration_s=float(row.get("pir_duration_s", 0.0)),
            preset=row.get("preset", ""),
            snapshot=row.get("snapshot", ""),
            clip=row.get("clip", ""),
        )
        inc.frames = inc.sightings
        return inc


@dataclass
class Decision:
    would_alert: bool
    passed: list
    failed: str = ""
    detail: str = ""


class AlertPolicy:
    def __init__(self, cfg, schedule, clock=None, overrides=None):
        c = (cfg._get("alerts", default={}) or {}) if hasattr(cfg, "_get") else (cfg or {})
        if overrides:
            c = dict(c)
            c.update(overrides)
        self.schedule = schedule
        self.require_pir = bool(c.get("require_pir", True))
        self.min_duration_s = float(c.get("min_duration_s", 4.0))
        self.min_sightings = int(c.get("min_sightings", 3))
        self.min_confidence = float(c.get("min_confidence", 0.7))
        self.min_interval_s = float(c.get("min_interval_s", 300))
        self.max_per_day = int(c.get("max_per_day", 12))
        self._clock = clock or dt.datetime.now
        self._last_alert_at = None
        self._day = None
        self._today_count = 0
        self._lock = threading.Lock()

    def _roll_day(self, now):
        if self._day != now.date():
            self._day = now.date()
            self._today_count = 0

    def evaluate(self, incident, now=None):
        """Apply the four gates in order, cheapest and most decisive first."""
        now = now or self._clock()
        passed = []
        with self._lock:
            self._roll_day(now)

            if not self.schedule.is_armed(incident.first_seen):
                return Decision(False, passed, "schedule",
                                f"not armed at {incident.first_seen:%a %H:%M} "
                                f"({self.schedule.describe(incident.first_seen)})")
            passed.append("schedule")

            if self.require_pir and not incident.pir_corroborated:
                return Decision(False, passed, "corroboration",
                                "the camera saw someone but the PIR did not")
            passed.append("corroboration")

            if incident.duration_s < self.min_duration_s:
                return Decision(False, passed, "persistence",
                                f"present for {incident.duration_s:.1f}s, "
                                f"minimum is {self.min_duration_s:.0f}s")
            if incident.sightings < self.min_sightings:
                return Decision(False, passed, "persistence",
                                f"{incident.sightings} sighting(s), "
                                f"minimum is {self.min_sightings}")
            if incident.max_confidence < self.min_confidence:
                return Decision(False, passed, "persistence",
                                f"best confidence {incident.max_confidence:.2f}, "
                                f"minimum is {self.min_confidence:.2f}")
            passed.append("persistence")

            if self._today_count >= self.max_per_day:
                return Decision(False, passed, "rate limit",
                                f"{self._today_count} alerts already today "
                                f"(ceiling {self.max_per_day})")
            if self._last_alert_at is not None:
                since = (now - self._last_alert_at).total_seconds()
                if since < self.min_interval_s:
                    return Decision(False, passed, "rate limit",
                                    f"last alert {since:.0f}s ago, minimum "
                                    f"interval is {self.min_interval_s:.0f}s")
            passed.append("rate limit")

            # Counted here rather than at send time: in shadow mode nothing is
            # sent, and the log must show the volume a real uplink would carry.
            self._last_alert_at = now
            self._today_count += 1
            return Decision(True, passed, "", incident.summary())

    def status(self):
        with self._lock:
            return {"alerts_today": self._today_count,
                    "max_per_day": self.max_per_day,
                    "last_alert_at": self._last_alert_at.isoformat()
                    if self._last_alert_at else None}


class ShadowLog:
    """Append-only record of what would have been sent.

    One JSON object per line per day, so a fortnight can be read with wc, jq,
    or the summary command in Phase 6.
    """

    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.Lock()

    def path_for(self, when):
        return self.root / f"{when:%Y-%m}" / f"{when:%Y-%m-%d}.jsonl"

    def record(self, incident, decision, when=None):
        when = when or dt.datetime.now()
        row = {
            "at": when.isoformat(timespec="seconds"),
            "would_alert": decision.would_alert,
            "passed": decision.passed,
            "failed": decision.failed,
            "detail": decision.detail,
            "summary": incident.summary(),
            "first_seen": incident.first_seen.isoformat(timespec="seconds"),
            "last_seen": incident.last_seen.isoformat(timespec="seconds"),
            "duration_s": round(incident.duration_s, 1),
            "sightings": incident.sightings,
            "max_confidence": round(incident.max_confidence, 3),
            "max_count": incident.max_count,
            "labels": sorted(incident.labels),
            "pir": incident.pir_corroborated,
            "pir_duration_s": round(incident.pir_duration_s, 1),
            "preset": incident.preset,
            "snapshot": incident.snapshot,
            "clip": incident.clip,
        }
        p = self.path_for(when)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, open(p, "a") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError as e:
            log.error("could not write the shadow log: %s", e)
            return None
        if decision.would_alert:
            log.info("WOULD ALERT: %s", decision.detail)
        else:
            log.info("no alert (%s): %s", decision.failed, decision.detail)
        return p

    def read(self, since=None, until=None):
        rows = []
        for p in sorted(self.root.rglob("*.jsonl")):
            try:
                for line in p.read_text().splitlines():
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    at = dt.datetime.fromisoformat(row["at"])
                    if since and at < since:
                        continue
                    if until and at > until:
                        continue
                    rows.append(row)
            except OSError:
                continue
        return rows
