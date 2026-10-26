"""Durable per-task folder successes plus inexpensive live reservations."""
from pathlib import Path
import time


def canonical(path):
    return str(Path(path).expanduser().resolve())


def initialize(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS music_folder_success (task_key TEXT PRIMARY KEY, folder TEXT NOT NULL, used_at REAL NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS music_folder_usage (folder TEXT PRIMARY KEY, completed INTEGER NOT NULL, last_used REAL NOT NULL)')


def record_successes(conn, batch, *, migration=False):
    if batch.get('sticker_origin'):
        return
    for item in batch.get('items', []):
        folder = item.get('music_folder')
        if not folder or item.get('status') != 'success' or item.get('folder_usage_recorded'):
            continue
        stamp = float(batch.get('updated_at') or batch.get('created_at') or time.time()) if migration else time.time()
        folder = canonical(folder)
        inserted = conn.execute('INSERT OR IGNORE INTO music_folder_success VALUES (?,?,?)',
                                (batch['id'] + ':' + str(item['id']), folder, stamp)).rowcount
        if inserted:
            conn.execute('INSERT INTO music_folder_usage VALUES (?,1,?) ON CONFLICT(folder) DO UPDATE SET completed=completed+1,last_used=MAX(last_used,excluded.last_used)', (folder, stamp))
        # Saved in the same transaction as the success and ledger event.
        item['folder_usage_recorded'] = True


def snapshot(conn, batches, exclude_task=None):
    result = {row[0]: {'completed': row[1], 'last_used': row[2], 'pending': 0}
              for row in conn.execute('SELECT folder,completed,last_used FROM music_folder_usage')}
    for batch in batches:
        if batch.get('sticker_origin'):
            continue
        for item in batch.get('items', []):
            folder = item.get('music_folder')
            if (not folder or item.get('dismissed') or item.get('cancel_requested')
                    or item.get('status') not in {'pending', 'running', 'validating', 'stalled_paused', 'recovery_paused'}
                    or (batch['id'], str(item['id'])) == exclude_task):
                continue
            value = result.setdefault(canonical(folder), {'completed': 0, 'last_used': 0, 'pending': 0})
            value['pending'] += 1
    return result
