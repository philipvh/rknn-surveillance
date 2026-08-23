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

"""Tests for the wall panel.

The claim that matters: no sequence of presses can leave a motor running, and
nothing the panel does bypasses the motor budget or the deadline watchdog.
"""
import base64
import time
import signal
import os
import json, datetime as dt, os, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config, ptz as ptz_mod  # noqa: E402
from schedule import Schedule  # noqa: E402
from webapp import create_app  # noqa: E402

CFG = """
camera: {{host: 10.0.0.5, http_port: 88, user: admin,
         main_path: videoMain, sub_path: videoSub}}
detection: {{source: sub, trigger_classes: [person]}}
trigger: {{pre_roll_s: 2, post_roll_s: 15}}
recording:
  segment_seconds: 60
  tiers:
    - {{name: main, stream: main, path: {root}/rec/main, max_age_days: 2}}
    - {{name: events, stream: null, path: {root}/events, max_age_days: 730, protected: true}}
retention: {{target_free_percent: 20}}
paths: {{events_root: {root}/events, detections_root: {root}/det}}
ptz:
  home_preset: Home
  scan_presets: [Home, Court1, Gate]
  move_deadline_s: 0.3
  watchdog_interval_s: 0.02
  max_continuous_move_s: 2.0
  budget: {{auto_seconds_per_hour: 10, manual_seconds_per_hour: 5}}
  http: {{timeout_s: 1, stop_timeout_s: 0.3, stop_retries: 2}}
web: {{auth_required: true, auth_user: tvw, keepalive_ms: 250,
      stream_mode: snapshot, stream_fps: 2}}
"""

OK = b"<CGI_Result><result>0</result></CGI_Result>"


class FakeCamera:
    def __init__(self):
        self.calls = []

    def get(self, url, params, timeout):
        cmd = params.get("cmd")
        self.calls.append(cmd)
        if cmd == "snapPicture2":
            return b"\xff\xd8\xff\xe0fake-jpeg-body"
        return OK

    def count(self, c):
        return self.calls.count(c)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "config.yaml").write_text(CFG.format(root=root))
        sec = root / "secrets.yaml"
        sec.write_text('camera:\n  password: "pw"\nweb:\n  password: "wpw"\n')
        sec.chmod(0o600)
        self.cfg = config.load(root / "config.yaml", sec)
        for p in ("rec/main", "events", "det"):
            (root / p).mkdir(parents=True, exist_ok=True)
        (root / "events" / "clip_test.mp4").write_bytes(b"\0" * 10)

        self.cam = FakeCamera()
        self.ptz = ptz_mod.PTZ(self.cfg, transport=self.cam,
                               install_signal_handlers=False)
        self.sched = Schedule.from_config({"armed": [
            {"days": "all", "from": "22:00", "to": "08:00"}]})
        self.app = create_app(self.cfg, ptz=self.ptz, controller=None,
                              schedule=self.sched)
        self.app.config["TESTING"] = True
        self.c = self.app.test_client()
        self.cam.calls.clear()

    def tearDown(self):
        self.ptz._closed = True
        self.tmp.cleanup()

    def auth(self, user="tvw", pw="wpw"):
        tok = base64.b64encode(f"{user}:{pw}".encode()).decode()
        return {"Authorization": "Basic " + tok}


