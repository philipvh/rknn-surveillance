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
# Reach the panel from a phone without a VPN client config, and optionally
# publish it on a public HTTPS name.
#
#   sudo bash tailscale.sh install     # packages only; joins nothing
#   sudo bash tailscale.sh up          # join the tailnet (prints a login URL)
#        bash tailscale.sh status      # no root needed
#   sudo bash tailscale.sh funnel on   # public HTTPS URL for the panel
#   sudo bash tailscale.sh funnel off
#   sudo bash tailscale.sh down        # leave the tailnet, keep the packages
#
# This does NOT replace the OpenVPN client. That is the development path and
# the way back in if this goes wrong; leave it alone until Tailscale has been
# working for a week. Nothing here touches it.
#
# Two flags matter more than the rest, and both are about not breaking a board
# an hour away:
#
#   --accept-dns=false    The board runs NetworkManager's dnsmasq for the
#                         access point and resolves `panel` itself. Letting
#                         Tailscale rewrite /etc/resolv.conf would take that
#                         over, and the wall tablet's bookmark with it.
#   no --accept-routes    and no exit node, so the default route is untouched
#                         and the 4G uplink stays the 4G uplink.
#
# On `funnel on` the panel becomes reachable from the internet, and every
# visitor arrives from 127.0.0.1 because Tailscale proxies locally. Set
# web.trusted_proxies to [127.0.0.1] or the metering and the session limit
# both read the proxy instead of the viewer -- full-quality video with no cap,
# on a 20 GB bundle. The script refuses to turn Funnel on until that is set.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
HOSTNAME_WANTED="${TS_HOSTNAME:-tvw-camera}"
PANEL_PORT="${PANEL_PORT:-8081}"

say() { echo "==> $*"; }
die() { echo "!!  $*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "run this with sudo"; }
have() { command -v "$1" >/dev/null 2>&1; }

panel_port() {
  (cd "$HERE" 2>/dev/null && "$PYTHON_BIN" -c \
    'import config; print(config.load(require_password=False)._get("web","port",default=8081))' \
    2>/dev/null) || echo "$PANEL_PORT"
}

trusted_proxies_set() {
  (cd "$HERE" 2>/dev/null && "$PYTHON_BIN" -c \
    'import config,sys
p = config.load(require_password=False)._get("web","trusted_proxies",default=[]) or []
sys.exit(0 if any(str(x).strip().startswith("127.") for x in p) else 1)' \
    2>/dev/null)
}

case "${1:-status}" in
  install)
    need_root
    have tailscale && { say "already installed: $(tailscale version | head -1)"; exit 0; }
    have curl || die "curl is needed"
    . /etc/os-release
    say "adding the Tailscale apt repository for ${ID} ${VERSION_CODENAME}"
    install -m 0755 -d /usr/share/keyrings
    curl -fsSL "https://pkgs.tailscale.com/stable/${ID}/${VERSION_CODENAME}.noarmor.gpg" \
      -o /usr/share/keyrings/tailscale-archive-keyring.gpg || die "could not fetch the key"
    curl -fsSL "https://pkgs.tailscale.com/stable/${ID}/${VERSION_CODENAME}.tailscale-keyring.list" \
      -o /etc/apt/sources.list.d/tailscale.list || die "could not fetch the source list"
    apt-get update -qq || die "apt update failed"
    apt-get install -y tailscale || die "apt install failed"
    systemctl enable --now tailscaled
    say "installed $(tailscale version | head -1); it has joined nothing yet"
    say "next:  sudo bash $0 up"
    ;;

  up)
    need_root
    have tailscale || die "not installed; run: sudo bash $0 install"
    say "joining as '${HOSTNAME_WANTED}'."
    say "A login URL will be printed -- open it and approve the machine."
    # --accept-dns=false and no --accept-routes: see the header.
    tailscale up --accept-dns=false --accept-routes=false \
                 --hostname="${HOSTNAME_WANTED}" || die "tailscale up failed"
    echo
    say "tailnet addresses:"
    tailscale ip -4 2>/dev/null | sed 's/^/    /'
    PORT=$(panel_port)
    say "the panel should now answer on  http://$(tailscale ip -4 | head -1):${PORT}/"
    say "OpenVPN is untouched: $(ip -4 -o addr show tun0 2>/dev/null | grep -oE 'inet [0-9.]+' | cut -d' ' -f2 || echo 'not up')"
    ;;

  funnel)
    need_root
    have tailscale || die "not installed"
    PORT=$(panel_port)
    case "${2:-}" in
      on)
        if ! trusted_proxies_set; then
          echo "!!  web.trusted_proxies does not include a loopback address." >&2
          echo "!!" >&2
          echo "!!  Funnel proxies from 127.0.0.1, so without it every visitor" >&2
          echo "!!  is read as the proxy: full-quality video, no data saver and" >&2
          echo "!!  no session limit, on a metered uplink. Set this in" >&2
          echo "!!  config.yaml and restart the service first:" >&2
          echo "!!" >&2
          echo "!!      web:" >&2
          echo "!!        trusted_proxies: [127.0.0.1]" >&2
          exit 2
        fi
        say "publishing the panel on the public Funnel name"
        tailscale funnel --bg "${PORT}" || {
          echo "!!  that failed. Funnel also has to be allowed for this" >&2
          echo "!!  tailnet in the admin console (Access Controls ->" >&2
          echo "!!  nodeAttrs -> funnel), and the CLI prints a link the first" >&2
          echo "!!  time. Try:  tailscale funnel status" >&2
          exit 1
        }
        echo
        tailscale funnel status 2>/dev/null | sed 's/^/    /'
        say "ANYONE with that URL reaches the panel. The panel's own password"
        say "is the only thing in front of it -- there is no Funnel login."
        ;;
      off)
        tailscale funnel --https=443 off 2>/dev/null \
          || tailscale funnel off 2>/dev/null \
          || die "could not turn Funnel off; try: tailscale funnel status"
        say "Funnel off; the tailnet still reaches the panel privately"
        ;;
      *) die "usage: sudo bash $0 funnel on|off" ;;
    esac
    ;;

  status)
    have tailscale || { say "tailscale: not installed"; exit 0; }
    say "version:  $(tailscale version | head -1)"
    BACKEND=$(tailscale status --json 2>/dev/null \
              | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("BackendState","?"))' 2>/dev/null)
    say "state:    ${BACKEND:-unknown}"
    say "address:  $(tailscale ip -4 2>/dev/null | head -1 || echo none)"
    # Direct or relayed matters on a mobile link: a relayed path adds latency
    # and sends every byte twice through Tailscale's infrastructure.
    say "peers:"
    tailscale status 2>/dev/null | sed 's/^/    /' | head -8
    echo
    say "funnel:"
    tailscale funnel status 2>/dev/null | sed 's/^/    /' | head -6 || echo "    (off)"
    echo
    say "openvpn:  $(ip -4 -o addr show tun0 2>/dev/null | grep -oE 'inet [0-9.]+' | cut -d' ' -f2 || echo 'not up')"
    ;;

  down)
    need_root
    tailscale down && say "left the tailnet; packages and OpenVPN untouched"
    ;;

  *) die "unknown command: $1 (install|up|funnel|status|down)" ;;
esac
