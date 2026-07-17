"""Scheduled re-publish: refresh photo URLs for the active playlist.

Runs on an EventBridge schedule (see template.yaml ScheduleExpression). In
"presign" mode the cadence must stay well under PresignExpirySeconds (S3
presigned URLs are also capped by the Lambda role session). In "cloudfront"
mode URLs live SIGNED_URL_TTL_SECONDS (~90 days) regardless of role
sessions, so the schedule can be weekly and is a safety net, not a
treadmill.

Critically, this reuses the pointer's resolved_start_epoch instead of
resolving "now" again: re-resolving would restart the slideshow on every
frame at each refresh. Only an explicit user publish restarts playback.
"""
from __future__ import annotations

from typing import Any

import boto3

from shared import config, publishing, store


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    s3 = boto3.client("s3")
    table = boto3.resource("dynamodb").Table(config.playlist_table())

    active = store.get_active(table)
    if not active or not active.get("playlist_id"):
        print("No active playlist; nothing to re-publish.")
        return {"published": False, "reason": "no active playlist"}

    playlist = store.get_playlist(table, active["playlist_id"])
    if not playlist:
        print(f"Active playlist {active['playlist_id']} no longer exists; clearing pointer.")
        store.clear_active(table)
        return {"published": False, "reason": "active playlist missing"}

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
