#!/usr/bin/env python3
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

"""Rebuild an annotated clip from the per-second stills of one event.

For events whose recorded segments are already gone but whose stills survive:
without this the media browser shows a run of thumbnails and no video at all.
The stills are written once per second, so the clip is encoded at 1 fps and a
wall-clock offset is the same number of seconds into the file.

    ./rebuild_from_stills.py 2026-08-20 13:22:00 13:22:30
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config                              # noqa: E402

STAMP = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})\.jpg$")


def main(argv):
    if len(argv) != 4:
        print(__doc__)
        return 2
    day, t_from, t_to = argv[1], argv[2], argv[3]
    cfg = config.load(require_password=False)
    src = cfg.detections_root / day
    if not src.is_dir():
        print(f"no stills directory for {day}")
        return 1

    lo = dt.datetime.fromisoformat(f"{day} {t_from}")
    hi = dt.datetime.fromisoformat(f"{day} {t_to}")
    frames = []
    for p in sorted(src.glob("*.jpg")):
        m = STAMP.search(p.name)
        if not m:
            continue
        when = dt.datetime.fromisoformat(
            f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}")
        if lo <= when <= hi:
            frames.append((when, p))
    if not frames:
        print(f"no stills between {t_from} and {t_to}")
        return 1

    t0, end = frames[0][0], frames[-1][0]
    stem = (f"clip_{t0.strftime('%Y-%m-%d_%H-%M-%S')}"
            f"_{end.strftime('%Y-%m-%d_%H-%M-%S')}")
    out_dir = cfg.events_root / day
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{stem}.annotated.mp4"
    if out.exists():
        print(f"already there: {out.name}")
        return 0

    # A concat list rather than a glob, so a gap in the stills does not
    # silently shift every later frame earlier in the clip.
    with tempfile.TemporaryDirectory() as td:
        lst = Path(td) / "frames.txt"
        lines = []
        for i, (when, p) in enumerate(frames):
            lines.append(f"file '{p}'")
            if i + 1 < len(frames):
                gap = (frames[i + 1][0] - when).total_seconds()
                lines.append(f"duration {max(0.04, gap):.3f}")
        lines.append(f"file '{frames[-1][1]}'")
        lst.write_text("\n".join(lines) + "\n")

        tmp = out.with_suffix(".mp4.tmp")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
               "-f", "concat", "-safe", "0", "-i", str(lst),
               "-vsync", "vfr", "-pix_fmt", "yuv420p",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-movflags", "+faststart", "-f", "mp4", str(tmp)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            print("ffmpeg failed:\n" + (r.stderr or "")[-2000:])
            tmp.unlink(missing_ok=True)
            return 1
        tmp.replace(out)

    side = out_dir / f"{stem}.json"
    if not side.exists():
        side.write_text(json.dumps({
            "t0": t0.isoformat(timespec="seconds"),
            "annotated_t0": t0.isoformat(timespec="seconds"),
            "window_start": t0.isoformat(timespec="seconds"),
            "window_end": end.isoformat(timespec="seconds"),
            "segments": 0,
            "rebuilt_from_stills": True,
        }, indent=1))

    print(f"{out.name}: {len(frames)} still(s), "
          f"{out.stat().st_size / 1e6:.1f} MB, "
          f"{(end - t0).total_seconds():.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
