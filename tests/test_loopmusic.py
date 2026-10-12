"""Contract, isolation, recovery, API and real media acceptance for music-led exports."""
import copy
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from mixcut.server import Application, Handler, ThreadingHTTPServer
from mixcut.loopmusic import read_handoff, file_stamp
from mixcut.processwatch import RenderStalled


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Application(self.root / 'state')
        self.service = self.app.loop_music
        self.source = self.root / '歌曲 Hindi टेस्ट'
        self.source.mkdir()
        self.songs = [self.source / f'{n:02d} Song.mp3' for n in (1,2,3)]
        for n, path in enumerate(self.songs): path.write_bytes(bytes([n+1])*100)
        self.setdir = self.root / 'Set'
        self.setdir.mkdir()
        self.motion = self.setdir / '动态封面_10秒.mp4'; self.motion.write_bytes(b'movie')
        self.cover = self.setdir / '新封面.png'; self.cover.write_bytes(b'cover')
        self.handoff = self.setdir / 'YPP交接.txt'
        self.write_handoff()

    def tearDown(self):
        self.app.closing.set()
        self.temp.cleanup()

    def write_handoff(self, order=(1,2,3), duration=3):
        blocks=[]
        for n, original in enumerate(order, 1):
            song=self.songs[original-1]
            blocks.append(f'[曲目 {n:02d}]\n名称：曲目 {original} हिन्दी\n音频路径：{song}\n原编号：{original}\n起始秒：{(n-1)*duration}\n时长秒：{duration}\nSHA256：{hashlib.sha256(song.read_bytes()).hexdigest()}')
        self.handoff.write_text(f'YPP音乐交接 TXT v1\n状态：已完成\nSet ID：set-test\n音乐来源原路径：{self.source}\n静态封面路径：{self.cover}\n动态封面路径：{self.motion}\n曲目总数：{len(order)}\n===== 播放顺序开始 =====\n'+ '\n\n'.join(blocks)+'\n===== 播放顺序结束 =====\n===== 视频文案开始 =====\nEnglish title: Test\nHindi: संगीत\n===== 视频文案结束 =====\n',encoding='utf-8')

    def imported(self, **config):
        result=self.service.import_tasks({'paths':[str(self.handoff)],'output_dir':str(self.root/'成品'),'config':config})
        self.assertEqual([], result['errors'])
        return result['imported'][0]

    def test_import_does_not_probe_decode_or_hash_audio(self):
        with patch('subprocess.run',side_effect=AssertionError('Import cannot decode')),patch.object(self.service,'verify_hash',side_effect=AssertionError('Import cannot hash')):
            identity=self.imported()
        task=self.service.get(identity)
        self.assertEqual([1,2,3],[s['original_number'] for s in task['music']])
        self.assertIn('संगीत',task['copy_text'])
        self.assertEqual([],self.app.store.batches())
        self.assertEqual([],self.app.visible_batches())
        self.assertEqual({},self.app.store.get('library',{}))

    def test_invalid_order_count_and_duplicate_section(self):
        original=self.handoff.read_text()
        for bad in [original.replace('[曲目 02]','[曲目 04]'),original.replace('曲目总数：3','曲目总数：4'),original+'\n===== 播放顺序开始 =====\n',original.replace('原编号：1','原编号：3')]:
            self.handoff.write_text(bad)
            with self.assertRaises(ValueError):read_handoff(self.handoff)
        self.handoff.write_text(original)

    def test_pending_set_is_rejected(self):
        self.handoff.write_text(self.handoff.read_text().replace('状态：已完成','状态：制作中'))
        with self.assertRaisesRegex(ValueError,'尚未'):read_handoff(self.handoff)

    def test_hash_verification_is_cached_and_changed_files_fail(self):
        identity=self.imported();song=self.service.get(identity)['music'][0]
        self.service.verify_hash(song,lambda:False)
        with patch('builtins.open',side_effect=AssertionError('Repeated hash read')):
            self.service.verify_hash(song,lambda:False)
        self.songs[0].write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'歌曲已变化'):self.service.verify_hash(song,lambda:False)

    def test_pause_restart_and_isolated_recovery(self):
        identity=self.imported();self.service.action(identity,'start')
        self.assertTrue(self.app.bootstrap()['active_work'])
        self.service.action(identity,'pause')
        self.assertEqual('paused',self.service.get(identity)['status'])
        self.service.action(identity,'start')
        recovered=Application(self.root/'state')
        self.assertEqual('paused',recovered.loop_music.get(identity)['status'])
        self.assertEqual([],recovered.store.batches());recovered.closing.set()

    def test_relocating_library_preserves_order_and_hashes(self):
        identity=self.imported();new=self.root/'new';shutil.move(self.source,new)
        task=self.service.relocate(identity,str(new))
        self.assertTrue(all(Path(s['path']).parent==new.resolve() for s in task['music']))
        for song in task['music']:self.service.verify_hash(song,lambda:False)

    def test_delete_record_preserves_every_source_and_output(self):
        identity=self.imported();out=self.root/'kept.mp4';out.write_bytes(b'keep')
        self.service.update(identity,output_path=str(out))
        self.service.action(identity,'delete')
        self.assertEqual([],self.service.listing());self.assertTrue(out.is_file())
        self.assertTrue(all(p.is_file() for p in self.songs+[self.cover,self.motion,self.handoff]))

    def test_bad_song_fails_without_substitution_and_next_task_can_run(self):
        identity=self.imported();self.songs[0].write_bytes(b'broken')
        self.service.action(identity,'start');self.service.run_next()
        task=self.service.get(identity)
        self.assertEqual('failed',task['status']);self.assertIn('歌曲已变化',task['error'])
        self.assertEqual([1,2,3],[s['original_number'] for s in task['music']])
        self.assertFalse(self.service.active)

    def test_bulk_start_validates_atomically_and_uses_current_parallel(self):
        identity = self.imported(parallel_tasks=1)
        with self.assertRaises(ValueError):
            self.service.start_tasks([identity, 'missing'], 3)
        self.assertEqual('draft', self.service.get(identity)['status'])
        for value in (0, 5, True, '3'):
            with self.assertRaises(ValueError):
                self.service.start_tasks([identity], value)
        self.service.start_tasks([identity], 3)
        self.assertEqual(3, self.service.get(identity)['config']['parallel_tasks'])
        self.assertEqual('queued', self.service.get(identity)['status'])

    def test_pool_fills_late_arrivals_and_replaces_finished_task(self):
        first = self.imported(parallel_tasks=3)
        identities = [first]
        for n in range(1, 4):
            task = copy.deepcopy(self.service.get(first))
            task.update(id=f'{n:016x}', created_at=task['created_at']+n)
            self.service.store.put('task:'+task['id'], task)
            identities.append(task['id'])
        entered = {identity: threading.Event() for identity in identities}
        release = {identity: threading.Event() for identity in identities}
        def execute(identity):
            self.service.update(identity, status='running')
            entered[identity].set()
            if not release[identity].wait(8):
                raise AssertionError('Worker did not receive release')
            self.service.update(identity, status='success')
        self.service.action(first, 'start')
        with patch.object(self.service, 'execute', side_effect=execute):
            worker = threading.Thread(target=self.service.run_next)
            worker.start()
            try:
                self.assertTrue(entered[first].wait(3))
                self.service.start_tasks(identities[1:], 3)
                self.assertTrue(entered[identities[1]].wait(3))
                self.assertTrue(entered[identities[2]].wait(3))
                self.assertFalse(entered[identities[3]].is_set())
                release[first].set()
                self.assertTrue(entered[identities[3]].wait(3))
                self.assertFalse(release[identities[1]].is_set())
                self.assertFalse(release[identities[2]].is_set())
            finally:
                for event in release.values(): event.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertTrue(all(event.is_set() for event in entered.values()))

    def test_stalled_task_releases_dispatcher(self):
        identity=self.imported(nonstop_music=False);self.service.action(identity,'start')
        with patch('mixcut.loopmusic._probe',return_value={'streams':[{'codec_type':'video'}],'format':{'duration':'1'}}), patch('mixcut.loopmusic.run_watched',side_effect=RenderStalled('stalled')):
            self.service.run_next()
        self.assertEqual('stalled_paused',self.service.get(identity)['status'])
        self.assertFalse(self.service.has_work())

    def test_deleted_between_selection_and_start_is_safe(self):
        identity=self.imported();self.service.action(identity,'delete')
        self.service.execute(identity)
        self.assertFalse(self.service.active)

    def test_http_routes_and_source_isolation(self):
        httpd=ThreadingHTTPServer(('127.0.0.1',0),Handler);httpd.app=self.app
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        try:
            def request(path,body=None):
                conn=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=3)
                conn.request('GET' if body is None else 'POST',path,body=None if body is None else json.dumps(body),headers={'Content-Type':'application/json'})
                r=conn.getresponse();status=r.status;data=r.read();conn.close();return status,data
            self.assertEqual(200,request('/static/loopmusic.js')[0])
            status,data=request('/api/music-loop/import',{'paths':[str(self.handoff)],'output_dir':str(self.root/'out')})
            self.assertEqual(200,status);identity=json.loads(data)['imported'][0]
            self.assertEqual(200,request(f'/api/music-loop/{identity}/delete',{})[0])
            self.assertEqual([],json.loads(request('/api/music-loop')[1])['tasks'])
            self.assertEqual([],json.loads(request('/api/batches')[1]))
        finally:httpd.shutdown();httpd.server_close();thread.join()

    @unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
    def test_real_render_full_playlist_loop_approval_rejection_and_cache_recovery(self):
        def run(*args):subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True)
        for n,song in enumerate(self.songs):run('-f','lavfi','-i',f'sine=frequency={400+n*100}:duration=3','-c:a','libmp3lame',str(song))
        run('-f','lavfi','-i','testsrc2=size=160x90:rate=30:duration=1','-c:v','libx264','-pix_fmt','yuv420p',str(self.motion))
        self.write_handoff(duration=3)
        identity=self.imported(nonstop_music=True)
        self.service.action(identity,'start')
        with patch.object(self.service.frames,'prepare',side_effect=AssertionError('Music Set must never extract frames')):
            self.service.run_next()
        task=self.service.get(identity)
        self.assertEqual('success',task['status'],task.get('error'))
        self.assertGreater(task['actual_duration'],7.8);self.assertLess(task['actual_duration'],8.1)
        self.assertIsNone(task.get('thumbnails'))
        self.assertFalse(any((self.service.store.directory/'keyframes').rglob('*.jpg')))
        output=Path(task['output_path']);inode=output.stat().st_ino
        # Decode the entire actual export to catch timestamp and loop-boundary failures.
        run('-xerror','-i',str(output),'-f','null','-')
        self.service.clear_cache()
        self.assertEqual('success',self.service.get(identity)['status'])
        task=self.service.get(identity);self.assertEqual('success',task['status'],task.get('error'))
        self.assertEqual(inode,output.stat().st_ino)
        self.service.action(identity,'approve');self.assertTrue(output.is_file())
        self.assertEqual(inode,output.stat().st_ino)
        self.assertEqual('approved',self.service.get(identity)['status'])
        self.assertTrue(all(p.is_file() for p in self.songs+[self.motion,self.cover]))
        self.service.action(identity,'delete');self.assertTrue(output.is_file())
        identity=self.imported(nonstop_music=False,resolution='1280x720')
        self.service.action(identity,'start');self.service.run_next();task=self.service.get(identity)
        self.assertEqual('success',task['status'],task.get('error'))
        self.assertGreater(task['actual_duration'],8.8)
        with patch('mixcut.loopmusic.move_to_trash',side_effect=lambda p:shutil.rmtree(p)) as trash:
            self.service.action(identity,'reject');self.assertEqual(1,trash.call_count)
        self.assertTrue(all(p.is_file() for p in self.songs+[self.motion,self.cover,self.handoff]))
