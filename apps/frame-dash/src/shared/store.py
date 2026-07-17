"""DynamoDB playlist store.

One table, two item shapes (see template.yaml PlaylistTable):

    pk="PLAYLIST", sk=<uuid hex>   name, items (ordered S3 keys — list order
                                   IS playback order), settings, created_at,
                                   updated_at
    pk="CONFIG",   sk="active"     playlist_id, resolved_start_epoch,
                                   last_published_at, last_version,
                                   manifest_key

`resolved_start_epoch` is frozen at the first publish of a sync playlist and
reused by the scheduled re-publish; resetting it would restart playback on
every frame at once.

All functions take a boto3 DynamoDB *Table* resource so tests can inject a
stubbed one.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from .manifest import utc_now_z

PLAYLIST_PK = "PLAYLIST"
CONFIG_PK = "CONFIG"
ACTIVE_SK = "active"

# Soft cap well under DynamoDB's 400 KB item limit (~3,000+ keys would fit).
MAX_ITEMS = 1000

DEFAULT_SETTINGS = {
    "mode": "sync",
    "slide_seconds": 1380,
    "start_epoch": "now",
}


def to_jsonable(value: Any) -> Any:
    """Recursively convert DynamoDB Decimals to int/float for json.dumps."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    return value


def _validate_items(items: list[Any]) -> list[str]:
    if not isinstance(items, list):
        raise ValueError("items must be a list of S3 keys")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"playlist exceeds {MAX_ITEMS} items")
    cleaned = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("items must be non-empty strings")
        cleaned.append(item.strip())
    return cleaned


def _validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    merged = {**DEFAULT_SETTINGS, **(settings or {})}
    if merged["mode"] not in ("sync", "inventory"):
        raise ValueError("settings.mode must be 'sync' or 'inventory'")
    merged["slide_seconds"] = int(merged["slide_seconds"])
    if merged["slide_seconds"] <= 0:
        raise ValueError("settings.slide_seconds must be positive")
    return merged


def _public(item: dict[str, Any]) -> dict[str, Any]:
    out = to_jsonable({k: v for k, v in item.items() if k not in ("pk", "sk")})
    out["id"] = item["sk"]
    return out


def create_playlist(table, name: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    now = utc_now_z()
    item = {
        "pk": PLAYLIST_PK,
        "sk": uuid.uuid4().hex,
        "name": name,
        "items": [],
        "settings": _validate_settings(settings or {}),
        "created_at": now,
        "updated_at": now,
    }
    table.put_item(Item=item)
    return _public(item)


def list_playlists(table) -> list[dict[str, Any]]:
    from boto3.dynamodb.conditions import Key

    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(PLAYLIST_PK)}
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last

    active = get_active(table)
    active_id = active.get("playlist_id") if active else None

    summaries = []
    for item in items:
        pub = _public(item)
        summaries.append(
            {
                "id": pub["id"],
                "name": pub["name"],
                "item_count": len(pub.get("items", [])),
                "settings": pub.get("settings", {}),
                "updated_at": pub.get("updated_at"),
                "is_active": pub["id"] == active_id,
            }
        )
    summaries.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    return summaries


def get_playlist(table, playlist_id: str) -> dict[str, Any] | None:
    resp = table.get_item(Key={"pk": PLAYLIST_PK, "sk": playlist_id})
    item = resp.get("Item")
    return _public(item) if item else None


def update_playlist(
    table,
    playlist_id: str,
    *,
    name: str | None = None,
    settings: dict[str, Any] | None = None,
    items: list[str] | None = None,
) -> dict[str, Any] | None:
    """Read-modify-write; `items` is a full replace (covers add/remove/reorder)."""
    resp = table.get_item(Key={"pk": PLAYLIST_PK, "sk": playlist_id})
    item = resp.get("Item")
    if not item:
        return None
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("name cannot be empty")
        item["name"] = name
    if settings is not None:
        item["settings"] = _validate_settings(to_jsonable(item.get("settings", {})) | settings)
    if items is not None:
        item["items"] = _validate_items(items)
    item["updated_at"] = utc_now_z()
    table.put_item(Item=item)
    return _public(item)


def delete_playlist(table, playlist_id: str) -> None:
    table.delete_item(Key={"pk": PLAYLIST_PK, "sk": playlist_id})
    active = get_active(table)
    if active and active.get("playlist_id") == playlist_id:
        clear_active(table)


def get_active(table) -> dict[str, Any] | None:
    resp = table.get_item(Key={"pk": CONFIG_PK, "sk": ACTIVE_SK})
    item = resp.get("Item")
    if not item:
        return None
    return to_jsonable({k: v for k, v in item.items() if k not in ("pk", "sk")})


def set_active(table, playlist_id: str) -> None:
    """Point the scheduled re-publish at a playlist (publish-time fields are
    written by record_publish)."""
    table.update_item(
        Key={"pk": CONFIG_PK, "sk": ACTIVE_SK},
        UpdateExpression="SET playlist_id = :pid",
        ExpressionAttributeValues={":pid": playlist_id},
    )


def clear_active(table) -> None:
    table.delete_item(Key={"pk": CONFIG_PK, "sk": ACTIVE_SK})


def record_publish(
    table,
    playlist_id: str,
    *,
    manifest_key: str,
    version: str,
    resolved_start_epoch: int | None,
) -> None:
    item: dict[str, Any] = {
        "pk": CONFIG_PK,
        "sk": ACTIVE_SK,
        "playlist_id": playlist_id,
        "manifest_key": manifest_key,
        "last_version": version,
        "last_published_at": utc_now_z(),
    }
    if resolved_start_epoch is not None:
        item["resolved_start_epoch"] = int(resolved_start_epoch)
    table.put_item(Item=item)