class TestAuth(Base):
    def test_panel_requires_a_password(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 401)
        self.assertIn("WWW-Authenticate", r.headers)

    def test_wrong_password_is_refused(self):
        self.assertEqual(self.c.get("/", headers=self.auth(pw="nope")).status_code, 401)

    def test_correct_password_gets_in(self):
        self.assertEqual(self.c.get("/", headers=self.auth()).status_code, 200)

    def test_ptz_routes_are_protected_too(self):
        r = self.c.post("/api/ptz/move", data={"dir": "left"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.cam.count("ptzMoveLeft"), 0,
                         "an unauthenticated request must never reach the camera")

    def test_healthz_is_open_for_monitoring(self):
        self.assertEqual(self.c.get("/healthz").status_code, 200)


class TestMotorSafety(Base):
    def test_move_then_stop(self):
        self.c.post("/api/ptz/move", data={"dir": "left"}, headers=self.auth())
        self.assertTrue(self.ptz.moving)
        self.c.post("/api/ptz/stop", headers=self.auth())
        self.assertFalse(self.ptz.moving)
        self.assertGreaterEqual(self.cam.count("ptzStopRun"), 1)

    def test_a_press_with_no_release_is_stopped_by_the_watchdog(self):
        """The tablet sleeping mid-press is the case this exists for."""
        import time
        self.c.post("/api/ptz/move", data={"dir": "right"}, headers=self.auth())
        self.assertTrue(self.ptz.moving)
        time.sleep(0.6)                     # deadline is 0.3s, no keepalive
        self.assertFalse(self.ptz.moving)
        self.assertGreaterEqual(self.ptz.watchdog_stops, 1)

    def test_keepalives_extend_without_re_sending(self):
        for _ in range(5):
            self.c.post("/api/ptz/move", data={"dir": "left"}, headers=self.auth())
        self.assertEqual(self.cam.count("ptzMoveLeft"), 1,
                         "refreshes must extend the deadline, not re-command")
        self.c.post("/api/ptz/stop", headers=self.auth())

    def test_panel_shares_the_motor_budget(self):
        """No route may bypass the budget the controller obeys."""
        self.ptz.budget.record(5, "manual")          # manual ceiling is 5s
        r = self.c.post("/api/ptz/move", data={"dir": "left"}, headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["ok"])
        self.assertIn("budget", r.get_json()["error"])
        self.assertEqual(self.cam.count("ptzMoveLeft"), 0)

    def test_bad_direction_is_rejected_without_touching_the_camera(self):
        r = self.c.post("/api/ptz/move", data={"dir": "widdershins"},
                        headers=self.auth())
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(self.cam.calls, [])

    def test_stop_accepts_get_for_a_dying_page(self):
        self.c.post("/api/ptz/move", data={"dir": "up"}, headers=self.auth())
        self.assertEqual(self.c.get("/api/ptz/stop", headers=self.auth()).status_code, 200)
        self.assertFalse(self.ptz.moving)

    def test_errors_come_back_as_json_not_a_500(self):
        """The panel shows the reason; a 500 would just look broken."""
        self.ptz.budget.record(5, "manual")
        r = self.c.post("/api/ptz/zoom", data={"dir": "in"}, headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertIn("error", r.get_json())


class TestPresetsAndArming(Base):
    def test_preset_goes_to_the_camera(self):
        r = self.c.post("/api/ptz/preset", data={"name": "Court1"},
                        headers=self.auth())
        self.assertTrue(r.get_json()["ok"])
        self.assertIn("ptzGotoPresetPoint", self.cam.calls)

    def test_disarm_expires(self):
        night = dt.datetime(2026, 8, 19, 23, 0)
        self.assertTrue(self.sched.is_armed(night))
        self.c.post("/api/arm", data={"armed": "0", "minutes": "120"},
                    headers=self.auth())
        self.assertFalse(self.sched.is_armed())
        self.c.post("/api/arm/clear", headers=self.auth())
        self.assertTrue(self.sched.is_armed(night))

    def test_override_minutes_are_clamped(self):
        r = self.c.post("/api/arm", data={"armed": "0", "minutes": "999999"},
                        headers=self.auth())
        self.assertLessEqual(r.get_json()["minutes"], 24 * 60,
                             "a disarm must not be effectively permanent")


class TestLiveView(Base):
    def test_snapshot_returns_a_jpeg(self):
        r = self.c.get("/snapshot.jpg", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b"\xff\xd8"))

    def test_mjpeg_stream_is_multipart_with_frames(self):
        r = self.c.get("/stream.mjpg?fps=10", headers=self.auth())
        self.assertIn("multipart/x-mixed-replace", r.headers["Content-Type"])
        it = r.response
        chunk = next(iter(it))
        self.assertIn(b"--rknnframe", chunk)
        self.assertIn(b"image/jpeg", chunk)
        it.close()

    def test_aim_stream_is_fresh_snapshots_regardless_of_stream_mode(self):
        # The aiming feed polls the stills endpoint even when the main view is
        # in ffmpeg mode, so it is always the current frame with no RTSP
        # buffering -- that is the whole reason it exists.
        r = self.c.get("/aim.mjpg?fps=6", headers=self.auth())
        self.assertIn("multipart/x-mixed-replace", r.headers["Content-Type"])
        it = r.response
        chunk = next(iter(it))
        self.assertIn(b"--rknnframe", chunk)
        self.assertIn(b"image/jpeg", chunk)
        it.close()


class TestBrowsing(Base):
    def test_lists_event_clips(self):
        r = self.c.get("/browse/events/", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"clip_test.mp4", r.data)

    def test_directory_traversal_is_refused(self):
        r = self.c.get("/browse/events/../../../etc", headers=self.auth())
        self.assertIn(r.status_code, (403, 404))

    def test_unknown_root_is_404(self):
        self.assertEqual(self.c.get("/browse/passwords/",
                                    headers=self.auth()).status_code, 404)

    def test_status_reports_ptz_state(self):
        r = self.c.get("/api/status", headers=self.auth())
        self.assertTrue(r.get_json()["has_ptz"])
        self.assertIn("ptz", r.get_json())


class TestBrowseOnlyMode(unittest.TestCase):
    """media_browser.py runs the same app with no camera."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "config.yaml").write_text(CFG.format(root=root))
        for p in ("rec/main", "events", "det"):
            (root / p).mkdir(parents=True, exist_ok=True)
        os.environ["RKNN_WEB_PASSWORD"] = "wpw"
        cfg = config.load(root / "config.yaml", root / "nope.yaml",
                          require_password=False)
        app = create_app(cfg)
        app.config["TESTING"] = True
        self.c = app.test_client()

    def tearDown(self):
        os.environ.pop("RKNN_WEB_PASSWORD", None)
        self.tmp.cleanup()

    def auth(self):
        tok = base64.b64encode(b"tvw:wpw").decode()
        return {"Authorization": "Basic " + tok}

    def test_panel_still_renders(self):
        r = self.c.get("/", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"unavailable", r.data)

    def test_ptz_routes_report_unavailable_not_crash(self):
        r = self.c.post("/api/ptz/move", data={"dir": "left"},
                        headers=self.auth())
        self.assertEqual(r.status_code, 503)




class TestMediaBrowser(Base):
    """The two-tab browser: stills and clips over one day, sharing a moment."""

    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        day = "2026-08-20"
        (root / "det" / day).mkdir(parents=True, exist_ok=True)
        for t in ("23-47-01", "23-47-02", "23-47-05"):
            (root / "det" / day / f"2026-08-20_{t}.jpg").write_bytes(b"\xff\xd8\xff")
        (root / "events" / day).mkdir(parents=True, exist_ok=True)
        (root / "events" / day /
         "clip_2026-08-20_23-46-59_2026-08-20_23-48-20.mp4").write_bytes(b"\0" * 2048)
        (root / "events" / day /
         "clip_2026-08-20_23-46-59_2026-08-20_23-48-20.annotated.mp4"
         ).write_bytes(b"\0" * 512)

    def _data(self, body, var):
        """The JS array the template embeds, as a count of entries."""
        import re as _re
        m = _re.search(var + r" = \[(.*?)\n  \];", body, _re.S)
        inner = (m.group(1) if m else "").strip()
        return 0 if not inner else inner.count("{name:")

    def test_the_page_renders_with_both_tabs(self):
        r = self.c.get("/media", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        body = r.data.decode()
        self.assertIn("Stills", body)
        self.assertIn("Video", body)
        self.assertEqual(self._data(body, "SHOTS"), 3)
        self.assertEqual(self._data(body, "CLIPS"), 1)

    def test_stills_are_listed_with_their_times(self):
        r = self.c.get("/media", headers=self.auth())
        for t in (b"23:47:01", b"23:47:02", b"23:47:05"):
            self.assertIn(t, r.data)

    def test_a_clip_carries_its_start_and_end_seconds(self):
        r = self.c.get("/media", headers=self.auth())
        # 23:46:59 -> 85619, 23:48:20 -> 85700
        self.assertIn(b"start:85619", r.data)
        self.assertIn(b"end:85700", r.data)

    def test_the_annotated_companion_is_a_mode_not_a_second_clip(self):
        body = self.c.get("/media", headers=self.auth()).data.decode()
        self.assertEqual(self._data(body, "CLIPS"), 1,
                         "one event, one entry")
        self.assertIn("ann:", body)
        self.assertIn("annotated.mp4", body,
                      "reachable as the annotated mode of that clip")

    def test_a_clip_without_a_companion_offers_none(self):
        root = Path(self.tmp.name)
        (root / "events" / "2026-08-21").mkdir(parents=True, exist_ok=True)
        (root / "events" / "2026-08-21" /
         "clip_2026-08-21_01-00-00_2026-08-21_01-01-00.mp4").write_bytes(b"\0" * 99)
        body = self.c.get("/media?day=2026-08-21",
                          headers=self.auth()).data.decode()
        self.assertIn('ann:""', body)

    def test_days_are_offered_newest_first(self):
        root = Path(self.tmp.name)
        (root / "det" / "2026-08-18").mkdir(parents=True, exist_ok=True)
        r = self.c.get("/media", headers=self.auth())
        body = r.data.decode()
        self.assertLess(body.index("2026-08-20"), body.index("2026-08-18"))

    def test_a_specific_day_can_be_asked_for(self):
        root = Path(self.tmp.name)
        (root / "det" / "2026-08-18").mkdir(parents=True, exist_ok=True)
        body = self.c.get("/media?day=2026-08-18",
                          headers=self.auth()).data.decode()
        self.assertEqual(self._data(body, "SHOTS"), 0)

    def test_a_day_with_nothing_does_not_error(self):
        r = self.c.get("/media?day=2020-01-01", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._data(r.data.decode(), "SHOTS"), 0)

    def test_it_needs_the_password(self):
        self.assertEqual(self.c.get("/media").status_code, 401)

    def test_media_files_are_actually_servable(self):
        r = self.c.get("/media", headers=self.auth())
        body = r.data.decode()
        import re as _re
        url = _re.search(r'url:"([^"]*\.jpg)"', body).group(1)
        got = self.c.get(url, headers=self.auth())
        self.assertEqual(got.status_code, 200)
        self.assertTrue(got.data.startswith(b"\xff\xd8"))




class TestClipSeeking(Base):
    """Each file has its own first frame, and seeking must use the right one.

    Reported from the tablet: a 10:19:17 thumbnail opened the annotated clip at
    10:19:20 but the full-resolution one at 10:18:28 -- because the two files
    start at different moments and the page used a single offset for both.
    """

    def setUp(self):
        super().setUp()
        import json
        root = Path(self.tmp.name)
        self.day = "2026-08-20"
        d = root / "events" / self.day
        d.mkdir(parents=True, exist_ok=True)
        base = "clip_2026-08-20_10-18-00_2026-08-20_10-20-30"
        (d / f"{base}.mp4").write_bytes(b"\0" * 4096)
        (d / f"{base}.annotated.mp4").write_bytes(b"\0" * 512)
        # full-res begins on the segment boundary 10:18:00;
        # the companion begins when the incident did, 10:19:02.
        (d / f"{base}.json").write_text(json.dumps({
            "t0": "2026-08-20T10:18:00",
            "annotated_t0": "2026-08-20T10:19:02",
            "window_start": "2026-08-20T10:19:00",
            "window_end": "2026-08-20T10:20:30"}))

    def clip(self):
        import re as _re
        body = self.c.get(f"/media?day={self.day}",
                          headers=self.auth()).data.decode()
        m = _re.search(r"\{name:\"clip_[^}]*\}", body)
        self.assertIsNotNone(m, "the clip should be listed")
        return m.group(0)

    def test_the_two_files_carry_different_start_times(self):
        c = self.clip()
        self.assertIn("t0:37080", c)        # 10:18:00
        self.assertIn("annT0:37142", c)     # 10:19:02

    def test_without_a_sidecar_both_fall_back_to_the_name(self):
        (Path(self.tmp.name) / "events" / self.day /
         "clip_2026-08-20_10-18-00_2026-08-20_10-20-30.json").unlink()
        c = self.clip()
        self.assertIn("t0:37080", c)
        self.assertIn("annT0:37080", c,
                      "with nothing better, the companion uses the same base")

    def test_coverage_starts_at_the_first_frame_not_the_window(self):
        """A still at 10:18:30 is inside the file even though the incident
        only began at 10:19:00."""
        c = self.clip()
        self.assertIn("t0:37080", c)
        self.assertIn("end:37230", c)       # 10:20:30

    def test_a_malformed_sidecar_does_not_break_the_page(self):
        (Path(self.tmp.name) / "events" / self.day /
         "clip_2026-08-20_10-18-00_2026-08-20_10-20-30.json"
         ).write_text("{ not json")
        r = self.c.get(f"/media?day={self.day}", headers=self.auth())
        self.assertEqual(r.status_code, 200)




class TestStillsWithoutClips(Base):
    """A still exists only because something triggered, so it should have a
    clip behind it -- except while the incident is still open, when the clip
    has not been cut yet. The page must say which."""

    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        d = root / "det" / "2026-08-20"
        d.mkdir(parents=True, exist_ok=True)
        (d / "2026-08-20_10-46-33.jpg").write_bytes(b"\xff\xd8\xff")

    def app_with(self, controller):
        from webapp import create_app
        app = create_app(self.cfg, ptz=self.ptz, controller=controller,
                         schedule=self.sched)
        app.config["TESTING"] = True
        return app.test_client()

    def test_no_open_incident_reports_minus_one(self):
        body = self.c.get("/media?day=2026-08-20",
                          headers=self.auth()).data.decode()
        self.assertIn("OPEN_SINCE = -1", body)

    def test_an_open_incident_is_reported_in_seconds(self):
        import datetime as _dt

        class Ctl:
            schedule = None
            def open_incident_start(self):
                return _dt.datetime(2026, 8, 20, 10, 46, 0)
            def status(self):
                return {}
        c = self.app_with(Ctl())
        body = c.get("/media?day=2026-08-20", headers=self.auth()).data.decode()
        self.assertIn("OPEN_SINCE = 38760", body)   # 10:46:00

    def test_an_incident_on_another_day_does_not_count(self):
        import datetime as _dt

        class Ctl:
            schedule = None
            def open_incident_start(self):
                return _dt.datetime(2026, 8, 19, 10, 46, 0)
            def status(self):
                return {}
        c = self.app_with(Ctl())
        body = c.get("/media?day=2026-08-20", headers=self.auth()).data.decode()
        self.assertIn("OPEN_SINCE = -1", body)

    def test_a_broken_controller_does_not_break_the_page(self):
        class Ctl:
            schedule = None
            def open_incident_start(self):
                raise RuntimeError("boom")
            def status(self):
                return {}
        c = self.app_with(Ctl())
        r = c.get("/media?day=2026-08-20", headers=self.auth())
        self.assertEqual(r.status_code, 200)




class TestLiveStrip(Base):
    """The strip must gain new frames without a reload -- and a poll must
    return only what the page does not already have, or appending would
    duplicate and rebuilding would lose the scroll position."""

    def setUp(self):
        super().setUp()
        self.day = "2026-08-20"
        d = Path(self.tmp.name) / "det" / self.day
        d.mkdir(parents=True, exist_ok=True)
        for t in ("10-00-01", "10-00-02", "10-00-03"):
            (d / f"2026-08-20_{t}.jpg").write_bytes(b"\xff\xd8\xff")

    def poll(self, after):
        r = self.c.get(f"/api/media?day={self.day}&after={after}",
                       headers=self.auth())
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_it_returns_only_stills_newer_than_the_marker(self):
        # 10:00:01 = 36001, 10:00:02 = 36002, 10:00:03 = 36003
        self.assertEqual(len(self.poll(-1)["shots"]), 3)
        self.assertEqual(len(self.poll(36001)["shots"]), 2)
        self.assertEqual(len(self.poll(36003)["shots"]), 0,
                         "nothing new must mean an empty list, not a resend")

    def test_a_new_still_appears_on_the_next_poll(self):
        self.assertEqual(self.poll(36003)["shots"], [])
        (Path(self.tmp.name) / "det" / self.day /
         "2026-08-20_10-00-09.jpg").write_bytes(b"\xff\xd8\xff")
        fresh = self.poll(36003)["shots"]
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]["secs"], 36009)

    def test_clips_come_back_whole_so_play_markers_update(self):
        d = Path(self.tmp.name) / "events" / self.day
        d.mkdir(parents=True, exist_ok=True)
        (d / "clip_2026-08-20_10-00-00_2026-08-20_10-00-30.mp4"
         ).write_bytes(b"\0" * 100)
        data = self.poll(36003)
        self.assertEqual(data["shots"], [])
        self.assertEqual(len(data["clips"]), 1,
                         "clips are few, so resending them keeps the play "
                         "markers correct without a rebuild")

    def test_it_reports_whether_an_incident_is_open(self):
        self.assertEqual(self.poll(-1)["open_since"], -1)

    def test_a_bad_after_value_does_not_error(self):
        r = self.c.get(f"/api/media?day={self.day}&after=banana",
                       headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()["shots"]), 3)

    def test_the_poll_needs_the_password(self):
        self.assertEqual(self.c.get("/api/media?day=x").status_code, 401)

    def test_the_page_carries_the_poll_interval(self):
        body = self.c.get("/media", headers=self.auth()).data.decode()
        self.assertIn("POLL_MS", body)
        self.assertIn("setInterval(poll", body)




class TestPageAndPollAgree(Base):
    """The page renders the clip list into JavaScript and the poll replaces it
    with JSON. If the two use different field names the page works until the
    first poll, then silently degrades -- which is exactly what happened:
    annotated mode fell back to full resolution five seconds in.
    """

    def setUp(self):
        super().setUp()
        import json
        self.day = "2026-08-20"
        d = Path(self.tmp.name) / "events" / self.day
        d.mkdir(parents=True, exist_ok=True)
        base = "clip_2026-08-20_10-18-00_2026-08-20_10-20-30"
        (d / f"{base}.mp4").write_bytes(b"\0" * 4096)
        (d / f"{base}.annotated.mp4").write_bytes(b"\0" * 512)
        (d / f"{base}.json").write_text(json.dumps({
            "t0": "2026-08-20T10:18:00",
            "annotated_t0": "2026-08-20T10:19:02",
            "window_end": "2026-08-20T10:20:30"}))
        det = Path(self.tmp.name) / "det" / self.day
        det.mkdir(parents=True, exist_ok=True)
        (det / "2026-08-20_10-19-00.jpg").write_bytes(b"\xff\xd8\xff")

    # Every field the page's JavaScript reads off a clip object.
    USED_BY_JS = ("name", "at", "start", "end", "t0", "annT0",
                  "size", "url", "ann", "annSize")

    def test_the_poll_returns_every_field_the_page_uses(self):
        clip = self.c.get(f"/api/media?day={self.day}",
                          headers=self.auth()).get_json()["clips"][0]
        missing = [k for k in self.USED_BY_JS if k not in clip]
        self.assertEqual(missing, [],
                         f"the poll would leave these undefined: {missing}")

    def test_the_rendered_page_uses_the_same_names(self):
        body = self.c.get(f"/media?day={self.day}",
                          headers=self.auth()).data.decode()
        import re as _re
        entry = _re.search(r"\{name:\"clip_[^}]*\}", body).group(0)
        for k in self.USED_BY_JS:
            self.assertIn(k + ":", entry,
                          f"{k} missing from the rendered clip object")

    def test_the_annotated_url_survives_a_poll(self):
        clip = self.c.get(f"/api/media?day={self.day}",
                          headers=self.auth()).get_json()["clips"][0]
        self.assertTrue(clip["ann"].endswith(".annotated.mp4"),
                        "annotated mode has nothing to play without this")
        self.assertNotEqual(clip["annT0"], clip["t0"],
                            "the two files start at different moments")




class TestSettingsScreen(Base):
    """The one screen that can stop the system recording anything."""

    def setUp(self):
        super().setUp()
        from settings import Settings
        self.store = Settings(Path(self.tmp.name) / "settings.json",
                              defaults={"trigger_classes":
                                        sorted(self.cfg.trigger_classes)})
        self.app = create_app(self.cfg, ptz=self.ptz, controller=None,
                              schedule=self.sched, settings=self.store)
        self.app.config["TESTING"] = True
        self.c = self.app.test_client()

    def test_the_page_renders_with_the_current_selection_ticked(self):
        r = self.c.get("/settings", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        body = r.data.decode()
        self.assertIn('value="person"', body)
        self.assertIn("Likely", body)

    def test_saving_changes_what_the_detector_will_match(self):
        r = self.c.post("/settings", data={"cls": ["person", "dog"]},
                        headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.store.trigger_classes, {"person", "dog"})

    def test_an_empty_post_is_honoured_and_warned_about(self):
        r = self.c.post("/settings", data={}, headers=self.auth())
        self.assertEqual(self.store.trigger_classes, set())
        self.assertIn("nothing will ever", r.data.decode().lower().replace(
            "<b>", "").replace("</b>", ""))

    def test_an_unknown_class_is_refused_rather_than_stored(self):
        self.c.post("/settings", data={"cls": ["person", "unicorn"]},
                    headers=self.auth())
        self.assertEqual(self.store.trigger_classes, {"person"},
                         "a class the model cannot emit can never fire")

    def test_reset_restores_the_configured_default(self):
        self.c.post("/settings", data={"cls": ["dog"]}, headers=self.auth())
        self.assertTrue(self.store.overridden("trigger_classes"))
        r = self.c.post("/settings/reset", headers=self.auth())
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.store.trigger_classes, self.cfg.trigger_classes)

    def test_it_needs_authentication_like_everything_else(self):
        self.assertEqual(self.c.get("/settings").status_code, 401)
        self.assertEqual(
            self.c.post("/settings", data={"cls": ["dog"]}).status_code, 401)
        self.assertEqual(self.store.trigger_classes, self.cfg.trigger_classes,
                         "an unauthenticated post must not change anything")

    def test_read_only_without_a_store(self):
        app = create_app(self.cfg, ptz=self.ptz, controller=None,
                         schedule=self.sched)
        app.config["TESTING"] = True
        c = app.test_client()
        r = c.get("/settings", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertIn("read-only", r.data.decode())
        self.assertEqual(c.post("/settings", data={"cls": ["dog"]},
                                headers=self.auth()).status_code, 503)


class TestEveryRouteIsProtected(Base):
    """A new route defaults to open, and nothing shouts when it is.

    /settings shipped unauthenticated because @protected sits below the route
    decorator and is easy to leave off. This walks the real url map instead of
    trusting a reading of the source.
    """

    # Deliberately open: healthz returns {ok:true} and nothing else, and
    # index.html only redirects to the panel, which is itself protected.
    OPEN = {"/healthz", "/index.html", "/static/<path:filename>"}

    def test_no_route_answers_without_credentials(self):
        leaked = []
        for rule in self.app.url_map.iter_rules():
            if str(rule) in self.OPEN:
                continue
            methods = rule.methods - {"HEAD", "OPTIONS"}
            if not methods:
                continue
            path = str(rule)
            if "<" in path:            # needs arguments; covered elsewhere
                continue
            for m in sorted(methods):
                r = self.c.open(path, method=m)
                if r.status_code != 401:
                    leaked.append(f"{m} {path} -> {r.status_code}")
        self.assertEqual(leaked, [],
                         "these answer without a password")




class TestCredentialsScreen(Base):
    """Changing the login is the one action that can lock everyone out."""

    NEWPW = "a longer password"

    def setUp(self):
        super().setUp()
        from settings import Settings
        self.store = Settings(Path(self.tmp.name) / "settings.json",
                              defaults={"trigger_classes":
                                        sorted(self.cfg.trigger_classes)})
        self.app = create_app(self.cfg, ptz=self.ptz, controller=None,
                              schedule=self.sched, settings=self.store)
        self.app.config["TESTING"] = True
        self.c = self.app.test_client()

    def post(self, **kw):
        data = {"user": "tvw", "current": "wpw",
                "new": self.NEWPW, "again": self.NEWPW}
        data.update(kw)
        return self.c.post("/settings/credentials", data=data,
                           headers=self.auth())

    def test_the_tab_renders(self):
        r = self.c.get("/settings/credentials", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertIn("Panel login", r.data.decode())

    def test_changing_the_login_takes_effect_at_once(self):
        self.post()
        self.assertTrue(self.store.has_credentials())
        # the old password no longer works, the new one does
        self.assertEqual(self.c.get("/", headers=self.auth(pw="wpw")).status_code,
                         401)
        self.assertEqual(
            self.c.get("/", headers=self.auth(pw=self.NEWPW)).status_code, 200)

    def test_the_current_password_is_required(self):
        r = self.post(current="wrong")
        self.assertFalse(self.store.has_credentials(),
                         "a wall panel is left logged in; without this check "
                         "anyone walking up could lock out the club")
        self.assertIn("not right", r.data.decode())

    def test_mismatched_confirmation_is_refused(self):
        r = self.post(again="something else")
        self.assertFalse(self.store.has_credentials())
        self.assertIn("do not match", r.data.decode())

    def test_a_short_password_is_refused_with_a_reason(self):
        r = self.post(new="short", again="short")
        self.assertFalse(self.store.has_credentials())
        self.assertIn("8 characters", r.data.decode())

    def test_the_user_name_can_change_too(self):
        self.post(user="club")
        self.assertEqual(
            self.c.get("/", headers=self.auth(user="club", pw=self.NEWPW)
                       ).status_code, 200)
        self.assertEqual(
            self.c.get("/", headers=self.auth(user="tvw", pw=self.NEWPW)
                       ).status_code, 401)

    def test_reset_restores_the_configured_login(self):
        self.post()
        self.assertEqual(
            self.c.post("/settings/credentials/reset",
                        headers=self.auth(pw=self.NEWPW)).status_code, 302)
        self.assertFalse(self.store.has_credentials())
        self.assertEqual(self.c.get("/", headers=self.auth(pw="wpw")).status_code,
                         200, "the secrets.yaml password works again")

    def test_resetting_the_login_leaves_the_categories_alone(self):
        self.c.post("/settings", data={"cls": ["dog"]}, headers=self.auth())
        self.post()
        self.c.post("/settings/credentials/reset", headers=self.auth(pw=self.NEWPW))
        self.assertEqual(self.store.trigger_classes, {"dog"})

    def test_it_needs_authentication(self):
        self.assertEqual(self.c.get("/settings/credentials").status_code, 401)
        self.assertEqual(
            self.c.post("/settings/credentials",
                        data={"current": "wpw", "new": self.NEWPW,
                              "again": self.NEWPW}).status_code, 401)
        self.assertFalse(self.store.has_credentials())

    def test_the_verdict_cache_does_not_outlive_a_change(self):
        # Warm the cache with the old password, then change it.
        self.assertEqual(self.c.get("/", headers=self.auth(pw="wpw")).status_code,
                         200)
        self.post()
        self.assertEqual(self.c.get("/", headers=self.auth(pw="wpw")).status_code,
                         401, "a cached pass must not survive the change")




class TestAccessModes(Base):
    """Who gets in without a password, and from where."""

    def setUp(self):
        super().setUp()
        from settings import Settings
        self.store = Settings(Path(self.tmp.name) / "settings.json",
                              defaults={"trigger_classes":
                                        sorted(self.cfg.trigger_classes)})
        self.app = create_app(self.cfg, ptz=self.ptz, controller=None,
                              schedule=self.sched, settings=self.store)
        self.app.config["TESTING"] = True
        self.c = self.app.test_client()

    def get(self, path="/", ip=None, **kw):
        env = {"REMOTE_ADDR": ip} if ip else {}
        return self.c.get(path, environ_overrides=env, **kw)

    def test_password_mode_is_the_default_and_still_bites(self):
        self.assertEqual(self.get().status_code, 401)

    def test_open_lets_everyone_in_with_no_credentials(self):
        self.c.post("/settings/access", data={"mode": "open"},
                    headers=self.auth())
        self.assertEqual(self.get().status_code, 200)
        self.assertEqual(self.get(ip="8.8.8.8").status_code, 200)

    def test_trusted_admits_the_local_network_only(self):
        self.c.post("/settings/access",
                    data={"mode": "trusted", "networks": "192.168.90.0/24"},
                    headers=self.auth())
        self.assertEqual(self.get(ip="192.168.90.50").status_code, 200)
        self.assertEqual(self.get(ip="192.168.91.50").status_code, 401,
                         "off the trusted network, a password is required")

    def test_an_untrusted_client_can_still_use_the_password(self):
        self.c.post("/settings/access",
                    data={"mode": "trusted", "networks": "192.168.90.0/24"},
                    headers=self.auth())
        r = self.c.get("/", headers=self.auth(),
                       environ_overrides={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(r.status_code, 200,
                         "trusted mode must not lock out everyone else")

    def test_trusted_with_no_network_is_refused_and_changes_nothing(self):
        r = self.c.post("/settings/access",
                        data={"mode": "trusted", "networks": ""},
                        headers=self.auth())
        self.assertIn("trusted network is needed", r.data.decode())
        self.assertEqual(self.get(ip="192.168.90.50").status_code, 401,
                         "a refused change must not have opened anything")

    def test_a_bad_network_is_refused_and_changes_nothing(self):
        r = self.c.post("/settings/access",
                        data={"mode": "trusted", "networks": "192.168.90.0/24, nope"},
                        headers=self.auth())
        self.assertIn("not an address", r.data.decode())
        self.assertEqual(self.get(ip="192.168.90.50").status_code, 401)

    def test_going_back_to_password_closes_it_again(self):
        self.c.post("/settings/access", data={"mode": "open"},
                    headers=self.auth())
        self.assertEqual(self.get().status_code, 200)
        self.c.post("/settings/access", data={"mode": "password"},
                    headers=self.auth())
        self.assertEqual(self.get().status_code, 401)

    def test_reset_returns_to_the_configured_setting(self):
        self.c.post("/settings/access", data={"mode": "open"},
                    headers=self.auth())
        self.assertEqual(
            self.c.post("/settings/access/reset", headers=self.auth()
                        ).status_code, 302)
        self.assertEqual(self.get().status_code, 401)

    def test_opening_access_needs_a_password_first(self):
        self.assertEqual(
            self.c.post("/settings/access", data={"mode": "open"}).status_code,
            401)
        self.assertEqual(self.get().status_code, 401,
                         "an unauthenticated request must not open the panel")

    def test_the_page_shows_the_callers_own_address(self):
        r = self.c.get("/settings/credentials", headers=self.auth(),
                       environ_overrides={"REMOTE_ADDR": "192.168.90.77"})
        self.assertIn("192.168.90.77", r.data.decode())
        self.assertIn("192.168.90.0/24", r.data.decode(),
                      "it should suggest the matching /24")




class TestSystemSettings(Base):
    """Editing config from the panel. This one can stop the service starting."""

    def setUp(self):
        super().setUp()
        from settings import Settings
        self.store = Settings(Path(self.tmp.name) / "settings.json",
                              defaults={"trigger_classes":
                                        sorted(self.cfg.trigger_classes)})
        self.app = create_app(self.cfg, ptz=self.ptz, controller=None,
                              schedule=self.sched, settings=self.store)
        self.app.config["TESTING"] = True
        self.c = self.app.test_client()

    def post(self, **fields):
        return self.c.post("/settings/system", data=fields,
                           headers=self.auth())

    def test_the_page_renders_with_the_effective_values(self):
        r = self.c.get("/settings/system", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        body = r.data.decode()
        self.assertIn("camera.sub_path", body)
        self.assertIn("Storage", body)

    def test_setting_a_field_is_stored_as_an_override(self):
        self.post(**{"camera.sub_path": "livestream/13"})
        self.assertEqual(
            self.store.config_overrides.get("camera", {}).get("sub_path"),
            "livestream/13")

    def test_an_empty_box_removes_the_override(self):
        self.post(**{"camera.sub_path": "livestream/13"})
        self.post(**{"camera.sub_path": ""})
        self.assertEqual(self.store.config_overrides, {},
                         "and the empty camera section goes with it")

    def test_numbers_are_stored_as_numbers_not_strings(self):
        self.post(**{"retention.target_free_percent": "25"})
        v = self.store.config_overrides["retention"]["target_free_percent"]
        self.assertEqual(v, 25)
        self.assertNotIsInstance(v, str, "yaml would quote it back")

    def test_a_non_number_in_a_number_field_is_refused(self):
        r = self.post(**{"retention.target_free_percent": "lots"})
        self.assertIn("must be a number", r.data.decode())
        self.assertEqual(self.store.config_overrides, {})

    def test_a_value_that_would_not_load_is_refused(self):
        # 150% free is outside the range config.py validates.
        r = self.post(**{"retention.target_free_percent": "150"})
        self.assertIn("stop the service starting", r.data.decode())
        self.assertEqual(self.store.config_overrides, {},
                         "the whole point: never store something unstartable")

    def test_raw_yaml_replaces_the_override_set(self):
        self.post(**{"camera.sub_path": "livestream/13"})
        self.post(raw="retention:\n  target_free_percent: 30\n")
        ov = self.store.config_overrides
        self.assertEqual(ov, {"retention": {"target_free_percent": 30}},
                         "the editor shows the whole set, so it replaces it")

    def test_broken_yaml_is_refused(self):
        r = self.post(raw="camera:\n  host: [unclosed\n")
        self.assertIn("not valid YAML", r.data.decode())
        self.assertEqual(self.store.config_overrides, {})

    def test_a_yaml_scalar_at_the_top_level_is_refused(self):
        r = self.post(raw="just a string")
        self.assertIn("mapping", r.data.decode())

    def test_a_password_in_the_yaml_is_dropped_not_stored(self):
        self.post(raw="camera:\n  host: 10.0.0.9\n  password: hunter2\n")
        ov = self.store.config_overrides
        self.assertEqual(ov.get("camera", {}).get("host"), "10.0.0.9")
        self.assertNotIn("password", ov.get("camera", {}),
                         "secrets belong in secrets.yaml, mode 600")
        self.assertNotIn("hunter2", json.dumps(ov))

    def test_reset_forgets_everything(self):
        self.post(**{"camera.sub_path": "livestream/13"})
        self.assertEqual(
            self.c.post("/settings/system/reset", headers=self.auth()
                        ).status_code, 302)
        self.assertEqual(self.store.config_overrides, {})

    def test_it_needs_authentication(self):
        self.assertEqual(self.c.get("/settings/system").status_code, 401)
        self.assertEqual(
            self.c.post("/settings/system",
                        data={"camera.host": "1.2.3.4"}).status_code, 401)
        self.assertEqual(self.store.config_overrides, {})

    def test_the_overrides_actually_reach_a_loaded_config(self):
        import config as config_mod
        self.post(**{"camera.sub_path": "livestream/13"})
        cfg2 = config_mod.load(self.cfg.path,
                               overrides=self.store.config_overrides,
                               require_password=False)
        self.assertEqual(cfg2.raw["camera"]["sub_path"], "livestream/13",
                         "storing it is only useful if it is read back")




class TestRestartButton(Base):
    """Restarting from the panel. The dangerous case is when nothing would
    bring the service back."""

    def setUp(self):
        super().setUp()
        from settings import Settings
        self.store = Settings(Path(self.tmp.name) / "settings.json",
                              defaults={"trigger_classes":
                                        sorted(self.cfg.trigger_classes)})
        self.app = create_app(self.cfg, ptz=self.ptz, controller=None,
                              schedule=self.sched, settings=self.store)
        self.app.config["TESTING"] = True
        self.c = self.app.test_client()
        self.killed = []

    def under_systemd(self, yes=True):
        if yes:
            os.environ["INVOCATION_ID"] = "test"
        else:
            os.environ.pop("INVOCATION_ID", None)
        self.addCleanup(os.environ.pop, "INVOCATION_ID", None)

    def no_actual_kill(self):
        """Let the route run without taking the test process with it."""
        import webapp as webapp_mod
        real = webapp_mod.os.kill
        webapp_mod.os.kill = lambda pid, sig: self.killed.append((pid, sig))
        self.addCleanup(setattr, webapp_mod.os, "kill", real)

    def test_it_refuses_when_nothing_would_restart_it(self):
        self.under_systemd(False)
        self.no_actual_kill()
        r = self.c.post("/settings/restart", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertIn("not running under systemd", r.data.decode())
        time.sleep(1.4)
        self.assertEqual(self.killed, [],
                         "stopping a camera that will not come back is worse "
                         "than refusing")

    def test_under_systemd_it_signals_itself(self):
        self.under_systemd(True)
        self.no_actual_kill()
        r = self.c.post("/settings/restart", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertIn("Restarting", r.data.decode())
        time.sleep(1.5)                 # the route waits ~1s so the page lands
        self.assertEqual(len(self.killed), 1)
        self.assertEqual(self.killed[0][1], signal.SIGTERM,
                         "SIGTERM runs the shutdown path, so an open incident "
                         "still gets its clip")

    def test_the_page_says_it_is_waiting_rather_than_failing(self):
        self.under_systemd(True)
        self.no_actual_kill()
        body = self.c.post("/settings/restart",
                           headers=self.auth()).data.decode()
        self.assertIn("healthz", body, "it polls for the service coming back")
        time.sleep(1.5)

    def test_the_button_is_offered_only_under_systemd(self):
        self.under_systemd(True)
        self.assertIn("Restart now",
                      self.c.get("/settings/system",
                                 headers=self.auth()).data.decode())
        self.under_systemd(False)
        body = self.c.get("/settings/system", headers=self.auth()).data.decode()
        self.assertNotIn("Restart now", body)
        self.assertIn("nothing would start it again", body)

    def test_it_needs_authentication(self):
        self.under_systemd(True)
        self.no_actual_kill()
        self.assertEqual(self.c.post("/settings/restart").status_code, 401)
        time.sleep(1.4)
        self.assertEqual(self.killed, [],
                         "an unauthenticated request must never stop the camera")




class TestEveryPageIsValidHtml(Base):
    """Render each page and check the tags actually nest.

    This exists because a settings pane was once inserted into the middle of
    a class="" attribute. Every string you would grep for was present in the
    output, so a check like `assertIn("Restart now", body)` passed happily
    while no browser would ever draw the button. Presence of text is not
    evidence that a page renders.
    """

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def setUp(self):
        super().setUp()
        from settings import Settings
        self.store = Settings(Path(self.tmp.name) / "settings.json",
                              defaults={"trigger_classes":
                                        sorted(self.cfg.trigger_classes)})
        self.app = create_app(self.cfg, ptz=self.ptz, controller=None,
                              schedule=self.sched, settings=self.store)
        self.app.config["TESTING"] = True
        self.c = self.app.test_client()

    def check(self, html, where):
        from html.parser import HTMLParser

        problems, stack = [], []
        outer = self

        class P(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag not in outer.VOID:
                    stack.append((tag, self.getpos()[0]))

            def handle_endtag(self, tag):
                if tag in outer.VOID:
                    return
                if not stack:
                    problems.append("</%s> at line %d closes nothing"
                                    % (tag, self.getpos()[0]))
                    return
                if stack[-1][0] != tag:
                    problems.append(
                        "</%s> at line %d, but <%s> from line %d is open"
                        % (tag, self.getpos()[0], stack[-1][0], stack[-1][1]))
                    for i in range(len(stack) - 1, -1, -1):
                        if stack[i][0] == tag:
                            del stack[i:]
                            return
                    return
                stack.pop()

        p = P(convert_charrefs=True)
        p.feed(html)
        self.assertEqual(problems, [], "%s: %s" % (where, problems[:3]))
        self.assertEqual([t for t, _ in stack], [],
                         "%s: never closed" % where)

    def test_every_page_nests_correctly(self):
        for path in ("/", "/media", "/settings", "/settings/credentials",
                     "/settings/system", "/settings/wifi", "/legal"):
            r = self.c.get(path, headers=self.auth())
            self.assertEqual(r.status_code, 200, path)
            self.check(r.data.decode(), path)

    def test_no_jinja_survives_into_the_output(self):
        # A stray {% %} means a block was pasted somewhere it is not parsed,
        # which is the other half of the same failure.
        for path in ("/settings", "/settings/credentials",
                     "/settings/system", "/settings/wifi"):
            body = self.c.get(path, headers=self.auth()).data.decode()
            for marker in ("{%", "{{"):
                self.assertNotIn(marker, body, "%s leaked %s" % (path, marker))

    def test_each_settings_tab_marks_itself_current(self):
        # The bug hid inside a tab link's class attribute, so assert the
        # attribute still does its job on every tab.
        import re
        for path, tab in (("/settings", "Categories"),
                          ("/settings/credentials", "Credentials"),
                          ("/settings/system", "System"),
                          ("/settings/wifi", "Wi-Fi")):
            body = self.c.get(path, headers=self.auth()).data.decode()
            m = re.search(r'<a[^>]*class="on"[^>]*>([^<]+)</a>', body)
            self.assertIsNotNone(m, "%s marks no tab as current" % path)
            self.assertEqual(m.group(1).strip(), tab, path)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class _StubController:
    def __init__(self, ready=True):
        self.ready = ready
        self.called = 0
    def sweep_once(self):
        self.called += 1
        return self.ready


class TestUsageMeter(Base):
    """The bundle counter on the System tab."""

    def setUp(self):
        super().setUp()
        from datausage import DataUsage
        net = Path(self.tmp.name) / "net" / "wan0" / "statistics"
        net.mkdir(parents=True)
        (net / "rx_bytes").write_text(str(3 * 1073741824))
        (net / "tx_bytes").write_text("0")
        self.usage = DataUsage(Path(self.tmp.name) / "usage.json",
                               iface="wan0", limit_gb=5.0, billing_day=1,
                               sys_net=str(Path(self.tmp.name) / "net"))
        self.usage._last_raw = 0            # pretend we have been watching
        self.usage._last_iface = "wan0"
        self.usage.sample()
        from settings import Settings
        self.store = Settings(Path(self.tmp.name) / "settings.json")
        self.app = create_app(self.cfg, ptz=self.ptz, controller=None,
                              schedule=self.sched, settings=self.store,
                              usage=self.usage)
        self.app.config["TESTING"] = True
        self.c = self.app.test_client()

    def test_the_system_tab_shows_the_bundle(self):
        body = self.c.get("/settings/system", headers=self.auth()).data
        self.assertIn(b"Mobile data", body)
        self.assertIn(b"3.0 GB", body)
        self.assertIn(b"of 5.0 GB", body)

    def test_it_warns_past_eighty_percent(self):
        (Path(self.tmp.name) / "net" / "wan0" / "statistics"
         / "rx_bytes").write_text(str(int(4.5 * 1073741824)))
        self.usage.sample()
        body = self.c.get("/settings/system", headers=self.auth()).data
        self.assertIn(b"80%", body)

    def test_status_carries_it_so_the_panel_can_warn(self):
        s = self.c.get("/api/status", headers=self.auth()).get_json()
        self.assertIn("usage", s)
        self.assertEqual(s["usage"]["used_gb"], 3.0)
        self.assertFalse(s["usage"]["warn"])

    def test_no_meter_configured_is_not_an_error(self):
        app = create_app(self.cfg, ptz=self.ptz, controller=None,
                         schedule=self.sched, settings=self.store)
        app.config["TESTING"] = True
        c = app.test_client()
        r = c.get("/settings/system", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"Mobile data", r.data)


class TestMeteredLink(Base):
    """A viewer over the 4G tunnel costs money; one on the board's own wiring
    does not. The streams are the only thing here big enough to matter."""

    LOCAL = {"REMOTE_ADDR": "192.168.92.5"}     # the wall tablet on the AP
    REMOTE = {"REMOTE_ADDR": "10.8.2.9"}        # somebody over the VPN

    def test_the_wall_tablet_gets_the_full_picture(self):
        body = self.c.get("/", headers=self.auth(),
                          environ_base=self.LOCAL).data
        self.assertNotIn(b'id="metered"', body)
        self.assertIn(b'var showDetector = true', body)

    def test_a_remote_viewer_is_warned_and_gets_overlays_off(self):
        # The overlay feed costs about 2.5x the plain one.
        body = self.c.get("/", headers=self.auth(),
                          environ_base=self.REMOTE).data
        self.assertIn(b'id="metered"', body)
        self.assertIn(b'var showDetector = false', body)

    def test_the_aiming_feed_is_capped_for_a_remote_viewer(self):
        # Full-size JPEGs at 6fps is over 3 GB an hour; asking for more than
        # the cap must not be honoured.
        r = self.c.get("/aim.mjpg?fps=10", headers=self.auth(),
                       environ_base=self.REMOTE)
        self.assertIn("multipart/x-mixed-replace", r.headers["Content-Type"])
        r.response.close()

    def test_an_unparseable_address_is_assumed_to_cost_money(self):
        body = self.c.get("/", headers=self.auth(),
                          environ_base={"REMOTE_ADDR": ""}).data
        self.assertIn(b'id="metered"', body,
                      "an unknown client was assumed free")


class TestPanelLayout(Base):
    """The wall panel's right-hand pane: two tabs, not one long column."""

    def test_both_tabs_render(self):
        body = self.c.get("/", headers=self.auth()).data
        self.assertIn(b'data-tab="view"', body)
        self.assertIn(b'data-tab="manual"', body)
        self.assertIn(b'id="pane-view"', body)
        self.assertIn(b'id="pane-manual"', body)

    def test_the_two_destinations_are_prominent_not_footnotes(self):
        # They were small links lost under a column of controls on the wall.
        body = self.c.get("/", headers=self.auth()).data.decode()
        self.assertIn('class="big" href="/media"', body)
        self.assertIn('class="big" href="/settings"', body)

    def test_movement_controls_live_in_the_manual_tab(self):
        body = self.c.get("/", headers=self.auth()).data.decode()
        manual = body.split('id="pane-manual"', 1)[1]
        view = body.split('id="pane-view"', 1)[1].split('id="pane-manual"', 1)[0]
        self.assertIn('data-move="left"', manual)
        self.assertNotIn('data-move=', view,
                         "a control that moves the camera is on the idle tab")


class TestSweepNow(Base):
    def setUp(self):
        super().setUp()
        from settings import Settings
        self.store = Settings(Path(self.tmp.name) / "settings.json")
        self.ctrl = _StubController(ready=True)
        self.app = create_app(self.cfg, ptz=self.ptz, controller=self.ctrl,
                              schedule=self.sched, settings=self.store)
        self.app.config["TESTING"] = True
        self.c = self.app.test_client()

    def test_sweep_now_runs_a_cycle(self):
        r = self.c.post("/api/ptz/sweep/once", headers=self.auth())
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.ctrl.called, 1)

    def test_set_home_saves_the_home_preset(self):
        r = self.c.post("/api/ptz/home/set", headers=self.auth())
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(r.get_json()["name"], "Home")
        self.assertIn("ptzAddPresetPoint", self.cam.calls)
        self.assertTrue(self.store.sweep_home_saved)
        s = self.c.get("/api/status", headers=self.auth()).get_json()
        self.assertTrue(s["sweep"]["home_saved"])

    def test_sweep_now_reports_when_endpoints_missing(self):
        self.ctrl.ready = False
        r = self.c.post("/api/ptz/sweep/once", headers=self.auth())
        self.assertFalse(r.get_json()["ok"])
        self.assertIn("Left and Right", r.get_json()["error"])


