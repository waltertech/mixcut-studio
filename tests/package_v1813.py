"""Packaged minute extraction and reuse of ready thirty-second paired images."""
import json,os,shutil,socket,subprocess,sys,tempfile,time
from pathlib import Path
from urllib.request import Request,urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mixcut.server import Application
with tempfile.TemporaryDirectory(prefix='mixcut1813-') as tmp:
 root=Path(tmp).resolve();output=root/'outputs';output.mkdir();state=root/'state'
 def ff(*args):return subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True,timeout=40)
 for name,color in [('a','red'),('b','blue')]:ff('-f','lavfi','-i',f'color=c={color}:s=128x72:r=2:d=35','-c:v','libx264',str(root/(name+'.mp4')))
 listing=root/'concat.txt';listing.write_text("file 'a.mp4'\nfile 'b.mp4'\n");fresh=output/'fresh.mp4';ff('-f','concat','-safe','0','-i',str(listing),'-c','copy',str(fresh));legacy=output/'legacy.mp4';shutil.copy2(fresh,legacy)
 app=Application(state);oldversion=app.keyframes._version(legacy,legacy=True);old=app.keyframes.root/oldversion;old.mkdir()
 ff('-i',str(legacy),'-vf',"select='gte(t,selected_n*30)'",'-fps_mode','vfr','-q:v','3',str(old/'%06d-thumb.jpg'))
 # FFmpeg image2 defaults to starting at 1; normalize to legacy zero-based cache names.
 for n in range(3):(old/f'{n+1:06d}-thumb.jpg').rename(old/f'{n:06d}-thumb.jpg')
 for n in range(3):shutil.copy2(old/f'{n:06d}-thumb.jpg',old/f'{n:06d}-large.jpg')
 inode=(old/'000002-thumb.jpg').stat().st_ino
 (old/'index.json').write_text(json.dumps({'version':oldversion,'duration':70.,'total':3,'frames':[{'index':n,'time':n*30.} for n in range(3)]}))
 items=[]
 for n,path in enumerate([fresh,legacy],1):
  st=path.stat();items.append({'id':str(n),'index':n,'status':'success','progress':1,'duration':70.,'music':[],'segments':[],'output_path':str(path),'output_name':path.name,'result':{'duration':70.,'output_size':st.st_size,'output_mtime_ns':st.st_mtime_ns},'thumbnails':{'status':'ready' if n==2 else 'queued','version':oldversion if n==2 else '', 'total':3 if n==2 else 0}})
 app.store.put('batch:abc123',{'id':'abc123','created_at':1,'updated_at':1,'status':'completed','config':{'output_dir':str(output)},'output_folder':str(output),'items':items,'assets':[]});app.closing.set()
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
   assert boot['version']=='1.8.18'
   for _ in range(400):
    batch=next(b for b in api('/api/batches') if b['id']=='abc123')
    if all(i.get('thumbnails',{}).get('status')=='ready' for i in batch['items']):break
    time.sleep(.1)
   else:raise AssertionError('previews did not finish')
   for n,path in enumerate([fresh,legacy],1):
    data=api(f'/api/keyframes?batch=abc123&item={n}');assert [f['time'] for f in data['frames']]==[0.,60.];assert data['total']==2
    for idx in range(2):
     for size in ['thumb','large']:
      target=state/'keyframes'/data['version']/f'{idx:06d}.jpg';assert target.is_file()
      rgb=ff('-i',str(target),'-vf','scale=1:1','-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','pipe:1').stdout;assert rgb[0 if idx==0 else 2]>200
    if n==2:assert (state/'keyframes'/data['version']/'000001.jpg').stat().st_ino==inode
   assert not old.exists()
   runtime=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
   subprocess.run([str(runtime/'bin/node'),'tests/ui_package_v1813.cjs'],env=dict(os.environ,NODE_PATH=str(runtime/'node_modules'),MIXCUT_TEST_URL=base),check=True,timeout=30)
   print('V1.8.18 packaged acceptance passed: new 70-second video has two single-image frames at 0/60s; both sizes match actual colors; legacy ready 30s previews reused by hardlink without full regeneration; old cache removed; real UI says every60seconds with two frames.')
  finally:
   try:api('/api/shutdown',{})
   except Exception:pass
   try:proc.wait(timeout=15)
   except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=5)
