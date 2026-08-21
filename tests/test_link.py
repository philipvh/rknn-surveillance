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

"""Tests for the radio link: wire format, queue, and the far end.

The three things the plan says Phase 7 is judged on -- an alert reaches a
phone, missing heartbeats raise an alarm, and a replayed packet is rejected --
are all testable with the two ends on one desk, or with no radio at all.
"""
import datetime as dt, sys, tempfile, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import link, outbox as outbox_mod, transports  # noqa: E402
from receiver import Notifier, Receiver  # noqa: E402

KEY = link.derive_key("a shared secret")
OTHER = link.derive_key("a different secret")


class FakeNotifier(Notifier):
    def __init__(self):
        super().__init__(topic=None)
        self.pushes = []

    def push(self, title, message, priority="default", tags=""):
        self.pushes.append({"title": title, "message": message,
                            "priority": priority})
        return True


class Cfg:
    def __init__(self, d):
        self.d = d

    def _get(self, *keys, default=None):
        node = self.d
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


LINK_CFG = {"link": {"ack_timeout_s": 0.5, "max_attempts": 3,
                     "min_tx_interval_s": 0.0, "require_ack": True}}


# ------------------------------------------------------------------ codec

class TestWireFormat(unittest.TestCase):
    def test_a_frame_fits_a_small_lora_payload(self):
        f = link.encode(KEY, link.ALERT, 1, link.pack_alert(1700000000))
        self.assertEqual(len(f), 30)
        self.assertLessEqual(len(f), 51,
                             "must fit the smallest sensible LoRa payload")

    def test_round_trip(self):
        body = link.pack_alert(1700000000, zone=4, count=3, confidence=0.87,
                               duration_s=45, flags=link.F_PIR | link.F_ARMED)
        f = link.encode(KEY, link.ALERT, 7, body)
        t, c, b = link.decode(KEY, f)
        self.assertEqual((t, c), (link.ALERT, 7))
        a = link.unpack_alert(b)
        self.assertEqual(a["zone"], 4)
        self.assertEqual(a["count"], 3)
        self.assertAlmostEqual(a["confidence"], 0.87, places=2)
        self.assertEqual(a["duration_s"], 45)
        self.assertTrue(a["pir"] and a["armed"])

    def test_the_payload_is_not_readable_on_air(self):
        body = link.pack_alert(1700000000, zone=9, count=5, confidence=0.99)
        f = link.encode(KEY, link.ALERT, 1, body)
        self.assertNotIn(body, f,
                         "the body must not appear in clear -- an eavesdropper "
                         "would learn when the club is empty")

    def test_the_wrong_key_cannot_decode(self):
        f = link.encode(KEY, link.ALERT, 1, link.pack_alert(1700000000))
        with self.assertRaises(link.LinkError):
            link.decode(OTHER, f)

    def test_a_tampered_frame_is_refused(self):
        f = bytearray(link.encode(KEY, link.ALERT, 1, link.pack_alert(1700000000)))
        f[10] ^= 0x01
        with self.assertRaises(link.LinkError):
            link.decode(KEY, bytes(f))

    def test_a_tampered_counter_is_refused(self):
        """The counter is in clear, so it must still be authenticated."""
        f = bytearray(link.encode(KEY, link.ALERT, 5, link.pack_alert(1700000000)))
        f[4] = 99
        with self.assertRaises(link.LinkError):
            link.decode(KEY, bytes(f))

    def test_the_counter_is_bound_to_the_ciphertext(self):
        """Two independent bindings, so removing either is still caught.

        The nonce is derived from the counter and the counter is also passed
        as associated data. A frame authenticated under one counter must not
        verify under another.
        """
        a = link.encode(KEY, link.ALERT, 5, link.pack_alert(1700000000))
        b = link.encode(KEY, link.ALERT, 6, link.pack_alert(1700000000))
        self.assertNotEqual(a[5:], b[5:],
                            "the same body under a different counter must "
                            "produce different ciphertext")
        spliced = b[:5] + a[5:]          # counter of one, ciphertext of another
        with self.assertRaises(link.LinkError):
            link.decode(KEY, spliced)

    def test_truncated_and_oversized_frames_are_refused(self):
        f = link.encode(KEY, link.ALERT, 1, link.pack_alert(1700000000))
        for bad in (f[:-1], f + b"\x00", b"", b"\x11"):
            with self.assertRaises(link.LinkError):
                link.decode(KEY, bad)

    def test_directions_do_not_share_a_nonce(self):
        """Both ends use one key; reusing a nonce across them would leak."""
        body = link.pack_ack(1, 1700000000)
        up = link.encode(KEY, link.ACK, 1, body, direction=0)
        down = link.encode(KEY, link.ACK, 1, body, direction=1)
        self.assertNotEqual(up, down)
        with self.assertRaises(link.LinkError):
            link.decode(KEY, up, direction=1)

    def test_all_body_types_are_the_same_size(self):
        for b in (link.pack_alert(1), link.pack_heartbeat(1), link.pack_ack(1, 1)):
            self.assertEqual(len(b), link.BODY_LEN)

    def test_duration_encoding_keeps_resolution_where_it_matters(self):
        for s in (0, 1, 30, 59):
            self.assertEqual(link._unlog_seconds(link._log_seconds(s)), s)
        self.assertGreater(link._unlog_seconds(link._log_seconds(3600)), 3000)

    def test_no_key_is_an_error_not_a_default(self):
        with self.assertRaises(link.LinkError):
            link.derive_key("")


