from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from mixcut import media


class MediaProbeOutput(unittest.TestCase):
    def test_binary_json_output_is_decoded(self):
        completed = subprocess.CompletedProcess([], 0, b'{"streams": [], "format": {}}', b'')
        with patch('mixcut.media.subprocess.run', return_value=completed):
            self.assertEqual([], media._run_probe(Path('sample.mp4'))['streams'])

    def test_missing_output_becomes_a_media_error(self):
        completed = subprocess.CompletedProcess([], 0, None, None)
        with patch('mixcut.media.subprocess.run', return_value=completed):
            with self.assertRaisesRegex(ValueError, '未返回媒体信息'):
                media._run_probe(Path('sample.mp4'))

    def test_invalid_json_becomes_a_media_error(self):
        completed = subprocess.CompletedProcess([], 0, b'not-json', b'')
        with patch('mixcut.media.subprocess.run', return_value=completed):
            with self.assertRaisesRegex(ValueError, '无效的媒体信息'):
                media._run_probe(Path('sample.mp4'))


if __name__ == '__main__':
    unittest.main()
