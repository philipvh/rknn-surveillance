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
# Publish the 4G router's web UI on board port 8082, over the tunnel.
#
#   sudo bash router_ui.sh install    # install and start the service
#   sudo bash router_ui.sh remove     # take it out again
#   bash router_ui.sh status          # no root needed
#
# NOT an iptables forward, unlike camera_ui.sh. The camera ignores the Host
# header, so a DNAT reaches it; the Huawei validates Host and answers anything
# else with a redirect to its own absolute address, which the client cannot
# route to. Measured on the club board:
#
#   camera  Host: 10.8.2.6:8080   -> 200   (and Host: wrong.example -> 200)
#   router  Host: 10.8.2.6:8082   -> 307 http://192.168.8.1/html/index.html
#
# So the request has to be rewritten, not merely forwarded. router_ui.py is a
# small reverse proxy that does exactly that and nothing else.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="${HERE}/router_ui.py"
UNIT=/etc/systemd/system/rknn-routerui.service
PORT="${PORT:-8082}"
RUN_AS="${RUN_AS:-radxa}"

say() { echo "==> $*"; }
die() { echo "!!  $*" >&2; exit 1; }

case "${1:-status}" in
  install)
    [ "$(id -u)" -eq 0 ] || die "run this with sudo"
    [ -f "$APP" ] || die "router_ui.py is not next to this script"
    cat > "$UNIT" <<UNITEOF
[Unit]
Description=Reach the 4G router's web UI from the tunnel
After=network-online.target
Wants=network-online.target

[Service]
# Binds to the tunnel address, so nothing outside the VPN can open a socket to
# it. The router UI holds the SIM, the wifi password and the firewall -- it is
# a bigger prize than the camera and does not belong on the club LAN.
ExecStart=/usr/bin/python3 ${APP} --port ${PORT}
User=${RUN_AS}
Restart=always
# The tunnel may not have an address yet at boot, and the proxy exits rather
# than binding somewhere it should not.
RestartSec=10
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
# read-only, not yes: the script itself lives under /home/radxa, and
# ProtectHome=yes would hide it from the service that runs it.
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
UNITEOF
    systemctl daemon-reload
    systemctl enable --now rknn-routerui.service >/dev/null 2>&1
    sleep 3
    systemctl is-active --quiet rknn-routerui.service \
      && say "running: $(systemctl show rknn-routerui -p ExecMainPID --value)" \
      || { journalctl -u rknn-routerui --no-pager -n 10; die "it did not start"; }
    TUN=$(ip -4 -o addr show tun0 2>/dev/null | grep -oE 'inet [0-9.]+' | cut -d' ' -f2)
    say "router UI at  http://${TUN:-<board-tunnel-ip>}:${PORT}/"
    ;;

  remove)
    [ "$(id -u)" -eq 0 ] || die "run this with sudo"
    systemctl disable --now rknn-routerui.service >/dev/null 2>&1
    rm -f "$UNIT"
    systemctl daemon-reload
    say "removed"
    ;;

  status)
    systemctl is-active rknn-routerui.service >/dev/null 2>&1 \
      && say "service:  active" || say "service:  not running"
    TUN=$(ip -4 -o addr show tun0 2>/dev/null | grep -oE 'inet [0-9.]+' | cut -d' ' -f2)
    GW=$(ip route show default | grep -oE 'via [0-9.]+' | cut -d' ' -f2)
    say "listening on  ${TUN:-<no tunnel>}:${PORT}"
    say "proxying to   ${GW:-<no gateway>}:80  (Host rewritten to it)"
    ;;

  *) die "unknown command: $1 (install|remove|status)" ;;
esac