class TestReplayGuard(unittest.TestCase):
    def test_first_message_is_accepted(self):
        self.assertTrue(link.ReplayGuard().accept(5))

    def test_replay_is_rejected(self):
        g = link.ReplayGuard()
        g.accept(5)
        with self.assertRaises(link.ReplayError):
            g.check(5)
        with self.assertRaises(link.ReplayError):
            g.check(4)

    def test_newer_counters_advance(self):
        g = link.ReplayGuard()
        g.accept(5)
        self.assertTrue(g.accept(6))
        self.assertEqual(g.last, 6)

    def test_a_retransmission_window_does_not_advance_the_ratchet(self):
        g = link.ReplayGuard(window=3)
        g.accept(10)
        self.assertFalse(g.check(9))
        self.assertEqual(g.last, 10)


# ------------------------------------------------------------------ outbox

class OutboxBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.box = outbox_mod.Outbox(self.root, max_messages=5, max_age_days=1)

    def tearDown(self):
        self.tmp.cleanup()


class TestOutbox(OutboxBase):
    def test_counter_is_monotonic_and_survives_a_restart(self):
        a = self.box.put(link.ALERT, link.pack_alert(1))
        b = self.box.put(link.ALERT, link.pack_alert(2))
        self.assertEqual(b.counter, a.counter + 1)
        again = outbox_mod.Outbox(self.root)
        c = again.put(link.ALERT, link.pack_alert(3))
        self.assertGreater(c.counter, b.counter,
                           "a restart must never reissue a counter -- that "
                           "would reuse a nonce")

    def test_messages_survive_a_restart(self):
        self.box.put(link.ALERT, link.pack_alert(1))
        self.assertEqual(len(outbox_mod.Outbox(self.root).pending()), 1)

    def test_ack_removes_it(self):
        m = self.box.put(link.ALERT, link.pack_alert(1))
        self.box.ack(m.counter)
        self.assertEqual(self.box.pending(), [])

    def test_queue_is_bounded_oldest_first(self):
        for i in range(8):
            self.box.put(link.ALERT, link.pack_alert(i))
        pending = self.box.pending()
        self.assertEqual(len(pending), 5, "an offline year must not fill the disk")
        self.assertEqual(pending[0].counter, 4, "the oldest go first")

    def test_stale_messages_are_dropped(self):
        m = self.box.put(link.ALERT, link.pack_alert(1))
        p = self.box._path(m.counter)
        d = __import__("json").loads(p.read_text())
        d["created"] = time.time() - 3 * 86400
        p.write_text(__import__("json").dumps(d))
        self.box._enforce_bounds()
        self.assertEqual(self.box.pending(), [],
                         "a three-week-old alert nobody has heard about is "
                         "not news")

    def test_a_corrupt_entry_is_dropped_not_fatal(self):
        self.box.put(link.ALERT, link.pack_alert(1))
        (self.box.spool / "9999999999.json").write_text("{not json")
        self.assertEqual(len(self.box.pending()), 1)


