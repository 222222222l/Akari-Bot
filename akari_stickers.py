# akari_stickers.py - 表情包库：扫描、按意图选图、file_info 缓存（24h TTL）
import json
import os
import random
import re
import time

import akari_web

MAX_UPLOAD = 4 * 1024 * 1024
EXTS = ('.png', '.jpg', '.jpeg', '.gif')

_cache = {}
_cache_lock = None
_recent = {}
_send_ts = {}
CFG_GATE = None


def _norm(s):
    s = (s or '').lower().strip()
    s = re.sub(r'[\s_\-·、,，。.!！?？~～]+', '', s)
    return s


class Library:
    def __init__(self, root, meta_path, cache_path, log):
        self.root = root
        self.meta_path = meta_path
        self.cache_path = cache_path
        self.log = log
        self.meta = {}
        self.items = []
        self.load_meta()
        self.load_cache()
        self.scan()

    # ---------- 素材 ----------
    def load_meta(self):
        try:
            with open(self.meta_path, encoding='utf-8') as f:
                self.meta = json.load(f)
        except Exception:
            self.meta = {}

    def save_meta(self):
        try:
            tmp = self.meta_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.meta, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.meta_path)
        except Exception as e:
            self.log('sticker meta save failed %r' % (e,))

    def scan(self):
        out = []
        try:
            names = sorted(os.listdir(self.root))
        except Exception as e:
            self.log('sticker scan failed %r' % (e,))
            return out
        for n in names:
            low = n.lower()
            if not low.endswith(EXTS):
                continue
            p = os.path.join(self.root, n)
            try:
                sz = os.path.getsize(p)
            except Exception:
                continue
            if sz > MAX_UPLOAD or sz < 100:
                continue
            key = os.path.splitext(n)[0]
            m = self.meta.get(key) or {}
            tags = [_norm(x) for x in (m.get('tags') or [])]
            out.append({'key': key, 'nkey': _norm(key), 'path': p, 'size': sz,
                        'tags': tags, 'aliases': [_norm(x) for x in (m.get('aliases') or [])],
                        'desc': m.get('desc') or '', 'anim': bool(m.get('anim'))})
        self.items = out
        return out

    def add(self, data, key, tags=None, desc=''):
        key = re.sub(r'[^\w\u4e00-\u9fa5\-]', '_', key)[:40] or ('meme_%d' % int(time.time()))
        ext = '.png'
        if data[:3] == b'\xff\xd8\xff':
            ext = '.jpg'
        elif data[:4] == b'GIF8':
            ext = '.gif'
        path = os.path.join(self.root, key + ext)
        i = 1
        while os.path.exists(path):
            i += 1
            path = os.path.join(self.root, key + '_%d' % i + ext)
        with open(path, 'wb') as f:
            f.write(data)
        k = os.path.splitext(os.path.basename(path))[0]
        entry = self.meta.setdefault(k, {})
        if tags:
            entry['tags'] = list(dict.fromkeys((entry.get('tags') or []) + list(tags)))
        if desc:
            entry['desc'] = desc
        self.save_meta()
        self.scan()
        return k, path

    def remove(self, key):
        hit = [x for x in self.items if _norm(x['key']) == _norm(key)]
        if not hit:
            return ''
        p = hit[0]['path']
        try:
            os.remove(p)
        except Exception as e:
            self.log('sticker rm failed %r' % (e,))
            return ''
        self.meta.pop(hit[0]['key'], None)
        self.save_meta()
        self.forget(hit[0]['key'])
        self.scan()
        return hit[0]['key']

    # ---------- file_info 缓存（腾讯给 86400s，留 4h 余量）----------
    def load_cache(self):
        global _cache
        try:
            with open(self.cache_path, encoding='utf-8') as f:
                raw = json.load(f)
            now = time.time()
            _cache = {k: v for k, v in raw.items() if v.get('exp', 0) > now}
        except Exception:
            _cache = {}

    def save_cache(self):
        try:
            tmp = self.cache_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(_cache, f, ensure_ascii=False)
            os.replace(tmp, self.cache_path)
        except Exception as e:
            self.log('sticker cache save failed %r' % (e,))

    def forget(self, key):
        p = os.path.join(self.root, key + '.png')
        for k in list(_cache):
            if os.path.basename(k).rsplit('.', 1)[0] == key:
                _cache.pop(k, None)
        self.save_cache()

    def get_file_info(self, path, cfg, token, scene, target):
        ent = _cache.get(path)
        if ent and ent.get('exp', 0) > time.time():
            return ent['file_info'], 'cache'
        uuid, info = akari_web.qq_upload_image(cfg['api_base'], cfg['app_id'],
                                              token, target, path, scene)
        _cache[path] = {'file_info': info, 'uuid': uuid, 'exp': time.time() + 68000}
        self.save_cache()
        return info, 'upload'

    # ---------- 选图 ----------
    def pick(self, query='', recent_secs=90):
        if not self.items:
            return None
        now = time.time()
        for k, t in list(_recent.items()):
            if now - t > recent_secs:
                _recent.pop(k, None)
        q = _norm(query)
        cands = []
        if q:
            for it in self.items:
                if it['nkey'] == q or q in it['nkey'] or it['nkey'] in q:
                    cands.append(it)
            if not cands:
                for it in self.items:
                    if any(t and (q in t or t in q) for t in it['tags']):
                        cands.append(it)
            if not cands:
                for it in self.items:
                    if any(a and (q in a or a in q) for a in it['aliases']):
                        cands.append(it)
            if not cands:
                qq = q[:2]
                if len(qq) >= 2:
                    for it in self.items:
                        blob = it['nkey'] + ''.join(it['tags']) + it['desc']
                        if qq in blob:
                            cands.append(it)
        pool = [x for x in cands if x['key'] not in _recent] if cands else []
        if not pool:
            pool = [x for x in self.items if x['key'] not in _recent]
        if not pool:
            pool = cands or self.items
        hit = random.choice(pool)
        _recent[hit['key']] = now
        return hit

    def pick_strict(self, query=''):
        """只按关键词/标签/别名精确对应选图；对不上就返回 None（绝不随机塞图）。"""
        if not self.items:
            return None
        q = _norm(query)
        if not q:
            return None
        for it in self.items:
            if it['nkey'] == q:
                return it
        hits = [it for it in self.items
                if q in it['nkey'] or (it['nkey'] and it['nkey'] in q)]
        if not hits:
            hits = [it for it in self.items
                    if any(t and (q in t or t in q) for t in it['tags'])]
        if not hits:
            hits = [it for it in self.items
                    if any(a and (q in a or a in q) for a in it['aliases'])]
        if not hits:
            return None
        return random.choice(hits)

    def brief(self, limit=30):
        if not self.items:
            return '（表情包库是空的）'
        lines = []
        for it in self.items[:limit]:
            tag = ('/' + '、'.join(it['tags'][:4])) if it['tags'] else ''
            lines.append(it['key'] + tag + ('[动]' if it['anim'] else ''))
        more = '' if len(self.items) <= limit else (' 等%d个' % len(self.items))
        return '、'.join(lines) + more


