"""Windows launcher for MixCut Studio.

Running the executable starts the loopback service in a detached child process and
opens the browser, mirroring the original double-click launcher. ``--server`` runs
the service in the foreground and ``--stop`` shuts a running service down.
"""
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
APP_TITLE = 'MixCut Studio'


def _attach_streams(state: Path):
    """Windowed builds have no console; send anything printed to the server log."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    state.mkdir(parents=True, exist_ok=True)
    stream = open(state / 'launcher.log', 'ab', buffering=0)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _readiness(timeout=2.0):
    try:
        with urlopen(URL + '/api/bootstrap', timeout=timeout) as response:
            data = json.load(response)
        return data if 'ffmpeg_available' in data and 'batches' in data else None
    except (URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def _notice(text, title=APP_TITLE):
    """Show a native message box; the GUI may be invisible in this process."""
    if sys.stderr is not None:
        try:
            print(text, flush=True)
        except Exception:
            pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:
        pass


def _stop():
    try:
        request = Request(URL + '/api/shutdown', data=b'{}', method='POST',
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=10) as response:
            json.load(response)
    except Exception as exc:
        _notice(f'退出后台失败：{exc}\n服务可能没有在运行。')
        return 1
    for _ in range(50):
        if _readiness(timeout=0.5) is None:
            return 0
        time.sleep(0.2)
    _notice('已发送退出请求，但服务仍在运行，请稍后重试。')
    return 1


def _spawn(state: Path):
    flags = 0
    for name in ('DETACHED_PROCESS', 'CREATE_NEW_PROCESS_GROUP', 'CREATE_NO_WINDOW'):
        flags |= getattr(subprocess, name, 0)
    log = open(state / 'server.log', 'ab')
    return subprocess.Popen([sys.executable, '--server'], cwd=str(state),
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            creationflags=flags)


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
        _notice('未找到 FFmpeg / ffprobe。\n\n安装包若已包含 FFmpeg，请重新安装；'
                '否则请安装 FFmpeg 后重新启动。')
        return 1

    state.mkdir(parents=True, exist_ok=True)
    try:
        child = _spawn(state)
    except OSError as exc:
        _notice(f'无法启动后台服务：{exc}')
        return 1

    for _ in range(75):
        if _readiness(timeout=1.0) is not None:
            webbrowser.open(URL)
            return 0
        if child.poll() is not None:
            break
        time.sleep(0.2)

    detail = ''
    log_path = state / 'server.log'
    if log_path.is_file():
        detail = log_path.read_text(encoding='utf-8', errors='replace').strip()[-800:]
    _notice('后台服务启动失败，请查看日志：\n'
            f'{log_path}\n\n{detail}')
    return 1


def _ffmpeg_from_path():
    from shutil import which
    return which('ffmpeg') and which('ffprobe')


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
