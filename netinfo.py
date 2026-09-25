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

"""Which networks this board can reach without paying for it.

A viewer whose frames never cross the mobile link should get the full picture
and no session limit. Listing those networks by hand in the config was wrong
twice over: it needs editing whenever the site is rewired, and it was silently
stale the day the wall panel moved onto the 4G router's own LAN -- the panel
dropped to 1 fps at 480 pixels and logged itself out every fifteen minutes,
with nothing to say why.

The kernel already knows the answer. An on-link route -- one with no gateway --
means "this subnet is reachable by shouting on the wire", which is exactly the
traffic that costs nothing. Tunnels are excluded: OpenVPN pushes on-link routes
for the networks behind it, and a viewer arriving down the tunnel is precisely
the one who does cost money.
"""

import ipaddress
import logging
import struct
import time

log = logging.getLogger("netinfo")

PROC_ROUTE = "/proc/net/route"

# A tunnel's routes look on-link but every byte is encapsulated and billed.
TUNNEL_PREFIXES = ("tun", "tap", "ppp", "wg", "sit", "gre", "ipsec")


def _addr(le_hex):
    """/proc/net/route stores addresses little-endian, as 8 hex digits."""
    return ipaddress.IPv4Address(struct.pack("<I", int(le_hex, 16)))


def is_tunnel(iface):
    return iface.lower().startswith(TUNNEL_PREFIXES)


def attached_networks(proc_route=None):
    """Directly-attached IPv4 subnets, as a list of IPv4Network.

    Skips the default route (mask 0), host routes, and tunnels.
    """
    proc_route = proc_route or PROC_ROUTE
    nets = []
    try:
        with open(proc_route, "r") as fh:
            next(fh, None)                      # header
            for line in fh:
                f = line.split()
                if len(f) < 8:
                    continue
                iface, dest, gw, _flags = f[0], f[1], f[2], f[3]
                mask = f[7]
                if is_tunnel(iface):
                    continue
                if int(gw, 16) != 0:            # via a router: not on-link
                    continue
                bits = bin(int(mask, 16)).count("1")
                if bits in (0, 32):             # default route, or a host route
                    continue
                try:
                    net = ipaddress.ip_network(f"{_addr(dest)}/{bits}",
                                               strict=False)
                except ValueError:
                    continue
                if net not in nets:
                    nets.append(net)
    except (OSError, ValueError, StopIteration) as e:
        log.warning("could not read %s (%s); no networks assumed free",
                    proc_route, e)
    return nets


class AttachedNetworks:
    """attached_networks() with a cache, for the request path.

    Re-read now and then rather than once at startup: the address on the 4G
    router's LAN comes from its DHCP and the wiring at a club changes without
    anybody restarting a service.
    """

    def __init__(self, ttl_s=60.0, proc_route=None, clock=time.monotonic):
        self.ttl_s = float(ttl_s)
        # Resolved at read time, not bound here, so a test can point
        # PROC_ROUTE somewhere else after this object exists.
        self.proc_route = proc_route
        self._clock = clock
        self._at = None
        self._nets = []

    def get(self):
        now = self._clock()
        if self._at is None or (now - self._at) >= self.ttl_s:
            fresh = attached_networks(self.proc_route or PROC_ROUTE)
            if fresh != self._nets:
                log.info("networks reached without the mobile link: %s",
                         ", ".join(str(n) for n in fresh) or "(none)")
            self._nets = fresh
            self._at = now
        return self._nets
