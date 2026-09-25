"""Render whole-song plans with bounded seeks and atomic, non-overwriting output."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .fsutil import publish

from . import media, stickers


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


def _encoding(codec, width, height, fps, requested='auto'):
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


def _seekable_source(segment, cache_dir):
    source = Path(segment['path'])
    if source.suffix.lower() not in {'.ts', '.mts', '.m2ts'}:
        return str(source)
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / (segment['asset_id'] + '.mp4')
    if not target.exists():
        partial = cache_dir / (segment['asset_id'] + '.' + uuid.uuid4().hex[:8] + '.mp4')
        try:
            result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(source),
                                     '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy',
                                     '-avoid_negative_ts', 'make_zero', str(partial)],
                                    capture_output=True, text=True)
            if result.returncode:
                raise ValueError('TS 时间轴缓存失败：' + result.stderr[-1000:])
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
    return str(target)


def validate(path, expected_duration):
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
    decoded = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(path),
                              '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'],
                             capture_output=True, text=True)
    if decoded.returncode:
        raise ValueError('输出不能完整解码：' + decoded.stderr[-1000:])
    return {'path': str(path.resolve()), 'duration': duration, 'audio_duration': audio_duration,
            'video_duration': video_duration, 'size': path.stat().st_size,
            'width': video['width'], 'height': video['height'], 'fps': fps, 'valid': True}


def render(item, config, output_path, work_dir, progress_callback=None):
    width, height, fps = (int(config.get(key, default)) for key, default in
                          [('width', 1280), ('height', 720), ('fps', 30)])
    duration = float(item['duration'])
    if min(width, height, fps, duration) <= 0 or not math.isfinite(duration):
        raise ValueError('导出规格或时长无效')
    if not item.get('segments') or not item.get('music'):
        raise ValueError('方案缺少视频或音乐')
    for asset in {a['id']: a for a in item['music'] + item.get('video_assets', [])}.values():
        media.verify_asset(asset)
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError('输出文件已存在，程序不会覆盖：' + str(output))
    work = Path(work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f'.{output.stem}-{uuid.uuid4().hex[:8]}.partial.mp4'
    log_path = work / 'ffmpeg.log'
    inputs, filters = [], []
    sources = {asset['id']: asset for asset in item.get('video_assets', [])}
    count = len(item['segments'])
    original_volume = float(config.get('original_volume', 0))
    for index, segment in enumerate(item['segments']):
        length = float(segment['duration'])
        source_path = _seekable_source(segment, Path(config.get('cache_dir', work / 'cache')) / 'seekable')
        inputs += ['-ss', f"{float(segment['start']):.6f}", '-t', f'{length + 0.1:.6f}', '-i', source_path]
        filters.append(f'[{index}:v:0]setpts=PTS-STARTPTS,trim=duration={length:.8f},'
                       f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
                       f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{index}]')
        if original_volume:
            has_audio = sources.get(segment['asset_id'], {}).get('has_audio')
            if has_audio is None:
                has_audio = any(s['codec_type'] == 'audio' for s in _probe(segment['path'])['streams'])
            if has_audio:
                filters.append(f'[{index}:a:0]asetpts=PTS-STARTPTS,atrim=duration={length:.8f},'
                               f'aresample=48000,aformat=channel_layouts=stereo,apad,'
                               f'atrim=duration={length:.8f},volume={original_volume}[o{index}]')
            else:
                filters.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={length:.8f}[o{index}]')
    video_labels = ''.join(f'[v{i}]' for i in range(count))
    filters.append(f'{video_labels}concat=n={count}:v=1:a=0,'
                   f'tpad=stop_mode=clone:stop_duration={count / fps + 0.1:.6f},'
                   f'trim=duration={duration:.8f},setpts=PTS-STARTPTS[vout]')
    for index, song in enumerate(item['music']):
        inputs += ['-i', song['path']]
        filters.append(f'[{count + index}:a:0]asetpts=PTS-STARTPTS,aresample=48000,'
                       f'aformat=sample_fmts=fltp:channel_layouts=stereo[a{index}]')
    music_labels = ''.join(f'[a{i}]' for i in range(len(item['music'])))
    filters.append(f'{music_labels}concat=n={len(item["music"])}:v=0:a=1,'
                   f'volume={float(config.get("music_volume", 1))}[music]')
    if original_volume:
        original_labels = ''.join(f'[o{i}]' for i in range(count))
        filters.append(f'{original_labels}concat=n={count}:v=0:a=1[original]')
        filters.append('[music][original]amix=inputs=2:duration=first:normalize=0:dropout_transition=0[aout]')
    else:
        filters.append('[music]anull[aout]')
    output_label = stickers.add_overlay_filters(inputs, filters, config.get('sticker_layers') or [],
                                                count + len(item['music']), 'vout', width, height, fps, duration)
    requested = config.get('hardware', 'auto')
    codecs = _codec_candidates(requested)
    started = time.monotonic()
    chosen = None
    try:
        for codec in codecs:
            encoding = _encoding(codec, width, height, fps, requested)
            command = ['ffmpeg', '-nostdin', '-y', '-hide_banner', '-loglevel', 'error',
                       '-filter_complex_threads', _filter_threads(), *inputs, '-filter_complex', ';'.join(filters),
                       '-map', f'[{output_label}]', '-map', '[aout]', *encoding, '-pix_fmt', 'yuv420p',
                       '-r', str(fps), '-c:a', 'aac', '-b:a', '192k', '-ar', '48000',
                       '-movflags', '+faststart', '-progress', 'pipe:1', '-nostats', str(temporary)]
            with log_path.open('w', encoding='utf-8') as log:
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log, text=True)
                try:
                    for line in process.stdout:
                        key, _, value = line.strip().partition('=')
                        if key == 'out_time_us' and value.isdigit() and progress_callback:
                            progress_callback({'stage': 'rendering', 'progress': min(0.95, int(value) / 1e6 / duration * 0.95)})
                    process.stdout.close()
                    result = process.wait()
                except BaseException:
                    process.terminate()
                    process.wait()
                    process.stdout.close()
                    raise
            if result == 0:
                chosen = codec
                break
        if chosen is None:
            raise ValueError('FFmpeg 渲染失败：' + log_path.read_text(encoding='utf-8')[-1500:])
        if progress_callback:
            progress_callback({'stage': 'validating', 'progress': 0.96})
        checked = validate(str(temporary), duration)
        publish(temporary, output)
        checked.update(path=str(output), encoder=chosen, elapsed_seconds=round(time.monotonic() - started, 3))
        if progress_callback:
            progress_callback({'stage': 'complete', 'progress': 1})
        return checked
    finally:
        temporary.unlink(missing_ok=True)


def overlay_existing(source, layers, output_path, work_dir, progress_callback=None):
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
    inputs, filters = ['-i', str(source)], []
    label = stickers.add_overlay_filters(inputs, filters, layers or [], 1, '0:v', width, height, fps, duration)
    if not layers:
        # No work and no re-encode is the only way to preserve the original byte-for-byte.
        return validate(str(source), duration)
    requested = 'auto'
    codecs = _codec_candidates(requested)
    log_path = work / 'overlay-ffmpeg.log'
    try:
        for codec in codecs:
            encoding = _encoding(codec, width, height, fps, requested)
            command = ['ffmpeg', '-nostdin', '-y', '-v', 'error', '-filter_complex_threads', _filter_threads(), *inputs, '-filter_complex', ';'.join(filters),
                       '-map', f'[{label}]', '-map', '0:a:0', *encoding, '-pix_fmt', 'yuv420p', '-c:a', 'copy',
                       '-movflags', '+faststart', '-progress', 'pipe:1', '-nostats', str(temporary)]
            with log_path.open('w', encoding='utf-8') as log:
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log, text=True)
                try:
                    for line in process.stdout:
                        key, _, value = line.strip().partition('=')
                        if key == 'out_time_us' and value.isdigit() and progress_callback:
                            progress_callback({'stage': 'rendering', 'progress': min(.95, int(value) / 1e6 / duration * .95)})
                    process.stdout.close()
                    result = process.wait()
                except BaseException:
                    process.terminate()
                    process.wait()
                    process.stdout.close()
                    raise
            if result == 0:
                break
        else:
            raise ValueError('贴纸渲染失败：' + log_path.read_text(encoding='utf-8')[-1000:])
        if progress_callback:
            progress_callback({'stage': 'validating', 'progress': .96})
        checked = validate(str(temporary), duration)
        publish(temporary, output)
        checked['path'] = str(output)
        if progress_callback:
            progress_callback({'stage': 'complete', 'progress': 1})
        return checked
    finally:
        temporary.unlink(missing_ok=True)
