"""Render whole-song plans with bounded seeks and atomic, non-overwriting output."""
from __future__ import annotations

import json
import math
import re
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import threading

from .fsutil import publish
from .processwatch import run as run_watched, RenderStalled

from . import media, stickers


class MusicInputError(ValueError):
    """A render identified an invalid music input, eligible for bounded replacement."""
    def __init__(self, message, asset_ids):
        super().__init__(message)
        self.asset_ids = list(asset_ids)


def music_input_error(log, item):
    # FFmpeg identifies decoder inputs as aist#<input>:<stream>; video inputs come first.
    offset = item.get('_video_input_count', len(item['segments']))
    bad = set()
    for line in log.splitlines():
        if not any(token in line for token in ('Error', 'Invalid data', 'error code')):
            continue
        for number in re.findall(r'aist#(\d+):\d+', line):
            index = int(number) - offset
            if 0 <= index < len(item['music']):
                bad.add(index)
    if bad:
        songs = [item['music'][index] for index in sorted(bad)]
        return MusicInputError('音乐解码异常：' + '、'.join(song.get('name', song['path']) for song in songs),
                               [song['id'] for song in songs])
    return None


def _codec_candidates(requested, platform_name=sys.platform, os_name=os.name):
    if requested in {'software', 'software_fast'}:
        return ['libx264']
    if requested == 'videotoolbox':
        return ['h264_videotoolbox']
    if requested != 'auto':
        raise ValueError('编码方式无效')
    if platform_name == 'darwin':
        return ['h264_videotoolbox', 'libx264']
    if os_name == 'nt':
        return ['h264_nvenc', 'h264_qsv', 'h264_amf', 'libx264']
    return ['libx264']


def _encoding(codec, width, height, fps, requested='auto', bitrate_mbps=0):
    if bitrate_mbps:
        result = ['-c:v', codec]
        if codec == 'libx264':
            result += ['-preset', 'ultrafast' if requested == 'software_fast' else 'veryfast']
        return result + ['-b:v', str(round(float(bitrate_mbps) * 1_000_000))]
    if codec == 'libx264':
        return ['-c:v', codec, '-preset', 'ultrafast' if requested == 'software_fast' else 'veryfast',
                '-crf', '24' if requested == 'software_fast' else '22']
    return ['-c:v', codec, '-b:v', str(max(800_000, int(width * height * fps * 0.12)))]


def _filter_threads():
    return str(max(2, min(8, os.cpu_count() or 2)))


def _probe(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams',
                             '-of', 'json', str(path)], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('输出无法读取：' + result.stderr[-500:])
    return json.loads(result.stdout)


_seekable_guard = threading.Lock()
_seekable_locks = {}


def _seekable_source(segment, cache_dir, progress_callback=None):
    source = Path(segment['path'])
    if source.suffix.lower() not in {'.ts', '.mts', '.m2ts'}:
        return str(source)
    stat = source.stat()
    # Path IDs stay stable when files change, so cache versions include file metadata.
    version = dict(segment, asset_id=f"{segment['asset_id']}.{stat.st_size}-{stat.st_mtime_ns}")
    key = str(Path(cache_dir) / version['asset_id'])
    with _seekable_guard:
        lock = _seekable_locks.setdefault(key, threading.Lock())
    with lock:
        return _seekable_source_locked(version, cache_dir, progress_callback)


def _seekable_source_locked(segment, cache_dir, progress_callback=None):
    source = Path(segment['path'])
    if source.suffix.lower() not in {'.ts', '.mts', '.m2ts'}:
        return str(source)
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / (segment['asset_id'] + '.mp4')
    if not target.exists():
        partial = cache_dir / (segment['asset_id'] + '.' + uuid.uuid4().hex[:8] + '.mp4')
        try:
            command = ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(source),
                       '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy',
                       '-avoid_negative_ts', 'make_zero', str(partial)]
            with target.with_suffix('.log').open('w+', encoding='utf-8') as log:
                result = run_watched(command, log, partial, progress_callback, stage='preparing')
                if result:
                    log.seek(0)
                    raise ValueError('TS 时间轴缓存失败：' + log.read()[-1000:])
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
    return str(target)


