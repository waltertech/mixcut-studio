"""Leaf sets stay independent regardless of classification depth or chosen root."""
import random
import unittest
from pathlib import Path
from unittest.mock import patch
from mixcut import foldermusic, planner

class LeafSetTests(unittest.TestCase):
    def songs(self):
        result=[]
        for folder in ['分类/短/A','分类/短/B','分类/长/A','/'.join(['深层']*10)+'/C']:
            for n in range(1,4):
                path=f'/music/{folder}/歌曲 {n:02d}.mp3'
                result.append({'id':path,'path':path,'duration':n*30,'mime_type':'audio/mpeg'})
        return result

    def test_leaf_groups_ten_levels_and_same_names(self):
        songs=self.songs()
        with patch('os.scandir',side_effect=AssertionError('metadata only')):
            groups=foldermusic.groups(songs+songs,'/music')
        self.assertEqual(4,len(groups))
        self.assertTrue(all(len(v)==3 for v in groups.values()))
        self.assertTrue(all(str(Path(s['path']).parent)==key for key,v in groups.items() for s in v))

    def test_higher_lower_and_direct_set_selection(self):
        songs=self.songs()
        high=foldermusic.groups(songs,'/music/分类')
        low=foldermusic.groups(songs,'/music/分类/短')
        direct=foldermusic.groups(songs,'/music/分类/短/A')
        self.assertEqual(3,len(high));self.assertEqual(2,len(low));self.assertEqual(1,len(direct))
        key='/music/分类/短/A'
        self.assertEqual(high[key],low[key]);self.assertEqual(low[key],direct[key])

    def test_parent_audio_not_merged_or_duplicated(self):
        songs=self.songs()+[{'id':'loose','path':'/music/分类/loose.mp3','duration':20}]
        groups=foldermusic.groups(songs,'/music')
        self.assertEqual(12,sum(map(len,groups.values())))
        self.assertNotIn('/music/分类',groups)
        # Non-music folders need not be inspected; direct song parents define sets.
        self.assertEqual(1,len(foldermusic.groups([songs[0]],'/music/分类/短/A')))

    def test_plan_uses_each_set_before_repeat_and_correct_duration(self):
        songs=self.songs()
        config={'music_mode':'folder','music_root':'/music','duration_basis':'music',
                'count':4,'mode':'single','allow_overlap':True,'music_repeats':2,'seed':3}
        videos=[{'id':str(n),'path':f'/v/{n}.mp4','duration':600} for n in range(4)]
        batch=planner.plan(videos,songs,config)
        self.assertEqual(4,len({i['music_folder'] for i in batch['items']}))
        for item in batch['items']:
            self.assertEqual(360,item['duration'])
            self.assertEqual(6,len(item['music']))
            self.assertEqual([1,2],[int(Path(s['path']).stem[-2:]) for s in item['music'][:2]])
            self.assertTrue(all(str(Path(s['path']).parent)==item['music_folder'] for s in item['music']))
