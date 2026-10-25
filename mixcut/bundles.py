"""Journal whole-directory renames so approval never copies large video files."""
import os
import re
from pathlib import Path
from datetime import datetime


def finish_relocation(record, persist):
    move = record.get('bundle_relocation')
    if not move:
        return record
    old, new = Path(move['old']), Path(move['new'])
    filename = Path(record['output_path']).name
    if old.is_symlink() or new.is_symlink():
        raise ValueError('成品目录是链接，停止审核改名')
    if old.exists():
        if new.exists() and any(new.iterdir()):
            raise ValueError('审核目标目录已有内容，原成品保留')
        os.rename(old, new)  # Destination is our reserved empty directory, same disk.
    if not (new / filename).is_file():
        raise ValueError('审核改名后的成片不存在，请检查目录')
    stamp = record.get('output_stamp')
    if stamp:
        stat = (new / filename).stat()
        if [stat.st_size, stat.st_mtime_ns] != stamp[1:3]:
            raise ValueError('成片在审核改名期间发生变化，停止审核')
        record['output_stamp'] = [str((new / filename).resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    record.update(output_bundle=str(new), output_path=str(new / filename))
    record.pop('bundle_relocation', None)
    persist(record)
    return record


def date_for_approval(record, root, reserve, persist):
    finish_relocation(record, persist)
    if not record.get('output_bundle'):
        return record  # Legacy shared directories must never be renamed as one task.
    old = Path(record['output_bundle'])
    root = Path(root).resolve()
    if old.is_symlink() or old.resolve().parent.parent != root or Path(record['output_path']).parent != old:
        raise ValueError('成品目录位置异常，停止审核改名')
    today = datetime.now().astimezone()
    day = root / today.strftime('%Y-%m-%d')
    if old.parent == day and re.fullmatch(today.strftime('%y%m%d') + r'-\d{3,}', old.name):
        return record
    new, _ = reserve(day, record)
    record['bundle_relocation'] = {'old': str(old), 'new': str(new)}
    persist(record)  # Durable intent before the filesystem mutation.
    return finish_relocation(record, persist)
