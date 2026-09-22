"""Real short-media tests; time is simulated, no production schedules are installed."""
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
import unittest

from mixcut.server import Application
from mixcut.scheduler import Scheduler, ScheduledRunCancelled
from mixcut import renderer


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
class ScheduledMediaIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.videos = self.root / 'videos'; self.videos.mkdir()
        self.music = self.root / 'Hindi POP Songs'; self.music.mkdir()
        self.base = time.time() + 20
        self.make_video('a', 'blue')
        for number in range(4):
            self.ffmpeg('-f', 'lavfi', '-i', f'sine=f={300 + number * 100}:r=48000:d=1',
                        '-c:a', 'libmp3lame', str(self.music / f'{number}.mp3'))
        self.app = Application(self.root / 'state')
        self.app.scheduler = Scheduler(self.app, background_checks=False)
        self.scheduler = self.app.scheduler

    def tearDown(self):
        self.app.closing.set()
        self.temp.cleanup()

    @staticmethod
    def ffmpeg(*args):
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', *args], check=True, capture_output=True, timeout=30)

    def make_video(self, name, color):
        self.ffmpeg('-f', 'lavfi', '-i', f'color=c={color}:s=640x360:r=30:d=2',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(self.videos / (name + '.mp4')))

    def schedule(self, name, target, start):
        result = self.scheduler.save({'name': name, 'start_at': start, 'repeat': 'once',
            'target_count': target, 'check_interval_minutes': 1, 'enabled': True,
            'video_dir': str(self.videos), 'music_dir': str(self.music), 'output_dir': str(self.root / 'out'),
            'config': {'mode': 'single', 'music_mode': 'pool', 'min_songs': 1, 'max_songs': 1,
                       'allow_overlap': False, 'min_duration': 1, 'max_duration': 1,
                       'width': 640, 'height': 360, 'fps': 30, 'hardware': 'software'}}, now=self.base)
        return next(s for s in result['schedules'] if s['name'] == name)

    def render_queued(self):
        for batch in self.app.store.batches():
            if batch['status'] == 'queued':
                self.app.execute_batch(batch['id'])

    def test_real_partial_new_video_completion_then_next_target_replaces_old(self):
        self.schedule('fill-four', 4, self.base + 10)
        self.scheduler.tick(self.base + 10)
        self.render_queued()
        self.scheduler.tick(self.base + 11)
        run = self.scheduler.active()
        self.assertEqual(('waiting', 2), (run['status'], run['completed_count']))
        original_count = len(self.app.store.batches())
        self.scheduler.tick(self.base + 71)
        self.assertEqual(original_count, len(self.app.store.batches()))
        self.make_video('b', 'red')
        self.scheduler.tick(self.base + 132)
        self.render_queued()
        self.scheduler.tick(self.base + 133)
        self.assertEqual(('completed', 4), (self.scheduler.active()['status'], self.scheduler.active()['completed_count']))
        output_files = [Path(i['output_path']) for b in self.app.store.batches() for i in b['items'] if i['status'] == 'success']
        self.assertEqual(4, len(output_files))
        hashes = {str(p): self.app.file_digest(p) for p in output_files}
        self.schedule('unfinished-50', 50, self.base + 200)
        self.schedule('next-one', 1, self.base + 300)
        self.scheduler.tick(self.base + 200)
        old_run_id = self.scheduler.active()['id']
        self.render_queued()
        self.scheduler.tick(self.base + 201)
        self.assertEqual(4, self.scheduler.active()['completed_count'])
        self.scheduler.tick(self.base + 300)
        self.assertEqual('superseded', self.app.store.get('scheduled-run:' + old_run_id)['status'])
        self.assertEqual(1, self.scheduler.active()['target_count'])
        self.render_queued()
        self.scheduler.tick(self.base + 301)
        self.assertEqual('completed', self.scheduler.active()['status'])
        for path, digest in hashes.items():
            self.assertEqual(digest, self.app.file_digest(path))

    def test_ffmpeg_cancellation_removes_partial_export(self):
        self.schedule('cancel', 2, self.base + 10)
        self.scheduler.tick(self.base + 10)
        batch = self.app.store.batches()[0]
        item = batch['items'][0]
        def cancel(progress):
            if progress.get('stage') == 'rendering':
                raise ScheduledRunCancelled('test supersession')
        with self.assertRaises(ScheduledRunCancelled):
            renderer.render(item, batch['config'], item['output_path'], str(self.root / 'work'), cancel)
        self.assertFalse(Path(item['output_path']).exists())
        self.assertEqual([], list(Path(item['output_path']).parent.glob('*.partial.mp4')))


if __name__ == '__main__':
    unittest.main()
