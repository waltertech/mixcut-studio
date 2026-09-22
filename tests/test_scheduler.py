"""Persistent scheduler tests with deterministic inventory and no background thread."""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import mixcut
from mixcut import server
from mixcut.scheduler import Scheduler


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.app = server.Application(self.root / 'state')
        self.app.scheduler = Scheduler(self.app, background_checks=False)
        self.scheduler = self.app.scheduler
        self.now = 10_000.0
        self.inventory = {'videos': [self.asset('video1', 100)],
                          'music': [self.asset('song1', 1), self.asset('song2', 1)], 'errors': []}
        self.fake_media = types.SimpleNamespace(scan=self.scan)
        self.media_patch = patch.dict(sys.modules, {'mixcut.media': self.fake_media})
        self.package_patch = patch.object(mixcut, 'media', self.fake_media, create=True)
        self.media_patch.start()
        self.package_patch.start()

    def tearDown(self):
        self.package_patch.stop()
        self.media_patch.stop()
        self.app.closing.set()
        self.temporary.cleanup()

    @staticmethod
    def asset(identity, duration):
        return {'id': identity, 'name': identity, 'path': f'/stable/{identity}', 'duration': duration,
                'size': 1, 'mtime_ns': 0, 'has_audio': True}

    def scan(self, *args, **kwargs):
        return {key: list(value) if isinstance(value, list) else value for key, value in self.inventory.items()}

    def save(self, *, name='schedule', repeat='once', start_at=None, target=50, enabled=True):
        body = {
            'name': name, 'repeat': repeat, 'start_at': self.now + 10 if start_at is None else start_at,
            'target_count': target, 'check_interval_minutes': 1, 'enabled': enabled,
            'video_dir': str(self.root / 'videos'), 'music_dir': str(self.root / 'music'),
            'output_dir': str(self.root / 'exports'),
            'config': {'mode': 'single', 'music_mode': 'fixed', 'min_duration': 0, 'max_duration': 10,
                       'segment_min': 1, 'segment_max': 10, 'start_gap': .01, 'allow_overlap': True,
                       'width': 640, 'height': 360, 'fps': 30},
        }
        self.scheduler.save(body, now=self.now)
        return self.scheduler.schedules()[0]

    def start_due(self, schedule):
        self.scheduler.tick(schedule['next_due'])
        return self.scheduler.active()

    def batches_for(self, run):
        return [batch for batch in self.app.store.batches() if batch.get('scheduled_run_id') == run['id']]

    def mark_complete(self, batch, successful=None):
        successful = len(batch['items']) if successful is None else successful
        def complete(value):
            for index, item in enumerate(value['items']):
                if index < successful:
                    item.update(status='success', progress=1)
            value['status'] = 'completed' if successful == len(value['items']) else 'paused'
        self.app.store.update(batch['id'], complete)

    def test_partial_target_only_rechecks_on_new_material_and_never_replans_same_inventory(self):
        schedule = self.save(target=50)
        run = self.start_due(schedule)
        initial = self.batches_for(run)
        self.assertEqual(1, len(initial))
        self.assertEqual(2, len(initial[0]['items']))
        self.assertEqual('queued', initial[0]['status'])

        self.mark_complete(initial[0])
        self.scheduler.tick(schedule['next_due'] + 1)
        run = self.scheduler.active()
        self.assertEqual('waiting', run['status'])
        self.assertEqual(2, run['completed_count'])

        self.scheduler.tick(self.scheduler.active()['next_check'])
        self.assertEqual(1, len(self.batches_for(run)))
        self.assertEqual('waiting', self.scheduler.active()['status'])

        self.inventory['music'].append(self.asset('song3', 1))
        self.scheduler.check_now(self.scheduler.active()['next_check'] + 1)
        batches = self.batches_for(run)
        self.assertEqual(2, len(batches))
        new_batch = next(batch for batch in batches if batch['id'] != initial[0]['id'])
        self.assertGreater(len(new_batch['items']), 0)
        self.assertEqual({'song1', 'song2', 'song3'}, {asset['id'] for asset in new_batch['assets'] if asset['id'].startswith('song')})
        old_orders = {tuple(song['id'] for song in item['music']) for item in initial[0]['items']}
        new_orders = {tuple(song['id'] for song in item['music']) for item in new_batch['items']}
        self.assertFalse(old_orders & new_orders)

        self.mark_complete(new_batch)
        self.scheduler.tick(self.scheduler.active()['next_check'] + 1)
        self.scheduler.tick(self.scheduler.active()['next_check'])
        self.assertEqual(2, len(self.batches_for(run)))
        self.assertLess(self.scheduler.active()['completed_count'], 50)

    def test_superseding_run_cancels_only_its_pending_work_and_cannot_resume_after_restart(self):
        first = self.save(name='first', target=50)
        old_run = self.start_due(first)
        old_batch = self.batches_for(old_run)[0]
        self.mark_complete(old_batch, successful=1)
        manual = {'id': 'manual01', 'status': 'paused', 'created_at': 2, 'updated_at': 2,
                  'config': {'output_dir': str(self.root / 'exports')}, 'items': [], 'assets': []}
        self.app.store.put('batch:manual01', manual)

        second = self.save(name='second', start_at=self.now + 20, target=50)
        self.scheduler.action(second['id'], 'run-now', now=self.now + 21)
        cancelled = self.app.batch(old_batch['id'])

        self.assertEqual('superseded', self.app.store.get('scheduled-run:' + old_run['id'])['status'])
        self.assertTrue(cancelled['scheduled_cancelled'])
        self.assertEqual('success', cancelled['items'][0]['status'])
        self.assertEqual('cancelled', cancelled['items'][1]['status'])
        self.assertEqual('stopped', cancelled['status'])
        self.assertEqual('paused', self.app.batch('manual01')['status'])
        with self.assertRaisesRegex(ValueError, '已取消'):
            self.app.action(old_batch['id'], 'resume')

        restarted = Scheduler(self.app, background_checks=False)
        restarted.tick(self.now + 22)
        self.assertEqual('stopped', self.app.batch(old_batch['id'])['status'])
        self.assertTrue(self.app.batch(old_batch['id'])['scheduled_cancelled'])

    def test_daily_tick_uses_only_the_latest_due_time_without_backlogging_runs(self):
        schedule = self.save(repeat='daily', start_at=self.now + 10, target=50)
        late = schedule['next_due'] + 4 * 24 * 60 * 60 + 30

        self.scheduler.tick(late)
        run = self.scheduler.active()
        updated = self.app.store.get('schedule:' + schedule['id'])
        self.assertLessEqual(run['scheduled_at'], late)
        self.assertGreater(updated['next_due'], late)
        self.assertEqual(1, len(self.scheduler.runs()))
        self.scheduler.tick(late)
        self.assertEqual(1, len(self.scheduler.runs()))

    def test_run_now_does_not_start_the_same_overdue_slot_twice(self):
        schedule = self.save(start_at=self.now + 10)
        self.scheduler.action(schedule['id'], 'run-now', now=self.now + 11)
        self.assertEqual(1, len(self.scheduler.runs()))
        self.assertEqual(self.now + 11, self.scheduler.active()['scheduled_at'])

    def test_listing_after_restart_does_not_resume_old_goal_before_due_switch(self):
        first = self.save(name='older', start_at=self.now + 10)
        old = self.start_due(first)
        child = self.batches_for(old)[0]
        self.app.store.update(child['id'], lambda b: b.update(status='paused', recovery_note='interrupted'))
        self.scheduler.listing()
        self.assertEqual('paused', self.app.batch(child['id'])['status'])
        self.save(name='newer', start_at=self.now + 20)
        self.scheduler.tick(self.now + 20)
        self.assertEqual('stopped', self.app.batch(child['id'])['status'])
        self.assertEqual('newer', self.scheduler.active()['name'])

    def test_missing_material_waits_disable_cancels_and_target_limit_is_enforced(self):
        with self.assertRaisesRegex(ValueError, '目标数量须为1～500'):
            self.save(target=501)

        schedule = self.save(target=50)
        self.inventory = {'videos': [], 'music': [], 'errors': [{'error': '文件夹不存在'}]}
        run = self.start_due(schedule)
        waiting = self.scheduler.active()
        self.assertEqual('waiting', waiting['status'])
        self.assertIn('文件夹不存在', waiting['last_error'])

        self.scheduler.action(schedule['id'], 'disable', now=schedule['next_due'] + 1)
        cancelled = self.app.store.get('scheduled-run:' + run['id'])
        self.assertEqual('cancelled', cancelled['status'])
        self.assertIsNone(cancelled['next_check'])


if __name__ == '__main__':
    unittest.main()
