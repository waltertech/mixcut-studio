"""Minute sampling and zero-decode reuse of completed 30s previews."""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from mixcut.keyframes import KeyframeCache

class MinuteSamplingTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.video=self.root/'video.mp4';self.video.write_bytes(b'video');self.cache=KeyframeCache(self.root/'cache')
 def test_three_hour_video_has_180_frames(self):
  with patch('mixcut.keyframes.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps({'format':{'duration':10800}}))):data=self.cache.describe(self.video)
  self.assertEqual(180,data['total']);self.assertEqual(60,data['interval_seconds']);self.assertEqual([0.,60.,120.],[f['time'] for f in data['frames'][:3]]);self.assertEqual(10740.,data['frames'][-1]['time'])
 def legacy(self):
  version=self.cache._version(self.video,legacy=True);folder=self.cache.root/version;folder.mkdir()
  data={'version':version,'duration':130.,'total':5,'frames':[{'index':i,'time':i*30.} for i in range(5)]}
  (folder/'index.json').write_text(json.dumps(data))
  for i in range(5):
   for size in ('thumb','large'):(folder/f'{i:06d}-{size}.jpg').write_bytes(f'{i}-{size}'.encode())
  return folder
 def test_legacy_cache_reused_every_other_pair_without_any_media_process(self):
  old=self.legacy()
  with patch('mixcut.keyframes.subprocess.run',side_effect=AssertionError('no probing')),patch('mixcut.keyframes.subprocess.Popen',side_effect=AssertionError('no decoding')):
   data=self.cache.prepare(self.video)
   self.assertEqual([0.,60.,120.],[f['time'] for f in data['frames']]);self.assertEqual(3,data['total'])
   self.cache.remove(old.name)
   for i in range(3):
    for size in ('thumb','large'):self.assertEqual(f'{i*2}-thumb'.encode(),self.cache.image(self.video,i,size).read_bytes())
 def test_old_incomplete_cache_is_not_reused(self):
  old=self.legacy();(old/'000004-large.jpg').unlink()
  with patch('mixcut.keyframes.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps({'format':{'duration':130}}))) as probe:data=self.cache.describe(self.video)
  probe.assert_called_once();self.assertEqual([0.,60.,120.],[f['time'] for f in data['frames']]);self.assertFalse(self.cache.ready(data['version'],3))
