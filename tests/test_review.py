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

"""Tests for shadow-log review and replay.

The property that makes 'tune' trustworthy: replaying the log with the
settings that were in force must reproduce exactly what the system decided at
the time. If that does not hold, every what-if answer is fiction.
"""
import datetime as dt, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import review  # noqa: E402
from alerts import AlertPolicy, Incident, ShadowLog  # noqa: E402
from schedule import Schedule  # noqa: E402


class Cfg:
    def __init__(self, alerts):
        self.d = {"alerts": alerts}

    def _get(self, *keys, default=None):
        node = self.d
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


ALERTS = {"require_pir": True, "min_duration_s": 4.0, "min_sightings": 3,
          "min_confidence": 0.7, "min_interval_s": 300, "max_per_day": 12}


def make_incident(when, dur=10.0, sightings=8, conf=0.9, pir=True):
    return Incident(first_seen=when, last_seen=when + dt.timedelta(seconds=dur),
                    sightings=sightings, max_confidence=conf,
                    max_count=1, labels={"person"}, pir_corroborated=pir)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Cfg(dict(ALERTS))
        self.sched = Schedule.from_config({"armed": [
            {"days": "all", "from": "22:00", "to": "08:00"}]})
        self.log = ShadowLog(self.root)
        self.labels = review.Labels(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def record_night(self, specs, day=19):
        """specs: list of (hour, minute, dur, sightings, conf, pir)"""
        holder = {"now": None}
        policy = AlertPolicy(self.cfg, self.sched, clock=lambda: holder["now"])
        written = []
        for (h, m, dur, n, conf, pir) in specs:
            when = dt.datetime(2026, 8, day, h, m)
            holder["now"] = when
            inc = make_incident(when, dur, n, conf, pir)
            d = policy.evaluate(inc, now=when)
            self.log.record(inc, d, when=when)
            written.append((inc, d))
        return written


class TestReplayFidelity(Base):
    def test_replay_reproduces_the_original_decisions(self):
        # Chronological, as an append-only log always is.
        original = self.record_night([
            (2, 0, 20, 9, 0.95, False),      # no PIR
            (3, 0, 20, 9, 0.55, True),       # low confidence
            (4, 0, 30, 9, 0.99, True),       # should alert
            (14, 0, 20, 9, 0.95, True),      # club hours
            (23, 0, 10, 8, 0.92, True),      # should alert
            (23, 2, 10, 8, 0.92, True),      # rate limited
            (23, 40, 1, 1, 0.95, True),      # too brief
        ])
        rows = self.log.read()
        self.assertEqual(len(rows), len(original))

        results = review.replay(rows, self.cfg, self.sched)
        by_at = {r["at"]: d for r, d in results}
        for row in rows:
            self.assertEqual(
                by_at[row["at"]].would_alert, row["would_alert"],
                f"replay disagreed with the recorded decision at {row['at']}")

    def test_replay_reproduces_the_blocking_reason_too(self):
        self.record_night([
            (14, 0, 20, 9, 0.95, True),
            (2, 0, 20, 9, 0.95, False),
            (3, 0, 20, 9, 0.55, True),
        ])
        rows = self.log.read()
        for row, decision in review.replay(rows, self.cfg, self.sched):
            self.assertEqual(decision.failed, row["failed"], row["at"])

    def test_rate_limiting_is_replayed_in_order(self):
        self.record_night([(23, 0, 10, 8, 0.9, True),
                           (23, 1, 10, 8, 0.9, True),
                           (23, 2, 10, 8, 0.9, True)])
        rows = self.log.read()
        results = review.replay(rows, self.cfg, self.sched)
        self.assertEqual(sum(1 for _, d in results if d.would_alert), 1,
                         "the 5 minute floor must survive replay")

    def test_a_backwards_clock_jump_does_not_break_replay(self):
        """With no NTP, a wrong clock after a power cut can write rows out of
        order. Sorting on read is the sane recovery."""
        self.record_night([(23, 0, 10, 8, 0.9, True)], day=20)
        self.record_night([(23, 0, 10, 8, 0.9, True)], day=19)
        rows = self.log.read()
        results = review.replay(rows, self.cfg, self.sched)
        self.assertEqual(len(results), 2)
        self.assertEqual([r["at"] for r, _ in results],
                         sorted(r["at"] for r, _ in results))

    def test_replay_is_order_independent_of_input(self):
        self.record_night([(23, 0, 10, 8, 0.9, True),
                           (23, 1, 10, 8, 0.9, True)])
        rows = self.log.read()
        a = review.replay(rows, self.cfg, self.sched)
        b = review.replay(list(reversed(rows)), self.cfg, self.sched)
        self.assertEqual([d.would_alert for _, d in a],
                         [d.would_alert for _, d in b],
                         "replay must sort chronologically itself")


class TestWhatIf(Base):
    def test_relaxing_confidence_admits_more(self):
        self.record_night([(23, 0, 20, 9, 0.65, True),
                           (1, 0, 20, 9, 0.62, True)])
        rows = self.log.read()
        strict = review.replay(rows, self.cfg, self.sched)
        loose = review.replay(rows, self.cfg, self.sched, {"min_confidence": 0.6})
        self.assertEqual(sum(d.would_alert for _, d in strict), 0)
        self.assertEqual(sum(d.would_alert for _, d in loose), 2)

    def test_dropping_pir_requirement_admits_camera_only_sightings(self):
        self.record_night([(23, 0, 20, 9, 0.95, False)])
        rows = self.log.read()
        self.assertEqual(
            sum(d.would_alert for _, d in review.replay(rows, self.cfg, self.sched)), 0)
        self.assertEqual(
            sum(d.would_alert for _, d in
                review.replay(rows, self.cfg, self.sched, {"require_pir": False})), 1)

    def test_sweep_covers_the_documented_parameters(self):
        self.record_night([(23, 0, 20, 9, 0.9, True), (2, 0, 5, 2, 0.6, False)])
        rows = self.log.read()
        out = review.sweep(rows, self.cfg, self.sched, self.labels)
        self.assertIn("baseline", out)
        for name in review.SWEEP:
            self.assertIn(name, out)
            self.assertTrue(any(o["is_current"] for o in out[name])
                            or name not in ALERTS)


class TestScoring(Base):
    def test_missed_real_incidents_are_counted(self):
        self.record_night([(23, 0, 1, 1, 0.99, True)])   # too brief -> blocked
        rows = self.log.read()
        self.labels.set(rows[0], review.REAL)
        s = review.score(review.replay(rows, self.cfg, self.sched), self.labels)
        self.assertEqual(s["real_missed"], 1)
        self.assertEqual(s["real_alerted"], 0)
        self.assertEqual(s["recall"], 0.0)

    def test_a_setting_that_recovers_a_missed_incident_shows_up(self):
        self.record_night([(23, 0, 1, 1, 0.99, True)])
        rows = self.log.read()
        self.labels.set(rows[0], review.REAL)
        relaxed = review.replay(rows, self.cfg, self.sched,
                                {"min_duration_s": 0.0, "min_sightings": 1})
        s = review.score(relaxed, self.labels)
        self.assertEqual(s["real_missed"], 0)
        self.assertEqual(s["real_alerted"], 1)
        self.assertEqual(s["recall"], 1.0)

    def test_precision_counts_false_alarms(self):
        self.record_night([(23, 0, 20, 9, 0.95, True),
                           (3, 0, 20, 9, 0.95, True)])
        rows = self.log.read()
        self.labels.set(rows[0], review.REAL)
        self.labels.set(rows[1], review.FALSE)
        s = review.score(review.replay(rows, self.cfg, self.sched), self.labels)
        self.assertEqual(s["real_alerted"], 1)
        self.assertEqual(s["false_alerted"], 1)
        self.assertAlmostEqual(s["precision"], 0.5)

    def test_unlabelled_data_still_yields_counts(self):
        self.record_night([(23, 0, 20, 9, 0.95, True)])
        s = review.score(review.replay(self.log.read(), self.cfg, self.sched),
                         self.labels)
        self.assertEqual(s["alerts"], 1)
        self.assertIsNone(s["precision"])


class TestLabels(Base):
    def test_labels_round_trip(self):
        self.record_night([(23, 0, 20, 9, 0.95, True)])
        rows = self.log.read()
        self.labels.set(rows[0], review.REAL)
        self.labels.save()
        again = review.Labels(self.root)
        self.assertEqual(again.get(rows[0]), review.REAL)

    def test_labels_do_not_alter_the_log(self):
        self.record_night([(23, 0, 20, 9, 0.95, True)])
        before = (self.root / "2026-08" / "2026-08-19.jsonl").read_text()
        rows = self.log.read()
        self.labels.set(rows[0], review.FALSE)
        self.labels.save()
        after = (self.root / "2026-08" / "2026-08-19.jsonl").read_text()
        self.assertEqual(before, after,
                         "labelling must never rewrite what the system decided")

    def test_corrupt_label_file_is_survivable(self):
        (self.root).mkdir(parents=True, exist_ok=True)
        (self.root / "labels.json").write_text("{not json")
        self.assertEqual(review.Labels(self.root).data, {})


class TestIncidentRoundTrip(Base):
    def test_row_rebuilds_into_an_equivalent_incident(self):
        when = dt.datetime(2026, 8, 19, 23, 0)
        inc = make_incident(when, dur=12.5, sightings=7, conf=0.83, pir=True)
        inc.preset = "Court1"; inc.snapshot = "a.jpg"; inc.clip = "b.mp4"
        policy = AlertPolicy(self.cfg, self.sched, clock=lambda: when)
        self.log.record(inc, policy.evaluate(inc, now=when), when=when)
        back = Incident.from_row(self.log.read()[0])
        self.assertEqual(back.sightings, inc.sightings)
        self.assertAlmostEqual(back.max_confidence, inc.max_confidence, places=3)
        self.assertAlmostEqual(back.duration_s, inc.duration_s, places=1)
        self.assertEqual(back.pir_corroborated, inc.pir_corroborated)
        self.assertEqual(back.preset, "Court1")
        self.assertEqual(back.clip, "b.mp4")


if __name__ == "__main__":
    unittest.main(verbosity=2)
