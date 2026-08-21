#!/usr/bin/env python3
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

"""The far end of the radio link: a small box at a board member's house.

Listens, verifies, acknowledges, and forwards to phones over that house's
internet. Also watches for silence.

That last part matters more than it sounds. With an always-on connection you
can check whether the system is up. Over a radio that only speaks when
something happens, hearing nothing for a week means either a quiet week or a
dead system, and there is no way to tell them apart. So the club sends one
small "alive" message a day, and this end raises the alarm when they stop --
which inverts the failure mode, so silence becomes the thing that gets
noticed.
"""

import argparse
import datetime as dt
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import link
import transports

log = logging.getLogger("receiver")


class Notifier:
    """Pushes to phones. ntfy by default: one small app, topic-based, no
    account, no bot token in a shared document, and nothing else can reach the
    topic to spam it."""

    def __init__(self, server="https://ntfy.sh", topic=None, timeout=10):
        self.server = server.rstrip("/")
        self.topic = topic
        self.timeout = timeout
        self.sent = 0
        self.failures = 0

    def push(self, title, message, priority="default", tags=""):
        if not self.topic:
            log.warning("no ntfy topic configured; would have sent: %s -- %s",
                        title, message)
            return False
        url = f"{self.server}/{urllib.parse.quote(self.topic)}"
        req = urllib.request.Request(
            url, data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                r.read()
            self.sent += 1
            log.info("pushed: %s -- %s", title, message)
            return True
        except (urllib.error.URLError, OSError) as e:
            self.failures += 1
            log.error("could not push to ntfy: %s", e)
            return False


class Receiver:
    def __init__(self, key, transport, notifier, state_path,
                 heartbeat_missed_alarm=2, clock=time.time,
                 site_name="RKNN surveillance"):
        self.key = key
        # What the alerts call themselves. The far end runs somewhere else
        # entirely, so it cannot read the site's config -- it is told.
        self.site_name = site_name
        self.transport = transport
        self.notifier = notifier
        self.state_path = Path(state_path)
        self.heartbeat_missed_alarm = int(heartbeat_missed_alarm)
        self._clock = clock
        self.state = self._load()
        self.guard = link.ReplayGuard(last=self.state.get("last_counter"))
        self.accepted = 0
        self.rejected = 0
        self.replays = 0

    # ----------------------------------------------------------------- state
    def _load(self):
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.state_path)

    # ------------------------------------------------------------- handling
    def handle(self, frame):
        """Decode, verify, act, acknowledge. Returns the message type or None."""
        try:
            msg_type, counter, body = link.decode(self.key, frame, direction=0)
        except link.LinkError as e:
            # Wrong key, corruption, or someone else's traffic on the band.
            self.rejected += 1
            log.debug("rejected a frame: %s", e)
            return None

        try:
            fresh = self.guard.check(counter)
        except link.ReplayError as e:
            self.replays += 1
            log.warning("REPLAY REJECTED: %s", e)
            return None
        if not fresh:
            # A retransmission of something already handled: acknowledge it
            # again so the sender stops repeating, but do not act twice.
            log.info("duplicate #%s, re-acknowledging", counter)
            self._ack(counter)
            return None

        self.guard.accept(counter)
        self.state["last_counter"] = counter
        self.accepted += 1

        if msg_type == link.ALERT:
            self._on_alert(counter, link.unpack_alert(body))
        elif msg_type == link.HEARTBEAT:
            self._on_heartbeat(counter, link.unpack_heartbeat(body))
        elif msg_type == link.TEST:
            self.notifier.push(f"{self.site_name}: test", "Test message received.",
                               priority="low", tags="white_check_mark")
        else:
            log.info("ignoring message type %s", msg_type)

        self._save()
        self._ack(counter)
        return msg_type

    def _ack(self, counter):
        try:
            frame = link.encode(self.key, link.ACK, counter,
                                link.pack_ack(counter, int(self._clock())),
                                direction=1)
            self.transport.send(frame)
        except Exception as e:
            log.warning("could not acknowledge #%s: %s", counter, e)

    def _on_alert(self, counter, a):
        when = dt.datetime.fromtimestamp(a["when"]).strftime("%H:%M")
        who = f"{a['count']} person" + ("s" if a["count"] != 1 else "")
        detail = (f"{who} at the club, {when}. "
                  f"Zone {a['zone']}, {a['duration_s']:.0f}s, "
                  f"confidence {a['confidence']:.0%}."
                  + ("" if a["pir"] else " (camera only, PIR did not agree)"))
        self.notifier.push(f"{self.site_name}: someone is at the club", detail,
                           priority="high", tags="rotating_light")
        self.state["last_alert"] = self._clock()

    def _on_heartbeat(self, counter, h):
        self.state["last_heartbeat"] = self._clock()
        self.state["last_heartbeat_detail"] = h
        log.info("heartbeat: %d event(s) today, disk %d%%, up %dh%s",
                 h["events_today"], h["disk_percent"], h["uptime_h"],
                 "" if not h["camera_bad"] else ", CAMERA UNHEALTHY")
        if self.state.pop("heartbeat_alarm_sent", None):
            self.notifier.push(f"{self.site_name}: back online",
                               "Heartbeats have resumed.", priority="default",
                               tags="white_check_mark")
        if h["disk_low"]:
            self.notifier.push(f"{self.site_name}: disk nearly full",
                               f"Disk at {h['disk_percent']}%.",
                               priority="default", tags="floppy_disk")
        if h["camera_bad"]:
            self.notifier.push(f"{self.site_name}: camera unhealthy",
                               "The recorder is not getting frames.",
                               priority="high", tags="warning")

    # ---------------------------------------------------------- the silence
    def check_silence(self, interval_s=86400.0):
        """Alarm when heartbeats stop. Returns True if it alarmed."""
        last = self.state.get("last_heartbeat")
        if last is None:
            return False
        missed = (self._clock() - last) / interval_s
        if missed < self.heartbeat_missed_alarm:
            return False
        if self.state.get("heartbeat_alarm_sent"):
            return False
        self.notifier.push(
            f"{self.site_name}: NO SIGNAL",
            f"No heartbeat for {missed:.1f} days. The system at the club may "
            f"be off, broken, or out of range.",
            priority="high", tags="warning")
        self.state["heartbeat_alarm_sent"] = True
        self._save()
        return True

    def run(self, poll_s=1.0):
        log.info("receiver listening")
        last_check = 0.0
        while True:
            frame = self.transport.receive(timeout=poll_s)
            if frame:
                try:
                    self.handle(frame)
                except Exception:
                    log.exception("failed to handle a frame")
            if self._clock() - last_check > 600:
                last_check = self._clock()
                try:
                    self.check_silence()
                except Exception:
                    log.exception("silence check failed")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--psk", help="pre-shared key (or set RKNN_LINK_PSK)")
    ap.add_argument("--ntfy-server", default="https://ntfy.sh")
    ap.add_argument("--ntfy-topic")
    ap.add_argument("--state", default="receiver-state.json")
    ap.add_argument("--missed", type=int, default=2,
                    help="raise the alarm after this many missed heartbeats")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    import os
    psk = args.psk or os.environ.get("RKNN_LINK_PSK")
    if not psk:
        print("no pre-shared key: pass --psk or set RKNN_LINK_PSK",
              file=sys.stderr)
        return 2
    if not args.ntfy_topic:
        print("warning: no --ntfy-topic, alerts will only be logged",
              file=sys.stderr)

    r = Receiver(link.derive_key(psk),
                 transports.SerialTransport(args.port, args.baud),
                 Notifier(args.ntfy_server, args.ntfy_topic),
                 args.state, heartbeat_missed_alarm=args.missed)
    try:
        r.run()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
