# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the MixCut Studio Windows build.

Produces a one-folder bundle containing the Python runtime, the mixcut package,
its static assets and the bundled FFmpeg/ffprobe binaries.  Build with:

    pyinstaller --noconfirm --clean build_tools/mixcut.spec
"""
from pathlib import Path
import os

PROJECT = Path(SPECPATH).parent
SRC = PROJECT / 'src_pkg' / 'mixcut-studio-main'
# Honour MIXCUT_TOOLS so a fresh checkout can reuse an existing toolchain cache
# instead of re-downloading FFmpeg (see build_tools/build.py).
TOOLS = Path(os.environ.get('MIXCUT_TOOLS') or PROJECT / '_tools')
FFMPEG_DIR = TOOLS / 'ffmpeg-bin'
ICON = PROJECT / 'build_tools' / 'mixcut.ico'
VERSION_FILE = PROJECT.parent / 'VERSION'
VERSION_INFO = PROJECT / 'build_tools' / '_version_info.generated.txt'

for required in [SRC / 'mixcut' / 'server.py', SRC / 'winlaunch.py', SRC / 'static' / 'app.js',
                 FFMPEG_DIR / 'ffmpeg.exe', FFMPEG_DIR / 'ffprobe.exe', ICON,
                 VERSION_FILE, VERSION_INFO]:
    if not required.exists():
        raise SystemExit(f'missing build input: {required}')

datas = [(str(SRC / 'static'), 'static'), (str(VERSION_FILE), '.')]
binaries = [(str(FFMPEG_DIR / 'ffmpeg.exe'), 'bin'), (str(FFMPEG_DIR / 'ffprobe.exe'), 'bin')]

hiddenimports = [
    'mixcut.folders', 'mixcut.keyframes', 'mixcut.lockfile', 'mixcut.media',
    'mixcut.naming', 'mixcut.planner', 'mixcut.renderer', 'mixcut.runtime',
    'mixcut.scheduler', 'mixcut.server', 'mixcut.stickers',
]

a = Analysis(
    [str(SRC / 'winlaunch.py')],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Only drop the test suite. Anything stdlib that urllib/ssl/email touch must stay:
    # winlaunch imports urllib.request, which pulls in email and http.client.
    excludes=['tests'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MixCutStudio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
    version=str(VERSION_INFO),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='MixCutStudio',
)
