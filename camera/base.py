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

"""What a camera has to be able to do, and nothing about how.

A backend is the only place a vendor's protocol is allowed to live. Add a
camera by writing one subclass and registering it; no file outside this
package should need editing, and none should learn a vendor's command names.

    class MyCam(CameraBackend):
        name       = "mycam"
        HTTP_PORT  = 80
        RTSP_PORT  = 554
        MAIN_PATH  = "stream1"
        SUB_PATH   = "stream2"
        CAPABILITIES = {Cap.PRESETS, Cap.ZOOM, Cap.SNAPSHOT}

        def start_move(self, direction): ...
        def start_zoom(self, direction): ...
        def stop(self, kind=None, timeout=None): ...

THE ONE RULE, and the reason this class looks so thin:

    A backend starts and stops motion. It does not decide when.

Every deadline, the motor budget, the retry-until-confirmed stop, the rescue
stop at startup and the watchdog all live in ptz.PTZ, above this interface,
and they apply to every backend equally. A backend that took those decisions
on itself -- "I'll just move for 2 seconds and stop" -- would be outside the
watchdog, and a camera outside the watchdog is a camera that can be left
turning against its own end stop when a process dies. So: `start_move` starts
motion and returns immediately, and `stop` is expected to be called by
somebody else, possibly repeatedly, possibly from another thread.

`stop` carries the one hard obligation in this file: it must either stop the
camera or raise. Returning quietly when the camera did not hear you is the
single worst thing a backend can do, because the layer above treats a clean
return as proof and stops retrying.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from urllib.parse import urlencode

log = logging.getLogger("camera")


class CameraError(Exception):
    """Anything the camera refused, could not do, or answered unusably."""


class NotSupported(CameraError):
    """This camera cannot do that at all.

    Distinct from a failure: a failure might work next time, this never will.
    Callers use it to hide a control rather than to show an error.
    """


class Cap:
    """Capability flags, so callers can ask instead of assuming.

    Cameras differ in ways that matter to the interface. The Foscam SD2X, for
    instance, reports no absolute pan/tilt position at all, so anything that
    wanted to draw a compass has to know not to try.
    """
    PRESETS = "presets"
    ZOOM = "zoom"
    SNAPSHOT = "snapshot"
    CLOCK = "clock"                  # the board can set the camera's time
    SPEED = "speed"
    DIAGONAL = "diagonal"            # topleft/bottomright and friends
    ABSOLUTE_POSITION = "absolute_position"
    PRESET_OVERWRITE = "preset_overwrite"   # saving an existing name re-aims it


# --------------------------------------------------------------- transport

class UrllibTransport:
    """Stdlib only -- one less package to install on the board.

    Injectable so a backend can be tested with no camera on the network; the
    tests pass a fake with the same one-method shape.
    """

    def get(self, url, params, timeout):
        full = f"{url}?{urlencode(params)}"
        req = urllib.request.Request(full, headers={"User-Agent": "rknn-ptz/1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()


# ----------------------------------------------------------------- backend

class CameraBackend:
    """One camera's protocol. Subclass, register, done.

    Everything optional raises NotSupported by default, so a minimal backend
    is three methods and a camera with no presets simply says so rather than
    failing in a way callers have to guess about.
    """

    #: short key used in config as `camera.type`
    name = "generic"

    #: Defaults for a camera of this make, so config only carries what differs.
    #: RTSP_PORT = None means "the same port as HTTP", which is how Foscam
    #: serves it; most other cameras want 554.
    HTTP_PORT = 80
    RTSP_PORT = 554
    MAIN_PATH = None
    SUB_PATH = None

    #: Which movement words this camera understands.
    DIRECTIONS = frozenset({"up", "down", "left", "right"})
    ZOOMS = frozenset({"in", "out"})

    CAPABILITIES = frozenset()

    def __init__(self, cfg, transport=None, timeout=5.0):
        self.cfg = cfg
        self.transport = transport or UrllibTransport()
        self.timeout = float(timeout)

    # ------------------------------------------------------------- queries
    @classmethod
    def supports(cls, cap):
        return cap in cls.CAPABILITIES

    def describe(self):
        """One line for the logs and the doctor, so an install says what it
        thinks it is talking to."""
        return f"{self.name} at {self.cfg.camera_host}:{self.cfg.camera_port}"

    # ------------------------------------------------------------- required
    def start_move(self, direction):
        """Begin continuous motion and return at once.

        Must not block for the duration of the move, and must not stop by
        itself -- the deadline above this class does that.
        """
        raise NotImplementedError

    def stop(self, kind=None, timeout=None):
        """Stop all motion. Raise if the camera did not confirm.

        `kind` is 'ptz', 'zoom' or None (stop everything). Some cameras need a
        different command per kind; stopping more than was started is fine and
        is the safer error.
        """
        raise NotImplementedError

    # ------------------------------------------------------------- optional
    def start_zoom(self, direction):
        raise NotSupported("this camera has no zoom")

    def list_presets(self):
        raise NotSupported("this camera has no presets")

    def goto_preset(self, name):
        raise NotSupported("this camera has no presets")

    def save_preset(self, name):
        """Save the current view under `name`, replacing any existing one.

        Overwriting is part of the contract, not an optimisation: a panel that
        cannot re-aim a saved position is a panel whose buttons lie. If the
        camera's own 'add' refuses to overwrite, delete first -- see the Foscam
        backend, where exactly that cost an afternoon.
        """
        raise NotSupported("this camera has no presets")

    def delete_preset(self, name):
        raise NotSupported("this camera has no presets")

    def set_speed(self, level):
        raise NotSupported("this camera has no speed control")

    def set_clock(self, utc, offset_seconds):
        """Set the camera's clock from a UTC instant and a local offset.

        Offsets are passed rather than a local time so the board's own DST
        handling is the only one that has to be right.
        """
        raise NotSupported("this camera's clock cannot be set")

    def snapshot(self):
        """Raw JPEG bytes of the current view."""
        raise NotSupported("this camera has no snapshot endpoint")

    def device_info(self):
        return {}

    def device_state(self):
        return {}
