from pathlib import Path
import tempfile
import unittest

from mixcut import planner
from mixcut.server import Application


def asset(identity, seconds):
    return {'id': identity, 'path': '/' + identity, 'duration': seconds}


class SourceDurationTests(unittest.TestCase):
    def test_single_mode_excludes_below_and_exact_boundary(self):
        result = planner.plan([asset('short', 299), asset('equal', 300), asset('long', 300.01)],
                              [asset('song', 10)], {'count': 1, 'min_source_duration_minutes': 5})
        self.assertEqual('long', result['items'][0]['segments'][0]['asset_id'])
        self.assertTrue(any('排除 2 条' in warning for warning in result['warnings']))

    def test_multi_mode_uses_only_eligible_sources(self):
        result = planner.plan([asset('short', 30), asset('a', 300.01), asset('b', 360)],
                              [asset('song', 20)], {'count': 1, 'mode': 'multi', 'segment_min': 10,
                                                   'segment_max': 10, 'min_source_duration_minutes': 5})
        self.assertEqual({'a', 'b'}, {s['asset_id'] for s in result['items'][0]['segments']})

    def test_zero_unset_and_fractional_minutes(self):
        for value in ({}, {'min_source_duration_minutes': 0}):
            self.assertEqual(1, len(planner.plan([asset('v', 10)], [asset('song', 5)], {'count': 1, **value})['items']))
        result = planner.plan([asset('equal', 30), asset('long', 30.01)], [asset('song', 5)],
                              {'count': 1, 'min_source_duration_minutes': .5})
        self.assertEqual('long', result['items'][0]['segments'][0]['asset_id'])

    def test_partial_planning_waits_while_strict_mode_explains_filter(self):
        videos, songs = [asset('v', 300)], [asset('song', 5)]
        config = {'count': 50, 'min_source_duration_minutes': 5}
        with self.assertRaisesRegex(ValueError, '大于 5 分钟'):
            planner.plan(videos, songs, config)
        result = planner.plan(videos, songs, config, allow_partial=True)
        self.assertEqual([], result['items'])
        self.assertEqual(50, result['stats']['remaining'])
        self.assertIn('大于 5 分钟', result['warnings'][0])

    def test_invalid_values_and_scheduler_snapshot(self):
        for value in (-1, float('nan'), float('inf'), 'bad', None):
            with self.assertRaisesRegex(ValueError, '源视频时长门槛'):
                planner.plan([], [], {'count': 1, 'min_source_duration_minutes': value}, allow_partial=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = Application(root / 'state')
            body = {'name': 'long sources', 'start_at': 10010, 'repeat': 'once', 'target_count': 50,
                    'check_interval_minutes': 1, 'enabled': False, 'video_dir': str(root / 'video'),
                    'music_dir': str(root / 'music'), 'output_dir': str(root / 'out'),
                    'config': {'min_source_duration_minutes': 5.5}}
            result = app.scheduler.save(body, now=10000)
            self.assertEqual(5.5, result['schedules'][0]['config']['min_source_duration_minutes'])
            reopened = Application(root / 'state')
            self.assertEqual(5.5, reopened.scheduler.listing()['schedules'][0]['config']['min_source_duration_minutes'])
            body['config']['min_source_duration_minutes'] = -1
            with self.assertRaisesRegex(ValueError, '源视频时长门槛'):
                app.scheduler.save(body, now=10000)


if __name__ == '__main__':
    unittest.main()
