"""Versioned drafts and publication state. No read implicitly migrates live data.

Legacy drafts are exposed as revision 0; their first conditional edit creates
revision 1. Published snapshots survive draft edits and deletion.
"""
from __future__ import annotations
import json
import unicodedata
import uuid
from decimal import Decimal
from typing import Any
from botocore.exceptions import ClientError
from . import config, s3_browse
from .manifest import utc_now_z

PLAYLIST_PK = 'PLAYLIST'
CONFIG_PK = 'CONFIG'
ACTIVE_SK = 'active'
MAX_ITEMS = 1000
# Leave room for DynamoDB attribute names/types and the publication envelope.
MAX_PLAYLIST_BYTES = 120_000
DEFAULT_SETTINGS = {'mode': 'sync', 'slide_seconds': 1380, 'start_epoch': 'now'}

class Conflict(Exception):
    """A conditional write lost, or a publication is awaiting recovery."""


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    return value


def revision(value):
    if type(value) is not int or value < 0:
        raise ValueError('expected_revision must be a nonnegative integer')
    return value


def _validate_name(name):
    if not isinstance(name, str) or not name.strip() or len(name.encode('utf-8')) > 256:
        raise ValueError('name must be a nonempty string of at most 256 UTF-8 bytes')
    return name.strip()


def _validate_items(items):
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ValueError(f'items must be a list of at most {MAX_ITEMS} S3 keys')
    prefix = config.photos_prefix()
    for key in items:
        if (not isinstance(key, str) or not key.startswith(prefix) or key == prefix
                or key.endswith('/') or len(key.encode('utf-8')) > 1024
                or any(unicodedata.category(c) == 'Cc' for c in key)):
            raise ValueError('items must be exact nonempty photo S3 keys under the configured prefix')
        if not s3_browse.is_displayable_key(key):
            raise ValueError('items must be displayable images (.jpg, .jpeg, .png, .webp, .gif); '
                             'HEIC, TIFF and video cannot be shown by the frames')
    if len(set(items)) != len(items):
        raise ValueError('duplicate S3 keys are not allowed')
    return list(items)  # S3 keys are opaque: NEVER strip, normalize, or decode.


def _validate_settings(settings):
    if not isinstance(settings, dict) or set(settings) - set(DEFAULT_SETTINGS):
        raise ValueError('settings must be an object containing only mode, slide_seconds, start_epoch')
    result = DEFAULT_SETTINGS | settings
    if result['mode'] not in ('sync', 'inventory'):
        raise ValueError('settings.mode must be sync or inventory')
    duration = result['slide_seconds']
    if type(duration) is not int or not 1 <= duration <= 86400:
        raise ValueError('settings.slide_seconds must be an integer from 1 to 86400')
    epoch = result['start_epoch']
    if epoch != 'now' and (type(epoch) is not int or not 0 <= epoch <= 253402300799):
        raise ValueError('settings.start_epoch must be now or a nonnegative integer epoch')
    return result


def validate_draft(draft):
    if not isinstance(draft, dict) or set(draft) != {'name', 'settings', 'items'}:
        raise ValueError('draft requires exactly name, settings, items')
    result = {'name': _validate_name(draft['name']), 'settings': _validate_settings(draft['settings']),
              'items': _validate_items(draft['items'])}
    check_size(result)
    return result


def check_size(item):
    if len(json.dumps(to_jsonable(item), ensure_ascii=True, allow_nan=False).encode()) > MAX_PLAYLIST_BYTES:
        raise ValueError(f'playlist serialized size exceeds {MAX_PLAYLIST_BYTES} bytes')


def _public(item):
    out = to_jsonable({k:v for k,v in item.items() if k not in ('pk', 'sk')})
    out['id'] = item['sk']
    out.setdefault('revision', 0)
    return out


def _is_stale_state(exc: ClientError) -> bool:
    """True only when the failure means "someone else changed the state".

    A TransactionCanceledException is a stale-state conflict only when every
    cancelled item either passed ('None') or failed its condition, and at least
    one failed. Throughput, contention (TransactionConflict), validation and
    server failures are dependency errors, not stale edits, and must not be
    reported to users as a 409 "reload and retry".
    """
    code = exc.response.get('Error', {}).get('Code')
    if code == 'ConditionalCheckFailedException':
        return True
    if code != 'TransactionCanceledException':
        return False
    reasons = [r.get('Code') or 'None' for r in exc.response.get('CancellationReasons') or []]
    return ('ConditionalCheckFailed' in reasons
            and all(r in ('None', 'ConditionalCheckFailed') for r in reasons))


def _conditional(fn, **kwargs):
    try:
        return fn(**kwargs)
    except ClientError as exc:
        if _is_stale_state(exc):
            raise Conflict('state changed; reload and retry') from exc
        raise


