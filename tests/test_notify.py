# Copyright 2026 Philip van Houtte, magicview.tv, the Netherlands
# SPDX-License-Identifier: Apache-2.0
"""Push notifications, and the gate that stops cats sending them."""

import datetime as dt
import io
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import notify  # noqa: E402
from alerts import AlertPolicy, Incident  # noqa: E402


class FakeSched:
    def __init__(self, armed=True):
        self.armed = armed

    def is_armed(self, when):
        return self.armed

    def describe(self, when):
        return "the night window"


def policy(**over):
    c = {"require_pir": False, "min_duration_s": 0, "min_sightings": 0,
         "min_confidence": 0, "min_interval_s": 0, "max_per_day": 99}
    c.update(over)
    return AlertPolicy(c, FakeSched(over.pop("_armed", True)))


def incident(labels, when=None, conf=0.9, snapshot=""):
    when = when or dt.datetime(2026, 9, 28, 3, 14, 0)
    return Incident(first_seen=when, last_seen=when + dt.timedelta(seconds=9),
                    labels=set(labels), max_confidence=conf, max_count=1,
                    snapshot=snapshot)


class TestTheClassGate(unittest.TestCase):
    """'I sometimes see cats or birds in the middle of the night.'"""

    CLASSES = ["person", "car", "bus", "truck", "motorbike", "bicycle"]

    def verdict(self, labels):
        return policy(alert_classes=self.CLASSES).evaluate(incident(labels))

    def test_a_person_is_worth_sending(self):
        self.assertTrue(self.verdict(["person"]).would_alert)

    def test_a_vehicle_is_worth_sending(self):
        for v in ("car", "truck", "motorbike"):
            self.assertTrue(self.verdict([v]).would_alert, v)

    def test_a_cat_at_three_in_the_morning_is_not(self):
        d = self.verdict(["cat"])
        self.assertFalse(d.would_alert)
        self.assertEqual(d.failed, "class")

    def test_a_bird_is_not(self):
        self.assertFalse(self.verdict(["bird"]).would_alert)

    def test_a_person_beside_a_cat_still_is(self):
        """The gate is 'anything worth sending', not 'only things worth
        sending' -- a cat in frame must not suppress the person next to it."""
        self.assertTrue(self.verdict(["cat", "person"]).would_alert)

    def test_an_empty_list_means_the_gate_is_off(self):
        self.assertTrue(policy(alert_classes=[]).evaluate(
            incident(["cat"])).would_alert)

    def test_the_gate_is_case_and_space_insensitive(self):
        """COCO labels in this repo carry trailing spaces."""
        p = policy(alert_classes=["Person "])
        self.assertTrue(p.evaluate(incident(["person"])).would_alert)

    def test_the_schedule_still_comes_first(self):
        """Nothing is sent outside the armed window, whatever was seen."""
        p = AlertPolicy({"require_pir": False, "min_duration_s": 0,
                         "min_sightings": 0, "min_confidence": 0,
                         "alert_classes": self.CLASSES}, FakeSched(armed=False))
        d = p.evaluate(incident(["person"]))
        self.assertFalse(d.would_alert)
        self.assertEqual(d.failed, "schedule")


