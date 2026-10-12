"""First-round anchors, random subsequent rounds, metadata-only planning."""
from pathlib import Path
import random
import unittest
from unittest.mock import patch
from mixcut import foldermusic


def songs(names):
    return [{'id':name,'name':name,'path':'/music/style/'+name,'duration':10} for name in names]


class OpeningOrderTests(unittest.TestCase):
    def test_opening_fixed_across_seeds_rest_random_and_later_rounds_unfixed(self):
        music=songs(['04 D.mp3','02 B.flac','03 C.m4a','01 A.mp3','05 E.mp3'])
        tails=set();later=set()
        with patch('subprocess.run',side_effect=AssertionError('no media reads')):
            for seed in range(100):
                order,total=foldermusic.allocate(music,150,{},random.Random(seed))
                self.assertEqual(['01','02'],[Path(s['path']).name[:2] for s in order[:2]])
                tails.add(tuple(s['id'] for s in order[2:5]));later.add(tuple(s['id'] for s in order[5:10]))
                for offset in range(0,len(order),5):self.assertEqual({s['id'] for s in music},{s['id'] for s in order[offset:offset+5]})
                self.assertGreaterEqual(total,150)
        self.assertGreater(len(tails),1);self.assertGreater(len(later),1)
        self.assertTrue(any(sequence[0].startswith('01') for sequence in later))
        self.assertTrue(any(not sequence[0].startswith('01') for sequence in later))

    def test_filename_boundaries_missing_and_duplicates_deterministic(self):
        music=songs(['010 not first.mp3','020 not second.mp3','01-A.mp3','01-Z.mp3','02B.flac'])
        self.assertEqual(['01-A.mp3','02B.flac'],[s['id'] for s in foldermusic.opening_tracks(music)])
        self.assertEqual([],foldermusic.opening_tracks(songs(['010.mp3','020.mp3'])))
        item={'music':songs(['a.mp3','b.mp3'])};foldermusic.describe(item)
        self.assertIn('01、02',item['music_order_warning'])
        item={'music':music};foldermusic.describe(item);self.assertEqual('',item['music_order_warning'])

    def test_two_song_and_single_song_folders_remain_usable(self):
        pair=songs(['02 B.mp3','01 A.mp3'])
        order,_=foldermusic.allocate(pair,55,{},random.Random(7));self.assertEqual(['01 A.mp3','02 B.mp3'],[s['id'] for s in order[:2]])
        order,_=foldermusic.allocate(pair[:1],25,{},random.Random(7));self.assertEqual(3,len(order))


import test_v154 as actionfixtures

class FolderAnchorAPITests(unittest.TestCase):
    def setUp(self):
        actionfixtures.FolderActionsTests.setUp(self)
        for index,song in enumerate(self.songs):
            name=f'{index%2+1:02d} song.mp3'
            song.update(name=name,path=str(Path(song['path']).parent/name))
        self.app.store.put('library',{'music_dir':str(self.root/'music'),'scan':{'videos':self.videos,'music':self.songs}})
        self.batch=self.app.create_plan({'config':self.batch['config']})
        self.bid=self.batch['id'];self.iid=self.batch['items'][0]['id']

    def assert_opening(self,item):
        self.assertEqual(['01','02'],[Path(song['path']).name[:2] for song in item['music'][:2]])

    def test_initial_refresh_and_folder_switch_anchor_manual_order_unrestricted(self):
        self.assert_opening(self.batch['items'][0])
        refreshed=self.app.refresh_music(self.bid,self.iid);self.assert_opening(refreshed['items'][0])
        switched=self.app.change_music_folder(self.bid,self.iid,{'random':True});self.assert_opening(switched['items'][0])
        ids=[s['id'] for s in switched['items'][0]['music']];ids[0],ids[1]=ids[1],ids[0]
        manual=self.app.reorder_music(self.bid,self.iid,ids)
        self.assertEqual(ids,[s['id'] for s in manual['items'][0]['music']])
        with self.assertRaisesRegex(ValueError,'前两首'):
            self.app.replace_media(self.bid,self.iid,{'kind':'music','index':0})
