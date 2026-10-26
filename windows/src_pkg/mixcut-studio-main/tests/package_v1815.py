"""Frozen API and browser: recursively separate leaf sets, accept direct set root."""
import json,os,shutil,socket,subprocess,sys,tempfile,time
from pathlib import Path
from urllib.request import Request,urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mixcut import media
from mixcut.server import Store
with tempfile.TemporaryDirectory(prefix='mixcut1815-') as tmp:
 root=Path(tmp).resolve();videos=root/'videos';videos.mkdir();music=root/'music';music.mkdir()
 def ff(*args):subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True,timeout=30)
 ff('-f','lavfi','-i','color=c=blue:s=160x90:r=24:d=2','-c:v','libx264',str(videos/'v.mp4'))
 ff('-f','lavfi','-i','sine=f=440:duration=1',str(root/'tone.wav'))
 for relative in ['时长/短/A','时长/短/B','时长/长/A','/'.join(['深层']*10)+'/C']:
  folder=music/relative;folder.mkdir(parents=True)
  for n in [1,2,3]:shutil.copy2(root/'tone.wav',folder/f'歌曲 {n:02d}.wav')
 state=root/'state';scan=media.scan(str(videos),str(music),str(root/'cache'),quick_music=True)
 Store(state).put('library',{'video_dir':str(videos),'music_dir':str(music),'scan':scan})
 with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
 base=f'http://127.0.0.1:{port}'
 def api(route,body=None):
  with urlopen(Request(base+route,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'}),timeout=30) as r:return json.load(r)
 with (root/'server.log').open('w') as log:
  proc=subprocess.Popen([sys.argv[1],'--server','--port',str(port),'--state-dir',str(state)],stdout=log,stderr=log)
  try:
   for _ in range(100):
    try:assert api('/api/bootstrap')['version']=='1.8.18';break
    except OSError:time.sleep(.1)
   runtime=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
   subprocess.run([str(runtime/'bin/node'),'tests/ui_package_v1815.cjs'],env=dict(os.environ,NODE_PATH=str(runtime/'node_modules'),MIXCUT_TEST_URL=base),check=True,timeout=45)
   batches=api('/api/batches');assert len(batches)==1
   batch=batches[0];assert len(batch['music_folder_catalog'])==4
   assert len({i['music_folder'] for i in batch['items']})==4
   for item in batch['items']:
    assert len(item['music'])==6 and abs(item['duration']-6)<.01,item
    assert all(str(Path(s['path']).parent)==item['music_folder'] for s in item['music'])
   # Re-scan the same fixture selecting a leaf root itself through actual HTTP.
   direct=music/'时长/短/A'
   job=api('/api/scan/start',{'video_dir':str(videos),'music_dir':str(direct),'kind':'both'})
   for _ in range(200):
    status=api('/api/scan/status?compact=1')
    if status['status']=='completed':break
    if status['status']=='failed':raise AssertionError(status)
    time.sleep(.05)
   batch=api('/api/plan',{'config':{'music_mode':'folder','duration_basis':'music','mode':'single','count':1,'music_repeats':2,'min_source_duration_minutes':0,'output_dir':str(root/'output'),'width':640,'height':360,'fps':24}})
   assert batch['items'][0]['music_folder']==str(direct)
   assert len(batch['items'][0]['music'])==6
   print('V1.8.18 frozen API/UI passed: real recursive scan, 4 separate sets at varying depth including 10 levels, 01/02 anchored, 2 rounds=6 tracks/6 seconds; direct leaf root scan and planning passed. No actual rendering requested.')
  finally:
   try:api('/api/shutdown',{})
   except Exception:pass
   try:proc.wait(timeout=15)
   except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=5)
