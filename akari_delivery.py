"""QQ delivery with durable reply sequences, bounded retries and media support."""
import base64
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
from akari_errors import DeliveryError
from akari_upload import ChunkedUploader

CURRENT = None


def media_type(path):
    ext = Path(path).suffix.lower()
    if ext in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}:
        return 1
    if ext in {'.mp4', '.mov', '.webm', '.mkv'}:
        return 2
    if ext in {'.silk', '.amr', '.wav', '.mp3', '.ogg', '.flac', '.m4a'}:
        return 3
    return 4


def reply_fields(ev):
    """Shared by existing image/voice helpers to avoid duplicate msg_seq=1."""
    if not ev.get('msg_id'):
        return {}
    return dict(msg_id=ev['msg_id'], msg_seq=CURRENT.store.next_seq(ev) if CURRENT else 1)


class QQTransport:
    def __init__(self, cfg, store, http, token, log, invalidate_token=None):
        self.cfg, self.store, self.http, self.token, self.log = cfg, store, http, token, log
        self.invalidate_token = invalidate_token

    def route(self, ev):
        group = ev['scene'] == 'group'
        target = ev.get('group_openid') if group else ev.get('user_openid')
        if not target:
            raise DeliveryError('QQ 消息缺少接收目标')
        return self.cfg['api_base'].rstrip('/') + '/v2/' + ('groups/' if group else 'users/') + target

    def post(self, url, body, timeout=25):
        stage = urllib.parse.urlsplit(url).path.rsplit('/', 1)[-1]
        for attempt in range(2):
            try:
                status, data = self.http(url, 'POST',
                    {'Authorization': 'QQBot ' + self.token(), 'X-Union-Appid': str(self.cfg['app_id'])}, body, timeout)
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    data = json.loads(exc.read(4096))
                except (ValueError, OSError):
                    data = {}
            except Exception as exc:
                raise DeliveryError('QQ %s 连接异常：%s' % (stage, type(exc).__name__), retryable=True, stage=stage) from exc
            code = str(data.get('code') or data.get('err_code') or data.get('biz_code') or '')
            if status == 401 and attempt == 0 and self.invalidate_token:
                self.invalidate_token()
                continue
            if 200 <= status < 300 and code in ('', '0'):
                return data
            message = str(data.get('message') or '接口拒绝请求')[:240]
            category = 'quota' if code == '40093002' else ''
            if any(x in message.lower() for x in ('file too large', 'file size exceeds', '文件大小超过', '文件过大', '文件大小超限')):
                category = 'platform_size'
            elif status == 413:
                category = 'request_size'
            window = code in {'40034031', '40034024', '40034025'} or any(x in message.lower() for x in ('msgid已经过期', '主动消息', '回复次数'))
            raise DeliveryError('QQ %s HTTP %s code=%s：%s' % (stage, status, code, message),
                                retryable=(status == 429 or status >= 500) and category != 'quota', window=window,
                                code=code or str(status), category=category, stage=stage)
        raise DeliveryError('QQ 认证失败')

    def send(self, ev, payload, receipt=None, checkpoint=lambda r: None):
        receipt = dict(receipt or {})
        if self.cfg.get('dry_run', True):
            # Never contact QQ, fetch a token, or mark a dry run delivered.
            raise DeliveryError('dry_run：消息保留，未发送')
        route = self.route(ev)
        if receipt.get('file_expiry') and receipt['file_expiry'] < time.time():
            # Re-upload expired media but retain the message's reply sequence.
            # This path is only reached after an earlier explicit upload/send failure.
            receipt.pop('file_info', None)
            if receipt.get('body', {}).get('msg_type') == 7:
                receipt['reply'] = {k: receipt['body'][k] for k in ('msg_id', 'msg_seq') if k in receipt['body']}
                receipt.pop('body')
        if 'body' not in receipt:
            body = {'msg_type': 0, 'content': payload.get('text', '')}
            if payload.get('kind') == 'media':
                path = Path(payload['path'])
                cap = int(self.cfg.get('agent_upload_max_bytes', 512 * 1024 * 1024))
                if payload.get('root') and (path.is_symlink() or not path.resolve().is_relative_to(Path(payload['root']).resolve())):
                    raise DeliveryError('产物路径已改变，拒绝发送任务目录之外的内容')
                if not path.is_file():
                    raise DeliveryError('产物文件不存在：' + path.name)
                if path.stat().st_size > cap:
                    raise DeliveryError('文件 %.1f MiB，超过 bot 本地配置上限 %.1f MiB（不是 QQ 官方限额）' %
                        (path.stat().st_size / 1048576, cap / 1048576), category='local_size')
                if not receipt.get('file_info') or receipt.get('file_expiry', 0) < time.time():
                    file_type = receipt.get('upload_file_type') or payload.get('file_type') or media_type(path)
                    try:
                        if self.cfg.get('agent_chunked_upload', True):
                            uploaded = ChunkedUploader(self.post, self.cfg).upload(route, path, file_type, receipt, checkpoint)
                        else:
                            # Explicit compatibility mode, bounded independently from large-file limits.
                            with path.open('rb') as stream:
                                raw = stream.read(1024 * 1024 + 1)
                            if len(raw) > 1024 * 1024:
                                raise DeliveryError('兼容上传仅支持 1 MiB，请启用官方分片上传', category='local_size')
                            uploaded = self.post(route + '/files', dict(file_type=file_type,
                                file_data=base64.b64encode(raw).decode('ascii'), file_name=path.name, srv_send_msg=False), 60)
                    except DeliveryError as exc:
                        # Only an explicit upload rejection permits changing media type.
                        # Never retry an uncertain /messages result as a different file.
                        if file_type != 3 or str(exc.code) != '40093013':
                            raise
                        for key in ('upload', 'upload_progress', 'file_info', 'file_expiry'):
                            receipt.pop(key, None)
                        receipt['upload_file_type'] = 4
                        receipt['audio_fallback_reason'] = 'QQ audio duration limit (40093013)'
                        checkpoint(receipt)
                        self.log('QQ audio duration rejected; retrying original bytes as file: ' + path.name)
                        return self.send(ev, payload, receipt, checkpoint)
                    if not uploaded.get('file_info'):
                        raise DeliveryError('QQ 上传响应没有 file_info')
                    receipt.update(file_info=uploaded['file_info'],
                                   file_expiry=time.time() + int(uploaded.get('ttl') or 300))
                    receipt.pop('upload', None)
                    checkpoint(receipt)
                body = {'msg_type': 7, 'media': {'file_info': receipt['file_info']}}
                if receipt.get('upload_progress'):
                    receipt['upload_progress']['phase'] = 'message'
            window = float(self.cfg.get('qq_reply_window_secs', 240))
            fresh = ev.get('msg_id') and time.time() - ev.get('ts', 0) < window
            if 'reply' in receipt:
                body.update(receipt.pop('reply'))
                fresh = bool(body.get('msg_id'))
            elif fresh:
                seq = self.store.next_seq(ev)
                if seq <= int(self.cfg.get('qq_reply_max_messages', 5)):
                    body.update(msg_id=ev['msg_id'], msg_seq=seq)
                else:
                    fresh = False
            if not fresh and not self.cfg.get('agent_active_push', True):
                raise DeliveryError('等待新的 QQ 消息后补发', window=True)
            receipt['body'] = body
            checkpoint(receipt)  # Persist before the external send, including its idempotency fields.
        try:
            data = self.post(route + '/messages', receipt['body'])
        except DeliveryError as exc:
            if exc.retryable and 'msg_id' not in receipt['body']:
                # Active messages have no documented idempotency key. A lost
                # response must not trigger an automatic duplicate active send.
                raise DeliveryError('主动消息未确认送达，请查询结果或手动 /补发；' + str(exc)) from exc
            # Only an explicit invalid/expired-reply rejection permits switching to active.
            # An ambiguous timeout must retry the SAME body instead of duplicating delivery.
            if exc.window and 'msg_id' in receipt['body'] and self.cfg.get('agent_active_push', True):
                receipt['body'].pop('msg_id', None)
                receipt['body'].pop('msg_seq', None)
                checkpoint(receipt)
                try:
                    data = self.post(route + '/messages', receipt['body'])
                except DeliveryError as active_error:
                    if active_error.retryable:
                        raise DeliveryError('主动消息未确认送达，请手动补发或查看结果；' + str(active_error)) from active_error
                    raise
            else:
                raise
        if not data.get('id'):
            raise DeliveryError('QQ 未返回消息 id，不能确认送达', retryable='msg_id' in receipt['body'])
        receipt['message_id'] = data['id']
        return receipt


