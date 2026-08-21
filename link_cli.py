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

"""Exercise the radio link without waiting for a burglary.

    ./link_cli.py genkey                 # a pre-shared key for secrets.yaml
    ./link_cli.py status                 # queue depth and settings
    ./link_cli.py queue                  # what is waiting to go out
    ./link_cli.py test                   # queue a test message
    ./link_cli.py drain                  # try to send everything, now
    ./link_cli.py decode <hex>           # read a frame off the air
    ./link_cli.py selftest               # both ends, on one desk, no radio

'selftest' is the plan's "testable entirely on the bench" step: it runs the
sender and the receiver against each other in memory, drops a packet to prove
the retry works, and replays one to prove it is rejected.
"""

import argparse
import logging
import os
import secrets
import sys

import config
import link
import transports


def cmd_genkey(args):
    key = secrets.token_urlsafe(32)
    print("Add to secrets.yaml at BOTH ends, and nowhere else:\n")
    print("link:")
    print(f'  psk: "{key}"')
    print("\nThe far end can also take it as TVW_LINK_PSK.")
    return 0


def _uplink(cfg):
    from uplink import Uplink
    return Uplink(cfg)


def cmd_status(args):
    cfg = config.load(require_password=False)
    c = cfg._get("link", default={}) or {}
    print(f"enabled     : {c.get('enabled', False)}")
    print(f"shadow_only : {cfg._get('alerts', 'shadow_only', default=True)}"
          "   (nothing is sent while this is true)")
    print(f"transport   : {c.get('transport')}"
          + (f"  {c.get('port')}" if c.get("transport") == "serial" else ""))
    print(f"psk         : {'set' if cfg.link_psk else 'MISSING'}")
    print(f"ack         : {c.get('require_ack', True)}, "
          f"timeout {c.get('ack_timeout_s', 8)}s, "
          f"{c.get('max_attempts', 4)} attempts")
    print(f"pacing      : one message per {c.get('min_tx_interval_s', 90)}s")
    u = _uplink(cfg)
    if u.outbox:
        print(f"queued      : {u.outbox.depth()}")
    return 0


def cmd_queue(args):
    cfg = config.load(require_password=False)
    u = _uplink(cfg)
    if not u.outbox:
        print("the link is not enabled")
        return 1
    msgs = u.outbox.pending()
    if not msgs:
        print("queue is empty")
        return 0
    for m in msgs:
        import datetime as dt
        age = dt.datetime.fromtimestamp(m.created).strftime("%Y-%m-%d %H:%M")
        print(f"  #{m.counter:<6} {link.TYPE_NAMES.get(m.msg_type, '?'):<10} "
              f"{age}  attempts={m.attempts}  "
              f"{m.meta.get('summary', '')}")
    print(f"\n{len(msgs)} message(s)")
    return 0


def cmd_test(args):
    cfg = config.load(require_password=False)
    u = _uplink(cfg)
    if not u.outbox:
        print("the link is not enabled in config.yaml")
        return 1
    m = u.send_test()
    print(f"queued {m!r}. Run './link_cli.py drain' or let the service send it.")
    return 0


def cmd_drain(args):
    cfg = config.load(require_password=False)
    u = _uplink(cfg)
    if not u.sender:
        print("the link is not enabled in config.yaml")
        return 1
    u.sender.min_tx_interval_s = 0.0        # this is a manual, deliberate act
    sent = 0
    while True:
        msg = u.outbox.peek()
        if msg is None:
            break
        if not u.sender.pump():
            print(f"could not send {msg!r}; it stays queued")
            break
        sent += 1
    print(f"sent {sent} message(s); {u.outbox.depth()} still queued")
    return 0


