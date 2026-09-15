import http.client
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from akari_admin import Admin, APIError, make_server
from akari_admin_schema import validate
from akari_runtime import Runtime, fingerprint
from akari_tasks import TaskStore


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / 'music').mkdir()
        self.config = dict(app_secret='super-secret-value', app_id='12345678',
                           model='test-model', temperature=.85, music_enabled=True,
                           unknown_plugin={'preserved': True})
        (self.base / 'config.json').write_text(json.dumps(self.config))
        (self.base / 'music/config.json').write_text(json.dumps(dict(diffusion_steps=40, cuda_visible_devices='7', style='test')))
        self.app = Admin(self.base)

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, changes, scope='bot', revision=None):
        return self.app.save(dict(scope=scope, changes=changes, revision=revision or fingerprint(self.app.raw(scope))))


class AdminTests(FixtureCase):
    def test_public_config_never_returns_secrets_or_unknown_fields(self):
        result = json.dumps(self.app.public_config())
        for secret in ('super-secret-value', '12345678', 'unknown_plugin', self.app.token):
            self.assertNotIn(secret, result)

    def test_save_preserves_secrets_unknown_fields_and_private_backup(self):
        self.save({'temperature': .7, 'music_enabled': False})
        raw = self.app.raw('bot')
        self.assertEqual(raw['app_secret'], self.config['app_secret'])
        self.assertEqual(raw['unknown_plugin'], self.config['unknown_plugin'])
        self.assertEqual(raw['temperature'], .7)
        self.assertFalse(raw['music_enabled'])
        backups = list(self.app.private.glob('bot-*.json'))
        self.assertEqual(json.loads(backups[0].read_text()), self.config)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.app.token_path.stat().st_mode & 0o777, 0o600)

    def test_stale_revision_rejected_without_overwrite(self):
        revision = fingerprint(self.config)
        self.save({'temperature': .6})
        with self.assertRaises(APIError) as ctx:
            self.save({'temperature': 1.0}, revision=revision)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.app.raw('bot')['temperature'], .6)

    def test_invalid_values_and_unknown_keys_rejected(self):
        for changes in ({'app_secret':'bad'}, {'agent_script':'/tmp/evil'}, {'temperature':True},
                        {'agent_workers':1.2}, {'voice_enabled':'false'}, {'temperature':float('nan')},
                        {'temperature':float('inf')}, {'top_p':0}, {'agent_workers':20},
                        {'group_reply_mode':'bad'}, {'keyword_regex':'['},
                        {'video_secs_min':8}, {'reconnect_min':300,'reconnect_max':20},
                        {'agent_default_timeout':86400,'agent_max_timeout':30}):
            with self.subTest(changes=changes), self.assertRaises(APIError) as ctx:
                self.save(changes)
            self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.app.raw('bot'), self.config)

    def test_music_save_and_gpu_validation(self):
        self.save({'diffusion_steps':50}, 'music')
        self.assertEqual(self.app.raw('music')['diffusion_steps'], 50)
        self.assertEqual(self.app.raw('bot'), self.config)
        with self.assertRaises(APIError):
            self.save({'cuda_visible_devices':'7;echo bad'}, 'music')

    def test_login_rate_limit_and_expiry(self):
        sid, session = self.app.login(self.app.token)
        self.assertEqual(self.app.session('akari_session=' + sid)[0], sid)
        session['expires'] = time.time() - 1
        with self.assertRaises(APIError):
            self.app.session('akari_session=' + sid)
        for _ in range(10):
            with self.assertRaises(APIError):
                self.app.login('wrong')
        with self.assertRaises(APIError) as ctx:
            self.app.login(self.app.token)
        self.assertEqual(ctx.exception.status, 429)

    def test_logs_redact_credentials_and_keep_stack(self):
        (self.base / 'key.txt').write_text('private-key-abcdef')
        c = dict(self.config, llm_key_file=str(self.base / 'key.txt'))
        (self.base / 'config.json').write_text(json.dumps(c))
        (self.app.state / 'bot.log').write_text('[09-15 10:00:00] ready\n[09-15 10:01:00] error super-secret-value\nTraceback (most recent call last):\n  line 42\nValueError: private-key-abcdef\n[09-15 10:02:00] token=abc-secret &access_token=xyz-secret Bearer bearer-secret session_id=session-secret\n')
        logs = self.app.logs(level='error', query='line 42')
        self.assertEqual(len(logs['records']), 1)
        self.assertIn('Traceback', logs['records'][0]['text'])
        text = json.dumps(self.app.logs())
        for secret in ('super-secret-value', 'private-key-abcdef', 'abc-secret', 'xyz-secret', 'bearer-secret', 'session-secret'):
            self.assertNotIn(secret, text)
        with self.assertRaises(APIError):
            self.app.logs('../../config.json')

    def test_missing_logs_empty_and_job_store_readonly(self):
        self.assertEqual(self.app.logs()['records'], [])
        self.assertFalse(self.app.jobs()['available'])
        self.assertFalse((self.app.state / 'tasks.sqlite3').exists())

    def test_jobs_only_return_operation_kind_and_redacted_text(self):
        store=TaskStore(self.app.state)
        job=dict(id='safe-id', status='failed', prompt='super-secret-value', error=None,
                 operation={'kind':'sing','url':'https://example.invalid/?token=private-url-token'})
        with store.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                       ('safe-id','test','test','event','failed',time.time(),json.dumps(job)))
        data=self.app.jobs()
        self.assertEqual(data['items'][0]['operation'],'sing')
        self.assertEqual(data['items'][0]['error'],'')
        self.assertNotIn('private-url-token',json.dumps(data))
        self.assertNotIn('super-secret-value',json.dumps(data))

    def test_restart_blocked_for_queued_and_running_tasks(self):
        with patch.object(self.app, 'jobs') as jobs, patch('akari_admin.subprocess.run') as run:
            for status in ('queued','running'):
                jobs.return_value = {'counts':{status:1}}
                with self.assertRaises(APIError) as ctx:
                    self.app.control('restart')
                self.assertEqual(ctx.exception.status,409)
                with self.assertRaises(APIError):
                    self.app.control('stop')
            run.assert_not_called()
            with self.assertRaises(APIError):
                self.app.control('restart; echo bad')

    def test_service_action_is_fixed_argv(self):
        with patch.object(self.app, 'jobs', return_value={'counts':{}}), patch('akari_admin.subprocess.run') as run:
            run.return_value.returncode=0
            self.app.control('restart')
            self.assertEqual(run.call_args.args[0], ['sudo','-n','systemctl','restart','akari-bot.service'])

    def test_service_guard_blocks_new_queue_writes_until_operation_finishes(self):
        TaskStore(self.app.state)
        def command(*args, **kwargs):
            with sqlite3.connect(self.app.state / 'tasks.sqlite3', timeout=.01) as other:
                with self.assertRaises(sqlite3.OperationalError):
                    other.execute('BEGIN IMMEDIATE')
            from types import SimpleNamespace
            return SimpleNamespace(returncode=0)
        with patch('akari_admin.subprocess.run', side_effect=command):
            self.app.control('restart')
        with sqlite3.connect(self.app.state / 'tasks.sqlite3', timeout=.01) as other:
            other.execute('BEGIN IMMEDIATE')

    def test_runtime_fingerprint_and_stale_or_wrong_pid(self):
        runtime=Runtime(self.app.state, self.config)
        runtime.set(gateway='ready',last_ack=time.time())
        with patch.object(self.app,'service',return_value={'active':'active','sub':'running','pid':os.getpid()}):
            self.assertFalse(self.app.status()['pending_restart'])
            self.save({'temperature':1.1})
            self.assertTrue(self.app.status()['pending_restart'])
            data=json.loads(runtime.path.read_text()); data['updated']=time.time()-30;runtime.path.write_text(json.dumps(data))
            self.assertIsNone(self.app.status()['pending_restart'])
        runtime.set()
        with patch.object(self.app,'service',return_value={'active':'active','sub':'running','pid':123}):
            self.assertFalse(self.app.status()['telemetry'])

    def test_failed_atomic_write_keeps_original(self):
        with patch('akari_admin.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.save({'temperature':.2})
        self.assertEqual(self.app.raw('bot'),self.config)


class HTTPTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.server=make_server(self.base,0)
        self.app=self.server.app
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.port=self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        super().tearDown()

    def request(self,path,body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=4)
        hdr={'Content-Type':'application/json',**(headers or {})}
        conn.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,hdr)
        resp=conn.getresponse();data=resp.read();out=(resp.status,dict(resp.getheaders()),data);conn.close();return out

    def authenticated(self):
        status, headers, raw=self.request('/api/login',{'token':self.app.token})
        self.assertEqual(status,200)
        self.assertIn('HttpOnly',headers['Set-Cookie'])
        return {'Cookie':headers['Set-Cookie'].split(';')[0],'X-CSRF-Token':json.loads(raw)['csrf']}

    def test_api_requires_login_and_safe_static_routes(self):
        self.assertEqual(self.request('/api/config')[0],401)
        status,headers,_=self.request('/')
        self.assertEqual(status,200)
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        auth=self.authenticated()
        for path in ('/../config.json','/config.json','/secrets/key','/favicon.svg/../../config.json'):
            self.assertEqual(self.request(path,headers=auth)[0],404)
        self.assertEqual(self.request('/api/config',headers=auth)[0],200)

    def test_csrf_and_host_and_origin_rejected(self):
        auth=self.authenticated()
        body=dict(scope='bot',revision=fingerprint(self.config),changes={'temperature':.7})
        self.assertEqual(self.request('/api/config',body,{'Cookie':auth['Cookie']})[0],403)
        self.assertEqual(self.request('/api/config',body,{**auth,'Origin':'https://evil.invalid'})[0],403)
        self.assertEqual(self.request('/api/config',headers={**auth,'Host':'evil.invalid'})[0],403)
        self.assertEqual(self.request('/api/config',body,{**auth,'Sec-Fetch-Site':'cross-site'})[0],403)
        self.assertEqual(self.app.raw('bot'),self.config)
        self.assertEqual(self.request('/api/config',body,auth)[0],200)

    def test_logout_invalidates_session(self):
        auth=self.authenticated()
        self.assertEqual(self.request('/api/logout',{},auth)[0],200)
        self.assertEqual(self.request('/api/config',headers=auth)[0],401)

    def test_login_origin_rejected(self):
        self.assertEqual(self.request('/api/login',{'token':self.app.token},{'Origin':'https://evil.invalid'})[0],403)


if __name__=='__main__':
    unittest.main()
