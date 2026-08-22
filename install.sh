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

set -euo pipefail

# One place to change the name. The service is derived from it, so the unit
# and the directory can never drift apart.
PROJECT_NAME="rknn-surveillance"
PROJECT_DIR="/home/radxa/${PROJECT_NAME}"
SERVICE="${PROJECT_NAME//_/-}"
USER_NAME="radxa"
PYTHON_BIN="/usr/bin/python3"

echo "==> Updating APT and installing system packages (needs sudo)…"
sudo apt-get install -y \
  ffmpeg \
  python3 \
  python3-opencv \
  python3-flask \
  python3-jinja2 \
  python3-werkzeug \
  python3-yaml \
  python3-numpy \
  gpiod \
  python3-libgpiod \
  openvpn \
  iw \
  wireless-regdb

# yolov10.py needs torch: dfl() and post_process_yolov10() use softmax, topk,
# gather and cat. The old install.sh never installed it, so a clean board would
# fail at the first import. Prefer the distro package; fall back to pip.
echo "==> Ensuring PyTorch is present..."
if ! python3 -c "import torch" 2>/dev/null; then
  sudo apt-get install -y python3-torch 2>/dev/null \
    || pip3 install --break-system-packages torch --index-url https://download.pytorch.org/whl/cpu
fi
python3 -c "import torch; print('    torch', torch.__version__)"

# The recordings usually live on their own disk, outside PROJECT_DIR.
# ProtectSystem=full leaves /mnt writable so this works either way, but naming
# it means the unit keeps working if the data root ever moves somewhere that
# hardening does cover.
DATA_RW=""
DATA_ROOTS=$(cd "${PROJECT_DIR}" && "${PYTHON_BIN}" - <<'PYEOF' 2>/dev/null || true
import config
c = config.load(require_password=False)
roots = set()
for p in (c.tier("main").path, c.events_root, c.detections_root):
    roots.add(str(p))
# The common parent, so one entry covers all three.
import os
print(os.path.commonpath(sorted(roots)) if roots else "")
PYEOF
)
if [ -n "${DATA_ROOTS}" ] && [ "${DATA_ROOTS}" != "/" ]; then
  case "${DATA_ROOTS}" in
    "${PROJECT_DIR}"*) : ;;                 # already covered
    *) DATA_RW=" ${DATA_ROOTS}"
       echo "==> Data root: ${DATA_ROOTS} (added to ReadWritePaths)" ;;
  esac
fi

# Create required folders
echo "==> Creating folders…"
mkdir -p "${PROJECT_DIR}/recordings/main" \
         "${PROJECT_DIR}/recordings/sub" \
         "${PROJECT_DIR}/detections" \
         "${PROJECT_DIR}/events" \
         "${PROJECT_DIR}/templates" \
         "${PROJECT_DIR}/static"

# Board-specific config: created once, never overwritten by a deploy.
if [ ! -f "${PROJECT_DIR}/config.local.yaml" ]; then
  echo "==> Creating config.local.yaml — put this board's camera address there"
  cp "${PROJECT_DIR}/config.local.example.yaml" "${PROJECT_DIR}/config.local.yaml"
fi

# Secrets file: created once, never overwritten, never world-readable.
if [ ! -f "${PROJECT_DIR}/secrets.yaml" ]; then
  echo "==> Creating secrets.yaml from the example — EDIT IT before starting"
  cp "${PROJECT_DIR}/secrets.example.yaml" "${PROJECT_DIR}/secrets.yaml"
fi
chmod 600 "${PROJECT_DIR}/secrets.yaml"

echo "==> Fixing ownership to ${USER_NAME}:${USER_NAME}…"
sudo chown -R "${USER_NAME}:${USER_NAME}" "${PROJECT_DIR}"

