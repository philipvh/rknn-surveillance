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

"""Which one-minute segments survive, and for how long.

Two states:

  ready      nothing is happening. The recorder keeps writing minute-long
             segments; each new one lets the previous be deleted. What is kept
             is a short rolling buffer, so a trigger has some pre-roll.

  triggered  something is happening. Nothing is deleted. Segments accumulate
             for as long as the incident lasts.

On the way back to ready, everything from the incident is concatenated into a
single clip and the sources are deleted -- so what stays on disk is one file
per event, and nothing at all for the minutes in between.

The decision is a pure function so it can be tested exhaustively: deleting the
wrong segment loses footage that cannot be recovered, and that is not a thing
to find out on site.
"""

import logging
import time
from pathlib import Path

log = logging.getLogger("capture")

READY = "ready"
TRIGGERED = "triggered"


def plan_pruning(segments, state, keep_from=None, ready_keep=1,
                 in_flight_s=75.0, segment_seconds=60, pinned=frozenset(),
                 now=None):
    """Which segments may be deleted.

    segments   list of (path, start_datetime, mtime)
    state      READY or TRIGGERED
    keep_from  while triggered, the earliest start time to preserve
    ready_keep how many completed segments to hold while ready

    The segment being written is never a candidate: it is identified by a
    recent mtime rather than by name, because only ffmpeg knows when it has
    finished with one.
    """
    now = time.time() if now is None else now
    # Segments a queued or running concat still needs. Going back to ready
    # does not mean the clip has been cut yet: the worker waits for the last
    # segment to stop growing, and until it finishes these files are the only
    # copy of that event.
    def _key(p):
        try:
            return str(Path(p).resolve())
        except OSError:               # a file deleted under us is not pinned
            return str(p)
    pinned = {_key(p) for p in pinned}
    segments = [s for s in segments if _key(s[0]) not in pinned]
    ordered = sorted(segments, key=lambda s: s[1])

    live = [s for s in ordered if (now - s[2]) < in_flight_s]
    settled = [s for s in ordered if (now - s[2]) >= in_flight_s]

    if state == TRIGGERED:
        # Keep everything from the incident. Anything older than the window is
        # still fair game -- it belongs to the quiet period before it started.
        if keep_from is None:
            return []
        # Compare the segment's END, not its start. A trigger at 23:03:30 lands
        # inside the segment that began at 23:03:00, and that segment holds all
        # of the pre-roll -- deleting it would throw away the approach.
        import datetime as _dt
        span = _dt.timedelta(seconds=segment_seconds)
        return [s[0] for s in settled if (s[1] + span) <= keep_from]

    # Ready: hold the newest `ready_keep` completed segments as pre-roll.
    if ready_keep <= 0:
        return [s[0] for s in settled]
    return [s[0] for s in settled[:-ready_keep]] if len(settled) > ready_keep else []


class CaptureManager:
    """Applies the pruning, and knows what belongs to the current incident."""

    def __init__(self, cfg, tier, clock=time.time):
        c = (cfg._get("capture", default={}) or {}) if hasattr(cfg, "_get") else (cfg or {})
        self.tier = tier
        self.ready_keep = int(c.get("ready_keep_clips", 1))
        self.in_flight_s = float(c.get("in_flight_s",
                                       (cfg.segment_seconds if hasattr(cfg, "segment_seconds") else 60) + 15))
        self._clock = clock
        self.state = READY
        self.keep_from = None
        self.segment_seconds = int(
            cfg.segment_seconds if hasattr(cfg, "segment_seconds") else 60)
        self.deleted = 0
        self.kept_for_incident = 0

    # ------------------------------------------------------------------ state
    def set_triggered(self, keep_from):
        if self.state != TRIGGERED:
            log.info("capture: ready -> triggered (keeping everything from %s)",
                     keep_from.strftime("%H:%M:%S") if keep_from else "now")
        self.state = TRIGGERED
        self.keep_from = keep_from

    def set_ready(self):
        if self.state != READY:
            log.info("capture: triggered -> ready")
        self.state = READY
        self.keep_from = None

    # ------------------------------------------------------------------ files
    def _segments(self):
        from segments import parse_seg_start
        out = []
        for p in Path(self.tier.path).rglob("*.mp4"):
            t0 = parse_seg_start(p)
            if t0 is None:
                continue
            try:
                out.append((p, t0, p.stat().st_mtime))
            except FileNotFoundError:
                continue
        return out

    def sweep(self, pinned=frozenset()):
        """Delete what the current state does not need. Returns how many.

        `pinned` are the files a pending concat still needs -- without them,
        going back to ready deletes an event's own footage out from under the
        cut that is about to use it.
        """
        segs = self._segments()
        doomed = plan_pruning(segs, self.state, keep_from=self.keep_from,
                              ready_keep=self.ready_keep,
                              in_flight_s=self.in_flight_s,
                              segment_seconds=self.segment_seconds,
                              pinned=pinned,
                              now=self._clock())
        n = 0
        for p in doomed:
            try:
                p.unlink()
                n += 1
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning("could not delete %s: %s", p.name, e)
        if n:
            self.deleted += n
            log.debug("capture: pruned %d segment(s) in %s state", n, self.state)
        return n

    def incident_segments(self, start, end):
        """Segments overlapping an incident, oldest first, for concatenation."""
        from segments import list_segments_between
        segs = list_segments_between(start, end, self.tier.path,
                                     self.segment_seconds)
        self.kept_for_incident = len(segs)
        return segs

    def status(self):
        return {"capture_state": self.state,
                "segments_held": len(self._segments()),
                "segments_deleted": self.deleted}
