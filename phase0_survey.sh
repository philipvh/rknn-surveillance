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

# phase0_survey.sh -- Phase 0 survey for the TVW camera rebuild.
#
# Probes the Foscam SD2X, the Rock 5B's GPIO and clock, the disk, and the
# site's internet, and writes a report. Read-only by default: it will NOT
# move the camera unless you pass --move.
#
#   ./phase0_survey.sh --host 192.168.1.50 --user admin
#   ./phase0_survey.sh --host 192.168.1.50 --user admin --move
#
# The password is read from $FOSCAM_PW, or prompted for. It is never written
# to the report.

set -uo pipefail

HOST=""; USER_NAME="admin"; HTTP_PORT=""; DO_MOVE=0
REPORT="phase0_report_$(date +%Y-%m-%d_%H-%M-%S).txt"

usage() { sed -n '2,13p' "$0" | sed 's/^# \?//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --host)  HOST="$2"; shift 2;;
    --user)  USER_NAME="$2"; shift 2;;
    --port)  HTTP_PORT="$2"; shift 2;;
    --move)  DO_MOVE=1; shift;;
    -h|--help) usage;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done

[ -z "$HOST" ] && { echo "error: --host is required (the camera's IP)" >&2; exit 2; }
if [ -z "${FOSCAM_PW:-}" ]; then
  read -rsp "Camera password for ${USER_NAME}: " FOSCAM_PW; echo
fi

exec > >(tee "$REPORT") 2>&1

hr()  { printf '\n== %s %s\n' "$1" "$(printf '=%.0s' $(seq 1 $((66-${#1}))))"; }
note() { printf '   %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

echo "TVW camera rebuild -- Phase 0 survey"
echo "run at : $(date -Is)"
echo "on     : $(uname -srm) / $(hostname)"
echo "camera : ${HOST} (user ${USER_NAME})"
[ "$DO_MOVE" = 1 ] && echo "MODE   : --move given, PTZ movement WILL be tested"

# ---------------------------------------------------------------- reachability
hr "1. Camera reachability"
if ping -c2 -W2 "$HOST" >/dev/null 2>&1; then note "ping OK"; else note "ping FAILED (may just be firewalled)"; fi

note "scanning likely ports..."
for p in 80 88 443 554 888 8000; do
  if have nc && nc -z -w2 "$HOST" "$p" 2>/dev/null; then
    case $p in
      80|88)   note "  port $p  open  <- HTTP / CGI / RTSP candidate";;
      443)     note "  port $p  open  <- HTTPS";;
      554)     note "  port $p  open  <- dedicated RTSP port";;
      888)     note "  port $p  open  <- ONVIF (manual says default 888)";;
      *)       note "  port $p  open";;
    esac
    [ -z "$HTTP_PORT" ] && [ "$p" = 88 ] && HTTP_PORT=88
    [ -z "$HTTP_PORT" ] && [ "$p" = 80 ] && HTTP_PORT=80
  fi
done
HTTP_PORT="${HTTP_PORT:-88}"
note "using HTTP port ${HTTP_PORT} for CGI and RTSP"

CGI="http://${HOST}:${HTTP_PORT}/cgi-bin/CGIProxy.fcgi"

# ---------------------------------------------------------------------- streams
hr "2. RTSP streams"
if have ffprobe; then
  for path in videoMain videoSub; do
    note "--- /${path}"
    out=$(ffprobe -v error -rtsp_transport tcp \
            -show_entries stream=codec_name,width,height,avg_frame_rate,bit_rate \
            -of default=nw=1 -timeout 8000000 \
            "rtsp://${USER_NAME}:${FOSCAM_PW}@${HOST}:${HTTP_PORT}/${path}" 2>&1)
    if [ -n "$out" ] && ! echo "$out" | grep -qi "error\|Invalid\|refused\|timed out"; then
      echo "$out" | sed 's/^/       /'
    else
      note "    FAILED: $(echo "$out" | head -2 | tr '\n' ' ')"
    fi
  done
else
  note "ffprobe not installed -- apt install ffmpeg"
fi

# ------------------------------------------------------------------- CGI probe
hr "3. Foscam CGI commands (read-only)"
note "result 0 = OK. Non-zero means the command was rejected or is unsupported;"
note "read the code against Foscam's CGI guide before concluding anything."
probe() {
  local cmd="$1" extra="${2:-}"
  local body code
  body=$(curl -s -m 8 --get "$CGI" \
          --data-urlencode "cmd=${cmd}" \
          --data-urlencode "usr=${USER_NAME}" \
          --data-urlencode "pwd=${FOSCAM_PW}" \
          ${extra:+--data-urlencode "$extra"} 2>/dev/null)
  if [ -z "$body" ]; then
    printf '   %-24s no response\n' "$cmd"; return
  fi
  code=$(echo "$body" | grep -o '<result>[-0-9]*</result>' | head -1 | tr -dc '0-9-')
  printf '   %-24s result=%-4s %s\n' "$cmd" "${code:-?}" \
    "$(echo "$body" | tr -d '\n\r' | sed 's/<[^>]*>/ /g' | tr -s ' ' | cut -c1-90)"
}
for c in getDevInfo getDevState getPTZPresetPointList getPTZSpeed \
         getPTZCruiseMapList getMotionDetectConfig getImageSetting getPortInfo; do
  probe "$c"