# ------------------------------------------------------------------ sender

class Clock:
    """Advances a little on every read.

    A frozen clock is not a real scenario, and a sender that polls for an ACK
    against one would never time out.
    """

    def __init__(self, start=0.0, step=0.05):
        self.t = start
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t

    def jump(self, seconds):
        self.t += seconds


class TestSender(OutboxBase):
    def setUp(self):
        super().setUp()
        self.club, self.house = transports.LoopbackTransport.pair()
        self.clock = Clock()
        self.sender = outbox_mod.Sender(Cfg(LINK_CFG), self.box, self.club,
                                        KEY, clock=self.clock)
        self.notifier = FakeNotifier()
        self.rx = Receiver(KEY, self.house, self.notifier,
                           self.root / "rx.json", clock=lambda: 1700000000)

    def pump_both(self):
        """Sender transmits; receiver handles and acks; sender sees the ack."""
        import threading
        result = {}

        def rx_loop():
            f = self.house.receive(timeout=2.0)
            if f:
                result["type"] = self.rx.handle(f)

        t = threading.Thread(target=rx_loop)
        t.start()
        ok = self.sender.pump()
        t.join(timeout=3)
        return ok, result

    def test_an_alert_reaches_the_far_end_and_is_acknowledged(self):
        self.box.put(link.ALERT, link.pack_alert(1700000000, zone=2, count=1,
                                                 confidence=0.9,
                                                 flags=link.F_PIR))
        ok, result = self.pump_both()
        self.assertTrue(ok, "the message should have been acknowledged")
        self.assertEqual(result.get("type"), link.ALERT)
        self.assertEqual(self.box.pending(), [], "an acked message leaves the queue")
        self.assertEqual(len(self.notifier.pushes), 1)
        self.assertIn("club", self.notifier.pushes[0]["title"])
        self.assertEqual(self.notifier.pushes[0]["priority"], "high")

    def test_a_lost_packet_is_retried_and_not_lost(self):
        self.box.put(link.ALERT, link.pack_alert(1700000000))
        self.club.drop_next(1)
        ok, _ = self.pump_both()
        self.assertFalse(ok)
        self.assertEqual(len(self.box.pending()), 1,
                         "an unacknowledged message must stay queued")
        ok, result = self.pump_both()
        self.assertTrue(ok)
        self.assertEqual(self.box.pending(), [])

    def test_repeated_failure_leaves_it_queued_rather_than_dropped(self):
        self.box.put(link.ALERT, link.pack_alert(1700000000))
        self.club.drop_next(10)
        for _ in range(6):
            self.sender.pump()
        self.assertEqual(len(self.box.pending()), 1,
                         "a burglary alert is worth more than a tidy queue")

    def test_duty_cycle_paces_transmission(self):
        cfg = Cfg({"link": dict(LINK_CFG["link"], min_tx_interval_s=90.0,
                                require_ack=False)})
        clock = Clock()
        s = outbox_mod.Sender(cfg, self.box, self.club, KEY, clock=clock)
        for i in range(3):
            self.box.put(link.ALERT, link.pack_alert(i))
        self.assertTrue(s.pump())
        self.assertFalse(s.pump(), "the band limits how often we may transmit")
        clock.jump(91)
        self.assertTrue(s.pump())

    def test_nothing_is_sent_when_the_queue_is_empty(self):
        self.assertFalse(self.sender.pump())
        self.assertEqual(self.club.sent, [])


