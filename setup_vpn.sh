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

# Turn an exported pfSense client config into a working, safe tunnel.
# Run as root on the board:
#
#   sudo bash setup_vpn.sh
#
# The problem this solves: the VPN server's own LAN is 192.168.90.0/24, which
# is the subnet this board sits on while it is at the office. The server
# pushes a route for it, and that route competes with the board's connected
# route for the same subnet -- which can blackhole the local network,
# including the ssh session running this script.
#
# So two pushed directives are refused, and the tunnel is started but NOT
# enabled at boot until local access has been proved to still work.
set -uo pipefail

SRC=/etc/openvpn/client/client.openvpn
NAME=club
DST=/etc/openvpn/client/${NAME}.conf
UNIT=openvpn-client@${NAME}
# Something on the local network that must stay reachable. If this goes away
# when the tunnel comes up, the tunnel is the reason.
LAN_CHECK=${LAN_CHECK:-192.168.90.6}

say() { echo "==> $*"; }
die() { echo "!!  $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this with sudo"
command -v openvpn >/dev/null || die "openvpn is not installed"

# The pfSense export, if it is still here. Once this script has run once the
# export is redundant -- everything it contained is in the .conf -- so fall
# back to that and re-derive from it. Re-running then stays possible after
# somebody tidies the original away, which is exactly what happened.
if [ ! -f "$SRC" ]; then
  if [ -f "$DST" ]; then
    say "No ${SRC}; rebuilding ${DST} from itself"
    SRC="$DST"
  else
    die "neither ${SRC} nor ${DST} exists -- export a client config from pfSense first"
  fi
fi

say "Building ${DST} from ${SRC}"
# .bak, not .conf, or the backup would look like a second tunnel.
cp -a "$SRC" "${SRC}.bak.$(date +%H%M%S)"

# Strip any previous run's additions so this is idempotent. Everything we add
# lives below the marker, so cutting at the marker removes all of it -- not
# just the lines an earlier version happened to know about. Via a temp file,
# because the source and the destination can be the same file.
TMP=$(mktemp)
sed "/^# --- added by setup_vpn.sh/,\$d" "$SRC" > "$TMP"
cat "$TMP" > "$DST"
rm -f "$TMP"

cat >> "$DST" <<'EOF'

# --- added by setup_vpn.sh ------------------------------------------------
# Do not let the server take over the default route. At the club the uplink
# is a phone, and tunnelling the camera's own traffic through it would cost
# mobile data for no benefit -- the tunnel exists to be reached, not to carry
# everything.
pull-filter ignore "redirect-gateway"

# Do not accept a route for the server's LAN. That subnet is the one this
# board sits on when it is at the office, and a pushed route for it fights
# the connected route and can take the local network down. Being reachable
# ON the tunnel does not require routing TO the server's LAN.
pull-filter ignore "route 192.168.90."

# A phone hotspot drops often; without these the tunnel does not come back.
keepalive 10 60

# Add the route to the lab LAN ourselves, conditionally -- see the script.
script-security 2
route-up /etc/openvpn/client/lab-route.sh
EOF

# The pushed route is refused above because it is dangerous while the board is
# ON that LAN. But when it is not -- at the club, on a hotspot -- the route is
# needed or the board cannot answer anything there, and the tunnel looks up
# from the server while being useless from the office. So decide at connect
# time instead of guessing at install time.
cat > /etc/openvpn/client/lab-route.sh <<'RSCRIPT'
#!/bin/sh
# Run by openvpn as route-up. $dev is the tunnel interface.
#
# openvpn hands scripts a minimal environment. On Debian `ip` is in /usr/sbin,
# which is not on that PATH, so calling it by bare name fails with "not found"
# and the route silently never appears -- while the tunnel itself comes up
# perfectly, which makes it look like a routing problem at the far end.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

LOG=/run/lab-route.log
say() { echo "$(date "+%H:%M:%S") $*" >> "$LOG"; logger -t lab-route "$*" 2>/dev/null; }

if ! command -v ip >/dev/null 2>&1; then
  say "FAILED: no ip command on PATH=$PATH"
  exit 0                    # never take the tunnel down over this
fi

if ip -4 addr show | grep -qE 'inet 192\.168\.90\.'; then
  say "on 192.168.90.0/24 already; not routing it over ${dev}"
  exit 0
fi

if ip route replace 192.168.90.0/24 dev "${dev}"; then
  say "routed 192.168.90.0/24 over ${dev}"
else
  say "FAILED to add 192.168.90.0/24 over ${dev}"
fi
exit 0
RSCRIPT
chmod 755 /etc/openvpn/client/lab-route.sh
chown root:root /etc/openvpn/client/lab-route.sh
say "Wrote /etc/openvpn/client/lab-route.sh"

chmod 600 "$DST"
chown root:root "$DST"
chmod 600 "$SRC" 2>/dev/null || true      # it holds a private key too
say "Wrote ${DST} (mode 600)"

# restart, not start: start is a no-op on an already-running unit, so the
# config just rewritten would not be read and route-up would never fire --
# while the script cheerfully reports the address of the old tunnel.
say "Restarting ${UNIT} so it reads the new config"
rm -f /run/lab-route.log
systemctl restart "$UNIT" || die "the unit failed to start; see: journalctl -u $UNIT"

say "Waiting for a tunnel address…"
ADDR=""
for _ in $(seq 1 30); do
  ADDR=$(ip -4 -o addr show dev tun0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
  [ -n "$ADDR" ] && break
  sleep 1
done

if [ -z "$ADDR" ]; then
  say "No tunnel came up. Stopping again so nothing is left half-connected."
  systemctl stop "$UNIT"
  echo
  journalctl -u "$UNIT" --no-pager -n 25 | sed 's/^/    /'
  die "the tunnel did not establish"
fi
say "Tunnel address: ${ADDR}"

# Proof that route-up actually ran, rather than an assumption that it did.
if [ -f /run/lab-route.log ]; then
  say "route-up ran:"
  sed "s/^/      /" /run/lab-route.log
else
  say "WARNING: route-up left no log -- it did not run."
  say "         Check that club.conf has script-security 2 and route-up."
fi

# The check below asks "did the tunnel break the LAN I am sitting on". Off
# that LAN -- at the club, on a hotspot -- there is no such LAN to break, and
# reaching it is now the tunnel's job rather than evidence of damage.
if ! ip -4 addr show | grep -qE "inet 192\\.168\\.90\\."; then
  say "Not on 192.168.90.0/24, so there is no local access to protect."
  if ping -c 2 -W 2 "$LAN_CHECK" >/dev/null 2>&1; then
    say "And ${LAN_CHECK} answers over the tunnel -- the lab route works."
  else
    say "WARNING: ${LAN_CHECK} does not answer over the tunnel yet."
    say "         Check /etc/openvpn/client/lab-route.sh ran: journalctl -t openvpn"
  fi
elif ping -c 2 -W 2 "$LAN_CHECK" >/dev/null 2>&1; then
  say "Local network is fine (${LAN_CHECK} still answers)."
else
  say "LOCAL NETWORK BROKE. Stopping the tunnel and restoring the routes."
  systemctl stop "$UNIT"
  sleep 2
  ping -c 2 -W 2 "$LAN_CHECK" >/dev/null 2>&1 \
    && say "Recovered." || say "Still broken -- check 'ip route'."
  die "the tunnel took the local network with it; it has NOT been enabled"
fi

echo
say "Routes now:"
ip route show | sed 's/^/    /'
echo
say "Tunnel is up and local access survived."
echo "    From your desk, once the board is off-site:"
echo "        ssh radxa@${ADDR}"
echo "        ./deploy.sh radxa@${ADDR} --restart"
echo
if [ "$(systemctl is-enabled "$UNIT" 2>/dev/null)" = "enabled" ]; then
  echo "    It already starts at boot."
else
  echo "    Nothing starts this at boot yet. When you are happy with it:"
  echo "        sudo systemctl enable ${UNIT}"
fi
