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

"""An annotated clip alongside each event clip.

The event clip is the camera's own bytes, unaltered -- that is what you hand
to a police officer or an insurer, and re-encoding it to draw boxes on would
lose quality, obscure detail, and bake a wrong detection in permanently.

So the boxes go in a second file next to it:

    clip_23-47-02_23-48-30.mp4            the evidence, untouched
    clip_23-47-02_23-48-30.annotated.mp4  what the model saw, 2 fps

Frames are buffered in memory only while an incident is open, which is why
this costs almost nothing: an eighty-second incident at two frames a second is
a few megabytes, and between incidents it holds nothing at all.
"""

import logging
import subprocess
import threading

log = logging.getLogger("annotated")


class AnnotatedClip:
    def __init__(self, fps=2.0, max_frames=1200, quality=6):
        self.fps = float(fps)
        # A cap so a stuck incident cannot eat the board's memory. 1200 frames
        # at 2 fps is ten minutes, which is the controller's own hold ceiling.
        self.max_frames = int(max_frames)
        self.quality = int(quality)
        self._frames = []
        self._lock = threading.Lock()
        self._open = False
        self.dropped = 0
        # Wall time of the first buffered frame. This clip begins when the
        # incident did, whereas the full-resolution clip begins at a whole
        # minute boundary -- so the two files have different t0 and a player
        # cannot use one offset for both.
        self.first_at = None

    # ------------------------------------------------------------ lifecycle
    def start(self):
        with self._lock:
            self._frames = []
            self._open = True
            self.dropped = 0
            self.first_at = None

    def add(self, jpeg, when=None):
        """Buffer one annotated frame. Cheap: it is already encoded."""
        if not jpeg:
            return
        with self._lock:
            if not self._open:
                return
            if self.first_at is None:
                import datetime as _dt
                self.first_at = when or _dt.datetime.now()
            if len(self._frames) >= self.max_frames:
                self.dropped += 1
                return
            self._frames.append(jpeg)

    def discard(self):
        with self._lock:
            self._frames = []
            self._open = False

    @property
    def count(self):
        with self._lock:
            return len(self._frames)

    # --------------------------------------------------------------- output
    def write(self, path):
        """Write the buffered frames as an mp4. Returns the path, or None."""
        with self._lock:
            frames, self._frames, self._open = self._frames, [], False
            dropped = self.dropped
        if not frames:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "image2pipe", "-framerate", f"{self.fps:g}", "-i", "-",
            # yuv420p and an even size, or half the players in the world will
            # refuse it -- including whatever is on an old Android tablet.
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-movflags", "+faststart",
            str(path),
        ]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
        except FileNotFoundError:
            log.error("ffmpeg not found; no annotated clip written")
            return None
        try:
            for f in frames:
                proc.stdin.write(f)
            proc.stdin.close()
            rc = proc.wait(timeout=120)
        except (BrokenPipeError, subprocess.TimeoutExpired, OSError) as e:
            log.warning("annotated clip failed: %s", e)
            try:
                proc.kill()
            except Exception:
                pass
            return None
        if rc != 0:
            err = (proc.stderr.read() or b"").decode("utf-8", "replace")[-200:]
            log.warning("annotated clip failed (rc=%s): %s", rc, err.strip())
            path.unlink(missing_ok=True)
            return None
        log.info("annotated clip: %s (%d frames%s)", path.name, len(frames),
                 f", {dropped} dropped at the cap" if dropped else "")
        return path
