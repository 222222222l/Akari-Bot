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
                   group_reply_mode='all', agent_enabled=True)
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