class TestTriggerSweepEndpoints(Base):
    """Saving sweep endpoints from the panel and toggling the behaviour."""

    def setUp(self):
        super().setUp()
        from settings import Settings
        self.store = Settings(Path(self.tmp.name) / "settings.json")
        self.app = create_app(self.cfg, ptz=self.ptz, controller=None,
                              schedule=self.sched, settings=self.store)
        self.app.config["TESTING"] = True
        self.c = self.app.test_client()

    def test_set_left_saves_a_preset_and_records_it(self):
        r = self.c.post("/api/ptz/sweep/set", data={"dir": "left"},
                        headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertTrue(self.store.sweep_left_saved)
        self.assertIn("ptzAddPresetPoint", self.cam.calls)

    def test_ready_only_after_both_ends(self):
        self.c.post("/api/ptz/sweep/set", data={"dir": "left"},
                    headers=self.auth())
        self.assertFalse(self.store.sweep_ready)
        self.c.post("/api/ptz/sweep/set", data={"dir": "right"},
                    headers=self.auth())
        self.assertTrue(self.store.sweep_ready)

    def test_bad_side_is_rejected_without_saving(self):
        r = self.c.post("/api/ptz/sweep/set", data={"dir": "up"},
                        headers=self.auth())
        self.assertFalse(r.get_json()["ok"])
        self.assertFalse(self.store.sweep_left_saved)

    def test_toggle_on_and_off(self):
        self.c.post("/api/ptz/sweep", data={"enabled": "1"},
                    headers=self.auth())
        self.assertTrue(self.store.sweep_enabled)
        self.c.post("/api/ptz/sweep", data={"enabled": "0"},
                    headers=self.auth())
        self.assertFalse(self.store.sweep_enabled)

    def test_settings_screen_saves_dwell_and_enable(self):
        r = self.c.post("/settings/sweep",
                        data={"sweep_enabled": "on", "sweep_dwell": "7.5"},
                        headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.store.sweep_enabled)
        self.assertEqual(self.store.sweep_dwell_s, 7.5)

    def test_settings_screen_saves_speed_and_budget(self):
        r = self.c.post("/settings/sweep",
                        data={"sweep_enabled": "on", "sweep_dwell": "6",
                              "sweep_speed": "3", "sweep_budget": "600"},
                        headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.store.sweep_speed, 3)
        self.assertEqual(self.store.sweep_budget_s, 600.0)

    def test_the_budget_takes_effect_without_a_restart(self):
        # The whole point of putting it on the panel.
        self.c.post("/settings/sweep",
                    data={"sweep_enabled": "on", "sweep_dwell": "6",
                          "sweep_speed": "", "sweep_budget": "450"},
                    headers=self.auth())
        self.assertEqual(self.ptz.budget.limits["auto"], 450.0)

    def test_a_blank_speed_leaves_the_camera_alone(self):
        self.c.post("/settings/sweep",
                    data={"sweep_enabled": "on", "sweep_dwell": "6",
                          "sweep_speed": "", "sweep_budget": "600"},
                    headers=self.auth())
        self.assertIsNone(self.store.sweep_speed)

    def test_speed_and_budget_are_clamped_not_trusted(self):
        self.c.post("/settings/sweep",
                    data={"sweep_enabled": "on", "sweep_dwell": "6",
                          "sweep_speed": "99", "sweep_budget": "999999"},
                    headers=self.auth())
        self.assertLessEqual(self.store.sweep_speed, 4)
        self.assertLessEqual(self.store.sweep_budget_s, 3600.0)

    def test_settings_screen_rejects_a_bad_dwell(self):
        r = self.c.post("/settings/sweep",
                        data={"sweep_enabled": "on", "sweep_dwell": "soon"},
                        headers=self.auth())
        self.assertIn(b"must be a number", r.data)

    def test_settings_page_shows_the_sweep_section(self):
        body = self.c.get("/settings", headers=self.auth()).data
        self.assertIn(b"Trigger sweep", body)
        self.assertIn(b"Sweep when triggered", body)

    def test_status_reports_sweep_state(self):
        self.c.post("/api/ptz/sweep/set", data={"dir": "left"},
                    headers=self.auth())
        s = self.c.get("/api/status", headers=self.auth()).get_json()
        self.assertIn("sweep", s)
        self.assertTrue(s["sweep"]["left_saved"])
        self.assertFalse(s["sweep"]["right_saved"])
