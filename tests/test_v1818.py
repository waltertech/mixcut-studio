"""Stable source-library naming, portable labels and approval-date consistency."""
import copy,tempfile,unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from mixcut.naming import item_filename,bundle_library_filename
from mixcut.bundles import date_for_approval,finish_relocation
from mixcut.server import Application

class LibraryNameTests(unittest.TestCase):
 def test_examples_and_rounds_never_change_library_label(self):
  for name in ['00时28分13秒_5首_U6deUFJXd0I','00时14分21秒_1首_M9q9pvKB7hY','01时05分30秒_12首_u8_-USakt0c']:
   item={'output_naming':'bundle-library-v1','output_bundle':'/out/2026-10-26/261026-001','music_folder':'/music/分类/'+name,'duration':100}
   for n in [1,2,3,4]:
    item.update(music_repeats=n,duration=100*n)
    self.assertEqual('261026-001_'+name+'.mp4',item_filename(item))
 def test_sanitizes_characters_limits_bytes_preserves_id_and_case(self):
  name='中文'*200+' :*?"<>|\n_5首__itb5qqD5vs'
  actual=bundle_library_filename({'output_bundle':'/out/261026-002','music_folder':'/music/'+name})
  self.assertLessEqual(len(actual.encode()),220)
  self.assertFalse(any(c in actual for c in ':/*?"<>|\n\\'))
  self.assertIn('_itb5qqD5vs',actual)
 def test_multiple_sources_keep_order_deduplicate_and_legacy_policy(self):
  item={'output_bundle':'/out/261026-001','music':[{'path':'/m/A/a.mp3'},{'path':'/m/B/b.mp3'},{'path':'/m/A/c.mp3'}]}
  self.assertEqual('261026-001_A__B.mp4',bundle_library_filename(item))
  self.assertEqual('Old_001.mp4',item_filename({'index':1,'music_styles':['Old']}))
  self.assertEqual('0小时0分5秒-M9q9pvKB7hY.mp4',item_filename({'output_naming':'duration-youtube-v1','duration':5,'music_folder':'/m/1首_M9q9pvKB7hY'}))
 def test_create_plan_round_change_and_folder_change_update_names(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();app=Application(root/'state');self.addCleanup(app.closing.set)
   songs=[{'id':folder,'path':str(root/'music'/folder/'歌曲 01.mp3'),'duration':10,'mime_type':'audio/mpeg'} for folder in ['00时00分10秒_1首_M9q9pvKB7hY','00时00分10秒_1首_u8_-USakt0c']]
   app.store.put('library',{'music_dir':str(root/'music'),'scan':{'videos':[{'id':'v','path':'/v','duration':20}],'music':songs}})
   batch=app.create_plan({'config':{'duration_basis':'music','music_mode':'folder','count':1,'music_repeats':2,'output_dir':str(root/'out'),'seed':3}})
   item=batch['items'][0];prefix=Path(item['output_bundle']).name
   self.assertEqual(prefix+'_'+Path(item['music_folder']).name+'.mp4',item['output_name'])
   changed=app.set_music_repeats(batch['id'],item['id'],{'music_repeats':4})['items'][0]
   self.assertEqual(item['output_name'],changed['output_name']);self.assertEqual(40,changed['duration'])
   other=next(str(Path(s['path']).parent) for s in songs if str(Path(s['path']).parent)!=item['music_folder'])
   changed=app.change_music_folder(batch['id'],item['id'],{'folder':other})['items'][0]
   self.assertEqual(prefix+'_'+Path(other).name+'.mp4',changed['output_name'])

class ApprovalNameTests(unittest.TestCase):
 def fixture(self,root):
  app=Application(root/'state');old=root/'out/2020-01-01/200101-001';old.mkdir(parents=True)
  record={'id':'1','index':1,'status':'success','music':[],'segments':[],'output_naming':'bundle-library-v1','music_folder':'/m/00时28分13秒_5首_U6deUFJXd0I','output_bundle':str(old)}
  name=item_filename(record);video=old/name;video.write_bytes(b'video');video.with_suffix('.txt').write_text('original doc');(old/'cover.jpg').write_bytes(b'cover')
  record.update(output_path=str(video),output_name=name)
  return app,record,video
 def test_approval_date_updates_video_txt_prefix_without_copy_or_duplicate(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();app,item,video=self.fixture(root);self.addCleanup(app.closing.set);inode=video.stat().st_ino;doc_inode=video.with_suffix('.txt').stat().st_ino
   date_for_approval(item,root/'out',app.reserve_review_bundle,lambda _:None)
   target=Path(item['output_path']);self.assertEqual(target.parent.name+'_00时28分13秒_5首_U6deUFJXd0I.mp4',target.name)
   self.assertEqual(inode,target.stat().st_ino);self.assertEqual(doc_inode,target.with_suffix('.txt').stat().st_ino);self.assertEqual('original doc',target.with_suffix('.txt').read_text());self.assertTrue((target.parent/'cover.jpg').exists())
   date_for_approval(item,root/'out',app.reserve_review_bundle,lambda _:None);self.assertEqual(str(target),item['output_path'])
 def test_resume_interruption_between_video_and_txt_rename(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();app,item,video=self.fixture(root);self.addCleanup(app.closing.set);persisted=[]
   from mixcut.fsutil import publish
   count=[]
   def interrupted(source,target):
    count.append(1)
    if len(count)==2:raise OSError('interrupt before TXT rename')
    publish(source,target)
   with patch('mixcut.bundles.publish',side_effect=interrupted):
    with self.assertRaises(OSError):date_for_approval(item,root/'out',app.reserve_review_bundle,lambda value:persisted.append(copy.deepcopy(value)))
   self.assertTrue(persisted[-1]['bundle_relocation'])
   recovered=persisted[-1];finish_relocation(recovered,lambda _:None)
   target=Path(recovered['output_path']);self.assertTrue(target.exists());self.assertTrue(target.with_suffix('.txt').exists());self.assertEqual(target.parent.name+'_'+Path(item['music_folder']).name+'.mp4',target.name)
