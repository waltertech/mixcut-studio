"""On-demand, versioned contact-sheet images for completed local exports."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import time
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
        self.deleted_versions = set()

    def _version(self, path):
        path = Path(path).resolve(strict=True)
        stat = path.stat()
        identity = f'{path}:{stat.st_size}:{stat.st_mtime_ns}:keyframes-v3-paired-30s'
        return hashlib.sha256(identity.encode()).hexdigest()[:24]

    def _lock(self, key):
        with self.guard:
            return self.locks.setdefault(key, threading.Lock())

    def describe(self, path):
        version = self._version(path)
        directory = self.root / version
        metadata = directory / 'index.json'
        with self._lock(version):
            if version in self.deleted_versions:
                raise ValueError('该任务的缩略图已清理')
            if metadata.is_file():
                return json.loads(metadata.read_text(encoding='utf-8'))
            with self.slots:
                result = subprocess.run(
                    ['ffprobe', '-v', 'error', '-threads', '1', '-show_entries',
                     'format=duration', '-of', 'json', str(path)],
                    capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise ValueError('关键帧读取失败：' + result.stderr[-500:])
            data = json.loads(result.stdout)
            duration = float(data.get('format', {}).get('duration', 0))
            if duration <= 0:
                raise ValueError('视频时长无效，请检查文件是否可正常播放')
            if self._version(path) != version:
                raise ValueError('视频已变化，请刷新后重新加载关键帧')
            timestamps = [float(value) for value in range(0, max(1, math.ceil(duration)), 30)]
            frames = [{'index': index, 'time': value} for index, value in enumerate(timestamps)]
            response = {'version': version, 'duration': duration, 'total': len(frames), 'frames': frames}
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / ('index-' + uuid.uuid4().hex + '.tmp')
            temporary.write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')
            temporary.replace(metadata)
            return response

    def ready(self, version, total):
        if not version or total < 1:
            return False
        directory = self.root / version
        return all((directory / f'{index:06d}-{size}.jpg').is_file()
                   for index in range(total) for size in ('thumb', 'large'))

    def remove(self, version):
        if not version or len(version) != 24 or any(c not in '0123456789abcdef' for c in version):
            return
        with self.guard:
            self.deleted_versions.add(version)
        with self._lock('prepare:' + version), self._lock(version):
            shutil.rmtree(self.root / version, ignore_errors=False) if (self.root / version).exists() else None

    def prepare(self, path, cancelled=lambda: False):
        """Extract all 30-second thumbnails in one bounded, cancellable FFmpeg pass."""
        data = self.describe(path)
        version = data['version']
        with self._lock('prepare:' + version):
            if cancelled():
                raise InterruptedError('缩略图生成已取消')
            if self.ready(version, data['total']):
                return data
            directory = self.root / version
            staging = directory / ('building-' + uuid.uuid4().hex)
            staging.mkdir(parents=True)
            process = None
            try:
                with self.slots, (staging / 'ffmpeg.log').open('w') as log:
                    process = subprocess.Popen([
                        'ffmpeg', '-nostdin', '-v', 'error', '-y', '-threads', '1',
                        '-reinit_filter:v', '0', '-i', str(path), '-filter_complex_threads', '1',
                        '-filter_complex',
                        "[0:v:0]select='gte(t,selected_n*30)',split=2[small][big];"
                        "[small]scale=min(320\\,iw):-2[thumb];[big]scale=min(960\\,iw):-2[large]",
                        '-map', '[thumb]', '-an', '-fps_mode', 'vfr', '-threads', '1', '-q:v', '3',
                        '-start_number', '0', str(staging / '%06d-thumb.jpg'),
                        '-map', '[large]', '-an', '-fps_mode', 'vfr', '-threads', '1', '-q:v', '3',
                        '-start_number', '0', str(staging / '%06d-large.jpg')],
                        stdout=subprocess.DEVNULL, stderr=log)
                    started = time.monotonic()
                    while process.poll() is None:
                        if cancelled():
                            raise InterruptedError('缩略图生成已取消')
                        if time.monotonic() - started > max(120, data['duration'] * 2):
                            raise ValueError('缩略图生成超时，请重试')
                        time.sleep(.1)
                if process.returncode:
                    raise ValueError('缩略图生成失败：' + (staging / 'ffmpeg.log').read_text()[-500:])
                if cancelled() or self._version(path) != version:
                    raise InterruptedError('任务已取消或视频已变化')
                for size in ('thumb', 'large'):
                    images = sorted(staging.glob(f'*-{size}.jpg'))
                    if len(images) != data['total']:
                        raise ValueError('缩略图采样数量不匹配，请重试')
                    for index in range(data['total']):
                        image = staging / f'{index:06d}-{size}.jpg'
                        if not image.is_file() or not image.stat().st_size:
                            raise ValueError('缩略图不完整，请重试')
                for image in staging.glob('*.jpg'):
                    image.replace(directory / image.name)
                return data
            finally:
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait()
                shutil.rmtree(staging, ignore_errors=True)

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
