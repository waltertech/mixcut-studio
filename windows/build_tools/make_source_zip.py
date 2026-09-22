"""Package the Windows-ported source tree into a single distributable zip.

The archive contains everything needed to rebuild the installer from scratch:
the ported source under ``src_pkg/``, the build/verification scripts under
``build_tools/`` and both delivery documents.  Toolchain caches, build output
and bytecode are deliberately left out; ``build_tools/build.py`` re-fetches the
toolchain on demand.

    python build_tools/make_source_zip.py [--output-dir output]
"""
from __future__ import annotations

import argparse
from pathlib import Path
import zipfile

PROJECT = Path(__file__).resolve().parent.parent
VERSION = '1.0.0'
ROOT_NAME = f'MixCutStudio-{VERSION}-src'

SOURCES = [
    (PROJECT / 'src_pkg' / 'mixcut-studio-main', 'src_pkg/mixcut-studio-main'),
    (PROJECT / 'build_tools', 'build_tools'),
    (PROJECT / '源码包说明.md', None),
    (PROJECT / '交付说明.md', None),
]

# Bytecode and editor/OS noise that must never travel in a source package.
EXCLUDED_DIRS = {'__pycache__', '.pytest_cache', '.mypy_cache', '.git', '.idea', '.vscode'}
EXCLUDED_SUFFIXES = {'.pyc', '.pyo', '.pyd'}
EXCLUDED_NAMES = {'Thumbs.db', 'desktop.ini', '.DS_Store'}


def wanted(relative: Path) -> bool:
    if any(part in EXCLUDED_DIRS for part in relative.parts):
        return False
    if relative.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    return relative.name not in EXCLUDED_NAMES


def collect():
    """Yield (absolute path, archive-relative path); stable, sorted order."""
    root = Path(ROOT_NAME)
    entries = []
    for source, prefix in SOURCES:
        if not source.exists():
            raise SystemExit(f'missing source for the archive: {source}')
        if source.is_file():
            entries.append((source, root / source.name))
            continue
        for path in sorted(source.rglob('*')):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            if not wanted(relative) or not wanted(path.relative_to(PROJECT)):
                continue
            entries.append((path, root / prefix / relative))
    return sorted(entries, key=lambda item: str(item[1]).lower())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', default=str(PROJECT / 'output'))
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f'{ROOT_NAME}.zip'

    entries = collect()
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source, relative in entries:
            archive.write(source, str(relative).replace('\\', '/'))

    total = sum(path.stat().st_size for path, _ in entries)
    print(f'{archive_path}')
    print(f'  files       : {len(entries)}')
    print(f'  source size : {total / 1024:.0f} KB')
    print(f'  archive size: {archive_path.stat().st_size / 1024:.0f} KB')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