done

note ""
note "--- snapshot"
snap=$(mktemp); curl -s -m 10 --get "$CGI" --data-urlencode "cmd=snapPicture2" \
  --data-urlencode "usr=${USER_NAME}" --data-urlencode "pwd=${FOSCAM_PW}" -o "$snap" 2>/dev/null
if have file && file -b "$snap" | grep -qi jpeg; then
  note "    snapPicture2 OK -- $(file -b "$snap"), $(stat -c%s "$snap") bytes"
  cp "$snap" ./phase0_snapshot.jpg && note "    saved as phase0_snapshot.jpg"
else
  note "    snapPicture2 did not return a JPEG ($(head -c 80 "$snap" | tr -d '\0'))"
fi
rm -f "$snap"

# ------------------------------------------------------------------- PTZ moves
hr "4. PTZ movement"
if [ "$DO_MOVE" = 1 ]; then
  note "moving left for 1s, then stopping. WATCH THE CAMERA."
  probe ptzMoveLeft
  sleep 1
  probe ptzStopRun
  note "if the camera did not stop, send this NOW:"
  note "  curl '${CGI}?cmd=ptzStopRun&usr=${USER_NAME}&pwd=***'"
else
  note "skipped -- pass --move to test. It will physically turn the camera."
fi

# ----------------------------------------------------------------------- GPIO
hr "5. GPIO (for the PIR contact)"
if have gpiodetect; then
  gpiodetect 2>&1 | sed 's/^/   /'
  note ""
  note "to find the line for your header pin and watch the PIR fire:"
  note "  gpioinfo | less"
  note "  gpiomon --num-events=10 gpiochipN LINE"
else
  note "libgpiod tools not installed -- apt install gpiod"
fi

# ---------------------------------------------------------------------- clock
hr "6. Clock and RTC"
note "system time : $(date -Is)"
if have timedatectl; then timedatectl 2>&1 | sed 's/^/   /'; fi
if [ -e /dev/rtc0 ]; then
  note "RTC device  : /dev/rtc0 present"
  have hwclock && note "RTC reads   : $(sudo -n hwclock -r 2>/dev/null || echo '(needs sudo)')"
else
  note "RTC device  : NONE -- with no NTP the clock will be wrong after a power cut"
fi

# ----------------------------------------------------------- storage + internet
hr "7. Storage"
df -h . | sed 's/^/   /'
if lsblk -o NAME,SIZE,TYPE,MOUNTPOINT 2>/dev/null | grep -qi nvme; then
  lsblk -o NAME,SIZE,TYPE,MOUNTPOINT 2>/dev/null | grep -i "nvme\|^NAME" | sed 's/^/   /'
else
  note "no NVMe found -- continuous recording to an SD card will wear it out"
fi

hr "8. Internet at the site"
if curl -s -m 6 -o /dev/null -w '%{http_code}' https://one.one.one.one 2>/dev/null | grep -q '^[23]'; then
  note "WAN reachable -- an uplink already exists here. This makes Phase 7 nearly free."
else
  note "no WAN from this machine (expected -- LAN only)."
  note "ASK ANYWAY: does the clubhouse have internet for the bar or a card terminal?"
fi

# ------------------------------------------------------------------ python deps
hr "9. Python dependencies"
for m in numpy cv2 torch flask; do
  v=$(python3 -c "import $m; print(getattr($m,'__version__','?'))" 2>/dev/null) \
    && note "$(printf '%-8s %s' "$m" "OK $v")" \
    || note "$(printf '%-8s %s' "$m" "MISSING")"
done
python3 -c "from rknn.api import RKNN" 2>/dev/null \
  && note "$(printf '%-8s %s' rknn OK)" \
  || note "$(printf '%-8s %s' rknn 'MISSING -- install the RKNN toolkit')"
note ""
note "NB: install.sh does not install torch, but yolov10.py needs it"
note "    (dfl() and post_process_yolov10() use torch.softmax/topk/gather)."

hr "Done"
echo "   Report written to: ${REPORT}"
echo "   Fill these into config.yaml in Phase 1:"
echo "     camera host, HTTP port, main/sub stream paths, preset names,"
echo "     gpiochip + line for the PIR."
