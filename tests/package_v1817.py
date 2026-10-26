"""Frozen service/browser regression: partial and all deletion of 73 task rows."""
import json,os,socket,subprocess,sys,tempfile,time
from pathlib import Path
from urllib.request import Request,urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mixcut.server import Store
with tempfile.TemporaryDirectory(prefix='mixcut1817-') as tmp:
 root=Path(tmp).resolve();state=root/'state';out=root/'output';out.mkdir()
 raw=root/'raw.mp4';raw.write_bytes(b'raw source');music=root/'song.mp3';music.write_bytes(b'music source')
 items=[]
 for n in range(73):
  folder=out/f'{n:03d}';folder.mkdir();output=folder/'video.mp4'
  item={'id':str(n),'index':n+1,'status':'pending','progress':0,'duration':60,'music':[],
        'segments':[],'output_path':str(output),'output_bundle':str(folder),'output_name':output.name,'test_payload':'x'*60000}
  if n==0:
   output.write_bytes(b'approved work');st=output.stat()
   item.update(status='success',progress=1,result={'output_size':st.st_size,'output_mtime_ns':st.st_mtime_ns},
    segments=[{'asset_id':'held','path':str(raw),'start':0,'duration':60}],
    review={'status':'approved','retained':True,'path':str(output)},
    cleanup={'output_retained':True,'temporary_segments_deleted':True,'original_recordings_deleted':True})
  items.append(item)
 store=Store(state);store.put('cache-cleanup-pending',['held'])
 store.put('batch:abc123',{'id':'abc123','created_at':1,'status':'draft','config':{'output_dir':str(out)},'items':items})
 with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
 base=f'http://127.0.0.1:{port}'
 def api(route,body=None):
  with urlopen(Request(base+route,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'}),timeout=15) as r:return json.load(r)
 with (root/'server.log').open('w') as log:
  proc=subprocess.Popen([sys.argv[1],'--server','--port',str(port),'--state-dir',str(state)],stdout=log,stderr=log)
  try:
   for _ in range(100):
    try:assert api('/api/bootstrap')['version']=='1.8.18';break
    except OSError:time.sleep(.1)
   start=time.monotonic()
   result=api('/api/tasks/forget',{'selections':[{'batch_id':'abc123','item_ids':['72']}]})
   assert result['deleted_items']==1 and len(result['updated_batches']['abc123']['items'])==72
   assert time.monotonic()-start<5
   runtime=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
   subprocess.run([str(runtime/'bin/node'),'tests/ui_package_v1817.cjs'],env=dict(os.environ,NODE_PATH=str(runtime/'node_modules'),MIXCUT_TEST_URL=base),check=True,timeout=30)
   assert not api('/api/batches')
   assert raw.exists() and music.exists() and (out/'000/video.mp4').exists()
   assert not any('database is locked' in line for line in (root/'server.log').read_text().splitlines())
   print('V1.8.18 frozen API/UI passed: 73 rows with ~4 MB payload; partial delete preserves approved survivor; browser Delete All removes remaining72; source video/music and approved work preserved; no database lock.')
  finally:
   try:api('/api/shutdown',{})
   except Exception:pass
   try:proc.wait(timeout=15)
   except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=5)
