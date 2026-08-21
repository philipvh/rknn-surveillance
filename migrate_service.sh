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

# Rename the installed service from tvw-surveillance to rknn-surveillance.
# Run once, as root, ON THE BOARD:
#
#   sudo bash migrate_service.sh
#
# It moves the install directory, installs the unit under the new name, and
# removes the old one. Recordings are NOT touched: they live on their own
# volume, and their paths are pinned in config.local.yaml, which moves with
# the directory.
set -uo pipefail

OLD_DIR=/home/radxa/tvw_surveillance
NEW_DIR=/home/radxa/rknn-surveillance
OLD_SVC=tvw-surveillance
NEW_SVC=rknn-surveillance
USER_NAME=radxa

say() { echo "==> $*"; }
die() { echo "!!  $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this with sudo"

if [ ! -d "$OLD_DIR" ] && [ -d "$NEW_DIR" ]; then
  say "Already migrated: $NEW_DIR exists and $OLD_DIR does not."
  systemctl is-active "$NEW_SVC" >/dev/null 2>&1 \
    && say "$NEW_SVC is active." || say "$NEW_SVC is NOT active."
  exit 0
fi
[ -d "$OLD_DIR" ] || die "$OLD_DIR does not exist; nothing to migrate"
[ -e "$NEW_DIR" ] && die "$NEW_DIR already exists; move it aside first"

# The config that names the recording volume is board-specific and is not part
# of a deploy. Losing it would point the recorder back at the SD card.
[ -f "$OLD_DIR/config.local.yaml" ] \
  || say "WARNING: no config.local.yaml -- this board may use defaults"

say "Stopping $OLD_SVC"
systemctl stop "$OLD_SVC" 2>/dev/null || true
systemctl disable "$OLD_SVC" 2>/dev/null || true

say "Moving $OLD_DIR -> $NEW_DIR"
mv "$OLD_DIR" "$NEW_DIR" || die "the move failed; nothing else has changed"
chown -R "${USER_NAME}:${USER_NAME}" "$NEW_DIR"

say "Removing the old unit"
rm -f "/etc/systemd/system/${OLD_SVC}.service"
rm -f "/etc/systemd/system/${OLD_SVC}.service.bak."* 2>/dev/null || true
rm -f /etc/systemd/journald.conf.d/tvw.conf
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

say "Installing $NEW_SVC from $NEW_DIR"
if ! sudo -u "$USER_NAME" bash -lc "cd '$NEW_DIR' && ./install.sh"; then
  echo
  die "install.sh failed. The files are at $NEW_DIR and the old unit is gone;
     fix the problem and re-run '$NEW_DIR/install.sh' rather than this script."
fi

echo
say "Done. Old references you may still have:"
echo "    ssh ... 'sudo systemctl restart ${OLD_SVC}'   ->  ${NEW_SVC}"
echo "    cd ~/tvw_surveillance                          ->  cd ~/${NEW_DIR##*/}"
echo
say "The recording volume is unchanged:"
grep -hE "events_root|detections_root" "$NEW_DIR/config.local.yaml" 2>/dev/null \
  | sed 's/^/    /' || echo "    (see config.local.yaml)"
