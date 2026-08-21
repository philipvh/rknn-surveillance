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

"""Tests for the config layer.

Chiefly: the password is handled correctly and never leaks into anything
loggable, and a broken config fails loudly at startup rather than at 3am.
"""
import os, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

BASE = """
camera: {host: 10.0.0.5, http_port: 88, user: admin,
         main_path: videoMain, sub_path: videoSub}
detection: {source: sub, target_fps: 2, conf_threshold: 0.6,
            trigger_classes: [person, bicycle, car, motorbike, bus, truck]}
trigger: {quiet_period_s: 15, pre_roll_s: 2, post_roll_s: 15, max_duration_min: 10}
recording:
  segment_seconds: 60
  tiers:
    - {name: main, stream: main, path: recordings/main, max_age_days: 2}
    - {name: sub, stream: sub, path: recordings/sub, max_age_days: 60}
    - {name: events, stream: null, path: events, max_age_days: 730, protected: true}
retention: {target_free_percent: 20, check_interval_s: 300}
paths: {events_root: events, detections_root: detections}
web: {bind: 0.0.0.0, port: 8080, stream_url: null}
"""

PW = 'tr&ub=le some'   # deliberately awkward: '&', '=', space


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg_p = self.d / "config.yaml"
        self.sec_p = self.d / "secrets.yaml"
        self.cfg_p.write_text(BASE)
        self.sec_p.write_text(f'camera:\n  password: "{PW}"\n')
        self.sec_p.chmod(0o600)
        os.environ.pop("RKNN_CAMERA_PASSWORD", None)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, **kw):
        return config.load(self.cfg_p, self.sec_p, **kw)


class TestPassword(Base):
    def test_special_characters_are_url_encoded(self):
        url = self.load().rtsp_url("main")
        self.assertIn("tr%26ub%3Dle%20some", url,
                      "'=', ' ' and '&' must be percent-encoded in the RTSP URL")
        self.assertNotIn("tr&ub=le", url)

    def test_redacted_url_carries_no_password(self):
        cfg = self.load()
        red = cfg.rtsp_url("main", redacted=True)
        self.assertNotIn("trub", red)
        self.assertNotIn("%3D", red)
        self.assertIn("***", red)
        self.assertIn("10.0.0.5:88/videoMain", red)

    def test_password_is_not_in_the_parsed_tree(self):
        cfg = self.load()
        self.assertNotIn("password", cfg.raw.get("camera", {}),
                         "a stray dump of cfg.raw must not expose the password")
        self.assertNotIn(PW, repr(cfg.raw))

    def test_env_var_overrides_secrets_file(self):
        os.environ["RKNN_CAMERA_PASSWORD"] = "from-env"
        try:
            self.assertIn("from-env", self.load().rtsp_url("sub"))
        finally:
            del os.environ["RKNN_CAMERA_PASSWORD"]

    def test_missing_password_is_a_startup_error(self):
        self.sec_p.unlink()
        with self.assertRaises(config.ConfigError) as e:
            self.load()
        self.assertIn("password", str(e.exception).lower())

    def test_world_readable_secrets_are_refused(self):
        self.sec_p.chmod(0o644)
        with self.assertRaises(config.ConfigError) as e:
            self.load()
        self.assertIn("chmod 600", str(e.exception))


class TestStreams(Base):
    def test_main_and_sub_resolve_to_different_paths(self):
        cfg = self.load()
        self.assertTrue(cfg.rtsp_url("main").endswith("/videoMain"))
        self.assertTrue(cfg.rtsp_url("sub").endswith("/videoSub"))

    def test_detector_follows_the_configured_source(self):
        cfg = self.load()
        self.assertEqual(cfg.detection_rtsp, cfg.rtsp_url("sub"))
        self.cfg_p.write_text(BASE.replace("source: sub", "source: main"))
        self.assertEqual(self.load().detection_rtsp, self.load().rtsp_url("main"))


class TestTiers(Base):
    def test_order_is_preserved_as_sacrifice_order(self):
        self.assertEqual([t.name for t in self.load().tiers],
                         ["main", "sub", "events"])

    def test_only_stream_backed_tiers_get_a_recorder(self):
        self.assertEqual([t.name for t in self.load().recording_tiers],
                         ["main", "sub"])

    def test_events_tier_is_protected(self):
        self.assertTrue(self.load().tier("events").protected)

    def test_max_age_converts_to_seconds(self):
        self.assertEqual(self.load().tier("main").max_age_s, 2 * 86400)


