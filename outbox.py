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

"""The outbound queue and the sender that drains it over the radio.

Designed for a link that is usually absent and always unreliable. Nothing is
lost by the radio being down, off, or not yet bought: messages accumulate on
disk, bounded, and go out when something can carry them. The same queue that
was written for "no uplink yet" is exactly what an intermittent link needs.

Transmission is paced. The licence-free 868 MHz band limits how much of the
time you may transmit, which at slower settings works out to one short message
every minute or two. That is a constraint, but it is the same ceiling the
alert policy already imposes for its own reasons -- so the radio enforces the
anti-flood rule in hardware: even a total logic failure cannot spam anyone,
because the transmitter will not let it.
"""

import json
import logging
import threading
import time
from pathlib import Path

import link

log = logging.getLogger("outbox")


class Message:
    def __init__(self, counter, msg_type, body_hex, created, attempts=0,
                 last_attempt=0.0, meta=None):
        self.counter = counter
        self.msg_type = msg_type
        self.body = bytes.fromhex(body_hex) if isinstance(body_hex, str) else body_hex
        self.created = created
        self.attempts = attempts
        self.last_attempt = last_attempt
        self.meta = meta or {}

    def to_dict(self):
        return {"counter": self.counter, "msg_type": self.msg_type,
                "body_hex": self.body.hex(), "created": self.created,
                "attempts": self.attempts, "last_attempt": self.last_attempt,
                "meta": self.meta}

    @classmethod
    def from_dict(cls, d):
        return cls(d["counter"], d["msg_type"], d["body_hex"], d["created"],
                   d.get("attempts", 0), d.get("last_attempt", 0.0),
                   d.get("meta"))

    def __repr__(self):
        return (f"<{link.TYPE_NAMES.get(self.msg_type, self.msg_type)} "
                f"#{self.counter} attempts={self.attempts}>")


class Outbox:
    """One file per message, so a power cut mid-write cannot corrupt the queue."""

    def __init__(self, root, max_messages=500, max_age_days=30):
        self.root = Path(root)
        self.spool = self.root / "queue"
        self.state_path = self.root / "state.json"
        self.max_messages = int(max_messages)
        self.max_age_s = float(max_age_days) * 86400
        self._lock = threading.RLock()
        self.spool.mkdir(parents=True, exist_ok=True)
        self._counter = self._load_counter()

    # ------------------------------------------------------------- counter
    def _load_counter(self):
        try:
            return int(json.loads(self.state_path.read_text())["counter"])
        except (OSError, ValueError, KeyError):
            return 0

    def _save_counter(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"counter": self._counter}))
        tmp.replace(self.state_path)

    def next_counter(self):
        """Monotonic across restarts.

        Persisted before use, not after: if the box loses power between
        handing out a counter and sending it, the worst case is a gap in the
        sequence. Handing the same counter out twice would reuse a nonce,
        which is the one failure this must not have.
        """
        with self._lock:
            self._counter += 1
            self._save_counter()
            return self._counter

    # --------------------------------------------------------------- queue
    def put(self, msg_type, body, meta=None):
        with self._lock:
            msg = Message(self.next_counter(), msg_type, body, time.time(),
                          meta=meta or {})
            self._write(msg)
            self._enforce_bounds()
        log.info("queued %r", msg)
        return msg

    def _path(self, counter):
        return self.spool / f"{counter:010d}.json"

    def _write(self, msg):
        p = self._path(msg.counter)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(msg.to_dict()))
        tmp.replace(p)

    def pending(self):
        out = []
        for p in sorted(self.spool.glob("*.json")):
            try:
                out.append(Message.from_dict(json.loads(p.read_text())))
            except (OSError, ValueError) as e:
                log.warning("dropping unreadable queue entry %s: %s", p.name, e)
                p.unlink(missing_ok=True)
        return out

    def peek(self):
        p = self.pending()
        return p[0] if p else None

    def ack(self, counter):
        with self._lock:
            self._path(counter).unlink(missing_ok=True)

    def record_attempt(self, msg):
        with self._lock:
            msg.attempts += 1
            msg.last_attempt = time.time()
            if self._path(msg.counter).exists():
                self._write(msg)

    def _enforce_bounds(self):
        """An offline year must not fill the disk, and stale alerts are noise.

        Oldest goes first: a burglary from three weeks ago that nobody has
        heard about by now is not news.
        """
        msgs = self.pending()
        now = time.time()
        for m in msgs:
            if self.max_age_s and (now - m.created) > self.max_age_s:
                log.info("dropping %r, older than the retention window", m)
                self._path(m.counter).unlink(missing_ok=True)
        msgs = self.pending()
        if len(msgs) > self.max_messages:
            for m in msgs[:len(msgs) - self.max_messages]:
                log.warning("queue full, dropping oldest: %r", m)
                self._path(m.counter).unlink(missing_ok=True)

    def depth(self):
        return len(list(self.spool.glob("*.json")))


