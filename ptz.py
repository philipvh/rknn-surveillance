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

"""PTZ control for the Foscam SD2X, with the safety properties attached.

Every movement command this camera accepts is *continuous*: it runs until
something stops it. That single fact drives the whole design here. If the
process that sent ptzMoveLeft dies, or the wall panel's wifi drops with a
finger on the button, the camera keeps turning until it grinds against its
mechanical limit. So:

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

The transport is injectable so all of the above can be tested without a camera.
"""

from collections import deque
from dataclasses import dataclass
from urllib.parse import urlencode
import atexit
import logging
import re
import signal
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger("ptz")

DIRECTIONS = {
    "up": "ptzMoveUp", "down": "ptzMoveDown",
    "left": "ptzMoveLeft", "right": "ptzMoveRight",
    "topleft": "ptzMoveTopLeft", "topright": "ptzMoveTopRight",
    "bottomleft": "ptzMoveBottomLeft", "bottomright": "ptzMoveBottomRight",
}
ZOOMS = {"in": "zoomIn", "out": "zoomOut"}

STOP_CMD = "ptzStopRun"
ZOOM_STOP_CMD = "zoomStop"


def foscam_time_params(utc, offset_seconds):
    """Build setSystemTime arguments for a UTC instant and a local offset.

    The camera treats the value it is given as UTC and then adds its timeZone
    for display -- and Foscam's timeZone has the opposite sign to everyone
    else's, so GMT+1 is -3600, not +3600. Both quirks are confined to this one
    function: give it a UTC datetime and the real offset east of UTC in
    seconds (positive for the Netherlands), and it returns the right dict.
    isDst is left 0 on purpose -- the offset already carries summer time, and
    this firmware's own DST flag does nothing.
    """
    return {
        "timeSource": "1",              # manual: we are the time source
        "timeZone": str(-int(offset_seconds)),
        "isDst": "0",
        "year": str(utc.year), "mon": str(utc.month), "day": str(utc.day),
        "hour": str(utc.hour), "minute": str(utc.minute), "sec": str(utc.second),
    }


class PTZError(Exception):
    pass


class BudgetExceeded(PTZError):
    pass


# --------------------------------------------------------------------- transport

