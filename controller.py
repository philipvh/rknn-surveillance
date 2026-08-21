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

"""The state machine: what the camera does, and when.

Holds all the state and is the only thing allowed to move the camera. It hears
the PIR and the detector, decides where to point, decides which footage
becomes a clip, and decides what would have been worth an alert.

    PARKED --PIR--> SETTLING --+1.5s--> SCANNING --person--> HOLDING
       ^                                    |                   |
       +---------- RETURNING <--------------+---- quiet 15s ----+

MANUAL is entered from any state by the wall panel and expires back to
RETURNING after a few minutes of no input.

SETTLING exists because the PIR switches the floodlights: the first second or
so after a trigger is the camera's exposure and IR-cut filter reacting, and
inference on those frames produces junk.
"""

import datetime as dt
import enum
import logging
import threading
from pathlib import Path

from alerts import Incident
from ptz import BudgetExceeded, PTZError

log = logging.getLogger("controller")


class State(enum.Enum):
    PARKED = "parked"
    SETTLING = "settling"
    SCANNING = "scanning"
    HOLDING = "holding"
    RETURNING = "returning"
    MANUAL = "manual"


class Detection:
    """What the detector saw in one frame."""

    def __init__(self, at, count=1, max_confidence=0.0, labels=()):
        self.at = at
        self.count = count
        self.max_confidence = max_confidence
        self.labels = set(labels)


