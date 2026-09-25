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
# Count only the traffic that actually costs money.
#
#   sudo bash wan_meter.sh status       # what is in place now
#   sudo bash wan_meter.sh apply        # create the counters, this boot only
#   sudo bash wan_meter.sh save         # apply and survive a reboot
#   sudo bash wan_meter.sh install      # save, plus the helper the service reads
#   sudo bash wan_meter.sh read         # the two byte totals
#   sudo bash wan_meter.sh refresh      # rebuild only if the subnets changed
#   sudo bash wan_meter.sh remove       # take it all out again
#
# Why this exists: /sys/class/net/<if>/statistics counts every frame on the
# NIC. That was the right measure while the wall panel hung off the board's own
# access point -- its video never touched the uplink. The USB adapter cannot
# carry video, so the panel moved onto the 4G router's LAN, and now every frame
# it pulls crosses the uplink NIC on the way to a tablet ten metres away. At
# roughly 0.8 GB an hour that is 10-19 GB a day of "mobile data" that never
# went near the mobile network, and the bundle looks spent in two days.
#
# iptables can make the distinction the kernel's interface counters cannot:
# count what crosses the uplink whose peer is NOT on a directly-attached
# subnet. The excluded subnets come from netinfo.py -- the same routing-table
# reading the panel uses to decide who gets the full picture -- so this tracks
# a rewire instead of rotting. Tunnels are not excluded, on purpose: a viewer
# down the VPN is exactly who we are trying to measure.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
HELPER=/usr/local/sbin/rknn-meterctl
PUBLISH=/run/rknn-meter
STAMP=/run/rknn-meter.refreshed
UNIT=/etc/systemd/system/rknn-meter.service
TIMER=/etc/systemd/system/rknn-meter.timer
REFRESH_EVERY_S=3600

IN_CHAIN=RKNN_WAN_IN
OUT_CHAIN=RKNN_WAN_OUT
IN_TAG=rknn-wan-in
OUT_TAG=rknn-wan-out

IPT="iptables -w 5"

say() { echo "==> $*"; }
die() { echo "!!  $*" >&2; exit 1; }

# ------------------------------------------------------------------ uplink
uplink() {
  [ -n "${WAN_IF:-}" ] && { echo "$WAN_IF"; return; }
  ip route show default 2>/dev/null | awk '/^default/{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}'
}

# Subnets reachable without paying: read from the kernel, never hard-coded.
free_nets() {
  [ -n "${FREE_NETS:-}" ] && { printf '%s\n' $FREE_NETS; return; }
  (cd "$HERE" 2>/dev/null && "$PYTHON_BIN" -c \
    'import netinfo;  print("\n".join(str(n) for n in netinfo.attached_networks()))' \
    2>/dev/null) || true
}

need_root() { [ "$(id -u)" -eq 0 ] || die "run this with sudo"; }

# ------------------------------------------------------------------- build
build() {
  local wan="$1"; shift
  $IPT -N "$IN_CHAIN"  2>/dev/null
  $IPT -N "$OUT_CHAIN" 2>/dev/null
  $IPT -F "$IN_CHAIN"
  $IPT -F "$OUT_CHAIN"
  local n
  for n in "$@"; do
    $IPT -A "$IN_CHAIN"  -s "$n" -j RETURN
    $IPT -A "$OUT_CHAIN" -d "$n" -j RETURN
  done
  # The catch-all at the end IS the meter: whatever reaches it crossed the
  # uplink and was not local. -j RETURN so nothing about traffic changes.
  $IPT -A "$IN_CHAIN"  -m comment --comment "$IN_TAG"  -j RETURN
  $IPT -A "$OUT_CHAIN" -m comment --comment "$OUT_TAG" -j RETURN

  # INPUT/OUTPUT is the board's own traffic; FORWARD is anything it routes out
  # for a client on the access point. A packet lands in exactly one of them, so
  # nothing is counted twice.
  $IPT -C INPUT   -i "$wan" -j "$IN_CHAIN"  >/dev/null 2>&1 || $IPT -I INPUT   -i "$wan" -j "$IN_CHAIN"
  $IPT -C OUTPUT  -o "$wan" -j "$OUT_CHAIN" >/dev/null 2>&1 || $IPT -I OUTPUT  -o "$wan" -j "$OUT_CHAIN"
  $IPT -C FORWARD -i "$wan" -j "$IN_CHAIN"  >/dev/null 2>&1 || $IPT -I FORWARD -i "$wan" -j "$IN_CHAIN"
  $IPT -C FORWARD -o "$wan" -j "$OUT_CHAIN" >/dev/null 2>&1 || $IPT -I FORWARD -o "$wan" -j "$OUT_CHAIN"
}

