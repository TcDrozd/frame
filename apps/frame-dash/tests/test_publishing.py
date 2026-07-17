import os
import sys
import unittest
from unittest.mock import patch

import boto3
from botocore.stub import Stubber

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shared import publishing  # noqa: E402


def make_s3():
    return boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


PLAYLIST = {
    "id": "a" * 32,
    "name": "test",
    "items": ["photos/a.jpg"],
    "settings": {"mode": "sync", "slide_seconds": 60, "start_epoch": "now"},
}


class TestPublishPlaylist(unittest.TestCase):
    def publish(self, env, **kwargs):
        s3 = make_s3()
        with Stubber(s3) as stubber:
            stubber.add_response(
                "head_object",
                {"ContentLength": 1, "ETag": '"e"'},
                {"Bucket": "b", "Key": "photos/a.jpg"},
            )
            with patch.dict(os.environ, env, clear=False), patch.object(
                publishing.manifest_mod, "write_manifest"
            ) as write, patch.object(publishing.store, "record_publish"):
                publishing.publish_playlist(
                    s3,
                    table=None,
                    bucket="b",
                    manifest_key="m/abc/manifest.json",
                    playlist=PLAYLIST,
                    expires=3600,
                    **kwargs,
                )
        return write

    def test_single_write_without_legacy_key(self):
        write = self.publish({"LEGACY_MANIFEST_KEY": "", "PHOTO_URL_MODE": "presign"})
        self.assertEqual([c.args[2] for c in write.call_args_list], ["m/abc/manifest.json"])

    def test_dual_write_with_legacy_key(self):
        write = self.publish({"LEGACY_MANIFEST_KEY": "manifest.json", "PHOTO_URL_MODE": "presign"})
        self.assertEqual(
            [c.args[2] for c in write.call_args_list],
            ["m/abc/manifest.json", "manifest.json"],
        )
        # Same rendered doc goes to both keys.
        docs = [c.args[3] for c in write.call_args_list]
        self.assertIs(docs[0], docs[1])

    def test_dry_run_never_writes(self):
        s3 = make_s3()
        with Stubber(s3) as stubber:
            stubber.add_response(
                "head_object",
                {"ContentLength": 1, "ETag": '"e"'},
                {"Bucket": "b", "Key": "photos/a.jpg"},
            )
            with patch.dict(
                os.environ, {"LEGACY_MANIFEST_KEY": "manifest.json", "PHOTO_URL_MODE": "presign"}
            ), patch.object(publishing.manifest_mod, "write_manifest") as write:
                publishing.publish_playlist(
                    s3,
                    table=None,
                    bucket="b",
                    manifest_key="m/abc/manifest.json",
                    playlist=PLAYLIST,
                    expires=3600,
                    dry_run=True,
                )
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
