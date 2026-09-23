"""Build, verify and package the native macOS MixCut Studio application."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

PROJECT = Path(__file__).resolve().parent.parent.parent
MACOS = PROJECT / 'macos'
SPEC = MACOS / 'build_tools' / 'mixcut-macos.spec'
ICON = MACOS / 'build_tools' / 'MixCutStudio.icns'
DIST = MACOS / 'dist'
BUILD = MACOS / 'build'
OUTPUT = MACOS / 'output'
APP = DIST / 'MixCut Studio.app'
EXECUTABLE = APP / 'Contents' / 'MacOS' / 'MixCutStudio'
VERIFY_PORT = 8893


def run(command, **kwargs):
    print('==>', ' '.join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def version():
    value = (PROJECT / 'VERSION').read_text(encoding='utf-8').strip()
    parts = value.split('.')
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise SystemExit('VERSION must use semantic form MAJOR.MINOR.PATCH')
    return value


def make_icon():
    run([sys.executable, MACOS / 'build_tools' / 'make_icon.py', ICON])


def freeze(release_version):
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise SystemExit('ffmpeg and ffprobe are required to build the self-contained app')
    env = dict(os.environ, MIXCUT_VERSION=release_version,
               MIXCUT_FFMPEG=ffmpeg, MIXCUT_FFPROBE=ffprobe)
    run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
         '--distpath', DIST, '--workpath', BUILD, SPEC], cwd=PROJECT, env=env)
    if not EXECUTABLE.is_file():
        raise SystemExit(f'PyInstaller did not produce {EXECUTABLE}')
    run(['codesign', '--force', '--deep', '--sign', '-', APP])
    run(['codesign', '--verify', '--deep', '--strict', APP])


def bootstrap(port, timeout=1):
    try:
        with urlopen(f'http://127.0.0.1:{port}/api/bootstrap', timeout=timeout) as response:
            return json.load(response)
    except Exception:
        return None


def verify(release_version):
    if bootstrap(VERIFY_PORT) is not None:
        raise SystemExit(f'port {VERIFY_PORT} is already in use')
    with tempfile.TemporaryDirectory(prefix='mixcut-macos-verify-') as state:
        process = subprocess.Popen([str(EXECUTABLE), '--server', '--port', str(VERIFY_PORT),
                                    '--state-dir', state], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True)
        try:
            for _ in range(80):
                data = bootstrap(VERIFY_PORT)
                if data is not None:
                    break
                if process.poll() is not None:
                    raise SystemExit('frozen application exited before becoming ready')
                time.sleep(.25)
            else:
                raise SystemExit('frozen application did not become ready')
            if not data.get('ffmpeg_available'):
                raise SystemExit('bundled FFmpeg was not detected')
            if data.get('version') != release_version:
                raise SystemExit(f'embedded version mismatch: {data.get("version")}')
            print(f'==> verified version={data["version"]} ffmpeg_available=True')
            request = Request(f'http://127.0.0.1:{VERIFY_PORT}/api/shutdown', data=b'{}', method='POST',
                              headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=10):
                pass
            process.wait(timeout=20)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


def package(release_version):
    architecture = platform.machine()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    target = OUTPUT / f'MixCutStudio-{release_version}-macOS-{architecture}.dmg'
    with tempfile.TemporaryDirectory(prefix='mixcut-dmg-') as temporary:
        staging = Path(temporary) / 'MixCut Studio'
        staging.mkdir()
        shutil.copytree(APP, staging / APP.name, symlinks=True)
        os.symlink('/Applications', staging / 'Applications')
        run(['hdiutil', 'create', '-volname', f'MixCut Studio {release_version}',
             '-srcfolder', staging, '-ov', '-format', 'UDZO', target])
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (target.with_suffix(target.suffix + '.sha256')).write_text(f'{digest}  {target.name}\n', encoding='utf-8')
    print(f'==> {target} ({target.stat().st_size / 1_000_000:.1f} MB)')
    print(f'==> SHA256 {digest}')
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-freeze', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        raise SystemExit('the macOS package must be built on macOS')
    release_version = version()
    make_icon()
    if not args.skip_freeze:
        freeze(release_version)
    verify(release_version)
    package(release_version)


if __name__ == '__main__':
    main()
