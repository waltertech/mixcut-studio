"""Music sets duration; planning has no decoding; real rendering preserves all songs."""
import random, shutil, subprocess, tempfile, unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from mixcut import musicled, planner, media, renderer
from mixcut.server import Application, Store


def songs(root='/music'):
 return [{'id':str(n),'name':f'{n:02d} song.m4a','path':f'{root}/style/{n:02d} song.m4a','duration':10+n,'mime_type':'audio/mp4'} for n in range(1,6)]

def videos(duration=20):
 return [{'id':'v','path':'/video.mp4','duration':duration,'mime_type':'video/mp4'}]

class MusicLedPlanningTests(unittest.TestCase):
 def test_folder_anchors_all_once_no_decode_and_video_repeat(self):
  config=dict(duration_basis='music',music_mode='folder',music_root='/music',count=1,seed=4,min_duration=900,max_duration=1000)
  with patch('subprocess.run',side_effect=AssertionError('no decode during planning')):
   item=planner.plan(videos(),songs(),config)['items'][0]
  self.assertEqual(['1','2'],[s['id'] for s in item['music'][:2]])
  self.assertEqual(5,len(item['music']));self.assertEqual(65,item['duration'])
  self.assertEqual([20,20,20,5],[s['duration'] for s in item['segments']])
 def test_long_video_truncated_and_fixed_all_once(self):
  item=planner.plan(videos(200),songs(),dict(duration_basis='music',music_mode='fixed',count=1))['items'][0]
  self.assertEqual(65,item['segments'][0]['duration']);self.assertEqual(5,len(item['music']))
 def test_pool_no_reuse_until_exhausted_no_duration_preference(self):
  library=[dict(s,duration=10000 if s['id']=='1' else 2) for s in songs()]
  result=planner.plan(videos(20000),library,dict(duration_basis='music',music_mode='pool',count=5,min_songs=1,max_songs=1,seed=7))
  self.assertEqual(5,len({i['music'][0]['id'] for i in result['items']}))
 def test_multi_video_rounds_match_music_exactly(self):
  vs=videos(3)+[dict(videos(4)[0],id='v2',path='/v2.mp4')]
  item=planner.plan(vs,songs(),dict(duration_basis='music',music_mode='fixed',mode='multi',segment_min=2,segment_max=4,count=1))['items'][0]
  self.assertAlmostEqual(65,sum(s['duration'] for s in item['segments']))
  self.assertTrue(all(a['asset_id']!=b['asset_id'] for a,b in zip(item['segments'],item['segments'][1:])))
 def test_legacy_folder_still_repeats_music(self):
  order,total=__import__('mixcut.foldermusic',fromlist=['allocate']).allocate(songs(),200,{},random.Random(0))
  self.assertGreater(len(order),5);self.assertGreaterEqual(total,200)
 def test_missing_anchors_do_not_block_complete_folder(self):
  for subset in [songs()[:1],songs()[1:2],songs()[2:],songs()[:1]+songs()[2:]]:
   order=musicled.choose_music(subset,{'music_mode':'folder'},random.Random(0))
   self.assertEqual(len(subset),len(order))
   self.assertEqual({s['id'] for s in subset},{s['id'] for s in order})
   if subset[0]['id']=='1':self.assertEqual('1',order[0]['id'])
 def test_suffix_warning_uses_same_number_rule_as_order(self):
  from mixcut import foldermusic
  pair=[dict(songs()[0],path='/music/A/track-去重 01.mp3'),dict(songs()[1],path='/music/A/track-去重 02.mp3')]
  item={'music':pair};foldermusic.describe(item);self.assertEqual('',item['music_order_warning'])
  item={'music':pair[:1]};foldermusic.describe(item)
  self.assertIn('02',item['music_order_warning']);self.assertIn('继续规划',item['music_order_warning'])
 def test_nonfinite_duration_rejected(self):
  with self.assertRaises(ValueError):musicled.fit_segments([{'duration':1}],float('inf'))

class MusicLedActionsTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name).resolve();self.app=Application(self.root/'state')
  self.music=songs(str(self.root/'music'))
  self.videos=videos(20)+[dict(videos(8)[0],id='v2',path='/other.mp4')]
  self.app.store.put('library',{'music_dir':str(self.root/'music'),'scan':{'videos':self.videos,'music':self.music}})
  self.batch=self.app.create_plan({'config':dict(duration_basis='music',music_mode='folder',count=1,output_dir=str(self.root/'exports'))})
  self.bid=self.batch['id'];self.iid=self.batch['items'][0]['id']
 def test_default_direct_join_and_folder_refresh_full_order(self):
  self.assertFalse(self.batch['config']['nonstop_music'])
  item=self.app.refresh_music(self.bid,self.iid)['items'][0]
  self.assertEqual(5,len(item['music']));self.assertEqual(65,item['duration'])
 def test_folder_single_replacement_swaps_without_missing_or_repeating(self):
  item=self.app.replace_media(self.bid,self.iid,{'kind':'music','index':2})['items'][0]
  self.assertEqual(Counter(s['id'] for s in self.music),Counter(s['id'] for s in item['music']))
  self.assertEqual(['1','2'],[s['id'] for s in item['music'][:2]])
 def test_short_video_replacement_preserves_audio_and_duration(self):
  before=self.batch['items'][0]
  item=self.app.replace_media(self.bid,self.iid,{'kind':'video','index':0})['items'][0]
  self.assertEqual([s['id'] for s in before['music']],[s['id'] for s in item['music']])
  self.assertAlmostEqual(65,sum(s['duration'] for s in item['segments']))

@unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
class MusicLedRenderTests(unittest.TestCase):
 def test_real_audio_drives_video_duration_and_cached_soundtrack(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);v=root/'video.mp4';m=root/'music';m.mkdir()
   def ff(*args):subprocess.run(['ffmpeg','-nostdin','-y','-v','error',*args],check=True,capture_output=True,timeout=60)
   ff('-f','lavfi','-i','testsrc2=s=320x180:r=24:d=2','-c:v','libx264',str(v))
   tracks=[]
   for n in range(1,4):
    p=m/f'{n:02d} song.m4a';ff('-f','lavfi','-i',f'sine=f={n*220}:duration=2','-af','adelay=500|500','-c:a','aac',str(p))
    tracks.append(media._asset(p,'music',quick_music=True))
   config=dict(duration_basis='music',music_mode='fixed',count=1,width=320,height=180,fps=24,hardware='software_fast',original_volume=0,music_volume=1)
   item=planner.plan([media._asset(v,'video')],tracks,config)['items'][0]
   stages=[]
   with patch('mixcut.renderer.render_video_chunk', wraps=renderer.render_video_chunk) as encode:
    result=renderer.render(item,config,str(root/'output.mp4'),str(root/'work'),lambda x:stages.append(x.get('stage')))
   self.assertLess(encode.call_count,len(item['segments'])+1)
   self.assertGreater(result['duration'],7.4) # Leading silence retained on all three songs.
   self.assertLess(result['duration'],7.7)
   self.assertEqual(3,len(result['played_music']))
   self.assertIn('music_assembly',stages)
   ff('-xerror','-i',str(root/'output.mp4'),'-f','null','-')
   with patch('mixcut.processwatch.run',side_effect=AssertionError('cached soundtrack should not be decoded again')):
    cached=musicled.prepare_audio(item,config,root/'work',None)
   self.assertAlmostEqual(result['duration'],cached['duration'],delta=.06)

   # Explicit opt-in trimming remains available and must not add extra folder rounds.
   from mixcut.nonstop import MusicEdges
   trimmed=MusicEdges(Store(root/'edge-state')).prepare(item)
   self.assertEqual(3,len(trimmed['music']))
   result2=renderer.render(trimmed,dict(config,nonstop_music=True),str(root/'trimmed.mp4'),str(root/'trim-work'))
   self.assertGreater(result2['duration'],4.5)
   self.assertLess(result2['duration'],7)
   self.assertEqual(3,len(result2['played_music']))
