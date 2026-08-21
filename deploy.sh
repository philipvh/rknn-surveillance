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

# Copy the project to the Rock 5B. Code and model only -- no recordings, no
# secrets, no board-specific config, no caches. Safe to re-run.
#
#   ./deploy.sh radxa@192.168.90.131 --setup-ssh    do this first, once
#   ./deploy.sh radxa@192.168.90.131                one-shot copy
#   ./deploy.sh radxa@192.168.90.131 --dry-run      show what would go
#   ./deploy.sh radxa@192.168.90.131 --watch        re-sync while you edit
#   ./deploy.sh radxa@192.168.90.131 --watch --restart
#   ./deploy.sh radxa@192.168.90.131 --logs         follow the journal
#
# All ssh and rsync traffic shares one multiplexed connection, so a password
# is asked at most once per run even in watch mode. --setup-ssh installs a key
# so it is not asked at all, which is what watch mode really wants.
set -euo pipefail

TARGET="${1:-}"
[ -z "$TARGET" ] && { sed -n '2,17p' "$0" | sed 's/^# \?//'; exit 2; }
shift

DRY=0; WATCH=0; RESTART=0; LOGS=0; SETUP=0
for a in "$@"; do
  case "$a" in
    --dry-run)   DRY=1;;
    --watch)     WATCH=1;;
    --restart)   RESTART=1;;
    --logs)      LOGS=1;;
    --setup-ssh) SETUP=1;;
    *) echo "unknown option: $a" >&2; exit 2;;
  esac
done

REMOTE_DIR="${REMOTE_DIR:-/home/radxa/rknn-surveillance}"
HERE="$(cd "$(dirname "$0")" && pwd)"
IGNORE="${HERE}/.deployignore"
SERVICE="$(basename "$REMOTE_DIR" | tr '_' '-')"
POLL_S="${POLL_S:-2}"

# ---------------------------------------------------------------- ssh reuse
CTL_DIR="${TMPDIR:-/tmp}/.rknn-deploy-$$"
mkdir -p "$CTL_DIR"; chmod 700 "$CTL_DIR"
CTL="${CTL_DIR}/ctl"
SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=${CTL}" -o ControlPersist=600)
cleanup() {
  ssh "${SSH_OPTS[@]}" -O exit "$TARGET" 2>/dev/null || true
  rm -rf "$CTL_DIR"
}
trap cleanup EXIT
rsh() { ssh "${SSH_OPTS[@]}" "$@"; }

# --------------------------------------------------------------- setup-ssh
if [ "$SETUP" = 1 ]; then
  KEY="${HOME}/.ssh/id_ed25519"
  if [ ! -f "${KEY}.pub" ]; then
    echo "==> No key at ${KEY}, generating one"
    ssh-keygen -t ed25519 -N "" -f "$KEY"
  fi
  echo "==> Installing your public key on ${TARGET} (asks for the password once)"
  ssh-copy-id -i "${KEY}.pub" "$TARGET"
  echo
  echo "==> Optional but worth it: let the restart work without a password."
  echo "    On the board, run:"
  echo
  echo "      echo \"\$USER ALL=(root) NOPASSWD: /bin/systemctl restart ${SERVICE}.service, \\"
  echo "            /bin/systemctl stop ${SERVICE}.service, /bin/systemctl start ${SERVICE}.service\" \\"
  echo "        | sudo tee /etc/sudoers.d/rknn-deploy"
  echo "      sudo chmod 440 /etc/sudoers.d/rknn-deploy"
  echo
  echo "    Then --watch --restart runs unattended."
  exit 0
fi

# ------------------------------------------------------------- connect once
echo "==> Connecting to ${TARGET}…"
if ! rsh -o ConnectTimeout=15 "$TARGET" true; then
  echo
  echo "Could not connect. If it asked for a password and refused it, or you"
  echo "want watch mode to stop asking:"
  echo "    $0 $TARGET --setup-ssh"
  exit 1
fi

if [ "$LOGS" = 1 ]; then
  exec ssh "${SSH_OPTS[@]}" -t "$TARGET" "journalctl -u ${SERVICE} -f -n 50"
fi

# ------------------------------------------------------------------ method
tar_excludes() {
  local line
  while IFS= read -r line; do
    line="${line%%#*}"; line="$(echo "$line" | tr -d '[:space:]')"
    [ -z "$line" ] && continue
    line="${line%/}"
    printf -- '--exclude=./%s\n--exclude=*/%s\n' "$line" "$line"
  done < "$IGNORE"
}

if [ -z "${METHOD:-}" ]; then
  if ! command -v rsync >/dev/null 2>&1; then
    METHOD=tar; WHY="rsync is not installed here"
  elif ! rsh "$TARGET" 'command -v rsync >/dev/null 2>&1'; then
    METHOD=tar; WHY="rsync is not installed on the board"
  else
    METHOD=rsync; WHY=""
  fi
fi
[ -n "${WHY:-}" ] && {
  echo "note: ${WHY}, falling back to tar over ssh."
  echo "      For watch mode: sudo apt install rsync   (on the board)"
}

