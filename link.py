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

"""The wire format for the point-to-point radio link.

The radio carries decisions, not data: an alert is a line of text's worth of
facts, and the snapshot and clip stay at the club. That keeps a message inside
a single small LoRa payload, which is what makes an on-demand link affordable.

Frame layout (30 bytes):

    0        version (high nibble) and message type (low nibble)
    1..4     counter, big-endian, in clear
    5..29    ChaCha20-Poly1305 over a 9-byte body (9 + 16 byte tag)

The counter travels in clear because the receiver needs it to rebuild the
nonce before it can decrypt. It leaks nothing beyond "how many messages have
been sent", and it is what makes replay detectable. It cannot be tampered
with: the nonce is derived from it, so altering the counter changes the nonce
and the tag stops verifying. Passing it as associated data as well is belt to
that braces -- keep both, since a future change to the nonce derivation would
otherwise silently remove the binding.

Why encrypted at all, for a burglar alarm on a tennis court: anyone with a
cheap receiver can listen. In clear, the link would tell a passer-by when the
club is empty, let them replay an old packet to exhaust the daily alert
budget, or let them forge an all-clear. Confidentiality and authenticity here
cost thirty lines and one library that ships with Debian.
"""

import hashlib
import struct

try:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    HAVE_AEAD = True
except ImportError:                                   # pragma: no cover
    ChaCha20Poly1305 = None
    HAVE_AEAD = False

VERSION = 1

# message types
ALERT = 1
HEARTBEAT = 2
ACK = 3
TEST = 4

TYPE_NAMES = {ALERT: "alert", HEARTBEAT: "heartbeat", ACK: "ack", TEST: "test"}

BODY_LEN = 9
TAG_LEN = 16
FRAME_LEN = 1 + 4 + BODY_LEN + TAG_LEN            # 30

# flag bits
F_PIR = 0x01
F_ARMED = 0x02
F_DISK_LOW = 0x04
F_CAMERA_BAD = 0x08
F_MULTIPLE = 0x10


class LinkError(Exception):
    pass


class ReplayError(LinkError):
    pass


def derive_key(psk):
    """32-byte key from whatever the operator typed in secrets.yaml."""
    if isinstance(psk, str):
        psk = psk.encode("utf-8")
    if not psk:
        raise LinkError("no pre-shared key configured for the radio link")
    return hashlib.sha256(b"rknn-link-v1|" + psk).digest()


def _nonce(counter, direction):
    """96-bit nonce, never transmitted.

    Derived from the counter plus a direction byte so the two ends can use the
    same key without ever colliding on a nonce -- reusing one with the same key
    would leak plaintext.
    """
    return struct.pack(">IB", counter, direction & 0xFF) + b"\x00" * 7


def encode(key, msg_type, counter, body, direction=0):
    if not HAVE_AEAD:
        raise LinkError(
            "python3-cryptography is not installed; the radio link refuses to "
            "run without it. Install it, or set link.allow_plaintext to accept "
            "an unencrypted link and understand what that means.")
    if len(body) != BODY_LEN:
        raise LinkError(f"body must be {BODY_LEN} bytes, got {len(body)}")
    if not 0 <= counter < 2 ** 32:
        raise LinkError("counter out of range")
    header = bytes([(VERSION << 4) | (msg_type & 0x0F)])
    ctr = struct.pack(">I", counter)
    aad = header + ctr
    ct = ChaCha20Poly1305(key).encrypt(_nonce(counter, direction), body, aad)
    return header + ctr + ct


