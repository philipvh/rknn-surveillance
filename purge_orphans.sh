#!/usr/bin/env bash
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

# Remove stills that no clip covers.
#
#   ./purge_orphans.sh            # show what would go
#   ./purge_orphans.sh --delete   # remove them
#
# A still only exists because something triggered, so every still should have
# a clip behind it. One case is legitimate: while an incident is still open,
# its stills exist and its clip has not been cut yet. Those are spared unless
# --include-open is given.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 - "$@" <<'PY'
import json, os, re, sys, time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

DELETE = "--delete" in sys.argv
INCLUDE_OPEN = "--include-open" in sys.argv
GRACE_S = 20 * 60          # comfortably longer than controller.max_hold_s

cfg = config.load(require_password=False)
events, dets = cfg.events_root, cfg.detections_root


def secs(name):
    m = re.search(r"(\d{2})-(\d{2})-(\d{2})(?!.*\d{2}-\d{2}-\d{2})", name)
    return None if not m else sum(int(v) * f for v, f in
                                  zip(m.groups(), (3600, 60, 1)))


def iso_secs(s):
    return int(s[11:13]) * 3600 + int(s[14:16]) * 60 + int(s[17:19])


# Spans a clip actually covers, from its sidecar where there is one.
spans = []
for clip in sorted(events.rglob("*.mp4")):
    if clip.name.endswith(".annotated.mp4"):
        continue
    side = clip.with_suffix(".json")
    day = clip.parent.name
    if side.exists():
        try:
            m = json.loads(side.read_text())
            spans.append((day, iso_secs(m["t0"]), iso_secs(m["window_end"])))
            continue
        except (OSError, ValueError, KeyError):
            pass
    m = re.search(r"clip_\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})"
                  r"_\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})", clip.name)
    if m:
        a = sum(int(v) * f for v, f in zip(m.groups()[:3], (3600, 60, 1)))
        b = sum(int(v) * f for v, f in zip(m.groups()[3:], (3600, 60, 1)))
        spans.append((day, a, b))

now = time.time()
orphans, covered, spared = [], 0, 0
for p in sorted(dets.rglob("*.jpg")):
    s = secs(p.name)
    if s is None:
        continue
    if any(d == p.parent.name and a <= s <= b for d, a, b in spans):
        covered += 1
        continue
    if not INCLUDE_OPEN and (now - p.stat().st_mtime) < GRACE_S:
        spared += 1
        continue
    orphans.append(p)

size = sum(p.stat().st_size for p in orphans)
print(f"  covered by a clip : {covered}")
print(f"  orphaned          : {len(orphans)}  ({size/1048576:.0f} MB)")
if spared:
    print(f"  spared as recent  : {spared}   (an incident may still be open;"
          f" --include-open removes these too)")

if not DELETE:
    print("\n  nothing changed. Re-run with --delete to remove them.")
    sys.exit(0)

gone = 0
for p in orphans:
    try:
        p.unlink()
        gone += 1
    except OSError as e:
        print(f"  could not remove {p.name}: {e}")
for d in sorted(dets.rglob("*"), key=lambda q: len(q.parts), reverse=True):
    if d.is_dir() and not any(d.iterdir()):
        try:
            d.rmdir()
        except OSError:
            pass
print(f"\n  removed {gone} still(s), freed {size/1048576:.0f} MB")
PY
