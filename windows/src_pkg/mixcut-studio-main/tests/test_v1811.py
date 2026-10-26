"""Exact repeated rounds, task overrides, video coverage and legacy isolation."""
import copy,unittest
from unittest.mock import patch
import test_v157 as fixture
from mixcut import musicrounds,planner
from mixcut.nonstop import MusicEdges
from mixcut.tracklist import played_music

class MusicRoundsTests(unittest.TestCase):
 setUp=fixture.RetainedReviewTests.setUp
 def plan(self,repeats=2,mode='folder',count=2):
  root=self.root/'music';self.songs=[{'id':str(n),'name':f'song-{n:02}.mp3','path':str(root/'a'/f'song-{n:02}.mp3'),'duration':300.,'mime_type':'audio/mpeg'} for n in range(1,6)]
  self.videos=[{'id':'v','path':str(self.root/'raw.mp4'),'duration':1800.,'mime_type':'video/mp4'}]
  self.app.store.put('library',{'video_dir':str(self.root),'music_dir':str(root),'scan':{'videos':self.videos,'music':self.songs,'errors':[]}})
  self.config={'duration_basis':'music','music_mode':mode,'music_repeats':repeats,'count':count,'mode':'single','min_songs':5,'max_songs':5,'width':640,'height':360,'fps':30,'output_dir':str(self.root/'out'),'seed':2}
  return self.app.create_plan({'config':self.config})
 def assert_rounds(self,item,n):
  base=[s['id'] for s in item['music_base']];self.assertEqual(base*n,[s['id'] for s in item['music']]);self.assertEqual(n,item['music_repeats']);self.assertAlmostEqual(1500*n,item['duration']);self.assertAlmostEqual(item['duration'],sum(s['duration'] for s in item['segments']))
 def test_plan_two_rounds_full_video_then_tail(self):
  b=self.plan();item=b['items'][0];self.assert_rounds(item,2)
  self.assertEqual(['1','2'],[s['id'] for s in item['music_base'][:2]])
  self.assertEqual([1800.,1200.],[s['duration'] for s in item['segments']]);self.assertEqual([0,0],[s['start'] for s in item['segments']])
 def test_overrides_not_exponential_and_other_task_unchanged(self):
  b=self.plan();other=copy.deepcopy(b['items'][1]);item=b['items'][0];base=copy.deepcopy(item['music_base'])
  for n in [3,4,1,2]:
   b=self.app.set_music_repeats(b['id'],item['id'],{'music_repeats':n});self.assert_rounds(b['items'][0],n);self.assertEqual(other['duration'],b['items'][1]['duration']);self.assertEqual([s['id'] for s in other['music']],[s['id'] for s in b['items'][1]['music']]);self.assertEqual([(s['asset_id'],s['start'],s['duration']) for s in other['segments']],[(s['asset_id'],s['start'],s['duration']) for s in b['items'][1]['segments']]);self.assertEqual([s['id'] for s in base],[s['id'] for s in b['items'][0]['music_base']])
 def test_reorder_one_round_updates_all_rounds(self):
  b=self.plan(3);item=b['items'][0];ids=[s['id'] for s in reversed(item['music_base'])]
  b=self.app.reorder_music(b['id'],item['id'],ids);self.assertEqual(ids*3,[s['id'] for s in b['items'][0]['music']]);self.assert_rounds(b['items'][0],3)
 def test_reshuffle_and_folder_change_preserve_round_count(self):
  b=self.plan(4);item=b['items'][0];b=self.app.refresh_music(b['id'],item['id']);self.assert_rounds(b['items'][0],4)
  b=self.app.change_music_folder(b['id'],item['id'],{'folder':item['music_folder']});self.assert_rounds(b['items'][0],4)
 def test_pool_and_fixed_repeat_chosen_subset(self):
  for mode in ['pool','fixed']:
   b=self.plan(2,mode);self.assert_rounds(b['items'][0],2)
 def test_existing_plan_without_repeats_remains_single_round(self):
  self.plan();cfg=dict(self.config);cfg.pop('music_repeats');b=self.app.create_plan({'config':cfg});self.assert_rounds(b['items'][0],1)
 def test_legacy_task_gains_base_when_manually_changed(self):
  b=self.plan(1);item=b['items'][0]
  for k in ['music_base','music_base_duration','music_repeats']:item.pop(k)
  self.app.store.put('batch:'+b['id'],b)
  b=self.app.set_music_repeats(b['id'],item['id'],{'music_repeats':3});self.assert_rounds(b['items'][0],3)
 def test_invalid_rounds_and_already_started_rejected(self):
  b=self.plan();item=b['items'][0]
  for n in [0,5,True,2.5,None,'bad']:
   with self.assertRaises(ValueError):self.app.set_music_repeats(b['id'],item['id'],{'music_repeats':n})
  for status in ['running','validating','success','failed','stalled_paused']:
   self.app.store.update(b['id'],lambda v:v['items'][0].update(status=status))
   with self.assertRaises(ValueError):self.app.set_music_repeats(b['id'],item['id'],{'music_repeats':3})
 def test_pending_task_of_queued_batch_editable_but_renderer_claim_blocks(self):
  b=self.plan();item=b['items'][0];self.app.store.update(b['id'],lambda v:v.update(status='queued'))
  b=self.app.set_music_repeats(b['id'],item['id'],{'music_repeats':3});self.assert_rounds(b['items'][0],3)
  self.app.render_claims.add((b['id'],item['id']))
  with self.assertRaises(ValueError):self.app.set_music_repeats(b['id'],item['id'],{'music_repeats':4})
 def test_track_document_sequence_contains_all_rounds(self):
  b=self.plan(4);item=b['items'][0];played=played_music(item,item['duration']);self.assertEqual([s['id'] for s in item['music']], [s['id'] for s in played]);self.assertEqual(20,len(played))
 def test_folder_usage_counts_one_success_not_four(self):
  b=self.plan(4);item=b['items'][0];self.app.store.update(b['id'],lambda v:v['items'][0].update(status='success'))
  self.assertEqual(1,self.app.store.folder_usage()[item['music_folder']]['completed'])
 def test_silence_crossfade_actual_duration_includes_all_rounds(self):
  b=self.plan(2);item=b['items'][0]
  with patch.object(MusicEdges,'detect',return_value={'start':1,'end':299,'effective_duration':298}):
   prepared=self.app.music_edges.prepare(item)
  self.assertEqual(10,len(prepared['music']));self.assertAlmostEqual(298*10-.5*9,prepared['duration'])
 def test_video_led_and_sticker_reject_manual_round_changes(self):
  b=self.plan();item=b['items'][0];b['config']['duration_basis']='video';self.app.store.put('batch:'+b['id'],b)
  with self.assertRaises(ValueError):self.app.set_music_repeats(b['id'],item['id'],{'music_repeats':2})

 def test_renderer_claim_prevents_pending_edit_race(self):
  import threading
  b=self.plan();item=b['items'][0];entered=threading.Event();release=threading.Event()
  def rendering(*args):entered.set();release.wait(3)
  with patch.object(self.app,'_execute_item',side_effect=rendering):
   worker=threading.Thread(target=self.app.execute_item,args=(b['id'],0));worker.start()
   try:
    self.assertTrue(entered.wait(2))
    with self.assertRaises(ValueError):self.app.set_music_repeats(b['id'],item['id'],{'music_repeats':3})
   finally:release.set();worker.join(3)
  self.assertEqual(set(),self.app.render_claims)

 def test_replacement_only_accepts_base_round_positions(self):
  b=self.plan(2);item=b['items'][0]
  with self.assertRaises(ValueError):self.app.replace_media(b['id'],item['id'],{'kind':'music','index':8})
