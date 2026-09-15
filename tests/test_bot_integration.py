import importlib.util
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_tasks import event


class BotIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        cfg = dict(app_id='test', app_secret='test', api_base='https://qq.invalid', model='test',
                   llm_base='https://model.invalid', agent_allowed_openids=['owner'],
                   dry_run=False, agent_workdir=str(root), sticker_enabled=False,
                   image_enabled=False, video_enabled=False, voice_enabled=False,
                   group_reply_mode='all', agent_enabled=True, role_task_dispatch=False,search_enabled=False)
        (root / 'config.json').write_text(json.dumps(cfg))
        (root / 'persona.txt').write_text('test')
        with patch.dict(os.environ, {'AKARI_BASE': str(root)}):
            spec = importlib.util.spec_from_file_location('isolated_bot', Path(__file__).resolve().parents[1] / 'akari_bot.py')
            self.bot = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.bot)
        self.bot.akari_stickers = None
        self.bot.log = lambda text: None

    def tearDown(self):
        self.tmp.cleanup()

    def ev(self, content, key):
        return dict(event(key=key), content=content, type='C2C_MESSAGE_CREATE', ev_mentions=[], nick='test', ts=time.time())

    def test_task_control_is_responsive_while_chat_model_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        def slow_model(messages):
            entered.set()
            release.wait(5)
            return 'done'
        self.bot.call_llm = slow_model
        thread = threading.Thread(target=self.bot.handle_event, args=(self.ev('hello', 'chat'),))
        thread.start()
        self.assertTrue(entered.wait(2))
        try:
            q = queue.Queue()
            start = time.monotonic()
            task_ev = self.ev('/任务 检查\n@input.txt', 'task')
            self.bot.dispatch_event(task_ev, q)
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 0.5)
            self.assertTrue(q.empty())
            job = self.bot.TASK_STORE.list()[0]
            self.assertEqual(job['prompt'], '检查\n@input.txt')
            self.bot.dispatch_event(task_ev, q)
            self.assertEqual(len(self.bot.TASK_STORE.list()), 1)
        finally:
            release.set()
            thread.join(3)

    def test_at_only_group_filter_still_applies_to_task_entry(self):
        self.bot.CFG['group_reply_mode'] = 'at_only'
        ev = dict(self.ev('/任务 do it', 'group'), scene='group', group_openid='g', type='GROUP_MESSAGE_CREATE')
        self.bot.dispatch_event(ev, queue.Queue())
        self.assertEqual(self.bot.TASK_STORE.list(), [])

    def test_chat_escape_is_not_submitted_as_a_task(self):
        ev = self.ev('/工作 开', 'mode')
        self.bot.dispatch_event(ev, queue.Queue())
        q = queue.Queue()
        self.bot.dispatch_event(self.ev('/聊 今天怎么样', 'chat'), q)
        self.assertEqual(q.get_nowait()['content'], '今天怎么样')
        self.assertEqual(self.bot.TASK_STORE.list(), [])

    def test_role_dispatch_reaches_chat_then_actual_queue_and_role_ack(self):
        self.bot.CFG['role_task_dispatch']=True
        ev=self.ev('/任务 写一个报告\n@input.txt','role-submit')
        q=queue.Queue();self.bot.dispatch_event(ev,q)
        self.assertEqual(self.bot.TASK_STORE.list(),[])
        self.assertEqual(q.qsize(),1)
        calls=[]
        def model(messages,tools=None):
            calls.append(messages)
            if len(calls)==1:
                return dict(tool_calls=[dict(id='call',type='function',function=dict(name='akari_task',arguments=json.dumps(dict(action='submit',kind='task',request='整理报告，读取 @input.txt'))))])
            return '嗯！灯里来帮你整理，做好就给你看哦。'
        self.bot.call_llm=model
        self.bot.handle_event(q.get_nowait())
        job=self.bot.TASK_STORE.list()[0]
        self.assertIn('@input.txt',job['prompt'])
        with self.bot.TASK_STORE.connect() as db:
            rows=db.execute('SELECT payload FROM outbox WHERE job_id=?',(job['id'],)).fetchall()
        self.assertEqual(len(rows),1)
        self.assertIn('灯里',json.loads(rows[0][0])['text'])
        self.assertEqual(calls[1][-1]['role'],'tool')

    def test_role_dispatch_does_not_delay_control_commands(self):
        self.bot.CFG['role_task_dispatch']=True
        q=queue.Queue();self.bot.dispatch_event(self.ev('/任务 do it','newtask'),q)
        self.bot.dispatch_event(self.ev('/进度','status'),q)
        self.assertEqual(q.qsize(),1)
        with self.bot.TASK_STORE.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)

    def test_unauthorized_role_request_gets_truthful_reply_without_task(self):
        self.bot.CFG['role_task_dispatch']=True
        seen=[]
        def model(messages,tools=None):
            seen.append(tools)
            return '我去准备翻唱啦！akari_task: submit, kind=music'
        self.bot.call_llm=model
        self.bot.handle_event(dict(self.ev('翻唱恋爱循环','denied'),user_openid='unlisted'))
        self.assertTrue(seen[0]);self.assertEqual(self.bot.TASK_STORE.list(),[])
        with self.bot.TASK_STORE.connect() as db:
            text=json.loads(db.execute('SELECT payload FROM outbox').fetchone()[0])['text']
        self.assertIn('没有任务权限',text);self.assertNotIn('akari_task',text)

    def test_unlisted_chat_user_searches_with_agent_disabled(self):
        self.bot.CFG.update(agent_enabled=False,search_enabled=True)
        call=dict(id='s',function=dict(name='akari_search',arguments=json.dumps({'query':'Python 官网'})))
        responses=iter([{'tool_calls':[call]},'Python 官网是 https://www.python.org/'])
        self.bot.call_llm=lambda messages,tools=None:next(responses)
        with patch('akari_role_tasks.akari_web.search',return_value=[dict(title='Python',url='https://www.python.org/')]):
            self.bot.handle_event(dict(self.ev('帮我查 Python 官网','search-only'),user_openid='unlisted'))
        self.assertEqual(self.bot.TASK_STORE.list(),[])
        with self.bot.TASK_STORE.connect() as db:
            payloads=[json.loads(r[0]) for r in db.execute('SELECT payload FROM outbox')]
        self.assertEqual(len(payloads),1);self.assertEqual(payloads[0]['kind'],'text')
        self.assertIn('python.org',payloads[0]['text'])

    def test_gateway_message_key_uses_stable_message_identity(self):
        payload = {'t':'C2C_MESSAGE_CREATE', 'd':{'id':'same-message','author':{'user_openid':'owner'}}}
        a, b = self.bot.build_event(payload), self.bot.build_event(dict(payload, id='delivery-attempt-2'))
        self.assertEqual(a['key'], b['key'])

    def test_group_author_member_openid_is_available_for_authorization(self):
        payload = {'t':'GROUP_AT_MESSAGE_CREATE','d':{'id':'group-message','group_openid':'g',
                   'author':{'member_openid':'group-owner'}}}
        self.assertEqual(self.bot.build_event(payload)['user_openid'], 'group-owner')


if __name__ == '__main__':
    unittest.main()
