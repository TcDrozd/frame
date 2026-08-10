"""Environment-driven configuration for the frame-dash Lambdas.

All values are injected by the SAM template (template.yaml) as Lambda
environment variables, so this module is the single place that names them.
"""
from __future__ import annotations

import os


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def photos_bucket() -> str:
    return _require("PHOTOS_BUCKET")


def photos_prefix() -> str:
    prefix = _require("PHOTOS_PREFIX")
    if not prefix.endswith("/"):
        prefix += "/"
    return prefix


def manifest_key() -> str:
    return _require("MANIFEST_KEY")


def playlist_table() -> str:
    return _require("PLAYLIST_TABLE")


def presign_expiry_seconds() -> int:
    return int(os.environ.get("PRESIGN_EXPIRY_SECONDS", "21600"))


def photo_url_mode() -> str:
    """"presign" (S3 presigned URLs) or "cloudfront" (long-lived signed URLs)."""
    return os.environ.get("PHOTO_URL_MODE", "presign").strip() or "presign"


def photos_cdn_domain() -> str:
    return _require("PHOTOS_CDN_DOMAIN")


def cf_key_pair_id() -> str:
    return _require("CF_KEY_PAIR_ID")


def private_key_ssm_name() -> str:
    return _require("PRIVATE_KEY_SSM_NAME")


def signed_url_ttl_seconds() -> int:
    return int(os.environ.get("SIGNED_URL_TTL_SECONDS", "7776000"))


def legacy_manifest_key() -> str:
    """Extra key every publish is mirrored to during migrations ("" = off)."""
    return os.environ.get("LEGACY_MANIFEST_KEY", "").strip()


def auto_publish_mode() -> str:
    """"off" = scheduled runs only refresh a curated playlist; "window" =
    when no playlist is active, publish a daily-rotating automatic selection."""
    return os.environ.get("AUTO_PUBLISH_MODE", "off").strip() or "off"


def auto_window_size() -> int:
    return int(os.environ.get("AUTO_WINDOW_SIZE", "50"))
