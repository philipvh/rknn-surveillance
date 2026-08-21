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

"""Rescue and inspect the panel settings from a shell.

For when the panel itself will not let you in: a forgotten password, a trusted
network that turned out not to include you, or a settings file that needs
reading before anything is changed.

    ./settings_cli.py show                    what is set, and what it means
    ./settings_cli.py passwd tvw              set the login (prompts, no echo)
    ./settings_cli.py open                    no password from anywhere
    ./settings_cli.py trusted 192.168.1.0/24  no password from there
    ./settings_cli.py password                require a password everywhere
    ./settings_cli.py reset-login             forget the panel login
    ./settings_cli.py reset-access            forget the access mode
    ./settings_cli.py reset                   forget everything set from the panel

Changes are picked up by the running service within a second; there is no need
to restart it. Everything here only ever touches settings.json -- config.yaml
and secrets.yaml are left alone, so `reset` always lands you back on the login
those files define.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as config_mod       # noqa: E402
from settings import Settings     # noqa: E402


def _store(cfg):
    return Settings(cfg.events_root.parent / "settings.json",
                    defaults={"trigger_classes": sorted(cfg.trigger_classes)})


def cmd_show(cfg, s, args):
    print(f"file           {s.path}"
          f"{'' if s.path.exists() else '   (does not exist yet)'}")
    print()

    mode = s.auth_mode
    if mode:
        where = ", ".join(str(n) for n in s.trusted_networks) or "-"
        detail = {"open": "no password from anywhere",
                  "password": "a password is required everywhere",
                  "trusted": f"no password from {where}"}.get(mode, "")
        print(f"access         {mode}  ({detail})   [set from the panel]")
    else:
        d = "a password is required" if cfg.web_auth_required else "no password"
        print(f"access         from config.yaml: {d}")

    if s.has_credentials():
        print(f"login          {s.web_user!r}   [set from the panel, PBKDF2]")
    else:
        who = (cfg.web or {}).get("auth_user", "tvw")
        print(f"login          {who!r}   (config.yaml + secrets.yaml)")

    if s.overridden("trigger_classes"):
        print(f"triggers       {', '.join(sorted(s.trigger_classes)) or '(none)'}"
              f"   [set from the panel]")
    else:
        print(f"triggers       {', '.join(sorted(cfg.trigger_classes))}"
              f"   (from config.yaml)")
    return 0


def cmd_passwd(cfg, s, args):
    user = args.user or s.web_user or (cfg.web or {}).get("auth_user", "tvw")
    pw = args.password
    if pw is None:
        pw = getpass.getpass(f"New password for {user!r}: ")
        again = getpass.getpass("Again: ")
        if pw != again:
            print("The two passwords do not match; nothing changed.",
                  file=sys.stderr)
            return 1
    try:
        s.set_web_credentials(user, pw)
    except ValueError as e:
        print(f"{e}; nothing changed.", file=sys.stderr)
        return 1
    print(f"Login set to {user!r}. The running service will use it within a "
          f"second.")
    return 0


def cmd_mode(cfg, s, args):
    try:
        nets = s.set_auth_mode(args.mode, getattr(args, "networks", None) or [])
    except ValueError as e:
        print(f"{e}; nothing changed.", file=sys.stderr)
        return 1
    if args.mode == "open":
        print("The panel is now OPEN: no password from anywhere. Anyone who "
              "can reach it can watch and move the camera.")
    elif args.mode == "trusted":
        print("No password from " + ", ".join(str(n) for n in nets) +
              "; a password is still required from anywhere else.")
    else:
        print("A password is now required from everywhere.")
    return 0


def cmd_reset(cfg, s, args):
    if args.what in ("login", "all"):
        s.clear_web_credentials()
        print("Forgot the panel login; the one in secrets.yaml applies again.")
    if args.what in ("access", "all"):
        s.clear_access()
        print("Forgot the access mode; config.yaml decides again.")
    if args.what == "all":
        s.clear("trigger_classes")
        print("Forgot the trigger classes; config.yaml decides again.")
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(__doc__.strip().split("\n")[2:]))
    p.add_argument("--config", help="path to config.yaml")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("show", help="print the current settings")

    sp = sub.add_parser("passwd", help="set the panel login")
    sp.add_argument("user", nargs="?", help="user name (keeps the current one)")
    sp.add_argument("--password", help="avoid the prompt (it lands in your "
                                       "shell history; prefer the prompt)")

    sub.add_parser("open", help="no password from anywhere")
    sub.add_parser("password", help="require a password everywhere")

    st = sub.add_parser("trusted", help="no password from these networks")
    st.add_argument("networks", nargs="+", help="e.g. 192.168.1.0/24")

    sr = sub.add_parser("reset", help="forget what the panel set")
    sr.add_argument("what", nargs="?", default="all",
                    choices=("all", "login", "access"))
    sub.add_parser("reset-login", help="forget the panel login")
    sub.add_parser("reset-access", help="forget the access mode")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.cmd:
        build_parser().print_help()
        return 2

    try:
        cfg = config_mod.load(args.config, require_password=False)
    except Exception as e:
        print(f"could not read the config: {e}", file=sys.stderr)
        return 1
    s = _store(cfg)

    if args.cmd == "show":
        return cmd_show(cfg, s, args)
    if args.cmd == "passwd":
        return cmd_passwd(cfg, s, args)
    if args.cmd in ("open", "password"):
        args.mode = args.cmd
        return cmd_mode(cfg, s, args)
    if args.cmd == "trusted":
        args.mode = "trusted"
        return cmd_mode(cfg, s, args)
    if args.cmd == "reset":
        return cmd_reset(cfg, s, args)
    if args.cmd == "reset-login":
        args.what = "login"
        return cmd_reset(cfg, s, args)
    if args.cmd == "reset-access":
        args.what = "access"
        return cmd_reset(cfg, s, args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
