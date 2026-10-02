"""Frame-aligned video checkpoints and bounded damaged-source recovery."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import random
import shutil
import time

CHUNK_SECONDS = 180
RECOVERY_SECONDS = 180
MAX_REPLACEMENTS = 2


class VideoRecoveryNeeded(RuntimeError):
    """A task keeps its checkpoints while yielding its queue slot."""


def source_version(path):
    path = Path(path).resolve()
    stat = path.stat()
    return f'{path}|{stat.st_size}|{stat.st_mtime_ns}'


def split_segments(segments, duration, fps):
    # Integer frame boundaries avoid accumulating one-frame errors at each join.
    remaining = round(duration * fps)
    result = []
    for seg in segments:
        frames = min(remaining, round(float(seg['duration']) * fps))
        offset = 0
        while offset < frames:
            count = min(round(CHUNK_SECONDS * fps), frames - offset)
            result.append(dict(seg, start=float(seg['start']) + offset / fps,
                               duration=count / fps))
            offset += count
        remaining -= frames
        if remaining <= 0:
            break
    return result


def rendered_segments(chunks):
    """Hide technical checkpoint boundaries from plans and source-use counts."""
    result = []
    for entry in chunks:
        seg = dict(entry['segment'])
        if (result and result[-1]['asset_id'] == seg['asset_id'] and result[-1]['path'] == seg['path']
                and abs(result[-1]['start'] + result[-1]['duration'] - seg['start']) < 1e-6):
            result[-1]['duration'] += seg['duration']
        else:
            result.append(seg)
    return result


def available_intervals(length, required, excluded):
    result, cursor = [], 0.
    for start, end in sorted(excluded):
        start, end = max(0., start), min(length, end)
        if start - cursor >= required - 1e-8:
            result.append((cursor, start))
        cursor = max(cursor, end)
    if length - cursor >= required - 1e-8:
        result.append((cursor, length))
    return result


def _signature(item, config):
    # Only cheap file metadata; never read all source bytes for an identity.
    settings = {k: config.get(k) for k in ('width', 'height', 'fps', 'hardware',
                                          'video_bitrate_mbps', 'original_volume')}
    data = dict(format=1, chunk_seconds=CHUNK_SECONDS, segments=item['segments'], duration=item['duration'], settings=settings)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _save(path, state):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    from .fsutil import flush_to_disk
    flush_to_disk(temporary)
    temporary.replace(path)


def prepare(item, config, work, callback=None):
    from . import renderer
    fps = int(config.get('fps', 30))
    root = Path(work) / 'checkpoints'
    if root.is_symlink():
        raise ValueError('分段缓存目录不能是符号链接')
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / 'manifest.json'
    signature = _signature(item, config)
    try:
        state = json.loads(manifest.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {}
    if state.get('signature') != signature:
        shutil.rmtree(root)
        root.mkdir()
        state = {'signature': signature, 'chunks': [], 'warnings': []}
    chunks = split_segments(item['segments'], float(item['duration']), fps)
    total = sum(s['duration'] for s in chunks)
    completed, paths = 0., []
    bad = dict(config.get('bad_video_sources', {}))
    usage = dict(config.get('video_usage', {}))
    occupied = list(config.get('occupied_video_segments', [])) + list(item['segments'])
    recovery_started = None

    def report(stage, local=0, heartbeat=False):
        if callback:
            callback({'stage': stage, 'progress': min(.8, (completed + local) / total * .8),
                      'heartbeat': heartbeat})

    def record_bad(seg):
        try:
            version = source_version(seg['path'])
        except OSError:
            return
        entry = bad.setdefault(version, {'ranges': []})
        interval = [seg['start'], seg['start'] + seg['duration']]
        if interval not in entry['ranges']:
            entry['ranges'].append(interval)
        notify = config.get('record_bad_video')
        if notify:
            notify(version, entry)

    def allowed(seg):
        try:
            entry = bad.get(source_version(seg['path']), {})
        except OSError:
            return False
        return not any(seg['start'] < end and seg['start'] + seg['duration'] > start
                       for start, end in entry.get('ranges', []))

    for index, original in enumerate(chunks):
        report('video_segments')
        target = root / f'{index:04d}.mov'
        saved = state['chunks'][index] if index < len(state['chunks']) else None
        if saved and target.is_file():
            try:
                try:
                    current = source_version(saved['segment']['path'])
                except FileNotFoundError:
                    current = saved['version']  # Complete checkpoints survive a missing input.
                stat = target.stat()
                if (current == saved['version'] and stat.st_size == saved['size']
                        and stat.st_mtime_ns == saved['mtime_ns']):
                    paths.append(target); completed += saved['duration']
                    occupied.append(saved['segment'])
                    notify = config.get('checkpoint_ready')
                    if notify: notify(state['chunks'][:index+1], state['warnings'])
                    report('video_segments')
                    continue
            except OSError:
                pass
        # Truncate the manifest at the first missing/invalid checkpoint.
        state['chunks'] = state['chunks'][:index]
        target.unlink(missing_ok=True)
        estimate = max(200_000_000, int(float(config.get('video_bitrate_mbps') or 8) * 125_000 * original['duration'] * 1.5))
        if shutil.disk_usage(root).free < estimate * max(1, int(config.get('parallel_tasks', 1))) + 200_000_000:
            raise OSError('本地分段缓存空间不足，请清理缓存后继续；已完成片段保留。')
        attempts = [(original, False), (original, True)] if allowed(original) else []
        # Try another non-overlapping interval of the same source before other files.
        candidates = list(config.get('video_candidates', item.get('video_assets', [])))
        random.SystemRandom().shuffle(candidates)
        candidates.sort(key=lambda a: (a.get('id') != original['asset_id'], usage.get(a.get('id'), 0)))
        alternatives = []
        for asset in candidates:
            groups = config.get('groups', {})
            if config.get('within_group') and groups.get(original['asset_id']) and groups.get(asset['id']) != groups[original['asset_id']]:
                continue
            length = float(asset.get('duration') or 0)
            if length < original['duration']:
                continue
            try:
                excluded = list(bad.get(source_version(asset['path']), {}).get('ranges', []))
            except OSError:
                continue
            excluded += [[other['start'], other['start'] + other['duration']] for other in occupied
                         if other['asset_id'] == asset['id']]
            if asset['id'] == original['asset_id']:
                excluded.append([original['start'], original['start'] + original['duration']])
            intervals = available_intervals(length, original['duration'], excluded)
            if intervals:
                left, right = random.SystemRandom().choice(intervals)
                # Fill from an edge so available spans are not needlessly fragmented.
                starts = list(dict.fromkeys([left, max(left, right - original['duration'])]))
                random.SystemRandom().shuffle(starts)
                for start in starts:
                    seg = dict(original, asset_id=asset['id'], path=asset['path'], start=start)
                    alternatives.append((seg, False))
                    if len(alternatives) == MAX_REPLACEMENTS: break
            if len(alternatives) == MAX_REPLACEMENTS:
                break
        attempts += alternatives
        success = None
        for number, (segment, tolerant) in enumerate(attempts):
            if recovery_started is not None and time.monotonic() - recovery_started >= RECOVERY_SECONDS:
                raise VideoRecoveryNeeded('素材异常，待继续：自动补救超过3分钟；已保留完成片段，后续任务继续。')
            stage = 'video_segments' if number == 0 and segment == original and not tolerant else 'video_recovery'
            report(stage)
            try:
                reserve = config.get('reserve_video_segment')
                if segment != original and reserve and not reserve(segment):
                    continue
                version = source_version(segment['path'])
                def progress(value):
                    if recovery_started is not None and time.monotonic() - recovery_started >= RECOVERY_SECONDS:
                        raise VideoRecoveryNeeded('素材补救超时，已保留完成片段；可手动继续。')
                    report(stage, min(segment['duration'], value.get('progress', 0) / .95 * segment['duration']),
                           value.get('heartbeat', False))
                encoder = renderer.render_video_chunk(segment, config, target, root / f'{index:04d}-{number}.log',
                                            progress, tolerant)
                if source_version(segment['path']) != version:
                    raise ValueError('源文件在渲染期间发生变化')
                success = segment
                break
            except (VideoRecoveryNeeded, renderer.RenderStalled, InterruptedError):
                raise
            except (ValueError, OSError) as exc:
                target.unlink(missing_ok=True)
                # Disk/encoder failures are not evidence of damaged source media.
                text = str(exc)
                if not any(word in text.lower() for word in ('decod', 'invalid data', '片段时长', '素材不存在', 'no such file')):
                    raise
                record_bad(segment)
                recovery_started = recovery_started or time.monotonic()
                state['warnings'].append(f'片段{index+1}补救：{text[-500:]}')
                _save(manifest, state)
        if success is None:
            if not paths:
                raise VideoRecoveryNeeded('素材异常，待继续：没有可用替代视频；后续任务继续。')
            state['warnings'].append('没有足够可用素材，保留已完成片段并缩短成片。')
            break
        stat = target.stat()
        record = {'segment': success, 'duration': original['duration'], 'version': version, 'encoder': encoder,
                  'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
        state['chunks'].append(record)
        _save(manifest, state)
        if success != original or tolerant:
            state['warnings'].append(f'片段{index+1}已自动' + ('跳过损坏画面' if tolerant else '替换视频'))
            _save(manifest, state)
        occupied.append(success)
        usage[success['asset_id']] = usage.get(success['asset_id'], 0) + 1
        notify = config.get('checkpoint_ready')
        if notify:
            notify(state['chunks'], state['warnings'])
        paths.append(target); completed += original['duration']
        recovery_started = None
    if not paths:
        raise VideoRecoveryNeeded('没有可用视频片段，后续任务继续。')
    concat = root / 'concat.txt'
    concat.write_text(''.join("file '" + path.name + "'\n" for path in paths), encoding='utf-8')
    return concat, completed, state
