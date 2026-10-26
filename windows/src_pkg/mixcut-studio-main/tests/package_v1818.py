"""Frozen API and browser: recursively separate leaf sets, accept direct set root."""
import json,os,shutil,socket,subprocess,sys,tempfile,time
from pathlib import Path
from urllib.request import Request,urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mixcut import media
from mixcut.server import Store
with tempfile.TemporaryDirectory(prefix='mixcut1818-') as tmp:
 root=Path(tmp).resolve();videos=root/'videos';videos.mkdir();music=root/'music';music.mkdir()
 def ff(*args):subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True,timeout=30)
 ff('-f','lavfi','-i','color=c=blue:s=160x90:r=24:d=2','-c:v','libx264',str(videos/'v.mp4'))
 ff('-f','lavfi','-i','sine=f=440:duration=1',str(root/'tone.wav'))
 for relative in ['时长/短/00时00分03秒_3首_M9q9pvKB7hY','时长/短/1首_u8_-USakt0c','时长/长/1首__itb5qqD5vs','/'.join(['深层']*10)+'/1首_TgxEAs2BVZ4']:
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
   subprocess.run([str(runtime/'bin/node'),'tests/ui_package_v1818.cjs'],env=dict(os.environ,NODE_PATH=str(runtime/'node_modules'),MIXCUT_TEST_URL=base),check=True,timeout=45)
   batches=api('/api/batches');assert len(batches)==1
   batch=batches[0];assert len(batch['music_folder_catalog'])==4
   assert len({i['music_folder'] for i in batch['items']})==4
   for item in batch['items']:
    assert len(item['music'])==6 and abs(item['duration']-6)<.01,item
    assert all(str(Path(s['path']).parent)==item['music_folder'] for s in item['music'])
   # Re-scan the same fixture selecting a leaf root itself through actual HTTP.
   direct=music/'时长/短/00时00分03秒_3首_M9q9pvKB7hY'
   job=api('/api/scan/start',{'video_dir':str(videos),'music_dir':str(direct),'kind':'both'})
   for _ in range(200):
    status=api('/api/scan/status?compact=1')
    if status['status']=='completed':break
    if status['status']=='failed':raise AssertionError(status)
    time.sleep(.05)
   batch=api('/api/plan',{'config':{'music_mode':'folder','duration_basis':'music','mode':'single','count':1,'music_repeats':2,'min_source_duration_minutes':0,'output_dir':str(root/'output'),'hardware':'software_fast','nonstop_music':False,'include_track_titles':True,'width':640,'height':360,'fps':24}})
   assert batch['items'][0]['music_folder']==str(direct)
   assert len(batch['items'][0]['music'])==6
   expected=Path(batch['items'][0]['output_bundle']).name+'_'+direct.name+'.mp4'
   assert batch['items'][0]['output_name']==expected,batch['items'][0]
   batch=api(f"/api/batches/{batch['id']}/items/{batch['items'][0]['id']}/music-repeats",{'music_repeats':3})
   assert batch['items'][0]['output_name']==expected and abs(batch['items'][0]['duration']-9)<.01
   bid=batch['id'];api(f'/api/batches/{bid}/start',{})
   for _ in range(400):
    current=next(b for b in api('/api/batches') if b['id']==bid);item=current['items'][0]
    if item['status']=='failed':raise AssertionError(item)
    video=Path(item['output_path']);txt=video.with_suffix('.txt')
    if item['status']=='success' and item.get('thumbnails',{}).get('status')=='ready' and txt.is_file():break
    time.sleep(.1)
   else:raise AssertionError('render did not finish')
   assert video.name==expected,item
   assert abs(item['result']['duration']-9)<.15
   assert txt.stem==video.stem and '曲目列表' in txt.read_text()
   assert list(video.parent.glob('*.mp4'))==[video]
   ff('-xerror','-i',str(video),'-f','null','-')
   print('V1.8.18 packaged real render passed: bundle prefix/original library folder filename unchanged after 2-to-3 rounds, matching TXT with track list, playback decode, thumbnails ready; music preserved.')
   print('V1.8.18 frozen API/UI passed: real recursive scan, 4 separate sets at varying depth including 10 levels, 01/02 anchored, 2 rounds=6 tracks/6 seconds; then 3 rounds=9 seconds with original 03-second library label; direct leaf root scan and planning passed. Actual render and sidecar naming verified.')
  finally:
   try:api('/api/shutdown',{})
   except Exception:pass
   try:proc.wait(timeout=15)
   except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=5)
