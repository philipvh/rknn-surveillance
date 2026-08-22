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
# Publish the camera's own web UI on a board port, so it can be reached over
# the VPN tunnel (and the club LAN) without a route into the camera segment.
#
#   sudo bash camera_ui.sh            # show what it would do
#   sudo bash camera_ui.sh --apply    # do it, this boot only
#   sudo bash camera_ui.sh --save     # do it and make it survive a reboot
#   sudo bash camera_ui.sh --remove   # take the forward out again
#
# Why this exists: the camera sits on its own segment (192.168.91.0/24) behind
# the board, deliberately not routed anywhere. From home over the tunnel there
# is no way to its web page. This forwards a board port to it instead, so
# http://<board-on-tunnel>:8080/ reaches the camera's UI. The board relays;
# nothing needs a route to the camera net.
#
# It is the same trick the bench INSTAR had on 8080. That camera is gone; this
# repoints the port at whatever camera the running config now names, and clears
# the dead INSTAR rule while it is here.
#
# The recording path does NOT use this: the service pulls RTSP from the camera
# directly, because the board itself can reach the segment. This is only the
# human-facing web UI.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Where the forward should answer. The wired LAN is the club's own network; the
# tunnel is home. Wi-Fi is discovered, because a dongle's name is not knowable
# until it is in. This is the same target set wan_ports.sh uses, on purpose.
LAN_IF="${LAN_IF:-enP4p65s0}"
CAM_IF="${CAM_IF:-enx00606ed70c2a}"     # the camera's own segment
CAM_NET="${CAM_NET:-192.168.91.0/24}"
LISTEN_PORT="${LISTEN_PORT:-8080}"      # board port the UI answers on

# The camera itself: taken from the running config so this tracks a move rather
# than hard-coding an address that then rots. Overridable for a dry test.
CAM_IP="${CAM_IP:-}"
CAM_PORT="${CAM_PORT:-}"

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
die() { echo "!!  $*" >&2; exit 1; }
run() { if [ "$APPLY" = 1 ]; then "$@"; else echo "    would: $*"; fi; }

[ "$(id -u)" -eq 0 ] || die "run this with sudo"

# --------------------------------------------------------- camera from config
if [ -z "$CAM_IP" ] || [ -z "$CAM_PORT" ]; then
  read -r _ip _port < <(cd "$HERE" && "$PYTHON_BIN" -c \
    "import config; c=config.load(require_password=False); \
     print(c._get('camera','host',default=''), \
           c._get('camera','http_port',default=88))" 2>/dev/null || true)
  CAM_IP="${CAM_IP:-$_ip}"
  CAM_PORT="${CAM_PORT:-$_port}"
fi
[ -n "$CAM_IP" ]   || die "no camera host in config; set CAM_IP=.. or fix config.local.yaml"
[ -n "$CAM_PORT" ] || die "no camera http_port in config; set CAM_PORT=.."

# The camera has to be on the segment this forwards into, or the forward is a
# rule to nowhere. Catch the config/segment mismatch here, not from a browser.
case "$CAM_IP" in
  "${CAM_NET%.*/*}".*) ;;               # 192.168.91.<x>
  *) die "camera ${CAM_IP} is not on ${CAM_NET}; wrong CAM_NET/CAM_IF?" ;;
esac

# ------------------------------------------------------------- target ifaces
TARGETS="$LAN_IF tun+"
for d in /sys/class/net/*/wireless /sys/class/net/*/phy80211; do
  [ -e "$d" ] || continue
  n=$(basename "$(dirname "$d")")
  case " $TARGETS " in *" $n "*) ;; *) TARGETS="$TARGETS $n" ;; esac
done

say "Camera UI:  ${CAM_IP}:${CAM_PORT}  ->  answer on :${LISTEN_PORT}"
say "Interfaces: ${TARGETS}"
say "Relay out:  ${CAM_IF} (${CAM_NET})"
echo

