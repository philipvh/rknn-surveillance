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
# Switch the wireless adapter between the two jobs it has to do.
#
#   ./wifi_mode.sh status
#   ./wifi_mode.sh client [name]     join a network -- the way out at a site
#                                    with an uplink, and how the tunnel is fed
#   ./wifi_mode.sh ap                become one -- the way the wall tablet
#                                    reaches the panel at a site with nothing
#
# One radio, so one job at a time. In ap mode the board has no uplink and the
# tunnel goes with it; that is the normal state at the club and the reason the
# panel has to work with no internet at all.
set -uo pipefail

AP_SSID="${AP_SSID:-tvw-camera}"
AP_PASS="${AP_PASS:-}"                     # empty -> read from ap-secret file
AP_SECRET="${AP_SECRET:-/etc/rknn-ap-password}"
AP_NET="${AP_NET:-192.168.92}"
AP_CON="${AP_CON:-rknn-ap}"
PANEL_PORT="${PANEL_PORT:-8081}"

say() { echo "==> $*"; }
die() { echo "!!  $*" >&2; exit 1; }

DEV=$(nmcli -t -f DEVICE,TYPE device 2>/dev/null | awk -F: '$2=="wifi"{print $1; exit}')
[ -n "${DEV:-}" ] || die "no wireless interface; is the dongle in?"

# ------------------------------------------------------------------ status
show() {
  local st con addr
  st=$(nmcli -t -f GENERAL.STATE device show "$DEV" | cut -d: -f2-)
  con=$(nmcli -t -f GENERAL.CONNECTION device show "$DEV" | cut -d: -f2-)
  addr=$(ip -4 -o addr show "$DEV" 2>/dev/null | awk '{print $4}')
  echo "  device      ${DEV}"
  echo "  state       ${st:-unknown}"
  echo "  connection  ${con:--}"
  echo "  address     ${addr:--}"
  if [ "${con:-}" = "$AP_CON" ]; then
    echo "  mode        ACCESS POINT -- '${AP_SSID}', no uplink"
    echo "  tablet      join '${AP_SSID}', then http://panel:${PANEL_PORT}/"
  elif [ -n "${con:-}" ] && [ "${con}" != "--" ]; then
    echo "  mode        client of '${con}'"
    echo "  tablet      join the same network, or use the tunnel address"
  else
    echo "  mode        idle"
  fi
}

case "${1:-status}" in
status) show; exit 0 ;;
esac

[ "$(id -u)" -eq 0 ] || die "run this with sudo"

case "${1}" in
# ------------------------------------------------------------------ client
client)
  WANT="${2:-}"
  say "Leaving access point mode"
  nmcli connection down "$AP_CON" >/dev/null 2>&1 || true

  if [ -n "$WANT" ]; then
    say "Joining '${WANT}'"
    nmcli connection up "$WANT" ifname "$DEV" >/dev/null 2>&1 \
      || nmcli device wifi connect "$WANT" ifname "$DEV" \
      || die "could not join '${WANT}'"
  else
    # Whatever it used to be on. autoconnect brings it back on its own, but
    # saying so beats waiting and wondering.
    say "Letting NetworkManager reconnect to a saved network"
    nmcli device set "$DEV" managed yes >/dev/null 2>&1 || true
    nmcli device connect "$DEV" >/dev/null 2>&1 || true
  fi
  sleep 4
  echo; show
  ;;

# ---------------------------------------------------------------------- ap
ap)
  if [ -z "$AP_PASS" ]; then
    if [ -s "$AP_SECRET" ]; then
      AP_PASS=$(head -1 "$AP_SECRET")
    else
      # Generated once and kept, so the tablet does not need reconfiguring
      # every time the mode is switched.
      AP_PASS=$(tr -dc 'a-z0-9' </dev/urandom | head -c 12)
      umask 077; printf '%s\n' "$AP_PASS" > "$AP_SECRET"
      say "Generated an access point password and saved it to ${AP_SECRET}"
    fi
  fi
  [ ${#AP_PASS} -ge 8 ] || die "the access point password must be 8+ characters"

  say "Configuring '${AP_SSID}' on ${DEV} at ${AP_NET}.1"
  nmcli connection delete "$AP_CON" >/dev/null 2>&1 || true
  nmcli connection add type wifi ifname "$DEV" con-name "$AP_CON" \
      autoconnect no ssid "$AP_SSID" >/dev/null \
    || die "could not create the connection"
  nmcli connection modify "$AP_CON" \
      802-11-wireless.mode ap \
      802-11-wireless.band bg \
      802-11-wireless-security.key-mgmt wpa-psk \
      802-11-wireless-security.psk "$AP_PASS" \
      ipv4.method shared \
      ipv4.addresses "${AP_NET}.1/24" \
      ipv6.method ignore \
    || die "could not configure the access point"

  # NetworkManager runs its own dnsmasq for a shared connection, and reads
  # extra settings from here. Without this the tablet can reach the panel only
  # by address, and a kiosk bookmarked to an address is the failure this
  # project has already had once.
  mkdir -p /etc/NetworkManager/dnsmasq-shared.d
  cat > /etc/NetworkManager/dnsmasq-shared.d/rknn-panel.conf <<EOF
# Written by wifi_mode.sh
address=/panel/${AP_NET}.1
address=/panel.local/${AP_NET}.1
EOF

  say "Bringing it up"
  nmcli connection up "$AP_CON" >/dev/null 2>&1 \
    || die "the access point did not start; see: journalctl -u NetworkManager"

  sleep 3
  echo; show
  echo
  say "Tablet: join '${AP_SSID}' with the password in ${AP_SECRET}"
  echo "        then http://panel:${PANEL_PORT}/"
  echo
  say "There is no uplink in this mode. The tunnel is down until you run:"
  echo "        sudo $0 client"
  ;;

# --------------------------------------------------------------- setpass
setpass)
  # The new password arrives in a file, not an argument: an argument would be
  # in /proc/<pid>/cmdline for anything on the box to read while this runs.
  # nmcli's own modify still takes it as an argument, which is a brief window
  # this cannot close without writing NetworkManager keyfiles by hand.
  SRC="${2:-/run/rknn-vpn/ap-password}"
  [ -s "$SRC" ] || die "no password supplied"
  NEW=$(head -1 "$SRC")
  shred -u "$SRC" 2>/dev/null || rm -f "$SRC"
  [ ${#NEW} -ge 8 ] || die "the password must be at least 8 characters"
  [ ${#NEW} -le 63 ] || die "the password must be at most 63 characters"

  umask 077
  printf '%s\n' "$NEW" > "$AP_SECRET"
  chmod 600 "$AP_SECRET"
  say "Stored the access point password"

  if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$AP_CON"; then
    nmcli connection modify "$AP_CON" \
        802-11-wireless-security.psk "$NEW" \
      || die "could not update the access point"
    say "Updated '${AP_SSID}'"
    if nmcli -t -f NAME connection show --active 2>/dev/null | grep -qx "$AP_CON"; then
      say "Restarting it so the new password takes effect"
      nmcli connection down "$AP_CON" >/dev/null 2>&1 || true
      nmcli connection up "$AP_CON" >/dev/null 2>&1 \
        || die "the access point did not come back up"
    fi
  else
    say "No access point configured yet; it will use this when you create one"
  fi
  echo; show
  ;;

*) die "usage: $0 {status|client [name]|ap|setpass [file]}" ;;
esac
