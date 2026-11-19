"""Fenced protocol tests with fault injection and deterministic interleaving."""
import copy
import hashlib
import io
import json
import os
import sys
import unittest
from unittest.mock import patch
import boto3
from botocore.exceptions import ClientError
from botocore.awsrequest import AWSResponse
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','src'))
from shared import publishing, publication, store
from test_store import FakeTable


def error(code): return ClientError({'Error':{'Code':code,'Message':code}},'Test')


class FakeS3:
    def __init__(self):
        self.objects={}
        self.writes=[]
        self.before_put=None
        self.after_put=None

    def head_object(self,Bucket,Key):
        if Key.startswith('photos/'):
            if 'missing' in Key: raise error('404')
            return {'ContentLength':10,'ETag':'"photo"'}
        if Key not in self.objects: raise error('404')
        obj=self.objects[Key]
        return {'ETag':obj['ETag'],'Metadata':copy.deepcopy(obj.get('Metadata',{}))}

    def generate_presigned_url(self,**kwargs): return 'https://example.invalid/'+kwargs['Params']['Key']

    def get_object(self,Bucket,Key): return {'Body':io.BytesIO(self.objects[Key]['Body'])}

    def put_object(self,**kwargs):
        if self.before_put:
            self.before_put(kwargs)
        key=kwargs['Key']
        old=self.objects.get(key)
        if kwargs.get('IfNoneMatch')=='*' and old: raise error('PreconditionFailed')
        if 'IfMatch' in kwargs and (not old or kwargs['IfMatch']!=old['ETag']): raise error('PreconditionFailed')
        obj=copy.deepcopy(kwargs)
        obj['ETag']='"'+hashlib.md5(obj['Body']).hexdigest()+'"'
        self.objects[key]=obj
        self.writes.append(key)
        if self.after_put: self.after_put(kwargs)
        return {'ETag':obj['ETag']}

    def list_objects_v2(self,**kwargs): return {'Contents':[{'Key':'photos/a.jpg'}]}


