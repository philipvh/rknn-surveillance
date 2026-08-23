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

"""Foscam, over the CGIProxy interface. Developed against an SD2X.

Everything Foscam-shaped lives here: the CGI endpoint, the command names, the
XML answers, and three quirks that are not in any manual and each cost real
time to find. They are written down at the point of use so the next person
gets them for free.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import xml.etree.ElementTree as ET

from .base import Cap, CameraBackend, CameraError

log = logging.getLogger("camera.foscam")

# Continuous movement: each of these runs until something stops it.
DIRECTIONS = {
    "up": "ptzMoveUp", "down": "ptzMoveDown",
    "left": "ptzMoveLeft", "right": "ptzMoveRight",
    "topleft": "ptzMoveTopLeft", "topright": "ptzMoveTopRight",
    "bottomleft": "ptzMoveBottomLeft", "bottomright": "ptzMoveBottomRight",
}
ZOOMS = {"in": "zoomIn", "out": "zoomOut"}

STOP_CMD = "ptzStopRun"
ZOOM_STOP_CMD = "zoomStop"


def foscam_time_params(utc, offset_seconds):
    """Build setSystemTime arguments for a UTC instant and a local offset.

    Two quirks, both confined to this function:

    * the camera treats the value it is given as UTC and adds its timeZone for
      display, so local time must not be sent;
    * Foscam's timeZone has the opposite sign to everyone else's -- GMT+1 is
      -3600, not +3600.

    isDst is deliberately 0. This firmware ignores its own DST flag entirely
    (setting it changes nothing), so summer time has to be carried in the
    offset instead. That is also why the board re-sends this hourly rather
    than setting it once: come October the offset changes and the camera will
    not notice on its own.
    """
    return {
        "timeSource": "1",              # manual: the board is the time source
        "timeZone": str(-int(offset_seconds)),
        "isDst": "0",
        "year": str(utc.year), "mon": str(utc.month), "day": str(utc.day),
        "hour": str(utc.hour), "minute": str(utc.minute), "sec": str(utc.second),
    }


class FoscamBackend(CameraBackend):
    name = "foscam"

    HTTP_PORT = 88                  # Foscam ships on 88, not 80
    # Foscam serves RTSP on its HTTP port unless a separate one is configured
    # (SD2X manual, section 3.4). None means "same as HTTP", which is what
    # tripped up the first stream URL: 554 is simply refused on this camera.
    RTSP_PORT = None
    MAIN_PATH = "videoMain"
    SUB_PATH = "videoSub"

    DIRECTIONS = frozenset(DIRECTIONS)
    ZOOMS = frozenset(ZOOMS)

    CAPABILITIES = frozenset({
        Cap.PRESETS, Cap.ZOOM, Cap.SNAPSHOT, Cap.CLOCK, Cap.SPEED,
        Cap.DIAGONAL,
        # Deliberately absent: ABSOLUTE_POSITION. getPTZAbsolutePos,
        # getPTZCurrentPos and getZoomAbsolutePos all answer result=-3 on this
        # firmware, so there is no way to ask where the camera is pointing.
        # Anything wanting to show a posture readout must not assume it.
        #
        # Also absent: PRESET_OVERWRITE -- see save_preset().
    })

    # ------------------------------------------------------------ plumbing
    @property
    def url(self):
        return (f"http://{self.cfg.camera_host}:{self.cfg.camera_port}"
                f"/cgi-bin/CGIProxy.fcgi")

    def params(self, cmd, **extra):
        p = {"cmd": cmd, "usr": self.cfg.camera_user,
             "pwd": self.cfg.camera_password}
        p.update(extra)
        return p

    def call(self, cmd, timeout=None, **extra):
        try:
            body = self.transport.get(self.url, self.params(cmd, **extra),
                                      timeout or self.timeout)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise CameraError(f"{cmd}: {e}") from e
        return self.parse(cmd, body)

    @staticmethod
    def parse(cmd, body):
        """XML in, dict out, with result=0 meaning success.

        A non-zero result is raised rather than returned: a camera that
        answered "no" has not done the thing, and every caller here would
        otherwise have to remember to check.
        """
        if isinstance(body, bytes):
            try:
                text = body.decode("utf-8", "replace")
            except Exception:
                text = ""
        else:
            text = body or ""
        if not text.strip():
            return {"result": None, "_raw": text}
        out = {}
        try:
            root = ET.fromstring(text)
            for child in root:
                out[child.tag] = (child.text or "").strip()
        except ET.ParseError:
            # Foscam is not always strict about its XML; fall back to scraping.
            for m in re.finditer(r"<(\w+)>([^<]*)</\1>", text):
                out[m.group(1)] = m.group(2).strip()
            if not out:
                raise CameraError(f"{cmd}: unparseable response {text[:120]!r}")
        if "result" in out:
            try:
                res = int(out["result"])
            except ValueError:
                res = None
            out["result"] = res
            if res not in (0, None):
                raise CameraError(f"{cmd}: camera returned result={res}")
        out["_raw"] = text
        return out

    # ------------------------------------------------------------- required
    def start_move(self, direction):
        cmd = DIRECTIONS.get(direction)
        if cmd is None:
            raise CameraError(f"unknown direction {direction!r}; "
                              f"expected one of {sorted(DIRECTIONS)}")
        self.call(cmd)

    def start_zoom(self, direction):
        cmd = ZOOMS.get(direction)
        if cmd is None:
            raise CameraError(f"unknown zoom {direction!r}; expected in or out")
        self.call(cmd)

    def stop(self, kind=None, timeout=None):
        # A zoom needs its own stop first, then the general one. Sending both
        # costs one extra request and covers the case where the caller's idea
        # of what is moving has drifted from the camera's.
        cmds = [STOP_CMD] if kind != "zoom" else [ZOOM_STOP_CMD, STOP_CMD]
        for c in cmds:
            self.call(c, timeout=timeout)

    # -------------------------------------------------------------- presets
    def list_presets(self):
        out = self.call("getPTZPresetPointList")
        names = []
        for k, v in out.items():
            if re.fullmatch(r"point\d+", k) and v:
                names.append((int(k[5:]), v))
        return [v for _, v in sorted(names)]

    def goto_preset(self, name):
        self.call("ptzGotoPresetPoint", name=name)

    def delete_preset(self, name):
        return self.call("ptzDeletePresetPoint", name=name)

    def save_preset(self, name):
        # ptzAddPresetPoint will NOT overwrite an existing name. It returns
        # result=0 either way and silently keeps the position the name was
        # FIRST saved at, so re-aiming a preset appears to work and changes
        # nothing -- which is exactly how a panel "Set Home" button came to be
        # a no-op, and why the camera kept returning to a stale position.
        # Deleting first is what makes the interface's overwrite contract true.
        try:
            self.delete_preset(name)
        except CameraError:
            pass                    # not there yet, which is fine
        return self.call("ptzAddPresetPoint", name=name)

    # ----------------------------------------------------------------- misc
    def set_speed(self, level):
        return self.call("setPTZSpeed", speed=int(level))

    def set_clock(self, utc, offset_seconds):
        return self.call("setSystemTime",
                         **foscam_time_params(utc, offset_seconds))

    def snapshot(self):
        try:
            return self.transport.get(self.url, self.params("snapPicture2"),
                                      self.timeout)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise CameraError(f"snapPicture2: {e}") from e

    def device_info(self):
        return self.call("getDevInfo")

    def device_state(self):
        return self.call("getDevState")
