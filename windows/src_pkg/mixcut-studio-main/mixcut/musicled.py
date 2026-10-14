"""Music-led recorded-video plans; legacy plans and music Sets are independent."""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path
import random
from collections import defaultdict

MAX_SEGMENTS = 4096


def active(config):
    return config.get('duration_basis') == 'music'


def choose_music(songs, config, rng, usage=None):
    from .foldermusic import opening_tracks
    songs = list({s['id']: s for s in songs}.values())
    if not songs:
        raise ValueError('没有可用歌曲')
    mode = config.get('music_mode', 'pool')
    if mode == 'folder':
        anchors = opening_tracks(songs)
        ids = {s['id'] for s in anchors}
        rest = [s for s in songs if s['id'] not in ids]
        rng.shuffle(rest)
        return anchors + rest
    if mode == 'fixed':
        rng.shuffle(songs)
        return songs
    if mode != 'pool':
        raise ValueError('音乐模式无效')
    lo = int(config.get('min_songs', 1))
    hi = int(config.get('max_songs', len(songs)))
    if lo < 1 or hi < lo or lo > len(songs):
        raise ValueError('歌曲数量设置无效，或可用歌曲少于最少首数')
    count = rng.randint(lo, min(hi, len(songs)))
    usage = usage or {}
    chosen = []
    while len(chosen) < count:
        level = min(usage.get(s['id'], 0) for s in songs)
        song = rng.choice([s for s in songs if usage.get(s['id'], 0) == level])
        chosen.append(song)
        songs.remove(song)
    rng.shuffle(chosen)
    return chosen


def fit_segments(segments, target):
    """Repeat the chosen visual sequence, cutting only the last occurrence."""
    target = float(target)
    if not segments or not math.isfinite(target) or target <= 0:
        raise ValueError('视频循环单元或音乐时长无效')
    source = [dict(s) for s in segments]
    if any(float(s['duration']) <= 0 for s in source):
        raise ValueError('视频片段时长无效')
    result, left, n = [], target, 0
    while left > 1e-7:
        if n >= MAX_SEGMENTS:
            raise ValueError('循环片段超过4096段，请使用更长的视频素材')
        seg = dict(source[n % len(source)])
        seg['duration'] = min(float(seg['duration']), left)
        result.append(seg)
        left -= seg['duration']
        n += 1
    return result


def video_segments(videos, target, config, rng, usage=None, occupied=None):
    from .planner import choose_video_segment
    usage = defaultdict(int, usage or {})
    occupied = defaultdict(list, {key:list(value) for key,value in (occupied or {}).items()})
    if not videos:
        raise ValueError('没有可用视频')
    if config.get('mode', 'single') == 'single':
        segment = choose_video_segment(videos, target, occupied, usage, rng,
                                       allow_overlap=True, min_start_gap=float(config.get('start_gap', 0)))
        if segment:
            return [segment]
        level = min(usage[v['id']] for v in videos)
        source = rng.choice([v for v in videos if usage[v['id']] == level])
        return fit_segments([{'asset_id': source['id'], 'path': source['path'],
                              'start': 0., 'duration': float(source['duration'])}], target)
    if config.get('mode') != 'multi':
        raise ValueError('视频组织方式无效')
    lo, hi = float(config.get('segment_min', 30)), float(config.get('segment_max', 120))
    if not math.isfinite(lo) or not math.isfinite(hi) or lo <= 0 or hi < lo:
        raise ValueError('单段最短/最长时长无效')
    if config.get('within_group'):
        groups = defaultdict(list)
        for v in videos:
            group = config.get('groups', {}).get(v['id'])
            if group: groups[group].append(v)
        eligible = [p for p in groups.values() if len(p) >= 2]
        if not eligible: raise ValueError('组内拼接需要至少两个同组视频')
        videos = rng.choice(eligible)
    elif len(videos) < 2:
        raise ValueError('多源拼接需要至少两个视频')
    result, remaining = [], float(target)
    while remaining > 1e-7:
        if len(result) >= MAX_SEGMENTS: raise ValueError('片段超过4096段，请增加单段时长')
        candidates = [v for v in videos if not result or v['id'] != result[-1]['asset_id']] or videos
        level = min(usage[v['id']] for v in candidates)
        source = rng.choice([v for v in candidates if usage[v['id']] == level])
        length = min(remaining, float(source['duration']), rng.uniform(lo, hi))
        seg = choose_video_segment([source], length, occupied, usage, rng, allow_overlap=True)
        result.append(seg)
        usage[source['id']] += 1
        occupied[source['id']].append((seg['start'], seg['start']+length))
        remaining -= length
    return result


