import concurrent.futures
from datetime import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from mixcut.server import Application
from mixcut.naming import review_folder_name

class ReviewFolderNames(unittest.TestCase):
    def test_daily_sequence_ignores_old_names_and_other_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'任意目录'; root.mkdir()
            for name in ['001-旧名称', '261022-999', '261023-001', '261023-003']:
                (root/name).mkdir()
            (root/'261023-900').write_text('not a directory')
            with patch('mixcut.server.datetime') as clock:
                clock.now.return_value.astimezone.return_value = datetime(2026,10,23)
                folder, name = Application.reserve_review_bundle(root, {'music_styles':['Hindi']})
                other, _ = Application.reserve_review_bundle(root, {}, label='不应出现在名称里')
            self.assertEqual('261023-004', folder.name)
            self.assertEqual('Hindi_004', name)
            self.assertEqual('261023-005', other.name)
            self.assertEqual(root, folder.parent)

    def test_concurrent_reservations_are_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'archive'
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                folders=list(pool.map(lambda _:Application.reserve_review_bundle(root,{})[0],range(12)))
            self.assertEqual(12,len(set(folders)))
            day=datetime.now().astimezone().strftime('%y%m%d')
            self.assertEqual({f'{day}-{n:03d}' for n in range(1,13)},{f.name for f in folders})

    def test_year_and_four_digit_sequence(self):
        self.assertEqual('261023-001',review_folder_name('261023',1))
        self.assertEqual('270101-1000',review_folder_name('270101',1000))

    def test_next_day_restarts_and_existing_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'261023-001').mkdir()
            (root/'261024-001').write_text('keep')
            with patch('mixcut.server.datetime') as clock:
                clock.now.return_value.astimezone.return_value=datetime(2026,10,24)
                folder,_=Application.reserve_review_bundle(root,{})
            self.assertEqual('261024-002',folder.name)
            self.assertEqual('keep',(root/'261024-001').read_text())
