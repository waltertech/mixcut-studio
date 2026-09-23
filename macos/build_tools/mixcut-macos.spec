# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os

PROJECT = Path(SPECPATH).parent.parent
MACOS = PROJECT / 'macos'
ICON = MACOS / 'build_tools' / 'MixCutStudio.icns'
VERSION = os.environ['MIXCUT_VERSION']
FFMPEG = Path(os.environ['MIXCUT_FFMPEG'])
FFPROBE = Path(os.environ['MIXCUT_FFPROBE'])

for required in (PROJECT / 'maclaunch.py', PROJECT / 'static' / 'app.js', PROJECT / 'VERSION',
                 ICON, FFMPEG, FFPROBE):
    if not required.exists():
        raise SystemExit(f'missing build input: {required}')

a = Analysis(
    [str(PROJECT / 'maclaunch.py')],
    pathex=[str(PROJECT)],
    binaries=[(str(FFMPEG), 'bin'), (str(FFPROBE), 'bin')],
    datas=[(str(PROJECT / 'static'), 'static'), (str(PROJECT / 'VERSION'), '.')],
    hiddenimports=[
        'mixcut.folders', 'mixcut.keyframes', 'mixcut.lockfile', 'mixcut.media',
        'mixcut.naming', 'mixcut.planner', 'mixcut.renderer', 'mixcut.runtime',
        'mixcut.scheduler', 'mixcut.server', 'mixcut.stickers',
    ],
    excludes=['tests'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name='MixCutStudio',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, argv_emulation=False, target_arch=None,
    codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='MixCutStudio')
app = BUNDLE(
    coll,
    name='MixCut Studio.app',
    icon=str(ICON),
    bundle_identifier='com.waltertech.mixcutstudio',
    version=VERSION,
    info_plist={
        'CFBundleDisplayName': 'MixCut Studio',
        'CFBundleShortVersionString': VERSION,
        'CFBundleVersion': VERSION,
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '12.0',
    },
)
