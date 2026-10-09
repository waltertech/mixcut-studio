"""Use the operating system's recycle bin; never silently fall back to unlink."""
import base64
from pathlib import Path
import subprocess
import sys


def move_to_trash(path):
    path = Path(path).absolute()
    if not path.exists() and not path.is_symlink():
        return None
    if sys.platform == 'darwin':
        script = '''ObjC.import('Foundation');
function run(argv) {
 const ok=$.NSFileManager.defaultManager.trashItemAtURLResultingItemURLError($.NSURL.fileURLWithPath(argv[0]),null,null);
 if (!ok) throw Error('系统无法将此路径移至废纸篓');
 return 'recycled';
}'''
        result = subprocess.run(['/usr/bin/osascript','-l','JavaScript','-e',script,str(path)],capture_output=True,text=True,timeout=60)
    elif sys.platform == 'win32':
        script = "$p='" + str(path).replace("'", "''") + "'; Add-Type -AssemblyName Microsoft.VisualBasic; if(Test-Path -LiteralPath $p -PathType Container){[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($p,'OnlyErrorDialogs','SendToRecycleBin')}else{[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($p,'OnlyErrorDialogs','SendToRecycleBin')}"
        result = subprocess.run(['powershell','-NoProfile','-NonInteractive','-EncodedCommand',base64.b64encode(script.encode('utf-16le')).decode('ascii')],capture_output=True,text=True,timeout=60)
    else:
        raise OSError('此系统暂不支持移至废纸篓，文件已保留')
    if result.returncode or path.exists():
        raise OSError('移至废纸篓失败，文件保留：' + (result.stderr.strip() or str(path)))
    return result.stdout.strip() or None