class TestPublishing(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'LEGACY_MANIFEST_KEY':'','PHOTO_URL_MODE':'presign','PHOTOS_PREFIX':'photos/'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.table=FakeTable(); self.s3=FakeS3()
        self.p=store.create_playlist(self.table,'test')
        self.p=store.update_playlist(self.table,self.p['id'],items=['photos/a.jpg'],expected_revision=1)

    def publish(self,**kwargs):
        return publishing.publish_playlist(self.s3,self.table,bucket='b',manifest_key='m.json',
                  playlist=kwargs.pop('playlist',self.p),expires=3600,
                  expected_revision=kwargs.pop('expected_revision',self.p['revision']),
                  restart=kwargs.pop('restart',True),**kwargs)

    def test_snapshot_isolated_and_refresh_frozen(self):
        first=self.publish(request_id='a'*32)
        active=store.get_active(self.table)
        store.update_playlist(self.table,self.p['id'],items=['photos/b.jpg'],settings={'slide_seconds':90},expected_revision=2)
        result=self.publish(playlist=active['snapshot'],refresh=True,expected_state_token=active['state_token'])
        self.assertEqual(result['manifest']['start_epoch'],first['manifest']['start_epoch'])
        self.assertEqual(result['manifest']['photos'][0]['key'],'photos/a.jpg')
        self.assertEqual(store.get_active(self.table)['snapshot'],active['snapshot'])

    def test_dry_run_zero_writes(self):
        before=copy.deepcopy(self.table.items)
        result=self.publish(dry_run=True)
        self.assertEqual(result['photo_count'],1)
        self.assertEqual(self.table.items,before)
        self.assertEqual(self.s3.writes,[])

    def test_explicit_restart_required(self):
        with self.assertRaises(ValueError): self.publish(restart=False)
        self.assertEqual(self.s3.writes,[])

    def test_unusable_selection(self):
        p=self.p|{'items':['photos/missing.jpg']}
        with self.assertRaises(ValueError): self.publish(playlist=p)
        self.assertEqual(self.s3.writes,[])

    def test_concurrent_edit_before_reservation(self):
        self.table.before_transaction=lambda:store.update_playlist(self.table,self.p['id'],name='edited',expected_revision=2)
        with self.assertRaises(store.Conflict): self.publish()
        self.assertNotIn('m.json',self.s3.objects)
        self.assertIsNone(store.get_active(self.table))

    def test_stale_scheduler_cannot_replace_publish(self):
        self.publish()
        old=store.get_active(self.table)
        self.publish()
        current=copy.deepcopy(self.s3.objects['m.json'])
        with self.assertRaises(store.Conflict):
            self.publish(playlist=old['snapshot'],refresh=True,expected_state_token=old['state_token'])
        self.assertEqual(self.s3.objects['m.json'],current)

    def test_scheduler_render_race_with_explicit_publish(self):
        self.publish(request_id='a'*32)
        active=store.get_active(self.table)
        original=self.s3.head_object
        def interleave(Bucket,Key):
            if Key.startswith('photos/'):
                self.s3.head_object=original
                self.publish(request_id='b'*32)
            return original(Bucket=Bucket,Key=Key)
        self.s3.head_object=interleave
        with self.assertRaises(store.Conflict):
            self.publish(playlist=active['snapshot'],refresh=True,expected_state_token=active['state_token'])
        self.assertEqual(self.s3.objects['m.json']['Metadata']['publication-id'],'b'*32)
        self.assertEqual(self.s3.writes.count('m.json'),2)

    def test_s3_success_pointer_failure_recover_exact_bytes(self):
        def inject(kwargs):
            if kwargs['Key']=='m.json':
                self.table.fail_transaction=RuntimeError('pointer outage')
                self.s3.after_put=None
        self.s3.after_put=inject
        with self.assertRaises(RuntimeError): self.publish(request_id='a'*32)
        active=store.get_active(self.table)
        self.assertIn('pending',active)
        original=self.s3.objects['m.json']['Body']
        store.update_playlist(self.table,self.p['id'],name='later edit',expected_revision=2)
        result=self.publish(playlist=store.get_playlist(self.table,self.p['id']),expected_revision=2,request_id='a'*32)
        self.assertEqual(result['version'],json.loads(original)['version'])
        self.assertEqual(self.s3.objects['m.json']['Body'],original)
        self.assertEqual(self.s3.writes.count('m.json'),1)
        self.assertEqual(store.get_active(self.table)['published_revision'],2)

    def test_failed_canonical_write_recovery(self):
        def fail(kwargs):
            if kwargs['Key']=='m.json': raise RuntimeError('S3 outage')
        self.s3.before_put=fail
        with self.assertRaises(RuntimeError): self.publish(request_id='a'*32)
        self.assertIn('pending',store.get_active(self.table))
        self.s3.before_put=None
        self.publish(request_id='a'*32)
        self.assertNotIn('pending',store.get_active(self.table))

    def test_staging_failure_does_not_reserve(self):
        self.s3.before_put=lambda kw: (_ for _ in ()).throw(RuntimeError('stage outage'))
        with self.assertRaises(RuntimeError): self.publish()
        self.assertIsNone(store.get_active(self.table))

    def test_stale_writer_fenced_after_new_generation(self):
        initial_writes = self.s3.writes.count('m.json')
        # Pause A just before its S3 CAS; another caller finishes A and publishes
        # B; resuming A must fail S3 precondition, never overwrite B.
        def interleave(kwargs):
            if kwargs['Key']!='m.json': return
            self.s3.before_put=None
            self.publish(request_id='a'*32)
            self.publish(request_id='b'*32)
        self.s3.before_put=interleave
        self.publish(request_id='a'*32)
        self.assertEqual(self.s3.objects['m.json']['Metadata']['publication-id'],'b'*32)
        self.assertEqual(store.get_active(self.table)['last_version'],json.loads(self.s3.objects['m.json']['Body'])['version'])
        self.assertEqual(self.s3.writes.count('m.json'),initial_writes+2)

    def test_stale_writer_with_existing_etag(self):
        self.publish(request_id='c'*32)
        self.test_stale_writer_fenced_after_new_generation()

    def test_old_request_retry_after_newer_publish_is_noop(self):
        first=self.publish(request_id='a'*32)
        self.publish(request_id='b'*32)
        result=self.publish(request_id='a'*32)
        self.assertEqual(result['version'],first['version'])
        self.assertEqual(self.s3.writes.count('m.json'),2)
        with self.assertRaises(store.Conflict): self.publish(request_id='a'*32,expected_revision=1)

    def test_clear_during_pending_rejected(self):
        def fail(kwargs):
            if kwargs['Key']=='m.json': raise RuntimeError('outage')
        self.s3.before_put=fail
        with self.assertRaises(RuntimeError): self.publish()
        with self.assertRaises(store.Conflict): store.clear_active(self.table)
        self.assertIn('pending',store.get_active(self.table))

    def test_dual_key_fails_closed(self):
        with patch.dict(os.environ,{'LEGACY_MANIFEST_KEY':'legacy.json'}):
            with self.assertRaises(ValueError): self.publish()
        self.assertEqual(self.s3.writes,[])

    def test_auto_freezes_epoch_and_checks_state(self):
        result=publishing.publish_auto(self.s3,self.table,bucket='b',manifest_key='m.json',prefix='photos/',window=10,expires=3600)
        state=store.get_active(self.table)
        again=publishing.publish_auto(self.s3,self.table,bucket='b',manifest_key='m.json',prefix='photos/',window=10,expires=3600,expected_state_token=state['state_token'])
        self.assertEqual(result['manifest']['start_epoch'],again['manifest']['start_epoch'])
        with self.assertRaises(store.Conflict):
            publishing.publish_auto(self.s3,self.table,bucket='b',manifest_key='m.json',prefix='photos/',window=10,expires=3600,expected_state_token=state['state_token'])

    def test_pointer_transaction_lost_ack_is_idempotent(self):
        original = self.table.transact_write_items
        def lose_ack(TransactItems):
            response = original(TransactItems)
            if any(operation.get('Put',{}).get('Item',{}).get('pk')=='PUBLICATION' for operation in TransactItems):
                self.table.transact_write_items = original
                raise RuntimeError('response lost after commit')
            return response
        self.table.transact_write_items = lose_ack
        with self.assertRaises(RuntimeError): self.publish(request_id='a'*32)
        result = self.publish(request_id='a'*32)
        self.assertEqual(self.s3.writes.count('m.json'),1)
        self.assertEqual(result['version'],store.get_active(self.table)['last_version'])

    def test_canonical_put_lost_ack_recovers_without_rewrite(self):
        def lose_ack(kwargs):
            if kwargs['Key']=='m.json':
                self.s3.after_put=None
                raise RuntimeError('S3 acknowledgement lost')
        self.s3.after_put=lose_ack
        with self.assertRaises(RuntimeError): self.publish(request_id='a'*32)
        self.publish(request_id='a'*32)
        self.assertEqual(self.s3.writes.count('m.json'),1)
        self.assertNotIn('pending',store.get_active(self.table))

    def test_refresh_keeps_snapshot_when_one_photo_disappears(self):
        self.p=store.update_playlist(self.table,self.p['id'],items=['photos/a.jpg','photos/b.jpg'],expected_revision=2)
        self.publish()
        active=store.get_active(self.table)
        original=self.s3.head_object
        def disappear(Bucket,Key):
            if Key=='photos/b.jpg': raise error('404')
            return original(Bucket=Bucket,Key=Key)
        self.s3.head_object=disappear
        result=self.publish(playlist=active['snapshot'],refresh=True,expected_state_token=active['state_token'])
        self.assertEqual(result['photo_count'],1)
        self.assertEqual(store.get_active(self.table)['snapshot'],active['snapshot'])

    def test_repeated_clear_preserves_previous_frozen_show(self):
        self.publish()
        previous=store.get_active(self.table)
        store.clear_active(self.table)
        store.clear_active(self.table)
        state=store.get_active(self.table)
        self.assertEqual(state['previous_snapshot'],previous['snapshot'])
        self.assertEqual(state['previous_resolved_start_epoch'],previous['resolved_start_epoch'])
        self.assertNotIn('resolved_start_epoch',state)

    def test_real_boto_resource_transaction_serialization(self):
        resource=boto3.resource('dynamodb',region_name='us-east-1',aws_access_key_id='testing',aws_secret_access_key='testing')
        table=resource.Table('test-table')
        client = table.meta.client
        writes = [
            {'Put':{'TableName':table.name, 'Item':{
                'pk':'CONFIG', 'sk':'active', 'revision':2,
                'snapshot':{'items':['photos/a.jpg'], 'settings':{
                    'mode':'sync', 'slide_seconds':7, 'start_epoch':123}}},
                'ConditionExpression':'state_token = :token AND attribute_not_exists(pending)',
                'ExpressionAttributeValues':{':token':'previous-token'}}},
            {'ConditionCheck':{'TableName':table.name,
                'Key':{'pk':'PLAYLIST', 'sk':'draft-id'},
                'ConditionExpression':'attribute_exists(pk) AND #rev = :revision',
                'ExpressionAttributeNames':{'#rev':'revision'},
                'ExpressionAttributeValues':{':revision':2}}},
        ]
        original = copy.deepcopy(writes)
        wire_requests = []

        def before_call(params, **kwargs):
            # Unlike Stubber (before-parameter-build), this observes the actual
            # JSON body AFTER resource-native values have been serialized.
            wire_requests.append(json.loads(params['body']))
            return AWSResponse(params['url'], 200, {}, None), {}

        event = 'before-call.dynamodb.TransactWriteItems'
        client.meta.events.register(event, before_call)
        self.addCleanup(client.meta.events.unregister, event, before_call)
        with patch.object(client._endpoint, 'make_request',
                          side_effect=AssertionError('transport must not be reached')) as transport:
            publication._transaction(table, writes)
            transport.assert_not_called()
        self.assertEqual(writes, original)
        self.assertEqual(len(wire_requests), 1)
        # Botocore supplies an idempotency token independently of our payload.
        self.assertRegex(wire_requests[0].pop('ClientRequestToken'),
                         r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
        self.assertEqual(wire_requests, [{'TransactItems':[
            {'Put':{'TableName':'test-table', 'Item':{
                'pk':{'S':'CONFIG'}, 'sk':{'S':'active'}, 'revision':{'N':'2'},
                'snapshot':{'M':{
                    'items':{'L':[{'S':'photos/a.jpg'}]},
                    'settings':{'M':{'mode':{'S':'sync'}, 'slide_seconds':{'N':'7'},
                                     'start_epoch':{'N':'123'}}}}}},
                'ConditionExpression':'state_token = :token AND attribute_not_exists(pending)',
                'ExpressionAttributeValues':{':token':{'S':'previous-token'}}}},
            {'ConditionCheck':{'TableName':'test-table',
                'Key':{'pk':{'S':'PLAYLIST'}, 'sk':{'S':'draft-id'}},
                'ConditionExpression':'attribute_exists(pk) AND #rev = :revision',
                'ExpressionAttributeNames':{'#rev':'revision'},
                'ExpressionAttributeValues':{':revision':{'N':'2'}}}},
        ]}])
