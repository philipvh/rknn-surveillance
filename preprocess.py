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

"""Getting a camera frame into the model without distorting it.

The model wants 640x640. A 16:9 frame resized straight to that is squashed:
a standing person becomes short and wide, which is exactly the shape the model
is least expecting. The rknn_model_zoo demo this code came from letterboxed
instead -- padding to square rather than stretching -- but that lived in the
__main__ block that was stripped out when the module was recovered.

Measured on the bench camera's 16:9 frames: squashing found 2 people where
letterboxing found 3. Modest, but free.

Letterboxing also changes how detections map back, which is the part that is
easy to get wrong: boxes come back in 640x640 space and must have the padding
removed before being divided by the scale, not simply multiplied by w/640.
"""

import cv2
import numpy as np

MODEL_SIZE = 640


class Letterbox:
    """Resize preserving aspect ratio, pad the rest, and map boxes back."""

    def __init__(self, size=MODEL_SIZE, colour=(0, 0, 0)):
        self.size = int(size)
        self.colour = colour
        self.scale = 1.0
        self.pad_x = 0
        self.pad_y = 0

    def apply(self, rgb):
        h, w = rgb.shape[:2]
        self.scale = min(self.size / h, self.size / w)
        nh, nw = int(round(h * self.scale)), int(round(w * self.scale))
        resized = cv2.resize(rgb, (nw, nh))
        out = np.full((self.size, self.size, 3), self.colour, dtype=rgb.dtype)
        self.pad_y = (self.size - nh) // 2
        self.pad_x = (self.size - nw) // 2
        out[self.pad_y:self.pad_y + nh, self.pad_x:self.pad_x + nw] = resized
        return out

    def to_frame(self, box, w, h):
        """A model-space box back to frame pixels, clamped to the frame."""
        x1 = (box[0] - self.pad_x) / self.scale
        y1 = (box[1] - self.pad_y) / self.scale
        x2 = (box[2] - self.pad_x) / self.scale
        y2 = (box[3] - self.pad_y) / self.scale
        return (int(max(0, min(x1, w - 1))), int(max(0, min(y1, h - 1))),
                int(max(0, min(x2, w - 1))), int(max(0, min(y2, h - 1))))


class Squash:
    """The previous behaviour, kept so it can be compared on site."""

    def __init__(self, size=MODEL_SIZE):
        self.size = int(size)

    def apply(self, rgb):
        return cv2.resize(rgb, (self.size, self.size))

    def to_frame(self, box, w, h):
        sx, sy = w / float(self.size), h / float(self.size)
        return (int(max(0, min(box[0] * sx, w - 1))),
                int(max(0, min(box[1] * sy, h - 1))),
                int(max(0, min(box[2] * sx, w - 1))),
                int(max(0, min(box[3] * sy, h - 1))))


def from_config(cfg):
    det = (cfg._get("detection", default={}) or {}) if hasattr(cfg, "_get") else (cfg or {})
    return Letterbox() if det.get("letterbox", True) else Squash()
