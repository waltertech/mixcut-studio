"""Local media discovery.  This module intentionally has no web/server dependency."""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from pathlib import Path
from typing import Any
from .naming import tag_music_style

# An extension only decides whether a file is worth probing. FFprobe still validates
# the streams and duration, so a renamed document cannot enter the library.
VIDEO_EXTENSIONS = frozenset({'.mp4', '.m4v', '.mov', '.ts', '.mts', '.m2ts', '.mkv',
                              '.avi', '.webm', '.flv', '.mpeg', '.mpg', '.wmv', '.3gp'})
AUDIO_EXTENSIONS = frozenset({'.mp3', '.m4a', '.aac', '.flac', '.wav', '.ogg', '.oga',
                              '.opus', '.wma', '.aif', '.aiff', '.aifc', '.ape', '.caf',
                              '.ac3', '.amr', '.mka', '.mp4', '.mov', '.mkv', '.webm',
                              '.wv', '.tta', '.mpc', '.dsf', '.dff', '.3gp'})

def _decoded_audio_duration(path: Path) -> float:
    """Count decoded PCM samples so MP3 padding never accumulates between songs."""
    process = subprocess.Popen(
        ['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(path),
         '-map', '0:a:0', '-ac', '1', '-ar', '48000', '-f', 's16le', 'pipe:1'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    samples = 0
    for block in iter(lambda: process.stdout.read(1024 * 1024), b''):
        samples += len(block)
    process.stdout.close()
    error = process.stderr.read().decode('utf-8', errors='replace')
    process.stderr.close()
    if process.wait() or not samples:
        raise ValueError('音乐无法完整解码：' + error[-500:])
    return samples / (48000 * 2)


def _run_probe(path: Path) -> dict[str, Any]:
    command = ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "ffprobe 无法读取媒体")
    return json.loads(result.stdout)


def _fraction(value: str | None) -> float | None:
    try:
        top, bottom = (value or "0/0").split("/", 1)
        return float(top) / float(bottom) if float(bottom) else None
    except (ValueError, ZeroDivisionError):
        return None


def _media_mime(info: dict[str, Any], kind: str) -> str:
    formats = set(str(info.get('format', {}).get('format_name', '')).split(','))
    if 'mp3' in formats:
        return 'audio/mpeg'
    if formats & {'mov', 'mp4', 'm4a', '3gp', '3g2', 'mj2'}:
        return 'video/mp4' if kind == 'video' else 'audio/mp4'
    if 'webm' in formats:
        return 'video/webm' if kind == 'video' else 'audio/webm'
    if 'matroska' in formats:
        return 'video/x-matroska' if kind == 'video' else 'audio/x-matroska'
    if 'mpegts' in formats:
        return 'video/mp2t'
    if 'wav' in formats:
        return 'audio/wav'
    if 'flac' in formats:
        return 'audio/flac'
    if 'ogg' in formats:
        return 'audio/ogg'
    return 'application/octet-stream'


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _asset(path: Path, kind: str, *, quick_music=False) -> dict[str, Any]:
    info = _run_probe(path)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if kind == "video" and video is None:
        raise ValueError("没有视频流")
    if kind == "music" and audio is None:
        raise ValueError("没有音频流")
    raw_duration = info.get("format", {}).get("duration")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        # Stream duration is less ideal but still better than silently accepting it.
        try:
            duration = float((audio or video or {}).get("duration"))
        except (TypeError, ValueError):
            raise ValueError("媒体时长无效") from None
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("媒体时长无效")
    if kind == 'music' and not quick_music:
        duration = _decoded_audio_duration(path)
    stat = path.stat()
    fingerprint = _sha256(path)
    return {
        "id": fingerprint,
        "path": str(path.resolve()),
        "name": path.name,
        "duration": duration,
        "width": int(video.get("width", 0)) if video else 0,
        "height": int(video.get("height", 0)) if video else 0,
        "fps": _fraction(video.get("avg_frame_rate")) if video else None,
        "has_audio": audio is not None,
        "mime_type": _media_mime(info, kind),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "index_version": 2,
        **({"duration_precise": not quick_music} if kind == 'music' else {}),
    }


def _known_nonmedia(path: Path) -> bool:
    """Skip only unmistakable non-media signatures; unknown content gets probed."""
    with path.open('rb') as source:
        header = source.read(16)
    return (not header or header.startswith((b'\x00\x05\x16\x07', b'%PDF-', b'PK\x03\x04', b'\x89PNG\r\n\x1a\n',
                                             b'\xff\xd8\xff', b'GIF87a', b'GIF89a',
                                             b'SQLite format 3\x00', b'{\\rtf'))
            or (header.startswith(b'RIFF') and header[8:12] == b'WEBP'))


def _content_priority(path: Path) -> int:
    """Analyze recognizable media before unknown files, without rejecting either."""
    try:
        with path.open('rb') as source:
            header = source.read(16)
    except OSError:
        return 1
    if (header.startswith((b'ID3', b'RIFF', b'OggS', b'fLaC', b'\x1a\x45\xdf\xa3'))
            or header[4:8] == b'ftyp'):
        return 0
    return 1


def _cached_asset(path: Path, kind: str, cache: dict[str, Any], identities: dict[tuple, dict],
                  *, quick_music=False) -> dict[str, Any] | None:
    key = str(path.resolve())
    record_key = kind + ':' + key
    stat = path.stat()
    legacy = cache.get(key)
    old = cache.get(record_key) or (legacy if legacy and legacy.get('kind') in (None, kind) else None)
    if old and old.get('asset', {}).get('index_version') == 2 and old.get("size") == stat.st_size and old.get("mtime_ns") == stat.st_mtime_ns:
        old.update(dev=stat.st_dev, ino=stat.st_ino, kind=kind)
        identities[(kind, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)] = old
        if not old['asset'].get('mime_type'):
            old['asset'] = dict(old['asset'], mime_type=_media_mime(_run_probe(path), kind))
        if kind == 'music' and not quick_music and old['asset'].get('duration_precise') is False:
            old['asset'] = dict(old['asset'], duration=_decoded_audio_duration(path), duration_precise=True)
        cache[record_key] = old
        if old is legacy:
            cache.pop(key, None)
        return old["asset"]
    identity = (kind, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    known = identities.get(identity)
    if known and known.get('asset', {}).get('index_version') == 2:
        asset = dict(known['asset'], path=key, name=path.name)
        if not asset.get('mime_type'):
            asset['mime_type'] = _media_mime(_run_probe(path), kind)
        if kind == 'music' and not quick_music and asset.get('duration_precise') is False:
            asset.update(duration=_decoded_audio_duration(path), duration_precise=True)
        cache[record_key] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                             "dev": stat.st_dev, "ino": stat.st_ino, "kind": kind, "asset": asset}
        return asset
    if _known_nonmedia(path):
        return None
    asset = _asset(path, kind, quick_music=quick_music)
    cache[record_key] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                         "dev": stat.st_dev, "ino": stat.st_ino, "kind": kind, "asset": asset}
    identities[identity] = cache[record_key]
    return asset


