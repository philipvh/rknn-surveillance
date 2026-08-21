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

"""Keeping a person roughly in frame, without the camera hunting.

This camera has no absolute-position command, so tracking has to be closed
loop: read the bounding box centre, compare it to the frame centre, and if the
error is outside a dead zone, pulse in that direction and re-measure.

Everything here is tuned against oscillation rather than for tightness. A dome
that hunts back and forth is worse than one that lags: it burns the motor
budget, it blurs every frame it records, and to anyone watching it looks
broken rather than watchful. So:

  * the dead zone is generous -- a quarter of the frame, not a few pixels;
  * a pulse only fires after the error has agreed with itself twice, which
    kills the response to one bad detection;
  * nothing moves during the cooldown after a pulse, so each correction is
    measured from a settled view rather than mid-swing;
  * pulses are capped per incident, so a bad night cannot grind the dome.

For vandalism the framing does not need to be good. It needs to be good enough
that the clip shows what happened, and visible enough that the person sees the
camera turn towards them -- which is the second rung of the deterrence ladder
and arguably worth more than the footage.
"""

import logging

from ptz import BudgetExceeded, PTZError

log = logging.getLogger("tracker")


class Tracker:
    def __init__(self, cfg, ptz, clock=None):
        c = (cfg._get("tracking", default={}) or {}) if hasattr(cfg, "_get") else (cfg or {})
        self.ptz = ptz
        self.enabled = bool(c.get("enabled", False))
        self.dead_zone = float(c.get("dead_zone", 0.25))
        self.min_pulse_s = float(c.get("min_pulse_s", 0.15))
        self.max_pulse_s = float(c.get("max_pulse_s", 0.5))
        self.cooldown_s = float(c.get("cooldown_s", 1.5))
        self.confirmations = int(c.get("confirmations", 2))
        self.max_pulses_per_incident = int(c.get("max_pulses_per_incident", 12))
        self.min_confidence = float(c.get("min_confidence", 0.7))
        self.vertical = bool(c.get("vertical", True))

        import time as _t
        self._clock = clock or _t.monotonic
        self._last_pulse_at = None
        self._pending = None            # (dx_sign, dy_sign) awaiting confirmation
        self._pending_count = 0
        self.pulses = 0
        self.reversals = 0
        self._last_direction = None

    # ------------------------------------------------------------- lifecycle
    def reset(self):
        """Called when an incident closes; the next one starts fresh."""
        self._pending = None
        self._pending_count = 0
        self._last_pulse_at = None
        self._last_direction = None
        self.pulses = 0

    def cooling_down(self):
        if self._last_pulse_at is None:
            return False
        return (self._clock() - self._last_pulse_at) < self.cooldown_s

    # ---------------------------------------------------------------- logic
    @staticmethod
    def pick_target(boxes, scores, min_confidence):
        """The largest confident box.

        Largest rather than most confident: for a group, the nearest person is
        the one worth framing, and area is a decent proxy for nearness.
        """
        best, best_area = None, 0.0
        for box, score in zip(boxes, scores):
            if score < min_confidence:
                continue
            area = max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])
            if area > best_area:
                best, best_area = box, area
        return best

    def error(self, box, width, height):
        """Offset of the box centre from the frame centre, as a fraction."""
        cx = (box[0] + box[2]) / 2.0
        cy = (box[1] + box[3]) / 2.0
        return ((cx - width / 2.0) / (width / 2.0),
                (cy - height / 2.0) / (height / 2.0))

    def _direction(self, dx, dy):
        horizontal = "right" if dx > 0 else "left"
        vertical = "down" if dy > 0 else "up"
        want_h = abs(dx) > self.dead_zone
        want_v = self.vertical and abs(dy) > self.dead_zone
        if want_h and want_v:
            return {("right", "up"): "topright", ("right", "down"): "bottomright",
                    ("left", "up"): "topleft", ("left", "down"): "bottomleft"
                    }[(horizontal, vertical)]
        if want_h:
            return horizontal
        if want_v:
            return vertical
        return None

    def _pulse_length(self, dx, dy):
        """Proportional, but bounded at both ends.

        Too short and the motor barely twitches; too long and the correction
        overshoots and the next one comes back the other way.
        """
        err = max(abs(dx), abs(dy))
        span = max(1e-6, 1.0 - self.dead_zone)
        frac = min(1.0, (err - self.dead_zone) / span)
        return self.min_pulse_s + frac * (self.max_pulse_s - self.min_pulse_s)

    def update(self, boxes, scores, width, height, source="auto"):
        """One measurement. Returns the direction pulsed, or None."""
        if not self.enabled or width <= 0 or height <= 0:
            return None
        if self.pulses >= self.max_pulses_per_incident:
            return None
        if self.cooling_down():
            return None

        box = self.pick_target(boxes, scores, self.min_confidence)
        if box is None:
            self._pending, self._pending_count = None, 0
            return None

        dx, dy = self.error(box, width, height)
        direction = self._direction(dx, dy)
        if direction is None:
            # Inside the dead zone: nothing to do, and forget any partial
            # agreement so a settled subject does not accumulate a nudge.
            self._pending, self._pending_count = None, 0
            return None

        # Require the same answer twice before spending a motor-second. One
        # frame of a wrong detection should not move the camera.
        if self._pending == direction:
            self._pending_count += 1
        else:
            self._pending, self._pending_count = direction, 1
        if self._pending_count < self.confirmations:
            return None

        duration = self._pulse_length(dx, dy)
        try:
            self.ptz.move(direction, source=source, deadline_s=duration)
        except BudgetExceeded as e:
            log.info("tracking gave way to the motor budget: %s", e)
            self.enabled = False        # for this incident; reset() restores it
            return None
        except PTZError as e:
            log.warning("tracking pulse failed: %s", e)
            return None

        if self._last_direction and direction != self._last_direction:
            self.reversals += 1
        self._last_direction = direction
        self._last_pulse_at = self._clock()
        self.pulses += 1
        self._pending, self._pending_count = None, 0
        log.info("tracking: %s for %.2fs (error %.2f, %.2f)",
                 direction, duration, dx, dy)
        return direction

    def status(self):
        return {"tracking_enabled": self.enabled, "pulses": self.pulses,
                "reversals": self.reversals, "cooling_down": self.cooling_down()}
