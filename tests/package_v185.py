"""Frozen app: forty real video files reviewed with shared-source retirement."""
import json,os,shutil,socket,subprocess,sys,tempfile,time
from pathlib import Path
from urllib.request import Request,urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mixcut import media
from mixcut.server import Application
from mixcut.tracklist import played_music
with tempfile.TemporaryDirectory(prefix='mixcut185-') as tmp:
 root=Path(tmp).resolve();videos=root/'videos';music=root/'music';videos.mkdir();(music/'style').mkdir(parents=True)
 def ff(*args):subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True,timeout=30)
 raw=videos/'raw.mp4';song=music/'style/song-去重 01.m4a';sample=root/'sample.mp4'
 ff('-f','lavfi','-i','color=c=red:s=640x360:r=24:d=2','-c:v','libx264',str(raw))
 ff('-f','lavfi','-i','sine=f=440:duration=1','-c:a','aac',str(song))
 ff('-i',str(raw),'-i',str(song),'-map','0:v:0','-map','1:a:0','-c','copy','-t','1',str(sample))
 state=root/'state';app=Application(state)
 app.store.put('library',{'video_dir':str(videos),'music_dir':str(music),'scan':media.scan(str(videos),str(music),str(root/'cache'),quick_music=True)})
 batch=app.create_plan({'config':{'duration_basis':'music','music_mode':'folder','count':40,'width':640,'height':360,'fps':24,'output_dir':str(root/'outputs')}})
 paths=[]
 for item in batch['items']:
  path=Path(item['output_path']);shutil.copy2(sample,path);st=path.stat();preview=app.keyframes.prepare(path)
  item.update(status='success',progress=1,result={'duration':1,'output_size':st.st_size,'output_mtime_ns':st.st_mtime_ns,'played_music':played_music(item,1)},thumbnails={'status':'ready','version':preview['version'],'total':preview['total']})
  paths.append(path)
 batch['status']='completed';app.store.put('batch:'+batch['id'],batch);app.closing.set()
 with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
 base=f'http://127.0.0.1:{port}'
 def api(route,body=None):
  with urlopen(Request(base+route,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'}),timeout=30) as r:return json.load(r)
 with (root/'server.log').open('w') as log:
  process=subprocess.Popen([sys.argv[1],'--server','--port',str(port),'--state-dir',str(state)],stdout=log,stderr=log)
  try:
   for _ in range(100):
    try:boot=api('/api/bootstrap');break
    except OSError:time.sleep(.1)
   assert boot['version']=='1.8.14'
   runtime=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
   subprocess.run([str(runtime/'bin/node'),'tests/ui_package_v185.cjs'],env=dict(os.environ,NODE_PATH=str(runtime/'node_modules'),MIXCUT_TEST_URL=base),check=True)
   assert api('/api/batches')==[];assert not raw.exists();assert song.exists()
   assert all(p.is_file() and p.read_bytes()==sample.read_bytes() and p.with_suffix('.txt').is_file() for p in paths)
   for _ in range(150):
    if app.store.get('batch:'+batch['id']) is None:break
    time.sleep(.1)
   else:raise AssertionError('approved records not pruned')
   print('Frozen-app acceptance passed: forty real work files and documents retained; shared original video recycled only after final approval; music preserved; all approved records automatically removed.')
  finally:
   try:api('/api/shutdown',{})
   except Exception:pass
   try:process.wait(timeout=15)
   except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)
