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

"""Tests for the annotated companion clip."""
import sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from annotated import AnnotatedClip  # noqa: E402

# A tiny valid JPEG, so tests never need a camera or a real encoder input.
import io
try:
    import numpy as np, cv2
    _ok, _buf = cv2.imencode(".jpg", np.zeros((48, 64, 3), dtype=np.uint8))
    JPEG = _buf.tobytes()
    HAVE_CV2 = True
except Exception:                                    # pragma: no cover
    JPEG = b"\xff\xd8\xff\xdb" + b"\x00" * 100 + b"\xff\xd9"
    HAVE_CV2 = False


class TestBuffering(unittest.TestCase):
    def setUp(self):
        self.a = AnnotatedClip(fps=2, max_frames=5)

    def test_nothing_is_buffered_before_an_incident_opens(self):
        self.a.add(JPEG)
        self.assertEqual(self.a.count, 0,
                         "between incidents this must hold nothing at all")

    def test_frames_accumulate_once_open(self):
        self.a.start()
        for _ in range(3):
            self.a.add(JPEG)
        self.assertEqual(self.a.count, 3)

    def test_the_cap_stops_a_stuck_incident_eating_memory(self):
        self.a.start()
        for _ in range(50):
            self.a.add(JPEG)
        self.assertEqual(self.a.count, 5)
        self.assertEqual(self.a.dropped, 45)

    def test_discard_empties_it(self):
        self.a.start()
        self.a.add(JPEG)
        self.a.discard()
        self.assertEqual(self.a.count, 0)

    def test_a_new_incident_starts_empty(self):
        self.a.start()
        self.a.add(JPEG)
        self.a.start()
        self.assertEqual(self.a.count, 0,
                         "the previous incident must not bleed into the next")

    def test_empty_frames_are_ignored(self):
        self.a.start()
        self.a.add(None)
        self.a.add(b"")
        self.assertEqual(self.a.count, 0)


class TestWriting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "sub" / "clip.annotated.mp4"

    def tearDown(self):
        self.tmp.cleanup()

    def test_writing_nothing_produces_nothing(self):
        a = AnnotatedClip()
        a.start()
        self.assertIsNone(a.write(self.out))
        self.assertFalse(self.out.exists())

    @unittest.skipUnless(HAVE_CV2, "needs cv2 to make a real jpeg")
    def test_it_writes_a_playable_file(self):
        import shutil
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg not on PATH")
        a = AnnotatedClip(fps=2)
        a.start()
        for _ in range(6):
            a.add(JPEG)
        path = a.write(self.out)
        self.assertIsNotNone(path, "ffmpeg should have produced a clip")
        self.assertTrue(self.out.exists())
        self.assertGreater(self.out.stat().st_size, 200)

    def test_write_empties_the_buffer(self):
        a = AnnotatedClip()
        a.start()
        a.add(JPEG)
        a.write(self.out)
        self.assertEqual(a.count, 0)

    def test_a_missing_ffmpeg_is_survivable(self):
        a = AnnotatedClip()
        a._frames = [JPEG]
        a._open = True
        import annotated as mod
        real = mod.subprocess.Popen
        mod.subprocess.Popen = lambda *x, **k: (_ for _ in ()).throw(FileNotFoundError())
        try:
            self.assertIsNone(a.write(self.out))
        finally:
            mod.subprocess.Popen = real


if __name__ == "__main__":
    unittest.main(verbosity=2)
