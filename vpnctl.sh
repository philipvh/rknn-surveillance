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
# The panel's privileged actions, in one validated place. Installed to
# /usr/local/sbin and granted NOPASSWD sudo, so the panel can work the tunnel
# and the radio without being root.
#
#   rknn-vpnctl {start|stop|restart|enable|disable|status} <tunnel>
#   rknn-vpnctl wifi {client|ap|status}
#
# The name says vpn because that is what it did first; it now carries the Wi-Fi
# mode switch too, because both need exactly the same thing -- a root action
# asked for by a process that is forbidden to become root -- and one audited
# channel is easier to reason about than two.
#
# The reason this exists rather than a sudoers line for systemctl: a rule like
#   radxa ALL=(root) NOPASSWD: /bin/systemctl * openvpn-client@*
# looks narrow and is not. sudo's wildcards match '/' too, so the argument can
# be walked somewhere else entirely. Here the action comes from a fixed list
# and the name has to match a strict pattern before systemctl is called at all.

set -uo pipefail

# Two ways in. Directly with an action and a name (from a shell, via sudo), or
# --from-request, which is how the panel reaches it: the service cannot use
# sudo at all, because its unit sets NoNewPrivileges=yes and sudo is setuid.
# Instead it drops a request file that a root .path unit is watching, so the
# unprivileged side never escalates anything -- it writes one line to a file.
REQ_DIR=/run/rknn-vpn
if [ "${1:-}" = "--from-request" ]; then
  REQ="${2:-$REQ_DIR/request}"
  [ -f "$REQ" ] || exit 0
  read -r RA RN _ < "$REQ" || true
  rm -f "$REQ"
  RESULT="$REQ_DIR/result"
  if out=$("$0" "${RA:-}" "${RN:-}" 2>&1); then
      printf 'ok %s\n' "$out" > "$RESULT"
  else
      printf 'fail %s\n' "$out" > "$RESULT"
  fi
  chmod 644 "$RESULT" 2>/dev/null || true
  exit 0
fi

SYSTEMCTL=/bin/systemctl
[ -x "$SYSTEMCTL" ] || SYSTEMCTL=/usr/bin/systemctl

usage() {
  echo "usage: $(basename "$0") {start|stop|restart|enable|disable|status} <name>" >&2
  exit 2
}

[ $# -eq 2 ] || usage
ACTION="$1"
NAME="$2"

# The radio. Its own script does the work and its own validation; this only
# decides that "wifi" plus one of three words is a thing the panel may ask for.
if [ "$ACTION" = "wifi" ]; then
  case "$NAME" in
    client|ap|status|setpass) ;;
    *) echo "wifi action must be client, ap, status or setpass" >&2; exit 3 ;;
  esac
  for d in /home/radxa/rknn-surveillance /opt/rknn-surveillance; do
    [ -x "$d/wifi_mode.sh" ] && exec bash "$d/wifi_mode.sh" "$NAME"
  done
  echo "wifi_mode.sh not found" >&2; exit 4
fi

case "$ACTION" in
  start|stop|restart|enable|disable|status) ;;
  *) usage ;;
esac

# Letters, digits, dash and underscore only. No dots, no slashes, nothing that
# could name a different unit or escape the openvpn-client@ template.
if ! printf '%s' "$NAME" | grep -qE '^[A-Za-z0-9_-]{1,64}$'; then
  echo "refusing a tunnel name that is not plain: $NAME" >&2
  exit 3
fi

UNIT="openvpn-client@${NAME}.service"

# It must correspond to a real configuration. Enabling a unit for a config
# that does not exist produces a service that fails at every boot.
if [ "$ACTION" != "status" ] && [ ! -f "/etc/openvpn/client/${NAME}.conf" ]; then
  echo "no such tunnel configuration: /etc/openvpn/client/${NAME}.conf" >&2
  exit 4
fi

case "$ACTION" in
  status)
    echo "active=$($SYSTEMCTL is-active "$UNIT" 2>/dev/null || true)"
    echo "enabled=$($SYSTEMCTL is-enabled "$UNIT" 2>/dev/null || true)"
    ;;
  enable)
    # --now so "on at boot" also means "on right now", which is what anyone
    # ticking a box on a wall panel means by it.
    exec "$SYSTEMCTL" enable --now "$UNIT"
    ;;
  disable)
    exec "$SYSTEMCTL" disable --now "$UNIT"
    ;;
  *)
    exec "$SYSTEMCTL" "$ACTION" "$UNIT"
    ;;
esac
