"""One renderer and fenced publisher shared by API and scheduler."""
from __future__ import annotations
import re
import uuid
from . import auto_select, config, manifest as manifest_mod, signing, store, publication


def _photo_url_strategy():
    if config.photo_url_mode() != 'cloudfront':
        return None, None
    pem = signing.get_private_key_pem(config.private_key_ssm_name())
    return signing.make_url_signer(config.photos_cdn_domain(), config.cf_key_pair_id(), pem,
                                  config.signed_url_ttl_seconds())


def _request_id(value):
    if value is None:
        return uuid.uuid4().hex
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{32}',value):
        raise ValueError('request_id must be 32 lowercase hexadecimal characters')
    return value


def _begin(s3,table,bucket,key,request_id,binding):
    legacy = config.legacy_manifest_key()
    if legacy and legacy != key:
        raise ValueError('fenced publication requires a single canonical key; disable LEGACY_MANIFEST_KEY')
    saved = publication.receipt(table,request_id)
    if saved:
        if saved['binding'] != binding:
            raise store.Conflict('request_id already used for a different publication')
        return None, saved['result']
    state = store.get_active(table)
    if state and state.get('pending'):
        pending = state['pending']
        result = publication.recover(s3,table,bucket=bucket,manifest_key=key,state=state)
        if pending['id'] == request_id and pending['binding'] == binding:
            return None,result
        raise store.Conflict('recovered earlier publication; reload before publishing')
    return state,None


def _render(s3,bucket,playlist,expires,epoch):
    draft = store.validate_draft({k:playlist[k] for k in ('name','settings','items')})
    if not draft['items']:
        raise ValueError('playlist has no items')
    settings = draft['settings']
    start = None
    if settings['mode'] == 'sync':
        start = epoch if epoch is not None else manifest_mod.resolve_start_epoch(settings['start_epoch'])
    url_for,url_expires = _photo_url_strategy()
    doc,skipped = manifest_mod.render_manifest(s3,bucket,draft['items'],mode=settings['mode'],
                        slide_seconds=settings['slide_seconds'],start_epoch=start,expires=expires,
                        url_for=url_for,url_expires_at=url_expires)
    if not doc['photos']:
        raise ValueError('no playlist items exist in the bucket; nothing to publish')
    # Snapshot exactly what was successfully published, not missing entries that
    # might unexpectedly appear in later URL refreshes.
    draft['items'] = [p['key'] for p in doc['photos']]
    return draft,doc,skipped,start


def publish_playlist(s3,table,*,bucket,manifest_key,playlist,expires,
                     resolved_start_epoch=None,dry_run=False,expected_revision=None,
                     restart=False,request_id=None,expected_state_token=None,refresh=False):
    if type(dry_run) is not bool or type(restart) is not bool:
        raise ValueError('dry_run and restart must be booleans')
    if type(expires) is not int or not 1 <= expires <= 604800:
        raise ValueError('expires must be an integer from 1 to 604800')
    request_id = _request_id(request_id)
    state = None
    binding = {'playlist_id':playlist['id'], 'revision':playlist.get('revision',0),
               'kind':'refresh' if refresh else 'publish'}
    if not dry_run:
        if refresh:
            binding['state_token'] = expected_state_token
        else:
            store.revision(expected_revision)
            if not restart:
                raise ValueError('explicit publish requires restart=true')
            binding['revision'] = expected_revision
        state,saved = _begin(s3,table,bucket,manifest_key,request_id,binding)
        if saved:
            return saved
        if refresh:
            if (not state or not state.get('snapshot') or
                    state.get('state_token') != expected_state_token or state['snapshot'] != playlist):
                raise store.Conflict('published snapshot changed; discard stale refresh')
            resolved_start_epoch = state.get('resolved_start_epoch')
            if playlist['settings']['mode']=='sync' and resolved_start_epoch is None:
                raise store.Conflict('published snapshot has no frozen epoch')
        elif playlist.get('revision',0) != expected_revision:
            raise store.Conflict('playlist revision changed')
    draft,doc,skipped,start = _render(s3,bucket,playlist,expires,resolved_start_epoch)
    result = {'version':doc['version'],'photo_count':len(doc['photos']),'skipped_keys':skipped,
              'manifest_key':manifest_key,'dry_run':dry_run,'request_id':request_id}
    if dry_run:
        return result | {'manifest':doc}
    snapshot = playlist if refresh else draft | {'id':playlist['id'],'revision':playlist.get('revision',0)}
    active = {'playlist_id':playlist['id'],'snapshot':snapshot,'published_revision':snapshot['revision'],
              'manifest_key':manifest_key,'last_published_at':manifest_mod.utc_now_z()}
    if start is not None:
        active['resolved_start_epoch'] = start
    committed = publication.commit(s3,table,bucket=bucket,manifest_key=manifest_key,state=state,doc=doc,
                  active=active,result=result,request_id=request_id,binding=binding,
                  draft=None if refresh else {'id':playlist['id'],'revision':expected_revision})
    return committed | {'manifest':doc}


def publish_auto(s3,table,*,bucket,manifest_key,prefix,window,expires,resolved_start_epoch=None,
                 expected_state_token=None):
    request_id = uuid.uuid4().hex
    binding = {'kind':'auto','state_token':expected_state_token}
    state,saved = _begin(s3,table,bucket,manifest_key,request_id,binding)
    if saved:
        return saved
    if (state or {}).get('state_token') != expected_state_token or (state or {}).get('playlist_id'):
        raise store.Conflict('selection changed; discard stale automatic job')
    pool = auto_select.list_media_keys(s3,bucket,prefix)
    keys = auto_select.select_window(pool,window,prefix=prefix)
    if not keys:
        raise ValueError('no photos found; nothing to publish')
    epoch = (state or {}).get('resolved_start_epoch') if (state or {}).get('source')=='auto' else None
    playlist = {'name':'Automatic window','settings':store.DEFAULT_SETTINGS,'items':keys}
    draft,doc,skipped,start = _render(s3,bucket,playlist,expires,epoch)
    result = {'version':doc['version'],'photo_count':len(doc['photos']),'pool_count':len(pool),
              'skipped_keys':skipped,'manifest_key':manifest_key,'request_id':request_id}
    active = {'source':'auto','snapshot':draft,'manifest_key':manifest_key,
              'last_published_at':manifest_mod.utc_now_z(),'resolved_start_epoch':start,
              'photo_count':len(doc['photos']),'pool_count':len(pool)}
    committed = publication.commit(s3,table,bucket=bucket,manifest_key=manifest_key,state=state,
                 doc=doc,active=active,result=result,request_id=request_id,binding=binding)
    return committed | {'manifest':doc}
