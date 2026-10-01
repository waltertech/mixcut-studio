"""Cache cleanup must release disk space without touching files or active readers."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from mixcut.server import Application


class CacheManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Application(self.root / 'state')
        self.cache = self.app.store.directory / 'cache' / 'seekable'
        self.cache.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_clear_preserves_sources_archives_links_and_active_cache(self):
        source = self.root / 'source.ts'
        source.write_bytes(b'original')
        (self.cache / 'unused.mp4').write_bytes(b'cache')
        (self.cache / 'active.mp4').write_bytes(b'active')
        (self.cache / 'linked.mp4').symlink_to(source)
        work = self.app.store.directory / 'work' / 'abc' / '1'
        work.mkdir(parents=True)
        (work / 'ffmpeg.log').write_bytes(b'active work')
        idle = work.with_name('2'); idle.mkdir(); (idle / 'log').write_bytes(b'idle')
        preview = self.app.store.directory / 'thumbnails'; preview.mkdir()
        (preview / 'thumb.jpg').write_bytes(b'preview')
        def render():
            result = self.app.clear_cache()
            self.assertGreater(result['bytes'], 0)
            self.assertEqual(1, result['active_tasks'])
            self.assertTrue((self.cache / 'active.mp4').exists())
            self.assertTrue(work.exists())
        self.app.render_with_cache('abc', {'id':'1','segments':[{'asset_id':'active'}]}, render)
        self.assertFalse((self.cache / 'unused.mp4').exists())
        self.assertFalse(idle.exists())
        self.assertFalse((preview / 'thumb.jpg').exists())
        self.assertEqual(b'original', source.read_bytes())
        self.assertTrue((self.cache / 'linked.mp4').is_symlink())
        self.assertEqual(0, self.app.clear_cache()['bytes'])
        self.assertEqual(b'original', source.read_bytes())

    def test_audit_cleanup_is_deferred_until_cache_reader_finishes(self):
        cached = self.cache / 'shared.mp4'; cached.write_bytes(b'cache')
        def render():
            self.app.cleanup_task_cache({}, {'segments':[{'asset_id':'shared'}]})
            self.assertTrue(cached.exists())
            self.assertEqual(['shared'], self.app.store.get('cache-cleanup-pending'))
        self.app.render_with_cache('abc', {'id':'1','segments':[{'asset_id':'shared'}]}, render)
        self.assertFalse(cached.exists())
        self.assertEqual([], self.app.store.get('cache-cleanup-pending'))

    def test_approval_deletes_conversion_cache_and_keeps_reviewed_video(self):
        output = self.root / 'output.mp4'; output.write_bytes(b'finished video')
        stat = output.stat()
        record = {'id':'abc123','created_at':1,'status':'completed','config':{'output_dir':str(self.root)},'output_folder':str(self.root),'items':[
            {'id':'1','index':1,'status':'success','output_path':str(output),
             'segments':[{'asset_id':'video'}], 'music':[],
             'result':{'output_size':stat.st_size,'output_mtime_ns':stat.st_mtime_ns}}]}
        self.app.store.put('batch:abc123',record)
        cached=self.cache/'video.mp4';cached.write_bytes(b'cache')
        self.app.approve({'batch_id':'abc123','item_id':'1','review_dir':str(self.root/'reviewed')})
        self.assertFalse(cached.exists())
        review=self.app.batch('abc123')['items'][0]['review']
        self.assertEqual(b'finished video',Path(review['path']).read_bytes())

    def test_sticker_approval_cleans_original_segment_cache(self):
        self.app.store.put('batch:abc123',{'items':[{'id':'1','segments':[{'asset_id':'video'}]}]})
        cached=self.cache/'video.mp4';cached.write_bytes(b'cache')
        self.app.cleanup_task_cache({'sticker_origin':{'batch_id':'abc123','item_id':'1'}}, {'segments':[]})
        self.assertFalse(cached.exists())

    def test_active_preview_is_preserved(self):
        preview=self.app.store.directory/'keyframes'/'test';preview.mkdir()
        (preview/'image.jpg').write_bytes(b'preview')
        with self.app.cached_preview():
            self.assertEqual(1,self.app.clear_cache()['active_tasks'])
            self.assertTrue(preview.exists())
        self.app.clear_cache()
        self.assertFalse(preview.exists())

    def test_retry_only_failed_visible_tasks_and_preserves_running_batch(self):
        self.app.store.put('batch:abc123',{'id':'abc123','created_at':1,'status':'running','items':[
            {'id':'1','status':'failed','attempts':3,'error':'failed','progress':.5},
            {'id':'2','status':'success'}, {'id':'3','status':'running'},
            {'id':'4','status':'failed','dismissed':True}]})
        result=self.app.retry_failed_tasks()
        self.assertEqual(1,result['retried'])
        batch=self.app.batch('abc123')
        self.assertEqual('running',batch['status'])
        self.assertEqual(['pending','success','running','failed'],[i['status'] for i in batch['items']])
        self.assertEqual(0,batch['items'][0]['attempts'])
        self.assertEqual(0,self.app.retry_failed_tasks()['retried'])
