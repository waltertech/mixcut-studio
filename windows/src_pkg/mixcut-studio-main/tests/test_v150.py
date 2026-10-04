"""Nonstop audio, bounded detection, persistence and short-output lifecycle."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from mixcut import media, renderer
from mixcut.nonstop import MusicEdges, boundaries
from mixcut.server import Application, Store


class NonstopTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.edges=MusicEdges(Store(self.root/'state'))
    def tearDown(self):self.temp.cleanup()
    def song(self):
        p=self.root/'music.wav';p.write_bytes(b'fixture')
        return {'id':'song','path':str(p),'duration':20}
    def test_boundary_parser_preserves_internal_silence(self):
        self.assertEqual((0,10),boundaries('silence_start: 4\nsilence_end: 5',10))
        self.assertEqual((2,8),boundaries('silence_start: 0\nsilence_end: 2\nsilence_start: 8',10))
    def test_persistent_cache_and_file_invalidation(self):
        song=self.song()
        with patch.object(MusicEdges,'_scan',return_value=(1,9)) as scan:
            result=self.edges.detect(song);self.assertEqual(2,scan.call_count)
            MusicEdges(Store(self.root/'state')).detect(song);self.assertEqual(2,scan.call_count)
            Path(song['path']).write_bytes(b'changed')
            self.edges.detect(song);self.assertEqual(4,scan.call_count)
            self.assertGreater(result['effective_duration'],0)
    def test_concurrent_requests_share_detection(self):
        song=self.song();results=[]
        with patch.object(MusicEdges,'_scan',return_value=(0,10)) as scan:
            workers=[threading.Thread(target=lambda:results.append(self.edges.detect(song))) for _ in range(4)]
            for t in workers:t.start()
            for t in workers:t.join()
            self.assertEqual(4,len(results));self.assertEqual(2,scan.call_count)
    def test_cancelled_detection_is_not_cached(self):
        with self.assertRaises(InterruptedError):self.edges.detect(self.song(),lambda:True)
        self.assertEqual([],self.edges.store.records('music-edges:'))
    def test_all_silence_is_bounded_and_not_cached(self):
        with patch.object(MusicEdges,'_scan',return_value=(20,0)) as scan:
            with self.assertRaises(ValueError):self.edges.detect(self.song())
            self.assertLessEqual(scan.call_count,2)
        self.assertEqual([],self.edges.store.records('music-edges:'))
    def test_shortening_keeps_original_plan_and_does_not_modify_music(self):
        song=self.song();item={'duration':40,'music':[song,song]}
        with patch.object(self.edges,'detect',return_value={'start':2,'end':12,'effective_duration':10}):
            result=self.edges.prepare(item)
        self.assertEqual(40,result['planned_duration']);self.assertAlmostEqual(19.45,result['duration'])
        self.assertEqual(40,item['duration']);self.assertNotIn('nonstop_edges',song)


@unittest.skipUnless(shutil.which('ffmpeg'),'ffmpeg required')
class RealNonstopTests(unittest.TestCase):
    def test_real_render_silence_removed_shorter_success_and_review_preserves_music(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);song=root/'song.wav';video=root/'v.mp4'
            def ff(*args):return subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True,text=True)
            ff('-f','lavfi','-i','sine=frequency=440:duration=3','-af','adelay=2000:all=1,apad=pad_dur=2',str(song))
            ff('-f','lavfi','-i','color=c=blue:s=640x360:r=24:d=10','-c:v','libx264',str(video))
            before=hashlib.sha256(song.read_bytes()).hexdigest()
            v=media._asset(video,'video');a=media._asset(song,'music',quick_music=True)
            app=Application(root/'state')
            item={'id':'one','duration':8,'segments':[{'asset_id':v['id'],'path':v['path'],'start':0,'duration':8}],
                  'music':[a,copy.deepcopy(a)],'video_assets':[v]}
            batch=app.make_batch({'width':640,'height':360,'fps':24,'hardware':'software_fast',
                                 'nonstop_music':True,'output_dir':str(root/'output')}, {'items':[item]},[v,a])
            app.store.update(batch['id'],lambda b:b.update(status='running'))
            app.execute_item(batch['id'],0)
            current=app.batch(batch['id'])['items'][0]
            self.assertEqual('success',current['status'],current.get('error'))
            self.assertLess(current['duration'],6);self.assertGreater(current['duration'],5)
            self.assertGreater(current['result']['shortened_seconds'],2)
            output=Path(current['output_path'])
            log=subprocess.run(['ffmpeg','-hide_banner','-i',str(output),'-af','silencedetect=noise=-50dB:d=0.3',
                                '-f','null','-'],capture_output=True,text=True).stderr
            self.assertNotIn('silence_start:',log)
            with patch.object(MusicEdges,'_scan',side_effect=AssertionError('must reuse cache')):
                MusicEdges(Store(root/'state')).detect(a)
            app.start_preview_worker();app.preview_queue.join()
            current=app.batch(batch['id'])['items'][0]
            self.assertEqual('ready',current['thumbnails']['status'])
            app.approve({'batch_id':batch['id'],'item_id':'one','review_dir':str(root/'approved')})
            self.assertEqual(before,hashlib.sha256(song.read_bytes()).hexdigest())
            self.assertEqual(1,len(list((root/'approved').rglob('*.mp4'))))
            app.closing.set();app.preview_worker.join(timeout=3)

    def test_mp3_m4a_flac_edge_seek_and_original_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); wav=root/'source.wav';edges=MusicEdges(Store(root/'state'))
            def ff(*args):subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True)
            ff('-f','lavfi','-i','sine=duration=3','-af','adelay=2000:all=1,apad=pad_dur=2',str(wav))
            for extension in ('mp3','m4a','flac'):
                with self.subTest(format=extension):
                    path=root/('song.'+extension);ff('-i',str(wav),str(path))
                    before=path.read_bytes();song=media._asset(path,'music',quick_music=True)
                    result=edges.detect(song)
                    self.assertAlmostEqual(2,result['start'],delta=.12)
                    self.assertAlmostEqual(5,result['end'],delta=.15)
                    self.assertEqual(before,path.read_bytes())

    def test_overestimated_music_duration_shortens_after_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);v=root/'v.mp4';a=root/'a.wav'
            def ff(*args):subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True)
            ff('-f','lavfi','-i','color=s=640x360:r=24:d=5','-c:v','libx264',str(v))
            ff('-f','lavfi','-i','sine=duration=2',str(a))
            va=media._asset(v,'video');song=media._asset(a,'music',quick_music=True)
            song['nonstop_edges']={'start':0,'end':4}
            item={'duration':4,'nonstop':{'crossfades':[]},'music':[song],'video_assets':[va],
                  'segments':[{'asset_id':va['id'],'path':str(v),'start':0,'duration':4}]}
            result=renderer.render(item,{'width':640,'height':360,'fps':24,'hardware':'software_fast'},
                                   str(root/'out.mp4'),str(root/'work'))
            self.assertLess(result['duration'],2.1)
