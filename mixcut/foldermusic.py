"""Directory-based music allocation using cached metadata only."""
from __future__ import annotations

import copy
import math
import os
from pathlib import Path

MAX_PLAYS = 4096


def groups(music, root):
    """Immediate children are groups; descendants belong to that same child."""
    if not root:
        raise ValueError('单文件夹音乐模式需要音乐库根路径，请重新扫描音乐库')
    base = Path(root).expanduser().resolve()
    result = {}
    seen = set()
    for song in music:
        try:
            path = Path(os.path.abspath(song['path']))
            relative = path.relative_to(base)
            duration = float(song['duration'])
        except (KeyError, ValueError, TypeError):
            continue
        if len(relative.parts) < 2 or not math.isfinite(duration) or duration <= .1 or str(path) in seen:
            continue
        seen.add(str(path))
        folder = str(base / relative.parts[0])
        result.setdefault(folder, []).append(song)
    return result


def catalog(grouped):
    return [{'path': path, 'name': Path(path).name, 'song_count': len(songs),
             'duration': sum(float(song['duration']) for song in songs)}
            for path, songs in sorted(grouped.items())]


def choose_folder(grouped, usage, rng, exclude=None):
    candidates = [path for path in grouped if path != exclude]
    if not candidates:
        raise ValueError('没有其他可用音乐文件夹')
    least = min(usage.get(path, 0) for path in candidates)
    return rng.choice([path for path in candidates if usage.get(path, 0) == least])


def shuffled_round(songs, rng, last_id=None):
    order = list(songs)
    rng.shuffle(order)
    if len(order) > 1 and order[0]['id'] == last_id:
        # Pick uniformly among the other positions; never repeat across a round boundary.
        index = rng.randrange(1, len(order))
        order[0], order[index] = order[index], order[0]
    return order


def allocate(songs, target, config, rng):
    """Store whole shuffled rounds, while the renderer consumes only the audible prefix."""
    target = float(target)
    if not math.isfinite(target) or target <= 0 or not songs:
        raise ValueError('文件夹没有可用音乐或目标时长无效')
    margin = float(config.get('music_margin_seconds', 3))
    if not math.isfinite(margin) or margin < 0:
        raise ValueError('音乐时长余量必须是非负有限数字')
    goal = target + min(margin, target * .005)
    result, total = [], 0.
    while total < goal:
        order = shuffled_round(songs, rng, result[-1]['id'] if result else None)
        if len(result) + len(order) > MAX_PLAYS:
            raise ValueError('音乐循环次数过多，请增加有效歌曲或缩短视频时长')
        result.extend(copy.deepcopy(order))
        total += sum(float(song['duration']) for song in order)
    return tuple(result), total


def describe(item):
    songs = item.get('music', [])
    item['music_unique_count'] = len({song['id'] for song in songs})
    item['music_play_count'] = len(songs)
    size = max(1, int(item.get('music_folder_song_count', item['music_unique_count'])))
    item['music_rounds'] = math.ceil(len(songs) / size)
