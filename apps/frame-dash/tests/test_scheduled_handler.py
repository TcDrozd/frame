"""The scheduled handler's mode gating: curated refresh vs auto window vs no-op.

publish_playlist/publish_auto themselves are covered in test_publishing.py;
here they are mocked and only the decision logic is under test.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduled import handler  # noqa: E402

ENV = {
    "PHOTOS_BUCKET": "b",
    "PHOTOS_PREFIX": "photos/",
    "MANIFEST_KEY": "manifest.json",
    "PLAYLIST_TABLE": "t",
}

AUTO_RESULT = {"version": "v1", "photo_count": 5, "pool_count": 9, "skipped_keys": []}
CURATED_RESULT = {"version": "v2", "photo_count": 3, "skipped_keys": []}


class TestScheduledHandler(unittest.TestCase):
    def run_handler(self, env, *, active, playlist=None):
        with patch.dict(os.environ, {**ENV, **env}, clear=False), \
                patch.object(handler.boto3, "client", MagicMock()), \
                patch.object(handler.boto3, "resource", MagicMock()), \
                patch.object(handler.store, "get_active", return_value=active), \
                patch.object(handler.store, "get_playlist", return_value=playlist), \
                patch.object(handler.store, "clear_active") as clear, \
                patch.object(handler.publishing, "publish_playlist", return_value=CURATED_RESULT) as curated, \
                patch.object(handler.publishing, "publish_auto", return_value=AUTO_RESULT) as auto:
            result = handler.lambda_handler({}, None)
        return result, curated, auto, clear

    def test_off_and_no_active_is_noop(self):
        result, curated, auto, _ = self.run_handler({"AUTO_PUBLISH_MODE": "off"}, active=None)
        self.assertFalse(result["published"])
        curated.assert_not_called()
        auto.assert_not_called()

    def test_window_publishes_auto_when_no_active(self):
        result, _, auto, _ = self.run_handler({"AUTO_PUBLISH_MODE": "window"}, active=None)
        self.assertTrue(result["published"])
        self.assertEqual(result["source"], "auto")
        self.assertIsNone(auto.call_args.kwargs["resolved_start_epoch"])

    def test_window_reuses_frozen_auto_epoch(self):
        active = {"source": "auto", "resolved_start_epoch": 1234}
        _, _, auto, _ = self.run_handler({"AUTO_PUBLISH_MODE": "window"}, active=active)
        self.assertEqual(auto.call_args.kwargs["resolved_start_epoch"], 1234)

    def test_curated_playlist_takes_precedence_over_auto(self):
        active = {"playlist_id": "p1", "resolved_start_epoch": 99}
        playlist = {"id": "p1", "name": "n", "items": ["photos/a.jpg"]}
        result, curated, auto, _ = self.run_handler(
            {"AUTO_PUBLISH_MODE": "window"}, active=active, playlist=playlist
        )
        self.assertTrue(result["published"])
        self.assertNotIn("source", result)
        self.assertEqual(curated.call_args.kwargs["resolved_start_epoch"], 99)
        auto.assert_not_called()

    def test_missing_playlist_falls_through_to_auto_with_fresh_epoch(self):
        active = {"playlist_id": "gone", "resolved_start_epoch": 99}
        result, _, auto, clear = self.run_handler(
            {"AUTO_PUBLISH_MODE": "window"}, active=active, playlist=None
        )
        clear.assert_called_once()
        self.assertTrue(result["published"])
        self.assertEqual(result["source"], "auto")
        # The stale curated epoch must not leak into the auto show.
        self.assertIsNone(auto.call_args.kwargs["resolved_start_epoch"])

    def test_missing_playlist_with_auto_off_clears_and_noops(self):
        active = {"playlist_id": "gone"}
        result, _, auto, clear = self.run_handler(
            {"AUTO_PUBLISH_MODE": "off"}, active=active, playlist=None
        )
        clear.assert_called_once()
        self.assertFalse(result["published"])
        auto.assert_not_called()


if __name__ == "__main__":
    unittest.main()