def revision_condition(expected):
    revision(expected)
    return ('attribute_exists(pk) AND attribute_not_exists(revision)' if expected == 0
            else 'attribute_exists(pk) AND revision = :revision')


def revision_values(expected):
    return {} if expected == 0 else {'ExpressionAttributeValues': {':revision': expected}}


def create_playlist(table, name, settings=None):
    now = utc_now_z()
    item = {'pk': PLAYLIST_PK, 'sk': uuid.uuid4().hex, 'name': _validate_name(name), 'items': [],
            'settings': _validate_settings({} if settings is None else settings),
            'revision': 1, 'created_at': now, 'updated_at': now}
    check_size(item)
    _conditional(table.put_item, Item=item, ConditionExpression='attribute_not_exists(pk)')
    return _public(item)


def get_playlist(table, playlist_id):
    item = table.get_item(Key={'pk': PLAYLIST_PK, 'sk': playlist_id}, ConsistentRead=True).get('Item')
    return _public(item) if item else None


def update_playlist(table, playlist_id, *, expected_revision=None, name=None, settings=None, items=None):
    revision(expected_revision)
    item = table.get_item(Key={'pk': PLAYLIST_PK, 'sk': playlist_id}, ConsistentRead=True).get('Item')
    if not item:
        return None
    if int(item.get('revision',0)) != expected_revision:
        raise Conflict('playlist revision changed')
    draft = validate_draft({'name': item['name'] if name is None else name,
                            'settings': to_jsonable(item.get('settings', {})) if settings is None else
                            to_jsonable(item.get('settings', {})) | _settings_patch(settings),
                            'items': item.get('items', []) if items is None else items})
    item.update(draft, revision=expected_revision+1, updated_at=utc_now_z())
    check_size(item)
    _conditional(table.put_item, Item=item, ConditionExpression=revision_condition(expected_revision),
                 **revision_values(expected_revision))
    return _public(item)


def _settings_patch(settings):
    _validate_settings(settings)
    return settings


def delete_playlist(table, playlist_id, *, expected_revision=None):
    revision(expected_revision)
    _conditional(table.delete_item, Key={'pk': PLAYLIST_PK, 'sk': playlist_id},
                 ConditionExpression=revision_condition(expected_revision), **revision_values(expected_revision))
    # Deleting a draft must not delete the immutable published show.


def list_playlists(table):
    from boto3.dynamodb.conditions import Key
    items = []
    kwargs = {'KeyConditionExpression': Key('pk').eq(PLAYLIST_PK), 'ConsistentRead':True}
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get('Items',[]))
        if not resp.get('LastEvaluatedKey'):
            break
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
    active = get_active(table) or {}
    return sorted([{'id':p['id'], 'name':p['name'], 'revision':p['revision'],
                    'item_count':len(p.get('items',[])), 'settings':p.get('settings',{}),
                    'updated_at':p.get('updated_at'), 'is_active':p['id']==active.get('playlist_id')}
                   for p in map(_public,items)], key=lambda p:p.get('updated_at') or '', reverse=True)


def get_active(table):
    item = table.get_item(Key={'pk':CONFIG_PK, 'sk':ACTIVE_SK}, ConsistentRead=True).get('Item')
    return to_jsonable({k:v for k,v in item.items() if k not in ('pk','sk')}) if item else None


def state_condition(state):
    if state and state.get('state_token'):
        return {'ConditionExpression':'state_token = :token AND attribute_not_exists(pending)',
                'ExpressionAttributeValues':{':token':state['state_token']}}
    return {'ConditionExpression':'attribute_not_exists(state_token) AND attribute_not_exists(pending)'}


def clear_active(table):
    state = get_active(table)
    # Preserve the last-good snapshot for status/recovery, but do not reuse its
    # timing for the next auto show. Never discard a pending publication.
    item = {'pk':CONFIG_PK, 'sk':ACTIVE_SK, 'state_token':uuid.uuid4().hex, 'source':'auto',
            'generation':(state or {}).get('generation',0)+1}
    if state:
        previous = state.get('snapshot') or state.get('previous_snapshot')
        if previous:
            item['previous_snapshot'] = previous
        epoch = state.get('resolved_start_epoch', state.get('previous_resolved_start_epoch'))
        if epoch is not None:
            item['previous_resolved_start_epoch'] = epoch
        version = state.get('last_version', state.get('previous_version'))
        if version:
            item['previous_version'] = version
    _conditional(table.put_item, Item=item, **state_condition(state))


def set_active(table, playlist_id):
    raise ValueError('activation requires publish with expected_revision and restart=true')
