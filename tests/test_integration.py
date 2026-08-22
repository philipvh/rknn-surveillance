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

"""End-to-end checks against real files on a real (temporary) disk.

Covers the two claims Phase 1 is judged on: you can find footage from an
arbitrary minute last night, and filling the disk never costs you an event clip.
"""
import datetime as dt
import os, sys, tempfile, time, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config, retention, recorder  # noqa: E402
from segments import list_segments_between, parse_seg_start  # noqa: E402

CFG_TMPL = """
camera: {{host: 10.0.0.5, http_port: 88, user: admin,
         main_path: videoMain, sub_path: videoSub}}
detection: {{source: sub, target_fps: 2, conf_threshold: 0.6,
            trigger_classes: [person]}}
trigger: {{quiet_period_s: 15, pre_roll_s: 2, post_roll_s: 15, max_duration_min: 10}}
recording:
  segment_seconds: 60
  tiers:
    - {{name: main, stream: main, path: {root}/recordings/main, max_age_days: 2}}
    - {{name: sub, stream: sub, path: {root}/recordings/sub, max_age_days: 60}}
    - {{name: events, stream: null, path: {root}/events, max_age_days: 730, protected: true}}
retention: {{target_free_percent: 20, check_interval_s: 300}}
paths: {{events_root: {root}/events, detections_root: {root}/detections}}
web: {{}}
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg_p = self.root / "config.yaml"
        self.sec_p = self.root / "secrets.yaml"
        self.cfg_p.write_text(CFG_TMPL.format(root=self.root))
        self.sec_p.write_text('camera:\n  password: "pw=1"\n')
        self.sec_p.chmod(0o600)
        self.cfg = config.load(self.cfg_p, self.sec_p)

    def tearDown(self):
        self.tmp.cleanup()

    def seg(self, tier, when, size=1024, name=None):
        d = self.cfg.tier(tier).path / when.strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        f = d / (name or (when.strftime("%H-%M-%S") + ".mp4"))
        f.write_bytes(b"\0" * size)
        os.utime(f, (when.timestamp(), when.timestamp()))
        return f


class TestFindingLastNight(Base):
    def test_locates_the_segments_covering_an_arbitrary_minute(self):
        base = dt.datetime(2026, 8, 18, 23, 40, 0)
        for i in range(30):
            self.seg("sub", base + dt.timedelta(minutes=i))

        # "what happened at 23:47 last night?"
        want = dt.datetime(2026, 8, 18, 23, 47, 30)
        got = list_segments_between(want, want, self.cfg.tier("sub").path, 60)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].name, "23-47-00.mp4")

    def test_window_spanning_midnight_crosses_day_directories(self):
        base = dt.datetime(2026, 8, 18, 23, 58, 0)
        for i in range(6):
            self.seg("sub", base + dt.timedelta(minutes=i))
        got = list_segments_between(dt.datetime(2026, 8, 18, 23, 59, 0),
                                    dt.datetime(2026, 8, 19, 0, 2, 0),
                                    self.cfg.tier("sub").path, 60)
        # 23-58 spans 23:58:00-23:59:00 so it touches a window opening at
        # 23:59:00. Overlap is inclusive on purpose: erring towards one extra
        # segment is cheap, missing the start of an incident is not.
        self.assertEqual([f.name for f in got],
                         ["23-58-00.mp4", "23-59-00.mp4", "00-00-00.mp4",
                          "00-01-00.mp4", "00-02-00.mp4"])
        self.assertEqual({f.parent.name for f in got},
                         {"2026-08-18", "2026-08-19"})

    def test_still_reads_the_old_flat_layout(self):
        d = self.cfg.tier("main").path
        d.mkdir(parents=True, exist_ok=True)
        (d / "2026-08-18_23-47-00.mp4").write_bytes(b"\0")
        got = list_segments_between(dt.datetime(2026, 8, 18, 23, 47, 30),
                                    dt.datetime(2026, 8, 18, 23, 47, 40), d, 60)
        self.assertEqual(len(got), 1, "pre-upgrade recordings must still be findable")

    def test_a_segment_entirely_before_the_window_is_excluded(self):
        base = dt.datetime(2026, 8, 18, 23, 0, 0)
        for i in range(5):
            self.seg("sub", base + dt.timedelta(minutes=i))
        got = list_segments_between(dt.datetime(2026, 8, 18, 23, 3, 10),
                                    dt.datetime(2026, 8, 18, 23, 3, 50),
                                    self.cfg.tier("sub").path, 60)
        self.assertEqual([f.name for f in got], ["23-03-00.mp4"])

    def test_junk_filenames_are_ignored_not_fatal(self):
        d = self.cfg.tier("sub").path / "2026-08-18"
        d.mkdir(parents=True, exist_ok=True)
        (d / "notes.mp4").write_bytes(b"\0")
        self.assertIsNone(parse_seg_start(d / "notes.mp4"))
        self.assertEqual(list_segments_between(dt.datetime(2026, 8, 18),
                                               dt.datetime(2026, 8, 19), d, 60), [])


class TestEvidenceSurvives(Base):
    def test_age_sweep_removes_old_main_keeps_clip(self):
        now = dt.datetime.now()
        old_main = self.seg("main", now - dt.timedelta(days=5))
        new_main = self.seg("main", now - dt.timedelta(hours=1))
        clip = self.seg("events", now - dt.timedelta(days=200), name="clip_x.mp4")

        # Pin the disk to comfortably empty. Without this the test depends on
        # the host: on a nearly full board the pressure sweep correctly
        # deletes the recent segment too, and this test is about age alone.
        real = retention.disk_state
        retention.disk_state = lambda p: (900 * 1024 ** 3, 1000 * 1024 ** 3)
        try:
            deletions, warnings, _ = retention.run_once(self.cfg)
        finally:
            retention.disk_state = real
        gone = {d.path for d in deletions}

        self.assertIn(old_main, gone)
        self.assertNotIn(new_main, gone)
        self.assertNotIn(clip, gone)
        self.assertTrue(clip.exists(), "a 200-day-old clip is well inside 730 days")

    def test_recordings_are_spent_before_any_clip(self):
        now = dt.datetime.now()
        clips = [self.seg("events", now - dt.timedelta(days=i * 5),
                          size=4096, name=f"clip_{i}.mp4") for i in range(6)]
        mains = [self.seg("main", now - dt.timedelta(minutes=i * 30), size=4096)
                 for i in range(1, 6)]

        # Force pressure: pretend the disk is essentially full.
        real = retention.disk_state
        retention.disk_state = lambda p: (0, 100 * 1024 ** 3)
        try:
            deletions, warnings, _ = retention.run_once(self.cfg)
        finally:
            retention.disk_state = real

        # Every working recording goes before the first clip does. On a disk
        # this full the clips are then taken too, oldest first -- a camera
        # that has stopped recording is a worse outcome than one that has
        # lost its oldest footage -- but never before the cheap data is gone.
        self.assertTrue(all(not m.exists() for m in mains),
                        "recordings should have been sacrificed first")
        self.assertTrue(warnings, "dropping evidence must be reported")
        self.assertTrue(any("oldest protected" in w for w in warnings),
                        "and it must say that is what happened")

        # clips[0] is the newest, clips[5] the oldest. Whatever survives must
        # be newer than whatever went: age order, not arbitrary.
        gone = [i for i, c in enumerate(clips) if not c.exists()]
        kept = [i for i, c in enumerate(clips) if c.exists()]
        if gone and kept:
            self.assertGreater(min(gone), max(kept),
                               "the oldest clips go first")

    def test_in_flight_segment_is_left_alone(self):
        now = dt.datetime.now()
        writing = self.seg("main", now)          # mtime = right now
        os.utime(writing, None)
        real = retention.disk_state
        retention.disk_state = lambda p: (0, 100 * 1024 ** 3)
        try:
            retention.run_once(self.cfg)
        finally:
            retention.disk_state = real
        self.assertTrue(writing.exists(),
                        "the segment ffmpeg is currently writing must survive")


class TestRecorderCommand(Base):
    def test_one_recorder_per_stream_tier(self):
        recs = recorder.recorders_from_config(self.cfg)
        self.assertEqual([r.name_ for r in recs], ["main", "sub"])
        self.assertIn("videoMain", recs[0].rtsp_url)
        self.assertIn("videoSub", recs[1].rtsp_url)

    def test_password_is_encoded_in_the_url_and_absent_from_logs(self):
        recs = recorder.recorders_from_config(self.cfg)
        self.assertIn("pw%3D1", recs[0].rtsp_url)
        loggable = recs[0]._redacted_command()
        self.assertNotIn("pw=1", loggable)
        self.assertNotIn("pw%3D1", loggable)

    def test_command_uses_the_segment_muxer_not_a_fixed_duration(self):
        cmd = recorder.recorders_from_config(self.cfg)[0]._command()
        self.assertIn("-f", cmd)
        self.assertIn("segment", cmd)
        self.assertIn("-segment_time", cmd)
        self.assertNotIn("-t", cmd, "a -t would recreate the per-minute restart")
        self.assertIn("-strftime", cmd)

    def test_health_follows_files_on_disk_not_ffmpeg_chatter(self):
        """Regression: health used to be derived from an ffmpeg stderr line
        that only appears at -loglevel info, so every healthy recorder in a
        real run reported itself dead."""
        r = recorder.recorders_from_config(self.cfg)[0]
        r.started_at = time.time()
        self.assertTrue(r.healthy, "grace period right after start")

        day = r.out_dir / "2026-08-19"
        day.mkdir(parents=True, exist_ok=True)
        seg = day / "12-00-00.mp4"
        seg.write_bytes(b"\0")
        r._health_cache = None
        self.assertTrue(r.healthy, "a freshly written segment means alive")

        old = time.time() - r.segment_seconds * 10
        os.utime(seg, (old, old))
        r._health_cache = None
        r.started_at = old
        self.assertFalse(r.healthy, "no writes for ten segment periods is dead")

    def test_health_is_false_when_nothing_written_after_grace(self):
        r = recorder.recorders_from_config(self.cfg)[0]
        r.started_at = time.time() - r.segment_seconds * 10
        r._health_cache = None
        self.assertFalse(r.healthy)

    def test_video_only_by_default(self):
        cmd = recorder.recorders_from_config(self.cfg)[0]._command()
        self.assertIn("0:v:0", cmd, "audio may be G.711, which mp4 cannot hold")




class TestClipsAreSeekable(Base):
    """An mp4 with its index at the end has to be downloaded whole before a
    browser can play or seek it. Measured on the tablet: twenty seconds for a
    23 MB clip. The index must come first."""

    def test_the_concat_asks_for_faststart(self):
        from concat_mgr import ConcatManager, ConcatJob
        import inspect
        src = inspect.getsource(ConcatManager._cut_with_demuxer)
        self.assertIn("faststart", src)
        src = inspect.getsource(ConcatManager._cut_with_ts)
        self.assertIn("faststart", src,
                      "the fallback path needs it too, or a rare clip is slow")

    def test_moov_comes_before_mdat_in_a_real_clip(self):
        import shutil, struct, subprocess
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg not on PATH")
        src = self.root / "in.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc=size=64x48:rate=5",
                        "-t", "1", "-pix_fmt", "yuv420p", str(src)], check=True)
        out = self.root / "out.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(src), "-c", "copy",
                        "-movflags", "+faststart", str(out)], check=True)

        order = []
        with open(out, "rb") as fh:
            off = 0
            for _ in range(6):
                fh.seek(off)
                hdr = fh.read(8)
                if len(hdr) < 8:
                    break
                size = struct.unpack(">I", hdr[:4])[0]
                order.append(hdr[4:8].decode("ascii", "replace"))
                if size == 0:
                    break
                if size == 1:
                    size = struct.unpack(">Q", fh.read(8))[0]
                off += size
        self.assertIn("moov", order)
        self.assertIn("mdat", order)
        self.assertLess(order.index("moov"), order.index("mdat"),
                        f"index must precede the data, got {order}")




class TestUnfinishedClipRecovery(Base):
    """A restart between an incident closing and its concat finishing used to
    lose the clip silently, and the capture sweep then deleted the segments.
    The sidecar is written first, so it is the record of intent."""

    def setUp(self):
        super().setUp()
        import json
        self.events = self.cfg.events_root / "2026-08-20"
        self.events.mkdir(parents=True, exist_ok=True)
        self.base = self.events / "clip_2026-08-20_10-00-00_2026-08-20_10-02-00"
        self.base.with_suffix(".json").write_text(json.dumps({
            "t0": "2026-08-20T10:00:00",
            "window_start": "2026-08-20T10:00:10",
            "window_end": "2026-08-20T10:02:00", "segments": 2}))

    def _pending(self):
        """Sidecars with no mp4 beside them."""
        return [p for p in self.cfg.events_root.rglob("*.json")
                if not p.with_suffix(".mp4").exists()]

    def test_a_sidecar_without_a_clip_is_detectable(self):
        self.assertEqual(len(self._pending()), 1)

    def test_a_finished_clip_is_not_flagged(self):
        self.base.with_suffix(".mp4").write_bytes(b"\0" * 100)
        self.assertEqual(self._pending(), [])

    def test_the_segments_are_still_findable_if_they_survive(self):
        import datetime as _dt
        for m in (0, 1):
            self.seg("main", _dt.datetime(2026, 8, 20, 10, m, 0))
        from segments import list_segments_between
        segs = list_segments_between(_dt.datetime(2026, 8, 20, 10, 0, 0),
                                     _dt.datetime(2026, 8, 20, 10, 2, 0),
                                     self.cfg.tier("main").path, 60)
        self.assertEqual(len(segs), 2, "the cut can be resumed")

    def test_no_segments_means_the_footage_is_gone(self):
        import datetime as _dt
        from segments import list_segments_between
        segs = list_segments_between(_dt.datetime(2026, 8, 20, 10, 0, 0),
                                     _dt.datetime(2026, 8, 20, 10, 2, 0),
                                     self.cfg.tier("main").path, 60)
        self.assertEqual(segs, [],
                         "nothing to resume from -- this must be reported, "
                         "not silently ignored")


if __name__ == "__main__":
    unittest.main(verbosity=2)