sync_once() {
  if [ "$METHOD" = "rsync" ]; then
    local args=(-a --delete-after --exclude-from="$IGNORE"
                -e "ssh -o ControlMaster=auto -o ControlPath=${CTL} -o ControlPersist=600")
    [ "$DRY" = 1 ] && args+=(--dry-run -v)
    [ "$WATCH" = 1 ] && args+=(--itemize-changes) || args+=(-v --human-readable)
    rsync "${args[@]}" "${HERE}/" "${TARGET}:${REMOTE_DIR}/"
  else
    mapfile -t EX < <(tar_excludes)
    tar -czf - -C "$HERE" "${EX[@]}" . \
      | rsh "$TARGET" "mkdir -p '${REMOTE_DIR}' && tar -xzpf - -C '${REMOTE_DIR}'"
  fi
}

service_is_active() {
  [ "$(rsh "$TARGET" "systemctl is-active ${SERVICE}.service 2>/dev/null" \
       2>/dev/null | tr -d '\r')" = "active" ]
}

restart_service() {
  # -t so sudo can prompt if there is no NOPASSWD rule yet; without it sudo
  # refuses with "a terminal is required".
  if ssh "${SSH_OPTS[@]}" -t "$TARGET" \
       "sudo -n systemctl restart ${SERVICE}.service" 2>/dev/null; then
    echo "    restarted ${SERVICE}"
  elif ssh "${SSH_OPTS[@]}" -t "$TARGET" \
       "sudo systemctl restart ${SERVICE}.service"; then
    echo "    restarted ${SERVICE} (asked for sudo)"
  else
    echo "!!  could not restart ${SERVICE} -- not installed yet, or sudo refused."
    echo "!!  THE BOARD IS STILL RUNNING THE OLD CODE."
    echo "    Run '$0 $TARGET --setup-ssh' for the NOPASSWD rule."
    return 1
  fi

  # Say whether it came back. A unit that dies on start sits in
  # "activating (auto-restart)" and looks like it is merely slow.
  local st=""
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    st=$(rsh "$TARGET" "systemctl is-active ${SERVICE}.service 2>/dev/null" \
         2>/dev/null | tr -d '\r')
    [ "$st" = "active" ] && break
    [ "$st" = "failed" ] && break
    sleep 1
  done
  if [ "$st" = "active" ]; then
    echo "    ${SERVICE} is active"
  else
    echo "!!  ${SERVICE} did not come back (state: ${st:-unknown})"
    rsh "$TARGET" "journalctl -u ${SERVICE}.service --no-pager -n 12" \
      2>/dev/null | sed 's/^/      /'
    return 1
  fi
}

# The failure this keeps catching: files land on the board, the running
# process keeps serving what it loaded at startup, and the change looks
# deployed because the deploy succeeded. Python is imported once and Flask
# caches templates outside debug mode, so nothing picks itself up.
warn_if_stale() {
  [ "$RESTART" = 1 ] && return 0
  service_is_active || return 0
  echo
  echo "!!  ${SERVICE} is running and was NOT restarted."
  echo "!!  The files are on the board; the running process still has the old"
  echo "!!  code and templates. Re-run with --restart, or:"
  echo "        ssh -t $TARGET 'sudo systemctl restart ${SERVICE}'"
}

echo "==> ${HERE}  ->  ${TARGET}:${REMOTE_DIR}   (via ${METHOD})"
sync_once

if [ "$DRY" = 1 ]; then echo "==> dry run, nothing changed"; exit 0; fi

rsh "$TARGET" "chmod +x '${REMOTE_DIR}'/*.py '${REMOTE_DIR}'/*.sh 2>/dev/null || true"
if [ "$RESTART" = 1 ]; then restart_service || true; else warn_if_stale; fi

if [ "$WATCH" = 0 ]; then
  cat <<EOF

Next, on the board:
  cd ${REMOTE_DIR}
  cp config.local.example.yaml config.local.yaml   # this board's camera address
  cp secrets.example.yaml secrets.yaml && chmod 600 secrets.yaml
  ./install.sh
  ./doctor.py
EOF
  exit 0
fi

# ------------------------------------------------------------------- watch
if [ "$METHOD" != "rsync" ]; then
  echo
  echo "--watch needs rsync: tar would re-send all 17 MB on every save."
  echo "  On the board:  sudo apt install rsync"
  exit 2
fi

if command -v inotifywait >/dev/null 2>&1; then
  echo "==> watching (inotify). Ctrl-C to stop."
  WAIT_CMD=(inotifywait -qq -r -e modify,create,delete,move
            --exclude '(\.git|__pycache__|\.tmp$|recordings|detections|events|shadow|link|state|hls)'
            "$HERE")
else
  echo "==> watching (polling every ${POLL_S}s). Ctrl-C to stop."
  echo "    'sudo apt install inotify-tools' here makes it instant."
  WAIT_CMD=(sleep "$POLL_S")
fi

trap 'echo; echo "==> stopped watching"; cleanup; exit 0' INT TERM
while true; do
  "${WAIT_CMD[@]}" || true
  OUT="$(sync_once)" || continue
  if [ -n "$OUT" ]; then
    echo "$(date +%H:%M:%S)"
    echo "$OUT" | sed 's/^/    /'
    rsh "$TARGET" "chmod +x '${REMOTE_DIR}'/*.py '${REMOTE_DIR}'/*.sh 2>/dev/null || true"
    [ "$RESTART" = 1 ] && restart_service
  fi
done
