"""Fair source-first allocation and inclusive source admission."""
import collections,copy,random,unittest
from mixcut import planner,musicled
import test_v157 as fixture

def video(name,duration):return {'id':name,'path':'/'+name+'.mp4','duration':duration,'mime_type':'video/mp4'}
def song():return {'id':'song','path':'/music/a/song01.mp3','duration':1800.,'mime_type':'audio/mpeg'}
def config(**kwargs):return dict(duration_basis='music',music_mode='folder',music_root='/music',music_repeats=2,count=1,mode='single',min_source_duration_minutes=20,seed=3,**kwargs)

class FairSourcesTests(unittest.TestCase):
 def test_sixty_tasks_not_concentrated_on_only_long_source(self):
  videos=[video(str(n),1500.+n) for n in range(138)]+[video('long',265*60)]
  cfg=config();cfg['count']=60;result=planner.plan(videos,[song()],cfg)
  sources=[{s['asset_id'] for s in i['segments']} for i in result['items']]
  self.assertTrue(all(len(s)==1 for s in sources));self.assertEqual(60,len(set.union(*sources)))
  self.assertEqual(0,result['stats']['segment_overlap_pairs'])
  self.assertTrue(all(abs(sum(s['duration'] for s in i['segments'])-3600)<1e-6 for i in result['items']))
 def test_loop_count_does_not_penalize_short_sources(self):
  videos=[video('short',1200),video('medium',1800),video('long',10000)]
  cfg=config();cfg['count']=21;items=planner.plan(videos,[song()],cfg)['items']
  counts=collections.Counter(next(iter({s['asset_id'] for s in i['segments']})) for i in items)
  self.assertEqual({'short':7,'medium':7,'long':7},dict(counts))
 def test_shorter_unused_source_preferred_to_long_used_source(self):
  segments=musicled.video_segments([video('short',1800),video('long',20000)],4500,{'mode':'single'},random.Random(0),{'short':0,'long':2})
  self.assertEqual([1800,1800,900],[s['duration'] for s in segments]);self.assertEqual({'short'},{s['asset_id'] for s in segments})
 def test_long_selected_source_randomly_trimmed_when_music_shorter(self):
  segments=musicled.video_segments([video('v',1800)],900,{'mode':'single'},random.Random(3))
  self.assertEqual(1,len(segments));self.assertEqual(900,segments[0]['duration']);self.assertGreater(segments[0]['start'],0);self.assertLessEqual(segments[0]['start']+900,1800)
 def test_inclusive_boundary_new_planner_and_legacy(self):
  for basis in ['music','video']:
   cfg=config();cfg.update(duration_basis=basis,count=1,min_duration=10,max_duration=12)
   result=planner.plan([video('below',1199.9),video('equal',1200)],[song()],cfg)
   self.assertEqual({'equal'},{s['asset_id'] for s in result['items'][0]['segments']})
 def test_previous_task_loop_segments_count_once_for_fairness(self):
  videos=[video('a',1200),video('b',1800)];old={'music':[song()],'segments':[{'asset_id':'a','path':'/a.mp4','start':0,'duration':1200}]*3,'duration':3600}
  cfg=config();cfg['count']=1
  result=planner.plan(videos,[song()],cfg,previous_items=[old]);self.assertEqual({'b'},{s['asset_id'] for s in result['items'][0]['segments']})
 def test_start_gap_cannot_force_switch_to_another_longer_source(self):
  segments=musicled.video_segments([video('a',1200),video('b',10000)],1200,{'mode':'single','start_gap':30},random.Random(1),{'a':0,'b':10},{'a':[(0,1200)]})
  self.assertEqual('a',segments[0]['asset_id']);self.assertEqual(1200,segments[0]['duration'])

 def test_loop_overlap_percentage_is_bounded_and_not_multiplied(self):
  part={'asset_id':'v','start':0,'duration':10}
  items=[{'id':'a','music':[],'duration':20,'segments':[dict(part),dict(part)]},{'id':'b','music':[],'duration':30,'segments':[dict(part),dict(part),dict(part)]}]
  stats=planner.annotate_batch(items);self.assertEqual(1,stats['segment_overlap_pairs']);self.assertEqual(1.,stats['max_overlap_ratio'])

class ReplacementTests(unittest.TestCase):
 setUp=fixture.RetainedReviewTests.setUp
 def test_manual_replacement_prefers_unused_and_includes_equal_threshold(self):
  assets=[video('current',5000),video('used',5000),video('equal',1200),video('below',1199)]
  self.batch.update(status='draft',assets=assets)
  self.batch['config'].update(duration_basis='music',mode='single',min_source_duration_minutes=20)
  self.item.update(duration=3600,segments=[{'asset_id':'current','path':'/current.mp4','start':0,'duration':3600}],video_assets=[assets[0]])
  other=copy.deepcopy(self.item);other.update(id='2',segments=[{'asset_id':'used','path':'/used.mp4','start':0,'duration':3600}]);self.batch['items'].append(other)
  self.app.store.put('batch:abc123',self.batch)
  result=self.app.replace_media('abc123','1',{'kind':'video','index':0})
  self.assertEqual({'equal'},{s['asset_id'] for s in result['items'][0]['segments']});self.assertEqual([1200,1200,1200],[s['duration'] for s in result['items'][0]['segments']])