def validate(path, expected_duration, progress_callback=None, log_path=None):
    path = Path(path)
    if not path.is_file() or not path.stat().st_size:
        raise ValueError('输出文件不存在或为空')
    info = _probe(path)
    video = next((s for s in info['streams'] if s['codec_type'] == 'video'), None)
    audio = next((s for s in info['streams'] if s['codec_type'] == 'audio'), None)
    if not video or not audio:
        raise ValueError('输出缺少视频或音频流')
    duration = float(info['format']['duration'])
    audio_duration = float(audio.get('duration', 0))
    video_duration = float(video.get('duration', 0))
    numerator, denominator = video.get('avg_frame_rate', '30/1').split('/')
    fps = float(numerator) / max(1, float(denominator))
    tolerance = max(0.08, 1 / max(1, fps) + 0.04)
    if abs(audio_duration - expected_duration) > 0.05:
        raise ValueError(f'音乐流时长 {audio_duration:.4f}s 不符合完整歌曲总长 {expected_duration:.4f}s')
    if abs(video_duration - expected_duration) > tolerance or abs(duration - expected_duration) > tolerance:
        raise ValueError(f'画面时长 {video_duration:.4f}s 不符合计划 {expected_duration:.4f}s')
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(path),
               '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-']
    import tempfile
    with (Path(log_path).open('w+', encoding='utf-8') if log_path else
          tempfile.TemporaryFile(mode='w+', encoding='utf-8')) as log:
        result = run_watched(command, log, callback=progress_callback, stage='validating', duration=duration)
        if result:
            log.seek(0)
            raise ValueError('输出不能完整解码：' + log.read()[-1000:])
    return {'path': str(path.resolve()), 'duration': duration, 'audio_duration': audio_duration,
            'video_duration': video_duration, 'size': path.stat().st_size,
            'width': video['width'], 'height': video['height'], 'fps': fps, 'valid': True}


def render_video_chunk(segment, config, target, log_path, callback, tolerant=False):
    """Publish one normalized, independently decodable video checkpoint."""
    width, height, fps = (int(config.get(k, d)) for k, d in
                          [('width', 1280), ('height', 720), ('fps', 30)])
    length = float(segment['duration'])
    partial = target.with_suffix('.partial.mov')
    source = _seekable_source(segment, Path(config.get('cache_dir', target.parent / 'cache')) / 'seekable', callback)
    inputs = ['-ss', str(segment['start']), '-t', str(length + .1), '-reinit_filter:v', '0', '-i', source]
    if tolerant:
        inputs = ['-err_detect', 'ignore_err', '-fflags', '+discardcorrupt', *inputs]
    video = (f'[0:v:0]setpts=PTS-STARTPTS,scale={width}:{height}:force_original_aspect_ratio=decrease,'
             f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},'
             f'tpad=stop_mode=clone:stop_duration={2/fps},trim=duration={length}[v]')
    filters = [video]
    mapping = ['-map', '[v]']
    if config.get('original_volume', 0):
        has_audio = any(stream['codec_type'] == 'audio' for stream in _probe(source)['streams'])
        prefix = '[0:a:0]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,apad,' if has_audio else 'anullsrc=r=48000:cl=stereo,'
        filters.append(prefix + f'atrim=duration={length}[a]')
        # PCM audio avoids AAC priming/gaps at video checkpoint boundaries.
        mapping += ['-map', '[a]', '-c:a', 'pcm_s16le']
    requested = config.get('hardware', 'auto')
    try:
        for codec in _codec_candidates(requested):
            command = ['ffmpeg', '-nostdin', '-y', '-hide_banner', '-v', 'error']
            if not tolerant:
                command += ['-xerror']
            command += ['-filter_complex_threads', _filter_threads(), *inputs,
                        '-filter_complex', ';'.join(filters), *mapping,
                        *_encoding(codec, width, height, fps, requested, config.get('video_bitrate_mbps', 0)),
                        '-pix_fmt', 'yuv420p', '-r', str(fps), '-video_track_timescale', str(fps * 1000),
                        '-movflags', '+faststart', str(partial)]
            with Path(log_path).open('w+', encoding='utf-8') as log:
                code = run_watched(command, log, partial, callback, duration=length)
                log.seek(0); error = log.read()[-1500:]
            if code:
                if any(word in error.lower() for word in ('decod', 'invalid data', 'no such file', 'matches no streams', 'does not contain any stream', 'could not find codec parameters')):
                    raise ValueError('视频片段解码失败：' + error)
                continue
            info = _probe(partial)
            stream = next((stream for stream in info['streams'] if stream['codec_type'] == 'video'), None)
            if not stream:
                raise ValueError('视频片段解码失败：没有视频流')
            actual = float(stream.get('duration', 0))
            if abs(actual - length) > 1 / fps + .02:
                raise ValueError(f'视频片段时长不足：需要{length:.3f}s，得到{actual:.3f}s')
            from .fsutil import flush_to_disk
            flush_to_disk(partial)
            partial.replace(target)
            return codec
        raise ValueError('视频片段编码失败：' + error)
    finally:
        partial.unlink(missing_ok=True)


