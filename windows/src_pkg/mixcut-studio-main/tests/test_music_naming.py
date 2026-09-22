from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mixcut.naming import export_filename, infer_music_style, music_styles
from mixcut.server import Application
from mixcut import media


class MusicNamingTests(unittest.TestCase):
    def test_three_style_roots_and_nested_albums(self):
        for style in ('Hindi DJ Remix', 'Hindi POP Songs', 'Old Hindi Songs'):
            root = Path('/music') / style
            self.assertEqual(style, infer_music_style(root / 'song.mp3', root))
            self.assertEqual(style, infer_music_style(root / 'album' / 'song.mp3', '/music'))
        self.assertEqual('Hindi POP Songs', infer_music_style('/music/hindi pop songs/a.mp3', '/music'))

    def test_other_styles_use_folder_name_not_audio_guessing(self):
        self.assertEqual('Jazz', infer_music_style('/music/Jazz/album/a.mp3', '/music'))
        self.assertEqual('去重歌曲03', infer_music_style('/data/去重歌曲03/a.mp3', '/data/去重歌曲03'))

    def test_multi_style_filename_and_same_style_dedup(self):
        songs = [{'path': '/music/Hindi DJ Remix/a.mp3'}, {'path': '/music/Hindi DJ Remix/b.mp3'},
                 {'path': '/music/Old Hindi Songs/c.mp3'}]
        styles = music_styles(songs, '/music')
        self.assertEqual(['Hindi DJ Remix', 'Old Hindi Songs'], styles)
        self.assertEqual('Hindi DJ Remix + Old Hindi Songs_003.mp4', export_filename(styles, 3))
        self.assertEqual('001.mp4', export_filename([], 1))

    def test_filename_sanitization_and_length(self):
        name = export_filename(['A:B/C*D?E|F"G<>', '中文' * 200], 1)
        self.assertTrue(name.endswith('_001.mp4'))
        self.assertFalse(any(c in name for c in '<>:"/\\|?*'))
        self.assertLessEqual(len(name.encode()), 200)

    def test_scan_tags_style_even_when_metadata_is_cached(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / 'Hindi DJ Remix'; folder.mkdir()
            song = folder / 'a.mp3'; song.touch()
            old = {'id': 'song', 'path': str(song), 'duration': 3}
            with patch('mixcut.media._cached_asset', return_value=old):
                result = media.scan(str(root / 'no-videos'), str(root), str(root / 'cache'))
            self.assertEqual('Hindi DJ Remix', result['music'][0]['music_style'])
            self.assertNotIn('music_style', old)

    def test_plan_names_new_files_and_review_preserves_basename(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            app = Application(root / 'state')
            song_path = root / 'music' / 'Hindi POP Songs' / 'song.mp3'
            app.store.put('library', {'music_dir': str(root / 'music'), 'scan': {
                'videos': [{'id': 'video', 'path': '/video', 'duration': 10}],
                'music': [{'id': 'song', 'path': str(song_path), 'duration': 3}]}})
            batch = app.create_plan({'config': {'count': 1, 'music_mode': 'fixed', 'output_dir': str(root / 'out')}})
            item = batch['items'][0]
            self.assertEqual('Hindi POP Songs_001.mp4', Path(item['output_path']).name)
            self.assertEqual(['Hindi POP Songs'], item['music_styles'])
            self.assertEqual('Hindi POP Songs', app.bootstrap()['scan']['music'][0]['music_style'])
            Path(item['output_path']).write_bytes(b'completed-video')
            app.store.update(batch['id'], lambda b: b['items'][0].update(status='success'))
            reviewed = app.approve({'batch_id': batch['id'], 'item_id': item['id'], 'review_dir': str(root / 'approved')})
            self.assertEqual('Hindi POP Songs_001.mp4', Path(reviewed['items'][0]['review']['path']).name)
            self.assertTrue(Path(item['output_path']).is_file())


if __name__ == '__main__':
    unittest.main()
