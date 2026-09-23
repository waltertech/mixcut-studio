"""Managed, static image stickers and FFmpeg overlay filter construction."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_CODECS = {"png", "mjpeg", "webp", "gif"}


def _probe(path: Path, count_frames: bool = False) -> dict[str, Any]:
    command = ["ffprobe", "-v", "error"] + (["-count_frames"] if count_frames else []) + ["-show_streams", "-show_format", "-of", "json", str(path)]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    stdout = (result.stdout or b"").decode("utf-8-sig", errors="replace")
    if result.returncode:
        raise ValueError("贴纸文件无法读取")
    if not stdout.strip():
        raise ValueError("贴纸文件未返回媒体信息")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("贴纸文件媒体信息无效") from exc


def import_image(data: bytes, name: str, root: Path) -> dict[str, Any]:
    if not data or len(data) > 12 * 1024 * 1024:
        raise ValueError("贴纸文件必须大于 0 且不超过 12MB")
    is_gif = data.startswith((b"GIF87a", b"GIF89a"))
    if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")
            or data.startswith(b"RIFF") and data[8:12] == b"WEBP" or is_gif):
        raise ValueError("仅支持 PNG、JPG、WEBP 或 GIF 位图文件")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        source = Path(temporary) / "upload"
        source.write_bytes(data)
        info = _probe(source)
        video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
        if not video or video.get("codec_name") not in _CODECS:
            raise ValueError("仅支持 PNG、JPG、WEBP 或 GIF 贴纸")
        width, height = int(video.get("width", 0)), int(video.get("height", 0))
        if not width or not height or width > 4096 or height > 4096 or width * height > 20_000_000:
            raise ValueError("贴纸尺寸不得超过 4096px 或 2000 万像素")
        counted = _probe(source, count_frames=True)
        frame_stream = next(s for s in counted["streams"] if s.get("codec_type") == "video")
        frames = frame_stream.get("nb_read_frames", video.get("nb_frames"))
        try:
            frame_count = int(frames)
        except (TypeError, ValueError):
            frame_count = 1
        animated = is_gif and frame_count > 1
        if not is_gif and frame_count > 1:
            raise ValueError("不支持动态贴纸")
        if animated:
            payload, suffix = data, ".gif"
        else:
            encoded = Path(temporary) / "canonical.png"
            result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(source),
                                     "-frames:v", "1", "-vf", "format=rgba", str(encoded)], capture_output=True, text=True)
            if result.returncode:
                raise ValueError("贴纸转换失败：" + result.stderr[-300:])
            payload, suffix = encoded.read_bytes(), ".png"
    digest = hashlib.sha256(payload).hexdigest()
    target = root / f"{digest}{suffix}"
    if not target.exists():
        target.write_bytes(payload)
    canonical = _probe(target)
    stream = next(s for s in canonical["streams"] if s.get("codec_type") == "video")
    return {"id": digest, "sha256": digest, "path": str(target), "name": name,
            "width_px": int(stream["width"]), "height_px": int(stream["height"]),
            "animated": animated, "frame_count": frame_count}


def resolve_layers(layers: list[dict[str, Any]] | None, assets: Any) -> list[dict[str, Any]]:
    if not layers:
        return []
    if len(layers) > 8:
        raise ValueError("每条视频最多添加 8 个贴纸")
    source = assets if isinstance(assets, dict) else {asset["id"]: asset for asset in assets}
    result = []
    for raw in layers:
        asset = source.get(raw.get("sticker_id"))
        if not asset:
            raise ValueError("贴纸不存在或不属于当前项目")
        values = {key: raw.get(key, default) for key, default in
                  (("x", .05), ("y", .05), ("width", .2), ("opacity", 1), ("start", 0), ("end", None))}
        try:
            x, y, width, opacity, start = (float(values[key]) for key in ("x", "y", "width", "opacity", "start"))
            end = None if values["end"] is None else float(values["end"])
        except (TypeError, ValueError):
            raise ValueError("贴纸位置、大小、透明度和时间必须是数字")
        if not all(math.isfinite(value) for value in (x, y, width, opacity, start)) or (end is not None and not math.isfinite(end)):
            raise ValueError("贴纸参数必须是有限数字")
        if not (0 <= x < 1 and 0 <= y < 1 and .01 <= width <= 1 and 0 <= opacity <= 1 and start >= 0):
            raise ValueError("贴纸位置、宽度或透明度超出范围")
        if end is not None and end <= start:
            raise ValueError("贴纸结束时间必须晚于开始时间")
        path = Path(asset["path"]).resolve()
        if path.suffix.lower() not in {".png", ".gif"} or not path.is_file():
            raise ValueError("贴纸受管文件不存在")
        result.append({"sticker_id": asset["id"], "path": str(path), "sha256": asset["sha256"], "name": asset["name"],
                       "width_px": int(asset["width_px"]), "height_px": int(asset["height_px"]),
                       "animated": bool(asset.get("animated", path.suffix.lower() == ".gif")),
                       **values, "x": x, "y": y, "width": width, "opacity": opacity, "start": start, "end": end})
    return result


def add_overlay_filters(inputs: list[str], filters: list[str], layers: list[dict[str, Any]], input_index: int,
                        base_label: str, width: int, height: int, fps: int, duration: float) -> str:
    label = base_label
    for offset, layer in enumerate(layers):
        if hashlib.sha256(Path(layer["path"]).read_bytes()).hexdigest() != layer["sha256"]:
            raise ValueError("贴纸文件已变化，请重新导入")
        aspect = float(layer["height_px"]) / float(layer["width_px"])
        requested = float(layer["width"]) * width
        actual_width = max(1, min(requested, width - float(layer["x"]) * width,
                                  (height - float(layer["y"]) * height) / aspect))
        actual_height = max(1, actual_width * aspect)
        x, y = float(layer["x"]) * width, float(layer["y"]) * height
        end = duration if layer["end"] is None else min(duration, float(layer["end"]))
        if layer.get("animated"):
            inputs += ["-stream_loop", "-1", "-ignore_loop", "1", "-i", layer["path"]]
        else:
            inputs += ["-loop", "1", "-framerate", str(fps), "-i", layer["path"]]
        image, output = f"st{offset}", f"ov{offset}"
        filters.append(f"[{input_index + offset}:v]setpts=PTS-STARTPTS,format=rgba,scale={actual_width:.4f}:{actual_height:.4f},colorchannelmixer=aa={float(layer['opacity']):.6f}[{image}]")
        filters.append(f"[{label}][{image}]overlay={x:.4f}:{y:.4f}:eof_action=pass:enable='between(t,{float(layer['start']):.6f},{end:.6f})'[{output}]")
        label = output
    return label
