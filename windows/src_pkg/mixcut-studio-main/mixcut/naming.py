"""Music-folder labels and portable export filenames; no audio-style guessing."""
import hashlib
import math
from pathlib import Path
import re

KNOWN_STYLES = ('Hindi DJ Remix', 'Hindi POP Songs', 'Old Hindi Songs')
_KNOWN = {name.casefold(): name for name in KNOWN_STYLES}


def infer_music_style(path, root=None):
    parent = Path(path).parent
    boundary = Path(root) if root else None
    folders = []
    for folder in (parent, *parent.parents):
        if folder.name:
            folders.append(folder.name)
        if boundary is not None and folder == boundary:
            break
    for name in folders:
        normalized = ' '.join(name.split())
        if normalized.casefold() in _KNOWN:
            return _KNOWN[normalized.casefold()]
    if boundary:
        try:
            relative = parent.relative_to(boundary)
            return relative.parts[0] if relative.parts else boundary.name
        except ValueError:
            pass
    return parent.name


def tag_music_style(asset, root=None):
    label = infer_music_style(asset.get('path', ''), root)
    return {**asset, 'music_style': label or asset.get('music_style', '')}


def music_styles(songs, root=None):
    labels = []
    for song in songs:
        label = song.get('music_style') or infer_music_style(song.get('path', ''), root)
        if label and label.casefold() not in {known.casefold() for known in labels}:
            labels.append(label)
    return labels


def music_folders(songs):
    """Return each used song's containing folder, preserving playback order."""
    folders = []
    for song in songs:
        if not song.get('path'):
            continue
        path = str(Path(song['path']).expanduser().resolve().parent)
        if path not in folders:
            folders.append(path)
    return folders


def export_filename(styles, index):
    prefix = ' + '.join(styles)
    prefix = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', prefix).strip(' .')
    if len(prefix.encode('utf-8')) > 180:
        prefix = prefix.encode('utf-8')[:165].decode('utf-8', errors='ignore').rstrip(' .') + '-' + hashlib.sha256(prefix.encode()).hexdigest()[:8]
    return f'{prefix}_{int(index):03d}.mp4' if prefix else f'{int(index):03d}.mp4'


def review_folder_name(day, number):
    """Portable daily task bundle name, independent of root labels or music."""
    return f'{day}-{int(number):03d}'


def youtube_id(folder):
    """Recognize standard IDs in Set directory names, never in ancestor labels."""
    name = Path(folder).name
    for pattern in (r'首[_ ]([A-Za-z0-9_-]{11})(?=$|[_\s])',
                    r'\[([A-Za-z0-9_-]{11})\]',
                    r'^([A-Za-z0-9_-]{11})$',
                    r'_([A-Za-z0-9_-]{11})$'):
        match = re.search(pattern, name)
        if match:
            return match[1]
    return None


def duration_filename(item, duration=None):
    """Name exports by duration and distinct leaf-folder YouTube IDs."""
    seconds = float(item.get('duration', 0) if duration is None else duration)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError('成片命名时长无效')
    seconds = int(seconds + .5)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    folders = ([item['music_folder']] if item.get('music_folder') else
               music_folders(item.get('music', [])) or item.get('music_source_folders', []))
    ids = list(dict.fromkeys(value for folder in folders if (value := youtube_id(folder))))
    label = '+'.join(ids) if ids else '未识别ID'
    if len(label) > 144:
        label = label[:131] + '-' + hashlib.sha256(label.encode()).hexdigest()[:8]
    return f'{hours}小时{minutes}分{seconds}秒-{label}.mp4'


def item_filename(item):
    if item.get('output_naming') == 'duration-youtube-v1':
        return duration_filename(item)
    return export_filename(item.get('music_styles', []), item['index'])
