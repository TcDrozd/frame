"""API Lambda: all /api/* routes behind the HTTP API JWT authorizer.

Single function with a small regex router (family-scale traffic; one role,
one cold start). Events are API Gateway HTTP API payload format 2.0.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any, Callable

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, ParamValidationError

from shared import config, publication, publishing, s3_browse, store

_clients: dict[str, Any] = {}

# HEAD failure codes -> sanitized observation codes. Raw AWS messages can carry
# bucket/key/account detail, so only these fixed labels ever reach the client.
_MISSING_CODES = {"404", "NoSuchKey", "NotFound"}
_OBSERVATION_CODES = {
    "403": "access_denied", "AccessDenied": "access_denied",
    "SlowDown": "throttled", "Throttling": "throttled", "ThrottlingException": "throttled",
    "RequestLimitExceeded": "throttled", "503": "unavailable",
    "ServiceUnavailable": "unavailable", "500": "unavailable", "InternalError": "unavailable",
}


def _observe_manifest() -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """HEAD the canonical manifest without conflating absence and failure.

    Genuine 404 -> missing. AWS/transport failures -> an explicit sanitized
    error state (status still renders, and never claims "not found" for a
    permission or outage problem). Anything else is a bug and propagates.
    """
    try:
        head = _get_s3().head_object(Bucket=config.photos_bucket(), Key=config.manifest_key())
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _MISSING_CODES:
            return None, {"state": "missing"}
        print(f"WARN manifest HEAD failed: {code}")
        return None, {"state": "error", "code": _OBSERVATION_CODES.get(code, "dependency_error")}
    except (NoCredentialsError, ParamValidationError) as exc:  # misconfiguration, not an outage
        print(f"WARN manifest HEAD failed: {type(exc).__name__}")
        return None, {"state": "error", "code": "dependency_error"}
    except BotoCoreError as exc:  # connection/endpoint failures
        print(f"WARN manifest HEAD failed: {type(exc).__name__}")
        return None, {"state": "error", "code": "unavailable"}
    return {
        "size": head.get("ContentLength"),
        "publication_id": head.get("Metadata", {}).get("publication-id"),
        "last_modified": head["LastModified"].isoformat() if head.get("LastModified") else None,
    }, {"state": "present"}


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
    try:
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        if not isinstance(raw, str):
            raise ValueError("body must be text")
        if len(raw.encode("utf-8")) > 150_000:
            raise ApiError(413, "request body too large")
        parsed = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise ApiError(400, "request body must be valid JSON")
    if not isinstance(parsed, dict):
        raise ApiError(400, "request body must be a JSON object")
    return parsed


def _require_under_photos_prefix(value: str, what: str) -> str:
    prefix = config.photos_prefix()
    if not isinstance(value, str) or not value.startswith(prefix):
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
    if len(set(keys)) != len(keys):
        raise ApiError(400, "duplicate keys are not allowed")
    store._validate_items(keys)
    expires = body.get("expires", 900)
    if type(expires) is not int or not 1 <= expires <= MAX_PREVIEW_EXPIRES:
        raise ApiError(400, "expires must be an integer from 1 to 3600")
    return {"urls": s3_browse.presign_get_many(_get_s3(), config.photos_bucket(), keys, expires)}


def get_playlists(event: dict, params: dict) -> Any:
    return {"playlists": store.list_playlists(_get_table())}


def post_playlists(event: dict, params: dict) -> Any:
    body = _body(event)
    if "settings" in body and body["settings"] is None:
        raise ApiError(400, "settings must be an object")
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
    for field in ("name", "settings", "items"):
        if field in body and body[field] is None:
            raise ApiError(400, f"{field} cannot be null")
    items = body.get("items")
    try:
        playlist = store.update_playlist(
            _get_table(),
            params["id"],
            expected_revision=body.get("expected_revision"),
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
    store.delete_playlist(_get_table(), params["id"], expected_revision=_body(event).get("expected_revision"))
    return None  # 204


def post_publish(event: dict, params: dict) -> Any:
    body = _body(event)
    dry_run = body.get("dry_run", False)
    if type(dry_run) is not bool:
        raise ApiError(400, "dry_run must be a boolean")
    playlist = store.get_playlist(_get_table(), params["id"])
    if not playlist and not dry_run and body.get("request_id") is not None:
        # A draft can be deleted after publication/reservation. Receipts and
        # pending snapshots still make a retry resolvable without resurrecting it.
        request_id = publishing._request_id(body["request_id"])
        saved = publication.receipt(_get_table(), request_id)
        active = store.get_active(_get_table()) or {}
        if saved or active.get("pending", {}).get("id") == request_id:
            playlist = {"id": params["id"]}  # resolved before any renderer call
    if not playlist:
        raise ApiError(404, "playlist not found")
    if "draft" in body:
        if not dry_run:
            raise ApiError(400, "draft is only accepted for dry_run; save and publish its revision")
        playlist = playlist | store.validate_draft(body["draft"])
    try:
        result = publishing.publish_playlist(
            _get_s3(),
            _get_table(),
            bucket=config.photos_bucket(),
            manifest_key=config.manifest_key(),
            playlist=playlist,
            expires=config.presign_expiry_seconds(),
            dry_run=dry_run,
            expected_revision=body.get("expected_revision"),
            restart=body.get("restart", False),
            request_id=body.get("request_id"),
        )
    except ValueError as exc:
        raise ApiError(400, str(exc))
    if not dry_run:
        result.pop("manifest", None)  # full doc only returned for dry-run previews
    return result


def put_active(event: dict, params: dict) -> Any:
    body = _body(event)
    if "playlist_id" not in body:
        raise ApiError(400, "playlist_id is required (null to clear)")
    playlist_id = body["playlist_id"]
    if playlist_id is None:
        store.clear_active(_get_table())
        return {"active": None}
    if not isinstance(playlist_id, str) or not re.fullmatch(r"[0-9a-f]{32}", playlist_id):
        raise ApiError(400, "playlist_id must be a playlist identifier or null")
    if not store.get_playlist(_get_table(), playlist_id):
        raise ApiError(404, "playlist not found")
    raise ApiError(409, "activation requires publish with expected_revision and restart=true")


def get_status(event: dict, params: dict) -> Any:
    active = store.get_active(_get_table())
    if active and active.get("playlist_id"):
        playlist = store.get_playlist(_get_table(), active["playlist_id"])
        active["playlist_name"] = active.get("snapshot", {}).get("name")
        active["draft_revision"] = playlist.get("revision") if playlist else None
    pending_summary = None
    if active:
        pending = active.get("pending")
        if pending:
            pending_summary = {"request_id": pending["id"],
                               "playlist_id": pending["active"].get("playlist_id"),
                               "published_revision": pending["active"].get("published_revision"),
                               "state": "pending; canonical manifest may already be written"}
        active = {k:v for k,v in active.items() if k != "pending"} | {"publication_pending": bool(pending)}

    manifest_head, observation = _observe_manifest()

    return {
        "active": active,
        "pending_publication": pending_summary,
        "manifest_key": config.manifest_key(),
        "manifest_head": manifest_head,
        "manifest_observation": observation,
        "presign_expiry_seconds": config.presign_expiry_seconds(),
        "auto_publish": {
            "mode": config.auto_publish_mode(),
            "window_size": config.auto_window_size(),
        },
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
        except store.Conflict as exc:
            return _response(409, {"error": str(exc)})
        except ValueError as exc:
            return _response(400, {"error": str(exc)})
        except Exception as exc:  # surfaced in CloudWatch; opaque to caller
            print(f"ERROR {method} {path}: {exc!r}")
            return _response(500, {"error": "internal error"})
        if result is None:
            return {"statusCode": 204, "headers": {}, "body": ""}
        return _response(200, result)

    return _response(404, {"error": f"no route for {method} {path}"})