# Systemd service: surveillance (detection, recording, PTZ, wall panel)
echo "==> Installing systemd service: ${SERVICE}.service"
sudo tee /etc/systemd/system/${SERVICE}.service >/dev/null <<EOF
[Unit]
Description=RKNN surveillance (detection, recording, PTZ, panel)
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
Type=notify
NotifyAccess=main
User=${USER_NAME}
WorkingDirectory=${PROJECT_DIR}
Environment=PYTHONUNBUFFERED=1
# Where the panel drops a tunnel request for root to pick up. systemd creates
# it owned by the service user and removes it when the service stops.
RuntimeDirectory=rknn-vpn
RuntimeDirectoryMode=0755
ExecStart=${PYTHON_BIN} ${PROJECT_DIR}/surveillance_main.py

# The last line of defence for the motors. An in-process handler cannot run
# after SIGKILL, so systemd sends the stop instead -- on every exit, clean or
# not. The driver also stops the camera when it next starts up, so a crash
# followed by a restart is covered twice.
ExecStopPost=-${PYTHON_BIN} ${PROJECT_DIR}/ptz_cli.py stop

# If the capture pipeline wedges, the process stops pinging and systemd
# restarts it. See health.py: the ping is deliberately conditional on frames
# still arriving, because a watchdog that always pings never fires.
WatchdogSec=120
Restart=always
RestartSec=5
# Keep trying: an unattended box at a tennis club has nobody to press retry.
StartLimitIntervalSec=0

# Give ffmpeg and the NPU room, but not the whole board.
Nice=-5
MemoryMax=3G
TasksMax=256

# Modest hardening. The service needs its own directory, /dev/video*, the GPIO
# character device and the network; it does not need anything else.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
# The project lives under /home, so ProtectHome must stay off. With =yes the
# namespace makes /home empty and systemd cannot even enter WorkingDirectory:
# the unit dies with status=200/CHDIR before Python runs. ReadWritePaths does
# not punch through it, and =tmpfs + BindPaths was tried on this board
# (systemd 247) and failed the same way. To get this hardening back, move the
# project to /opt/${PROJECT_NAME} -- then =yes costs nothing.
ProtectHome=no
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
ReadWritePaths=${PROJECT_DIR}${DATA_RW}

[Install]
WantedBy=multi-user.target
EOF

# The wall panel runs inside this service so it shares the process's PTZ
# object, watchdog and motor budget; media_browser.py still exists for
# browsing without PTZ. There is no separate media-browser unit.
#
# The older rknn_yolov10 install is deliberately left alone -- but it cannot
# run at the same time as this one, so say so rather than fighting over the
# camera and port 8080.
echo "==> Checking for an older install still running…"
for old in surveillance media-browser; do
  if systemctl is-active --quiet "${old}.service" 2>/dev/null; then
    echo "    CONFLICT: ${old}.service is running."
    echo "              It will fight this one for port 8080 and for the camera."
    echo "              Stop it when you are ready to switch over:"
    echo "                sudo systemctl disable --now ${old}.service"
  fi
done

# --- things that are silent failures if nobody looks ---
echo "==> Checking the clock and the RTC…"
if [ -e /dev/rtc0 ]; then
  echo "    RTC present: $(sudo hwclock -r 2>/dev/null || echo 'could not read')"
else
  echo "    WARNING: no /dev/rtc0. With no internet there is no NTP either, so"
  echo "             after a power cut the clock will be wrong and recordings"
  echo "             will be filed under a nonsense date. Fit the RTC battery."
fi
if ! date +%Y | grep -qE '^20[2-9][0-9]$'; then
  echo "    WARNING: the system clock reads $(date). Set it before starting:"
  echo "             sudo date -s 'YYYY-MM-DD HH:MM:SS' && sudo hwclock -w"
fi

echo "==> Checking storage…"
if lsblk -o NAME 2>/dev/null | grep -qi nvme; then
  echo "    NVMe present"
else
  echo "    WARNING: no NVMe found. Continuous recording to an SD card will"
  echo "             wear it out within months."
fi

echo "==> Checking GPIO access…"
if id -nG "${USER_NAME}" | grep -qw gpio; then
  echo "    ${USER_NAME} is in the gpio group"
else
  echo "    ${USER_NAME} is not in the gpio group; adding it (log out and in)"
  sudo usermod -aG gpio "${USER_NAME}" || true
fi

