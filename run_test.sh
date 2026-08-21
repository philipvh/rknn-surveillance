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

# Start/stop the service by hand for bench testing, with a pid file.
#
#   ./run_test.sh start [args...]     e.g. ./run_test.sh start --no-record
#   ./run_test.sh stop
#   ./run_test.sh status
#
# Uses a pid file rather than pkill -f, because a pattern like
# "surveillance_main" also matches the ssh command that invoked it, which
# kills the launching shell before the service ever starts. Systemd is the
# right answer in production; this is for a laptop and an ssh session.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="${HERE}/.run_test.pid"
LOG="${LOG:-/tmp/surv.log}"

running() {
  [ -f "$PIDFILE" ] || return 1
  local pid; pid="$(cat "$PIDFILE" 2>/dev/null)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

case "${1:-status}" in
  start)
    shift
    if running; then echo "already running as $(cat "$PIDFILE")"; exit 0; fi
    cd "$HERE"
    setsid python3 surveillance_main.py "$@" </dev/null >"$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 1
    running && echo "started $(cat "$PIDFILE"), log $LOG" || {
      echo "failed to start; log tail:"; tail -5 "$LOG"; exit 1; }
    ;;
  stop)
    if ! running; then echo "not running"; rm -f "$PIDFILE"; exit 0; fi
    pid="$(cat "$PIDFILE")"
    kill "$pid" 2>/dev/null
    for _ in $(seq 1 20); do running || break; sleep 0.5; done
    running && kill -9 "$pid" 2>/dev/null
    rm -f "$PIDFILE"
    echo "stopped"
    ;;
  status)
    if running; then
      echo "running as $(cat "$PIDFILE")"
    else
      echo "not running"
    fi
    ;;
  *) echo "usage: $0 {start|stop|status}"; exit 2;;
esac
