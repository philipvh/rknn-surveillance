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
#
# Build the camera-side network the system runs on, reproducibly.
#
#   sudo bash setup_network.sh                    # show what it would do
#   sudo bash setup_network.sh --apply
#
# This segment is the one part of the installation that must work with no
# internet, no phone and no uplink of any kind: the camera lives on it, and so
# does the wall tablet via an access point. The board is its address, its DHCP
# server and its DNS resolver, so the clubhouse is self-contained.
#
# Everything is overridable:
#   IFACE=enx00606ed70c2a NET=192.168.91 CAMERA_MAC=aa:bb:.. sudo bash setup_network.sh
set -uo pipefail

IFACE="${IFACE:-enx00606ed70c2a}"     # the board's camera-side interface
NET="${NET:-192.168.91}"              # first three octets of the segment
SELF="${SELF:-${NET}.1}"              # the board
RANGE_FROM="${RANGE_FROM:-${NET}.10}"
RANGE_TO="${RANGE_TO:-${NET}.100}"
CAMERA_IP="${CAMERA_IP:-${NET}.200}"
CAMERA_MAC="${CAMERA_MAC:-}"          # discovered if not given
PANEL_NAME="${PANEL_NAME:-panel}"     # http://panel:8081/ from the tablet
PANEL_PORT="${PANEL_PORT:-8081}"

APPLY=0
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

say() { echo "==> $*"; }
die() { echo "!!  $*" >&2; exit 1; }
run() { if [ "$APPLY" = 1 ]; then "$@"; else echo "    would: $*"; fi; }

[ "$(id -u)" -eq 0 ] || die "run this with sudo"
[ -e "/sys/class/net/${IFACE}" ] || die "no such interface: ${IFACE}"

