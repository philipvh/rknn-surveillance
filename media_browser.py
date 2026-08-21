#!/usr/bin/env python3
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

"""Standalone media browser -- recordings only, no camera control.

The wall panel normally runs inside the surveillance process so it shares that
process's PTZ object, watchdog and motor budget. This entry point exists for
the case where you want to browse footage without the main service
running at all; PTZ routes return 503.
"""

import logging
import sys

import config
from webapp import create_app


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
                        datefmt="%H:%M:%S")
    try:
        cfg = config.load(require_password=False)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    web = cfg.web or {}
    app = create_app(cfg)
    app.run(host=web.get("bind", "0.0.0.0"),
            port=int(web.get("port", 8080)), threaded=True, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
