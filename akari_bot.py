#!/usr/bin/env python3
# akari_bot.py - standalone QQ bot powered by local sglang gateway (stdlib only)
import base64, json, os, queue, re, signal, socket, ssl, struct, sys, threading, time, traceback
from akari_tasks import TaskStore, split_full
from akari_commands import Commands, task_text
from akari_worker import TaskService
import akari_delivery
from akari_runtime import Runtime
import urllib.parse, urllib.request

try:
    import akari_web
except Exception as _e:
    akari_web = None
try:
    import akari_stickers
except Exception:
    akari_stickers = None
try:
    import akari_image
except Exception:
    akari_image = None
try:
    import akari_video
except Exception:
    akari_video = None
try:
    import akari_voice
except Exception:
    akari_voice = None

BASE = os.path.abspath(os.environ.get('AKARI_BASE', os.path.expanduser('~/.akari')))
STATE = os.path.join(BASE, 'state')
os.makedirs(STATE, exist_ok=True)
with open(os.path.join(BASE, 'config.json'), encoding='utf-8') as f:
    CFG = json.load(f)
with open(os.path.join(BASE, 'persona.txt'), encoding='utf-8') as f:
    PERSONA = f.read()
_ov = os.environ.get('AKARI_CFG_OVERRIDE')
if _ov:
    try:
        CFG.update(json.loads(_ov))
    except Exception:
        pass

RUNTIME = Runtime(STATE, CFG)

RULES = '''


【当前场景】你在QQ上通过API和真人聊天，对方不知道也不需要知道你的运行方式。
【回复规则】
1. 永远以赤座灯里的身份和口吻回复，绝不提及AI、模型、程序、服务器、接口等词。
2. 回复要短：一般1~2句、70字以内，像QQ随手打字的感觉，不要写小作文。
3. 群聊里话题和你无关、也没人喊你时，可以只输出 NO_REPLY 保持安静。
   但只要有人@你、喊你名字、或直接问你问题，绝对禁止输出 NO_REPLY，必须回应。
4. 需要查时效信息（新番、新闻、天气、比分等）时，先只输出一行：SEARCH: 关键词
   我会把查到的内容贴回来，你再用口语总结。查不到就说不知道，不要编造。
5. 有人发图片时，图会直接给你看到。看清了就自然地说你看到了什么，可以夸一句；只有系统明确告诉你没拿到图时才说看不到，绝对不要编造画面内容。
6. 不用markdown、不用编号列表。动作和心理描写用括号，一条消息最多一处。
7. 群成员昵称是QQ昵称，认不全也不要硬猜。
'''

_log_lock = threading.Lock()

