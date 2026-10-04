"""Recovery checkpoints are exercised through real FFmpeg and isolated queues."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from mixcut import checkpoints, renderer, media
from mixcut.server import Application


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / 'good.mp4'
        self.ff('-f','lavfi','-i','testsrc2=s=320x180:r=30:d=10','-f','lavfi','-i','sine=d=10:r=48000',
                '-c:v','libx264','-g','30','-pix_fmt','yuv420p','-c:a','aac',str(self.video))
        self.song = media._asset(self.video, 'music', quick_music=True)
        self.asset = media._asset(self.video, 'video')
        self.config = {'width':320,'height':180,'fps':30,'hardware':'software'}
        self.item = {'duration':6,'segments':[self.segment(0,6)],'video_assets':[self.asset],'music':[self.song]}

    def ff(self,*args):
        return subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],capture_output=True,check=True,timeout=30)

    def segment(self,start,duration):
        return {'asset_id':self.asset['id'],'path':str(self.video),'start':start,'duration':duration}

    def test_resume_reuses_completed_chunks_and_cleans_after_success(self):
        work=self.root/'work';calls=[]
        original=renderer.render_video_chunk
        def interrupted(seg,*args,**kwargs):
            calls.append(seg['start'])
            if seg['start']==2: raise InterruptedError('interrupted')
            return original(seg,*args,**kwargs)
        with patch.object(checkpoints,'CHUNK_SECONDS',2), patch.object(renderer,'render_video_chunk',side_effect=interrupted):
            with self.assertRaises(InterruptedError): renderer.render(self.item,self.config,self.root/'out.mp4',work)
        first=work/'checkpoints' /'0000.mov'; stamp=first.stat().st_mtime_ns
        calls.clear()
        def track(seg,*args,**kwargs):
            calls.append(seg['start']); self.assertEqual(stamp,first.stat().st_mtime_ns)
            return original(seg,*args,**kwargs)
        with patch.object(checkpoints,'CHUNK_SECONDS',2), patch.object(renderer,'render_video_chunk',side_effect=track):
            result=renderer.render(self.item,self.config,self.root/'out.mp4',work)
        self.assertEqual([2,4],calls); self.assertTrue(result['valid'])
        self.assertEqual(1,len(result['rendered_segments']))
        self.assertEqual('copy',result['assembly_encoder']);self.assertFalse((work/'checkpoints').exists())

    def test_same_length_replacement_when_decoder_cannot_read_source(self):
        bad=self.root/'bad.mp4';bad.write_bytes(b'invalid input')
        self.item['segments'][0]['path']=str(bad);self.item['segments'][0]['asset_id']='bad'
        records=[];config=dict(self.config,video_candidates=[self.asset],record_bad_video=lambda *args:records.append(args))
        with patch.object(checkpoints,'CHUNK_SECONDS',2):
            result=renderer.render(self.item,config,self.root/'out.mp4',self.root/'work')
        self.assertTrue(result['valid']);self.assertAlmostEqual(6,result['duration'])
        self.assertTrue(records);self.assertTrue(result['recovery_warnings'])
        self.assertEqual({self.asset['id']},{s['asset_id'] for s in result['rendered_segments']})

    def test_exhausted_recovery_keeps_prefix_and_shortens(self):
        original=renderer.render_video_chunk
        def fail_later(seg,*args,**kwargs):
            if seg['start']>=2: raise ValueError('Invalid data in decoder')
            return original(seg,*args,**kwargs)
        config=dict(self.config,video_candidates=[],occupied_video_segments=[self.segment(0,10)])
        with patch.object(checkpoints,'CHUNK_SECONDS',2),patch.object(renderer,'render_video_chunk',side_effect=fail_later):
            result=renderer.render(self.item,config,self.root/'out.mp4',self.root/'work')
        self.assertTrue(result['valid']);self.assertAlmostEqual(2,result['duration'])
        self.assertTrue(any('缩短' in s for s in result['recovery_warnings']))

    def test_source_change_invalidates_checkpoint(self):
        work=self.root/'work'
        with patch.object(checkpoints,'CHUNK_SECONDS',2): checkpoints.prepare(self.item,self.config,work)
        import os
        os.utime(self.video,ns=(self.video.stat().st_atime_ns,self.video.stat().st_mtime_ns+10000))
        calls=[];original=renderer.render_video_chunk
        def track(seg,*args,**kwargs):
            calls.append(seg['start']);return original(seg,*args,**kwargs)
        with patch.object(checkpoints,'CHUNK_SECONDS',2),patch.object(renderer,'render_video_chunk',side_effect=track):
            checkpoints.prepare(self.item,self.config,work)
        self.assertEqual([0,2,4],calls)

    def test_original_audio_and_nonstop_survive_checkpoint_joins(self):
        from mixcut.nonstop import MusicEdges
        # Use a separate WAV so this test checks the two independent audio paths.
        music=self.root/'song.wav';self.ff('-f','lavfi','-i','sine=d=4:r=48000',str(music))
        song=media._asset(music,'music',quick_music=True)
        self.item['music']=[song,dict(song)]
        self.item=MusicEdges(Application(self.root/'state').store).prepare(self.item)
        with patch.object(checkpoints,'CHUNK_SECONDS',2):
            result=renderer.render(self.item,dict(self.config,original_volume=.1),self.root/'out.mp4',self.root/'work')
        self.assertTrue(result['valid']);self.assertAlmostEqual(6,result['duration'])

    def test_fractional_frame_lengths_do_not_accumulate_drift(self):
        self.item['duration']=5.31
        self.item['segments']=[self.segment(0,1.77),self.segment(2,1.77),self.segment(4,1.77)]
        result=renderer.render(self.item,self.config,self.root/'out.mp4',self.root/'work')
        self.assertTrue(result['valid']);self.assertLess(abs(5.31-result['duration']),.05)

    def test_no_alternative_yields_instead_of_looping(self):
        with patch.object(renderer,'render_video_chunk',side_effect=ValueError('Invalid data in decoder')):
            with self.assertRaises(checkpoints.VideoRecoveryNeeded):
                checkpoints.prepare(self.item,dict(self.config,video_candidates=[]),self.root/'work')


    def test_parallel_replacement_reservations_prevent_duplicate_spans(self):
        app=Application(self.root/'state');out=self.root/'out';out.mkdir()
        music=self.root/'music.wav';self.ff('-f','lavfi','-i','sine=d=10:r=48000',str(music))
        song=media._asset(music,'music',quick_music=True)
        bad=self.root/'bad.mp4';bad.write_bytes(b'invalid input')
        items=[]
        for i in range(2):
            items.append({'id':str(i),'index':i+1,'status':'pending','duration':3,'attempts':0,
                          'segments':[{'asset_id':'bad','path':str(bad),'start':0,'duration':3}],
                          'video_assets':[], 'music':[song], 'output_path':str(out/f'{i}.mp4')})
        batch={'id':'abc123','created_at':1,'status':'queued','items':items,
               'config':dict(self.config,parallel_tasks=2,output_dir=str(out)), 'assets':[self.asset,song]}
        app.store.put('batch:abc123',batch)
        with patch.object(app,'enqueue_previews'),patch.object(app,'check_space'):
            app.execute_batch('abc123')
        entries=app.batch('abc123')['items']
        self.assertEqual(['success','success'],[entry['status'] for entry in entries])
        a,b=[entry['segments'][0] for entry in entries]
        self.assertFalse(a['start'] < b['start']+b['duration'] and a['start']+a['duration'] > b['start'])
        self.assertEqual(self.asset['id'],entries[0]['video_assets'][0]['id'])
        self.assertTrue(app.store.get('bad-video-sources'))
        self.assertTrue(music.exists())

    def test_complete_checkpoints_survive_missing_source(self):
        work=self.root/'work'
        with patch.object(checkpoints,'CHUNK_SECONDS',2): checkpoints.prepare(self.item,self.config,work)
        self.video.unlink()
        with patch.object(checkpoints,'CHUNK_SECONDS',2),patch.object(renderer,'render_video_chunk') as chunk:
            _,duration,_=checkpoints.prepare(self.item,self.config,work)
        chunk.assert_not_called();self.assertEqual(6,duration)

    def test_recovery_time_budget_preserves_previous_work(self):
        import time
        def fail(*args,**kwargs):
            time.sleep(.02);raise ValueError('Invalid data in decoder')
        with patch.object(checkpoints,'RECOVERY_SECONDS',.001),patch.object(renderer,'render_video_chunk',side_effect=fail):
            with self.assertRaises(checkpoints.VideoRecoveryNeeded):
                checkpoints.prepare(self.item,self.config,self.root/'work')
        self.assertTrue((self.root/'work'/'checkpoints'/'manifest.json').exists())

    def test_cache_space_check_prevents_partial_unbounded_writes(self):
        from collections import namedtuple
        Space=namedtuple('Space','total used free')
        with patch.object(shutil,'disk_usage',return_value=Space(100,99,1)),patch.object(renderer,'render_video_chunk') as chunk:
            with self.assertRaises(OSError): checkpoints.prepare(self.item,self.config,self.root/'work')
        chunk.assert_not_called()

    def test_interval_selection_does_not_miss_available_spans(self):
        self.assertEqual([(2,8)],checkpoints.available_intervals(10,2,[[0,2],[8,10]]))
        self.assertEqual([],checkpoints.available_intervals(10,3,[[0,2],[4,6],[8,10]]))


class QueueTests(unittest.TestCase):
    def test_recovery_pause_releases_slot_and_manual_start_is_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);app=Application(root/'state');out=root/'out';out.mkdir()
            batch={'id':'abc123','created_at':1,'status':'queued','config':{'output_dir':str(out),'parallel_tasks':1},'assets':[],
                   'items':[{'id':str(i),'index':i,'status':'pending','duration':1,'attempts':0,'segments':[], 'music':[],
                             'output_path':str(out/f'{i}.mp4')} for i in range(2)]}
            app.store.put('batch:abc123',batch);calls=[]
            def render(item,config,path,work,progress):
                calls.append(item['id'])
                if item['id']=='0':raise checkpoints.VideoRecoveryNeeded('no alternative')
                Path(path).write_bytes(b'output');return {'duration':1}
            with patch.object(renderer,'render',side_effect=render),patch.object(app,'enqueue_previews'),patch.object(app,'check_space'):
                app.execute_batch('abc123')
            self.assertEqual(['0','1'],calls)
            self.assertEqual('recovery_paused',app.batch('abc123')['items'][0]['status'])
            self.assertEqual('success',app.batch('abc123')['items'][1]['status'])
            app.item_action('abc123','0','start')
            self.assertEqual('pending',app.batch('abc123')['items'][0]['status'])

    def test_approval_or_deletion_removes_work_but_never_music(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);app=Application(root/'state')
            work=app.store.directory/'work'/'batch'/'item';work.mkdir(parents=True);(work/'chunk.mp4').write_bytes(b'cached')
            music=root/'keep.mp3';music.write_bytes(b'important')
            app.remove_task_work('batch','item')
            self.assertFalse(work.exists());self.assertTrue(music.exists())
            work.mkdir(parents=True);(work/'chunk.mp4').write_bytes(b'cached')
            app.cache_users[('batch','item')]=set();app.remove_task_work('batch','item')
            self.assertTrue(work.exists())

if __name__=='__main__':unittest.main()