def cmd_decode(args):
    cfg = config.load(require_password=False)
    psk = cfg.link_psk or os.environ.get("TVW_LINK_PSK")
    if not psk:
        print("no pre-shared key configured", file=sys.stderr)
        return 2
    try:
        frame = bytes.fromhex(args.hex.replace(" ", ""))
    except ValueError:
        print("that is not hex", file=sys.stderr)
        return 2
    for direction, label in ((0, "club -> house"), (1, "house -> club")):
        try:
            t, c, body = link.decode(link.derive_key(psk), frame, direction)
        except link.LinkError:
            continue
        name = link.TYPE_NAMES.get(t, str(t))
        print(f"{label}: {name} #{c}")
        unpack = {link.ALERT: link.unpack_alert,
                  link.HEARTBEAT: link.unpack_heartbeat,
                  link.ACK: link.unpack_ack}.get(t)
        if unpack:
            for k, v in unpack(body).items():
                print(f"  {k:14s} {v}")
        return 0
    print("could not authenticate that frame with this key")
    return 1


def cmd_selftest(args):
    """Both ends against each other in memory. No radio, no camera."""
    import tempfile
    from pathlib import Path
    from outbox import Outbox, Sender
    from receiver import Notifier, Receiver

    logging.basicConfig(level=logging.WARNING)
    psk = "selftest-key"
    key = link.derive_key(psk)
    club, house = transports.LoopbackTransport.pair()

    class Collect(Notifier):
        def __init__(self):
            super().__init__(topic=None)
            self.pushes = []

        def push(self, title, message, priority="default", tags=""):
            self.pushes.append((title, message))
            print(f"    phone: [{priority}] {title} -- {message}")
            return True

    class Cfg:
        d = {"link": {"ack_timeout_s": 0.5, "max_attempts": 3,
                      "min_tx_interval_s": 0.0, "require_ack": True}}

        def _get(self, *keys, default=None):
            node = self.d
            for k in keys:
                if not isinstance(node, dict) or k not in node:
                    return default
                node = node[k]
            return node

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        box = Outbox(d / "spool")
        notifier = Collect()
        rx = Receiver(key, house, notifier, d / "rx.json")
        sender = Sender(Cfg(), box, club, key)
        fails = []

        def exchange(label):
            import threading
            got = {}

            def rxl():
                f = house.receive(timeout=2.0)
                if f:
                    got["t"] = rx.handle(f)
            t = threading.Thread(target=rxl); t.start()
            ok = sender.pump()
            t.join(timeout=3)
            print(f"  {label}: {'acknowledged' if ok else 'NOT acknowledged'}")
            return ok, got

        print("1. an alert reaches the far end")
        box.put(link.ALERT, link.pack_alert(__import__("time").time(), zone=1,
                                            count=1, confidence=0.93,
                                            duration_s=42, flags=link.F_PIR))
        ok, got = exchange("   alert")
        if not ok or not notifier.pushes:
            fails.append("the alert did not arrive")

        print("2. a lost packet is retried")
        box.put(link.ALERT, link.pack_alert(__import__("time").time()))
        club.drop_next(1)
        ok, _ = exchange("   first try (dropped)")
        if ok:
            fails.append("a dropped packet was reported as delivered")
        ok, _ = exchange("   retry")
        if not ok:
            fails.append("the retry did not get through")

        print("3. a replayed packet is rejected")
        before = len(notifier.pushes)
        replayed = club.sent[-1]
        rx.handle(replayed)
        if len(notifier.pushes) != before:
            fails.append("a replayed packet produced a second alert")
        else:
            print("    replay rejected")

        print("4. missing heartbeats raise the alarm")
        rx._clock = lambda: 1_000_000.0
        rx.handle(link.encode(key, link.HEARTBEAT, 9999,
                              link.pack_heartbeat(1_000_000)))
        rx._clock = lambda: 1_000_000.0 + 3 * 86400
        if not rx.check_silence():
            fails.append("silence did not raise an alarm")
        else:
            print("    alarm raised after 3 days of silence")

    print()
    if fails:
        print("SELFTEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("SELFTEST PASSED -- the protocol works end to end with no radio.")
    print("What this cannot test is range. Take both ends to their real")
    print("positions before mounting anything permanently.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("genkey").set_defaults(fn=cmd_genkey)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("queue").set_defaults(fn=cmd_queue)
    sub.add_parser("test").set_defaults(fn=cmd_test)
    sub.add_parser("drain").set_defaults(fn=cmd_drain)
    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)
    d = sub.add_parser("decode"); d.add_argument("hex"); d.set_defaults(fn=cmd_decode)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
