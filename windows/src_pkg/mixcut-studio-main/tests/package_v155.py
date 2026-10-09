"""Run real render/API acceptance against a supplied frozen executable in isolated state."""
import json, os, socket, subprocess, sys, tempfile, time
from pathlib import Path
from urllib.request import Request, urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mixcut import media
from mixcut.server import Store


def main():
 executable=sys.argv[1]
 with tempfile.TemporaryDirectory(prefix='mixcut155-package-') as tmp:
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
   assert boot['version']=='1.5.5' and boot['api_protocol']==12
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
   bid,iid=batches[-1]
   reviewed=api('/api/approve',{'batch_id':bid,'item_id':iid,'review_dir':str(root/'approved')})
   approved=reviewed['items'][0]['review']['path'];assert Path(approved).with_suffix('.txt').read_text().count('曲目列表')==1
   api('/api/tasks/track-titles',{'enabled':False,'selections':[{'batch_id':bid,'item_id':iid}]})
   assert '曲目列表' not in Path(approved).with_suffix('.txt').read_text()
   assert all(Path(song['path']).is_file() for song in songs),'music must remain'
   print('V1.5.5 packaged acceptance: folder + pool render, thumbnails, actual ordered TXT, toggle, approval archive; music preserved')
  finally:
   try:api('/api/shutdown',{})
   except Exception:pass
   try:process.wait(timeout=15)
   except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)
   log.close()

if __name__=='__main__':main()
