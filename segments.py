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

"""Locating recorded segments in time.

Separate from surveillance_core so it can be tested without importing rknn or
cv2 -- these are the functions that decide which footage ends up in a clip, so
they are worth testing on a machine that has no NPU.
"""

import datetime as dt
import logging
from pathlib import Path

log = logging.getLogger("segments")

TS_FMT = "%Y-%m-%d_%H-%M-%S"
DAY_FMT = "%Y-%m-%d"


def parse_seg_start(path):
    """Start time of a segment file, or None if the name is not a timestamp.

    Handles the current layout (<tier>/YYYY-MM-DD/HH-MM-SS.mp4) and the flat
    YYYY-MM-DD_HH-MM-SS.mp4 files the previous recorder wrote, so upgrading
    does not orphan footage that is already on disk.
    """
    p = Path(path)
    for candidate in (f"{p.parent.name}_{p.stem}", p.stem):
        try:
            return dt.datetime.strptime(candidate, TS_FMT)
        except ValueError:
            continue
    return None


def list_segments_between(start, end, tier_path, segment_seconds):
    """Segments overlapping [start, end], oldest first."""
    found = []
    for f in Path(tier_path).rglob("*.mp4"):
        t0 = parse_seg_start(f)
        if t0 is None:
            continue
        t1 = t0 + dt.timedelta(seconds=segment_seconds)
        if t0 <= end and t1 >= start:
            found.append((t0, f))
    found.sort(key=lambda x: x[0])
    return [f for _, f in found]


def pinned_paths(concat_mgr, tier_path, since, segment_seconds, now=None):
    """Files retention must not touch: anything queued for cutting, plus the
    segments covering a trigger window that is still open."""
    now = now or dt.datetime.now()
    pinned = set()
    try:
        for key in list(concat_mgr.in_progress):
            for part in key.split("|"):
                pinned.add(Path(part))
        for job in list(concat_mgr.q.queue):
            for f in job.files:
                pinned.add(Path(f).resolve())
    except Exception as e:            # bookkeeping must never stop a sweep
        log.debug("could not read the concat queue: %s", e)
    if since is not None:
        for f in list_segments_between(since, now, tier_path, segment_seconds):
            pinned.add(f.resolve())
    return pinned
