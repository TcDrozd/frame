import os
import sys
import unittest

import boto3
from botocore.stub import Stubber

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shared import auto_select  # noqa: E402

PREFIX = "photos/"


def keys(collection: str, n: int) -> list[str]:
    return [f"photos/{collection}/{i:03}.jpg" for i in range(n)]


class TestSelectWindow(unittest.TestCase):
    def test_deterministic_within_a_day_and_listing_order(self):
        pool = keys("a", 30) + keys("b", 30)
        first = auto_select.select_window(pool, 10, prefix=PREFIX, day="2026-07-22")
        second = auto_select.select_window(list(reversed(pool)), 10, prefix=PREFIX, day="2026-07-22")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 10)

    def test_rotates_across_days(self):
        pool = keys("a", 200)
        d1 = auto_select.select_window(pool, 20, prefix=PREFIX, day="2026-07-22")
        d2 = auto_select.select_window(pool, 20, prefix=PREFIX, day="2026-07-23")
        self.assertNotEqual(d1, d2)

    def test_interleaves_collections(self):
        # A 9:1 pool must not produce a 9:1 window head: round-robin keeps the
        # small collection fully represented.
        pool = keys("big", 90) + keys("small", 10)
        out = auto_select.select_window(pool, 20, prefix=PREFIX, day="2026-07-22")
        small = sum(1 for k in out if k.startswith("photos/small/"))
        self.assertEqual(small, 10)
        self.assertEqual(len(out), 20)

    def test_window_larger_than_pool_returns_everything(self):
        pool = keys("a", 5)
        out = auto_select.select_window(pool, 50, prefix=PREFIX, day="2026-07-22")
        self.assertEqual(sorted(out), sorted(pool))

    def test_no_duplicates(self):
        pool = keys("a", 40) + keys("b", 3)
        out = auto_select.select_window(pool, 30, prefix=PREFIX, day="2026-07-22")
        self.assertEqual(len(out), len(set(out)))

    def test_files_at_prefix_root_are_included(self):
        pool = ["photos/loose.jpg"] + keys("a", 2)
        out = auto_select.select_window(pool, 10, prefix=PREFIX, day="2026-07-22")
        self.assertIn("photos/loose.jpg", out)
        self.assertEqual(len(out), 3)


class TestListMediaKeys(unittest.TestCase):
    def test_paginates_and_filters_non_media(self):
        s3 = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        with Stubber(s3) as stubber:
            stubber.add_response(
                "list_objects_v2",
                {
                    "Contents": [{"Key": "photos/a.jpg"}, {"Key": "photos/notes.txt"}],
                    "NextContinuationToken": "t",
                    "IsTruncated": True,
                },
                {"Bucket": "b", "Prefix": "photos/"},
            )
            stubber.add_response(
                "list_objects_v2",
                {"Contents": [{"Key": "photos/sub/c.png"}]},
                {"Bucket": "b", "Prefix": "photos/", "ContinuationToken": "t"},
            )
            result = auto_select.list_media_keys(s3, "b", "photos/")
        self.assertEqual(result, ["photos/a.jpg", "photos/sub/c.png"])


if __name__ == "__main__":
    unittest.main()
