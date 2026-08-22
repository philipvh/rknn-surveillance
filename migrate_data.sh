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
# Move the recordings to a new disk -- an NVMe, typically, because a card
# taking a fresh 18 MB file every minute forever will not last.
#
#   sudo bash migrate_data.sh /dev/nvme0n1          # show the plan
#   sudo bash migrate_data.sh /dev/nvme0n1 --apply
#
# The old card is never touched. It is unmounted and left exactly as it is, so
# if anything is wrong the way back is to put its UUID in fstab again. Nothing
# here deletes a recording.
set -uo pipefail

DEV="${1:-}"
APPLY=0
for a in "${@:2}"; do
  case "$a" in
    --apply) APPLY=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

MOUNT="${MOUNT:-/mnt/tvw-data}"
STAGE="${STAGE:-/mnt/tvw-new}"
OWNER="${OWNER:-radxa}"
SERVICE="${SERVICE:-rknn-surveillance}"
HERE="$(cd "$(dirname "$0")" && pwd)"

say() { echo "==> $*"; }
die() { echo "!!  $*" >&2; exit 1; }
run() { if [ "$APPLY" = 1 ]; then "$@"; else echo "    would: $*"; fi; }

[ -n "$DEV" ] || die "usage: sudo $0 /dev/<disk> [--apply]"
[ "$(id -u)" -eq 0 ] || die "run this with sudo"
[ -b "$DEV" ] || die "not a block device: $DEV"

case "$DEV" in
  *p[0-9]|*[0-9][0-9]) die "give the whole disk (${DEV%p*}), not a partition" ;;
esac

# Refuse to touch the disk the system is running from. Getting this wrong
# costs the whole board, not just the recordings.
ROOTDEV=$(findmnt -no SOURCE / | sed 's/p\?[0-9]*$//')
[ "$DEV" = "$ROOTDEV" ] && die "$DEV is the root disk"

say "New disk:  $DEV  ($(lsblk -dno SIZE "$DEV" | tr -d ' '))"
say "Currently: $(findmnt -no SOURCE "$MOUNT" 2>/dev/null || echo 'nothing') at $MOUNT"

if findmnt -no SOURCE "$MOUNT" >/dev/null 2>&1; then
  USED=$(du -sh "$MOUNT" 2>/dev/null | cut -f1)
  FILES=$(find "$MOUNT" -type f 2>/dev/null | wc -l)
  say "To copy:   ${USED:-?} in ${FILES} file(s)"
else
  die "$MOUNT is not mounted; there is nothing to migrate"
fi

# Anything mounted from the new disk has to go before it can be formatted.
# Our own staging mountpoint left over from an interrupted run is ours to
# clear -- refusing there just makes the retry a two-step dance for no reason.
# Anything else is somebody's data and stays where it is.
while read -r mp; do
  [ -n "$mp" ] || continue
  if [ "$mp" = "$STAGE" ]; then
    say "Clearing our own staging mount at ${mp} from an earlier run"
    umount "$mp" || die "could not unmount ${mp}"
  else
    die "$DEV is mounted at ${mp}; unmount it first if you mean to erase it"
  fi
done <<EOF
$(lsblk -nlo MOUNTPOINT "$DEV" 2>/dev/null | grep -v '^$')
EOF

echo
say "Plan"
echo "    1. stop ${SERVICE} so nothing is writing"
echo "    2. format ${DEV}  (destroys anything on it)"
echo "    3. copy ${MOUNT} -> the new disk, and verify"
echo "    4. point ${MOUNT} at the new disk"
echo "    5. start ${SERVICE}"
echo "    the old card is left untouched and unmounted"

if [ "$APPLY" != 1 ]; then
  echo
  say "Nothing changed. Re-run with --apply."
  exit 0
fi

# ------------------------------------------------------------------- do it
# From here the service is down. Whatever happens, bring it back: a failed
# migration must not also mean a camera that quietly stopped recording.
STOPPED=0
restore_service() {
  if [ "$STOPPED" = 1 ] && ! systemctl is-active --quiet "$SERVICE"; then
    echo "==> Restarting ${SERVICE} (it was stopped for the migration)"
    systemctl start "$SERVICE" || \
      echo "!!  could not restart ${SERVICE} -- start it by hand"
  fi
}
trap restore_service EXIT

