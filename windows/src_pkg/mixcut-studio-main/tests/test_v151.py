"""Stall detection and queue continuation use real subprocesses and isolated state."""
import io
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from mixcut import processwatch, renderer
from mixcut.server import Application


class WatchTests(unittest.TestCase):
    def run_program(self, program, log, **kwargs):
        # Explicit progress option avoids adding FFmpeg arguments to a Python fixture.
        return processwatch.run([sys.executable, '-c', program, '-progress', 'pipe:1'], log, **kwargs)

    def test_default_is_three_minutes(self):
        self.assertEqual(180, processwatch.STALL_SECONDS)

    def test_silent_process_is_stopped_and_evidence_retained(self):
        with tempfile.TemporaryFile(mode='w+') as log, patch.object(processwatch, 'STALL_SECONDS', .15), patch.object(processwatch, 'POLL_SECONDS', .02):
            started = time.monotonic()
            with self.assertRaises(processwatch.RenderStalled):
                self.run_program('import time; time.sleep(30)', log)
            self.assertLess(time.monotonic()-started, 3)
            log.seek(0)
            self.assertIn('render_stalled', log.read())

    def test_repeated_same_progress_does_not_reset_timer(self):
        code = 'import time\nfor i in range(200):\n print("out_time_us=1000", flush=True); time.sleep(.02)'
        with tempfile.TemporaryFile(mode='w+') as log, patch.object(processwatch, 'STALL_SECONDS', .15), patch.object(processwatch, 'POLL_SECONDS', .02):
            with self.assertRaises(processwatch.RenderStalled): self.run_program(code, log)

    def test_genuine_progress_and_file_writes_prevent_false_pause(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryFile(mode='w+') as log, patch.object(processwatch, 'STALL_SECONDS', .15), patch.object(processwatch, 'POLL_SECONDS', .02):
            for mode in ('time', 'file'):
                output = Path(directory)/mode
                code = ('import time\nfor i in range(10):\n ' +
                        ('print("out_time_us="+str(i*100), flush=True)' if mode=='time' else f'open({str(output)!r},"a").write("x")') + '; time.sleep(.05)')
                self.assertEqual(0, self.run_program(code, log, output=output))

    def test_blocked_external_disk_stat_does_not_block_stall_timer(self):
        def blocked_stat(path):
            time.sleep(.6)
            raise FileNotFoundError()
        with tempfile.TemporaryFile(mode='w+') as log, patch.object(processwatch, 'STALL_SECONDS', .15), patch.object(processwatch, 'POLL_SECONDS', .02), patch.object(Path,'stat',blocked_stat):
            started=time.monotonic()
            with self.assertRaises(processwatch.RenderStalled):
                self.run_program('import time;time.sleep(30)',log,output='unresponsive-drive-output')
            self.assertLess(time.monotonic()-started,.5)

    def test_cancel_is_observed_even_when_ffmpeg_is_silent(self):
        with tempfile.TemporaryFile(mode='w+') as log, patch.object(processwatch, 'POLL_SECONDS', .02):
            def cancel(value): raise InterruptedError('cancelled')
            with self.assertRaises(InterruptedError): self.run_program('import time; time.sleep(30)', log, callback=cancel)


class QueueTests(unittest.TestCase):
    def test_stall_skips_to_next_and_can_be_explicitly_restarted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);app=Application(root/'state');out=root/'out';out.mkdir()
            batch={'id':'abc123','created_at':1,'status':'queued','config':{'output_dir':str(out),'parallel_tasks':1},'assets':[],
                   'items':[{'id':str(i),'index':i,'status':'pending','duration':1,'attempts':0,'segments':[], 'music':[],
                             'output_path':str(out/f'{i}.mp4')} for i in range(2)]}
            app.store.put('batch:abc123',batch);calls=[]
            def render(item,config,path,work,progress):
                calls.append(item['id'])
                if item['id']=='0':
                    with tempfile.TemporaryFile(mode='w+') as log, patch.object(processwatch,'STALL_SECONDS',.15), patch.object(processwatch,'POLL_SECONDS',.02):
                        processwatch.run([sys.executable,'-c','import time;time.sleep(30)','-progress','pipe:1'],log,callback=progress)
                Path(path).write_bytes(b'output');return {'duration':1}
            with patch.object(renderer,'render',side_effect=render),patch.object(app,'enqueue_previews'),patch.object(app,'check_space'):
                app.execute_batch('abc123')
            result=app.batch('abc123')
            self.assertEqual(['0','1'],calls)
            self.assertEqual('stalled_paused',result['items'][0]['status'])
            self.assertEqual('success',result['items'][1]['status'])
            self.assertTrue(list((root/'state'/'diagnostics').glob('*.json')))
            app.recover()
            self.assertEqual('stalled_paused',app.batch('abc123')['items'][0]['status'])
            app.item_action('abc123','0','start')
            self.assertEqual('pending',app.batch('abc123')['items'][0]['status'])
            self.assertEqual(0,app.batch('abc123')['items'][0]['attempts'])


class ResolutionChangeTests(unittest.TestCase):
    def test_midstream_resolution_change_keeps_full_timeline(self):
        import shutil
        import subprocess
        from mixcut import media
        if not shutil.which('ffmpeg'): self.skipTest('FFmpeg required')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            def ff(*args):
                subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True,timeout=20)
            for i,dimensions in enumerate(('320x180','640x360')):
                ff('-f','lavfi','-i',f'color=blue:s={dimensions}:r=30:d=2',
                   '-c:v','libx264','-pix_fmt','yuv420p','-f','mpegts',str(root/f'{i}.ts'))
            joined=root/'changed.ts';joined.write_bytes((root/'0.ts').read_bytes()+(root/'1.ts').read_bytes())
            source=root/'changed.mp4';ff('-i',str(joined),'-map','0:v:0','-c','copy',str(source))
            music=root/'music.wav';ff('-f','lavfi','-i','sine=d=5:r=48000',str(music))
            song=media._asset(music,'music',quick_music=True);video=media._asset(source,'video')
            item={'duration':4,'segments':[{'asset_id':video['id'],'path':str(source),'start':0,'duration':.5},
                  {'asset_id':video['id'],'path':str(source),'start':0,'duration':3.5}],
                  'video_assets':[video],'music':[song]}
            # Same concat graph as production, with real changing SPS dimensions.
            with patch.object(processwatch,'STALL_SECONDS',5):
                result=renderer.render(item,{'width':320,'height':180,'fps':30,'hardware':'software'},
                                       root/'output.mp4',root/'work')
            self.assertAlmostEqual(4,result['duration'],places=1)
            self.assertTrue(result['valid'])
