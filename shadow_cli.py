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

"""Read the shadow log: what the system would have sent, and why not.

    ./shadow_cli.py summary            # the whole log
    ./shadow_cli.py summary --days 14  # the fortnight Phase 6 asks for
    ./shadow_cli.py list --alerts-only
    ./shadow_cli.py label              # mark which ones were really worth it
    ./shadow_cli.py tune               # what other thresholds would have done

Phase 6 is a fortnight of waiting, then 'summary' to see the volume, 'label'
to record which incidents were real, and 'tune' to find the thresholds that
would have caught the real ones without the rest -- from data already in
hand, rather than by guessing and waiting another fortnight.
"""

import argparse
import datetime as dt
import sys
from collections import Counter

import config
import review
from alerts import ShadowLog
from schedule import Schedule


def load(args):
    cfg = config.load(require_password=False)
    root = cfg.resolve(cfg._get("alerts", "shadow_root", default="shadow"))
    since = (dt.datetime.now() - dt.timedelta(days=args.days)) if args.days else None
    return root, ShadowLog(root).read(since=since)


def load_full(args):
    cfg = config.load(require_password=False)
    root = cfg.resolve(cfg._get("alerts", "shadow_root", default="shadow"))
    since = (dt.datetime.now() - dt.timedelta(days=args.days)) if args.days else None
    rows = ShadowLog(root).read(since=since)
    return cfg, root, rows, Schedule.from_config(cfg), review.Labels(root)


def _pct(x):
    return "  -  " if x is None else f"{x*100:4.0f}%"


def cmd_summary(args):
    root, rows = load(args)
    if not rows:
        print(f"no shadow log entries in {root}")
        print("Nothing has been detected yet, or the service has not run.")
        return 0

    alerts = [r for r in rows if r["would_alert"]]
    first = min(r["at"] for r in rows)
    last = max(r["at"] for r in rows)
    days = max(1, (dt.datetime.fromisoformat(last)
                   - dt.datetime.fromisoformat(first)).days + 1)

    print(f"shadow log: {root}")
    print(f"period    : {first[:16]} -> {last[:16]}  ({days} day(s))")
    print(f"incidents : {len(rows)}")
    print(f"would have alerted: {len(alerts)}  "
          f"({len(alerts)/days:.1f} per day)")
    print()

    print("blocked by:")
    for reason, n in Counter(r["failed"] for r in rows if not r["would_alert"]).most_common():
        print(f"  {reason or '(none)':16s} {n:5d}")

    print("\nby hour of day:")
    hours = Counter(int(r["at"][11:13]) for r in rows)
    peak = max(hours.values()) if hours else 1
    for h in range(24):
        n = hours.get(h, 0)
        if n:
            bar = "#" * max(1, int(20 * n / peak))
            a = sum(1 for r in rows if int(r["at"][11:13]) == h and r["would_alert"])
            print(f"  {h:02d}:00 {bar:<20s} {n:4d}" + (f"   ({a} would alert)" if a else ""))

    labels = review.Labels(root)
    counts = labels.counts(rows)
    if counts.get(review.REAL) or counts.get(review.FALSE):
        print(f"\nlabelled: {counts.get(review.REAL,0)} real, "
              f"{counts.get(review.FALSE,0)} false, "
              f"{counts.get(review.UNSURE,0)} unsure, "
              f"{counts.get(None,0)} unlabelled")
        missed = [r for r in rows
                  if labels.get(r) == review.REAL and not r["would_alert"]]
        if missed:
            print(f"  MISSED {len(missed)} incident(s) you marked real:")
            for r in missed:
                print(f"    {r['at'][:16]}  {r['summary']}  "
                      f"[blocked by {r['failed']}]")
    else:
        print("\nnothing labelled yet -- run './shadow_cli.py label' so that")
        print("'tune' can tell you about being right, not just about volume.")

    if alerts:
        print("\nwould have alerted:")
        for r in alerts:
            mark = {review.REAL: " (real)", review.FALSE: " (FALSE ALARM)"}.get(
                labels.get(r), "")
            print(f"  {r['at'][:16]}  {r['summary']}{mark}")

    print()
    per_day = len(alerts) / days
    if per_day <= 1:
        print(f"VERDICT: {per_day:.1f} alerts a day. That is a rate worth")
        print("         building the radio link for.")
    elif per_day <= 3:
        print(f"VERDICT: {per_day:.1f} alerts a day. Tolerable, but read the")
        print("         list above and see whether the extras are real.")
    else:
        print(f"VERDICT: {per_day:.1f} alerts a day would be a flood again.")
        print("         Tighten alerts.* in config.yaml and run another fortnight")
        print("         before spending money on radios.")
    return 0


def cmd_list(args):
    _, rows = load(args)
    if args.alerts_only:
        rows = [r for r in rows if r["would_alert"]]
    for r in rows:
        mark = "ALERT " if r["would_alert"] else "  -   "
        why = "" if r["would_alert"] else f"  [{r['failed']}: {r['detail']}]"
        print(f"{mark}{r['at'][:19]}  {r['summary']}{why}")
    print(f"\n{len(rows)} row(s)")
    return 0


