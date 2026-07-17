"""API Lambda: all /api/* routes behind the HTTP API JWT authorizer.

Single function with a small regex router (family-scale traffic; one role,
one cold start). Events are API Gateway HTTP API payload format 2.0.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any, Callable

import boto3

from shared import config, publishing, s3_browse, store

_clients: dict[str, Any] = {}


def _get_s3():
    if "s3" not in _clients:
        _clients["s3"] = boto3.client("s3")
    return _clients["s3"]


def _get_table():
    if "table" not in _clients:
        _clients["table"] = boto3.resource("dynamodb").Table(config.playlist_table())
    return _clients["table"]


MAX_PREVIEW_KEYS = 100
MAX_PREVIEW_EXPIRES = 3600


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _response(status: int, payload: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body")
    if not raw:
        return {}
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ApiError(400, "request body must be JSON")
    if not isinstance(parsed, dict):
        raise ApiError(400, "request body must be a JSON object")
    return parsed


def _require_under_photos_prefix(value: str, what: str) -> str:
    prefix = config.photos_prefix()
    if not value.startswith(prefix) or ".." in value:
        raise ApiError(400, f"{what} must start with {prefix!r}")
    return value


# ---- route handlers ---------------------------------------------------------


def get_browse(event: dict, params: dict) -> Any:
    qs = event.get("queryStringParameters") or {}
    prefix = (qs.get("prefix") or config.photos_prefix()).strip()
    _require_under_photos_prefix(prefix, "prefix")
    if not prefix.endswith("/"):
        prefix += "/"
    result = s3_browse.browse(
        _get_s3(),
        config.photos_bucket(),
        prefix,
        continuation_token=qs.get("token") or None,
    )
    # Never navigate above the photos root.
    if len(result["parent_prefix"]) < len(config.photos_prefix()):
        result["parent_prefix"] = None if prefix == config.photos_prefix() else config.photos_prefix()
    return result


def post_preview_urls(event: dict, params: dict) -> Any:
    body = _body(event)
    keys = body.get("keys")
    if not isinstance(keys, list) or not keys:
        raise ApiError(400, "keys must be a non-empty list")
    if len(keys) > MAX_PREVIEW_KEYS:
        raise ApiError(400, f"at most {MAX_PREVIEW_KEYS} keys per request")
    for key in keys:
        if not isinstance(key, str):
            raise ApiError(400, "keys must be strings")
        _require_under_photos_prefix(key, "key")
    expires = min(int(body.get("expires") or 900), MAX_PREVIEW_EXPIRES)
    return {"urls": s3_browse.presign_get_many(_get_s3(), config.photos_bucket(), keys, expires)}


def get_playlists(event: dict, params: dict) -> Any:
    return {"playlists": store.list_playlists(_get_table())}


def post_playlists(event: dict, params: dict) -> Any:
    body = _body(event)
    try:
        return store.create_playlist(_get_table(), body.get("name", ""), body.get("settings"))
    except ValueError as exc:
        raise ApiError(400, str(exc))


def get_playlist(event: dict, params: dict) -> Any:
    playlist = store.get_playlist(_get_table(), params["id"])
    if not playlist:
        raise ApiError(404, "playlist not found")
    return playlist


def put_playlist(event: dict, params: dict) -> Any:
    body = _body(event)
    items = body.get("items")
    if items is not None:
        for key in items:
            if not isinstance(key, str):
                raise ApiError(400, "items must be strings")
            _require_under_photos_prefix(key, "item")
    try:
        playlist = store.update_playlist(
            _get_table(),
            params["id"],
            name=body.get("name"),
            settings=body.get("settings"),
            items=items,
        )
    except ValueError as exc:
        raise ApiError(400, str(exc))
    if not playlist:
        raise ApiError(404, "playlist not found")
    return playlist


def delete_playlist(event: dict, params: dict) -> Any:
    store.delete_playlist(_get_table(), params["id"])
    return None  # 204


def post_publish(event: dict, params: dict) -> Any:
    playlist = store.get_playlist(_get_table(), params["id"])
    if not playlist:
        raise ApiError(404, "playlist not found")
    dry_run = bool(_body(event).get("dry_run"))
    try:
        result = publishing.publish_playlist(
            _get_s3(),
            _get_table(),
            bucket=config.photos_bucket(),
            manifest_key=config.manifest_key(),
            playlist=playlist,
            expires=config.presign_expiry_seconds(),
            dry_run=dry_run,
        )
    except ValueError as exc:
        raise ApiError(400, str(exc))
    if not dry_run:
        result.pop("manifest")  # full doc only returned for dry-run previews
    return result


def put_active(event: dict, params: dict) -> Any:
    body = _body(event)
    if "playlist_id" not in body:
        raise ApiError(400, "playlist_id is required (null to clear)")
    playlist_id = body["playlist_id"]
    if playlist_id is None:
        store.clear_active(_get_table())
        return {"active": None}
    if not store.get_playlist(_get_table(), playlist_id):
        raise ApiError(404, "playlist not found")
    store.set_active(_get_table(), playlist_id)
    return {"active": store.get_active(_get_table())}


def get_status(event: dict, params: dict) -> Any:
    active = store.get_active(_get_table())
    if active and active.get("playlist_id"):
        playlist = store.get_playlist(_get_table(), active["playlist_id"])
        active["playlist_name"] = playlist["name"] if playlist else None

    manifest_head = None
    try:
        head = _get_s3().head_object(Bucket=config.photos_bucket(), Key=config.manifest_key())
        manifest_head = {
            "size": head.get("ContentLength"),
            "last_modified": head["LastModified"].isoformat() if head.get("LastModified") else None,
        }
    except Exception:
        pass

    return {
        "active": active,
        "manifest_key": config.manifest_key(),
        "manifest_head": manifest_head,
        "presign_expiry_seconds": config.presign_expiry_seconds(),
    }


# ---- router -----------------------------------------------------------------

ROUTES: list[tuple[str, re.Pattern, Callable]] = [
    ("GET", re.compile(r"^/api/browse$"), get_browse),
    ("POST", re.compile(r"^/api/preview-urls$"), post_preview_urls),
    ("GET", re.compile(r"^/api/playlists$"), get_playlists),
    ("POST", re.compile(r"^/api/playlists$"), post_playlists),
    ("GET", re.compile(r"^/api/playlists/(?P<id>[0-9a-f]{32})$"), get_playlist),
    ("PUT", re.compile(r"^/api/playlists/(?P<id>[0-9a-f]{32})$"), put_playlist),
    ("DELETE", re.compile(r"^/api/playlists/(?P<id>[0-9a-f]{32})$"), delete_playlist),
    ("POST", re.compile(r"^/api/playlists/(?P<id>[0-9a-f]{32})/publish$"), post_publish),
    ("PUT", re.compile(r"^/api/active$"), put_active),
    ("GET", re.compile(r"^/api/status$"), get_status),
]


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "")

    for route_method, pattern, fn in ROUTES:
        if method != route_method:
            continue
        match = pattern.match(path)
        if not match:
            continue
        try:
            result = fn(event, match.groupdict())
        except ApiError as exc:
            return _response(exc.status, {"error": exc.message})
        except Exception as exc:  # surfaced in CloudWatch; opaque to caller
            print(f"ERROR {method} {path}: {exc!r}")
            return _response(500, {"error": "internal error"})
        if result is None:
            return {"statusCode": 204, "headers": {}, "body": ""}
        return _response(200, result)

    return _response(404, {"error": f"no route for {method} {path}"})
