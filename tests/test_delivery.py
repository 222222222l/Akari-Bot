import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error

from akari_delivery import DeliveryError, Outbox, QQTransport
from akari_tasks import TaskStore
from test_tasks import event


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = TaskStore(self.tmp.name)
        self.calls = []
        self.responses = []
        self.cfg = dict(api_base='https://qq.invalid', app_id='fake', dry_run=False, agent_active_push=False,
                        agent_chunked_upload=False)
        self.transport = QQTransport(self.cfg, self.db, self.http, lambda: 'test-token', lambda msg: None)
        self.ev = dict(event(), ts=time.time())

    def tearDown(self):
        self.tmp.cleanup()

    def http(self, url, method, headers, body, timeout):
        self.calls.append((url, json.loads(json.dumps(body))))
        result = self.responses.pop(0) if self.responses else (200, {'id': 'delivered'})
        if isinstance(result, Exception):
            raise result
        return result

    def test_ack_result_media_get_distinct_sequences(self):
        self.transport.send(self.ev, dict(kind='text', text='ack'))
        self.transport.send(self.ev, dict(kind='text', text='result'))
        path = Path(self.tmp.name) / 'image.png'
        path.write_bytes(b'fake-png')
        self.responses = [(200, dict(file_info='uploaded')), (200, dict(id='media-message'))]
        self.transport.send(self.ev, dict(kind='media', path=str(path)))
        messages = [body for url, body in self.calls if url.endswith('/messages')]
        self.assertEqual([b['msg_seq'] for b in messages], [1, 2, 3])
        upload = self.calls[-2][1]
        self.assertEqual(upload['file_type'], 1)
        self.assertFalse(upload['srv_send_msg'])

    def test_qq_business_error_is_not_success_even_with_http_200(self):
        self.responses = [(200, dict(code=40034031, message='msgid已经过期,不能回复'))]
        with self.assertRaises(DeliveryError) as caught:
            self.transport.send(self.ev, dict(kind='text', text='result'))
        self.assertTrue(caught.exception.window)

    def test_upload_quota_business_code_is_not_success_or_retryable(self):
        self.responses = [(200,dict(biz_code=40093002,message='daily upload limit exceeded'))]
        with self.assertRaises(DeliveryError) as caught:
            self.transport.post('https://qq.invalid/upload_prepare',{})
        self.assertEqual(caught.exception.category,'quota')
        self.assertFalse(caught.exception.retryable)

    def test_timeout_retries_same_reply_sequence_from_durable_receipt(self):
        self.db.enqueue(self.ev, dict(kind='text', text='result'))
        outbox = Outbox(self.db, self.transport, lambda msg: None)
        self.responses = [TimeoutError()]
        outbox.once()
        with self.db.connect() as db:
            db.execute('UPDATE outbox SET next_attempt=0')
        outbox.once()
        self.assertEqual(self.calls[0][1], self.calls[1][1])
        with self.db.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM outbox').fetchone()[0], 'sent')

    def test_refresh_retries_only_unsent_for_same_owner_and_scene(self):
        job = self.db.submit(self.ev, 'one', 30, self.tmp.name)
        self.db.enqueue(self.ev, dict(kind='text', text='sent'), job['id'], 'sent')
        self.db.enqueue(self.ev, dict(kind='text', text='waiting'), job['id'], 'waiting')
        other = dict(event(user='stranger'), ts=time.time())
        self.db.enqueue(other, dict(kind='text', text='other'), None, 'other')
        with self.db.connect() as db:
            db.execute("UPDATE outbox SET status='waiting',error='reply_window:expired'")
            db.execute("UPDATE outbox SET status='sent' WHERE dedupe='sent'")
        self.db.refresh(dict(self.ev, msg_id='new'), retry=True)
        with self.db.connect() as db:
            rows = {r['dedupe']: dict(r) for r in db.execute('SELECT * FROM outbox')}
        self.assertEqual(rows['sent']['status'], 'sent')
        self.assertEqual(rows['waiting']['status'], 'pending')
        self.assertEqual(json.loads(rows['waiting']['event'])['msg_id'], 'new')
        self.assertEqual(rows['other']['status'], 'waiting')

    def test_expired_reply_is_saved_without_attempting_invalid_send(self):
        self.db.enqueue(dict(self.ev, ts=1), dict(kind='text', text='saved result'))
        Outbox(self.db, self.transport, lambda msg: None).once()
        self.assertEqual(self.calls, [])
        with self.db.connect() as db:
            row = db.execute('SELECT status,error FROM outbox').fetchone()
        self.assertEqual(row['status'], 'waiting')
        self.assertTrue(row['error'].startswith('reply_window:'))

    def test_dry_run_never_calls_http_or_token(self):
        self.cfg['dry_run'] = True
        self.transport.token = lambda: self.fail('dry run fetched a token')
        self.db.enqueue(self.ev, dict(kind='text', text='dry'))
        Outbox(self.db, self.transport, lambda msg: None).once()
        self.assertEqual(self.calls, [])
        with self.db.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM outbox').fetchone()[0], 'waiting')

    def test_file_type_4_rejection_is_preserved_as_waiting(self):
        path = Path(self.tmp.name) / 'report.pdf'
        path.write_bytes(b'fake-pdf')
        self.responses = [urllib.error.HTTPError('url', 400, 'Bad Request', {},
                          io.BytesIO(b'{"code":400,"message":"file type unsupported"}'))]
        self.db.enqueue(self.ev, dict(kind='media', path=str(path)))
        Outbox(self.db, self.transport, lambda msg: None).once()
        self.assertEqual(self.calls[0][1]['file_type'], 4)
        with self.db.connect() as db:
            row = db.execute('SELECT status,error FROM outbox').fetchone()
        self.assertEqual(row['status'], 'waiting')
        self.assertIn('unsupported', row['error'])

    def test_missing_id_does_not_mark_sent(self):
        self.responses = [(200, {})]
        with self.assertRaises(DeliveryError):
            self.transport.send(self.ev, dict(kind='text', text='result'))

    def test_active_timeout_does_not_automatically_duplicate_send(self):
        self.cfg['agent_active_push'] = True
        self.responses = [TimeoutError()]
        self.db.enqueue(dict(self.ev, ts=1), dict(kind='text', text='long task result'))
        outbox = Outbox(self.db, self.transport, lambda msg: None)
        outbox.once()
        self.assertFalse(outbox.once())
        self.assertEqual(len(self.calls), 1)
        with self.db.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM outbox').fetchone()[0], 'waiting')

    def test_same_event_refresh_preserves_retry_receipt(self):
        self.db.enqueue(self.ev, dict(kind='text', text='result'))
        self.responses = [TimeoutError()]
        Outbox(self.db, self.transport, lambda msg: None).once()
        self.db.refresh(self.ev)
        with self.db.connect() as db:
            receipt = json.loads(db.execute('SELECT receipt FROM outbox').fetchone()[0])
        self.assertEqual(receipt['body']['msg_seq'], 1)

    def test_control_reply_takes_priority_over_attachment_backlog(self):
        self.db.enqueue(self.ev, dict(kind='media', path='unused'), 'task')
        self.db.enqueue(self.ev, dict(kind='text', text='control reply'))
        item = self.db.pending()
        self.assertEqual(item['payload']['text'], 'control reply')

    def test_expired_media_is_reuploaded_with_original_reply_sequence(self):
        path = Path(self.tmp.name) / 'image.png'
        path.write_bytes(b'fake-png')
        receipt = dict(file_expiry=1, file_info='old', body=dict(msg_type=7, msg_id='m1', msg_seq=3, media={'file_info':'old'}))
        self.responses = [(200, dict(file_info='new')), (200, dict(id='sent'))]
        self.transport.send(self.ev, dict(kind='media', path=str(path)), receipt)
        self.assertEqual(self.calls[-1][1]['msg_seq'], 3)
        self.assertEqual(self.calls[-1][1]['media']['file_info'], 'new')

    def test_active_send_in_flight_at_restart_is_not_blindly_replayed(self):
        self.db.enqueue(self.ev, dict(kind='text', text='result'))
        item = self.db.pending()
        self.db.receipt(item['id'], {'body':{'content':'result','msg_type':0}})
        self.db.recover_outbox()
        self.assertIsNone(self.db.pending())
        with self.db.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM outbox').fetchone()[0], 'waiting')

    def test_timeout_after_expired_reply_fallback_is_not_retried(self):
        self.cfg['agent_active_push'] = True
        self.responses = [(400, dict(code=40034031, message='msgid已经过期')), TimeoutError()]
        with self.assertRaises(DeliveryError) as caught:
            self.transport.send(self.ev, dict(kind='text', text='result'))
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(self.calls), 2)

    def test_historical_media_msg_id_rejection_falls_back_without_reupload(self):
        self.cfg['agent_active_push'] = True
        path = Path(self.tmp.name)/'voice.wav'
        path.write_bytes(b'fake-wav')
        self.responses = [(200, dict(file_info='voice-file')), (400, dict(code=40034024, message='请求参数msg_id无效或越权')),
                          (200, dict(id='voice-sent'))]
        receipt = self.transport.send(self.ev, dict(kind='media',path=str(path)))
        self.assertEqual(receipt['message_id'], 'voice-sent')
        self.assertEqual(len([url for url, _ in self.calls if url.endswith('/files')]), 1)
        self.assertIn('msg_id', self.calls[1][1])
        self.assertNotIn('msg_id', self.calls[2][1])


if __name__ == '__main__':
    unittest.main()
