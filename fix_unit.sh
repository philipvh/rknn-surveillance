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

# Repair the installed unit in place. Run as root:
#
#   sudo bash fix_unit.sh
#
# The project lives under /home, and ProtectHome=yes makes /home empty inside
# the service's namespace -- systemd then cannot enter WorkingDirectory and the
# unit dies with status=200/CHDIR before Python ever runs. ReadWritePaths does
# not punch through ProtectHome; only tmpfs+BindPaths or ProtectHome=no do.
#
# Tries the strict option first and falls back to the permissive one, so this
# ends with a running service either way rather than another round trip.
set -uo pipefail

UNIT=/etc/systemd/system/tvw-surveillance.service
SERVICE=tvw-surveillance

[ -f "$UNIT" ] || { echo "!! $UNIT not found -- run ./install.sh first"; exit 1; }

cp -a "$UNIT" "${UNIT}.bak.$(date +%H%M%S)"

try_start() {
  systemctl daemon-reload
  systemctl restart "${SERVICE}.service" 2>/dev/null || true
  for _ in $(seq 1 20); do
    case "$(systemctl is-active ${SERVICE}.service 2>/dev/null)" in
      active) return 0 ;;
      failed) return 1 ;;
    esac
    sleep 1
  done
  return 1
}

echo "==> Attempt 1: ProtectHome=tmpfs + BindPaths (keeps /home hidden)"
python3 - "$UNIT" <<'PYEOF'
import re, sys
p = sys.argv[1]
s = open(p).read()
s = re.sub(r'^ProtectHome=.*$', 'ProtectHome=tmpfs', s, count=1, flags=re.M)
if 'BindPaths=' not in s:
    s = s.replace('ProtectHome=tmpfs',
                  'ProtectHome=tmpfs\nBindPaths=/home/radxa/tvw_surveillance', 1)
open(p, 'w').write(s)
PYEOF

if try_start; then
  echo "==> Service is active (strict hardening kept)."
else
  echo "==> Still failing. Attempt 2: ProtectHome=no"
  python3 - "$UNIT" <<'PYEOF'
import re, sys
p = sys.argv[1]
s = open(p).read()
s = re.sub(r'^ProtectHome=.*$', 'ProtectHome=no', s, count=1, flags=re.M)
s = re.sub(r'^BindPaths=.*\n', '', s, flags=re.M)
open(p, 'w').write(s)
PYEOF
  if try_start; then
    echo "==> Service is active (ProtectHome dropped -- /home visible to it)."
  else
    echo
    echo "!! Still not starting. This is no longer the CHDIR problem:"
    journalctl -u "${SERVICE}.service" --no-pager -n 20 | sed 's/^/    /'
    exit 1
  fi
fi

PORT=$(cd /home/radxa/tvw_surveillance && python3 -c \
  "import config; print(config.load(require_password=False)._get('web','port',default=8080))" \
  2>/dev/null || echo 8080)
echo
echo "ProtectHome is now: $(grep -m1 '^ProtectHome=' "$UNIT")"
echo "Panel (the board has more than one NIC -- use the one your tablet is on):"
for _a in $(hostname -I); do echo "  http://${_a}:${PORT}/"; done
