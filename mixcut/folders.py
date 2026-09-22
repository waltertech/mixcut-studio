"""Native Mac folder selection and stable batch output locations."""
from pathlib import Path
import subprocess
import sys


def choose_folder(prompt, current_path=''):
    if sys.platform != 'darwin':
        raise ValueError('当前文件夹选择器仅支持 Mac；仍可直接填写文件夹路径')
    initial = Path(current_path or Path.home()).expanduser().resolve()
    while not initial.is_dir() and initial != initial.parent:
        initial = initial.parent
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


def batch_output_folder(batch):
    """Old batches keep their original UUID folders; new batches use dated folders."""
    return Path(batch.get('output_folder') or Path(batch['config']['output_dir']) / batch['id'])