echo "==> Limiting the journal so logs cannot fill the disk…"
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/${SERVICE}.conf >/dev/null <<'EOF'
[Journal]
SystemMaxUse=500M
MaxRetentionSec=1month
EOF
sudo systemctl restart systemd-journald || true

# The cap above only bounds what journald keeps from here on. A board that has
# been running a while can already be sitting on far more, so reclaim it now --
# on a 15 GB root, a gigabyte of old journal is real money.
_j_before=$(journalctl --disk-usage 2>/dev/null | grep -o "[0-9.]*[MG]" | tail -1)
sudo journalctl --vacuum-size=500M >/dev/null 2>&1 || true
echo "    journal: ${_j_before:-?} -> $(journalctl --disk-usage 2>/dev/null | grep -o '[0-9.]*[MG]' | tail -1)"

# Let the service join a wireless network without being root. Two narrow
# NetworkManager actions, for this one user -- not shell root, and not a
# sudoers rule that would grant far more than intended. polkit 0.105 (Debian
# 11) reads .pkla; newer versions read .rules, so write whichever applies.
echo "==> Allowing ${USER_NAME} to manage network connections…"
# sudo test, not test: /etc/polkit-1/localauthority is mode 700 root, so an
# unprivileged [ -d ] on anything inside it is false however present it is.
# Testing it as the ordinary user wrote no rule at all and said it had.
if sudo test -d /etc/polkit-1/localauthority; then
  sudo mkdir -p /etc/polkit-1/localauthority/50-local.d
  # ResultAny, not ResultActive. "Active" means an interactive session on a
  # local seat. The panel is a daemon with no session at all, and an ssh login
  # is a session but not an active one -- granting only ResultActive grants
  # nothing to either of the two things that actually need it.
  sudo tee /etc/polkit-1/localauthority/50-local.d/50-${SERVICE}-nm.pkla \
    >/dev/null <<EOF
[Let ${USER_NAME} manage NetworkManager]
Identity=unix-user:${USER_NAME}
Action=org.freedesktop.NetworkManager.settings.modify.system;org.freedesktop.NetworkManager.network-control;org.freedesktop.NetworkManager.enable-disable-wifi;org.freedesktop.NetworkManager.wifi.scan;org.freedesktop.NetworkManager.wifi.share.open;org.freedesktop.NetworkManager.wifi.share.protected
ResultAny=yes
ResultInactive=yes
ResultActive=yes
EOF
elif sudo test -d /etc/polkit-1/rules.d; then
  sudo tee /etc/polkit-1/rules.d/50-${SERVICE}-nm.rules >/dev/null <<EOF
polkit.addRule(function (action, subject) {
  if (subject.user == "${USER_NAME}" &&
      action.id.indexOf("org.freedesktop.NetworkManager.") === 0) {
    return polkit.Result.YES;
  }
});
EOF
fi
sudo systemctl reload polkit 2>/dev/null \
  || sudo systemctl restart polkit 2>/dev/null || true

# Prove it, rather than assume it. Creating and deleting a dummy connection
# needs exactly the permission the panel needs, and costs nothing.
if nmcli connection add type dummy ifname _pk_probe \
     con-name _pk_probe autoconnect no >/dev/null 2>&1; then
  nmcli connection delete _pk_probe >/dev/null 2>&1
  echo "    network settings: ${USER_NAME} can change them"
else
  echo "!!  network settings: ${USER_NAME} still CANNOT change them."
  echo "!!  Joining a wireless network from the panel will not work."
  echo "!!  Check: sudo cat /etc/polkit-1/localauthority/50-local.d/50-${SERVICE}-nm.pkla"
fi

# Let the panel work the tunnel without being root. A validating helper plus
# a sudoers rule naming only that helper -- not systemctl, whose arguments a
# wildcard rule cannot safely constrain.
if [ -f "${PROJECT_DIR}/vpnctl.sh" ]; then
  echo "==> Installing the tunnel helper…"
  sudo install -o root -g root -m 755 "${PROJECT_DIR}/vpnctl.sh" \
    /usr/local/sbin/rknn-vpnctl
  sudo tee /etc/sudoers.d/${SERVICE}-vpn >/dev/null <<EOF