def decode(key, frame, direction=0):
    """Returns (msg_type, counter, body). Raises LinkError if it is not ours."""
    if not HAVE_AEAD:
        raise LinkError("python3-cryptography is not installed")
    if len(frame) != FRAME_LEN:
        raise LinkError(f"frame is {len(frame)} bytes, expected {FRAME_LEN}")
    header = frame[0:1]
    version = header[0] >> 4
    msg_type = header[0] & 0x0F
    if version != VERSION:
        raise LinkError(f"unknown protocol version {version}")
    counter = struct.unpack(">I", frame[1:5])[0]
    try:
        body = ChaCha20Poly1305(key).decrypt(
            _nonce(counter, direction), frame[5:], header + frame[1:5])
    except Exception as e:
        # Wrong key, corrupt packet, or someone else's traffic. All the same
        # from here: it is not ours, so it never reaches the application.
        raise LinkError(f"authentication failed: {type(e).__name__}") from e
    return msg_type, counter, body


# ------------------------------------------------------------------ bodies

def pack_alert(when, zone=0, count=1, confidence=0.0, duration_s=0, flags=0):
    return struct.pack(
        ">IBBBBB",
        int(when) & 0xFFFFFFFF,
        zone & 0xFF,
        min(count, 255) & 0xFF,
        max(0, min(int(round(confidence * 100)), 100)),
        _log_seconds(duration_s),
        flags & 0xFF,
    )


def unpack_alert(body):
    ts, zone, count, conf, dur, flags = struct.unpack(">IBBBBB", body)
    return {
        "when": ts, "zone": zone, "count": count,
        "confidence": conf / 100.0,
        "duration_s": _unlog_seconds(dur),
        "flags": flags,
        "pir": bool(flags & F_PIR),
        "armed": bool(flags & F_ARMED),
        "disk_low": bool(flags & F_DISK_LOW),
        "camera_bad": bool(flags & F_CAMERA_BAD),
    }


def pack_heartbeat(when, events_today=0, disk_percent=0, flags=0, uptime_h=0):
    return struct.pack(
        ">IBBBH",
        int(when) & 0xFFFFFFFF,
        min(events_today, 255) & 0xFF,
        max(0, min(int(disk_percent), 100)),
        flags & 0xFF,
        min(int(uptime_h), 65535),
    )


def unpack_heartbeat(body):
    ts, ev, disk, flags, up = struct.unpack(">IBBBH", body)
    return {"when": ts, "events_today": ev, "disk_percent": disk,
            "flags": flags, "uptime_h": up,
            "armed": bool(flags & F_ARMED),
            "disk_low": bool(flags & F_DISK_LOW),
            "camera_bad": bool(flags & F_CAMERA_BAD)}


def pack_ack(counter, when, status=0):
    return struct.pack(">IIB", counter & 0xFFFFFFFF, int(when) & 0xFFFFFFFF,
                       status & 0xFF)


def unpack_ack(body):
    counter, ts, status = struct.unpack(">IIB", body)
    return {"acked": counter, "when": ts, "status": status}


def _log_seconds(s):
    """Durations that matter are 1s..2h, and one byte is all we can spare.

    Linear seconds would cap at 4 minutes; this keeps a second of resolution
    where it counts and degrades gracefully after that.
    """
    s = max(0, int(s))
    if s < 60:
        return s
    if s < 60 + 195 * 30:
        return min(60 + (s - 60) // 30, 254)
    return 255


def _unlog_seconds(v):
    if v < 60:
        return float(v)
    if v >= 255:
        return 6000.0
    return 60.0 + (v - 60) * 30.0


class ReplayGuard:
    """Rejects anything not strictly newer than the last message accepted.

    Without this, a packet captured off the air can be replayed to exhaust the
    daily alert budget -- or worse, an old heartbeat can be replayed to keep
    the far end believing a dead system is alive.
    """

    def __init__(self, last=None, window=0):
        self.last = last
        self.window = window        # allow this much reordering, 0 = strict

    def check(self, counter):
        if self.last is None:
            return True
        if counter > self.last:
            return True
        if self.window and (self.last - counter) <= self.window:
            # Tolerated as a retransmission, but never advances the ratchet.
            return False
        raise ReplayError(
            f"counter {counter} is not newer than {self.last} -- replay, "
            f"or the sender's state was reset")

    def accept(self, counter):
        if self.check(counter):
            self.last = counter
            return True
        return False
