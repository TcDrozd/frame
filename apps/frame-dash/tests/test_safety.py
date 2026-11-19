"""Safety regressions; fake AWS enforces conditions, not just call shapes."""
import copy
import io
import json
import os
import sys
import unittest
from unittest.mock import patch
from botocore.exceptions import ClientError
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from shared import store, publishing
from api import handler
from test_store import FakeTable

class Safety(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        self.env = patch.dict(os.environ, {'PHOTOS_PREFIX':'photos/', 'PHOTOS_BUCKET':'b', 'MANIFEST_KEY':'m.json', 'LEGACY_MANIFEST_KEY':'', 'PHOTO_URL_MODE':'presign'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_strict_validation(self):
        for settings in [[], {'slide_seconds':True}, {'slide_seconds':'60'}, {'start_epoch':[]}, {'start_epoch':-1}]:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                store.create_playlist(self.table, 'x', settings)
        for name in [1, [], {}, ' ']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                store.create_playlist(self.table, name)

    def test_exact_key_and_duplicates(self):
        p = store.create_playlist(self.table, 'x')
        key = 'photos/ white space .jpg '
        p = store.update_playlist(self.table, p['id'], items=[key], expected_revision=p['revision'])
        self.assertEqual(p['items'], [key])
        with self.assertRaises(ValueError):
            store.update_playlist(self.table, p['id'], items=[key,key], expected_revision=p['revision'])

    def test_stale_edit_rejected(self):
        p = store.create_playlist(self.table, 'x')
        store.update_playlist(self.table, p['id'], name='winner', expected_revision=p['revision'])
        with self.assertRaises(store.Conflict):
            store.update_playlist(self.table, p['id'], name='stale', expected_revision=p['revision'])

    def test_invalid_handler_payloads_are_4xx(self):
        with patch.object(handler, '_get_table', return_value=self.table):
            for body in [{'name':1}, {'name':'x','settings':[]}, {'name':'x','settings':{'start_epoch':{}}}]:
                result = handler.lambda_handler({'rawPath':'/api/playlists','requestContext':{'http':{'method':'POST'}},'body':json.dumps(body)}, None)
                self.assertEqual(result['statusCode'],400)

    def test_serialized_limit(self):
        p = store.create_playlist(self.table,'x')
        with self.assertRaises(ValueError):
            store.update_playlist(self.table,p['id'],items=['photos/'+str(i)+'x'*1000 for i in range(500)],expected_revision=p['revision'])

class HandlerSafety(unittest.TestCase):
    def setUp(self):
        Safety.setUp(self)
        from test_publishing import FakeS3
        self.s3 = FakeS3()
        self.p = store.create_playlist(self.table, 'reviewed')
        self.p = store.update_playlist(self.table, self.p['id'], items=['photos/a.jpg'], expected_revision=1)

    def call(self, method, suffix, payload=None, raw=None, **extra):
        event = {'rawPath':'/api/'+suffix,'requestContext':{'http':{'method':method}},
                 'body':json.dumps(payload) if raw is None else raw, **extra}
        with patch.object(handler,'_get_table',return_value=self.table), patch.object(handler,'_get_s3',return_value=self.s3):
            return handler.lambda_handler(event,None)

    def test_handler_invalid_updates_and_preview_expiry(self):
        path='playlists/'+self.p['id']
        for field,values in {'name':[1,False,[],{},None], 'items':['photos/a.jpg',{},1,None,['photos/a.jpg','photos/a.jpg']],
                             'settings':[[],1,None,{'mode':[]},{'slide_seconds':False},{'start_epoch':'123'}]}.items():
            for value in values:
                with self.subTest(field=field,value=value):
                    result=self.call('PUT',path,{'expected_revision':2,field:value})
                    self.assertEqual(result['statusCode'],400,result)
        for expires in [False,0,-1,3601,1.5,'60',[],{},None]:
            with self.subTest(expires=expires):
                self.assertEqual(self.call('POST','preview-urls',{'keys':['photos/a.jpg'],'expires':expires})['statusCode'],400)

    def test_control_keys_rejected_without_writes_or_revision_advance(self):
        path = 'playlists/' + self.p['id']
        before = copy.deepcopy(self.table.items)
        # All Unicode Cc characters: C0, DEL, and C1 (including NEL).
        for codepoint in [*range(0x20), *range(0x7f, 0xa0)]:
            key = 'photos/before' + chr(codepoint) + 'after.jpg'
            draft = {'name':'reviewed', 'settings':{}, 'items':[key]}
            for method, suffix, body in [
                ('PUT', path, {'expected_revision':2, 'items':[key]}),
                ('POST', path + '/publish', {'dry_run':True, 'draft':draft}),
            ]:
                with self.subTest(codepoint=hex(codepoint), method=method):
                    with patch.object(self.table, 'put_item', wraps=self.table.put_item) as put, \
                         patch.object(self.table, 'transact_write_items', wraps=self.table.transact_write_items) as transact, \
                         patch.object(self.s3, 'put_object', wraps=self.s3.put_object) as s3_put:
                        result = self.call(method, suffix, body)
                        self.assertEqual(result['statusCode'], 400, result)
                        put.assert_not_called()
                        transact.assert_not_called()
                        s3_put.assert_not_called()
                    self.assertEqual(self.table.items, before)
                    self.assertEqual(store.get_playlist(self.table, self.p['id'])['revision'], 2)
                    self.assertEqual(self.s3.writes, [])

    def test_valid_opaque_keys_preserved_by_handler(self):
        # Do not broaden Cc rejection to whitespace, format (Cf), private-use
        # characters, or normalization/URL-sensitive sequences.
        keys = ['photos/ white space .jpg ', 'photos/caf\u00e9.jpg',
                'photos/cafe\u0301.jpg', 'photos/\u00a0\u200b\u2028\ue000\U0001f4f7.jpg',
                'photos/%2F+?#.jpg', 'photos/../literal.jpg']
        path = 'playlists/' + self.p['id']
        result = self.call('PUT', path, {'expected_revision':2, 'items':keys})
        self.assertEqual(result['statusCode'], 200, result)
        saved = json.loads(result['body'])
        self.assertEqual(saved['revision'], 3)
        self.assertEqual([key.encode('utf-8') for key in saved['items']],
                         [key.encode('utf-8') for key in keys])
        self.assertEqual(store.get_playlist(self.table, self.p['id'])['items'], keys)
        before = copy.deepcopy(self.table.items)
        result = self.call('POST', path + '/publish', {'dry_run':True, 'draft':{
            'name':'preview', 'settings':{}, 'items':keys}})
        self.assertEqual(result['statusCode'], 200, result)
        self.assertEqual([photo['key'] for photo in json.loads(result['body'])['manifest']['photos']], keys)
        self.assertEqual(self.table.items, before)
        self.assertEqual(self.s3.writes, [])

    def test_malformed_and_base64(self):
        for raw in ['{','[]','null','{"name":NaN}']:
            self.assertEqual(self.call('POST','playlists',raw=raw)['statusCode'],400)
        self.assertEqual(self.call('POST','playlists',raw='%%%',isBase64Encoded=True)['statusCode'],400)
        self.assertEqual(self.call('POST','playlists',raw='x'*150001)['statusCode'],413)

    def test_unsaved_draft_preview_no_writes(self):
        before=copy.deepcopy(self.table.items)
        draft={'name':'unsaved', 'settings':{'mode':'sync','slide_seconds':7,'start_epoch':123},'items':['photos/b.jpg']}
        result=self.call('POST','playlists/'+self.p['id']+'/publish',{'dry_run':True,'draft':draft})
        self.assertEqual(result['statusCode'],200,result)
        manifest=json.loads(result['body'])['manifest']
        self.assertEqual(manifest['photos'][0]['key'],'photos/b.jpg')
        self.assertEqual(manifest['slide_seconds'],7)
        self.assertEqual(manifest['start_epoch'],123)
        self.assertEqual(self.table.items,before)
        self.assertEqual(self.s3.writes,[])

    def test_conflict_status_and_required_publish_contract(self):
        path='playlists/'+self.p['id']
        result=self.call('PUT',path,{'name':'new','expected_revision':2})
        self.assertEqual(result['statusCode'],200)
        for method,suffix,body in [('PUT',path,{'items':[],'expected_revision':2}),
                                  ('DELETE',path,{'expected_revision':2}),
                                  ('POST',path+'/publish',{'expected_revision':2,'restart':True})]:
            result=self.call(method,suffix,body)
            self.assertEqual(result['statusCode'],409,result)
        for body in [{'expected_revision':3}, {'expected_revision':True,'restart':True},
                     {'expected_revision':3,'restart':'true'}, {'dry_run':'false'},
                     {'expected_revision':3,'restart':True,'draft':{} }]:
            result=self.call('POST',path+'/publish',body)
            self.assertEqual(result['statusCode'],400,result)
        self.assertEqual(self.s3.writes,[])

    def test_publish_retry_after_draft_deleted(self):
        path='playlists/'+self.p['id']
        body={'expected_revision':2,'restart':True,'request_id':'d'*32}
        first=self.call('POST',path+'/publish',body)
        self.assertEqual(first['statusCode'],200,first)
        store.delete_playlist(self.table,self.p['id'],expected_revision=2)
        again=self.call('POST',path+'/publish',body)
        self.assertEqual(again['statusCode'],200,again)
        self.assertEqual(json.loads(again['body'])['version'],json.loads(first['body'])['version'])
        self.assertEqual(self.s3.writes.count('m.json'),1)

    def test_activation_rejects_without_writing(self):
        before=copy.deepcopy(self.table.items)
        self.assertEqual(self.call('PUT','active',{'playlist_id':self.p['id']})['statusCode'],409)
        self.assertEqual(self.call('PUT','active',{'playlist_id':[]})['statusCode'],400)
        self.assertEqual(self.table.items,before)

if __name__ == '__main__': unittest.main()
