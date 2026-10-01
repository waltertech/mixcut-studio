"""Precomputed thumbnails, review gating and deletion lifecycle."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from mixcut.server import Application
from mixcut.keyframes import KeyframeCache


def save(app,root,number='one',path=None):
    output=path or root/(number+'.mp4')
    if path is None:output.write_bytes(b'fixture')
    batch={'id':'abc123','created_at':1,'status':'completed','config':{'output_dir':str(root)},'assets':[],
           'items':[{'id':number,'index':1,'status':'success','duration':65,'music':[], 'segments':[],
                     'output_name':output.name,'output_path':str(output),'result':{}}]}
    app.store.put('batch:abc123',batch)
    return batch


def wait_ready(app):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        item=app.batch('abc123')['items'][0]
        if item.get('thumbnails',{}).get('status') in {'ready','failed'}:return item
        time.sleep(.02)
    raise AssertionError('preview worker did not finish')


class PreviewLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.app=Application(self.root/'state')
    def tearDown(self):
        self.app.closing.set()
        if self.app.preview_worker:self.app.preview_worker.join(timeout=5)
        self.temp.cleanup()

    def test_review_is_blocked_while_previews_are_queued(self):
        save(self.app,self.root);self.app.enqueue_previews('abc123','one')
        with self.assertRaisesRegex(ValueError,'缩略图'):
            self.app.approve({'batch_id':'abc123','item_id':'one','review_dir':str(self.root/'approved')})
        self.assertFalse(self.app.review_ready(self.app.batch('abc123')['items'][0]))
        with self.assertRaises(ValueError):self.app.start_review_all({'review_dir':str(self.root/'approved')})

    def test_deleting_record_removes_small_and_large_images_but_keeps_video(self):
        b=save(self.app,self.root);item=b['items'][0];version=self.app.keyframes._version(item['output_path'])
        directory=self.app.keyframes.root/version;directory.mkdir()
        for name in ('000000-thumb.jpg','000000-large.jpg','index.json'):(directory/name).write_bytes(b'image')
        self.app.forget_items([{'batch_id':'abc123','item_ids':['one']}])
        self.assertFalse(directory.exists());self.assertTrue(Path(item['output_path']).is_file())

    def test_deletion_cancels_generation_and_prevents_recreation(self):
        save(self.app,self.root);started=threading.Event()
        def prepare(path,cancelled):
            started.set()
            while not cancelled():time.sleep(.01)
            raise InterruptedError('cancelled')
        with patch.object(self.app.keyframes,'prepare',side_effect=prepare):
            self.app.start_preview_worker();self.app.enqueue_previews('abc123','one')
            self.assertTrue(started.wait(3))
            self.app.forget_items([{'batch_id':'abc123','item_ids':['one']}])
            self.app.preview_queue.join()
            self.assertFalse(list(self.app.keyframes.root.iterdir()))
            self.assertEqual([],self.app.visible_batches())

    def test_failed_generation_can_retry_without_rerendering(self):
        save(self.app,self.root)
        with patch.object(self.app.keyframes,'prepare',side_effect=ValueError('fixture error')):
            self.app.start_preview_worker();self.app.enqueue_previews('abc123','one')
            self.assertEqual('failed',wait_ready(self.app)['thumbnails']['status'])
            self.app.preview_queue.join()
        def complete(path,cancelled):return {'version':self.app.keyframes._version(path),'total':1}
        with patch.object(self.app.keyframes,'prepare',side_effect=complete):
            self.app.enqueue_previews('abc123','one')
            self.assertEqual('ready',wait_ready(self.app)['thumbnails']['status'])


@unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
class ActualThumbnailTests(unittest.TestCase):
    def test_single_pass_all_timestamps_cached_and_cleaned_on_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);video=root/'video.mp4'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','color=c=blue:s=128x72:r=2:d=65',
                            '-f','lavfi','-i','sine=frequency=440:duration=65','-c:v','libx264','-c:a','aac',str(video)],check=True,capture_output=True)
            app=Application(root/'state');save(app,root,path=video)
            try:
                app.start_preview_worker();app.enqueue_previews('abc123','one');item=wait_ready(app)
                self.assertEqual('ready',item['thumbnails']['status'],item['thumbnails'].get('error'))
                self.assertEqual(3,item['thumbnails']['total'])
                data=app.keyframes.describe(video);self.assertEqual([0.,30.,60.],[f['time'] for f in data['frames']])
                with patch('mixcut.keyframes.subprocess.run',side_effect=AssertionError('must serve cached image')):
                    for index in range(3):self.assertTrue(app.keyframes.image(video,index).is_file())
                version=data['version'];app.keyframes.image(video,0,'large')
                app.approve({'batch_id':'abc123','item_id':'one','review_dir':str(root/'approved')})
                self.assertFalse((app.keyframes.root/version).exists());self.assertFalse(video.exists())
                approved=app.batch('abc123')['items'][0]['review']['path'];self.assertTrue(Path(approved).is_file())
            finally:
                app.closing.set();app.preview_worker.join(timeout=5)

class SerialQueueTests(unittest.TestCase):
    def test_only_one_thumbnail_job_runs_at_a_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);app=Application(root/'state');first=save(app,root)
            second=dict(first['items'][0],id='two',output_path=str(root/'two.mp4'))
            Path(second['output_path']).write_bytes(b'fixture')
            app.store.update('abc123',lambda b:b['items'].append(second))
            active=0;maximum=0;guard=threading.Lock()
            def prepare(path,cancelled):
                nonlocal active,maximum
                with guard:active+=1;maximum=max(maximum,active)
                time.sleep(.04)
                with guard:active-=1
                return {'version':app.keyframes._version(path),'total':1}
            try:
                with patch.object(app.keyframes,'prepare',side_effect=prepare):
                    app.enqueue_previews('abc123','one');app.enqueue_previews('abc123','two')
                    app.start_preview_worker();app.preview_queue.join()
                self.assertEqual(1,maximum)
                self.assertTrue(all(i['thumbnails']['status']=='ready' for i in app.batch('abc123')['items']))
            finally:
                app.closing.set();app.preview_worker.join(timeout=5)