# ---------------------------------------------------------------- receiver

class TestReceiver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.club, self.house = transports.LoopbackTransport.pair()
        self.notifier = FakeNotifier()
        self.now = [1700000000.0]
        self.rx = Receiver(KEY, self.house, self.notifier,
                           self.root / "rx.json", clock=lambda: self.now[0])

    def tearDown(self):
        self.tmp.cleanup()

    def frame(self, msg_type, counter, body):
        return link.encode(KEY, msg_type, counter, body)

    def test_a_replayed_packet_is_rejected(self):
        f = self.frame(link.ALERT, 10, link.pack_alert(1700000000))
        self.assertEqual(self.rx.handle(f), link.ALERT)
        self.assertEqual(len(self.notifier.pushes), 1)
        self.rx.handle(f)
        self.assertEqual(len(self.notifier.pushes), 1,
                         "a captured packet replayed off the air must not "
                         "produce a second alert")
        self.assertEqual(self.rx.replays, 1)

    def test_an_older_counter_is_rejected(self):
        self.rx.handle(self.frame(link.ALERT, 20, link.pack_alert(1)))
        self.rx.handle(self.frame(link.ALERT, 5, link.pack_alert(1)))
        self.assertEqual(len(self.notifier.pushes), 1)

    def test_someone_elses_traffic_is_ignored(self):
        f = link.encode(OTHER, link.ALERT, 1, link.pack_alert(1))
        self.assertIsNone(self.rx.handle(f))
        self.assertEqual(self.notifier.pushes, [])
        self.assertEqual(self.rx.rejected, 1)

    def test_replay_state_survives_a_restart(self):
        f = self.frame(link.ALERT, 10, link.pack_alert(1))
        self.rx.handle(f)
        again = Receiver(KEY, self.house, self.notifier, self.root / "rx.json",
                         clock=lambda: self.now[0])
        again.handle(f)
        self.assertEqual(len(self.notifier.pushes), 1,
                         "restarting the receiver must not reopen the replay "
                         "window")

    def test_an_alert_is_acknowledged(self):
        self.rx.handle(self.frame(link.ALERT, 1, link.pack_alert(1700000000)))
        reply = self.club.receive(timeout=1)
        self.assertIsNotNone(reply)
        t, c, b = link.decode(KEY, reply, direction=1)
        self.assertEqual(t, link.ACK)
        self.assertEqual(link.unpack_ack(b)["acked"], 1)

    def test_heartbeat_does_not_wake_anyone(self):
        self.rx.handle(self.frame(link.HEARTBEAT, 1,
                                  link.pack_heartbeat(1700000000, 3, 40)))
        self.assertEqual(self.notifier.pushes, [],
                         "a routine heartbeat must be silent")

    def test_silence_raises_the_alarm(self):
        self.rx.handle(self.frame(link.HEARTBEAT, 1,
                                  link.pack_heartbeat(1700000000)))
        self.assertFalse(self.rx.check_silence())
        self.now[0] += 1.5 * 86400
        self.assertFalse(self.rx.check_silence(), "one missed is not an alarm")
        self.now[0] += 1.0 * 86400
        self.assertTrue(self.rx.check_silence())
        self.assertIn("NO SIGNAL", self.notifier.pushes[-1]["title"])

    def test_the_silence_alarm_fires_once_not_hourly(self):
        self.rx.handle(self.frame(link.HEARTBEAT, 1, link.pack_heartbeat(1)))
        self.now[0] += 5 * 86400
        self.assertTrue(self.rx.check_silence())
        for _ in range(5):
            self.assertFalse(self.rx.check_silence())
        self.assertEqual(sum(1 for p in self.notifier.pushes
                             if "NO SIGNAL" in p["title"]), 1)

    def test_recovery_is_announced(self):
        self.rx.handle(self.frame(link.HEARTBEAT, 1, link.pack_heartbeat(1)))
        self.now[0] += 5 * 86400
        self.rx.check_silence()
        self.rx.handle(self.frame(link.HEARTBEAT, 2, link.pack_heartbeat(2)))
        self.assertIn("back online", self.notifier.pushes[-1]["title"])

    def test_health_flags_reach_the_phone(self):
        self.rx.handle(self.frame(
            link.HEARTBEAT, 1,
            link.pack_heartbeat(1700000000, disk_percent=97,
                                flags=link.F_DISK_LOW | link.F_CAMERA_BAD)))
        titles = " ".join(p["title"] for p in self.notifier.pushes)
        self.assertIn("disk", titles)
        self.assertIn("camera", titles)


