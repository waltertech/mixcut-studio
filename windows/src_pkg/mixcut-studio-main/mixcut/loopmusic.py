"""Music-led exports. Independent state and file policy; never consumes source media."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from .fsutil import flush_to_disk, publish
from .keyframes import KeyframeCache
from .processwatch import RenderStalled, run as run_watched
from .renderer import _probe
from .trash import move_to_trash

HEADER = 'YPP音乐交接 TXT v1'
START, END = '===== 播放顺序开始 =====', '===== 播放顺序结束 ====='
COPY_START, COPY_END = '===== 视频文案开始 =====', '===== 视频文案结束 ====='
ACTIVE = {'running', 'preparing', 'thumbnails'}


def positive(value, label):
    try:
        value = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(label + '无效') from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(label + '无效')
    return value


def section(text, start, end):
    # Markers must occupy entire lines; prose must not masquerade as a playlist.
    lines = text.splitlines()
    if lines.count(start) != 1 or lines.count(end) != 1:
        raise ValueError('交接文档必须只有一份完整的' + start)
    a, b = lines.index(start), lines.index(end)
    if a >= b:
        raise ValueError('交接文档区间顺序错误')
    return '\n'.join(lines[a + 1:b])


def fields(text):
    result = {}
    for line in text.splitlines():
        key, separator, value = line.partition('：')
        if separator:
            if key in result:
                raise ValueError('交接文档字段重复：' + key)
            result[key] = value
    return result


def file_stamp(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError('不是可读取的文件：' + str(path))
    stat = path.stat()
    return [str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def read_handoff(path, relocate=None):
    source = Path(path).expanduser().resolve(strict=True)
    if source.stat().st_size > 4_000_000:
        raise ValueError('交接文档过大')
    text = source.read_text(encoding='utf-8-sig')
    if not text.splitlines() or text.splitlines()[0] != HEADER:
        raise ValueError('请选择 Cover Maker 4.1 的 YPP交接.txt（TXT v1）')
    ordered = section(text, START, END)
    metadata = fields(text.split(START)[0])
    if metadata.get('状态') != '已完成':
        raise ValueError('上游 Set 尚未制作完成')
    copy_text = section(text, COPY_START, COPY_END)
    old_root = Path(metadata['音乐来源原路径'])
    if not old_root.is_absolute():
        raise ValueError('音乐来源必须是绝对路径')
    new_root = Path(relocate).expanduser().resolve(strict=True) if relocate else None
    def resolve(value, music=False):
        candidate = Path(value)
        if not candidate.is_absolute():
            raise ValueError('交接文件必须使用绝对路径：' + value)
        if music and new_root:
            try:
                candidate = new_root / candidate.relative_to(old_root)
            except ValueError as exc:
                raise ValueError('歌曲路径不在声明的音乐目录中') from exc
        elif not music and not candidate.is_file():
            # A moved Set folder can still locate its own sibling artifacts.
            candidate = source.parent / candidate.name
        file_stamp(candidate)
        return str(candidate.resolve())
    matches = list(re.finditer(r'^\[曲目 (\d+)\]$', ordered, re.M))
    if not matches or len(matches) > 500:
        raise ValueError('交接文档需包含 1～500 首歌曲')
    songs = []
    for number, match in enumerate(matches, 1):
        if int(match[1]) != number:
            raise ValueError('播放编号必须从 01 开始连续且唯一')
        block = ordered[match.end():matches[number].start() if number < len(matches) else len(ordered)]
        song = fields(block)
        audio = resolve(song['音频路径'], music=True)
        digest = song['SHA256'].lower()
        if not re.fullmatch('[a-f0-9]{64}', digest):
            raise ValueError('歌曲校验值无效：' + song['名称'])
        songs.append({'id': hashlib.sha256(audio.encode()).hexdigest()[:24], 'path': audio,
                      'name': song['名称'], 'duration': positive(song['时长秒'], '歌曲时长'),
                      'sha256': digest, 'original_number': int(song['原编号']), 'play_number': number})
    if len(songs) != int(metadata['曲目总数']):
        raise ValueError('歌曲数量与交接文档不一致')
    if len(songs) < 2 or [song['original_number'] for song in songs[:2]] != [1, 2]:
        raise ValueError('交接清单的前两首必须对应原编号 01、02')
    if len({song['path'] for song in songs}) != len(songs):
        raise ValueError('交接清单存在重复歌曲路径，请重新建立 Set')
    motion = resolve(metadata['动态封面路径'])
    cover = resolve(metadata['静态封面路径'])
    return {'set_id': metadata['Set ID'], 'name': old_root.name, 'manifest_path': str(source),
            'motion_path': motion, 'cover_path': cover, 'copy_text': copy_text,
            'music': songs, 'duration': sum(song['duration'] for song in songs),
            'motion_stamp': file_stamp(motion), 'cover_stamp': file_stamp(cover)}


class LoopMusic:
    """Compose existing media utilities with an isolated queue and store."""
    def __init__(self, app, store_factory):
        self.app = app
        self.store = store_factory(app.store.directory / 'loop-mode')
        self.frames = KeyframeCache(self.store.directory / 'keyframes')
        self.lock = threading.RLock()
        self.active = set()
        for task in self.listing():
            if task['status'] in ACTIVE or task['status'] == 'queued':
                task.update(status='paused', stage='上次运行已中断，点击开始继续')
                self.store.put('task:' + task['id'], task)

    def listing(self):
        return sorted((t for t in self.store.records('task:') if not t.get('deleted')),
                      key=lambda t: t['created_at'])

    def get(self, identity):
        task = self.store.get('task:' + str(identity))
        if not task or task.get('deleted'):
            raise ValueError('音乐循环任务不存在')
        return task

    def update(self, identity, **changes):
        with self.lock:
            task = self.store.get('task:' + identity)
            if task is None:
                raise InterruptedError('任务已删除')
            task.update(changes, updated_at=time.time())
            self.store.put('task:' + identity, task)
            return task

    def has_work(self):
        return bool(self.active) or any(t['status'] == 'queued' for t in self.listing())

    def import_tasks(self, body):
        paths = body.get('paths')
        if not isinstance(paths, list) or not paths or len(paths) > 200:
            raise ValueError('请选择交接 TXT 或 Set 文件夹路径（每次最多 200 个）')
        config = body.get('config', {})
        resolution = config.get('resolution', 'source')
        if resolution not in {'source', '1280x720', '1920x1080'}:
            raise ValueError('不支持的输出分辨率')
        parallel = int(config.get('parallel_tasks', 1))
        fps = int(config.get('fps', 30))
        volume = float(config.get('music_volume', 1))
        if parallel not in (1, 2, 3, 4) or fps not in (30, 60) or not math.isfinite(volume) or not 0 <= volume <= 1:
            raise ValueError('导出参数无效')
        output = Path(body.get('output_dir') or '').expanduser()
        if not output.is_absolute():
            raise ValueError('请选择绝对输出路径')
        output = output.resolve()
        files = []
        for value in paths:
            path = Path(value.strip()).expanduser().resolve(strict=True)
            if path.is_dir():
                direct = path / 'YPP交接.txt'
                files.extend([direct] if direct.is_file() else sorted(path.glob('*/YPP交接.txt')))
            else:
                files.append(path)
        files = list(dict.fromkeys(files))
        if not files or len(files) > 200:
            raise ValueError('未找到交接文件，或数量超过 200')
        imported, errors = [], []
        for path in files:
            try:
                task = read_handoff(path, body.get('music_root'))
                # Use Set identity plus source location to avoid accidental duplicate imports.
                with self.lock:
                    if any(t['set_id'] == task['set_id'] and t['manifest_path'] == task['manifest_path'] for t in self.listing()):
                        raise ValueError('该 Set 已在任务列表中')
                    identity = uuid.uuid4().hex[:16]
                    task.update(id=identity, status='draft', progress=0, stage='已导入，等待开始',
                                output_root=str(output), created_at=time.time(),
                                config={'resolution': resolution, 'fps': fps, 'music_volume': volume,
                                        'parallel_tasks': parallel, 'nonstop_music': bool(config.get('nonstop_music', True))})
                    self.store.put('task:' + identity, task)
                    imported.append(identity)
            except (OSError, ValueError, KeyError, IndexError) as exc:
                errors.append({'path': str(path), 'error': str(exc)})
        return {'imported': imported, 'errors': errors, 'tasks': self.listing()}

    def start_tasks(self, identities, parallel):
        if not isinstance(identities, list) or not identities or len(identities) > 200:
            raise ValueError('请选择 1～200 条任务')
        if isinstance(parallel, bool) or parallel not in (1, 2, 3, 4):
            raise ValueError('并行数量必须为 1、2、3 或 4')
        with self.lock:
            # Validate before changing any record; dispatcher takes the same lock.
            tasks = [self.get(identity) for identity in dict.fromkeys(identities)]
            self.store.put('execution-settings', {'parallel_tasks': int(parallel)})
            started = []
            for task in tasks:
                if task['status'] not in {'draft', 'paused', 'stalled_paused', 'failed', 'queued'}:
                    continue
                config = dict(task['config'], parallel_tasks=int(parallel))
                self.update(task['id'], config=config, status='queued', cancel=False,
                            error=None, stage='等待制作')
                started.append(task['id'])
        self.app.wake.set()
        return {'started': started, 'parallel_tasks': int(parallel)}

    def action(self, identity, action):
        with self.lock:
            task = self.get(identity)
            if action == 'start':
                if task['status'] in ACTIVE or task['status'] in {'success', 'approved', 'rejected'}:
                    raise ValueError('当前任务无法开始')
                self.update(identity, status='queued', cancel=False, error=None, stage='等待制作')
            elif action == 'pause':
                if task['status'] in ACTIVE:
                    self.update(identity, cancel=True, stage='正在暂停')
                elif task['status'] == 'queued':
                    self.update(identity, status='paused', stage='已暂停')
            elif action == 'delete':
                self.update(identity, deleted=True, cancel=True)
                if identity not in self.active:
                    self.cleanup(task)
                    self.store.delete('task:' + identity)
            elif action == 'approve':
                if task['status'] == 'approved':
                    return task
                if task['status'] != 'success':
                    raise ValueError('视频制作完成后才能审核')
                self.check_output(task)
                from .bundles import date_for_approval
                with self.app.review_lock:
                    date_for_approval(task, task['output_root'], self.app.reserve_review_bundle,
                                      lambda value: self.update(identity, **{k: value.get(k) for k in ('output_bundle', 'output_path', 'bundle_relocation', 'output_stamp')}))
                self.cleanup(task)
                self.update(identity, status='approved', stage='已审核，作品保留原处')
            elif action == 'reject':
                if task['status'] != 'success':
                    raise ValueError('只有可审核的任务才能审核不通过')
                self.check_output(task)
                folder = Path(task['output_bundle'])
                if folder.is_symlink() or folder.resolve().parent.parent != Path(task['output_root']).resolve():
                    raise ValueError('成品目录位置异常，文件已保留')
                # Entire generated bundle only. Sources are deliberately outside this policy.
                move_to_trash(folder)
                self.cleanup(task)
                self.update(identity, status='rejected', stage='作品已移入废纸篓；原音乐与动态封面保留')
            else:
                raise ValueError('不支持的操作')
        self.app.wake.set()
        return self.store.get('task:' + identity, {'deleted': True})

    def check_output(self, task):
        if task.get('bundle_relocation'):
            from .bundles import finish_relocation
            finish_relocation(task, lambda value: self.update(task['id'], **{k: value.get(k) for k in ('output_bundle', 'output_path', 'bundle_relocation', 'output_stamp')}))
        if file_stamp(task['output_path']) != task.get('output_stamp'):
            raise ValueError('成品文件已变化，请检查后再审核')

    def cleanup(self, task):
        preview = task.get('thumbnails') or {}
        if preview.get('version'):
            self.frames.remove(preview['version'])
        shutil.rmtree(self.store.directory / 'work' / task['id'], ignore_errors=True)

    def cancelled(self, identity):
        task = self.store.get('task:' + identity, {})
        return self.app.closing.is_set() or task.get('deleted') or task.get('cancel') or not task

    def verify_hash(self, song, cancelled):
        stamp = file_stamp(song['path'])
        key = 'verified:' + song['id']
        cached = self.store.get(key, {})
        if cached.get('stamp') == stamp and cached.get('sha256') == song['sha256']:
            return
        digest = hashlib.sha256()
        with open(song['path'], 'rb') as source:
            while True:
                if cancelled():
                    raise InterruptedError('任务已暂停')
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        if digest.hexdigest() != song['sha256'] or file_stamp(song['path']) != stamp:
            raise ValueError('歌曲已变化，不能静默替换：' + song['name'])
        self.store.put(key, {'stamp': stamp, 'sha256': song['sha256']})

    def relocate(self, identity, root):
        with self.lock:
            task = self.get(identity)
            if task['status'] in ACTIVE or task['status'] in {'queued', 'success', 'approved', 'rejected'}:
                raise ValueError('请先暂停未完成任务再修复路径')
            replacement = read_handoff(task['manifest_path'], root)
            if replacement['set_id'] != task['set_id'] or [s['sha256'] for s in replacement['music']] != [s['sha256'] for s in task['music']]:
                raise ValueError('修复路径不能改变歌曲或顺序')
            return self.update(identity, music=replacement['music'], motion_path=replacement['motion_path'],
                               cover_path=replacement['cover_path'], motion_stamp=replacement['motion_stamp'],
                               cover_stamp=replacement['cover_stamp'], error=None, status='paused', stage='路径已修复，等待开始')

    def cache_info(self):
        total, count = 0, 0
        for folder in ('work', 'keyframes'):
            for path in (self.store.directory / folder).rglob('*'):
                try:
                    if path.is_file():
                        total += path.stat().st_size
                        count += 1
                except OSError:
                    continue
        return {'bytes': total, 'files': count}

    def clear_cache(self):
        with self.lock:
            removed, skipped = 0, 0
            for task in self.listing():
                if task['id'] in self.active or task['status'] == 'queued':
                    skipped += 1
                    continue
                preview = task.get('thumbnails') or {}
                if preview.get('version'):
                    self.frames.remove(preview['version'])
                    with self.frames.guard:
                        self.frames.deleted_versions.discard(preview['version'])
                work = self.store.directory / 'work' / task['id']
                for entry in work.glob('*'):
                    if entry.name != 'result.json' and entry.is_file(): entry.unlink()
                removed += 1
            return {'removed': removed, 'skipped': skipped}

    def run_next(self):
        with self.lock:
            tasks = [t for t in self.listing() if t['status'] == 'queued']
            if not tasks:
                return False
            # Old persisted tasks remain usable before the first new batch-start request.
            fallback = tasks[0]['config'].get('parallel_tasks', 1)
            limit = self.store.get('execution-settings', {}).get('parallel_tasks', fallback)
        # The existing dispatcher owns this pool; original and loop renders never overlap.
        # Keep vacant slots filled, including tasks queued while an earlier task is running.
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix='music-loop') as pool:
            pending = {}
            while not self.app.closing.is_set():
                with self.lock:
                    limit = self.store.get('execution-settings', {}).get('parallel_tasks', limit)
                    queued = [t for t in self.listing()
                              if t['status'] == 'queued' and t['id'] not in pending.values()]
                    for task in queued[:max(0, limit - len(pending))]:
                        pending[pool.submit(self.execute, task['id'])] = task['id']
                if not pending:
                    break
                completed, _ = wait(pending, timeout=.25, return_when=FIRST_COMPLETED)
                for future in completed:
                    pending.pop(future)
                    future.result()
                # Return control between idle waves so the original queue keeps priority.
                if not pending:
                    break
        return True

    def execute(self, identity):
        with self.lock:
            task = self.store.get('task:' + identity)
            if not task or task.get('deleted') or task['status'] != 'queued':
                return
            self.active.add(identity)
            self.update(identity, status='preparing', stage='检查交接素材', progress=0)
        work = self.store.directory / 'work' / identity
        try:
            work.mkdir(parents=True, exist_ok=True)
            cancelled = lambda: self.cancelled(identity)
            for number, song in enumerate(task['music'], 1):
                self.update(identity, stage=f'检查歌曲文件 {number}/{len(task["music"])}')
                self.verify_hash(song, cancelled)
            if file_stamp(task['motion_path']) != task['motion_stamp'] or file_stamp(task['cover_path']) != task['cover_stamp']:
                raise ValueError('动态封面或静态封面已变化，请重新导入 Set')
            source_stamps = {song['path']: file_stamp(song['path']) for song in task['music']}
            songs = copy.deepcopy(task['music'])
            if task['config']['nonstop_music']:
                for index, song in enumerate(songs):
                    self.update(identity, stage=f'正在进行音乐静音检测 {index + 1}/{len(songs)}')
                    song['nonstop_edges'] = self.app.music_edges.detect(song, cancelled)
            lengths = [s.get('nonstop_edges', {}).get('effective_duration', s['duration']) for s in songs]
            fades = [min(.5, lengths[n]/2, lengths[n+1]/2) for n in range(len(songs)-1)] if task['config']['nonstop_music'] else []
            target = sum(lengths) - sum(fades)
            def progress(event):
                if cancelled():
                    raise InterruptedError('任务已暂停')
                if not event.get('heartbeat'):
                    self.update(identity, progress=min(.94, float(event.get('progress', 0))))
            def command_run(command, filename, stage, duration):
                if cancelled():
                    raise InterruptedError('任务已暂停')
                self.update(identity, status='running', stage=stage)
                with (work / (Path(filename).name + '.log')).open('w+', encoding='utf-8') as log:
                    code = run_watched(command, log, work / filename, progress, duration=duration)
                    if code:
                        log.seek(0)
                        raise ValueError(stage + '失败：' + log.read()[-1800:])
            motion = _probe(task['motion_path'])
            motion_video = next((s for s in motion['streams'] if s['codec_type'] == 'video'), None)
            if not motion_video:
                raise ValueError('动态封面没有视频流')
            source_key = 'motion-verified:' + hashlib.sha256(task['motion_path'].encode()).hexdigest()
            if self.store.get(source_key) != task['motion_stamp']:
                command_run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-threads', '1', '-i', task['motion_path'], '-map', '0:v:0', '-an', '-f', 'null', '-'],
                            'motion-check', '检查短循环视频', float(motion['format']['duration']))
                self.store.put(source_key, task['motion_stamp'])
            # Reuse a completed audio checkpoint only when inputs/settings still match.
            audio = work / 'music.m4a'
            signature = hashlib.sha256(json.dumps([songs, task['config']['music_volume']], sort_keys=True).encode()).hexdigest()
            checkpoint = work / 'audio.json'
            try:
                saved = json.loads(checkpoint.read_text())
            except (OSError, ValueError):
                saved = {}
            if saved.get('signature') != signature or not audio.is_file() or saved.get('stamp') != file_stamp(audio):
                command = ['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-y', '-threads', '1']
                filters = []
                for n, song in enumerate(songs):
                    command += ['-i', song['path']]
                    edges = song.get('nonstop_edges')
                    trim = f"atrim=start={edges['start']:.8f}:end={edges['end']:.8f}," if edges else ''
                    filters.append(f'[{n}:a:0]{trim}asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{n}]')
                label = 'a0'
                if fades:
                    for n, fade in enumerate(fades, 1):
                        filters.append(f'[{label}][a{n}]acrossfade=d={fade:.8f}:c1=tri:c2=tri[j{n}]')
                        label = f'j{n}'
                elif len(songs) > 1:
                    filters.append(''.join(f'[a{n}]' for n in range(len(songs))) + f'concat=n={len(songs)}:v=0:a=1[join]')
                    label = 'join'
                filters.append(f"[{label}]volume={task['config']['music_volume']}[out]")
                temporary = work / 'audio.partial.m4a'
                command += ['-filter_complex_threads', '1', '-filter_complex', ';'.join(filters), '-map', '[out]', '-vn', '-c:a', 'aac', '-b:a', '192k', str(temporary)]
                command_run(command, temporary.name, '正在按曲目顺序拼接音乐', target)
                temporary.replace(audio)
                checkpoint.write_text(json.dumps({'signature': signature, 'stamp': file_stamp(audio)}))
            if any(file_stamp(path) != stamp for path, stamp in source_stamps.items()):
                raise ValueError('音乐在制作过程中发生变化，请修复后重试')
            info = _probe(audio)
            actual = positive(info['format']['duration'], '实际音乐时长')
            if cancelled():
                raise InterruptedError('任务已暂停')
            # Reserve a unique bundle once. Existing complete artifacts support interrupted recovery.
            task = self.get(identity)
            if not task.get('output_path'):
                date = Path(task['output_root']) / datetime.now().astimezone().strftime('%Y-%m-%d')
                date.mkdir(parents=True, exist_ok=True)
                with self.app.review_lock:
                    folder, _ = self.app.reserve_review_bundle(date, {'output_name': '成品视频.mp4'}, label=Path(task['output_root']).name)
                task = self.update(identity, output_bundle=str(folder), output_path=str(folder / '成品视频.mp4'))
            output = Path(task['output_path'])
            receipt = work / 'result.json'
            video_info = _probe(task['motion_path'])
            video = next((s for s in video_info['streams'] if s['codec_type'] == 'video'), None)
            if not video:
                raise ValueError('动态封面没有视频流')
            if output.exists():
                try:
                    result = json.loads(receipt.read_text())
                except (OSError, ValueError) as exc:
                    raise ValueError('输出已有文件但缺少制作记录，请移走文件后重试') from exc
                if result.get('signature') != signature or result.get('output_stamp') != file_stamp(output):
                    raise ValueError('已有输出与本任务不一致，程序不会覆盖')
            else:
                self.app.check_space(output.parent)
                partial = output.parent / '.video.partial.mp4'
                command = ['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-y', '-threads', '1', '-stream_loop', '-1', '-i', task['motion_path'], '-i', str(audio), '-map', '0:v:0', '-map', '1:a:0']
                resolution = task['config']['resolution']
                copy_video = resolution == 'source' and video.get('codec_name') == 'h264'
                if copy_video:
                    command += ['-c:v', 'copy']
                else:
                    from .renderer import _codec_candidates, _encoding
                    codec = _codec_candidates('auto')[0]
                    width, height = (int(x) for x in resolution.split('x')) if resolution != 'source' else (video['width'], video['height'])
                    command += _encoding(codec, width, height, task['config']['fps'])
                    if resolution != 'source':
                        command += ['-vf', 'scale=' + resolution.replace('x', ':')]
                    command += ['-r', str(task['config']['fps']), '-pix_fmt', 'yuv420p']
                command += ['-c:a', 'copy', '-t', f'{actual:.8f}', '-movflags', '+faststart', str(partial)]
                try:
                    command_run(command, str(partial), '正在循环画面并合成视频', actual)
                except ValueError:
                    if copy_video or codec == 'libx264':
                        raise
                    # Hardware encoder availability differs across machines. Retry with software.
                    start = command.index('-c:v')
                    command[start + 1] = 'libx264'
                    command[start + 2:start + 2] = ['-preset', 'veryfast']
                    command_run(command, str(partial), '正在使用软件编码合成视频', actual)
                rendered = _probe(partial)
                streams = rendered['streams']
                v = next((s for s in streams if s['codec_type'] == 'video'), None)
                a = next((s for s in streams if s['codec_type'] == 'audio'), None)
                if (not v or not a or abs(float(rendered['format']['duration']) - actual) > .2
                        or abs(float(v.get('duration', 0)) - actual) > .2
                        or abs(float(a.get('duration', 0)) - actual) > .08):
                    raise ValueError('成品音画时长不匹配')
                self.update(identity, stage='正在保存成品')
                flush_to_disk(partial)
                publish(partial, output)
                result = {'signature': signature, 'duration': actual, 'copied_video': copy_video, 'output_stamp': file_stamp(output)}
                receipt.write_text(json.dumps(result))
            # Publish original artwork/copy plus an actual timeline, not a second competing source order.
            folder = output.parent
            shutil.copy2(task['cover_path'], folder / '封面.png')
            (folder / '发布文案.txt').write_text(task['copy_text'] + '\n', encoding='utf-8')
            elapsed, lines = 0., ['曲目播放顺序与实际时间轴', '']
            for n, (song, length) in enumerate(zip(songs, lengths)):
                if n and fades: elapsed -= fades[n-1]
                lines.extend([f"{n+1:02d}. {song['name']}", f'起始秒：{elapsed:.3f}', '音频路径：' + song['path'], ''])
                elapsed += length
            (folder / '曲目与来源.txt').write_text('\n'.join(lines), encoding='utf-8')
            if cancelled():
                raise InterruptedError('任务已暂停')
            self.update(identity, status='success', stage='可审核', progress=1, thumbnails=None,
                        output_stamp=file_stamp(output), actual_duration=actual, error=None)
        except InterruptedError:
            self.update(identity, status='paused', stage='已暂停，点击开始继续', cancel=False)
        except RenderStalled as exc:
            self.update(identity, status='stalled_paused', stage='执行被卡住暂停', error=str(exc), cancel=False)
        except Exception as exc:
            self.update(identity, status='failed', stage='制作失败，可修复后重试', error=str(exc))
        finally:
            with self.lock:
                self.active.discard(identity)
                task = self.store.get('task:' + identity, {})
                if task.get('deleted'):
                    self.cleanup(task)
                    self.store.delete('task:' + identity)
                # Partial outputs belong to the checkpoint area, never user sources.
                for partial in work.glob('*.partial.*'):
                    partial.unlink(missing_ok=True)
                if task.get('output_bundle'):
                    (Path(task['output_bundle']) / '.video.partial.mp4').unlink(missing_ok=True)