def _token(cfg):
    import json as _j
    import urllib.request as _u
    r = _u.Request('https://bots.qq.com/app/getAppAccessToken',
                   data=_j.dumps({'appId': cfg['app_id'], 'clientSecret': cfg['app_secret']}).encode(),
                   headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                   method='POST')
    return _j.load(_u.urlopen(r, timeout=20))['access_token']


def send_sticker(lib, ev, cfg, token, log, query='', strict=False, cooldown_secs=0):
    """选一张图并发出。返回 (ok, key)。strict=True 时关键词必须对得上，否则不发。"""
    if CFG_GATE is not None and not CFG_GATE():
        return False, 'disabled'
    scope = (ev.get('scene') or 'g') + ':' + (ev.get('group_openid') or ev.get('user_openid') or '')
    if cooldown_secs > 0:
        last = _send_ts.get(scope, 0)
        if time.time() - last < cooldown_secs:
            log('sticker throttled scope=%s left=%ds' % (scope[:24], int(cooldown_secs - (time.time() - last))))
            return False, 'cooldown'
    if strict:
        hit = lib.pick_strict(query)
        if hit is None:
            log('sticker skip: no match for %r' % (query or '')[:40])
            return False, 'nomatch'
    else:
        hit = lib.pick(query)
    if not hit:
        return False, 'empty'
    scene = ev.get('scene') or 'group'
    target = ev.get('group_openid') if scene == 'group' else ev.get('user_openid')
    if not target:
        return False, 'no target'
    for attempt in (1, 2):
        try:
            info, how = lib.get_file_info(hit['path'], cfg, token, scene, target)
            ok, detail = akari_web.qq_send_image(cfg['api_base'], cfg['app_id'], token,
                                                target, info,
                                                # media + msg_id is rejected 40034024, so send without msg_id first
                                                '' if attempt == 1 else (ev.get('msg_id') or ''),
                                                scene)
            log('sticker %s via %s attempt=%d ok=%s' % (hit['key'], how, attempt, ok))
            if ok:
                _send_ts[scope] = time.time()
                return True, hit['key']
            _cache.pop(hit['path'], None)
            lib.save_cache()
        except Exception as e:
            log('sticker send failed attempt=%d %r' % (attempt, e))
            _cache.pop(hit['path'], None)
            lib.save_cache()
    return False, hit['key']


