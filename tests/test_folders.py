import concurrent.futures
from datetime import datetime
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mixcut.folders import batch_output_folder, choose_folder
from mixcut.server import Application


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.app = Application(self.root / 'state')

    def tearDown(self):
        self.temp.cleanup()

    def test_numbered_directories_do_not_overwrite_and_survive_restart(self):
        output = self.root / 'output'
        output.mkdir()
        today = datetime.now().strftime('%Y-%m-%d')
        existing = output / f'{today}_001'
        existing.mkdir()
        (existing / 'keep.txt').write_text('keep')
        first = self.app.reserve_output_folder(output)
        second = Application(self.root / 'state').reserve_output_folder(output)
        self.assertEqual(f'{today}_002', first.name)
        self.assertEqual(f'{today}_003', second.name)
        self.assertEqual('keep', (existing / 'keep.txt').read_text())

    def test_concurrent_reservations_have_unique_names(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            folders = list(pool.map(lambda _: self.app.reserve_output_folder(self.root / 'out'), range(8)))
        self.assertEqual(8, len(set(folders)))

    def test_cancel_keeps_saved_output_and_releases_picker(self):
        self.app.preferences({'output_dir': str(self.root)})
        with patch('mixcut.server.choose_folder', return_value=None):
            self.assertEqual({'path': None, 'cancelled': True}, self.app.pick_folder({'kind': 'output'}))
        self.assertEqual(str(self.root), self.app.bootstrap()['output_dir'])
        self.assertFalse(self.app.picker_lock.locked())

    def test_selected_output_is_remembered_after_restart(self):
        with patch('mixcut.server.choose_folder', return_value=str(self.root)):
            result = self.app.pick_folder({'kind': 'output'})
        self.assertFalse(result['cancelled'])
        self.assertEqual(str(self.root), Application(self.root / 'state').bootstrap()['output_dir'])

    def test_native_script_uses_separate_arguments_and_handles_cancel(self):
        with patch('mixcut.folders.sys.platform', 'darwin'), patch('mixcut.folders.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, '', '')
            self.assertIsNone(choose_folder('选择文件夹', str(self.root)))
            args = run.call_args.args[0]
            self.assertEqual(str(self.root), args[-1])
            self.assertNotIn(str(self.root), args[2])

    def test_plan_output_manifest_and_legacy_paths(self):
        asset = {'id': 'video', 'path': '/video', 'duration': 100}
        song = {'id': 'music', 'path': '/music', 'duration': 10}
        self.app.store.put('library', {'scan': {'videos': [asset], 'music': [song]}})
        result = self.app.create_plan({'config': {'count': 1, 'output_dir': str(self.root / 'custom'),
                                                'music_mode': 'fixed'}})
        directory = Path(result['output_folder'])
        self.assertRegex(directory.name, r'^\d{4}-\d{2}-\d{2}_001$')
        self.assertEqual(directory / '001.mp4', Path(result['items'][0]['output_path']))
        self.app.write_manifest(result['id'])
        self.assertTrue((directory / 'manifest.json').is_file())
        self.assertEqual(directory, batch_output_folder(result))
        legacy = {'id': 'oldbatch', 'config': {'output_dir': str(self.root)}}
        self.assertEqual(self.root / 'oldbatch', batch_output_folder(legacy))


if __name__ == '__main__':
    unittest.main()