def log(msg):
    line = '[%s] %s' % (time.strftime('%m-%d %H:%M:%S'), msg)
    with _log_lock:
        try:
            with open(os.path.join(STATE, 'bot.log'), 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass
    print(line, flush=True)

def http_json(url, method='GET', headers=None, body=None, timeout=25, verify=True):
    data = None
    h = dict(headers or {})
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        h.setdefault('Content-Type', 'application/json; charset=utf-8')
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        code = r.status
        txt = r.read().decode('utf-8', 'replace')
    try:
        return code, (json.loads(txt) if txt.strip() else {})
    except Exception:
        return code, {'raw': txt[:300]}

_tok = {'t': '', 'exp': 0.0}
_tok_lock = threading.Lock()

def get_token():
    with _tok_lock:
        if _tok['t'] and time.time() < _tok['exp'] - 120:
            return _tok['t']
        st, d = http_json('https://bots.qq.com/app/getAppAccessToken', 'POST',
                          {'Accept': 'application/json'},
                          {'appId': CFG['app_id'], 'clientSecret': CFG['app_secret']}, 20)
        t = d.get('access_token') or ''
        if not t:
            raise RuntimeError('token failed http=%s %s' % (st, json.dumps(d, ensure_ascii=False)[:200]))
        _tok['t'] = t
        _tok['exp'] = time.time() + max(60, int(d.get('expires_in') or 3600))
        log('access_token ok expires_in=%s' % d.get('expires_in'))
        return t

class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.chats = self._load('chats.json', {})
        self.done = self._load('done.json', [])
        self.done_set = set(self.done)

    @staticmethod
    def _load(name, default):
        try:
            with open(os.path.join(STATE, name), encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return default

    def mark(self, key):
        with self.lock:
            if key in self.done_set:
                return False
            self.done.append(key)
            self.done_set.add(key)
            if len(self.done) > 3000:
                for k in self.done[:-3000]:
                    self.done_set.discard(k)
                self.done = self.done[-3000:]
            return True

    def save(self):
        with self.lock:
            for name, obj in (('chats.json', self.chats), ('done.json', self.done)):
                tmp = os.path.join(STATE, name + '.tmp')
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(obj, f, ensure_ascii=False)
                os.replace(tmp, os.path.join(STATE, name))

class WS:
    def __init__(self, url):
        u = urllib.parse.urlsplit(url)
        self.host = u.hostname
        self.port = u.port or 443
        self.path = (u.path or '/') + (('?' + u.query) if u.query else '')
        self.sock = None
        self.buf = b''

    def connect(self):
        raw = socket.create_connection((self.host, self.port), timeout=15)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        req = ('GET ' + self.path + ' HTTP/1.1\r\nHost: ' + self.host +
               '\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
               'Sec-WebSocket-Key: ' + key + '\r\nSec-WebSocket-Version: 13\r\n\r\n')
        self.sock.sendall(req.encode('ascii'))
        head = b''
        while b'\r\n\r\n' not in head:
            c = self.sock.recv(4096)
            if not c:
                raise ConnectionError('handshake closed')
            head += c
        first = head.split(b'\r\n', 1)[0].decode('latin-1')
        if ' 101' not in first:
            raise ConnectionError('handshake failed: ' + first)
        self.buf = head.split(b'\r\n\r\n', 1)[1]

    def _recv(self, n):
        while len(self.buf) < n:
            c = self.sock.recv(65536)
            if not c:
                raise ConnectionError('socket closed')
            self.buf += c
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, opcode, payload):
        n = len(payload)
        if n < 126:
            hdr = struct.pack('!BB', 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack('!BBH', 0x80 | opcode, 0x80 | 126, n)
        else:
            hdr = struct.pack('!BBQ', 0x80 | opcode, 0x80 | 127, n)
        mask = os.urandom(4)
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(n))
        self.sock.sendall(hdr + mask + masked)

    def send_text(self, s):
        self._frame(0x1, s.encode('utf-8'))

    def recv_frame(self):
        msgs = []
        while True:
            b0, b1 = self._recv(2)
            fin = b0 & 0x80
            op = b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack('!H', self._recv(2))[0]
            elif ln == 127:
                ln = struct.unpack('!Q', self._recv(8))[0]
            payload = self._recv(ln) if ln else b''
            if b1 & 0x80:
                mask = self._recv(4)
                payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
            if op == 0x9:
                self._frame(0xA, payload)
                continue
            if op == 0x8:
                code = 1005
                rsn = ''
                if len(payload) >= 2:
                    code = struct.unpack('!H', payload[:2])[0]
                    rsn = payload[2:].decode('utf-8', 'replace')
                self.close()
                raise ConnectionError('close code=%s reason=%s' % (code, rsn[:150]))
            if op in (0x1, 0x2):
                msgs = [payload]
            elif op == 0x0:
                if not msgs:
                    continue
                msgs.append(payload)
            else:
                continue
            if fin:
                data = b''.join(msgs)
                msgs = []
                return data

    def close(self, code=1000):
        try:
            self._frame(0x8, struct.pack('!H', code))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


EVENT_TYPES = {'C2C_MESSAGE_CREATE', 'GROUP_AT_MESSAGE_CREATE', 'GROUP_MESSAGE_CREATE'}
STORE = Store()
_llm_sem = threading.Semaphore(2)

def build_event(payload):
    d = payload.get('d') or {}
    author = d.get('author') or {}
    member = d.get('member') or {}
    etype = payload.get('t') or ''
    return {
        'key': (etype + ':' + str(d['id'])) if d.get('id') else (payload.get('id') or (etype + '-' + str(time.time()))),
        'type': etype,
        'scene': 'group' if etype.startswith('GROUP') else 'c2c',
        'content': (d.get('content') or '').strip(),
        'msg_id': d.get('id') or '',
        'group_openid': d.get('group_openid') or '',
        'user_openid': author.get('user_openid') or author.get('member_openid') or author.get('id') or '',
        'nick': member.get('nick') or author.get('username') or author.get('nick') or '',
        'ts': time.time(),
        'ev_mentions': d.get('mentions') or [],
        'attachments': d.get('attachments') or [],
    }

STOP = threading.Event()
CURRENT_WS = {'ws': None}


class _SessionRefresh(Exception):
    '''主动会话刷新：网关大约30分钟会4009强杀会话，到点自己主动重连，避免30s退避盲窗。'''
    pass


def _on_signal(signum, _frame):
    if STOP.is_set():
        return
    STOP.set()
    TASK_SERVICE.stop.set()
    log('signal %s received, closing connection gracefully' % signum)
    _ws = CURRENT_WS.get('ws')
    if _ws is not None:
        try:
            _ws.close()
        except Exception:
            pass


def install_signals():
    for _s in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_s, _on_signal)
        except Exception:
            pass


