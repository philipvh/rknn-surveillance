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

"""The PIR contact, read from a GPIO line.

The sensor already switches the floodlights and offers a dry contact, so this
is deliberately the simple version: the contact sits between a GPIO line and
ground with the SoC's internal pull-up, and a closed contact reads low.

Two decisions worth knowing about:

  * It polls rather than waiting on edges. libgpiod's edge API changed
    incompatibly between v1 and v2, while reading a value works the same in
    both, and a PIR's timescale is seconds to minutes -- polling twenty times
    a second loses nothing and works on every board this might land on.

  * The signal is treated as a level, not a pulse. The lights stay on for as
    long as the PIR's own timer runs, and how long that was is useful
    information about what happened outside.

Missing or unreadable hardware is never fatal. It logs, sets available=False,
and the rest of the system carries on detecting on the parked view.
"""

from dataclasses import dataclass
import logging
import threading
import time

log = logging.getLogger("trigger")


@dataclass(frozen=True)
class TriggerEvent:
    kind: str                 # 'active' | 'inactive' | 'stuck' | 'error'
    at: float                 # wall clock
    duration: float = 0.0     # how long it was active, for 'inactive'/'stuck'
    detail: str = ""


class BackendUnavailable(Exception):
    pass


# ---------------------------------------------------------------- backends

class GpiodV2Backend:
    """libgpiod 2.x python bindings."""

    name = "libgpiod-v2"

    def __init__(self, chip, line, bias="pull-up"):
        import gpiod
        from gpiod.line import Bias, Direction
        bias_map = {"pull-up": Bias.PULL_UP, "pull-down": Bias.PULL_DOWN,
                    "disabled": Bias.DISABLED, "as-is": Bias.AS_IS}
        self._line = int(line)
        path = chip if str(chip).startswith("/") else f"/dev/{chip}"
        self._req = gpiod.request_lines(
            path, consumer="rknn-trigger",
            config={self._line: gpiod.LineSettings(
                direction=Direction.INPUT,
                bias=bias_map.get(bias, Bias.PULL_UP))})

    def read(self):
        from gpiod.line import Value
        return 1 if self._req.get_value(self._line) == Value.ACTIVE else 0

    def close(self):
        try:
            self._req.release()
        except Exception:
            pass


class GpiodV1Backend:
    """libgpiod 1.x python bindings (Debian bookworm's python3-libgpiod)."""

    name = "libgpiod-v1"

    def __init__(self, chip, line, bias="pull-up"):
        import gpiod
        self._chip = gpiod.Chip(str(chip))
        self._line_obj = self._chip.get_line(int(line))
        flags = 0
        flag_name = {"pull-up": "LINE_REQ_FLAG_BIAS_PULL_UP",
                     "pull-down": "LINE_REQ_FLAG_BIAS_PULL_DOWN",
                     "disabled": "LINE_REQ_FLAG_BIAS_DISABLE"}.get(bias)
        if flag_name and hasattr(gpiod, flag_name):
            flags = getattr(gpiod, flag_name)
        elif flag_name:
            # bias flags arrived in libgpiod 1.5; without them an external
            # pull-up resistor is required, so say so rather than fail quietly.
            log.warning("this libgpiod has no bias flags -- fit an external "
                        "%s resistor, or the line will float", bias)
        self._line_obj.request(consumer="rknn-trigger",
                               type=gpiod.LINE_REQ_DIR_IN, flags=flags)

    def read(self):
        return int(self._line_obj.get_value())

    def close(self):
        for obj, meth in ((self._line_obj, "release"), (self._chip, "close")):
            try:
                getattr(obj, meth)()
            except Exception:
                pass


def open_backend(chip, line, bias="pull-up"):
    """First backend that works, in order of preference."""
    errors = []
    for cls in (GpiodV2Backend, GpiodV1Backend):
        try:
            b = cls(chip, line, bias)
            log.info("GPIO via %s on %s line %s (bias %s)",
                     cls.name, chip, line, bias)
            return b
        except Exception as e:
            errors.append(f"{cls.name}: {type(e).__name__}: {e}")
    raise BackendUnavailable("; ".join(errors) or "no backend available")


# ----------------------------------------------------------------- watcher