${USER_NAME} ALL=(root) NOPASSWD: /usr/local/sbin/rknn-vpnctl
EOF
  sudo chmod 440 /etc/sudoers.d/${SERVICE}-vpn

  # The panel cannot use sudo: its own unit sets NoNewPrivileges=yes, which is
  # exactly what stops a setuid binary elevating. So it asks by writing a file
  # into its runtime directory, and root notices.
  sudo tee /etc/systemd/system/${SERVICE}-vpn.path >/dev/null <<EOF
[Unit]
Description=Watch for a tunnel request from the ${SERVICE} panel

[Path]
PathExists=/run/rknn-vpn/request
Unit=${SERVICE}-vpn.service

[Install]
WantedBy=multi-user.target
EOF
  sudo tee /etc/systemd/system/${SERVICE}-vpn.service >/dev/null <<EOF
[Unit]
Description=Apply a tunnel request from the ${SERVICE} panel

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/rknn-vpnctl --from-request /run/rknn-vpn/request
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now ${SERVICE}-vpn.path
  # A bad sudoers file locks everyone out of sudo, so check before trusting it.
  if ! sudo visudo -c -f /etc/sudoers.d/${SERVICE}-vpn >/dev/null 2>&1; then
    echo "!!  the sudoers rule did not validate; removing it"
    sudo rm -f /etc/sudoers.d/${SERVICE}-vpn
  fi
fi

echo "==> Reloading systemd daemon…"
sudo systemctl daemon-reload

echo "==> Enabling services to start at boot…"
sudo systemctl enable ${SERVICE}.service

echo "==> Starting services now…"
sudo systemctl restart ${SERVICE}.service

# Say plainly whether it came up. A unit that fails at CHDIR or a bad config
# sits in "activating (auto-restart)" and restarts forever, which reads as
# "starting" unless you look -- so wait, then check, then show the reason.
echo "==> Waiting for the service to report ready…"
_ok=""
for _i in $(seq 1 30); do
  _st=$(systemctl is-active ${SERVICE}.service 2>/dev/null || true)
  if [ "${_st}" = "active" ]; then _ok="yes"; break; fi
  if [ "${_st}" = "failed" ]; then break; fi
  sleep 2
done

_PORT=$(cd "${PROJECT_DIR}" && "${PYTHON_BIN}" -c \
  "import config; print(config.load(require_password=False)._get('web','port',default=8080))" \
  2>/dev/null || echo 8080)

if [ -n "${_ok}" ]; then
  # The camera segment is not created here: it changes the machine's networking
# and deserves to be a deliberate act, not a side effect of a code deploy.
if ! ip -4 addr show 2>/dev/null | grep -q "inet 192.168.91.1/"; then
  echo
  echo "NOTE: the camera network is not configured on this board."
  echo "      The camera and the wall tablet live on it, and it is the part"
  echo "      that must work with no internet at all:"
  echo "          sudo bash ${PROJECT_DIR}/setup_network.sh"
fi

echo "==> Done -- ${SERVICE} is active."
  echo "• Wall panel (the board has more than one NIC):"
  for _a in $(hostname -I); do echo "    http://${_a}:${_PORT}/"; done
else
  echo
  echo "!!  ${SERVICE} did NOT come up (state: $(systemctl is-active ${SERVICE}.service 2>/dev/null))."
  echo "!!  Last lines:"
  sudo journalctl -u ${SERVICE}.service --no-pager -n 15 2>/dev/null | sed "s/^/      /"
  echo
  echo "!!  Nothing is serving the panel until this is fixed."
  exit 1
fi
echo "• Check everything: ${PROJECT_DIR}/doctor.py"
echo "• Check logs:"
echo "    sudo journalctl -u ${SERVICE}.service -f"

echo "NOTE:"
echo "• If the RKNN toolkit is not installed yet, install it before running."
echo "• Edit ${PROJECT_DIR}/config.yaml (camera host/port) and"
echo "  ${PROJECT_DIR}/secrets.yaml (camera password) before starting."
echo "• Check the config parses:  python3 -c 'import config; config.load()'"

