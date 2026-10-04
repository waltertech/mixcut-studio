"""Paired previews and explicit rejection without deleting reusable audio."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from mixcut.server import Application
from mixcut.keyframes import KeyframeCache


class RejectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.app = Application(self.root/'state')
        sources = self.root/'sources'; sources.mkdir()
        self.source = sources/'source.mp4'; self.source.write_bytes(b'video')
        self.music = self.root/'song.mp3'; self.music.write_bytes(b'audio')
        output = self.root/'exports'; output.mkdir()
        self.output = output/'out.mp4'; self.output.write_bytes(b'export')
        asset = {'id':'video','path':str(self.source),'mime_type':'video/mp4'}
        item = {'id':'one','index':1,'status':'success','output_path':str(self.output),
                'segments':[{'asset_id':'video','path':str(self.source),'start':0,'duration':10}],
                'video_assets':[asset],'music':[{'id':'song','path':str(self.music)}]}
        self.batch = {'id':'abc123','created_at':1,'status':'completed','output_folder':str(output),
                      'config':{'source_video_dir':str(sources),'output_dir':str(output)},'items':[item]}
        self.app.store.put('batch:abc123', self.batch)
        self.app.store.put('library', {'scan':{'videos':[asset], 'music':item['music']}})
        self.version = self.app.keyframes._version(self.output)
        folder = self.app.keyframes.root/self.version;folder.mkdir()
        for name in ('index.json','000000-thumb.jpg','000000-large.jpg'):
            (folder/name).write_bytes(b'preview')

    def reject(self):
        return self.app.reject({'batch_id':'abc123','item_id':'one','confirmation':'REJECT_VIDEO_AND_SOURCES'})

    def test_rejection_deletes_video_and_paired_previews_keeps_music_and_excludes_source(self):
        result = self.reject()
        self.assertTrue(result['ok'])
        self.assertFalse(self.output.exists()); self.assertFalse(self.source.exists())
        self.assertTrue(self.music.exists()); self.assertFalse((self.app.keyframes.root/self.version).exists())
        self.assertEqual([],self.app.visible_batches())
        # Reintroducing the same pathname cannot bring rejected media back into rotation.
        self.source.write_bytes(b'new video')
        self.assertEqual([],self.app.allowed_videos(self.batch['items'][0]['video_assets']))
        self.assertEqual([],self.app.bootstrap()['scan']['videos'])
        self.assertTrue(self.reject()['ok'])

    def test_cleanup_failure_keeps_task_and_can_retry(self):
        original = Path.unlink
        def denied(path, *args, **kwargs):
            if path == self.source:raise PermissionError('fixture denied')
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',denied): result=self.reject()
        self.assertFalse(result['ok']);self.assertTrue(self.source.exists());self.assertTrue(self.music.exists())
        self.assertEqual('reject_failed',self.app.visible_batches()[0]['items'][0]['review']['status'])
        self.assertTrue(self.reject()['ok']);self.assertFalse(self.source.exists())

    def test_confirmation_and_audio_protection_before_deletion(self):
        with self.assertRaises(ValueError): self.app.reject({'batch_id':'abc123','item_id':'one'})
        self.batch['items'][0]['music'].append({'id':'same','path':str(self.source)})
        self.app.store.put('batch:abc123',self.batch)
        with self.assertRaisesRegex(ValueError,'音乐'):self.reject()
        self.assertTrue(self.source.exists());self.assertTrue(self.output.exists())

    def test_other_pending_reference_does_not_block_explicit_rejection(self):
        other=json.loads(json.dumps(self.batch));other['id']='def456';other['items'][0]['id']='other'
        other['items'][0]['status']='pending';self.app.store.put('batch:def456',other)
        self.assertTrue(self.reject()['ok']);self.assertFalse(self.source.exists())
        self.assertEqual('pending',self.app.batch('def456')['items'][0]['status'])
        self.assertTrue(self.music.exists())

    def test_approved_output_cleanup_failure_remains_visible(self):
        self.app.store.update('abc123',lambda b:b['items'][0].update(review={'status':'approved'},cleanup={'output_deleted':False}))
        self.assertEqual(1,len(self.app.visible_batches()[0]['items']))
        self.app.store.update('abc123',lambda b:b['items'][0]['cleanup'].update(output_deleted=True))
        self.assertEqual([],self.app.visible_batches())


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
class PairedFrameTests(unittest.TestCase):
    def test_metadata_transition_does_not_reset_sampling_and_both_sizes_are_precomputed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def ff(*args):
                return subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*args],check=True,capture_output=True,timeout=30)
            for name,color,extra in [('one','red',[]),('two','blue',['-color_range','tv','-colorspace','bt709','-color_primaries','bt709','-color_trc','bt709'])]:
                ff('-f','lavfi','-i',f'color=c={color}:s=128x72:r=2:d=35','-c:v','libx264','-g','2',*extra,str(root/(name+'.mp4')))
            listing=root/'concat.txt';listing.write_text("file 'one.mp4'\nfile 'two.mp4'\n")
            video=root/'joined.mp4';ff('-f','concat','-safe','0','-i',str(listing),'-c','copy',str(video))
            cache=KeyframeCache(root/'cache');data=cache.prepare(video)
            self.assertEqual([0.,30.,60.],[f['time'] for f in data['frames']])
            self.assertTrue(cache.ready(data['version'],3))
            for index in range(3):
                for size in ('thumb','large'):
                    with patch('mixcut.keyframes.subprocess.run',side_effect=AssertionError('cached preview must not decode')):
                        image=cache.image(video,index,size)
                    rgb=ff('-i',str(image),'-vf','scale=1:1','-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','pipe:1').stdout
                    self.assertEqual(3,len(rgb))
                    self.assertGreater(rgb[0] if index<2 else rgb[2],200)

    def test_rejected_original_checkpoint_is_not_reused(self):
        from mixcut import media, renderer, checkpoints
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            assets=[]
            for name,color in [('original','red'),('replacement','blue')]:
                video=root/(name+'.mp4')
                subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',f'color=c={color}:s=128x72:r=30:d=10',
                                '-c:v','libx264',str(video)],check=True,capture_output=True)
                assets.append(media._asset(video,'video'))
            segment={'asset_id':assets[0]['id'],'path':assets[0]['path'],'start':0,'duration':4}
            item={'duration':4,'segments':[segment],'video_assets':[assets[0]]}
            config={'width':128,'height':72,'fps':30,'hardware':'software','video_candidates':assets}
            with patch.object(checkpoints,'CHUNK_SECONDS',2):
                checkpoints.prepare(item,config,root/'work')
                config['video_source_allowed']=lambda seg:seg['asset_id']!=assets[0]['id']
                config['video_candidates']=[assets[1]]
                _,_,state=checkpoints.prepare(item,config,root/'work')
                chunks=state['chunks']
            self.assertEqual({assets[1]['id']},{chunk['segment']['asset_id'] for chunk in chunks})
