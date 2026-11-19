"""Recoverable publication with S3 ETag fencing, not an expiring lock.

Protocol: stage an immutable uniquely-versioned manifest; transactionally reserve
CONFIG/active (and check the reviewed draft revision); CAS the canonical S3 key;
transactionally replace the pointer and persist a permanent request receipt.
Reservations NEVER expire or transfer. Any caller may finish the SAME reserved
bytes. A paused writer only has the old ETag, so cannot overwrite a later show.

Cutover prerequisites: stop ALL legacy/unconditional writers; grant GetObject on
canonical/staged keys and transactional Put/ConditionCheck permissions; disable
the legacy second key.
Two independently writable canonical keys cannot be atomically published.
No live migration is performed. Orphan staged objects are harmless; GC must never
remove a pending object. Do not TTL request receipts or reuse request IDs.
"""
from __future__ import annotations
import json
import uuid
from botocore.exceptions import ClientError
from . import store


def receipt(table, request_id):
    item = table.get_item(Key={'pk':'PUBLICATION', 'sk':request_id}, ConsistentRead=True).get('Item')
    return store.to_jsonable(item) if item else None


def _head(s3, bucket, key):
    try:
        return s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response['Error']['Code'] in ('404','NoSuchKey','NotFound'):
            return None
        raise


def _transaction(table, writes):
    return store._conditional(table.meta.client.transact_write_items, TransactItems=writes)


def recover(s3, table, *, bucket, manifest_key, state=None):
    state = state or store.get_active(table) or {}
    pending = state.get('pending')
    if not pending:
        return None
    if pending['bucket'] != bucket or pending['manifest_key'] != manifest_key:
        raise store.Conflict('pending publication targets another manifest; operator recovery required')
    operation = pending['id']
    saved = receipt(table, operation)
    if saved:
        return saved['result']
    staged = s3.get_object(Bucket=bucket, Key=pending['staging_key'])['Body'].read()
    head = _head(s3, bucket, manifest_key)
    if not head or head.get('Metadata',{}).get('publication-id') != operation:
        condition = {'IfMatch':pending['base_etag']} if pending.get('base_etag') else {'IfNoneMatch':'*'}
        try:
            s3.put_object(Bucket=bucket, Key=manifest_key, Body=staged,
                          ContentType='application/json; charset=utf-8', CacheControl='no-store, max-age=0',
                          Metadata={'publication-id':operation}, **condition)
        except ClientError as exc:
            if exc.response['Error']['Code'] not in ('PreconditionFailed','412','ConditionalRequestConflict','409'):
                raise
            # Another finisher might have won this same CAS. A newer operation
            # is never overwritten; a legacy/out-of-band write requires repair.
            head = _head(s3,bucket,manifest_key)
            if not head or head.get('Metadata',{}).get('publication-id') != operation:
                saved = receipt(table,operation)
                if saved:
                    return saved['result']
                raise store.Conflict('manifest changed outside the reserved generation') from exc
    final = pending['active'] | {'pk':store.CONFIG_PK, 'sk':store.ACTIVE_SK,
                                 'state_token':uuid.uuid4().hex,
                                 'generation':state.get('generation',0)+1}
    record = {'pk':'PUBLICATION', 'sk':operation, 'binding':pending['binding'], 'result':pending['result']}
    try:
        _transaction(table, [
            {'Put':{'TableName':table.name, 'Item':final,
                    'ConditionExpression':'pending.#id = :operation',
                    'ExpressionAttributeNames':{'#id':'id'},
                    'ExpressionAttributeValues':{':operation':operation}}},
            {'Put':{'TableName':table.name, 'Item':record, 'ConditionExpression':'attribute_not_exists(pk)'}}])
    except store.Conflict:
        saved = receipt(table,operation)
        if not saved:
            raise
        return saved['result']
    return pending['result']


def commit(s3, table, *, bucket, manifest_key, state, doc, active, result, request_id,
           binding, draft=None):
    """Reserve against EXACT previously observed state and optional draft revision."""
    if state and state.get('pending'):
        raise store.Conflict('publication pending; recover before starting another')
    stage_key = manifest_key + '.publications/' + uuid.uuid4().hex + '.json'
    # Version must be unique across generations: prevents an ETag ABA even if
    # two renders happen during the same wall-clock second.
    doc['version'] = doc['version'] + '-' + request_id
    result['version'] = doc['version']
    active['last_version'] = doc['version']
    body = (json.dumps(doc, ensure_ascii=True, allow_nan=False) + '\n').encode()
    head = _head(s3,bucket,manifest_key)
    s3.put_object(Bucket=bucket, Key=stage_key, Body=body,
                  ContentType='application/json', IfNoneMatch='*')
    pending = {'id':request_id, 'bucket':bucket, 'manifest_key':manifest_key, 'staging_key':stage_key,
               'active':active, 'binding':binding, 'result':result}
    if head:
        pending['base_etag'] = head['ETag']
    reserved = dict(state or {}) | {'pk':store.CONFIG_PK, 'sk':store.ACTIVE_SK, 'pending':pending}
    writes = [
        {'Put':{'TableName':table.name,'Item':reserved, **store.state_condition(state)}},
        {'ConditionCheck':{'TableName':table.name, 'Key':{'pk':'PUBLICATION','sk':request_id},
                           'ConditionExpression':'attribute_not_exists(pk)'}}]
    if draft:
        expected = draft['revision']
        writes.append({'ConditionCheck':{'TableName':table.name,
                       'Key':{'pk':store.PLAYLIST_PK,'sk':draft['id']},
                       'ConditionExpression':store.revision_condition(expected),
                       **store.revision_values(expected)}})
    _transaction(table,writes)
    return recover(s3,table,bucket=bucket,manifest_key=manifest_key,state=reserved)
