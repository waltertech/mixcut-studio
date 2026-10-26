"""Moved completed outputs and sidecar failures must not block the render queue."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mixcut.server import Application


class SidecarIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = Application(self.root / 'state')
        self.addCleanup(self.app.closing.set)

    def item(self, number, status='success'):
        video = self.root / 'output' / str(number) / f'{number}.mp4'
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b'fixture')
        return {'id': str(number), 'index': number, 'status': status,
                'output_path': str(video), 'music': [{'path': '/music/set/song.mp3'}]}

    def save(self, items, status='completed'):
        batch = {'id': 'test', 'status': status, 'created_at': 0,
                 'config': {'parallel_tasks': 2}, 'items': items}
        self.app.store.put('batch:test', batch)

    def test_moved_completed_folder_does_not_block_later_parallel_tasks(self):
        old = self.item(1)
        old['review'] = {'status': 'approved', 'retained': True, 'path': old['output_path']}
        moved = self.root / 'moved'
        Path(old['output_path']).parent.rename(moved)
        self.save([old] + [self.item(n, 'pending') for n in range(2, 7)], 'queued')
        ran = []

        def finish(batch_id, index):
            ran.append(index)
            self.app.store.update(batch_id, lambda b: b['items'][index].update(status='success'))

        with patch.object(self.app, 'execute_item', side_effect=finish):
            self.app.execute_batch('test')
        self.assertEqual(set(range(1, 6)), set(ran))
        self.assertEqual('completed', self.app.batch('test')['status'])
        self.assertFalse(Path(old['output_path']).parent.exists())
        self.assertTrue((moved / '1.mp4').is_file())
        self.assertFalse((moved / '1.txt').exists())
        manifest = self.app.store.directory / 'manifests/test/manifest.json'
        self.assertEqual('completed', json.loads(manifest.read_text())['status'])

    def test_missing_video_in_existing_folder_does_not_recreate_sidecar(self):
        old, healthy = self.item(1), self.item(2)
        Path(old['output_path']).unlink()
        self.save([old, healthy])
        self.app.write_manifest('test')
        self.assertFalse(Path(old['output_path']).with_suffix('.txt').exists())
        self.assertEqual('/music/set', Path(healthy['output_path']).with_suffix('.txt').read_text().strip())

    def test_sidecar_write_race_or_permission_failure_keeps_other_documents(self):
        for error in [FileNotFoundError('folder moved during write'), PermissionError('read-only folder')]:
            with self.subTest(error=type(error).__name__):
                old, healthy = self.item(1), self.item(2)
                self.save([old, healthy])
                original = self.app.write_music_sidecar

                def write(item, target, **kwargs):
                    if item['id'] == '1':
                        raise error
                    return original(item, target, **kwargs)

                with patch.object(self.app, 'write_music_sidecar', side_effect=write):
                    self.app.write_manifest('test')
                self.assertTrue(Path(healthy['output_path']).with_suffix('.txt').is_file())
                self.assertEqual('success', self.app.batch('test')['items'][0]['status'])

    def test_approved_relocated_output_uses_review_path(self):
        old = self.item(1)
        moved = self.root / 'approved'
        Path(old['output_path']).parent.rename(moved)
        old['review'] = {'status': 'approved', 'path': str(moved / '1.mp4')}
        self.save([old])
        self.app.write_manifest('test')
        self.assertTrue((moved / '1.txt').is_file())
        self.assertFalse(Path(old['output_path']).parent.exists())
