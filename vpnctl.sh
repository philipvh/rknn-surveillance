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
# Start, stop, enable or disable ONE openvpn client unit. Installed to
# /usr/local/sbin and granted NOPASSWD sudo, so the panel can work the tunnel
# without being root.
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
