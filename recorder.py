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

"""Continuous segment recording.

The previous version spawned a fresh ffmpeg for every 60-second clip, so the
camera was disconnected and reconnected once a minute and each boundary lost a
second or two of footage. This runs one long-lived ffmpeg per stream and lets
ffmpeg's segment muxer cut the files, so the connection is only re-established
when something actually goes wrong.

One recorder per tier that names a stream, so the low-resolution sub stream can
be kept for months while the full-resolution main stream is kept for days.
"""

from pathlib import Path
import logging
import re
import shlex
import subprocess
import threading
import time

log = logging.getLogger("recorder")

# Day-subdirectory layout. NOTE: the segment muxer expands strftime in the
# filename but will NOT create the directories -- strftime_mkdir is an option
# of the *hls* muxer, not this one, and is absent even in ffmpeg 8. Verified
# against a real camera; without _ensure_day_dirs() below, recording stops at
# midnight when the new day's directory does not exist.
SEGMENT_PATTERN = "%Y-%m-%d/%H-%M-%S.mp4"

BACKOFF_START_S = 2.0
BACKOFF_MAX_S = 30.0

_caps_cache = None


def ffmpeg_caps():
    """Probe this ffmpeg for the two options whose names/availability vary.

    'stimeout' was renamed to 'timeout' for the RTSP demuxer in ffmpeg 5, and
    passing the wrong one makes ffmpeg exit immediately instead of recording.
    'strftime_mkdir' is probed too, but do not count on it: it belongs to the
    hls muxer and the segment muxer has never had it. _ensure_day_dirs() is
    what actually keeps recording alive across midnight.
    """
    global _caps_cache
    if _caps_cache is not None:
        return _caps_cache
    caps = {"rtsp_timeout_opt": None, "strftime_mkdir": False}
    try:
        rtsp = subprocess.run(["ffmpeg", "-hide_banner", "-h", "demuxer=rtsp"],
                              capture_output=True, text=True, timeout=10).stdout
        if "-stimeout" in rtsp:
            caps["rtsp_timeout_opt"] = "stimeout"
        elif "-timeout" in rtsp:
            caps["rtsp_timeout_opt"] = "timeout"

        seg = subprocess.run(["ffmpeg", "-hide_banner", "-h", "muxer=segment"],
                             capture_output=True, text=True, timeout=10).stdout
        caps["strftime_mkdir"] = "strftime_mkdir" in seg
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as e:
        log.warning("could not probe ffmpeg capabilities (%s); "
                    "falling back to the most portable options", e)
    _caps_cache = caps
    log.info("ffmpeg capabilities: %s", caps)
    return caps