class UrllibTransport:
    """Stdlib only -- one less package to install on the board."""

    def get(self, url, params, timeout):
        full = f"{url}?{urlencode(params)}"
        req = urllib.request.Request(full, headers={"User-Agent": "rknn-ptz/1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()


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
                 install_signal_handlers=True, stop_on_start=True):
        self.cfg = cfg
        self.p = (cfg._get("ptz", default={}) or {})
        http = self.p.get("http", {}) or {}
        # A camera that is not there, or not a Foscam, must not be commanded.
        # Without this the watchdog retries a stop against an unreachable
        # host forever and floods the log.
        self.enabled = bool(self.p.get("enabled", True))

        self.url = cfg.cgi_url()
        self._params_for = cfg.cgi_params
        self.transport = transport or UrllibTransport()
        self._clock = clock

        self.timeout = float(http.get("timeout_s", 5.0))
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

    # ------------------------------------------------------------ transport
    def _call(self, cmd, timeout=None, **extra):
        if not self.enabled:
            raise PTZError("PTZ is disabled in config")
        params = self._params_for(cmd, **extra)
        try:
            body = self.transport.get(self.url, params, timeout or self.timeout)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise PTZError(f"{cmd}: {e}") from e
        return self._parse(cmd, body)

    @staticmethod
    def _parse(cmd, body):
        if isinstance(body, bytes):
            try:
                text = body.decode("utf-8", "replace")
            except Exception:
                text = ""
        else:
            text = body or ""
        if not text.strip():
            return {"result": None, "_raw": text}
        out = {}
        try:
            root = ET.fromstring(text)
            for child in root:
                out[child.tag] = (child.text or "").strip()
        except ET.ParseError:
            # Foscam is not always strict about its XML; fall back to scraping.
            for m in re.finditer(r"<(\w+)>([^<]*)</\1>", text):
                out[m.group(1)] = m.group(2).strip()
            if not out:
                raise PTZError(f"{cmd}: unparseable response {text[:120]!r}")
        if "result" in out:
            try:
                res = int(out["result"])
            except ValueError:
                res = None
            out["result"] = res
            if res not in (0, None):
                raise PTZError(f"{cmd}: camera returned result={res}")
        out["_raw"] = text
        return out

    # ---------------------------------------------------------------- moving
    def _begin_move(self, cmd, kind, source, deadline_s):
        with self._lock:
            self._moving = True
            self._move_kind = kind
            self._move_source = source
            self._move_started = self._clock()
            self._deadline = self._clock() + deadline_s
            self._stopped = False
            self.moves += 1
        self._call(cmd)

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
        if direction not in DIRECTIONS:
            raise PTZError(f"unknown direction {direction!r}; "
                           f"expected one of {sorted(DIRECTIONS)}")
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

        self._begin_move(DIRECTIONS[direction], "ptz", source, deadline_s)
        self.budget.record(deadline_s, source=source)
        log.info("move %s (%s), deadline %.1fs", direction, source, deadline_s)

    def zoom(self, direction, source="manual", deadline_s=None):
        direction = direction.lower()
        if direction not in ZOOMS:
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
            self._begin_move(ZOOMS[direction], "zoom", source, deadline_s)
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

        cmds = [STOP_CMD] if kind != "zoom" else [ZOOM_STOP_CMD, STOP_CMD]

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
            return self._stop_loop(cmds, reason)
        finally:
            self._stop_lock.release()

    def _stop_loop(self, cmds, reason):
        last_err = None
        for attempt in range(1, self.stop_retries + 1):
            try:
                for c in cmds:
                    self._call(c, timeout=self.stop_timeout)
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
        out = self._call("getPTZPresetPointList")
        names = []
        for k, v in out.items():
            if re.fullmatch(r"point\d+", k) and v:
                names.append((int(k[5:]), v))
        return [v for _, v in sorted(names)]

    def goto_preset(self, name, source="auto", is_scan_start=False):
        decision = self.budget.check(self.preset_estimate_s, source=source,
                                     is_scan_start=is_scan_start)
        if not decision.allowed:
            raise BudgetExceeded(decision.reason)
        self._call("ptzGotoPresetPoint", name=name)
        self.budget.record(self.preset_estimate_s, source=source,
                           is_scan_start=is_scan_start)
        with self._lock:
            # The camera drives itself to the preset; treat it as movement so
            # settled() stays false until it has plausibly arrived.
            self.last_stopped_at = self._clock() + self.preset_estimate_s
        log.info("goto preset %r (%s)", name, source)

    def delete_preset(self, name):
        return self._call("ptzDeletePresetPoint", name=name)

    def add_preset(self, name):
        # ptzAddPresetPoint will NOT overwrite an existing name: the camera
        # returns success but keeps the original position, so re-aiming a preset
        # from the panel silently does nothing -- which is exactly how "Set
        # Home" appeared broken. Delete first (ignoring "not there"), then add,
        # so saving a preset always stores the current view.
        try:
            self.delete_preset(name)
        except PTZError:
            pass
        return self._call("ptzAddPresetPoint", name=name)

    def go_home(self, source="auto"):
        return self.goto_preset(self.p.get("home_preset", "Home"), source=source)

    # ----------------------------------------------------------------- misc
    def set_speed(self, level):
        return self._call("setPTZSpeed", speed=int(level))

    def set_time(self, when_utc=None, offset_seconds=None):
        """Push the clock to the camera, correct for the season.

        This camera cannot be left to keep its own time. It is on the isolated
        segment with no path to an NTP server, so its clock free-runs; and its
        firmware ignores the isDst flag, so even with time it would not follow
        summer time. Both are fixed here by not trusting the camera with any of
        it: the board -- which does have NTP and does handle DST -- hands over
        UTC plus the offset that is correct right now, and the camera only has
        to add them. Run periodically, because a free-running clock drifts and
        because the offset changes twice a year.

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
        return self._call("setSystemTime",
                          **foscam_time_params(when_utc, offset_seconds))

    def dev_state(self):
        return self._call("getDevState")

    def dev_info(self):
        return self._call("getDevInfo")

    def snapshot(self):
        """Raw JPEG bytes. Used as the wall panel's last-resort live view."""
        params = self._params_for("snapPicture2")
        try:
            return self.transport.get(self.url, params, self.timeout)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise PTZError(f"snapPicture2: {e}") from e

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
