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

"""PTZ control with the safety properties attached.

Vendor-neutral: the camera's own protocol lives in the camera package, and
this module is what makes any of them safe to drive.

Movement on these cameras is *continuous*: a command runs until something
stops it. That single fact drives the whole design here. If the process that
started a move dies, or the wall panel's wifi drops with a finger on the
button, the camera keeps turning until it grinds against its mechanical
limit. So:

  * No move is ever issued without a deadline. A watchdog thread stops the
    camera when the deadline passes, and the caller must keep refreshing it
    for as long as it wants motion.
  * A stop that fails is retried until it succeeds. Swallowing a failed stop
    is the one bug in this module that would break a motor.
  * The driver sends a stop when it starts up, before anything else. If a
    previous instance was killed mid-move, this is what rescues the camera --
    an in-process handler cannot run after SIGKILL, but the next start can.
  * A rolling motor-second budget refuses movement that would run the motors
    harder than the manual's own advice against long cruises allows.

The backend and its transport are both injectable, so all of the above can be
tested without a camera -- and so a new camera inherits every guarantee above
by implementing three methods rather than by being careful.
"""

from collections import deque
from dataclasses import dataclass
import atexit
import logging
import signal
import threading
import time

# Cap is re-exported: callers ask ptz.supports(Cap.PRESETS) and should not
# need to know which module the flags live in.
from camera.base import Cap, CameraError, NotSupported, UrllibTransport  # noqa: F401
from camera.registry import make_backend

log = logging.getLogger("ptz")

# The vendor's protocol lives in the camera package. This module is the safety
# layer above it: deadlines, the motor budget, retry-until-confirmed stops and
# the watchdog. It knows no command names at all, which is what lets a second
# camera be a subclass rather than a fork.
#
# PTZError is what this module has always raised, and a backend failure is one
# of those, so the two are the same class rather than a translation layer.
PTZError = CameraError


class BudgetExceeded(PTZError):
    pass


# ----------------------------------------------------------------------- budget

@dataclass
class BudgetDecision:
    allowed: bool
    reason: str = ""


class MotorBudget:
    """Rolling window of motor-seconds, plus a floor on how often an automatic
    scan may start.

    The SD2X manual warns against long cruises for motor life and thermal
    reasons. This is that warning made enforceable: when the budget is spent,
    movement is refused and logged rather than queued, so a stuck PIR or a
    branch waving in a floodlight cannot grind the dome down over a season.
    """

    def __init__(self, auto_s_per_hour=90.0, manual_s_per_hour=300.0,
                 min_scan_interval_s=60.0, window_s=3600.0, clock=time.monotonic):
        self.limits = {"auto": float(auto_s_per_hour),
                       "manual": float(manual_s_per_hour)}
        self.min_scan_interval_s = float(min_scan_interval_s)
        self.window_s = float(window_s)
        self._clock = clock
        self._spent = deque()          # (t, seconds, source)
        self._last_scan_at = None
        self._lock = threading.Lock()

    def _prune(self, now):
        cutoff = now - self.window_s
        while self._spent and self._spent[0][0] < cutoff:
            self._spent.popleft()

    def spent(self, source=None):
        now = self._clock()
        with self._lock:
            self._prune(now)
            return sum(s for _, s, src in self._spent
                       if source is None or src == source)

    def remaining(self, source="auto"):
        return max(0.0, self.limits[source] - self.spent(source))

    def check(self, seconds, source="auto", is_scan_start=False):
        if source not in self.limits:
            return BudgetDecision(False, f"unknown budget source {source!r}")
        now = self._clock()
        with self._lock:
            self._prune(now)
            used = sum(s for _, s, src in self._spent if src == source)
            limit = self.limits[source]
            if used + seconds > limit:
                return BudgetDecision(
                    False,
                    f"{source} motor budget spent: {used:.1f}s of {limit:.0f}s "
                    f"used in the last {self.window_s/60:.0f} min, "
                    f"{seconds:.1f}s requested")
            if is_scan_start and self._last_scan_at is not None:
                since = now - self._last_scan_at
                if since < self.min_scan_interval_s:
                    return BudgetDecision(
                        False,
                        f"last scan was {since:.0f}s ago, minimum interval is "
                        f"{self.min_scan_interval_s:.0f}s")
        return BudgetDecision(True)

    def record(self, seconds, source="auto", is_scan_start=False):
        now = self._clock()
        with self._lock:
            self._spent.append((now, max(0.0, float(seconds)), source))
            if is_scan_start:
                self._last_scan_at = now

    def note_scan(self):
        with self._lock:
            self._last_scan_at = self._clock()


