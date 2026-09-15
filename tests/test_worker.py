import json
import io
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from akari_tasks import TaskStore, scope_of
from akari_worker import TaskService, process_stamp
from test_tasks import event


FAKE = r'''
import json, os, pathlib, subprocess, sys, time
root = pathlib.Path(os.environ['AKARI_TASK_DIR'])
prompt = pathlib.Path(sys.argv[1]).read_text().splitlines()[0]
def emit(v): print(json.dumps(v,ensure_ascii=False), flush=True)
emit({'type':'tool_execution_start','toolName':'bash'})
if prompt.startswith('sleep'):
    child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
    (root/'child.pid').write_text(str(child.pid))
    emit({'type':'message_update','assistantMessageEvent':{'type':'text_delta','delta':'部分结果：已经开始'}})
    time.sleep(30)
if prompt.startswith('slow'):
    time.sleep(0.7)
if prompt.startswith('feedback'):
    path = root/'artifacts'/'ready.txt'
    path.write_text('ready')
    (root/'feedback.jsonl').write_text(json.dumps({'type':'akari_artifact','path':str(path)})+'\n')
    time.sleep(0.7)
if prompt.startswith('error'):
    emit({'type':'message_end','message':{'role':'assistant','content':[], 'stopReason':'error','errorMessage':'injected provider error'}})
    sys.exit(0)
if prompt.startswith('exit'):
    print('partial plain output',flush=True)
    sys.exit(7)
(root/'artifacts'/'report.csv').write_text('name,value\nexample,42\n')
(root/'artifacts'/'picture.png').write_bytes(b'fake-png')
emit({'type':'message_end','message':{'role':'assistant','stopReason':'stop','content':[{'type':'text','text':prompt+'\n'+'x'*4000+'\n```python\nprint(42)\n```'}]}})
'''


