"""Smoke-test the frozen MixCutStudio bundle as a black box.

Starts ``MixCutStudio.exe --server`` on a spare port, waits for a healthy
``/api/bootstrap``, checks the bundled static assets, then asks the service to
shut itself down and confirms the process exits.  Run with any Python 3.10+:

    python build_tools/verify_frozen.py [--port 8897]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / 'dist' / 'MixCutStudio' / 'MixCutStudio.exe'
SMOKE = ROOT / '_smoke' / 'frozen'


def get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.read()


def bootstrap(port, timeout=3.0):
    try:
        status, body = get(f'http://127.0.0.1:{port}/api/bootstrap', timeout)
        return json.loads(body) if status == 200 else None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8897)
    parser.add_argument('--timeout', type=float, default=40.0)
    args = parser.parse_args()

    if not EXE.is_file():
        raise SystemExit(f'frozen executable missing: {EXE}')
    if bootstrap(args.port, 1.0) is not None:
        raise SystemExit(f'port {args.port} is already serving a MixCut instance')

    state = SMOKE / 'state'
    if state.exists():
        import shutil
        shutil.rmtree(state)
    state.mkdir(parents=True, exist_ok=True)

    failures = []
    print(f'launching {EXE.name} --server --port {args.port}')
    process = subprocess.Popen([str(EXE), '--server', '--port', str(args.port),
                                '--state-dir', str(state)],
                               cwd=str(EXE.parent),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        data = None
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                failures.append('the service process exited during startup')
                break
            data = bootstrap(args.port)
            if data is not None:
                break
            time.sleep(0.4)

        if data is None:
            failures.append('/api/bootstrap never became healthy')
        else:
            print('  bootstrap OK')
            for key in ('ffmpeg_available', 'batches', 'video_dir', 'music_dir',
                        'output_dir', 'review_dir'):
                print(f'    {key:17} = {data.get(key)!r}')
            if not data.get('ffmpeg_available'):
                failures.append('bundled FFmpeg/ffprobe were not detected')

            for path in ('/', '/static/app.js', '/static/style.css'):
                status, body = get(f'http://127.0.0.1:{args.port}{path}')
                print(f'    {path:20} {status} {len(body):>7} bytes')
                if status != 200 or not body:
                    failures.append(f'{path} served {status} with {len(body)} bytes')

        if bootstrap(args.port, 1.0) is not None:
            request = urllib.request.Request(
                f'http://127.0.0.1:{args.port}/api/shutdown', data=b'{}', method='POST',
                headers={'Content-Type': 'application/json'})
            get_result = urllib.request.urlopen(request, timeout=15)
            with get_result as response:
                print('  shutdown ->', json.load(response))
            try:
                process.wait(timeout=30)
                print(f'  process exited with code {process.returncode}')
            except subprocess.TimeoutExpired:
                failures.append('the process did not exit after /api/shutdown')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=15)

    log = state / 'server.log'
    if failures and log.is_file():
        print('--- server.log ---')
        print(log.read_text(encoding='utf-8', errors='replace')[-1500:])

    if failures:
        print('\nFAILED:')
        for item in failures:
            print(f'  - {item}')
        return 1
    print('\nFROZEN BUILD OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
