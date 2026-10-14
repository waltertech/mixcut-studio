"""Refresh real files through the frozen application's UI, in isolated state."""
import json,os,shutil,socket,subprocess,sys,tempfile,time
from pathlib import Path
from urllib.request import Request,urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mixcut import media
from mixcut.server import Store
with tempfile.TemporaryDirectory(prefix='mixcut183-') as tmp:
 root=Path(tmp).resolve()
 for name in ['videos','music','new-music']:(root/name).mkdir()
 def ff(*args):subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True,timeout=30)
 ff('-f','lavfi','-i','color=c=red:s=160x90:r=24:d=1','-c:v','libx264',str(root/'backup.mp4'))
 for name in ['kept.mp4','removed.mp4']:shutil.copy2(root/'backup.mp4',root/'videos'/name)
 ff('-f','lavfi','-i','sine=f=440:duration=1','-c:a','libmp3lame',str(root/'music/song.mp3'))
 shutil.copy2(root/'music/song.mp3',root/'new-music/song.mp3')
 state=root/'state';Store(state).put('library',{'video_dir':str(root/'videos'),'music_dir':str(root/'music'),'scan':media.scan(str(root/'videos'),str(root/'music'),str(root/'cache'),quick_music=True)})
 with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
 base=f'http://127.0.0.1:{port}'
 def api(route,body=None):
  with urlopen(Request(base+route,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'}),timeout=15) as response:return json.load(response)
 with (root/'server.log').open('w') as log:
  process=subprocess.Popen([sys.argv[1],'--server','--port',str(port),'--state-dir',str(state)],stdout=log,stderr=log)
  try:
   for _ in range(100):
    try:boot=api('/api/bootstrap');break
    except OSError:time.sleep(.1)
   assert boot['version']=='1.8.4'
   runtime=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
   subprocess.run([str(runtime/'bin/node'),'tests/ui_v183.cjs'],env=dict(os.environ,NODE_PATH=str(runtime/'node_modules'),MIXCUT_TEST_URL=base,MIXCUT_SCAN_FIXTURE=str(root)),check=True)
   batch=api('/api/plan',{'config':{'duration_basis':'music','music_mode':'fixed','count':1,'output_dir':str(root/'output')}})
   assert all(s['path'].endswith('/new.mp4') for s in batch['items'][0]['segments'])
   assert all(s['path'].endswith('/added.mp3') for s in batch['items'][0]['music'])
   print('Packaged fresh-plan API passed: only refreshed current video/music selected.')
  finally:
   try:api('/api/shutdown',{})
   except Exception:pass
   try:process.wait(timeout=15)
   except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)
