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

"""Notice when the camera stops looking where it was aimed.

The club's dome reports no absolute position: it counts steps from an origin
it adopts at power-up. An electrician cut the power on 15 October 2026 while
it was sweeping, and it came back with every preset displaced by the same
offset. For twelve days each return home parked it on a view of its own
mount. Nothing complained. The log said `incident closed ... at Home`
throughout, because "at Home" is the name the software asked for and never
what the lens saw, and the health checks stayed green because frames kept
arriving -- of the wrong thing.

So this module asks the only question that could have caught it: *does the
parked view still look like the view we aimed at?* It compares a picture
against a reference picture, which costs no motor seconds and needs no
cooperation from a camera that cannot tell us where it is pointing.

What it measures is a SHIFT, not a difference. Two frames of the same scene
differ enormously between noon and midnight, between rain and sun, and
whenever somebody walks through; a difference score large enough to ignore
all that would be too large to notice a camera pointing at the ceiling.
Phase correlation instead finds how far the whole picture has moved, which is
exactly what drift is, and reports it in pixels. It divides the magnitude out
of the cross-power spectrum, so what survives is alignment and not contrast,
which is why a change of light reads as nothing much.

Measured on real frames off the club camera, raw snapshots at 256 pixels:

    two frames of the same aim, 45s apart     shift   0   confidence 238
    the same aim, lit a quarter as brightly   shift   0   confidence 243
    home against the view of its own mount    shift  33   confidence   7
    home against the post-calibration aim     shift  56   confidence   9
    home against a 2.4-second pan right       shift  30   confidence   7

So the thresholds are not a matter of taste: anything the camera is still
aimed at scores above 200, and everything it is not scores under 10.

Two further defences against crying wolf:

- A reading is confirmed before it is believed. Somebody standing in front of
  the lens is a big apparent shift that is gone a minute later.
- The daily check happens at the same time of day the reference was taken, so
  a night reference is compared against night and not against noon.

It cannot tell you WHY the view moved -- a power cut, a slipped gear, a hand
on the housing. That is the point: it reports the thing that matters, which
is that what you believe you are watching is not what you are watching.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
import datetime as dt
from collections import namedtuple

try:                                    # the board has it; a laptop may not
    import numpy as np
except ImportError:                     # pragma: no cover - exercised by absence
    np = None

log = logging.getLogger("aimwatch")

# What a check concluded. `shift` is in pixels of the normalised square the
# comparison runs on, so it is comparable between cameras and resolutions.
Reading = namedtuple("Reading", "dx dy shift confidence recognisable drifted")


def available():
    """Whether the maths can run at all."""
    return np is not None


# ------------------------------------------------------------------ pixels

def _resize(a, size):
    """Area-average square downsample, in numpy alone.

    Averaging, not sampling: 1920x1080 into 256 is a factor of seven, and
    dropping seven pixels out of eight turns a gravel path into aliasing
    noise that the correlation then tries to match. Measured on real frames,
    nearest-neighbour cost about a fifth of the confidence on two pictures of
    the same scene. Done with an integral image so the cost does not depend
    on how far the frame is being shrunk.

    Deliberately not cv2: this module has to keep working when the detector
    stack does not.
    """
    h, w = a.shape[:2]
    ii = np.pad(np.cumsum(np.cumsum(a, axis=0), axis=1), ((1, 0), (1, 0)))
    ys = np.linspace(0, h, size + 1).astype(np.int64)
    xs = np.linspace(0, w, size + 1).astype(np.int64)
    total = (ii[ys[1:, None], xs[None, 1:]] - ii[ys[:-1, None], xs[None, 1:]]
             - ii[ys[1:, None], xs[None, :-1]]
             + ii[ys[:-1, None], xs[None, :-1]])
    cells = np.maximum(np.diff(ys)[:, None] * np.diff(xs)[None, :], 1)
    return total / cells


def _prepare(gray, size):
    """A frame reduced to what the correlation needs.

    A Hann window, so the frame's own border does not read as a feature, and
    nothing else. Two earlier steps did not survive measurement against real
    club frames: gradient magnitude, on the theory that edges outlast a
    change of light, and subtracting the mean. Both left every number in the
    third significant figure, because the phase normalisation below already
    throws away everything except alignment. They are gone rather than kept
    as steps that look careful and do nothing.
    """
    a = np.asarray(gray, dtype=np.float64)
    if a.ndim == 3:                     # colour in, luminance out
        a = a.mean(axis=2)
    win = np.hanning(size)
    return _resize(a, size) * np.outer(win, win)


def compare(reference_gray, current_gray, size=256):
    """How far the current view has moved relative to the reference.

    Phase correlation: the cross-power spectrum with its magnitude divided
    out, so what is left is the alignment and not the contrast. A uniform
    change of brightness cancels exactly; a change of lighting mostly does.

    `confidence` is the correlation peak against the noise floor of the rest
    of the surface. A low one means the two pictures have no common geometry
    at all -- a blocked lens, or a camera pointing somewhere entirely else --
    which is not a measurable shift but is certainly worth saying out loud.
    """
    if np is None:
        raise RuntimeError("numpy is needed to compare frames")
    a = _prepare(reference_gray, size)
    b = _prepare(current_gray, size)
    fa = np.fft.rfft2(a)
    fb = np.fft.rfft2(b)
    r = fa * np.conj(fb)
    r /= np.abs(r) + 1e-12
    surface = np.fft.irfft2(r, s=a.shape)
    peak = int(np.argmax(surface))
    py, px = divmod(peak, surface.shape[1])
    # The surface wraps: a peak near the far edge is a small shift the other
    # way, not an enormous one.
    dy = py - size if py > size // 2 else py
    dx = px - size if px > size // 2 else px
    top = float(surface.flat[peak])
    noise = float(surface.std()) or 1e-12
    # Negated: the correlation gives where the reference sits in the current
    # frame, and what a person wants told is which way the CAMERA went.
    return float(-dx), float(-dy), math.hypot(dx, dy), top / noise


# ------------------------------------------------------------------- watch

class AimWatch:
    """The daily question, with the state to ask it at the right moment."""

    def __init__(self, reference_path, enabled=True, every_hours=24.0,
                 window_minutes=90.0, shift_px=20.0, min_confidence=12.0,
                 confirm_seconds=120.0, cooldown_hours=24.0, size=256,
                 decode=None, clock=time.time):
        self.path = str(reference_path)
        self.state_path = self.path + ".json"
        self.enabled = bool(enabled) and available()
        if bool(enabled) and not available():
            log.warning("the aim check needs numpy; it is switched off")
        self.every_s = float(every_hours) * 3600.0
        self.window_s = float(window_minutes) * 60.0
        self.shift_px = float(shift_px)
        self.min_confidence = float(min_confidence)
        self.confirm_s = float(confirm_seconds)
        self.cooldown_s = float(cooldown_hours) * 3600.0
        self.size = int(size)
        self._decode = decode
        self._clock = clock
        self._pending = None            # (first seen at, Reading)
        self._ref_at = None
        self._last_check = None
        self._last_alert = None
        self.checks = 0
        self.alerts = 0
        self._load()

    # ------------------------------------------------------------- storage
    def _load(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return
        # Surviving a restart matters: without this, every restart would
        # re-arm the check and a board that reboots daily would alert daily.
        for k in ("ref_at", "last_check", "last_alert"):
            v = raw.get(k)
            setattr(self, "_" + k, float(v) if isinstance(v, (int, float))
                    else None)

    def _save(self):
        data = {"ref_at": self._ref_at, "last_check": self._last_check,
                "last_alert": self._last_alert}
        try:
            d = os.path.dirname(self.path) or "."
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".aim-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.state_path)
        except OSError:
            log.warning("could not write the aim check's state", exc_info=True)

    @property
    def has_reference(self):
        return self._ref_at is not None and os.path.exists(self.path)

    def set_reference(self, jpeg, now=None):
        """This is the view. Called when somebody saves the home position.

        Saving home and saying "watch this" are the same act: the person has
        just aimed the camera at what they want watched. Asking them to press
        a second button is how the reference ends up stale, or never taken.
        """
        now = self._clock() if now is None else now
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".ref-", suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            fh.write(jpeg)
        os.replace(tmp, self.path)
        self._ref_at = float(now)
        self._last_check = float(now)
        self._last_alert = None         # a new aim is a clean slate
        self._pending = None
        self._save()
        log.info("saved the aim reference; the view will be checked against "
                 "it daily")

    # --------------------------------------------------------------- timing
    def _same_time_of_day(self, now):
        """Within the window around the hour the reference was taken.

        A reference shot at midnight compared against noon is two different
        pictures of the same place, and the comparison would rather not have
        to be that clever.
        """
        if not self._ref_at:
            return False
        ref = dt.datetime.fromtimestamp(self._ref_at)
        here = dt.datetime.fromtimestamp(now)
        ref_s = ref.hour * 3600 + ref.minute * 60 + ref.second
        now_s = here.hour * 3600 + here.minute * 60 + here.second
        gap = abs(now_s - ref_s)
        return min(gap, 86400 - gap) <= self.window_s

    def due(self, now=None):
        """Is this a moment to look?

        Cheap on purpose: it is asked on every pass of the controller loop,
        and the expensive part -- a snapshot off the camera and an FFT -- must
        only happen when the answer is yes.
        """
        if not self.enabled or not self.has_reference:
            return False
        now = self._clock() if now is None else now
        if self._pending is not None:
            # A suspicious reading is waiting for a second opinion, and that
            # one is not on tomorrow's schedule.
            return now - self._pending[0] >= self.confirm_s
        if self._last_check and now - self._last_check < self.every_s:
            return False
        return self._same_time_of_day(now)

    # ---------------------------------------------------------------- check
    def _gray(self, jpeg):
        if self._decode is not None:
            return self._decode(jpeg)
        import cv2                      # only the board needs this
        buf = np.frombuffer(jpeg, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise ValueError("could not decode the frame")
        return img

    def check(self, jpeg, now=None):
        """One comparison. Returns a Reading, or None if it could not look.

        A reading that is suspicious is not reported as drift the first time:
        it is held, and the next check -- two minutes later, not tomorrow --
        decides. A person standing in front of the camera and a camera that
        has moved look identical for about a minute, and only one of them is
        still there afterwards.
        """
        now = self._clock() if now is None else now
        if not self.has_reference:
            return None
        try:
            with open(self.path, "rb") as fh:
                ref = self._gray(fh.read())
            cur = self._gray(jpeg)
            dx, dy, shift, conf = compare(ref, cur, self.size)
        except Exception:
            log.warning("the aim check could not compare the view",
                        exc_info=True)
            return None
        self.checks += 1
        self._last_check = float(now)
        recognisable = conf >= self.min_confidence
        moved = (not recognisable) or shift >= self.shift_px
        r = Reading(dx, dy, shift, conf, recognisable, False)
        if not moved:
            self._pending = None
            self._save()
            return r
        if self._pending is None:
            self._pending = (float(now), r)
            self._save()
            log.info("the view looks %s; checking again in %.0fs",
                     "unrecognisable" if not recognisable
                     else "shifted by %.0f px" % shift, self.confirm_s)
            return r
        # Second opinion, and it agrees.
        self._pending = None
        if self._last_alert and now - self._last_alert < self.cooldown_s:
            log.info("the view is still off, but one alert a day is enough")
            self._save()
            return r
        self._last_alert = float(now)
        self.alerts += 1
        self._save()
        return r._replace(drifted=True)

    # --------------------------------------------------------------- report
    def message(self, reading):
        """What to put in front of a person at one in the morning."""
        if not reading.recognisable:
            return ("The camera's view no longer matches what it was aimed "
                    "at, and does not resemble it at all. Check the camera.")
        return ("The camera's view has moved about %d pixels from where it "
                "was aimed (%+d across, %+d down). Nothing in the system "
                "asked it to move." % (round(reading.shift), round(reading.dx),
                                       round(reading.dy)))

    def status(self):
        return {
            "enabled": bool(self.enabled),
            "has_reference": bool(self.has_reference),
            "reference_at": self._ref_at,
            "last_check": self._last_check,
            "last_alert": self._last_alert,
            "checks": self.checks,
            "alerts": self.alerts,
            "pending": self._pending is not None,
        }


def from_config(cfg, root):
    """Build one from the `aim_watch:` block, or None if it is switched off."""
    a = (cfg._get("aim_watch", default={}) or {}) if cfg is not None else {}
    if not a.get("enabled", True):
        return None
    return AimWatch(
        os.path.join(str(root), "aim-reference.jpg"),
        enabled=True,
        every_hours=a.get("every_hours", 24),
        window_minutes=a.get("window_minutes", 90),
        shift_px=a.get("shift_px", 20),
        min_confidence=a.get("min_confidence", 12.0),
        confirm_seconds=a.get("confirm_seconds", 120),
        cooldown_hours=a.get("cooldown_hours", 24),
    )
