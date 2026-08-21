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

"""Tests for the spoken warning.

This is the only part of the system that acts on a bystander rather than
recording one, so the tests are mostly about when it stays quiet.
"""
import datetime as dt, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from announcer import Announcer, CommandPlayer, NullPlayer  # noqa: E402
from schedule import Schedule  # noqa: E402


class RecordingPlayer:
    def __init__(self, ok=True):
        self.plays = []
        self.ok = ok

    def play(self, path):
        self.plays.append(path)
        return self.ok


class Cfg:
    def __init__(self, tmp, **over):
        self.path = Path(tmp) / "config.yaml"
        d = {"enabled": True, "sound": "warning.wav", "require_armed": True,
             "min_confidence": 0.8, "min_interval_s": 120, "max_per_day": 6}
        d.update(over)
        self.d = {"announce": d}

    def _get(self, *keys, default=None):
        node = self.d
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


class Incident:
    def __init__(self, conf=0.95):
        self.max_confidence = conf


NIGHT = dt.datetime(2026, 8, 19, 23, 30)
DAY = dt.datetime(2026, 8, 19, 14, 0)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        (Path(self.tmp.name) / "warning.wav").write_bytes(b"RIFF")
        self.sched = Schedule.from_config({"armed": [
            {"days": "all", "from": "22:00", "to": "08:00"}]})
        self.player = RecordingPlayer()
        self.now = NIGHT

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **over):
        return Announcer(Cfg(self.tmp.name, **over), self.sched,
                         player=self.player, clock=lambda: self.now)


class TestGates(Base):
    def test_it_speaks_at_night_to_a_confident_detection(self):
        a = self.make()
        self.assertTrue(a.maybe_announce(Incident()))
        self.assertEqual(len(self.player.plays), 1)

    def test_it_stays_quiet_during_club_hours(self):
        self.now = DAY
        a = self.make()
        self.assertFalse(a.maybe_announce(Incident()))
        self.assertEqual(self.player.plays, [],
                         "a voice addressed to a member is worse than no voice")

    def test_it_stays_quiet_when_disabled(self):
        self.assertFalse(self.make(enabled=False).maybe_announce(Incident()))

    def test_it_stays_quiet_below_the_confidence_floor(self):
        a = self.make()
        self.assertFalse(a.maybe_announce(Incident(conf=0.6)))
        self.assertEqual(self.player.plays, [])

    def test_it_speaks_once_then_holds_off(self):
        a = self.make()
        self.assertTrue(a.maybe_announce(Incident()))
        self.assertFalse(a.maybe_announce(Incident()))
        self.now += dt.timedelta(seconds=121)
        self.assertTrue(a.maybe_announce(Incident()))

    def test_the_daily_ceiling_holds(self):
        a = self.make(max_per_day=3, min_interval_s=0)
        for _ in range(10):
            a.maybe_announce(Incident())
            self.now += dt.timedelta(seconds=1)
        self.assertEqual(len(self.player.plays), 3)

    def test_the_ceiling_resets_the_next_day(self):
        a = self.make(max_per_day=1, min_interval_s=0)
        self.assertTrue(a.maybe_announce(Incident()))
        self.assertFalse(a.maybe_announce(Incident()))
        self.now += dt.timedelta(days=1)
        self.assertTrue(a.maybe_announce(Incident()))

    def test_a_missing_sound_file_is_reported_not_crashed(self):
        a = self.make(sound="does-not-exist.wav")
        self.assertFalse(a.maybe_announce(Incident()))
        self.assertEqual(self.player.plays, [])

    def test_a_failing_player_is_survivable(self):
        self.player.ok = False
        self.assertFalse(self.make().maybe_announce(Incident()))

    def test_every_refusal_is_counted_with_a_reason(self):
        self.now = DAY
        a = self.make()
        a.maybe_announce(Incident())
        self.assertTrue(a.refusals)
        self.assertTrue(any("armed" in k or "open" in k for k in a.refusals))


class TestMute(Base):
    def test_mute_silences_it(self):
        a = self.make()
        a.mute(120)
        self.assertTrue(a.muted)
        self.assertFalse(a.maybe_announce(Incident()))

    def test_mute_expires_on_its_own(self):
        a = self.make()
        a.mute(60)
        self.now += dt.timedelta(minutes=61)
        self.assertFalse(a.muted)
        self.assertTrue(a.maybe_announce(Incident()))

    def test_mute_cannot_be_made_permanent(self):
        a = self.make()
        until = a.mute(99999)
        self.assertLessEqual((until - self.now).total_seconds(), 24 * 3600 + 1,
                             "a voice nobody can silence gets unplugged; one "
                             "that can be silenced forever is not a deterrent")

    def test_unmute_works(self):
        a = self.make()
        a.mute(120)
        a.unmute()
        self.assertFalse(a.muted)
        self.assertTrue(a.maybe_announce(Incident()))


class TestPlayers(unittest.TestCase):
    def test_null_player_is_the_default_without_a_command(self):
        class C:
            path = Path("config.yaml")
            def _get(self, *k, default=None):
                return {"enabled": True} if k == ("announce",) else default
        a = Announcer(C(), Schedule.from_config({}))
        self.assertIsInstance(a.player, NullPlayer)

    def test_command_player_reports_a_missing_binary(self):
        p = CommandPlayer("definitely-not-a-real-binary {file}")
        self.assertFalse(p.play(Path("/tmp/x.wav")))

    def test_command_player_runs_a_real_command(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.wav"
            f.write_bytes(b"x")
            self.assertTrue(CommandPlayer("true {file}").play(f))
            self.assertFalse(CommandPlayer("false {file}").play(f))




class TestNoSelfDeadlock(Base):
    """Regression, found on the board.

    status() took the lock and then read `muted`, which locks again. A plain
    Lock is not reentrant, so it blocked forever -- and because
    Controller.status() called this while holding its own lock, the detection
    loop stopped with it. The panel polls status every two seconds, so the
    panel was killing the detector.
    """

    def _with_timeout(self, fn, seconds=3.0):
        import threading
        done = []
        t = threading.Thread(target=lambda: done.append(fn()), daemon=True)
        t.start()
        t.join(timeout=seconds)
        self.assertTrue(done, f"{fn.__name__} did not return within {seconds}s "
                              f"-- it deadlocked")
        return done[0]

    def test_status_returns(self):
        a = self.make()
        s = self._with_timeout(a.status)
        self.assertIn("muted", s)

    def test_status_returns_while_muted(self):
        a = self.make()
        a.mute(60)
        s = self._with_timeout(a.status)
        self.assertTrue(s["muted"])

    def test_status_is_safe_to_call_repeatedly_from_threads(self):
        import threading
        a = self.make()
        errors = []

        def hammer():
            try:
                for _ in range(50):
                    a.status()
                    a.muted
            except Exception as e:
                errors.append(e)

        ts = [threading.Thread(target=hammer, daemon=True) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=5)
        self.assertTrue(all(not t.is_alive() for t in ts),
                        "concurrent status calls deadlocked")
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
