"""Deterministic, constraint-first plans for complete-song batch edits."""
from __future__ import annotations

import hashlib
import itertools
import math
import random
from collections import defaultdict
from typing import Any


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _selected(items: list[dict[str, Any]], ids: list[str] | None, label: str) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in items}
    wanted = list(by_id) if ids is None else list(dict.fromkeys(ids))
    chosen = [by_id[value] for value in wanted if value in by_id]
    if ids is not None and len(chosen) != len(wanted):
        missing = [value for value in ids if value not in by_id]
        raise ValueError(f"所选{label}不存在：{', '.join(missing[:3])}")
    return chosen


def _collect_music_orders(music: list[dict[str, Any]], config: dict[str, Any], limit: int) -> tuple[list[tuple[dict[str, Any], ...]], bool, float | None]:
    """Bound *examined* permutations; retain nearest duration for actionable errors."""
    mode = config.get("music_mode", "pool")
    first = set(config.get("first_song_ids", []))
    lower, upper = float(config.get("min_duration", 0)), float(config.get("max_duration", math.inf))
    lengths = [len(music)] if mode == "fixed" else range(max(1, int(config.get("min_songs", 1))), min(len(music), int(config.get("max_songs", len(music)))) + 1)
    found, examined, nearest = [], 0, None
    target = (lower + upper) / 2 if math.isfinite(upper) else lower
    if mode == 'fixed':
        fixed_duration = sum(float(song['duration']) for song in music)
        if fixed_duration < lower - 1e-7 or fixed_duration > upper + 1e-7:
            return [], False, abs(fixed_duration - target)
    for length in lengths:
        for order in itertools.permutations(music, length):
            examined += 1
            if examined > limit:
                return found, True, nearest
            if first and order[0]["id"] not in first:
                continue
            total = sum(float(song["duration"]) for song in order)
            distance = abs(total - target)
            nearest = distance if nearest is None else min(nearest, distance)
            if lower - 1e-7 <= total <= upper + 1e-7:
                found.append(order)
    return found, False, nearest


def _allocate(total: float, count: int, lo: float, hi: float, rng: random.Random) -> list[float] | None:
    if count * lo > total + 1e-6 or count * hi < total - 1e-6:
        return None
    result = [lo] * count
    remaining = total - count * lo
    choices = list(range(count))
    while remaining > 1e-7:
        rng.shuffle(choices)
        progressed = False
        for index in choices:
            add = min(hi - result[index], remaining)
            if add > 1e-7:
                # Random shares make otherwise identical source sequences genuinely different.
                add = min(add, max(0.001, remaining * rng.uniform(0.18, 0.72)))
                result[index] += add
                remaining -= add
                progressed = True
                if remaining <= 1e-7:
                    break
        if not progressed:
            return None
    result[-1] += total - sum(result)
    return result


def _single_segments(videos, duration, state, config, rng):
    candidates = [video for video in videos if float(video["duration"]) + 1e-6 >= duration]
    if not candidates:
        return None
    rng.shuffle(candidates)
    candidates.sort(key=lambda video: len(state[video['id']]))
    overlap = bool(config.get("allow_overlap", True))
    gap = max(0.001, float(config.get("start_gap", 1)))
    for video in candidates:
        available = float(video["duration"]) - duration
        previous = state[video['id']]
        if overlap:
            # Find starts in the remaining gaps, and prefer ones furthest from prior starts.
            options = [0.0, available]
            for old_start, _ in previous:
                options.extend([old_start - gap, old_start + gap])
            options = [x for x in options if 0 <= x <= available and
                       all(abs(x - old) >= gap - 1e-6 for old, _ in previous)]
            if not options:
                continue
            rng.shuffle(options)
            start = max(options, key=lambda x: min((abs(x - old) for old, _ in previous), default=0))
        else:
            start = max((end for _, end in previous), default=0)
            if start + duration > float(video["duration"]) + 1e-6:
                continue
        previous.append((start, start + duration))
        return [{"asset_id": video["id"], "path": video["path"], "start": round(start, 6), "duration": duration}]
    return None


