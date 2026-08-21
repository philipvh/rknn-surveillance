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

"""Check the PIR wiring, before and after trusting it in code.

    ./trigger_cli.py chips          # what GPIO chips and lines exist
    ./trigger_cli.py doctor         # is the configured line usable?
    ./trigger_cli.py read           # one reading, raw and interpreted
    ./trigger_cli.py watch          # live events -- wave at the sensor

'watch' is the one to run on site. Wave at the PIR: the lights should come on
and a trigger should appear with a duration when they go off again.
"""

import argparse
import logging
import sys
import time

import config
import trigger as trig


def conf(cfg):
    return (cfg._get("trigger_input", default={}) or {})


def cmd_chips(args):
    """List chips and lines. Equivalent to gpiodetect + gpioinfo."""
    try:
        import gpiod
    except ImportError:
        print("python gpiod bindings not installed.")
        print("  sudo apt install gpiod python3-libgpiod")
        print("Command-line equivalents:  gpiodetect ; gpioinfo")
        return 1

    import glob, os
    chips = sorted(glob.glob("/dev/gpiochip*"))
    if not chips:
        print("no /dev/gpiochip* found")
        return 1
    for path in chips:
        name = os.path.basename(path)
        try:
            if hasattr(gpiod, "Chip"):
                c = gpiod.Chip(path if _is_v2() else name)
                label = getattr(c, "label", None) or getattr(
                    getattr(c, "get_info", lambda: None)(), "label", "?")
                nlines = (c.get_info().num_lines if _is_v2()
                          else c.num_lines())
                print(f"  {name:14s} {label!s:24s} {nlines} lines")
                c.close()
        except Exception as e:
            print(f"  {name:14s} could not open: {e}")
    print("\nUse 'gpioinfo' to map a header pin to a line number, then set")
    print("trigger_input.chip and trigger_input.line in config.yaml.")
    return 0


def _is_v2():
    try:
        import gpiod
        return hasattr(gpiod, "request_lines")
    except ImportError:
        return False


def cmd_doctor(args):
    cfg = config.load(require_password=False)
    c = conf(cfg)
    print(f"config: chip={c.get('chip')} line={c.get('line')} "
          f"active_low={c.get('active_low')} bias={c.get('bias')}")
    print(f"        debounce={c.get('debounce_s')}s "
          f"min_active={c.get('min_active_s')}s")

    if not c.get("enabled", True):
        print("\ntrigger_input.enabled is false -- nothing will be watched.")
        return 0

    try:
        import gpiod  # noqa: F401
        print(f"bindings: libgpiod {'v2' if _is_v2() else 'v1'}")
    except ImportError:
        print("bindings: MISSING -- sudo apt install python3-libgpiod")

    try:
        b = trig.open_backend(c.get("chip"), c.get("line"), c.get("bias", "pull-up"))
    except trig.BackendUnavailable as e:
        print(f"\nCANNOT OPEN THE LINE:\n  {e}")
        print("\nThings to check:")
        print("  * is the user in the 'gpio' group?  groups")
        print("  * does the chip exist?              gpiodetect")
        print("  * is the line already claimed?      gpioinfo | grep -w used")
        return 1

    raw = b.read()
    b.close()
    logical = (raw == 0) if c.get("active_low", True) else (raw == 1)
    print(f"\nline reads raw={raw} -> {'ACTIVE' if logical else 'idle'}")
    if logical:
        print("  The line is active right now. If nobody is in front of the")
        print("  sensor, active_low is probably set the wrong way round.")
    print(f"\nCross-check with libgpiod directly:")
    print(f"  gpiomon {c.get('chip')} {c.get('line')}")
    return 0


def cmd_read(args):
    cfg = config.load(require_password=False)
    c = conf(cfg)
    b = trig.open_backend(c.get("chip"), c.get("line"), c.get("bias", "pull-up"))
    raw = b.read()
    b.close()
    logical = (raw == 0) if c.get("active_low", True) else (raw == 1)
    print(f"raw={raw}  logical={'ACTIVE' if logical else 'idle'}")
    return 0


def cmd_watch(args):
    cfg = config.load(require_password=False)
    started = time.time()

    def on_event(ev):
        stamp = time.strftime("%H:%M:%S", time.localtime(ev.at))
        if ev.kind == "active":
            print(f"{stamp}  ACTIVE     lights should be on now")
        elif ev.kind == "inactive":
            print(f"{stamp}  released   after {ev.duration:.1f}s")
        elif ev.kind == "stuck":
            print(f"{stamp}  STUCK      {ev.detail}")
        else:
            print(f"{stamp}  {ev.kind}  {ev.detail}")
        sys.stdout.flush()

    t = trig.TriggerInput(cfg, on_event=on_event)
    t.start()
    time.sleep(0.5)
    if not t.available:
        print(f"no usable GPIO: {t.error}", file=sys.stderr)
        print("Run './trigger_cli.py doctor' for what to check.", file=sys.stderr)
        return 1

    print("watching. Wave at the sensor. Ctrl-C to stop.\n")
    try:
        while True:
            time.sleep(args.status_every)
            if args.verbose:
                s = t.status()
                print(f"           [{time.strftime('%H:%M:%S')}] "
                      f"active={s['active']} activations={s['activations']} "
                      f"blips={s['rejected_blips']} errors={s['read_errors']}")
    except KeyboardInterrupt:
        pass
    finally:
        t.stop()
        s = t.status()
        elapsed = time.time() - started
        print(f"\nwatched for {elapsed:.0f}s: {s['activations']} activation(s), "
              f"{s['rejected_blips']} blip(s) filtered, "
              f"{s['read_errors']} read error(s)")
    return 0


def main(argv=None):
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-7s %(name)s %(message)s")
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("chips").set_defaults(fn=cmd_chips)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("read").set_defaults(fn=cmd_read)
    w = sub.add_parser("watch")
    w.add_argument("-v", "--verbose", action="store_true",
                   help="print a status line periodically")
    w.add_argument("--status-every", type=float, default=10.0)
    w.set_defaults(fn=cmd_watch)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except trig.BackendUnavailable as e:
        print(f"GPIO unavailable: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
