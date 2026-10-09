import tempfile
import unittest
from pathlib import Path
from mixcut.tracklist import played_music, sidecar_content, for_item
from mixcut.server import Application


class TracklistTests(unittest.TestCase):
    def song(self, title, duration=10):
        return {'id':title,'path':'/音乐/'+title,'duration':duration}

    def test_order_repeats_partial_and_unused_suffix(self):
        item={'music':[self.song('E.m4a'),self.song('D.live.flac'),self.song('E.m4a'),self.song('A.mp3')]}
        tracks=played_music(item,25)
        self.assertEqual(['E','D.live','E'],[x['title'] for x in tracks])
        self.assertEqual([10,10,5],[x['duration'] for x in tracks])
        self.assertEqual(2,len(played_music(item,20)))

    def test_trim_and_crossfade_timeline(self):
        songs=[self.song('一.mp3'),self.song('二.mp3'),self.song('三.mp3')]
        for song in songs: song['nonstop_edges']={'effective_duration':8}
        item={'music':songs,'nonstop':{'crossfades':[.5,.5]}}
        tracks=played_music(item,15.2)
        self.assertEqual([0,7.5,15],[x['start'] for x in tracks])
        self.assertAlmostEqual(.2,tracks[-1]['duration'])

    def test_sidecar_retains_paths_and_authoritative_snapshot(self):
        item={'music':[self.song('计划.mp3')],'music_source_folders':['/音乐'], 'duration':10,
              'include_track_titles':True,'result':{'played_music':[{'title':'实际曲目'}]}}
        self.assertEqual('/音乐\n\n曲目列表\n1. 实际曲目\n',sidecar_content(item))
        item['include_track_titles']=False
        self.assertEqual('/音乐\n',sidecar_content(item))
        item['result']['played_music']=[]
        self.assertEqual([],for_item(item))

    def test_bulk_pending_and_completed_and_archive_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); app=Application(root/'state')
            try:
                video=root/'成片.mp4';video.write_bytes(b'keep video')
                archived=root/'审核.mp4';archived.write_bytes(b'archive')
                entries=[{'id':'pending','status':'pending','music':[]},
                         {'id':'done','status':'success','output_path':str(video),'music_source_folders':['/音乐'],
                          'result':{'played_music':[{'title':'中文 曲目'}, {'title':'中文 曲目'}]}},
                         {'id':'archive','status':'success','review':{'status':'approved','path':str(archived)},
                          'music_source_folders':['/音乐'],'result':{'played_music':[{'title':'归档曲目'}]}}]
                app.store.put('batch:test',{'id':'test','items':entries,'status':'draft','config':{}})
                body={'enabled':True,'selections':[{'batch_id':'test','item_id':x['id']} for x in entries]}
                result=app.set_track_titles(body)
                self.assertTrue(result['ok']);self.assertEqual(3,result['updated'])
                self.assertIn('1. 中文 曲目\n2. 中文 曲目',video.with_suffix('.txt').read_text())
                self.assertIn('1. 归档曲目',archived.with_suffix('.txt').read_text())
                app.set_track_titles(body)
                self.assertEqual(1,video.with_suffix('.txt').read_text().count('曲目列表'))
                body['enabled']=False;app.set_track_titles(body)
                self.assertEqual('/音乐\n',video.with_suffix('.txt').read_text())
                self.assertEqual(b'keep video',video.read_bytes())
                self.assertFalse(app.batch('test')['items'][0]['include_track_titles'])
            finally:app.closing.set()

    def test_invalid_bulk_is_prevalidated(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=Application(Path(tmp)/'state')
            try:
                app.store.put('batch:test',{'id':'test','items':[{'id':'one','status':'running'}]})
                with self.assertRaises(ValueError):
                    app.set_track_titles({'enabled':True,'selections':[{'batch_id':'test','item_id':'one'},{'batch_id':'test','item_id':'missing'}]})
                self.assertNotIn('include_track_titles',app.batch('test')['items'][0])
            finally:app.closing.set()
