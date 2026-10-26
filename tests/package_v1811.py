"""Frozen backend/browser: actual repeated songs verified by audio frequencies."""
import array,json,math,os,re,socket,sqlite3,subprocess,sys,tempfile,time
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
with tempfile.TemporaryDirectory(prefix='mixcut1811-') as tmp:
 root=Path(tmp).resolve();videos=root/'videos';music=root/'music';videos.mkdir();(music/'style').mkdir(parents=True);state=root/'state';outputs=root/'outputs'
 def ff(*args):return subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True,timeout=90)
 raw=videos/'raw.mp4';ff('-f','lavfi','-i','color=c=red:s=640x360:r=30:d=3','-c:v','libx264',str(raw))
 for n in range(1,6):ff('-f','lavfi','-i',f'sine=f={330+n*110}:duration=1.2','-c:a','aac',str(music/'style'/f'song-{n:02}.m4a'))
 with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
 base=f'http://127.0.0.1:{port}'
 def api(route,body=None):
  with urlopen(Request(base+route,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'}),timeout=60) as r:return json.load(r)
 with (root/'server.log').open('w') as log:
  proc=subprocess.Popen([sys.argv[1],'--server','--port',str(port),'--state-dir',str(state)],stdout=log,stderr=log)
  try:
   for _ in range(100):
    try:boot=api('/api/bootstrap');break
    except OSError:time.sleep(.1)
   assert boot['version']=='1.8.16';api('/api/scan',{'video_dir':str(videos),'music_dir':str(music)});api('/api/preferences',{'output_dir':str(outputs)})
   runtime=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
   subprocess.run([str(runtime/'bin/node'),'tests/ui_package_v1811.cjs'],env=dict(os.environ,NODE_PATH=str(runtime/'node_modules'),MIXCUT_TEST_URL=base),check=True,timeout=90)
   batch=next(b for b in api('/api/batches') if b['status']=='draft');api('/api/batches/'+batch['id']+'/start',{})
   for _ in range(1500):
    current=next(b for b in api('/api/batches') if b['id']==batch['id'])
    if any(i['status']=='failed' for i in current['items']):raise AssertionError([(i['status'],i.get('error')) for i in current['items']])
    if all(i['status']=='success' and i.get('thumbnails',{}).get('status')=='ready' for i in current['items']):break
    time.sleep(.1)
   else:raise AssertionError('render did not finish')
   for item in current['items']:
    count=item['music_repeats'];played=item['result']['played_music'];assert len(played)==count*5
    ids=[s['id'] for s in item['music_base']];assert [s['id'] for s in played]==ids*count
    assert abs(item['result']['duration']-6*count)<.2
    output=Path(item['output_path']);assert output.exists()
    text=output.with_suffix('.txt').read_text();titles=re.findall(r'^\d+\. (.+)$',text,re.M);assert titles==[t['title'] for t in played]
    samples=array.array('f');samples.frombytes(ff('-i',str(output),'-vn','-ac','1','-ar','8000','-f','f32le','pipe:1').stdout)
    freqs=[330+n*110 for n in range(1,6)]
    for track in played:
     mid=track['start']+.5;window=samples[int(mid*8000):int(mid*8000)+800]
     def power(f):return sum(v*math.cos(2*math.pi*f*n/8000) for n,v in enumerate(window))**2+sum(v*math.sin(2*math.pi*f*n/8000) for n,v in enumerate(window))**2
     expected=330+int(re.search(r'(\d+)$',track['title'])[1])*110
     assert max(freqs,key=power)==expected,(count,track)
    try:api('/api/batches/'+current['id']+'/items/'+item['id']+'/music-repeats',{'music_repeats':4})
    except HTTPError as e:assert e.code==400
    else:raise AssertionError('completed edit should reject')
   with sqlite3.connect((state/'state.sqlite3').as_uri()+'?mode=ro',uri=True) as conn:assert conn.execute('select sum(completed) from music_folder_usage').fetchone()[0]==2
   assert raw.exists();assert len(list(music.rglob('*.m4a')))==5
   print('V1.8.16 packaged acceptance passed: two actual 2/3-round renders; audio frequency verified every song in every round; complete repeated title documents; duration matched; finished edits rejected; folder history counted two tasks; original video/music preserved.')
  finally:
   try:api('/api/shutdown',{})
   except Exception:pass
   try:proc.wait(timeout=15)
   except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=5)