def plan(videos, music, config, previous_items=None, allow_partial=False):
    from . import planner, foldermusic
    count = int(config.get('count', 1))
    threshold = float(config.get('min_source_duration_minutes', 0))
    if not math.isfinite(threshold) or threshold < 0 or not 1 <= count <= 500:
        raise ValueError('生成数量或源视频时长门槛无效')
    videos = planner._selected(videos, config.get('video_ids'), '视频')
    before = len(videos)
    videos = [v for v in videos if math.isfinite(float(v['duration'])) and float(v['duration']) > max(.1, threshold*60)]
    music = planner._selected(music, None if config.get('music_mode') == 'folder' else config.get('music_ids'), '音乐')
    music = [s for s in music if math.isfinite(float(s['duration'])) and float(s['duration']) > .1]
    if not videos or not music:
        if allow_partial:
            return {'items': [], 'stats': {'count':0,'requested_count':count,'remaining':count}, 'warnings':['没有符合条件的视频或音乐']}
        raise ValueError('没有符合源视频时长门槛的视频，或没有可用音乐')
    grouped = foldermusic.groups(music, config.get('music_root')) if config.get('music_mode') == 'folder' else {}
    if config.get('music_mode') == 'folder' and not grouped: raise ValueError('音乐根目录下没有有效音乐子文件夹')
    rng = random.Random(config.get('seed', 0))
    mu, vu, fu, occupied = defaultdict(int), defaultdict(int), defaultdict(int), defaultdict(list)
    for item in previous_items or []:
        for s in item['music']: mu[s['id']] += 1
        for s in item['segments']:
            vu[s['asset_id']] += 1
            occupied[s['asset_id']].append((s['start'],s['start']+s['duration']))
        if item.get('music_folder'): fu[item['music_folder']] += 1
    items, seen, warnings = [], {planner.video_plan_key(i['segments'], config.get('fps',30)) for i in previous_items or []}, []
    for index in range(count):
        folder = foldermusic.choose_folder(grouped, fu, rng) if grouped else None
        order = choose_music(grouped[folder] if folder else music, config, rng, mu)
        total = sum(float(s['duration']) for s in order)
        for attempt in range(24):
            segments = video_segments(videos, total, config, rng, vu, occupied)
            key = planner.video_plan_key(segments, config.get('fps',30))
            if key not in seen: break
        if key in seen and not warnings: warnings.append('当前素材不足以保证每条画面组合不同，部分作品会重复使用画面；音乐保持完整。')
        seen.add(key)
        item = {'id':planner._fingerprint((index,key,tuple(s['id'] for s in order))), 'index':index,
                'duration_basis':'music','duration':total,'music_total_duration':total,
                'segments':segments,'music':copy.deepcopy(order),'video_assets':list({v['id']:v for v in videos if any(s['asset_id']==v['id'] for s in segments)}.values()),
                'video_fingerprint':planner._fingerprint(key),'music_fingerprint':planner._fingerprint(tuple(s['id'] for s in order))}
        if folder:
            item.update(music_folder=folder,music_folder_song_count=len(order))
            fu[folder] += 1
        items.append(item)
        for song in order: mu[song['id']] += 1
        for seg in segments:
            vu[seg['asset_id']] += 1
            occupied[seg['asset_id']].append((seg['start'],seg['start']+seg['duration']))
    stats = planner.annotate_batch(items)
    if before > len(videos): warnings.append(f'源视频时长筛选已排除 {before-len(videos)} 条视频')
    return {'items':items,'stats':dict(stats,count=len(items),requested_count=count,remaining=0,
            unique_music_orders=len({i['music_fingerprint'] for i in items}),unique_video_plans=len(seen)), 'warnings':warnings}


