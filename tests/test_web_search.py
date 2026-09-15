import unittest
from unittest.mock import patch
from akari_web import parse_search,search

class SearchTests(unittest.TestCase):
    def test_ddg_attribute_order_quotes_and_nested_title(self):
        html="<a href='//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa' class='result__a extra'>A <b>title</b></a>"
        self.assertEqual(parse_search(html,'ddg')[0]['url'],'https://example.com/a')
        self.assertEqual(parse_search(html,'ddg')[0]['title'],'A title')
    def test_bing_heading_attributes_and_tracking_link(self):
        import base64
        token=base64.urlsafe_b64encode(b'https://example.com/source').decode().rstrip('=')
        html='<h2 class="title"><a target="_blank" href="https://www.bing.com/ck/a?u=a1'+token+'">News <strong>source</strong></a></h2>'
        self.assertEqual(parse_search(html,'bing')[0]['url'],'https://example.com/source')
    def test_challenge_falls_back_and_logs_result_count(self):
        logs=[]
        with patch('akari_web._fetch',side_effect=['<html>anomaly.js captcha</html>','<h2 class="title"><a href="https://example.com">Source</a></h2>']),patch('akari_web.fetch_page',return_value=''):
            found=search('test',logger=logs.append)
        self.assertEqual(len(found),1)
        self.assertIn('challenge=True',logs[0]);self.assertIn('results=1',logs[1])

    def test_searxng_partial_engine_failure_keeps_results(self):
        import json
        payload=json.dumps({'results':[{'title':'Anime','url':'https://example.com/anime','content':'Schedule'}], 'unresponsive_engines':[['duckduckgo','CAPTCHA']]})
        with patch('akari_web._fetch',return_value=payload) as fetch,patch('akari_web.fetch_page') as page:
            results=search('anime',cfg={'searxng_url':'http://localhost:8888'})
        self.assertEqual(results[0]['snippet'],'Schedule')
        self.assertEqual(fetch.call_count,1)
        page.assert_not_called()

    def test_searxng_failure_then_challenge_then_bing(self):
        with patch('akari_web._fetch',side_effect=[TimeoutError(),'<html>captcha</html>','<h2><a href="https://example.com">Source</a></h2>']),patch('akari_web.fetch_page',return_value=''):
            results=search('anime',cfg={'searxng_url':'http://localhost:8888'})
        self.assertEqual(results[0]['title'],'Source')

    def test_all_backends_fail(self):
        logs=[]
        with patch('akari_web._fetch',side_effect=TimeoutError()):
            self.assertEqual(search('anime',logger=logs.append,cfg={'searxng_url':'http://localhost:8888'}),[])
        self.assertEqual(len(logs),3)
