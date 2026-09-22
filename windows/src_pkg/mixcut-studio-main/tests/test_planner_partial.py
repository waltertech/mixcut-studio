from __future__ import annotations

import unittest

from mixcut import planner


def asset(identity, duration):
    return {'id': identity, 'path': '/' + identity, 'name': identity, 'duration': duration,
            'size': 1, 'mtime_ns': 1, 'has_audio': True}


class PartialPlanningTests(unittest.TestCase):
    def config(self, count=50, **extra):
        value = {'mode': 'single', 'count': count, 'music_mode': 'pool', 'min_songs': 1,
                 'max_songs': 1, 'min_duration': 10, 'max_duration': 10, 'allow_overlap': True,
                 'start_gap': 1, 'seed': 4}
        value.update(extra)
        return value

    def test_partial_returns_all_available_music_orders(self):
        result = planner.plan([asset('v', 100)], [asset('a', 10), asset('b', 10)], self.config(), allow_partial=True)
        self.assertEqual(2, len(result['items']))
        self.assertEqual(50, result['stats']['requested_count'])
        self.assertEqual(48, result['stats']['remaining'])

    def test_same_previous_items_produce_no_duplicates(self):
        first = planner.plan([asset('v', 100)], [asset('a', 10), asset('b', 10)], self.config(2), allow_partial=True)
        again = planner.plan([asset('v', 100)], [asset('a', 10), asset('b', 10)], self.config(2), allow_partial=True, previous_items=first['items'])
        self.assertEqual([], again['items'])

    def test_new_video_can_use_orders_left_by_old_capacity(self):
        songs = [asset('a', 10), asset('b', 10), asset('c', 10)]
        first = planner.plan([asset('old', 10)], songs, self.config(3, allow_overlap=False), allow_partial=True)
        second = planner.plan([asset('old', 10), asset('new', 10)], songs, self.config(3, allow_overlap=False), allow_partial=True, previous_items=first['items'])
        self.assertEqual(1, len(first['items']))
        self.assertGreaterEqual(len(second['items']), 1)
        self.assertTrue({i['music_fingerprint'] for i in first['items']}.isdisjoint(i['music_fingerprint'] for i in second['items']))

    def test_previous_intervals_block_nonoverlap_across_calls(self):
        previous = [{'segments': [{'asset_id': 'v', 'start': 0, 'duration': 10}], 'music': []}]
        result = planner.plan([asset('v', 20)], [asset('a', 10)], self.config(1, allow_overlap=False), allow_partial=True, previous_items=previous)
        self.assertEqual(1, len(result['items']))
        self.assertGreaterEqual(result['items'][0]['segments'][0]['start'], 10)

    def test_partial_empty_media_and_invalid_config(self):
        result = planner.plan([], [], self.config(), allow_partial=True)
        self.assertEqual([], result['items'])
        self.assertEqual([], planner.plan([], [], {'count': 1}, allow_partial=True)['items'])
        with self.assertRaisesRegex(ValueError, '最短/最长'):
            planner.plan([], [], self.config(min_duration=11, max_duration=10), allow_partial=True)

    def test_strict_mode_remains_strict(self):
        with self.assertRaises(ValueError):
            planner.plan([asset('v', 100)], [asset('a', 10)], self.config(2))


if __name__ == '__main__':
    unittest.main()
