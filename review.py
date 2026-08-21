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

"""Making a fortnight of shadow log decisive.

Two things turn "we logged a lot of incidents" into "we know what to do":

  * labelling  -- someone says which incidents were really worth an alert.
    Without that, tuning is guesswork about counts rather than about being
    right.
  * replay     -- re-evaluating the same recorded incidents under different
    thresholds. The log stores the raw facts a decision was made from, so
    "what if min_confidence were 0.8?" is answerable from data already in
    hand, instead of costing another two weeks per guess.
"""

import datetime as dt
import json
import logging
from pathlib import Path

from alerts import AlertPolicy, Incident

log = logging.getLogger("review")

REAL, FALSE, UNSURE = "real", "false", "unsure"

# Parameters worth sweeping, with the values to try.
SWEEP = {
    "min_confidence": [0.5, 0.6, 0.7, 0.8, 0.9],
    "min_sightings": [1, 2, 3, 5, 8],
    "min_duration_s": [0.0, 2.0, 4.0, 8.0, 15.0],
    "require_pir": [True, False],
    "min_interval_s": [0, 60, 300, 900],
    "max_per_day": [3, 6, 12, 50],
}


class Labels:
    """Verdicts on individual incidents, kept beside the log.

    Stored separately so labelling never rewrites the record of what the
    system actually decided at the time.
    """

    def __init__(self, root):
        self.path = Path(root) / "labels.json"
        self.data = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                log.warning("could not read %s: %s", self.path, e)

    def get(self, row):
        return self.data.get(row["at"])

    def set(self, row, verdict):
        self.data[row["at"]] = verdict

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        tmp.replace(self.path)

    def counts(self, rows):
        c = {REAL: 0, FALSE: 0, UNSURE: 0, None: 0}
        for r in rows:
            c[self.get(r)] = c.get(self.get(r), 0) + 1
        return c


def replay(rows, cfg, schedule, overrides=None):
    """Re-run the policy over recorded incidents, in order.

    Rate limiting and the daily ceiling are stateful, so the rows have to be
    replayed chronologically through a fresh policy rather than judged one at
    a time.
    """
    rows = sorted(rows, key=lambda r: r["at"])
    holder = {"now": None}
    policy = AlertPolicy(cfg, schedule, clock=lambda: holder["now"],
                         overrides=overrides or {})
    out = []
    for row in rows:
        holder["now"] = dt.datetime.fromisoformat(row["at"])
        decision = policy.evaluate(Incident.from_row(row), now=holder["now"])
        out.append((row, decision))
    return out


def score(results, labels):
    """How a candidate setting would have performed.

    Missing a real incident is far worse than one spurious message, so the
    two are reported separately rather than rolled into one number.
    """
    alerts = [r for r, d in results if d.would_alert]
    days = _span_days([r for r, _ in results])
    s = {
        "alerts": len(alerts),
        "per_day": len(alerts) / days if days else 0.0,
        "days": days,
        "real_alerted": 0, "real_missed": 0,
        "false_alerted": 0, "unlabelled_alerted": 0,
    }
    for row, decision in results:
        verdict = labels.get(row)
        if decision.would_alert:
            if verdict == REAL:
                s["real_alerted"] += 1
            elif verdict == FALSE:
                s["false_alerted"] += 1
            else:
                s["unlabelled_alerted"] += 1
        elif verdict == REAL:
            s["real_missed"] += 1
    total_real = s["real_alerted"] + s["real_missed"]
    s["recall"] = (s["real_alerted"] / total_real) if total_real else None
    told = s["real_alerted"] + s["false_alerted"]
    s["precision"] = (s["real_alerted"] / told) if told else None
    return s


def _span_days(rows):
    if not rows:
        return 0
    stamps = [dt.datetime.fromisoformat(r["at"]) for r in rows]
    return max(1, (max(stamps) - min(stamps)).days + 1)


def sweep(rows, cfg, schedule, labels, params=None):
    """Vary one parameter at a time from the current settings."""
    baseline = replay(rows, cfg, schedule)
    results = {"baseline": score(baseline, labels)}
    current = (cfg._get("alerts", default={}) or {})
    for name, values in (params or SWEEP).items():
        rowset = []
        for v in values:
            r = replay(rows, cfg, schedule, {name: v})
            s = score(r, labels)
            s["value"] = v
            s["is_current"] = (current.get(name) == v)
            rowset.append(s)
        results[name] = rowset
    return results
