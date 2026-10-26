"""Minute-spaced, low-resolution review images extracted by input seeking."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

LOGGER = logging.getLogger('mixcut.thumbnails')
if not LOGGER.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    LOGGER.addHandler(handler)
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False

POINT_TIMEOUT = 8.0
POINT_ATTEMPTS = 2


class KeyframeCache:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.slots = threading.BoundedSemaphore(4)
        self.guard = threading.Lock()
        self.locks = {}
        self.deleted_versions = set()

    def _version(self, path, legacy=False):
        path = Path(path).resolve(strict=True)
        stat = path.stat()
        sampling = ('keyframes-v4-paired-60s' if legacy == 'minute' else
                    'keyframes-v3-paired-30s' if legacy else 'keyframes-v5-seek-60s-480-q7')
        return hashlib.sha256(f'{path}:{stat.st_size}:{stat.st_mtime_ns}:{sampling}'.encode()).hexdigest()[:24]

    def previous_versions(self, path):
        return [self._version(path, legacy='minute'), self._version(path, legacy=True)]

    def _lock(self, key):
        with self.guard:
            return self.locks.setdefault(key, threading.Lock())

    def _reuse_legacy(self, path, version, directory):
        for legacy, step in zip(self.previous_versions(path), (1, 2)):
            old = self.root / legacy
            try:
                data = json.loads((old / 'index.json').read_text(encoding='utf-8'))
                if (data.get('version') != legacy or not self.ready(legacy, data['total'])
                        or len(data['frames']) != data['total'] or not data['frames']):
                    continue
                chosen = data['frames'][::step]
                directory.mkdir(parents=True, exist_ok=True)
                for index, frame in enumerate(chosen):
                    source = old / f"{int(frame['index']):06d}-thumb.jpg"
                    target = directory / f'{index:06d}.jpg'
                    if not target.exists():
                        try:
                            os.link(source, target)
                        except OSError:
                            shutil.copyfile(source, target)
                if self._version(path) != version:
                    raise ValueError('视频已变化，请重新加载缩略图')
                response = dict(data, version=version, total=len(chosen), interval_seconds=60, storage='single',
                                frames=[{'index':i, 'time':frame['time']} for i,frame in enumerate(chosen)])
                self._save_index(directory, response)
                return response
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                continue
        return None

    @staticmethod
    def _save_index(directory, data):
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / ('index-' + uuid.uuid4().hex + '.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        temporary.replace(directory / 'index.json')

    def describe(self, path):
        version = self._version(path)
        directory = self.root / version
        with self._lock(version):
            if version in self.deleted_versions:
                raise ValueError('该任务的缩略图已清理')
            metadata = directory / 'index.json'
            if metadata.is_file():
                return json.loads(metadata.read_text(encoding='utf-8'))
            reused = self._reuse_legacy(path, version, directory)
            if reused is not None:
                return reused
            with self.slots:
                result = subprocess.run(['ffprobe', '-v', 'error', '-threads', '1', '-show_entries',
                                         'format=duration', '-of', 'json', str(path)],
                                        capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise ValueError('缩略图读取失败：' + result.stderr[-500:])
            duration = float(json.loads(result.stdout).get('format', {}).get('duration', 0))
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError('视频时长无效，请检查文件是否可正常播放')
            if self._version(path) != version:
                raise ValueError('视频已变化，请刷新后重新加载缩略图')
            frames = [{'index':i, 'time':float(t)} for i,t in enumerate(range(0, max(1, math.ceil(duration)), 60))]
            data = {'version':version, 'duration':duration, 'total':len(frames), 'frames':frames,
                    'interval_seconds':60, 'storage':'single', 'width':480}
            self._save_index(directory, data)
            return data

    def ready(self, version, total):
        if not version or total < 1:
            return False
        directory = self.root / version
        def exists(path):
            return path.is_file() and path.stat().st_size > 0
        return all(exists(directory / f'{index:06d}.jpg') or
                   all(exists(directory / f'{index:06d}-{size}.jpg') for size in ('thumb', 'large'))
                   for index in range(total))

    def remove(self, version):
        if not version or len(version) != 24 or any(c not in '0123456789abcdef' for c in version):
            return
        with self.guard:
            self.deleted_versions.add(version)
        with self._lock('prepare:' + version), self._lock(version):
            if (self.root / version).exists():
                shutil.rmtree(self.root / version)

    def _capture(self, path, point, target, cancelled, deadline):
        process = None
        try:
            with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as log:
                process = subprocess.Popen([
                    'ffmpeg', '-nostdin', '-v', 'error', '-y', '-ss', f'{point:.9f}',
                    '-threads', '1', '-i', str(path), '-frames:v', '1', '-an', '-sn', '-dn',
                    '-vf', 'scale=min(480\\,iw):-2', '-threads', '1', '-q:v', '7', str(target)],
                    stdout=subprocess.DEVNULL, stderr=log)
                stop = min(deadline, time.monotonic() + POINT_TIMEOUT)
                while process.poll() is None:
                    if cancelled():
                        raise InterruptedError('缩略图生成已取消')
                    if time.monotonic() >= stop:
                        raise ValueError(f'缩略图采样点 {point:g} 秒读取超时')
                    try:
                        process.wait(timeout=.05)
                    except subprocess.TimeoutExpired:
                        pass
                if process.returncode or not target.is_file() or not target.stat().st_size:
                    log.seek(0)
                    raise ValueError(f'缩略图采样点 {point:g} 秒失败：' + log.read()[-500:])
        finally:
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()

    def _ensure_image(self, path, data, frame, cancelled, deadline):
        destination = self.root / data['version'] / f"{frame['index']:06d}.jpg"
        with self._lock(str(destination)):
            if destination.is_file() and destination.stat().st_size:
                return destination
            temporary = destination.with_name(destination.stem + '-' + uuid.uuid4().hex + '.jpg')
            for attempt in range(POINT_ATTEMPTS):
                try:
                    if cancelled():
                        raise InterruptedError('缩略图生成已取消')
                    if time.monotonic() >= deadline:
                        raise ValueError('本条视频缩略图生成超时，可重试并复用已生成图片')
                    with self.slots:
                        self._capture(path, frame['time'], temporary, cancelled, deadline)
                    if cancelled() or self._version(path) != data['version']:
                        raise InterruptedError('任务已取消或视频已变化')
                    temporary.replace(destination)
                    return destination
                except (OSError, ValueError):
                    if attempt + 1 == POINT_ATTEMPTS or time.monotonic() >= deadline:
                        raise
                finally:
                    temporary.unlink(missing_ok=True)

    def prepare(self, path, cancelled=lambda: False):
        data = self.describe(path)
        version = data['version']
        with self._lock('prepare:' + version):
            if cancelled():
                raise InterruptedError('缩略图生成已取消')
            if self.ready(version, data['total']):
                return data
            started = time.monotonic()
            deadline = started + max(45., min(180., data['total'] * 2.))
            LOGGER.info('thumbnail seek start version=%s frames=%s', version, data['total'])
            for frame in data['frames']:
                self._ensure_image(path, data, frame, cancelled, deadline)
            LOGGER.info('thumbnail seek ready version=%s frames=%s elapsed=%.3fs', version, data['total'], time.monotonic()-started)
            return data

    def image(self, path, index, size='thumb', version=None):
        if size not in {'thumb', 'large'}:
            raise ValueError('无效的关键帧尺寸')
        data = self.describe(path)
        if version and version != data['version']:
            raise ValueError('视频已变化，请刷新关键帧列表')
        if index < 0 or index >= data['total']:
            raise ValueError('关键帧编号超出范围')
        with self._lock('prepare:' + data['version']):
            return self._ensure_image(path, data, data['frames'][index], lambda: False,
                                      time.monotonic() + POINT_TIMEOUT * POINT_ATTEMPTS)
