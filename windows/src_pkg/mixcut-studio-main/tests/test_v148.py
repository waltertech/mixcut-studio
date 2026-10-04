"""Lightweight planning and bounded music recovery regression coverage."""
import random
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from mixcut import media, planner, renderer
from mixcut.server import Application


def asset(identity, duration=20):
    return {'id':identity,'name':identity,'path':'/'+identity,'duration':duration,
            'size':1,'mtime_ns':1,'duration_precise':False}


class LightweightPlanningTests(unittest.TestCase):
    def test_plan_does_not_decode_or_hash_candidate_music(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=Application(Path(tmp)/'state')
            app.store.put('library',{'scan':{'videos':[asset('v',100)],'music':[asset('m')]}})
            with patch.object(media,'precise_music',side_effect=AssertionError('decoded')), \
                 patch.object(media,'_sha256',side_effect=AssertionError('hashed')):
                batch=app.create_plan({'config':{'count':1,'min_duration':10,'max_duration':10,
                                      'min_songs':1,'max_songs':1,'output_dir':tmp}})
            self.assertEqual(10,batch['items'][0]['duration'])
            self.assertFalse(batch['items'][0]['music'][0]['duration_precise'])
            self.assertEqual(3,batch['config']['music_margin_seconds'])

    def test_margin_selects_additional_song_without_changing_video_duration(self):
        songs=[asset('a',10),asset('b',10)]
        order,total=planner.allocate_music(songs,10,{'min_songs':1,'max_songs':2,'music_margin_seconds':3},random.Random(1))
        self.assertEqual(2,len(order));self.assertEqual(20,total)
        with self.assertRaises(ValueError):
            planner.allocate_music(songs,20,{'min_songs':1,'max_songs':2,'music_margin_seconds':3},random.Random(1))

    def test_decoder_error_only_identifies_music_inputs(self):
        item={'segments':[{},{}],'music':[asset('a'),asset('b')]}
        self.assertIsNone(renderer.music_input_error('[aist#0:1/aac] Error processing packet',item))
        error=renderer.music_input_error('[aist#3:1/aac] Error submitting packet to decoder: Invalid data',item)
        self.assertEqual(['b'],error.asset_ids)
        self.assertIsNone(renderer.music_input_error('No space left on device',item))


class MusicRecoveryTests(unittest.TestCase):
    def test_render_failure_replaces_music_and_preserves_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);app=Application(root/'state');songs=[asset('bad'),asset('good')]
            entry={'id':'one','duration':10,'segments':[{'asset_id':'v','start':0,'duration':10}],
                   'music':[songs[0]],'video_assets':[]}
            batch=app.make_batch({'width':640,'height':360,'fps':30,'output_dir':tmp,'music_ids':['bad','good'],'min_songs':1,'max_songs':1},
                                 {'items':[entry]},[asset('v')]+songs)
            app.store.update(batch['id'],lambda b:b.update(status='running'))
            seen=[]
            def render(item,config,output,work,progress):
                seen.append(item['music'][0]['id'])
                if seen[-1]=='bad':raise renderer.MusicInputError('bad audio',['bad'])
                Path(output).write_bytes(b'output');return {'valid':True}
            with patch.object(media,'verify_asset',return_value=True),patch.object(renderer,'render',side_effect=render):
                app.execute_item(batch['id'],0)
            result=app.batch(batch['id'])['items'][0]
            self.assertEqual(['bad','good'],seen);self.assertEqual('success',result['status'])
            self.assertEqual(batch['items'][0]['output_path'],result['output_path'])
            self.assertEqual(1,len(result['music_replacements']))
            self.assertEqual(['bad'],app.batch(batch['id'])['failed_music_ids'])

    def test_failed_music_is_excluded_and_exhaustion_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=Application(Path(tmp)/'state');song=asset('bad')
            batch=app.make_batch({'width':640,'height':360,'fps':30,'output_dir':tmp,'music_ids':['bad'],'min_songs':1,'max_songs':1},
                                 {'items':[{'id':'one','duration':10,'segments':[], 'music':[song]}]},[song])
            with self.assertRaises(ValueError):app._replace_failed_music(batch['id'],0,['bad'])
            self.assertEqual('bad',app.batch(batch['id'])['items'][0]['music'][0]['id'])

class ActualRenderRecoveryTests(unittest.TestCase):
    def test_corrupt_aac_is_replaced_during_real_render(self):
        import json, shutil, subprocess
        if not shutil.which('ffmpeg'):self.skipTest('ffmpeg unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);video=root/'v.mp4';bad=root/'bad.m4a';good=root/'good.wav'
            def ff(*args):subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True)
            ff('-f','lavfi','-i','color=c=blue:s=640x360:r=24:d=3','-an','-c:v','libx264',str(video))
            ff('-f','lavfi','-i','sine=frequency=440:duration=3','-c:a','aac',str(bad))
            ff('-f','lavfi','-i','sine=frequency=880:duration=3',str(good))
            packets=json.loads(subprocess.run(['ffprobe','-v','error','-show_packets','-of','json',str(bad)],capture_output=True,text=True,check=True).stdout)['packets']
            packet=next(p for p in packets if float(p.get('pts_time',0))>=1)
            with bad.open('r+b') as f:f.seek(int(packet['pos']));f.write(b'\xff'*int(packet['size']))
            v=media._asset(video,'video');b=media._asset(bad,'music',quick_music=True);g=media._asset(good,'music',quick_music=True)
            app=Application(root/'state')
            entry={'id':'one','duration':2,'segments':[{'asset_id':v['id'],'path':v['path'],'start':0,'duration':2}],
                   'music':[b],'video_assets':[v]}
            batch=app.make_batch({'width':640,'height':360,'fps':24,'hardware':'software_fast','output_dir':str(root/'out'),
                                 'music_ids':[b['id'],g['id']],'min_songs':1,'max_songs':1,'music_margin_seconds':3},
                                 {'items':[entry]},[v,b,g])
            app.store.update(batch['id'],lambda x:x.update(status='running'))
            app.execute_item(batch['id'],0)
            item=app.batch(batch['id'])['items'][0]
            self.assertEqual('success',item['status'],item.get('error'))
            self.assertEqual(g['id'],item['music'][0]['id'])
            self.assertEqual(1,len(item['music_replacements']))
            self.assertTrue(item['result']['valid'])
