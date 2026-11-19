"""Only formats BOTH frame clients can decode may reach a manifest.

The browser kiosk decodes with the platform image decoder; the Pi Zero client
decodes with Pillow. HEIC/HEIF, TIFF, BMP and video are excluded: one
undecodable manifest entry used to block every subsequent update on both
clients. Upload-time conversion to a display derivative is Milestone B work.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from shared import auto_select, s3_browse, store  # noqa: E402

DISPLAYABLE = ["photos/a.jpg", "photos/b.JPEG", "photos/c.png", "photos/d.webp", "photos/e.gif"]
UNDISPLAYABLE = ["photos/f.heic", "photos/g.HEIF", "photos/h.tif", "photos/i.tiff", "photos/j.bmp",
                 "photos/k.mov", "photos/l.mp4", "photos/m.m4v", "photos/n.avi", "photos/o.mkv",
                 "photos/p.webm", "photos/q.csv", "photos/jpg", "photos/r.jpg.mov"]


class FakeS3:
    def __init__(self, keys):
        self.keys = keys

    def list_objects_v2(self, **kwargs):
        return {"Contents": [{"Key": k} for k in self.keys]}


class TestDisplayFormats(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"PHOTOS_PREFIX": "photos/"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_displayable_allowlist(self):
        for key in DISPLAYABLE + ["photos/ white space .jpg "]:
            with self.subTest(key=key):
                self.assertTrue(s3_browse.is_displayable_key(key))
        for key in UNDISPLAYABLE:
            with self.subTest(key=key):
                self.assertFalse(s3_browse.is_displayable_key(key))

    def test_auto_select_pool_excludes_undecodable_formats(self):
        pool = auto_select.list_media_keys(FakeS3(DISPLAYABLE + UNDISPLAYABLE), "bucket", "photos/")
        self.assertEqual(sorted(pool), sorted(DISPLAYABLE))

    def test_playlist_rejects_undecodable_items(self):
        playlist = store.validate_draft({"name": "ok", "settings": {}, "items": DISPLAYABLE})
        self.assertEqual(playlist["items"], DISPLAYABLE)
        for key in UNDISPLAYABLE:
            with self.subTest(key=key), self.assertRaises(ValueError):
                store.validate_draft({"name": "bad", "settings": {}, "items": ["photos/a.jpg", key]})


if __name__ == "__main__":
    unittest.main()