def cmd_label(args):
    """Walk unlabelled incidents and record which were really worth an alert."""
    cfg, root, rows, sched, labels = load_full(args)
    if not rows:
        print("nothing in the shadow log yet")
        return 0
    todo = [r for r in rows if labels.get(r) is None]
    if args.relabel:
        todo = rows
    if args.alerts_only:
        todo = [r for r in todo if r["would_alert"]]
    if not todo:
        print("everything is labelled. './shadow_cli.py tune' is the next step.")
        return 0

    print(f"{len(todo)} incident(s) to review.")
    print("  r = real (worth waking someone)    f = false alarm")
    print("  u = unsure    s = skip    q = save and quit\n")
    try:
        for i, row in enumerate(todo, 1):
            mark = "WOULD ALERT" if row["would_alert"] else f"blocked: {row['failed']}"
            print(f"[{i}/{len(todo)}] {row['at'][:19]}  {mark}")
            print(f"        {row['summary']}")
            print(f"        pir={row['pir']}  sightings={row['sightings']}  "
                  f"conf={row['max_confidence']:.2f}  {row['duration_s']}s")
            if row.get("snapshot"):
                print(f"        snapshot: {row['snapshot']}")
            if row.get("clip"):
                print(f"        clip:     {row['clip']}")
            while True:
                try:
                    a = input("        real/false/unsure [r/f/u/s/q]: ").strip().lower()
                except EOFError:
                    a = "q"
                if a in ("r", "f", "u", "s", "q"):
                    break
            if a == "q":
                break
            if a != "s":
                labels.set(row, {"r": review.REAL, "f": review.FALSE,
                                 "u": review.UNSURE}[a])
            print()
    finally:
        labels.save()
        print(f"\nsaved to {labels.path}")
    return 0


def cmd_tune(args):
    """Replay the log under other thresholds."""
    cfg, root, rows, sched, labels = load_full(args)
    if not rows:
        print("nothing in the shadow log yet")
        return 0
    if args.set:
        overrides = {}
        for item in args.set:
            k, _, v = item.partition("=")
            overrides[k.strip()] = _coerce(v.strip())
        results = review.replay(rows, cfg, sched, overrides)
        s = review.score(results, labels)
        print(f"with {overrides}:")
        _print_score(s)
        alerts = [r for r, d in results if d.would_alert]
        if alerts:
            print("\n  would have alerted:")
            for r in alerts:
                v = labels.get(r)
                tag = {review.REAL: " (real)", review.FALSE: " (FALSE)"}.get(v, "")
                print(f"    {r['at'][:16]}  {r['summary']}{tag}")
        return 0

    sweeps = review.sweep(rows, cfg, sched, labels)
    base = sweeps.pop("baseline")
    print(f"{base['days']} day(s), {len(rows)} incident(s)\n")
    print("current settings:")
    _print_score(base)
    have_labels = any(labels.get(r) for r in rows)
    print("\nvarying one setting at a time"
          + ("" if have_labels else "  (label some incidents for real/missed)"))
    for name, options in sweeps.items():
        print(f"\n  {name}")
        for s in options:
            mark = " <- current" if s["is_current"] else ""
            line = (f"    {str(s['value']):>6}  "
                    f"{s['alerts']:3d} alerts  {s['per_day']:5.2f}/day")
            if have_labels:
                line += (f"   real {s['real_alerted']}"
                         f"  missed {s['real_missed']}"
                         f"  false {s['false_alerted']}")
            print(line + mark)
    print("\nA setting that misses nothing real and cuts the false ones is the")
    print("one to take. Missing a real incident is much worse than one extra")
    print("message, so read the 'missed' column first.")
    return 0


def _print_score(s):
    print(f"    {s['alerts']} alert(s), {s['per_day']:.2f} per day")
    if s["real_alerted"] or s["real_missed"] or s["false_alerted"]:
        print(f"    real alerted {s['real_alerted']}, real MISSED "
              f"{s['real_missed']}, false alarms {s['false_alerted']}")
        if s["precision"] is not None:
            print(f"    precision {_pct(s['precision'])}, "
                  f"recall {_pct(s['recall'])}")


def _coerce(v):
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=0,
                    help="only the last N days (default: everything)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary").set_defaults(fn=cmd_summary)
    l = sub.add_parser("list")
    l.add_argument("--alerts-only", action="store_true")
    l.set_defaults(fn=cmd_list)

    lb = sub.add_parser("label")
    lb.add_argument("--relabel", action="store_true",
                    help="review incidents that already have a verdict")
    lb.add_argument("--alerts-only", action="store_true")
    lb.set_defaults(fn=cmd_label)

    t = sub.add_parser("tune")
    t.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="try one specific combination, e.g. --set min_confidence=0.8")
    t.set_defaults(fn=cmd_tune)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
