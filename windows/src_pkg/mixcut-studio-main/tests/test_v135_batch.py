"""Batch rotation, plan editing and approval cleanup regression tests."""
from __future__ import annotations

from collections import Counter
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from mixcut import planner
from mixcut.server import Application, Handler, ThreadingHTTPServer


def asset(identity, duration, *, path=None):
    return {'id': identity, 'path': str(path or '/' + identity), 'name': identity,
            'duration': duration, 'size': 1, 'mtime_ns': 1, 'has_audio': True}


class BatchRotationTests(unittest.TestCase):
    def test_fifty_tasks_exhaust_music_and_video_sources_before_reuse(self):
        songs = [asset(f'm{index}', 12) for index in range(100)]
        videos = [asset(f'v{index}', 30) for index in range(50)]
        result = planner.plan(videos, songs, {'mode': 'single', 'music_mode': 'pool',
            'count': 50, 'min_songs': 2, 'max_songs': 2, 'min_duration': 20,
            'max_duration': 20, 'allow_overlap': False, 'seed': 135})
        chosen = [song['id'] for item in result['items'] for song in item['music']]
        sources = [item['segments'][0]['asset_id'] for item in result['items']]
        self.assertEqual(100, len(set(chosen)))
        self.assertEqual(50, len(set(sources)))
        self.assertEqual(0, result['stats']['segment_overlap_pairs'])
        self.assertTrue(all(song['batch_use_count'] == 1 for item in result['items'] for song in item['music']))

    def test_short_library_starts_second_round_only_after_first_is_used(self):
        result = planner.plan([asset(f'v{i}', 25) for i in range(3)],
            [asset(f'm{i}', 12) for i in range(4)],
            {'mode': 'single', 'music_mode': 'pool', 'count': 3, 'min_songs': 2,
             'max_songs': 2, 'min_duration': 20, 'max_duration': 20,
             'allow_overlap': False, 'seed': 29})
        ordered = [song['id'] for item in result['items'] for song in item['music']]
        self.assertEqual(4, len(set(ordered[:4])))
        self.assertEqual([1, 1, 2, 2], sorted(Counter(ordered).values()))

    def test_legacy_opening_choice_no_longer_limits_the_batch(self):
        result = planner.plan([asset(f'v{i}', 25) for i in range(3)],
            [asset(f'm{i}', 12) for i in range(7)],
            {'mode': 'single', 'music_mode': 'pool', 'count': 3, 'min_songs': 2,
             'max_songs': 2, 'min_duration': 20, 'max_duration': 20,
             'first_song_ids': ['m0'], 'allow_overlap': False, 'seed': 29})
        self.assertEqual(6, len({song['id'] for item in result['items'] for song in item['music']}))
        self.assertGreater(len({item['music'][0]['id'] for item in result['items']}), 1)

    def test_song_count_and_first_song_vary_without_an_opening_constraint(self):
        result = planner.plan([asset(f'v{i}', 30) for i in range(15)],
            [asset(f'm{i}', 12) for i in range(60)],
            {'mode': 'single', 'music_mode': 'pool', 'count': 15, 'min_songs': 1,
             'max_songs': 3, 'min_duration': 10, 'max_duration': 10,
             'allow_overlap': False, 'seed': 135})
        self.assertGreater(len({len(item['music']) for item in result['items']}), 1)
        self.assertEqual(15, len({item['music'][0]['id'] for item in result['items']}))

    def test_multi_video_uses_distinct_sources_across_batch(self):
        result = planner.plan([asset(f'v{i}', 100) for i in range(30)],
            [asset(f'm{i}', 30) for i in range(10)],
            {'mode': 'multi', 'music_mode': 'pool', 'count': 10, 'min_songs': 1,
             'max_songs': 1, 'min_duration': 30, 'max_duration': 30,
             'segment_min': 15, 'segment_max': 15, 'seed': 18})
        used = [part['asset_id'] for item in result['items'] for part in item['segments']]
        self.assertEqual(20, len(set(used)))
        self.assertEqual(0, result['stats']['segment_overlap_pairs'])


class PlanEditingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Application(self.root / 'state')
        self.videos = [asset(f'v{i}', 40) for i in range(4)]
        self.songs = [asset(f'm{i}', 12) for i in range(8)]
        self.config = {'mode': 'single', 'music_mode': 'pool', 'count': 3, 'min_songs': 1,
                       'max_songs': 1, 'min_duration': 10, 'max_duration': 10,
                       'allow_overlap': False, 'seed': 35,
                       'width': 640, 'height': 360, 'fps': 30,
                       'video_ids': [v['id'] for v in self.videos],
                       'music_ids': [m['id'] for m in self.songs],
                       'first_song_ids': [], 'output_dir': str(self.root / 'exports')}
        result = planner.plan(self.videos, self.songs, self.config)
        self.batch = self.app.make_batch(self.config, result, self.videos + self.songs)

    def tearDown(self):
        self.app.closing.set()
        self.temp.cleanup()

    def test_individual_replacements_and_group_refresh_recompute_counts(self):
        batch_id = self.batch['id']
        item_id = self.batch['items'][0]['id']
        old_song = self.batch['items'][0]['music'][0]['id']
        old_video = self.batch['items'][0]['segments'][0]['asset_id']
        changed = self.app.replace_media(batch_id, item_id, {'kind': 'music', 'index': 0})
        self.assertNotEqual(old_song, changed['items'][0]['music'][0]['id'])
        changed = self.app.replace_media(batch_id, item_id, {'kind': 'video', 'index': 0})
        self.assertNotEqual(old_video, changed['items'][0]['segments'][0]['asset_id'])
        self.assertEqual(0, changed['stats']['segment_overlap_pairs'])
        refreshed = self.app.refresh_music(batch_id, item_id)
        self.assertEqual(3, len(refreshed['items']))
        for item in refreshed['items']:
            for song in item['music']:
                self.assertEqual(refreshed['stats']['music_usage'][song['id']], song['batch_use_count'])
            for segment in item['segments']:
                self.assertEqual(1, segment['batch_use_count'])
        self.assertEqual(refreshed['stats']['music_usage'], self.app.batch(batch_id)['stats']['music_usage'])

    def test_refresh_http_route_is_available(self):
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        httpd.app = self.app
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            path = f"/api/batches/{self.batch['id']}/items/{self.batch['items'][0]['id']}/refresh-music"
            client = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
            client.request('POST', path, body='{}', headers={'Content-Type': 'application/json'})
            response = client.getresponse()
            payload = json.loads(response.read())
            client.close()
            self.assertEqual(200, response.status, payload)
            self.assertEqual(self.batch['id'], payload['id'])
        finally:
            httpd.shutdown()
            thread.join(timeout=3)
            httpd.server_close()

    def test_replacement_is_rejected_after_rendering_starts(self):
        self.app.store.update(self.batch['id'], lambda value: value.update(status='queued'))
        with self.assertRaisesRegex(ValueError, '草稿'):
            self.app.replace_media(self.batch['id'], self.batch['items'][0]['id'],
                                   {'kind': 'music', 'index': 0})

    def test_manual_reorder_allows_any_song_first_in_legacy_plan(self):
        config = dict(self.config, min_songs=2, max_songs=2, first_song_ids=['m0'])
        batch = self.app.make_batch(config, planner.plan(self.videos, self.songs, config),
                                    self.videos + self.songs)
        item = batch['items'][0]
        ids = [song['id'] for song in item['music']]
        changed = self.app.reorder_music(batch['id'], item['id'], ids[::-1])
        self.assertEqual(ids[::-1], [song['id'] for song in changed['items'][0]['music']])


class ApprovalCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Application(self.root / 'state')
        self.video_dir = self.root / 'videos'
        self.video_dir.mkdir()
        self.source = self.video_dir / 'clip.ts'
        self.source.write_bytes(b'original recording')
        stat = self.source.stat()
        self.source_asset = {'id': hashlib.sha256(self.source.read_bytes()).hexdigest(),
                             'path': str(self.source), 'name': self.source.name, 'duration': 20,
                             'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
        self.export = self.root / 'exports' / 'batch'
        self.export.mkdir(parents=True)
        items = []
        for index in (1, 2):
            output = self.export / f'{index:03d}.mp4'
            output.write_bytes(f'finished-{index}'.encode())
            output_stat = output.stat()
            items.append({'id': str(index), 'index': index, 'status': 'success', 'duration': 10,
                          'segments': [{'asset_id': self.source_asset['id'], 'path': str(self.source),
                                        'start': 0, 'duration': 10}],
                          'video_assets': [self.source_asset], 'music': [], 'output_path': str(output),
                          'result': {'output_size': output_stat.st_size,
                                     'output_mtime_ns': output_stat.st_mtime_ns}})
        self.app.store.put('batch:abc123', {'id': 'abc123', 'status': 'completed', 'created_at': 1,
            'updated_at': 1, 'items': items, 'config': {'output_dir': str(self.root / 'exports'),
            'source_video_dir': str(self.video_dir)}, 'output_folder': str(self.export)})

    def tearDown(self):
        self.app.closing.set()
        self.temp.cleanup()

    def test_source_is_deferred_until_last_reference_is_approved(self):
        first = self.app.approve({'batch_id': 'abc123', 'item_id': '1',
                                  'review_dir': str(self.root / 'approved')})
        self.assertTrue(self.source.exists())
        self.assertFalse(Path(first['items'][0]['output_path']).exists())
        self.assertTrue(Path(first['items'][0]['review']['path']).exists())
        self.assertEqual(Path(first['items'][0]['review']['path']),
                         self.app.completed_output('abc123', '1'))
        self.assertIn('等待另外 1 条', next(iter(first['items'][0]['cleanup']['source_files'].values())))
        second = self.app.approve({'batch_id': 'abc123', 'item_id': '2',
                                   'review_dir': str(self.root / 'approved')})
        self.assertFalse(self.source.exists())
        self.assertFalse(Path(second['items'][1]['output_path']).exists())
        self.assertTrue(all(item['cleanup']['original_recordings_deleted'] for item in second['items']))

    def test_changed_source_is_preserved_with_cleanup_error(self):
        self.app.approve({'batch_id': 'abc123', 'item_id': '1',
                          'review_dir': str(self.root / 'approved')})
        self.source.write_bytes(b'changed recording')
        second = self.app.approve({'batch_id': 'abc123', 'item_id': '2',
                                   'review_dir': str(self.root / 'approved')})
        self.assertTrue(self.source.exists())
        self.assertTrue(second['items'][1]['review']['status'] == 'approved')
        self.assertIn('删除失败', next(iter(second['items'][1]['cleanup']['source_files'].values())))

    def test_missing_dependent_archive_preserves_original_recording(self):
        first = self.app.approve({'batch_id': 'abc123', 'item_id': '1',
                                  'review_dir': str(self.root / 'approved')})
        Path(first['items'][0]['review']['path']).unlink()
        second = self.app.approve({'batch_id': 'abc123', 'item_id': '2',
                                   'review_dir': str(self.root / 'approved')})
        self.assertTrue(self.source.exists())
        self.assertIn('审核成片缺失', next(iter(second['items'][1]['cleanup']['source_files'].values())))

    def test_legacy_plan_without_recorded_source_root_keeps_recording(self):
        self.app.store.update('abc123', lambda value: value['config'].pop('source_video_dir'))
        self.app.approve({'batch_id': 'abc123', 'item_id': '1',
                          'review_dir': str(self.root / 'approved')})
        second = self.app.approve({'batch_id': 'abc123', 'item_id': '2',
                                   'review_dir': str(self.root / 'approved')})
        self.assertTrue(self.source.exists())
        self.assertIn('无法确认源文件属于已扫描', next(iter(second['items'][1]['cleanup']['source_files'].values())))

    def test_restart_retries_working_output_cleanup(self):
        approved = self.app.approve({'batch_id': 'abc123', 'item_id': '1',
                                     'review_dir': str(self.root / 'approved')})
        output = Path(approved['items'][0]['output_path'])
        output.write_bytes(b'finished-1')
        self.app.store.update('abc123', lambda value: value['items'][0]['cleanup'].update(output_deleted=False))
        restarted = Application(self.root / 'state')
        self.assertFalse(output.exists())
        self.assertTrue(restarted.batch('abc123')['items'][0]['cleanup']['output_deleted'])

    def test_restart_retries_output_cleanup_for_variant_without_source_segments(self):
        self.app.store.update('abc123', lambda value: value['items'][0].update(segments=[]))
        approved = self.app.approve({'batch_id': 'abc123', 'item_id': '1',
                                     'review_dir': str(self.root / 'approved')})
        output = Path(approved['items'][0]['output_path'])
        output.write_bytes(b'finished-1')
        self.app.store.update('abc123', lambda value: value['items'][0]['cleanup'].update(output_deleted=False))
        restarted = Application(self.root / 'state')
        self.assertFalse(output.exists())
        self.assertTrue(restarted.batch('abc123')['items'][0]['cleanup']['output_deleted'])

    def test_missing_review_copy_keeps_working_output(self):
        approved = self.app.approve({'batch_id': 'abc123', 'item_id': '1',
                                     'review_dir': str(self.root / 'approved')})
        item = approved['items'][0]
        output = Path(item['output_path'])
        output.write_bytes(b'finished-1')
        Path(item['review']['path']).unlink()
        self.app.store.update('abc123', lambda value: value['items'][0]['cleanup'].update(output_deleted=False))
        restarted = Application(self.root / 'state')
        self.assertTrue(output.exists())
        self.assertIn('审核副本缺失', restarted.batch('abc123')['items'][0]['cleanup']['output_error'])

    def test_changed_review_copy_is_not_used_for_preview(self):
        approved = self.app.approve({'batch_id': 'abc123', 'item_id': '1',
                                     'review_dir': str(self.root / 'approved')})
        archive = Path(approved['items'][0]['review']['path'])
        self.assertEqual(archive, self.app.completed_output('abc123', '1'))
        archive.write_bytes(b'modified-review-copy')
        with self.assertRaisesRegex(ValueError, '校验失败'):
            self.app.completed_output('abc123', '1')


if __name__ == '__main__':
    unittest.main()