def _multi_segments(videos: list[dict[str, Any]], total: float, config: dict[str, Any], rng: random.Random) -> list[dict[str, Any]] | None:
    lo, hi = float(config.get("segment_min", 30)), float(config.get("segment_max", 120))
    if lo <= 0 or hi < lo:
        raise ValueError("片段最短/最长时长设置无效")
    minimum_count = max(2, math.ceil(total / hi))
    if minimum_count > 128:
        raise ValueError('当前时长至少需要超过 128 个片段；第一版每条最多 128 段，请增加单段最长时长')
    counts = range(minimum_count, min(128, math.floor(total / lo)) + 1)
    groups = config.get('groups', {})
    if config.get('within_group', False):
        buckets = defaultdict(list)
        for video in videos:
            group = groups.get(video['id'], '')
            if group:
                buckets[group].append(video)
        pools = [pool for pool in buckets.values() if len(pool) >= 2]
        if not pools:
            raise ValueError('组内拼接需要至少两个视频填写相同的分组名称')
    else:
        pools = [videos]
    pools = [pool for pool in pools if sum(float(v['duration']) for v in pool) + 1e-6 >= total]
    rng.shuffle(pools)
    for count in counts:
        for pool in pools:
            for attempt in range(3):
                lengths = _allocate(total, count, lo, hi, rng)
                if not lengths:
                    continue
                used = defaultdict(list)
                segments = []
                for index, length in enumerate(lengths):
                    choices = []
                    for video in pool:
                        if index == 1 and video['id'] == segments[0]['asset_id']:
                            continue
                        cursor = 0.0
                        intervals = sorted(used[video['id']]) + [(float(video['duration']), float(video['duration']))]
                        for left, right in intervals:
                            if left - cursor + 1e-7 >= length:
                                choices.append((video, cursor, max(cursor, left - length)))
                            cursor = max(cursor, right)
                    if not choices:
                        break
                    video, lower, upper = rng.choice(choices)
                    # Keep allocations packed on retries so random gaps cannot block a feasible fit.
                    start = rng.uniform(lower, upper) if attempt == 0 else (lower if attempt == 1 else upper)
                    used[video['id']].append((start, start + length))
                    segments.append({'asset_id': video['id'], 'path': video['path'],
                                     'start': start, 'duration': length})
                if len(segments) == count:
                    return segments
    return None


