"""Frozen service: three real thumbnail jobs concurrently, one JPEG per minute."""
import json,os,socket,subprocess,sys,tempfile,time
from pathlib import Path
from urllib.request import Request,urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mixcut.server import Application
with tempfile.TemporaryDirectory(prefix='mixcut1814-') as tmp:
 root=Path(tmp).resolve();state=root/'state';out=root/'outputs';out.mkdir();items=[]
 for n,color in enumerate(['red','green','blue'],1):
  path=out/f'{n}.mp4';subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',f'color=c={color}:s=320x180:r=2:d=600','-c:v','libx264',str(path)],check=True,capture_output=True,timeout=30)
  st=path.stat();items.append({'id':str(n),'index':n,'status':'success','progress':1,'duration':600.,'music':[],'segments':[],'output_path':str(path),'output_name':path.name,'result':{'duration':600.,'output_size':st.st_size,'output_mtime_ns':st.st_mtime_ns},'thumbnails':{'status':'queued'}})
 app=Application(state);app.store.put('batch:abc123',{'id':'abc123','created_at':1,'status':'completed','config':{'output_dir':str(out),'parallel_tasks':3},'output_folder':str(out),'assets':[],'items':items});app.closing.set()
 with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
 base=f'http://127.0.0.1:{port}'
 def api(route,body=None):
  with urlopen(Request(base+route,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'}),timeout=30) as r:return json.load(r)
 logpath=root/'server.log'
 with logpath.open('w') as log:
  proc=subprocess.Popen([sys.argv[1],'--server','--port',str(port),'--state-dir',str(state)],stdout=log,stderr=log)
  try:
   for _ in range(100):
    try:assert api('/api/bootstrap')['version']=='1.8.14';break
    except OSError:time.sleep(.1)
   peak=0
   for _ in range(800):
    batch=api('/api/batches')[0];states=[i['thumbnails']['status'] for i in batch['items']];peak=max(peak,states.count('generating'))
    if 'failed' in states:raise AssertionError(batch)
    if all(s=='ready' for s in states):break
    time.sleep(.02)
   else:raise AssertionError('thumbnail jobs did not finish')
   active=0;logged_peak=0
   for line in logpath.read_text().splitlines():
    if 'thumbnail seek start' in line:active+=1;logged_peak=max(logged_peak,active)
    elif 'thumbnail seek ready' in line:active-=1
   assert logged_peak==3,(peak,logged_peak,logpath.read_text())
   for item in batch['items']:
    data=api('/api/keyframes?batch=abc123&item='+item['id']);assert len(data['frames'])==10;assert [f['time'] for f in data['frames']]==list(range(0,600,60))
    folder=state/'keyframes'/data['version'];assert len(list(folder.glob('*.jpg')))==10;assert not list(folder.glob('*-large.jpg'))
    urls=[base+f"/api/keyframe?batch=abc123&item={item['id']}&index=1&size={size}&v={data['version']}" for size in ['thumb','large']]
    assert urlopen(urls[0]).read()==urlopen(urls[1]).read()
   runtime=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
   subprocess.run([str(runtime/'bin/node'),'tests/ui_package_v1814.cjs'],env=dict(os.environ,NODE_PATH=str(runtime/'node_modules'),MIXCUT_TEST_URL=base),check=True,timeout=30)
   print('V1.8.14 packaged acceptance passed: three actual thumbnail jobs overlap; selected parallelism3 honored; 600-second videos each contain10 single JPEGs; minute timestamps; thumb/large APIs identical; hover uses same URL; no high-resolution files.')
  finally:
   try:api('/api/shutdown',{})
   except Exception:pass
   try:proc.wait(timeout=15)
   except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=5)
