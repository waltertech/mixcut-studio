import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mixcut.keyframes import KeyframeCache
from mixcut.server import Application


class KeyframeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.video = cls.root / 'test.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi',
                        '-i', 'testsrc2=size=640x360:rate=10:duration=3',
                        '-c:v', 'libx264', '-g', '10', '-keyint_min', '10',
                        '-sc_threshold', '0', '-bf', '0', '-pix_fmt', 'yuv420p', str(cls.video)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.cache = KeyframeCache(self.root / self._testMethodName)

    def test_all_encoded_keyframes_are_reported_in_order(self):
        data = self.cache.describe(self.video)
        self.assertEqual(3, data['total'])
        self.assertEqual([0, 1, 2], [frame['time'] for frame in data['frames']])
        self.assertEqual([0, 1, 2], [frame['index'] for frame in data['frames']])

    def test_metadata_and_images_reuse_cache(self):
        expected = self.cache.describe(self.video)
        thumbnail = self.cache.image(self.video, 1)
        with patch('mixcut.keyframes.subprocess.run', side_effect=AssertionError('cache should avoid FFmpeg')):
            self.assertEqual(expected, self.cache.describe(self.video))
            self.assertEqual(thumbnail, self.cache.image(self.video, 1))
        self.assertEqual(b'\xff\xd8', thumbnail.read_bytes()[:2])

    def test_thumbnail_and_large_have_distinct_dimensions(self):
        for size, width in [('thumb', 320), ('large', 640)]:
            path = self.cache.image(self.video, 2, size)
            result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                     '-show_entries', 'stream=width', '-of', 'csv=p=0', str(path)],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(width, int(result.stdout.strip()))

    def test_invalid_indices_sizes_and_stale_version_are_rejected(self):
        for index, size, version in [(-1, 'thumb', None), (3, 'thumb', None),
                                     (0, 'huge', None), (0, 'thumb', 'old-version')]:
            with self.assertRaises(ValueError):
                self.cache.image(self.video, index, size, version)

    def test_changed_file_gets_a_new_cache_version(self):
        old = self.cache.describe(self.video)
        stat = self.video.stat()
        os.utime(self.video, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        new = self.cache.describe(self.video)
        self.assertNotEqual(old['version'], new['version'])

    def test_application_only_serves_completed_registered_outputs(self):
        app = Application(self.root / 'app-state')
        record = {'id': 'test', 'created_at': 1, 'status': 'draft',
                  'items': [{'id': '1', 'status': 'pending', 'output_path': str(self.video)}]}
        app.store.put('batch:test', record)
        with self.assertRaises(ValueError):
            app.completed_output('test', '1')
        record['items'][0]['status'] = 'success'
        app.store.put('batch:test', record)
        self.assertEqual(self.video, app.completed_output('test', '1'))
        with self.assertRaises(ValueError):
            app.completed_output('test', '../../file')


if __name__ == '__main__':
    unittest.main()
