import concurrent.futures
from datetime import datetime
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from recycle_fixture import install_recycle_fixture
from mixcut.server import Application


class LegacyArchiveReviewTests(unittest.TestCase):
    def setUp(self):
        legacy=patch.object(Application, "approve", Application._approve_legacy)
        legacy.start();self.addCleanup(legacy.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        install_recycle_fixture(self,self.root)
        self.app = Application(self.root / 'state')
        self.export = self.root / 'exports' / '2026-09-22_001'
        self.export.mkdir(parents=True)
        self.target_root = self.root / 'reviewed'
        items = []
        for index in (1, 2):
            file = self.export / f'{index:03d}.mp4'
            file.write_bytes(f'original-video-{index}'.encode() * 1000)
            stat = file.stat()
            items.append({'id': str(index), 'index': index, 'status': 'success', 'output_path': str(file),
                          'music': [{'path': str(self.root / 'music' / '去重歌曲03' / f'track-{index}.mp3')}],
                          'result': {'output_size': stat.st_size, 'output_mtime_ns': stat.st_mtime_ns}})
        self.record = {'id': 'abc123', 'created_at': 1, 'updated_at': 1, 'status': 'completed',
                       'config': {'output_dir': str(self.root / 'exports')},
                       'output_folder': str(self.export), 'items': items}
        self.app.store.put('batch:abc123', self.record)

    def tearDown(self):
        self.temp.cleanup()

    def approve(self, item='1'):
        return self.app.approve({'batch_id': 'abc123', 'item_id': item, 'review_dir': str(self.target_root)})

    def wait_for_batch_review(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.app.batch_review_status('abc123')
            if job['status'] != 'running':
                return job
            time.sleep(.01)
        self.fail('batch review did not finish')

    def test_approved_task_vanishes_from_public_list_and_can_be_pruned(self):
        self.approve()
        self.assertEqual(['2'], [item['id'] for item in self.app.visible_batches()[0]['items']])
        self.app.prune_forgotten_items()
        self.assertEqual(['2'], [item['id'] for item in self.app.batch('abc123')['items']])

    def test_global_review_finishes_without_a_visible_batch_group(self):
        started = self.app.start_review_all({'review_dir': str(self.target_root)})
        self.assertEqual(2, started['total'])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.app.review_all_status()['status'] == 'running':
            time.sleep(.01)
        job = self.app.review_all_status()
        self.assertEqual('completed', job['status'])
        self.assertEqual(2, job['completed'])
        self.assertEqual([], self.app.visible_batches())

    def test_batch_review_uses_task_index_order_and_skips_approved(self):
        self.record['items'].reverse()
        self.app.store.put('batch:abc123', self.record)
        calls = []
        original = self.app.approve
        def record_approval(body):
            calls.append(body['item_id'])
            return original(body)
        with patch.object(self.app, 'approve', side_effect=record_approval):
            self.app.start_batch_review('abc123', {'review_dir': str(self.target_root),
                                                   'item_ids': ['1', '2']})
            job = self.wait_for_batch_review()
        self.assertEqual('completed', job['status'])
        self.assertEqual(['1', '2'], calls)
        self.assertEqual(2, job['completed'])
        self.assertEqual(2, len(list(self.target_root.rglob('*.mp4'))))
        with self.assertRaisesRegex(ValueError, '没有可审核'):
            self.app.start_batch_review('abc123', {'review_dir': str(self.target_root), 'item_ids': []})

    def test_batch_review_stops_on_error_and_can_resume(self):
        original = self.app.approve
        def fail_second(body):
            if body['item_id'] == '2':
                raise OSError('simulated archive failure')
            return original(body)
        with patch.object(self.app, 'approve', side_effect=fail_second):
            self.app.start_batch_review('abc123', {'review_dir': str(self.target_root),
                                                   'item_ids': ['1', '2']})
            job = self.wait_for_batch_review()
        self.assertEqual('failed', job['status'])
        self.assertEqual('2', job['failed_item'])
        self.assertEqual(1, job['completed'])
        self.assertEqual('approved', self.app.batch('abc123')['items'][0]['review']['status'])
        self.app.start_batch_review('abc123', {'review_dir': str(self.target_root), 'item_ids': ['2']})
        self.assertEqual('completed', self.wait_for_batch_review()['status'])
        self.assertEqual(2, len(list(self.target_root.rglob('*.mp4'))))

    def test_batch_review_prevents_deleting_records_while_archiving(self):
        entered, release = threading.Event(), threading.Event()
        original = self.app.approve
        def delayed_approval(body):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test review wait expired')
            return original(body)
        with patch.object(self.app, 'approve', side_effect=delayed_approval):
            self.app.start_batch_review('abc123', {'review_dir': str(self.target_root),
                                                   'item_ids': ['1', '2']})
            self.assertTrue(entered.wait(2))
            with self.assertRaisesRegex(ValueError, '正在一键审核'):
                self.app.delete_items([{'batch_id': 'abc123', 'item_ids': ['1']}],
                                      'DELETE_TASK_RECORDS', 1)
            release.set()
            self.assertEqual('completed', self.wait_for_batch_review()['status'])

    def test_copy_cleans_working_output_and_groups_approved_items(self):
        original = (self.export / '001.mp4').read_bytes()
        first = self.approve()['items'][0]['review']
        second = self.approve('2')['items'][1]['review']
        self.assertEqual('approved', first['status'])
        self.assertEqual(self.target_root, Path(first['path']).parent.parent)
        self.assertEqual(Path(first['path']).parent.parent, Path(second['path']).parent.parent)
        self.assertNotEqual(Path(first['path']).parent, Path(second['path']).parent)
        self.assertEqual('去重歌曲03_001', Path(first['path']).stem)
        self.assertEqual('去重歌曲03_002', Path(second['path']).stem)
        self.assertEqual('001-reviewed', Path(first['path']).parent.name)
        self.assertEqual('002-reviewed', Path(second['path']).parent.name)
        self.assertEqual(original, Path(first['path']).read_bytes())
        sidecar = Path(first['path']).with_suffix('.txt')
        self.assertEqual([str(self.root / 'music' / '去重歌曲03')], sidecar.read_text().splitlines())
        self.assertEqual(str(sidecar), first['music_paths'])
        self.assertFalse((self.export / '001.mp4').exists())
        reopened_item = Application(self.root / 'state').batch('abc123')['items'][0]
        self.assertEqual(first, reopened_item['review'])
        self.assertTrue(reopened_item['cleanup']['output_deleted'])
        self.assertTrue(reopened_item['cleanup']['temporary_segments_deleted'])
        self.assertFalse(reopened_item['cleanup']['original_recordings_deleted'])

    def test_double_click_is_idempotent_even_concurrently(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.approve(), range(2)))
        self.assertEqual(results[0]['items'][0]['review'], results[1]['items'][0]['review'])
        self.assertEqual(1, len(list(self.target_root.rglob('*.mp4'))))
        self.assertEqual(1, len(list(self.target_root.rglob('*.txt'))))

    def test_already_approved_video_backfills_missing_music_txt(self):
        approved = self.approve()['items'][0]['review']
        sidecar = Path(approved['path']).with_suffix('.txt')
        sidecar.unlink()

        reopened = Application(self.root / 'state')
        result = reopened.batch('abc123')['items'][0]['review']

        self.assertTrue(sidecar.is_file())
        self.assertEqual([str(self.root / 'music' / '去重歌曲03')], sidecar.read_text().splitlines())
        self.assertEqual('approved', result['status'])

    def test_preferences_partial_update_preserves_output_folder(self):
        self.app.preferences({'output_dir': str(self.export)})
        self.app.preferences({'review_dir': str(self.target_root)})
        data = self.app.bootstrap()
        self.assertEqual(str(self.export), data['output_dir'])
        self.assertEqual(str(self.target_root), data['review_dir'])

    def test_only_successful_outputs_can_be_approved(self):
        self.record['items'][0]['status'] = 'pending'
        self.app.store.put('batch:abc123', self.record)
        with self.assertRaisesRegex(ValueError, '只能审核'):
            self.approve()

    def test_copy_failure_is_not_recorded_as_approved(self):
        with patch('mixcut.server.shutil.copyfile', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.approve()
        self.assertEqual('failed', self.app.batch('abc123')['items'][0]['review']['status'])
        self.assertTrue((self.export / '001.mp4').exists())
        self.assertEqual([], list(self.target_root.rglob('*.mp4')))
        self.assertEqual('approved', self.approve()['items'][0]['review']['status'])

    def test_different_existing_file_is_not_overwritten(self):
        day = datetime.now().astimezone().strftime('%Y-%m-%d')
        folder = self.target_root / '001-reviewed'; folder.mkdir(parents=True)
        target = folder / '去重歌曲03_001.mp4'
        target.write_bytes(b'keep-existing')
        approved = self.approve()['items'][0]['review']
        self.assertEqual(b'keep-existing', target.read_bytes())
        self.assertEqual('去重歌曲03_002.mp4', Path(approved['path']).name)

    def test_restart_recovers_copy_finished_before_state_commit(self):
        approved = self.approve()['items'][0]['review']
        before = Path(approved['path']).stat().st_mtime_ns
        self.app.store.update('abc123', lambda b: b['items'][0]['review'].update(status='copying'))
        self.app = Application(self.root / 'state')
        self.assertEqual('completed', self.app.batch('abc123')['status'])
        self.assertEqual('approved', self.app.batch('abc123')['items'][0]['review']['status'])
        self.assertEqual(before, Path(approved['path']).stat().st_mtime_ns)

    def test_modified_original_is_rejected(self):
        (self.export / '001.mp4').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '已被修改'):
            self.approve()

    def test_new_review_root_does_not_move_previous_approvals(self):
        first = self.approve()['items'][0]['review']
        self.target_root = self.root / 'new-review-root'
        second = self.approve('2')['items'][1]['review']
        self.assertNotEqual(Path(first['path']).parent, Path(second['path']).parent)
        self.assertTrue(Path(first['path']).is_file())

    def sticker_replacement(self, *, shared_source=False, another_variant=False):
        original = self.export / '001.mp4'
        stat = original.stat()
        source_asset = {'id': hashlib.sha256(original.read_bytes()).hexdigest(),
                        'path': str(original), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
        recordings = self.root / 'recordings'
        recordings.mkdir()
        raw = recordings / 'capture.ts'
        raw.write_bytes(b'original-recording')
        raw_stat = raw.stat()
        raw_asset = {'id': hashlib.sha256(raw.read_bytes()).hexdigest(), 'path': str(raw),
                     'size': raw_stat.st_size, 'mtime_ns': raw_stat.st_mtime_ns}
        self.record['config']['source_video_dir'] = str(recordings)
        self.record['items'][0]['segments'] = [{'path': str(raw), 'asset_id': raw_asset['id']}]
        self.record['items'][0]['video_assets'] = [raw_asset]
        if shared_source:
            self.record['items'][1]['segments'] = [{'path': str(raw), 'asset_id': raw_asset['id']}]
            self.record['items'][1]['video_assets'] = [raw_asset]
        self.app.store.put('batch:abc123', self.record)
        replacement = self.root / 'exports' / 'sticker' / 'sticker.mp4'
        replacement.parent.mkdir()
        replacement.write_bytes(b'accepted-sticker-output')
        replacement_stat = replacement.stat()
        batch = {'id': 'sticker01', 'status': 'completed', 'created_at': 2, 'updated_at': 2,
                 'config': {'output_dir': str(self.root / 'exports')},
                 'output_folder': str(replacement.parent),
                 'sticker_origin': {'batch_id': 'abc123', 'item_id': '1',
                                    'replace_origin_on_approval': True},
                 'items': [{'id': 'sticker-item', 'index': 1, 'kind': 'sticker_variant',
                            'status': 'success', 'segments': [], 'music': [],
                            'source_asset': source_asset, 'output_path': str(replacement),
                            'result': {'output_size': replacement_stat.st_size,
                                       'output_mtime_ns': replacement_stat.st_mtime_ns}}]}
        self.app.store.put('batch:sticker01', batch)
        if another_variant:
            self.app.store.put('batch:sticker02', {
                'id': 'sticker02', 'status': 'draft', 'created_at': 3, 'updated_at': 3,
                'sticker_origin': {'batch_id': 'abc123', 'item_id': '1'},
                'items': [{'id': 'other', 'status': 'pending', 'output_path': str(self.root / 'other.mp4')}]})
        return original, raw, replacement

    def test_approving_sticker_retires_original_and_deletes_unshared_recording(self):
        original, raw, replacement = self.sticker_replacement()
        approved = self.app.approve({'batch_id': 'sticker01', 'item_id': 'sticker-item',
                                     'review_dir': str(self.target_root)})
        self.assertTrue(Path(approved['items'][0]['review']['path']).is_file())
        self.assertFalse(replacement.exists())
        self.assertFalse(original.exists())
        self.assertFalse(raw.exists())
        parent = self.app.batch('abc123')['items'][0]
        self.assertEqual('superseded', parent['review']['status'])
        self.assertTrue(parent['cleanup']['output_deleted'])
        self.assertTrue(parent['cleanup']['original_recordings_deleted'])

    def test_shared_recording_waits_for_other_task_review(self):
        original, raw, _ = self.sticker_replacement(shared_source=True)
        self.app.approve({'batch_id': 'sticker01', 'item_id': 'sticker-item',
                          'review_dir': str(self.target_root)})
        self.assertFalse(original.exists())
        self.assertTrue(raw.exists())
        parent = self.app.batch('abc123')['items'][0]
        self.assertIn('等待另外 1 条', next(iter(parent['cleanup']['source_files'].values())))

    def test_other_unreviewed_sticker_keeps_original_until_retry(self):
        original, raw, _ = self.sticker_replacement(another_variant=True)
        approved = self.app.approve({'batch_id': 'sticker01', 'item_id': 'sticker-item',
                                     'review_dir': str(self.target_root)})
        self.assertTrue(original.exists())
        self.assertTrue(raw.exists())
        self.assertIn('其他未审核', approved['items'][0]['cleanup']['origin_error'])
        self.app.store.delete('batch:sticker02')
        retried = self.app.retry_approved_cleanup('sticker01', 'sticker-item')
        self.assertTrue(retried['items'][0]['cleanup']['origin_output_deleted'])
        self.assertFalse(original.exists())
        self.assertFalse(raw.exists())

    def test_changed_original_is_preserved_while_sticker_archive_remains_approved(self):
        original, raw, _ = self.sticker_replacement()
        original.write_bytes(b'changed-after-sticker-render')
        approved = self.app.approve({'batch_id': 'sticker01', 'item_id': 'sticker-item',
                                     'review_dir': str(self.target_root)})
        self.assertEqual('approved', approved['items'][0]['review']['status'])
        self.assertIn('素材已变化', approved['items'][0]['cleanup']['origin_error'])
        self.assertTrue(original.exists())
        self.assertTrue(raw.exists())
        self.assertNotEqual('superseded', self.app.batch('abc123')['items'][0].get('review', {}).get('status'))

    def test_independently_approved_original_keeps_its_review_archive(self):
        original, _, _ = self.sticker_replacement()
        parent = self.approve()['items'][0]
        parent_archive = Path(parent['review']['path'])
        self.assertFalse(original.exists())
        approved = self.app.approve({'batch_id': 'sticker01', 'item_id': 'sticker-item',
                                     'review_dir': str(self.target_root)})
        self.assertEqual('approved', self.app.batch('abc123')['items'][0]['review']['status'])
        self.assertTrue(parent_archive.is_file())
        self.assertEqual('原视频已单独审核，保留其审核归档',
                         approved['items'][0]['cleanup']['origin_status'])

    def test_original_cannot_be_approved_while_sticker_render_needs_its_output(self):
        original, _, _ = self.sticker_replacement()
        self.app.store.update('sticker01', lambda batch: batch['items'][0].update(status='pending'))
        with self.assertRaisesRegex(ValueError, '贴图版本仍需读取'):
            self.approve()
        self.assertTrue(original.exists())

    def test_restart_does_not_retroactively_delete_legacy_sticker_original(self):
        original, raw, replacement = self.sticker_replacement()
        archive = self.target_root / 'old-sticker.mp4'
        self.target_root.mkdir()
        archive.write_bytes(replacement.read_bytes())
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        def legacy(batch):
            batch['sticker_origin'].pop('replace_origin_on_approval')
            batch['items'][0]['review'] = {'status': 'approved', 'path': str(archive),
                                           'sha256': digest}
            batch['items'][0]['cleanup'] = {'output_deleted': True}
        self.app.store.update('sticker01', legacy)
        replacement.unlink()
        Application(self.root / 'state')
        self.assertTrue(original.exists())
        self.assertTrue(raw.exists())


if __name__ == '__main__':
    unittest.main()
