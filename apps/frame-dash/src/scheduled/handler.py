"""Scheduled re-publish: refresh photo URLs for the active playlist.

Runs on an EventBridge schedule (see template.yaml ScheduleExpression). In
"presign" mode the cadence must stay well under PresignExpirySeconds (S3
presigned URLs are also capped by the Lambda role session). In "cloudfront"
mode URLs live SIGNED_URL_TTL_SECONDS (~90 days) regardless of role
sessions, so the schedule can be daily/weekly and is a safety net, not a
treadmill.

Critically, this reuses the pointer's resolved_start_epoch instead of
resolving "now" again: re-resolving would restart the slideshow on every
frame at each refresh. Only an explicit user publish restarts playback.

With AUTO_PUBLISH_MODE=window this is also the automatic publisher: when no
curated playlist is active, each run publishes a deterministic daily window
selected from the whole photo pool (see shared.auto_select), so frames keep
rotating fresh content without anyone touching the dashboard. A curated
publish always takes precedence; clearing the active playlist hands control
back to auto on the next run. In auto mode the schedule cadence is also the
freshness cadence — keep it daily-or-better even after the CloudFront flip.
"""
from __future__ import annotations

from typing import Any

import boto3

from shared import config, publishing, store


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    s3 = boto3.client("s3")
    table = boto3.resource("dynamodb").Table(config.playlist_table())

    active = store.get_active(table)
    if active and active.get("playlist_id"):
        playlist = store.get_playlist(table, active["playlist_id"])
        if playlist:
            result = publishing.publish_playlist(
                s3,
                table,
                bucket=config.photos_bucket(),
                manifest_key=config.manifest_key(),
                playlist=playlist,
                expires=config.presign_expiry_seconds(),
                resolved_start_epoch=active.get("resolved_start_epoch"),
            )
            print(
                f"Re-published playlist {playlist['id']} ({playlist['name']}) as "
                f"{result['version']} with {result['photo_count']} photos; "
                f"skipped={result['skipped_keys']}"
            )
            return {
                "published": True,
                "version": result["version"],
                "photo_count": result["photo_count"],
                "skipped_keys": result["skipped_keys"],
            }
        print(f"Active playlist {active['playlist_id']} no longer exists; clearing pointer.")
        store.clear_active(table)
        active = None

    if config.auto_publish_mode() != "window":
        print("No active playlist; nothing to re-publish.")
        return {"published": False, "reason": "no active playlist"}

    epoch = (
        active.get("resolved_start_epoch")
        if active and active.get("source") == "auto"
        else None
    )
    result = publishing.publish_auto(
        s3,
        table,
        bucket=config.photos_bucket(),
        manifest_key=config.manifest_key(),
        prefix=config.photos_prefix(),
        window=config.auto_window_size(),
        expires=config.presign_expiry_seconds(),
        resolved_start_epoch=epoch,
    )
    print(
        f"Auto-published window {result['version']}: {result['photo_count']} of "
        f"{result['pool_count']} pool photos; skipped={result['skipped_keys']}"
    )
    return {
        "published": True,
        "source": "auto",
        "version": result["version"],
        "photo_count": result["photo_count"],
        "pool_count": result["pool_count"],
    }
