"""Persistent local schedules with one active target and incremental material checks."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import threading
import time
import uuid


class ScheduledRunCancelled(RuntimeError):
    pass


class Scheduler:
    def __init__(self, app, *, background_checks=True):
        self.app = app
        self.store = app.store
        self.lock = threading.RLock()
        self.checking = set()
        self.check_slots = threading.BoundedSemaphore(2)
        self.background_checks = background_checks
        self.thread = None

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._loop, daemon=True, name='schedule-clock')
        self.thread.start()

    def _loop(self):
        while not self.app.closing.is_set():
            try:
                self.tick()
            except Exception as exc:
                self.store.put('scheduler-error', str(exc))
            self.app.closing.wait(1)

    def schedules(self):
        return sorted(self.store.records('schedule:'), key=lambda s: (s.get('next_due') or float('inf'), s['id']))

    def runs(self):
        return sorted(self.store.records('scheduled-run:'), key=lambda r: r['started_at'], reverse=True)

    def active(self):
        run_id = self.store.get('scheduler-active')
        return self.store.get('scheduled-run:' + run_id) if run_id else None

    def _put_run(self, run):
        self.store.put('scheduled-run:' + run['id'], run)

    def listing(self):
        with self.lock:
            run = self.active()
            if run:
                self._reconcile(run, time.time())
            return {'schedules': self.schedules(), 'active_run': self.active(),
                    'runs': self.runs()[:100], 'timezone': datetime.now().astimezone().tzname(),
                    'error': self.store.get('scheduler-error')}

    @staticmethod
    def _timestamp(value):
        try:
            timestamp = float(value) if isinstance(value, (float, int)) else datetime.fromisoformat(str(value)).timestamp()
        except (ValueError, TypeError, OverflowError):
            raise ValueError('请选择有效的启动日期和时间')
        if not math.isfinite(timestamp):
            raise ValueError('启动时间无效')
        return timestamp

    @staticmethod
    def _next_after(schedule, due, now):
        if schedule['repeat'] == 'once':
            return None
        days = 1 if schedule['repeat'] == 'daily' else 7
        local = datetime.fromtimestamp(due)
        elapsed_days = max(0, (datetime.fromtimestamp(now).date() - local.date()).days)
        local += timedelta(days=max(1, elapsed_days // days) * days)
        while local.timestamp() <= now:
            local += timedelta(days=days)
        return local.timestamp()

    @staticmethod
    def _latest_due(schedule, now):
        due = schedule['next_due']
        if schedule['repeat'] == 'once':
            return due
        days = 1 if schedule['repeat'] == 'daily' else 7
        local = datetime.fromtimestamp(due)
        elapsed_days = max(0, (datetime.fromtimestamp(now).date() - local.date()).days)
        candidate = local + timedelta(days=(elapsed_days // days) * days)
        if candidate.timestamp() > now:
            candidate -= timedelta(days=days)
        return max(due, candidate.timestamp())

    def save(self, body, now=None):
        now = time.time() if now is None else now
        name = str(body.get('name', '')).strip()
        if not name or len(name) > 80:
            raise ValueError('任务名称请填写1～80个字符')
        repeat = body.get('repeat', 'once')
        if repeat not in {'once', 'daily', 'weekly'}:
            raise ValueError('重复方式须为一次、每天或每周')
        start = self._timestamp(body.get('start_at'))
        target = int(body.get('target_count', 50))
        interval = float(body.get('check_interval_minutes', 10))
        if not 1 <= target <= 500 or not math.isfinite(interval) or not 1 <= interval <= 1440:
            raise ValueError('目标数量须为1～500，检查间隔须为1～1440分钟')
        config = copy.deepcopy(body.get('config', {}))
        config['count'] = target
        config.pop('video_ids', None)
        config.pop('music_ids', None)
        if config.get('within_group'):
            raise ValueError('定时任务请关闭手工组内拼接；可用不同视频文件夹分别建立任务')
        mode = config.get('mode', 'single')
        if mode not in {'single', 'multi'}:
            raise ValueError('剪辑模式无效')
        for field, default in [('width', 1280), ('height', 720), ('fps', 30)]:
            config[field] = int(config.get(field, default))
        if (config['width'], config['height']) not in {(640, 360), (1280, 720), (1920, 1080)} or config['fps'] not in {24, 25, 30, 60}:
            raise ValueError('导出规格无效')
        if config.get('hardware', 'auto') not in {'auto', 'software', 'software_fast', 'videotoolbox'}:
            raise ValueError('编码方式无效')
        for field, default in [('music_volume', 1), ('original_volume', 0)]:
            config[field] = float(config.get(field, default))
            if not math.isfinite(config[field]) or not 0 <= config[field] <= 2:
                raise ValueError('音量须在0～2之间')
        config.setdefault('min_duration', 30)
        config.setdefault('max_duration', 450)
        config.setdefault('min_songs', 1)
        config.setdefault('max_songs', 3)
        config.setdefault('segment_min', 30)
        config.setdefault('segment_max', 120)
        config.setdefault('start_gap', 30)
        config.setdefault('allow_overlap', True)
        try:
            config['min_source_duration_minutes'] = float(config.get('min_source_duration_minutes', 0))
        except (ValueError, TypeError):
            raise ValueError('源视频时长门槛必须是非负有限数字（分钟）')
        if not math.isfinite(config['min_source_duration_minutes']) or config['min_source_duration_minutes'] < 0:
            raise ValueError('源视频时长门槛必须是非负有限数字（分钟）')
        for field in ['min_duration', 'max_duration', 'segment_min', 'segment_max', 'start_gap']:
            config[field] = float(config[field])
            if not math.isfinite(config[field]) or config[field] < 0:
                raise ValueError('时长和间隔必须是非负有限数字')
        if config['min_duration'] > config['max_duration'] or config['segment_min'] <= 0 or config['segment_max'] < config['segment_min']:
            raise ValueError('最短/最长时长设置无效')
        if not 1 <= int(config['min_songs']) <= int(config['max_songs']):
            raise ValueError('歌曲数量范围无效')
        if config.get('music_mode', 'pool') not in {'fixed', 'pool'}:
            raise ValueError('音乐模式无效')
        template, layers = self.app.resolve_sticker_template(config.get('sticker_template_id'))
        config.update(sticker_template=template, sticker_layers=layers)
        paths = {}
        for field in ['video_dir', 'music_dir', 'output_dir']:
            value = str(body.get(field, '')).strip()
            if not value:
                raise ValueError('请填写视频、音乐和导出文件夹')
            path = Path(value).expanduser().resolve()
            if path.exists() and not path.is_dir():
                raise ValueError('路径必须是文件夹')
            paths[field] = str(path)
        config['output_dir'] = paths['output_dir']
        with self.lock:
            schedule_id = body.get('id') or uuid.uuid4().hex[:12]
            old = self.store.get('schedule:' + schedule_id)
            if body.get('id') and not old:
                raise ValueError('定时任务不存在')
            schedule = {'id': schedule_id, 'name': name, 'start_at': start, 'repeat': repeat,
                        'next_due': start, 'target_count': target, 'check_interval_minutes': interval,
                        'enabled': bool(body.get('enabled', False)), 'config': config, **paths,
                        'created_at': old['created_at'] if old else now, 'updated_at': now}
            if start <= now:
                if repeat == 'once':
                    raise ValueError('一次性启动时间须在未来；保存未来时间后可点击“立即启动”测试')
                schedule['next_due'] = self._next_after(schedule, start, now)
            self.store.put('schedule:' + schedule_id, schedule)
            if not schedule['enabled']:
                active = self.active()
                if active and active['schedule_id'] == schedule_id:
                    self._cancel_run(active, now, 'cancelled', '定时任务已停用')
        return self.listing()

    def action(self, schedule_id, action, now=None):
        now = time.time() if now is None else now
        with self.lock:
            schedule = self.store.get('schedule:' + schedule_id)
            if not schedule:
                raise ValueError('定时任务不存在')
            if action == 'run-now':
                # An explicit start is newer than any missed scheduled occurrence.
                # Consume overdue slots first so tick cannot immediately supersede it again.
                for pending in self.schedules():
                    if pending['enabled'] and pending.get('next_due') is not None and pending['next_due'] <= now:
                        latest = self._latest_due(pending, now)
                        pending['next_due'] = self._next_after(pending, latest, now)
                        self.store.put('schedule:' + pending['id'], pending)
                self._begin(schedule, now, now)
            elif action in {'enable', 'disable'}:
                schedule['enabled'] = action == 'enable'
                if action == 'enable' and not schedule.get('next_due'):
                    raise ValueError('该一次性任务已结束，请编辑为新的未来时间或立即启动')
                self.store.put('schedule:' + schedule_id, schedule)
                active = self.active()
                if action == 'disable' and active and active['schedule_id'] == schedule_id:
                    self._cancel_run(active, now, 'cancelled', '定时任务已停用')
            else:
                raise ValueError('未知定时操作')
        self.tick(now)
        return self.listing()

    def _begin(self, schedule, due, now):
        previous = self.active()
        if previous and previous['status'] in {'running', 'waiting'}:
            self._cancel_run(previous, now, 'superseded', f'由新任务「{schedule["name"]}」接管，剩余目标取消')
        run = {'id': uuid.uuid4().hex[:12], 'schedule_id': schedule['id'], 'name': schedule['name'],
               'schedule': copy.deepcopy(schedule), 'status': 'waiting', 'target_count': schedule['target_count'],
               'completed_count': 0, 'batch_ids': [], 'started_at': now, 'scheduled_at': due,
               'next_check': now, 'last_check': None, 'last_error': None, 'reason': '等待首次扫描',
               'inventory_signature': None}
        self._put_run(run)
        self.store.put('scheduler-active', run['id'])

    def _run_batches(self, run):
        return [batch for batch in self.store.batches() if batch.get('scheduled_run_id') == run['id']]

    def _cancel_run(self, run, now, status, reason):
        if run['status'] not in {'waiting', 'running'}:
            return
        self._reconcile(run, now)
        if run['status'] == 'completed':
            return
        run.update(status=status, reason=reason, next_check=None, finished_at=now)
        self._put_run(run)
        for batch in self._run_batches(run):
            if batch['status'] == 'completed':
                continue
            def cancel(value):
                value['scheduled_cancelled'] = True
                has_active = False
                for item in value['items']:
                    if item['status'] in {'running', 'validating'}:
                        has_active = True
                    elif item['status'] != 'success':
                        item.update(status='cancelled', error=reason)
                value['status'] = 'stopping' if has_active else 'stopped'
            self.store.update(batch['id'], cancel)

    def finish_cancelled_batch(self, batch_id):
        def finish(batch):
            for item in batch['items']:
                if item['status'] != 'success':
                    item.update(status='cancelled', error='定时目标已取消')
            batch['status'] = 'stopped'
        with self.lock:
            batch = self.store.update(batch_id, finish)
            run_id = batch.get('scheduled_run_id')
            run = self.store.get('scheduled-run:' + run_id) if run_id else None
            if run:
                run['completed_count'] = sum(i['status'] == 'success' for b in self._run_batches(run) for i in b['items'])
                self._put_run(run)

    def _reconcile(self, run, now, *, resume=False):
        if run['status'] not in {'waiting', 'running'}:
            return
        batches = self._run_batches(run)
        done = sum(i['status'] == 'success' for batch in batches for i in batch['items'])
        run['completed_count'] = done
        run['batch_ids'] = [batch['id'] for batch in sorted(batches, key=lambda b: b['created_at'])]
        active_batches = [b for b in batches if b['status'] in {'queued', 'running', 'pausing', 'stopping'}]
        if done >= run['target_count']:
            run.update(status='completed', reason='已达到目标数量', next_check=None, finished_at=now)
        elif active_batches:
            run.update(status='running', reason='正在生成本轮可用方案')
        else:
            # Interrupted scheduler batches resume automatically only if this target is still current.
            recoverable = [b for b in batches if (b['status'] == 'draft' or
                           (b['status'] == 'paused' and b.get('recovery_note'))) and not b.get('scheduled_cancelled')]
            resumed = False
            if resume:
                for batch in recoverable:
                    self.store.update(batch['id'], lambda b: b.pop('recovery_note', None))
                    try:
                        self.app.action(batch['id'], 'resume')
                        resumed = True
                    except Exception as exc:
                        run['last_error'] = str(exc)
                for batch in batches:
                    if batch['status'] in {'paused', 'failed', 'stopped'} and not batch.get('recovery_note') and not batch.get('scheduled_cancelled'):
                        if batch.get('error'):
                            run['last_error'] = batch['error']
                        errors = [item.get('error') for item in batch['items'] if item.get('error')]
                        if errors:
                            run['last_error'] = errors[-1]
                        self.store.update(batch['id'], lambda b: b.update(scheduled_cancelled=True))
                        self.finish_cancelled_batch(batch['id'])
            run['status'] = 'running' if resumed else 'waiting'
            if not resumed and batches:
                run['reason'] = f'已完成{done}/{run["target_count"]}，等待新素材补足'
        self._put_run(run)

    def tick(self, now=None):
        now = time.time() if now is None else now
        check = None
        with self.lock:
            due = []
            for schedule in self.schedules():
                if schedule['enabled'] and schedule.get('next_due') is not None and schedule['next_due'] <= now:
                    latest = self._latest_due(schedule, now)
                    due.append((latest, schedule['created_at'], schedule['id'], schedule))
                    schedule['next_due'] = self._next_after(schedule, latest, now)
                    self.store.put('schedule:' + schedule['id'], schedule)
            if due:
                latest, _, _, schedule = max(due, key=lambda entry: entry[:3])
                self._begin(schedule, latest, now)
            run = self.active()
            if run:
                self._reconcile(run, now, resume=True)
                if run['status'] == 'waiting' and run.get('next_check') is not None and run['next_check'] <= now and run['id'] not in self.checking:
                    self.checking.add(run['id'])
                    run['last_check'] = now
                    run['next_check'] = now + run['schedule']['check_interval_minutes'] * 60
                    run['reason'] = '正在检查文件夹'
                    self._put_run(run)
                    check = run['id']
        if check:
            if self.background_checks:
                threading.Thread(target=self._check, args=(check, now), daemon=True, name='material-check').start()
            else:
                self._check(check, now)

    def check_now(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            run = self.active()
            if not run or run['status'] not in {'waiting', 'running'}:
                raise ValueError('当前没有待补足的定时目标')
            run['next_check'] = now
            run['inventory_signature'] = None
            self._put_run(run)
        self.tick(now)
        return self.listing()

    def _check(self, run_id, now):
        from . import media, planner
        try:
            with self.check_slots:
                with self.lock:
                    run = self.active()
                    if self.app.closing.is_set() or not run or run['id'] != run_id or run['status'] not in {'waiting', 'running'}:
                        return
                    schedule = copy.deepcopy(run['schedule'])
                excluded = [str(self.store.directory)]
                for batch in self.store.batches():
                    excluded.append(str(Path(batch.get('output_folder') or Path(batch['config']['output_dir']) / batch['id'])))
                    excluded.extend(batch.get('review_folders', {}).values())
                scan = media.scan(schedule['video_dir'], schedule['music_dir'],
                                  str(self.store.directory / 'schedule-cache' / run_id), exclude_dirs=excluded)
                # Avoid files that are still being copied into the watched folder.
                for kind in ['videos', 'music']:
                    scan[kind] = [a for a in scan[kind] if now - a.get('mtime_ns', 0) / 1e9 >= 2]
                signature = hashlib.sha256(json.dumps([[a['id'], a.get('mtime_ns'), a['path']]
                    for a in scan['videos'] + scan['music']], sort_keys=True).encode()).hexdigest()
                with self.lock:
                    run = self.active()
                    if self.app.closing.is_set() or not run or run['id'] != run_id or run['status'] not in {'waiting', 'running'}:
                        return
                    self._reconcile(run, now)
                    if run['status'] != 'waiting':
                        return
                    if run.get('inventory_signature') == signature:
                        run['reason'] = '未发现新素材，等待下次检查'
                        self._put_run(run)
                        return
                    previous = [i for batch in self._run_batches(run) for i in batch['items'] if i['status'] == 'success']
                    config = copy.deepcopy(schedule['config'])
                    config['count'] = run['target_count'] - run['completed_count']
                result = planner.plan(scan['videos'], scan['music'], config, allow_partial=True, previous_items=previous)
                with self.lock:
                    run = self.active()
                    if self.app.closing.is_set() or not run or run['id'] != run_id or run['status'] != 'waiting':
                        return
                    run['inventory_signature'] = signature
                    run['last_error'] = '; '.join(str(e.get('error', e) if isinstance(e, dict) else e) for e in scan.get('errors', [])) or None
                    if not result['items']:
                        run['reason'] = '; '.join(result.get('warnings', [])) or '素材不足，等待下次检查'
                        self._put_run(run)
                        return
                    config['count'] = len(result['items'])
                    batch = self.app.make_batch(config, result, scan['videos'] + scan['music'],
                                                music_root=schedule['music_dir'], scheduled_run_id=run_id,
                                                schedule_name=run['name'], index_offset=run['completed_count'])
                    run['batch_ids'].append(batch['id'])
                    run.update(status='running', reason=f'本次可生成{len(result["items"])}条，完成后继续检查')
                    self._put_run(run)
                    self.app.action(batch['id'], 'start')
        except Exception as exc:
            with self.lock:
                run = self.active()
                if run and run['id'] == run_id and run['status'] in {'waiting', 'running'}:
                    run.update(last_error=str(exc), reason='检查或规划失败，将按间隔重试')
                    self._put_run(run)
        finally:
            with self.lock:
                self.checking.discard(run_id)
