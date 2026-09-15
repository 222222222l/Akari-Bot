# akari_voice.py - 让阿卡林在QQ群里开口说话的语音模块（stdlib only）
# 依赖本地 voice_server.py (CosyVoice2) 提供 /tts 接口，输出 wav 24kHz
import base64, json, os, re, threading, time, urllib.error, urllib.request
import akari_delivery

_last_voice = {}  # chat_key -> ts
_lock = threading.Lock()


def _cfg(CFG):
    return {
        'enabled': bool(CFG.get('voice_enabled', True)),
        'url': CFG.get('voice_url', 'http://127.0.0.1:8902'),
        'cooldown': int(CFG.get('voice_cooldown_secs', 240)),
        'max_chars': int(CFG.get('voice_max_chars', 160)),
        'timeout': int(CFG.get('voice_timeout', 180)),
    }


def hint(forced=False):
    h = '''
【语音能力】你现在可以在QQ里发语音条了！
规则：
- 当你特别想说、或对方明确想听你声音时，在回复的最后单独加一行：VOICE: [情感: 情感描述] 要说的话
- 情感描述：用一句简短中文概括这句话的情绪，如「平静自然的闲聊」「开心，语气轻快」「有点失落」「惊讶」。拿不准就写「平静自然」。它只用于配音，不会显示给用户。
- 语音内容要口语化、自然，像QQ发语音一样；去掉所有括号动作描写和多余标点；一般不超过60字。
- 每条回复最多一个 VOICE:。普通回复不要加，别每条都发语音。
'''
    if forced:
        h += '【重要】对方明确要求你用语音回答！本次回复必须在最后加上 VOICE: 行（带 [情感: ...]），把回答内容用日语口语说出来，绝不能省略！\n'
    return h


def parse_voice_line(line):
    """'[情感: 平静] 你好' -> ('你好', '平静'); 无标签 -> ('你好', None)"""
    line = (line or '').strip()
    em = re.match(r'^\[\s*情感[:：]\s*(.+?)\s*\]\s*(.+)$', line) or \
         re.match(r'^情感[:：]\s*(.+?)[，,、]\s*(.+)$', line)
    if em:
        return em.group(2).strip(), em.group(1).strip()
    return line, None


def extract(reply):
    """return (text_without_voice, (voice_text, emo_text) or None)"""
    m = re.search(r'(?m)^\s*VOICE[:：]\s*(.+?)\s*$', reply or '')
    if not m:
        return (reply or ''), None
    line, emo = parse_voice_line(m.group(1))
    text = (reply[:m.start()] + reply[m.end():]).strip()
    if not line:
        return text, None
    return text, (line, emo)


# 用户明确要求语音的关键词（命中则强制走语音流程）
VOICE_REQ_RE = re.compile(r'用语音|用声音|语音告诉|语音说|发(?:个|条)?语音|(?:说|读|念)给我听|(?:读|念)出来|开口说')


def request_voice(text):
    """message text 是否明确要求用语音回答"""
    return bool(VOICE_REQ_RE.search(text or ''))


def fallback_voice_text(reply, max_chars=160):
    """模型漏写 VOICE 行时，把回复内容清洗成适合读出来的文本"""
    s = reply or ''
    s = re.sub(r'@[^\s@]+', ' ', s)                 # 去掉 @昵称
    s = re.sub(r'VOICE[:：][^\n]*', '', s)          # 去掉残留 VOICE 行
    s = re.sub(r'[（(][^（）()]{0,15}[）)]', '', s)   # 去掉括号动作描写
    s = re.sub(r'\s+', ' ', s).strip(' ，。,.!?！?、~～')
    return s[:max_chars]


def command(text):
    """/说 <content> -> voice content or None if no content"""
    low = (text or '').strip()
    for p in ('/说', '/语音', '/voice'):
        if low.startswith(p):
            return low[len(p):].strip() or None
    return None


def synth(CFG, text, log, emo_text=None):
    """text -> wav bytes via local voice server (optional emo_text)"""
    c = _cfg(CFG)
    body = json.dumps({'text': text, 'emo_text': emo_text}, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(c['url'] + '/tts', data=body, method='POST',
                                 headers={'Content-Type': 'application/json'})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=c['timeout']) as r:
        wav = r.read()
    log('voice synth ok chars=%d bytes=%d rtf=%.2f' % (len(text), len(wav),
                                                       (time.time() - t0) / max(1, len(text))))
    return wav