say "Stopping ${SERVICE}"
systemctl stop "$SERVICE" 2>/dev/null || true
STOPPED=1
sleep 2
if fuser -m "$MOUNT" >/dev/null 2>&1; then
  say "Something still has files open on ${MOUNT}:"
  fuser -vm "$MOUNT" 2>&1 | sed 's/^/      /'
  die "stop it and try again, or the copy will miss what it is writing"
fi

say "Formatting ${DEV}"
# NO_FSTAB: this is a staging mount. mount_data.sh writes the real entry once
# the copy has been verified, and only for ${MOUNT}.
MOUNT="$STAGE" NO_FSTAB=1 bash "${HERE}/format_data.sh" "$DEV" --erase \
  || die "format_data.sh failed; nothing has been moved"

# Ask what partition actually appeared rather than constructing a name.
# "${DEV}1" on /dev/nvme0n1 yields /dev/nvme0n11 -- not a typo the eye
# catches, and not a device that exists.
PART=$(lsblk -nlo NAME "$DEV" | sed -n '2p')
PART="${PART:+/dev/$PART}"
[ -b "${PART:-}" ] || die "no partition appeared on ${DEV} after formatting"
say "New partition: ${PART}"
mkdir -p "$STAGE"
mountpoint -q "$STAGE" || mount "$PART" "$STAGE" || die "could not mount $PART"

say "Copying (this is the slow part)"
rsync -aH --info=progress2 --no-inc-recursive "${MOUNT}/" "${STAGE}/" \
  || die "the copy failed; ${MOUNT} is untouched"

say "Verifying"
a_files=$(find "$MOUNT" -type f | wc -l)
b_files=$(find "$STAGE" -type f | wc -l)
# Sum the files themselves, not `du`. du counts directory blocks, and a
# freshly made filesystem allocates those differently -- a few kB of
# difference that says nothing about whether the recordings copied.
# "%.0f", and neither of the two obvious alternatives. On the board's mawk a
# sum this large prints as 1.16827e+10 with a bare print, and %d saturates at
# 2147483647 -- both make every total compare equal, so the check would pass
# while recordings were missing. A verification that cannot fail is worse than
# none, because it is believed. %.0f goes through a double and is exact well
# past any disk this will ever see.
sum_bytes() {
  find "$1" -type f -printf '%s\n' 2>/dev/null \
    | awk '{s+=$1} END{printf "%.0f\n", s}'
}
a_bytes=$(sum_bytes "$MOUNT")
b_bytes=$(sum_bytes "$STAGE")
echo "    old: ${a_files} files, ${a_bytes} bytes"
echo "    new: ${b_files} files, ${b_bytes} bytes"
[ "$a_files" = "$b_files" ] || die "file counts differ; nothing has been switched"
if [ "$a_bytes" != "$b_bytes" ]; then
  echo "    difference: $(( b_bytes - a_bytes )) bytes"
  die "file sizes differ; nothing has been switched"
fi

# The clips are the evidence. Check a sample properly rather than trusting du.
say "Checksumming a sample of the clips"
bad=0
for f in $(find "$MOUNT" -name '*.mp4' -type f | head -5); do
  rel="${f#$MOUNT/}"
  s1=$(md5sum "$f" | cut -d' ' -f1)
  s2=$(md5sum "${STAGE}/${rel}" 2>/dev/null | cut -d' ' -f1)
  if [ "$s1" = "$s2" ]; then echo "    ok  ${rel##*/}"
  else echo "!!  differs: ${rel}"; bad=1; fi
done
[ "$bad" = 0 ] || die "a clip did not copy correctly; nothing has been switched"

say "Switching ${MOUNT} to the new disk"
umount "$STAGE" || true
umount "$MOUNT" || die "could not unmount ${MOUNT}"
bash "${HERE}/mount_data.sh" "$PART" || die "mount_data.sh failed"

chown -R "${OWNER}:${OWNER}" "$MOUNT" 2>/dev/null || true

say "Starting ${SERVICE}"
systemctl start "$SERVICE"

echo
say "Done. ${MOUNT} is now:"
findmnt -no SOURCE,SIZE,USED,AVAIL,FSTYPE "$MOUNT" | sed 's/^/      /'
echo
say "The old card is still in the slot and still holds everything."
echo "    Leave it until you are happy, then reformat or keep it as a backup."
