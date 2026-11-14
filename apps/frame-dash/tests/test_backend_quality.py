"""Backend quality regressions: recovery freshness and dependency observations."""
import copy
import json
import os
import sys
import unittest
from unittest.mock import patch
from botocore.exceptions import ClientError, EndpointConnectionError
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from shared import publication, publishing, store
from scheduled import handler as scheduled
from api import handler as api
from test_store import FakeTable
from test_publishing import FakeS3


def canceled(*codes):
    return ClientError({'Error': {'Code': 'TransactionCanceledException', 'Message': 'private detail'},
                        'CancellationReasons': [{'Code': code, 'Message': 'private detail'} for code in codes]},
                       'TransactWriteItems')


class BackendQuality(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'PHOTOS_BUCKET':'b', 'PHOTOS_PREFIX':'photos/',
            'MANIFEST_KEY':'m.json', 'PLAYLIST_TABLE':'t', 'LEGACY_MANIFEST_KEY':'',
            'PHOTO_URL_MODE':'presign', 'AUTO_PUBLISH_MODE':'window'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.table = FakeTable(); self.s3 = FakeS3()
        self.p = store.create_playlist(self.table, 'reviewed', {'start_epoch':123})
        self.p = store.update_playlist(self.table, self.p['id'], items=['photos/a.jpg'], expected_revision=1)

    def pending(self, canonical_written=False, auto=False):
        def fail(kw):
            if kw['Key'] == 'm.json':
                if canonical_written:
                    self.table.fail_transaction = RuntimeError('pointer outage')
                else:
                    raise RuntimeError('canonical outage')
        if canonical_written: self.s3.after_put = fail
        else: self.s3.before_put = fail
        with self.assertRaises(RuntimeError):
            if auto:
                publishing.publish_auto(self.s3, self.table, bucket='b', manifest_key='m.json',
                                        prefix='photos/', window=10, expires=1)
            else:
                publishing.publish_playlist(self.s3, self.table, bucket='b', manifest_key='m.json',
                    playlist=self.p, expires=1, expected_revision=2, restart=True, request_id='a'*32)
        self.s3.before_put = self.s3.after_put = None
        return store.get_active(self.table)

    def run_scheduler(self):
        with patch.object(scheduled.boto3, 'client', return_value=self.s3), \
             patch.object(scheduled.boto3, 'resource') as resource:
            resource.return_value.Table.return_value = self.table
            return scheduled.lambda_handler({}, None)

    def test_delayed_pending_recovery_then_fresh_generation_exact_bytes(self):
        for written in (False, True):
            with self.subTest(canonical_written=written):
                self.table = FakeTable(); self.s3 = FakeS3()
                # Re-create the reviewed draft for the new table.
                self.table.put_item(Item=dict(self.p, pk='PLAYLIST', sk=self.p['id']))
                state = self.pending(written)
                pending = state['pending']; staged = self.s3.objects[pending['staging_key']]['Body']
                recovered_bodies = []
                self.s3.after_put = lambda kw: recovered_bodies.append(kw['Body']) if kw['Key']=='m.json' else None
                with patch.object(self.s3, 'generate_presigned_url', return_value='https://fresh.invalid/photo'):
                    result = self.run_scheduler()
                active = store.get_active(self.table)
                self.assertTrue(result['recovered']); self.assertTrue(result['refreshed'])
                self.assertEqual(result['recovered_version'], pending['result']['version'])
                self.assertNotEqual(result['version'], result['recovered_version'])
                self.assertEqual(active['generation'], 2)
                self.assertEqual(active['snapshot'], pending['active']['snapshot'])
                self.assertEqual(active['resolved_start_epoch'], 123)
                self.assertEqual(publication.receipt(self.table, 'a'*32)['result'], pending['result'])
                self.assertEqual(self.s3.objects[pending['staging_key']]['Body'], staged)
                self.assertEqual(self.s3.writes.count('m.json'), 2)
                if not written: self.assertEqual(recovered_bodies[0], staged)
                self.assertIn(b'https://fresh.invalid/photo', self.s3.objects['m.json']['Body'])

    def test_auto_pending_also_gets_fresh_generation(self):
        state = self.pending(auto=True)
        result = self.run_scheduler()
        self.assertTrue(result['recovered']); self.assertTrue(result['refreshed'])
        active = store.get_active(self.table)
        self.assertEqual(active['generation'], 2)
        self.assertEqual(active['resolved_start_epoch'], state['pending']['active']['resolved_start_epoch'])

    def test_followup_failure_is_explicit_not_recovery_success(self):
        self.pending()
        with patch.object(scheduled.publishing, 'publish_playlist', side_effect=RuntimeError('private detail')):
            with self.assertRaisesRegex(RuntimeError, 'recovered.*fresh refresh failed'):
                self.run_scheduler()
        self.assertIsNotNone(publication.receipt(self.table, 'a'*32))
        self.assertNotIn('pending', store.get_active(self.table))

    def test_followup_superseded_by_newer_generation_is_not_failure(self):
        self.pending()
        with patch.object(scheduled, '_refresh', side_effect=store.Conflict('state changed')):
            result = self.run_scheduler()
        self.assertTrue(result['recovered'])
        self.assertFalse(result['refreshed'])
        self.assertIn('superseded', result['reason'])
        self.assertIsNotNone(publication.receipt(self.table, 'a'*32))

    def test_reload_authoritative_active_after_recovery(self):
        self.pending()
        original = publication.recover
        def recover_then_new_publish(*args, **kwargs):
            result = original(*args, **kwargs)
            updated = store.update_playlist(self.table, self.p['id'], name='new reviewed', expected_revision=2)
            publishing.publish_playlist(self.s3, self.table, bucket='b', manifest_key='m.json',
                playlist=updated, expires=3600, expected_revision=3, restart=True, request_id='b'*32)
            return result
        with patch.object(scheduled.publication, 'recover', side_effect=recover_then_new_publish) as recover:
            # Avoid recursively intercepting commit's own recover call.
            def once(*args, **kwargs):
                recover.side_effect = original
                return recover_then_new_publish(*args, **kwargs)
            recover.side_effect = once
            result = self.run_scheduler()
        self.assertTrue(result['refreshed'])
        self.assertEqual(store.get_active(self.table)['snapshot']['name'], 'new reviewed')
        self.assertEqual(store.get_active(self.table)['generation'], 3)

    def test_preview_pending_zero_writes(self):
        self.pending()
        before = copy.deepcopy(self.table.items), copy.deepcopy(self.s3.objects), list(self.s3.writes)
        publishing.publish_playlist(self.s3, self.table, bucket='b', manifest_key='m.json',
                                    playlist=self.p, expires=3600, dry_run=True)
        self.assertEqual(before, (self.table.items, self.s3.objects, self.s3.writes))

    def test_status_observations_sanitized(self):
        for code, state, safe in [('404','missing',None), ('NoSuchKey','missing',None),
                ('NotFound','missing',None), ('403','error','access_denied'),
                ('AccessDenied','error','access_denied'), ('SlowDown','error','throttled'),
                ('ServiceUnavailable','error','unavailable'), ('NewCodeSecret','error','dependency_error')]:
            with self.subTest(code=code), patch.object(api, '_get_table', return_value=self.table), \
                 patch.object(api, '_get_s3', return_value=self.s3), \
                 patch.object(self.s3, 'head_object', side_effect=ClientError(
                    {'Error':{'Code':code, 'Message':'secret bucket/path'}}, 'HeadObject')):
                result = api.get_status({}, {})
                self.assertEqual(result['manifest_observation']['state'], state)
                self.assertEqual(result['manifest_observation'].get('code'), safe)
                self.assertIsNone(result['manifest_head'])
                self.assertNotIn('secret', json.dumps(result))
        with patch.object(api, '_get_table', return_value=self.table), patch.object(api, '_get_s3', return_value=self.s3):
            self.s3.put_object(Key='m.json', Body=b'{}', Metadata={'publication-id':'a'*32})
            self.assertEqual(api.get_status({}, {})['manifest_observation'], {'state':'present'})
            with patch.object(self.s3, 'head_object', side_effect=EndpointConnectionError(endpoint_url='https://secret.invalid')):
                self.assertEqual(api.get_status({}, {})['manifest_observation'], {'state':'error','code':'unavailable'})
            with patch.object(self.s3, 'head_object', side_effect=RuntimeError('unexpected')):
                with self.assertRaises(RuntimeError): api.get_status({}, {})

    def test_transaction_cancellation_only_conditional_is_conflict(self):
        for codes in [('None','ConditionalCheckFailed'), ('ConditionalCheckFailed','ConditionalCheckFailed')]:
            with self.subTest(codes=codes), self.assertRaises(store.Conflict):
                store._conditional(lambda: (_ for _ in ()).throw(canceled(*codes)))
        for codes in [(), ('None',), ('ProvisionedThroughputExceeded','None'),
                      ('TransactionConflict','None'), ('ThrottlingError',), ('ValidationError',),
                      ('InternalServerError',), ('ConditionalCheckFailed','TransactionConflict'),
                      ('ConditionalCheckFailed','ValidationError')]:
            exc = canceled(*codes)
            with self.subTest(codes=codes), self.assertRaises(ClientError) as raised:
                store._conditional(lambda: (_ for _ in ()).throw(exc))
            self.assertIs(raised.exception, exc)

    def test_api_dependency_cancellation_not_409(self):
        event = {'rawPath':'/api/playlists/'+self.p['id']+'/publish',
                 'requestContext':{'http':{'method':'POST'}},
                 'body':json.dumps({'expected_revision':2,'restart':True})}
        for code, expected in [('ConditionalCheckFailed',409), ('TransactionConflict',500),
                              ('ProvisionedThroughputExceeded',500), ('ValidationError',500)]:
            with self.subTest(code=code), patch.object(api, '_get_table', return_value=self.table), \
                 patch.object(api, '_get_s3', return_value=self.s3):
                self.table.fail_transaction = canceled('None', code, 'None')
                self.assertEqual(api.lambda_handler(event, None)['statusCode'], expected)