class TestFileTransport(unittest.TestCase):
    def test_frames_are_written_for_inspection_with_no_radio(self):
        with tempfile.TemporaryDirectory() as d:
            t = transports.FileTransport(Path(d) / "out.hex")
            f = link.encode(KEY, link.ALERT, 1, link.pack_alert(1700000000))
            t.send(f)
            text = (Path(d) / "out.hex").read_text()
            self.assertIn(f.hex(), text)
            self.assertIn("TX", text)




# ------------------------------------------------------------------ uplink

class TestUplinkGating(unittest.TestCase):
    """Nothing may leave the club until a labelled fortnight says so."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, **over):
        d = {
            "link": {"enabled": True, "transport": "loopback",
                     "spool_root": str(self.root / "link"),
                     "min_tx_interval_s": 0.0},
            "alerts": {"shadow_only": True},
            "ptz": {"scan_presets": ["Home", "Court1", "Gate"]},
        }
        for k, v in over.items():
            d.setdefault(k, {}).update(v)

        class C:
            link_psk = "test-psk"

            def _get(self, *keys, default=None):
                node = d
                for k in keys:
                    if not isinstance(node, dict) or k not in node:
                        return default
                    node = node[k]
                return node

            def resolve(self, p):
                return Path(p)
        return C()

    def incident(self):
        from alerts import Incident
        now = dt.datetime(2026, 8, 19, 23, 30)
        inc = Incident(first_seen=now, last_seen=now + dt.timedelta(seconds=30))
        inc.sightings = 9
        inc.max_confidence = 0.94
        inc.max_count = 2
        inc.labels = {"person"}
        inc.pir_corroborated = True
        inc.preset = "Court1"
        return inc

    def decision(self, would=True):
        from alerts import Decision
        return Decision(would, ["schedule", "corroboration", "persistence",
                                "rate limit"])

    def test_shadow_mode_queues_nothing(self):
        import uplink as up
        u = up.Uplink(self.cfg(), transport=transports.LoopbackTransport())
        self.assertIsNone(u.on_decision(self.incident(), self.decision()))
        self.assertEqual(u.outbox.depth(), 0,
                         "shadow mode must not put anything on the air")

    def test_with_shadow_off_a_passing_alert_is_queued(self):
        import uplink as up
        u = up.Uplink(self.cfg(alerts={"shadow_only": False}),
                      transport=transports.LoopbackTransport())
        m = u.on_decision(self.incident(), self.decision())
        self.assertIsNotNone(m)
        self.assertEqual(u.outbox.depth(), 1)
        t, c, body = link.decode(u.key, link.encode(u.key, m.msg_type,
                                                    m.counter, m.body))
        a = link.unpack_alert(body)
        self.assertEqual(a["count"], 2)
        self.assertEqual(a["zone"], 2, "Court1 is the second configured preset")
        self.assertTrue(a["pir"])

    def test_a_blocked_decision_is_never_queued(self):
        import uplink as up
        u = up.Uplink(self.cfg(alerts={"shadow_only": False}),
                      transport=transports.LoopbackTransport())
        u.on_decision(self.incident(), self.decision(would=False))
        self.assertEqual(u.outbox.depth(), 0)

    def test_no_psk_refuses_to_run_rather_than_running_in_clear(self):
        import uplink as up
        c = self.cfg(alerts={"shadow_only": False})
        c.link_psk = ""
        u = up.Uplink(c, transport=transports.LoopbackTransport())
        self.assertFalse(u.enabled)
        self.assertIsNone(u.outbox)

    def test_heartbeat_is_daily_not_hourly(self):
        import uplink as up
        u = up.Uplink(self.cfg(alerts={"shadow_only": False}),
                      transport=transports.LoopbackTransport())
        day = dt.datetime(2026, 8, 19, 9, 0)
        self.assertIsNotNone(u.maybe_heartbeat({}, now=day))
        self.assertIsNone(u.maybe_heartbeat({}, now=day.replace(hour=10)))
        self.assertIsNone(u.maybe_heartbeat({}, now=day.replace(hour=23)))
        self.assertIsNotNone(u.maybe_heartbeat({}, now=day + dt.timedelta(days=1)))

    def test_heartbeat_waits_for_its_hour(self):
        import uplink as up
        u = up.Uplink(self.cfg(alerts={"shadow_only": False}),
                      transport=transports.LoopbackTransport())
        self.assertIsNone(u.maybe_heartbeat(
            {}, now=dt.datetime(2026, 8, 19, 3, 0)))

    def test_heartbeat_carries_health(self):
        import uplink as up
        u = up.Uplink(self.cfg(alerts={"shadow_only": False}),
                      transport=transports.LoopbackTransport())
        m = u.maybe_heartbeat({"disk_percent": 93, "disk_low": True,
                               "camera_bad": True, "armed": True,
                               "events_today": 4},
                              now=dt.datetime(2026, 8, 19, 9, 0))
        h = link.unpack_heartbeat(m.body)
        self.assertEqual(h["disk_percent"], 93)
        self.assertTrue(h["disk_low"] and h["camera_bad"] and h["armed"])
        self.assertEqual(h["events_today"], 4)

    def test_disabled_link_is_inert_and_harmless(self):
        import uplink as up
        u = up.Uplink(self.cfg(link={"enabled": False}))
        self.assertIsNone(u.on_decision(self.incident(), self.decision()))
        self.assertIsNone(u.maybe_heartbeat({}))
        self.assertEqual(u.status(), {"link": "disabled"})




class TestSenderThreadHygiene(unittest.TestCase):
    """Same shadowing bug as TriggerInput: Sender is a Thread too."""

    def test_start_and_join_do_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            box = outbox_mod.Outbox(Path(d))
            s = outbox_mod.Sender(Cfg(LINK_CFG), box,
                                  transports.LoopbackTransport(), KEY)
            s.start()
            s.stop()
            s.join(timeout=2)
            self.assertFalse(s.is_alive())

    def test_no_thread_internals_are_shadowed(self):
        import threading
        # Only methods matter: Thread.__init__ legitimately sets instance
        # attributes like _initialized. The bug is replacing a method the
        # machinery calls -- join() calls self._stop() -- with a non-callable.
        reserved = {n for n in dir(threading.Thread)
                    if n.startswith("_") and callable(getattr(threading.Thread, n, None))}
        with tempfile.TemporaryDirectory() as d:
            s = outbox_mod.Sender(Cfg(LINK_CFG), outbox_mod.Outbox(Path(d)),
                                  transports.LoopbackTransport(), KEY)
            clashes = sorted(n for n in set(vars(s)) & reserved
                             if not callable(vars(s)[n]))
            self.assertEqual(clashes, [],
                             f"these attributes shadow Thread methods: {clashes}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
