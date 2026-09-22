"""Double-click launcher: start a detached local service and open the browser."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.request import urlopen
import webbrowser

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:8877'


def ready():
    try:
        with urlopen(URL + '/api/bootstrap', timeout=2) as response:
            data = json.load(response)
            return 'ffmpeg_available' in data and 'batches' in data
    except Exception:
        return False


def main():
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        print('缺少 FFmpeg / ffprobe。请安装 FFmpeg 后重新启动。')
        return 1
    if not ready():
        state = ROOT / '.mixcut'
        state.mkdir(exist_ok=True)
        with (state / 'server.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, '-m', 'mixcut.server'], cwd=ROOT,
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True)
        for _ in range(50):
            if ready():
                break
            if process.poll() is not None:
                print(f'启动失败，请查看 {state / "server.log"}')
                return 1
            time.sleep(0.2)
        else:
            print(f'启动超时，请查看 {state / "server.log"}')
            return 1
    print(f'工具已启动：{URL}\n关闭浏览器不停止后台任务。退出后台请使用界面中的退出按钮。')
    webbrowser.open(URL)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