def _upload(CFG, tok, ev, data, log):
    """upload wav as voice (file_type=3), return file_info or None"""
    scene_id = ev['group_openid'] if ev['scene'] == 'group' else ev['user_openid']
    url = CFG['api_base'] + '/v2/' + ('groups/' if ev['scene'] == 'group' else 'users/') + scene_id + '/files'
    body = {'file_type': 3,
            'file_data': base64.b64encode(data).decode(),
            'srv_send_msg': False}
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                     headers={'Authorization': 'QQBot ' + tok,
                                              'X-Union-Appid': str(CFG['app_id']),
                                              'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        log('voice upload HTTP %s %s' % (e.code, e.read().decode('utf-8', 'replace')[:250]))
        return None
    except Exception as e:
        log('voice upload error %r' % (e,))
        return None
    fi = (d.get('file_info') or '').strip()
    if fi and fi[0] == '{':
        try:
            fi = json.dumps(json.loads(fi))
        except Exception:
            pass
    log('voice upload ok=%s raw=%s' % (bool(fi), json.dumps(d, ensure_ascii=False)[:160]))
    return fi or None


def _send_media_once(CFG, tok, ev, file_info, log, with_msg_id):
    scene_id = ev['group_openid'] if ev['scene'] == 'group' else ev['user_openid']
    url = CFG['api_base'] + '/v2/' + ('groups/' if ev['scene'] == 'group' else 'users/') + scene_id + '/messages'
    body = {'msg_type': 7, 'media': {'file_info': file_info}}
    if with_msg_id and ev.get('msg_id'):
        body.update(akari_delivery.reply_fields(ev))
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                     headers={'Authorization': 'QQBot ' + tok,
                                              'X-Union-Appid': str(CFG['app_id']),
                                              'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode() or '{}')
        ok = bool(d.get('id'))
        log('voice msg send (msg_id=%s) ok=%s id=%s raw=%s' % (with_msg_id, ok, d.get('id', ''), json.dumps(d, ensure_ascii=False)[:160]))
        return ok
    except urllib.error.HTTPError as e:
        log('voice msg send (msg_id=%s) HTTP %s %s' % (with_msg_id, e.code, e.read().decode('utf-8', 'replace')[:250]))
        return False
    except Exception as e:
        log('voice msg send (msg_id=%s) error %r' % (with_msg_id, e))
        return False


def _send_media(CFG, tok, ev, file_info, log):
    # try as reply first; fall back to active (no msg_id)
    if _send_media_once(CFG, tok, ev, file_info, log, True):
        return True
    time.sleep(1.0)
    return _send_media_once(CFG, tok, ev, file_info, log, False)


def send_voice(CFG, ev, voice, log, get_token):
    """main entry: synthesize + upload + send. voice: str or (text, emo_text). returns True on success."""
    c = _cfg(CFG)
    if not c['enabled']:
        return False
    if isinstance(voice, (tuple, list)):
        voice_text = (voice[0] or '')
        emo_text = (voice[1] if len(voice) > 1 else None) or None
    else:
        voice_text = voice or ''
        emo_text = None
    voice_text = re.sub(r'\s+', ' ', voice_text).strip()[:c['max_chars']]
    if not voice_text:
        return False
    chat_key = ev['key']
    with _lock:
        last = _last_voice.get(chat_key, 0)
    if time.time() - last <= c['cooldown']:
        log('voice skipped (cooldown) key=' + chat_key[:20])
        return False
    try:
        data = synth(CFG, voice_text, log, emo_text)
    except Exception as e:
        log('voice synth failed %r' % (e,))
        return False
    tok = get_token()
    log('voice sending %d bytes emo=%s for key=%s text=%s' % (len(data), (emo_text or 'auto')[:20], chat_key[:20], voice_text[:60]))
    fi = _upload(CFG, tok, ev, data, log)
    if not fi:
        log('voice upload failed, give up')
        return False
    if _send_media(CFG, tok, ev, fi, log):
        with _lock:
            _last_voice[chat_key] = time.time()
        return True
    return False


def run_voice_task(CFG, ev, voice_text, log, get_token):
    try:
        send_voice(CFG, ev, voice_text, log, get_token)
    except Exception:
        log('voice task error\n' + __import__('traceback').format_exc())
