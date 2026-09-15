"""Real Pi CLI + scripted local provider: check image and long-task wire budgets."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import struct
import tempfile
import threading
import unittest
import zlib


@unittest.skipUnless(shutil.which('pi'), 'Pi CLI not installed')
class PiContextTests(unittest.TestCase):
    def test_twelve_image_turns_keep_two_images_and_lower_output_budget(self):
        self.run_scripted_turns(12, images=True)

    def test_eighty_tool_turns_archive_history_and_reach_completion(self):
        self.run_scripted_turns(80, images=False)

    def run_scripted_turns(self, turns, *, images):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            png = root/'frame.png'
            def chunk(kind, data):
                return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data))
            png.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',32,32,8,2,0,0,0))+
                chunk(b'IDAT',zlib.compress((b'\0'+b'\xff\x80\0'*32)*32))+chunk(b'IEND',b''))
            source = png if images else root/'large-result.txt'
            if not images:
                source.write_text('0123456789abcdef'*700)
            requests = []
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    requests.append(body)
                    turn = len(requests)
                    self.send_response(200)
                    self.send_header('Content-Type','text/event-stream')
                    self.end_headers()
                    if turn <= turns:
                        delta = dict(role='assistant',tool_calls=[dict(index=0,id='read%d'%turn,type='function',
                            function=dict(name='read',arguments=json.dumps(dict(path=str(source)))) )])
                        reason = 'tool_calls'
                    else:
                        delta, reason = dict(role='assistant',content='scripted-turns-complete'), 'stop'
                    for chunk in [dict(id='r%d'%turn,object='chat.completion.chunk',choices=[dict(index=0,delta=delta,finish_reason=None)]),
                                  dict(id='r%d'%turn,object='chat.completion.chunk',choices=[dict(index=0,delta={},finish_reason=reason)],
                                       usage=dict(prompt_tokens=1000,completion_tokens=30,total_tokens=1030))]:
                        self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode())
                    self.wfile.write(b'data: [DONE]\n\n')
            server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                agent = root/'agent';agent.mkdir()
                (agent/'settings.json').write_text(json.dumps(dict(defaultProvider='scripted',defaultModel='vision-test',
                    compaction=dict(enabled=False))))
                (agent/'models.json').write_text(json.dumps(dict(providers=dict(scripted=dict(
                    baseUrl='http://127.0.0.1:%d/v1'%server.server_port,api='openai-completions',apiKey='test-only',
                    models=[dict(id='vision-test',name='test',reasoning=False,input=['text','image'],
                        contextWindow=262144,maxTokens=65536,cost=dict(input=0,output=0,cacheRead=0,cacheWrite=0))])))))
                env = dict(os.environ,PI_CODING_AGENT_DIR=str(agent),AKARI_TASK_DIR=str(root))
                extra = []
                if not images:
                    # Fixed prompt overhead leaves little room for tool history.
                    system = root/'system.txt'
                    system.write_text('Fixed test instructions.\n' + 's'*105000)
                    extra = ['--append-system-prompt',str(system)]
                result = subprocess.run(['pi','--provider','scripted','--model','vision-test','--no-extensions',
                    '-e',str(Path(__file__).resolve().parents[1]/'akari_context.mjs'),
                    *extra,'--no-session','--mode','json','-p','Read the test source repeatedly.'],
                    cwd=root,env=env,capture_output=True,text=True,timeout=90)
                self.assertEqual(result.returncode,0,result.stderr[-2000:]+result.stdout[-1500:])
                self.assertEqual(len(requests),turns+1,result.stdout[-2000:])
                self.assertIn('scripted-turns-complete',result.stdout)
                counts = [sum(block.get('type')=='image_url' for msg in request['messages']
                    if isinstance(msg.get('content'),list) for block in msg['content']) for request in requests]
                self.assertEqual(max(counts),2 if images else 0,result.stdout[-7000:]+result.stderr[-1500:])
                self.assertTrue(all(request.get('max_tokens',request.get('max_completion_tokens'))==16384 for request in requests))
                self.assertTrue((root/'context-cache'/'budget.jsonl').is_file())
                if not images:
                    logs = [json.loads(line) for line in (root/'context-cache'/'budget.jsonl').read_text().splitlines()]
                    self.assertTrue(any(log['archivedExchanges'] for log in logs))
                    self.assertTrue((root/'context-cache'/'steps-index.json').is_file())
                    for turn, request in enumerate(requests):
                        self.assertLessEqual(len(json.dumps(request,ensure_ascii=False,separators=(',',':')).encode()),196608)
                        calls = [call['id'] for message in request['messages'] for call in message.get('tool_calls',[])]
                        replies = [message['tool_call_id'] for message in request['messages'] if message['role']=='tool']
                        self.assertCountEqual(calls,replies)
                        if turn:
                            self.assertIn('read%d'%turn,replies)
            finally:
                server.shutdown();server.server_close();thread.join(2)


if __name__ == '__main__':unittest.main()