def render(item, config, output_path, work_dir, progress_callback=None):
    width, height, fps = (int(config.get(key, default)) for key, default in
                          [('width', 1280), ('height', 720), ('fps', 30)])
    duration = float(item['duration'])
    if min(width, height, fps, duration) <= 0 or not math.isfinite(duration):
        raise ValueError('导出规格或时长无效')
    if not item.get('segments') or not item.get('music'):
        raise ValueError('方案缺少视频或音乐')
    if item.get('music_folder') and not item.get('nonstop'):
        # The rest of a shuffled round is stored for review but never opened by FFmpeg.
        total, prefix = 0., []
        for song in item['music']:
            prefix.append(song); total += float(song['duration'])
            if total >= duration: break
        item = dict(item, music=prefix)
    for asset in {a['id']: a for a in item['music']}.values():
        try:
            media.verify_asset(asset)
        except (OSError, ValueError) as exc:
            if item.get('music_folder'):
                raise MusicInputError('当前文件夹音乐无法读取：' + str(exc), [asset['id']]) from exc
            raise
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError('输出文件已存在，程序不会覆盖：' + str(output))
    work = Path(work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f'.{output.stem}-{uuid.uuid4().hex[:8]}.partial.mp4'
    log_path = work / 'ffmpeg.log'
    log_path.write_text('', encoding='utf-8')
    from . import checkpoints
    concat, video_duration, checkpoint_state = checkpoints.prepare(item, config, work, progress_callback)
    duration = min(duration, video_duration)
    inputs = ['-f', 'concat', '-safe', '1', '-i', str(concat)]
    filters = []
    count = 1
    original_volume = float(config.get('original_volume', 0))
    if original_volume:
        filters.append(f'[0:a:0]volume={original_volume}[original]')
    for index, song in enumerate(item['music']):
        inputs += ['-i', song['path']]
        edges = song.get('nonstop_edges') if item.get('nonstop') else None
        trim = f"atrim=start={edges['start']:.8f}:end={edges['end']:.8f}," if edges else ''
        filters.append(f'[{count + index}:a:0]{trim}asetpts=PTS-STARTPTS,aresample=48000,'
                       f'aformat=sample_fmts=fltp:channel_layouts=stereo[a{index}]')
    if item.get('nonstop'):
        label = 'a0'
        for index, fade in enumerate(item['nonstop']['crossfades'], 1):
            next_label = f'join{index}'
            filters.append(f'[{label}][a{index}]acrossfade=d={fade:.8f}:c1=tri:c2=tri[{next_label}]')
            label = next_label
        filters.append(f'[{label}]atrim=duration={duration:.8f},'
                       f'volume={float(config.get("music_volume", 1))},afade=t=out:'
                       f'st={max(0.,duration-.15):.8f}:d={min(.15,duration):.8f}[music]')
    else:
        music_labels = ''.join(f'[a{i}]' for i in range(len(item['music'])))
        filters.append(f'{music_labels}concat=n={len(item["music"])}:v=0:a=1,'
                       f'atrim=duration={duration:.8f},volume={float(config.get("music_volume", 1))}[music]')
    if original_volume:
        filters.append('[music][original]amix=inputs=2:duration=first:normalize=0:dropout_transition=0[aout]')
    else:
        filters.append('[music]anull[aout]')
    output_label = stickers.add_overlay_filters(inputs, filters, config.get('sticker_layers') or [],
                                                count + len(item['music']), '0:v:0', width, height, fps, duration)
    requested = config.get('hardware', 'auto')
    codecs = _codec_candidates(requested) if config.get('sticker_layers') else ['copy']
    started = time.monotonic()
    chosen = None
    try:
        for codec in codecs:
            encoding = (['-c:v', 'copy'] if codec == 'copy' else
                        _encoding(codec, width, height, fps, requested, config.get('video_bitrate_mbps', 0)))
            command = ['ffmpeg', '-nostdin', '-y', '-hide_banner', '-loglevel', 'error', '-xerror',
                       '-filter_complex_threads', _filter_threads(), *inputs, '-filter_complex', ';'.join(filters),
                       '-map', (f'[{output_label}]' if config.get('sticker_layers') else '0:v:0'), '-map', '[aout]', *encoding, '-pix_fmt', 'yuv420p',
                       '-r', str(fps), '-t', str(duration), '-c:a', 'aac', '-b:a', '192k', '-ar', '48000',
                       '-movflags', '+faststart', '-progress', 'pipe:1', '-nostats', str(temporary)]
            with log_path.open('a', encoding='utf-8') as log:
                log.write(f'\nEncoder attempt: {codec}\n'); log.flush()
                def mux_progress(value):
                    if progress_callback:
                        value = dict(value)
                        if value.get('stage') == 'rendering':
                            value.update(stage='assembling', progress=.8 + value.get('progress', 0) / .95 * .15)
                        progress_callback(value)
                result = run_watched(command, log, temporary, mux_progress, duration=duration)
            music_error = music_input_error(log_path.read_text(encoding='utf-8'), dict(item, _video_input_count=1))
            if music_error:
                raise music_error
            if result == 0:
                chosen = codec
                break
        if chosen is None:
            raise ValueError('FFmpeg 渲染失败：' + log_path.read_text(encoding='utf-8')[-1500:])
        if progress_callback:
            progress_callback({'stage': 'validating', 'progress': 0.96})
        try:
            checked = validate(str(temporary), duration, progress_callback, work / 'validation.log')
        except ValueError as exc:
            if str(exc).startswith('音乐流时长'):
                info = _probe(temporary)
                audio = next(stream for stream in info['streams'] if stream['codec_type'] == 'audio')
                actual = float(audio.get('duration', 0))
                if item.get('nonstop') and actual > .1 and actual < duration - .05:
                    shortened = temporary.with_name(temporary.stem + '-short.mp4')
                    try:
                        remux = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(temporary),
                                                '-t', str(actual), '-map', '0:v:0', '-map', '0:a:0',
                                                '-c', 'copy', '-movflags', '+faststart', str(shortened)],
                                               capture_output=True, text=True, timeout=120)
                        if remux.returncode: raise ValueError('截短成片失败：' + remux.stderr[-500:])
                        checked = validate(str(shortened), actual, progress_callback, work / 'validation-short.log')
                        duration = min(duration, actual)
                        os.replace(shortened, temporary)
                    finally:
                        shortened.unlink(missing_ok=True)
                elif actual < duration - .05:
                    raise MusicInputError('音乐实际时长不足：' + str(exc), []) from exc
                else:
                    raise
            else:
                raise
        from .tracklist import played_music
        actual_music = played_music(item, min(duration, float(checked.get('duration', duration))))
        recovery_result = dict(checked, played_music=actual_music, recovery_warnings=checkpoint_state['warnings'],
                               rendered_segments=checkpoints.rendered_segments(checkpoint_state['chunks']))
        snapshot = work / 'render-result.json'
        snapshot.write_text(json.dumps(recovery_result, ensure_ascii=False), encoding='utf-8')
        from .fsutil import flush_to_disk
        flush_to_disk(snapshot)
        publish(temporary, output)
        checked.update(path=str(output), encoder=checkpoint_state['chunks'][0].get('encoder', chosen), assembly_encoder=chosen, elapsed_seconds=round(time.monotonic() - started, 3),
                       recovery_warnings=checkpoint_state['warnings'],
                       rendered_segments=checkpoints.rendered_segments(checkpoint_state['chunks']), played_music=actual_music)
        import shutil
        shutil.rmtree(work / 'checkpoints', ignore_errors=True)
        if progress_callback:
            progress_callback({'stage': 'complete', 'progress': 1})
        return checked
    finally:
        temporary.unlink(missing_ok=True)


