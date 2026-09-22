import concurrent.futures
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mixcut.server import Application


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
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
                          'result': {'output_size': stat.st_size, 'output_mtime_ns': stat.st_mtime_ns}})
        self.record = {'id': 'abc123', 'created_at': 1, 'updated_at': 1, 'status': 'completed',
                       'config': {'output_dir': str(self.root / 'exports')},
                       'output_folder': str(self.export), 'items': items}
        self.app.store.put('batch:abc123', self.record)

    def tearDown(self):
        self.temp.cleanup()

    def approve(self, item='1'):
        return self.app.approve({'batch_id': 'abc123', 'item_id': item, 'review_dir': str(self.target_root)})

    def test_copy_preserves_original_and_groups_approved_items(self):
        first = self.approve()['items'][0]['review']
        second = self.approve('2')['items'][1]['review']
        self.assertEqual('approved', first['status'])
        self.assertRegex(Path(first['path']).parent.name, r'^\d{4}-\d{2}-\d{2}_\d{3}$')
        self.assertEqual(Path(first['path']).parent, Path(second['path']).parent)
        self.assertEqual((self.export / '001.mp4').read_bytes(), Path(first['path']).read_bytes())
        self.assertTrue((self.export / '001.mp4').is_file())
        self.assertEqual(first, Application(self.root / 'state').batch('abc123')['items'][0]['review'])

    def test_double_click_is_idempotent_even_concurrently(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.approve(), range(2)))
        self.assertEqual(results[0]['items'][0]['review'], results[1]['items'][0]['review'])
        self.assertEqual(1, len(list(self.target_root.rglob('*.mp4'))))

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
        self.approve('2')
        folder = Path(self.app.batch('abc123')['items'][1]['review']['path']).parent
        target = folder / '001.mp4'
        target.write_bytes(b'keep-existing')
        with self.assertRaisesRegex(ValueError, '不会覆盖'):
            self.approve()
        self.assertEqual(b'keep-existing', target.read_bytes())

    def test_restart_recovers_copy_finished_before_state_commit(self):
        approved = self.approve()['items'][0]['review']
        before = Path(approved['path']).stat().st_mtime_ns
        self.app.store.update('abc123', lambda b: b['items'][0]['review'].update(status='copying'))
        self.app = Application(self.root / 'state')
        self.assertEqual('completed', self.app.batch('abc123')['status'])
        self.assertEqual('failed', self.app.batch('abc123')['items'][0]['review']['status'])
        self.assertEqual('approved', self.approve()['items'][0]['review']['status'])
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


if __name__ == '__main__':
    unittest.main()
