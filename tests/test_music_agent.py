import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from akari_commands import Commands
from akari_music import is_request, parse_request, settings
from akari_music_source import public_url, search, resolve_source
from akari_music_tool import invoke
from akari_tasks import TaskStore
from test_tasks import event

ROOT = Path(__file__).resolve().parents[1]


class MusicAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        cfg = json.loads((ROOT/'music/config.example.json').read_text())
        cfg.update(reference_extract_vocals=False,downloader='yt-dlp')
        self.path=self.root/'music.json';self.path.write_text(json.dumps(cfg))
        self.music=settings(self.path)
        self.db=TaskStore(self.root/'state')
        self.cfg=dict(dry_run=False,agent_enabled=True,music_enabled=True,agent_allowed_openids=['owner'],
                      agent_workdir=str(self.root),music_config=str(self.path))
        self.commands=Commands(self.db,self.cfg,lambda x:None)

    def tearDown(self):self.temp.cleanup()

    def test_original_parameter_parser_does_not_require_source(self):
        op=parse_request('/原创 /风格 "Japanese, J-pop"\n[Verse]\n春が来た')
        self.assertEqual(op['mode'],'generate');self.assertEqual(op['song'],'');self.assertIn('春',op['lyrics'])

    def test_music_commands_and_natural_language_route_to_pi(self):
        for index,text in enumerate(['/音乐 写一首日语动漫歌','/作曲 雨后放学的歌','/唱歌 帮我翻唱某首歌','阿卡林，帮我写一首轻快的歌']):
            ev=dict(event(),key='music'+str(index),msg_id='music'+str(index),content=text)
            self.assertTrue(self.commands.recognizes(ev),text)
            self.commands.handle(ev)
        jobs=self.db.list(limit=20)
        self.assertEqual(len(jobs),4)
        self.assertTrue(all(j['operation']['kind']=='music_agent' for j in jobs))
        self.assertTrue(all('akari_music' in j['prompt'] for j in jobs))

    def test_ordinary_discussion_is_not_auto_executed_and_authorization_is_kept(self):
        for text in ('你会唱歌吗','这首歌真好听','为什么不能生成音乐','/聊 帮我写一首歌','帮我修复音乐模块'):
            self.assertFalse(is_request(text),text)
        self.assertFalse(self.commands.recognizes(dict(event(user='stranger'),content='帮我写一首歌')))
        self.cfg['music_auto_route']=False
        self.assertFalse(self.commands.recognizes(dict(event(),content='帮我写一首歌')))

    def test_cover_prose_routes_to_native_agent(self):
        ev=dict(event(),content='/翻唱 为我翻唱花泽香菜的恋爱循环')
        self.commands.handle(ev)
        self.assertEqual(self.db.list()[0]['operation']['kind'],'music_agent')

    def test_missing_catalog_is_accepted_as_a_download_task(self):
        ev=dict(event(),content='/翻唱 一首不在曲库里的歌')
        self.commands.handle(ev)
        self.assertEqual(self.db.list()[0]['operation']['song'],'一首不在曲库里的歌')

    def test_search_rejects_private_urls_and_does_not_pass_credentials(self):
        for url in ('http://127.0.0.1/x','file:///etc/passwd','https://evil.invalid/song','https://user:pass@youtube.com/watch?v=x'):
            with self.assertRaises(ValueError):public_url(url)
        with patch('akari_music_source.socket.getaddrinfo',return_value=[(None,None,None,None,('127.0.0.1',443))]):
            with self.assertRaises(ValueError):public_url('https://youtube.com/watch?v=x')
        result={'entries':[dict(title='Song',url='https://www.youtube.com/watch?v=test',duration=100,channel='Official')]}
        with patch('akari_music_source.run',return_value=json.dumps(result)) as runner:
            self.assertEqual(search('song',self.music)[0]['title'],'Song')
            command=runner.call_args.args[0]
            self.assertIn('--force-ipv4',command);self.assertIn('--ignore-config',command);self.assertNotIn('--cookies',command)
            self.assertNotIn('Authorization',' '.join(command))

    def test_network_disabled_and_catalog_traversal_are_rejected(self):
        cfg=dict(self.music,auto_download=False)
        with self.assertRaises(ValueError):search('song',cfg)
        for value in ('../secret','/etc/passwd'):
            with self.assertRaises(ValueError):resolve_source(value,self.root,cfg)

    def test_native_parameters_and_incomplete_calls_are_not_silently_replayed(self):
        for params in (dict(action='generate'),dict(action='cover'),dict(action='generate',lyrics='test',source='a')):
            with self.assertRaises(ValueError):invoke(params,'invalid',self.root,self.music)
        import hashlib
        incomplete=self.root/'music-tools'/hashlib.sha256(b'call').hexdigest()[:16]
        incomplete.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError,'已开始但未完成'):
            invoke(dict(action='generate',lyrics='[Verse]\ntest'),'call',self.root,self.music)

    def test_completed_call_returns_receipt_without_regeneration(self):
        params=dict(action='generate',lyrics='[Verse]\nTest original')
        def generated(op,task,output,cfg):
            output.mkdir(parents=True);(output/'song.mp3').write_bytes(b'fixture');return 'done'
        with patch('akari_music_tool.perform',side_effect=generated) as call:
            first=invoke(params,'same-call',self.root,self.music)
            second=invoke(params,'same-call',self.root,self.music)
            self.assertEqual(first,second);self.assertEqual(call.call_count,1)

    @unittest.skipUnless(shutil.which('pi'),'Pi CLI not installed')
    def test_real_pi_registers_and_executes_native_music_tool(self):
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(data)
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                if len(requests)==1:
                    delta=dict(role='assistant',tool_calls=[dict(index=0,id='music-status',type='function',
                        function=dict(name='akari_music',arguments=json.dumps(dict(action='status'))))]);reason='tool_calls'
                else:delta=dict(role='assistant',content='native-music-complete');reason='stop'
                for chunk in [dict(id='test',choices=[dict(index=0,delta=delta,finish_reason=None)]),
                              dict(id='test',choices=[dict(index=0,delta={},finish_reason=reason)])]:
                    self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            agent=self.root/'agent';agent.mkdir()
            (agent/'settings.json').write_text(json.dumps(dict(compaction=dict(enabled=False))))
            (agent/'models.json').write_text(json.dumps(dict(providers=dict(scripted=dict(
                baseUrl='http://127.0.0.1:%d/v1'%server.server_port,api='openai-completions',apiKey='test-only',
                models=[dict(id='music-test',name='test',reasoning=False,input=['text'],contextWindow=262144,maxTokens=8192)])))))
            env=dict(os.environ,PI_CODING_AGENT_DIR=str(agent),AKARI_TASK_DIR=str(self.root),AKARI_MUSIC_ENABLED='1',AKARI_MUSIC_ONLY='1',AKARI_MUSIC_CONFIG=str(self.path))
            result=subprocess.run(['pi','--provider','scripted','--model','music-test','--no-extensions','-e',str(ROOT/'akari_music_tools.mjs'),
                '--no-session','--mode','json','-p','Check music status with native tool.'],cwd=self.root,env=env,capture_output=True,text=True,timeout=45)
            self.assertEqual(result.returncode,0,result.stderr[-2000:]+result.stdout[-2000:])
            self.assertEqual(len(requests),2,result.stderr[-1500:]+result.stdout[-3000:])
            names=[tool['function']['name'] for tool in requests[0]['tools']]
            self.assertIn('akari_music',names)
            self.assertFalse(set(names) & {'bash','edit','write'})
            self.assertIn('参考音频',json.dumps(requests[1],ensure_ascii=False))
            self.assertIn('native-music-complete',result.stdout)
        finally:server.shutdown();server.server_close();thread.join()
