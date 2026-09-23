"""macOS application launcher for MixCut Studio."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
import webbrowser

PORT = 8877
URL = f'http://127.0.0.1:{PORT}'


def _readiness(port=PORT, timeout=2.0):
    try:
        with urlopen(f'http://127.0.0.1:{port}/api/bootstrap', timeout=timeout) as response:
            data = json.load(response)
        return data if 'ffmpeg_available' in data and 'batches' in data else None
    except (URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def _attach_streams(state: Path):
    if sys.stdout is not None and sys.stderr is not None:
        return
    state.mkdir(parents=True, exist_ok=True)
    stream = open(state / 'launcher.log', 'ab', buffering=0)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _stop():
    try:
        request = Request(URL + '/api/shutdown', data=b'{}', method='POST',
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=10) as response:
            json.load(response)
    except Exception as exc:
        print(f'退出后台失败：{exc}', flush=True)
        return 1
    for _ in range(50):
        if _readiness(timeout=.5) is None:
            return 0
        time.sleep(.2)
    return 1


def _spawn(state: Path):
    log = open(state / 'server.log', 'ab')
    command = [sys.executable, '--server'] if getattr(sys, 'frozen', False) else [sys.executable, __file__, '--server']
    return subprocess.Popen(command, cwd=str(state), stdin=subprocess.DEVNULL,
                            stdout=log, stderr=log, start_new_session=True)


def main(argv):
    from mixcut.runtime import activate_bundled_tools, data_root

    activate_bundled_tools()
    data = data_root()
    state = data / '.mixcut'
    _attach_streams(state)

    if '--server' in argv:
        from mixcut import server
        forwarded = [item for item in argv if item != '--server']
        if '--state-dir' not in forwarded:
            forwarded += ['--state-dir', str(state)]
        return server.main(forwarded)
    if '--stop' in argv:
        return _stop()
    if _readiness() is not None:
        webbrowser.open(URL)
        return 0
    if not (os.environ.get('PATH') and _ffmpeg_from_path()):
        print('安装包内未找到 FFmpeg / ffprobe，请重新安装。', flush=True)
        return 1

    state.mkdir(parents=True, exist_ok=True)
    try:
        child = _spawn(state)
    except OSError as exc:
        print(f'无法启动后台服务：{exc}', flush=True)
        return 1
    for _ in range(75):
        if _readiness(timeout=1) is not None:
            webbrowser.open(URL)
            return 0
        if child.poll() is not None:
            break
        time.sleep(.2)
    print(f'后台服务启动失败，请查看 {state / "server.log"}', flush=True)
    return 1


def _ffmpeg_from_path():
    from shutil import which
    return which('ffmpeg') and which('ffprobe')


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
