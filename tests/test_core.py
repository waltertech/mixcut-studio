from __future__ import annotations

import unittest

from mixcut import planner


def asset(identity: str, duration: float) -> dict:
    return {"id": identity, "path": f"/{identity}", "name": identity, "duration": duration,
            "size": 1, "mtime_ns": 1}


class PlannerBoundaries(unittest.TestCase):
    def test_fixed_first_song_has_only_two_orders_for_three_songs(self):
        songs = [asset("a", 10), asset("b", 10), asset("c", 10)]
        videos = [asset("v", 100)]
        with self.assertRaisesRegex(ValueError, "只有 2 个"):
            planner.plan(videos, songs, {"mode": "single", "count": 3, "music_mode": "fixed",
                                         "first_song_ids": ["a"], "min_duration": 30, "max_duration": 30})

    def test_fixed_five_songs_with_first_has_24_orders(self):
        songs = [asset(str(i), 10) for i in range(5)]
        with self.assertRaisesRegex(ValueError, "只有 24 个"):
            planner.plan([asset("v", 100)], songs, {"mode": "single", "count": 25, "music_mode": "fixed",
                         "first_song_ids": ["0"], "min_duration": 50, "max_duration": 50})

    def test_single_rejects_source_shorter_than_complete_music(self):
        with self.assertRaisesRegex(ValueError, "只能生成 0"):
            planner.plan([asset("v", 9)], [asset("m", 10)], {"mode": "single", "count": 1,
                         "music_mode": "fixed", "min_duration": 10, "max_duration": 10})

    def test_multi_requires_two_sources(self):
        with self.assertRaisesRegex(ValueError, "只能生成 0"):
            planner.plan([asset("v", 100)], [asset("m", 20)], {"mode": "multi", "count": 1,
                         "music_mode": "fixed", "min_duration": 20, "max_duration": 20,
                         "segment_min": 10, "segment_max": 10})

    def test_pool_generates_unique_orders_and_exact_song_duration(self):
        videos = [asset("v1", 100), asset("v2", 100)]
        songs = [asset("a", 10), asset("b", 11), asset("c", 12)]
        result = planner.plan(videos, songs, {"mode": "multi", "count": 3, "music_mode": "pool",
             "min_songs": 2, "max_songs": 2, "min_duration": 21, "max_duration": 23,
             "segment_min": 5, "segment_max": 12, "seed": 3})
        self.assertEqual(3, len(result["items"]))
        self.assertEqual(3, len({item["music_fingerprint"] for item in result["items"]}))
        self.assertEqual(3, result['stats']['unique_music_orders'])
        self.assertEqual(3, result['stats']['unique_video_plans'])
        self.assertEqual(6, sum(result['stats']['music_usage'].values()))
        for item in result["items"]:
            self.assertAlmostEqual(item["duration"], sum(song["duration"] for song in item["music"]))
            self.assertGreaterEqual(len({p["asset_id"] for p in item["segments"]}), 2)

    def test_empty_explicit_selection_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "至少选择"):
            planner.plan([asset("v", 20)], [asset("m", 10)], {"video_ids": [], "count": 1})

    def test_bounded_search_marks_uncertainty(self):
        songs = [asset(str(i), 10) for i in range(5)]
        with self.assertRaisesRegex(ValueError, "搜索达到上限"):
            planner.plan([asset("v", 100)], songs, {"mode": "single", "count": 1, "music_mode": "pool",
                         "first_song_ids": ['4'], "min_songs": 1, "max_songs": 1,
                         "min_duration": 10, "max_duration": 10, "search_limit": 1})

    def test_no_duration_match_reports_nearest(self):
        with self.assertRaisesRegex(ValueError, "最接近"):
            planner.plan([asset("v", 100)], [asset("a", 10), asset("b", 10)], {"mode": "single", "music_mode": "pool", "count": 1,
                         "min_songs": 2, "max_songs": 2, "min_duration": 25, "max_duration": 26})

    def test_invalid_duration_range_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "最短/最长"):
            planner.plan([asset("v", 100)], [asset("m", 10)], {"count": 1, "min_duration": 11, "max_duration": 10})

    def test_multi_repeated_source_intervals_do_not_overlap(self):
        videos = [asset("a", 100), asset("b", 100)]
        item = planner.plan(videos, [asset("m", 30)], {"mode": "multi", "count": 1, "music_mode": "fixed",
                            "min_duration": 30, "max_duration": 30, "segment_min": 10, "segment_max": 10, "seed": 4})["items"][0]
        parts = item["segments"]
        for i, left in enumerate(parts):
            for right in parts[i + 1:]:
                if left["asset_id"] == right["asset_id"]:
                    self.assertTrue(left["start"] + left["duration"] <= right["start"] or right["start"] + right["duration"] <= left["start"])

    def test_single_start_gap_is_respected_even_after_filling_endpoints(self):
        result = planner.plan([asset('v', 100)], [asset(str(i), 10) for i in range(5)],
                              {'count': 8, 'music_mode': 'pool', 'min_songs': 1, 'max_songs': 2,
                               'min_duration': 10, 'max_duration': 20, 'allow_overlap': True,
                               'start_gap': 10, 'seed': 7})
        starts = sorted(i['segments'][0]['start'] for i in result['items'])
        self.assertTrue(all(b - a >= 10 - 1e-6 for a, b in zip(starts, starts[1:])))

    def test_same_group_never_mixes_group_labels(self):
        groups = {'a': 'fps', 'b': 'fps', 'c': 'moba', 'd': 'moba'}
        result = planner.plan([asset(i, 100) for i in groups], [asset('m', 20)],
                              {'mode': 'multi', 'count': 1, 'music_mode': 'fixed',
                               'segment_min': 10, 'segment_max': 10,
                               'within_group': True, 'groups': groups})
        self.assertEqual(1, len({groups[p['asset_id']] for p in result['items'][0]['segments']}))

    def test_unselected_first_song_is_explained(self):
        with self.assertRaisesRegex(ValueError, '开头候选'):
            planner.plan([asset('v', 100)], [asset('a', 10), asset('b', 10)],
                         {'music_ids': ['a'], 'first_song_ids': ['b']})

    def test_within_group_requires_explicit_group(self):
        with self.assertRaisesRegex(ValueError, '分组名称'):
            planner.plan([asset('a', 100), asset('b', 100)], [asset('m', 20)],
                         {'mode': 'multi', 'within_group': True, 'segment_min': 10, 'segment_max': 10})

    def test_batch_count_has_hard_limit(self):
        with self.assertRaisesRegex(ValueError, '500'):
            planner.plan([asset('v', 100)], [asset('m', 20)], {'count': 50001})

    def test_multisource_nearly_full_sources_can_be_allocated(self):
        result = planner.plan([asset('a', 30), asset('b', 30)], [asset('m', 60)],
                              {'mode': 'multi', 'segment_min': 10, 'segment_max': 10, 'seed': 8})
        self.assertEqual(6, len(result['items'][0]['segments']))


if __name__ == "__main__":
    unittest.main()
