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


def video_plan_key(segments, fps=30):
    """Compare rendered timelines at output-frame precision."""
    rate = max(1, int(fps))
    return tuple((part['asset_id'], round(float(part.get('start', 0)) * rate),
                  round(float(part.get('duration', 0)) * rate)) for part in segments)


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


def allocate_music(music, target, config, rng, usage=None):
    """Choose a feasible song set, exhausting each usage round before the next."""
    margin = min(float(config.get('music_margin_seconds', 0)), max(0, float(target)) * 0.005)
    if not math.isfinite(margin) or margin < 0:
        raise ValueError('音乐时长余量必须是非负有限数字')
    target += margin
    usage = defaultdict(int, usage or {})
    minimum = max(1, int(config.get('min_songs', 1)))
    maximum = min(len(music), int(config.get('max_songs', len(music))))
    if maximum < minimum:
        raise ValueError('所选歌曲数少于每条任务的最少首数')
    levels = sorted({usage[song['id']] for song in music})
    counts = list(range(minimum, maximum + 1))
    rng.shuffle(counts)

    def best_remaining(available, slots):
        if len(available) < slots:
            return -math.inf
        return sum(sorted((float(song['duration']) for song in available), reverse=True)[:slots])

    for ceiling in levels:
        for count in counts:
            remaining = [song for song in music if usage[song['id']] <= ceiling]
            if best_remaining(remaining, count) + 1e-7 < target:
                continue
            chosen = []
            total = 0.0
            for position in range(count):
                viable = []
                slots = count - position - 1
                by_duration = sorted(remaining, key=lambda entry: float(entry['duration']), reverse=True)
                best = sum(float(entry['duration']) for entry in by_duration[:slots])
                top_ids = {entry['id'] for entry in by_duration[:slots]}
                next_duration = float(by_duration[slots]['duration']) if slots < len(by_duration) else 0.0
                for song in remaining:
                    if len(remaining) - 1 < slots:
                        continue
                    rest_best = best - float(song['duration']) + next_duration if song['id'] in top_ids else best
                    if total + float(song['duration']) + rest_best + 1e-7 >= target:
                        viable.append(song)
                if not viable:
                    break
                min_level = min(usage[song['id']] for song in viable)
                viable = [song for song in viable if usage[song['id']] == min_level]
                song = rng.choice(viable)
                chosen.append(song)
                remaining.remove(song)
                total += float(song['duration'])
            if len(chosen) == count and total + 1e-7 >= target:
                # Choose the set for feasibility and batch usage, then choose playback order.
                rng.shuffle(chosen)
                return tuple(chosen), total
    raise ValueError(f'本轮未用歌曲在 {minimum}～{maximum} 首内无法覆盖目标视频 {target:.1f} 秒；请增加曲目或调整时长')


def _free_starts(source_duration, length, occupied):
    """Return start ranges whose whole segment is outside occupied intervals."""
    ranges = []
    cursor = 0.0
    for left, right in sorted(occupied):
        left, right = max(0.0, left), min(source_duration, right)
        if left - cursor + 1e-7 >= length:
            ranges.append((cursor, max(cursor, left - length)))
        cursor = max(cursor, right)
    if source_duration - cursor + 1e-7 >= length:
        ranges.append((cursor, max(cursor, source_duration - length)))
    return ranges


def _respect_start_gap(ranges, old_starts, gap):
    if gap <= 0:
        return ranges
    for old in old_starts:
        kept = []
        for lower, upper in ranges:
            if lower <= old - gap:
                kept.append((lower, min(upper, old - gap)))
            if upper >= old + gap:
                kept.append((max(lower, old + gap), upper))
        ranges = [(lower, upper) for lower, upper in kept if lower <= upper + 1e-7]
    return ranges


def choose_video_segment(videos, length, occupied, usage, rng, *, allow_overlap=True,
                         excluded_ids=(), protected=None, placement='random', min_start_gap=0):
    """Use the least-used eligible source, and prefer unseen time ranges in every round."""
    excluded_ids = set(excluded_ids)
    for permit_reused_time in (False, True) if allow_overlap else (False,):
        choices = []
        for video in videos:
            if video['id'] in excluded_ids or float(video['duration']) + 1e-7 < length:
                continue
            reservations = ((protected or {}).get(video['id'], []) if permit_reused_time
                            else occupied.get(video['id'], []))
            starts = _free_starts(float(video['duration']), length, reservations)
            starts = _respect_start_gap(starts, (left for left, _ in occupied.get(video['id'], [])),
                                        min_start_gap)
            for lower, upper in starts:
                if placement == 'grid' and min_start_gap > 0:
                    first = math.ceil((lower - 1e-7) / min_start_gap)
                    last = math.floor((upper + 1e-7) / min_start_gap)
                    step = max(1, math.ceil(max(0, last - first + 1) / 2048))
                    choices.extend((video, point * min_start_gap, point * min_start_gap)
                                   for point in range(first, last + 1, step))
                else:
                    choices.append((video, lower, upper))
        if not choices:
            continue
        level = min(usage[video['id']] for video, _, _ in choices)
        video, lower, upper = rng.choice([choice for choice in choices if usage[choice[0]['id']] == level])
        if placement == 'spread':
            old = [left for left, _ in occupied.get(video['id'], [])]
            endpoints = [lower, upper]
            rng.shuffle(endpoints)
            start = max(endpoints, key=lambda point: min((abs(point - value) for value in old),
                                                         default=0.0))
        else:
            start = lower if placement == 'start' else upper if placement == 'end' else rng.uniform(lower, upper)
        start = round(start, 6)
        return {'asset_id': video['id'], 'path': video['path'], 'start': start, 'duration': length}
    return None


