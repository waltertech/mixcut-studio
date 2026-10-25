"""Exercise the actual packaged app and page against generated, disposable media."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen


def main():
    executable=sys.argv[1]
    with tempfile.TemporaryDirectory(prefix='mixcut170-acceptance-') as tmp:
        root=Path(tmp);source=root/'音乐';source.mkdir();setdir=root/'Set';setdir.mkdir()
        def ff(*args):subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True)
        songs=[]
        for n in range(1,4):
            p=source/f'{n:02d} Hindi Song.mp3';ff('-f','lavfi','-i',f'sine=frequency={300+n*100}:duration=22','-c:a','libmp3lame',str(p));songs.append(p)
        motion=setdir/'动态封面_10秒.mp4';ff('-f','lavfi','-i','testsrc2=size=320x180:rate=30:duration=10','-c:v','libx264','-pix_fmt','yuv420p',str(motion))
        cover=setdir/'新封面.png';ff('-i',str(motion),'-frames:v','1',str(cover))
        manifest=setdir/'YPP交接.txt'
        blocks=[]
        for n,p in enumerate(songs,1):blocks.append(f'[曲目 {n:02d}]\n名称：Hindi Song {n}\n音频路径：{p}\n原编号：{n}\n起始秒：{(n-1)*22}\n时长秒：22\nSHA256：{hashlib.sha256(p.read_bytes()).hexdigest()}')
        manifest.write_text(f'YPP音乐交接 TXT v1\n状态：已完成\nSet ID：package-set\n音乐来源原路径：{source}\n静态封面路径：{cover}\n动态封面路径：{motion}\n曲目总数：3\n===== 播放顺序开始 =====\n'+ '\n\n'.join(blocks)+'\n===== 播放顺序结束 =====\n===== 视频文案开始 =====\nEnglish title: Test\nHindi title: संगीत\n===== 视频文案结束 =====\n')
        parent = root/'Sets'; parent.mkdir()
        import shutil
        shutil.move(setdir, parent/'Set 1')
        setdir = parent/'Set 1'
        for n in (2, 3):
            destination = parent/f'Set {n}'
            shutil.copytree(setdir, destination)
            doc = destination/'YPP交接.txt'
            doc.write_text(doc.read_text().replace('Set ID：package-set', f'Set ID：package-set-{n}'))
        manifest = setdir/'YPP交接.txt'
        motion = setdir/motion.name
        cover = setdir/cover.name
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        def api(path,body=None):
            request=Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'})
            with urlopen(request,timeout=15) as response:return json.load(response)
        with (root/'server.log').open('w') as log:
            process=subprocess.Popen([executable,'--server','--port',str(port),'--state-dir',str(root/'state')],stdout=log,stderr=log)
            try:
                for _ in range(100):
                    try:boot=api('/api/bootstrap');break
                    except OSError:time.sleep(.2)
                else:raise AssertionError('Packaged server failed to start')
                assert boot['version']=='1.8.14' and boot['api_protocol']==17
                env=dict(os.environ,NODE_PATH=str(Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'),MIXCUT_TEST_URL=base,MIXCUT_LOOP_SET=str(parent),MIXCUT_LOOP_OUTPUT=str(root/'outputs'),MIXCUT_LOOP_SCREENSHOT='/tmp/mixcut170-page.png')
                subprocess.run([str(Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'),'tests/ui_loopmusic.cjs'],env=env,check=True)
                output=next((root/'outputs').rglob('成品视频.mp4'))
                ff('-xerror','-i',str(output),'-f','null','-')
                assert all(p.is_file() for p in songs+[motion,cover,manifest])
                assert api('/api/batches')==[]
                assert api('/api/music-loop')['tasks']==[]
                assert len(list((root/'outputs').rglob('成品视频.mp4')))==3
                assert output.is_file() and (output.parent/'发布文案.txt').is_file()
                print('Packaged three concurrent music-led exports completed; one fully decoded; approval retained output, record deletion preserved all files, old task store stayed empty.')
            finally:
                try:api('/api/shutdown',{})
                except Exception:pass
                try:process.wait(timeout=15)
                except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)

if __name__=='__main__':main()
