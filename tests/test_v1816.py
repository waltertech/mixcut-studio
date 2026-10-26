"""Duration + leaf YouTube ID names, durable final rename and legacy compatibility."""
import tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from mixcut.naming import youtube_id, duration_filename, item_filename
from mixcut.server import Application

class ExportNameTests(unittest.TestCase):
 def test_ids_and_actual_duration(self):
  for identity in ['M9q9pvKB7hY','u8_-USakt0c','_itb5qqD5vs']:
   for folder in [f'/music/分类/00时14分21秒_1首_{identity}',f'/m/00时25分00秒_18首_{identity}_有曲目名_50674B47',f'/m/[{identity}]']:
    self.assertEqual(identity,youtube_id(folder))
   self.assertEqual(f'1小时50分30秒-{identity}.mp4',duration_filename({'music_folder':f'/m/1首_{identity}','duration':6630}))
  self.assertIsNone(youtube_id('/m/分类/invalid'))
  self.assertEqual('0小时0分5秒-未识别ID.mp4',duration_filename({'duration':4.9}))
 def test_multiple_ids_dedup_and_no_category_id(self):
  item={'duration':61,'music':[{'path':'/music/category/1首_M9q9pvKB7hY/01.mp3'},{'path':'/music/category/1首_u8_-USakt0c/02.mp3'},{'path':'/music/category/1首_M9q9pvKB7hY/03.mp3'}]}
  self.assertEqual('0小时1分1秒-M9q9pvKB7hY+u8_-USakt0c.mp4',duration_filename(item))
  self.assertEqual('0小时0分3秒-M9q9pvKB7hY+u8_-USakt0c.mp4',duration_filename(item,3))
  self.assertEqual('Old_001.mp4',item_filename({'index':1,'music_styles':['Old']}))
 def fixture(self,root):
  app=Application(root/'state');folder=root/'outputs';folder.mkdir();source=folder/'planned.mp4';source.write_bytes(b'video')
  item={'id':'1','index':1,'status':'pending','duration':100,'output_naming':'duration-youtube-v1','music_folder':'/m/1首_M9q9pvKB7hY','output_name':source.name,'output_path':str(source),'music':[]}
  app.store.put('batch:test',{'id':'test','status':'running','config':{},'items':[item]})
  return app,source
 def test_final_rename_actual_duration_no_copy_and_collision(self):
  with tempfile.TemporaryDirectory() as tmp:
   app,source=self.fixture(Path(tmp));inode=source.stat().st_ino
   collision=source.with_name('0小时0分7秒-M9q9pvKB7hY.mp4');collision.write_bytes(b'other')
   target=app.finalize_output_name('test',0,7)
   self.assertEqual('0小时0分7秒-M9q9pvKB7hY-02.mp4',target.name)
   self.assertEqual(inode,target.stat().st_ino);self.assertFalse(source.exists());self.assertEqual(b'other',collision.read_bytes())
   self.assertEqual(str(target),app.batch('test')['items'][0]['output_path'])
   self.assertEqual(target,app.finalize_output_name('test',0,7))
   sidecar=app.write_music_sidecar(dict(app.batch('test')['items'][0],music_source_folders=['/music']),target)
   self.assertEqual(target.stem,sidecar.stem)
 def test_rename_interrupt_recovers_without_rerender(self):
  with tempfile.TemporaryDirectory() as tmp:
   app,source=self.fixture(Path(tmp));original=app.store.update;calls=[]
   def crash(key,fn):
    calls.append(1)
    if len(calls)==2:raise OSError('simulated interruption after rename')
    return original(key,fn)
   with patch.object(app.store,'update',side_effect=crash):
    with self.assertRaises(OSError):app.finalize_output_name('test',0,7)
   self.assertFalse(source.exists());self.assertIn('output_rename',app.batch('test')['items'][0])
   target=app.finalize_output_name('test',0)
   self.assertEqual('0小时0分7秒-M9q9pvKB7hY.mp4',target.name)
   self.assertNotIn('output_rename',app.batch('test')['items'][0])
