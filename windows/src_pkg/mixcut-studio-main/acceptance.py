"""Reproducible real-media acceptance via the same API used by the UI.

Run samples first; inspect them before running --batch50.
"""
import argparse
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
BASE = 'http://127.0.0.1:8877'


def api(path, body=None):
    request = Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=600) as response:
        return json.load(response)


def wait(batch_id):
    previous = None
    while True:
        batch = api('/api/batches/' + batch_id)
        done = sum(item['status'] == 'success' for item in batch['items'])
        failed = [item for item in batch['items'] if item['status'] == 'failed']
        state = (batch['status'], done, len(failed))
        if state != previous:
            print(batch_id, state, flush=True)
            previous = state
        if batch['status'] in ['completed', 'failed', 'paused', 'stopped']:
            if failed or batch['status'] != 'completed':
                raise RuntimeError(json.dumps({'error': batch.get('error'), 'failed': failed}, ensure_ascii=False))
            return batch
        time.sleep(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch50', action='store_true')
    parser.add_argument('--pause-after-first', metavar='BATCH_ID')
    parser.add_argument('--resume-check', metavar='BATCH_ID')
    args = parser.parse_args()
    recovery_file = ROOT / '.mixcut' / 'recovery-check.json'
    if args.pause_after_first:
        batch_id = args.pause_after_first
        api('/api/batches/' + batch_id + '/resume', {})
        while True:
            batch = api('/api/batches/' + batch_id)
            if any(item['status'] == 'success' for item in batch['items']):
                api('/api/batches/' + batch_id + '/pause', {})
                break
            if batch['status'] in ['failed', 'stopped']:
                raise RuntimeError(batch['status'])
            time.sleep(0.5)
        while True:
            batch = api('/api/batches/' + batch_id)
            if batch['status'] in ['paused', 'completed']:
                break
            time.sleep(0.5)
        snapshot = [{'path': i['output_path'], 'mtime_ns': Path(i['output_path']).stat().st_mtime_ns,
                     'size': Path(i['output_path']).stat().st_size}
                    for i in batch['items'] if i['status'] == 'success']
        recovery_file.write_text(json.dumps({'batch_id': batch_id, 'before': snapshot}, ensure_ascii=False, indent=2))
        print('PAUSED', batch_id, 'completed', len(snapshot), 'remaining', len(batch['items']) - len(snapshot), flush=True)
        return
    if args.resume_check:
        record = json.loads(recovery_file.read_text())
        assert record['batch_id'] == args.resume_check
        batch = api('/api/batches/' + args.resume_check)
        assert batch['status'] == 'paused', batch['status']
        api('/api/batches/' + args.resume_check + '/resume', {})
        batch = wait(args.resume_check)
        for previous in record['before']:
            stat = Path(previous['path']).stat()
            assert stat.st_mtime_ns == previous['mtime_ns'] and stat.st_size == previous['size']
        record.update(passed=True, final_count=len(batch['items']), preserved=len(record['before']))
        recovery_file.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        print('RESTART_RESUME_PASS', json.dumps(record, ensure_ascii=False), flush=True)
        return
    scan = api('/api/scan', {'video_dir': str(ROOT / '哔哩哔哩'), 'music_dir': str(ROOT / '去重歌曲03')})
    songs = {song['name']: song['id'] for song in scan['music']}
    base = {'min_duration': 0, 'max_duration': 500, 'music_mode': 'fixed',
            'music_ids': [songs['歌曲03.mp3'], songs['歌曲07.mp3']],
            'allow_overlap': True, 'start_gap': 5, 'segment_min': 10, 'segment_max': 30,
            'seed': 20260921, 'width': 1280, 'height': 720, 'fps': 30,
            'music_volume': 1, 'original_volume': 0, 'hardware': 'auto',
            'output_dir': str(ROOT / 'exports' / '验收样片')}
    jobs = []
    if args.batch50:
        base.update(mode='multi', count=50, music_mode='pool', min_songs=1, max_songs=4,
                    music_ids=[songs[name] for name in ['歌曲03.mp3', '歌曲07.mp3', '歌曲06.mp3', '歌曲09.mp3']],
                    min_duration=30, max_duration=450, segment_min=15, segment_max=120,
                    width=640, height=360, fps=24,
                    output_dir=str(ROOT / 'exports' / '批量验收360p'))
        jobs = [base]
    else:
        jobs = [dict(base, mode='single', count=1), dict(base, mode='multi', count=1, original_volume=0.15)]
    report = []
    for config in jobs:
        start = time.monotonic()
        batch = api('/api/plan', {'config': config})
        print('PLAN', batch['id'], 'items', len(batch['items']), 'seconds', sum(i['duration'] for i in batch['items']), flush=True)
        api('/api/batches/' + batch['id'] + '/start', {})
        batch = wait(batch['id'])
        video_keys = {item['video_fingerprint'] for item in batch['items']}
        music_keys = {item['music_fingerprint'] for item in batch['items']}
        assert len(video_keys) == len(music_keys) == len(batch['items'])
        report.append({'batch': batch['id'], 'mode': config['mode'], 'count': len(batch['items']),
                       'elapsed_seconds': round(time.monotonic() - start, 2),
                       'duration_total': sum(i['duration'] for i in batch['items']),
                       'bytes': sum(Path(i['output_path']).stat().st_size for i in batch['items']),
                       'files': [i['output_path'] for i in batch['items']]})
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
