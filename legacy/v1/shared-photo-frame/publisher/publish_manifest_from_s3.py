#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
import boto3


def utc_now_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def list_photo_objects(s3, bucket: str, prefix: str) -> list[dict]:
    """Return [{key, size}] for objects under prefix, excluding 'folders'."""
    out = []
    token = None

    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token

        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            out.append({"key": key, "size": obj.get("Size", None)})

        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", default="trevor-shared-photo-stream")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--photos-prefix", default="photos/")  # S3 key prefix
    ap.add_argument("--manifest-key", default="manifest.json")  # where to upload in S3
    ap.add_argument("--expires", type=int, default=12 * 60 * 60, help="presign expiry seconds (default 12h)")
    ap.add_argument("--mode", default="inventory", choices=["inventory", "sync"])
    ap.add_argument("--slide-seconds", type=int, default=12 * 60 * 60)  # 2/day by default
    ap.add_argument("--schema", type=int, default=1)
    ap.add_argument("--version", default=None, help="Optional version string. If omitted, timestamped.")
    args = ap.parse_args()

    session = boto3.session.Session(region_name=args.region)
    s3 = session.client("s3")

    # 1) list photos
    objs = list_photo_objects(s3, args.bucket, args.photos_prefix)

    # Filter to image-y extensions only (keep minimal; extend if needed)
    exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    objs = [o for o in objs if any(o["key"].lower().endswith(ext) for ext in exts)]

    # Stable ordering (by filename)
    objs.sort(key=lambda o: o["key"].lower())

    # 2) presign urls
    photos = []
    for o in objs:
        key = o["key"]
        filename = key.split("/")[-1]
        url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": args.bucket, "Key": key},
            ExpiresIn=args.expires,
        )
        photos.append(
            {
                "id": filename,
                "url": url,
                "name": filename.rsplit(".", 1)[0].replace("_", " "),
                "bytes": o.get("size"),
            }
        )

    # 3) build manifest
    version = args.version or datetime.now(timezone.utc).strftime("v%Y%m%d-%H%M%SZ")
    manifest = {
        "schema": args.schema,
        "version": version,
        "generated_at": utc_now_z(),
        "mode": args.mode,
        "slide_seconds": args.slide_seconds,
        "photos": photos,
    }

    body = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")

    # 4) upload manifest (public-read is granted by bucket policy; still set sane caching)
    s3.put_object(
        Bucket=args.bucket,
        Key=args.manifest_key,
        Body=body,
        ContentType="application/json; charset=utf-8",
        CacheControl="no-store, max-age=0",
    )

    print(f"Uploaded s3://{args.bucket}/{args.manifest_key} with {len(photos)} photos")
    print(f"Manifest URL: https://{args.bucket}.s3.{args.region}.amazonaws.com/{args.manifest_key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())