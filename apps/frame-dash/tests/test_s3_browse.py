import os
import sys
import unittest
from datetime import datetime, timezone

import boto3
from botocore.stub import Stubber

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shared import s3_browse  # noqa: E402


def make_s3():
    return boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


class TestParentPrefix(unittest.TestCase):
    def test_nested(self):
        self.assertEqual(s3_browse.parent_prefix("photos/2025/trip/"), "photos/2025/")

    def test_top_level(self):
        self.assertEqual(s3_browse.parent_prefix("photos/"), "")

    def test_root(self):
        self.assertEqual(s3_browse.parent_prefix(""), "")


class TestBrowse(unittest.TestCase):
    def test_folders_photos_and_pagination(self):
        s3 = make_s3()
        ts = datetime(2026, 6, 1, tzinfo=timezone.utc)
        with Stubber(s3) as stubber:
            stubber.add_response(
                "list_objects_v2",
                {
                    "CommonPrefixes": [
                        {"Prefix": "photos/zoo/"},
                        {"Prefix": "photos/beach/"},
                    ],
                    "Contents": [
                        {"Key": "photos/", "Size": 0, "LastModified": ts},
                        {"Key": "photos/IMG_1.jpg", "Size": 100, "LastModified": ts},
                        {"Key": "photos/notes.txt", "Size": 5, "LastModified": ts},
                    ],
                    "NextContinuationToken": "tok123",
                    "IsTruncated": True,
                },
                {
                    "Bucket": "b",
                    "Prefix": "photos/",
                    "Delimiter": "/",
                    "MaxKeys": 100,
                },
            )
            result = s3_browse.browse(s3, "b", "photos/")

        self.assertEqual(result["folders"], ["photos/beach/", "photos/zoo/"])
        self.assertEqual(len(result["photos"]), 1)  # prefix itself + .txt filtered out
        self.assertEqual(result["photos"][0]["key"], "photos/IMG_1.jpg")
        self.assertEqual(result["photos"][0]["last_modified"], "2026-06-01T00:00:00+00:00")
        self.assertEqual(result["next_token"], "tok123")
        self.assertEqual(result["parent_prefix"], "")

    def test_continuation_token_passed(self):
        s3 = make_s3()
        with Stubber(s3) as stubber:
            stubber.add_response(
                "list_objects_v2",
                {},
                {
                    "Bucket": "b",
                    "Prefix": "photos/",
                    "Delimiter": "/",
                    "MaxKeys": 100,
                    "ContinuationToken": "tok123",
                },
            )
            result = s3_browse.browse(s3, "b", "photos/", continuation_token="tok123")
        self.assertEqual(result["photos"], [])
        self.assertIsNone(result["next_token"])


class TestPresign(unittest.TestCase):
    def test_presign_many(self):
        s3 = make_s3()
        urls = s3_browse.presign_get_many(s3, "b", ["photos/a.jpg", "photos/b.jpg"])
        self.assertEqual(set(urls.keys()), {"photos/a.jpg", "photos/b.jpg"})
        for key, url in urls.items():
            self.assertIn(key, url)


if __name__ == "__main__":
    unittest.main()