current_nets() {
  $IPT -S "$IN_CHAIN" 2>/dev/null | sed -n 's/^-A .* -s \([0-9./]*\) -j RETURN$/\1/p'
}

# Normalise for comparison: iptables prints a /32 without the suffix.
norm() { "$PYTHON_BIN" -c 'import sys,ipaddress
out=[]
for a in sys.argv[1:]:
    try: out.append(str(ipaddress.ip_network(a, strict=False)))
    except ValueError: pass
print(" ".join(sorted(out)))' "$@"; }

do_read() {
  local ib ob
  ib=$($IPT -t filter -L "$IN_CHAIN"  -v -x -n 2>/dev/null | awk -v t="$IN_TAG"  '$0 ~ t {print $2; exit}')
  ob=$($IPT -t filter -L "$OUT_CHAIN" -v -x -n 2>/dev/null | awk -v t="$OUT_TAG" '$0 ~ t {print $2; exit}')
  [ -n "$ib" ] && [ -n "$ob" ] || return 1
  echo "$ib $ob"
}

do_publish() {
  local out
  out=$(do_read) || return 1
  # Atomic: a half-written file must never be read as a counter going
  # backwards, which the sampler would book as a whole new day of traffic.
  printf '%s\n' "$out" > "${PUBLISH}.tmp" && mv -f "${PUBLISH}.tmp" "$PUBLISH"
  chmod 0644 "$PUBLISH"
  echo "$out"
}

CMD="${1:-status}"
case "$CMD" in
  publish)
    need_root
    do_publish || die "counters are not installed; run: sudo bash $0 install"
    ;;

  read)
    need_root
    do_read || die "counters are not installed; run: sudo bash $0 install"
    ;;

  status)
    WAN=$(uplink); NETS=$(free_nets)
    say "uplink:        ${WAN:-none}"
    say "not counted:   $(echo $NETS | tr '\n' ' ')"
    if out=$(do_read 2>/dev/null); then
      set -- $out
      say "counted so far: in=$1 out=$2 bytes ($(( ($1 + $2) / 1024 / 1024 )) MB)"
    elif [ "$(id -u)" -ne 0 ]; then
      say "counters:      need root to look (the published file below is the"
      say "               same numbers, and needs no privileges)"
    else
      say "counters:      NOT installed"
    fi
    [ -x "$HELPER" ] && say "helper:        $HELPER" || say "helper:        not installed"
    if [ -s "$PUBLISH" ]; then
      say "published:     $(cat "$PUBLISH")  ($(( ($(date +%s) - $(stat -c %Y "$PUBLISH")) ))s old)"
    else
      say "published:     nothing at $PUBLISH"
    fi
    systemctl is-active rknn-meter.timer >/dev/null 2>&1 \
      && say "timer:         active" || say "timer:         not running"
    ;;

  apply|save|install)
    need_root
    WAN=$(uplink); [ -n "$WAN" ] || die "no default route; nothing to meter"
    # shellcheck disable=SC2046
    NETS=$(free_nets)
    [ -n "$NETS" ] || die "netinfo found no attached subnets; refusing to meter everything as local"
    say "uplink ${WAN}; not counting $(echo $NETS | tr '\n' ' ')"
    # shellcheck disable=SC2086
    build "$WAN" $NETS
    say "counters in place"
    if [ "$CMD" != apply ]; then
      command -v netfilter-persistent >/dev/null && netfilter-persistent save >/dev/null \
        && say "saved for the next boot" || say "netfilter-persistent missing; not saved"
    fi
    if [ "$CMD" = install ]; then
      install -m 0755 "$0" "$HELPER"
      # The service is hardened with NoNewPrivileges, so it cannot use sudo --
      # and it should not have to. Root publishes the two numbers to a file and
      # the service only reads it. No sudoers rule exists for this at all.
      cat > "$UNIT" <<UNITEOF