# ---------------- 与聊天流程的接口 ----------------
def hint(lib, limit=200):
    """注入 system prompt 的表情包说明书（由模型自主判断要不要发）。"""
    if not lib.items:
        return ''
    lines = []
    for it in lib.items[:limit]:
        tag = '/'.join((it['tags'] or [])[:3])
        lines.append(it['key'] + (('｜' + tag) if tag else '') + ('［动］' if it['anim'] else ''))
    return ('\n\n【表情包】你自己也有一套表情包，可以往群里发图。'
            '全部可用的（名字｜情绪标签）：\n' + '\n'.join(lines) + '\n'
            '要不要发，由你自己看着办，按下面几条判断：\n'
            '1. 先问自己：这张图是不是比文字更能接住对方这句话。只有对方在**抒发情绪**（抱怨、开心、惊讶、害羞、求安慰）时才考虑发；'
            '回答事实、报数字、说明事情、别人在问正经问题时，都不要发图。\n'
            '2. 宁可不发也不要凑，拿不准就当不发。名字必须从上面一字不差挑一个意思真对得上的；'
            '找不到贴合的就什么图都别发，绝对不准随便拉一张不相干的上去。\n'
            '3. 不要连环发图：自己刚发过图，接下来几条就老实说话。\n'
            '4. 决定要发时，在回复的最末尾单独一行写：STICKER: 名字'
            '（不加引号、不加句号、不加括号）。前面的正文照常写，图会自动跟在文字后发出去。\n'
            '5. 一条回复最多一张图。')


def extract(text):
    """把 STICKER: 行从正文里摘出来。返回 (干净正文, [关键词]) """
    out, queries = [], []
    for ln in (text or '').split('\n'):
        m = re.match(r'(?i)^\s*STICKERS?\s*[:：]\s*(.*)$', ln)
        if m:
            queries.append(m.group(1).strip()[:40])
        else:
            out.append(ln)
    return '\n'.join(out).strip(), queries


def fetch_bytes(url, headers=None, timeout=20, cap=4 * 1024 * 1024):
    import urllib.request
    req = urllib.request.Request(url, headers=headers or {'User-Agent': 'Mozilla/5.0'})
    resp = urllib.request.urlopen(req, timeout=timeout)
    data = resp.read(cap + 1)
    if len(data) > cap:
        raise RuntimeError('image too large')
    return data


def command(lib, ev, cfg, token, log, low):
    """处理表情相关指令。返回 (是否已处理, 需要回复的文字)。"""
    if lib is None:
        return (False, '')
    low = (low or '').strip()
    # /表情包  —— 列清单
    if low in ('/表情包', '/表情列表', '/表情包列表', '/memes'):
        return (True, '我现在有这些表情包：' + lib.brief())
    # /表情 [关键词]  —— 手动发一张
    if low.startswith('/表情') or low.startswith('/meme'):
        q = low.split(None, 1)[1].strip() if ' ' in low else ''
        ok, key = send_sticker(lib, ev, cfg, token, log, q)
        if not ok:
            if key == 'empty':
                return (True, '表情包库还是空的啦……先发张图再打 /收藏 名字 教我用呗')
            return (True, '唔……图发失败了（' + str(key) + '）')
        return (True, '')
    # /收藏 名字[:标签,标签]  —— 把本条消息里的图存进库
    if low.startswith('/收藏') or low.startswith('/收藏表情') or low.startswith('/save'):
        allowed = [x for x in (cfg.get('agent_allowed_openids') or []) if x]
        if allowed and (ev.get('user_openid') or '') not in allowed:
            return (True, '收���这件事只有主人能教我啦。(摇头)')
        atts = ev.get('attachments') or []
        if not atts:
            return (True, '这条消息里没有图呀，先发图再打 /收藏 名字 才好使')
        arg = low.split(None, 1)[1].strip() if ' ' in low else ''
        name, _, tagstr = arg.partition(':')
        tags = [t for t in re.split(r'[,，\s]+', tagstr) if t]
        name = re.sub(r'[:：/\\\\]', '', name)[:24] or ('meme_%d' % int(time.time()))
        saved = []
        for a in atts[:1]:
            url = (a or {}).get('url') or ''
            if not url.startswith('http'):
                return (True, '这张图的地址我取不到，存不了……')
            try:
                hdr = {'User-Agent': 'Mozilla/5.0', 'Authorization': chr(66) + 'earer ' + token}
                try:
                    data = fetch_bytes(url, {'User-Agent': 'Mozilla/5.0'})
                except Exception:
                    data = fetch_bytes(url, hdr)
                key, path = lib.add(data, name, tags, '')
                saved.append(key)
            except Exception as e:
                return (True, '存图失败了：' + repr(e)[:90])
        if not saved:
            return (True, '没存下来……')
        lib.scan()
        return (True, '收下啦！以后打 /表情 ' + saved[0] + ' 我就甩这张'
                + ('（标签：' + '、'.join(tags) + '）' if tags else ''))
    # /删表情 名字
    if low.startswith('/删表情') or low.startswith('/unmem'):
        allowed = [x for x in (cfg.get('agent_allowed_openids') or []) if x]
        if allowed and (ev.get('user_openid') or '') not in allowed:
            return (True, '删表情包也只有主人能做啦。')
        arg = low.split(None, 1)[1].strip() if ' ' in low else ''
        gone = lib.remove(arg)
        return (True, ('删掉「' + gone + '」了。' if gone else '库里没有叫这个的……'))
    return (False, '')