"""Loopback-only HTTP interface and durable single-worker queue."""
from __future__ import annotations

import argparse
import base64
import binascii
import copy
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import uuid
from .folders import batch_output_folder, choose_folder, reveal
from .fsutil import flush_to_disk, publish
from .keyframes import KeyframeCache
from .lockfile import LockUnavailable, acquire as acquire_lock, release as release_lock
from .naming import export_filename, music_folders, music_styles, tag_music_style
from .runtime import activate_bundled_tools, data_root, resource_root

# ROOT keeps holding read-only resources so existing callers stay valid; DATA is the
# writable per-user directory used for state, cache and the default material folders.
ROOT = resource_root()
DATA = data_root()


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'state.sqlite3'
        self.lock = threading.RLock()
        with self.connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, body TEXT NOT NULL)')

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, key, default=None):
        with self.lock, self.connection() as conn:
            row = conn.execute('SELECT body FROM records WHERE id=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.lock, self.connection() as conn:
            conn.execute('INSERT OR REPLACE INTO records VALUES (?,?)',
                         (key, json.dumps(value, ensure_ascii=False)))

    def batches(self):
        with self.lock, self.connection() as conn:
            rows = conn.execute("SELECT body FROM records WHERE id LIKE 'batch:%'").fetchall()
            return sorted((json.loads(row[0]) for row in rows),
                          key=lambda batch: batch.get('created_at', 0), reverse=True)

    def records(self, prefix):
        with self.lock, self.connection() as conn:
            return [json.loads(row[0]) for row in conn.execute('SELECT body FROM records WHERE id LIKE ?', (prefix + '%',))]

    def update(self, batch_id, change):
        with self.lock:
            batch = self.get('batch:' + batch_id)
            if batch is None:
                raise ValueError('批次不存在')
            change(batch)
            batch['updated_at'] = time.time()
            self.put('batch:' + batch_id, batch)
            return batch


class Application:
    def __init__(self, state_dir):
        self.store = Store(state_dir)
        self.closing = threading.Event()
        self.wake = threading.Event()
        self.scan_lock = threading.Lock()
        self.picker_lock = threading.Lock()
        self.review_lock = threading.Lock()
        self.sticker_lock = threading.RLock()
        self.keyframes = KeyframeCache(self.store.directory / 'keyframes')
        self.worker = None
        self.recover()
        from .scheduler import Scheduler
        self.scheduler = Scheduler(self)

    def bootstrap(self):
        library = self.store.get('library', {})
        preferences = self.store.get('preferences', {})
        scan = library.get('scan', {'videos': [], 'music': [], 'errors': []})
        scan['music'] = [tag_music_style(song, library.get('music_dir')) for song in scan.get('music', [])]
        return {'video_dir': library.get('video_dir', str(DATA / '哔哩哔哩')),
                'music_dir': library.get('music_dir', str(DATA / '去重歌曲03')),
                'output_dir': preferences.get('output_dir', str(DATA / 'exports')),
                'review_dir': preferences.get('review_dir', str(DATA / '审核通过')),
                'scan': scan,
                'batches': self.store.batches(),
                'sticker_catalog': self.sticker_catalog(),
                'ffmpeg_available': bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))}

    def sticker_catalog(self):
        return {'assets': self.store.get('sticker-assets', []),
                'templates': self.store.get('sticker-templates', [])}

    def sticker_asset(self, asset_id):
        asset = next((a for a in self.store.get('sticker-assets', []) if a['id'] == asset_id), None)
        if not asset or not Path(asset['path']).is_file():
            raise ValueError('贴图不存在，请重新导入')
        return asset

    def import_sticker(self, body):
        from . import stickers
        encoded = body.get('data', '')
        if not isinstance(encoded, str) or len(encoded) > 17_000_000:
            raise ValueError('贴图文件不能超过12MB')
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError('贴图上传内容无效')
        with self.sticker_lock:
            asset = stickers.import_image(data, str(body.get('name', '贴图')), self.store.directory / 'stickers')
            assets = self.store.get('sticker-assets', [])
            if not any(a['id'] == asset['id'] for a in assets):
                asset['created_at'] = time.time()
                assets.append(asset)
                self.store.put('sticker-assets', assets)
        return self.sticker_catalog()

    def save_sticker_template(self, body):
        from . import stickers
        name = str(body.get('name', '')).strip()
        if not name or len(name) > 80:
            raise ValueError('模板名称请填写1～80个字符')
        with self.sticker_lock:
            layers = stickers.resolve_layers(body.get('layers', []), self.store.get('sticker-assets', []))
            if not layers:
                raise ValueError('模板至少需要一张贴图；不添加贴图请直接选择“不添加贴图”')
            # Store portable layout only; server resolves trusted managed image files when used.
            portable = [{key: layer[key] for key in ('sticker_id', 'x', 'y', 'width', 'opacity', 'start', 'end')}
                        for layer in layers]
            templates = self.store.get('sticker-templates', [])
            template_id = body.get('id') or uuid.uuid4().hex[:12]
            old = next((t for t in templates if t['id'] == template_id), None)
            if body.get('id') and not old:
                raise ValueError('需要更新的贴图模板不存在')
            template = {'id': template_id, 'name': name, 'layers': portable,
                        'created_at': old['created_at'] if old else time.time(), 'updated_at': time.time()}
            templates = [t for t in templates if t['id'] != template_id] + [template]
            self.store.put('sticker-templates', templates)
        return {**self.sticker_catalog(), 'template': template}

    def resolve_sticker_template(self, template_id):
        from . import stickers
        if not template_id:
            return None, []
        template = next((t for t in self.store.get('sticker-templates', []) if t['id'] == template_id), None)
        if not template:
            raise ValueError('贴图模板不存在，请重新选择')
        return copy.deepcopy(template), stickers.resolve_layers(template['layers'], self.store.get('sticker-assets', []))

    def create_sticker_variant(self, body):
        from . import media, stickers
        with self.sticker_lock:
            if body.get('layers') is not None:
                layers = stickers.resolve_layers(body.get('layers'), self.store.get('sticker-assets', []))
                template = {'id': None, 'name': str(body.get('name') or '视频内手动贴图').strip(),
                            'layers': [{key: layer[key] for key in
                                        ('sticker_id', 'x', 'y', 'width', 'opacity', 'start', 'end')}
                                       for layer in layers]}
            else:
                template, layers = self.resolve_sticker_template(body.get('template_id'))
            if not layers:
                raise ValueError('请至少添加一张贴图')
            source_batch_id, source_item_id = str(body['batch_id']), str(body['item_id'])
            source_batch = self.batch(source_batch_id)
            source_item = next((i for i in source_batch['items'] if i['id'] == source_item_id), None)
            source = self.completed_output(source_batch_id, source_item_id)
            stat = source.stat()
            result = source_item.get('result', {})
            if result.get('output_mtime_ns') and (stat.st_mtime_ns != result['output_mtime_ns'] or
                                                stat.st_size != result.get('output_size')):
                raise ValueError('原成片已发生变化，不能直接套用模板')
            source_asset = {'id': self.file_digest(source), 'path': str(source), 'name': source.name,
                            'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
            media.verify_asset(source_asset)
            signature = hashlib.sha256(json.dumps([source_batch_id, source_item_id, source_asset, template],
                                                  ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            for existing in self.store.batches():
                if (existing.get('sticker_origin', {}).get('signature') == signature and
                        existing['status'] in {'queued', 'running', 'completed'} and
                        (existing['status'] != 'completed' or Path(existing['items'][0]['output_path']).is_file())):
                    return {'job_id': existing['id'], **self.sticker_variant_status(existing['id'])}
            output_root = Path(source_batch['config']['output_dir'])
            folder = self.reserve_output_folder(output_root)
            batch_id = uuid.uuid4().hex[:12]
            config = copy.deepcopy(source_batch['config'])
            config.update(count=1, sticker_template_id=template.get('id'),
                          sticker_template=template, sticker_layers=layers)
            styles = source_item.get('music_styles') or music_styles(source_item.get('music', []))
            filename = export_filename(styles, 1) if styles else source.name
            item = {'id': uuid.uuid4().hex, 'index': 1, 'kind': 'sticker_variant',
                    'duration': source_item['duration'], 'segments': [], 'music': [],
                    'music_source_folders': source_item.get('music_source_folders') or
                                            music_folders(source_item.get('music', [])),
                    'source_asset': source_asset, 'status': 'pending', 'progress': 0, 'error': None, 'attempts': 0,
                    'music_styles': styles, 'output_name': filename,
                    'output_path': str(folder / filename)}
            origin = {'batch_id': source_batch_id, 'item_id': source_item_id, 'signature': signature,
                      'template_name': template['name']}
            batch = {'id': batch_id, 'status': 'draft', 'created_at': time.time(), 'updated_at': time.time(),
                     'config': config, 'items': [item], 'assets': [], 'sticker_origin': origin,
                     'output_folder': str(folder), 'folder_name': folder.name,
                     'stats': {'count': 1, 'total_duration': item['duration']},
                     'warnings': ['这是追加贴图的新版本；原视频和原审核状态保留，音轨保持完整。']}
            self.store.put('batch:' + batch_id, batch)
            self.action(batch_id, 'start')
            return {'job_id': batch_id, **self.sticker_variant_status(batch_id)}

    def sticker_variant_status(self, job_id):
        batch = self.batch(job_id)
        if not batch.get('sticker_origin'):
            raise ValueError('该任务不是贴图版本任务')
        item = batch['items'][0]
        status = batch['status']
        if status in {'paused', 'stopped', 'pausing', 'stopping'}:
            status = 'failed'
        return {'id': batch['id'], 'status': status, 'progress': item['progress'],
                'error': item.get('error') or batch.get('error'),
                'batch_id': batch['sticker_origin']['batch_id'], 'item_id': batch['sticker_origin']['item_id'],
                'output_batch_id': batch['id']}

    def preferences(self, body):
        changes = {}
        for field, label in [('output_dir', '导出'), ('review_dir', '审核通过')]:
            if field not in body:
                continue
            value = str(body[field] or '').strip()
            if not value:
                raise ValueError(f'请填写{label}文件夹')
            path = Path(value).expanduser().resolve()
            if path.exists() and not path.is_dir():
                raise ValueError(f'{label}路径必须是文件夹')
            changes[field] = str(path)
        if not changes:
            raise ValueError('请提供需要保存的文件夹设置')
        with self.store.lock:
            preferences = self.store.get('preferences', {})
            preferences.update(changes)
            self.store.put('preferences', preferences)
        return preferences

    def pick_folder(self, body):
        kind = body.get('kind')
        prompts = {'video': '选择视频素材文件夹', 'music': '选择音乐素材文件夹', 'output': '选择导出文件夹',
                   'review': '选择审核通过视频的保存文件夹'}
        if kind not in prompts:
            raise ValueError('无效的文件夹类型')
        if not self.picker_lock.acquire(blocking=False):
            raise ValueError('已有文件夹选择窗口打开，请先完成或取消选择')
        try:
            path = choose_folder(prompts[kind], str(body.get('current_path', '')))
            if path and kind in {'output', 'review'} and body.get('persist', True):
                self.preferences({kind + '_dir': path})
            return {'path': path, 'cancelled': path is None}
        finally:
            self.picker_lock.release()

    def reserve_output_folder(self, output):
        day = datetime.now().strftime('%Y-%m-%d')
        key = f'folder-sequence:{output}:{day}'
        output.mkdir(parents=True, exist_ok=True)
        with self.store.lock:
            number = int(self.store.get(key, 0)) + 1
            while True:
                folder = output / f'{day}_{number:03d}'
                try:
                    folder.mkdir()
                    break
                except FileExistsError:
                    number += 1
            self.store.put(key, number)
        return folder

    def scan(self, body):
        from . import media
        with self.scan_lock:
            video_dir = Path(body['video_dir']).expanduser().resolve()
            music_dir = Path(body['music_dir']).expanduser().resolve()
            if not video_dir.is_dir() or not music_dir.is_dir():
                raise ValueError('视频和音乐路径必须是存在的文件夹')
            excluded = [str(ROOT / 'exports'), str(self.store.directory)]
            excluded += [str(batch_output_folder(batch)) for batch in self.store.batches()]
            excluded += [folder for batch in self.store.batches() for folder in batch.get('review_folders', {}).values()]
            result = media.scan(str(video_dir), str(music_dir), str(self.store.directory / 'cache'), exclude_dirs=excluded)
            self.store.put('library', {'video_dir': str(video_dir), 'music_dir': str(music_dir), 'scan': result})
            return result

    def create_plan(self, body):
        from . import planner
        config = dict(body.get('config', {}))
        template, layers = self.resolve_sticker_template(config.get('sticker_template_id'))
        config['sticker_template'] = template
        config['sticker_layers'] = layers
        config.setdefault('count', 50)
        count = int(config['count'])
        if not 1 <= count <= 500:
            raise ValueError('每批数量须为 1～500')
        config['count'] = count
        for field, default in [('width', 1280), ('height', 720), ('fps', 30)]:
            config[field] = int(config.get(field, default))
        if (config['width'], config['height']) not in [(1280, 720), (1920, 1080), (640, 360)]:
            raise ValueError('输出尺寸须为 720p、1080p 或 360p 验证规格')
        if config['fps'] not in [24, 25, 30, 60]:
            raise ValueError('不支持的帧率')
        if config.get('hardware', 'auto') not in {'auto', 'software', 'software_fast', 'videotoolbox'}:
            raise ValueError('编码方式无效')
        for field, default in [('original_volume', 0), ('music_volume', 1)]:
            config[field] = float(config.get(field, default))
            if not 0 <= config[field] <= 2:
                raise ValueError('音量须在 0～2 之间')
        library = self.store.get('library', {}).get('scan', {})
        if not library.get('videos') or not library.get('music'):
            raise ValueError('请先扫描视频和音乐素材')
        output = Path(config.get('output_dir') or self.bootstrap()['output_dir']).expanduser().resolve()
        if output.exists() and not output.is_dir():
            raise ValueError('导出路径必须是文件夹')
        config['output_dir'] = str(output)
        config.setdefault('seed', int(time.time() * 1000) % 2147483647)
        result = planner.plan(library['videos'], library['music'], config)
        if len(result['items']) != count:
            raise ValueError(f"只找到 {len(result['items'])}/{count} 条方案，请调整设置")
        batch = self.make_batch(config, result, library['videos'] + library['music'],
                                music_root=self.store.get('library', {}).get('music_dir'))
        self.preferences({'output_dir': str(output)})
        return batch

    def make_batch(self, config, result, assets, *, music_root=None, scheduled_run_id=None,
                   schedule_name=None, index_offset=0):
        config = copy.deepcopy(config)
        result = copy.deepcopy(result)
        output = Path(config['output_dir']).expanduser().resolve()
        batch_id = uuid.uuid4().hex[:12]
        total_duration = sum(item['duration'] for item in result['items'])
        bitrate_estimate = max(800_000, config['width'] * config['height'] * config['fps'] * 0.12) + 192_000
        estimated_bytes = int(total_duration * bitrate_estimate / 8)
        ancestor = output
        while not ancestor.exists():
            ancestor = ancestor.parent
        free_bytes = shutil.disk_usage(ancestor).free
        result.setdefault('stats', {}).update(estimated_output_bytes=estimated_bytes,
                                              total_duration=total_duration)
        result.setdefault('warnings', []).append(
            f'估计成片占用 {estimated_bytes / 1024**3:.2f} GB，目标磁盘可用 {free_bytes / 1024**3:.1f} GB。'
            '实际大小随编码变化；另需 TS 缓存和临时文件空间。')
        if estimated_bytes > free_bytes:
            result['warnings'].append('估计输出超过磁盘可用空间，请更换输出磁盘或减少数量。')
        items = result['items']
        output_folder = self.reserve_output_folder(output)
        for index, item in enumerate(items, 1):
            item['id'] = str(item.get('id') or index)
            item['index'] = index + index_offset
            item.update(status='pending', progress=0, error=None, attempts=0)
            item['music'] = [tag_music_style(song, music_root) for song in item.get('music', [])]
            item['music_source_folders'] = music_folders(item['music'])
            item['music_styles'] = music_styles(item['music'], music_root)
            item['output_name'] = export_filename(item['music_styles'], item['index'])
            item['output_path'] = str(output_folder / item['output_name'])
        batch = {'id': batch_id, 'status': 'draft', 'created_at': time.time(),
                 'updated_at': time.time(), 'config': config, 'items': items,
                 'stats': result.get('stats', {}), 'warnings': result.get('warnings', []),
                 'assets': assets,
                 'output_folder': str(output_folder), 'folder_name': output_folder.name}
        if scheduled_run_id:
            batch.update(scheduled_run_id=scheduled_run_id, schedule_name=schedule_name)
        self.store.put('batch:' + batch_id, batch)
        return batch

    def batch(self, batch_id):
        batch = self.store.get('batch:' + batch_id)
        if not batch:
            raise ValueError('批次不存在')
        return batch

    def reorder_music(self, batch_id, item_id, music_ids):
        if not isinstance(music_ids, list) or not all(isinstance(value, str) for value in music_ids):
            raise ValueError('歌曲顺序格式无效')

        def fingerprint(ids):
            return hashlib.sha256(repr(tuple(ids)).encode('utf-8')).hexdigest()

        def change(batch):
            if batch['status'] != 'draft':
                raise ValueError('只有尚未开始渲染的草稿方案可以调整歌曲顺序')
            item = next((entry for entry in batch['items'] if str(entry['id']) == str(item_id)), None)
            if item is None:
                raise ValueError('方案条目不存在')
            current_ids = [song['id'] for song in item.get('music', [])]
            if len(music_ids) != len(current_ids) or sorted(music_ids) != sorted(current_ids):
                raise ValueError('只能调整当前方案已有歌曲的顺序')
            candidate = fingerprint(music_ids)
            for other in batch['items']:
                if other is item:
                    continue
                other_ids = [song['id'] for song in other.get('music', [])]
                if fingerprint(other_ids) == candidate:
                    raise ValueError('该歌曲顺序与本批另一条方案重复，请换一个顺序')
            by_id = {song['id']: song for song in item['music']}
            item['music'] = [by_id[value] for value in music_ids]
            item['music_fingerprint'] = candidate
            item['manual_music_order'] = True
            batch.setdefault('stats', {})['unique_music_orders'] = len({
                fingerprint([song['id'] for song in entry.get('music', [])]) for entry in batch['items']
            })

        return self.store.update(batch_id, change)

    def action(self, batch_id, action):
        def change(batch):
            status = batch['status']
            if batch.get('scheduled_cancelled') and action in {'start', 'resume', 'retry'}:
                raise ValueError('该定时目标已取消，不能再继续旧任务')
            if action in ['start', 'resume', 'retry']:
                if status in ['running', 'queued', 'pausing', 'stopping']:
                    raise ValueError('批次正在执行，请等待当前操作完成')
                if action == 'retry':
                    for item in batch['items']:
                        if item['status'] == 'failed':
                            item.update(status='pending', attempts=0, error=None, progress=0)
                if all(item['status'] == 'success' for item in batch['items']):
                    batch['status'] = 'completed'
                    return
                if not any(item['status'] == 'pending' for item in batch['items']):
                    raise ValueError('没有待处理任务；失败条目请点击重试')
                output = batch_output_folder(batch)
                output.mkdir(parents=True, exist_ok=True)
                probe = output / ('.write-check-' + uuid.uuid4().hex)
                try:
                    probe.touch(exist_ok=False)
                finally:
                    probe.unlink(missing_ok=True)
                self.check_space(output)
                batch['status'] = 'queued'
            elif action == 'pause':
                if status == 'running':
                    batch['status'] = 'pausing'
                elif status == 'queued':
                    batch['status'] = 'paused'
                else:
                    raise ValueError('仅执行中或排队中的批次可以暂停')
            elif action == 'stop':
                batch['status'] = 'stopping' if status in ['running', 'pausing'] else 'stopped'
            else:
                raise ValueError('未知操作')
        result = self.store.update(batch_id, change)
        self.wake.set()
        return result

    @staticmethod
    def check_space(directory):
        if shutil.disk_usage(directory).free < 512 * 1024 * 1024:
            raise OSError('可用磁盘空间少于 512 MB，请清理空间后继续')

    def recover(self):
        for batch in self.store.batches():
            changed = False
            review_changed = False
            for item in batch['items']:
                if item.get('review', {}).get('status') == 'approved':
                    archived = Path(item['review'].get('path', ''))
                    sidecar = archived.with_suffix('.txt')
                    if archived.is_file() and not sidecar.exists():
                        try:
                            created = self.write_music_sidecar(item, archived)
                            if created:
                                item['review']['music_paths'] = str(created)
                                review_changed = True
                        except (OSError, ValueError):
                            pass
                if item.get('review', {}).get('status') == 'copying':
                    item['review'].update(status='failed', error='上次归档中断，请再次点击通过审核以恢复')
                    review_changed = True
                if item['status'] == 'success' and not Path(item['output_path']).is_file():
                    item.update(status='failed', error='已完成的输出文件已被移动或删除')
                    changed = True
                elif item['status'] == 'success' and item.get('result', {}).get('output_mtime_ns'):
                    stat = Path(item['output_path']).stat()
                    saved = item['result']
                    if stat.st_size != saved.get('output_size') or stat.st_mtime_ns != saved['output_mtime_ns']:
                        item.update(status='failed', error='已完成的输出文件已改变；请检查文件后重试')
                        changed = True
                elif item['status'] in ['running', 'validating']:
                    item.update(status='pending', progress=0, error='上次运行中断，继续后将核对输出并恢复')
                    changed = True
            if batch.get('scheduled_cancelled'):
                for item in batch['items']:
                    if item['status'] != 'success':
                        item.update(status='cancelled', error='定时目标已取消')
                batch['status'] = 'stopped'
                self.store.put('batch:' + batch['id'], batch)
            elif batch['status'] in ['running', 'queued', 'pausing', 'stopping'] or changed:
                batch['status'] = 'paused'
                batch['recovery_note'] = '后台已重启，请确认后继续；成功条目会保留。'
                self.store.put('batch:' + batch['id'], batch)
            elif review_changed:
                batch['updated_at'] = time.time()
                self.store.put('batch:' + batch['id'], batch)

    def start_worker(self):
        self.worker = threading.Thread(target=self.run_queue, daemon=True, name='render-queue')
        self.worker.start()
        self.scheduler.start()

    def run_queue(self):
        while not self.closing.is_set():
            candidates = [b for b in self.store.batches() if b['status'] == 'queued']
            if not candidates:
                self.wake.wait(1)
                self.wake.clear()
                continue
            batch = min(candidates, key=lambda b: b['created_at'])
            try:
                self.execute_batch(batch['id'])
            except Exception as exc:
                def failed(b):
                    b['status'] = 'paused'
                    b['error'] = str(exc)
                    for item in b['items']:
                        if item['status'] in ['running', 'validating']:
                            item.update(status='failed', error=str(exc))
                self.store.update(batch['id'], failed)

    def execute_batch(self, batch_id):
        from . import media, renderer
        def begin(batch):
            if batch['status'] == 'queued' and not batch.get('scheduled_cancelled'):
                batch['status'] = 'running'
                batch.pop('error', None)
        batch = self.store.update(batch_id, begin)
        if batch['status'] != 'running':
            return
        for index in range(len(batch['items'])):
            batch = self.batch(batch_id)
            if batch.get('scheduled_cancelled'):
                self.scheduler.finish_cancelled_batch(batch_id)
                return
            if self.closing.is_set() or batch['status'] in ['pausing', 'stopping']:
                state = 'stopped' if batch['status'] == 'stopping' else 'paused'
                self.store.update(batch_id, lambda b: b.update(status=state))
                return
            item = batch['items'][index]
            if item['status'] != 'pending':
                continue
            output = Path(item['output_path'])
            try:
                output.parent.mkdir(parents=True, exist_ok=True)
                self.check_space(output.parent)
            except OSError as exc:
                self.store.update(batch_id, lambda b: b.update(status='paused', error=str(exc)))
                return
            used_ids = {seg['asset_id'] for seg in item['segments']} | {song['id'] for song in item['music']}
            try:
                if item.get('kind') == 'sticker_variant':
                    media.verify_asset(item['source_asset'])
                for asset in batch.get('assets', []):
                    if asset['id'] in used_ids:
                        if media.verify_asset(asset) is False:
                            raise ValueError('素材已变化：' + asset.get('name', asset['path']))
            except (ValueError, OSError) as exc:
                self.store.update(batch_id, lambda b: b.update(status='paused', error=str(exc)))
                return
            # A crash after final rename but before SQLite commit must not duplicate an export.
            if output.exists():
                try:
                    metadata = renderer.validate(str(output), item['duration'])
                    metadata.update(output_size=output.stat().st_size, output_mtime_ns=output.stat().st_mtime_ns)
                    self.store.update(batch_id, lambda b: b['items'][index].update(
                        status='success', progress=1, result=metadata, error=None))
                    continue
                except Exception:
                    self.store.update(batch_id, lambda b: b['items'][index].update(
                        status='failed', error='输出位置已有文件但校验失败；请移走该文件后重试，程序不会覆盖'))
                    continue
            for attempt in range(int(item.get('attempts', 0)), 3):
                self.store.update(batch_id, lambda b: b['items'][index].update(
                    status='running', attempts=attempt + 1, progress=0, error=None))
                last_update = [0.0]
                def progress(value, *args, **kwargs):
                    now = time.monotonic()
                    stage = value.get('stage') if isinstance(value, dict) else None
                    if stage != 'complete' and self.batch(batch_id).get('scheduled_cancelled'):
                        from .scheduler import ScheduledRunCancelled
                        raise ScheduledRunCancelled('新定时任务已启动，旧目标取消')
                    if now - last_update[0] < 0.5 and stage != 'validating':
                        return
                    last_update[0] = now
                    if isinstance(value, dict):
                        value = value.get('progress', 0)
                    try:
                        value = min(0.99, max(0, float(value)))
                    except (ValueError, TypeError):
                        return
                    self.store.update(batch_id, lambda b: b['items'][index].update(
                        progress=value, status='validating' if stage == 'validating' else 'running'))
                try:
                    render_config = dict(batch['config'], cache_dir=str(self.store.directory / 'cache'))
                    work_dir = str(self.store.directory / 'work' / batch_id / item['id'])
                    if item.get('kind') == 'sticker_variant':
                        result = renderer.overlay_existing(item['source_asset']['path'], render_config['sticker_layers'],
                                                           str(output), work_dir, progress)
                    else:
                        result = renderer.render(item, render_config, str(output), work_dir, progress)
                    result.update(output_size=output.stat().st_size, output_mtime_ns=output.stat().st_mtime_ns)
                    self.store.update(batch_id, lambda b: b['items'][index].update(
                        status='success', progress=1, error=None, result=result))
                    break
                except Exception as exc:
                    if self.batch(batch_id).get('scheduled_cancelled'):
                        self.scheduler.finish_cancelled_batch(batch_id)
                        return
                    message = str(exc)
                    self.store.update(batch_id, lambda b: b['items'][index].update(
                        status='failed', error=message))
                    current = self.batch(batch_id)
                    if self.closing.is_set() or current['status'] in ['pausing', 'stopping']:
                        break
                    if 'space' in message.lower() or '空间' in message:
                        self.store.update(batch_id, lambda b: b.update(status='pausing', error=message))
                        break
            self.write_manifest(batch_id)
        batch = self.batch(batch_id)
        completed = all(item['status'] == 'success' for item in batch['items'])
        status = 'completed' if completed else ('stopped' if batch['status'] == 'stopping' else 'paused' if batch['status'] == 'pausing' else 'failed')
        self.store.update(batch_id, lambda b: b.update(status=status))
        self.write_manifest(batch_id)

    def write_manifest(self, batch_id):
        batch = self.batch(batch_id)
        directory = batch_output_folder(batch)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / '.manifest.json.tmp'
        temporary.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(directory / 'manifest.json')
        for item in batch.get('items', []):
            if item.get('status') != 'success':
                continue
            self.write_music_sidecar(item, item['output_path'], replace=True)

    @staticmethod
    def write_music_sidecar(item, video_path, *, replace=False):
        folders = item.get('music_source_folders') or music_folders(item.get('music', []))
        if not folders:
            return None
        sidecar = Path(video_path).with_suffix('.txt')
        content = '\n'.join(folders) + '\n'
        if sidecar.exists() and not replace:
            if sidecar.is_file() and sidecar.read_text(encoding='utf-8') == content:
                return sidecar
            raise ValueError('审核文件夹中已有同名的不同 TXT，程序不会覆盖；请选择其他审核目录')
        temporary = sidecar.with_name('.' + sidecar.name + '-' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(content, encoding='utf-8')
            flush_to_disk(temporary)
            if replace:
                os.replace(temporary, sidecar)
            else:
                publish(temporary, sidecar)
        finally:
            temporary.unlink(missing_ok=True)
        return sidecar

    def asset(self, asset_id):
        scan = self.store.get('library', {}).get('scan', {})
        for asset in scan.get('videos', []) + scan.get('music', []):
            if asset['id'] == asset_id:
                return asset
        raise ValueError('素材不存在，请重新扫描')

    def completed_output(self, batch_id, item_id):
        batch = self.batch(batch_id)
        item = next((i for i in batch['items'] if i['id'] == str(item_id)), None)
        if not item or item['status'] != 'success':
            raise ValueError('该成片尚未完成或不存在')
        path = Path(item['output_path'])
        if not path.is_file():
            raise ValueError('成片文件不存在，可能已被移动或删除')
        return path

    @staticmethod
    def file_digest(path):
        digest = hashlib.sha256()
        with Path(path).open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def approve(self, body):
        batch_id, item_id = str(body['batch_id']), str(body['item_id'])
        # Serialize approvals, not rendering. Double clicks cannot copy the same item twice.
        with self.review_lock:
            batch = self.batch(batch_id)
            index = next((n for n, item in enumerate(batch['items']) if item['id'] == item_id), None)
            if index is None or batch['items'][index]['status'] != 'success':
                raise ValueError('只能审核已成功生成的视频')
            item = batch['items'][index]
            previous = item.get('review', {})
            if previous.get('status') == 'approved':
                archived = Path(previous['path'])
                if archived.is_file() and self.file_digest(archived) == previous.get('sha256'):
                    self.write_music_sidecar(item, archived)
                    return batch
                raise ValueError('审核归档文件已被移动或修改，请检查原保存位置')
            source = self.completed_output(batch_id, item_id)
            source_stat = source.stat()
            expected = item.get('result', {})
            if expected.get('output_mtime_ns') and (
                    source_stat.st_size != expected.get('output_size') or
                    source_stat.st_mtime_ns != expected['output_mtime_ns']):
                raise ValueError('原成片已被修改，请确认文件后重新生成，不能直接通过审核')
            root_text = body.get('review_dir')
            if root_text is None:
                root_text = self.store.get('preferences', {}).get('review_dir', str(ROOT / '审核通过'))
            root_text = str(root_text).strip()
            if not root_text:
                raise ValueError('请先设置审核通过文件夹')
            root = Path(root_text).expanduser().resolve()
            root.mkdir(parents=True, exist_ok=True)
            if root == batch_output_folder(batch).resolve():
                raise ValueError('审核通过文件夹不能与当前批次的原导出子文件夹相同')
            folders = batch.get('review_folders', {})
            if str(root) in folders:
                folder = Path(folders[str(root)])
                folder.mkdir(parents=True, exist_ok=True)
            else:
                folder = self.reserve_output_folder(root)
                self.store.update(batch_id, lambda b: b.setdefault('review_folders', {}).update({str(root): str(folder)}))
            target = folder / source.name
            pending = {'status': 'copying', 'path': str(target), 'review_dir': str(root)}
            self.store.update(batch_id, lambda b: b['items'][index].update(review=pending))
            temporary = folder / ('.' + source.stem + '-' + uuid.uuid4().hex + '.review.tmp')
            try:
                digest = self.file_digest(source)
                if target.exists():
                    if not target.is_file() or self.file_digest(target) != digest:
                        raise ValueError('审核文件夹中已有同名的不同文件，程序不会覆盖；请选择其他审核目录')
                else:
                    if shutil.disk_usage(folder).free < source_stat.st_size + 64 * 1024 * 1024:
                        raise OSError('审核目录可用空间不足，原成片保留，请更换目录后重试')
                    shutil.copyfile(source, temporary)
                    flush_to_disk(temporary)
                    after = source.stat()
                    if after.st_size != source_stat.st_size or after.st_mtime_ns != source_stat.st_mtime_ns:
                        raise ValueError('复制期间原成片发生变化，请检查文件后重试')
                    if self.file_digest(temporary) != digest:
                        raise ValueError('审核副本完整性校验失败，请重试')
                    publish(temporary, target)
                sidecar = self.write_music_sidecar(item, target)
                approved = {'status': 'approved', 'path': str(target), 'review_dir': str(root),
                            'approved_at': datetime.now().astimezone().isoformat(timespec='seconds'),
                            'sha256': digest, 'size': target.stat().st_size,
                            'music_paths': str(sidecar) if sidecar else None}
                result = self.store.update(batch_id, lambda b: b['items'][index].update(review=approved))
                self.preferences({'review_dir': str(root)})
                return result
            except Exception as exc:
                self.store.update(batch_id, lambda b: b['items'][index].update(
                    review={**pending, 'status': 'failed', 'error': str(exc)}))
                raise
            finally:
                temporary.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    server_version = 'MixCut/1.0'

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        if args and str(args[0]).startswith(('POST', 'GET /api/scan')):
            super().log_message(fmt, *args)

    def guard(self):
        host = self.headers.get('Host', '')
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        if host not in allowed:
            raise PermissionError('仅允许本机访问')
        origin = self.headers.get('Origin')
        if origin and origin not in {'http://' + value for value in allowed}:
            raise PermissionError('拒绝跨站请求')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise PermissionError('拒绝跨站请求')

    def json_response(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.route(False)

    def do_POST(self):
        self.route(True)

    def route(self, post):
        try:
            self.guard()
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if post:
                if not self.headers.get('Content-Type', '').startswith('application/json'):
                    raise ValueError('请使用 JSON 请求')
                size = int(self.headers.get('Content-Length', 0))
                if size < 0 or size > (18_000_000 if path == '/api/stickers/import' else 2_000_000):
                    raise ValueError('请求过大')
                body = json.loads(self.rfile.read(size) or '{}')
                if path == '/api/stickers/import':
                    return self.json_response(self.app.import_sticker(body))
                if path == '/api/sticker-templates':
                    return self.json_response(self.app.save_sticker_template(body))
                if path == '/api/sticker-variants':
                    return self.json_response(self.app.create_sticker_variant(body))
                if path == '/api/scan':
                    return self.json_response(self.app.scan(body))
                if path == '/api/schedules':
                    return self.json_response(self.app.scheduler.save(body))
                if path == '/api/scheduler/check':
                    return self.json_response(self.app.scheduler.check_now())
                schedule_match = re.fullmatch(r'/api/schedules/([a-f0-9]+)/(enable|disable|run-now)', path)
                if schedule_match:
                    return self.json_response(self.app.scheduler.action(*schedule_match.groups()))
                if path == '/api/pick-folder':
                    return self.json_response(self.app.pick_folder(body))
                if path == '/api/preferences':
                    return self.json_response(self.app.preferences(body))
                if path == '/api/approve':
                    return self.json_response(self.app.approve(body))
                if path == '/api/plan':
                    return self.json_response(self.app.create_plan(body))
                music_order_match = re.fullmatch(r'/api/batches/([a-f0-9]+)/items/([^/]+)/music-order', path)
                if music_order_match:
                    return self.json_response(self.app.reorder_music(
                        music_order_match[1], music_order_match[2], body.get('music_ids')))
                match = re.fullmatch(r'/api/batches/([a-f0-9]+)/([a-z]+)', path)
                if match:
                    return self.json_response(self.app.action(*match.groups()))
                if path == '/api/reveal':
                    batch = self.app.batch(body['batch_id'])
                    target = batch_output_folder(batch)
                    if body.get('item_id'):
                        item = next(i for i in batch['items'] if i['id'] == str(body['item_id']))
                        if body.get('review'):
                            if item.get('review', {}).get('status') != 'approved':
                                raise ValueError('该视频尚未通过审核')
                            target = Path(item['review']['path'])
                        else:
                            target = Path(item['output_path'])
                    if not target.exists():
                        raise ValueError('输出尚未生成')
                    reveal(target)
                    return self.json_response({'ok': True})
                if path == '/api/shutdown':
                    if any(b['status'] in ['running', 'pausing', 'stopping'] for b in self.app.store.batches()):
                        raise ValueError('请先暂停批次，等待当前成片完成后退出')
                    self.json_response({'ok': True})
                    self.app.closing.set()
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
            else:
                if path == '/api/schedules':
                    return self.json_response(self.app.scheduler.listing())
                if path == '/api/stickers':
                    return self.json_response(self.app.sticker_catalog())
                if path == '/api/sticker':
                    return self.send_file(Path(self.app.sticker_asset(query.get('id', [''])[0])['path']))
                variant_match = re.fullmatch(r'/api/sticker-variants/([a-f0-9]+)', path)
                if variant_match:
                    return self.json_response(self.app.sticker_variant_status(variant_match[1]))
                if path == '/api/bootstrap':
                    return self.json_response(self.app.bootstrap())
                if path == '/api/batches':
                    return self.json_response(self.app.store.batches())
                match = re.fullmatch(r'/api/batches/([a-f0-9]+)', path)
                if match:
                    return self.json_response(self.app.batch(match[1]))
                if path in ['/api/media', '/api/thumbnail']:
                    asset = self.app.asset(query.get('id', [''])[0])
                    target = Path(asset['path'])
                    if path == '/api/thumbnail':
                        target = self.app.store.directory / 'thumbnails' / (asset['id'] + '.jpg')
                        if not target.exists():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', '1', '-i', asset['path'],
                                            '-frames:v', '1', '-vf', 'scale=320:-2', str(target)],
                                           check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    return self.send_file(target)
                if path in ['/api/output', '/api/keyframes', '/api/keyframe']:
                    output = self.app.completed_output(query['batch'][0], query['item'][0])
                    if path == '/api/keyframes':
                        return self.json_response(self.app.keyframes.describe(output))
                    if path == '/api/keyframe':
                        target = self.app.keyframes.image(output, int(query['index'][0]),
                                                          query.get('size', ['thumb'])[0],
                                                          query.get('v', [None])[0])
                        return self.send_file(target)
                    return self.send_file(output)
                if path == '/api/manifest':
                    return self.json_response(self.app.batch(query['batch'][0]))
                files = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css',
                         '/static/app.js': 'app.js', '/static/style.css': 'style.css'}
                if path in files:
                    return self.send_file(ROOT / 'static' / files[path])
            self.json_response({'error': '接口不存在'}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except PermissionError as exc:
            self.json_response({'error': str(exc)}, 403)
        except (ValueError, KeyError, StopIteration, FileNotFoundError) as exc:
            self.json_response({'error': str(exc) or '请求的资源不存在'}, 400)
        except Exception as exc:
            self.json_response({'error': str(exc)}, 500)

    def send_file(self, path):
        if not path.is_file():
            raise FileNotFoundError('文件不存在')
        total = path.stat().st_size
        start, end, partial = 0, total - 1, False
        header = self.headers.get('Range')
        if header:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
            if not match or not any(match.groups()):
                self.send_error(416)
                return
            left, right = match.groups()
            if left:
                start = int(left)
                end = min(int(right), total - 1) if right else total - 1
            else:
                start = max(0, total - int(right))
            if start > end or start >= total:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{total}')
                self.end_headers()
                return
            partial = True
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(max(0, end - start + 1)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        if partial:
            self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
        self.end_headers()
        with path.open('rb') as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                data = stream.read(min(256 * 1024, remaining))
                if not data:
                    break
                self.wfile.write(data)
                remaining -= len(data)


def main(argv=None):
    activate_bundled_tools()
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8877)
    parser.add_argument('--state-dir', default=str(DATA / '.mixcut'))
    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = (state_dir / 'server.lock').open('a+')
    try:
        acquire_lock(lock)
    except LockUnavailable:
        raise SystemExit('后台已运行，请打开 http://127.0.0.1:8877')
    app = Application(state_dir)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    except OSError as exc:
        release_lock(lock)
        raise SystemExit(f'无法监听 127.0.0.1:{args.port}，端口可能已被其他程序占用（{exc}）')
    server.app = app
    app.start_worker()
    print(f'MixCut ready: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.closing.set()
        server.server_close()
        release_lock(lock)


if __name__ == '__main__':
    main()
