"""Refresh only immutable published snapshots; never infer them from drafts."""
from __future__ import annotations
from typing import Any
import boto3
from shared import config, publishing, store, publication


def _refresh(s3, table, active: dict[str, Any] | None) -> dict[str, Any]:
    """One fenced refresh generation from the authoritative active state."""
    if active and active.get('playlist_id'):
        if not active.get('snapshot'):
            # The old pointer cannot prove what was actually published. A live
            # draft is NOT a safe migration source. Explicit reviewed publish only.
            return {'published':False,'reason':'legacy active state requires explicit publish'}
        result = publishing.publish_playlist(s3,table,bucket=config.photos_bucket(),
                    manifest_key=config.manifest_key(),playlist=active['snapshot'],
                    expires=config.presign_expiry_seconds(),refresh=True,
                    expected_state_token=active.get('state_token'),
                    resolved_start_epoch=active.get('resolved_start_epoch'))
        return {'published':True,'version':result['version'],'photo_count':result['photo_count'],
                'skipped_keys':result['skipped_keys']}
    if config.auto_publish_mode() != 'window':
        return {'published':False,'reason':'no active playlist'}
    # Old auto pointers also require an explicit cutover decision (clear active)
    # rather than silently adopting arbitrary historic epoch/selection metadata.
    if active and not active.get('state_token'):
        return {'published':False,'reason':'legacy auto state requires explicit clear or publish'}
    result = publishing.publish_auto(s3,table,bucket=config.photos_bucket(),manifest_key=config.manifest_key(),
                prefix=config.photos_prefix(),window=config.auto_window_size(),expires=config.presign_expiry_seconds(),
                expected_state_token=(active or {}).get('state_token'),
                resolved_start_epoch=(active or {}).get('resolved_start_epoch'))
    return {'published':True,'source':'auto','version':result['version'],
            'photo_count':result['photo_count'],'pool_count':result['pool_count']}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    s3 = boto3.client('s3')
    table = boto3.resource('dynamodb').Table(config.playlist_table())
    active = store.get_active(table)
    if not (active and active.get('pending')):
        return _refresh(s3, table, active)

    # A pending reservation is finished with its EXACT staged bytes first: that
    # preserves fencing, the receipt and the frozen epoch. But if the outage
    # outlived those bytes' presigned URLs, the recovered manifest is already
    # unplayable, so immediately follow with a fresh fenced generation from the
    # authoritative (reloaded) state rather than waiting for the next schedule.
    recovered = publication.recover(s3,table,bucket=config.photos_bucket(),
                                    manifest_key=config.manifest_key(),state=active) or {}
    try:
        fresh = _refresh(s3, table, store.get_active(table))
    except store.Conflict:
        # A newer publish/refresh/clear won the race after recovery. That
        # generation was rendered fresh, so this is not a stale-URL failure;
        # raising would trigger a pointless async retry and a false alarm.
        print('WARN post-recovery refresh superseded by a newer generation')
        fresh = {'published': False, 'reason': 'superseded by a newer generation'}
    except Exception as exc:
        raise RuntimeError(
            f"recovered pending publication {recovered.get('version')} but fresh refresh failed; "
            "the recovered manifest's photo URLs may be expired") from exc
    return {**fresh, 'recovered':True, 'recovered_version':recovered.get('version'),
            'refreshed':bool(fresh.get('published'))}
