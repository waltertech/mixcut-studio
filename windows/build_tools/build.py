"""One-shot build pipeline for the MixCut Studio Windows installer.

Steps: fetch/extract FFmpeg -> generate the icon -> freeze with PyInstaller ->
optionally smoke-test the frozen bundle -> compile the Inno Setup installer.

Run with the build virtual environment:

    "%USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\mixcut\\Scripts\\python.exe" build_tools/build.py

Add --skip-freeze / --skip-installer to resume partway, and --verify to start the
frozen executable, wait for a healthy bootstrap response and shut it down again.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

PROJECT = Path(__file__).resolve().parent.parent
# MIXCUT_TOOLS lets a second checkout reuse an already-populated toolchain cache.
TOOLS = Path(os.environ.get('MIXCUT_TOOLS') or PROJECT / '_tools')
BUILD_TOOLS = PROJECT / 'build_tools'
SRC = PROJECT / 'src_pkg' / 'mixcut-studio-main'
FFMPEG_ZIP = TOOLS / 'ffmpeg-essentials.zip'
FFMPEG_BIN = TOOLS / 'ffmpeg-bin'
FFMPEG_URL = ('https://github.com/GyanD/codexffmpeg/releases/download/'
              '9.0.2/ffmpeg-9.0.2-essentials_build.zip')
INNO_VERSION = '6.7.3'
INNO_TAG = 'is-6_7_3'
INNO_URL = (f'https://github.com/jrsoftware/issrc/releases/download/'
            f'{INNO_TAG}/innosetup-{INNO_VERSION}.exe')
INNO_LANG_URL = (f'https://raw.githubusercontent.com/jrsoftware/issrc/{INNO_TAG}/'
                 'Files/Languages/Unofficial/ChineseSimplified.isl')
INNO_DIR = TOOLS / 'InnoSetup'
ISCC = INNO_DIR / 'ISCC.exe'
INNO_LANG = INNO_DIR / 'Languages' / 'ChineseSimplified.isl'
SPEC = BUILD_TOOLS / 'mixcut.spec'
ISS = BUILD_TOOLS / 'mixcut-setup.iss'
DIST = PROJECT / 'dist'
FROZEN = DIST / 'MixCutStudio'
EXE = FROZEN / 'MixCutStudio.exe'
INSTALLER_DIR = PROJECT / 'output'
VERSION_FILE = PROJECT.parent / 'VERSION'
VERSION = VERSION_FILE.read_text(encoding='utf-8').strip()
if not re.fullmatch(r'\d+\.\d+\.\d+', VERSION):
    raise SystemExit('VERSION must use semantic form MAJOR.MINOR.PATCH')
VERSION_INFO = BUILD_TOOLS / '_version_info.generated.txt'


def log(message):
    print(f'==> {message}', flush=True)


def download(url, target):
    """Fetch ``url`` to ``target`` atomically (via a .part file)."""
    log(f'downloading {target.name} <- {url}')
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + '.part')
    request = urllib.request.Request(url, headers={'User-Agent': 'mixcut-build'})
    with urllib.request.urlopen(request, timeout=300) as response, partial.open('wb') as sink:
        shutil.copyfileobj(response, sink, length=1 << 20)
    partial.replace(target)


def download_ffmpeg():
    if FFMPEG_ZIP.is_file() and zipfile.is_zipfile(FFMPEG_ZIP):
        return
    # github.com/<owner>/<repo>/releases/download/... is usually reachable even when
    # the upstream vendor site or a plain clone is not.
    download(FFMPEG_URL, FFMPEG_ZIP)


def ensure_inno_setup():
    """Install Inno Setup + the Simplified Chinese language pack into _tools.

    The source package deliberately ships without the toolchain, so a fresh
    checkout can still reach a finished installer with a single command.
    """
    if not ISCC.is_file():
        installer = TOOLS / f'innosetup-{INNO_VERSION}.exe'
        if not installer.is_file():
            download(INNO_URL, installer)
        log(f'installing Inno Setup {INNO_VERSION} -> {INNO_DIR}')
        subprocess.run([str(installer), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                        '/SP-', '/CURRENTUSER', f'/DIR={INNO_DIR}'], check=True)
        if not ISCC.is_file():
            raise SystemExit(f'Inno Setup install did not produce {ISCC}')
    if not INNO_LANG.is_file():
        # Ships without a Simplified Chinese translation; the .iss references it.
        download(INNO_LANG_URL, INNO_LANG)
    return ISCC


def extract_ffmpeg():
    wanted = {'ffmpeg.exe', 'ffprobe.exe'}
    FFMPEG_BIN.mkdir(parents=True, exist_ok=True)
    missing = {name for name in wanted if not (FFMPEG_BIN / name).is_file()}
    if not missing:
        return
    log(f'extracting {sorted(missing)} -> {FFMPEG_BIN}')
    with zipfile.ZipFile(FFMPEG_ZIP) as archive:
        for member in archive.infolist():
            name = Path(member.filename).name
            if name in missing and '/bin/' in member.filename.replace('\\', '/'):
                with archive.open(member) as source, (FFMPEG_BIN / name).open('wb') as sink:
                    shutil.copyfileobj(source, sink, length=1 << 20)
                log(f'  {name} ({(FFMPEG_BIN / name).stat().st_size / 1e6:.1f} MB)')


def make_icon():
    target = BUILD_TOOLS / 'mixcut.ico'
    if target.is_file():
        # The icon ships with the source package, so Pillow is only needed when
        # regenerating it. Delete the file (or pass --force-icon) to redraw.
        log(f'icon present, keeping {target.name}')
        return
    log('generating application icon')
    subprocess.run([sys.executable, str(BUILD_TOOLS / 'make_icon.py'), str(target)], check=True)


def write_version_info():
    """Generate the Windows executable resource from the shared root VERSION."""
    template = (BUILD_TOOLS / 'version_info.txt').read_text(encoding='utf-8')
    VERSION_INFO.write_text(template.replace('__VERSION_QUAD__', f'{VERSION}.0'), encoding='utf-8')


def freeze():
    log('freezing with PyInstaller')
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                    '--distpath', str(DIST), '--workpath', str(PROJECT / 'build'),
                    str(SPEC)], check=True, cwd=str(PROJECT))
    if not EXE.is_file():
        raise SystemExit(f'freeze did not produce {EXE}')


def _bootstrap(timeout=2.0):
    import json
    try:
        with urllib.request.urlopen('http://127.0.0.1:8877/api/bootstrap', timeout=timeout) as response:
            return json.load(response)
    except Exception:
        return None


def verify():
    """Start the frozen build and confirm the bundled FFmpeg is detected."""
    log('smoke-testing the frozen build')
    if _bootstrap() is not None:
        raise SystemExit('port 8877 is already in use; stop any running MixCut Studio first')
    process = subprocess.Popen([str(EXE)], cwd=str(FROZEN))
    try:
        for _ in range(60):
            data = _bootstrap(timeout=1.0)
            if data is not None:
                break
            time.sleep(0.5)
        else:
            (FROZEN / '_verify.log').write_text('bootstrap never became ready', encoding='utf-8')
            raise SystemExit('frozen build did not become ready')
        server = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'MixCutStudio' / '.mixcut' / 'server.log'
        print(f'    ffmpeg_available = {data.get("ffmpeg_available")}')
        print(f'    batches          = {len(data.get("batches", []))}')
        print(f'    default video_dir= {data.get("video_dir")}')
        if not data.get('ffmpeg_available'):
            raise SystemExit(f'bundled FFmpeg was not detected (see {server})')
    finally:
        subprocess.run([str(EXE), '--stop'], check=False, cwd=str(FROZEN))
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
    log('smoke test passed')


def build_installer():
    ensure_inno_setup()
    log('compiling the installer')
    INSTALLER_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(ISCC), f'/DAppVersion={VERSION}', str(ISS)], check=True,
                   cwd=str(BUILD_TOOLS))
    produced = sorted(INSTALLER_DIR.glob('MixCutStudio-*-Setup.exe'))
    if not produced:
        raise SystemExit('installer was not produced')
    for path in produced:
        print(f'    {path}  ({path.stat().st_size / 1e6:.1f} MB)')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-freeze', action='store_true')
    parser.add_argument('--skip-installer', action='store_true')
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()

    if not SRC.is_dir():
        raise SystemExit(f'source tree missing: {SRC}')
    write_version_info()
    make_icon()
    if not args.skip_freeze:
        download_ffmpeg()
        extract_ffmpeg()
        freeze()
    if args.verify:
        verify()
    if not args.skip_installer:
        build_installer()
    log('done')


if __name__ == '__main__':
    main()
