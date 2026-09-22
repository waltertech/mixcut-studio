"""Small real-FFmpeg checks for rendering and decoded-duration media indexing."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mixcut import media, renderer


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg and ffprobe are required')
class RenderIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.videos = self.root / 'videos'
        self.music = self.root / 'music'
        self.videos.mkdir()
        self.music.mkdir()
        self.make_media()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def ffmpeg(*arguments):
        subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', *arguments], check=True,
                       capture_output=True, text=True, timeout=30)

    def make_media(self):
        # One source has no audio; the second has a tone.  Both support non-zero seeking.
        self.ffmpeg('-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=30:d=2',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(self.videos / 'silent.mp4'))
        self.ffmpeg('-f', 'lavfi', '-i', 'color=c=red:s=320x180:r=30:d=2',
                    '-f', 'lavfi', '-i', 'sine=f=330:r=48000:d=2', '-shortest',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                    str(self.videos / 'with-audio.mp4'))
        for index, frequency in enumerate((440, 660), 1):
            self.ffmpeg('-f', 'lavfi', '-i', f'sine=f={frequency}:r=48000:d=1',
                        '-c:a', 'libmp3lame', '-b:a', '128k', str(self.music / f'tone{index}.mp3'))

    def scanned_assets(self):
        result = media.scan(str(self.videos), str(self.music), str(self.root / 'cache'))
        self.assertEqual([], result['errors'])
        self.assertEqual(2, len(result['videos']))
        self.assertEqual(2, len(result['music']))
        return result

    def plan(self):
        assets = self.scanned_assets()
        videos = {entry['name']: entry for entry in assets['videos']}
        songs = assets['music']
        expected = sum(song['duration'] for song in songs)
        self.assertAlmostEqual(2.0, expected, places=5)
        return {
            'duration': expected,
            'segments': [
                {'asset_id': videos['silent.mp4']['id'], 'path': videos['silent.mp4']['path'],
                 'start': 0.25, 'duration': 1.0},
                {'asset_id': videos['with-audio.mp4']['id'], 'path': videos['with-audio.mp4']['path'],
                 'start': 0.50, 'duration': 1.0},
            ],
            'video_assets': list(videos.values()),
            'music': songs,
        }, expected

    def test_render_mixes_audio_sources_keeps_complete_songs_and_reports_progress(self):
        item, expected_duration = self.plan()
        output = self.root / 'exports' / 'mixed.mp4'
        progress = []

        result = renderer.render(
            item,
            {'width': 320, 'height': 180, 'fps': 30, 'hardware': 'software',
             'music_volume': 1, 'original_volume': 0.2},
            str(output), str(self.root / 'work'), progress.append)

        self.assertTrue(output.is_file())
        self.assertEqual('libx264', result['encoder'])
        self.assertAlmostEqual(expected_duration, result['audio_duration'], places=3)
        self.assertAlmostEqual(expected_duration, result['video_duration'], places=2)
        self.assertGreaterEqual(len(progress), 2)
        self.assertIn('validating', [event['stage'] for event in progress])
        self.assertEqual({'stage': 'complete', 'progress': 1}, progress[-1])
        self.assertTrue(renderer.validate(output, expected_duration)['valid'])

    def test_render_refuses_to_overwrite_an_existing_file(self):
        item, _ = self.plan()
        output = self.root / 'exports' / 'sentinel.mp4'
        output.parent.mkdir(parents=True)
        output.write_bytes(b'do-not-overwrite')

        with self.assertRaisesRegex(ValueError, '不会覆盖'):
            renderer.render(item, {'width': 320, 'height': 180, 'fps': 30, 'hardware': 'software'},
                            str(output), str(self.root / 'work'))

        self.assertEqual(b'do-not-overwrite', output.read_bytes())

    def test_music_scan_uses_decoded_duration_and_reuses_the_unchanged_cache(self):
        first = self.scanned_assets()
        first_durations = [entry['duration'] for entry in first['music']]
        self.assertTrue(all(abs(duration - 1.0) < 1 / 48000 for duration in first_durations))

        with patch('mixcut.media._decoded_audio_duration', side_effect=AssertionError('cache miss')):
            second = media.scan(str(self.videos), str(self.music), str(self.root / 'cache'))

        self.assertEqual(first_durations, [entry['duration'] for entry in second['music']])
        self.assertEqual([], second['errors'])


if __name__ == '__main__':
    unittest.main()
