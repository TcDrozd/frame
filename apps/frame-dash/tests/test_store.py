import copy
import os
import sys
import types
import unittest
from decimal import Decimal
from botocore.exceptions import ClientError
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from shared import store


class FakeTable:
    """Copy-on-read fake enforcing conditions and atomic transactions."""
    name = 'test-table'

    def __init__(self):
        self.items = {}
        self.meta = types.SimpleNamespace(client=self)
        self.before_transaction = None
        self.fail_transaction = None

    @staticmethod
    def _key(key):
        return key['pk'],key['sk']

    def _check(self,key,spec):
        item = self.items.get(self._key(key),{})
        names = spec.get('ExpressionAttributeNames',{})
        values = spec.get('ExpressionAttributeValues',{})
        expression = spec.get('ConditionExpression','')
        for clause in expression.split(' AND '):
            if not clause:
                continue
            if clause.startswith('attribute_not_exists('):
                valid = clause[len('attribute_not_exists('):-1] not in item
            elif clause.startswith('attribute_exists('):
                valid = clause[len('attribute_exists('):-1] in item
            else:
                left,right = clause.split(' = ')
                actual = item
                for path in left.split('.'):
                    actual = actual.get(names.get(path,path)) if isinstance(actual,dict) else None
                valid = actual == values[right]
            if not valid:
                raise ClientError({'Error':{'Code':'ConditionalCheckFailedException','Message':expression}},'Write')

    def put_item(self,Item,**kwargs):
        self._check(Item,kwargs)
        self.items[self._key(Item)] = copy.deepcopy(Item)
        return {}

    def get_item(self,Key,**kwargs):
        item = self.items.get(self._key(Key))
        return {'Item':copy.deepcopy(item)} if item else {}

    def delete_item(self,Key,**kwargs):
        self._check(Key,kwargs)
        self.items.pop(self._key(Key),None)
        return {}

    def transact_write_items(self,TransactItems):
        if self.before_transaction:
            callback,self.before_transaction = self.before_transaction,None
            callback()
        if self.fail_transaction:
            error,self.fail_transaction = self.fail_transaction,None
            raise error
        # Mirror DynamoDB: a failed condition cancels the whole transaction with
        # TransactionCanceledException and one CancellationReason per item,
        # not a bare ConditionalCheckFailedException.
        backup = copy.deepcopy(self.items)
        reasons = [{'Code':'None'} for _ in TransactItems]
        failed = False
        for index, operation in enumerate(TransactItems):
            kind,spec = next(iter(operation.items()))
            try:
                if kind == 'Put':
                    self.put_item(**{k:v for k,v in spec.items() if k!='TableName'})
                elif kind == 'ConditionCheck':
                    self._check(spec['Key'],spec)
                else:
                    raise AssertionError(kind)
            except ClientError as exc:
                if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                    self.items = backup
                    raise
                reasons[index] = {'Code':'ConditionalCheckFailed',
                                  'Message':'The conditional request failed'}
                failed = True
        if failed:
            self.items = backup
            raise ClientError({'Error':{'Code':'TransactionCanceledException',
                                        'Message':'Transaction cancelled'},
                               'CancellationReasons':reasons}, 'TransactWriteItems')
        return {}

    def query(self,KeyConditionExpression,**kwargs):
        pk = KeyConditionExpression._values[1]
        return {'Items':[copy.deepcopy(v) for (p,_),v in self.items.items() if p==pk]}


