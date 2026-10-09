"""Source recycling preserves recoverable files and never falls back to unlink."""
from pathlib import Path
import unittest
from unittest.mock import patch
import test_v157 as fixtures
from mixcut.media import path_identity

class SourceRecycleTests(unittest.TestCase):
    setUp = fixtures.RetainedReviewTests.setUp

    def source_fixture(self):
        directory=self.root/'originals';directory.mkdir()
        source=directory/'original.mp4';source.write_bytes(b'original')
        music=self.root/'song.mp3';music.write_bytes(b'reusable')
        stat=source.stat();asset={'id':path_identity(source),'path':str(source),'size':stat.st_size,'mtime_ns':stat.st_mtime_ns,'identity_mode':'path'}
        self.item.update(segments=[{'asset_id':asset['id'],'path':str(source),'start':0,'duration':3}],video_assets=[asset],music=[{'id':'music','path':str(music)}])
        self.batch['config']['source_video_dir']=str(directory)
        self.app.store.put('batch:abc123',self.batch)
        return source,music

    def test_approval_recycles_source_retains_export_and_music(self):
        source,music=self.source_fixture()
        self.app.approve({'batch_id':'abc123','item_id':'1'})
        self.assertFalse(source.exists());self.assertTrue(self.video.exists());self.assertTrue(music.exists())
        recycled=list((self.root/'test-trash').rglob('original.mp4'))
        self.assertEqual(1,len(recycled));self.assertEqual(b'original',recycled[0].read_bytes())
        states=self.app.batch('abc123')['items'][0]['cleanup']['source_files']
        self.assertEqual(['已移入废纸篓'],list(states.values()))

    def test_rejection_recycles_source_and_export_retains_music(self):
        source,music=self.source_fixture()
        result=self.app.reject({'batch_id':'abc123','item_id':'1','confirmation':'REJECT_VIDEO_AND_SOURCES'})
        self.assertTrue(result['ok']);self.assertFalse(source.exists());self.assertFalse(self.video.exists());self.assertTrue(music.exists())
        self.assertEqual(b'original',next((self.root/'test-trash').rglob('original.mp4')).read_bytes())

    def test_source_recycle_failure_retains_file_and_retry_succeeds(self):
        source,music=self.source_fixture()
        with patch('mixcut.server.move_to_trash',side_effect=OSError('Trash unavailable')):
            self.app.approve({'batch_id':'abc123','item_id':'1'})
        self.assertTrue(source.exists());self.assertTrue(music.exists());self.assertTrue(self.video.exists())
        self.assertFalse(self.app.batch('abc123')['items'][0]['cleanup']['original_recordings_deleted'])
        self.app.retry_approved_cleanup('abc123','1')
        self.assertFalse(source.exists());self.assertTrue(self.video.exists())
