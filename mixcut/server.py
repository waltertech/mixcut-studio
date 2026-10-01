"""Loopback-only HTTP interface and durable single-worker queue."""
from __future__ import annotations

import argparse
import base64
import binascii
import copy
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import logging
import math
import mimetypes
import os
from pathlib import Path
import random
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
from .runtime import API_PROTOCOL, activate_bundled_tools, app_version, data_root, resource_root

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

    def delete(self, key):
        with self.lock, self.connection() as conn:
            conn.execute('DELETE FROM records WHERE id=?', (key,))


class Application:
    def __init__(self, state_dir):
        # Preserve the process version even if an installer replaces VERSION on disk.
        self.running_version = app_version()
        self.store = Store(state_dir)
        self.closing = threading.Event()
        self.wake = threading.Event()
        self.cache_lock = threading.RLock()
        self.cache_users = {}
        self.cache_preview_users = 0
        self.scan_lock = threading.Lock()
        self.scan_state_lock = threading.RLock()
        self.scan_job = None
        self.thumbnail_semaphore = threading.BoundedSemaphore(2)
        self.picker_lock = threading.Lock()
        self.review_lock = threading.RLock()
        self.review_job_lock = threading.Lock()
        self.review_jobs = {}
        self.global_review_job = {'status': 'idle', 'total': 0, 'completed': 0}
        self.archive_verifications = set()
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
        with self.review_job_lock:
            review_jobs = {batch_id: dict(job) for batch_id, job in self.review_jobs.items()}
            global_review_job = dict(self.global_review_job)
        return {'video_dir': library.get('video_dir', str(DATA / '哔哩哔哩')),
                'music_dir': library.get('music_dir', str(DATA / '去重歌曲03')),
                'output_dir': preferences.get('output_dir', str(DATA / 'exports')),
                'review_dir': preferences.get('review_dir', str(DATA / '审核通过')),
                'scan': scan,
                'scan_job': self.scan_status() if self.scan_job else None,
                'batches': self.visible_batches(),
                'review_jobs': review_jobs,
                'global_review_job': global_review_job,
                'sticker_catalog': self.sticker_catalog(),
                'version': self.running_version,
                'api_protocol': API_PROTOCOL,
                'active_work': any(batch['status'] in {'queued', 'running', 'pausing', 'stopping'}
                                   for batch in self.store.batches()),
                'ffmpeg_available': bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))}

    @contextmanager
    def cached_preview(self):
        with self.cache_lock:
            self.cache_preview_users += 1
        try:
            yield
        finally:
            with self.cache_lock:
                self.cache_preview_users -= 1

    def cache_status(self):
        """Measure generated video conversion and render-work files without reading contents."""
        total = count = 0
        for root in tuple(self.store.directory / name for name in ('cache', 'work', 'thumbnails', 'keyframes')):
            if root.is_symlink():
                continue
            for directory, _, names in os.walk(root, followlinks=False):
                for name in names:
                    path = Path(directory) / name
                    try:
                        if path.is_symlink():
                            continue
                        stat = path.stat()
                        total += stat.st_blocks * 512 if hasattr(stat, 'st_blocks') else stat.st_size
                        count += 1
                    except FileNotFoundError:
                        pass
        return {'bytes': total, 'files': count}

    def _remove_cache_files(self, asset_ids=None):
        protected_ids = {identity for ids in self.cache_users.values() for identity in ids}
        removed = 0
        errors = []
        cache = self.store.directory / 'cache' / 'seekable'
        if not (self.store.directory / 'cache').is_symlink() and not cache.is_symlink():
            for path in cache.glob('*'):
                # Only generated cache files are managed; never follow links or user paths.
                identity = path.name.split('.')[0]
                if path.is_symlink() or not path.is_file() or identity in protected_ids:
                    continue
                if asset_ids is not None and identity not in asset_ids:
                    continue
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except OSError as exc:
                    errors.append(str(exc))
        return removed, errors

    def clear_cache(self):
        with self.cache_lock:
            removed, errors = self._remove_cache_files()
            work = self.store.directory / 'work'
            if not work.is_symlink():
                for batch_dir in work.glob('*'):
                    if batch_dir.is_symlink() or not batch_dir.is_dir():
                        continue
                    for task_dir in batch_dir.iterdir():
                        if task_dir.is_symlink() or (batch_dir.name, task_dir.name) in self.cache_users:
                            continue
                        try:
                            if task_dir.is_dir():
                                shutil.rmtree(task_dir)
                            else:
                                task_dir.unlink(missing_ok=True)
                        except OSError as exc:
                            errors.append(str(exc))
            if not self.cache_preview_users:
                for name in ('thumbnails', 'keyframes'):
                    root = self.store.directory / name
                    if root.is_symlink():
                        continue
                    for path in root.glob('*'):
                        if path.is_symlink():
                            continue
                        try:
                            if path.is_dir():
                                shutil.rmtree(path)
                            else:
                                path.unlink(missing_ok=True)
                        except OSError as exc:
                            errors.append(str(exc))
            return dict(self.cache_status(), removed_files=removed,
                        active_tasks=len(self.cache_users) + self.cache_preview_users, errors=errors)

    def cleanup_task_cache(self, batch, item):
        ids = {part['asset_id'] for part in item.get('segments', []) if part.get('asset_id')}
        origin = batch.get('sticker_origin')
        if origin:
            original_batch = self.store.get('batch:' + origin['batch_id'])
            original = next((entry for entry in (original_batch or {}).get('items', [])
                             if entry['id'] == origin['item_id']), None)
            if original:
                ids.update(part['asset_id'] for part in original.get('segments', []) if part.get('asset_id'))
        with self.cache_lock:
            pending = set(self.store.get('cache-cleanup-pending', [])) | ids
            previews = set(self.store.get('cache-preview-cleanup-pending', []))
            try:
                previews.add(self.keyframes._version(item['output_path']))
            except (KeyError, OSError):
                pass
            self.store.put('cache-preview-cleanup-pending', sorted(previews))
            self.store.put('cache-cleanup-pending', sorted(pending))
            self._drain_cache_cleanup()

    def _drain_cache_cleanup(self):
        with self.cache_lock:
            previews = set(self.store.get('cache-preview-cleanup-pending', []))
            if not self.cache_preview_users and not self.keyframes.root.is_symlink():
                remaining_previews = []
                for version in previews:
                    if not re.fullmatch(r'[a-f0-9]{24}', version):
                        continue
                    target = self.keyframes.root / version
                    try:
                        if not target.is_symlink() and target.is_dir():
                            shutil.rmtree(target)
                    except OSError:
                        remaining_previews.append(version)
                self.store.put('cache-preview-cleanup-pending', remaining_previews)
            pending = set(self.store.get('cache-cleanup-pending', []))
            if not pending:
                return
            self._remove_cache_files(pending)
            remaining = {identity for identity in pending
                         if any((self.store.directory / 'cache' / 'seekable').glob(identity + '.*'))}
            self.store.put('cache-cleanup-pending', sorted(remaining))

    def render_with_cache(self, batch_id, item, render):
        key = (batch_id, item['id'])
        with self.cache_lock:
            self.cache_users[key] = {part['asset_id'] for part in item.get('segments', [])}
        try:
            return render()
        finally:
            with self.cache_lock:
                self.cache_users.pop(key, None)
                try:
                    self._drain_cache_cleanup()
                except OSError:
                    logging.exception('deferred cache cleanup failed')

    def retry_failed_tasks(self):
        retried = 0
        errors = []
        for batch in self.visible_batches():
            for item in batch['items']:
                if item['status'] != 'failed':
                    continue
                try:
                    self.item_action(batch['id'], item['id'], 'start')
                    retried += 1
                except (ValueError, OSError) as exc:
                    errors.append(str(exc))
        return {'retried': retried, 'errors': errors}

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
            if source == Path(source_item['output_path']) and result.get('output_mtime_ns') and (
                                                stat.st_mtime_ns != result['output_mtime_ns'] or
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
                      'template_name': template['name'], 'replace_origin_on_approval': True}
            batch = {'id': batch_id, 'status': 'draft', 'created_at': time.time(), 'updated_at': time.time(),
                     'config': config, 'items': [item], 'assets': [], 'sticker_origin': origin,
                     'output_folder': str(folder), 'folder_name': folder.name,
                     'stats': {'count': 1, 'total_duration': item['duration']},
                     'warnings': ['这是追加贴图的新版本；审核新版本后会清理未审核的原成片和可安全删除的原始素材。']}
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

    def scan_status(self, compact=False):
        with self.scan_state_lock:
            if compact and self.scan_job and self.scan_job['status'] in {'discovering', 'analyzing'}:
                return {key: copy.deepcopy(value) for key, value in self.scan_job.items() if key != 'scan'}
            return copy.deepcopy(self.scan_job)

    def start_scan(self, body):
        from . import media
        kind = body.get('kind', 'both')
        if kind not in {'video', 'music', 'both'}:
            raise ValueError('扫描类型必须是 video、music 或 both')
        video_dir = Path(body['video_dir']).expanduser().resolve()
        music_dir = Path(body['music_dir']).expanduser().resolve()
        if not video_dir.is_dir() or not music_dir.is_dir():
            raise ValueError('视频和音乐路径必须是存在的文件夹')
        old = self.store.get('library', {})
        if not old.get('scan'):
            kind = 'both'
        kinds = ('video', 'music') if kind == 'both' else (kind,)
        for candidate, path in (('video', video_dir), ('music', music_dir)):
            if candidate not in kinds and str(path) != old.get(candidate + '_dir'):
                raise ValueError('另一个素材路径也已更改，请同时刷新两个素材库')
        if not self.scan_lock.acquire(blocking=False):
            raise ValueError('已有素材扫描正在进行，请等待完成')
        job = {'id': uuid.uuid4().hex[:12], 'status': 'discovering', 'kind': kind,
               'video_dir': str(video_dir), 'music_dir': str(music_dir),
               'totals': {}, 'processed': {'video': 0, 'music': 0},
               'scan': {'videos': [] if 'video' in kinds else list(old.get('scan', {}).get('videos', [])),
                        'music': [] if 'music' in kinds else list(old.get('scan', {}).get('music', [])),
                        'errors': []}}
        with self.scan_state_lock:
            self.scan_job = job

        def progress(event):
            with self.scan_state_lock:
                if event['phase'] == 'inventory':
                    job['totals'] = event['totals']
                    job['status'] = 'analyzing'
                else:
                    job['processed'][event['kind']] += 1
                    if event['asset']:
                        key = 'videos' if event['kind'] == 'video' else 'music'
                        job['scan'][key].append(event['asset'])

        def work():
            try:
                excluded = [str(ROOT / 'exports'), str(self.store.directory)]
                excluded += [str(batch_output_folder(batch)) for batch in self.store.batches()]
                excluded += [folder for batch in self.store.batches()
                             for folder in batch.get('review_folders', {}).values()]
                result = media.scan(str(video_dir), str(music_dir), str(self.store.directory / 'cache'),
                                    exclude_dirs=excluded, kinds=kinds, previous=old.get('scan'),
                                    progress=progress, quick_music=True)
                self.store.put('library', {'video_dir': str(video_dir), 'music_dir': str(music_dir),
                                           'scan': result})
                with self.scan_state_lock:
                    job['scan'] = result
                    job['status'] = 'completed'
            except Exception as exc:
                with self.scan_state_lock:
                    job['status'] = 'failed'
                    job['error'] = str(exc)
            finally:
                self.scan_lock.release()

        try:
            threading.Thread(target=work, daemon=True, name='media-scan').start()
        except Exception as exc:
            with self.scan_state_lock:
                job['status'] = 'failed'
                job['error'] = str(exc)
            self.scan_lock.release()
            raise
        return self.scan_status()

    def create_plan(self, body):
        from . import media, planner
        with self.scan_state_lock:
            if self.scan_job and self.scan_job['status'] in {'discovering', 'analyzing'}:
                raise ValueError('素材库仍在扫描，请等待完成后再生成方案')
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
        try:
            config['video_bitrate_mbps'] = float(config.get('video_bitrate_mbps') or 0)
        except (TypeError, ValueError):
            raise ValueError('视频码率须为 0～50 Mbps')
        if not math.isfinite(config['video_bitrate_mbps']) or not 0 <= config['video_bitrate_mbps'] <= 50:
            raise ValueError('视频码率须为 0～50 Mbps')
        for field, default in [('original_volume', 0), ('music_volume', 1)]:
            config[field] = float(config.get(field, default))
            if not 0 <= config[field] <= 2:
                raise ValueError('音量须在 0～2 之间')
        library_record = self.store.get('library', {})
        library = library_record.get('scan', {})
        if not library.get('videos') or not library.get('music'):
            raise ValueError('请先扫描视频和音乐素材')
        selected_music = set(config.get('music_ids') or [song['id'] for song in library['music']])
        prepared_music = media.precise_music(library['music'], selected_music,
                                             str(self.store.directory / 'cache'))
        if prepared_music != library['music']:
            library = dict(library, music=prepared_music)
            library_record['scan'] = library
            self.store.put('library', library_record)
        output = Path(config.get('output_dir') or self.bootstrap()['output_dir']).expanduser().resolve()
        if output.exists() and not output.is_dir():
            raise ValueError('导出路径必须是文件夹')
        config['output_dir'] = str(output)
        config['source_video_dir'] = self.store.get('library', {}).get('video_dir')
        config.setdefault('seed', random.SystemRandom().randrange(2**63))
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
        bitrate_estimate = (config['video_bitrate_mbps'] * 1_000_000 if config.get('video_bitrate_mbps')
                            else max(800_000, config['width'] * config['height'] * config['fps'] * 0.12)) + 192_000
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

    @staticmethod
    def _fingerprint(value):
        return hashlib.sha256(repr(value).encode('utf-8')).hexdigest()

    def _update_plan_counts(self, batch):
        from . import planner
        items = batch['items']
        stats = batch.setdefault('stats', {})
        stats.update(planner.annotate_batch(items))
        stats['count'] = len(items)
        stats['total_duration'] = sum(float(item.get('duration', 0)) for item in items)
        config = batch.get('config', {})
        bitrate = (config.get('video_bitrate_mbps', 0) * 1_000_000 if config.get('video_bitrate_mbps')
                   else max(800_000, config.get('width', 1280) * config.get('height', 720) *
                            config.get('fps', 30) * 0.12)) + 192_000
        stats['estimated_output_bytes'] = int(stats['total_duration'] * bitrate / 8)
        stats['unique_music_orders'] = len({self._fingerprint(tuple(song['id'] for song in item['music']))
                                             for item in items})
        stats['unique_video_plans'] = len({self._fingerprint(planner.video_plan_key(
            item['segments'], batch.get('config', {}).get('fps', 30))) for item in items})
        warnings = [message for message in batch.get('warnings', []) if not (
            isinstance(message, str) and message.startswith((
                '估计成片占用 ', '当前方案估计成片占用 ', '估计输出超过磁盘可用空间')))]
        warnings.append(f"当前方案估计成片占用 {stats['estimated_output_bytes'] / 1024**3:.2f} GB；实际大小随编码变化。")
        batch['warnings'] = warnings

    def _set_item_music(self, batch, item, order):
        root = self.store.get('library', {}).get('music_dir')
        item['music'] = [tag_music_style(song, root) for song in order]
        item['music_total_duration'] = sum(float(song['duration']) for song in order)
        item['music_fingerprint'] = self._fingerprint(tuple(song['id'] for song in order))
        item['music_source_folders'] = music_folders(item['music'])
        item['music_styles'] = music_styles(item['music'], root)
        item['output_name'] = export_filename(item['music_styles'], item['index'])
        item['output_path'] = str(Path(batch['output_folder']) / item['output_name'])
        self._update_plan_counts(batch)

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
            self._update_plan_counts(batch)

        return self.store.update(batch_id, change)

    def _choose_item_music(self, batch, item, target, *, avoid_current=False):
        from . import planner
        selected = set(batch.get('config', {}).get('music_ids') or [])
        music = [asset for asset in batch.get('assets', []) if asset.get('id') in selected]
        usage = defaultdict(int)
        for other in batch['items']:
            if other is not item:
                for song in other.get('music', []):
                    usage[song['id']] += 1
        old_ids = tuple(song['id'] for song in item.get('music', []))
        used_orders = {tuple(song['id'] for song in other['music']) for other in batch['items'] if other is not item}
        for attempt in range(24):
            order, _ = planner.allocate_music(music, target, batch['config'],
                                              random.Random(time.time_ns() + attempt), usage)
            ids = tuple(song['id'] for song in order)
            if ids not in used_orders and (not avoid_current or ids != old_ids):
                return order
        raise ValueError('没有找到符合整批使用次数和时长要求的另一组音乐')

    def refresh_music(self, batch_id, item_id):
        def change(batch):
            if batch['status'] != 'draft':
                raise ValueError('只有草稿方案可以刷新音乐')
            if batch['config'].get('music_mode', 'pool') != 'pool':
                raise ValueError('固定歌曲集合模式不能随机刷新；请选择候选池组合')
            item = next((entry for entry in batch['items'] if str(entry['id']) == str(item_id)), None)
            if item is None:
                raise ValueError('方案条目不存在')
            order = self._choose_item_music(batch, item, float(item['duration']), avoid_current=True)
            self._set_item_music(batch, item, order)
        return self.store.update(batch_id, change)

    def replace_media(self, batch_id, item_id, body):
        from . import planner
        kind = body.get('kind')
        if kind not in {'music', 'video'}:
            raise ValueError('替换类型必须是 music 或 video')
        try:
            index = int(body['index'])
        except (KeyError, ValueError, TypeError):
            raise ValueError('替换位置无效')

        def change(batch):
            if batch['status'] != 'draft':
                raise ValueError('只有草稿方案可以替换素材')
            item = next((entry for entry in batch['items'] if str(entry['id']) == str(item_id)), None)
            if item is None:
                raise ValueError('方案条目不存在')
            field = 'music' if kind == 'music' else 'segments'
            if index < 0 or index >= len(item.get(field, [])):
                raise ValueError('替换位置超出范围')
            config = batch['config']
            rng = random.Random(time.time_ns())
            if kind == 'music':
                if config.get('music_mode', 'pool') != 'pool':
                    raise ValueError('固定歌曲集合模式不能替换单曲')
                selected = set(config.get('music_ids') or [])
                current = item['music']
                ids = {song['id'] for song in current}
                usage = defaultdict(int)
                for other in batch['items']:
                    if other is not item:
                        for song in other['music']:
                            usage[song['id']] += 1
                remaining_duration = sum(float(song['duration']) for n, song in enumerate(current) if n != index)
                candidates = [song for song in batch['assets'] if song['id'] in selected and song['id'] not in ids
                              and remaining_duration + float(song['duration']) + 1e-7 >= float(item['duration'])]
                if not candidates:
                    raise ValueError('没有满足时长和任务内去重要求的替换歌曲')
                least = min(usage[song['id']] for song in candidates)
                replacement = rng.choice([song for song in candidates if usage[song['id']] == least])
                order = list(current)
                order[index] = replacement
                self._set_item_music(batch, item, order)
                return

            selected = set(config.get('video_ids') or [])
            threshold = float(config.get('min_source_duration_minutes', 0)) * 60
            current = item['segments'][index]
            videos = [asset for asset in batch['assets'] if asset['id'] in selected
                      and float(asset['duration']) > threshold and asset['id'] != current['asset_id']]
            if config.get('mode') == 'multi' and config.get('within_group'):
                other = next((part for n, part in enumerate(item['segments']) if n != index), None)
                group = config.get('groups', {}).get(other['asset_id']) if other else None
                videos = [video for video in videos if group and config.get('groups', {}).get(video['id']) == group]
            occupied = defaultdict(list)
            protected = defaultdict(list)
            usage = defaultdict(int)
            for other in batch['items']:
                for n, segment in enumerate(other.get('segments', [])):
                    if other is item and n == index:
                        continue
                    interval = (float(segment['start']), float(segment['start']) + float(segment['duration']))
                    occupied[segment['asset_id']].append(interval)
                    usage[segment['asset_id']] += 1
                    if other is item:
                        protected[segment['asset_id']].append(interval)
            excluded = ({item['segments'][1 - index]['asset_id']}
                        if config.get('mode') == 'multi' and index in (0, 1)
                        and len(item['segments']) > 1 else set())
            other_duration = sum(float(part['duration']) for n, part in enumerate(item['segments']) if n != index)
            if config.get('mode', 'single') == 'multi':
                minimum = max(float(config.get('segment_min', 30)),
                              float(config.get('min_duration', 0)) - other_duration)
                maximum = min(float(config.get('segment_max', 120)),
                              float(config.get('max_duration', item['duration'])) - other_duration)
            else:
                minimum = float(config.get('min_duration', item['duration']))
                maximum = float(config.get('max_duration', item['duration']))
            current_length = float(current['duration'])
            exact_lengths = [(None, current_length)]
            flexible_lengths = []
            shuffled = list(videos)
            rng.shuffle(shuffled)
            for video in shuffled:
                limit = min(maximum, float(video['duration']))
                if limit + 1e-7 < minimum:
                    continue
                candidate_lengths = (limit,) if body.get('replan_duration') else (limit, rng.uniform(minimum, limit))
                for length in candidate_lengths:
                    if abs(length - current_length) > 1e-5:
                        flexible_lengths.append((video, length))
            lengths = flexible_lengths if body.get('replan_duration') else exact_lengths + flexible_lengths
            failure = '没有符合时长、分组和整批去重要求的替换视频片段'
            for source, length in lengths:
                segment = planner.choose_video_segment(videos if source is None else [source], length,
                    occupied, usage, rng, allow_overlap=bool(config.get('allow_overlap', True)),
                    protected=protected, excluded_ids=excluded,
                    min_start_gap=float(config.get('start_gap', 1))
                    if config.get('mode', 'single') == 'single' else 0)
                if segment is None:
                    continue
                updated = list(item['segments'])
                updated[index] = segment
                target = other_duration + length
                key = planner.video_plan_key(updated, config.get('fps', 30))
                fingerprint = self._fingerprint(key)
                if any(other is not item and planner.video_plan_key(
                        other.get('segments', []), config.get('fps', 30)) == key for other in batch['items']):
                    failure = '可替换视频与本批另一条成片完全相同'
                    continue
                order = None
                if abs(target - float(item['duration'])) > 1e-5:
                    if config.get('music_mode', 'pool') == 'pool':
                        try:
                            order = self._choose_item_music(batch, item, target)
                        except ValueError as exc:
                            failure = f'视频可替换，但联动刷新音乐失败：{exc}'
                            continue
                    elif sum(float(song['duration']) for song in item['music']) + 1e-7 < target:
                        failure = '固定歌曲集合无法覆盖新视频时长；请改用候选池模式'
                        continue
                item['segments'] = updated
                item['duration'] = target
                item['video_fingerprint'] = fingerprint
                by_id = {asset['id']: asset for asset in batch['assets']}
                item['video_assets'] = [by_id[part['asset_id']] for part in updated]
                if order is not None:
                    self._set_item_music(batch, item, order)
                else:
                    self._update_plan_counts(batch)
                return
            raise ValueError(failure)

        result = self.store.update(batch_id, change)
        if kind == 'video':
            self._cleanup_approved_sources()
            result = self.batch(batch_id)
        return result

    def item_action(self, batch_id, item_id, action):
        if action == 'delete':
            return self.forget_items([{'batch_id': batch_id, 'item_ids': [item_id]}])
        def change(batch):
            item = next((entry for entry in batch['items'] if str(entry['id']) == str(item_id)), None)
            if not item:
                raise ValueError('任务不存在')
            if action == 'stop':
                if item['status'] in {'running', 'validating'}:
                    item['cancel_requested'] = True
                elif item['status'] == 'pending':
                    item.update(status='cancelled', error='已手动终止')
            elif action == 'start':
                if item['status'] not in {'cancelled', 'failed'}:
                    raise ValueError('只有已终止或失败的单条任务可以重新开始')
                if batch.get('scheduled_cancelled'):
                    raise ValueError('该定时任务已取消，请生成新方案')
                item.update(status='pending', error=None, attempts=0, progress=0, cancel_requested=False)
                if batch['status'] not in {'running', 'queued', 'pausing', 'stopping'}:
                    batch['status'] = 'queued'
            else:
                raise ValueError('未知单条任务操作')
        result = self.store.update(batch_id, change)
        self.wake.set()
        return result

    @staticmethod
    def _visible_batch(batch):
        if batch is None:
            return None
        items = [item for item in batch.get('items', []) if not item.get('dismissed')
                 and item.get('review', {}).get('status') not in {'approved', 'superseded'}]
        return {**batch, 'items': items} if items else None

    def visible_batches(self):
        return [visible for batch in self.store.batches()
                if (visible := self._visible_batch(batch)) is not None]

    def forget_items(self, selections, *, clear_history=False):
        """Forget task rows promptly; ongoing work is cancelled without touching media files."""
        if not isinstance(selections, list):
            raise ValueError('请选择要删除的任务')
        review_available = self.review_lock.acquire(blocking=False)
        try:
            with self.review_job_lock:
                reviewing = {key for key, job in self.review_jobs.items() if job['status'] == 'running'}
            changed = {}
            removed = 0
            with self.store.lock, self.store.connection() as conn:
                for entry in selections:
                    if not isinstance(entry, dict) or not isinstance(entry.get('item_ids'), list):
                        raise ValueError('任务选择格式无效')
                    batch_id = str(entry.get('batch_id', ''))
                    if batch_id in changed:
                        raise ValueError('请勿重复选择同一组任务')
                    row = conn.execute('SELECT body FROM records WHERE id=?', ('batch:' + batch_id,)).fetchone()
                    if row is None:
                        changed[batch_id] = None
                        continue
                    batch = json.loads(row[0])
                    wanted = {str(item_id) for item_id in entry['item_ids']}
                    active = (batch['status'] in {'running', 'queued', 'pausing', 'stopping'}
                              or batch_id in reviewing or not review_available)
                    retained = []
                    for item in batch['items']:
                        if str(item['id']) not in wanted:
                            retained.append(item)
                            continue
                        if not item.get('dismissed'):
                            removed += 1
                        if active:
                            item['dismissed'] = True
                            item['cancel_requested'] = True
                            if item['status'] == 'pending':
                                item['status'] = 'cancelled'
                            retained.append(item)
                    batch['items'] = retained
                    if retained:
                        if batch['status'] == 'draft':
                            self._update_plan_counts(batch)
                        batch['updated_at'] = time.time()
                        conn.execute('UPDATE records SET body=? WHERE id=?',
                                     (json.dumps(batch, ensure_ascii=False), 'batch:' + batch_id))
                        changed[batch_id] = self._visible_batch(batch)
                    else:
                        conn.execute('DELETE FROM records WHERE id=?', ('batch:' + batch_id,))
                        changed[batch_id] = None
                if clear_history:
                    conn.execute("DELETE FROM records WHERE id LIKE 'deletion:%'")
        finally:
            if review_available:
                self.review_lock.release()
        self.wake.set()
        return {'ok': True, 'deleted_items': removed, 'updated_batches': changed}

    def forget_all_items(self):
        selections = [{'batch_id': batch['id'], 'item_ids': [item['id'] for item in batch['items']]}
                      for batch in self.store.batches()]
        return self.forget_items(selections, clear_history=True)

    def prune_forgotten_items(self):
        """Drop hidden rows after their worker/reviewer has stopped using their indexes."""
        if not self.review_lock.acquire(blocking=False):
            return
        try:
            with self.review_job_lock:
                reviewing = {key for key, job in self.review_jobs.items() if job['status'] == 'running'}
            with self.store.lock, self.store.connection() as conn:
                batches = self.store.batches()
                dependents = {(origin.get('batch_id'), origin.get('item_id')) for batch in batches
                              if (origin := batch.get('sticker_origin')) and batch.get('items')}
                for batch in batches:
                    if batch['status'] in {'running', 'queued', 'pausing', 'stopping'} or batch['id'] in reviewing:
                        continue
                    retained = []
                    for item in batch['items']:
                        cleanup = item.get('cleanup', {})
                        archived = item.get('review', {}).get('status') in {'approved', 'superseded'}
                        settled = (archived and cleanup.get('output_deleted')
                                   and (not item.get('segments') or cleanup.get('original_recordings_deleted'))
                                   and (not batch.get('sticker_origin') or cleanup.get('origin_output_deleted'))
                                   and (batch['id'], item['id']) not in dependents)
                        if not item.get('dismissed') and not settled:
                            retained.append(item)
                    if len(retained) == len(batch['items']):
                        continue
                    if retained:
                        batch['items'] = retained
                        batch['updated_at'] = time.time()
                        conn.execute('UPDATE records SET body=? WHERE id=?',
                                     (json.dumps(batch, ensure_ascii=False), 'batch:' + batch['id']))
                    else:
                        conn.execute('DELETE FROM records WHERE id=?', ('batch:' + batch['id'],))
        finally:
            self.review_lock.release()

    def delete_items(self, selections, confirmation, expected_count=None):
        """Remove selected task records, retaining a durable snapshot of cleanup state."""
        with self.review_lock:
            return self._delete_items_locked(selections, confirmation, expected_count)

    def _delete_items_locked(self, selections, confirmation, expected_count=None):
        if confirmation != 'DELETE_TASK_RECORDS':
            raise ValueError('请完成删除任务记录的二次确认')
        if not isinstance(selections, list) or not selections:
            raise ValueError('请先选择要删除的任务')
        batches = {batch['id']: batch for batch in self.store.batches()}
        with self.review_job_lock:
            active_reviews = {batch_id for batch_id, job in self.review_jobs.items()
                              if job['status'] == 'running'}
        selected = {}
        for entry in selections:
            if not isinstance(entry, dict) or not isinstance(entry.get('item_ids'), list):
                raise ValueError('批量删除的任务格式无效')
            batch_id = str(entry.get('batch_id', ''))
            if batch_id in active_reviews:
                raise ValueError(f'批次 {batch_id} 正在一键审核，请等待审核结束后再删除任务')
            if batch_id in selected or batch_id not in batches:
                raise ValueError('批次不存在或重复')
            ids = [str(value) for value in entry['item_ids']]
            if not ids or len(ids) != len(set(ids)):
                raise ValueError('请选择不重复的任务')
            batch = batches[batch_id]
            if batch['status'] in {'running', 'queued', 'pausing', 'stopping'}:
                raise ValueError(f'请先停止批次 {batch_id} 并等待当前任务退出')
            by_id = {str(item['id']): item for item in batch['items']}
            if any(item_id not in by_id for item_id in ids):
                raise ValueError('选中的任务已发生变化，请刷新列表')
            if any(by_id[item_id]['status'] in {'running', 'validating'} for item_id in ids):
                raise ValueError('请先终止正在处理的任务')
            selected[batch_id] = ids
        count = sum(len(ids) for ids in selected.values())
        if expected_count is not None and int(expected_count) != count:
            raise ValueError('任务数量已发生变化，请刷新后重新确认')
        snapshot = {'id': uuid.uuid4().hex, 'deleted_at': datetime.now().astimezone().isoformat(),
                    'output_and_archive_files_preserved': True, 'entries': []}
        for batch_id, ids in selected.items():
            batch = batches[batch_id]
            wanted = set(ids)
            snapshot['entries'].append({'batch_id': batch_id, 'batch_status': batch['status'],
                'output_folder': batch.get('output_folder'), 'source_video_dir': batch.get('config', {}).get('source_video_dir'),
                'items': copy.deepcopy([item for item in batch['items'] if str(item['id']) in wanted])})
        changed_batches = {}
        for batch_id, ids in selected.items():
            wanted = set(ids)
            current = copy.deepcopy(batches[batch_id])
            current['items'] = [item for item in current['items'] if str(item['id']) not in wanted]
            for index, item in enumerate(current['items'], 1):
                item['index'] = index
            if current['items']:
                self._update_plan_counts(current)
                current['updated_at'] = time.time()
            changed_batches[batch_id] = current
        with self.store.lock, self.store.connection() as conn:
            for batch_id in selected:
                row = conn.execute('SELECT body FROM records WHERE id=?', ('batch:' + batch_id,)).fetchone()
                if not row or json.loads(row[0]).get('updated_at') != batches[batch_id].get('updated_at'):
                    raise ValueError('任务状态已发生变化，请刷新后重新确认')
            conn.execute('INSERT INTO records VALUES (?,?)',
                         ('deletion:' + snapshot['id'], json.dumps(snapshot, ensure_ascii=False)))
            for batch_id, current in changed_batches.items():
                if current['items']:
                    conn.execute('UPDATE records SET body=? WHERE id=?',
                                 (json.dumps(current, ensure_ascii=False), 'batch:' + batch_id))
                else:
                    conn.execute('DELETE FROM records WHERE id=?', ('batch:' + batch_id,))
        cleanup_warning = None
        try:
            if self.store.batches():
                self._cleanup_approved_sources()
        except Exception as exc:
            logging.exception('source cleanup check failed after task records were deleted')
            cleanup_warning = f'任务记录已删除，但原片清理检查失败：{exc}'
        return {'ok': True, 'deleted_items': count, 'deleted_batches': sum(
            len(ids) == len(batches[batch_id]['items']) for batch_id, ids in selected.items()),
            'deletion_record_id': snapshot['id'],
            'cleanup_warning': cleanup_warning,
            'updated_batches': {batch_id: self.store.get('batch:' + batch_id)
                                for batch_id in selected}}

    def clear_batches(self, delete_outputs=False, confirmation=None, expected_count=None):
        with self.review_lock:
            return self._clear_batches_locked(delete_outputs, confirmation, expected_count)

    def _clear_batches_locked(self, delete_outputs=False, confirmation=None, expected_count=None):
        if delete_outputs:
            raise ValueError('批量清除只删除任务记录；导出视频请另行核对后处理')
        if confirmation != 'DELETE_TASK_RECORDS':
            raise ValueError('请完成删除任务记录的二次确认')
        batches = self.store.batches()
        with self.review_job_lock:
            if any(job['status'] == 'running' for job in self.review_jobs.values()):
                raise ValueError('一键审核正在进行，请等待结束后再清除记录')
        if any(batch['status'] in {'running', 'queued', 'pausing', 'stopping'} for batch in batches):
            raise ValueError('请先停止所有执行中的批次')
        item_count = sum(len(batch['items']) for batch in batches)
        if expected_count is not None and int(expected_count) != item_count:
            raise ValueError('任务数量已发生变化，请刷新后重新确认')
        selections = [{'batch_id': batch['id'], 'item_ids': [item['id'] for item in batch['items']]}
                      for batch in batches if batch['items']]
        result = (self.delete_items(selections, confirmation, expected_count) if selections
                  else {'ok': True, 'deleted_items': 0, 'deleted_batches': 0})
        for batch in batches:
            if not batch['items']:
                self.store.delete('batch:' + batch['id'])
                result['deleted_batches'] += 1
        result['deleted'] = result['deleted_batches']
        return result

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

    @staticmethod
    def reserve_review_bundle(root, item):
        day = datetime.now().astimezone().strftime('%Y-%m-%d')
        parent = Path(root) / day
        parent.mkdir(parents=True, exist_ok=True)
        styles = item.get('music_styles') or music_styles(item.get('music', []))
        for number in range(1, 10000):
            name = Path(export_filename(styles, number)).stem
            folder = parent / name
            try:
                folder.mkdir()
                return folder, name
            except FileExistsError:
                continue
        raise OSError('当天审核文件夹编号已用尽')

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
                    archived = Path(item['review'].get('path', ''))
                    if item.get('cleanup', {}).get('output_deleted') and archived.is_file():
                        item['review'].update(status='approved')
                        item['review'].pop('error', None)
                    else:
                        item['review'].update(status='failed', error='上次归档中断，请再次点击通过审核以恢复')
                    review_changed = True
                if (item['status'] == 'success' and item.get('review', {}).get('status') not in {'approved', 'superseded'}
                        and not item.get('cleanup', {}).get('output_deleted')
                        and not Path(item['output_path']).is_file()):
                    item.update(status='failed', error='已完成的输出文件已被移动或删除')
                    changed = True
                elif (item['status'] == 'success' and item.get('review', {}).get('status') not in {'approved', 'superseded'}
                      and not item.get('cleanup', {}).get('output_deleted')
                      and item.get('result', {}).get('output_mtime_ns')):
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
        # Resume an interrupted output/sticker cleanup before serving the
        # result. Source cleanup can hash every archived video, so defer that.
        for batch in self.store.batches():
            for item in batch['items']:
                if item.get('review', {}).get('status') != 'approved':
                    continue
                if not item.get('cleanup', {}).get('output_deleted'):
                    self._cleanup_approved_output(batch['id'], item['id'])
                if (batch.get('sticker_origin', {}).get('replace_origin_on_approval')
                        and not item.get('cleanup', {}).get('origin_output_deleted')):
                    self._cleanup_sticker_origin(batch['id'], item['id'])

    def _complete_approved_cleanup(self):
        pending_source_cleanup = False
        for batch in self.store.batches():
            for item in batch['items']:
                if item.get('review', {}).get('status') == 'approved':
                    if not item.get('cleanup', {}).get('output_deleted'):
                        self._cleanup_approved_output(batch['id'], item['id'])
                    if (batch.get('sticker_origin', {}).get('replace_origin_on_approval')
                            and not item.get('cleanup', {}).get('origin_output_deleted')):
                        self._cleanup_sticker_origin(batch['id'], item['id'])
                    if item.get('cleanup', {}).get('source_cleanup_requested'):
                        pending_source_cleanup = True
                elif (item.get('review', {}).get('status') == 'superseded'
                      and item.get('cleanup', {}).get('source_cleanup_requested')):
                    pending_source_cleanup = True
        if pending_source_cleanup:
            self._cleanup_approved_sources()

    def start_worker(self):
        self.worker = threading.Thread(target=self.run_queue, daemon=True, name='render-queue')
        self.worker.start()
        self.scheduler.start()
        threading.Thread(target=self.run_housekeeping, daemon=True, name='task-housekeeping').start()

    def run_housekeeping(self):
        while not self.closing.is_set():
            if not self.review_lock.acquire(blocking=False):
                if self.closing.wait(60):
                    return
                continue
            try:
                self._complete_approved_cleanup()
                self._drain_cache_cleanup()
                self.prune_forgotten_items()
            except Exception:
                logging.exception('background source cleanup failed')
            finally:
                self.review_lock.release()
            if self.closing.wait(60):
                return

    def run_queue(self):
        last_prune = 0.0
        while not self.closing.is_set():
            if time.monotonic() - last_prune >= 10:
                try:
                    self.prune_forgotten_items()
                except Exception:
                    logging.exception('task record pruning failed')
                last_prune = time.monotonic()
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
                    current_item = next(entry for entry in self.batch(batch_id)['items'] if entry['id'] == item['id'])
                    if stage != 'complete' and current_item.get('cancel_requested'):
                        raise InterruptedError('单条任务已手动终止')
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
                        result = self.render_with_cache(batch_id, item, lambda: renderer.overlay_existing(item['source_asset']['path'], render_config['sticker_layers'],
                                                           str(output), work_dir, progress,
                                                           video_bitrate_mbps=render_config.get('video_bitrate_mbps', 0)))
                    else:
                        result = self.render_with_cache(batch_id, item, lambda: renderer.render(item, render_config, str(output), work_dir, progress))
                    result.update(output_size=output.stat().st_size, output_mtime_ns=output.stat().st_mtime_ns)
                    self.store.update(batch_id, lambda b: b['items'][index].update(
                        status='success', progress=1, error=None, result=result))
                    break
                except Exception as exc:
                    if isinstance(exc, InterruptedError):
                        self.store.update(batch_id, lambda b: b['items'][index].update(
                            status='cancelled', error=str(exc), cancel_requested=False))
                        break
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
        if batch['status'] == 'running' and any(entry['status'] == 'pending' for entry in batch['items']):
            status = 'queued'
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
        with self.scan_state_lock:
            job_scan = (self.scan_job or {}).get('scan', {}) if (self.scan_job or {}).get('status') in {
                'discovering', 'analyzing'} else {}
            for asset in job_scan.get('videos', []) + job_scan.get('music', []):
                if asset['id'] == asset_id:
                    return asset
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
        review = item.get('review', {})
        if review.get('status') == 'approved' and review.get('sha256'):
            archived = Path(review.get('path', ''))
            if archived.is_file():
                stat = archived.stat()
                key = (str(archived), stat.st_size, stat.st_mtime_ns, review['sha256'])
                if (review.get('size') in (None, stat.st_size)
                        and (key in self.archive_verifications or self.file_digest(archived) == review['sha256'])):
                    if key not in self.archive_verifications:
                        if archived.stat().st_mtime_ns != stat.st_mtime_ns:
                            raise ValueError('审核归档文件在校验时发生变化')
                        if len(self.archive_verifications) > 1024:
                            self.archive_verifications.clear()
                        self.archive_verifications.add(key)
                    return archived
            raise ValueError('审核归档文件不存在或校验失败')
        if path.is_file():
            return path
        raise ValueError('成片文件不存在，可能已被移动或删除')

    @staticmethod
    def file_digest(path):
        digest = hashlib.sha256()
        with Path(path).open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def _cleanup_approved_output(self, batch_id, item_id):
        batch = self.batch(batch_id)
        item = next(entry for entry in batch['items'] if entry['id'] == item_id)
        review = item.get('review', {})
        if review.get('status') != 'approved':
            return batch
        archived = Path(review['path'])
        try:
            if not archived.is_file() or self.file_digest(archived) != review.get('sha256'):
                raise ValueError('审核副本缺失或校验失败，制作目录成品暂不删除')
            output = Path(item['output_path'])
            if output.is_file() and self.file_digest(output) != review['sha256']:
                raise ValueError('制作目录中的同名文件已变化，暂不删除')
            self.cleanup_task_cache(batch, item)
            output.unlink(missing_ok=True)
            output.with_suffix('.txt').unlink(missing_ok=True)
            shutil.rmtree(self.store.directory / 'work' / batch_id / item_id, ignore_errors=True)
            return self.store.update(batch_id, lambda current: current['items'][next(
                n for n, entry in enumerate(current['items']) if entry['id'] == item_id)].setdefault('cleanup', {}).update(
                    output_deleted=True, temporary_segments_deleted=True, output_error=None))
        except (OSError, ValueError) as exc:
            return self.store.update(batch_id, lambda current: current['items'][next(
                n for n, entry in enumerate(current['items']) if entry['id'] == item_id)].setdefault('cleanup', {}).update(
                    output_deleted=False, output_error=str(exc)))

    def _verified_accepted_archive(self, batch, item):
        """Return the reviewed final file for an approved item or its accepted replacement."""
        seen = set()
        while item.get('review', {}).get('status') == 'superseded':
            key = (batch['id'], item['id'])
            if key in seen:
                raise ValueError('贴图替代关系形成循环')
            seen.add(key)
            review = item['review']
            batch = self.batch(review['replacement_batch_id'])
            item = next((entry for entry in batch['items']
                         if entry['id'] == review['replacement_item_id']), None)
            if item is None:
                raise ValueError('贴图替代任务记录不存在')
        review = item.get('review', {})
        if review.get('status') != 'approved':
            raise ValueError('关联任务尚未审核通过')
        archive = Path(review.get('path', ''))
        if not archive.is_file() or not review.get('sha256') or self.file_digest(archive) != review['sha256']:
            raise ValueError('关联任务的审核成片缺失或校验失败')
        return archive

    def _cleanup_sticker_origin(self, batch_id, item_id):
        """Retire an unapproved source output once its sticker replacement is accepted."""
        with self.sticker_lock:
            return self._cleanup_sticker_origin_locked(batch_id, item_id)

    def _cleanup_sticker_origin_locked(self, batch_id, item_id):
        batch = self.batch(batch_id)
        origin = batch.get('sticker_origin')
        if not origin:
            return batch
        item = next((entry for entry in batch['items'] if entry['id'] == item_id), None)
        if item is None or item.get('review', {}).get('status') != 'approved':
            return batch
        try:
            self._verified_accepted_archive(batch, item)
            source_batch = self.batch(origin['batch_id'])
            source_item = next((entry for entry in source_batch['items']
                                if entry['id'] == origin['item_id']), None)
            if source_item is None:
                raise ValueError('原任务记录已删除，无法自动清理原成片和素材')
            source_review = source_item.get('review', {})
            if source_review.get('status') == 'approved':
                # An independently approved original is a separate accepted deliverable.
                if not source_item.get('cleanup', {}).get('output_deleted'):
                    source_batch = self._cleanup_approved_output(source_batch['id'], source_item['id'])
                    source_item = next(entry for entry in source_batch['items'] if entry['id'] == source_item['id'])
                return self.store.update(batch_id, lambda current: next(
                    entry for entry in current['items'] if entry['id'] == item_id
                ).setdefault('cleanup', {}).update(
                    origin_output_deleted=bool(source_item.get('cleanup', {}).get('output_deleted')),
                    origin_status=('原视频已单独审核，保留其审核归档'
                                   if source_item.get('cleanup', {}).get('output_deleted') else None),
                    origin_error=(None if source_item.get('cleanup', {}).get('output_deleted')
                                  else source_item.get('cleanup', {}).get('output_error', '原视频归档需检查'))))
            if source_review.get('status') == 'superseded' and (
                    source_review.get('replacement_batch_id') != batch_id or
                    source_review.get('replacement_item_id') != item_id):
                if not source_item.get('cleanup', {}).get('output_deleted'):
                    raise ValueError('原任务已由另一贴图版本替代，但原成片尚未清理')
                return self.store.update(batch_id, lambda current: next(
                    entry for entry in current['items'] if entry['id'] == item_id
                ).setdefault('cleanup', {}).update(
                    origin_output_deleted=True, origin_status='原成片已由另一贴图版本清理', origin_error=None))
            source_path = Path(source_item['output_path'])
            asset = item.get('source_asset', {})
            if Path(asset.get('path', '')).resolve() != source_path.resolve():
                raise ValueError('贴图输入与原任务成片路径不一致，暂不删除')
            for dependent in self.store.batches():
                relation = dependent.get('sticker_origin', {})
                if (dependent['id'] != batch_id and relation.get('batch_id') == source_batch['id']
                        and relation.get('item_id') == source_item['id']
                        and any(entry.get('review', {}).get('status') != 'approved'
                                for entry in dependent.get('items', []))):
                    raise ValueError('还有其他未审核的贴图版本使用原成片，暂不删除')
            if source_item.get('status') != 'success':
                raise ValueError('原任务并非已完成状态，暂不删除')
            if source_path.is_file():
                from . import media
                media.verify_asset(asset)
            elif not source_review.get('status') == 'superseded':
                raise ValueError('原成片已被移动或删除，无法验证原文件')
            def mark_replaced(current):
                original = next(entry for entry in current['items'] if entry['id'] == source_item['id'])
                original['review'] = {'status': 'superseded', 'replacement_batch_id': batch_id,
                                      'replacement_item_id': item_id}
                original.setdefault('cleanup', {}).update(
                    source_cleanup_requested=bool(original.get('segments')))
            self.store.update(source_batch['id'], mark_replaced)
            self.cleanup_task_cache(source_batch, source_item)
            source_path.unlink(missing_ok=True)
            source_path.with_suffix('.txt').unlink(missing_ok=True)
            shutil.rmtree(self.store.directory / 'work' / source_batch['id'] / source_item['id'], ignore_errors=True)
            self.store.update(source_batch['id'], lambda current: next(
                entry for entry in current['items'] if entry['id'] == source_item['id']
            ).setdefault('cleanup', {}).update(output_deleted=True, temporary_segments_deleted=True,
                                               output_error=None))
            self.store.update(batch_id, lambda current: next(
                entry for entry in current['items'] if entry['id'] == item_id
            ).setdefault('cleanup', {}).update(origin_output_deleted=True, origin_error=None))
        except (OSError, ValueError, KeyError, StopIteration) as exc:
            self.store.update(batch_id, lambda current: next(
                entry for entry in current['items'] if entry['id'] == item_id
            ).setdefault('cleanup', {}).update(origin_error=str(exc)))
        return self.batch(batch_id)

    def _cleanup_approved_sources(self):
        """Delete verified source files only after no unfinished task still references them."""
        from . import media
        batches = self.store.batches()
        references = defaultdict(list)
        for batch in batches:
            for item in batch.get('items', []):
                for segment in item.get('segments', []):
                    if segment.get('path'):
                        references[str(Path(segment['path']).resolve())].append((batch, item, segment))
        updates = defaultdict(dict)
        verified_archives = set()
        for path_text, refs in references.items():
            def settled(item):
                review = item.get('review', {}).get('status')
                return review == 'approved' or (review == 'superseded'
                                                and item.get('cleanup', {}).get('output_deleted'))
            requested = [(batch, item, segment) for batch, item, segment in refs
                         if settled(item) and item.get('cleanup', {}).get('source_cleanup_requested')]
            if not requested:
                continue
            unfinished = sum(not settled(item) for _, item, _ in refs)
            if unfinished:
                status = f'等待另外 {unfinished} 条引用该原片的任务审核或移除'
            else:
                batch, item, segment = requested[0]
                path = Path(segment['path'])
                root_text = batch.get('config', {}).get('source_video_dir')
                try:
                    for dependent_batch, dependent, _ in refs:
                        key = (dependent_batch['id'], dependent['id'])
                        if key not in verified_archives:
                            self._verified_accepted_archive(dependent_batch, dependent)
                            verified_archives.add(key)
                    asset = next((asset for asset in item.get('video_assets', [])
                                  if asset['id'] == segment['asset_id']), None)
                    if asset is None or Path(asset['path']).resolve() != path.resolve():
                        raise ValueError('无法确认源文件与方案记录一致')
                    if not root_text:
                        # Older batches did not save their source root. Only trust the current
                        # scanned root when this exact asset is still present in its index.
                        library = self.store.get('library', {})
                        scanned = library.get('scan', {}).get('videos', [])
                        if any(video.get('id') == asset['id'] and
                               Path(video.get('path', '')).resolve() == path.resolve() for video in scanned):
                            root_text = library.get('video_dir')
                    if not root_text or not path.resolve().is_relative_to(Path(root_text).resolve()) or path.is_symlink():
                        raise ValueError('无法确认源文件属于已扫描的视频目录；请重新扫描原目录后重试清理')
                    if path.exists():
                        media.verify_asset(asset)
                        path.unlink()
                    status = '已删除'
                except (OSError, ValueError, StopIteration) as exc:
                    status = f'删除失败：{exc}'
            for batch, item, _ in requested:
                updates[batch['id']].setdefault(item['id'], {})[path_text] = status
        for batch_id, by_item in updates.items():
            def change(batch):
                for item in batch['items']:
                    if item['id'] not in by_item:
                        continue
                    cleanup = item.setdefault('cleanup', {})
                    cleanup.setdefault('source_files', {}).update(by_item[item['id']])
                    cleanup['original_recordings_deleted'] = bool(cleanup['source_files']) and all(
                        value == '已删除' for value in cleanup['source_files'].values())
            self.store.update(batch_id, change)

    def retry_approved_cleanup(self, batch_id, item_id):
        with self.review_lock:
            batch = self.batch(batch_id)
            item = next((entry for entry in batch['items'] if str(entry['id']) == str(item_id)), None)
            if item is None or item.get('review', {}).get('status') != 'approved':
                raise ValueError('只有审核通过的任务可以重试清理')
            archive = Path(item['review'].get('path', ''))
            if not archive.is_file() or self.file_digest(archive) != item['review'].get('sha256'):
                raise ValueError('审核成片缺失或校验失败，请先恢复审核文件')
            self.store.update(batch_id, lambda current: next(
                entry for entry in current['items'] if str(entry['id']) == str(item_id)
            ).setdefault('cleanup', {}).update(source_cleanup_requested=bool(item.get('segments'))))
            self._cleanup_approved_output(batch_id, item_id)
            self._cleanup_sticker_origin(batch_id, item_id)
            self._cleanup_approved_sources()
            return self.batch(batch_id)

    def approve(self, body):
        batch_id, item_id = str(body['batch_id']), str(body['item_id'])
        # Serialize approvals, not rendering. Double clicks cannot copy the same item twice.
        with self.review_lock, self.sticker_lock:
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
                    self.store.update(batch_id, lambda b: b['items'][index].setdefault('cleanup', {}).update(
                        source_cleanup_requested=bool(item.get('segments'))))
                    self._cleanup_approved_output(batch_id, item_id)
                    self._cleanup_sticker_origin(batch_id, item_id)
                    self._cleanup_approved_sources()
                    return self.batch(batch_id)
                raise ValueError('审核归档文件已被移动或修改，请检查原保存位置')
            for dependent in self.store.batches():
                relation = dependent.get('sticker_origin', {})
                if (relation.get('batch_id') == batch_id and relation.get('item_id') == item_id
                        and any(entry.get('status') != 'success' and
                                entry.get('review', {}).get('status') != 'approved'
                                for entry in dependent.get('items', []))):
                    raise ValueError('贴图版本仍需读取该原成片；请先完成或删除贴图任务，再审核原视频')
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
            folder, bundle_name = self.reserve_review_bundle(root, item)
            self.store.update(batch_id, lambda b: b.setdefault('review_folders', {}).update({str(root): str(folder.parent)}))
            target = folder / f'{bundle_name}.mp4'
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
                self.store.update(batch_id, lambda b: b['items'][index].update(
                    review=approved, cleanup={'output_deleted': False, 'temporary_segments_deleted': False,
                                             'original_recordings_deleted': False,
                                             'source_cleanup_requested': bool(item.get('segments'))}))
            except Exception as exc:
                self.store.update(batch_id, lambda b: b['items'][index].update(
                    review={**pending, 'status': 'failed', 'error': str(exc)}))
                raise
            finally:
                temporary.unlink(missing_ok=True)
                try:
                    folder.rmdir()
                except OSError:
                    pass
            self._cleanup_approved_output(batch_id, item_id)
            self._cleanup_sticker_origin(batch_id, item_id)
            self._cleanup_approved_sources()
            self.preferences({'review_dir': str(root)})
            return self.batch(batch_id)

    def start_batch_review(self, batch_id, body):
        review_dir = str(body.get('review_dir', '')).strip()
        if not review_dir:
            raise ValueError('请先设置审核通过文件夹')
        with self.review_lock, self.review_job_lock:
            existing = self.review_jobs.get(batch_id)
            if existing and existing['status'] == 'running':
                return dict(existing)
            batch = self.batch(batch_id)
            candidates = sorted((item for item in batch.get('items', [])
                                 if item.get('status') == 'success'
                                 and item.get('review', {}).get('status') not in ('approved', 'superseded', 'copying')),
                                 key=lambda item: (int(item.get('index') or 0), str(item['id'])))
            item_ids = [str(item['id']) for item in candidates]
            if not item_ids:
                raise ValueError('此批次没有可审核的已完成任务')
            if body.get('item_ids') != item_ids:
                raise ValueError('任务列表已变化，请刷新后重新确认一键审核')
            job = {'batch_id': batch_id, 'status': 'running', 'total': len(item_ids),
                   'completed': 0, 'current_item': None, 'error': None}
            self.review_jobs[batch_id] = job

        def work():
            for item_id in item_ids:
                with self.review_job_lock:
                    job['current_item'] = item_id
                try:
                    self.approve({'batch_id': batch_id, 'item_id': item_id, 'review_dir': review_dir})
                except Exception as exc:
                    with self.review_job_lock:
                        job.update(status='failed', error=str(exc), failed_item=item_id,
                                   current_item=None)
                    return
                with self.review_job_lock:
                    job['completed'] += 1
            with self.review_job_lock:
                job.update(status='completed', current_item=None)

        threading.Thread(target=work, name=f'mixcut-review-{batch_id}', daemon=True).start()
        return dict(job)

    def batch_review_status(self, batch_id):
        with self.review_job_lock:
            return dict(self.review_jobs.get(batch_id, {'batch_id': batch_id, 'status': 'idle'}))

    def start_review_all(self, body):
        review_dir = str(body.get('review_dir', '')).strip()
        if not review_dir:
            raise ValueError('请先设置审核通过文件夹')
        with self.review_job_lock:
            if self.global_review_job['status'] == 'running':
                return dict(self.global_review_job)
            if any(job['status'] == 'running' for job in self.review_jobs.values()):
                raise ValueError('另一项审核正在进行，请稍后重试')
            candidates = [(batch['id'], str(item['id'])) for batch in
                          sorted(self.visible_batches(), key=lambda entry: entry.get('created_at', 0))
                          for item in sorted(batch['items'], key=lambda entry: (int(entry.get('index') or 0),
                                                                               str(entry['id'])))
                          if item['status'] == 'success' and
                          item.get('review', {}).get('status') not in {'approved', 'superseded', 'copying'}]
            if not candidates:
                raise ValueError('没有待审核的已完成任务')
            job = {'status': 'running', 'total': len(candidates), 'completed': 0,
                   'skipped': 0, 'current_item': None, 'error': None}
            self.global_review_job = job

        def work():
            for batch_id, item_id in candidates:
                with self.review_job_lock:
                    job['current_item'] = item_id
                batch = self.store.get('batch:' + batch_id)
                item = next((entry for entry in batch.get('items', []) if entry['id'] == item_id), None) if batch else None
                if item is None or item.get('dismissed'):
                    with self.review_job_lock:
                        job['skipped'] += 1
                    continue
                try:
                    self.approve({'batch_id': batch_id, 'item_id': item_id, 'review_dir': review_dir})
                except Exception as exc:
                    with self.review_job_lock:
                        job.update(status='failed', error=str(exc), failed_item=item_id,
                                   current_item=None)
                    return
                with self.review_job_lock:
                    job['completed'] += 1
            with self.review_job_lock:
                job.update(status='completed', current_item=None)

        threading.Thread(target=work, name='mixcut-review-all', daemon=True).start()
        return dict(job)

    def review_all_status(self):
        with self.review_job_lock:
            return dict(self.global_review_job)


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
                if path == '/api/scan/start':
                    return self.json_response(self.app.start_scan(body))
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
                batch_review_match = re.fullmatch(r'/api/batches/([a-f0-9]+)/approve-all', path)
                if batch_review_match:
                    return self.json_response(self.app.start_batch_review(batch_review_match[1], body))
                if path == '/api/plan':
                    return self.json_response(self.app.create_plan(body))
                music_order_match = re.fullmatch(r'/api/batches/([a-f0-9]+)/items/([^/]+)/music-order', path)
                if music_order_match:
                    return self.json_response(self.app.reorder_music(
                        music_order_match[1], music_order_match[2], body.get('music_ids')))
                music_refresh_match = re.fullmatch(r'/api/batches/([a-f0-9]+)/items/([^/]+)/refresh-music', path)
                if music_refresh_match:
                    return self.json_response(self.app.refresh_music(*music_refresh_match.groups()))
                replacement_match = re.fullmatch(r'/api/batches/([a-f0-9]+)/items/([^/]+)/replace', path)
                if replacement_match:
                    return self.json_response(self.app.replace_media(*replacement_match.groups(), body))
                cleanup_match = re.fullmatch(r'/api/batches/([a-f0-9]+)/items/([^/]+)/retry-cleanup', path)
                if cleanup_match:
                    return self.json_response(self.app.retry_approved_cleanup(*cleanup_match.groups()))
                item_action_match = re.fullmatch(r'/api/batches/([a-f0-9]+)/items/([^/]+)/(start|stop|delete)', path)
                if item_action_match:
                    return self.json_response(self.app.item_action(*item_action_match.groups()))
                if path == '/api/batches/delete-items':
                    return self.json_response(self.app.delete_items(body.get('selections'),
                        body.get('confirmation'), body.get('expected_count')))
                if path == '/api/tasks/forget':
                    return self.json_response(self.app.forget_items(body.get('selections')))
                if path == '/api/cache/clear':
                    return self.json_response(self.app.clear_cache())
                if path == '/api/tasks/retry-failed':
                    return self.json_response(self.app.retry_failed_tasks())
                if path == '/api/tasks/clear':
                    return self.json_response(self.app.forget_all_items())
                if path == '/api/tasks/approve-all':
                    return self.json_response(self.app.start_review_all(body))
                if path == '/api/batches/clear':
                    return self.json_response(self.app.clear_batches(bool(body.get('delete_outputs')),
                        body.get('confirmation'), body.get('expected_count')))
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
                    with self.app.review_job_lock:
                        if any(job['status'] == 'running' for job in self.app.review_jobs.values()):
                            raise ValueError('一键审核正在进行，请等待结束后退出')
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
                if path == '/api/scan/status':
                    compact = parse_qs(urlparse(self.path).query).get('compact') == ['1']
                    return self.json_response(self.app.scan_status(compact=compact) or {'status': 'idle'})
                if path == '/api/cache':
                    return self.json_response(self.app.cache_status())
                if path == '/api/batches':
                    return self.json_response(self.app.visible_batches())
                if path == '/api/tasks/approve-all':
                    return self.json_response(self.app.review_all_status())
                batch_review_match = re.fullmatch(r'/api/batches/([a-f0-9]+)/approve-all', path)
                if batch_review_match:
                    return self.json_response(self.app.batch_review_status(batch_review_match[1]))
                if path == '/api/deletion-records':
                    return self.json_response(self.app.store.records('deletion:'))
                match = re.fullmatch(r'/api/batches/([a-f0-9]+)', path)
                if match:
                    return self.json_response(self.app.batch(match[1]))
                if path in ['/api/media', '/api/thumbnail']:
                    asset = self.app.asset(query.get('id', [''])[0])
                    target = Path(asset['path'])
                    with self.app.cached_preview():
                        if path == '/api/thumbnail':
                            target = self.app.store.directory / 'thumbnails' / (asset['id'] + '.jpg')
                            if not target.is_file() or target.stat().st_size == 0:
                                with self.app.thumbnail_semaphore:
                                    if not target.is_file() or target.stat().st_size == 0:
                                        target.parent.mkdir(parents=True, exist_ok=True)
                                        temporary = target.with_name('.' + asset['id'] + '-' + uuid.uuid4().hex + '.jpg')
                                        try:
                                            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', '1', '-i', asset['path'],
                                                            '-frames:v', '1', '-vf', 'scale=320:-2', str(temporary)],
                                                           check=True, timeout=30, stdout=subprocess.DEVNULL,
                                                           stderr=subprocess.PIPE)
                                            os.replace(temporary, target)
                                        finally:
                                            temporary.unlink(missing_ok=True)
                        return self.send_file(target, content_type=asset.get('mime_type') if path == '/api/media' else None)
                if path in ['/api/output', '/api/keyframes', '/api/keyframe']:
                    output = self.app.completed_output(query['batch'][0], query['item'][0])
                    with self.app.cached_preview():
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
                    return self.send_file(ROOT / 'static' / files[path], no_store=True)
            self.json_response({'error': '接口不存在'}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except PermissionError as exc:
            self.json_response({'error': str(exc)}, 403)
        except (ValueError, KeyError, StopIteration, FileNotFoundError) as exc:
            self.json_response({'error': str(exc) or '请求的资源不存在'}, 400)
        except Exception as exc:
            self.json_response({'error': str(exc)}, 500)

    def send_file(self, path, *, content_type=None, no_store=False):
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
        self.send_header('Content-Type', content_type or mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(max(0, end - start + 1)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        if no_store:
            self.send_header('Cache-Control', 'no-store')
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