class TriggerInput(threading.Thread):
    def __init__(self, cfg, backend=None, on_event=None, clock=time.monotonic,
                 wall_clock=time.time):
        super().__init__(daemon=True, name="trigger")
        c = (cfg._get("trigger_input", default={}) or {}) if hasattr(cfg, "_get") else cfg
        self.enabled = bool(c.get("enabled", True))
        self.chip = c.get("chip", "gpiochip0")
        self.line = c.get("line", 17)
        self.active_low = bool(c.get("active_low", True))
        self.bias = c.get("bias", "pull-up")
        self.poll_interval_s = float(c.get("poll_interval_s", 0.05))
        self.debounce_s = float(c.get("debounce_s", 0.2))
        self.min_active_s = float(c.get("min_active_s", 0.3))
        self.stuck_after_s = float(c.get("stuck_after_s", 1800))

        self._clock = clock
        self._wall = wall_clock
        self._on_event = on_event
        self._stopping = threading.Event()
        self._lock = threading.Lock()

        self.backend = backend
        self.available = backend is not None
        self.error = None

        self._active = False
        self._active_since = None
        self._stuck_reported = False

        # debounce bookkeeping
        self._candidate = None
        self._candidate_since = 0.0

        self.activations = 0
        self.rejected_blips = 0
        self.read_errors = 0

    # -------------------------------------------------------------- opening
    def open(self):
        if self.backend is not None:
            self.available = True
            return True
        if not self.enabled:
            log.info("trigger input disabled in config; detection will run on "
                     "the parked view alone")
            return False
        try:
            self.backend = open_backend(self.chip, self.line, self.bias)
            self.available = True
            return True
        except BackendUnavailable as e:
            # Deliberately not fatal: a wiring or driver fault should degrade
            # the system, not stop it.
            self.error = str(e)
            self.available = False
            log.warning("no GPIO trigger (%s). Carrying on without it -- "
                        "detection still runs on the parked view. "
                        "Check: gpiodetect; gpioinfo; gpiomon %s %s",
                        e, self.chip, self.line)
            return False

    # ---------------------------------------------------------------- state
    @property
    def active(self):
        with self._lock:
            return self._active

    def active_duration(self):
        with self._lock:
            if not self._active or self._active_since is None:
                return 0.0
            return self._clock() - self._active_since

    def status(self):
        with self._lock:
            return {
                "available": self.available,
                "active": self._active,
                "active_for_s": round(
                    self._clock() - self._active_since, 1)
                if self._active and self._active_since else 0.0,
                "activations": self.activations,
                "rejected_blips": self.rejected_blips,
                "read_errors": self.read_errors,
                "error": self.error,
            }

    # --------------------------------------------------------------- events
    def _emit(self, ev):
        log.info("trigger %s%s", ev.kind,
                 f" after {ev.duration:.1f}s" if ev.duration else "")
        if self._on_event:
            try:
                self._on_event(ev)
            except Exception:
                log.exception("trigger callback failed")

    # ---------------------------------------------------------------- logic
    def _read_logical(self):
        raw = self.backend.read()
        return (raw == 0) if self.active_low else (raw == 1)

    def poll_once(self, now=None):
        """One debounced sample. Separated out so it can be driven by tests."""
        now = self._clock() if now is None else now
        try:
            reading = self._read_logical()
        except Exception as e:
            self.read_errors += 1
            if self.read_errors in (1, 10, 100) or self.read_errors % 1000 == 0:
                log.warning("GPIO read failed (%d so far): %s",
                            self.read_errors, e)
            return

        with self._lock:
            settled = self._active

        if reading != settled:
            # A change must persist before it counts -- relay contacts bounce.
            if self._candidate != reading:
                self._candidate = reading
                self._candidate_since = now
            elif (now - self._candidate_since) >= self.debounce_s:
                self._commit(reading, now)
                self._candidate = None
        else:
            self._candidate = None

        # stuck-on detection
        with self._lock:
            active, since = self._active, self._active_since
        if active and since is not None and not self._stuck_reported:
            held = now - since
            if held >= self.stuck_after_s:
                self._stuck_reported = True
                self._emit(TriggerEvent(
                    "stuck", self._wall(), held,
                    f"line has been active for {held/60:.0f} min -- a stuck "
                    f"relay or a light left on? Ignoring it as a fresh trigger."))

    def _commit(self, now_active, now):
        if now_active:
            with self._lock:
                self._active = True
                self._active_since = now
                self.activations += 1
            self._stuck_reported = False
            self._emit(TriggerEvent("active", self._wall()))
        else:
            with self._lock:
                since = self._active_since
                self._active = False
                self._active_since = None
            held = (now - since) if since is not None else 0.0
            if held < self.min_active_s:
                self.rejected_blips += 1
                log.debug("ignoring a %.2fs blip (minimum is %.2fs)",
                          held, self.min_active_s)
                return
            self._emit(TriggerEvent("inactive", self._wall(), held))

    # ----------------------------------------------------------------- loop
    def run(self):
        if not self.open():
            return
        log.info("watching %s line %s for the PIR contact", self.chip, self.line)
        while not self._stopping.is_set():
            self.poll_once()
            self._stopping.wait(self.poll_interval_s)
        if self.backend:
            self.backend.close()
        log.info("trigger watcher stopped")

    def stop(self):
        self._stopping.set()
