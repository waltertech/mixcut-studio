"""Native frozen-app deletion: empty, unreviewed bundle and approved retention."""
import json,socket,subprocess,sys,tempfile,time
from pathlib import Path
from urllib.request import Request,urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mixcut import media
from mixcut.server import Store
with tempfile.TemporaryDirectory(prefix='mixcut184-') as tmp:
 root=Path(tmp).resolve();videos=root/'videos';music=root/'music';videos.mkdir();(music/'style').mkdir(parents=True)
 def ff(*args):subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True,timeout=30)
 v=videos/'raw.mp4';song=music/'style/song-去重 01.m4a'
 ff('-f','lavfi','-i','color=c=red:s=640x360:r=24:d=2','-c:v','libx264',str(v))
 ff('-f','lavfi','-i','sine=f=440:duration=1','-c:a','aac',str(song))
 state=root/'state';Store(state).put('library',{'video_dir':str(videos),'music_dir':str(music),'scan':media.scan(str(videos),str(music),str(root/'cache'),quick_music=True)})
 with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
 def api(route,body=None):
  with urlopen(Request(f'http://127.0.0.1:{port}'+route,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'}),timeout=30) as response:return json.load(response)
 with (root/'server.log').open('w') as log:
  process=subprocess.Popen([sys.argv[1],'--server','--port',str(port),'--state-dir',str(state)],stdout=log,stderr=log)
  try:
   for _ in range(100):
    try:boot=api('/api/bootstrap');break
    except OSError:time.sleep(.1)
   assert boot['version']=='1.8.4'
   config={'duration_basis':'music','music_mode':'folder','count':10,'width':640,'height':360,'fps':24,'hardware':'software_fast','output_dir':str(root/'outputs')}
   batch=api('/api/plan',{'config':config});folders=[Path(i['output_bundle']) for i in batch['items']]
   api('/api/tasks/forget',{'selections':[{'batch_id':batch['id'],'item_ids':[i['id'] for i in batch['items']]}]})
   assert all(not f.exists() for f in folders)
   batch=api('/api/plan',{'config':dict(config,count=2)})
   assert Path(batch['items'][0]['output_bundle']).name.startswith('001-')
   bid=batch['id'];api(f'/api/batches/{bid}/start',{})
   for _ in range(300):
    batch=next(b for b in api('/api/batches') if b['id']==bid)
    if any(i['status']=='failed' for i in batch['items']):raise AssertionError(batch)
    if all(i['status']=='success' and i.get('thumbnails',{}).get('status')=='ready' for i in batch['items']):break
    time.sleep(.1)
   else:raise AssertionError('render timeout')
   first,second=batch['items'];a=Path(first['output_bundle']);b=Path(second['output_bundle'])
   api('/api/approve',{'batch_id':bid,'item_id':first['id']})
   snapshot={p.name:p.read_bytes() for p in a.iterdir() if p.is_file()}
   api('/api/tasks/forget',{'selections':[{'batch_id':bid,'item_ids':[first['id'],second['id']]}]})
   assert a.exists() and snapshot=={p.name:p.read_bytes() for p in a.iterdir() if p.is_file()}
   assert not b.exists()
   assert v.exists() and song.exists()
   assert api('/api/batches')==[]
   print('V1.8.4 packaged native acceptance passed: ten empty reservations removed and numbers reusable; rendered unreviewed bundle recycled; approved folder/files byte-identical; original video/music preserved; task rows deleted.')
  finally:
   try:api('/api/shutdown',{})
   except Exception:pass
   try:process.wait(timeout=15)
   except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)