def run_ws(url, q):
    ws = WS(url)
    ws.connect()
    CURRENT_WS['ws'] = ws
    log('ws connected')
    state = {'seq': None, 'hb': 41.0, 'next_hb': 0.0, 'identified': False, 'ready': False, 'deadline': 0.0, 'hb_count': 0, 'ready_ts': 0.0}
    ws.sock.settimeout(4.0)
    while True:
        now = time.time()
        if STOP.is_set():
            ws.close()
            raise ConnectionError('stopping by signal')
        if state['identified'] and not state['ready'] and state['deadline'] and now > state['deadline']:
            ws.close()
            raise ConnectionError('no READY within timeout, dropping connection')
        _la = state.get('last_ack')
        if state['ready'] and _la and (now - _la) > state['hb'] * float(CFG.get('stale_hb_limit', 4)):
            ws.close()
            raise ConnectionError('no HEARTBEAT_ACK for %.0fs (dead session), recycling' % (now - _la))
        if state['ready'] and state['ready_ts'] and \
                now - state['ready_ts'] > float(CFG.get('session_refresh_secs', 1500)):
            ws.close()
            raise _SessionRefresh('proactive refresh after %.0fs (pre-empt gateway 30min drop)' % (now - state['ready_ts']))
        if state['identified'] and now >= state['next_hb']:
            ws.send_text(json.dumps({'op': 1, 'd': state['seq'] if state['seq'] is not None else 0}))
            state['next_hb'] = now + state['hb']
            state['hb_sent'] = state.get('hb_sent', 0) + 1
            if state['hb_sent'] <= 3 or state['hb_sent'] % 12 == 0:
                log('heartbeat sent #%d seq=%s ready=%s' % (state['hb_sent'], state['seq'], state['ready']))
            if state['ready']:
                state['hb_count'] += 1
            continue
        try:
            frame = ws.recv_frame()
        except socket.timeout:
            continue
        if frame is None:
            continue
        try:
            p = json.loads(frame.decode('utf-8'))
        except Exception:
            continue
        if CFG.get('wire_dump'):
            log('wire op=%s t=%s s=%s len=%d' % (p.get('op'), p.get('t'), p.get('s'), len(frame)))
        op = p.get('op')
        d = p.get('d')
        if op == 10:
            state['hb'] = max(5.0, float((d or {}).get('heartbeat_interval') or 41250) / 1000.0)
            state['next_hb'] = time.time() + state['hb']
            tok = get_token()
            if CFG.get('identify_token_format', 'qqbot') == 'dot':
                tok = str(CFG['app_id']) + '.' + tok
            else:
                tok = 'QQBot ' + tok
            ws.send_text(json.dumps({'op': 2, 'd': {'token': tok,
                                                   'intents': int(CFG['intents']), 'shard': [0, 1]}}))
            state['identified'] = True
            state['deadline'] = time.time() + float(CFG.get('ready_timeout', 75))
            log('identify sent intents=%s' % CFG['intents'])
        elif op == 0:
            if p.get('s') is not None:
                state['seq'] = p['s']
            t = p.get('t')
            if t == 'READY':
                state['ready'] = True
                state['ready_ts'] = time.time()
                state['deadline'] = 0.0
                state['last_ack'] = time.time()
                RUNTIME.set(gateway='ready', last_ack=time.time())
                log('READY session_id=' + str((d or {}).get('session_id')))
            elif t == 'RESUMED':
                RUNTIME.set(gateway='ready', last_ack=time.time())
                log('RESUMED')
            elif t in EVENT_TYPES:
                ev = build_event(p)
                log('event %s nick=%s text=%s' % (t, ev['nick'][:16], ev['content'][:60].replace('\n', ' ')))
                if ev.get('attachments') or '<image' in (ev['content'] or '').lower():
                    log('att_raw %s' % json.dumps(p.get('d', {}).get('attachments'), ensure_ascii=False)[:400])  # patch12 fix name
                state['hb_count'] = 0
                q.put(ev)
            elif t:
                log('dispatch ' + str(t))
        elif op == 11:
            RUNTIME.set(last_ack=time.time())
            state['last_ack'] = time.time()
            state['ack_count'] = state.get('ack_count', 0) + 1
            if state['ack_count'] <= 3 or state['ack_count'] % 20 == 0:
                log('HEARTBEAT_ACK #%d' % state['ack_count'])
        elif op == 9:
            ws.close()
            raise ConnectionError('invalid session d=%s' % d)

