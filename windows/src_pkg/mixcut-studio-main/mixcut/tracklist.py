"""Build ordered title lists from the rendered audio timeline, without media I/O."""
from pathlib import Path
import math


def played_music(item, final_duration):
    limit = float(final_duration)
    if not math.isfinite(limit) or limit <= 0:
        return []
    result, start = [], 0.
    fades = item.get('nonstop', {}).get('crossfades', [])
    for index, song in enumerate(item.get('music', [])):
        edges = song.get('nonstop_edges') if item.get('nonstop') else None
        length = float(edges.get('effective_duration', edges.get('end', song.get('duration', 0)) - edges.get('start', 0)) if edges else song.get('duration', 0))
        if not math.isfinite(length) or length <= 0:
            continue
        if index and index - 1 < len(fades):
            start -= float(fades[index - 1])
        if start >= limit - 1e-6:
            break
        path = song.get('path') or song.get('name') or song.get('id', '')
        title = ' '.join(Path(path).stem.splitlines())
        result.append({'id': song.get('id'), 'path': song.get('path', ''),
                       'title': title, 'start': max(0., start),
                       'duration': min(length, limit - start)})
        start += length
    return result


def for_item(item):
    result = item.get('result', {})
    if 'played_music' in result:
        return result['played_music']
    if 'played_music' in item:
        return item['played_music']
    # Legacy tasks can use saved metadata even after original audio paths move.
    return played_music(item, result.get('duration', item.get('duration', 0)))


def sidecar_content(item):
    from .naming import music_folders
    folders = item.get('music_source_folders') or music_folders(item.get('music', []))
    content = '\n'.join(folders) + '\n' if folders else ''
    if item.get('include_track_titles', False):
        tracks = for_item(item)
        if tracks:
            content += ('\n' if content else '') + '曲目列表\n'
            content += '\n'.join(f"{number}. {track['title']}" for number, track in enumerate(tracks, 1)) + '\n'
    return content
