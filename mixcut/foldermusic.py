"""Directory-based music allocation using cached metadata only."""
from __future__ import annotations

import copy
import math
import os
import re
from pathlib import Path

MAX_PLAYS = 4096


def groups(music, root):
    """Group by deepest music-bearing directories, independent of root depth.

    Use scanned metadata only: no second traversal or media reads. Ancestor
    directories containing loose audio are excluded when a deeper set exists.
    Non-music subdirectories do not disqualify an otherwise valid set.
    """
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
        if not relative.parts or not math.isfinite(duration) or duration <= .1 or str(path) in seen:
            continue
        seen.add(str(path))
        folder = str(path.parent)
        result.setdefault(folder, []).append(song)
    ancestors = set()
    for folder in result:
        parent = Path(folder).parent
        while parent != base.parent and parent != parent.parent:
            ancestors.add(str(parent))
            if parent == base:
                break
            parent = parent.parent
    return {folder: songs for folder, songs in result.items() if folder not in ancestors}


def catalog(grouped):
    return [{'path': path, 'name': Path(path).name, 'song_count': len(songs),
             'duration': sum(float(song['duration']) for song in songs)}
            for path, songs in sorted(grouped.items())]


def choose_folder(grouped, usage, rng, exclude=None, *, history=None, local_usage=None):
    candidates = [path for path in grouped if path != exclude]
    if not candidates:
        raise ValueError('没有其他可用音乐文件夹')
    if local_usage is not None:
        local_least = min(local_usage.get(path, 0) for path in candidates)
        candidates = [path for path in candidates if local_usage.get(path, 0) == local_least]
    least = min(usage.get(path, 0) for path in candidates)
    candidates = [path for path in candidates if usage.get(path, 0) == least]
    history = history or {}
    oldest = min(history.get(path, {}).get('last_used', 0) for path in candidates)
    return rng.choice([path for path in candidates if history.get(path, {}).get('last_used', 0) == oldest])


def shuffled_round(songs, rng, last_id=None):
    order = list(songs)
    rng.shuffle(order)
    if len(order) > 1 and order[0]['id'] == last_id:
        # Pick uniformly among the other positions; never repeat across a round boundary.
        index = rng.randrange(1, len(order))
        order[0], order[index] = order[index], order[0]
    return order


def opening_tracks(songs):
    """Prefer trailing 01/02 before the extension; support legacy leading numbers."""
    candidates = []
    for song in songs:
        path = str(song.get('path') or song.get('name', ''))
        name = Path(path).stem
        trailing = re.search(r'(?<!\d)(\d{2})\s*$', name)
        leading = re.match(r'^\s*0([12])(?!\d)', name)
        # A trailing number is authoritative, even when it is 03 or greater.
        if trailing:
            number, priority = int(trailing[1]), 0
        elif leading:
            number, priority = int(leading[1]), 1
        else:
            continue
        if number in (1, 2):
            candidates.append((priority, path.casefold(), number, song))
    anchors = {}
    for _, _, number, song in sorted(candidates, key=lambda entry: entry[:2]):
        anchors.setdefault(number, song)
    return [anchors[number] for number in (1, 2) if number in anchors]


def allocate(songs, target, config, rng):
    """Store whole shuffled rounds, while the renderer consumes only the audible prefix."""
    if config.get('duration_basis') == 'music':
        from .musicled import choose_music
        order = choose_music(songs, config, rng)
        return order, sum(float(song['duration']) for song in order)
    target = float(target)
    if not math.isfinite(target) or target <= 0 or not songs:
        raise ValueError('文件夹没有可用音乐或目标时长无效')
    margin = float(config.get('music_margin_seconds', 3))
    if not math.isfinite(margin) or margin < 0:
        raise ValueError('音乐时长余量必须是非负有限数字')
    goal = target + min(margin, target * .005)
    result, total = [], 0.
    while total < goal:
        if not result:
            anchors = opening_tracks(songs)
            anchored_ids = {song['id'] for song in anchors}
            remaining = [song for song in songs if song['id'] not in anchored_ids]
            rng.shuffle(remaining)
            order = anchors + remaining
        else:
            order = shuffled_round(songs, rng, result[-1]['id'])
        if len(result) + len(order) > MAX_PLAYS:
            raise ValueError('音乐循环次数过多，请增加有效歌曲或缩短视频时长')
        result.extend(copy.deepcopy(order))
        total += sum(float(song['duration']) for song in order)
    return tuple(result), total


def describe(item):
    songs = item.get('music', [])
    anchors = opening_tracks(songs)
    def number(song):
        name = Path(song.get('path') or song.get('name', '')).stem
        trailing = re.search(r'(?<!\d)(\d{2})\s*$', name)
        leading = re.match(r'^\s*0([12])(?!\d)', name)
        return int(trailing[1]) if trailing else int(leading[1]) if leading else None
    found = {number(song) for song in anchors}
    missing = [f'{n:02d}' for n in (1, 2) if n not in found]
    item['music_order_warning'] = ('未找到编号 ' + '、'.join(missing) + ' 的歌曲；已使用现有歌曲继续规划，已有编号歌曲优先，其余随机排列。') if missing else ''
    item['music_unique_count'] = len({song['id'] for song in songs})
    item['music_play_count'] = len(songs)
    size = max(1, int(item.get('music_folder_song_count', item['music_unique_count'])))
    item['music_rounds'] = math.ceil(len(songs) / size)
