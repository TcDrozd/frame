from __future__ import annotations

import boto3
from botocore.config import Config
from dataclasses import dataclass
from typing import Any, Dict, Iterable
from datetime import datetime
import uuid

from ..config import settings

_session = boto3.session.Session(region_name=settings.AWS_REGION)
_s3 = _session.client("s3", config=Config(signature_version="s3v4"))

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


@dataclass(frozen=True)
class S3MediaObject:
    key: str
    size: int | None
    etag: str | None

def build_object_key(original_filename: str | None, content_type: str | None, upload_prefix: str | None = None) -> str:
    # photos/original/YYYY/MM/<uuid>_<sanitizedname>.jpg
    now = datetime.utcnow()
    yyyy = now.strftime("%Y")
    mm = now.strftime("%m")
    u = uuid.uuid4().hex

    name = (original_filename or "upload").strip()
    # simple sanitize
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".", " ") else "_" for ch in name)
    safe = safe.replace(" ", "_")[:80] or "upload"
    prefix = (upload_prefix or settings.PHOTOS_PREFIX).strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return f"{prefix}{yyyy}/{mm}/{u}_{safe}"

def presign_post(key: str, content_type: str | None, max_bytes: int = 25_000_000, expires_seconds: int = 300) -> Dict[str, Any]:
    conditions = [
        {"bucket": settings.S3_BUCKET},
        ["starts-with", "$key", settings.PHOTOS_PREFIX],
        ["content-length-range", 1, max_bytes],
    ]
    fields = {"key": key}

    if content_type:
        fields["Content-Type"] = content_type
        conditions.append({"Content-Type": content_type})

    return _s3.generate_presigned_post(
        Bucket=settings.S3_BUCKET,
        Key=key,
        Fields=fields,
        Conditions=conditions,
        ExpiresIn=expires_seconds,
    )


def presign_get(key: str, expires_seconds: int = 3600) -> str:
    return _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET, "Key": key},
        ExpiresIn=expires_seconds,
    )

def head_object(key: str) -> Dict[str, Any]:
    return _s3.head_object(Bucket=settings.S3_BUCKET, Key=key)

def get_manifest_head() -> Dict[str, Any] | None:
    try:
        return head_object(settings.MANIFEST_KEY)
    except Exception:
        return None


def _normalize_prefix(raw: str) -> str:
    prefix = (raw or "").strip().lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return prefix


def _is_media_key(key: str) -> bool:
    lowered = key.lower()
    for ext in MEDIA_EXTENSIONS:
        if lowered.endswith(ext):
            return True
    return False


def _csv_list(value: str | None) -> list[str]:
    if not value:
        return []
    parts = []
    for raw in value.replace("\n", ",").split(","):
        token = raw.strip()
        if token:
            parts.append(token)
    return parts


def list_media_objects(include_prefixes: list[str], exclude_prefixes: list[str] | None = None) -> Iterable[S3MediaObject]:
    excludes = [_normalize_prefix(p) for p in (exclude_prefixes or []) if p.strip()]
    paginator = _s3.get_paginator("list_objects_v2")
    for raw_prefix in include_prefixes:
        prefix = _normalize_prefix(raw_prefix)
        for page in paginator.paginate(Bucket=settings.S3_BUCKET, Prefix=prefix):
            for item in page.get("Contents", []):
                key = item.get("Key")
                if not key:
                    continue
                if excludes and any(key.startswith(x) for x in excludes):
                    continue
                if not _is_media_key(key):
                    continue
                etag = (item.get("ETag") or "").strip('"') or None
                yield S3MediaObject(key=key, size=item.get("Size"), etag=etag)


def get_gallery_prefixes(include_raw: str | None, exclude_raw: str | None) -> tuple[list[str], list[str]]:
    include = _csv_list(include_raw) or [settings.PHOTOS_PREFIX]
    exclude = _csv_list(exclude_raw)
    return include, exclude
