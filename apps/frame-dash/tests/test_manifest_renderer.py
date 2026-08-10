import os
import re
import sys
import unittest
from datetime import datetime, timezone

import boto3
from botocore.stub import Stubber

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shared import manifest  # noqa: E402


def make_s3():
    return boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


def stub_head(stubber, bucket, key, size, etag='"abc123"'):
    stubber.add_response(
        "head_object",
        {"ContentLength": size, "ContentType": "image/jpeg", "ETag": etag},
        {"Bucket": bucket, "Key": key},
    )


class TestRenderManifest(unittest.TestCase):
    BUCKET = "test-bucket"

    def render(self, keys, *, mode="sync", start_epoch=1750000000, **kwargs):
        s3 = make_s3()
        with Stubber(s3) as stubber:
            for key in keys:
                stub_head(stubber, self.BUCKET, key, 1234)
            return manifest.render_manifest(
                s3,
                self.BUCKET,
                keys,
                mode=mode,
                slide_seconds=900,
                start_epoch=start_epoch,
                expires=3600,
                **kwargs,
            )

    # Schema 2 is a strict superset of schema 1: every schema-1 field keeps
    # its exact name and type (the v1 client reads them and ignores the rest).
    SCHEMA1_TOP = {"schema", "version", "generated_at", "mode", "slide_seconds", "photos", "start_epoch"}
    SCHEMA1_PHOTO = {"id", "url", "name", "bytes"}

    def test_contract_shape_sync(self):
        result, skipped = self.render(["photos/trip/IMG_0001.jpg"])
        self.assertEqual(skipped, [])
        self.assertEqual(set(result.keys()), self.SCHEMA1_TOP | {"url_expires_at"})
        self.assertEqual(result["schema"], 2)
        self.assertEqual(result["mode"], "sync")
        self.assertEqual(result["slide_seconds"], 900)
        self.assertEqual(result["start_epoch"], 1750000000)

    def test_contract_shape_inventory_has_no_start_epoch(self):
        result, _ = self.render(["photos/a.jpg"], mode="inventory", start_epoch=None)
        self.assertEqual(
            set(result.keys()),
            (self.SCHEMA1_TOP | {"url_expires_at"}) - {"start_epoch"},
        )

    def test_photo_entry_fields(self):
        result, _ = self.render(["photos/2025/trip/Beach_Day_001.jpg"])
        (photo,) = result["photos"]
        self.assertEqual(set(photo.keys()), self.SCHEMA1_PHOTO | {"key", "etag"})
        self.assertEqual(photo["id"], "Beach_Day_001.jpg")
        self.assertEqual(photo["name"], "Beach Day 001")
        self.assertEqual(photo["bytes"], 1234)
        self.assertEqual(photo["key"], "photos/2025/trip/Beach_Day_001.jpg")
        self.assertEqual(photo["etag"], "abc123")  # quotes stripped
        self.assertIn("test-bucket", photo["url"])
        self.assertIn("photos/2025/trip/Beach_Day_001.jpg", photo["url"])

    def test_url_expires_at_derived_from_presign_expiry(self):
        before = int(datetime.now(timezone.utc).timestamp())
        result, _ = self.render(["photos/a.jpg"])
        after = int(datetime.now(timezone.utc).timestamp())
        self.assertTrue(before + 3600 <= result["url_expires_at"] <= after + 3600)

    def test_url_for_override_used_instead_of_presign(self):
        result, _ = self.render(
            ["photos/a.jpg"],
            url_for=lambda key: f"https://cdn.example/{key}?sig=x",
            url_expires_at=1760000000,
        )
        (photo,) = result["photos"]
        self.assertEqual(photo["url"], "https://cdn.example/photos/a.jpg?sig=x")
        self.assertEqual(result["url_expires_at"], 1760000000)

    def test_order_preserved(self):
        keys = ["photos/c.jpg", "photos/a.jpg", "photos/b.jpg"]
        result, _ = self.render(keys)
        self.assertEqual([p["id"] for p in result["photos"]], ["c.jpg", "a.jpg", "b.jpg"])

    def test_missing_key_skipped_not_fatal(self):
        s3 = make_s3()
        with Stubber(s3) as stubber:
            stub_head(stubber, self.BUCKET, "photos/exists.jpg", 10)
            stubber.add_client_error(
                "head_object",
                service_error_code="404",
                http_status_code=404,
                expected_params={"Bucket": self.BUCKET, "Key": "photos/gone.jpg"},
            )
            result, skipped = manifest.render_manifest(
                s3,
                self.BUCKET,
                ["photos/exists.jpg", "photos/gone.jpg"],
                mode="inventory",
                slide_seconds=60,
                expires=3600,
            )
        self.assertEqual(skipped, ["photos/gone.jpg"])
        self.assertEqual([p["id"] for p in result["photos"]], ["exists.jpg"])

    def test_version_format(self):
        result, _ = self.render(["photos/a.jpg"])
        self.assertRegex(result["version"], r"^v\d{8}-\d{6}Z$")

    def test_explicit_version_passthrough(self):
        result, _ = self.render(["photos/a.jpg"], version="v-custom")
        self.assertEqual(result["version"], "v-custom")

    def test_generated_at_is_iso_z_no_microseconds(self):
        result, _ = self.render(["photos/a.jpg"])
        self.assertRegex(result["generated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        datetime.strptime(result["generated_at"], "%Y-%m-%dT%H:%M:%SZ")

    def test_sync_requires_start_epoch(self):
        with self.assertRaises(ValueError):
            self.render(["photos/a.jpg"], start_epoch=None)

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            self.render(["photos/a.jpg"], mode="party")


class TestHelpers(unittest.TestCase):
    def test_resolve_start_epoch_now(self):
        before = int(datetime.now(timezone.utc).timestamp())
        resolved = manifest.resolve_start_epoch("now")
        after = int(datetime.now(timezone.utc).timestamp())
        self.assertTrue(before <= resolved <= after)

    def test_resolve_start_epoch_none_means_now(self):
        before = int(datetime.now(timezone.utc).timestamp())
        self.assertGreaterEqual(manifest.resolve_start_epoch(None), before)

    def test_resolve_start_epoch_numeric(self):
        self.assertEqual(manifest.resolve_start_epoch("1750000000"), 1750000000)
        self.assertEqual(manifest.resolve_start_epoch(1750000000), 1750000000)


class TestWriteManifest(unittest.TestCase):
    def test_put_object_headers(self):
        s3 = make_s3()
        doc = {"schema": 1, "version": "v1", "photos": []}
        with Stubber(s3) as stubber:
            stubber.add_response(
                "put_object",
                {},
                {
                    "Bucket": "b",
                    "Key": "manifest.dev.json",
                    "Body": ANYBody(),
                    "ContentType": "application/json; charset=utf-8",
                    "CacheControl": "no-store, max-age=0",
                },
            )
            manifest.write_manifest(s3, "b", "manifest.dev.json", doc)


class TestGoldenManifest(unittest.TestCase):
    """The renderer must reproduce the checked-in golden manifest byte-for-byte
    from frozen inputs. This is THE contract test: if it fails, the manifest
    shape changed and every consumer (apps/client) must be re-verified.
    """

    def test_renderer_reproduces_golden_fixture(self):
        import json
        from unittest.mock import patch

        golden_path = os.path.join(os.path.dirname(__file__), "fixtures", "golden_manifest.json")
        with open(golden_path) as f:
            golden = json.load(f)

        s3 = make_s3()
        with Stubber(s3) as stubber:
            stub_head(stubber, "test-bucket", "photos/01_scans/2008/SCAN0060.JPG", 111, '"etag-a"')
            stub_head(
                stubber,
                "test-bucket",
                "photos/02_digital-camera_family/2004/Beach_Day_001.jpg",
                222,
                '"etag-b"',
            )
            with patch.object(manifest, "utc_now_z", return_value="2026-01-01T00:00:00Z"):
                result, skipped = manifest.render_manifest(
                    s3,
                    "test-bucket",
                    [
                        "photos/01_scans/2008/SCAN0060.JPG",
                        "photos/02_digital-camera_family/2004/Beach_Day_001.jpg",
                    ],
                    mode="sync",
                    slide_seconds=1380,
                    start_epoch=1750000000,
                    expires=3600,
                    version="v20260101-000000Z",
                    url_for=lambda key: f"https://cdn.example/{key}?sig=golden",
                    url_expires_at=1757776000,
                )
        self.assertEqual(skipped, [])
        self.assertEqual(result, golden)


class TestCloudFrontSigner(unittest.TestCase):
    def test_signed_url_shape_and_expiry(self):
        from urllib.parse import parse_qs, urlparse

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        from shared import signing

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

        before = int(datetime.now(timezone.utc).timestamp())
        url_for, expires_at = signing.make_url_signer("dxyz.cloudfront.net", "KTESTKEYID", pem, 7776000)
        after = int(datetime.now(timezone.utc).timestamp())
        self.assertTrue(before + 7776000 <= expires_at <= after + 7776000)

        url = url_for("photos/2025/trip/Beach Day 001.jpg")
        parsed = urlparse(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "dxyz.cloudfront.net")
        # Space percent-encoded so the signed resource matches what CloudFront sees.
        self.assertEqual(parsed.path, "/photos/2025/trip/Beach%20Day%20001.jpg")
        qs = parse_qs(parsed.query)
        self.assertEqual(qs["Key-Pair-Id"], ["KTESTKEYID"])
        self.assertEqual(qs["Expires"], [str(expires_at)])
        self.assertTrue(qs["Signature"][0])


class ANYBody:
    """Matches any Body bytes in Stubber expected params."""

    def __eq__(self, other):
        return isinstance(other, (bytes, bytearray))


if __name__ == "__main__":
    unittest.main()