# -------------------------------------------------------------------------- PTZ

class PTZ:
    def __init__(self, cfg, transport=None, clock=time.monotonic,
                 install_signal_handlers=True, stop_on_start=True,
                 backend=None):
        self.cfg = cfg
        self.p = (cfg._get("ptz", default={}) or {})
        http = self.p.get("http", {}) or {}
        # A camera that is not there must not be commanded. Without this the
        # watchdog retries a stop against an unreachable host forever and
        # floods the log.
        self.enabled = bool(self.p.get("enabled", True))

        self.transport = transport or UrllibTransport()
        self._clock = clock

        self.timeout = float(http.get("timeout_s", 5.0))
        # The camera's own protocol, chosen by camera.type. Everything below
        # this line is vendor-neutral; everything vendor-specific is in there.
        self.backend = backend or make_backend(
            cfg, transport=self.transport, timeout=self.timeout)
        self.stop_timeout = float(http.get("stop_timeout_s", 3.0))
        self.stop_retries = int(http.get("stop_retries", 5))
        # Base for the exponential backoff between failed stop sequences.
        self.stop_backoff_base_s = float(http.get("stop_backoff_base_s", 1.0))
        self.stop_backoff_max_s = float(http.get("stop_backoff_max_s", 60.0))

        self.move_deadline_s = float(self.p.get("move_deadline_s", 0.6))
        self.watchdog_interval_s = float(self.p.get("watchdog_interval_s", 0.1))
        self.max_continuous_move_s = float(self.p.get("max_continuous_move_s", 10.0))
        self.preset_estimate_s = float(self.p.get("preset_move_estimate_s", 3.0))

        b = self.p.get("budget", {}) or {}
        self.budget = MotorBudget(
            auto_s_per_hour=b.get("auto_seconds_per_hour", 90),
            manual_s_per_hour=b.get("manual_seconds_per_hour", 300),
            min_scan_interval_s=b.get("min_scan_interval_s", 60),
            clock=clock)

        self._lock = threading.RLock()
        # Serialises stop sequences: the watchdog and a caller can both
        # decide to stop at the same instant, and two retry loops
        # hammering the camera in parallel is waste, not extra safety.
        self._stop_lock = threading.Lock()
        self._moving = False           # a continuous command is outstanding
        self._move_kind = None         # 'ptz' | 'zoom'
        self._deadline = 0.0
        self._move_started = 0.0
        self._move_source = "auto"
        self._stop_pending = False     # a stop that has not been confirmed
        self._stopped = False          # the camera has confirmed it is still
        self._closed = False

        self.last_stopped_at = self._clock()
        self.moves = 0
        self.watchdog_stops = 0
        self.failed_stops = 0

        # Backoff for stops that cannot be confirmed, so an unreachable
        # camera costs one retry a minute rather than one every 100 ms.
        self._stop_fail_streak = 0
        self._retry_stop_after = 0.0

        if not self.enabled:
            log.info("PTZ is disabled in config; no commands will be sent")
            return

        self._wd = threading.Thread(target=self._watchdog, daemon=True,
                                    name="ptz-watchdog")
        self._wd.start()

        # Rescue a camera left turning by a previous instance that was killed
        # before it could stop. This is the layer that actually covers SIGKILL.
        if stop_on_start:
            try:
                self.stop(reason="startup")
            except PTZError as e:
                log.warning("startup stop failed (%s); watchdog will retry", e)

        if install_signal_handlers:
            self._install_handlers()

    # ------------------------------------------------------------- lifecycle
    def _install_handlers(self):
        atexit.register(self._emergency_stop)
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                prev = signal.getsignal(sig)

                def handler(signum, frame, _prev=prev):
                    log.info("signal %s -- stopping the camera before exit", signum)
                    self._emergency_stop()
                    if callable(_prev) and _prev not in (signal.SIG_IGN, signal.SIG_DFL):
                        _prev(signum, frame)
                    else:
                        raise KeyboardInterrupt

                signal.signal(sig, handler)
            except (ValueError, OSError):
                # not the main thread, or no such signal on this platform
                pass

    def _emergency_stop(self):
        if self._closed:
            return
        try:
            self.stop(reason="shutdown")
        except Exception as e:
            log.error("could not stop the camera on shutdown: %s", e)

    def close(self):
        self._emergency_stop()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ----------------------------------------------------------------- state
    @property
    def moving(self):
        with self._lock:
            return self._moving

    def since_last_move(self):
        with self._lock:
            if self._moving:
                return 0.0
            return self._clock() - self.last_stopped_at

    def settled(self):
        """False while the motors run and for settle_s afterwards.

        Frames taken while the camera is panning are motion-blurred, and the
        trigger logic has no concept of the view having changed underneath it.
        """
        return self.since_last_move() >= float(self.p.get("settle_s", 0.5))

    # -------------------------------------------------------------- backend
    def _cam(self):
        """The backend, or a refusal if PTZ is switched off.

        A camera that is not there, or deliberately disabled, must not be
        commanded: without this the watchdog would retry a stop against an
        unreachable host for ever and fill the log.
        """
        if not self.enabled:
            raise PTZError("PTZ is disabled in config")
        return self.backend

    def _call(self, cmd, timeout=None, **extra):
        """Raw vendor command, for probing tools only.

        Not used by anything in this module -- it exists so ptz_cli can ask a
        camera what it supports. Backends that do not expose a raw call say so
        rather than pretending.
        """
        cam = self._cam()
        if not hasattr(cam, "call"):
            raise NotSupported(
                f"the {cam.name} backend has no raw command interface")
        return cam.call(cmd, timeout=timeout, **extra)

    # ---------------------------------------------------------------- moving
    def _begin_move(self, kind, direction, source, deadline_s):
        with self._lock:
            self._moving = True
            self._move_kind = kind
            self._move_source = source
            self._move_started = self._clock()
            self._deadline = self._clock() + deadline_s
            self._stopped = False
            self.moves += 1
        cam = self._cam()
        if kind == "zoom":
            cam.start_zoom(direction)
        else:
            cam.start_move(direction)

    def move(self, direction, source="auto", deadline_s=None):
        """Start (or extend) a continuous move.

        Call again before the deadline to keep going -- that is the keepalive
        the wall panel sends while a button is held.

        Raises BudgetExceeded if the motor budget or the per-move ceiling
        refuses the request, or PTZError if a safety stop taken on the way to
        refusing could not be confirmed. The latter takes precedence: a camera
        that will not stop is a more urgent problem than a spent budget.
        """
        direction = direction.lower()
        if direction not in self.backend.DIRECTIONS:
            raise PTZError(f"unknown direction {direction!r}; "
                           f"expected one of {sorted(self.backend.DIRECTIONS)}")
        deadline_s = self.move_deadline_s if deadline_s is None else deadline_s

        with self._lock:
            already = self._moving and self._move_kind == "ptz"
            elapsed = self._clock() - self._move_started if already else 0.0

        if already and elapsed >= self.max_continuous_move_s:
            self.stop(reason="max continuous move reached")
            raise BudgetExceeded(
                f"a single move may not exceed {self.max_continuous_move_s:.0f}s")

        # Charge the budget for the slice we are about to authorise.
        decision = self.budget.check(deadline_s, source=source)
        if not decision.allowed:
            if already:
                self.stop(reason="budget exhausted mid-move")
            raise BudgetExceeded(decision.reason)

        if already:
            with self._lock:                      # just extend, do not re-send
                self._deadline = self._clock() + deadline_s
            self.budget.record(deadline_s, source=source)
            return

        self._begin_move("ptz", direction, source, deadline_s)
        self.budget.record(deadline_s, source=source)
        log.info("move %s (%s), deadline %.1fs", direction, source, deadline_s)

    def zoom(self, direction, source="manual", deadline_s=None):
        direction = direction.lower()
        if direction not in self.backend.ZOOMS:
            raise PTZError(f"unknown zoom {direction!r}; expected in or out")
        deadline_s = self.move_deadline_s if deadline_s is None else deadline_s
        decision = self.budget.check(deadline_s, source=source)
        if not decision.allowed:
            raise BudgetExceeded(decision.reason)
        with self._lock:
            already = self._moving and self._move_kind == "zoom"
        if already:
            with self._lock:
                self._deadline = self._clock() + deadline_s
        else:
            self._begin_move("zoom", direction, source, deadline_s)
        self.budget.record(deadline_s, source=source)

    def stop(self, reason=""):
        """Stop the camera, retrying until the camera confirms.

        A failed stop is never swallowed: _stop_pending stays set and the
        watchdog keeps trying, because the alternative is a motor running
        against its limit until someone notices.
        """
        if not self.enabled:
            # Nothing was ever commanded, so there is nothing to stop. Without
            # this, shutdown logs five failed attempts and an error for a
            # camera that was never touched.
            return True
        with self._lock:
            kind = self._move_kind
            self._moving = False
            self._move_kind = None
            self._deadline = 0.0
            self._stop_pending = True

        if not self._stop_lock.acquire(timeout=0.05):
            # Another thread is already pursuing a stop. _stop_pending stays
            # set so it will keep going until the camera confirms; a second
            # retry loop would only double the traffic, not the safety.
            log.debug("stop already in progress, deferring to it")
            return False
        try:
            with self._lock:
                if self._stopped and not self._moving:
                    # Someone else already stopped it and nothing has moved
                    # since. Re-sending would be pure noise.
                    return True
            return self._stop_loop(kind, reason)
        finally:
            self._stop_lock.release()

    def _stop_loop(self, kind, reason):
        last_err = None
        for attempt in range(1, self.stop_retries + 1):
            try:
                self._cam().stop(kind, timeout=self.stop_timeout)
                with self._lock:
                    self._stop_pending = False
                    self._stopped = True
                    self._stop_fail_streak = 0
                    self._retry_stop_after = 0.0
                    self.last_stopped_at = self._clock()
                if reason:
                    log.info("stopped (%s)", reason)
                return True
            except PTZError as e:
                last_err = e
                log.warning("stop attempt %d/%d failed: %s",
                            attempt, self.stop_retries, e)
                time.sleep(min(0.2 * attempt, 1.0))
        self.failed_stops += 1
        self._stop_fail_streak += 1
        # 1s, 2s, 4s ... capped at a minute. The first few are urgent; after
        # that the camera is unreachable rather than runaway, and hammering it
        # only fills the log.
        delay = min(self.stop_backoff_base_s * 2 ** (self._stop_fail_streak - 1),
                    self.stop_backoff_max_s)
        self._retry_stop_after = self._clock() + delay
        if self._stop_fail_streak <= 3:
            log.error("STOP FAILED after %d attempts -- the camera may still "
                      "be moving. Retrying in %.0fs.", self.stop_retries, delay)
        elif self._stop_fail_streak % 10 == 0:
            log.error("stop has failed %d times; the camera looks unreachable. "
                      "Still retrying every %.0fs.", self._stop_fail_streak, delay)
        raise PTZError(f"stop failed: {last_err}")

    def _watchdog(self):
        while True:
            time.sleep(self.watchdog_interval_s)
            try:
                with self._lock:
                    now = self._clock()
                    expired = self._moving and now >= self._deadline
                    too_long = (self._moving and
                                (now - self._move_started) >= self.max_continuous_move_s)
                    pending = self._stop_pending and not self._moving
                if expired or too_long:
                    self.watchdog_stops += 1
                    why = ("exceeded max continuous move" if too_long
                           else "deadline passed with no refresh")
                    log.info("watchdog stopping the camera: %s", why)
                    try:
                        self.stop(reason=f"watchdog: {why}")
                    except PTZError:
                        pass          # _stop_pending stays set; retried below
                elif pending and self._clock() >= self._retry_stop_after:
                    try:
                        self.stop(reason="retrying an unconfirmed stop")
                    except PTZError:
                        pass
            except Exception:
                log.exception("watchdog iteration failed")

    # --------------------------------------------------------------- presets
    def list_presets(self):
        return self._cam().list_presets()

    def goto_preset(self, name, source="auto", is_scan_start=False):
        decision = self.budget.check(self.preset_estimate_s, source=source,
                                     is_scan_start=is_scan_start)
        if not decision.allowed:
            raise BudgetExceeded(decision.reason)
        self._cam().goto_preset(name)
        self.budget.record(self.preset_estimate_s, source=source,
                           is_scan_start=is_scan_start)
        with self._lock:
            # The camera drives itself to the preset; treat it as movement so
            # settled() stays false until it has plausibly arrived.
            self.last_stopped_at = self._clock() + self.preset_estimate_s
        log.info("goto preset %r (%s)", name, source)

    def delete_preset(self, name):
        return self._cam().delete_preset(name)

    def add_preset(self, name):
        """Save the current view under `name`, replacing any existing one.

        Overwriting is the backend's contract; how it is achieved is the
        backend's problem (the Foscam one has to delete first, because its own
        add silently refuses to replace).
        """
        return self._cam().save_preset(name)

    def go_home(self, source="auto"):
        return self.goto_preset(self.p.get("home_preset", "Home"), source=source)

    # ----------------------------------------------------------------- misc
    def set_speed(self, level):
        return self._cam().set_speed(level)

    def set_time(self, when_utc=None, offset_seconds=None):
        """Push the clock to the camera, correct for the season.

        A camera on the isolated segment has no path to an NTP server, so its
        clock free-runs; and a camera may ignore its own DST flag, as the
        Foscam does. Both are handled by not trusting the camera with any of
        it: the board -- which has NTP and handles DST -- hands over UTC plus
        the offset that is correct right now, and the camera only has to store
        them. Run periodically, because a free-running clock drifts and because
        the offset changes twice a year.

        The wrong time on a vandalism clip is not a cosmetic problem: it is the
        difference between footage that corroborates an account and footage a
        defence can wave away.
        """
        import datetime as _dt
        import time as _t
        if when_utc is None:
            when_utc = _dt.datetime.utcnow()
        if offset_seconds is None:
            # The offset in effect at this instant, DST and all, from the OS.
            offset_seconds = (-_t.timezone if _t.localtime().tm_isdst <= 0
                              else -_t.altzone)
        return self._cam().set_clock(when_utc, offset_seconds)

    def dev_state(self):
        return self._cam().device_state()

    def dev_info(self):
        return self._cam().device_info()

    def snapshot(self):
        """Raw JPEG bytes. Used as the wall panel's near-live aiming view."""
        return self._cam().snapshot()

    def supports(self, cap):
        """Whether the camera in use can do something at all.

        Lets callers hide a control rather than offer one that will always
        fail -- this camera reports no absolute position, for instance.
        """
        return self.backend.supports(cap)

    def status(self):
        with self._lock:
            return {
                "moving": self._moving,
                "kind": self._move_kind,
                "stop_pending": self._stop_pending,
                "settled": self.settled(),
                "moves": self.moves,
                "watchdog_stops": self.watchdog_stops,
                "failed_stops": self.failed_stops,
                "budget_auto_used_s": round(self.budget.spent("auto"), 1),
                "budget_auto_left_s": round(self.budget.remaining("auto"), 1),
                "budget_manual_left_s": round(self.budget.remaining("manual"), 1),
            }
