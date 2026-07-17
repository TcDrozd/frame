"""Publish a playlist as the live manifest.

Single code path used by both the API publish route and the scheduled
re-publish Lambda. The semantic split between the two callers:

- Explicit user publish: resolve start_epoch fresh from the playlist settings
  ("now" -> current epoch). Publishing deliberately (re)starts the show.
- Scheduled re-publish: pass the pointer's resolved_start_epoch so the
  slideshow position is preserved while presigned URLs are refreshed.
"""
from __future__ import annotations

from typing import Any

from . import config
from . import manifest as manifest_mod
from . import signing
from . import store


def _photo_url_strategy():
    """Return (url_for, url_expires_at) per PHOTO_URL_MODE.

    (None, None) means "use S3 presigned URLs" — render_manifest's default.
    """
    if config.photo_url_mode() != "cloudfront":
        return None, None
    pem = signing.get_private_key_pem(config.private_key_ssm_name())
    return signing.make_url_signer(
        config.photos_cdn_domain(),
        config.cf_key_pair_id(),
        pem,
        config.signed_url_ttl_seconds(),
    )


def publish_playlist(
    s3,
    table,
    *,
    bucket: str,
    manifest_key: str,
    playlist: dict[str, Any],
    expires: int,
    resolved_start_epoch: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    keys = playlist.get("items") or []
    if not keys:
        raise ValueError("playlist has no items")

    settings = playlist.get("settings") or {}
    mode = settings.get("mode", "sync")
    slide_seconds = int(settings.get("slide_seconds", 1380))

    start_epoch: int | None = None
    if mode == "sync":
        if resolved_start_epoch is not None:
            start_epoch = int(resolved_start_epoch)
        else:
            start_epoch = manifest_mod.resolve_start_epoch(settings.get("start_epoch"))

    url_for, url_expires_at = _photo_url_strategy()
    doc, skipped = manifest_mod.render_manifest(
        s3,
        bucket,
        keys,
        mode=mode,
        slide_seconds=slide_seconds,
        start_epoch=start_epoch,
        expires=expires,
        url_for=url_for,
        url_expires_at=url_expires_at,
    )
    if not doc["photos"]:
        raise ValueError("no playlist items exist in the bucket; nothing to publish")

    if not dry_run:
        manifest_mod.write_manifest(s3, bucket, manifest_key, doc)
        legacy_key = config.legacy_manifest_key()
        if legacy_key and legacy_key != manifest_key:
            manifest_mod.write_manifest(s3, bucket, legacy_key, doc)
        store.record_publish(
            table,
            playlist["id"],
            manifest_key=manifest_key,
            version=doc["version"],
            resolved_start_epoch=start_epoch,
        )

    return {
        "manifest": doc,
        "version": doc["version"],
        "photo_count": len(doc["photos"]),
        "skipped_keys": skipped,
        "manifest_key": manifest_key,
        "dry_run": dry_run,
    }
