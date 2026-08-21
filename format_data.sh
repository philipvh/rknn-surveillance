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

# Erase a disk and set it up for recordings. THIS DESTROYS EVERYTHING ON IT.
#
#   sudo ./format_data.sh /dev/mmcblk1              # shows what would be lost
#   sudo ./format_data.sh /dev/mmcblk1 --erase      # actually does it
#
# Refuses to touch the disk the system is running from, refuses if anything on
# it is mounted, and does nothing at all without --erase.
set -euo pipefail
export PATH="/sbin:/usr/sbin:$PATH"     # wipefs, sfdisk, mkfs live here

DEV="${1:-}"
CONFIRM="${2:-}"
# The mountpoint and label keep their original names: renaming them means
# unmounting a live volume, editing fstab and moving the recordings, for no
# functional gain. Override with MOUNT= and LABEL= on a fresh board.
MOUNT="${MOUNT:-/mnt/tvw-data}"
OWNER="${OWNER:-radxa}"
LABEL="${LABEL:-tvwdata}"

[ -z "$DEV" ] && { echo "usage: sudo $0 /dev/<disk> [--erase]"; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "needs root: sudo $0 $*"; exit 2; }
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

# ---------------------------------------------------------------- guards
ROOT_SRC="$(findmnt -no SOURCE / || true)"
ROOT_DISK="$(lsblk -no PKNAME "$ROOT_SRC" 2>/dev/null || true)"
DEV_NAME="$(basename "$DEV")"
if [ "$DEV_NAME" = "$ROOT_DISK" ] || [ "$DEV" = "$ROOT_SRC" ]; then
  echo "REFUSING: $DEV is the disk this system is running from."; exit 1
fi
for part in $(lsblk -lno NAME "$DEV" | tail -n +2); do
  if [ "$part" = "$ROOT_DISK" ] || findmnt -no TARGET "/dev/$part" >/dev/null 2>&1; then
    echo "REFUSING: /dev/$part is mounted at $(findmnt -no TARGET "/dev/$part")."
    echo "          Unmount it first if you really mean this."
    exit 1
  fi
done
if [ "$(lsblk -dno RM "$DEV" 2>/dev/null)" != "1" ] && \
   [ "$(lsblk -dno HOTPLUG "$DEV" 2>/dev/null)" != "1" ]; then
  echo "NOTE: $DEV does not report itself as removable. Check the device name."
fi

# ------------------------------------------------------------- what is lost
echo "about to erase:"
lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,UUID "$DEV" | sed 's/^/    /'
MODEL="$(cat "/sys/block/${DEV_NAME}/device/name" 2>/dev/null || echo unknown)"
echo "    card: $MODEL, $(lsblk -dno SIZE "$DEV")"
echo "    root filesystem is on: ${ROOT_SRC} (disk ${ROOT_DISK}) -- not this one"

if [ "$CONFIRM" != "--erase" ]; then
  echo
  echo "Nothing has been changed. Re-run with --erase to go ahead:"
  echo "    sudo $0 $DEV --erase"
  exit 0
fi

# --------------------------------------------------------------- do it
echo
echo "erasing $DEV ..."
wipefs -a "$DEV" >/dev/null
# One partition, the whole disk, starting at 1 MiB so erase blocks line up --
# alignment matters more on flash than it does on a spinning disk.
sfdisk "$DEV" >/dev/null <<EOF
label: gpt
start=2048, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name="tvwdata"
EOF
partprobe "$DEV" 2>/dev/null || true
sleep 2

PART="${DEV}p1"
[ -b "$PART" ] || PART="${DEV}1"
[ -b "$PART" ] || { echo "could not find the new partition on $DEV"; exit 1; }

# -m 1: keep 1% for root instead of the default 5%. On 59 GB that is 3 GB back.
# The journal stays: power cuts are expected at a tennis club, and a journal is
# what makes the filesystem survive one.
mkfs.ext4 -q -F -L "$LABEL" -m 1 "$PART"
UUID="$(blkid -s UUID -o value "$PART")"
echo "made ext4 on $PART (label $LABEL, UUID $UUID)"

mkdir -p "$MOUNT"
mount "$PART" "$MOUNT"
DATA="${MOUNT}/tvw"
mkdir -p "$DATA"/{recordings/main,recordings/sub,events,detections}
chown -R "$OWNER:$OWNER" "$MOUNT"

# NO_FSTAB=1 when this is a staging mount that something else will move into
# place afterwards. Writing an entry for a temporary mountpoint leaves the disk
# mounted twice at the next boot, under two names.
if [ "${NO_FSTAB:-0}" = "1" ]; then
  echo "NO_FSTAB=1: leaving /etc/fstab alone"
else
prune_fstab "$MOUNT"
# nofail: a dead or missing disk must never stop the board from booting.
echo "UUID=$UUID  $MOUNT  ext4  defaults,nofail,noatime,x-systemd.device-timeout=10  0  2" >> /etc/fstab
systemctl daemon-reload || true
echo "added to /etc/fstab (nofail)"
fi

echo
df -h "$MOUNT" | tail -1 | awk '{print "  " $4 " free of " $2 " at " $6}'
cat <<EOF

Add this to config.local.yaml on the board:

recording:
  tiers:
    - {name: main,       stream: main, path: $DATA/recordings/main, max_age_days: 3}
    - {name: sub,        stream: sub,  path: $DATA/recordings/sub,  max_age_days: 30}
    - {name: detections, stream: null, path: $DATA/detections,      max_age_days: 365}
    - {name: events,     stream: null, path: $DATA/events,          max_age_days: 730, protected: true}
paths:
  events_root: $DATA/events
  detections_root: $DATA/detections

Then drop --no-record:
  ./run_test.sh stop && ./run_test.sh start
EOF