def overlay_existing(source, layers, output_path, work_dir, progress_callback=None, video_bitrate_mbps=0):
    """Render a review variant while stream-copying the already validated music track."""
    source = Path(source).resolve()
    info = _probe(source)
    video = next((stream for stream in info['streams'] if stream['codec_type'] == 'video'), None)
    audio = next((stream for stream in info['streams'] if stream['codec_type'] == 'audio'), None)
    if not video or not audio:
        raise ValueError('原成片缺少视频或音频流')
    duration = float(audio.get('duration') or info['format']['duration'])
    width, height = int(video['width']), int(video['height'])
    numerator, denominator = video.get('avg_frame_rate', '30/1').split('/')
    fps = max(1, round(float(numerator) / max(1, float(denominator))))
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError('输出文件已存在，程序不会覆盖：' + str(output))
    work = Path(work_dir).resolve(); work.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f'.{output.stem}-{uuid.uuid4().hex[:8]}.partial.mp4'
    inputs, filters = ['-reinit_filter:v', '0', '-i', str(source)], []
    label = stickers.add_overlay_filters(inputs, filters, layers or [], 1, '0:v', width, height, fps, duration)
    if not layers:
        # No work and no re-encode is the only way to preserve the original byte-for-byte.
        return validate(str(source), duration)
    requested = 'auto'
    codecs = _codec_candidates(requested)
    log_path = work / 'overlay-ffmpeg.log'
    log_path.write_text('', encoding='utf-8')
    try:
        for codec in codecs:
            encoding = _encoding(codec, width, height, fps, requested, video_bitrate_mbps)
            command = ['ffmpeg', '-nostdin', '-y', '-v', 'error', '-filter_complex_threads', _filter_threads(), *inputs, '-filter_complex', ';'.join(filters),
                       '-map', f'[{label}]', '-map', '0:a:0', *encoding, '-pix_fmt', 'yuv420p', '-c:a', 'copy',
                       '-movflags', '+faststart', '-progress', 'pipe:1', '-nostats', str(temporary)]
            with log_path.open('a', encoding='utf-8') as log:
                log.write(f'\nEncoder attempt: {codec}\n'); log.flush()
                result = run_watched(command, log, temporary, progress_callback, duration=duration)
            if result == 0:
                break
        else:
            raise ValueError('贴纸渲染失败：' + log_path.read_text(encoding='utf-8')[-1000:])
        if progress_callback:
            progress_callback({'stage': 'validating', 'progress': .96})
        checked = validate(str(temporary), duration, progress_callback, work / 'validation.log')
        publish(temporary, output)
        checked['path'] = str(output)
        if progress_callback:
            progress_callback({'stage': 'complete', 'progress': 1})
        return checked
    finally:
        temporary.unlink(missing_ok=True)
