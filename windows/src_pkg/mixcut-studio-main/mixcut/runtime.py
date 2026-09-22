"""Runtime locations for both source checkouts and frozen Windows builds.

Read-only resources (``static/``, bundled ``bin/``) are resolved relative to the
bundle, while everything the tool writes lives in a per-user data directory so a
build installed under ``Program Files`` never tries to write next to itself.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = 'MixCutStudio'
DATA_ENV_VAR = 'MIXCUT_DATA_DIR'


def frozen() -> bool:
    return bool(getattr(sys, 'frozen', False))


def resource_root() -> Path:
    """Directory holding read-only bundled resources."""
    if frozen():
        bundle = getattr(sys, '_MEIPASS', None)
        if bundle:
            return Path(bundle)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_root() -> Path:
    """Writable per-user directory for state, cache and default material folders."""
    override = os.environ.get(DATA_ENV_VAR)
    if override:
        return Path(override).expanduser()
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
        if base:
            return Path(base) / APP_NAME
    return resource_root()


def bundled_bin() -> Path:
    """Directory containing the FFmpeg binaries shipped with the build."""
    return resource_root() / 'bin'


def activate_bundled_tools() -> Path | None:
    """Prepend the bundled bin directory so ffmpeg/ffprobe resolve by name.

    The application calls FFmpeg as plain ``ffmpeg``/``ffprobe`` commands, so the
    packaged binaries only need to be earlier on ``PATH`` than anything else.
    """
    directory = bundled_bin()
    if not directory.is_dir():
        return None
    entries = [entry for entry in os.environ.get('PATH', '').split(os.pathsep) if entry]
    resolved = str(directory)
    if resolved not in entries:
        for probe in ('ffmpeg.exe', 'ffmpeg'):
            if (directory / probe).is_file():
                os.environ['PATH'] = os.pathsep.join([resolved] + entries)
                break
    return directory