def gateway_loop(q):
    if CFG.get('probe_once'):
        try:
            st, d = http_json(CFG['api_base'] + '/gateway',
                              headers={'Accept': 'application/json',
                                       'Authorization': 'QQBot ' + get_token(),
                                       'X-Union-Appid': str(CFG['app_id'])}, timeout=20)
            url = (d or {}).get('url') or ''
            if not url:
                log('PROBE FAIL no-gateway-url http=%s %s' % (st, json.dumps(d, ensure_ascii=False)[:160]))
                sys.exit(3)
            run_ws(url, q)
            log('PROBE OK READY')
            sys.exit(0)
        except SystemExit:
            raise
        except Exception as e:
            log('PROBE FAIL %r' % (e,))
            sys.exit(3)
    min_b = float(CFG.get('reconnect_min', 30.0))
    max_b = float(CFG.get('reconnect_max', 300.0))
    backoff = min_b
    while True:
        if STOP.is_set():
            log('gateway loop exiting (stop requested)')
            return
        t0 = time.time()
        try:
            st, d = http_json(CFG['api_base'] + '/gateway',
                              headers={'Accept': 'application/json',
                                       'Authorization': 'QQBot ' + get_token(),
                                       'X-Union-Appid': str(CFG['app_id'])}, timeout=20)
            url = (d or {}).get('url') or ''
            if not url:
                raise RuntimeError('gateway url failed http=%s %s' % (st, json.dumps(d, ensure_ascii=False)[:200]))
            log('gateway url=' + url)
            run_ws(url, q)
            backoff = 2.0
        except _SessionRefresh as e:
            RUNTIME.set(gateway='connecting')
            log('session refresh: %s' % e)
            if STOP.wait(2.0):
                log('gateway loop exiting (stop requested)')
                return
            backoff = 2.0
        except Exception as e:
            RUNTIME.set(gateway='disconnected')
            if time.time() - t0 > 120:
                backoff = min_b
            log('gateway error: %r -> retry in %.0fs' % (e, backoff))
            if STOP.wait(backoff):
                log('gateway loop exiting (stop requested)')
                return
            backoff = min(backoff * 2.0, max_b)

def system_prompt():
    return PERSONA + RULES

def call_llm(messages):
    try:
        with open(CFG['llm_key_file'], encoding='utf-8') as f:
            key = f.read().strip()
    except Exception as e:
        log('llm key read failed: %r' % (e,))
        return ''
    body = {'model': CFG['model'], 'messages': messages,
            'temperature': float(CFG.get('temperature', 0.85)),
            'top_p': float(CFG.get('top_p', 0.95)),
            'max_tokens': int(CFG.get('max_tokens', 420))}
    url = CFG['llm_base'].rstrip('/') + '/v1/chat/completions'
    for attempt in (1, 2):
        try:
            st, d = http_json(url, 'POST', {'Authorization': 'Bearer ' + key}, body,
                              timeout=int(CFG.get('llm_timeout', 90)), verify=False)
            if st == 200 and d.get('choices'):
                content = ((d['choices'][0].get('message') or {}).get('content') or '').strip()
                content = re.sub(r'<think.*?>.*?</think.*?>', '', content, flags=re.S).strip()
                if content:
                    return content
            log('llm attempt=%d http=%s resp=%s' % (attempt, st, json.dumps(d, ensure_ascii=False)[:200]))
        except Exception as e:
            log('llm attempt=%d error=%r' % (attempt, e))
        time.sleep(2)
    return ''

def fetch_image_b64(url):
    cap = int(CFG.get('vision_max_bytes', 4000000))
    raw = b''
    ct = ''
    for attempt in (1, 2):
        try:
            hdr = {'User-Agent': 'Mozilla/5.0'}
            if attempt == 2:
                hdr['Authorization'] = 'QQBot ' + get_token()
            req = urllib.request.Request(url, headers=hdr)
            resp = urllib.request.urlopen(req, timeout=int(CFG.get('vision_dl_timeout', 20)))
            raw = resp.read(cap + 1)
            ct = (resp.headers.get('Content-Type') or '').lower()
            break
        except Exception as e:
            log('image download try=%d failed %r' % (attempt, e))
            raw = b''
    if not raw:
        return ''
    if len(raw) > cap:
        log('image too big, skip')
        return ''
    mime = 'image/jpeg'
    if ct.startswith('image/'):
        mime = ct.split(';')[0].strip()
    elif raw[:8] == b'\x89PNG\r\n\x1a\n':
        mime = 'image/png'
    elif raw[:3] == b'GIF':
        mime = 'image/gif'
    log('image ok bytes=%d mime=%s' % (len(raw), mime))
    return 'data:' + mime + ';base64,' + base64.b64encode(raw).decode()


