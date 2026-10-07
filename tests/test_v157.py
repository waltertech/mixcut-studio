"""Retention, bundle ownership, native trash and legacy shared directory tests."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from mixcut.server import Application


class RetainedReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.app=Application(self.root/'state')
        self.addCleanup(self.app.closing.set)
        self.output=self.root/'1007 aidj';self.folder=self.output/'2026-10-07'/'001-1007 aidj';self.folder.mkdir(parents=True)
        self.video=self.folder/'成片.mp4';self.video.write_bytes(b'video');st=self.video.stat()
        self.item={'id':'1','index':1,'duration':3,'status':'success','progress':1,'music':[], 'music_source_folders':['/music'],
          'segments':[],'output_bundle':str(self.folder),'output_path':str(self.video),'result':{'output_size':st.st_size,'output_mtime_ns':st.st_mtime_ns}}
        self.batch={'id':'abc123','status':'completed','items':[self.item],'config':{'output_dir':str(self.output)},'output_folder':str(self.folder.parent)}
        self.app.store.put('batch:abc123',self.batch)

    def test_approval_keeps_inode_no_copy_hash_or_transfer_and_restart(self):
        st=self.video.stat()
        with patch('shutil.copyfile',side_effect=AssertionError('no copying')),patch.object(self.app,'file_digest',side_effect=AssertionError('no hashing')):
            result=self.app.approve({'batch_id':'abc123','item_id':'1'})
            self.app.approve({'batch_id':'abc123','item_id':'1'})
            self.assertEqual(self.video,self.app.completed_output('abc123','1'))
        self.assertEqual(st.st_ino,self.video.stat().st_ino)
        self.assertTrue(result['items'][0]['review']['retained'])
        self.assertTrue(self.video.with_suffix('.txt').is_file())
        other=Application(self.root/'state');self.addCleanup(other.closing.set)
        self.assertEqual(self.video,other.completed_output('abc123','1'))
        self.assertTrue(self.video.exists())

    def recycle(self,path):
        target=self.root/'trash'/Path(path).name;target.parent.mkdir(exist_ok=True);Path(path).rename(target)
        return str(target)

    def test_delete_approved_bundle_recycles_whole_folder_and_removes_row(self):
        self.app.approve({'batch_id':'abc123','item_id':'1'})
        (self.folder/'封面.txt').write_text('belongs to task')
        with patch('mixcut.trash.move_to_trash',side_effect=self.recycle):
            result=self.app.forget_items([{'batch_id':'abc123','item_ids':['1']}])
        self.assertEqual(1,result['deleted_items']);self.assertFalse(self.folder.exists())
        self.assertTrue((self.root/'trash'/self.folder.name/'封面.txt').exists())
        self.assertIsNone(self.app.store.get('batch:abc123'))

    def test_failure_preserves_record_and_all_files(self):
        with patch('mixcut.trash.move_to_trash',side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):self.app.forget_items([{'batch_id':'abc123','item_ids':['1']}])
        self.assertTrue(self.video.exists());self.assertEqual(1,len(self.app.batch('abc123')['items']))

    def test_old_shared_folder_deletes_only_matching_video_and_txt(self):
        self.item.pop('output_bundle');self.app.store.put('batch:abc123',self.batch)
        sibling=self.folder/'other.mp4';sibling.write_bytes(b'keep')
        self.video.with_suffix('.txt').write_text('doc')
        with patch('mixcut.trash.move_to_trash',side_effect=self.recycle):self.app.forget_items([{'batch_id':'abc123','item_ids':['1']}])
        self.assertTrue(sibling.exists());self.assertTrue(self.folder.exists());self.assertFalse(self.video.exists())

    def test_rejection_trashes_bundle_without_touching_music(self):
        with patch('mixcut.trash.move_to_trash',side_effect=self.recycle):
            result=self.app.reject({'batch_id':'abc123','item_id':'1','confirmation':'REJECT_VIDEO_AND_SOURCES'})
        self.assertTrue(result['ok']);self.assertFalse(self.folder.exists())

    def test_shared_original_source_deleted_only_after_both_retained_approvals(self):
        import copy
        from mixcut.media import path_identity
        source_dir=self.root/'recordings';source_dir.mkdir();source=source_dir/'source.mp4';source.write_bytes(b'original')
        st=source.stat();asset={'id':path_identity(source),'path':str(source),'size':st.st_size,'mtime_ns':st.st_mtime_ns,'identity_mode':'path'}
        self.item.update(segments=[{'asset_id':asset['id'],'path':str(source),'start':0,'duration':3}],video_assets=[asset])
        second=copy.deepcopy(self.item);second['id']='2';second['index']=2
        folder=self.folder.parent/'002-1007 aidj';folder.mkdir();output=folder/'second.mp4';output.write_bytes(b'second')
        stat=output.stat();second.update(output_bundle=str(folder),output_path=str(output),result={'output_size':stat.st_size,'output_mtime_ns':stat.st_mtime_ns})
        self.batch['items'].append(second);self.batch['config']['source_video_dir']=str(source_dir)
        self.app.store.put('batch:abc123',self.batch)
        with patch.object(self.app,'file_digest',side_effect=AssertionError('retained approvals must not hash videos')):
            self.app.approve({'batch_id':'abc123','item_id':'1'});self.assertTrue(source.exists())
            self.app.approve({'batch_id':'abc123','item_id':'2'});self.assertFalse(source.exists())
        self.assertTrue(self.video.exists());self.assertTrue(output.exists())
