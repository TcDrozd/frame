"""Scheduler never reads mutable drafts, including during legacy cutover."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','src'))
from scheduled import handler
ENV={'PHOTOS_BUCKET':'b','PHOTOS_PREFIX':'photos/','MANIFEST_KEY':'m.json','PLAYLIST_TABLE':'t'}
RESULT={'version':'v1','photo_count':3,'pool_count':5,'skipped_keys':[]}

class TestScheduledHandler(unittest.TestCase):
    def run_handler(self,active,mode='window'):
        with patch.dict(os.environ,ENV|{'AUTO_PUBLISH_MODE':mode}), \
             patch.object(handler.boto3,'client',MagicMock()), \
             patch.object(handler.boto3,'resource',MagicMock()), \
             patch.object(handler.store,'get_active',return_value=active), \
             patch.object(handler.store,'get_playlist',side_effect=AssertionError('must not read draft')), \
             patch.object(handler.publishing,'publish_playlist',return_value=RESULT) as curated, \
             patch.object(handler.publishing,'publish_auto',return_value=RESULT) as auto, \
             patch.object(handler.publication,'recover',return_value=RESULT) as recover:
            result=handler.lambda_handler({},None)
        return result,curated,auto,recover

    def test_off_no_active(self):
        result,curated,auto,_=self.run_handler(None,'off')
        self.assertFalse(result['published']); curated.assert_not_called(); auto.assert_not_called()

    def test_window_no_active(self):
        result,_,auto,_=self.run_handler(None)
        self.assertTrue(result['published'])
        self.assertIsNone(auto.call_args.kwargs['expected_state_token'])

    def test_window_frozen_epoch(self):
        _,_,auto,_=self.run_handler({'source':'auto','state_token':'x','resolved_start_epoch':123})
        self.assertEqual(auto.call_args.kwargs['resolved_start_epoch'],123)

    def test_snapshot_only(self):
        snapshot={'id':'p','name':'published','revision':2,'items':['photos/a.jpg'],'settings':{'mode':'sync'}}
        result,curated,auto,_=self.run_handler({'playlist_id':'p','snapshot':snapshot,'state_token':'t','resolved_start_epoch':99})
        self.assertTrue(result['published']); auto.assert_not_called()
        self.assertEqual(curated.call_args.kwargs['playlist'],snapshot)
        self.assertEqual(curated.call_args.kwargs['resolved_start_epoch'],99)
        self.assertTrue(curated.call_args.kwargs['refresh'])
        self.assertEqual(curated.call_args.kwargs['expected_state_token'],'t')

    def test_legacy_curated_no_migration(self):
        result,curated,auto,_=self.run_handler({'playlist_id':'gone','resolved_start_epoch':99})
        self.assertFalse(result['published']); curated.assert_not_called(); auto.assert_not_called()
        self.assertIn('legacy',result['reason'])

    def test_legacy_auto_no_migration(self):
        result,curated,auto,_=self.run_handler({'source':'auto','resolved_start_epoch':99})
        self.assertFalse(result['published']); curated.assert_not_called(); auto.assert_not_called()

    def test_recover_pending_before_selecting_anything(self):
        result,curated,auto,recover=self.run_handler({'pending':{'id':'x'}})
        self.assertTrue(result['recovered']); curated.assert_not_called(); auto.assert_not_called()
        recover.assert_called_once()