def wait_until(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    raise AssertionError('condition not reached within %ss' % seconds)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = TaskStore(self.root)
        fake = self.root / 'fake.py'
        fake.write_text(FAKE)
        script = self.root / 'fake.sh'
        script.write_text('exec python3 -u "' + str(fake) + '" "$@"\n')
        self.cfg = dict(dry_run=False, agent_script=str(script), agent_workers=2, agent_progress_secs=0)
        self.service = TaskService(self.db, self.cfg, lambda msg: None)

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def submit(self, prompt, key='m1', user='owner', timeout=5):
        return self.db.submit(dict(event(user=user, key=key), ts=time.time()), prompt, timeout, str(self.root))

    def run_one(self, job):
        claimed = self.db.claim()
        self.assertEqual(claimed['id'], job['id'])
        self.service.execute(claimed)
        return self.db.get(job['id'])

    def test_complete_result_and_artifacts_survive_reopen(self):
        job = self.run_one(self.submit('success'))
        self.assertEqual(job['status'], 'succeeded')
        self.assertGreater(len(job['result']), 4000)
        self.assertIn('```python\nprint(42)\n```', job['result'])
        self.assertEqual(len(job['artifacts']), 2)
        reopened = TaskStore(self.root)
        self.assertEqual(reopened.get(job['id'])['result'], job['result'])
        self.assertEqual((Path(job['directory'])/'result.md').read_text(), job['result'])
        with self.db.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outbox WHERE job_id=?', (job['id'],)).fetchone()[0], 3)

    def test_model_error_with_exit_zero_is_failed(self):
        job = self.run_one(self.submit('error'))
        self.assertEqual(job['status'], 'failed')
        self.assertIn('injected provider error', job['error'])

    def test_nonzero_exit_is_failed(self):
        job = self.run_one(self.submit('exit'))
        self.assertEqual(job['status'], 'failed')
        self.assertIn('7', job['error'])

    def test_timeout_keeps_partial_and_stops_grandchild(self):
        job = self.run_one(self.submit('sleep', timeout=0.8))
        self.assertEqual(job['status'], 'timed_out')
        self.assertIn('部分结果', job['result'])
        self.assert_child_stopped(job)

    def assert_child_stopped(self, job):
        pid = (Path(job['directory']) / 'child.pid').read_text()
        stat = Path('/proc') / pid / 'stat'
        wait_until(lambda: not stat.exists() or stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z')

    def test_cancel_running_stops_process_group_and_preserves_partial(self):
        job = self.submit('sleep')
        claimed = self.db.claim()
        thread = threading.Thread(target=self.service.execute, args=(claimed,))
        thread.start()
        wait_until(lambda: (Path(job['directory']) / 'child.pid').exists())
        self.db.cancel(job['id'], job['scope'])
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        updated = self.db.get(job['id'])
        self.assertEqual(updated['status'], 'cancelled')
        self.assertIn('部分结果', updated['result'])
        self.assert_child_stopped(updated)

    def test_restart_marks_interrupted_without_replaying_and_recovers_queued(self):
        running = self.submit('previous', key='old')
        self.db.claim()
        (Path(running['directory']) / 'partial.txt').write_text('saved partial')
        queued = self.submit('next', key='new')
        self.service.recover()
        self.assertEqual(self.db.get(running['id'])['status'], 'interrupted')
        self.assertEqual(self.db.get(running['id'])['result'], 'saved partial')
        self.assertEqual(self.db.claim()['id'], queued['id'])
        with self.db.connect() as db:
            first = db.execute('SELECT count(*) FROM outbox').fetchone()[0]
        self.service.publish(self.db.get(running['id']))
        with self.db.connect() as db:
            self.assertEqual(first, db.execute('SELECT count(*) FROM outbox').fetchone()[0])

    def test_two_workers_serialize_session_without_cross_prompt_contamination(self):
        a = self.submit('slow-A', key='a')
        b = self.submit('B', key='b')
        c = self.submit('C', key='c', user='other')
        self.service.start()
        wait_until(lambda: all(self.db.get(j['id'])['status'] == 'succeeded' for j in (a, b, c)))
        a, b, c = [self.db.get(j['id']) for j in (a, b, c)]
        self.assertGreaterEqual(b['started'], a['finished'])
        self.assertLess(c['started'], a['finished'])
        self.assertTrue(a['result'].startswith('slow-A\n'))
        self.assertTrue(b['result'].startswith('B\n'))
        self.assertTrue(c['result'].startswith('C\n'))

    def test_dry_run_service_leaves_queue_unexecuted(self):
        self.cfg['dry_run'] = True
        job = self.submit('success')
        self.service.start()
        time.sleep(0.2)
        self.assertEqual(self.db.get(job['id'])['status'], 'queued')
        self.assertFalse((Path(job['directory']) / 'events.jsonl').exists())

    def test_artifact_symlink_cannot_escape_task_output_directory(self):
        job = self.submit('success')
        private = self.root / 'private.txt'
        private.write_text('private')
        (Path(job['directory']) / 'artifacts' / 'leak.txt').symlink_to(private)
        self.assertEqual(self.service.artifacts(job), [])

    def test_ready_artifact_is_available_before_task_finishes_without_duplicate_send(self):
        job = self.submit('feedback')
        claimed = self.db.claim()
        thread = threading.Thread(target=self.service.execute, args=(claimed,))
        thread.start()
        wait_until(lambda: bool(self.db.get(job['id'])['artifacts']))
        self.assertEqual(self.db.get(job['id'])['status'], 'running')
        thread.join(5)
        with self.db.connect() as db:
            payloads = [json.loads(row['payload']) for row in db.execute('SELECT payload FROM outbox')]
        ready = [p for p in payloads if p.get('path', '').endswith('/ready.txt')]
        self.assertEqual(len(ready), 1)

    def test_attachment_is_downloaded_inside_task_and_no_token_forwarded(self):
        ev = dict(event(), attachments=[dict(filename='../说明.txt', url='https://qq-cdn.invalid/file', size=7)])
        job = self.db.submit(ev, 'attachment', 30, str(self.root))
        with patch('akari_worker.urllib.request.urlopen', return_value=io.BytesIO(b'content')) as opened:
            files, errors = self.service.input_files(job)
        self.assertFalse(errors)
        self.assertEqual(Path(files[0]).read_bytes(), b'content')
        self.assertEqual(Path(files[0]).parent, Path(job['directory'])/'inputs')
        self.assertNotIn('Authorization', opened.call_args.args[0].headers)

    def test_oversize_attachment_leaves_no_partial_file(self):
        self.cfg['agent_attachment_max_bytes'] = 3
        ev = dict(event(), attachments=[dict(filename='big.txt', url='https://qq-cdn.invalid/file')])
        job = self.db.submit(ev, 'attachment', 30, str(self.root))
        with patch('akari_worker.urllib.request.urlopen', return_value=io.BytesIO(b'too large')):
            files, errors = self.service.input_files(job)
        self.assertEqual(files, [])
        self.assertTrue(errors)
        self.assertFalse((Path(job['directory'])/'inputs'/'01-big.txt').exists())

    def test_announced_artifact_index_stays_stable_as_more_files_arrive(self):
        job = self.submit('artifacts')
        root = Path(job['directory'])/'artifacts'
        (root/'z.txt').write_text('first')
        self.db.update(job['id'], artifacts=self.service.artifacts(job))
        (root/'a.txt').write_text('second')
        self.assertEqual([a['name'] for a in self.service.artifacts(job)], ['z.txt','a.txt'])


if __name__ == '__main__':
    unittest.main()