STICKER_DIR = os.path.join(BASE, 'stickers')
os.makedirs(STICKER_DIR, exist_ok=True)
if akari_stickers is not None:
    try:
        LIB = akari_stickers.Library(STICKER_DIR, os.path.join(STATE, 'sticker_meta.json'),
                                    os.path.join(STATE, 'sticker_cache.json'), log)
        log('stickers loaded: %d' % len(LIB.items))
    except Exception:
        log('sticker lib init error\n' + traceback.format_exc())
        LIB = None
else:
    LIB = None


def clean_reply(t):
    t = re.sub(r'```.*?```', ' ', t, flags=re.S)
    t = re.sub(r'\n{3,}', '\n\n', t).strip()
    return t[:int(CFG.get('reply_max_chars', 1200))]

def split_text(text):
    return split_full(text, int(CFG.get('split_len', 900)))


def send_reply(ev, reply):
    for part in split_text(reply):
        TASK_STORE.enqueue(ev, {'kind': 'text', 'text': part})


def clean_text(raw):
    t = raw or ''
    t = re.sub(r'<@[^>]*>', ' ', t)
    t = re.sub(r'<faceType=[^>]*>', '[表情]', t)
    t = re.sub(r'<img[^>]*>', '[图片]', t)
    t = re.sub(r'@[0-9A-Za-z\u4e00-\u9fa5]+', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    if not t:
        t = '[对方只发了表情或图片，没有配文字]'
    return t


def handle_event(ev):
    if not STORE.mark(ev['key']):
        return
    mode = CFG.get('group_reply_mode', 'all')
    mentioned = (ev['type'] == 'GROUP_AT_MESSAGE_CREATE'
                 or bool(ev.get('ev_mentions'))
                 or (CFG.get('bot_name', '赤座灯里') in (ev['content'] or '')))
    if ev['scene'] == 'group':
        if mode == 'at_only' and not mentioned:
            STORE.save()
            return
        if mode == 'at_or_keyword' and not (mentioned or re.search(CFG.get('keyword_regex', ''), ev['content'], re.IGNORECASE)):
            STORE.save()
            return
    text = clean_text(ev['content'])
    if (ev.get('attachments') or []) and '[图片]' not in text:
        text = (text + ' [这条消息带了一张图片]').strip()
    if CFG.get('agent_enabled', True) and text:
        low = text.lstrip()
        if akari_voice is not None and CFG.get('voice_enabled', True):
            _pfx = next((p for p in ('/说', '/语音', '/voice') if low.startswith(p)), None)
            if _pfx:
                _vspec = low[len(_pfx):].strip()
                if not _vspec:
                    _tip = '要我说什么呀？这样写：/说 要说的话'
                    if CFG.get('dry_run', True):
                        log('DRYRUN would reply -> ' + _tip)
                    else:
                        send_reply(ev, _tip)
                    STORE.save()
                    return
                log('voice cmd queued: ' + _vspec[:100])
                if not CFG.get('dry_run', True):
                    threading.Thread(target=akari_voice.run_voice_task,
                                     args=(CFG, ev, _vspec, log, get_token), daemon=True).start()
                STORE.save()
                return
        if akari_stickers is not None:
            try:
                _done, _msg = akari_stickers.command(LIB, ev, CFG, get_token(), log, low)
            except Exception:
                log('sticker command error\n' + traceback.format_exc())
                _done, _msg = False, ''
            if _done:
                if _msg:
                    if CFG.get('dry_run', True):
                        log('DRYRUN sticker cmd -> ' + _msg[:200])
                    else:
                        send_reply(ev, _msg)
                STORE.save()
                return
        if low.startswith(('/动', '/gif', '/动图')) and akari_video is not None:
            _sp = low.find(' ')
            vspec = low[_sp + 1:].strip() if _sp > 0 else ''
            if not vspec:
                tip = ('要让它动什么呀？这样写：'
                       '/动 @附件 | 她瞫眼并拥抱 | 3秒')
                if CFG.get('dry_run', True):
                    log('DRYRUN video tip -> ' + tip)
                else:
                    send_reply(ev, tip)
                STORE.save()
                return
            if CFG.get('dry_run', True):
                log('DRYRUN video cmd -> ' + vspec[:120])
            else:
                send_reply(ev, '收到！我把它动起来，要多等一小会儿说~')
                threading.Thread(target=run_video_task, args=(ev, vspec), daemon=True).start()
            STORE.save()
            return
        if low.startswith('/画') and akari_image is not None:
            sp = low.find(' ')
            spec = low[sp + 1:].strip() if sp > 0 else ''
            if not spec:
                tip = '画什么呀？比如这样：/画 樱花下的少女 | 3:4'
                if CFG.get('dry_run', True):
                    log('DRYRUN image tip -> ' + tip)
                else:
                    send_reply(ev, tip)
                STORE.save()
                return
            if CFG.get('dry_run', True):
                log('DRYRUN image cmd -> ' + spec[:120])
            else:
                send_reply(ev, '收到！这张我这就去画，要等一小会儿哦~')
                threading.Thread(target=run_image_task, args=(ev, spec), daemon=True).start()
            STORE.save()
            return
    chat_key = ('g:' + ev['group_openid']) if ev['scene'] == 'group' else ('c:' + ev['user_openid'])
    with STORE.lock:
        hist = STORE.chats.setdefault(chat_key, [])
    hist.append({'role': 'user', 'name': ev['nick'] or '某人', 'text': text, 'ts': ev['ts']})
    del hist[:-40]
    sysmsg = system_prompt()
    if ev['scene'] == 'group':
        nicks = '、'.join(sorted({m.get('name', '') for m in hist[-8:] if m.get('role') == 'user' and m.get('name')}))
        if nicks:
            sysmsg += '\n（这是多人QQ�������，最近发言的群友昵称：' + nicks + '）'
    if akari_stickers is not None and LIB is not None and LIB.items and CFG.get('sticker_enabled', True):
        sysmsg += akari_stickers.hint(LIB, int(CFG.get('sticker_hint_limit', 200)))
    if akari_image is not None and CFG.get('image_enabled', True):
        sysmsg += akari_image.hint()
    if akari_video is not None and CFG.get('video_enabled', True):
        sysmsg += akari_video.hint()
    _voice_forced = False
    if akari_voice is not None and CFG.get('voice_enabled', True):
        _voice_forced = akari_voice.request_voice(text)
        if _voice_forced:
            log('voice requested by user keywords')
        sysmsg += akari_voice.hint(forced=_voice_forced)
    msgs = [{'role': 'system', 'content': sysmsg}]
    for m in hist[-int(CFG.get('history_max', 24)):]:
        if m['role'] == 'assistant':
            msgs.append({'role': 'assistant', 'content': m['text']})
        else:
            label = (m['name'] + ': ') if ev['scene'] == 'group' else ''
            msgs.append({'role': 'user', 'content': label + m['text']})
    if CFG.get('vision_enabled', True) and (ev.get('attachments') or []):
        _imgs = []
        for _a in ev['attachments'][:int(CFG.get('vision_max_images', 2))]:
            _u = (_a or {}).get('url') or ''
            if not _u.startswith('http'):
                log('attachment no url: %s' % json.dumps(_a, ensure_ascii=False)[:160])
                continue
            _d = fetch_image_b64(_u)
            if _d:
                _imgs.append(_d)
        if _imgs and msgs:
            _last = msgs[-1].get('content')
            msgs[-1] = {'role': 'user', 'content':
                        [{'type': 'text', 'text': _last if isinstance(_last, str) else ''}] +
                        [{'type': 'image_url', 'image_url': {'url': _x}} for _x in _imgs]}
            log('vision: %d image(s) fed to model' % len(_imgs))
        else:
            msgs.append({'role': 'user', 'content':
                         '[系统] 对方发了图片但我没能拿到图，请如实说看不到，不要编。'})
    with _llm_sem:
        reply = call_llm(msgs)
    reply = (reply or '').strip()
    _m = re.match(r'(?i)^SEARCH\s*[:：]\s*(.+)$', reply.split('\n')[0].strip())
    if _m and akari_web and CFG.get('search_enabled', True):
        _q = _m.group(1).strip()[:120]
        log('search requested: ' + _q)
        try:
            _items = akari_web.search(_q, int(CFG.get('search_results', 5)), log)
        except Exception as _e:
            log('search error %r' % (_e,))
            _items = []
        log('search results: %d' % len(_items))
        _msgs2 = msgs + [{'role': 'assistant', 'content': 'SEARCH: ' + _q},
                        {'role': 'user', 'content': akari_web.format_results(_q, _items)}]
        with _llm_sem:
            _r2 = call_llm(_msgs2)
        if _r2:
            reply = _r2.strip()
    if not reply:
        log('no reply (llm failed) key=' + ev['key'][:24])
        STORE.save()
        return
    reply = clean_reply(reply)
    _stq = []
    if akari_stickers is not None:
        reply, _stq = akari_stickers.extract(reply)
    # IMAGEGUARD: 声称要画却没给 IMAGE: 时，自动重问一次
    if (reply and akari_image is not None and CFG.get('image_enabled', True)
            and 'IMAGE:' not in reply
            and 'VIDEO:' not in reply and 'GIF:' not in reply):
        _say = ('我去画', '我画一', '画一张', '我来画',
                '这就画', '马上画', '去生图', '画个', '画张')
        _no = ('画不了', '不会画', '画不出', '没法画',
               '不能画', '暂时不')
        if any(x in reply for x in _say) and not any(x in reply for x in _no):
            log('IMAGEGUARD claim without tag, retry once')
            _m2 = msgs + [{'role': 'assistant', 'content': reply},
                           {'role': 'user', 'content':
                            '（系统）你刚答应了但没给生图指令。'
                            '请立刻补一行 IMAGE: 提示词 | 比例。'}]
            with _llm_sem:
                _r2 = call_llm(_m2)
            if _r2 and 'IMAGE:' in _r2:
                log('IMAGEGUARD fixed')
                reply = _r2.strip()
    _imq = []
    if akari_image is not None:
        reply, _imq = akari_image.extract(reply)
    _vdq = []
    if akari_video is not None:
        reply, _vdq = akari_video.extract(reply)
    _voq = None
    if akari_voice is not None:
        reply, _voq = akari_voice.extract(reply)
    if len(reply) <= 16 and 'NO_REPLY' in reply.upper():
        if mentioned:
            log('NO_REPLY suppressed (mentioned)')
            reply = '诶？我在的啦！刚那条没看明白，再说一次嘛。(鼓腮帮子)'
        else:
            log('silent (NO_REPLY)')
            STORE.save()
            return
    if CFG.get('dry_run', True):
        log('DRYRUN would reply -> ' + (reply or '(只发图)').replace('\n', ' ')[:220])
        for _q in _stq:
            _hit = LIB.pick_strict(_q) if LIB is not None else None
            log('DRYRUN would send sticker -> ' + (_hit['key'] if _hit else 'none'))
        for _s in _imq:
            log('DRYRUN would generate image -> ' + _s[:120])
        for _s in _vdq:
            log('DRYRUN would render gif -> ' + _s[:120])
        hist.append({'role': 'assistant', 'name': CFG.get('bot_name', '赤座灯里'), 'text': reply, 'ts': time.time()})
        STORE.save()
        return
    if reply:
        send_reply(ev, reply)
    if _imq and akari_image is not None and CFG.get('image_enabled', True):
        threading.Thread(target=run_image_task, args=(ev, _imq[0]), daemon=True).start()
    if _vdq and akari_video is not None and CFG.get('video_enabled', True):
        threading.Thread(target=run_video_task, args=(ev, _vdq[0]), daemon=True).start()
    if akari_voice is not None and CFG.get('voice_enabled', True):
        if _voq:
            log('voice queued: ' + repr(_voq)[:160])
            threading.Thread(target=akari_voice.run_voice_task,
                             args=(CFG, ev, _voq, log, get_token), daemon=True).start()
        elif _voice_forced:
            _fb = akari_voice.fallback_voice_text(reply, int(CFG.get('voice_max_chars', 160)))
            if _fb:
                log('voice fallback queued (model omitted VOICE): ' + _fb[:100])
                threading.Thread(target=akari_voice.run_voice_task,
                                 args=(CFG, ev, _fb, log, get_token), daemon=True).start()
            else:
                log('voice fallback skipped (empty reply content)')
    for _q in _stq[:int(CFG.get('sticker_max_per_reply', 1))]:
        if akari_stickers is not None and LIB is not None and CFG.get('sticker_enabled', True):
            try:
                akari_stickers.send_sticker(LIB, ev, CFG, get_token(), log, _q,
                                           strict=True,
                                           cooldown_secs=int(CFG.get('sticker_cooldown_secs', 90)))
            except Exception:
                log('sticker send error\n' + traceback.format_exc())
    hist.append({'role': 'assistant', 'name': CFG.get('bot_name', '赤座灯��'), 'text': reply, 'ts': time.time()})
    STORE.save()

def send_result(ev, text):
    send_reply(ev, text)


def run_image_task(ev, spec):
    try:
        ok, info = akari_image.request(CFG, log, ev, spec, get_token)
        if ok or info == 'disabled':
            return
        if info == 'wait timeout':
            tip = '（这张图等排队等太久了，先不画了，你晚点再叫我就好）'
        else:
            tip = "'唔……这张没画出来：'" + str(info)[:80]
        if not CFG.get('dry_run', True):
            send_reply(ev, tip)
    except Exception:
        log('image task error ' + traceback.format_exc()[:300])


def run_video_task(ev, spec):
    try:
        ok, info = akari_video.request(CFG, log, ev, spec, get_token)
        if ok or info == 'disabled':
            return
        if info == 'wait timeout':
            tip = '（动图排队排太久了，这次先不做啦，你晚点再叫我就好）'
        else:
            tip = "'唓……这张动图没做出来：'" + str(info)[:80]
        if not CFG.get('dry_run', True):
            send_reply(ev, tip)
    except Exception:
        log('video task error ' + traceback.format_exc()[:300])


def event_allowed(ev):
    if ev['scene'] != 'group':
        return True
    mode = CFG.get('group_reply_mode', 'all')
    mentioned = (ev['type'] == 'GROUP_AT_MESSAGE_CREATE' or ev.get('ev_mentions')
                 or CFG.get('bot_name', '赤座灯里') in ev.get('content', ''))
    if mode == 'at_only':
        return bool(mentioned)
    if mode == 'at_or_keyword':
        return bool(mentioned or re.search(CFG.get('keyword_regex', ''), ev.get('content', ''), re.I))
    return True


def dispatch_event(ev, chat_queue):
    if not event_allowed(ev):
        return
    with STORE.lock:
        if ev['key'] in STORE.done_set:
            return
    if COMMANDS.recognizes(ev):
        COMMANDS.handle(ev)
        STORE.mark(ev['key'])
        STORE.save()
        return
    if COMMANDS.allowed(ev):
        TASK_STORE.refresh(ev)
    raw = task_text(ev.get('content', ''))
    if raw == '/聊' or raw.startswith('/聊 '):
        ev = dict(ev, content=raw[2:].lstrip())
    chat_queue.put(ev)


def dispatcher_loop(incoming, chat_queue):
    while not STOP.is_set():
        try:
            ev = incoming.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            dispatch_event(ev, chat_queue)
        except Exception:
            log('dispatch error:\n' + traceback.format_exc())
            send_reply(ev, '指令处理出现异常，尚未确认完成。请用 /任务列表 查看后再重试。')
        finally:
            incoming.task_done()


def responder_loop(q):
    while True:
        ev = q.get()
        try:
            handle_event(ev)
        except Exception:
            log('handle_event error:\n' + traceback.format_exc())

def invalidate_qq_token():
    with _tok_lock:
        _tok.update(t='', exp=0.0)


TASK_STORE = TaskStore(STATE)
COMMANDS = Commands(TASK_STORE, CFG, log)
TASK_SERVICE = TaskService(TASK_STORE, CFG, log)
QQ_TRANSPORT = akari_delivery.QQTransport(CFG, TASK_STORE, http_json, get_token, log, invalidate_qq_token)
akari_delivery.CURRENT = QQ_TRANSPORT
OUTBOX = akari_delivery.Outbox(TASK_STORE, QQ_TRANSPORT, log)


def main():
    install_signals()
    threading.Thread(target=RUNTIME.run, args=(STOP,), name='runtime-status', daemon=True).start()
    incoming, chat_queue = queue.Queue(), queue.Queue()
    log('boot: model=%s llm=%s dry_run=%s group_mode=%s workbench=2' % (CFG['model'], CFG['llm_base'], CFG.get('dry_run'), CFG.get('group_reply_mode')))
    TASK_SERVICE.start()
    threading.Thread(target=OUTBOX.run, args=(STOP, 'text'), name='qq-text-delivery', daemon=True).start()
    threading.Thread(target=OUTBOX.run, args=(STOP, 'media'), name='qq-media-delivery', daemon=True).start()
    threading.Thread(target=responder_loop, args=(chat_queue,), daemon=True).start()
    threading.Thread(target=dispatcher_loop, args=(incoming, chat_queue), daemon=True).start()
    try:
        gateway_loop(incoming)
    finally:
        STOP.set()
        TASK_SERVICE.close()
        STORE.save()
        RUNTIME.set(gateway='stopped')


if __name__ == '__main__':
    main()