def plan(videos: list[dict[str, Any]], music: list[dict[str, Any]], config: dict[str, Any], *,
         allow_partial: bool = False, previous_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build unique ordered music and video plans; does not render or mutate sources."""
    count = int(config.get("count", 1))
    try:
        source_minutes = float(config.get('min_source_duration_minutes', 0))
    except (ValueError, TypeError):
        raise ValueError('源视频时长门槛必须是非负有限数字（分钟）')
    if not math.isfinite(source_minutes) or source_minutes < 0:
        raise ValueError('源视频时长门槛必须是非负有限数字（分钟）')
    for key in ("min_duration", "max_duration", "segment_min", "segment_max", "start_gap"):
        if key in config and not math.isfinite(float(config[key])):
            raise ValueError(f"{key} 必须是有限数字")
    if float(config.get("min_duration", 0)) < 0 or float(config.get("max_duration", math.inf)) < float(config.get("min_duration", 0)):
        raise ValueError("成片最短/最长时长设置无效")
    if count > 500:
        raise ValueError('每批最多 500 条，请分批生成')
    if count < 1:
        raise ValueError("请至少选择一个视频、一首音乐，并将数量设为正数")
    if config.get('music_mode', 'pool') not in {'fixed', 'pool'}:
        raise ValueError('音乐模式必须为 fixed 或 pool')
    default_max_songs = len(music) if music else 1
    if int(config.get('min_songs', 1)) < 1 or int(config.get('max_songs', default_max_songs)) < int(config.get('min_songs', 1)):
        raise ValueError('每条最少/最多歌曲数设置无效')
    if float(config.get('start_gap', 1)) < 0:
        raise ValueError('起点间隔不能为负数')
    mode = config.get("mode", "single")
    if mode not in {"single", "multi"}:
        raise ValueError("mode 只能是 single 或 multi")
    videos = _selected(videos, config.get("video_ids"), "视频")
    selected_video_count = len(videos)
    if source_minutes > 0:
        videos = [video for video in videos if float(video['duration']) > source_minutes * 60]
    filtered_video_count = selected_video_count - len(videos)
    music = _selected(music, config.get("music_ids"), "音乐")
    if not set(config.get('first_song_ids', [])).issubset({song['id'] for song in music}):
        raise ValueError('开头候选歌曲必须同时勾选为参与剪辑的歌曲')
    if not videos or not music:
        if not videos and source_minutes > 0:
            message = f'没有时长大于 {source_minutes:g} 分钟的可用源视频（已排除 {filtered_video_count} 条）；请降低门槛或添加更长的视频'
            if not allow_partial:
                raise ValueError(message)
            return {'items': [], 'stats': {'count': 0, 'requested_count': count, 'remaining': count,
                    'filtered_video_count': filtered_video_count, 'video_usage': {}, 'max_overlap_ratio': 0.0},
                    'warnings': [message]}
        if not allow_partial:
            raise ValueError("请至少选择一个视频、一首音乐，并将数量设为正数")
        missing = "视频和音乐" if not videos and not music else ("视频" if not videos else "音乐")
        return {"items": [], "stats": {"count": 0, "requested_count": count, "remaining": count,
                "video_usage": {}, "max_overlap_ratio": 0.0}, "warnings": [f"没有可用{missing}，本次未生成方案"]}
    rng = random.Random(config.get("seed", 0))
    # Avoid materializing factorially many permutations.  The limit is deliberately
    # visible to callers: this is a bounded search, not a false impossibility proof.
    search_limit = max(count, min(50000, int(config.get("search_limit", 50000))))
    orders, bound_reached, nearest = _collect_music_orders(music, config, search_limit)
    previous_items = previous_items or []
    def music_key(item):
        return item.get('music_fingerprint') or _fingerprint(tuple(song['id'] for song in item.get('music', [])))
    def video_key(item):
        return item.get('video_fingerprint') or _fingerprint(tuple((part['asset_id'], part['start'], round(part['duration'], 6)) for part in item.get('segments', [])))
    prior_music = {music_key(item) for item in previous_items}
    prior_video = {video_key(item) for item in previous_items}
    orders = [order for order in orders if _fingerprint(tuple(song['id'] for song in order)) not in prior_music]
    if len(orders) < count and not allow_partial:
        suffix = "（搜索达到上限，可能仍有更多方案）" if bound_reached else ""
        hint = f"；最接近目标时长相差约 {nearest:.2f} 秒" if nearest is not None else ""
        raise ValueError(f"符合完整歌曲与时长条件的不同音乐顺序只有 {len(orders)} 个，少于所需 {count} 个{hint}{suffix}")
    rng.shuffle(orders)
    state = defaultdict(list)
    if mode == 'single':
        for old in previous_items:
            for segment in old.get('segments', []):
                state[segment['asset_id']].append((float(segment['start']), float(segment['start']) + float(segment['duration'])))
    items, used_video = [], set(prior_video)
    for order in orders:
        total = sum(float(song["duration"]) for song in order)
        segments = (_single_segments(videos, total, state, config, rng) if mode == "single"
                    else _multi_segments(videos, total, config, rng))
        if not segments:
            continue
        video_key = tuple((part["asset_id"], part["start"], round(part["duration"], 6)) for part in segments)
        music_key = tuple(song["id"] for song in order)
        video_fingerprint = _fingerprint(video_key)
        if video_fingerprint in used_video:
            continue
        used_video.add(video_fingerprint)
        segment_assets = {video["id"]: video for video in videos}
        items.append({"id": _fingerprint((video_key, music_key)), "index": len(items), "duration": total,
                      "segments": segments, "music": list(order),
                      "video_assets": [segment_assets[p["asset_id"]] for p in segments],
                      "video_fingerprint": video_fingerprint, "music_fingerprint": _fingerprint(music_key)})
        if len(items) == count:
            break
    if len(items) < count and not allow_partial:
        raise ValueError(f"只能生成 {len(items)} 条不同视频方案；请允许重叠、增加素材或缩短时长")
    usage = defaultdict(int)
    for item in items:
        for segment in item["segments"]:
            usage[segment["asset_id"]] += 1
    overlaps = []
    for left, right in itertools.combinations(items, 2):
        shared = 0.0
        for a in left["segments"]:
            for b in right["segments"]:
                if a["asset_id"] == b["asset_id"]:
                    shared += max(0.0, min(a["start"] + a["duration"], b["start"] + b["duration"])
                                  - max(a["start"], b["start"]))
        overlaps.append(shared / min(left["duration"], right["duration"]))
    warnings = (["音乐组合搜索达到上限；更多可行方案可能存在"] if bound_reached else [])
    if filtered_video_count:
        warnings.append(f'源视频须大于 {source_minutes:g} 分钟，已排除 {filtered_video_count} 条不符合时长的视频')
    if allow_partial and len(items) < count:
        warnings.append(f"仅生成 {len(items)}/{count} 条；可用音乐顺序或视频方案不足")
    return {"items": items, "stats": {"count": len(items), "requested_count": count, "remaining": count - len(items), "video_usage": dict(usage),
            "max_overlap_ratio": max(overlaps, default=0.0)}, "warnings": warnings}
