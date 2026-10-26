"""Latest-scan recycling preserves music, changed files and task sources."""
import copy
import unittest
from unittest.mock import patch
import test_v157 as fixture
from mixcut import media

class ScanCleanupTests(unittest.TestCase):
 setUp=fixture.RetainedReviewTests.setUp
 def prepare(self):
  self.raw=self.root/'raw';self.raw.mkdir();self.music=self.root/'music';self.music.mkdir()
  self.bad=self.raw/'broken.mp4';self.bad.write_bytes(b'bad')
  st=self.bad.stat();self.error={'path':str(self.bad),'error':'moov atom not found','kind':'video','snapshot':{'size':st.st_size,'mtime_ns':st.st_mtime_ns,'dev':st.st_dev,'ino':st.st_ino}}
  self.library={'video_dir':str(self.raw),'music_dir':str(self.music),'scan':{'videos':[],'music':[],'errors':[self.error]}}
  self.app.store.put('library',self.library)
 def test_invalid_video_recycled_and_errors_persistently_removed(self):
  self.prepare()
  with patch('mixcut.media._asset',side_effect=ValueError('moov atom not found')):
   result=self.app.clear_scan_errors()
  self.assertEqual([str(self.bad)],result['removed']);self.assertFalse(self.bad.exists())
  self.assertEqual([],self.app.store.get('library')['scan']['errors'])
  self.assertEqual([],self.app.clear_scan_errors()['removed'])
 def test_changed_file_is_kept_without_probing(self):
  self.prepare();self.bad.write_bytes(b'recording continues')
  with patch('mixcut.media._asset') as probe:
   result=self.app.clear_scan_errors();probe.assert_not_called()
  self.assertEqual(1,len(result['skipped']));self.assertTrue(self.bad.exists())
 def test_recovered_video_kept(self):
  self.prepare()
  with patch('mixcut.media._asset',return_value={}):result=self.app.clear_scan_errors()
  self.assertEqual(1,len(result['skipped']));self.assertTrue(self.bad.exists())
 def test_task_reference_kept(self):
  self.prepare();self.item['segments']=[{'path':str(self.bad)}];self.app.store.put('batch:abc123',self.batch)
  with patch('mixcut.media._asset') as probe:
   result=self.app.clear_scan_errors();probe.assert_not_called()
  self.assertEqual(1,len(result['skipped']));self.assertTrue(self.bad.exists())
 def test_music_and_directory_errors_never_recycled(self):
  self.prepare();audio=self.music/'keep.mp3';audio.write_bytes(b'audio')
  errors=self.library['scan']['errors'];errors[0]['kind']='music';errors.append({'path':str(self.raw),'error':'access'})
  self.app.store.put('library',self.library)
  with patch('mixcut.server.move_to_trash') as trash:
   self.app.clear_scan_errors();trash.assert_not_called()
  self.assertTrue(audio.exists());self.assertTrue(self.bad.exists())
 def test_trash_failure_retains_error_for_retry(self):
  self.prepare()
  with patch('mixcut.media._asset',side_effect=ValueError('bad')),patch('mixcut.server.move_to_trash',side_effect=OSError('offline')):
   result=self.app.clear_scan_errors()
  self.assertEqual(1,len(result['failed']));self.assertTrue(self.bad.exists());self.assertEqual(1,len(self.app.store.get('library')['scan']['errors']))
 def test_concurrent_scan_and_incomplete_refresh_block_cleanup(self):
  self.prepare();self.app.scan_lock.acquire()
  try:
   with self.assertRaises(ValueError):self.app.clear_scan_errors()
  finally:self.app.scan_lock.release()
  self.library['refresh_required']=True;self.app.store.put('library',self.library)
  with self.assertRaises(ValueError):self.app.clear_scan_errors()
  self.assertTrue(self.bad.exists())
 def test_file_changes_during_probe_kept(self):
  self.prepare()
  def probing(*args,**kwargs):self.bad.write_bytes(b'new recording');raise ValueError('bad')
  with patch('mixcut.media._asset',side_effect=probing):result=self.app.clear_scan_errors()
  self.assertEqual(1,len(result['skipped']));self.assertTrue(self.bad.exists())
 def test_scan_records_failed_file_identity(self):
  self.prepare()
  with patch('mixcut.media._cached_asset',side_effect=ValueError('bad')):
   errors=[];media._scan_paths([self.bad],'video',self.raw,{}, {},errors)
  self.assertEqual('video',errors[0]['kind']);self.assertEqual(self.error['snapshot'],errors[0]['snapshot'])
