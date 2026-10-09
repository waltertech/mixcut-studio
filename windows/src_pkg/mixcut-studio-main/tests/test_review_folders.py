import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from mixcut.server import Application
from mixcut.naming import review_folder_name


class ReviewFolderNames(unittest.TestCase):
    def test_directory_name_and_number_across_styles_and_existing_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'1006 aidj';root.mkdir()
            (root/'001-1006 aidj').mkdir();(root/'003-1006 aidj').mkdir()
            (root/'2026-10-06').mkdir()
            folder,name=Application.reserve_review_bundle(root,{'music_styles':['任意歌曲名']})
            self.assertEqual('004-1006 aidj',folder.name)
            self.assertEqual(root,folder.parent)
            other,_=Application.reserve_review_bundle(root,{'music_styles':['完全不同']})
            self.assertEqual('005-1006 aidj',other.name)

    def test_concurrent_reservations_are_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'1006 aidj'
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                folders=list(pool.map(lambda _:Application.reserve_review_bundle(root,{})[0],range(12)))
            self.assertEqual(12,len(set(folders)))
            self.assertEqual({f'{n:03d}-1006 aidj' for n in range(1,13)},{f.name for f in folders})

    def test_unicode_long_name_and_four_digit_number(self):
        self.assertEqual('001-中文 aidj',review_folder_name('中文 aidj',1))
        self.assertEqual('1000-1006 aidj',review_folder_name('1006 aidj',1000))
        self.assertLess(len(review_folder_name('中'*150,1).encode()),255)
        self.assertNotIn(':',review_folder_name('name:other',1))
