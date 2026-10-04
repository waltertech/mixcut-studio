"""Folder isolation, balanced allocation, bounded repeats and render integration."""
import copy
import random
import shutil
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from mixcut import foldermusic, media, planner, renderer
from mixcut.nonstop import MusicEdges
from mixcut.server import Application


def song(root, folder, name, duration=12):
    return {'id':folder+name, 'name':name, 'path':str(root/folder/name),
            'duration':duration, 'mime_type':'audio/mpeg'}


class FolderPlanningTests(unittest.TestCase):
    def test_nested_groups_root_files_invalid_durations_and_path_identity(self):
        root=Path('/music')
        songs=[song(root,'A','a.mp3'),song(root,'A/nested','b.flac'),song(root,'B','a.mp3'),
               song(root,'','loose.m4a'),song(root,'C','zero.mp3',0),song(root,'C','nan.mp3',float('nan'))]
        groups=foldermusic.groups(songs+[songs[0]],root)
        self.assertEqual({'/music/A','/music/B'},set(groups))
        self.assertEqual(2,len(groups['/music/A']))
        self.assertEqual(3,sum(len(v) for v in groups.values()))

    def test_rounds_include_all_songs_no_boundary_repeat_and_no_length_bias(self):
        songs=[song(Path('/music'),'A',str(i),10+i) for i in range(3)]
        order,total=foldermusic.allocate(songs,100,{},random.Random(4))
        self.assertGreaterEqual(total,100)
        for offset in range(0,len(order),3):
            self.assertEqual({s['id'] for s in songs},{s['id'] for s in order[offset:offset+3]})
            if offset:self.assertNotEqual(order[offset-1]['id'],order[offset]['id'])
        groups={'short':[songs[0]],'long':[dict(songs[0],duration=900)]}
        usage=Counter();rng=random.Random(2)
        for _ in range(101):usage[foldermusic.choose_folder(groups,usage,rng)]+=1
        self.assertLessEqual(max(usage.values())-min(usage.values()),1)

    def test_plan_10_tasks_6_folders_and_count_semantics(self):
        root=Path('/music');songs=[song(root,str(n),'a.mp3',3) for n in range(6)]
        videos=[{'id':str(n),'path':'/v/'+str(n),'duration':200} for n in range(10)]
        config={'music_mode':'folder','music_root':str(root),'count':10,'min_duration':10,
                'max_duration':10,'mode':'single','allow_overlap':True,'min_songs':100,'max_songs':0,'seed':7}
        with patch('subprocess.run',side_effect=AssertionError('planning must not decode')):
            result=planner.plan(videos,songs,config)
        counts=Counter(i['music_folder'] for i in result['items'])
        self.assertEqual(6,len(counts));self.assertEqual({1,2},set(counts.values()))
        for item in result['items']:
            self.assertEqual(counts[item['music_folder']],item['music_folder_use_count'])
            self.assertEqual(4,item['music_play_count']);self.assertEqual(4,item['music_rounds'])
            self.assertTrue(all(str(Path(s['path']).parent)==item['music_folder'] for s in item['music']))
        previous=result['items'][:2]; config['count']=4
        more=planner.plan(videos,songs,config,previous_items=previous)
        self.assertEqual(6,len({i['music_folder'] for i in previous+more['items']}))

    def test_no_groups_and_runaway_cycles_fail_promptly(self):
        self.assertEqual({},foldermusic.groups([song(Path('/music'),'','root.mp3')],'/music'))
        with self.assertRaises(ValueError):foldermusic.allocate([song(Path('/music'),'A','a',.2)],1e8,{},random.Random())


class FolderActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.app=Application(self.root/'state')
        self.songs=[song(self.root/'music',str(n),name,12) for n in range(4) for name in ['a.mp3','b.flac']]
        self.videos=[{'id':'v'+str(n),'path':str(self.root/f'v{n}.mp4'),'duration':120,'mime_type':'video/mp4'} for n in range(5)]
        self.app.store.put('library',{'music_dir':str(self.root/'music'),'scan':{'videos':self.videos,'music':self.songs}})
        self.batch=self.app.create_plan({'config':{'music_mode':'folder','count':3,'min_duration':10,'max_duration':20,
                       'output_dir':str(self.root/'exports'),'video_ids':[v['id'] for v in self.videos],'allow_overlap':True,
                       'width':640,'height':360,'fps':24,'seed':2}})
        self.bid=self.batch['id'];self.iid=self.batch['items'][0]['id']

    def test_refresh_and_manual_folder_change_counts_persist_and_invalid_change_atomic(self):
        original=self.batch['items'][0]['music_folder']
        refreshed=self.app.refresh_music(self.bid,self.iid)
        self.assertEqual(original,refreshed['items'][0]['music_folder'])
        used={i['music_folder'] for i in refreshed['items']}
        changed=self.app.change_music_folder(self.bid,self.iid,{'random':True})
        self.assertNotIn(changed['items'][0]['music_folder'],used)
        other=changed['items'][1]['music_folder']
        changed=self.app.change_music_folder(self.bid,self.iid,{'folder':other})
        self.assertEqual(2,changed['items'][0]['music_folder_use_count'])
        self.assertEqual(2,changed['items'][1]['music_folder_use_count'])
        before=copy.deepcopy(changed)
        with self.assertRaises(ValueError):self.app.change_music_folder(self.bid,self.iid,{'folder':'/elsewhere'})
        self.assertEqual(before,self.app.batch(self.bid))
        restarted=Application(self.root/'state')
        self.assertEqual(other,restarted.batch(self.bid)['items'][0]['music_folder'])

    def test_single_song_video_replacement_and_failed_music_never_escape_folder(self):
        original=self.batch['items'][0]['music_folder']
        changed=self.app.replace_media(self.bid,self.iid,{'kind':'music','index':0})
        self.assertTrue(all(Path(s['path']).is_relative_to(original) for s in changed['items'][0]['music']))
        changed=self.app.replace_media(self.bid,self.iid,{'kind':'video','index':0,'replan_duration':True})
        item=changed['items'][0]
        self.assertEqual(original,item['music_folder'])
        self.assertGreaterEqual(item['music_total_duration'],item['duration'])
        bad=item['music'][0]['id']
        changed=self.app._replace_failed_music(self.bid,0,[bad]);item=changed['items'][0]
        self.assertTrue(all(s['id']!=bad and Path(s['path']).is_relative_to(original) for s in item['music']))
        good_ids=list({s['id'] for s in item['music']})
        with self.assertRaises(ValueError):self.app._replace_failed_music(self.bid,0,good_ids)

    def test_repeated_ids_can_be_reordered_and_running_task_cannot_change_folder(self):
        item=self.batch['items'][0];order=[s['id'] for s in item['music']]*2
        self.app.store.update(self.bid,lambda b:b['items'][0].update(music=b['items'][0]['music']*2))
        result=self.app.reorder_music(self.bid,self.iid,list(reversed(order)))
        self.assertEqual(list(reversed(order)),[s['id'] for s in result['items'][0]['music']])
        self.app.store.update(self.bid,lambda b:b.update(status='running'))
        with self.assertRaises(ValueError):self.app.change_music_folder(self.bid,self.iid,{'random':True})

    def test_effective_audio_appends_rounds_detects_once_and_skips_unused_suffix(self):
        a,b,c=self.songs[:3]
        item={'duration':20,'music_folder':'fixture','music_folder_song_count':2,'music':[a,b]}
        with patch.object(self.app.music_edges,'detect',return_value={'start':0,'end':6,'effective_duration':6}) as detect:
            prepared=self.app.music_edges.prepare(item)
        self.assertEqual(20,prepared['duration']);self.assertEqual(4,len(prepared['music']))
        self.assertEqual(2,detect.call_count)
        item.update(duration=2,music=[a,b,c])
        with patch.object(self.app.music_edges,'detect',return_value={'start':0,'end':6,'effective_duration':6}) as detect:
            prepared=self.app.music_edges.prepare(item)
        self.assertEqual(1,len(prepared['music']));self.assertEqual(1,detect.call_count)

    def test_new_folder_and_refresh_http_routes(self):
        import json
        import threading
        from urllib.request import Request, urlopen
        from mixcut.server import Handler, ThreadingHTTPServer
        httpd=ThreadingHTTPServer(('127.0.0.1',0),Handler);httpd.app=self.app
        worker=threading.Thread(target=httpd.serve_forever,daemon=True);worker.start()
        try:
            for route,body in [('music-folder',{'random':True}),('refresh-music',{})]:
                request=Request(f'http://127.0.0.1:{httpd.server_port}/api/batches/{self.bid}/items/{self.iid}/{route}',
                    data=json.dumps(body).encode(),headers={'Content-Type':'application/json'},method='POST')
                with urlopen(request,timeout=5) as response: result=json.load(response)
                self.assertEqual(self.bid,result['id']);self.assertIn('music_folder',result['items'][0])
        finally:
            httpd.shutdown();worker.join(timeout=2);httpd.server_close()

    def test_deleted_music_is_replaced_during_render_without_cross_folder_fallback(self):
        batch=self.batch;original=batch['items'][0]['music_folder']
        self.app.store.update(self.bid,lambda b:(b.update(status='running'),b['config'].update(nonstop_music=False)))
        seen=[]
        def render(item,config,output,work,progress):
            seen.append([s['id'] for s in item['music']])
            if len(seen)==1:raise renderer.MusicInputError('fixture missing audio',[item['music'][0]['id']])
            self.assertTrue(all(Path(s['path']).is_relative_to(original) for s in item['music']))
            Path(output).write_bytes(b'output');return {'valid':True}
        with patch.object(renderer,'render',side_effect=render):self.app.execute_item(self.bid,0)
        result=self.app.batch(self.bid)['items'][0]
        self.assertEqual('success',result['status'],result.get('error'))
        self.assertEqual(2,len(seen));self.assertNotIn(seen[0][0],seen[1])


@unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
class FolderRenderTests(unittest.TestCase):
    def test_real_cycle_render_thumbnails_review_and_music_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();folder=root/'music'/'DJ';folder.mkdir(parents=True);video=root/'v.mp4'
            def ff(*args):subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True,timeout=45)
            ff('-f','lavfi','-i','color=c=blue:s=640x360:r=24:d=6','-an','-c:v','libx264',str(video))
            songs=[]
            for n,freq in enumerate([440,880]):
                audio=folder/f'{n}.wav';ff('-f','lavfi','-i',f'sine=frequency={freq}:duration=2','-af','adelay=300:all=1,apad=pad_dur=0.3',str(audio))
                songs.append(media._asset(audio,'music',quick_music=True))
            v=media._asset(video,'video');app=Application(root/'state')
            app.store.put('library',{'video_dir':str(root),'music_dir':str(root/'music'),'scan':{'videos':[v],'music':songs}})
            batch=app.create_plan({'config':{'music_mode':'folder','nonstop_music':True,'count':1,
                      'min_duration':5,'max_duration':5,'width':640,'height':360,'fps':24,
                      'hardware':'software_fast','output_dir':str(root/'output'),'allow_overlap':True}})
            app.store.update(batch['id'],lambda b:b.update(status='running'))
            app.execute_item(batch['id'],0)
            item=app.batch(batch['id'])['items'][0]
            self.assertEqual('success',item['status'],item.get('error'));self.assertAlmostEqual(5,item['duration'],places=1)
            self.assertGreaterEqual(len(item['music']),3)
            app.start_preview_worker();app.preview_queue.join()
            self.assertEqual('ready',app.batch(batch['id'])['items'][0]['thumbnails']['status'])
            app.approve({'batch_id':batch['id'],'item_id':item['id'],'review_dir':str(root/'approved')})
            self.assertTrue(all(Path(s['path']).exists() for s in songs))


if __name__=='__main__':unittest.main()
