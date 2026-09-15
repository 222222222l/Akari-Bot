"""QQ official prepare / COS PUT / part_finish / files upload protocol.

Protocol source: tencent-connect/qqbot-agent-sdk (api_client.py, dto.py).
File bytes never enter a Base64 JSON body or carry QQ credentials to COS.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from akari_errors import DeliveryError

HASH_PREFIX_BYTES = 10002432  # QQ protocol value; NOT 10 * 1024 * 1024.


def file_hashes(path):
    md5, sha1, prefix = hashlib.md5(), hashlib.sha1(), hashlib.md5()
    read = 0
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            md5.update(block)
            sha1.update(block)
            prefix.update(block[:max(0, HASH_PREFIX_BYTES - read)])
            read += len(block)
    return dict(md5=md5.hexdigest(), sha1=sha1.hexdigest(), md5_10m=prefix.hexdigest())


def file_blocks(path, offset, length):
    with Path(path).open('rb') as stream:
        stream.seek(offset)
        while length:
            data = stream.read(min(65536, length))
            if not data:
                raise DeliveryError('上传期间文件被截断，请重新登记最终产物')
            length -= len(data)
            yield data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def put_part(url, path, offset, length, timeout):
    if urllib.parse.urlsplit(url).scheme != 'https':
        raise DeliveryError('QQ 未返回 HTTPS 分片地址')
    request = urllib.request.Request(url, data=file_blocks(path, offset, length), method='PUT',
        headers={'Content-Length': str(length), 'Content-Type':'application/octet-stream'})
    try:
        # Signed URL is the only credential; do not use the QQ JSON HTTP helper.
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise DeliveryError('分片上传 HTTP %d' % response.status, retryable=response.status >= 500)
    except urllib.error.HTTPError as exc:
        raise DeliveryError('分片上传 HTTP %d' % exc.code, retryable=exc.code == 429 or exc.code >= 500,
                            code=exc.code, category='upload_url' if exc.code == 403 else '') from None
    except (OSError, urllib.error.URLError) as exc:
        raise DeliveryError('分片连接异常：' + type(exc).__name__, retryable=True) from None


class ChunkedUploader:
    def __init__(self, post, cfg, put=put_part, sleep=time.sleep):
        self.post, self.cfg, self.put, self.sleep = post, cfg, put, sleep

    def upload(self, route, path, file_type, receipt, checkpoint):
        path = Path(path)
        stat = path.stat()
        fingerprint = [str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ino]
        if not stat.st_size:
            raise DeliveryError('不能上传空文件')
        deadline = time.monotonic() + max(30, float(self.cfg.get('agent_upload_timeout_secs', 1800)))

        def retry(call, limit=3):
            for attempt in range(limit):
                if time.monotonic() >= deadline:
                    raise DeliveryError('附件上传达到时限，已保留分片进度', retryable=True)
                try:
                    return call()
                except DeliveryError as exc:
                    if not exc.retryable or attempt == limit - 1:
                        raise
                    self.sleep(min(2 ** attempt, max(0, deadline - time.monotonic())))

        state = receipt.get('upload') or {}
        if state.get('fingerprint') != fingerprint or state.get('expires', 0) < time.time():
            receipt['upload_progress'] = dict(name=path.name, bytes=0, total=stat.st_size, parts=0,
                                               total_parts=0, phase='prepare')
            checkpoint(receipt)
            hashes = file_hashes(path)
            prepared = retry(lambda: self.post(route + '/upload_prepare', dict(file_type=file_type,
                file_name=path.name, file_size=stat.st_size, **hashes), 60))
            state = dict(fingerprint=fingerprint, prepared=prepared, completed=[], expires=time.time() + 900)
            receipt['upload'] = state
            checkpoint(receipt)
        prepared = state['prepared']
        try:
            block = int(prepared.get('block_size', 0))
            parts = sorted(prepared.get('parts') or [], key=lambda p: int(p['index']))
            upload_id = prepared['upload_id']
            if not upload_id or block <= 0 or not parts:
                raise ValueError()
            cursor = 0
            for part in parts:
                offset = (int(part['index']) - 1) * block
                length = min(int(part.get('block_size') or block), stat.st_size - offset)
                if offset != cursor or length <= 0 or not part.get('presigned_url'):
                    raise ValueError()
                cursor += length
            if cursor != stat.st_size:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            receipt.pop('upload', None)
            checkpoint(receipt)
            raise DeliveryError('QQ 分片响应不完整或分片范围不连续') from None
        config = prepared.get('upload_config') or prepared
        concurrency = max(1, min(int(config.get('concurrency') or 1), int(self.cfg.get('agent_upload_concurrency', 2)), 4))
        part_timeout = max(1, min(float(config.get('retry_timeout') or 120), 300))
        lock = threading.Lock()
        completed = set(state.get('completed', []))

        def progress():
            uploaded = sum(min(int(p.get('block_size') or block), stat.st_size - (int(p['index']) - 1) * block)
                           for p in parts if int(p['index']) in completed)
            receipt['upload_progress'] = dict(name=path.name, bytes=uploaded, total=stat.st_size,
                                               parts=len(completed), total_parts=len(parts), phase='parts')
            state['completed'] = sorted(completed)
            checkpoint(receipt)

        progress()

        def transfer(part):
            index = int(part['index'])
            if index in completed:
                return
            offset = (index - 1) * block
            length = min(int(part.get('block_size') or block), stat.st_size - offset)
            md5 = hashlib.md5()
            for data in file_blocks(path, offset, length):
                md5.update(data)
            retry(lambda: self.put(part['presigned_url'], path, offset, length, min(120, max(1, deadline - time.monotonic()))))
            finish_deadline = min(deadline, time.monotonic() + part_timeout)
            while True:
                try:
                    retry(lambda: self.post(route + '/upload_part_finish', dict(upload_id=upload_id,
                        part_index=index, block_size=length, md5=md5.hexdigest()), 60))
                    break
                except DeliveryError as exc:
                    if exc.code != '40093001' or time.monotonic() >= finish_deadline:
                        raise
                    self.sleep(min(float(config.get('retry_delay') or 1), 5, max(0, finish_deadline - time.monotonic())))
            with lock:
                completed.add(index)
                progress()

        try:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                list(pool.map(transfer, parts))
        except DeliveryError as exc:
            if exc.category == 'upload_url':
                receipt.pop('upload', None)  # Expired signed URLs require a fresh prepare.
                checkpoint(receipt)
            raise
        current = path.stat()
        if [str(path.resolve()), current.st_size, current.st_mtime_ns, current.st_ino] != fingerprint:
            receipt.pop('upload', None)
            checkpoint(receipt)
            raise DeliveryError('上传期间文件发生变化，请等待生成完成后重新发送')
        receipt['upload_progress']['phase'] = 'complete'
        checkpoint(receipt)
        uploaded = retry(lambda: self.post(route + '/files', {'upload_id':upload_id}, 60))
        if not (uploaded.get('file_info') or uploaded.get('file_uuid')):
            raise DeliveryError('QQ 完成上传响应没有文件凭据')
        return dict(uploaded, file_info=uploaded.get('file_info') or uploaded['file_uuid'])
