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

# Mount a data disk for recordings, and keep it mounted across reboots.
#
#   sudo ./mount_data.sh /dev/mmcblk1p2
#   sudo ./mount_data.sh /dev/nvme0n1p1
#
# Non-destructive: it mounts an existing filesystem and never formats. If the
# disk holds something you want gone, erase it deliberately and separately --
# this script will not do it for you.
set -euo pipefail

DEV="${1:-}"
# The mountpoint and label keep their original names: renaming them means
# unmounting a live volume, editing fstab and moving the recordings, for no
# functional gain. Override with MOUNT= and LABEL= on a fresh board.
MOUNT="${MOUNT:-/mnt/tvw-data}"
OWNER="${OWNER:-radxa}"

[ -z "$DEV" ] && { echo "usage: sudo $0 /dev/<partition>"; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "needs root: sudo $0 $DEV"; exit 2; }
[ -b "$DEV" ] || { echo "not a block device: $DEV"; exit 2; }

# Replace any existing entry for this mountpoint rather than adding a second.
# Keying on UUID alone leaves a stale line behind when a disk is reformatted --
# the old UUID no longer exists, and fstab ends up with two entries for one
# mountpoint, which is ambiguous at boot.
prune_fstab() {
  local mp="$1"
  if awk -v mp="$mp" '$0 !~ /^\s*#/ && $2 == mp {found=1} END {exit !found}' /etc/fstab; then
    cp /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d%H%M%S)"
    awk -v mp="$mp" '$0 ~ /^\s*#/ || $2 != mp' /etc/fstab > /etc/fstab.new
    mv /etc/fstab.new /etc/fstab
    echo "removed a previous /etc/fstab entry for $mp"
  fi
}

# --- refuse to touch anything the system is running from ---
ROOT_SRC="$(findmnt -no SOURCE / || true)"
if [ "$DEV" = "$ROOT_SRC" ]; then
  echo "REFUSING: $DEV is the root filesystem."; exit 1
fi
ROOT_DISK="$(lsblk -no PKNAME "$ROOT_SRC" 2>/dev/null || true)"
DEV_DISK="$(lsblk -no PKNAME "$DEV" 2>/dev/null || true)"
if [ -n "$ROOT_DISK" ] && [ "$ROOT_DISK" = "$DEV_DISK" ]; then
  echo "REFUSING: $DEV is on /dev/$DEV_DISK, the same disk as the root filesystem."
  exit 1
fi

UUID="$(blkid -s UUID -o value "$DEV")"
FSTYPE="$(blkid -s TYPE -o value "$DEV")"
SIZE="$(lsblk -no SIZE "$DEV" | tr -d ' ')"
[ -z "$UUID" ] && { echo "no filesystem on $DEV -- format it first, deliberately"; exit 1; }

echo "device : $DEV  ($SIZE, $FSTYPE, UUID=$UUID)"
echo "mount  : $MOUNT"

mkdir -p "$MOUNT"
if ! findmnt -no TARGET "$DEV" >/dev/null 2>&1; then
  mount "$DEV" "$MOUNT"
  echo "mounted."
else
  echo "already mounted at $(findmnt -no TARGET "$DEV")"
fi

# --- show what is already there before writing anything ---
echo
echo "what is on it:"
ls -1A "$MOUNT" | head -12 | sed 's/^/   /'
[ "$(ls -1A "$MOUNT" | wc -l)" -gt 12 ] && echo "   ... $(ls -1A "$MOUNT" | wc -l) entries in total"
df -h "$MOUNT" | tail -1 | awk '{print "   " $4 " free of " $2}'

DATA="${MOUNT}/tvw"
mkdir -p "$DATA"/{recordings/main,recordings/sub,events,detections}
chown -R "$OWNER:$OWNER" "$DATA"
echo
echo "created $DATA (owned by $OWNER)"

# --- survive a reboot, by UUID so it does not depend on device order ---
prune_fstab "$MOUNT"
# nofail: a dead or missing disk must never stop the board from booting.
echo "UUID=$UUID  $MOUNT  $FSTYPE  defaults,nofail,noatime,x-systemd.device-timeout=10  0  2" >> /etc/fstab
systemctl daemon-reload || true
echo "added to /etc/fstab (nofail)"

cat <<EOF

Now point the recordings at it. In config.local.yaml:

recording:
  tiers:
    - {name: main,       stream: main, path: $DATA/recordings/main, max_age_days: 2}
    - {name: sub,        stream: sub,  path: $DATA/recordings/sub,  max_age_days: 60}
    - {name: detections, stream: null, path: $DATA/detections,      max_age_days: 365}
    - {name: events,     stream: null, path: $DATA/events,          max_age_days: 730, protected: true}
paths:
  events_root: $DATA/events
  detections_root: $DATA/detections

Then: ./run_test.sh stop && ./run_test.sh start
EOF