[Unit]
Description=Publish the uplink byte counters for rknn-surveillance

[Service]
Type=oneshot
ExecStart=${HELPER} refresh
ExecStart=${HELPER} publish
UNITEOF
      cat > "$TIMER" <<TIMEREOF
[Unit]
Description=Keep ${PUBLISH} fresh

[Timer]
OnBootSec=20
OnUnitActiveSec=30
AccuracySec=5

[Install]
WantedBy=timers.target
TIMEREOF
      # An earlier design gave the service a NOPASSWD rule to read the
      # counters itself. It never worked -- the unit sets NoNewPrivileges, so
      # sudo cannot elevate inside it -- and nothing needs the rule now.
      if [ -f /etc/sudoers.d/rknn-meter ]; then
        rm -f /etc/sudoers.d/rknn-meter
        say "removed the obsolete sudoers rule; the service needs no privileges"
      fi
      systemctl daemon-reload
      systemctl enable --now rknn-meter.timer >/dev/null 2>&1
      systemctl start rknn-meter.service >/dev/null 2>&1
      say "installed ${HELPER}, publishing to ${PUBLISH} every 30s"
      [ -s "$PUBLISH" ] && say "first publish: $(cat "$PUBLISH")" \
                        || say "WARNING: ${PUBLISH} is not there yet"
    fi
    ;;

  refresh)
    # Rebuilds ONLY when the subnet list actually moved,
    # because rebuilding zeroes the counters and the sampler would then book
    # the whole total again as if it had just been used.
    need_root
    # The timer runs this every 30s; checking costs a python start, so only
    # look properly once an hour unless the chains have gone missing.
    if do_read >/dev/null 2>&1 && [ -f "$STAMP" ]; then
      now=$(date +%s); then_=$(stat -c %Y "$STAMP" 2>/dev/null || echo 0)
      if [ $(( now - then_ )) -lt "$REFRESH_EVERY_S" ]; then
        echo "unchanged"; exit 0
      fi
    fi
    touch "$STAMP"
    WAN=$(uplink); [ -n "$WAN" ] || exit 0
    NETS=$(free_nets); [ -n "$NETS" ] || exit 0
    # shellcheck disable=SC2086
    want=$(norm $NETS)
    # shellcheck disable=SC2046
    have=$(norm $(current_nets))
    if [ "$want" = "$have" ] && do_read >/dev/null 2>&1; then
      echo "unchanged"
    else
      # shellcheck disable=SC2086
      build "$WAN" $NETS
      echo "rebuilt"
    fi
    ;;

  remove)
    need_root
    WAN=$(uplink)
    for c in INPUT OUTPUT FORWARD; do
      while $IPT -D "$c" -i "${WAN:-none}" -j "$IN_CHAIN"  2>/dev/null; do :; done
      while $IPT -D "$c" -o "${WAN:-none}" -j "$OUT_CHAIN" 2>/dev/null; do :; done
    done
    $IPT -F "$IN_CHAIN"  2>/dev/null; $IPT -X "$IN_CHAIN"  2>/dev/null
    $IPT -F "$OUT_CHAIN" 2>/dev/null; $IPT -X "$OUT_CHAIN" 2>/dev/null
    systemctl disable --now rknn-meter.timer >/dev/null 2>&1
    rm -f "$HELPER" "$UNIT" "$TIMER" "$PUBLISH" "$STAMP"
    systemctl daemon-reload >/dev/null 2>&1
    command -v netfilter-persistent >/dev/null && netfilter-persistent save >/dev/null
    say "removed; metering falls back to the interface counter"
    ;;

  *) die "unknown command: $CMD (status|apply|save|install|read|publish|refresh|remove)" ;;
esac
