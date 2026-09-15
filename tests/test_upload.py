import copy
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from akari_delivery import QQTransport, Outbox
from akari_errors import DeliveryError
from akari_tasks import TaskStore
from akari_upload import ChunkedUploader, file_blocks, file_hashes, HASH_PREFIX_BYTES, put_part
from test_tasks import event


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'movie.mp4'
        self.path.write_bytes(b'abcdefghijk')
        self.receipt, self.saved, self.calls, self.puts = {}, [], [], []
        self.prepared = dict(upload_id='upload', block_size=4, parts=[
            dict(index=i, presigned_url='https://cos.invalid/part%d' % i) for i in (1,2,3)],
            upload_config=dict(concurrency=1))
        self.fail_part = None

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, url, body, timeout):
        self.calls.append((url, copy.deepcopy(body)))
        if url.endswith('/upload_prepare'):
            return self.prepared
        if url.endswith('/files'):
            return dict(file_info='one-complete-file', ttl=600)
        return {}

    def put(self, url, path, offset, length, timeout):
        data = b''.join(file_blocks(path, offset, length))
        self.puts.append(data)
        if data == self.fail_part:
            raise DeliveryError('injected failure')

    def upload(self):
        return ChunkedUploader(self.post, {}, self.put, lambda _: None).upload('https://qq.invalid/v2/groups/g',
            self.path, 2, self.receipt, lambda r:self.saved.append(copy.deepcopy(r)))

    def test_complete_file_without_base64_and_short_last_part(self):
        result = self.upload()
        self.assertEqual(result['file_info'], 'one-complete-file')
        self.assertEqual(self.puts, [b'abcd', b'efgh', b'ijk'])
        prepare = self.calls[0][1]
        self.assertEqual(prepare['file_size'], 11)
        self.assertEqual(prepare['md5'], hashlib.md5(b'abcdefghijk').hexdigest())
        finish = [b for u,b in self.calls if u.endswith('/upload_part_finish')]
        self.assertEqual(finish[-1]['block_size'], 3)
        self.assertEqual(finish[-1]['md5'], hashlib.md5(b'ijk').hexdigest())
        self.assertEqual(self.calls[-1][1], {'upload_id':'upload'})
        self.assertNotIn('file_data', str(self.calls))
        self.assertEqual(self.saved[-1]['upload_progress']['bytes'], 11)

    def test_resume_skips_confirmed_parts_and_reuses_prepare(self):
        self.fail_part = b'efgh'
        with self.assertRaises(DeliveryError):self.upload()
        self.assertIn(1, self.receipt['upload']['completed'])
        before = len(self.puts)
        self.fail_part = None
        self.upload()
        self.assertNotIn(b'abcd', self.puts[before:])
        self.assertEqual(sum(u.endswith('/upload_prepare') for u,b in self.calls), 1)

    def test_bad_part_ranges_never_upload_or_complete(self):
        self.prepared['parts'].pop(1)
        with self.assertRaises(DeliveryError):self.upload()
        self.assertEqual(self.puts, [])
        self.assertFalse(any(u.endswith('/files') for u,b in self.calls))

    def test_pending_part_finish_retries_without_resending_part(self):
        original, attempts = self.post, []
        def post(url, body, timeout):
            if url.endswith('/upload_part_finish') and not attempts:
                attempts.append(1)
                raise DeliveryError('processing', code='40093001')
            return original(url, body, timeout)
        self.post = post
        self.upload()
        self.assertEqual(self.puts.count(b'abcd'), 1)

    def test_changed_file_discards_prior_upload(self):
        self.upload()
        self.path.write_bytes(b'12345678901')
        self.upload()
        self.assertEqual(sum(u.endswith('/upload_prepare') for u,b in self.calls), 2)

    def test_hash_prefix_is_qq_protocol_length(self):
        self.path.write_bytes(b'a' * HASH_PREFIX_BYTES + b'b' * 65536)
        hashes = file_hashes(self.path)
        self.assertEqual(hashes['md5_10m'], hashlib.md5(b'a' * HASH_PREFIX_BYTES).hexdigest())
        self.assertNotEqual(hashes['md5_10m'], hashes['md5'])

    def test_put_uses_streaming_body_and_no_qq_token(self):
        class Response:
            status = 200
            def __enter__(self):return self
            def __exit__(self, *args):pass
        class Opener:
            def open(inner, req, timeout):
                self.assertEqual(req.get_method(), 'PUT')
                self.assertIsNone(req.get_header('Authorization'))
                self.assertEqual(req.get_header('Content-length'), '3')
                self.assertEqual(b''.join(req.data), b'ijk')
                return Response()
        with patch('akari_upload.urllib.request.build_opener', return_value=Opener()):
            put_part('https://cos.invalid/part', self.path, 8, 3, 30)

    def test_over_twenty_mib_uses_chunked_transport_and_one_message(self):
        with self.path.open('wb') as stream:stream.truncate(21 * 1048576)
        db = TaskStore(Path(self.tmp.name)/'state')
        calls = []
        def http(url, method, headers, body, timeout):
            calls.append((url, body))
            if url.endswith('/upload_prepare'):
                return 200, dict(upload_id='big', block_size=8*1048576, parts=[
                    dict(index=i, presigned_url='https://cos.invalid/%d'%i) for i in (1,2,3)])
            if url.endswith('/files'):return 200,dict(file_info='complete')
            return 200,dict(id='sent')
        transport = QQTransport(dict(api_base='https://qq.invalid', app_id='test', dry_run=False), db, http, lambda:'fake', print)
        with patch('akari_delivery.ChunkedUploader', side_effect=lambda post,cfg:ChunkedUploader(post,cfg,self.put)):
            receipt = transport.send(dict(event(group='g'),ts=time.time()), dict(kind='media',path=str(self.path)))
        self.assertEqual(receipt['message_id'], 'sent')
        self.assertEqual(sum(u.endswith('/messages') for u,b in calls), 1)
        self.assertEqual(sum(map(len,self.puts)), 21*1048576)

    def test_text_delivery_does_not_wait_for_blocked_upload(self):
        db = TaskStore(Path(self.tmp.name)/'state')
        ev = dict(event(),ts=time.time())
        db.enqueue(ev,dict(kind='media',path=str(self.path)), 'job')
        db.enqueue(ev,dict(kind='text',text='progress'))
        entered, release, replied = threading.Event(), threading.Event(), threading.Event()
        class Transport:
            def send(inner, ev, payload, *args):
                if payload['kind'] == 'media':
                    entered.set(); release.wait(5)
                else:replied.set()
                return dict(message_id='ok')
        outbox = Outbox(db,Transport(),lambda _:None)
        thread = threading.Thread(target=outbox.once,args=('media',))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            outbox.once('text')
            self.assertTrue(replied.is_set())
        finally:
            release.set();thread.join(3)

    def test_new_reply_preserves_uploaded_parts(self):
        db = TaskStore(Path(self.tmp.name)/'state')
        ev = dict(event(),ts=time.time())
        db.enqueue(ev,dict(kind='media',path=str(self.path)))
        item = db.pending()
        db.receipt(item['id'],dict(upload=dict(completed=[1]), body=dict(msg_id='old',msg_seq=1)))
        db.delivery(item['id'],'waiting','failed')
        db.refresh(dict(ev,msg_id='new'),retry=True)
        receipt = db.pending()['receipt']
        self.assertEqual(receipt['upload']['completed'],[1])
        self.assertNotIn('body',receipt)


if __name__ == '__main__':unittest.main()
