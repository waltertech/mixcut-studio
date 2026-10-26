"""Approval-date relocation, restart recovery, whole-bundle and inode retention."""
import copy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
import unittest
import test_v157 as fixture
from mixcut.server import Application

class ApprovalDateTests(unittest.TestCase):
    setUp = fixture.RetainedReviewTests.setUp

    def make_old(self):
        old=self.output/'2026-10-22'/'001-old';old.parent.mkdir(parents=True,exist_ok=True)
        self.folder.rename(old);self.folder=old;self.video=old/'成片.mp4'
        self.item.update(output_bundle=str(old),output_path=str(self.video))
        self.batch['output_folder']=str(old.parent)
        self.app.store.put('batch:abc123',self.batch)
        return old

    def test_cross_day_approval_preserves_inode_cover_and_documents(self):
        old=self.make_old();(old/'cover.jpg').write_bytes(b'cover');(old/'文案.txt').write_text('copy')
        inode=self.video.stat().st_ino
        result=self.app.approve({'batch_id':'abc123','item_id':'1'})
        item=result['items'][0];new=Path(item['output_bundle']);video=Path(item['output_path'])
        today=datetime.now().astimezone()
        self.assertEqual(today.strftime('%Y-%m-%d'),new.parent.name)
        self.assertRegex(new.name,'^'+today.strftime('%y%m%d')+r'-\d{3,}$')
        self.assertFalse(old.exists());self.assertEqual(inode,video.stat().st_ino)
        self.assertEqual(b'cover',(new/'cover.jpg').read_bytes());self.assertEqual('copy',(new/'文案.txt').read_text())
        self.app.approve({'batch_id':'abc123','item_id':'1'})
        reopened=Application(self.root/'state');self.addCleanup(reopened.closing.set)
        self.assertEqual(video,reopened.completed_output('abc123','1'))
        self.assertEqual(str(video),item['review']['path'])

    def test_restart_recovers_rename_before_database_path_commit(self):
        old=self.make_old();new=self.output/'2026-10-23'/'261023-001';new.parent.mkdir(parents=True,exist_ok=True)
        self.item['bundle_relocation']={'old':str(old),'new':str(new)}
        self.app.store.put('batch:abc123',self.batch);old.rename(new)
        restarted=Application(self.root/'state');self.addCleanup(restarted.closing.set)
        self.assertEqual(new/'成片.mp4',restarted.completed_output('abc123','1'))
        self.assertFalse(restarted.batch('abc123')['items'][0].get('bundle_relocation'))

    def test_failed_filesystem_rename_keeps_original_and_retry_recovers(self):
        old=self.make_old()
        with patch('mixcut.bundles.os.rename',side_effect=OSError('busy')):
            with self.assertRaises(OSError):self.app.approve({'batch_id':'abc123','item_id':'1'})
        self.assertTrue(self.video.is_file())
        result=self.app.approve({'batch_id':'abc123','item_id':'1'})
        self.assertEqual('approved',result['items'][0]['review']['status'])
        self.assertFalse(old.exists())

    def test_unfinished_sticker_dependency_blocks_rename(self):
        old=self.make_old()
        dependent={'id':'child','status':'queued','config':{},'sticker_origin':{'batch_id':'abc123','item_id':'1'},'items':[{'id':'x','status':'running'}]}
        self.app.store.put('batch:child',dependent)
        with self.assertRaisesRegex(ValueError,'贴图任务'):self.app.approve({'batch_id':'abc123','item_id':'1'})
        self.assertTrue(old.is_dir());self.assertTrue(self.video.is_file())

class LoopApprovalDateTests(unittest.TestCase):
    def test_cross_day_loop_approval_updates_file_stamp_and_retains_sources(self):
        from test_loopmusic import LoopTests
        from mixcut.loopmusic import file_stamp
        fixture = LoopTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        identity=fixture.imported()
        task=fixture.service.get(identity)
        old=Path(task['output_root'])/'2026-10-22'/'001-old';old.mkdir(parents=True)
        video=old/'成品视频.mp4';video.write_bytes(b'video');(old/'封面.png').write_bytes(b'cover')
        inode=video.stat().st_ino
        fixture.service.update(identity,status='success',output_path=str(video),output_bundle=str(old),output_stamp=file_stamp(video))
        approved=fixture.service.action(identity,'approve')
        new=Path(approved['output_path'])
        self.assertEqual('approved',approved['status']);self.assertFalse(old.exists())
        self.assertEqual(inode,new.stat().st_ino);fixture.service.check_output(approved)
        self.assertEqual(b'cover',(new.parent/'封面.png').read_bytes())
        self.assertTrue(all(p.exists() for p in fixture.songs))
        self.assertEqual(str(new),fixture.service.action(identity,'approve')['output_path'])
