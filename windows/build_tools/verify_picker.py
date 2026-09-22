"""Verify the Windows folder-picker plumbing without a human clicking the dialog.

Checks the Python side of mixcut.folders.choose_folder: temp script writing,
argument passing, Chinese prompt handling, UTF-8 result round-trip for a path
containing non-ASCII characters, and cleanup of the temporary directory.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src_pkg' / 'mixcut-studio-main'))

from mixcut import folders  # noqa: E402

AUTO_CONFIRM = '''param(
    [string]$Prompt,
    [string]$Initial,
    [string]$ResultFile
)
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = $Prompt
[System.IO.File]::WriteAllText($ResultFile, "$Prompt|$Initial", (New-Object System.Text.UTF8Encoding($false)))
'''


def main():
    workdir = Path(tempfile.mkdtemp(prefix='picker-test-'))
    # A path with Chinese characters exercises the whole encoding chain.
    target = workdir / '素材目录 测试' / '子目录'
    target.mkdir(parents=True)
    prompt = '选择视频素材文件夹'

    original = folders._WINDOWS_PICKER
    captured = workdir / 'captured.txt'
    folders._WINDOWS_PICKER = AUTO_CONFIRM.replace('$Prompt|$Initial', '$Prompt|$Initial')
    try:
        # Route the script result into a file we control, then read the arguments back.
        probe = AUTO_CONFIRM
        result_file = workdir / 'probe.txt'
        script = workdir / 'probe.ps1'
        script.write_text(probe, encoding='ascii')
        import subprocess
        run = subprocess.run(
            ['powershell.exe', '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
             '-File', str(script), prompt, str(target), str(result_file)],
            capture_output=True, text=True, errors='replace', timeout=120,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        assert run.returncode == 0, f'powershell failed: {run.stderr}'
        round_trip = result_file.read_text(encoding='utf-8-sig')
        sent_prompt, sent_initial = round_trip.split('|', 1)
        assert sent_prompt == prompt, f'prompt garbled: {sent_prompt!r} != {prompt!r}'
        assert sent_initial == str(target), f'path garbled: {sent_initial!r} != {target!r}'
        print(f'  argument round-trip OK: prompt={sent_prompt!r}')
        print(f'  argument round-trip OK: path={sent_initial!r}')

        # Now exercise choose_folder itself with the dialog replaced by an auto-confirm.
        # The auto-confirm variant echoes back SelectedPath, which is seeded from
        # current_path, so a correct call must return the path we pass in.
        folders._WINDOWS_PICKER = AUTO_CONFIRM_BROWSER
        picked = folders.choose_folder(prompt, str(target))
        assert picked is not None, 'choose_folder returned None'
        assert Path(picked) == target.resolve(), f'{picked} != {target.resolve()}'
        print(f'  choose_folder returned  {picked}')
    finally:
        folders._WINDOWS_PICKER = original

    leftovers = list(Path(tempfile.gettempdir()).glob('mixcut-picker-*'))
    print(f'  leftover picker temp dirs: {len(leftovers)}')
    assert not leftovers, f'temporary directories were not cleaned up: {leftovers}'
    print('folder picker plumbing OK')


AUTO_CONFIRM_BROWSER = '''param(
    [string]$Prompt,
    [string]$Initial,
    [string]$ResultFile
)
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = $Prompt
if ($Initial -and (Test-Path -LiteralPath $Initial)) {
    $dialog.SelectedPath = $Initial
}
$dialog.ShowNewFolderButton = $true
[System.IO.File]::WriteAllText($ResultFile, $dialog.SelectedPath, (New-Object System.Text.UTF8Encoding($false)))
'''


if __name__ == '__main__':
    main()
