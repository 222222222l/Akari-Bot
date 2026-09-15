import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from akari_music_source import resolve_source, search, run

class SourceTests(unittest.TestCase):
    def test_unavailable_first_result_tries_next_in_separate_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg={'root':tmp}
            candidates=[{'url':'https://youtube.com/watch?v=bad'}, {'url':'https://youtube.com/watch?v=good'}]
            def fetch(url,folder,cfg):
                if url.endswith('bad'):raise ValueError('YouTube 需要登录验证')
                return Path(folder)/'source.wav', {'url':url}
            with patch('akari_music_source.search',return_value=candidates),patch('akari_music_source.download',side_effect=fetch) as downloader:
                audio,meta=resolve_source('恋爱循环',root,cfg)
            self.assertTrue(meta['url'].endswith('good'))
            self.assertNotEqual(*[call.args[1] for call in downloader.call_args_list])
            self.assertEqual(len(json.loads((root/'source-selection.json').read_text())['attempts']),2)

    def test_platform_fallback_and_failure_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            def lookup(q,cfg,platform='youtube'):
                return [{'url':'https://'+('youtube.com/watch?v=x' if platform=='youtube' else 'bilibili.com/video/BVx')}]
            with patch('akari_music_source.search',side_effect=lookup),patch('akari_music_source.download',side_effect=ValueError('unavailable')) as d:
                with self.assertRaisesRegex(ValueError,'2 个候选'):resolve_source('恋爱循环',tmp,{'root':tmp})
            self.assertEqual(d.call_count,2)
            self.assertEqual(len(json.loads((Path(tmp)/'source-selection.json').read_text())['attempts']),2)

    def test_explicit_url_is_not_silently_replaced(self):
        with patch('akari_music_source.download',side_effect=ValueError('blocked')),patch('akari_music_source.search') as s:
            with self.assertRaises(ValueError):resolve_source('https://youtube.com/watch?v=x','/tmp',{})
            s.assert_not_called()

    def test_bilibili_search_prefix(self):
        with patch('akari_music_source.run',return_value=json.dumps({'entries':[{'title':'song','url':'https://www.bilibili.com/video/BVx','duration':100}]})) as r:
            self.assertEqual(len(search('song',{'downloader':'yt-dlp'},platform='bilibili')),1)
            self.assertEqual(r.call_args.args[0][-1],'bilisearch5:song')

    def test_auth_failure_is_short_and_full_details_are_logged(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            log=Path(tmp)/'metadata.log'
            raw="ERROR: Sign in to confirm you're not a bot. Use --cookies https://example.test/help"
            with patch('akari_music_source.subprocess.run',return_value=SimpleNamespace(returncode=1,stdout='',stderr=raw)):
                with self.assertRaisesRegex(ValueError,'人机验证') as error:run(['yt-dlp'],10,log)
            self.assertNotIn('--cookies',str(error.exception))
            self.assertIn('--cookies',log.read_text())

    def test_one_candidate_metadata_failure_keeps_other_titles(self):
        candidates={'entries':[{'url':'https://bilibili.com/video/a'}, {'url':'https://bilibili.com/video/b'}]}
        with patch('akari_music_source.public_url'),patch('akari_music_source.run',side_effect=[json.dumps(candidates),ValueError('offline'),json.dumps({'title':'正确歌曲','uploader':'歌手'})]):
            found=search('song',{'downloader':'yt-dlp'},platform='bilibili')
        self.assertEqual(len(found),2)
        self.assertIn('metadata_error',found[0]);self.assertEqual(found[1]['title'],'正确歌曲')