class Outbox:
    def __init__(self, store, transport, log):
        self.store, self.transport, self.log = store, transport, log

    def adapt_rejected_video(self, item, exc):
        """Adapt once after a pre-send rejection, never after ambiguous delivery.

        850012 is not universally a size error. This account accepts a 190 MiB
        prepare and rejects 210 MiB, so only apply this workaround above budget.
        """
        cfg = self.transport.cfg
        if not cfg.get('agent_video_auto_adapt', True) or not item.get('job_id'):
            return False
        payload = item['payload']
        if payload.get('kind') != 'media' or (payload.get('file_type') or media_type(payload.get('path', ''))) != 2:
            return False
        size_rejection = exc.category in ('local_size', 'platform_size', 'request_size')
        proxy_rejection = exc.stage == 'upload_prepare' and exc.code == '850012'
        if not (size_rejection or proxy_rejection) or exc.stage == 'messages':
            return False
        path = Path(payload['path'])
        budget = max(1048576, min(int(cfg.get('agent_video_fallback_max_bytes', 190*1048576)),
                                 int(cfg.get('agent_upload_max_bytes', 512*1048576))))
        job = self.store.get(item['job_id'])
        if not job or path.is_symlink() or not path.is_file() or path.stat().st_size <= budget:
            return False
        root = Path(job['directory']) / 'artifacts'
        if not path.resolve().is_relative_to(root.resolve()):
            return False
        ev = dict(item['event'], key='delivery-adapt:' + str(item['id']))
        child = self.store.submit(ev, 'QQ 拒绝原视频上传，自动分段交付 %s，保留原文件。' % path.name,
            max(300, int(cfg.get('agent_default_timeout', 1800))), job['cwd'], parent=job,
            max_queued=int(cfg.get('agent_queue_limit', 30)), operation=dict(kind='adapt', source=str(path),
                root=str(root), max_bytes=budget, delivery_parent=job['id'], delivery_outbox=item['id']))
        current = self.store.get_receipt(item['id'])
        current['fallback_job'] = child['id']
        self.store.delivery(item['id'], 'replaced', '原视频上传被拒绝，改用分段任务 ' + child['id'], receipt=current)
        hint = ('【视频自动适配】%s（%.1f MiB）的上传被 QQ 拒绝。\n'
                '已创建任务 %s，按每段不超过 %.1f MiB 分段并发送，原文件保留。\n'
                '查询：/进度 %s；原任务 /补发 也会重试这些分段。') % (
                    path.name, path.stat().st_size/1048576, child['id'], budget/1048576, child['id'])
        self.store.enqueue(item['event'], dict(kind='text',text=hint), job['id'], 'delivery-adapt-notice:'+str(item['id']))
        self.log('outbox adapted id=%s child=%s reason=%s' % (item['id'], child['id'], exc))
        return True

    def once(self, lane=None):
        item = self.store.pending(lane)
        if not item:
            return False
        try:
            receipt = self.transport.send(item['event'], item['payload'], item['receipt'],
                                         lambda r: self.store.receipt(item['id'], r))
            self.store.delivery(item['id'], 'sent', receipt=receipt)
            self.log('outbox sent id=%s task=%s' % (item['id'], item['job_id']))
        except DeliveryError as exc:
            try:
                if self.adapt_rejected_video(item, exc):
                    return True
            except Exception as adapt_error:
                self.log('outbox adaptation deferred id=%s error=%s' % (item['id'], type(adapt_error).__name__))
            retry = exc.retryable and item['attempts'] < 2
            self.store.delivery(item['id'], 'pending' if retry else 'waiting', str(exc),
                                delay=min(300, 5 * 3 ** item['attempts']))
            self.log('outbox deferred id=%s %s' % (item['id'], exc))
            if not retry and item['payload'].get('kind') == 'media':
                path = Path(item['payload']['path'])
                hint = '【附件待发送】%s：%s\n已保留文件；可用 /补发 %s 重试。' % (path.name, str(exc)[:200], item['job_id'] or '最近')
                if item['job_id']:
                    hint += '\n正文：/结果 %s；文本附件可用 /预览 %s 序号 查看。' % (item['job_id'], item['job_id'])
                    job = self.store.get(item['job_id']) or {}
                    index = next((i for i, a in enumerate(job.get('artifacts', []), 1) if a['path'] == str(path)), None)
                    if index and media_type(path) == 2 and exc.category in ('local_size', 'platform_size', 'request_size'):
                        hint += '\n可分段成独立视频：/适配 %s %d 20（每段目标 20 MiB，保留原文件）' % (item['job_id'], index)
                    if index:
                        hint += '\n下载链接：/下载 %s %d（需已配置下载站点）' % (item['job_id'], index)
                    if exc.category == 'quota':
                        hint += '\nQQ 当日上传额度已用完，分片或分段都不能绕过，请额度恢复后重试。'
                self.store.enqueue(item['event'], dict(kind='text', text=hint), item['job_id'], 'delivery-error:%s' % item['id'])
        except Exception as exc:
            self.store.delivery(item['id'], 'waiting', '交付异常：' + type(exc).__name__)
            self.log('outbox exception id=%s %r' % (item['id'], exc))
        return True

    def run(self, stop, lane=None):
        while not stop.is_set():
            try:
                sent = self.once(lane)
            except Exception as exc:
                self.log('outbox loop error %r' % (exc,))
                sent = False
            stop.wait(1.0 if sent else 0.3)