def _files(folder: str | os.PathLike[str], errors: list[dict[str, str]], exclude_dirs=(),
           *, kind: str | None = None, stats: dict[str, int] | None = None) -> list[Path]:
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        errors.append({"path": str(root), "error": "目录不存在或不可访问"})
        return []
    paths = []
    stack = [root]
    extensions = VIDEO_EXTENSIONS if kind == 'video' else AUDIO_EXTENSIONS
    while stack:
        directory = stack.pop()
        if any(directory.is_relative_to(excluded) for excluded in exclude_dirs):
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(path)
                        elif entry.is_file():
                            if stats is not None:
                                stats['files'] += 1
                            if kind and path.suffix.lower() not in extensions:
                                if stats is not None:
                                    stats['skipped_extension'] += 1
                                continue
                            if any(path.resolve().is_relative_to(excluded) for excluded in exclude_dirs):
                                continue
                            paths.append(path)
                    except OSError as exc:
                        errors.append({"path": str(path), "error": str(exc)})
        except OSError as exc:
            errors.append({"path": str(directory), "error": str(exc)})
    return sorted(paths)


def _scan_paths(paths: list[Path], kind: str, root: Path, cache: dict[str, Any],
                identities: dict[tuple, dict], errors: list[dict[str, str]], progress=None,
                quick_music=False, stats: dict[str, int] | None = None) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Existing entries become usable immediately; new files can take much longer to decode.
    def cached_first(path):
        try:
            key = str(path.resolve())
            legacy = cache.get(key)
            record = cache.get(kind + ':' + key) or (legacy if legacy and legacy.get('kind') in (None, kind) else None)
            stat = path.stat()
            return (0, 0) if (record and record.get('size') == stat.st_size and
                              record.get('mtime_ns') == stat.st_mtime_ns) else (1, _content_priority(path))
        except OSError:
            return (1, 1)
    paths.sort(key=cached_first)
    for path in paths:
        asset = None
        before_errors = len(errors)
        try:
            asset = _cached_asset(path, kind, cache, identities, quick_music=quick_music)
            if asset and kind == 'music':
                asset = tag_music_style(asset, root)
            if asset and asset["id"] not in seen:  # content, not filename, defines a source asset
                seen.add(asset["id"])
                found.append(asset)
            else:
                if stats is not None:
                    stats['duplicates' if asset else 'skipped_content'] += 1
                asset = None
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            errors.append({"path": str(path), "error": str(exc)})
        if stats is not None and len(errors) > before_errors:
            stats['failed'] += 1
        if progress:
            progress({'phase': 'asset', 'kind': kind, 'asset': asset, 'path': str(path)})
    return found


