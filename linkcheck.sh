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
# Measure the link to the board, comparably.
#
#   ./linkcheck.sh radxa@10.8.2.10 "usb dongle"
#   ./linkcheck.sh radxa@10.8.2.10 "tp-link extender"
#
# The reason this exists: the first comparison between a USB Wi-Fi dongle and
# a wired extender was taken while a browser had the media page open, firing
# thousands of thumbnail requests down the same link. The dongle looked far
# worse than it may actually be. A measurement taken next to unknown traffic
# is not a measurement, so this checks the link is quiet first and says so
# when it is not.
set -uo pipefail

TARGET="${1:-}"
LABEL="${2:-unlabelled}"
[ -n "$TARGET" ] || { echo "usage: $0 user@host [label]" >&2; exit 2; }
HOST="${TARGET#*@}"

SSH="ssh -o ConnectTimeout=8 -o BatchMode=yes"

echo "=== ${LABEL}  ->  ${HOST}"

# --------------------------------------------------------------- is it idle?
read -r rx1 tx1 <<<"$($SSH "$TARGET" '
  d=$(ip route show default | head -1 | sed -n "s/.* dev \([^ ]*\).*/\1/p")
  cat /sys/class/net/$d/statistics/rx_bytes /sys/class/net/$d/statistics/tx_bytes \
    | tr "\n" " "' 2>/dev/null)"
sleep 2
read -r rx2 tx2 <<<"$($SSH "$TARGET" '
  d=$(ip route show default | head -1 | sed -n "s/.* dev \([^ ]*\).*/\1/p")
  cat /sys/class/net/$d/statistics/rx_bytes /sys/class/net/$d/statistics/tx_bytes \
    | tr "\n" " "' 2>/dev/null)"

if [ -n "${rx1:-}" ] && [ -n "${rx2:-}" ]; then
  busy=$(( (rx2 - rx1 + tx2 - tx1) / 2 ))
  printf "  idle check   %s B/s on the uplink" "$busy"
  if [ "$busy" -gt 40000 ]; then
    echo "   <-- SOMETHING IS USING IT"
    echo "               close the panel and any live view, then run this again;"
    echo "               a number taken next to a video stream means nothing"
  else
    echo "   (quiet enough)"
  fi
fi

# -------------------------------------------------------------------- latency
out=$(ping -c 20 -i 0.3 -W 3 "$HOST" 2>/dev/null | tail -3)
loss=$(printf '%s' "$out" | sed -n 's/.*, \([0-9.]*\)% packet loss.*/\1/p')
rtt=$(printf '%s' "$out" | sed -n 's|.*= \([0-9./]*\) ms|\1|p')
printf "  loss         %s%%\n" "${loss:-?}"
printf "  rtt          %s ms  (min/avg/max/jitter)\n" "${rtt:-?}"

# ----------------------------------------------------------------- throughput
# Two runs: mobile links vary enough that one sample says little.
for i in 1 2; do
  t=$( { $SSH "$TARGET" 'dd if=/dev/zero bs=1M count=5 2>/dev/null' \
         | dd of=/dev/null bs=1M; } 2>&1 | tail -1 \
       | grep -oE '[0-9,.]+ [kMG]B/s' | tail -1)
  printf "  throughput   %s  (run %d, 5 MB down)\n" "${t:-failed}" "$i"
done

echo "  ---"
echo "  Compare runs only when the idle check was quiet in both."
