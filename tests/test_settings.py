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

"""Tests for the panel-editable settings.

This is the one screen that can stop the system recording anything at all, so
the failure modes matter more than the happy path: a corrupt file, a class the
model cannot emit, an empty selection.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from settings import Settings  # noqa: E402

DEFAULTS = {"trigger_classes": ["person", "bicycle", "car"]}
KNOWN = ("person", "bicycle", "car", "motorbike ", "bus ", "truck ", "dog")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "settings.json"

    def store(self):
        return Settings(self.path, defaults=DEFAULTS)


class TestFallingBackToTheConfig(Base):
    def test_no_file_means_the_config_applies(self):
        self.assertEqual(self.store().trigger_classes,
                         {"person", "bicycle", "car"})

    def test_nothing_is_written_just_by_reading(self):
        self.store()
        self.assertFalse(self.path.exists(),
                         "a read must not create an override")

    def test_a_corrupt_file_falls_back_rather_than_raising(self):
        self.path.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(self.store().trigger_classes,
                         {"person", "bicycle", "car"},
                         "a bad file must not take the detector down")

    def test_a_json_list_at_the_top_level_is_ignored(self):
        self.path.write_text('["person"]', encoding="utf-8")
        self.assertEqual(self.store().trigger_classes,
                         {"person", "bicycle", "car"})

    def test_a_non_list_value_falls_back(self):
        self.path.write_text('{"trigger_classes": "person"}', encoding="utf-8")
        self.assertEqual(self.store().trigger_classes,
                         {"person", "bicycle", "car"},
                         "a string is not a class list")


class TestSaving(Base):
    def test_a_saved_set_survives_a_reload(self):
        self.store().set_trigger_classes(["person", "dog"], known=KNOWN)
        self.assertEqual(self.store().trigger_classes, {"person", "dog"})

    def test_labels_are_stripped_like_the_model_emits_them(self):
        s = self.store()
        s.set_trigger_classes(["  truck  ", "bus"], known=KNOWN)
        self.assertEqual(s.trigger_classes, {"truck", "bus"},
                         "the COCO list has trailing spaces on several names")

    def test_a_class_the_model_cannot_emit_is_dropped(self):
        s = self.store()
        kept = s.set_trigger_classes(["person", "unicorn"], known=KNOWN)
        self.assertEqual(kept, {"person"})
        self.assertEqual(s.trigger_classes, {"person"},
                         "saving it would fail silently: it can never fire")

    def test_an_empty_selection_is_honoured_not_ignored(self):
        # Ticking nothing is a legitimate, if drastic, choice. Quietly
        # substituting the default would be worse than doing as asked -- the
        # page warns about it instead.
        s = self.store()
        s.set_trigger_classes([], known=KNOWN)
        self.assertEqual(s.trigger_classes, set())
        self.assertTrue(s.overridden("trigger_classes"))

    def test_the_file_is_valid_json_on_disk(self):
        self.store().set_trigger_classes(["car"], known=KNOWN)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"trigger_classes": ["car"]})

    def test_no_temp_files_are_left_behind(self):
        self.store().set_trigger_classes(["car"], known=KNOWN)
        leftovers = [p.name for p in self.tmp.iterdir()
                     if p.name != "settings.json"]
        self.assertEqual(leftovers, [], "the atomic write must clean up")

    def test_the_directory_is_created_if_missing(self):
        deep = self.tmp / "a" / "b" / "settings.json"
        Settings(deep, defaults=DEFAULTS).set_trigger_classes(["car"],
                                                              known=KNOWN)
        self.assertTrue(deep.exists())


class TestResetting(Base):
    def test_clearing_restores_the_config_value(self):
        s = self.store()
        s.set_trigger_classes(["dog"], known=KNOWN)
        self.assertTrue(s.overridden("trigger_classes"))
        s.clear("trigger_classes")
        self.assertFalse(s.overridden("trigger_classes"))
        self.assertEqual(s.trigger_classes, {"person", "bicycle", "car"})

    def test_clearing_something_never_set_is_harmless(self):
        s = self.store()
        s.clear("trigger_classes")
        self.assertEqual(s.trigger_classes, {"person", "bicycle", "car"})


class TestTheLiveReadPath(Base):
    """The detector re-reads per frame, so a change must be visible at once."""

    def test_a_second_reader_sees_a_change_after_reload(self):
        writer = self.store()
        reader = self.store()
        writer.set_trigger_classes(["dog"], known=KNOWN)
        self.assertEqual(reader.reload().trigger_classes, {"dog"})

    def test_the_same_object_reflects_its_own_write_immediately(self):
        s = self.store()
        s.set_trigger_classes(["dog"], known=KNOWN)
        self.assertEqual(s.trigger_classes, {"dog"},
                         "no reload should be needed on the writing side")




class TestCredentials(Base):
    def test_no_credentials_by_default(self):
        self.assertFalse(self.store().has_credentials())

    def test_the_password_is_never_stored_in_the_clear(self):
        s = self.store()
        s.set_web_credentials("tvw", "correct horse battery")
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("correct horse battery", raw)
        rec = json.loads(raw)["web_password"]
        self.assertEqual(rec["algo"], "pbkdf2_sha256")
        self.assertEqual(len(rec["salt"]), 32)
        self.assertGreaterEqual(rec["iterations"], 100000)

    def test_it_verifies_the_right_password_and_only_that_one(self):
        s = self.store()
        s.set_web_credentials("tvw", "correct horse battery")
        self.assertTrue(s.check_password("correct horse battery"))
        for wrong in ("Correct horse battery", "correct horse batter", "", " "):
            self.assertFalse(s.check_password(wrong), repr(wrong))

    def test_two_identical_passwords_get_different_hashes(self):
        a, b = self.store(), Settings(self.tmp / "b.json", defaults=DEFAULTS)
        a.set_web_credentials("tvw", "same password")
        b.set_web_credentials("tvw", "same password")
        self.assertNotEqual(a.get("web_password")["hash"],
                            b.get("web_password")["hash"],
                            "each record must carry its own salt")

    def test_a_short_password_is_refused(self):
        s = self.store()
        with self.assertRaises(ValueError):
            s.set_web_credentials("tvw", "short")
        self.assertFalse(s.has_credentials(),
                         "a refused change must not half-apply")

    def test_an_empty_user_is_refused(self):
        with self.assertRaises(ValueError):
            self.store().set_web_credentials("   ", "long enough password")

    def test_credentials_survive_a_reload(self):
        self.store().set_web_credentials("club", "long enough password")
        s2 = self.store()
        self.assertEqual(s2.web_user, "club")
        self.assertTrue(s2.check_password("long enough password"))

    def test_clearing_falls_back_to_the_configured_login(self):
        s = self.store()
        s.set_web_credentials("club", "long enough password")
        s.clear_web_credentials()
        self.assertFalse(s.has_credentials())
        self.assertEqual(s.web_user, "")

    def test_clearing_leaves_the_other_settings_alone(self):
        s = self.store()
        s.set_trigger_classes(["dog"], known=KNOWN)
        s.set_web_credentials("club", "long enough password")
        s.clear_web_credentials()
        self.assertEqual(s.trigger_classes, {"dog"},
                         "resetting the login must not reset the categories")

    def test_a_malformed_record_refuses_rather_than_raising(self):
        self.path.write_text('{"web_user":"x","web_password":{"algo":"pbkdf2_sha256"}}',
                             encoding="utf-8")
        s = self.store()
        self.assertFalse(s.check_password("anything"),
                         "a broken record must never authenticate anyone")

    def test_a_plaintext_record_is_not_accepted(self):
        # Someone hand-editing the file might try this.
        self.path.write_text('{"web_user":"x","web_password":"hunter2"}',
                             encoding="utf-8")
        self.assertFalse(self.store().check_password("hunter2"))




class TestAccessModes(Base):
    def test_no_mode_set_means_the_config_decides(self):
        self.assertEqual(self.store().auth_mode, "")

    def test_open_and_password_need_no_networks(self):
        s = self.store()
        s.set_auth_mode("open")
        self.assertEqual(s.auth_mode, "open")
        s.set_auth_mode("password")
        self.assertEqual(s.auth_mode, "password")

    def test_trusted_without_a_network_is_refused(self):
        s = self.store()
        with self.assertRaises(ValueError):
            s.set_auth_mode("trusted", "")
        self.assertEqual(s.auth_mode, "",
                         "a trusted mode that trusts nothing is a lockout")

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            self.store().set_auth_mode("whatever")

    def test_a_bad_network_is_refused_before_anything_is_stored(self):
        s = self.store()
        with self.assertRaises(ValueError):
            s.set_auth_mode("trusted", "192.168.1.0/24, not-an-address")
        self.assertEqual(s.auth_mode, "")

    def test_matching_inside_and_outside_the_network(self):
        s = self.store()
        s.set_auth_mode("trusted", "192.168.90.0/24")
        self.assertTrue(s.is_trusted("192.168.90.1"))
        self.assertTrue(s.is_trusted("192.168.90.255"))
        self.assertFalse(s.is_trusted("192.168.91.1"))

    def test_a_bare_address_means_that_host_alone(self):
        s = self.store()
        s.set_auth_mode("trusted", "10.0.0.7")
        self.assertTrue(s.is_trusted("10.0.0.7"))
        self.assertFalse(s.is_trusted("10.0.0.8"))

    def test_several_networks(self):
        s = self.store()
        s.set_auth_mode("trusted", "192.168.90.0/24 10.0.0.0/8")
        self.assertTrue(s.is_trusted("192.168.90.5"))
        self.assertTrue(s.is_trusted("10.9.9.9"))
        self.assertFalse(s.is_trusted("172.16.0.1"))

    def test_rubbish_client_addresses_are_never_trusted(self):
        s = self.store()
        s.set_auth_mode("trusted", "0.0.0.0/0")
        for bad in ("", None, "garbage", "999.1.1.1", "1.2.3.4.5"):
            self.assertFalse(s.is_trusted(bad), repr(bad))

    def test_ipv6_does_not_match_an_ipv4_network(self):
        s = self.store()
        s.set_auth_mode("trusted", "192.168.90.0/24")
        self.assertFalse(s.is_trusted("::1"))

    def test_a_host_bit_set_is_accepted_as_the_network(self):
        s = self.store()
        s.set_auth_mode("trusted", "192.168.90.131/24")
        self.assertTrue(s.is_trusted("192.168.90.4"),
                        "typing your own address with a prefix is what people do")

    def test_unusable_stored_networks_trust_nobody(self):
        self.path.write_text(
            '{"web_auth_mode":"trusted","web_trusted_networks":["nonsense"]}',
            encoding="utf-8")
        s = self.store()
        self.assertEqual(s.trusted_networks, [])
        self.assertFalse(s.is_trusted("192.168.90.1"),
                         "fail closed, not open")

    def test_clearing_access_leaves_the_login_alone(self):
        s = self.store()
        s.set_web_credentials("club", "long enough password")
        s.set_auth_mode("open")
        s.clear_access()
        self.assertEqual(s.auth_mode, "")
        self.assertTrue(s.has_credentials(),
                        "resetting access must not reset the password")




class TestNoticingOutsideEdits(Base):
    """The rescue CLI writes this file while the service is running.

    A change nobody notices until the next restart is not much of a rescue, so
    reads re-stat the file and reload when it has moved underneath them.
    """

    def setUp(self):
        super().setUp()
        self.now = 1000.0

    def store(self):
        return Settings(self.path, defaults=DEFAULTS, clock=lambda: self.now)

    def test_an_edit_by_someone_else_is_picked_up(self):
        live = self.store()
        self.assertEqual(live.trigger_classes, {"person", "bicycle", "car"})

        rescue = self.store()                 # stands in for settings_cli.py
        rescue.set_trigger_classes(["dog"], known=KNOWN)

        self.now += Settings.POLL_S + 0.1
        self.assertEqual(live.trigger_classes, {"dog"},
                         "the running service must see the rescue")

    def test_a_password_set_from_the_shell_is_picked_up(self):
        live = self.store()
        self.store().set_web_credentials("club", "long enough password")
        self.now += Settings.POLL_S + 0.1
        self.assertTrue(live.check_password("long enough password"))
        self.assertEqual(live.web_user, "club")

    def test_it_does_not_stat_on_every_single_read(self):
        live = self.store()
        self.store().set_trigger_classes(["dog"], known=KNOWN)
        # No time has passed, so the poll interval has not elapsed.
        self.assertEqual(live.trigger_classes, {"person", "bicycle", "car"},
                         "reads are throttled; the detector calls this "
                         "several times a second")

    def test_the_revision_moves_only_when_something_changed(self):
        live = self.store()
        before = live.revision
        self.now += Settings.POLL_S + 0.1
        live.trigger_classes                       # a read, nothing changed
        self.assertEqual(live.revision, before)

        self.store().set_trigger_classes(["dog"], known=KNOWN)
        self.now += Settings.POLL_S + 0.1
        live.trigger_classes
        self.assertGreater(live.revision, before,
                           "callers caching a derived decision need this")

    def test_deleting_the_file_falls_back_to_the_config(self):
        live = self.store()
        live.set_trigger_classes(["dog"], known=KNOWN)
        self.path.unlink()
        self.now += Settings.POLL_S + 0.1
        self.assertEqual(live.trigger_classes, {"person", "bicycle", "car"},
                         "rm settings.json is the bluntest rescue and must work")

    def test_our_own_write_does_not_look_like_someone_elses(self):
        live = self.store()
        live.set_trigger_classes(["dog"], known=KNOWN)
        rev = live.revision
        self.now += Settings.POLL_S + 0.1
        self.assertEqual(live.trigger_classes, {"dog"})
        self.assertEqual(live.revision, rev,
                         "writing should not trigger a reload of our own data")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSweepSettings(Base):
    def test_defaults_off_and_unset(self):
        s = self.store()
        self.assertFalse(s.sweep_enabled)
        self.assertFalse(s.sweep_ready)
        self.assertEqual(s.sweep_dwell_s, 4.0)

    def test_ready_needs_both_ends(self):
        s = self.store()
        s.mark_sweep_saved("left")
        self.assertFalse(s.sweep_ready)
        s.mark_sweep_saved("right")
        self.assertTrue(s.sweep_ready)

    def test_dwell_is_clamped(self):
        s = self.store()
        s.set_sweep_dwell_s(0.1)
        self.assertGreaterEqual(s.sweep_dwell_s, 0.5)
        s.set_sweep_dwell_s(999)
        self.assertLessEqual(s.sweep_dwell_s, 60.0)

    def test_bad_side_raises(self):
        with self.assertRaises(ValueError):
            self.store().mark_sweep_saved("sideways")

    def test_persists_across_reload(self):
        s = self.store()
        s.set_sweep_enabled(True)
        s.mark_sweep_saved("left")
        s.mark_sweep_saved("right")
        again = self.store()          # fresh instance, same file
        self.assertTrue(again.sweep_enabled)
        self.assertTrue(again.sweep_ready)
