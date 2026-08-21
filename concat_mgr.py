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

"""Cutting event clips out of recorded segments.

Uses ffmpeg's concat *demuxer* on the mp4 segments directly. The previous
version remuxed every input to MPEG-TS with a bitstream filter and joined them
with the concat *protocol*; measured against real recordings from the bench
camera, both produce identical frame counts, but the demuxer preserves
duration slightly more accurately, writes a smaller file, and needs no temp
directory, no codec probing and no bitstream filters.

The TS path is kept as a fallback for the case the demuxer cannot handle:
segments whose codec parameters differ, which is what a camera reboot or a
stream reconfiguration in the middle of a trigger window looks like.

Note on timestamps: concatenating segments recorded with -reset_timestamps
produces "non monotonically increasing dts" complaints from the muxer, because
the camera declares no frame rate and timestamps come straight from RTP.
Verified on real files: every frame still decodes, and duration and frame
count match the inputs exactly. It is noise, not damage.
"""

import logging
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("concat")

# How long to wait for ffmpeg to finish writing the last segment in a window.
IN_FLIGHT_QUIET_S = 5.0
IN_FLIGHT_TIMEOUT_S = 180.0


class ConcatJob:
    def __init__(self, files, out_path, delete_sources=False):
        self.files = [Path(f) for f in files]
        self.out_path = Path(out_path)
        # Only after the clip is written and validated. A source deleted on a
        # failed concat is footage that no longer exists anywhere.
        self.delete_sources = bool(delete_sources)

    @property
    def key(self):
        return "|".join(str(f.resolve()) for f in self.files)


def _probe(path, entries):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", entries, "-of", "default=nk=1:nw=1", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    except (subprocess.SubprocessError, OSError):
        return []


def duration_of(path):
    vals = _probe(path, "format=duration")
    try:
        return float(vals[0])
    except (IndexError, ValueError):
        return 0.0


