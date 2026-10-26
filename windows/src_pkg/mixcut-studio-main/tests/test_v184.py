"""Task-folder recycling separates unreviewed and approved outputs."""
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from mixcut.server import Application
import test_v157 as retained_fixture

class FolderDeletionTests(unittest.TestCase):
 setUp=retained_fixture.RetainedReviewTests.setUp
 recycle=retained_fixture.RetainedReviewTests.recycle
 def test_empty_reservation_deleted_and_number_reusable(self):
  self.video.unlink()
  self.app.forget_items([{'batch_id':'abc123','item_ids':['1']}])
  self.assertFalse(self.folder.exists())
  next_folder,_=self.app.reserve_review_bundle(self.folder.parent,{},label=self.output.name)
  self.assertEqual(datetime.now().astimezone().strftime('%y%m%d') + '-001',next_folder.name)
 def test_unreviewed_bundle_and_documents_recycled_sources_untouched(self):
  source=self.root/'raw.mp4';source.write_bytes(b'raw')
  song=self.root/'song.mp3';song.write_bytes(b'music')
  (self.folder/'封面.txt').write_text('cover')
  self.item.update(segments=[{'path':str(source)}],music=[{'path':str(song)}])
  self.app.store.put('batch:abc123',self.batch)
  with patch('mixcut.trash.move_to_trash',side_effect=self.recycle):
   result=self.app.forget_items([{'batch_id':'abc123','item_ids':['1']}])
  self.assertEqual(1,result['deleted_items']);self.assertFalse(self.folder.exists())
  self.assertTrue((self.root/'trash'/self.folder.name/'封面.txt').exists())
  self.assertTrue(source.exists());self.assertTrue(song.exists())
 def test_active_task_deferred_and_retry_survives_restart(self):
  self.item['status']='running';self.batch['status']='running'
  self.app.store.put('batch:abc123',self.batch)
  self.app.forget_items([{'batch_id':'abc123','item_ids':['1']}])
  self.assertTrue(self.video.exists());self.assertTrue(self.app.store.records('forgotten-output:'))
  self.app.store.update('abc123',lambda b:b['items'][0].update(status='cancelled'))
  restarted=Application(self.root/'state');self.addCleanup(restarted.closing.set)
  with patch('mixcut.trash.move_to_trash',side_effect=self.recycle):restarted.cleanup_forgotten_outputs()
  self.assertFalse(self.folder.exists());self.assertFalse(restarted.store.records('forgotten-output:'))
 def test_approved_task_only_removes_row(self):
  self.app.approve({'batch_id':'abc123','item_id':'1'})
  before={p.name:p.read_bytes() for p in self.folder.iterdir()}
  with patch('mixcut.trash.move_to_trash',side_effect=AssertionError('approved files must stay')):
   self.app.forget_all_items()
  self.assertEqual(before,{p.name:p.read_bytes() for p in self.folder.iterdir()})
  self.assertFalse(self.app.store.records('forgotten-output:'))
 def test_recycling_failure_keeps_receipt_and_does_not_block_row_deletion(self):
  with patch('mixcut.trash.move_to_trash',side_effect=OSError('offline')):
   result=self.app.forget_items([{'batch_id':'abc123','item_ids':['1']}])
  self.assertEqual(1,result['deleted_items']);self.assertTrue(result['preview_cleanup_errors'])
  self.assertTrue(self.video.exists());self.assertTrue(self.app.store.records('forgotten-output:'))
  with patch('mixcut.trash.move_to_trash',side_effect=self.recycle):self.app.cleanup_forgotten_outputs()
  self.assertFalse(self.folder.exists())

 def test_approval_finishing_during_delete_keeps_output(self):
  self.item['status']='running';self.batch['status']='running'
  self.app.store.put('batch:abc123',self.batch)
  self.app.forget_items([{'batch_id':'abc123','item_ids':['1']}])
  self.app.store.update('abc123',lambda b:b['items'][0].update(status='success',review={'status':'approved'}))
  self.app.cleanup_forgotten_outputs()
  self.assertTrue(self.video.exists());self.assertFalse(self.app.store.records('forgotten-output:'))
