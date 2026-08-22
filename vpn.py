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

"""Reporting on the outbound tunnel, so you can see it before driving away.

A phone hotspot puts the board behind carrier NAT: it can reach out, but
nothing can reach in. An OpenVPN client dialling a server you control fixes
that, and this says whether it worked.

Certificates and keys stay out of it: those are not something to upload
through a web page, and they belong at a shell. What the panel does get is the
answer to "is it up, at what address, and will it come back after a power
cut" -- and a switch for that last one, because whether the tunnel starts at
boot is the setting you most want to change while standing next to the board
and least want to drive back for.

Turning it on needs root, so it goes through a helper that validates its
arguments (see vpnctl.sh) rather than a sudoers rule for systemctl, whose
wildcards cannot be constrained safely.

Nothing here reaches the network unless someone presses the button: an
offline-first box should not quietly phone out every time a settings page is
opened.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time

log = logging.getLogger("vpn")

CLIENT_DIR = "/etc/openvpn/client"


def _run(args, timeout=5):
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError):
        return 1, "", ""


def installed():
    return shutil.which("openvpn") is not None


def configs():
    """Client configurations present, by name. Never reads their contents."""
    out = []
    for pat in ("*.conf", "*.ovpn"):
        for p in sorted(glob.glob(os.path.join(CLIENT_DIR, pat))):
            out.append(os.path.splitext(os.path.basename(p))[0])
    return sorted(set(out))


def units():
    """Every openvpn client unit systemd knows, with its state."""
    rc, out, _ = _run(["systemctl", "list-units", "--all", "--no-legend",
                       "--plain", "--type=service", "openvpn-client@*"])
    found = {}
    for line in out.splitlines():
        parts = line.split()
        if not parts or not parts[0].startswith("openvpn-client@"):
            continue
        unit = parts[0]
        name = unit.split("@", 1)[1].replace(".service", "")
        found[name] = {"name": name, "unit": unit,
                       "active": parts[2] if len(parts) > 2 else "unknown"}

    # A configuration with no unit yet is the normal state before anyone has
    # enabled it, and saying so is more useful than omitting it.
    for name in configs():
        found.setdefault(name, {"name": name,
                                "unit": "openvpn-client@%s.service" % name,
                                "active": "not started"})
    for name, u in found.items():
        rc, out, _ = _run(["systemctl", "is-enabled", u["unit"]])
        u["enabled"] = (out or "").strip() or "disabled"
        rc, out, _ = _run(["systemctl", "is-active", u["unit"]])
        u["active"] = (out or "").strip() or u["active"]
    return [found[k] for k in sorted(found)]


def tunnels():
    """Point-to-point interfaces that are up, with their addresses."""
    rc, out, _ = _run(["ip", "-j", "addr", "show"])
    ifaces = []
    if rc == 0 and out.strip():
        try:
            for d in json.loads(out):
                name = d.get("ifname", "")
                if not (name.startswith("tun") or name.startswith("tap")
                        or name.startswith("wg")):
                    continue
                addrs = [a.get("local", "") for a in d.get("addr_info", [])
                         if a.get("family") == "inet"]
                ifaces.append({"iface": name,
                               "up": d.get("operstate") in ("UP", "UNKNOWN"),
                               "ip": addrs[0] if addrs else ""})
        except (ValueError, TypeError):
            pass
    return ifaces


def default_route():
    """Which interface traffic leaves by, which is not the same as internet."""
    rc, out, _ = _run(["ip", "-j", "route", "show", "default"])
    if rc != 0 or not out.strip():
        return {}
    try:
        rows = json.loads(out)
    except (ValueError, TypeError):
        return {}
    if not rows:
        return {}
    r = rows[0]
    return {"via": r.get("gateway", ""), "dev": r.get("dev", "")}


def status():
    """Everything knowable without touching the network."""
    tuns = [t for t in tunnels() if t["ip"]]
    return {
        "installed": installed(),
        "configs": configs(),
        "units": units(),
        "tunnels": tuns,
        "up": bool(tuns),
        "address": tuns[0]["ip"] if tuns else "",
        "route": default_route(),
        "client_dir": CLIENT_DIR,
    }


def reach(host="1.1.1.1", port=443, timeout=2.0):
    """One TCP connect, only ever when someone asks for it.

    Deliberately an address rather than a name, so a failure means "no route"
    and not "no DNS" -- those need different fixes and the distinction is the
    whole value of the check.
    """
    started = time.time()
    try:
        with socket.create_connection((host, int(port)), timeout=float(timeout)):
            return True, "Reached %s in %.0f ms." % (
                host, (time.time() - started) * 1000)
    except socket.timeout:
        return False, "No answer from %s within %.0fs." % (host, timeout)
    except OSError as e:
        return False, "Could not reach %s: %s" % (host, e)


def resolves(name="github.com", timeout=2.0):
    """Whether DNS works, asked separately from whether routing works."""
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(float(timeout))
    try:
        socket.getaddrinfo(name, None)
        return True, "DNS resolves %s." % name
    except OSError as e:
        return False, "DNS cannot resolve %s: %s" % (name, e)
    finally:
        socket.setdefaulttimeout(old)


HELPER = "/usr/local/sbin/rknn-vpnctl"


REQ_DIR = "/run/rknn-vpn"


def can_control():
    """Can a request be made at all?

    The service cannot use sudo -- its unit sets NoNewPrivileges=yes, and sudo
    is setuid, so it is refused before it starts. What it can do is write a
    file that a root .path unit is watching. So the question is whether both
    ends of that arrangement exist: the runtime directory to write into, and
    the helper root will run.
    """
    return os.path.isdir(REQ_DIR) and os.access(REQ_DIR, os.W_OK) \
        and os.path.exists(HELPER)


def control(action, name, timeout=45.0):
    """Start/stop/enable/disable one tunnel. Returns (ok, message).

    Everything is validated again inside the helper, which runs as root; this
    side only exists to give the page something readable to show.
    """
    if action not in ("start", "stop", "restart", "enable", "disable"):
        return False, "Unknown action."
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", name or ""):
        return False, "That is not a usable tunnel name."
    if not os.path.exists(HELPER):
        return False, ("The tunnel helper is not installed. Run ./install.sh "
                       "on the board.")

    return ask("%s %s" % (action, name), timeout=timeout, verb=action)


def ask(line, timeout=45.0, verb="do"):
    """Ask root to do something, and wait for the answer.

    The panel cannot use sudo: its unit sets NoNewPrivileges and sudo is
    setuid. What it can do is write a file that a root .path unit is watching.
    Everything it asks for is validated again on the other side.
    """
    if not os.path.isdir(REQ_DIR) or not os.access(REQ_DIR, os.W_OK):
        return False, ("The privileged helper is not installed. Run "
                       "./install.sh on the board.")
    req = os.path.join(REQ_DIR, "request")
    res = os.path.join(REQ_DIR, "result")
    try:
        # Clear any previous answer, so a stale one is never read as this
        # request's outcome.
        if os.path.exists(res):
            os.unlink(res)
        with open(req, "w", encoding="utf-8") as fh:
            fh.write(line.strip() + "\n")
    except OSError as e:
        return False, "Could not ask for the change: %s" % e

    # Root picks the file up through a .path unit, which is not instant.
    rc, out, err = 1, "", ""
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        try:
            with open(res, encoding="utf-8") as fh:
                answer = fh.read().strip()
            os.unlink(res)
            rc = 0 if answer.startswith("ok") else 1
            out = answer.split(" ", 1)[1] if " " in answer else ""
            err = "" if rc == 0 else out
            break
        except OSError:
            time.sleep(0.25)
    else:
        return False, ("No answer from the tunnel helper. Is "
                       "rknn-surveillance-vpn.path running?")

    msg = (err or out or "").strip().splitlines()
    msg = msg[-1] if msg else ""
    if rc == 0:
        said = {"enable": "will start at boot, and is starting now",
                "disable": "will not start at boot, and is stopping now",
                "start": "starting", "stop": "stopped",
                "restart": "restarting"}.get(verb)
        log.warning("privileged request %r succeeded", line)
        return True, ("Tunnel %s." % said) if said else (msg or "Done.")
    return False, msg or ("Could not %s." % verb)