class ConcatManager:
    def __init__(self, recorders=()):
        self.recorders = list(recorders)
        self.q = queue.Queue()
        self.in_progress = set()
        self.done = set()
        self._lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._worker, daemon=True,
                         name="concat").start()
        log.info("clip worker started")

    def submit(self, job):
        if job.out_path.exists():
            log.info("skip, output already exists: %s", job.out_path.name)
            return
        key = job.key
        with self._lock:
            if key in self.done or key in self.in_progress:
                log.info("skip, already handled: %s", job.out_path.name)
                return
            self.in_progress.add(key)
        log.info("queued %s from %d segment(s)",
                 job.out_path.name, len(job.files))
        self.q.put(job)

    def drain(self, timeout=90.0):
        """Wait for queued and running cuts to finish. True if the queue emptied.

        Called at shutdown: a job only lives in memory, so exiting while one is
        queued loses it until the next start recovers it from its sidecar.
        """
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            with self._lock:
                busy = len(self.in_progress)
            if busy == 0 and self.q.empty():
                return True
            time.sleep(0.25)
        with self._lock:
            left = len(self.in_progress)
        log.warning("drain timed out with %d cut(s) unfinished", left)
        return False

    # ------------------------------------------------------------- internals
    def _wait_until_quiet(self, job):
        """Wait for the last segment to stop growing.

        ffmpeg owns a segment until it rolls over to the next one, so
        "not modified for a few seconds" is the signal that it is complete.
        """
        deadline = time.time() + IN_FLIGHT_TIMEOUT_S
        while time.time() < deadline:
            busy = []
            for f in job.files:
                try:
                    if (time.time() - f.stat().st_mtime) < IN_FLIGHT_QUIET_S:
                        busy.append(f.name)
                except FileNotFoundError:
                    pass
            if not busy:
                return True
            time.sleep(0.5)
        log.warning("timed out waiting for %s to settle; cutting anyway", busy)
        return False

    @staticmethod
    def _write_list(files, path):
        with open(path, "w") as fh:
            for f in files:
                # the concat demuxer's own escaping: ' -> '\''
                p = str(Path(f).resolve()).replace("'", "'\\''")
                fh.write(f"file '{p}'\n")

    def _cut_with_demuxer(self, job):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
            list_path = Path(tf.name)
        try:
            self._write_list(job.files, list_path)
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "concat", "-safe", "0", "-i", str(list_path),
                   "-c", "copy",
                   # Move the index to the front. Without this the mp4 keeps
                   # its moov atom at the end, and a browser has to fetch the
                   # whole file -- tens of megabytes over wifi -- before it can
                   # play or seek at all. Costs one rewrite pass at cut time
                   # and saves twenty seconds every time anyone opens it.
                   "-movflags", "+faststart",
                   str(job.out_path)]
            subprocess.run(cmd, check=True, capture_output=True)
        finally:
            list_path.unlink(missing_ok=True)

    def _cut_with_ts(self, job):
        """Fallback: normalise through MPEG-TS first.

        Slower and needs a temp directory, but tolerates inputs the concat
        demuxer refuses -- chiefly segments whose codec parameters differ.
        """
        tmpdir = Path(tempfile.mkdtemp(prefix="concat_ts_"))
        try:
            ts_files = []
            for i, f in enumerate(job.files):
                codec = (_probe(f, "stream=codec_name") or [""])[0]
                bsf = {"h264": "h264_mp4toannexb",
                       "hevc": "hevc_mp4toannexb"}.get(codec)
                dst = tmpdir / f"{i:03d}.ts"
                cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                       "-i", str(f), "-map", "0", "-c", "copy"]
                if bsf:
                    cmd += ["-bsf:v", bsf]
                cmd += [str(dst)]
                subprocess.run(cmd, check=True, capture_output=True)
                ts_files.append(dst)
            joined = "concat:" + "|".join(str(p) for p in ts_files)
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-i", joined, "-c", "copy",
                            "-movflags", "+faststart", str(job.out_path)],
                           check=True, capture_output=True)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    def _validate(job, expected_s):
        """A clip that exists but is empty or truncated is worse than none:
        it looks like evidence and is not."""
        if not job.out_path.exists():
            return False, "no output file"
        size = job.out_path.stat().st_size
        if size < 1024:
            return False, f"output is only {size} bytes"
        got = duration_of(job.out_path)
        if got <= 0:
            return False, "output has no readable duration"
        if expected_s > 0 and got < expected_s * 0.5:
            return False, (f"output is {got:.1f}s but the inputs total "
                           f"{expected_s:.1f}s")
        return True, f"{got:.1f}s, {size/1e6:.1f} MB"

    def _worker(self):
        while True:
            job = self.q.get()
            key = job.key
            try:
                self._wait_until_quiet(job)
                expected = sum(duration_of(f) for f in job.files)
                job.out_path.parent.mkdir(parents=True, exist_ok=True)

                ok = False
                try:
                    self._cut_with_demuxer(job)
                    ok, detail = self._validate(job, expected)
                    if not ok:
                        log.warning("concat demuxer produced a bad clip (%s); "
                                    "falling back to the TS path", detail)
                except subprocess.CalledProcessError as e:
                    err = (e.stderr or b"").decode("utf-8", "replace")[-300:]
                    log.warning("concat demuxer failed (%s); falling back to "
                                "the TS path", err.strip())

                if not ok:
                    job.out_path.unlink(missing_ok=True)
                    try:
                        self._cut_with_ts(job)
                        ok, detail = self._validate(job, expected)
                    except subprocess.CalledProcessError as e:
                        err = (e.stderr or b"").decode("utf-8", "replace")[-300:]
                        ok, detail = False, err.strip()

                if ok:
                    with self._lock:
                        self.done.add(key)
                    log.info("clip written: %s (%s)", job.out_path.name, detail)
                    if job.delete_sources:
                        freed = 0
                        for f in job.files:
                            try:
                                freed += f.stat().st_size
                                f.unlink()
                            except FileNotFoundError:
                                pass
                            except OSError as e:
                                log.warning("could not remove %s: %s", f.name, e)
                        log.info("removed %d source segment(s), %.0f MB",
                                 len(job.files), freed / 1e6)
                else:
                    job.out_path.unlink(missing_ok=True)
                    log.error("could not cut %s: %s", job.out_path.name, detail)
            except Exception:
                log.exception("clip job failed: %s", job.out_path)
            finally:
                with self._lock:
                    self.in_progress.discard(key)
                self.q.task_done()
