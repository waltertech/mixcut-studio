"""Extension-first, content-verified incremental library scanning."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import http.client
import json

from mixcut import media
from mixcut.server import Application, Handler, ThreadingHTTPServer


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class IncrementalScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.videos = self.root / 'videos'; self.videos.mkdir()
        self.music = self.root / 'music'; self.music.mkdir()
        self.app = Application(self.root / 'state')

    def tearDown(self):
        self.app.closing.set()
        self.temp.cleanup()

    def make_video(self, name='recording.mp4'):
        path = self.videos / name
        color = 'red' if name == 'recording.mp4' else 'blue'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        f'color=c={color}:s=160x90:r=24:d=1', '-c:v', 'mpeg4', '-f', 'mp4', str(path)],
                       check=True, capture_output=True, timeout=30)
        return path

    def make_music(self, name='song.mp3'):
        path = self.music / name
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'sine=f=440:r=48000:d=1', '-c:a', 'libmp3lame', '-f', 'mp3', str(path)],
                       check=True, capture_output=True, timeout=30)
        return path

    def test_extension_prefilter_then_stream_validation(self):
        video = self.make_video()
        song = self.make_music()
        (self.music / 'fake.mp3').write_text('ordinary text, not audio')
        (self.music / 'document.dat').write_bytes(b'%PDF-1.7\n' + b'0' * 20)
        (self.videos / 'metadata.txt').write_text('ordinary text')
        (self.music / '._metadata.mp3').write_bytes(b'\x00\x05\x16\x07' + b'0' * 20)
        result = media.scan(str(self.videos), str(self.music), str(self.root / 'cache'))
        self.assertEqual([video.name], [entry['name'] for entry in result['videos']])
        self.assertEqual([song.name], [entry['name'] for entry in result['music']])
        self.assertTrue(any('fake.mp3' in error['path'] for error in result['errors']))
        self.assertFalse(any('document.dat' in error['path'] for error in result['errors']))
        self.assertFalse(any('._metadata.mp3' in error['path'] for error in result['errors']))
        self.assertEqual(1, result['summary']['video']['skipped_extension'])
        self.assertEqual(1, result['summary']['music']['skipped_extension'])
        self.assertEqual(1, result['summary']['music']['skipped_content'])

    def test_nonmedia_files_do_not_start_probe_processes(self):
        self.make_video()
        self.make_music()
        for index in range(250):
            (self.music / f'notes-{index}.txt').write_text('not audio')
            (self.videos / f'notes-{index}.txt').write_text('not video')
        (self.videos / '._ghost.ts').write_bytes(b'\x00\x05\x16\x07metadata')
        (self.music / '._ghost.flac').write_bytes(b'\x00\x05\x16\x07metadata')
        with patch('mixcut.media._run_probe', wraps=media._run_probe) as probe:
            result = media.scan(str(self.videos), str(self.music), str(self.root / 'cache'), quick_music=True)
        self.assertEqual(2, probe.call_count)
        self.assertEqual(250, result['summary']['video']['skipped_extension'])
        self.assertEqual(250, result['summary']['music']['skipped_extension'])
        self.assertEqual(1, result['summary']['video']['skipped_content'])
        self.assertEqual(1, result['summary']['music']['skipped_content'])

    def test_ts_video_and_flac_music_are_supported(self):
        video = self.videos / 'capture.ts'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'color=c=red:s=160x90:r=24:d=1', '-c:v', 'mpeg2video', '-f', 'mpegts', str(video)],
                       check=True, capture_output=True, timeout=30)
        song = self.music / 'song.flac'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'sine=f=440:r=48000:d=1', '-c:a', 'flac', str(song)],
                       check=True, capture_output=True, timeout=30)
        result = media.scan(str(self.videos), str(self.music), str(self.root / 'cache'), quick_music=True)
        self.assertEqual([video.name], [entry['name'] for entry in result['videos']])
        self.assertEqual([song.name], [entry['name'] for entry in result['music']])

    def test_undated_image_like_stream_does_not_abort_full_scan(self):
        video = self.make_video()
        self.make_music()
        image = self.videos / 'still.mp4'
        image.write_bytes(b'not recognized by the quick content check')
        original_probe = media._run_probe
        def probe(path):
            if path == image:
                return {'streams': [{'codec_type': 'video'}], 'format': {}}
            return original_probe(path)
        with patch('mixcut.media._run_probe', side_effect=probe):
            result = media.scan(str(self.videos), str(self.music), str(self.root / 'cache'))
        self.assertEqual([video.name], [entry['name'] for entry in result['videos']])
        self.assertTrue(any('still.mp4' in error['path'] for error in result['errors']))

    def test_same_volume_rename_reuses_metadata_but_gets_new_path_id(self):
        self.make_video()
        old = self.make_music()
        first = media.scan(str(self.videos), str(self.music), str(self.root / 'cache'))
        renamed = old.rename(self.music / 'renamed.m4a')
        with patch('mixcut.media._asset', side_effect=AssertionError('unnecessary reanalysis')):
            second = media.scan(str(self.videos), str(self.music), str(self.root / 'cache'))
        self.assertNotEqual(first['music'][0]['id'], second['music'][0]['id'])
        self.assertEqual(str(renamed.resolve()), second['music'][0]['path'])

    def test_shared_path_has_separate_video_and_music_cache_records(self):
        shared = self.videos / 'both.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'color=c=blue:s=160x90:r=24:d=1', '-f', 'lavfi', '-i',
                        'sine=f=330:r=48000:d=1', '-shortest', '-c:v', 'mpeg4',
                        '-c:a', 'aac', '-f', 'mp4', str(shared)],
                       check=True, capture_output=True, timeout=30)
        result = media.scan(str(self.videos), str(self.videos), str(self.root / 'cache'))
        self.assertEqual(1, len(result['videos']))
        self.assertEqual(1, len(result['music']))
        self.assertEqual('video/mp4', result['videos'][0]['mime_type'])
        self.assertEqual('audio/mp4', result['music'][0]['mime_type'])

    def test_audio_preview_uses_detected_type_when_extension_differs(self):
        self.make_video()
        self.make_music('song.m4a')
        song = self.app.scan({'video_dir': str(self.videos), 'music_dir': str(self.music)})['music'][0]
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        httpd.app = self.app
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            client = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
            client.request('GET', '/api/media?id=' + song['id'])
            response = client.getresponse()
            response.read()
            self.assertEqual(200, response.status)
            self.assertEqual('audio/mpeg', response.getheader('Content-Type'))
            client.close()
        finally:
            httpd.shutdown()
            thread.join(timeout=3)
            httpd.server_close()

    def test_scan_http_start_and_status_return_progress(self):
        self.make_video()
        self.make_music()
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        httpd.app = self.app
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            client = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
            payload = json.dumps({'video_dir': str(self.videos), 'music_dir': str(self.music)})
            client.request('POST', '/api/scan/start', payload, {'Content-Type': 'application/json'})
            response = client.getresponse()
            started = json.loads(response.read())
            self.assertEqual(200, response.status)
            self.assertIn(started['status'], {'discovering', 'analyzing', 'completed'})
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                client.request('GET', '/api/scan/status?compact=1')
                response = client.getresponse()
                status = json.loads(response.read())
                self.assertEqual(200, response.status)
                if status['status'] not in {'discovering', 'analyzing'}:
                    break
                self.assertNotIn('scan', status)
                time.sleep(.02)
            self.assertEqual('completed', status['status'], status.get('error'))
            self.assertEqual({'video': 1, 'music': 1}, status['processed'])
            client.close()
        finally:
            httpd.shutdown()
            thread.join(timeout=3)
            httpd.server_close()

    def test_compact_status_omits_partial_library_until_completion(self):
        self.make_video()
        self.make_music()
        released = threading.Event()
        original = media.scan
        def delayed_scan(*args, **kwargs):
            kwargs['progress']({'phase': 'inventory', 'totals': {'video': 1, 'music': 1}})
            released.wait(3)
            return original(*args, **kwargs)
        with patch('mixcut.media.scan', side_effect=delayed_scan):
            self.app.start_scan({'video_dir': str(self.videos), 'music_dir': str(self.music)})
            deadline = time.monotonic() + 3
            while self.app.scan_status()['status'] == 'discovering' and time.monotonic() < deadline:
                time.sleep(.01)
            compact = self.app.scan_status(compact=True)
            self.assertEqual('analyzing', compact['status'])
            self.assertNotIn('scan', compact)
            released.set()
            deadline = time.monotonic() + 10
            while self.app.scan_status()['status'] in {'discovering', 'analyzing'} and time.monotonic() < deadline:
                time.sleep(.02)
        finished = self.app.scan_status(compact=True)
        self.assertEqual('completed', finished['status'])
        self.assertEqual(1, len(finished['scan']['music']))

    def test_background_scan_updates_one_library_and_keeps_other(self):
        self.make_video()
        self.make_music()
        body = {'video_dir': str(self.videos), 'music_dir': str(self.music)}
        self.app.scan(body)
        original_music = self.app.bootstrap()['scan']['music']
        self.make_video('another.mp4')
        started = self.app.start_scan({**body, 'kind': 'video'})
        self.assertEqual(original_music, started['scan']['music'])
        deadline = time.monotonic() + 10
        while self.app.scan_status()['status'] in {'discovering', 'analyzing'} and time.monotonic() < deadline:
            time.sleep(.02)
        finished = self.app.scan_status()
        self.assertEqual('completed', finished['status'], finished.get('error'))
        self.assertEqual(2, len(finished['scan']['videos']))
        self.assertEqual(original_music, finished['scan']['music'])
        self.assertEqual(2, len(self.app.bootstrap()['scan']['videos']))

    def test_background_scan_failure_keeps_last_complete_library(self):
        self.make_video()
        self.make_music()
        body = {'video_dir': str(self.videos), 'music_dir': str(self.music)}
        original = self.app.scan(body)
        with patch('mixcut.media.scan', side_effect=RuntimeError('temporary scanner failure')):
            self.app.start_scan({**body, 'kind': 'music'})
            deadline = time.monotonic() + 10
            while self.app.scan_status()['status'] in {'discovering', 'analyzing'} and time.monotonic() < deadline:
                time.sleep(.02)
        self.assertEqual('failed', self.app.scan_status()['status'])
        self.assertEqual(original, self.app.bootstrap()['scan'])

    def test_legacy_cache_gets_content_type_without_rehashing(self):
        self.make_video()
        self.make_music()
        first = media.scan(str(self.videos), str(self.music), str(self.root / 'cache'))
        cache_file = self.root / 'cache' / 'media-index.json'
        cache = json.loads(cache_file.read_text())
        for record in cache.values():
            record['asset'].pop('mime_type', None)
        cache_file.write_text(json.dumps(cache))
        with patch('mixcut.media._sha256', side_effect=AssertionError('unnecessary rehash')):
            second = media.scan(str(self.videos), str(self.music), str(self.root / 'cache'))
        self.assertEqual(first['music'][0]['id'], second['music'][0]['id'])
        self.assertEqual('audio/mpeg', second['music'][0]['mime_type'])

    def test_library_load_defers_full_music_decode_until_planning(self):
        self.make_video()
        self.make_music()
        body = {'video_dir': str(self.videos), 'music_dir': str(self.music)}
        with patch('mixcut.media._decoded_audio_duration', side_effect=AssertionError('early decode')):
            self.app.start_scan(body)
            deadline = time.monotonic() + 10
            while self.app.scan_status()['status'] in {'discovering', 'analyzing'} and time.monotonic() < deadline:
                time.sleep(.02)
        finished = self.app.scan_status()
        self.assertEqual('completed', finished['status'], finished.get('error'))
        self.assertIs(False, finished['scan']['music'][0]['duration_precise'])
        video = finished['scan']['videos'][0]
        song = finished['scan']['music'][0]
        batch = self.app.create_plan({'config': {
            'mode': 'single', 'music_mode': 'pool', 'count': 1,
            'min_songs': 1, 'max_songs': 1, 'min_duration': .5, 'max_duration': .5,
            'allow_overlap': False, 'video_ids': [video['id']], 'music_ids': [song['id']],
            'width': 640, 'height': 360, 'fps': 24, 'output_dir': str(self.root / 'exports')}})
        self.assertEqual(1, len(batch['items']))
        self.assertIs(True, self.app.bootstrap()['scan']['music'][0]['duration_precise'])


if __name__ == '__main__':
    unittest.main()
