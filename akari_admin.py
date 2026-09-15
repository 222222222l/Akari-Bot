#!/usr/bin/env python3
"""Local authenticated control panel. Python standard library only."""
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit, parse_qs

from akari_admin_schema import FIELDS, GROUPS, validate
from akari_runtime import fingerprint

STATIC = Path(__file__).resolve().parent / 'admin'
SERVICE = 'akari-bot.service'


def atomic_json(path, data):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class Admin:
    def __init__(self, base):
        self.base = Path(base).resolve()
        self.state = self.base / 'state'
        self.private = self.state / 'admin'
        self.private.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.private.chmod(0o700)
        self.lock = threading.RLock()
        self.sessions = {}
        self.failures = deque(maxlen=100)
        self.token_path = self.private / 'access.token'
        if not self.token_path.exists():
            fd = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(secrets.token_urlsafe(32) + '\n')
        self.token_path.chmod(0o600)
        self.token = self.token_path.read_text().strip()
        if len(self.token) < 24:
            raise RuntimeError('控制台访问口令无效，请重新生成 state/admin/access.token')

    def path(self, scope):
        if scope == 'bot':
            return self.base / 'config.json'
        if scope == 'music':
            config = json.loads((self.base / 'config.json').read_text())
            path = Path(config.get('music_config', self.base / 'music/config.json'))
            return path if path.is_absolute() else self.base / path
        raise APIError(400, '未知配置类型')

    def raw(self, scope):
        return json.loads(self.path(scope).read_text(encoding='utf-8'))

    def public_config(self):
        result = {}
        for scope in ('bot', 'music'):
            try:
                raw = self.raw(scope)
            except FileNotFoundError:
                result[scope] = {'available': False, 'values': {}, 'revision': None}
                continue
            result[scope] = dict(available=True, revision=fingerprint(raw), values={
                f['key']: raw.get(f['key'], f['default']) for f in FIELDS if f['scope'] == scope})
        return dict(config=result, fields=FIELDS, groups=GROUPS, accounts=self.accounts())

    def save(self, body):
        scope, patch = body.get('scope'), body.get('changes')
        with self.lock:
            path = self.path(scope)
            current = self.raw(scope)
            if body.get('revision') != fingerprint(current):
                raise APIError(409, '配置已被其他窗口或程序修改，请重新载入后再编辑。')
            try:
                validate(scope, patch, current)
            except ValueError as e:
                raise APIError(400, str(e)) from None
            if not patch:
                return self.public_config()
            backup = self.private / ('%s-%s-%s.json' % (scope, time.strftime('%Y%m%d-%H%M%S'), secrets.token_hex(3)))
            atomic_json(backup, current)
            updated = dict(current, **patch)
            # Recheck immediately before commit. Other admin writes are serialized.
            if fingerprint(self.raw(scope)) != body['revision']:
                raise APIError(409, '保存期间配置发生变化，请重新载入。')
            atomic_json(path, updated)
            self.audit('config_saved', scope=scope, fields=sorted(patch))
            return self.public_config()

    def redact(self, value):
        text = str(value)
        hidden = [self.token]
        try:
            config = self.raw('bot')
            for key in ('app_secret', 'app_id', 'llm_key', 'api_key'):
                if config.get(key):
                    hidden.append(str(config[key]))
            key_path = config.get('llm_key_file')
            if key_path:
                p = Path(key_path).expanduser()
                if p.is_file() and p.stat().st_size < 8192:
                    hidden.append(p.read_text().strip())
        except (OSError, ValueError):
            pass
        for secret in hidden:
            if secret and len(secret) >= 4:
                text = text.replace(secret, '[已隐藏]')
        text = re.sub(r'(?i)(\b(?:bearer|qqbot)\s+)[^\s,\"\'}]+', r'\1[已隐藏]', text)
        text = re.sub(r'''(?ix)((?:[?&]|\b)(?:access_token|app_secret|api_key|password|token|secret|session_id|authorization)["']?\s*[:=]\s*["']?)[^\s&"',}\]]+''', r'\1[已隐藏]', text)
        return text

    def audit(self, action, **data):
        entry = json.dumps(dict(time=time.strftime('%Y-%m-%d %H:%M:%S'), action=action, **data), ensure_ascii=False)
        with self.lock:
            with (self.private / 'audit.log').open('a') as f:
                f.write(entry + '\n')

    def login(self, token):
        with self.lock:
            now = time.time()
            while self.failures and self.failures[0] < now - 60:
                self.failures.popleft()
            if len(self.failures) >= 10:
                raise APIError(429, '尝试次数过多，请在一分钟后重试。')
            if not isinstance(token, str) or not secrets.compare_digest(token.encode(), self.token.encode()):
                self.failures.append(now)
                raise APIError(401, '访问口令不正确')
            self.sessions = {k: v for k, v in self.sessions.items() if v['expires'] > now}
            if len(self.sessions) >= 64:
                self.sessions.pop(next(iter(self.sessions)))
            session = dict(csrf=secrets.token_urlsafe(24), expires=now + 12 * 3600)
            sid = secrets.token_urlsafe(32)
            self.sessions[sid] = session
            return sid, session

    def session(self, cookie):
        try:
            parsed = SimpleCookie(cookie or '')
            sid = parsed['akari_session'].value
        except (KeyError, ValueError):
            raise APIError(401, '请先登录控制台') from None
        with self.lock:
            session = self.sessions.get(sid)
            if not session or session['expires'] < time.time():
                self.sessions.pop(sid, None)
                raise APIError(401, '登录已过期，请重新登录')
            return sid, session

    def accounts(self):
        """Recent identities from actual received-message envelopes, never nick matching."""
        path=self.state/'tasks.sqlite3'
        if not path.exists():return []
        with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
            rows=db.execute('SELECT event FROM outbox ORDER BY id DESC LIMIT 300').fetchall()
        seen=set();items=[]
        for row in rows:
            ev=json.loads(row[0]);uid=ev.get('user_openid','')
            if not isinstance(uid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',uid) or uid in seen:continue
            seen.add(uid)
            items.append(dict(openid=uid,nick=str(ev.get('nick') or '未记录昵称')[:80],
                              scene='群聊' if ev.get('scene')=='group' else '私聊',last_seen=ev.get('ts')))
            if len(items)>=30:break
        return items

    def jobs(self):
        path = self.state / 'tasks.sqlite3'
        if not path.exists():
            return dict(items=[], counts={}, available=False)
        with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
            counts = dict(db.execute('SELECT status, count(*) FROM jobs GROUP BY status'))
            rows = db.execute('SELECT data FROM jobs ORDER BY created DESC LIMIT 40').fetchall()
        items = []
        for row in rows:
            j = json.loads(row[0])
            result = {k: j.get(k) for k in ('id', 'status', 'created', 'updated', 'started', 'finished')}
            operation = j.get('operation')
            result['operation'] = str(operation.get('kind', ''))[:40] if isinstance(operation, dict) else ''
            result['prompt'] = self.redact(j.get('prompt') or '')[:300]
            result['error'] = self.redact(j.get('error') or '')[:2000]
            items.append(result)
        return dict(items=items, counts=counts, available=True)

    def service(self):
        try:
            p = subprocess.run(['systemctl', 'show', SERVICE, '--property=ActiveState,SubState,MainPID'], capture_output=True, text=True, timeout=4)
            if p.returncode:
                return dict(active='unknown', sub='无法读取 systemd', pid=0)
            data = dict(line.split('=', 1) for line in p.stdout.splitlines() if '=' in line)
            return dict(active=data.get('ActiveState', 'unknown'), sub=data.get('SubState', ''), pid=int(data.get('MainPID', 0)))
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return dict(active='unknown', sub='无法读取 systemd', pid=0)

    def status(self):
        service = self.service()
        try:
            runtime = json.loads((self.state / 'runtime.json').read_text())
        except (OSError, ValueError):
            runtime = {}
        fresh = bool(runtime and time.time() - runtime.get('updated', 0) < 20 and runtime.get('pid') == service['pid'] and service['active'] == 'active')
        revision = fingerprint(self.raw('bot'))
        return dict(service=service, runtime=runtime if fresh else {}, telemetry=fresh,
                    pending_restart=revision != runtime.get('config_revision') if fresh else None,
                    jobs=self.jobs(), time=time.time())

    @contextlib.contextmanager
    def idle_guard(self, action):
        # Serialize the idle check with TaskStore.submit/claim across processes.
        # Keep new work out of the queue until systemd finishes the operation.
        path = self.state / 'tasks.sqlite3'
        if action == 'start' or not path.exists():
            yield
            return
        with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=3)) as db:
            try:
                db.execute('BEGIN IMMEDIATE')
            except sqlite3.OperationalError:
                raise APIError(409, '任务队列正在更新，请稍后重试。') from None
            try:
                yield
            finally:
                db.rollback()

    def control(self, action):
        if action not in ('start', 'restart', 'stop'):
            raise APIError(400, '不支持此服务操作')
        with self.lock, self.idle_guard(action):
            jobs = self.jobs()
            if action in ('restart', 'stop') and any(jobs['counts'].get(k, 0) for k in ('queued', 'running')):
                raise APIError(409, '有排队或执行中的任务，请等待任务结束后再操作。')
            try:
                proc = subprocess.run(['sudo', '-n', 'systemctl', action, SERVICE], capture_output=True, text=True, timeout=35)
            except subprocess.TimeoutExpired:
                raise APIError(504, '服务操作仍未返回，请刷新状态检查结果。') from None
            if proc.returncode:
                raise APIError(503, '服务操作失败：' + self.redact(proc.stderr[-1000:]))
            self.audit('service_' + action)
            return dict(ok=True, message='操作已完成，正在等待服务状态更新。')

    def logs(self, source='bot', level='all', query='', limit=300):
        sources = {'bot': self.state / 'bot.log', 'console': self.state / 'console.log', 'admin': self.private / 'audit.log'}
        if source not in sources or level not in ('all', 'error', 'warning', 'info'):
            raise APIError(400, '日志来源或级别无效')
        path = sources[source]
        records = []
        try:
            with path.open('rb') as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 512 * 1024))
                if size > 512 * 1024:
                    f.readline()
                raw = f.read().decode('utf-8', errors='replace')
        except FileNotFoundError:
            raw, size = '', 0
        raw = self.redact(raw)
        for line in raw.splitlines():
            if records and not re.match(r'^\[\d\d-\d\d |^\{"time":', line):
                records[-1]['text'] += '\n' + line
            else:
                records.append(dict(text=line, level='info'))
        for record in records:
            text = record['text']
            if re.search(r'\b(error|exception|traceback|failed|failure|timed.?out)\b|失败|异常', text, re.I):
                record['level'] = 'error'
            elif re.search(r'\b(warn(?:ing)?|retry|backoff)\b|重试', text, re.I):
                record['level'] = 'warning'
        counts = {kind: sum(r['level'] == kind for r in records) for kind in ('error', 'warning', 'info')}
        matching = [r for r in records if (level == 'all' or r['level'] == level) and query.casefold() in r['text'].casefold()]
        return dict(records=matching[-max(1, min(limit, 1000)):], counts=counts, bytes=size,
                    source=source, truncated=size > 512 * 1024, time=time.time())

    def diagnostics(self):
        config = self.raw('bot')
        def probe(item):
            key, name = item
            parsed = urlsplit(config.get(key, ''))
            start = time.monotonic()
            if parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
                return dict(name=name, ok=None, detail='非本机地址，未进行探测')
            try:
                port = parsed.port or (443 if parsed.scheme == 'https' else 80)
                with socket.create_connection((parsed.hostname, port), timeout=2):
                    pass
                return dict(name=name, ok=True, detail='本机端口可连接', ms=round((time.monotonic() - start) * 1000))
            except (OSError, ValueError):
                return dict(name=name, ok=False, detail='本机端口连接失败，请检查对应服务')
        with ThreadPoolExecutor(max_workers=3) as pool:
            checks = list(pool.map(probe, [('llm_base', '对话模型'), ('comfy_url', 'ComfyUI'), ('voice_url', '语音服务')]))
        try:
            proc = subprocess.run(['nvidia-smi', '--query-gpu=index,name,memory.total,memory.used,utilization.gpu', '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5)
            gpus = []
            for line in proc.stdout.splitlines() if proc.returncode == 0 else []:
                idx, name, total, used, util = [v.strip() for v in line.split(',')]
                gpus.append(dict(index=idx, name=name, total=int(total), used=int(used), utilization=int(util)))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            gpus = []
        return dict(checks=checks, gpus=gpus, note='连通性检查仅验证端口；不调用模型，不产生 QQ 消息。', time=time.time())


class Handler(BaseHTTPRequestHandler):
    server_version = 'AkariConsole'
    def log_message(self, format, *args):
        pass  # Do not persist URLs, cookies or login inputs.

    @property
    def app(self):
        return self.server.app

    def send(self, status, data, content_type='application/json; charset=utf-8', headers=None):
        body = json.dumps(data, ensure_ascii=False).encode() if content_type.startswith('application/json') else data
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise APIError(415, '请求必须为 JSON')
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 32768:
                raise ValueError()
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, UnicodeError):
            raise APIError(400, '请求内容无效或过大') from None

    def route(self):
        self.connection.settimeout(10)
        host = self.headers.get('Host', '')
        if host not in self.server.allowed_hosts:
            raise APIError(403, '不允许此访问地址')
        parsed = urlsplit(self.path)
        path = parsed.path
        if self.command == 'GET' and path in ('/', '/app.js', '/style.css', '/favicon.svg'):
            file = STATIC / {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css', '/favicon.svg': 'favicon.svg'}[path]
            kind = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}[file.suffix]
            return self.send(200, file.read_bytes(), kind)
        if self.command == 'POST':
            origin = self.headers.get('Origin')
            if origin and origin not in ('http://' + host, 'https://' + host):
                raise APIError(403, '跨站请求已拒绝')
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise APIError(403, '跨站请求已拒绝')
        if path == '/api/login' and self.command == 'POST':
            sid, session = self.app.login(self.body().get('token'))
            return self.send(200, {'csrf': session['csrf']}, headers={'Set-Cookie': 'akari_session=' + sid + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200'})
        sid, session = self.app.session(self.headers.get('Cookie'))
        if self.command == 'POST':
            if not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf']):
                raise APIError(403, '会话校验失败，请重新登录')
            body = self.body()
            if path == '/api/logout':
                with self.app.lock:
                    self.app.sessions.pop(sid, None)
                return self.send(200, {'ok': True}, headers={'Set-Cookie': 'akari_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
            if path == '/api/config':
                return self.send(200, self.app.save(body))
            if path == '/api/service':
                return self.send(200, self.app.control(body.get('action')))
            if path == '/api/diagnostics':
                return self.send(200, self.app.diagnostics())
        if self.command == 'GET':
            if path == '/api/session':
                return self.send(200, {'csrf': session['csrf']})
            if path == '/api/config':
                return self.send(200, self.app.public_config())
            if path == '/api/status':
                return self.send(200, self.app.status())
            if path == '/api/logs':
                params = parse_qs(parsed.query)
                return self.send(200, self.app.logs(params.get('source', ['bot'])[0], params.get('level', ['all'])[0], params.get('q', [''])[0][:200]))
        raise APIError(404, '页面或接口不存在')

    def handle_request(self):
        try:
            self.route()
        except APIError as e:
            self.send(e.status, {'error': e.message})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as e:
            self.app.audit('api_error', error=self.app.redact(type(e).__name__ + ': ' + str(e)))
            self.send(500, {'error': '服务读取失败，请查看管理日志或服务日志。'})

    do_GET = handle_request
    do_POST = handle_request


def make_server(base, port=18763):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.app = Admin(base)
    actual = server.server_address[1]
    server.allowed_hosts = {'127.0.0.1:' + str(actual), 'localhost:' + str(actual)}
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default=str(Path(__file__).resolve().parent))
    parser.add_argument('--port', type=int, default=18763)
    args = parser.parse_args()
    server = make_server(args.base, args.port)
    print('Akari 控制台：http://127.0.0.1:%s' % server.server_address[1], flush=True)
    print('访问口令文件：%s' % server.app.token_path, flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
