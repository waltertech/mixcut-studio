"""Frozen application: cross-day approval renames a full bundle without copying."""
import json,socket,subprocess,sys,tempfile,time
from pathlib import Path
from datetime import datetime
from urllib.request import Request,urlopen
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mixcut.server import Store
with tempfile.TemporaryDirectory(prefix='mixcut189-') as tmp:
 root=Path(tmp).resolve();out=root/'exports';old=out/'2000-01-01'/'001-old';old.mkdir(parents=True)
 video=old/'video.mp4';subprocess.run(['/Applications/MixCut Studio.app/Contents/Frameworks/bin/ffmpeg','-v','error','-f','lavfi','-i','color=c=red:s=160x90:r=24:d=2','-c:v','libx264',str(video)],check=True);(old/'cover.jpg').write_bytes(b'cover');(old/'发布文案.txt').write_text('copy')
 stat=video.stat();inode=stat.st_ino
 item={'id':'one','index':1,'duration':3,'status':'success','progress':1,'music':[],'segments':[], 'output_bundle':str(old),'output_path':str(video),'result':{'output_size':stat.st_size,'output_mtime_ns':stat.st_mtime_ns}}
 state=root/'state';Store(state).put('batch:fixture',{'id':'fixture','status':'completed','created_at':time.time(),'config':{'output_dir':str(out)},'output_folder':str(old.parent),'items':[item]})
 with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
 def api(route,body=None):
  try:
   with urlopen(Request(f'http://127.0.0.1:{port}'+route,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'}),timeout=20) as response:return json.load(response)
  except HTTPError as e:raise AssertionError(e.read().decode()) from e
 process=subprocess.Popen([sys.argv[1],'--server','--port',str(port),'--state-dir',str(state)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 try:
  for _ in range(100):
   try:boot=api('/api/bootstrap');break
   except OSError:time.sleep(.1)
  assert boot['version']=='1.8.18'
  for _ in range(200):
   current=api('/api/batches')[0]['items'][0]
   if current.get('thumbnails',{}).get('status')=='ready':break
   time.sleep(.1)
  else:raise AssertionError('previews not ready')
  result=api('/api/approve',{'batch_id':'fixture','item_id':'one'});new=Path(result['items'][0]['output_path']);today=datetime.now().astimezone()
  assert new.parent.name==today.strftime('%y%m%d')+'-001'
  assert new.parent.parent.name==today.strftime('%Y-%m-%d')
  assert new.stat().st_ino==inode and not old.exists()
  assert (new.parent/'cover.jpg').read_bytes()==b'cover' and (new.parent/'发布文案.txt').read_text()=='copy'
  assert result['items'][0]['review']['path']==str(new)
  print('V1.8.18 frozen API acceptance passed: approval-date parent and child naming; full bundle retained; original inode preserved; persisted review path updated.')
 finally:
  try:api('/api/shutdown',{})
  except Exception:pass
  process.wait(timeout=15)
