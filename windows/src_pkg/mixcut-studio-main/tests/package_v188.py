"""Frozen app: independent batches, actual renders and durable success history."""
import json,os,socket,sqlite3,subprocess,sys,tempfile,time
from pathlib import Path
from urllib.request import Request,urlopen
with tempfile.TemporaryDirectory(prefix='mixcut188-') as tmp:
 root=Path(tmp).resolve();videos=root/'videos';music=root/'music';videos.mkdir();music.mkdir();state=root/'state';outputs=root/'outputs'
 def ff(*args):subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True,timeout=30)
 raw=videos/'raw.mp4';ff('-f','lavfi','-i','color=c=red:s=640x360:r=24:d=4','-c:v','libx264',str(raw))
 for n in ['a','b','c']:
  folder=music/n;folder.mkdir();ff('-f','lavfi','-i','sine=f=440:duration=1.5','-c:a','aac',str(folder/'song01.m4a'))
 with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
 base=f'http://127.0.0.1:{port}'
 def api(route,body=None):
  with urlopen(Request(base+route,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'}),timeout=30) as r:return json.load(r)
 def ledger():
  with sqlite3.connect((state/'state.sqlite3').as_uri()+'?mode=ro',uri=True) as conn:return {row[0]:row[1] for row in conn.execute('select folder,completed from music_folder_usage')}
 def start():
  proc=subprocess.Popen([sys.argv[1],'--server','--port',str(port),'--state-dir',str(state)],stdout=log,stderr=log)
  for _ in range(100):
   try:
    assert api('/api/bootstrap')['version']=='1.8.14';return proc
   except OSError:time.sleep(.1)
  raise AssertionError('server not ready')
 def stop(proc):
  try:api('/api/shutdown',{})
  except Exception:pass
  try:proc.wait(timeout=15)
  except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=5)
 with (root/'server.log').open('w') as log:
  proc=start()
  try:
   api('/api/scan',{'video_dir':str(videos),'music_dir':str(music)})
   cfg={'duration_basis':'music','music_mode':'folder','count':1,'width':640,'height':360,'fps':24,'parallel_tasks':1,'nonstop_music':False,'output_dir':str(outputs),'hardware':'software_fast','seed':7}
   batches=[api('/api/plan',{'config':cfg}) for _ in range(3)]
   folders=[b['items'][0]['music_folder'] for b in batches];assert len(set(folders))==3
   for b in batches:
    api('/api/batches/'+b['id']+'/start',{})
    for _ in range(400):
     current=next(x for x in api('/api/batches') if x['id']==b['id']);item=current['items'][0]
     if item['status']=='failed':raise AssertionError(item.get('error'))
     if item['status']=='success' and item.get('thumbnails',{}).get('status')=='ready':break
     time.sleep(.1)
    else:raise AssertionError('render did not finish')
    assert ledger()[b['items'][0]['music_folder']]==1
    api('/api/batches/'+b['id']+'/items/'+item['id']+'/delete',{})
   assert ledger()=={folder:1 for folder in folders};assert raw.exists();assert len(list(music.rglob('*.m4a')))==3
   stop(proc);proc=start();assert ledger()=={folder:1 for folder in folders}
   fourth=api('/api/plan',{'config':cfg});assert fourth['items'][0]['music_folder']==folders[0],'equal counts choose oldest successful folder'
   assert all(f['historical_usage']['completed']==1 for f in fourth['music_folder_catalog'])
   runtime=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
   subprocess.run([str(runtime/'bin/node'),'tests/ui_package_v188.cjs'],env=dict(os.environ,NODE_PATH=str(runtime/'node_modules'),MIXCUT_TEST_URL=base),check=True,timeout=30)
   api('/api/batches/'+fourth['id']+'/items/'+fourth['items'][0]['id']+'/delete',{})
   assert ledger()=={folder:1 for folder in folders}
   print('V1.8.14 packaged acceptance passed: three separate plans chose three folders; three actual renders recorded one success each; task deletion and server restart preserved counts; oldest folder chosen next; history UI visible; all source video/music preserved.')
  finally:stop(proc)