# ------------------------------------------------------- clear the dead INSTAR
# A DNAT to a camera that is gone can never match and survives every save.
# Clearing it is why "likewise foscam" does not just pile a second rule on top.
if [ "$REMOVE" != 1 ]; then
  while read -r line; do
    [ -n "$line" ] || continue
    dst=$(printf '%s' "$line" | sed -n 's/.*--to-destination \([0-9.]*\).*/\1/p')
    [ -n "$dst" ] || continue
    [ "$dst" = "$CAM_IP" ] && continue   # the one we are about to (re)add
    case "$dst" in
      "${CAM_NET%.*/*}".*)               # some other host on the camera net
        say "Clearing an old forward to ${dst} (no longer the camera)"
        # shellcheck disable=SC2086
        run iptables -t nat -D PREROUTING ${line#-A PREROUTING } ;;
    esac
  done <<EOF
$(iptables-save -t nat 2>/dev/null | grep -E "^-A PREROUTING .* --dport ${LISTEN_PORT} .*-j DNAT" || true)
EOF
fi

# --------------------------------------------------------------- the forward
for iface in $TARGETS; do
  # tun+ etc. is a wildcard iptables understands directly; a name that does not
  # exist yet (a dongle not in) is harmless -- the rule simply never matches.
  DNAT_ARGS=(-i "$iface" -p tcp --dport "$LISTEN_PORT"
             -j DNAT --to-destination "${CAM_IP}:${CAM_PORT}")
  if [ "$REMOVE" = 1 ]; then
    if iptables -t nat -C PREROUTING "${DNAT_ARGS[@]}" 2>/dev/null; then
      say "Removing forward on ${iface}"
      run iptables -t nat -D PREROUTING "${DNAT_ARGS[@]}"
    fi
  else
    if iptables -t nat -C PREROUTING "${DNAT_ARGS[@]}" 2>/dev/null; then
      echo "    already there on ${iface}"
    else
      say "Forwarding :${LISTEN_PORT} on ${iface} -> ${CAM_IP}:${CAM_PORT}"
      run iptables -t nat -I PREROUTING "${DNAT_ARGS[@]}"
    fi
  fi
done

# ------------------------------------------------ masquerade and forward path
# The camera answers whoever asked. From the tunnel that source is a 10.8.2.x
# it cannot route to, so its reply is dropped and the page never loads. Making
# every relayed request look like it came from the board fixes that. These are
# the same rules wan_ports.sh adds; -C means running both is harmless, and
# --remove leaves them, because the recording forwards may still want them.
if [ "$REMOVE" != 1 ]; then
  if ! iptables -t nat -C POSTROUTING -o "$CAM_IF" -d "$CAM_NET" -j MASQUERADE 2>/dev/null; then
    say "Adding MASQUERADE towards ${CAM_NET}"
    run iptables -t nat -A POSTROUTING -o "$CAM_IF" -d "$CAM_NET" -j MASQUERADE
  else
    say "MASQUERADE towards ${CAM_NET} already present"
  fi

  # Docker sets the FORWARD policy to DROP; relayed packets then need an
  # explicit accept or they are discarded silently.
  POL=$(iptables -L FORWARD -n | head -1 | grep -oE "policy [A-Z]+" | awk '{print $2}')
  say "FORWARD policy is ${POL:-unknown}"
  if [ "${POL:-}" = "DROP" ]; then
    for iface in $TARGETS; do
      iptables -C FORWARD -i "$iface" -o "$CAM_IF" -j ACCEPT 2>/dev/null \
        || run iptables -I FORWARD -i "$iface" -o "$CAM_IF" -j ACCEPT
    done
    iptables -C FORWARD -i "$CAM_IF" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null \
      || run iptables -I FORWARD -i "$CAM_IF" -m state --state RELATED,ESTABLISHED -j ACCEPT
  fi
fi

# ---------------------------------------------------------------------- save
if [ "$SAVE" = 1 ]; then
  say "Saving so it survives a reboot"
  run netfilter-persistent save
elif [ "$APPLY" = 1 ]; then
  say "NOT saved. Re-run with --save once you have tested it in a browser."
fi

if [ "$APPLY" != 1 ]; then
  echo; say "Nothing changed. Re-run with --apply, then --save."
  exit 0
fi

echo
if [ "$REMOVE" = 1 ]; then
  say "Removed. The camera UI is no longer forwarded."
else
  BOARD_TUN=$(ip -4 -o addr show tun0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
  say "The camera UI now answers at:"
  echo "    over the tunnel:  http://${BOARD_TUN:-<board-tunnel-ip>}:${LISTEN_PORT}/"
  echo "    on the club LAN:  http://<board-LAN-ip>:${LISTEN_PORT}/"
fi