class TestTriggerClasses(Base):
    def test_classes_are_stripped_for_comparison(self):
        # The whole point: yolov10.CLASSES has 'motorbike ', 'bus ', 'truck '
        cfg = self.load()
        self.assertEqual(cfg.trigger_classes,
                         {"person", "bicycle", "car", "motorbike", "bus", "truck"})
        for label in ("motorbike ", "bus ", "truck "):
            self.assertIn(label.strip(), cfg.trigger_classes)


class TestValidation(Base):
    def test_missing_section_names_the_setting(self):
        self.cfg_p.write_text(BASE.replace("retention: {target_free_percent: 20, check_interval_s: 300}", ""))
        with self.assertRaises(config.ConfigError) as e:
            self.load()
        self.assertIn("retention", str(e.exception))

    def test_absurd_free_target_is_rejected(self):
        self.cfg_p.write_text(BASE.replace("target_free_percent: 20",
                                           "target_free_percent: 140"))
        with self.assertRaises(config.ConfigError):
            self.load()

    def test_no_tiers_is_rejected(self):
        self.cfg_p.write_text(BASE[:BASE.index("  tiers:")] + "  tiers: []\n"
                              + BASE[BASE.index("retention:"):])
        with self.assertRaises(config.ConfigError) as e:
            self.load()
        self.assertIn("empty", str(e.exception))




class TestRollingBufferTiers(Base):
    """A rolling buffer is measured in minutes, not fractions of a day."""

    def test_max_age_minutes_is_supported(self):
        self.cfg_p.write_text(BASE.replace(
            "- {name: main, stream: main, path: recordings/main, max_age_days: 2}",
            "- {name: main, stream: main, path: recordings/main, max_age_minutes: 45}"))
        t = self.load().tier("main")
        self.assertEqual(t.max_age_s, 45 * 60)

    def test_minutes_win_over_days_if_both_are_given(self):
        self.cfg_p.write_text(BASE.replace(
            "- {name: main, stream: main, path: recordings/main, max_age_days: 2}",
            "- {name: main, stream: main, path: recordings/main, "
            "max_age_days: 2, max_age_minutes: 30}"))
        self.assertEqual(self.load().tier("main").max_age_s, 30 * 60)

    def test_a_tier_with_no_lifetime_is_rejected(self):
        self.cfg_p.write_text(BASE.replace(
            "- {name: main, stream: main, path: recordings/main, max_age_days: 2}",
            "- {name: main, stream: main, path: recordings/main}"))
        with self.assertRaises(config.ConfigError) as e:
            self.load()
        self.assertIn("never be pruned", str(e.exception))




class TestTheShippedTriggerList(unittest.TestCase):
    """config.yaml is the thing that runs, so assert against it, not a fixture.

    Every trigger class must exist in the model's own label list, stripped the
    same way the detector strips it. A typo here is silent: the class simply
    never fires, and nothing in the logs says so.
    """

    def setUp(self):
        self.cfg = config.load(
            config_path=str(ROOT / "config.yaml"), require_password=False)

    def test_car_is_a_trigger(self):
        self.assertIn("car", self.cfg.trigger_classes,
                      "vehicles arriving at the club are the point")

    def test_every_trigger_class_exists_in_the_model(self):
        # Read the tuple rather than importing it: yolov10 pulls in cv2 and
        # torch, which are on the board but not on a development machine, and
        # a check that only runs in one place is a check that gets skipped.
        import ast, re
        src = (ROOT / "yolov10.py").read_text(encoding="utf-8")
        m = re.search(r"^CLASSES\s*=\s*(\(.*?\))", src, re.S | re.M)
        self.assertIsNotNone(m, "CLASSES is not where this test expects it")
        known = {c.strip() for c in ast.literal_eval(m.group(1))}
        self.assertIn("person", known, "sanity: the list parsed correctly")
        unknown = sorted(self.cfg.trigger_classes - known)
        self.assertEqual(unknown, [],
                         "these can never fire: no such COCO label")


if __name__ == "__main__":
    unittest.main(verbosity=2)
