"""Local media discovery.  This module intentionally has no web/server dependency."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from .naming import tag_music_style

VIDEO_EXTENSIONS = {".ts", ".mts", ".m2ts", ".mp4", ".mov", ".mkv", ".avi", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}


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
    # Keep the pipe in binary mode.  A frozen windowed Windows process has no
    # console streams, and some hosts have returned ``None`` for text captures.
    # Explicit decoding gives the scanner one stable path on every platform.
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    stdout = (result.stdout or b"").decode("utf-8-sig", errors="replace")
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    if result.returncode:
        raise ValueError(stderr.strip() or "ffprobe 无法读取媒体")
    if not stdout.strip():
        raise ValueError("ffprobe 未返回媒体信息")
    try:
        return json.loads(stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("ffprobe 返回了无效的媒体信息") from exc


def _fraction(value: str | None) -> float | None:
    try:
        top, bottom = (value or "0/0").split("/", 1)
        return float(top) / float(bottom) if float(bottom) else None
    except (ValueError, ZeroDivisionError):
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _asset(path: Path, kind: str) -> dict[str, Any]:
    info = _run_probe(path)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if kind == "video" and video is None:
        raise ValueError("没有视频流")
    if kind == "music" and audio is None:
        raise ValueError("没有音频流")
    try:
        duration = float(info.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        # Stream duration is less ideal but still better than silently accepting it.
        duration = float((audio or video or {}).get("duration"))
    if duration <= 0:
        raise ValueError("媒体时长无效")
    if kind == 'music':
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
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "index_version": 2,
    }


def _cached_asset(path: Path, kind: str, cache: dict[str, Any]) -> dict[str, Any]:
    key = str(path.resolve())
    stat = path.stat()
    old = cache.get(key)
    if old and old.get('asset', {}).get('index_version') == 2 and old.get("size") == stat.st_size and old.get("mtime_ns") == stat.st_mtime_ns:
        return old["asset"]
    asset = _asset(path, kind)
    cache[key] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "asset": asset}
    return asset


def _scan_folder(folder: str | os.PathLike[str], kind: str, cache: dict[str, Any], errors: list[dict[str, str]], exclude_dirs=()) -> list[dict[str, Any]]:
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        errors.append({"path": str(root), "error": "目录不存在或不可访问"})
        return []
    extensions = VIDEO_EXTENSIONS if kind == "video" else AUDIO_EXTENSIONS
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(path.resolve().is_relative_to(excluded) for excluded in exclude_dirs):
            continue
        if (any(part.startswith(".") for part in relative.parts) or not path.is_file()
                or path.suffix.lower() not in extensions):
            continue
        try:
            asset = _cached_asset(path, kind, cache)
            if kind == 'music':
                asset = tag_music_style(asset, root)
            if asset["id"] not in seen:  # content, not filename, defines a source asset
                seen.add(asset["id"])
                found.append(asset)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append({"path": str(path), "error": str(exc)})
    return found


def scan(video_dir: str, music_dir: str, cache_dir: str | None = None, exclude_dirs=()) -> dict[str, Any]:
    """Recursively discover supported media and content-deduplicate each collection."""
    cache_path = Path(cache_dir or ".mixcut-cache").expanduser() / "media-index.json"
    try:
        cache = json.loads(cache_path.read_text("utf-8")) if cache_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        cache = {}
    errors: list[dict[str, str]] = []
    excluded = [Path(path).resolve() for path in exclude_dirs]
    videos = _scan_folder(video_dir, "video", cache, errors, excluded)
    music = _scan_folder(music_dir, "music", cache, errors, excluded)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), "utf-8")
    except OSError as exc:
        errors.append({"path": str(cache_path), "error": f"无法写入缓存：{exc}"})
    return {"videos": videos, "music": music, "errors": errors}


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