class TestPlaylistCrud(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        self.env = patch.dict(os.environ, {'PHOTOS_PREFIX':'photos/'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.table = FakeTable()

    def test_create_defaults(self):
        p=store.create_playlist(self.table,'  Family Favorites  ')
        self.assertEqual(p['name'],'Family Favorites')
        self.assertEqual(p['items'],[])
        self.assertEqual(p['settings'],store.DEFAULT_SETTINGS)
        self.assertEqual(p['revision'],1)
        self.assertNotIn('pk',p)

    def test_create_requires_name(self):
        with self.assertRaises(ValueError): store.create_playlist(self.table,'  ')

    def test_get_roundtrip(self):
        p=store.create_playlist(self.table,'Trip')
        self.assertEqual(store.get_playlist(self.table,p['id']),p)
        self.assertIsNone(store.get_playlist(self.table,'missing'))

    def test_order_and_revision(self):
        p=store.create_playlist(self.table,'Trip')
        keys=['photos/c.jpg','photos/a.jpg','photos/b.jpg']
        p=store.update_playlist(self.table,p['id'],items=keys,expected_revision=1)
        self.assertEqual(p['items'],keys)
        p=store.update_playlist(self.table,p['id'],items=keys[::-1],expected_revision=2)
        self.assertEqual(p['items'],keys[::-1])
        self.assertEqual(p['revision'],3)

    def test_settings_merge(self):
        p=store.create_playlist(self.table,'Trip')
        p=store.update_playlist(self.table,p['id'],settings={'slide_seconds':60},expected_revision=1)
        self.assertEqual(p['settings'],store.DEFAULT_SETTINGS|{'slide_seconds':60})
        with self.assertRaises(ValueError):
            store.update_playlist(self.table,p['id'],settings={'mode':'party'},expected_revision=2)

    def test_missing(self):
        self.assertIsNone(store.update_playlist(self.table,'missing',name='x',expected_revision=1))

    def test_item_cap(self):
        p=store.create_playlist(self.table,'Big')
        with self.assertRaises(ValueError):
            store.update_playlist(self.table,p['id'],items=[f'photos/{i}.jpg' for i in range(1001)],expected_revision=1)

    def test_delete_preserves_published_pointer(self):
        p=store.create_playlist(self.table,'A')
        active={'pk':'CONFIG','sk':'active','playlist_id':p['id'],'snapshot':copy.deepcopy(p)}
        self.table.put_item(Item=active)
        store.delete_playlist(self.table,p['id'],expected_revision=1)
        self.assertIsNone(store.get_playlist(self.table,p['id']))
        self.assertEqual(store.get_active(self.table)['snapshot'],p)

    def test_list_marks_active(self):
        p=store.create_playlist(self.table,'A')
        self.table.put_item(Item={'pk':'CONFIG','sk':'active','playlist_id':p['id']})
        self.assertTrue(store.list_playlists(self.table)[0]['is_active'])

    def test_legacy_revision_zero_no_implicit_migration(self):
        item={'pk':'PLAYLIST','sk':'old','name':'legacy','settings':store.DEFAULT_SETTINGS,'items':[]}
        self.table.put_item(Item=item)
        self.assertEqual(store.get_playlist(self.table,'old')['revision'],0)
        self.assertNotIn('revision',self.table.items[('PLAYLIST','old')])
        p=store.update_playlist(self.table,'old',name='reviewed',expected_revision=0)
        self.assertEqual(p['revision'],1)
        with self.assertRaises(store.Conflict):
            store.update_playlist(self.table,'old',name='stale',expected_revision=0)

    def test_stale_delete(self):
        p=store.create_playlist(self.table,'A')
        store.update_playlist(self.table,p['id'],name='new',expected_revision=1)
        with self.assertRaises(store.Conflict): store.delete_playlist(self.table,p['id'],expected_revision=1)

    def test_concurrent_append_wins_over_stale_reorder(self):
        from unittest.mock import patch
        p=store.create_playlist(self.table,'A')
        p=store.update_playlist(self.table,p['id'],items=['photos/a.jpg','photos/b.jpg'],expected_revision=1)
        original=self.table.put_item
        def interleave(**kwargs):
            self.table.put_item=original
            store.update_playlist(self.table,p['id'],items=p['items']+['photos/c.jpg'],expected_revision=2)
            return original(**kwargs)
        with patch.object(self.table,'put_item',side_effect=interleave):
            with self.assertRaises(store.Conflict):
                store.update_playlist(self.table,p['id'],items=p['items'][::-1],expected_revision=2)
        self.assertEqual(store.get_playlist(self.table,p['id'])['items'],p['items']+['photos/c.jpg'])

    def test_delete_between_read_and_write_cannot_resurrect(self):
        from unittest.mock import patch
        p=store.create_playlist(self.table,'A')
        original=self.table.put_item
        def interleave(**kwargs):
            store.delete_playlist(self.table,p['id'],expected_revision=1)
            return original(**kwargs)
        with patch.object(self.table,'put_item',side_effect=interleave):
            with self.assertRaises(store.Conflict):
                store.update_playlist(self.table,p['id'],name='resurrected',expected_revision=1)
        self.assertIsNone(store.get_playlist(self.table,p['id']))

    def test_missing_revision_fails_closed(self):
        p=store.create_playlist(self.table,'A')
        with self.assertRaises(ValueError): store.update_playlist(self.table,p['id'],name='unsafe')
        with self.assertRaises(ValueError): store.delete_playlist(self.table,p['id'])


class TestActivePointer(unittest.TestCase):
    def test_activation_requires_publish(self):
        with self.assertRaises(ValueError): store.set_active(FakeTable(),'pid')

    def test_clear_does_not_inherit_metadata(self):
        t=FakeTable()
        t.put_item(Item={'pk':'CONFIG','sk':'active','resolved_start_epoch':99,'last_version':'old',
                         'snapshot':{'name':'last good'}})
        store.clear_active(t)
        active=store.get_active(t)
        self.assertNotIn('resolved_start_epoch',active)
        self.assertNotIn('last_version',active)
        self.assertEqual(active['previous_snapshot']['name'],'last good')


class TestJsonable(unittest.TestCase):
    def test_decimal_conversion(self):
        self.assertEqual(store.to_jsonable({'a':Decimal('5'),'b':[Decimal('1.5')]}),{'a':5,'b':[1.5]})
