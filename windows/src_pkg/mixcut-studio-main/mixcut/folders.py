"""Native folder selection, file-manager reveal and stable batch output locations."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile


def _existing_directory(candidate):
    """Walk upwards until an existing directory is found."""
    initial = Path(candidate).expanduser()
    try:
        initial = initial.resolve()
    except OSError:
        return Path.home()
    while not initial.is_dir() and initial != initial.parent:
        initial = initial.parent
    return initial


def _choose_folder_macos(prompt, current_path=''):
    initial = _existing_directory(current_path or Path.home())
    # Arguments are passed separately; folder names are never interpolated into AppleScript.
    script = '''on run argv
        activate
        try
            set chosen to choose folder with prompt (item 1 of argv) default location (POSIX file (item 2 of argv))
            return POSIX path of chosen
        on error messageText number errorNumber
            if errorNumber is -128 then return ""
            error messageText number errorNumber
        end try
    end run'''
    result = subprocess.run(['/usr/bin/osascript', '-e', script, prompt, str(initial)],
                            capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise ValueError('无法打开文件夹选择器：' + result.stderr.strip()[-500:])
    selected = result.stdout.strip()
    if not selected:
        return None
    path = Path(selected).resolve()
    if not path.is_dir():
        raise ValueError('选择的文件夹不存在')
    return str(path)


# Windows PowerShell host script. The body stays pure ASCII: the Chinese prompt and
# the initial path arrive as arguments and the result is written as UTF-8 to a file,
# so no console code page can corrupt either direction.
_WINDOWS_PICKER = '''param(
    [string]$Prompt,
    [string]$Initial,
    [string]$ResultFile
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = $Prompt
$dialog.ShowNewFolderButton = $true
if ($Initial -and (Test-Path -LiteralPath $Initial)) {
    $dialog.SelectedPath = $Initial
}
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$owner.Opacity = 0
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$owner.Show()
$owner.Activate()
try {
    if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        $encoding = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($ResultFile, $dialog.SelectedPath, $encoding)
    }
}
finally {
    $owner.Close()
    $owner.Dispose()
    $dialog.Dispose()
}
'''


def _choose_folder_windows(prompt, current_path=''):
    initial = _existing_directory(current_path or Path.home())
    workdir = Path(tempfile.mkdtemp(prefix='mixcut-picker-'))
    script = workdir / 'picker.ps1'
    script.write_text(_WINDOWS_PICKER, encoding='ascii')
    result_file = workdir / 'result.txt'
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
             '-File', str(script), prompt, str(initial), str(result_file)],
            capture_output=True, text=True, errors='replace', timeout=600, creationflags=flags)
        if result.returncode:
            raise ValueError('无法打开文件夹选择器：' + (result.stderr or '').strip()[-500:])
        if not result_file.is_file():
            return None
        selected = result_file.read_text(encoding='utf-8-sig').strip()
        if not selected:
            return None
        path = Path(selected)
        if not path.is_dir():
            raise ValueError('选择的文件夹不存在')
        return str(path.resolve())
    finally:
        for leftover in (script, result_file):
            try:
                leftover.unlink()
            except OSError:
                pass
        try:
            workdir.rmdir()
        except OSError:
            pass


def choose_folder(prompt, current_path=''):
    if sys.platform == 'darwin':
        return _choose_folder_macos(prompt, current_path)
    if os.name == 'nt':
        return _choose_folder_windows(prompt, current_path)
    raise ValueError('当前系统不支持文件夹选择器；仍可直接填写文件夹路径')


def reveal(target):
    """Show a file or directory in the platform file manager."""
    target = Path(target)
    if sys.platform == 'darwin':
        subprocess.Popen(['open', '-R', str(target)] if target.is_file() else ['open', str(target)])
    elif os.name == 'nt':
        if target.is_file():
            # explorer.exe needs the selection switch and the path in one token.
            subprocess.Popen(['explorer', f'/select,{target}'])
        else:
            os.startfile(str(target))  # noqa: S606 - intentional shell open
    else:
        subprocess.Popen(['xdg-open', str(target.parent if target.is_file() else target)])


def batch_output_folder(batch):
    """Old batches keep their original UUID folders; new batches use dated folders."""
    return Path(batch.get('output_folder') or Path(batch['config']['output_dir']) / batch['id'])
