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

"""Turning alert decisions into queued radio messages, and the daily heartbeat.

Deliberately inert by default. Nothing leaves the club while
alerts.shadow_only is true, which stays true until a labelled fortnight says
the volume is acceptable. Queueing still happens if link.enabled is set with
shadow mode off, so the two switches read as: "is the policy trusted yet?" and
"is there anything to carry it?".
"""

import datetime as dt
import logging
import shutil
import threading
import time

import link
from outbox import Outbox, Sender

log = logging.getLogger("uplink")


def zone_id(preset, presets):
    """One byte for where the camera was looking."""
    try:
        return (list(presets).index(preset) + 1) & 0xFF
    except (ValueError, AttributeError):
        return 0


class Uplink:
    def __init__(self, cfg, transport=None, clock=time.time):
        c = (cfg._get("link", default={}) or {})
        self.cfg = cfg
        self.enabled = bool(c.get("enabled", False))
        self.shadow_only = bool(cfg._get("alerts", "shadow_only", default=True))
        self.presets = list(cfg._get("ptz", "scan_presets", default=[]) or [])
        self.heartbeat_hour = int(c.get("heartbeat_hour", 6))
        self._clock = clock
        self._last_heartbeat_day = None
        self.sender = None
        self.outbox = None
        self.transport = transport
        self.started_at = clock()

        if not self.enabled:
            log.info("radio link disabled in config; nothing will be sent")
            return
        psk = cfg.link_psk
        if not psk:
            log.error("link.enabled is set but no pre-shared key is "
                      "configured; refusing to run an unauthenticated link")
            self.enabled = False
            return
        self.key = link.derive_key(psk)
        root = cfg.resolve(c.get("spool_root", "link"))
        self.outbox = Outbox(root,
                             max_messages=int(c.get("max_queued", 500)),
                             max_age_days=float(c.get("max_queue_age_days", 30)))
        if self.transport is None:
            import transports
            self.transport = transports.from_config(cfg)
        self.sender = Sender(cfg, self.outbox, self.transport, self.key)

    def start(self):
        if self.sender:
            self.sender.start()
            log.info("radio link active, %d message(s) already queued",
                     self.outbox.depth())

    def stop(self):
        if self.sender:
            self.sender.stop()
        if self.transport:
            try:
                self.transport.close()
            except Exception:
                pass

    # ------------------------------------------------------------- alerting
    def on_decision(self, incident, decision, health=None):
        """Called for every incident, whatever the policy decided."""
        if not decision.would_alert:
            return None
        if self.shadow_only:
            log.info("shadow mode: not sending an alert that passed every gate")
            return None
        if not self.outbox:
            return None
        flags = 0
        if incident.pir_corroborated:
            flags |= link.F_PIR
        flags |= link.F_ARMED
        if incident.max_count > 1:
            flags |= link.F_MULTIPLE
        for bit, key in ((link.F_DISK_LOW, "disk_low"),
                         (link.F_CAMERA_BAD, "camera_bad")):
            if (health or {}).get(key):
                flags |= bit
        body = link.pack_alert(
            when=incident.first_seen.timestamp(),
            zone=zone_id(incident.preset, self.presets),
            count=incident.max_count or 1,
            confidence=incident.max_confidence,
            duration_s=incident.duration_s,
            flags=flags)
        return self.outbox.put(link.ALERT, body,
                               meta={"summary": incident.summary()})

    # ------------------------------------------------------------ heartbeat
    def maybe_heartbeat(self, health=None, now=None):
        """One 'still alive' message a day.

        With an alert-only link, silence is ambiguous: a quiet week and a dead
        system look identical. This is what lets the far end tell them apart.
        """
        if not self.outbox:
            return None
        now = now or dt.datetime.now()
        if now.hour < self.heartbeat_hour:
            return None
        if self._last_heartbeat_day == now.date():
            return None
        self._last_heartbeat_day = now.date()
        h = health or {}
        flags = link.F_ARMED if h.get("armed") else 0
        if h.get("disk_low"):
            flags |= link.F_DISK_LOW
        if h.get("camera_bad"):
            flags |= link.F_CAMERA_BAD
        body = link.pack_heartbeat(
            when=now.timestamp(),
            events_today=h.get("events_today", 0),
            disk_percent=h.get("disk_percent", 0),
            flags=flags,
            uptime_h=int((self._clock() - self.started_at) / 3600))
        log.info("queueing the daily heartbeat")
        return self.outbox.put(link.HEARTBEAT, body)

    def send_test(self):
        if not self.outbox:
            return None
        return self.outbox.put(link.TEST, link.pack_alert(time.time()))

    def status(self):
        if not self.sender:
            return {"link": "disabled"}
        s = self.sender.status()
        s["shadow_only"] = self.shadow_only
        return s


def disk_health(path, warn_percent=90):
    try:
        u = shutil.disk_usage(str(path))
        pct = int(u.used / u.total * 100)
        return {"disk_percent": pct, "disk_low": pct >= warn_percent}
    except OSError:
        return {"disk_percent": 0, "disk_low": False}