class FakeHTTP:
    """Stands in for urlopen, recording what would have gone out."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append({"url": req.full_url, "body": req.data,
                           "headers": dict(req.header_items())})
        if self.errors:
            e = self.errors.pop(0)
            if e is not None:
                raise e

        class R:
            status = 200

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False
        return R()


class TestNotifier(unittest.TestCase):
    def build(self, http=None, **over):
        c = {"enabled": True, "topic": "sekrit", "site": "TVW", "retries": 0}
        c.update(over)
        return notify.Notifier(c, opener=http or FakeHTTP())

    def test_it_does_nothing_when_no_topic_is_set(self):
        http = FakeHTTP()
        n = self.build(http, topic="")
        self.assertFalse(n.send("t", "m"))
        self.assertEqual(http.calls, [], "it posted with nowhere to post to")

    def test_it_does_nothing_when_disabled(self):
        http = FakeHTTP()
        self.assertFalse(self.build(http, enabled=False).send("t", "m"))
        self.assertEqual(http.calls, [])

    def test_the_topic_is_in_the_url_and_not_in_the_status(self):
        """The topic is the password on a public server."""
        http = FakeHTTP()
        n = self.build(http)
        n.send("Title", "Body")
        self.assertTrue(http.calls[0]["url"].endswith("/sekrit"))
        self.assertNotIn("sekrit", repr(n.status()))

    def test_a_message_with_no_image_is_the_body(self):
        http = FakeHTTP()
        self.build(http).send("Title", "a person at 03:14")
        call = http.calls[0]
        self.assertEqual(call["body"], b"a person at 03:14")
        self.assertNotIn("Message", call["headers"])

    def test_a_token_becomes_a_bearer_header(self):
        http = FakeHTTP()
        self.build(http, token="tk_x").send("t", "m")
        self.assertEqual(http.calls[0]["headers"]["Authorization"],
                         "Bearer tk_x")

    def test_a_4xx_is_not_retried(self):
        """A bad topic or token is a mistake, not weather. Retrying it only
        spends the bundle and delays the log line that explains it."""
        http = FakeHTTP(urllib.error.HTTPError("u", 403, "no", {}, None),
                        None, None)
        n = self.build(http, retries=3)
        self.assertFalse(n.send("t", "m"))
        self.assertEqual(len(http.calls), 1)
        self.assertIn("403", n.last_error)

    def test_a_network_failure_is_retried_then_reported(self):
        http = FakeHTTP(urllib.error.URLError("down"),
                        urllib.error.URLError("down"))
        n = self.build(http, retries=1)
        self.assertFalse(n.send("t", "m"))
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(n.failed, 1)

    def test_a_send_never_raises_at_the_caller(self):
        """This runs while an incident is closing. It may fail; it may not
        take the recording down with it."""
        class Boom:
            def __call__(self, *a, **kw):
                raise RuntimeError("something unforeseen")
        n = self.build(Boom())
        self.assertFalse(n.send("t", "m"))

    def test_an_oversized_still_is_dropped_not_sent(self):
        tmp = tempfile.mkdtemp()
        big = Path(tmp) / "big.jpg"
        big.write_bytes(b"\xff" * 5000)
        http = FakeHTTP()
        n = self.build(http, max_image_bytes=1000)
        n.send("t", "m", image=str(big))
        self.assertEqual(http.calls[0]["body"], b"m",
                         "it sent an image over the size limit")

    def test_non_ascii_does_not_break_the_headers(self):
        """ntfy headers are latin-1 on the wire and the site name is whatever
        the operator typed."""
        http = FakeHTTP()
        self.build(http, site="Tennisvereniging Weebosch — café")\
            .send("Café — person", "m")
        for v in http.calls[0]["headers"].values():
            v.encode("ascii")           # raises if it slipped through

    def test_counters_and_last_error(self):
        http = FakeHTTP(None, urllib.error.URLError("x"))
        n = self.build(http, retries=0)
        n.send("t", "m")
        n.send("t", "m")
        self.assertEqual((n.sent, n.failed), (1, 1))


class TestSendIncident(unittest.TestCase):
    def test_it_says_who_when_and_for_how_long(self):
        http = FakeHTTP()
        n = notify.Notifier({"enabled": True, "topic": "t", "site": "TVW",
                             "retries": 0}, opener=http)
        n.send_incident(incident(["person"]))
        body = http.calls[0]["body"].decode()
        self.assertIn("person", body)
        self.assertIn("03:14", body)
        self.assertIn("9s", body)
        self.assertIn("TVW", http.calls[0]["headers"]["Title"])

    def test_a_person_gets_the_louder_tag(self):
        http = FakeHTTP()
        n = notify.Notifier({"enabled": True, "topic": "t", "retries": 0},
                            opener=http)
        n.send_incident(incident(["person"]))
        self.assertEqual(http.calls[0]["headers"]["Tags"], "rotating_light")
