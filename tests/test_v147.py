import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
from mixcut import media, renderer
from mixcut.server import Application


class PathIdentityTests(unittest.TestCase):
    def test_new_assets_and_verification_never_read_full_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);videos=root/'video';music=root/'music';videos.mkdir();music.mkdir()
            (videos/'same.mp4').write_bytes(b'candidate-video')
            (music/'same.mp3').write_bytes(b'candidate-audio')
            info={'format':{'duration':'30','format_name':'mp4'},'streams':[
                {'codec_type':'video','width':640,'height':360}, {'codec_type':'audio'}]}
            with patch.object(media,'_run_probe',return_value=info), patch.object(media,'_sha256',side_effect=AssertionError('full file hash')):
                scan=media.scan(str(videos),str(music),str(root/'cache'),quick_music=True)
                self.assertEqual([],scan['errors'])
                for asset in scan['videos']+scan['music']:
                    self.assertEqual('path',asset['identity_mode']);self.assertTrue(media.verify_asset(asset))
                changed=videos/'same.mp4';changed.write_bytes(b'changed file')
                with self.assertRaises(ValueError):media.verify_asset(scan['videos'][0])

    def test_same_filename_different_directories_remain_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'a').mkdir();(root/'b').mkdir()
            for folder in ('a','b'):(root/folder/'same.mp4').write_bytes(b'identical contents')
            info={'format':{'duration':'30'},'streams':[{'codec_type':'video'}]}
            with patch.object(media,'_run_probe',return_value=info),patch.object(media,'_sha256',side_effect=AssertionError('hash')):
                scan=media.scan(str(root),str(root),str(root/'cache'),kinds=('video',),quick_music=True)
            self.assertEqual(2,len(scan['videos']))
            self.assertNotEqual(scan['videos'][0]['id'],scan['videos'][1]['id'])

    def test_legacy_index_migrates_without_probe_or_file_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();p=root/'v.mp4';p.write_bytes(b'file');st=p.stat();cache=root/'cache';cache.mkdir()
            old={'id':'old-content-id','index_version':2,'path':str(p),'name':p.name,'duration':30,'mime_type':'video/mp4'}
            (cache/'media-index.json').write_text(json.dumps({'video:'+str(p):{'size':st.st_size,'mtime_ns':st.st_mtime_ns,'asset':old}}))
            with patch.object(media,'_run_probe',side_effect=AssertionError('probe')),patch.object(media,'_sha256',side_effect=AssertionError('hash')):
                result=media.scan(str(root),str(root),str(cache),kinds=('video',))
            self.assertEqual(media.path_identity(p),result['videos'][0]['id'])
            self.assertEqual(3,result['videos'][0]['index_version'])


class ParallelQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.app=Application(self.root/'state')

    def tearDown(self):self.temp.cleanup()

    def save(self,n,parallel):
        record={'id':'abc123','created_at':1,'status':'queued','config':{'output_dir':str(self.root/'out'),'parallel_tasks':parallel},'assets':[],
            'items':[{'id':str(i),'index':i,'status':'pending','duration':1,'attempts':0,'segments':[], 'music':[],
                      'output_path':str(self.root/'out'/f'{i}.mp4')} for i in range(n)]}
        self.app.store.put('batch:abc123',record)

    def test_all_four_limits_are_real_concurrency_limits(self):
        for parallel in (1,2,3,4):
            with self.subTest(parallel=parallel):
                shutil.rmtree(self.root/'out',ignore_errors=True)
                self.save(parallel*2,parallel);barrier=threading.Barrier(parallel);guard=threading.Lock();active=0;maximum=0
                def render(entry,config,output,work,progress):
                    nonlocal active,maximum
                    with guard:active+=1;maximum=max(maximum,active)
                    barrier.wait(timeout=5)
                    Path(output).write_bytes(b'rendered')
                    with guard:active-=1
                    return {'valid':True}
                with patch.object(renderer,'render',side_effect=render):self.app.execute_batch('abc123')
                self.assertEqual(parallel,maximum)
                self.assertEqual('completed',self.app.batch('abc123')['status'])
                self.assertTrue(all(i['status']=='success' for i in self.app.batch('abc123')['items']))

    def test_pause_waits_for_active_pair_and_does_not_start_remaining(self):
        self.save(4,2);ready=threading.Barrier(3);release=threading.Event()
        def render(entry,config,output,work,progress):
            ready.wait(timeout=5);release.wait(timeout=5);Path(output).write_bytes(b'rendered');return {}
        with patch.object(renderer,'render',side_effect=render),ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.app.execute_batch,'abc123')
            try:
                ready.wait(timeout=5);self.app.action('abc123','pause')
            finally:release.set()
            future.result(timeout=5)
        batch=self.app.batch('abc123');self.assertEqual('paused',batch['status'])
        self.assertEqual(['success','success','pending','pending'],[i['status'] for i in batch['items']])

    def test_delete_one_active_task_does_not_cancel_other_task(self):
        self.save(2,2);ready=threading.Barrier(3);release=threading.Event()
        def render(entry,config,output,work,progress):
            ready.wait(timeout=5);release.wait(timeout=5);progress(.3)
            Path(output).write_bytes(b'rendered');return {}
        with patch.object(renderer,'render',side_effect=render),ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.app.execute_batch,'abc123')
            try:
                ready.wait(timeout=5);self.app.forget_items([{'batch_id':'abc123','item_ids':['0']}]);self.app.prune_forgotten_items()
                self.assertEqual(2,len(self.app.batch('abc123')['items']))
            finally:release.set()
            future.result(timeout=5)
        batch=self.app.batch('abc123');self.assertEqual('cancelled',batch['items'][0]['status']);self.assertEqual('success',batch['items'][1]['status'])

    def test_parallel_config_rejects_invalid_values(self):
        for value in (0,5,1.5,True,'garbage'):
            with self.assertRaisesRegex(ValueError,'并行剪辑'):
                self.app.create_plan({'config':{'parallel_tasks':value}})


