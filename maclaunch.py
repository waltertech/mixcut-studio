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


def _compatible(data):
    from mixcut.runtime import API_PROTOCOL, app_version
    return (data is not None and data.get('api_protocol') == API_PROTOCOL
            and data.get('version') == app_version())


def _active_tasks(data):
    return any(batch.get('status') in {'queued', 'running', 'pausing', 'stopping'}
               for batch in data.get('batches', []))


def _show_error(message):
    quoted = message.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    subprocess.run(['osascript', '-e', f'display alert "MixCut Studio 无法启动" message "{quoted}"'],
                   check=False)


def _open_client():
    if getattr(sys, 'frozen', False):
        window = Path(sys.executable).parent / 'MixCutStudioWindow'
        if not window.is_file():
            raise RuntimeError('安装包缺少独立窗口，请重新安装。')
        if subprocess.call([str(window), URL]) != 0:
            raise RuntimeError('独立窗口未能打开，请重新安装或检查系统日志。')
        return 0
    webbrowser.open(URL)
    return 0


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
    try:
        return subprocess.Popen(command, cwd=str(state), stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, start_new_session=True)
    finally:
        log.close()


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
    try:
        existing = _readiness()
        if existing is not None and not _compatible(existing):
            if _active_tasks(existing):
                raise RuntimeError('旧版后台仍有正在执行的任务。请等待任务完成，再重新打开应用。')
            if _stop() != 0:
                raise RuntimeError('无法关闭旧版后台。请在旧版页面点击“停止后台服务”后重试。')
            existing = None
        if _compatible(existing):
            return _open_client()
        if not (os.environ.get('PATH') and _ffmpeg_from_path()):
            raise RuntimeError('安装包内未找到 FFmpeg / ffprobe，请重新安装。')
        state.mkdir(parents=True, exist_ok=True)
        child = _spawn(state)
        for _ in range(75):
            current = _readiness(timeout=1)
            if _compatible(current):
                return _open_client()
            if current is not None:
                raise RuntimeError('8877 端口仍由旧版后台占用，请停止旧版后台后重试。')
            if child.poll() is not None:
                break
            time.sleep(.2)
        raise RuntimeError(f'后台服务启动失败，请查看 {state / "server.log"}')
    except (OSError, RuntimeError) as exc:
        print(f'启动失败：{exc}', flush=True)
        if getattr(sys, 'frozen', False):
            _show_error(str(exc))
        return 1


def _ffmpeg_from_path():
    from shutil import which
    return which('ffmpeg') and which('ffprobe')


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
