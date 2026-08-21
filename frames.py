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

"""The detector's latest annotated frame, for anyone who wants to look.

Boxes used to exist only on the JPEGs written to detections/, which answers
"what did it see last night?" but not "is it seeing me right now?" -- and the
second question is the one you ask while aiming a camera or choosing a
threshold. This holds the most recent frame the NPU looked at, annotated, so
the wall panel can show it.

Deliberately a single frame and not a queue: a slow viewer should see the
newest frame, not a backlog of stale ones.
"""

import threading
import time

import cv2


class LatestFrame:
    def __init__(self, quality=70):
        self.quality = int(quality)
        self._jpeg = None
        self._at = 0.0
        self._seq = 0
        self._lock = threading.Lock()
        self._new = threading.Condition(self._lock)

    def publish(self, bgr, overlay=None):
        """Encode and store. Called from the detection loop."""
        if bgr is None:
            return
        try:
            if overlay:
                bgr = bgr.copy()
                # Bottom-left: cameras put their own timestamp at the top and
                # two overlays on top of each other are unreadable.
                y = bgr.shape[0] - 12 - 22 * (len(overlay) - 1)
                for line in overlay:
                    # drawn twice so it stays readable over a bright scene
                    cv2.putText(bgr, line, (8, y), cv2.FONT_HERSHEY_SIMPLEX,
                                0.6, (0, 0, 0), 3, cv2.LINE_AA)
                    cv2.putText(bgr, line, (8, y), cv2.FONT_HERSHEY_SIMPLEX,
                                0.6, (255, 255, 255), 1, cv2.LINE_AA)
                    y += 22
            ok, buf = cv2.imencode(".jpg", bgr,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
            if not ok:
                return
        except Exception:
            return
        with self._new:
            self._jpeg = buf.tobytes()
            self._at = time.time()
            self._seq += 1
            self._new.notify_all()

    def get(self):
        with self._lock:
            return self._jpeg, self._at, self._seq

    def wait_for_new(self, last_seq, timeout=5.0):
        """Block until a frame newer than last_seq arrives, or time out."""
        with self._new:
            if self._seq != last_seq and self._jpeg is not None:
                return self._jpeg, self._seq
            self._new.wait(timeout)
            if self._jpeg is not None and self._seq != last_seq:
                return self._jpeg, self._seq
        return None, last_seq

    @property
    def age(self):
        with self._lock:
            return None if self._at == 0 else time.time() - self._at
