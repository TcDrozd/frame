"""S3 browsing helpers: folder-style listing and presigned GET URLs.

Ported from apps/portal/app/services/s3_service.py (MEDIA_EXTENSIONS,
presign_get) and apps/portal/app/routers/ui.py (_parent_prefix). Unlike the
portal these take the client/bucket as arguments so they are testable with
botocore Stubber and reusable across Lambdas.
"""
from __future__ import annotations

from typing import Any

MEDIA_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".webp",
    ".heic",
    ".heif",
    ".tif",
    ".tiff",
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
    ".webm",
}

# Formats BOTH frame clients can decode: the browser kiosk (platform image
# decoder) and the Pi Zero client (Pillow). Anything else -- HEIC/HEIF, TIFF,
# BMP, video -- must never reach a manifest: one undecodable entry used to block
# every subsequent update on both clients. Phone originals need a converted
# display derivative (Milestone B upload processing), not a wider allowlist.
DISPLAYABLE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def is_media_key(key: str) -> bool:
    lowered = key.lower()
    return any(lowered.endswith(ext) for ext in MEDIA_EXTENSIONS)


def is_displayable_key(key: str) -> bool:
    """True only for a real filename with a displayable extension.

    Classification only: trailing whitespace is ignored when reading the
    extension, but callers must still store/publish the key byte-for-byte.
    """
    name = key.rsplit("/", 1)[-1].rstrip().lower()
    stem, dot, ext = name.rpartition(".")
    return bool(dot and stem) and "." + ext in DISPLAYABLE_EXTENSIONS


def parent_prefix(prefix: str) -> str:
    """photos/2025/trip/ -> photos/2025/ ; photos/ -> '' (caller clamps to root)."""
    trimmed = prefix.rstrip("/")
    if not trimmed:
        return ""
    parts = trimmed.split("/")[:-1]
    if not parts:
        return ""
    return "/".join(parts) + "/"


def browse(
    s3,
    bucket: str,
    prefix: str,
    *,
    continuation_token: str | None = None,
    max_keys: int = 100,
) -> dict[str, Any]:
    """One page of folder-style listing under `prefix`.

    Uses Delimiter="/" so CommonPrefixes become "folders" and Contents are the
    photos directly inside the prefix. Returns a JSON-ready dict.
    """
    kwargs: dict[str, Any] = {
        "Bucket": bucket,
        "Prefix": prefix,
        "Delimiter": "/",
        "MaxKeys": max_keys,
    }
    if continuation_token:
        kwargs["ContinuationToken"] = continuation_token

    page = s3.list_objects_v2(**kwargs)

    folders = sorted(
        cp["Prefix"] for cp in page.get("CommonPrefixes", []) if cp.get("Prefix")
    )
    photos = []
    for item in page.get("Contents", []):
        key = item.get("Key")
        if not key or key == prefix or not is_displayable_key(key):
            continue
        photos.append(
            {
                "key": key,
                "size": item.get("Size"),
                "last_modified": item["LastModified"].isoformat()
                if item.get("LastModified")
                else None,
            }
        )

    return {
        "prefix": prefix,
        "parent_prefix": parent_prefix(prefix),
        "folders": folders,
        "photos": photos,
        "next_token": page.get("NextContinuationToken"),
    }


def presign_get(s3, bucket: str, key: str, expires_seconds: int = 900) -> str:
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_seconds,
    )


def presign_get_many(
    s3, bucket: str, keys: list[str], expires_seconds: int = 900
) -> dict[str, str]:
    return {key: presign_get(s3, bucket, key, expires_seconds) for key in keys}
