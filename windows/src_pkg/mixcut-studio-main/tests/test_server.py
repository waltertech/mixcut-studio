"""Queue durability tests.  These intentionally do not require ffmpeg or media files."""
from __future__ import annotations

import sys
import tempfile
import http.client
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import mixcut
from mixcut import server


def item(number=1, status='pending'):
    return {
        'id': str(number), 'index': number, 'status': status, 'progress': 0,
        'error': None, 'attempts': 0, 'duration': 12,
        'segments': [{'asset_id': 'video'}], 'music': [{'id': 'song'}],
    }


def batch(batch_id='abc123', status='queued', items=None, output_dir=None):
    items = items if items is not None else [item()]
    for index, entry in enumerate(items, 1):
        entry.setdefault('output_path', str(Path(output_dir) / batch_id / f'{index:03d}.mp4'))
    return {
        'id': batch_id, 'status': status, 'created_at': 1, 'updated_at': 1,
        'config': {'output_dir': str(output_dir)}, 'items': items,
        'assets': [{'id': 'video'}, {'id': 'song'}],
    }


class FakeMedia:
    def __init__(self, verify=None):
        self.verified = []
        self.verify = verify

    def verify_asset(self, asset):
        self.verified.append(asset['id'])
        if self.verify:
            self.verify(asset)


class FakeRenderer:
    def __init__(self, render=None, validate=None):
        self.renders = []
        self.validations = []
        self.render = render or self._render
        self.validate = validate or self._validate

    def _render(self, entry, config, output, work, progress):
        self.renders.append(entry['id'])
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b'video')
        progress(1)
        return {'ok': True}

    def _validate(self, output, duration):
        self.validations.append((output, duration))
        return {'valid': True}


class ApplicationTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.app = server.Application(self.root / 'state')

    def tearDown(self):
        self.app.closing.set()
        self.temporary.cleanup()

    def save(self, record):
        self.app.store.put('batch:' + record['id'], record)
        return record

    def execute(self, record, media=None, renderer=None):
        media = media or FakeMedia()
        renderer = renderer or FakeRenderer()
        for entry in record['items']:
            Path(entry['output_path']).parent.mkdir(parents=True, exist_ok=True)
        with (patch.dict(sys.modules, {'mixcut.media': media, 'mixcut.renderer': renderer}),
              patch.object(mixcut, 'media', media, create=True),
              patch.object(mixcut, 'renderer', renderer, create=True)):
            self.app.execute_batch(record['id'])
        return media, renderer

    def test_store_persists_records_between_instances(self):
        self.app.store.put('library', {'scan': {'videos': ['v']}})
        reopened = server.Store(self.root / 'state')
        self.assertEqual({'scan': {'videos': ['v']}}, reopened.get('library'))

    def test_bootstrap_reports_process_version_and_hidden_active_work(self):
        record = batch('hidden1', 'running', [item()], self.root / 'exports')
        record['items'][0]['dismissed'] = True
        self.save(record)
        with patch('mixcut.server.app_version', return_value='different-on-disk'):
            data = self.app.bootstrap()
        self.assertEqual('1.5.9', data['version'])
        self.assertEqual(15, data['api_protocol'])
        self.assertTrue(data['active_work'])
        self.assertEqual([], data['batches'])

    def test_recovery_defers_large_archive_validation_until_background_work(self):
        approved = batch('approved1', 'completed', [item(1, 'success')], self.root / 'exports')
        approved['items'][0].update(review={'status': 'approved', 'path': str(self.root / 'archive.mp4')},
                                    cleanup={'output_deleted': True, 'source_cleanup_requested': True})
        self.save(approved)
        with patch.object(server.Application, '_cleanup_approved_sources',
                          side_effect=AssertionError('archive validation blocked startup')):
            restarted = server.Application(self.root / 'state')
        self.assertEqual('approved', restarted.batch('approved1')['items'][0]['review']['status'])

    def test_draft_music_order_can_change_but_cannot_duplicate_another_item(self):
        first = item(1)
        first['music'] = [{'id': 'a', 'name': 'A'}, {'id': 'b', 'name': 'B'}, {'id': 'c', 'name': 'C'}]
        second = item(2)
        second['music'] = [{'id': 'b'}, {'id': 'a'}, {'id': 'c'}]
        record = self.save(batch('musicorder1', 'draft', [first, second], self.root / 'exports'))

        changed = self.app.reorder_music(record['id'], first['id'], ['c', 'a', 'b'])

        self.assertEqual(['c', 'a', 'b'], [song['id'] for song in changed['items'][0]['music']])
        self.assertTrue(changed['items'][0]['manual_music_order'])
        self.assertEqual(2, changed['stats']['unique_music_orders'])
        with self.assertRaisesRegex(ValueError, '重复'):
            self.app.reorder_music(record['id'], first['id'], ['b', 'a', 'c'])
        changed['status'] = 'queued'
        self.app.store.put('batch:' + changed['id'], changed)
        with self.assertRaisesRegex(ValueError, '草稿'):
            self.app.reorder_music(record['id'], first['id'], ['a', 'b', 'c'])

    def test_recovery_pauses_interrupted_work_and_keeps_existing_success(self):
        existing = self.root / 'exports' / 'recover1' / '001.mp4'
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b'ok')
        record = batch('recover1', 'running', [item(1, 'success'), item(2, 'running')], self.root / 'exports')
        record['items'][0]['output_path'] = str(existing)
        self.save(record)

        recovered = server.Application(self.root / 'state').batch('recover1')

        self.assertEqual('paused', recovered['status'])
        self.assertEqual('success', recovered['items'][0]['status'])
        self.assertEqual('pending', recovered['items'][1]['status'])
        self.assertIn('中断', recovered['items'][1]['error'])

    def test_recovery_marks_missing_completed_output_failed(self):
        record = self.save(batch('missing1', 'completed', [item(1, 'success')], self.root / 'exports'))
        recovered = server.Application(self.root / 'state').batch(record['id'])

        self.assertEqual('paused', recovered['status'])
        self.assertEqual('failed', recovered['items'][0]['status'])
        self.assertIn('移动或删除', recovered['items'][0]['error'])

    def test_recovery_marks_completed_output_failed_when_size_or_mtime_changed(self):
        record = batch('changed1', 'completed', [item(1, 'success')], self.root / 'exports')
        output = Path(record['items'][0]['output_path'])
        output.parent.mkdir(parents=True)
        output.write_bytes(b'original')
        stat = output.stat()
        record['items'][0]['result'] = {
            'output_size': stat.st_size,
            'output_mtime_ns': stat.st_mtime_ns,
        }
        output.write_bytes(b'replaced-with-different-size')
        self.save(record)

        recovered = server.Application(self.root / 'state').batch(record['id'])

        self.assertEqual('paused', recovered['status'])
        self.assertEqual('failed', recovered['items'][0]['status'])
        self.assertIn('已改变', recovered['items'][0]['error'])

    def test_actions_reject_duplicate_start_and_pause_a_queued_batch(self):
        record = self.save(batch('guard1', 'queued', output_dir=self.root / 'exports'))
        with self.assertRaisesRegex(ValueError, '正在执行'):
            self.app.action(record['id'], 'start')

        result = self.app.action(record['id'], 'pause')
        self.assertEqual('paused', result['status'])
        with self.assertRaisesRegex(ValueError, '仅执行中或排队中'):
            self.app.action(record['id'], 'pause')

    def test_single_item_stop_restart_delete_and_clear(self):
        record = self.save(batch('items1', 'paused', [item(1), item(2, 'failed')], self.root / 'exports'))
        stopped = self.app.item_action(record['id'], '1', 'stop')
        self.assertEqual('cancelled', stopped['items'][0]['status'])
        restarted = self.app.item_action(record['id'], '1', 'start')
        self.assertEqual('queued', restarted['status'])
        self.assertEqual('pending', restarted['items'][0]['status'])
        self.app.store.update(record['id'], lambda value: value.update(status='stopped'))
        deleted = self.app.item_action(record['id'], '2', 'delete')
        self.assertEqual(['1'], [entry['id'] for entry in deleted['updated_batches'][record['id']]['items']])
        self.assertEqual(1, deleted['updated_batches'][record['id']]['items'][0]['index'])
        self.assertEqual([], self.app.store.records('deletion:'))
        self.assertEqual(1, self.app.clear_batches(confirmation='DELETE_TASK_RECORDS')['deleted'])
        self.assertEqual([], self.app.store.batches())

    def test_existing_valid_output_is_committed_without_rendering_again(self):
        record = batch('rename1', output_dir=self.root / 'exports')
        output = Path(record['items'][0]['output_path'])
        output.parent.mkdir(parents=True)
        output.write_bytes(b'final-before-db-commit')
        self.save(record)
        renderer = FakeRenderer()

        media, renderer = self.execute(record, renderer=renderer)
        saved = self.app.batch(record['id'])

        self.assertEqual([], renderer.renders)
        self.assertEqual(1, len(renderer.validations))
        self.assertEqual('success', saved['items'][0]['status'])
        self.assertEqual('completed', saved['status'])
        self.assertEqual(['song'], media.verified)
        self.assertEqual(output.stat().st_size, saved['items'][0]['result']['output_size'])
        self.assertEqual(output.stat().st_mtime_ns, saved['items'][0]['result']['output_mtime_ns'])

    def test_unrecoverable_encoder_error_does_not_repeat_whole_task(self):
        record = self.save(batch('retry1', output_dir=self.root / 'exports'))
        renderer = FakeRenderer()

        def fail(*args):
            renderer.renders.append(args[0]['id'])
            raise RuntimeError('encoder failed')
        renderer.render = fail
        self.execute(record, renderer=renderer)
        saved = self.app.batch(record['id'])

        self.assertEqual(['1'], renderer.renders)
        self.assertEqual(1, saved['items'][0]['attempts'])
        self.assertEqual('failed', saved['items'][0]['status'])
        self.assertEqual('failed', saved['status'])

    def test_pause_finishes_current_item_then_leaves_remaining_pending(self):
        record = self.save(batch('pause1', items=[item(1), item(2)], output_dir=self.root / 'exports'))
        renderer = FakeRenderer()

        def pause_after_render(entry, config, output, work, progress):
            renderer._render(entry, config, output, work, progress)
            self.app.action(record['id'], 'pause')
            return {'ok': True}
        renderer.render = pause_after_render
        self.execute(record, renderer=renderer)
        saved = self.app.batch(record['id'])

        self.assertEqual('paused', saved['status'])
        self.assertEqual('success', saved['items'][0]['status'])
        self.assertEqual('pending', saved['items'][1]['status'])

    def test_stop_finishes_current_item_then_marks_batch_stopped(self):
        record = self.save(batch('stop1', items=[item(1), item(2)], output_dir=self.root / 'exports'))
        renderer = FakeRenderer()

        def stop_after_render(entry, config, output, work, progress):
            renderer._render(entry, config, output, work, progress)
            self.app.action(record['id'], 'stop')
            return {'ok': True}
        renderer.render = stop_after_render
        self.execute(record, renderer=renderer)
        saved = self.app.batch(record['id'])

        self.assertEqual('stopped', saved['status'])
        self.assertEqual('success', saved['items'][0]['status'])
        self.assertEqual('pending', saved['items'][1]['status'])

    def test_changed_music_fails_only_its_task_without_rendering(self):
        """The queue turns a changed source into a paused, user-actionable batch."""
        record = self.save(batch('source1', output_dir=self.root / 'exports'))
        def changed(asset):
            self.app.closing.set()
            raise ValueError('素材已变更')
        media = FakeMedia(changed)
        renderer = FakeRenderer()

        Path(record['items'][0]['output_path']).parent.mkdir(parents=True)
        with (patch.dict(sys.modules, {'mixcut.media': media, 'mixcut.renderer': renderer}),
              patch.object(mixcut, 'media', media, create=True),
              patch.object(mixcut, 'renderer', renderer, create=True)):
            self.app.run_queue()
        saved = self.app.batch(record['id'])

        self.assertEqual('failed', saved['items'][0]['status'])
        self.assertIn('素材已变更', saved['items'][0].get('error', ''))
        self.assertEqual([], renderer.renders)


class HandlerTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.app = server.Application(Path(self.temporary.name) / 'state')
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.httpd.app = self.app
        self.port = self.httpd.server_port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.app.closing.set()
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.server_close()
        self.temporary.cleanup()

    def get(self, path, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        connection.request('GET', path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        status, response_headers = response.status, dict(response.getheaders())
        connection.close()
        return status, response_headers, body

    def test_suffix_range_returns_exact_tail_with_206(self):
        path = server.ROOT / 'static' / 'app.js'
        expected = path.read_bytes()[-17:]

        status, headers, body = self.get('/app.js', {'Range': 'bytes=-17'})

        self.assertEqual(206, status)
        self.assertEqual(expected, body)
        self.assertEqual(f'bytes {path.stat().st_size - 17}-{path.stat().st_size - 1}/{path.stat().st_size}',
                         headers['Content-Range'])
        self.assertEqual('bytes', headers['Accept-Ranges'])

    def test_unsatisfiable_range_returns_416(self):
        status, headers, body = self.get('/app.js', {'Range': 'bytes=999999999-'})

        self.assertEqual(416, status)
        self.assertIn('bytes */', headers['Content-Range'])
        self.assertEqual(b'', body)

    def test_loopback_guard_rejects_forged_host_and_cross_site_origin(self):
        status, _, _ = self.get('/api/bootstrap', {'Host': 'example.test:1'})
        self.assertEqual(403, status)

        status, _, _ = self.get('/api/bootstrap', {'Origin': 'https://example.test'})
        self.assertEqual(403, status)

    def test_static_aliases_work_but_arbitrary_workspace_paths_do_not(self):
        status, headers, body = self.get('/static/app.js')
        self.assertEqual(200, status)
        self.assertEqual((server.ROOT / 'static' / 'app.js').read_bytes(), body)
        self.assertEqual('no-store', headers['Cache-Control'])

        status, _, _ = self.get('/PROJECT_PLAN.md')
        self.assertEqual(404, status)
        status, _, _ = self.get('/static/../PROJECT_PLAN.md')
        self.assertEqual(404, status)


if __name__ == '__main__':
    unittest.main()