class StreamRecorder(threading.Thread):
    def __init__(self, name, rtsp_url, out_dir, segment_seconds,
                 redacted_url=None, record_audio=False, tee_outputs=None):
        super().__init__(daemon=True, name=f"recorder-{name}")
        self.name_ = name
        self.rtsp_url = rtsp_url
        self.redacted_url = redacted_url or "rtsp://<redacted>"
        self.out_dir = Path(out_dir)
        self.segment_seconds = int(segment_seconds)
        self.record_audio = record_audio
        self.tee_outputs = list(tee_outputs or [])

        self.stop_event = threading.Event()
        self._proc = None
        self._lock = threading.Lock()

        # health, for the watchdog in Phase 8
        self.started_at = None
        self.restarts = 0
        self.last_segment_at = None
        self._health_cache = None

    # ------------------------------------------------------------------ api
    def stop(self):
        self.stop_event.set()
        with self._lock:
            p = self._proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    @property
    def healthy(self):
        """True if footage is actually landing on disk.

        Deliberately measured from the filesystem, not from ffmpeg's output:
        the "Opening ... for writing" line only appears at -loglevel info, so
        parsing stderr for it reported every healthy recorder as dead. While a
        segment is being written its mtime keeps advancing, which makes file
        freshness an exact liveness signal.
        """
        newest = self._newest_write()
        limit = self.segment_seconds * 3
        if newest is None:
            # Nothing yet -- allow one grace period after start before alarming.
            started = self.started_at or time.time()
            return (time.time() - started) < limit
        return (time.time() - newest) < limit

    def _newest_write(self, cache_s=5.0):
        now = time.time()
        if self._health_cache and (now - self._health_cache[0]) < cache_s:
            return self._health_cache[1]
        newest = None
        try:
            # Only the two most recent day directories can hold a live segment.
            days = sorted((d for d in self.out_dir.iterdir() if d.is_dir()),
                          reverse=True)[:2]
            for d in days:
                for f in d.iterdir():
                    if f.is_file():
                        m = f.stat().st_mtime
                        if newest is None or m > newest:
                            newest = m
        except (OSError, FileNotFoundError):
            pass
        self._health_cache = (now, newest)
        return newest

    # -------------------------------------------------------------- internals
    def _command(self):
        out = self.out_dir / SEGMENT_PATTERN
        caps = ffmpeg_caps()
        cmd = [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning",
            "-rtsp_transport", "tcp",
        ]
        # Without this a dead socket can hang ffmpeg forever, and a supervisor
        # that only restarts on exit would never notice.
        if caps["rtsp_timeout_opt"]:
            cmd += [f"-{caps['rtsp_timeout_opt']}", "10000000"]   # microseconds
        cmd += ["-i", self.rtsp_url]
        # Video only by default: the SD2X may serve G.711 audio, which mp4 will
        # not accept, and an unattended recorder must not die over that. Turn
        # recording.record_audio on once ffprobe (phase0_survey.sh) shows the
        # audio codec is something mp4 can hold.
        cmd += ["-map", "0" if self.record_audio else "0:v:0", "-c", "copy"]
        cmd += [
            "-f", "segment",
            "-segment_time", str(self.segment_seconds),
            "-segment_format", "mp4",
            "-reset_timestamps", "1",
            "-strftime", "1",
        ]
        if caps["strftime_mkdir"]:
            cmd += ["-strftime_mkdir", "1"]
        if self.tee_outputs:
            # Feed a local consumer (e.g. the detector) from this same pull so
            # the camera only serves one session for this stream.
            cmd += ["-f", "tee", "|".join([str(out)] + self.tee_outputs)]
        else:
            cmd += [str(out)]
        return cmd

    def _redacted_command(self):
        return " ".join(shlex.quote(a.replace(self.rtsp_url, self.redacted_url))
                        for a in self._command())

    def _pump_stderr(self, proc):
        """ffmpeg's complaints used to go to DEVNULL, which made every failure
        look identical. Surface them."""
        seg_re = re.compile(r"Opening '(?P<f>[^']+)' for writing")
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            m = seg_re.search(line)
            if m:
                self.last_segment_at = time.time()
                log.debug("[%s] segment %s", self.name_, m.group("f"))
            else:
                log.warning("[%s] ffmpeg: %s", self.name_, line)

    def _ensure_day_dirs(self):
        """Create today's and tomorrow's directories.

        The segment muxer does not create them, so without this ffmpeg fails
        the moment the date rolls over -- and a recorder that dies at midnight
        is a recorder that misses the entire night.
        """
        now = time.time()
        for offset in (0, 86400):
            d = self.out_dir / time.strftime("%Y-%m-%d", time.localtime(now + offset))
            d.mkdir(parents=True, exist_ok=True)

    def _day_dir_ticker(self):
        while not self.stop_event.wait(300):
            try:
                self._ensure_day_dirs()
            except OSError as e:
                log.warning("[%s] could not create day directory: %s", self.name_, e)

    def run(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._day_dir_ticker, daemon=True,
                         name=f"days-{self.name_}").start()
        backoff = BACKOFF_START_S
        self.started_at = time.time()

        while not self.stop_event.is_set():
            self._ensure_day_dirs()

            log.info("[%s] starting: %s", self.name_, self._redacted_command())
            try:
                proc = subprocess.Popen(
                    self._command(), stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE)
            except FileNotFoundError:
                log.error("[%s] ffmpeg not found -- apt install ffmpeg", self.name_)
                return

            with self._lock:
                self._proc = proc
            started = time.time()

            pump = threading.Thread(target=self._pump_stderr, args=(proc,),
                                    daemon=True, name=f"stderr-{self.name_}")
            pump.start()
            rc = proc.wait()
            pump.join(timeout=2)

            with self._lock:
                self._proc = None

            if self.stop_event.is_set():
                log.info("[%s] stopped", self.name_)
                return

            ran_for = time.time() - started
            self.restarts += 1
            # A run that lasted a while was probably a transient network fault;
            # start the backoff over. Instant failures back off hard.
            if ran_for > 60:
                backoff = BACKOFF_START_S
            log.warning("[%s] ffmpeg exited rc=%s after %.0fs (restart #%d), "
                        "retrying in %.0fs", self.name_, rc, ran_for,
                        self.restarts, backoff)
            self.stop_event.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)


def recorders_from_config(cfg):
    """One StreamRecorder per recording tier that names a camera stream."""
    out = []
    for tier in cfg.recording_tiers:
        out.append(StreamRecorder(
            name=tier.name,
            rtsp_url=cfg.rtsp_url(tier.stream),
            redacted_url=cfg.rtsp_url(tier.stream, redacted=True),
            out_dir=tier.path,
            segment_seconds=cfg.segment_seconds,
            record_audio=bool(cfg._get("recording", "record_audio", default=False)),
        ))
    return out