# The camera's lease should follow the camera, not a guess. Prefer an existing
# reservation, then the live ARP table, and only then give up -- a wrong MAC
# would hand the camera a random address and break every recorder URL.
if [ -z "$CAMERA_MAC" ]; then
  CAMERA_MAC=$(grep -hoE "dhcp-host=([0-9a-f]{2}:){5}[0-9a-f]{2}" \
    /etc/dnsmasq.conf /etc/dnsmasq.d/* 2>/dev/null | head -1 | cut -d= -f2)
fi
if [ -z "$CAMERA_MAC" ]; then
  CAMERA_MAC=$(ip neigh show dev "$IFACE" 2>/dev/null \
    | awk -v ip="$CAMERA_IP" '$1==ip {print $5; exit}')
fi
if [ -n "$CAMERA_MAC" ]; then
  say "Camera MAC: ${CAMERA_MAC} -> ${CAMERA_IP} (fixed lease)"
else
  say "No camera MAC found; it will get an address from the pool instead."
  say "Re-run with CAMERA_MAC=.. once the camera has been seen, so its"
  say "address stops moving."
fi

say "Interface ${IFACE} = ${SELF}, DHCP ${RANGE_FROM}-${RANGE_TO}"

# ------------------------------------------------------------------ address
# ifupdown rather than NetworkManager: NM already treats this interface as
# unmanaged, and a static address that comes up before any service is simpler
# to reason about than a NM profile with an activation order.
IFFILE=/etc/network/interfaces.d/rknn-camera-net
say "Writing ${IFFILE}"
if [ "$APPLY" = 1 ]; then
  [ -f "$IFFILE" ] && cp -a "$IFFILE" "${IFFILE}.bak.$(date +%H%M%S)"
  cat > "$IFFILE" <<EOF
# The camera segment. Written by setup_network.sh -- edit that, not this.
auto ${IFACE}
allow-hotplug ${IFACE}
iface ${IFACE} inet static
    address ${SELF}
    netmask 255.255.255.0
EOF
fi

# An older hand-made file for the same interface would fight this one.
for old in /etc/network/interfaces.d/*; do
  [ -f "$old" ] || continue
  [ "$old" = "$IFFILE" ] && continue
  if grep -q "$IFACE" "$old" 2>/dev/null; then
    say "Disabling an earlier config for the same interface: ${old}"
    run mv "$old" "${old}.replaced.$(date +%H%M%S)"
  fi
done

# -------------------------------------------------------------------- dnsmasq
command -v dnsmasq >/dev/null 2>&1 || {
  say "Installing dnsmasq"
  run apt-get install -y dnsmasq
}

CONF=/etc/dnsmasq.d/rknn-camera-net.conf
say "Writing ${CONF}"
if [ "$APPLY" = 1 ]; then
  mkdir -p /etc/dnsmasq.d
  [ -f "$CONF" ] && cp -a "$CONF" "${CONF}.bak.$(date +%H%M%S)"
  {
    echo "# The camera segment. Written by setup_network.sh."
    echo "interface=${IFACE}"
    echo "bind-interfaces"
    echo "except-interface=lo"
    echo "domain-needed"
    echo "bogus-priv"
    echo
    echo "dhcp-range=${RANGE_FROM},${RANGE_TO},255.255.255.0,12h"
    echo "dhcp-authoritative"
    echo
    echo "# The board is the gateway and the resolver. Naming a public DNS"
    echo "# server here is what breaks a clubhouse with no internet: every"
    echo "# lookup waits for a timeout before anything falls back."
    echo "dhcp-option=3,${SELF}"
    echo "dhcp-option=6,${SELF}"
    echo
    echo "# So the tablet can be pointed at a name instead of an address, and"
    echo "# that name keeps working when there is no uplink at all."
    echo "address=/${PANEL_NAME}/${SELF}"
    echo "address=/${PANEL_NAME}.local/${SELF}"
    if [ -n "$CAMERA_MAC" ]; then
      echo
      echo "dhcp-host=${CAMERA_MAC},${CAMERA_IP},camera,infinite"
      echo "address=/camera/${CAMERA_IP}"
    fi
    echo
    echo "# Upstream resolvers come from the system when there is an uplink,"
    echo "# and simply are not there when there is not. Nothing is hard-coded,"
    echo "# so no client ever waits on an unreachable server."
    echo "resolv-file=/etc/resolv.conf"
  } > "$CONF"
fi

# The stock /etc/dnsmasq.conf on this board carries hand-made settings for the
# same interface, including a hard-coded public resolver. Leave the file, but
# stop it competing.
if [ -f /etc/dnsmasq.conf ] && grep -qE "^\s*(interface|dhcp-range|server)=" /etc/dnsmasq.conf 2>/dev/null; then
  say "Neutralising the hand-made settings in /etc/dnsmasq.conf"
  if [ "$APPLY" = 1 ]; then
    cp -a /etc/dnsmasq.conf "/etc/dnsmasq.conf.bak.$(date +%H%M%S)"
    sed -i -E 's/^(\s*)(interface|bind-interfaces|dhcp-range|dhcp-option|dhcp-host|server|no-resolv)=?/#\1\2/' \
      /etc/dnsmasq.conf
    grep -q "^conf-dir=/etc/dnsmasq.d" /etc/dnsmasq.conf \
      || echo "conf-dir=/etc/dnsmasq.d/,*.conf" >> /etc/dnsmasq.conf
  fi
fi

# --------------------------------------------------------------------- apply
if [ "$APPLY" != 1 ]; then
  echo
  say "Nothing changed. Re-run with --apply."
  exit 0
fi

say "Bringing ${IFACE} up"
ifdown "$IFACE" >/dev/null 2>&1 || true
ifup "$IFACE" >/dev/null 2>&1 || ip addr replace "${SELF}/24" dev "$IFACE"
ip link set "$IFACE" up

say "Restarting dnsmasq"
if ! systemctl restart dnsmasq; then
  echo
  systemctl status dnsmasq --no-pager -n 12 | sed 's/^/    /'
  die "dnsmasq did not start; the backups beside each file undo this"
fi
systemctl enable dnsmasq >/dev/null 2>&1 || true

# ---------------------------------------------------------------- verify it
echo
say "Checking"
ok=1
addr=$(ip -4 -o addr show dev "$IFACE" | awk '{print $4}')
[ "$addr" = "${SELF}/24" ] && echo "    address    ${addr}" \
  || { echo "!!  address    ${addr:-none}, expected ${SELF}/24"; ok=0; }

systemctl is-active --quiet dnsmasq && echo "    dnsmasq    running" \
  || { echo "!!  dnsmasq    not running"; ok=0; }

if command -v dig >/dev/null 2>&1; then
  got=$(dig +short +time=2 +tries=1 "@${SELF}" "$PANEL_NAME" 2>/dev/null | head -1)
elif command -v nslookup >/dev/null 2>&1; then
  got=$(nslookup "$PANEL_NAME" "$SELF" 2>/dev/null | awk '/^Address: /{print $2; exit}')
else
  got="(no dig or nslookup to check with)"
fi
[ "$got" = "$SELF" ] && echo "    dns        ${PANEL_NAME} -> ${got}" \
  || echo "    dns        ${PANEL_NAME} -> ${got:-no answer}"

ss -lunp 2>/dev/null | grep -q ":67" && echo "    dhcp       listening" \
  || echo "!!  dhcp       nothing on port 67"

echo
if [ "$ok" = 1 ]; then
  say "The camera segment is up and does not need an uplink."
  echo "    Tablet:  join the access point on this segment, then"
  echo "             http://${PANEL_NAME}:${PANEL_PORT}/"
  echo "             (or http://${SELF}:${PANEL_PORT}/)"
  echo "    Camera:  ${CAMERA_IP}"
else
  say "Something is not right -- see the lines marked !! above."
  exit 1
fi
