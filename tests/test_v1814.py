"""Fast single-image sampling, bounded failures and configured parallel jobs."""
import json,subprocess,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from mixcut.keyframes import KeyframeCache
from mixcut.server import Application
from test_v149 import save

class FastPreviewTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.video=self.root/'v.mp4';self.video.write_bytes(b'video');self.cache=KeyframeCache(self.root/'cache')
  with patch('mixcut.keyframes.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps({'format':{'duration':130}}))):self.data=self.cache.describe(self.video)
 def test_one_image_serves_both_sizes_and_cached_calls_never_decode(self):
  def capture(path,point,target,*args):target.write_bytes(b'jpeg')
  with patch.object(self.cache,'_capture',side_effect=capture) as extract:
   self.cache.prepare(self.video);self.assertEqual(3,extract.call_count)
  with patch.object(self.cache,'_capture',side_effect=AssertionError('cached')):
   a=self.cache.image(self.video,1,'thumb');b=self.cache.image(self.video,1,'large');self.assertEqual(a,b)
  self.assertEqual(3,len(list(a.parent.glob('*.jpg'))))
 def test_partial_failure_retries_only_bad_point_and_next_retry_resumes(self):
  points=[]
  def capture(path,point,target,*args):
   points.append(point)
   if point==60:raise ValueError('bad sample')
   target.write_bytes(b'jpeg')
  with patch.object(self.cache,'_capture',side_effect=capture):
   with self.assertRaises(ValueError):self.cache.prepare(self.video)
  self.assertEqual([0.,60.,60.],points);points.clear()
  def success(path,point,target,*args):points.append(point);target.write_bytes(b'jpeg')
  with patch.object(self.cache,'_capture',side_effect=success):self.cache.prepare(self.video)
  self.assertEqual([60.,120.],points);self.assertTrue(self.cache.ready(self.data['version'],3))
 def test_cancel_while_child_runs_terminates_and_does_not_retry(self):
  cancel=threading.Event()
  class Child:
   returncode=None;stopped=False
   def poll(self):return 0 if self.stopped else None
   def wait(self,timeout=None):
    if self.stopped:return 0
    cancel.set();raise subprocess.TimeoutExpired('ffmpeg',timeout)
   def terminate(self):self.stopped=True
  child=Child()
  with patch('mixcut.keyframes.subprocess.Popen',return_value=child) as spawn:
   with self.assertRaises(InterruptedError):self.cache.prepare(self.video,cancel.is_set)
  self.assertTrue(child.stopped);spawn.assert_called_once();self.assertFalse(list((self.cache.root/self.data['version']).glob('*.jpg')))
 def test_stuck_point_timeout_is_bounded_and_child_terminated(self):
  children=[]
  class Child:
   returncode=None;stopped=False
   def poll(self):return 0 if self.stopped else None
   def wait(self,timeout=None):return 0
   def terminate(self):self.stopped=True
  def spawn(*args,**kwargs):c=Child();children.append(c);return c
  with patch('mixcut.keyframes.POINT_TIMEOUT',0),patch('mixcut.keyframes.subprocess.Popen',side_effect=spawn):
   with self.assertRaisesRegex(ValueError,'超时'):self.cache.prepare(self.video)
  self.assertEqual(2,len(children));self.assertTrue(all(c.stopped for c in children))
 def test_seek_precedes_input_and_only_one_low_quality_output(self):
  captured=[]
  class Child:
   returncode=0
   def poll(self):return 0
  def spawn(command,**kwargs):captured.append(command);Path(command[-1]).write_bytes(b'jpeg');return Child()
  with patch('mixcut.keyframes.subprocess.Popen',side_effect=spawn):self.cache.prepare(self.video)
  for cmd in captured:
   self.assertLess(cmd.index('-ss'),cmd.index('-i'));self.assertEqual('1',cmd[cmd.index('-frames:v')+1]);self.assertEqual('7',cmd[cmd.index('-q:v')+1]);self.assertIn('480',cmd[cmd.index('-vf')+1]);self.assertNotIn('-filter_complex',cmd)
 def test_source_change_during_sampling_does_not_publish_wrong_frame(self):
  def capture(path,point,target,*args):target.write_bytes(b'jpeg');self.video.write_bytes(b'changed source')
  with patch.object(self.cache,'_capture',side_effect=capture):
   with self.assertRaises(InterruptedError):self.cache.prepare(self.video)
  self.assertFalse(list((self.cache.root/self.data['version']).glob('*.jpg')))

 def test_completed_minute_cache_is_reused_without_probe_or_decode(self):
  cache=KeyframeCache(self.root/'another-cache');version=cache._version(self.video,legacy='minute');old=cache.root/version;old.mkdir()
  data=dict(self.data,version=version)
  (old/'index.json').write_text(json.dumps(data))
  for i in range(3):
   for size in ['thumb','large']:(old/f'{i:06d}-{size}.jpg').write_bytes(f'{i}-{size}'.encode())
  with patch('mixcut.keyframes.subprocess.run',side_effect=AssertionError('no probe')),patch('mixcut.keyframes.subprocess.Popen',side_effect=AssertionError('no decode')):
   result=cache.prepare(self.video)
   self.assertEqual(3,result['total']);self.assertEqual(b'1-thumb',cache.image(self.video,1,'large').read_bytes());self.assertEqual(3,len(list((cache.root/result['version']).glob('*.jpg'))))

class ParallelPreviewsTests(unittest.TestCase):
 def test_parallelism_matches_1_2_3_4_and_failure_does_not_block_queue(self):
  for limit in [1,2,3,4]:
   with self.subTest(limit=limit),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);app=Application(root/'state');batch=save(app,root)
    def configure(b):
     b['config']['parallel_tasks']=limit;first=b['items'][0]
     for n in range(1,6):
      path=root/f'{n}.mp4';path.write_bytes(b'fixture');b['items'].append(dict(first,id=str(n),output_path=str(path),thumbnails={'status':'queued'}))
    app.store.update('abc123',configure);active=0;peak=0;guard=threading.Lock()
    def prepare(path,cancelled):
     nonlocal active,peak
     with guard:active+=1;peak=max(peak,active)
     try:
      time.sleep(.12)
      if Path(path).name=='1.mp4':raise ValueError('fixture point failure')
      return {'version':app.keyframes._version(path),'total':1}
     finally:
      with guard:active-=1
    try:
     with patch.object(app.keyframes,'prepare',side_effect=prepare):
      for item in app.batch('abc123')['items']:app.enqueue_previews('abc123',item['id'])
      app.start_preview_worker();app.preview_queue.join()
     self.assertEqual(limit,peak);states=[i['thumbnails']['status'] for i in app.batch('abc123')['items']];self.assertEqual(1,states.count('failed'));self.assertEqual(5,states.count('ready'))
    finally:app.closing.set();app.preview_worker.join(timeout=5)
