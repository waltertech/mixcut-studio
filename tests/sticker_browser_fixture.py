"""Isolated browser acceptance fixture; never adds demo stickers to the user's library."""
from pathlib import Path
import subprocess
import tempfile
import time

from mixcut.server import Application, Handler, ThreadingHTTPServer


def main():
    root = Path(tempfile.mkdtemp(prefix='mixcut-sticker-browser-')).resolve()
    videos = root / 'videos'; music = root / 'music'; outputs = root / 'exports'
    for folder in (videos, music, outputs):
        folder.mkdir()
    video = videos / 'test.mp4'
    song = music / 'tone.mp3'
    sticker = root / 'test-sticker.png'
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=navy:s=640x360:r=30:d=3', '-f', 'lavfi', '-i',
                    'sine=f=440:r=48000:d=3', '-c:v', 'libx264', '-c:a', 'aac', str(video)], check=True)
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=f=440:r=48000:d=3', '-c:a', 'libmp3lame', str(song)], check=True)
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=orange:s=160x80:d=0.04,format=rgba', '-frames:v', '1', str(sticker)], check=True)
    app = Application(root / 'state')
    app.scan({'video_dir': str(videos), 'music_dir': str(music)})
    app.preferences({'output_dir': str(outputs), 'review_dir': str(root / 'approved')})
    stat = video.stat()
    batch = {'id': 'aabb0011', 'created_at': time.time(), 'updated_at': time.time(), 'status': 'completed',
             'config': {'output_dir': str(outputs), 'width': 640, 'height': 360, 'fps': 30},
             'assets': [], 'stats': {}, 'warnings': [], 'items': [{'id': '1', 'index': 1, 'duration': 3,
             'status': 'success', 'progress': 1, 'segments': [], 'music': [], 'output_path': str(video),
             'result': {'output_size': stat.st_size, 'output_mtime_ns': stat.st_mtime_ns}}]}
    app.store.put('batch:aabb0011', batch)
    server = ThreadingHTTPServer(('127.0.0.1', 8891), Handler)
    server.app = app
    app.start_worker()
    print(f'FIXTURE_ROOT={root}\nSTICKER_FILE={sticker}\nURL=http://127.0.0.1:8891/#stickers', flush=True)
    try:
        server.serve_forever()
    finally:
        app.closing.set()
        server.server_close()


if __name__ == '__main__':
    main()
