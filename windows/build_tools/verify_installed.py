"""End-to-end check of the *installed* MixCut Studio, using only its own entry points.

Exercises the same path a user takes: launch the shortcut target, confirm the
loopback service answers and that FFmpeg resolves, then stop it with ``--stop``.
Run with any Python 3.10+:

    python build_tools/verify_installed.py [--port 8877]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

LOCALAPPDATA = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
INSTALLED = LOCALAPPDATA / 'Programs' / 'MixCut Studio'
EXE = INSTALLED / 'MixCutStudio.exe'
DATA = LOCALAPPDATA / 'MixCutStudio'


def bootstrap(port, timeout=3.0):
    try:
        url = f'http://127.0.0.1:{port}/api/bootstrap'
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.load(response)
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8877)
    parser.add_argument('--timeout', type=float, default=45.0)
    args = parser.parse_args()

    if not EXE.is_file():
        raise SystemExit(f'not installed at {EXE}')
    if bootstrap(args.port, 1.0) is not None:
        raise SystemExit(f'port {args.port} is already serving; stop it first')

    failures = []
    print(f'launching installed app: {EXE}')
    launcher = subprocess.Popen([str(EXE)], cwd=str(INSTALLED),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    data = None
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        data = bootstrap(args.port)
        if data is not None:
            break
        time.sleep(0.5)

    if data is None:
        failures.append('the service never answered /api/bootstrap')
    else:
        print('  service is up')
        for key in ('ffmpeg_available', 'video_dir', 'music_dir', 'output_dir', 'review_dir'):
            print(f'    {key:17} = {data.get(key)!r}')
        if not data.get('ffmpeg_available'):
            failures.append('ffmpeg_available is False')
        for key in ('video_dir', 'music_dir', 'output_dir', 'review_dir'):
            value = data.get(key)
            if not value or not value.startswith(str(DATA)):
                failures.append(f'{key} is not under the per-user data directory: {value!r}')
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/', timeout=10) as response:
                body = response.read()
            print(f'    index page {response.status}, {len(body)} bytes')
            if response.status != 200 or len(body) < 1000:
                failures.append('the index page did not load')
        except Exception as exc:
            failures.append(f'index page failed: {exc}')

    print('  stopping via --stop')
    stop = subprocess.run([str(EXE), '--stop'], cwd=str(INSTALLED),
                          capture_output=True, text=True, timeout=60)
    print(f'    exit={stop.returncode}')
    if stop.returncode != 0:
        failures.append(f'--stop returned {stop.returncode}')

    settled = False
    for _ in range(30):
        if bootstrap(args.port, 1.0) is None:
            settled = True
            break
        time.sleep(0.5)
    if not settled:
        failures.append('the service still answers after --stop')

    if launcher.poll() is None:
        launcher.wait(timeout=20)

    log = DATA / '.mixcut' / 'server.log'
    if log.is_file():
        print('  --- server.log (tail) ---')
        for line in log.read_text(encoding='utf-8', errors='replace').splitlines()[-6:]:
            print(f'    {line}')

    if failures:
        print('\nFAILED:')
        for item in failures:
            print(f'  - {item}')
        return 1
    print('\nINSTALLED APP OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
