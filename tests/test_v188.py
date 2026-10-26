"""Persistent folder balancing, task lifecycle and restart regressions."""
import copy,json,random,tempfile,threading,unittest
from pathlib import Path
from mixcut.server import Store
from mixcut import foldermusic,planner
import test_v157 as fixture

class FolderLedgerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve();self.store=Store(self.root/'state');self.folder=str(self.root/'music/a')
 def batch(self,status='pending',bid='a'):
  return {'id':bid,'created_at':10,'updated_at':20,'config':{'music_mode':'folder'},'items':[{'id':'1','status':status,'music_folder':self.folder}]}
 def test_success_count_survives_delete_and_restart(self):
  self.store.put('batch:a',self.batch('success'));self.store.delete('batch:a')
  other=Store(self.root/'state');self.assertEqual(1,other.folder_usage()[self.folder]['completed'])
 def test_retries_repeated_writes_and_stale_snapshot_count_once(self):
  b=self.batch('success');old=copy.deepcopy(b)
  for _ in range(3):self.store.put('batch:a',b)
  self.store.put('batch:a',old);old['items'][0]['status']='pending';self.store.put('batch:a',old)
  old['items'][0]['status']='success';self.store.put('batch:a',old)
  self.assertEqual(1,self.store.folder_usage()[self.folder]['completed'])
 def test_pending_fail_cancel_delete_release_reservations(self):
  b=self.batch();self.store.put('batch:a',b);self.assertEqual(1,self.store.folder_usage()[self.folder]['pending'])
  for status in ['failed','cancelled']:
   b['items'][0]['status']=status;self.store.put('batch:a',b);self.assertEqual({},self.store.folder_usage())
  b['items'][0]['status']='pending';self.store.put('batch:a',b);self.assertEqual(1,self.store.folder_usage()[self.folder]['pending'])
  self.store.delete('batch:a');self.assertEqual({},self.store.folder_usage())
 def test_restart_seeds_legacy_successes_exactly_once(self):
  with self.store.connection() as c:c.execute('INSERT INTO records VALUES (?,?)',('batch:a',json.dumps(self.batch('success'))))
  for _ in range(2):
   self.store=Store(self.root/'state');self.assertEqual(1,self.store.folder_usage()[self.folder]['completed'])
  self.assertEqual(20,self.store.folder_usage()[self.folder]['last_used'])
 def test_sticker_variants_not_counted_or_reserved(self):
  for status in ['pending','success']:
   b=self.batch(status);b['sticker_origin']={'batch_id':'origin'};self.store.put('batch:a',b)
  self.assertEqual({},self.store.folder_usage())
 def test_switch_releases_old_folder_and_exclusion_avoids_self_bias(self):
  b=self.batch();self.store.put('batch:a',b);self.assertEqual({},self.store.folder_usage(('a','1')))
  b['items'][0]['music_folder']=str(self.root/'music/b');self.store.put('batch:a',b)
  self.assertNotIn(self.folder,self.store.folder_usage())
 def test_multiple_tasks_in_same_folder_and_parallel_successes(self):
  batches=[self.batch('success',str(n)) for n in range(8)]
  threads=[threading.Thread(target=self.store.put,args=('batch:'+b['id'],b)) for b in batches]
  for t in threads:t.start()
  for t in threads:t.join()
  self.assertEqual(8,self.store.folder_usage()[self.folder]['completed'])
 def test_least_count_then_oldest_then_random(self):
  grouped={'a':[],'b':[],'c':[]};history={'a':{'last_used':1},'b':{'last_used':2},'c':{'last_used':0}}
  self.assertEqual('a',foldermusic.choose_folder(grouped,{'a':1,'b':1,'c':5},random.Random(1),history=history))
  self.assertEqual('c',foldermusic.choose_folder(grouped,{'a':1,'b':1},random.Random(1),history=history))
  self.assertEqual('b',foldermusic.choose_folder(grouped,{'a':1,'b':1,'c':5},random.Random(1),'a',history=history))