def scan(video_dir: str, music_dir: str, cache_dir: str | None = None, exclude_dirs=(), *,
         kinds=('video', 'music'), previous=None, progress=None, quick_music=False) -> dict[str, Any]:
    """Recursively discover supported media and content-deduplicate each collection."""
    cache_path = Path(cache_dir or ".mixcut-cache").expanduser() / "media-index.json"
    try:
        cache = json.loads(cache_path.read_text("utf-8")) if cache_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        cache = {}
    errors: list[dict[str, str]] = []
    excluded = [Path(path).resolve() for path in exclude_dirs]
    identities = {(old.get('kind'), old.get('dev'), old.get('ino'), old.get('size'), old.get('mtime_ns')): old
                  for old in cache.values() if old.get('kind') and old.get('dev') is not None}
    previous = previous or {}
    paths = {}
    summary: dict[str, dict[str, int]] = {}
    roots = {'video': Path(video_dir).expanduser().resolve(), 'music': Path(music_dir).expanduser().resolve()}
    for kind in kinds:
        summary[kind] = {'files': 0, 'skipped_extension': 0, 'candidates': 0,
                         'skipped_content': 0, 'duplicates': 0, 'failed': 0, 'accepted': 0}
        paths[kind] = _files(roots[kind], errors, excluded, kind=kind, stats=summary[kind])
        summary[kind]['candidates'] = len(paths[kind])
    if progress:
        progress({'phase': 'inventory', 'totals': {kind: len(value) for kind, value in paths.items()}})
    videos = (_scan_paths(paths['video'], 'video', roots['video'], cache, identities, errors, progress,
                          stats=summary['video'])
              if 'video' in paths else list(previous.get('videos', [])))
    music = (_scan_paths(paths['music'], 'music', roots['music'], cache, identities, errors, progress,
                         quick_music, stats=summary['music'])
             if 'music' in paths else list(previous.get('music', [])))
    if 'video' in summary:
        summary['video']['accepted'] = len(videos)
    if 'music' in summary:
        summary['music']['accepted'] = len(music)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
        temporary.replace(cache_path)
    except OSError as exc:
        errors.append({"path": str(cache_path), "error": f"无法写入缓存：{exc}"})
    return {"videos": videos, "music": music, "errors": errors, "summary": summary}


def precise_music(assets: list[dict[str, Any]], selected_ids: set[str], cache_dir: str) -> list[dict[str, Any]]:
    """Resolve approximate library durations before a plan uses selected songs."""
    cache_path = Path(cache_dir) / 'media-index.json'
    try:
        cache = json.loads(cache_path.read_text('utf-8'))
    except (OSError, json.JSONDecodeError):
        cache = {}
    updated = []
    changed = False
    for asset in assets:
        if asset['id'] not in selected_ids or asset.get('duration_precise') is not False:
            updated.append(asset)
            continue
        path = Path(asset['path'])
        try:
            stat = path.stat()
        except OSError:
            raise ValueError(f'歌曲不存在，请重新扫描：{path.name}')
        if stat.st_size != asset['size'] or stat.st_mtime_ns != asset['mtime_ns']:
            raise ValueError(f'歌曲已变化，请重新扫描：{path.name}')
        exact = dict(asset, duration=_decoded_audio_duration(path), duration_precise=True)
        updated.append(exact)
        changed = True
        key = str(path.resolve())
        record = cache.get('music:' + key) or cache.get(key)
        if record and record.get('size') == stat.st_size and record.get('mtime_ns') == stat.st_mtime_ns:
            record['asset'] = dict(record['asset'], duration=exact['duration'], duration_precise=True)
    if changed:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(cache, ensure_ascii=False), 'utf-8')
        temporary.replace(cache_path)
    return updated


def verify_asset(asset: dict[str, Any]) -> bool:
    """Raise when a planned source was replaced, moved, or modified."""
    try:
        path = Path(asset["path"])
        stat = path.stat()
        valid = (stat.st_size == asset["size"] and stat.st_mtime_ns == asset["mtime_ns"]
                 and _sha256(path) == asset["id"])
        if not valid:
            raise ValueError(f"素材已变化：{path.name}")
        return True
    except (KeyError, OSError):
        raise ValueError(f"素材不存在或无法读取：{asset.get('path', '未知路径')}")
