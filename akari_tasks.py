"""Durable task/session/outbox state. No QQ or model calls in this module."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

TERMINAL = {'succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted'}
LABELS = dict(queued='排队中', running='执行中', succeeded='执行完成', failed='失败',
              timed_out='超时', cancelled='已取消', interrupted='执行中断')


def scope_of(ev):
    # A group is a delivery target, never an authentication/session identity.
    return json.dumps([ev['scene'], ev.get('group_openid', ''), ev['user_openid']], separators=(',', ':'))


def split_full(text, limit=800):
    """Lossless bounded chunks, including long code/URLs with no punctuation."""
    limit = max(1, int(limit))
    parts = []
    while text:
        end = min(len(text), limit)
        if end < len(text):
            boundary = text.rfind('\n', end // 2, end)
            if boundary >= 0:
                end = boundary + 1
        parts.append(text[:end])
        text = text[end:]
    return parts or ['']


def atomic_text(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class TaskStore:
    def __init__(self, state):
        self.root = Path(state).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs = self.root / 'tasks'
        self.jobs.mkdir(exist_ok=True, mode=0o700)
        self.path = self.root / 'tasks.sqlite3'
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, session_id TEXT NOT NULL,
                    event_key TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created);
                CREATE TABLE IF NOT EXISTS sessions (scope TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe TEXT UNIQUE,
                    scope TEXT NOT NULL, job_id TEXT, event TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', receipt TEXT NOT NULL DEFAULT '{}');
                CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(status, next_attempt);
                CREATE TABLE IF NOT EXISTS sequences (route TEXT PRIMARY KEY, seq INTEGER NOT NULL);
            ''')
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def session(self, scope, **changes):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM sessions WHERE scope=?', (scope,)).fetchone()
            data = json.loads(row['data']) if row else dict(mode='chat', generation=0)
            data.update(changes)
            db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?)', (scope, json.dumps(data)))
            return data

    def submit(self, ev, prompt, timeout, cwd, parent=None, max_queued=30, operation=None):
        scope = scope_of(ev)
        event_key = scope + ':' + (ev.get('key') or ev['msg_id'])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM jobs WHERE event_key=?', (event_key,)).fetchone()
            if row:
                return json.loads(row['data'])
            count = db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if count >= max_queued:
                raise ValueError('任务队列已满，请稍后重试或取消不需要的任务。')
            session = db.execute('SELECT data FROM sessions WHERE scope=?', (scope,)).fetchone()
            settings = json.loads(session['data']) if session else {}
            sid = hashlib.sha256((scope + ':' + str(settings.get('generation', 0))).encode()).hexdigest()[:32]
            jid = uuid.uuid4().hex[:12]
            directory = self.jobs / jid
            directory.mkdir(mode=0o700)
            (directory / 'artifacts').mkdir()
            (directory / 'inputs').mkdir()
            job = dict(id=jid, scope=scope, session_id=parent['session_id'] if parent else sid,
                       event_key=event_key, event=dict(ev), prompt=prompt, timeout=timeout,
                       cwd=parent['cwd'] if parent else settings.get('cwd', cwd),
                       directory=str(directory), status='queued', created=time.time(),
                       updated=time.time(), progress='等待执行', cancel_requested=False,
                       result='', artifacts=[], parent=parent['id'] if parent else None)
            if operation:
                job['operation'] = operation
            atomic_text(directory / 'request.txt', prompt)
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                       (jid, scope, job['session_id'], event_key, 'queued', job['created'], json.dumps(job)))
            return job

    def get(self, jid, scope=None):
        with self.connect() as db:
            where, args = ('scope=?', [scope]) if scope is not None else ('1=1', [])
            if jid not in ('最近', 'latest', ''):
                where += ' AND id=?'
                args.append(jid)
            row = db.execute('SELECT data FROM jobs WHERE ' + where + ' ORDER BY created DESC LIMIT 1', args).fetchone()
            return json.loads(row['data']) if row else None

    def list(self, scope=None, limit=10):
        with self.connect() as db:
            rows = db.execute('SELECT data FROM jobs' + (' WHERE scope=?' if scope is not None else '')
                              + ' ORDER BY created DESC LIMIT ?', ([scope] if scope is not None else []) + [limit])
            return [json.loads(r['data']) for r in rows]

    @staticmethod
    def _save(db, job):
        job['updated'] = time.time()
        db.execute('UPDATE jobs SET status=?, data=? WHERE id=?', (job['status'], json.dumps(job), job['id']))
        return job

    def update(self, jid, **changes):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM jobs WHERE id=?', (jid,)).fetchone()
            job = json.loads(row['data'])
            job.update(changes)
            return self._save(db, job)

    def claim(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("""SELECT data FROM jobs j WHERE status='queued'
                AND NOT EXISTS (SELECT 1 FROM jobs r WHERE r.status='running' AND r.session_id=j.session_id)
                ORDER BY created LIMIT 1""").fetchone()
            if not row:
                return None
            job = json.loads(row['data'])
            job.update(status='running', started=time.time(), progress='准备执行')
            return self._save(db, job)

    def cancel(self, jid, scope):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM jobs WHERE id=? AND scope=?', (jid, scope)).fetchone()
            if not row:
                return None
            job = json.loads(row['data'])
            if job['status'] not in TERMINAL:
                job['cancel_requested'] = True
                if job['status'] == 'queued':
                    job.update(status='cancelled', finished=time.time())
                self._save(db, job)
            return job

    def enqueue(self, ev, payload, job_id=None, dedupe=None):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO outbox(dedupe,scope,job_id,event,payload) VALUES (?,?,?,?,?)',
                       (dedupe, scope_of(ev), job_id, json.dumps(ev), json.dumps(payload)))

    def pending(self, lane=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            where = " AND json_extract(payload,'$.kind')='media'" if lane == 'media' else (
                " AND json_extract(payload,'$.kind')!='media'" if lane == 'text' else '')
            row = db.execute("SELECT * FROM outbox WHERE status='pending' AND next_attempt<=?" + where +
                " ORDER BY (job_id IS NOT NULL), id LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return None
            db.execute("UPDATE outbox SET status='sending' WHERE id=?", (row['id'],))
            return self._decode_item(row)

    @staticmethod
    def _decode_item(row):
        item = dict(row)
        for field in ('event', 'payload', 'receipt'):
            item[field] = json.loads(item[field])
        return item

    def delivery(self, oid, status, error='', receipt=None, delay=0):
        with self.connect() as db:
            db.execute('UPDATE outbox SET status=?, error=?, next_attempt=?, attempts=attempts+1 WHERE id=?',
                       (status, error[:600], time.time() + delay, oid))
            if receipt is not None:
                db.execute('UPDATE outbox SET receipt=? WHERE id=?', (json.dumps(receipt), oid))

    def receipt(self, oid, receipt):
        with self.connect() as db:
            db.execute('UPDATE outbox SET receipt=? WHERE id=?', (json.dumps(receipt), oid))

    def get_receipt(self, oid):
        with self.connect() as db:
            row = db.execute('SELECT receipt FROM outbox WHERE id=?', (oid,)).fetchone()
            return json.loads(row['receipt']) if row else {}

    def delivery_children(self, jid):
        with self.connect() as db:
            return [json.loads(row['data']) for row in db.execute(
                "SELECT data FROM jobs WHERE json_extract(data,'$.operation.delivery_parent')=? ORDER BY created", (jid,))]

    def media_status(self, jid):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT status,count(*) AS n FROM outbox WHERE job_id=? "
                "AND json_extract(payload,'$.kind')='media' GROUP BY status", (jid,))]

    def delivery_info(self, jid):
        with self.connect() as db:
            rows = db.execute('SELECT status,error,count(*) AS n FROM outbox WHERE job_id=? GROUP BY status,error', (jid,)).fetchall()
            return [dict(r) for r in rows]

    def upload_progress(self, jid):
        with self.connect() as db:
            rows = db.execute("SELECT receipt FROM outbox WHERE job_id=? AND status='sending'", (jid,)).fetchall()
            return [p for row in rows if (p := json.loads(row['receipt']).get('upload_progress'))]

    def refresh(self, ev, jid=None, retry=False):
        """A fresh message opens a new reply opportunity for this owner and target."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            scope = scope_of(ev)
            row = db.execute('SELECT data FROM sessions WHERE scope=?', (scope,)).fetchone()
            session = json.loads(row['data']) if row else dict(mode='chat', generation=0)
            session['event'] = ev
            db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?)', (scope, json.dumps(session)))
            where = "scope=? AND status IN ('pending','waiting')"
            args = [scope]
            if jid:
                children = [r['id'] for r in db.execute("SELECT id FROM jobs WHERE json_extract(data,'$.operation.delivery_parent')=?", (jid,))]
                ids = [jid] + children
                where += ' AND job_id IN (' + ','.join('?' for _ in ids) + ')'
                args.extend(ids)
            # Rebinding the same message would reset the idempotency receipt and
            # may duplicate a send whose response was lost. Only a NEW message
            # creates a new reply opportunity.
            where += " AND json_extract(event, '$.msg_id') != ?"
            args.append(ev.get('msg_id', ''))
            # Do not wake permanent media errors on every unrelated message.
            if not retry:
                where += " AND (status='pending' OR error LIKE 'reply_window:%')"
            rows = db.execute('SELECT id,receipt FROM outbox WHERE ' + where, args).fetchall()
            for row in rows:
                previous = json.loads(row['receipt'])
                # Rebind message identity while retaining reusable media and uploaded parts.
                retained = {k:v for k,v in previous.items() if k in ('upload', 'upload_progress', 'file_info', 'file_expiry', 'upload_file_type', 'audio_fallback_reason')}
                db.execute("UPDATE outbox SET event=?,receipt=?,status='pending',attempts=0,next_attempt=0 WHERE id=?",
                           (json.dumps(ev), json.dumps(retained), row['id']))

    def next_seq(self, ev):
        route = json.dumps([ev['scene'], ev.get('group_openid') or ev['user_openid'], ev.get('msg_id')])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT seq FROM sequences WHERE route=?', (route,)).fetchone()
            seq = row['seq'] + 1 if row else 1
            db.execute('INSERT OR REPLACE INTO sequences VALUES (?,?)', (route, seq))
            return seq

    def recover_outbox(self):
        with self.connect() as db:
            db.execute("""UPDATE outbox SET status='waiting',error='主动消息在重启前未确认送达，请手动补发或查看结果'
                WHERE status='sending' AND json_type(receipt,'$.body') IS NOT NULL
                AND json_extract(receipt,'$.body.msg_id') IS NULL""")
            # Keep receipts: a retry after an ambiguous send uses the same msg_id/seq.
            db.execute("UPDATE outbox SET status='pending' WHERE status='sending'")