class FolderPlanningTests(unittest.TestCase):
 setUp=fixture.RetainedReviewTests.setUp
 def library(self):
  self.music_root=self.root/'music'
  videos=[{'id':'v','path':str(self.root/'raw.mp4'),'name':'raw.mp4','duration':60,'mime_type':'video/mp4'}]
  songs=[{'id':n,'path':str(self.music_root/n/'song01.mp3'),'duration':12,'mime_type':'audio/mpeg'} for n in ['a','b','c']]
  self.app.store.put('library',{'video_dir':str(self.root),'music_dir':str(self.music_root),'scan':{'videos':videos,'music':songs,'errors':[]}})
  return videos,songs
 def config(self):return {'duration_basis':'music','music_mode':'folder','count':1,'width':640,'height':360,'fps':30,'output_dir':str(self.root/'out'),'seed':3}
 def test_three_separate_batches_use_three_folders_then_deleted_draft_releases(self):
  self.library();batches=[self.app.create_plan({'config':self.config()}) for _ in range(3)]
  folders=[b['items'][0]['music_folder'] for b in batches];self.assertEqual(3,len(set(folders)))
  for b in batches:self.assertNotIn('_music_folder_history',b['config'])
  self.app.store.delete('batch:'+batches[0]['id'])
  new=self.app.create_plan({'config':self.config()});self.assertEqual(folders[0],new['items'][0]['music_folder'])
 def test_completed_folder_deprioritized_after_task_deleted(self):
  self.library();b=self.app.create_plan({'config':self.config()});folder=b['items'][0]['music_folder']
  self.app.store.update(b['id'],lambda value:value['items'][0].update(status='success'))
  self.app.store.delete('batch:'+b['id']);new=self.app.create_plan({'config':self.config()})
  self.assertNotEqual(folder,new['items'][0]['music_folder'])
  selected=next(f for f in new['music_folder_catalog'] if f['path']==folder)
  self.assertEqual(1,selected['historical_usage']['completed'])
 def test_parallel_plan_calls_see_reservations(self):
  self.library();result=[];errors=[]
  def run():
   try:result.append(self.app.create_plan({'config':self.config()}))
   except Exception as e:errors.append(str(e))
  threads=[threading.Thread(target=run) for _ in range(3)]
  for t in threads:t.start()
  for t in threads:t.join()
  self.assertEqual([],errors);self.assertEqual(3,len({b['items'][0]['music_folder'] for b in result}))
 def test_both_planners_use_history_without_double_counting_scheduler_previous(self):
  videos,songs=self.library();folder=str(self.music_root/'a');history={folder:{'completed':5,'pending':0,'last_used':1}}
  for basis in ['music','video']:
   cfg=dict(self.config(),duration_basis=basis,min_duration=8,max_duration=10,mode='single',allow_overlap=True,_music_folder_history=history,music_root=str(self.music_root))
   result=planner.plan(videos,songs,cfg)
   self.assertNotEqual(folder,result['items'][0]['music_folder'])
  cfg=dict(self.config(),music_root=str(self.music_root),count=3,_music_folder_history={})
  prev=planner.plan(videos,songs,cfg)['items'];cfg['count']=1
  # Explicit empty history is authoritative: previous songs/video still constrain, folders aren't counted twice.
  observed=[]
  original=foldermusic.choose_folder
  def choose(grouped,usage,rng,*args,**kwargs):
   observed.append(dict(usage))
   return original(grouped,usage,rng,*args,**kwargs)
  with unittest.mock.patch('mixcut.foldermusic.choose_folder',side_effect=choose):
   planner.plan(videos,songs,cfg,previous_items=prev)
  self.assertEqual([{}],observed)

 def test_history_does_not_break_within_batch_unique_folder_rule(self):
  videos,songs=self.library();folder=str(self.music_root/'a')
  for basis in ['music','video']:
   cfg=dict(self.config(),duration_basis=basis,count=3,min_duration=8,max_duration=10,mode='single',allow_overlap=True,music_root=str(self.music_root),_music_folder_history={folder:{'completed':8,'pending':0,'last_used':1}})
   result=planner.plan(videos,songs,cfg)
   self.assertEqual(3,len({i['music_folder'] for i in result['items']}))

 def test_approval_and_automatic_retirement_preserve_history(self):
  folder=str(self.root/'music/a');self.item['music_folder']=folder
  self.app.store.put('batch:abc123',self.batch)
  self.app.approve({'batch_id':'abc123','item_id':'1'});self.app.prune_forgotten_items()
  self.assertIsNone(self.app.store.get('batch:abc123'))
  self.assertEqual(1,self.app.store.folder_usage()[folder]['completed'])
  self.assertTrue(self.video.exists())
