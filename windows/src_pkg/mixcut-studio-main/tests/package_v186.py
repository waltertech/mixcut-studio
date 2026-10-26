"""Packaged backend and browser: scan errors cleared through the actual UI/API."""
import json,os,socket,subprocess,sys,tempfile,time
from pathlib import Path
from urllib.request import Request,urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mixcut.server import Application
from mixcut import media
with tempfile.TemporaryDirectory(prefix='mixcut186-') as tmp:
 root=Path(tmp).resolve();videos=root/'videos';music=root/'music';videos.mkdir();music.mkdir()
 bad=videos/'bad.mp4';bad.write_bytes(b'broken mp4');changing=videos/'recording.mp4';changing.write_bytes(b'incomplete')
 song=music/'keep.mp3';song.write_bytes(b'bad music still kept')
 good=videos/'valid.mp4'
 subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','color=c=red:s=160x90:r=24:d=1','-c:v','libx264',str(good)],check=True,timeout=30)
 state=root/'state';app=Application(state);scan=media.scan(str(videos),str(music),str(root/'cache'),quick_music=True)
 assert len(scan['errors'])==3
 app.store.put('library',{'video_dir':str(videos),'music_dir':str(music),'scan':scan});app.closing.set()
 changing.write_bytes(b'recording still in progress')
 with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
 base=f'http://127.0.0.1:{port}'
 def api(route,body=None):
  with urlopen(Request(base+route,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'}),timeout=30) as r:return json.load(r)
 with (root/'server.log').open('w') as log:
  proc=subprocess.Popen([sys.argv[1],'--server','--port',str(port),'--state-dir',str(state)],stdout=log,stderr=log)
  try:
   for _ in range(100):
    try:boot=api('/api/bootstrap');break
    except OSError:time.sleep(.1)
   assert boot['version']=='1.8.16'
   runtime=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
   subprocess.run([str(runtime/'bin/node'),'tests/ui_package_v186.cjs'],env=dict(os.environ,NODE_PATH=str(runtime/'node_modules'),MIXCUT_TEST_URL=base),check=True,timeout=60)
   assert not bad.exists();assert changing.exists();assert song.exists();assert good.exists()
   assert len(api('/api/bootstrap')['scan']['errors'])==2
   print('V1.8.16 packaged acceptance passed: real scan, real browser button, actual API, native Trash, changed recording preserved, valid video and music preserved, persisted errors updated.')
  finally:
   try:api('/api/shutdown',{})
   except Exception:pass
   try:proc.wait(timeout=15)
   except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=5)
