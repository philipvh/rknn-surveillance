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

# Remove what is left of the try-out installs. Run once, as root:
#
#   sudo bash finish_cleanup.sh
#
# Everything here is either a duplicate or a demo tree that nothing
# references. The model zoo was migrated to ditan and verified byte-identical
# first, and the try-out source is archived there too.
set -uo pipefail

echo "==> Free before: $(df -h / | awk 'NR==2{print $4}')"

# Both units point at /home/radxa/rknn_yolov10 and belong to the try-out.
# media-browser held port 8080; tvw_surveillance can move onto it afterwards.
for svc in media-browser surveillance; do
  unit="/etc/systemd/system/${svc}.service"
  if [ -f "$unit" ]; then
    echo "==> Removing ${svc}.service (try-out)"
    systemctl disable --now "${svc}.service" 2>/dev/null || true
    rm -f "$unit"
  fi
done
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

for p in /home/radxa/rknn_yolov10 \
         /home/radxa/rknn_yolov5 \
         /home/radxa/rknn_yolov5_demo.tar.gz; do
  if [ -e "$p" ]; then
    echo "==> Removing $p ($(du -sh "$p" 2>/dev/null | cut -f1))"
    rm -rf "$p"
  fi
done

# Deliberately left alone: /home/radxa/rknn_key_0016.pem is the RKNN licence
# key, not part of any demo tree.

echo "==> Free after:  $(df -h / | awk 'NR==2{print $4}')"
echo
echo "Port 8080 is free now. Next, as radxa:"
echo "    cd ~/tvw_surveillance && ./run_test.sh stop && ./install.sh"
echo "which installs tvw-surveillance under systemd (enabled at boot) and"
echo "vacuums the journal."