def _single_segments(videos, duration, state, config, rng, usage):
    overlap = bool(config.get('allow_overlap', True))
    segment = choose_video_segment(videos, duration, state, usage, rng,
                                   allow_overlap=overlap,
                                   min_start_gap=float(config.get('start_gap', 1)) if overlap else 0,
                                   placement='grid' if overlap else 'start')
    return [segment] if segment else None


def _multi_segments(videos: list[dict[str, Any]], total: float, config: dict[str, Any], rng: random.Random,
                    usage: dict[str, int], state: dict[str, list[tuple[float, float]]]) -> list[dict[str, Any]] | None:
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
    for permit_overlap in (False, True) if config.get('allow_overlap', True) else (False,):
        for count in counts:
            for pool in pools:
                for attempt in range(6):
                    lengths = _allocate(total, count, lo, hi, rng)
                    if not lengths:
                        continue
                    used = defaultdict(list)
                    local_usage = defaultdict(int)
                    segments = []
                    for index, length in enumerate(lengths):
                        occupied = {video['id']: state[video['id']] + used[video['id']] for video in pool}
                        source_usage = defaultdict(int, {video['id']: usage[video['id']] + local_usage[video['id']]
                                                         for video in pool})
                        excluded = {segments[0]['asset_id']} if index == 1 else set()
                        segment = choose_video_segment(pool, length, occupied, source_usage, rng,
                                                       allow_overlap=permit_overlap,
                                                       excluded_ids=excluded, protected=used,
                                                       placement='random' if attempt < 4 else 'start' if attempt == 4 else 'end')
                        if segment is None:
                            break
                        used[segment['asset_id']].append((segment['start'], segment['start'] + length))
                        local_usage[segment['asset_id']] += 1
                        segments.append(segment)
                    if len(segments) == count:
                        return segments
    return None


