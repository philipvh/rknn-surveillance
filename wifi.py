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

"""Joining a wireless network, through NetworkManager.

The board normally sits on wired ethernet. This exists so it can also reach
the internet through a phone hotspot at a site with no wiring -- enough for
git, for an outbound tunnel, and for the clock to find NTP.

Nothing here stores a wireless password. nmcli is given it on stdin, never on
the command line where /proc would expose it, and NetworkManager keeps it in
/etc/NetworkManager/system-connections at mode 600 where only root can read
it. This module never sees it again.

The wired connection is deliberately never touched: it is how anyone gets in
to fix a wireless setting that went wrong.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess

log = logging.getLogger("wifi")

# nmcli -t escapes its separator; a network called "Bar : Grill" is real.
_UNESCAPE = re.compile(r"\\(.)")


def _split(line, n=None):
    out, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    if n is not None:
        out = (out + [""] * n)[:n]
    return out


class WiFi:
    """A thin wrapper over nmcli. Every call is fallible and says why."""

    def __init__(self, runner=None, timeout=60):
        self._run = runner or self._subprocess
        self.timeout = timeout

    # ------------------------------------------------------------- plumbing
    def _subprocess(self, args, stdin=None, timeout=None):
        try:
            p = subprocess.run(
                ["nmcli"] + args, input=stdin, capture_output=True, text=True,
                timeout=timeout or self.timeout)
            return p.returncode, p.stdout, p.stderr
        except FileNotFoundError:
            return 127, "", "nmcli is not installed"
        except subprocess.TimeoutExpired:
            return 124, "", "nmcli timed out"

    @property
    def available(self):
        return shutil.which("nmcli") is not None

    # --------------------------------------------------------------- device
    def device(self):
        """The first wireless interface, or None if there is no radio."""
        rc, out, _ = self._run(["-t", "-f", "DEVICE,TYPE,STATE", "device"])
        if rc != 0:
            return None
        for line in out.splitlines():
            dev, typ, state = _split(line, 3)
            if typ == "wifi":
                return {"device": dev, "state": state}
        return None

    def status(self, nets=None):
        """What the radio is doing right now, and on what address.

        Pass the scan result in if you already have one. Working it out again
        here meant the settings page ran two scans per request and took half a
        minute to load -- long enough that a browser gives up on it.
        """
        d = self.device()
        if d is None:
            return {"present": False, "connected": False, "ssid": "",
                    "state": "no wireless adapter", "ip": "", "signal": 0}
        rc, out, _ = self._run(
            ["-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS",
             "device", "show", d["device"]])
        ssid, ip, state = "", "", d["state"]
        for line in out.splitlines():
            k, _, v = line.partition(":")
            if k == "GENERAL.CONNECTION" and v not in ("--", ""):
                ssid = v
            elif k.startswith("IP4.ADDRESS") and v:
                ip = v
            elif k == "GENERAL.STATE":
                state = v
        signal = 0
        for n in (self.scan(rescan=False) if nets is None else nets):
            if n["in_use"]:
                signal = n["signal"]
                break
        return {"present": True, "connected": bool(ssid and ip),
                "ssid": ssid, "state": state, "ip": ip,
                "device": d["device"], "signal": signal}

    # ----------------------------------------------------------------- scan
    def scan(self, rescan=True):
        """Visible networks, strongest first, one entry per SSID."""
        if self.device() is None:
            return []
        # --rescan no, explicitly. Leaving it out means "auto", which
        # rescans whenever nmcli thinks the cache is stale -- so asking not to
        # rescan still cost fifteen seconds, and the settings page with it.
        args = ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi",
                "list", "--rescan", "yes" if rescan else "no"]
        rc, out, _ = self._run(args, timeout=25)
        if rc != 0:
            return []
        seen, nets = {}, []
        for line in out.splitlines():
            in_use, ssid, signal, sec = _split(line, 4)
            if not ssid:
                continue                      # hidden; join it by name instead
            try:
                sig = int(signal)
            except ValueError:
                sig = 0
            # The same network on two bands appears twice; keep the stronger.
            if ssid in seen:
                if sig > seen[ssid]["signal"]:
                    seen[ssid].update(signal=sig, security=sec)
                if in_use == "*":
                    seen[ssid]["in_use"] = True
                continue
            entry = {"ssid": ssid, "signal": sig,
                     "security": sec or "open", "in_use": in_use == "*"}
            seen[ssid] = entry
            nets.append(entry)
        saved = set(self.saved())
        for n in nets:
            n["saved"] = n["ssid"] in saved
        nets.sort(key=lambda n: (-n["in_use"], -n["signal"]))
        return nets

    def saved(self):
        """Names of stored wireless connections."""
        rc, out, _ = self._run(["-t", "-f", "NAME,TYPE", "connection", "show"])
        if rc != 0:
            return []
        names = []
        for line in out.splitlines():
            name, typ = _split(line, 2)
            if typ in ("802-11-wireless", "wifi"):
                names.append(name)
        return names

    # -------------------------------------------------------------- connect
    def connect(self, ssid, password=None, hidden=False):
        """Join a network. Returns (ok, message).

        The password goes in on stdin. Passing it as an argument would put it
        in /proc/<pid>/cmdline for anyone on the box to read while the command
        runs, and into the shell history of whoever debugs this later.
        """
        ssid = (ssid or "").strip()
        if not ssid:
            return False, "No network name."
        if self.device() is None:
            return False, ("No wireless adapter. Plug in the USB dongle and "
                           "try again -- there is nothing to configure "
                           "without a radio.")

        args = ["--wait", "45"]
        if password:
            args.append("--ask")
        args += ["device", "wifi", "connect", ssid]
        if hidden:
            args += ["hidden", "yes"]

        rc, out, err = self._run(
            args, stdin=(password + "\n") if password else None, timeout=60)
        msg = (err or out or "").strip().splitlines()
        msg = msg[-1] if msg else ""
        if rc == 0:
            log.warning("joined wireless network %r", ssid)
            return True, "Connected to %s." % ssid
        # nmcli's own wording is more useful than anything invented here.
        low = msg.lower()
        if "secrets were required" in low or "no key available" in low:
            msg = "Wrong password, or the network needs a different one."
        elif "not authorized" in low or "insufficient privileges" in low:
            msg = ("Not allowed to change network settings. Run ./install.sh "
                   "to add the polkit rule.")
        elif "no network with ssid" in low:
            msg = ("No network called %r is in range. If it is hidden, tick "
                   "the box." % ssid)
        log.warning("could not join %r: %s", ssid, msg)
        return False, msg or "Could not connect."

    def forget(self, ssid):
        """Delete a saved wireless connection. Never touches the wired one."""
        if ssid not in self.saved():
            return False, "No saved network called %r." % ssid
        rc, out, err = self._run(["connection", "delete", ssid], timeout=20)
        if rc == 0:
            log.warning("forgot wireless network %r", ssid)
            return True, "Forgot %s." % ssid
        return False, (err or out or "Could not remove it.").strip()

    def disconnect(self):
        d = self.device()
        if d is None:
            return False, "No wireless adapter."
        rc, out, err = self._run(["device", "disconnect", d["device"]],
                                 timeout=20)
        if rc == 0:
            return True, "Disconnected."
        return False, (err or out or "Could not disconnect.").strip()


def set_mode(mode):
    """Switch the radio between joining a network and being one.

    Goes through the same privileged channel as the tunnel: the panel writes a
    request, root validates it again and acts. One radio, so one job at a time
    -- in ap mode there is no uplink and the tunnel goes with it, which is the
    normal state at a site with no internet.
    """
    if mode not in ("client", "ap", "status"):
        return False, "Unknown wireless mode."
    import vpn
    ok, msg = vpn.ask("wifi %s" % mode, timeout=60.0, verb="switch the radio")
    if ok:
        log.warning("wireless mode -> %s", mode)
        msg = {"ap": "Now an access point. There is no uplink in this mode, "
                     "so the tunnel is down until it goes back to client.",
               "client": "Rejoining a network. The uplink and the tunnel "
                         "should come back within a few seconds.",
               "status": msg}.get(mode, msg)
    return ok, msg
