"""Journal whole-directory renames so approval never copies large video files."""
import os
import re
from .fsutil import publish
from pathlib import Path
from datetime import datetime


def finish_relocation(record, persist):
    move = record.get('bundle_relocation')
    if not move:
        return record
    old, new = Path(move['old']), Path(move['new'])
    filename = move.get('filename_old', Path(record['output_path']).name)
    final_name = move.get('filename_new', filename)
    if old.is_symlink() or new.is_symlink():
        raise ValueError('成品目录是链接，停止审核改名')
    if old.exists():
        if new.exists() and any(new.iterdir()):
            raise ValueError('审核目标目录已有内容，原成品保留')
        os.rename(old, new)  # Destination is our reserved empty directory, same disk.
    if final_name != filename:
        for old_path, new_path in ((new / filename, new / final_name),
                                   ((new / filename).with_suffix('.txt'), (new / final_name).with_suffix('.txt'))):
            if old_path.exists():
                if new_path.exists():
                    if not os.path.samefile(old_path, new_path):
                        raise ValueError('审核目标文件名已有内容，文件已保留')
                    old_path.unlink()
                else:
                    publish(old_path, new_path)
    if not (new / final_name).is_file():
        raise ValueError('审核改名后的成片不存在，请检查目录')
    stamp = record.get('output_stamp')
    if stamp:
        stat = (new / final_name).stat()
        if [stat.st_size, stat.st_mtime_ns] != stamp[1:3]:
            raise ValueError('成片在审核改名期间发生变化，停止审核')
        record['output_stamp'] = [str((new / final_name).resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    record.update(output_bundle=str(new), output_path=str(new / final_name))
    if final_name != filename:
        record.update(output_name=final_name, output_name_base=final_name)
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
    move = {'old': str(old), 'new': str(new)}
    if record.get('output_naming') == 'bundle-library-v1':
        from .naming import item_filename
        move.update(filename_old=Path(record['output_path']).name,
                    filename_new=item_filename({**record, 'output_bundle':str(new)}))
    record['bundle_relocation'] = move
    persist(record)  # Durable intent before the filesystem mutation.
    return finish_relocation(record, persist)
