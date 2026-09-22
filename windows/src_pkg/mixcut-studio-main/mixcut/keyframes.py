"""On-demand, versioned contact-sheet images for completed local exports."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import subprocess
import threading
import uuid


class KeyframeCache:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.slots = threading.BoundedSemaphore(2)
        self.guard = threading.Lock()
        self.locks = {}

    def _version(self, path):
        path = Path(path).resolve(strict=True)
        stat = path.stat()
        identity = f'{path}:{stat.st_size}:{stat.st_mtime_ns}:keyframes-v1'
        return hashlib.sha256(identity.encode()).hexdigest()[:24]

    def _lock(self, key):
        with self.guard:
            return self.locks.setdefault(key, threading.Lock())

    def describe(self, path):
        version = self._version(path)
        directory = self.root / version
        metadata = directory / 'index.json'
        with self._lock(version):
            if metadata.is_file():
                return json.loads(metadata.read_text(encoding='utf-8'))
            with self.slots:
                result = subprocess.run(
                    ['ffprobe', '-v', 'error', '-threads', '1', '-skip_frame', 'nokey',
                     '-select_streams', 'v:0', '-show_frames', '-show_format',
                     '-show_entries', 'frame=best_effort_timestamp_time:format=duration',
                     '-of', 'json', str(path)], capture_output=True, text=True, timeout=180)
            if result.returncode:
                raise ValueError('关键帧读取失败：' + result.stderr[-500:])
            data = json.loads(result.stdout)
            duration = float(data.get('format', {}).get('duration', 0))
            timestamps = set()
            for frame in data.get('frames', []):
                try:
                    timestamp = float(frame['best_effort_timestamp_time'])
                except (KeyError, TypeError, ValueError):
                    continue
                if math.isfinite(timestamp) and 0 <= timestamp < duration:
                    timestamps.add(timestamp)
            if not timestamps:
                raise ValueError('该视频未读取到关键帧，请检查文件是否可正常播放')
            if self._version(path) != version:
                raise ValueError('视频已变化，请刷新后重新加载关键帧')
            frames = [{'index': index, 'time': value} for index, value in enumerate(sorted(timestamps))]
            response = {'version': version, 'duration': duration, 'total': len(frames), 'frames': frames}
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / ('index-' + uuid.uuid4().hex + '.tmp')
            temporary.write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')
            temporary.replace(metadata)
            return response

    def image(self, path, index, size='thumb', version=None):
        if size not in {'thumb', 'large'}:
            raise ValueError('无效的关键帧尺寸')
        data = self.describe(path)
        if version and version != data['version']:
            raise ValueError('视频已变化，请刷新关键帧列表')
        if index < 0 or index >= data['total']:
            raise ValueError('关键帧编号超出范围')
        destination = self.root / data['version'] / f'{index:06d}-{size}.jpg'
        with self._lock(str(destination)):
            if destination.is_file():
                return destination
            width = 320 if size == 'thumb' else 960
            temporary = destination.with_name(destination.stem + '-' + uuid.uuid4().hex + '.jpg')
            try:
                with self.slots:
                    result = subprocess.run(
                        ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-ss',
                         f"{data['frames'][index]['time']:.9f}", '-threads', '1', '-i', str(path),
                         '-frames:v', '1', '-an', '-vf', f'scale=min({width}\\,iw):-2',
                         '-threads', '1', '-q:v', '3', str(temporary)],
                        capture_output=True, text=True, timeout=60)
                if result.returncode or not temporary.is_file() or not temporary.stat().st_size:
                    raise ValueError('关键帧图片生成失败：' + result.stderr[-500:])
                if self._version(path) != data['version']:
                    raise ValueError('视频已变化，请刷新关键帧列表')
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
            return destination
