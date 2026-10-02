"""Watch actual FFmpeg output; stop stalled workers without blocking the queue."""
import json
from pathlib import Path
import queue
import subprocess
import threading
import time

STALL_SECONDS = 180
POLL_SECONDS = 1


class RenderStalled(RuntimeError):
    pass


def run(command, log, output=None, callback=None, stage='rendering', duration=1):
    command = list(command)
    if '-progress' not in command:
        command[-1:-1] = ['-progress', 'pipe:1', '-nostats']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log, text=True)
    messages = queue.Queue()
    def read():
        try:
            for line in process.stdout:
                messages.put(line)
        finally:
            messages.put(None)
    reader = threading.Thread(target=read, daemon=True, name='mixcut-progress-reader')
    reader.start()
    stopped = threading.Event()
    file_observation = [None]
    def observe_file():
        # Removable-drive stat can block; it must not block the stall timer.
        while not stopped.is_set():
            try:
                stat = Path(output).stat()
                file_observation[0] = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                pass
            stopped.wait(POLL_SECONDS)
    observer = threading.Thread(target=observe_file, daemon=True, name='mixcut-output-observer') if output else None
    if observer:
        observer.start()
    last_change = time.monotonic()
    media_time = -1
    file_state = None
    last_heartbeat = 0
    progress = .96 if stage == 'validating' else 0
    try:
        while True:
            try:
                line = messages.get(timeout=POLL_SECONDS)
            except queue.Empty:
                line = ''
            if line:
                key, _, value = line.strip().partition('=')
                if key == 'out_time_us':
                    try:
                        current = int(value)
                    except ValueError:
                        current = -1
                    if current > media_time:
                        media_time = current
                        last_change = time.monotonic()
                        if stage == 'rendering':
                            progress = min(.95, max(0, current / 1e6 / duration * .95))
                        if callback:
                            callback({'stage': stage, 'progress': progress})
            current_file = file_observation[0]
            if current_file is not None and current_file != file_state:
                file_state = current_file
                last_change = time.monotonic()
            if callback and time.monotonic() - last_heartbeat >= POLL_SECONDS:
                callback({'stage': stage, 'progress': progress, 'heartbeat': True})
                last_heartbeat = time.monotonic()
            if process.poll() is not None and (line is None or messages.empty()):
                return process.returncode
            idle = time.monotonic() - last_change
            if idle >= STALL_SECONDS:
                evidence = {'event': 'render_stalled', 'stage': stage, 'idle_seconds': round(idle, 2),
                            'media_time_us': media_time, 'file_state': file_state,
                            'pid': process.pid, 'command': command, 'time': time.time()}
                log.write('\n' + json.dumps(evidence, ensure_ascii=False) + '\n')
                log.flush()
                raise RenderStalled('执行被卡住暂停：连续3分钟没有实际进展，后续任务继续；可手动重试该任务。')
    finally:
        stopped.set()
        if observer:
            observer.join(timeout=.1)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        reader.join(timeout=2)
        process.stdout.close()