def annotate_batch(items):
    """Derive display counts from the current plan after every edit or refresh."""
    video_usage = defaultdict(int)
    music_usage = defaultdict(int)
    for item in items:
        for segment in item.get('segments', []):
            video_usage[segment['asset_id']] += 1
        for song in item.get('music', []):
            music_usage[song['id']] += 1
    for item in items:
        for song in item.get('music', []):
            song['batch_use_count'] = music_usage[song['id']]
        for segment in item.get('segments', []):
            segment['source_use_count'] = video_usage[segment['asset_id']]
            segment['batch_use_count'] = 1 + sum(
                1 for other in items if other is not item and any(
                    candidate['asset_id'] == segment['asset_id'] and
                    min(float(candidate.get('start', 0)) + float(candidate.get('duration', 0)),
                        float(segment.get('start', 0)) + float(segment.get('duration', 0))) -
                    max(float(candidate.get('start', 0)), float(segment.get('start', 0))) > 1e-6
                    for candidate in other.get('segments', [])))
    overlaps = []
    overlap_pairs = 0
    for left, right in itertools.combinations(items, 2):
        shared = 0.0
        for a in left.get('segments', []):
            for b in right.get('segments', []):
                if a['asset_id'] == b['asset_id']:
                    shared += max(0.0, min(a.get('start', 0) + a.get('duration', 0),
                                           b.get('start', 0) + b.get('duration', 0))
                                  - max(a.get('start', 0), b.get('start', 0)))
        overlap_pairs += shared > 1e-6
        overlaps.append(shared / min(left['duration'], right['duration']))
    return {'video_usage': dict(video_usage), 'music_usage': dict(music_usage),
            'segment_overlap_pairs': overlap_pairs,
            'max_overlap_ratio': max(overlaps, default=0.0)}


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
    orders, bound_reached, nearest = _collect_music_orders(music, config, search_limit) if config.get('music_mode', 'pool') == 'fixed' else ([], False, None)
    previous_items = previous_items or []
    def music_key(item):
        return item.get('music_fingerprint') or _fingerprint(tuple(song['id'] for song in item.get('music', [])))
    def video_key(item):
        return _fingerprint(video_plan_key(item.get('segments', []), config.get('fps', 30)))
    prior_music = {music_key(item) for item in previous_items}
    prior_video = {video_key(item) for item in previous_items}
    orders = [order for order in orders if _fingerprint(tuple(song['id'] for song in order)) not in prior_music]
    if config.get('music_mode', 'pool') == 'fixed' and len(orders) < count and not allow_partial:
        suffix = "（搜索达到上限，可能仍有更多方案）" if bound_reached else ""
        hint = f"；最接近目标时长相差约 {nearest:.2f} 秒" if nearest is not None else ""
        raise ValueError(f"符合完整歌曲与时长条件的不同音乐顺序只有 {len(orders)} 个，少于所需 {count} 个{hint}{suffix}")
    rng.shuffle(orders)
    state = defaultdict(list)
    for old in previous_items:
        for segment in old.get('segments', []):
            state[segment['asset_id']].append((float(segment['start']), float(segment['start']) + float(segment['duration'])))
    items, used_video = [], set(prior_video)
    batch_video_usage = defaultdict(int)
    for old in previous_items:
        for segment in old.get('segments', []):
            batch_video_usage[segment['asset_id']] += 1
    batch_music_usage = defaultdict(int)
    for old in previous_items:
        for song in old.get('music', []):
            batch_music_usage[song['id']] += 1
    if config.get('music_mode', 'pool') == 'pool':
        orders = [None] * count
    for order in orders:
        if order is None:
            lower = float(config.get('min_duration', min(float(song['duration']) for song in music)))
            upper = float(config.get('max_duration', lower))
            candidates = ([rng.uniform(lower, upper) for _ in range(6)] + [lower]
                          if math.isfinite(upper) else [max(lower, min(float(song['duration']) for song in music))])
            last_error = None
            for target in candidates:
                try:
                    order, music_total = allocate_music(music, target, config, rng, batch_music_usage)
                    break
                except ValueError as exc:
                    last_error = exc
            else:
                if allow_partial:
                    break
                raise last_error
        else:
            target = music_total = sum(float(song["duration"]) for song in order)
        segments = (_single_segments(videos, target, state, config, rng, batch_video_usage) if mode == "single"
                    else _multi_segments(videos, target, config, rng, batch_video_usage, state))
        if not segments:
            continue
        video_key = video_plan_key(segments, config.get('fps', 30))
        music_key = tuple(song["id"] for song in order)
        video_fingerprint = _fingerprint(video_key)
        if video_fingerprint in used_video:
            continue
        used_video.add(video_fingerprint)
        segment_assets = {video["id"]: video for video in videos}
        items.append({"id": _fingerprint((video_key, music_key)), "index": len(items), "duration": target,
                      "segments": segments, "music": list(order),
                      "music_total_duration": music_total,
                      "video_assets": [segment_assets[p["asset_id"]] for p in segments],
                      "video_fingerprint": video_fingerprint, "music_fingerprint": _fingerprint(music_key)})
        for song in order:
            batch_music_usage[song['id']] += 1
        for segment in segments:
            batch_video_usage[segment['asset_id']] += 1
            state[segment['asset_id']].append((float(segment['start']),
                                               float(segment['start']) + float(segment['duration'])))
        if len(items) == count:
            break
    if len(items) < count and not allow_partial:
        raise ValueError(f"只能生成 {len(items)} 条不同视频方案；请允许重叠、增加素材或缩短时长")
    statistics = annotate_batch(items)
    warnings = (["音乐组合搜索达到上限；更多可行方案可能存在"] if bound_reached else [])
    if config.get('music_mode', 'pool') == 'fixed' and len(items) > 1:
        warnings.append('固定歌曲集合模式会在多条任务中重复使用同一批歌曲；整批轮转仅适用于候选池组合')
    if (config.get('music_mode', 'pool') == 'pool' and
            any(value > 1 for value in statistics['music_usage'].values()) and
            len(statistics['music_usage']) < len(music)):
        warnings.append('本轮剩余未用歌曲无法在最多首数内覆盖目标时长，已使用下一轮歌曲；可增加最多首数或降低成片时长')
    if filtered_video_count:
        warnings.append(f'源视频须大于 {source_minutes:g} 分钟，已排除 {filtered_video_count} 条不符合时长的视频')
    if allow_partial and len(items) < count:
        warnings.append(f"仅生成 {len(items)}/{count} 条；可用音乐顺序或视频方案不足")
    return {"items": items, "stats": {"count": len(items), "requested_count": count, "remaining": count - len(items),
            **statistics,
            "unique_music_orders": len({item["music_fingerprint"] for item in items}),
            "unique_video_plans": len({item["video_fingerprint"] for item in items})}, "warnings": warnings}
