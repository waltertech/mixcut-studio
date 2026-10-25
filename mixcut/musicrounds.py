"""Repeat one chosen playlist without reshuffling subsequent rounds."""
import copy
from .musicled import fit_segments


def validate(value):
    if isinstance(value, bool) or str(value) not in {'1', '2', '3', '4'}:
        raise ValueError('音乐播放轮数须为 1、2、3 或 4')
    return int(value)


def apply(item, base, repeats):
    repeats = validate(repeats)
    if not base:
        raise ValueError('一轮歌单不能为空')
    if len(base) * repeats > 4096:
        raise ValueError('重复后的音乐清单过长，请减少歌曲或播放轮数')
    item['music_base'] = copy.deepcopy(base)
    item['music_repeats'] = repeats
    item['music_base_duration'] = sum(float(s['duration']) for s in base)
    item['music'] = [copy.deepcopy(song) for _ in range(repeats) for song in base]
    item['music_total_duration'] = item['music_base_duration'] * repeats
    return item['music_total_duration']


def retime(item, target, config):
    if config.get('mode', 'single') == 'single' and item.get('segments'):
        source_id = item['segments'][0]['asset_id']
        source = next((a for a in item.get('video_assets', []) if a['id'] == source_id), None)
        if source:
            length = float(source['duration'])
            start = float(item['segments'][0]['start']) if target <= length else 0.
            if start + min(target, length) > length:
                start = 0.
            return fit_segments([{'asset_id':source_id, 'path':source['path'], 'start':start,
                                  'duration':length-start}], target)
    return fit_segments(item['segments'], target)
