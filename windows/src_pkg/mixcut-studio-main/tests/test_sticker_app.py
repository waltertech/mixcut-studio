"""Durable sticker catalog, template, and review-variant application tests."""
from __future__ import annotations

import base64
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import mixcut
from mixcut import server


# A valid one-pixel PNG.  Importing it exercises the real static-image normalizer.
PIXEL_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAACXBIWXMAAAABAAAAAQBPJcTWAAAAEElEQVR4nGP8wwACLGCSAQANBAECv1AVswAAAABJRU5ErkJggg=='
)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg and ffprobe are required')
class StickerApplicationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.app = server.Application(self.root / 'state')
        self.output_root = self.root / 'exports'

    def tearDown(self):
        self.app.closing.set()
        self.temporary.cleanup()

    def imported_asset(self):
        catalog = self.app.import_sticker({
            'name': 'pixel.png',
            'data': base64.b64encode(PIXEL_PNG).decode('ascii'),
        })
        self.assertEqual(1, len(catalog['assets']))
        asset = catalog['assets'][0]
        self.assertTrue(Path(asset['path']).is_file())
        self.assertTrue(Path(asset['path']).is_relative_to((self.root / 'state' / 'stickers').resolve()))
        return asset

    @staticmethod
    def layer(asset, x=.1):
        return {'sticker_id': asset['id'], 'x': x, 'y': .2, 'width': .3,
                'opacity': .8, 'start': .1, 'end': None}

    def save_template(self, asset, name='corner', template_id=None, x=.1):
        body = {'name': name, 'layers': [self.layer(asset, x)]}
        if template_id:
            body['id'] = template_id
        catalog = self.app.save_sticker_template(body)
        return catalog['templates'][0]

    def test_imported_sticker_and_template_persist_as_managed_catalog_entries(self):
        asset = self.imported_asset()
        template = self.save_template(asset)

        reopened = server.Application(self.root / 'state')
        catalog = reopened.sticker_catalog()

        self.assertEqual(asset['id'], catalog['assets'][0]['id'])
        self.assertEqual(template['id'], catalog['templates'][0]['id'])
        self.assertEqual(self.layer(asset), catalog['templates'][0]['layers'][0])
        self.assertEqual(catalog, reopened.bootstrap()['sticker_catalog'])

    def test_template_edits_do_not_mutate_already_planned_layer_snapshots(self):
        asset = self.imported_asset()
        template = self.save_template(asset, x=.1)
        self.app.store.put('library', {'scan': {'videos': [{'id': 'v'}], 'music': [{'id': 'm'}]}})
        fake_planner = types.SimpleNamespace(plan=lambda videos, music, config: {
            'items': [{'duration': 1, 'segments': [], 'music': []}], 'stats': {}, 'warnings': [],
        })

        def create(template_id):
            config = {'count': 1, 'width': 640, 'height': 360, 'fps': 30,
                      'output_dir': str(self.output_root), 'sticker_template_id': template_id}
            with (patch.dict(sys.modules, {'mixcut.planner': fake_planner}),
                  patch.object(mixcut, 'planner', fake_planner, create=True)):
                return self.app.create_plan({'config': config})

        before = create(template['id'])
        no_sticker = create(None)
        self.save_template(asset, template_id=template['id'], x=.7)
        after = create(template['id'])

        self.assertEqual(.1, before['config']['sticker_layers'][0]['x'])
        self.assertEqual(.7, after['config']['sticker_layers'][0]['x'])
        self.assertEqual([], no_sticker['config']['sticker_layers'])
        self.assertIsNone(no_sticker['config']['sticker_template'])

    def source_batch(self, source):
        stat = source.stat()
        batch = {
            'id': 'original01', 'status': 'completed', 'created_at': 1, 'updated_at': 1,
            'config': {'output_dir': str(self.output_root), 'width': 640, 'height': 360, 'fps': 30},
            'output_folder': str(source.parent), 'folder_name': source.parent.name,
            'items': [{
                'id': 'item01', 'status': 'success', 'duration': 1, 'output_path': str(source),
                'progress': 1, 'result': {'output_size': stat.st_size, 'output_mtime_ns': stat.st_mtime_ns},
                'review': {'status': 'approved', 'path': str(self.root / 'review-copy.mp4')},
            }], 'assets': [],
        }
        self.app.store.put('batch:' + batch['id'], batch)
        return batch

    def test_variant_queues_in_its_own_dated_folder_without_changing_original_review(self):
        asset = self.imported_asset()
        template = self.save_template(asset)
        source = self.output_root / 'original' / '001.mp4'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'original-output-not-to-be-modified')
        original = self.source_batch(source)

        reply = self.app.create_sticker_variant({
            'batch_id': original['id'], 'item_id': 'item01', 'template_id': template['id'],
        })
        job = self.app.batch(reply['job_id'])
        job_item = job['items'][0]

        self.assertEqual('queued', reply['status'])
        self.assertEqual('sticker_variant', job_item['kind'])
        self.assertEqual([], job_item['segments'])
        self.assertEqual([], job_item['music'])
        self.assertEqual(source.read_bytes(), b'original-output-not-to-be-modified')
        self.assertEqual({'status': 'approved', 'path': str(self.root / 'review-copy.mp4')},
                         self.app.batch(original['id'])['items'][0]['review'])
        self.assertEqual(template['id'], job['config']['sticker_template_id'])
        self.assertEqual(asset['id'], job['config']['sticker_layers'][0]['sticker_id'])
        self.assertRegex(Path(job['output_folder']).name, r'^\d{4}-\d{2}-\d{2}_\d{3}$')
        self.assertEqual(Path(job['output_folder']) / '001.mp4', Path(job_item['output_path']))
        self.assertEqual(job['id'], reply['output_batch_id'])

    def test_invalid_template_or_source_and_tampered_completed_file_are_rejected(self):
        asset = self.imported_asset()
        with self.assertRaisesRegex(ValueError, '贴纸不存在'):
            self.app.save_sticker_template({'name': 'invalid', 'layers': [self.layer({'id': 'missing'})]})
        with self.assertRaisesRegex(ValueError, '模板不存在'):
            self.app.create_sticker_variant({'batch_id': 'missing', 'item_id': 'nope', 'template_id': 'missing'})

        template = self.save_template(asset)
        source = self.output_root / 'original' / '001.mp4'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'original')
        original = self.source_batch(source)
        source.write_bytes(b'tampered-output-with-a-different-size')

        with self.assertRaisesRegex(ValueError, '已发生变化'):
            self.app.create_sticker_variant({
                'batch_id': original['id'], 'item_id': 'item01', 'template_id': template['id'],
            })
        with self.assertRaisesRegex(ValueError, '需要更新.*不存在'):
            self.app.save_sticker_template({'id': 'missing', 'name': 'bad', 'layers': [self.layer(asset)]})


if __name__ == '__main__':
    unittest.main()
