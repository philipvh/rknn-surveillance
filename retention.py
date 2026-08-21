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

"""Retention: decide what to delete, and never delete the evidence.

The old logic in surveillance_core.py deleted the oldest whole day under disk
pressure. For vandalism that is exactly backwards -- damage is discovered days
later, so the oldest footage is often the footage about to be asked for, and
the event clips are the one thing that must survive a full disk.

This module is split so the decision is pure and testable: plan_deletions()
takes a list of files and a disk state and returns what to remove and why,
touching no filesystem. scan() and apply() are the thin impure edges.
"""

# The board runs Python 3.9; `float | None` in an annotation is a
# runtime TypeError before 3.10 unless annotations are deferred.
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import logging
import shutil
import time

log = logging.getLogger("retention")

# A segment ffmpeg is still writing has a recent mtime. Never delete a file
# younger than the segment length plus this margin.
IN_FLIGHT_MARGIN_S = 15.0


@dataclass(frozen=True)
class Candidate:
    path: Path
    mtime: float
    size: int
    tier_name: str
    tier_index: int          # position in config order = sacrifice order
    max_age_s: float | None
    protected: bool


@dataclass(frozen=True)
class Deletion:
    path: Path
    reason: str              # 'age' | 'pressure'
    tier_name: str
    size: int


def plan_deletions(candidates, *, now, free_bytes, total_bytes,
                   target_free_ratio, pinned=frozenset(), min_age_s=0.0):
    """Return (deletions, warnings). Pure -- no filesystem access.

    Two passes:
      1. Age. Every tier obeys its own max_age_days, protected or not.
      2. Pressure. If the disk is still fuller than we want, delete oldest-first
         from unprotected tiers in config order. Protected tiers are never
         touched here, even if that means the target is not met -- in which case
         a warning comes back, because a full disk is a problem to fix, not a
         reason to throw away the only evidence of a break-in.
    """
    deletions, warnings = [], []
    doomed = set()
    freed = 0

    def eligible(c):
        if c.path in pinned:
            return False
        if (now - c.mtime) < min_age_s:      # still being written
            return False
        return True

    # ---- pass 1: age ----
    for c in candidates:
        if not eligible(c) or c.max_age_s is None:
            continue
        if (now - c.mtime) > c.max_age_s:
            doomed.add(c.path)
            freed += c.size
            deletions.append(Deletion(c.path, "age", c.tier_name, c.size))

    # ---- pass 2: pressure ----
    want_free = target_free_ratio * total_bytes
    shortfall = want_free - (free_bytes + freed)
    if shortfall > 0:
        pool = [c for c in candidates
                if c.path not in doomed and eligible(c) and not c.protected]
        pool.sort(key=lambda c: (c.tier_index, c.mtime))
        for c in pool:
            if shortfall <= 0:
                break
            doomed.add(c.path)
            shortfall -= c.size
            deletions.append(Deletion(c.path, "pressure", c.tier_name, c.size))

        if shortfall > 0:
            protected_left = sum(
                c.size for c in candidates
                if c.protected and c.path not in doomed and eligible(c))
            warnings.append(
                f"disk still {_h(shortfall)} short of the {target_free_ratio:.0%} "
                f"free target after deleting everything unprotected. "
                f"{_h(protected_left)} of protected data was left alone. "
                f"Fix the disk -- do not widen the policy.")

    return deletions, warnings


def scan(tiers, now=None):
    """Walk the tier directories and build Candidates. Impure."""
    now = time.time() if now is None else now
    out = []
    for idx, t in enumerate(tiers):
        if not t.path.exists():
            continue
        for p in t.path.rglob("*"):
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            out.append(Candidate(
                path=p, mtime=st.st_mtime, size=st.st_size,
                tier_name=t.name, tier_index=idx,
                max_age_s=t.max_age_s, protected=t.protected,
            ))
    return out


def disk_state(path):
    u = shutil.disk_usage(str(path))
    return u.free, u.total


def apply(deletions, dry_run=False):
    """Delete the planned files. Returns bytes actually freed."""
    freed = 0
    for d in deletions:
        if dry_run:
            log.info("[dry-run] would remove (%s) %s", d.reason, d.path)
            freed += d.size
            continue
        try:
            d.path.unlink()
            freed += d.size
            log.info("removed (%s, tier=%s) %s", d.reason, d.tier_name, d.path)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("could not remove %s: %s", d.path, e)
    return freed


def prune_empty_dirs(tiers):
    """Tidy up day directories left behind. Never removes a tier root."""
    for t in tiers:
        if not t.path.exists():
            continue
        for p in sorted(t.path.rglob("*"), key=lambda q: len(q.parts), reverse=True):
            if p.is_dir() and p != t.path and not any(p.iterdir()):
                try:
                    p.rmdir()
                except OSError:
                    pass


def run_once(cfg, pinned=frozenset(), dry_run=False, now=None):
    """One maintenance sweep. Returns (deletions, warnings, bytes_freed)."""
    now = time.time() if now is None else now
    tiers = cfg.tiers
    candidates = scan(tiers, now=now)
    free, total = disk_state(tiers[0].path.parent)
    deletions, warnings = plan_deletions(
        candidates, now=now, free_bytes=free, total_bytes=total,
        target_free_ratio=cfg.target_free_ratio, pinned=pinned,
        min_age_s=cfg.segment_seconds + IN_FLIGHT_MARGIN_S,
    )
    freed = apply(deletions, dry_run=dry_run)
    if not dry_run:
        prune_empty_dirs(tiers)
    for w in warnings:
        log.error("%s", w)
    if deletions:
        by_tier = {}
        for d in deletions:
            by_tier[d.tier_name] = by_tier.get(d.tier_name, 0) + 1
        log.info("pruned %d files (%s), freed %s",
                 len(deletions), by_tier, _h(freed))
    return deletions, warnings, freed


def _h(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024
