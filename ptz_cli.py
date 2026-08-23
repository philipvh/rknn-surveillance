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

"""Command-line PTZ control, for setting the camera up and proving it behaves.

    ./ptz_cli.py status
    ./ptz_cli.py presets
    ./ptz_cli.py goto Home
    ./ptz_cli.py move left --duration 1.0
    ./ptz_cli.py stop
    ./ptz_cli.py zoom in --duration 0.5
    ./ptz_cli.py snapshot /tmp/shot.jpg
    ./ptz_cli.py budget
    ./ptz_cli.py probe
    ./ptz_cli.py selftest

Nothing here depends on the detector, the recorder or the NPU, so presets can
be set up and the safety behaviour verified before anything else exists.
"""

import argparse
import json
import logging
import sys
import time

import config
import ptz as ptz_mod


def build(args, **kw):
    cfg = config.load()
    return ptz_mod.PTZ(cfg, **kw)


def cmd_status(args):
    p = build(args)
    print(json.dumps(p.status(), indent=2))
    try:
        print("\ndevice state:")
        for k, v in sorted(p.dev_state().items()):
            if k != "_raw":
                print(f"  {k:24s} {v}")
    except ptz_mod.PTZError as e:
        print(f"  (getDevState failed: {e})")
    p.close()
    return 0


def cmd_presets(args):
    p = build(args)
    try:
        names = p.list_presets()
    except ptz_mod.PTZError as e:
        print(f"could not list presets: {e}", file=sys.stderr)
        return 1
    finally:
        p.close()
    if not names:
        print("no presets set. Create them in the camera's web UI, or:")
        print("  ./ptz_cli.py addpreset Home")
        return 0
    want = set(p.p.get("scan_presets", []) or []) | {p.p.get("home_preset", "Home")}
    for n in names:
        print(f"  {n}{'' if n in want else '   (not used by config.yaml)'}")
    missing = sorted(want - set(names))
    if missing:
        print(f"\nconfig.yaml refers to presets the camera does not have: "
              f"{', '.join(missing)}")
        return 1
    return 0


def cmd_goto(args):
    p = build(args)
    try:
        p.goto_preset(args.name, source="manual")
        print(f"moving to {args.name!r}")
        time.sleep(p.preset_estimate_s)
        print("done")
    except ptz_mod.PTZError as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1
    finally:
        p.close()
    return 0


def cmd_move(args):
    p = build(args)
    try:
        end = time.monotonic() + args.duration
        print(f"moving {args.direction} for {args.duration:.1f}s "
              f"(refreshing the deadline, as the wall panel does)")
        while time.monotonic() < end:
            p.move(args.direction, source="manual")
            time.sleep(p.move_deadline_s / 3)
        p.stop(reason="cli move finished")
        print("stopped")
    except ptz_mod.BudgetExceeded as e:
        print(f"refused: {e}", file=sys.stderr)
        return 3
    except ptz_mod.PTZError as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1
    finally:
        p.close()
    return 0


def cmd_zoom(args):
    p = build(args)
    try:
        end = time.monotonic() + args.duration
        while time.monotonic() < end:
            p.zoom(args.direction, source="manual")
            time.sleep(p.move_deadline_s / 3)
        p.stop(reason="cli zoom finished")
        print("stopped")
    except ptz_mod.PTZError as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1
    finally:
        p.close()
    return 0


def cmd_stop(args):
    p = build(args, stop_on_start=False)
    try:
        p.stop(reason="cli")
        print("stop sent")
    except ptz_mod.PTZError as e:
        print(f"STOP FAILED: {e}", file=sys.stderr)
        return 1
    finally:
        p._closed = True
    return 0


def cmd_addpreset(args):
    p = build(args)
    try:
        p.add_preset(args.name)
        print(f"preset {args.name!r} saved at the current position")
    except ptz_mod.PTZError as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1
    finally:
        p.close()
    return 0


def cmd_snapshot(args):
    p = build(args)
    try:
        data = p.snapshot()
    except ptz_mod.PTZError as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1
    finally:
        p.close()
    if not data.startswith(b"\xff\xd8"):
        print(f"camera did not return a JPEG ({data[:60]!r})", file=sys.stderr)
        return 1
    with open(args.out, "wb") as f:
        f.write(data)
    print(f"wrote {len(data)} bytes to {args.out}")
    return 0


def cmd_budget(args):
    p = build(args, stop_on_start=False)
    b = p.budget
    for src in ("auto", "manual"):
        print(f"  {src:7s} {b.spent(src):6.1f}s used of {b.limits[src]:.0f}s "
              f"per hour, {b.remaining(src):.1f}s left")
    print(f"  minimum interval between automatic scans: "
          f"{b.min_scan_interval_s:.0f}s")
    p._closed = True
    return 0