class Controller:
    def __init__(self, cfg, ptz, schedule, policy, shadow_log,
                 clip_fn=None, snapshot_fn=None, tracker=None, announcer=None,
                 mark_open_fn=None, mark_done_fn=None,
                 annotated=None, capture=None,
                 clock=None, wall=None):
        self.cfg = cfg
        self.ptz = ptz
        self.schedule = schedule
        self.policy = policy
        self.shadow = shadow_log
        self.clip_fn = clip_fn or (lambda start, end: None)
        # A record on disk that a window is open, so a kill -9 or a power cut
        # can still be turned into a clip on the next start. The in-memory
        # incident does not survive either of those.
        self.mark_open_fn = mark_open_fn or (lambda start: None)
        self.mark_done_fn = mark_done_fn or (lambda: None)
        self.snapshot_fn = snapshot_fn or (lambda: "")
        self.tracker = tracker
        self.announcer = announcer
        self.annotated = annotated
        self.capture = capture

        p = (cfg._get("ptz", default={}) or {})
        self.presets = list(p.get("scan_presets") or [])
        self.home = p.get("home_preset", "Home")
        self.dwell_s = float(p.get("dwell_s", 4.0))
        self.settle_s = float(p.get("settle_s", 0.5))
        # With no PTZ, or no presets, the camera is simply fixed: watch the
        # view it has rather than retrying moves that can never succeed.
        self.can_move = bool(getattr(ptz, "enabled", True)) and bool(self.presets)

        c = (cfg._get("controller", default={}) or {})
        self.lights_settle_s = float(c.get("lights_settle_s", 1.5))
        self.quiet_period_s = float(c.get("quiet_period_s", 15.0))
        self.max_hold_s = float(c.get("max_hold_s", 600.0))
        self.max_scan_s = float(c.get("max_scan_s", 120.0))
        self.manual_timeout_s = float(c.get("manual_timeout_s", 180.0))
        self.return_timeout_s = float(c.get("return_timeout_s", 30.0))
        self.pir_required_to_scan = bool(c.get("pir_required_to_scan", True))

        t = (cfg._get("trigger", default={}) or {})
        self.pre_roll_s = float(t.get("pre_roll_s", 2.0))
        self.post_roll_s = float(t.get("post_roll_s", 15.0))

        self._clock = clock or __import__("time").monotonic
        self._wall = wall or dt.datetime.now

        self._lock = threading.RLock()
        self._state = State.PARKED
        self._entered_at = self._clock()
        self._deadline = None

        self._preset_index = 0
        self._pir_active = False
        self._pir_last_release = None
        self._pir_last_duration = 0.0

        self._incident = None
        self._last_detection_at = None
        self._last_trigger_at = None

        self.transitions = []          # (wall, from, to, why) -- for the panel
        self.incidents_closed = 0
        self.clips_requested = 0

    # ------------------------------------------------------------------ state
    @property
    def state(self):
        with self._lock:
            return self._state

    def _elapsed(self):
        return self._clock() - self._entered_at

    def _go(self, new, why=""):
        with self._lock:
            if new is self._state:
                return
            old, self._state = self._state, new
            self._entered_at = self._clock()
            self._deadline = None
            self.transitions.append((self._wall(), old.value, new.value, why))
            del self.transitions[:-200]
        log.info("%s -> %s%s", old.value, new.value, f"  ({why})" if why else "")

    # ------------------------------------------------------- detection gating
    def detection_enabled(self):
        """False while the motors run and briefly after they stop.

        Frames from a panning camera are motion-blurred: a waste of NPU time
        and a source of false positives, and the trigger logic has no concept
        of the view having changed underneath it.
        """
        try:
            if self.ptz.moving:
                return False
            if not self.ptz.settled():
                return False
        except Exception:
            pass
        return self.state is not State.SETTLING

    # ----------------------------------------------------------------- inputs
    def on_pir(self, ev):
        with self._lock:
            if ev.kind == "active":
                self._pir_active = True
            elif ev.kind == "inactive":
                self._pir_active = False
                self._pir_last_release = self._clock()
                self._pir_last_duration = ev.duration
                if self._incident:
                    self._incident.pir_duration_s = ev.duration
            elif ev.kind == "stuck":
                # A stuck line must not hold the camera scanning all night.
                self._pir_active = False
                log.warning("ignoring a stuck PIR line as a trigger")
                return

        if ev.kind == "active":
            # The PIR triggers a capture exactly as a sighting does.
            with self._lock:
                self._last_trigger_at = self._clock()
                if self._incident is None:
                    # self._wall, not datetime.now: the controller has one
                    # source of wall time and mixing them makes first_seen and
                    # last_seen incomparable.
                    self._open_incident(self._wall(), "PIR")
            if self._state in (State.PARKED, State.RETURNING):
                self._go(State.SETTLING, "PIR closed")

    def track(self, boxes, scores, width, height):
        """Offer the frame to the tracker. Only acts while holding, so a sweep
        is never fought by the tracker and the two cannot argue over the dome."""
        if self.tracker is None or self.state is not State.HOLDING:
            return None
        return self.tracker.update(boxes, scores, width, height)

    def _open_incident(self, when, source):
        """Start a capture window. Both trigger sources land here.

        The PIR and the detector do the same job -- they say something is
        happening -- so they extend one window rather than running two. A
        window that only ever saw the PIR ends up with no sightings, which the
        alert policy's persistence gate refuses on its own; the clip is still
        cut, because a clip is what a trigger is for.

        Caller must hold the lock.
        """
        self._incident = Incident(first_seen=when, last_seen=when)
        log.info("incident opened at %s (%s)", when.strftime("%H:%M:%S"), source)
        if self.annotated is not None:
            self.annotated.start()
        if self.capture is not None:
            self.capture.set_triggered(
                when - dt.timedelta(seconds=self.pre_roll_s))
        try:
            self.mark_open_fn(when - dt.timedelta(seconds=self.pre_roll_s))
        except Exception:
            log.debug("could not mark the incident open", exc_info=True)
        self._incident.pir_corroborated = self._pir_recent()
        self._incident.preset = self._current_preset()
        try:
            self._incident.snapshot = self.snapshot_fn() or ""
        except Exception:
            log.debug("snapshot failed", exc_info=True)

    def note_snapshot(self, path):
        """The first JPEG of this window, recorded as the incident's image."""
        with self._lock:
            if self._incident is not None and not self._incident.snapshot:
                self._incident.snapshot = str(path)

    def on_detection(self, det):
        """One frame's worth of sightings. Only called when detection is enabled."""
        with self._lock:
            self._last_detection_at = self._clock()
            self._last_trigger_at = self._clock()
            if self._incident is None:
                self._open_incident(det.at, "detector")
            inc = self._incident
            inc.last_seen = det.at
            inc.sightings += 1
            inc.frames += 1
            inc.max_confidence = max(inc.max_confidence, det.max_confidence)
            inc.max_count = max(inc.max_count, det.count)
            inc.labels |= det.labels
            if self._pir_recent():
                inc.pir_corroborated = True

        if self._state in (State.SCANNING, State.SETTLING, State.PARKED):
            self._go(State.HOLDING, "person in view")
            # Rung three of the deterrence ladder, once per incident: the
            # camera has already turned to face them by getting here.
            if self.announcer is not None:
                try:
                    self.announcer.maybe_announce(self._incident)
                except Exception:
                    log.exception("announcement failed")
        elif self._state is State.HOLDING:
            self._entered_at = self._entered_at   # keep hold start for max_hold

    def on_manual(self, active=True):
        """The wall panel took or released control."""
        if active:
            self._go(State.MANUAL, "wall panel")
        elif self._state is State.MANUAL:
            self._go(State.RETURNING, "panel released")

    def _pir_recent(self, within_s=60.0):
        with self._lock:
            if self._pir_active:
                return True
            if self._pir_last_release is None:
                return False
            return (self._clock() - self._pir_last_release) <= within_s

    def _current_preset(self):
        if self._state is State.SCANNING and self.presets:
            return self.presets[(self._preset_index - 1) % len(self.presets)]
        if self._state in (State.PARKED, State.SETTLING):
            return self.home
        return ""

    # ------------------------------------------------------------------- tick
    def tick(self):
        st = self.state
        handler = getattr(self, f"_tick_{st.value}", None)
        if handler:
            try:
                handler()
            except (PTZError, BudgetExceeded) as e:
                log.warning("%s in %s: %s", type(e).__name__, st.value, e)
                if st in (State.SCANNING, State.SETTLING):
                    self._go(State.RETURNING, f"{type(e).__name__}")

    def _tick_parked(self):
        # A window opened by the PIR while the camera stayed parked still has
        # to close, or the capture state would never be released.
        self._expire_idle_incident()

    def _expire_idle_incident(self):
        with self._lock:
            open_ = self._incident is not None
            last = self._last_trigger_at
            pir = self._pir_active
        if not open_ or pir or last is None:
            return
        if (self._clock() - last) >= self.quiet_period_s:
            self._close_incident("no trigger for the quiet period")

    def _tick_settling(self):
        # Let the floodlights come up and the camera's exposure catch up.
        if self._elapsed() < self.lights_settle_s:
            return
        if self.pir_required_to_scan and not self._pir_active:
            self._go(State.PARKED, "PIR released before the scan began")
            return
        if not self.can_move:
            self._go(State.HOLDING, "camera is fixed; watching the view it has")
            return
        self._preset_index = 0
        self._go(State.SCANNING, "lights up")

    def _tick_scanning(self):
        if not self.can_move:
            self._go(State.HOLDING, "camera is fixed")
            return
        if self._elapsed() > self.max_scan_s:
            self._go(State.RETURNING, "scan took too long")
            return
        if self._deadline is None:
            if self._preset_index >= len(self.presets):
                self._go(State.RETURNING, "swept every preset")
                return
            name = self.presets[self._preset_index]
            self._preset_index += 1
            try:
                self.ptz.goto_preset(name, source="auto",
                                     is_scan_start=(self._preset_index == 1))
            except BudgetExceeded as e:
                self._go(State.RETURNING, f"motor budget: {e}")
                return
            self._deadline = self._clock() + self.dwell_s + self.ptz.preset_estimate_s
            return
        if self._clock() >= self._deadline:
            self._deadline = None          # dwell finished, move to the next

    def _tick_holding(self):
        # Either source keeps the window open: the PIR still seeing movement
        # is as good a reason to keep recording as the camera is.
        with self._lock:
            last = self._last_trigger_at
            pir = self._pir_active
        if self._elapsed() > self.max_hold_s:
            self._close_incident("held for the maximum time")
            self._go(State.RETURNING, "maximum hold reached")
            return
        if pir:
            return
        if last is not None and (self._clock() - last) >= self.quiet_period_s:
            self._close_incident("quiet period elapsed")
            self._go(State.RETURNING, "no trigger for the quiet period")

    def _tick_returning(self):
        if not self.can_move:
            self._go(State.PARKED, "camera is fixed")
            return
        if self._deadline is None:
            try:
                self.ptz.goto_preset(self.home, source="auto")
            except BudgetExceeded as e:
                # Refusing to return home would leave the camera pointing at a
                # corner all night, so this is worth saying loudly.
                log.error("could not return home, motor budget refused: %s", e)
                self._go(State.PARKED, "budget refused the return home")
                return
            self._deadline = self._clock() + self.ptz.preset_estimate_s
            return
        if self._clock() >= self._deadline or self._elapsed() > self.return_timeout_s:
            self._go(State.PARKED, "home")

    def _tick_manual(self):
        if self._elapsed() > self.manual_timeout_s:
            self._go(State.RETURNING, "panel idle")

    # -------------------------------------------------------------- incidents
    def close_open_incident(self, why="shutting down"):
        """Flush an incident that is still open. Returns True if there was one.

        An incident lives in memory until the quiet period elapses. Stopping
        the service in that window used to drop it entirely: the stills stayed
        on the card and the clip was never cut, so the browser showed
        thumbnails for an event with no video. Restarts happen -- deploys,
        watchdog bounces, power blips -- and they land mid-incident often
        enough to matter.
        """
        with self._lock:
            open_now = self._incident is not None
        if open_now:
            self._close_incident(why)
        return open_now

    def _close_incident(self, why):
        with self._lock:
            inc, self._incident = self._incident, None
            self._last_detection_at = None
            self._last_trigger_at = None
        if inc is None:
            return
        inc.pir_duration_s = inc.pir_duration_s or self._pir_last_duration
        if self.tracker is not None:
            self.tracker.reset()

        start = inc.first_seen - dt.timedelta(seconds=self.pre_roll_s)
        end = inc.last_seen + dt.timedelta(seconds=self.post_roll_s)
        try:
            clip = self.clip_fn(start, end)
            inc.clip = str(clip) if clip else ""
            if clip:
                self.clips_requested += 1
        except Exception:
            log.exception("could not request a clip")
            clip = None
        # The real sidecar now stands in for the marker.
        try:
            self.mark_done_fn()
        except Exception:
            log.debug("could not clear the incident marker", exc_info=True)

        # The evidence clip stays the camera's own bytes; the boxes go in a
        # second file beside it.
        if self.annotated is not None:
            try:
                if clip:
                    base = Path(str(clip)).with_suffix("")
                    ann_t0 = self.annotated.first_at
                    written = self.annotated.write(
                        Path(f"{base}.annotated.mp4"))
                    # Record where the companion actually starts, so the
                    # browser can seek it to a wall-clock moment too.
                    if written and ann_t0:
                        side = Path(str(clip)).with_suffix(".json")
                        try:
                            import json as _json
                            data = (_json.loads(side.read_text())
                                    if side.exists() else {})
                            data["annotated_t0"] = ann_t0.isoformat(
                                timespec="seconds")
                            side.write_text(_json.dumps(data, indent=1))
                        except (OSError, ValueError) as e:
                            log.warning("could not record the annotated t0: %s", e)
                else:
                    self.annotated.discard()
            except Exception:
                log.exception("could not write the annotated clip")

        if self.capture is not None:
            self.capture.set_ready()

        decision = self.policy.evaluate(inc)
        self.shadow.record(inc, decision)
        self.incidents_closed += 1
        log.info("incident closed (%s): %s", why, inc.summary())

    @property
    def triggered(self):
        """True while an incident is open -- the capture state machine's
        'triggered', and what gates the per-second JPEGs."""
        with self._lock:
            return self._incident is not None

    def open_incident_start(self):
        """When the currently open incident began, or None.

        Retention uses this to pin the segments an in-progress incident will
        need, so a sweep cannot delete footage that is about to become a clip.
        """
        with self._lock:
            return self._incident.first_seen if self._incident else None

    # ----------------------------------------------------------------- status
    def status(self):
        """Snapshot of everything the panel shows.

        Own state is read under the lock; everything else is asked for
        afterwards. Calling into other objects while holding this lock is what
        let a wedged announcer stop the detection loop -- the panel polls this
        every two seconds, so any lock taken here is on the critical path of
        the whole system.
        """
        with self._lock:
            inc = self._incident
            out = {
                "state": self._state.value,
                "for_s": round(self._elapsed(), 1),
                "pir_active": self._pir_active,
                "incident_open": inc is not None,
                "incident": inc.summary() if inc else None,
                "incidents_closed": self.incidents_closed,
                "clips_requested": self.clips_requested,
                "preset": self._current_preset(),
            }
        # Outside the lock, and defensively: a panel poll must never be able
        # to take the camera down.
        for name, fn in (("schedule", self._schedule_status),
                         ("policy", self.policy.status),
                         ("tracker", self.tracker.status if self.tracker else dict),
                         ("announcer", self.announcer.status if self.announcer else dict)):
            try:
                out.update(fn())
            except Exception:
                log.exception("could not read %s status", name)
        try:
            out["detection_enabled"] = self.detection_enabled()
        except Exception:
            out["detection_enabled"] = None
        return out

    def _schedule_status(self):
        return {"armed": self.schedule.is_armed(),
                "schedule": self.schedule.describe()}
