import json
from pathlib import Path
import tempfile
import time
import unittest

from akari_commands import Commands
from akari_delivery import Outbox, QQTransport
from akari_errors import DeliveryError
from akari_tasks import TaskStore
from test_tasks import event


class VideoFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = TaskStore(self.tmp.name)
        self.ev = dict(event(), ts=time.time())
        self.job = self.db.submit(self.ev, 'send video', 60, self.tmp.name)
        self.path = Path(self.job['directory'])/'artifacts'/'large.mp4'
        with self.path.open('wb') as stream:stream.truncate(210*1048576)
        self.db.update(self.job['id'],status='succeeded',artifacts=[dict(path=str(self.path),name=self.path.name,size=self.path.stat().st_size)])
        self.payload = dict(kind='media',path=str(self.path),file_type=2,root=str(self.path.parent))
        self.db.enqueue(self.ev,self.payload,self.job['id'])
        self.item = self.db.pending()
        self.cfg = dict(dry_run=False,agent_allowed_openids=['owner'],agent_workdir=self.tmp.name)
        class Transport:
            cfg = self.cfg
        self.outbox = Outbox(self.db,Transport(),lambda _:None)
        self.error = DeliveryError('internal proxy',retryable=True,code='850012',stage='upload_prepare')

    def tearDown(self):self.tmp.cleanup()

    def test_large_prepare_proxy_error_creates_only_one_durable_adaptation(self):
        self.assertTrue(self.outbox.adapt_rejected_video(self.item,self.error))
        self.assertTrue(self.outbox.adapt_rejected_video(self.item,self.error))
        children = self.db.delivery_children(self.job['id'])
        self.assertEqual(len(children),1)
        self.assertEqual(children[0]['operation']['max_bytes'],190*1048576)
        with self.db.connect() as db:
            item = db.execute('SELECT status,receipt FROM outbox WHERE id=?',(self.item['id'],)).fetchone()
            self.assertEqual(item['status'],'replaced')
            self.assertEqual(json.loads(item['receipt'])['fallback_job'],children[0]['id'])
        self.assertEqual(self.path.stat().st_size,210*1048576)

    def test_other_stages_timeouts_and_quota_never_trigger_size_workaround(self):
        for error in [DeliveryError('timeout',retryable=True,stage='upload_prepare'),
                      DeliveryError('quota',code='40093002',stage='upload_prepare'),
                      DeliveryError('message failed',code='850012',stage='messages'),
                      DeliveryError('processing',code='850012',stage='files')]:
            self.assertFalse(self.outbox.adapt_rejected_video(self.item,error))
        self.assertEqual(self.db.delivery_children(self.job['id']),[])

    def test_small_video_proxy_error_is_not_misreported_as_size_limit(self):
        with self.path.open('wb') as stream:stream.truncate(52*1048576)
        self.assertFalse(self.outbox.adapt_rejected_video(self.item,self.error))

    def test_parent_resend_retries_child_waiting_parts_without_replaying_original_or_sent_parts(self):
        self.outbox.adapt_rejected_video(self.item,self.error)
        child = self.db.delivery_children(self.job['id'])[0]
        self.db.enqueue(self.ev,dict(kind='media',path='part1'),child['id'],'part1')
        self.db.enqueue(self.ev,dict(kind='media',path='part2'),child['id'],'part2')
        with self.db.connect() as db:
            db.execute("UPDATE outbox SET status='sent' WHERE dedupe='part1'")
            db.execute("UPDATE outbox SET status='waiting' WHERE dedupe='part2'")
        self.db.refresh(dict(self.ev,msg_id='new'),jid=self.job['id'],retry=True)
        with self.db.connect() as db:
            states = {r['dedupe']:r['status'] for r in db.execute('SELECT dedupe,status FROM outbox')}
            original = db.execute('SELECT status FROM outbox WHERE id=?',(self.item['id'],)).fetchone()[0]
        self.assertEqual(states['part1'],'sent')
        self.assertEqual(states['part2'],'pending')
        self.assertEqual(original,'replaced')

    def test_progress_distinguishes_execution_from_actual_media_receipts(self):
        self.outbox.adapt_rejected_video(self.item,self.error)
        child = self.db.delivery_children(self.job['id'])[0]
        Commands(self.db,self.cfg,lambda _:None).handle(dict(self.ev,msg_id='query',content='/进度 '+self.job['id']))
        with self.db.connect() as db:
            text = json.loads(db.execute('SELECT payload FROM outbox ORDER BY id DESC LIMIT 1').fetchone()[0])['text']
        self.assertIn('执行完成',text)
        self.assertIn('附件 QQ 接收确认 0 个',text)
        self.assertIn(child['id'],text)

    def test_parent_progress_counts_confirmed_child_video_receipts(self):
        self.outbox.adapt_rejected_video(self.item,self.error)
        child = self.db.delivery_children(self.job['id'])[0]
        self.db.enqueue(self.ev,dict(kind='media',path='part1'),child['id'],'confirmed-part')
        with self.db.connect() as db:
            db.execute("UPDATE outbox SET status='sent',receipt=? WHERE dedupe='confirmed-part'",(json.dumps(dict(message_id='confirmed')),))
        Commands(self.db,self.cfg,lambda _:None).handle(dict(self.ev,msg_id='query',content='/进度 '+self.job['id']))
        with self.db.connect() as db:
            text = json.loads(db.execute('SELECT payload FROM outbox ORDER BY id DESC LIMIT 1').fetchone()[0])['text']
        self.assertIn('附件 QQ 接收确认 1 个，待发送 0 个',text)

    def test_http_error_retains_stage_for_same_business_code(self):
        transport = QQTransport(dict(app_id='fake'),self.db,
            lambda *a:(500,dict(code=850012,message='call inner proxy error')),lambda:'fake',lambda _:None)
        with self.assertRaises(DeliveryError) as caught:
            transport.post('https://qq.invalid/v2/groups/g/upload_prepare',{})
        self.assertEqual(caught.exception.stage,'upload_prepare')
        self.assertIn('upload_prepare',str(caught.exception))


if __name__ == '__main__':unittest.main()