def cmd_probe(args):
    """Ask the camera which CGI commands it actually supports.

    The Foscam CGI reference is a separate document that does not ship with
    this camera's manual, so the command names in ptz.py are expectations
    until this says otherwise. Read-only: nothing here moves the camera.
    """
    p = build(args, stop_on_start=False)
    checks = ["getDevInfo", "getDevState", "getPTZPresetPointList",
              "getPTZSpeed", "getPTZCruiseMapList", "getMotionDetectConfig",
              "getImageSetting", "getPortInfo"]
    ok = 0
    for c in checks:
        try:
            out = p._call(c)
            keys = [k for k in out if k != "_raw"][:6]
            print(f"  {c:26s} OK    {keys}")
            ok += 1
        except ptz_mod.PTZError as e:
            print(f"  {c:26s} FAIL  {e}")
    print(f"\n{ok}/{len(checks)} read-only commands answered.")
    print("Movement commands are not probed here -- use 'selftest' for that.")
    p._closed = True
    return 0 if ok else 1


def cmd_selftest(args):
    """Prove the safety behaviour on the real camera. This MOVES the camera."""
    if not args.yes:
        print("This will move the camera. Re-run with --yes to confirm.")
        return 2
    p = build(args)
    fails = []

    print("1. move left 1s with refreshes, then stop")
    try:
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            p.move("left", source="manual")
            time.sleep(p.move_deadline_s / 3)
        p.stop(reason="selftest 1")
        print("   ok")
    except ptz_mod.PTZError as e:
        fails.append(f"1: {e}")
        print(f"   FAILED: {e}")

    print("2. move right, then stop refreshing -- the watchdog must stop it")
    before = p.watchdog_stops
    try:
        p.move("right", source="manual")
        time.sleep(p.move_deadline_s * 3 + 0.5)
        if p.moving:
            fails.append("2: still moving after the deadline")
            print("   FAILED: still moving")
        elif p.watchdog_stops > before:
            print(f"   ok (watchdog stopped it, {p.watchdog_stops - before} stop)")
        else:
            print("   ok (stopped, though not attributed to the watchdog)")
    except ptz_mod.PTZError as e:
        fails.append(f"2: {e}")
        print(f"   FAILED: {e}")

    print("3. budget refusal")
    p.budget.record(p.budget.limits["auto"], "auto")
    try:
        p.move("up", source="auto")
        fails.append("3: an over-budget move was allowed")
        print("   FAILED: move was allowed")
        try:
            p.stop()
        except ptz_mod.PTZError:
            pass
    except ptz_mod.BudgetExceeded:
        print("   ok (refused)")
    except ptz_mod.PTZError as e:
        # move() performs a safety stop before refusing, and will surface a
        # failure of that stop in preference to the budget error. If we land
        # here, stopping is broken -- which matters far more than the budget.
        fails.append(f"3: stop failed while refusing an over-budget move: {e}")
        print(f"   FAILED: {e}")

    print("4. return home")
    try:
        p.go_home(source="manual")
        time.sleep(p.preset_estimate_s)
        print("   ok")
    except ptz_mod.PTZError as e:
        fails.append(f"4: {e}")
        print(f"   FAILED: {e}")

    p.close()
    print()
    if fails:
        print("SELFTEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("SELFTEST PASSED")
    print("\nStill to check by hand, because no in-process code can cover it:")
    print("  ./ptz_cli.py move left --duration 30   # then kill -9 this process")
    print("  The camera keeps turning until something stops it. Then run any")
    print("  ptz_cli command, or start the service: the driver sends a stop")
    print("  before anything else. That is the SIGKILL safety net.")
    return 0


def main(argv=None):
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-7s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("presets").set_defaults(fn=cmd_presets)
    sub.add_parser("stop").set_defaults(fn=cmd_stop)
    sub.add_parser("budget").set_defaults(fn=cmd_budget)
    sub.add_parser("probe").set_defaults(fn=cmd_probe)

    g = sub.add_parser("goto"); g.add_argument("name"); g.set_defaults(fn=cmd_goto)
    a = sub.add_parser("addpreset"); a.add_argument("name")
    a.set_defaults(fn=cmd_addpreset)

    m = sub.add_parser("move")
    # No fixed choices: which directions exist is the camera's business, and
    # a custom backend may have its own. PTZ.move validates against the
    # backend in use and names what it does accept.
    m.add_argument("direction", metavar="DIRECTION",
                   help="up, down, left, right (and diagonals on some cameras)")
    m.add_argument("--duration", type=float, default=1.0)
    m.set_defaults(fn=cmd_move)

    z = sub.add_parser("zoom")
    z.add_argument("direction", metavar="DIRECTION", help="in or out")
    z.add_argument("--duration", type=float, default=0.5)
    z.set_defaults(fn=cmd_zoom)

    s = sub.add_parser("snapshot"); s.add_argument("out")
    s.set_defaults(fn=cmd_snapshot)

    t = sub.add_parser("selftest")
    t.add_argument("--yes", action="store_true", help="confirm the camera may move")
    t.set_defaults(fn=cmd_selftest)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
