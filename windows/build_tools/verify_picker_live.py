"""Drive the real Windows folder dialog end to end and auto-confirm it.

A helper PowerShell process waits for the dialog to appear and presses Enter,
which activates the dialog's default button and returns the preselected folder.
Run under an external timeout; a hang means the dialog never accepted input.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src_pkg' / 'mixcut-studio-main'))

from mixcut import folders  # noqa: E402

SEND_ENTER = '''
Start-Sleep -Seconds 7
Add-Type -AssemblyName System.Windows.Forms
for ($i = 0; $i -lt 6; $i++) {
    [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
    Start-Sleep -Milliseconds 900
}
'''


def poke():
    script = Path(tempfile.gettempdir()) / 'mixcut-poke.ps1'
    script.write_text(SEND_ENTER, encoding='ascii')
    subprocess.run(['powershell.exe', '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
                    '-File', str(script)], capture_output=True)


def main():
    target = Path(tempfile.mkdtemp(prefix='picker-live-')) / '素材 目录'
    target.mkdir(parents=True)
    thread = threading.Thread(target=poke, daemon=True)
    thread.start()
    started = time.time()
    try:
        picked = folders.choose_folder('选择视频素材文件夹 - 自动化测试', str(target))
    except Exception as exc:
        print(f'  choose_folder raised: {type(exc).__name__}: {exc}')
        return 1
    print(f'  elapsed {time.time() - started:.1f}s')
    print(f'  returned {picked!r}')
    if picked is None:
        print('  dialog was dismissed or never confirmed')
        return 1
    if Path(picked) != target.resolve():
        print(f'  unexpected path (expected {target.resolve()})')
        return 1
    print('real dialog confirmed OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
