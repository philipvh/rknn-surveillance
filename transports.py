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

"""Ways to move a frame from one end of the link to the other.

The radio is attached over USB serial rather than wired to the SPI pins: an
ESP32-class board running a serial bridge is far less trouble than an SPI
radio driver on RK3588, keeps the radio code out of this process, and means
the module can be swapped without touching the Rock 5B.

The serial protocol is deliberately human-readable so it can be driven from a
terminal while debugging:

    ->  TX <hex>\\n           host asks the bridge to transmit
    <-  RX <hex> [rssi]\\n    bridge reports a received packet
    <-  OK / ERR <reason>    bridge acknowledges the TX request

Loopback and File transports exist so the whole queue, retry and heartbeat
story is testable with no radio at all, which is the plan's "testable entirely
on the bench" requirement.
"""

import logging
import queue
import threading
import time
from pathlib import Path

log = logging.getLogger("transport")


class Transport:
    def send(self, frame):
        raise NotImplementedError

    def receive(self, timeout=1.0):
        raise NotImplementedError

    def close(self):
        pass


class LoopbackTransport(Transport):
    """Two ends wired together in memory. Used to test the protocol itself."""

    def __init__(self, drop_rate=0.0, peer=None):
        self.inbox = queue.Queue()
        self.peer = peer
        self.drop_rate = drop_rate
        self.sent = []
        self._drop_next = 0

    @classmethod
    def pair(cls):
        a = cls()
        b = cls()
        a.peer, b.peer = b, a
        return a, b

    def drop_next(self, n=1):
        """Simulate a lost packet -- the case ACK and retry exist for."""
        self._drop_next = n

    def send(self, frame):
        self.sent.append(frame)
        if self._drop_next > 0:
            self._drop_next -= 1
            log.debug("dropping a frame on purpose")
            return
        if self.peer is not None:
            self.peer.inbox.put(frame)

    def receive(self, timeout=1.0):
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            return None


class FileTransport(Transport):
    """Writes frames to a file as hex, one per line.

    The 'no radio at all' mode: run the whole system, let it queue and
    transmit, and read the file afterwards to see exactly what would have gone
    on air. Useful long before any hardware is bought.
    """

    def __init__(self, path, inbox_path=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.inbox_path = Path(inbox_path) if inbox_path else None
        self._pos = 0

    def send(self, frame):
        with open(self.path, "a") as fh:
            fh.write(f"{time.time():.3f} TX {frame.hex()}\n")

    def receive(self, timeout=1.0):
        if not self.inbox_path or not self.inbox_path.exists():
            time.sleep(min(timeout, 0.05))
            return None
        with open(self.inbox_path) as fh:
            fh.seek(self._pos)
            line = fh.readline()
            self._pos = fh.tell()
        line = line.strip()
        if not line:
            time.sleep(min(timeout, 0.05))
            return None
        try:
            return bytes.fromhex(line.split()[-1])
        except ValueError:
            return None


class SerialTransport(Transport):
    """Talks to a radio bridge over USB serial."""

    def __init__(self, port, baud=115200, timeout=1.0):
        import serial                                   # optional dependency
        self.ser = serial.Serial(port, baud, timeout=timeout)
        self._lock = threading.Lock()
        # Anything already buffered predates us and cannot be replied to.
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        log.info("radio bridge on %s at %d baud", port, baud)

    def send(self, frame):
        with self._lock:
            self.ser.write(b"TX " + frame.hex().encode() + b"\n")
            self.ser.flush()

    def receive(self, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self.ser.readline()
            except Exception as e:
                log.warning("serial read failed: %s", e)
                return None
            if not raw:
                continue
            line = raw.decode("ascii", "replace").strip()
            if not line:
                continue
            if line.startswith("RX "):
                parts = line.split()
                try:
                    return bytes.fromhex(parts[1])
                except (IndexError, ValueError):
                    log.debug("unparseable RX line: %r", line)
            elif line.startswith("ERR"):
                log.warning("radio bridge reported: %s", line)
            # OK and anything else: ignore, keep waiting
        return None

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


def from_config(cfg):
    c = (cfg._get("link", default={}) or {}) if hasattr(cfg, "_get") else (cfg or {})
    mode = c.get("transport", "file")
    if mode == "serial":
        return SerialTransport(c.get("port", "/dev/ttyUSB0"),
                               int(c.get("baud", 115200)))
    if mode == "file":
        root = c.get("spool_root", "link")
        return FileTransport(Path(root) / "outgoing.hex",
                             Path(root) / "incoming.hex")
    if mode == "loopback":
        return LoopbackTransport()
    raise ValueError(f"unknown link.transport {mode!r}")