class SharedCacheTests(unittest.TestCase):
    def test_concurrent_ts_cache_build_only_remuxes_once_and_versions_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.ts';source.write_bytes(b'source');cache=root/'cache'
            segment={'asset_id':'path-id','path':str(source)};guard=threading.Lock();calls=[]
            def remux(command,**kwargs):
                with guard:calls.append(command)
                Path(command[-1]).write_bytes(b'mp4-cache')
                return type('Result',(),{'returncode':0})()
            with patch.object(renderer.subprocess,'run',side_effect=remux),ThreadPoolExecutor(max_workers=4) as pool:
                targets=list(pool.map(lambda _:renderer._seekable_source(segment,cache),range(4)))
                self.assertEqual(1,len(calls));self.assertEqual(1,len(set(targets)))
                source.write_bytes(b'changed source')
                changed=renderer._seekable_source(segment,cache)
                self.assertNotEqual(targets[0],changed);self.assertEqual(2,len(calls))

@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
class ActualParallelRenderTests(unittest.TestCase):
    def test_two_real_outputs_share_one_ts_cache_without_content_hash(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);video=root/'videos';audio=root/'audio';video.mkdir();audio.mkdir()
            def make(*args):subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True,timeout=30)
            make('-f','lavfi','-i','color=c=blue:s=160x90:r=24:d=2','-c:v','mpeg2video','-f','mpegts',str(video/'source.ts'))
            make('-f','lavfi','-i','sine=f=440:r=48000:d=1','-c:a','pcm_s16le',str(audio/'song.wav'))
            with patch.object(media,'_sha256',side_effect=AssertionError('unnecessary full-file hash')):
                scan=media.scan(str(video),str(audio),str(root/'index'),quick_music=True)
                self.assertEqual([],scan['errors']);v=scan['videos'][0];song=scan['music'][0]
                app=Application(root/'state');items=[]
                for i in range(2):
                    items.append({'id':str(i),'index':i,'status':'pending','attempts':0,'duration':1,
                                  'segments':[{'asset_id':v['id'],'path':v['path'],'start':i*.1,'duration':1}],
                                  'video_assets':[v],'music':[song], 'output_path':str(root/'out'/f'{i}.mp4')})
                app.store.put('batch:abc123',{'id':'abc123','created_at':1,'status':'queued','config':{
                    'parallel_tasks':2,'output_dir':str(root/'out'),'width':640,'height':360,'fps':24,
                    'hardware':'software_fast','original_volume':0,'music_volume':1},'assets':[v,song],'items':items})
                barrier=threading.Barrier(2);original=renderer.render
                def together(*args,**kwargs):barrier.wait(timeout=10);return original(*args,**kwargs)
                with patch.object(renderer,'render',side_effect=together):app.execute_batch('abc123')
            result=app.batch('abc123');self.assertEqual('completed',result['status'],result)
            self.assertTrue(all(i['result']['valid'] for i in result['items']))
            self.assertEqual(1,len(list((app.store.directory/'cache'/'seekable').glob('*.mp4'))))