def prepare_audio(item, config, work, callback):
    """Produce the actual soundtrack once during rendering, never during planning."""
    from . import media, renderer
    from .processwatch import run
    work = Path(work); work.mkdir(parents=True, exist_ok=True)
    songs = item['music']
    versions = []
    for s in songs:
        media.verify_asset(s)
        st = Path(s['path']).stat()
        versions.append([s['path'],st.st_size,st.st_mtime_ns,st.st_ctime_ns])
    signature = hashlib.sha256(json.dumps([versions,item.get('nonstop'),[s.get('nonstop_edges') for s in songs],config.get('music_volume',1)],sort_keys=True).encode()).hexdigest()
    output, receipt = work/'soundtrack.m4a', work/'soundtrack.json'
    try:
        saved = json.loads(receipt.read_text())
        st = output.stat()
        reusable = saved.get('signature') == signature and saved.get('stamp') == [st.st_size,st.st_mtime_ns]
    except (OSError,ValueError): reusable = False
    if not reusable:
        command = ['ffmpeg','-nostdin','-v','error','-xerror','-y','-threads','1']
        filters=[]
        for n,song in enumerate(songs):
            command += ['-i',song['path']]
            edges = song.get('nonstop_edges') if item.get('nonstop') else None
            trim = f"atrim=start={edges['start']:.8f}:end={edges['end']:.8f}," if edges else ''
            filters.append(f'[{n}:a:0]{trim}asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{n}]')
        label='a0'
        fades=(item.get('nonstop') or {}).get('crossfades',[])
        if fades:
            for n,fade in enumerate(fades,1):
                filters.append(f'[{label}][a{n}]acrossfade=d={fade:.8f}:c1=tri:c2=tri[j{n}]'); label=f'j{n}'
        elif len(songs)>1:
            filters.append(''.join(f'[a{n}]' for n in range(len(songs)))+f'concat=n={len(songs)}:v=0:a=1[join]');label='join'
        filters.append(f"[{label}]volume={config.get('music_volume',1)}[out]")
        partial=work/'soundtrack.partial.m4a'
        command += ['-filter_complex_threads','1','-filter_complex',';'.join(filters),'-map','[out]','-c:a','aac','-b:a','192k',str(partial)]
        def progress(value):
            if callback: callback(dict(value,stage='music_assembly'))
        try:
            with (work/'soundtrack.log').open('w+',encoding='utf-8') as log:
                code=run(command,log,partial,progress,duration=item['duration'])
                if code:
                    log.seek(0); text=log.read()
                    raise renderer.music_input_error(text,dict(item,_video_input_count=0)) or renderer.MusicInputError('音乐拼接失败：'+text[-1200:],[])
            for path, size, mtime, ctime in versions:
                st = Path(path).stat()
                if [st.st_size, st.st_mtime_ns, st.st_ctime_ns] != [size, mtime, ctime]:
                    raise renderer.MusicInputError('音乐文件在拼接期间发生变化，请重试', [s['id'] for s in songs if s['path'] == path])
            partial.replace(output)
            st=output.stat();receipt.write_text(json.dumps({'signature':signature,'stamp':[st.st_size,st.st_mtime_ns]}))
        finally: partial.unlink(missing_ok=True)
    actual=float(renderer._probe(output)['format']['duration'])
    if not math.isfinite(actual) or actual <= .1: raise ValueError('音乐拼接结果没有有效时长')
    prepared=copy.deepcopy(item)
    prepared.update(planned_duration=float(item.get('planned_duration',item['duration'])),duration=actual,
                    segments=fit_segments(item['segments'],actual),prepared_audio=str(output))
    return prepared
