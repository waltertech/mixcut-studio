from datetime import datetime
"""Approved retention, pending shared-source cleanup and deferred record pruning."""
import copy,unittest
from pathlib import Path
from unittest.mock import patch
import test_v157 as fixture
from mixcut.media import path_identity

class ApprovedRetirementTests(unittest.TestCase):
 setUp=fixture.RetainedReviewTests.setUp
 def test_finished_approval_hides_then_removes_record_preserving_work(self):
  extra=self.folder/'cover.txt';extra.write_text('keep cover')
  self.app.approve({'batch_id':'abc123','item_id':'1'})
  self.assertEqual([],self.app.visible_batches())
  self.app.prune_forgotten_items()
  self.assertIsNone(self.app.store.get('batch:abc123'))
  self.assertTrue(self.video.exists());self.assertEqual('keep cover',extra.read_text())
 def test_shared_sources_keep_pending_record_until_both_approved(self):
  rawdir=self.root/'raw';rawdir.mkdir();raw=rawdir/'source.mp4';raw.write_bytes(b'raw');st=raw.stat()
  asset={'id':path_identity(raw),'path':str(raw),'size':st.st_size,'mtime_ns':st.st_mtime_ns,'identity_mode':'path'}
  self.item.update(segments=[{'asset_id':asset['id'],'path':str(raw),'start':0,'duration':3}],video_assets=[asset])
  second=copy.deepcopy(self.item);second.update(id='2',index=2)
  folder=self.folder.parent/(datetime.now().astimezone().strftime('%y%m%d')+'-002');folder.mkdir();video=folder/'second.mp4';video.write_bytes(b'second');st=video.stat()
  second.update(output_bundle=str(folder),output_path=str(video),result={'output_size':st.st_size,'output_mtime_ns':st.st_mtime_ns})
  self.batch['items'].append(second);self.batch['config']['source_video_dir']=str(rawdir);self.app.store.put('batch:abc123',self.batch)
  self.app.approve({'batch_id':'abc123','item_id':'1'})
  self.app.prune_forgotten_items();self.assertTrue(raw.exists());self.assertEqual(2,len(self.app.batch('abc123')['items']))
  self.app.approve({'batch_id':'abc123','item_id':'2'})
  self.assertFalse(raw.exists());self.assertEqual([],self.app.visible_batches())
  self.app.prune_forgotten_items();self.assertIsNone(self.app.store.get('batch:abc123'))
  self.assertTrue(self.video.exists());self.assertTrue(video.exists())
 def test_failed_cleanup_retains_record_until_retry(self):
  with patch.object(self.app,'cleanup_task_cache',side_effect=OSError('offline')):
   self.app.approve({'batch_id':'abc123','item_id':'1'})
  self.app.prune_forgotten_items();self.assertEqual(1,len(self.app.visible_batches()))
  self.assertTrue(self.video.exists())
  self.app.retry_approved_cleanup('abc123','1');self.app.prune_forgotten_items()
  self.assertIsNone(self.app.store.get('batch:abc123'));self.assertTrue(self.video.exists())
 def test_active_batch_keeps_indexes_until_worker_finishes(self):
  self.batch['status']='running';self.app.store.put('batch:abc123',self.batch)
  self.app.approve({'batch_id':'abc123','item_id':'1'});self.app.prune_forgotten_items()
  self.assertEqual(1,len(self.app.batch('abc123')['items']));self.assertEqual([],self.app.visible_batches())
  self.app.store.update('abc123',lambda b:b.update(status='completed'));self.app.prune_forgotten_items()
  self.assertIsNone(self.app.store.get('batch:abc123'));self.assertTrue(self.video.exists())
 def test_queued_cache_cleanup_blocks_retirement(self):
  self.item['segments']=[{'asset_id':'shared','path':'/unused'}];self.item['review']={'status':'approved','retained':True};self.item['cleanup']={'output_retained':True,'temporary_segments_deleted':True,'original_recordings_deleted':True}
  self.app.store.put('cache-cleanup-pending',['shared'])
  self.assertFalse(self.app.approved_record_ready(self.batch,self.item))
 def test_sticker_dependency_keeps_internal_origin(self):
  self.app.approve({'batch_id':'abc123','item_id':'1'})
  self.app.store.put('batch:dependent',{'id':'dependent','status':'draft','items':[{'id':'x','status':'pending'}],'sticker_origin':{'batch_id':'abc123','item_id':'1'}})
  self.app.prune_forgotten_items();self.assertIsNotNone(self.app.store.get('batch:abc123'));self.assertTrue(self.video.exists())

 def test_running_schedule_does_not_lose_completed_count(self):
  self.batch['scheduled_run_id']='run1';self.app.store.put('batch:abc123',self.batch)
  self.app.store.put('scheduled-run:run1',{'id':'run1','status':'waiting'})
  self.app.approve({'batch_id':'abc123','item_id':'1'});self.app.prune_forgotten_items()
  self.assertEqual(1,len(self.app.batch('abc123')['items']))
  self.app.store.put('scheduled-run:run1',{'id':'run1','status':'completed'});self.app.prune_forgotten_items()
  self.assertIsNone(self.app.store.get('batch:abc123'));self.assertTrue(self.video.exists())

 def test_retirement_does_not_wait_for_cache_lock(self):
  import threading
  self.app.approve({'batch_id':'abc123','item_id':'1'})
  done=threading.Event()
  def retire():
   try:self.app.prune_forgotten_items()
   finally:done.set()
  with self.app.cache_lock:
   worker=threading.Thread(target=retire);worker.start()
   self.assertTrue(done.wait(1),'store/cache lock inversion must not block task pruning')
  worker.join(timeout=2);self.assertTrue(self.video.exists())