class Sender(threading.Thread):
    """Drains the outbox, one message at a time, waiting for an ACK."""

    def __init__(self, cfg, outbox, transport, key, clock=time.monotonic):
        super().__init__(daemon=True, name="link-sender")
        c = (cfg._get("link", default={}) or {}) if hasattr(cfg, "_get") else (cfg or {})
        self.outbox = outbox
        self.transport = transport
        self.key = key
        self.ack_timeout_s = float(c.get("ack_timeout_s", 8.0))
        self.max_attempts = int(c.get("max_attempts", 4))
        self.min_tx_interval_s = float(c.get("min_tx_interval_s", 90.0))
        self.poll_interval_s = float(c.get("poll_interval_s", 1.0))
        self.require_ack = bool(c.get("require_ack", True))
        self._clock = clock
        self._stopping = threading.Event()
        self._last_tx = None

        self.sent = 0
        self.failed = 0
        self.acked = 0

    def stop(self):
        self._stopping.set()

    def duty_ready(self):
        if self._last_tx is None:
            return True
        return (self._clock() - self._last_tx) >= self.min_tx_interval_s

    def send_once(self, msg):
        """One transmission plus its ACK wait. Returns True if acknowledged."""
        frame = link.encode(self.key, msg.msg_type, msg.counter, msg.body,
                            direction=0)
        self.outbox.record_attempt(msg)
        self.transport.send(frame)
        self._last_tx = self._clock()
        self.sent += 1
        log.info("sent %r (%d bytes, attempt %d)", msg, len(frame), msg.attempts)

        if not self.require_ack:
            self.outbox.ack(msg.counter)
            return True

        deadline = self._clock() + self.ack_timeout_s
        # A transport that returns immediately would spin this loop hot, so
        # bound the iterations as well as the wall time.
        budget = int(self.ack_timeout_s / 0.05) + 20
        while self._clock() < deadline and budget > 0:
            budget -= 1
            remaining = max(0.05, deadline - self._clock())
            reply = self.transport.receive(timeout=remaining)
            if not reply:
                continue
            try:
                msg_type, counter, body = link.decode(self.key, reply,
                                                      direction=1)
            except link.LinkError as e:
                log.debug("ignoring an undecodable reply: %s", e)
                continue
            if msg_type != link.ACK:
                continue
            info = link.unpack_ack(body)
            if info["acked"] == msg.counter:
                self.outbox.ack(msg.counter)
                self.acked += 1
                log.info("acknowledged %r", msg)
                return True
            log.debug("ack for #%s while waiting for #%s",
                      info["acked"], msg.counter)
        log.warning("no acknowledgement for %r within %.1fs",
                    msg, self.ack_timeout_s)
        return False

    def pump(self):
        """One iteration. Separated so tests can drive it without a thread."""
        msg = self.outbox.peek()
        if msg is None:
            return False
        if not self.duty_ready():
            return False
        if msg.attempts >= self.max_attempts:
            # Keep it queued rather than dropping it: the link may come back,
            # and a burglary alert is worth more than a tidy queue.
            if msg.attempts == self.max_attempts:
                self.failed += 1
                log.error("%r has failed %d times; it stays queued until the "
                          "link recovers", msg, msg.attempts)
                self.outbox.record_attempt(msg)
            return False
        try:
            return self.send_once(msg)
        except Exception as e:
            log.warning("send failed for %r: %s", msg, e)
            self.outbox.record_attempt(msg)
            return False

    def run(self):
        log.info("sender running: ack timeout %.0fs, at most one message every "
                 "%.0fs", self.ack_timeout_s, self.min_tx_interval_s)
        while not self._stopping.is_set():
            try:
                self.pump()
            except Exception:
                log.exception("sender iteration failed")
            self._stopping.wait(self.poll_interval_s)

    def status(self):
        return {"queued": self.outbox.depth(), "sent": self.sent,
                "acked": self.acked, "failed": self.failed,
                "duty_ready": self.duty_ready()}
