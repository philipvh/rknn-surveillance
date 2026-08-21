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
# Make the camera's port forwards answer on more than the wired LAN -- on the
# VPN tunnel, and on Wi-Fi when a dongle is in. Run as root on the board:
#
#   sudo bash wan_ports.sh            # show what it would do
#   sudo bash wan_ports.sh --apply    # do it
#   sudo bash wan_ports.sh --save     # do it and make it survive a reboot
#   sudo bash wan_ports.sh --remove   # take the added rules out again
#
# The port list is not written here. It is read from the rules that already
# work on the wired interface and copied to the others, so adding a forward
# later means running this again rather than editing two places and forgetting
# one of them.
set -uo pipefail

LAN_IF="${LAN_IF:-enP4p65s0}"          # where the forwards already work
CAM_IF="${CAM_IF:-enx00606ed70c2a}"    # the camera's own segment
CAM_NET="${CAM_NET:-192.168.91.0/24}"

APPLY=0; SAVE=0; REMOVE=0
for a in "$@"; do
  case "$a" in
    --apply)  APPLY=1 ;;
    --save)   APPLY=1; SAVE=1 ;;
    --remove) APPLY=1; REMOVE=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

say() { echo "==> $*"; }
run() {
  if [ "$APPLY" = 1 ]; then "$@"; else echo "    would: $*"; fi
}

[ "$(id -u)" -eq 0 ] || { echo "run this with sudo" >&2; exit 1; }

# Which interfaces should also reach the camera. tun+ covers the OpenVPN
# tunnel, so traffic arriving from the lab LAN is forwarded like the wired
# LAN's. Wi-Fi is discovered rather than assumed, because the dongle's name is
# not knowable until it is plugged in.
TARGETS="tun+"
for d in /sys/class/net/*/wireless /sys/class/net/*/phy80211; do
  [ -e "$d" ] || continue
  n=$(basename "$(dirname "$d")")
  case " $TARGETS " in *" $n "*) ;; *) TARGETS="$TARGETS $n" ;; esac
done
say "Interfaces to serve: ${TARGETS}"
case "$TARGETS" in
  *tun*) ;;
esac
if ! echo "$TARGETS" | grep -qE "wl|wlan"; then
  say "No wireless interface found -- plug the dongle in and run this again"
fi

# The forwards that already work, taken from the running table.
mapfile -t RULES < <(iptables-save -t nat 2>/dev/null \
  | grep -E "^-A PREROUTING -i ${LAN_IF} " || true)
if [ "${#RULES[@]}" -eq 0 ]; then
  echo "!!  no PREROUTING rules found for ${LAN_IF}; nothing to copy" >&2
  exit 1
fi
say "Found ${#RULES[@]} forward(s) on ${LAN_IF}:"
for r in "${RULES[@]}"; do
  echo "      ${r#-A PREROUTING -i ${LAN_IF} }"
done

# ------------------------------------------------------- stale interfaces
# A rule naming an interface that no longer exists is dead weight: it can
# never match, it survives every save, and it makes the table harder to read
# than it needs to be. Swapping a USB dongle for a wired extender leaves a
# handful behind, so clear them out on every run rather than by hand.
iface_exists() {
  case "$1" in
    *+) for d in /sys/class/net/"${1%+}"*; do [ -e "$d" ] && return 0; done
        return 1 ;;
    *)  [ -e "/sys/class/net/$1" ] ;;
  esac
}

STALE=0
while read -r line; do
  [ -n "$line" ] || continue
  ifc=$(printf '%s' "$line" | sed -n 's/.* -i \([^ ]*\) .*/\1/p')
  [ -n "$ifc" ] || continue
  if ! iface_exists "$ifc"; then
    STALE=$((STALE + 1))
    say "Stale rule for missing interface ${ifc}"
    # shellcheck disable=SC2086
    run iptables -t nat -D PREROUTING ${line#-A PREROUTING }
  fi
done <<EOF
$(iptables-save -t nat 2>/dev/null | grep -E "^-A PREROUTING -i " || true)
EOF
[ "$STALE" = 0 ] && say "No stale interface rules"

# --------------------------------------------------------------- the rules
ACTION_FLAG=$([ "$REMOVE" = 1 ] && echo "-D" || echo "-A")
say $([ "$REMOVE" = 1 ] && echo "Removing" || echo "Adding") "copies"

for iface in $TARGETS; do
  for r in "${RULES[@]}"; do
    # shellcheck disable=SC2086
    set -- ${r#-A PREROUTING }
    args=()
    while [ $# -gt 0 ]; do
      if [ "$1" = "-i" ]; then args+=("-i" "$iface"); shift 2; continue; fi
      args+=("$1"); shift
    done
    if [ "$REMOVE" = 1 ]; then
      iptables -t nat -C PREROUTING "${args[@]}" 2>/dev/null \
        && run iptables -t nat -D PREROUTING "${args[@]}"
    else
      if iptables -t nat -C PREROUTING "${args[@]}" 2>/dev/null; then
        echo "    already there: -i $iface ${args[*]: -2}"
      else
        run iptables -t nat -I PREROUTING "${args[@]}"
      fi
    fi
  done
done

if [ "$REMOVE" != 1 ]; then
  # The camera answers to whoever asked. From the LAN that works because the
  # board is its gateway; from the tunnel the reply would go to a 10.8.2.x it
  # has no route to. Masquerading makes every request look like it came from
  # the board, which the camera can always answer.
  if ! iptables -t nat -C POSTROUTING -o "$CAM_IF" -d "$CAM_NET" \
        -j MASQUERADE 2>/dev/null; then
    say "Adding MASQUERADE towards ${CAM_NET} on ${CAM_IF}"
    run iptables -t nat -A POSTROUTING -o "$CAM_IF" -d "$CAM_NET" -j MASQUERADE
  else
    say "MASQUERADE towards ${CAM_NET} already present"
  fi

  # Docker sets the FORWARD policy to DROP. If it is, forwarded packets need
  # saying so explicitly or they are silently discarded.
  POL=$(iptables -L FORWARD -n | head -1 | grep -oE "policy [A-Z]+" | awk '{print $2}')
  say "FORWARD policy is ${POL:-unknown}"
  if [ "${POL:-}" = "DROP" ]; then
    for iface in $TARGETS; do
      if ! iptables -C FORWARD -i "$iface" -o "$CAM_IF" -j ACCEPT 2>/dev/null; then
        run iptables -I FORWARD -i "$iface" -o "$CAM_IF" -j ACCEPT
      fi
    done
    if ! iptables -C FORWARD -i "$CAM_IF" -m state \
          --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null; then
      run iptables -I FORWARD -i "$CAM_IF" -m state \
        --state RELATED,ESTABLISHED -j ACCEPT
    fi
  fi
fi

if [ "$SAVE" = 1 ]; then
  say "Saving so it survives a reboot"
  netfilter-persistent save
else
  [ "$APPLY" = 1 ] && say "NOT saved. Re-run with --save once you have tested it."
fi

if [ "$APPLY" = 1 ]; then
  echo
  say "PREROUTING now:"
  iptables -t nat -L PREROUTING -n -v --line-numbers | sed 's/^/    /'
fi
