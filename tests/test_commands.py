import json
from pathlib import Path
import tempfile
import time
import unittest

from akari_commands import Commands, task_text, timeout_of
from akari_tasks import TaskStore, scope_of
from test_tasks import event


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = TaskStore(self.tmp.name)
        self.cfg = dict(dry_run=False, agent_allowed_openids=['owner'], agent_workdir=self.tmp.name)
        self.cmd = Commands(self.db, self.cfg, lambda text: None)
        self.seq = 0

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, text, user='owner', group=''):
        self.seq += 1
        ev = dict(event(user=user, group=group, key='msg-%s' % self.seq), content=text, ts=time.time())
        self.cmd.handle(ev)
        return ev

    def test_work_mode_natural_language_and_multiline_are_persisted(self):
        self.send('/工作 开')
        text = '执行下面的代码\n```\ncat @input.txt\n```\n写给 a@b.cn'
        ev = self.send(text)
        self.assertEqual(self.db.get('最近', scope_of(ev))['prompt'], text)
        self.assertEqual(self.db.session(scope_of(ev))['mode'], 'work')

    def test_work_with_content_submits_exact_multiline_task_in_either_mode(self):
        for mode in ('chat', 'work'):
            self.send('/工作 ' + ('开' if mode == 'work' else '关'))
            prompt = '读取已有图片并整理成 PDF\n保留 @input 参数和换行'
            ev = self.send('/工作 ' + prompt)
            job = self.db.get('最近', scope_of(ev))
            self.assertIsNotNone(job)
            self.assertEqual(job['prompt'], prompt)
            self.assertEqual(self.db.session(scope_of(ev))['mode'], mode)

    def test_work_toggle_and_status_do_not_submit_jobs(self):
        for command in ('/工作', '/工作 状态', '/工作 开', '/工作 关'):
            self.send(command)
        self.assertEqual(self.db.list(), [])

    def test_work_task_still_checks_authorization_and_timeout(self):
        self.send('/工作 echo private', user='other')
        self.assertEqual(self.db.list(), [])
        ev = self.send('/工作 /超时3600 整理已有资料')
        job = self.db.get('最近', scope_of(ev))
        self.assertIsNotNone(job)
        self.assertEqual(job['timeout'], 3600)
        self.assertEqual(job['prompt'], '整理已有资料')

    def test_unauthorized_users_cannot_run_or_read_owner_tasks(self):
        ev = self.send('/任务 private')
        job = self.db.get('最近', scope_of(ev))
        self.send('/任务 hack', user='other')
        self.send('/结果 ' + job['id'], user='other')
        self.assertEqual(len(self.db.list()), 1)
        with self.db.connect() as db:
            last = json.loads(db.execute('SELECT payload FROM outbox ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertNotIn('private', last['text'])

    def test_empty_allowlist_is_not_public_shell(self):
        self.cfg['agent_allowed_openids'] = []
        self.send('/任务 write something')
        self.assertEqual(self.db.list(), [])

    def test_prefix_boundaries_and_timeout_only_parse_leading_options(self):
        ev = dict(event(), content='/taskforce is not /task')
        self.assertFalse(self.cmd.recognizes(ev))
        text = '读取 /tmp/x 和 /超时300 不改变正文\nnext'
        prompt, timeout = timeout_of('/超时3600 ' + text, self.cfg)
        self.assertEqual(prompt, text)
        self.assertEqual(timeout, 3600)
        self.assertEqual(task_text('<@!bot>  cat @file\nnext'), 'cat @file\nnext')

    def test_new_session_changes_context_without_deleting_history(self):
        ev = self.send('/任务 old')
        old = self.db.get('最近', scope_of(ev))
        self.send('/新会话')
        ev = self.send('/任务 new')
        new = self.db.get('最近', scope_of(ev))
        self.assertNotEqual(old['session_id'], new['session_id'])
        self.assertIsNotNone(self.db.get(old['id'], scope_of(ev)))

    def test_dry_run_does_not_create_executable_job(self):
        self.cfg['dry_run'] = True
        self.send('/任务 do something')
        self.assertEqual(self.db.list(), [])

    def test_result_page_preserves_code_and_reaches_beyond_old_limit(self):
        ev = self.send('/任务 result')
        job = self.db.get('最近', scope_of(ev))
        self.db.update(job['id'], status='succeeded', result='x' * 2800 + '```python\nprint(42)\n```')
        self.send('/结果 %s 5' % job['id'])
        with self.db.connect() as db:
            last = json.loads(db.execute('SELECT payload FROM outbox ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertIn('```python\nprint(42)\n```', last['text'])

    def test_text_artifact_can_be_read_in_qq_without_file_upload(self):
        ev = self.send('/任务 artifact')
        job = self.db.get('最近', scope_of(ev))
        path = Path(job['directory'])/'artifacts'/'table.csv'
        path.write_text('name,value\nexample,42\n')
        self.db.update(job['id'], artifacts=[dict(path=str(path),name='table.csv',size=path.stat().st_size)])
        self.send('/预览 %s 1' % job['id'])
        with self.db.connect() as db:
            payload = json.loads(db.execute('SELECT payload FROM outbox ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertIn('example,42', payload['text'])


if __name__ == '__main__':
    unittest.main()
