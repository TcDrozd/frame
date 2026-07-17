"""Render playlist photo sequences into the manifest.json contract.

The display client (apps/client/app.js) is the consumer. Schema 2 is a
strict superset of schema 1 — the v1 client ignores unknown fields, so the
schema-1 shape below must never lose a field or change a type:

    {
      "schema": 2,
      "version": "v%Y%m%d-%H%M%SZ",
      "generated_at": "<ISO-8601 UTC, Z suffix, no microseconds>",
      "mode": "inventory" | "sync",
      "slide_seconds": <int>,
      "photos": [ {"id", "url", "name", "bytes", "key", "etag"} ],
      "start_epoch": <int>,         # only when mode == "sync"
      "url_expires_at": <int>       # epoch when photo URLs die (when known)
    }

Schema-2 additions: per-photo "key" (full S3 key — "id" is the basename and
collides across folders) and "etag" (content-change token from head_object);
top-level "url_expires_at" so clients can refetch before URLs expire.

The mix/window/shuffle/inject machinery of the original v1 script is
intentionally not ported — playlists are explicit ordered sequences.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import ClientError

VALID_MODES = ("inventory", "sync")


def utc_now_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def epoch_now_utc() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def default_version() -> str:
    return datetime.now(timezone.utc).strftime("v%Y%m%d-%H%M%SZ")


def resolve_start_epoch(raw: Any) -> int:
    """Resolve a playlist's start_epoch setting ("now" | int | None) to epoch seconds."""
    if raw is None:
        return epoch_now_utc()
    text = str(raw).strip().lower()
    if text in ("", "now"):
        return epoch_now_utc()
    return int(text)


def photo_entry(key: str, url: str, size: int | None, etag: str | None = None) -> dict[str, Any]:
    filename = key.split("/")[-1]
    return {
        "id": filename,
        "url": url,
        "name": filename.rsplit(".", 1)[0].replace("_", " "),
        "bytes": size,
        "key": key,
        "etag": (etag or "").strip('"') or None,
    }


def render_manifest(
    s3,
    bucket: str,
    keys: list[str],
    *,
    mode: str,
    slide_seconds: int,
    start_epoch: int | None = None,
    expires: int,
    version: str | None = None,
    schema: int = 2,
    url_for: Any = None,
    url_expires_at: int | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Build a manifest dict from an ordered list of S3 keys.

    Returns (manifest, skipped_keys). Keys missing from the bucket are skipped
    rather than failing the publish, so one deleted photo can't brick a
    playlist. `start_epoch` is required when mode == "sync" — the caller owns
    resolving and persisting it (the scheduled re-publish must reuse the value
    from the first publish or every refresh would restart client playback).

    `url_for(key)` overrides URL generation (CloudFront signed URLs); when
    None, S3 presigned GETs with `expires` are used and url_expires_at is
    derived from `expires`.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")
    if mode == "sync" and start_epoch is None:
        raise ValueError("start_epoch is required when mode == 'sync'")

    if url_for is None:
        def url_for(key: str) -> str:
            return s3.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expires,
            )

        if url_expires_at is None:
            url_expires_at = epoch_now_utc() + int(expires)

    photos: list[dict[str, Any]] = []
    skipped: list[str] = []
    for key in keys:
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                skipped.append(key)
                continue
            raise
        photos.append(
            photo_entry(key, url_for(key), head.get("ContentLength"), head.get("ETag"))
        )

    manifest: dict[str, Any] = {
        "schema": schema,
        "version": version or default_version(),
        "generated_at": utc_now_z(),
        "mode": mode,
        "slide_seconds": int(slide_seconds),
        "photos": photos,
    }
    if mode == "sync":
        manifest["start_epoch"] = int(start_epoch)
    if url_expires_at is not None:
        manifest["url_expires_at"] = int(url_expires_at)

    return manifest, skipped


def write_manifest(s3, bucket: str, manifest_key: str, manifest: dict[str, Any]) -> None:
    import json

    body = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    s3.put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=body,
        ContentType="application/json; charset=utf-8",
        CacheControl="no-store, max-age=0",
    )
