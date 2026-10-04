"""Bounded edge detection with persistent, metadata-versioned results."""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import threading
import time

RULES = {'version': 1, 'threshold_db': -50, 'silence_seconds': .3, 'keep_seconds': .04}


def boundaries(log, length):
    intervals = []
    start = None
    for kind, value in re.findall(r'silence_(start|end):\s*([\d.eE+-]+)', log):
        value = max(0., min(length, float(value)))
        if kind == 'start':
            start = value
        elif start is not None:
            intervals.append((start, value)); start = None
    if start is not None:
        intervals.append((start, length))
    head = next((end for begin, end in intervals if begin <= .03), 0.)
    tail = next((begin for begin, end in reversed(intervals) if end >= length - .05), length)
    return head, tail


class MusicEdges:
    def __init__(self, store):
        self.store = store
        # Serialize disk seeks, and make simultaneous requests for a song share one result.
        self.lock = threading.Lock()

    @staticmethod
    def _scan(path, start, length, cancelled):
        cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-threads', '1', '-v', 'info',
               '-ss', str(start), '-i', str(path), '-t', str(length), '-map', '0:a:0', '-vn',
               '-filter_threads', '1', '-af', 'asetpts=PTS-STARTPTS,silencedetect=noise=-50dB:d=0.3',
               '-f', 'null', '-']
        process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 60
        try:
            while True:
                if cancelled():
                    raise InterruptedError('任务已取消')
                if time.monotonic() >= deadline:
                    raise ValueError('音乐首尾检测超时')
                try:
                    _, log = process.communicate(timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                raise ValueError('音乐首尾无法读取：' + log[-600:])
            return boundaries(log, length)
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill(); process.communicate()
            if process.stderr: process.stderr.close()

    def detect(self, song, cancelled=lambda: False):
        path = Path(song['path']).resolve()
        stat = path.stat()
        duration = float(song['duration'])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('歌曲时长无效')
        identity = [str(path), stat.st_size, stat.st_mtime_ns, duration, RULES]
        version = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        key = 'music-edges:' + hashlib.sha256(str(path).encode()).hexdigest()
        while not self.lock.acquire(timeout=.2):
            if cancelled(): raise InterruptedError('任务已取消')
        try:
            if cancelled(): raise InterruptedError('任务已取消')
            cached = self.store.get(key)
            if cached and cached.get('version') == version:
                return cached
            trim_start, trim_end = 0., duration
            for side in ('head', 'tail'):
                for window in (10., 20., 40., 60.):
                    length = min(window, duration)
                    offset = 0. if side == 'head' else duration - length
                    head, tail = self._scan(path, offset, length, cancelled)
                    boundary = head if side == 'head' else length - tail
                    if boundary < length - .05:
                        if side == 'head': trim_start = max(0., head - RULES['keep_seconds'])
                        else: trim_end = min(duration, offset + tail + RULES['keep_seconds'])
                        break
                    if length >= duration or window == 60.:
                        raise ValueError('歌曲首尾静音过长或没有有效声音')
            if trim_end - trim_start <= .1:
                raise ValueError('歌曲没有足够的有效声音')
            after = path.stat()
            if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise ValueError('音乐检测期间文件发生变化')
            result = dict(version=version, path=str(path), start=trim_start, end=trim_end,
                          effective_duration=trim_end-trim_start, rules=RULES, detected_at=time.time())
            self.store.put(key, result)
            return result
        finally:
            self.lock.release()

    def prepare(self, item, cancelled=lambda: False, progress=lambda done,total: None):
        prepared = copy.deepcopy(item)
        songs = prepared['music']
        for number, song in enumerate(songs):
            progress(number, len(songs))
            try:
                song['nonstop_edges'] = self.detect(song, cancelled)
            except (OSError, ValueError) as exc:
                from .renderer import MusicInputError
                raise MusicInputError(str(exc) + '：' + song.get('name', song['path']), [song['id']]) from exc
        progress(len(songs), len(songs))
        lengths = [song['nonstop_edges']['effective_duration'] for song in songs]
        fades = [min(.5, lengths[n]/2, lengths[n+1]/2) for n in range(len(songs)-1)]
        available = sum(lengths) - sum(fades)
        planned = float(item.get('planned_duration', item['duration']))
        # Small allowance for codec/container duration rounding, never pad missing sound.
        target = min(planned, max(0., available - .05))
        if target <= .1: raise ValueError('没有可制作成片的有效音乐')
        prepared.update(planned_duration=planned, duration=target,
                        nonstop={'available_duration': available, 'crossfades': fades})
        return prepared
