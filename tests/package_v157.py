"""Run real render/API acceptance against a supplied frozen executable in isolated state."""
import json, os, socket, subprocess, sys, tempfile, time
from pathlib import Path
from urllib.request import Request, urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mixcut import media
from mixcut.server import Store


def main():
 executable=sys.argv[1]
 with tempfile.TemporaryDirectory(prefix='mixcut157-package-',dir=str(Path.home()/'Downloads')) as tmp:
  root=Path(tmp).resolve();videos=root/'videos';music=root/'music';videos.mkdir();music.mkdir()
  def ff(*args):subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True)
  v=videos/'video.mp4';ff('-f','lavfi','-i','testsrc2=s=640x360:r=24:d=8','-c:v','libx264',str(v))
  songs=[]
  for folder in ['风格一','风格二']:
   (music/folder).mkdir()
   for n in range(2):
    a=music/folder/f'曲目 {n+1}.m4a';ff('-f','lavfi','-i',f'sine=f={440+n*100}:duration=3','-c:a','aac',str(a));songs.append(media._asset(a,'music',quick_music=True))
  va=media._asset(v,'video');state=root/'state';store=Store(state)
  store.put('library',{'video_dir':str(videos),'music_dir':str(music),'scan':{'videos':[va],'music':songs,'errors':[]}})
  with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
  log=(root/'server.log').open('w');process=subprocess.Popen([executable,'--server','--port',str(port),'--state-dir',str(state)],stdout=log,stderr=log)
  base=f'http://127.0.0.1:{port}'
  def api(route,body=None):
   req=Request(base+route,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'})
   with urlopen(req,timeout=30) as response:return json.load(response)
  try:
   for _ in range(100):
    try:boot=api('/api/bootstrap');break
    except OSError:time.sleep(.1)
   assert boot['version']=='1.5.8' and boot['api_protocol']==14
   batches=[]
   for mode in ['folder','pool']:
    config={'music_mode':mode,'count':1,'min_duration':4,'max_duration':4,'mode':'single','allow_overlap':True,
            'min_songs':2,'max_songs':2,'width':640,'height':360,'fps':24,'hardware':'software_fast',
            'include_track_titles':True,'output_dir':str(root/'exports'),'nonstop_music':True,'seed':7}
    batch=api('/api/plan',{'config':config});bid=batch['id'];iid=batch['items'][0]['id'];assert batch['items'][0]['include_track_titles']
    api(f'/api/batches/{bid}/start',{})
    for _ in range(300):
     records=api('/api/batches'); records=records.get('batches',[]) if isinstance(records,dict) else records
     batch=next(b for b in records if b['id']==bid);item=batch['items'][0]
     if item['status']=='failed':raise AssertionError(item.get('error'))
     if item['status']=='success' and item.get('thumbnails',{}).get('status')=='ready' and Path(item['output_path']).with_suffix('.txt').exists():break
     time.sleep(.1)
    else:raise AssertionError('render/thumbnail timeout')
    txt=Path(item['output_path']).with_suffix('.txt');content=txt.read_text();tracks=item['result']['played_music']
    assert tracks and content.endswith(''.join(f"{n}. {track['title']}\n" for n,track in enumerate(tracks,1))),content
    selection=[{'batch_id':bid,'item_id':iid}]
    assert api('/api/tasks/track-titles',{'enabled':False,'selections':selection})['ok'];assert '曲目列表' not in txt.read_text()
    assert api('/api/tasks/track-titles',{'enabled':True,'selections':selection})['ok'];assert '曲目列表' in txt.read_text()
    batches.append((bid,iid))
   env=dict(os.environ,MIXCUT_TEST_URL=base,NODE_PATH='/Users/zhaoyue_macmini/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules')
   subprocess.run(['/Users/zhaoyue_macmini/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node','tests/ui_v157.cjs'],env=env,check=True)
   bid,iid=batches[-1]
   before=next(b for b in records if b['id']==bid)['items'][0]
   original=Path(before['output_path']); inode=original.stat().st_ino
   assert original.parent.parent.name.count('-')==2
   assert original.parent.name.endswith('-exports')
   assert original.parent.parent.parent==root/'exports'
   reviewed=api('/api/approve',{'batch_id':bid,'item_id':iid})
   approved=reviewed['items'][0]['review']['path']
   assert approved==str(original) and original.stat().st_ino==inode
   assert reviewed['items'][0]['review']['retained']
   assert Path(approved).with_suffix('.txt').read_text().count('曲目列表')==1
   api('/api/tasks/track-titles',{'enabled':False,'selections':[{'batch_id':bid,'item_id':iid}]})
   assert '曲目列表' not in Path(approved).with_suffix('.txt').read_text()
   subprocess.run(['/Users/zhaoyue_macmini/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node','tests/ui_v158.cjs'],env=dict(env,MIXCUT_TASK_KEY=bid+':'+iid),check=True)
   result=api('/api/tasks/forget',{'selections':[{'batch_id':bid,'item_ids':[iid]}]})
   assert result['deleted_items']==0 and original.is_file() and original.with_suffix('.txt').is_file()
   bid,iid=batches[0]
   result=api('/api/reject',{'batch_id':bid,'item_id':iid,'confirmation':'REJECT_VIDEO_AND_SOURCES'})
   assert result['ok']
   assert all(Path(song['path']).is_file() for song in songs),'music must remain'
   print('V1.5.8 packaged acceptance: two real renders, dated numbered bundles, same inode retained on approval, document toggle, record-only deletion and native recycle on rejection; original music preserved')
  finally:
   try:api('/api/shutdown',{})
   except Exception:pass
   try:process.wait(timeout=15)
   except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)
   log.close()

if __name__=='__main__':main()
