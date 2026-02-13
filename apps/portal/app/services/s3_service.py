from __future__ import annotations

import boto3
from botocore.config import Config
from typing import Any, Dict, Tuple
from datetime import datetime
import uuid
import os

from ..config import settings

_session = boto3.session.Session(region_name=settings.AWS_REGION)
_s3 = _session.client("s3", config=Config(signature_version="s3v4"))

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

def head_object(key: str) -> Dict[str, Any]:
    return _s3.head_object(Bucket=settings.S3_BUCKET, Key=key)

def get_manifest_head() -> Dict[str, Any] | None:
    try:
        return head_object(settings.MANIFEST_KEY)
    except Exception:
        return None
