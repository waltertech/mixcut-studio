"""Regression cases for task deletion, source cleanup and linked replanning."""
from __future__ import annotations

from collections import Counter
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mixcut import planner
from mixcut.server import Application


def asset(identity, duration, path=None):
    return {'id': identity, 'path': str(path or '/' + identity), 'name': identity,
            'duration': duration, 'size': 1, 'mtime_ns': 1}


class MusicPlanningTests(unittest.TestCase):
    def test_twenty_short_songs_can_cover_hour_without_batch_reuse(self):
        videos = [asset('v1', 4000), asset('v2', 4000)]
        songs = [asset(f'm{n}', 240) for n in range(40)]
        result = planner.plan(videos, songs, {'mode': 'single', 'music_mode': 'pool',
            'count': 2, 'min_duration': 3600, 'max_duration': 3600,
            'min_songs': 15, 'max_songs': 20, 'first_song_ids': ['m0'],
            'allow_overlap': False, 'seed': 138})
        chosen = [song['id'] for item in result['items'] for song in item['music']]
        self.assertEqual(len(chosen), len(set(chosen)))
        self.assertTrue(all(15 <= len(item['music']) <= 20 for item in result['items']))
        self.assertTrue(all(item['music_total_duration'] >= item['duration'] for item in result['items']))
        self.assertEqual(1, max(Counter(chosen).values()))


class EditingAndDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Application(self.root / 'state')
        self.videos = [asset('v1', 20), asset('v2', 30)]
        self.songs = [asset(f'm{n}', 10) for n in range(8)]
        self.config = {'mode': 'single', 'music_mode': 'pool', 'min_duration': 10,
                       'max_duration': 30, 'min_songs': 2, 'max_songs': 4,
                       'allow_overlap': False, 'width': 640, 'height': 360, 'fps': 30,
                       'video_ids': ['v1', 'v2'], 'music_ids': [song['id'] for song in self.songs],
                       'output_dir': str(self.root / 'outputs')}
        item = {'id': 'one', 'index': 1, 'duration': 20, 'status': 'pending',
                'segments': [{'asset_id': 'v1', 'path': '/v1', 'start': 0, 'duration': 20}],
                'video_assets': [self.videos[0]], 'music': self.songs[:2],
                'music_total_duration': 20}
        self.batch = self.app.make_batch(self.config, {'items': [item], 'stats': {}},
                                         self.videos + self.songs)

    def tearDown(self):
        self.app.closing.set()
        self.temp.cleanup()

    def test_longer_video_atomically_replans_entire_music_list(self):
        result = self.app.replace_media(self.batch['id'], 'one',
                                        {'kind': 'video', 'index': 0, 'replan_duration': True})
        item = result['items'][0]
        self.assertEqual('v2', item['segments'][0]['asset_id'])
        self.assertEqual(30, item['duration'])
        self.assertGreaterEqual(item['music_total_duration'], item['duration'])
        self.assertGreaterEqual(len(item['music']), 3)
        self.assertEqual(len(item['music']), len({song['id'] for song in item['music']}))

    def test_failed_linked_replan_keeps_original_plan(self):
        self.app.store.update(self.batch['id'], lambda batch: batch['config'].update(
            music_ids=['m0', 'm1'], max_songs=2))
        before = copy.deepcopy(self.app.batch(self.batch['id'])['items'][0])
        with self.assertRaisesRegex(ValueError, '联动刷新音乐失败'):
            self.app.replace_media(self.batch['id'], 'one',
                                   {'kind': 'video', 'index': 0, 'replan_duration': True})
        self.assertEqual(before, self.app.batch(self.batch['id'])['items'][0])

    def test_bulk_delete_preserves_cleanup_snapshot_and_output(self):
        path = Path(self.batch['items'][0]['output_path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'finished video')
        self.app.store.update(self.batch['id'], lambda batch: batch['items'][0].update(
            status='success', review={'status': 'approved', 'path': str(self.root / 'archive.mp4')},
            cleanup={'source_cleanup_requested': True, 'original_recordings_deleted': False}))
        with self.assertRaisesRegex(ValueError, '二次确认'):
            self.app.delete_items([{'batch_id': self.batch['id'], 'item_ids': ['one']}], None)
        result = self.app.delete_items([{'batch_id': self.batch['id'], 'item_ids': ['one']}],
                                       'DELETE_TASK_RECORDS', 1)
        self.assertEqual(1, result['deleted_items'])
        self.assertTrue(path.is_file())
        self.assertIsNone(self.app.store.get('batch:' + self.batch['id']))
        snapshot = self.app.store.get('deletion:' + result['deletion_record_id'])
        self.assertFalse(snapshot['entries'][0]['items'][0]['cleanup']['original_recordings_deleted'])

    def test_draft_partial_delete_returns_authoritative_remaining_batch(self):
        second = copy.deepcopy(self.batch['items'][0])
        second.update(id='two', index=2)
        self.app.store.update(self.batch['id'], lambda batch: batch['items'].append(second))
        result = self.app.delete_items([{'batch_id': self.batch['id'], 'item_ids': ['one']}],
                                       'DELETE_TASK_RECORDS', 1)
        remaining = result['updated_batches'][self.batch['id']]
        self.assertEqual(['two'], [item['id'] for item in remaining['items']])
        self.assertEqual(1, remaining['items'][0]['index'])
        self.assertEqual(1, remaining['stats']['count'])
        final = self.app.delete_items([{'batch_id': self.batch['id'], 'item_ids': ['two']}],
                                      'DELETE_TASK_RECORDS', 1)
        self.assertIsNone(final['updated_batches'][self.batch['id']])

    def test_post_delete_cleanup_error_does_not_hide_successful_deletion(self):
        second = copy.deepcopy(self.batch['items'][0])
        second.update(id='two', index=2)
        self.app.store.update(self.batch['id'], lambda batch: batch['items'].append(second))
        with patch.object(self.app, '_cleanup_approved_sources', side_effect=RuntimeError('cleanup unavailable')), \
                patch('mixcut.server.logging.exception') as logged:
            result = self.app.delete_items([{'batch_id': self.batch['id'], 'item_ids': ['one']}],
                                           'DELETE_TASK_RECORDS', 1)
        logged.assert_called_once()
        self.assertEqual(1, result['deleted_items'])
        self.assertIn('cleanup unavailable', result['cleanup_warning'])
        self.assertEqual(['two'], [item['id'] for item in result['updated_batches'][self.batch['id']]['items']])

    def test_bulk_delete_rejects_active_batch_before_writing_snapshot(self):
        self.app.store.update(self.batch['id'], lambda batch: batch.update(status='running'))
        with self.assertRaisesRegex(ValueError, '请先停止'):
            self.app.delete_items([{'batch_id': self.batch['id'], 'item_ids': ['one']}],
                                  'DELETE_TASK_RECORDS', 1)
        self.assertEqual([], self.app.store.records('deletion:'))


class LegacySourceCleanupTests(unittest.TestCase):
    def test_scanned_asset_allows_safe_cleanup_of_legacy_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = Application(root / 'state')
            source_dir = root / 'videos'
            source_dir.mkdir()
            source = source_dir / 'old.ts'
            source.write_bytes(b'original source')
            stat = source.stat()
            source_asset = asset(hashlib.sha256(source.read_bytes()).hexdigest(), 20, source)
            source_asset.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
            archive = root / 'review.mp4'
            archive.write_bytes(b'approved output')
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            item = {'id': 'one', 'index': 1, 'status': 'success', 'output_path': str(root / 'done.mp4'),
                    'segments': [{'asset_id': source_asset['id'], 'path': str(source), 'start': 0,
                                  'duration': 20}], 'video_assets': [source_asset], 'music': [],
                    'review': {'status': 'approved', 'path': str(archive), 'sha256': digest},
                    'cleanup': {'source_cleanup_requested': True, 'original_recordings_deleted': False}}
            app.store.put('library', {'video_dir': str(source_dir),
                                      'scan': {'videos': [source_asset], 'music': []}})
            app.store.put('batch:old', {'id': 'old', 'created_at': 1, 'updated_at': 1,
                'status': 'completed', 'config': {}, 'items': [item]})
            app._cleanup_approved_sources()
            self.assertFalse(source.exists())
            self.assertTrue(app.batch('old')['items'][0]['cleanup']['original_recordings_deleted'])


if __name__ == '__main__':
    unittest.main()
