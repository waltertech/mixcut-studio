from __future__ import annotations

import subprocess
import tempfile
import unittest
import hashlib
from pathlib import Path

from mixcut import renderer, stickers


def run(command):
    subprocess.run(command, check=True, capture_output=True)


class StickerRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.png = self.root / 'red.png'
        self.jpg = self.root / 'blue.jpg'
        run(['ffmpeg', '-y', '-f', 'lavfi', '-i', 'color=red:s=20x10', '-frames:v', '1', str(self.png)])
        run(['ffmpeg', '-y', '-f', 'lavfi', '-i', 'color=blue:s=10x20', '-frames:v', '1', str(self.jpg)])
        self.source = self.root / 'source.mp4'
        run(['ffmpeg', '-y', '-f', 'lavfi', '-i', 'color=black:s=100x100:r=30:d=2', '-f', 'lavfi', '-i', 'sine=frequency=400:d=2', '-shortest', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(self.source)])

    def tearDown(self):
        self.temp.cleanup()

    def test_import_normalizes_png_and_jpeg(self):
        one = stickers.import_image(self.png.read_bytes(), 'red.png', self.root / 'assets')
        two = stickers.import_image(self.jpg.read_bytes(), 'blue.jpg', self.root / 'assets')
        self.assertTrue(one['path'].endswith('.png'))
        self.assertTrue(Path(two['path']).is_file())
        self.assertEqual((20, 10), (one['width_px'], one['height_px']))

    def test_layers_validate_geometry_and_client_path_is_ignored(self):
        asset = stickers.import_image(self.png.read_bytes(), 'red', self.root / 'assets')
        layer = stickers.resolve_layers([{'sticker_id': asset['id'], 'path': '/untrusted', 'x': .9, 'y': .9, 'width': .5}], [asset])[0]
        self.assertEqual(asset['path'], layer['path'])
        self.assertEqual([], stickers.resolve_layers([], [asset]))
        with self.assertRaises(ValueError):
            stickers.resolve_layers([{'sticker_id': asset['id'], 'width': .001}], [asset])
        with self.assertRaises(ValueError):
            stickers.import_image(b'not-an-image', 'bad', self.root / 'assets')
        with self.assertRaises(ValueError):
            stickers.resolve_layers([{'sticker_id': asset['id'], 'x': 1}], [asset])

    def test_overlay_location_transparency_timing_and_audio(self):
        asset = stickers.import_image(self.png.read_bytes(), 'red', self.root / 'assets')
        layers = stickers.resolve_layers([{'sticker_id': asset['id'], 'x': 0, 'y': 0, 'width': .2, 'opacity': .5, 'start': 1, 'end': 2}], [asset])
        original = self.source.read_bytes()
        output = self.root / 'variant.mp4'
        result = renderer.overlay_existing(self.source, layers, output, self.root / 'work')
        self.assertTrue(result['valid'])
        self.assertEqual(original, self.source.read_bytes())
        original_audio = float(next(stream['duration'] for stream in renderer._probe(self.source)['streams'] if stream['codec_type'] == 'audio'))
        self.assertAlmostEqual(result['audio_duration'], original_audio, places=2)
        def pixel(time):
            data = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(time), '-i', str(output), '-frames:v', '1', '-vf', 'crop=2:2:0:0', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], capture_output=True, check=True).stdout
            return tuple(data[:3])
        self.assertLess(sum(pixel(.4)), 25)
        red = pixel(1.2)
        self.assertGreater(red[0], 60)
        self.assertGreater(red[0], red[1] * 2)

    def test_generation_render_applies_layer_and_rejects_tamper(self):
        asset = stickers.import_image(self.png.read_bytes(), 'red', self.root / 'assets')
        layer = stickers.resolve_layers([{'sticker_id': asset['id'], 'x': 0, 'y': 0, 'width': .2}], [asset])
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        source_asset = {'id': digest, 'path': str(self.source), 'name': 'source', 'size': self.source.stat().st_size,
                        'mtime_ns': self.source.stat().st_mtime_ns, 'duration': 2, 'has_audio': True}
        item = {'id': 'one', 'duration': 2, 'segments': [{'asset_id': digest, 'path': str(self.source), 'start': 0, 'duration': 2}],
                'video_assets': [source_asset], 'music': [source_asset]}
        output = self.root / 'generated.mp4'
        renderer.render(item, {'width': 100, 'height': 100, 'fps': 30, 'hardware': 'software', 'sticker_layers': layer}, output, self.root / 'work')
        data = subprocess.run(['ffmpeg', '-v', 'error', '-ss', '1', '-i', str(output), '-frames:v', '1', '-vf', 'crop=2:2:0:0', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], capture_output=True, check=True).stdout
        self.assertGreater(data[0], 100)
        Path(asset['path']).write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, '已变化'):
            stickers.add_overlay_filters([], [], layer, 0, 'v', 100, 100, 30, 2)

    def test_empty_layers_returns_original_metadata(self):
        result = renderer.overlay_existing(self.source, [], self.root / 'unused.mp4', self.root / 'work')
        self.assertEqual(str(self.source.resolve()), result['path'])


if __name__ == '__main__':
    unittest.main()
