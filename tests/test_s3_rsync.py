import tempfile
import unittest
from pathlib import Path
from unittest import mock

import boto3
from botocore.stub import Stubber

from tools import s3_rsync


class TestS3Rsync(unittest.TestCase):
    def test_nested_path_key_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "photos"
            nested = source / "2025" / "trip"
            nested.mkdir(parents=True)
            file_path = nested / "img.jpg"
            file_path.write_bytes(b"abc")

            rel = s3_rsync.to_rel_posix(file_path, source)
            dest = s3_rsync.parse_s3_uri("s3://bucket/photos/2025")
            key = dest.key_for(rel)

            self.assertEqual(rel, "2025/trip/img.jpg")
            self.assertEqual(key, "photos/2025/2025/trip/img.jpg")

    def test_cache_read_write_and_hash_cache_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / ".cache.json"
            file_path = Path(tmp) / "a.jpg"
            file_path.write_bytes(b"content")
            st = file_path.stat()

            cache = s3_rsync.ShaCache(cache_path)
            cache.set_sha(file_path, st.st_size, int(st.st_mtime), "abc123")
            cache.save()

            cache2 = s3_rsync.ShaCache(cache_path)
            self.assertEqual(
                cache2.get_sha(file_path, st.st_size, int(st.st_mtime)),
                "abc123",
            )
            self.assertIsNone(cache2.get_sha(file_path, st.st_size + 1, int(st.st_mtime)))

    def test_get_local_sha_uses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / ".cache.json"
            file_path = Path(tmp) / "a.jpg"
            file_path.write_bytes(b"content")
            st = file_path.stat()
            file_obj = s3_rsync.LocalFile(
                path=file_path,
                rel_posix="a.jpg",
                size=st.st_size,
                mtime=int(st.st_mtime),
            )
            cache = s3_rsync.ShaCache(cache_path)
            cache.set_sha(file_path, st.st_size, int(st.st_mtime), "cachedsha")

            with mock.patch.object(s3_rsync, "compute_sha256") as mocked_hash:
                sha = s3_rsync.get_local_sha(cache, file_obj)
                self.assertEqual(sha, "cachedsha")
                mocked_hash.assert_not_called()

    def test_head_object_404_with_stubber(self) -> None:
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="x",
            aws_secret_access_key="y",
        )
        with Stubber(client) as stubber:
            stubber.add_client_error(
                "head_object",
                service_error_code="404",
                service_message="Not Found",
                http_status_code=404,
                expected_params={"Bucket": "my-bucket", "Key": "photos/a.jpg"},
            )
            exists, metadata = s3_rsync.head_object_metadata(client, "my-bucket", "photos/a.jpg")

        self.assertFalse(exists)
        self.assertIsNone(metadata)

    def test_upload_decision_with_remote_metadata(self) -> None:
        file_obj = s3_rsync.LocalFile(
            path=Path("/tmp/a.jpg"),
            rel_posix="a.jpg",
            size=100,
            mtime=1700000000,
        )

        needs_upload, reason = s3_rsync.choose_upload_action(
            remote_exists=True,
            remote_metadata={"sha256": "abc"},
            file_obj=file_obj,
            local_sha="abc",
        )
        self.assertFalse(needs_upload)
        self.assertEqual(reason, "remote sha256 match")

        needs_upload2, reason2 = s3_rsync.choose_upload_action(
            remote_exists=True,
            remote_metadata={"size": "100", "mtime": "1700000000"},
            file_obj=file_obj,
            local_sha=None,
        )
        self.assertFalse(needs_upload2)
        self.assertEqual(reason2, "remote size+mtime match")

        needs_upload3, reason3 = s3_rsync.choose_upload_action(
            remote_exists=False,
            remote_metadata=None,
            file_obj=file_obj,
            local_sha=None,
        )
        self.assertTrue(needs_upload3)
        self.assertEqual(reason3, "remote missing")


if __name__ == "__main__":
    unittest.main()
