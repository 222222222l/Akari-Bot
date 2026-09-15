# akari_image.py - 调用 ComfyUI 工作流自主生成图片并发送到 QQ
import json
import os
import random
import re
import threading
import time
import urllib.parse
import urllib.request

MAX_UPLOAD = 4 * 1024 * 1024
NL = chr(10)
_gen_lock = threading.Semaphore(1)
_send_ts = {}

RATIOS = {
    '1:1': '1:1 (Square)',
    '2:3': '2:3 (Portrait Photo)',
    '3:2': '3:2 (Photo)',
    '3:4': '3:4 (Portrait Standard)',
    '4:3': '4:3 (Standard)',
    '9:16': '9:16 (Portrait Widescreen)',
    '16:9': '16:9 (Widescreen)',
    '21:9': '21:9 (Ultrawide)',
}
_ALIAS = {
    '方': '1:1', '正方': '1:1', '方形': '1:1', '头像': '1:1',
    '横': '16:9', '横屏': '16:9', '横图': '16:9', '壁纸': '16:9',
    '竖': '9:16', '竖屏': '9:16', '竖图': '9:16', '手机': '9:16',
    '宽': '21:9', '超宽': '21:9', '人像': '2:3', '肖像': '3:4',
}


def norm_ratio(s):
    s = (s or '').strip().replace('：', ':').replace(' ', '')
    if not s:
        return RATIOS['1:1']
    if s in RATIOS:
        return RATIOS[s]
    if s in _ALIAS:
        return RATIOS[_ALIAS[s]]
    m = re.search(r'([0-9]+)[_:xX/]([0-9]+)', s)
    if m:
        k = m.group(1) + ':' + m.group(2)
        if k in RATIOS:
            return RATIOS[k]
    return RATIOS['1:1']


def _req(url, data=None, ctype='application/json', timeout=30):
    body = None
    headers = {'Accept': 'application/json, image/*, */*'}
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode('utf-8')
        headers['Content-Type'] = ctype
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method='POST' if data is not None else 'GET')
    return urllib.request.urlopen(req, timeout=timeout)


def _api(cfg, path, data=None, timeout=30):
    base = (cfg.get('comfy_url') or 'http://127.0.0.1:8190').rstrip('/')
    return json.load(_req(base + path, data=data, timeout=timeout))


# 画阿卡林本人才带触发词，画别的内容不带
_AKARI_RE = re.compile('(?i)(akaza|akari|akarin|赤座|灯里|阿卡林|团子头)')


def uses_akari(text, cfg=None):
    '''True 表示这张是画阿卡林本人，要走带 LoRA 的工作流。'''
    trig = (cfg or {}).get('image_trigger', 'Akaza-Akari')
    raw = (text or '')
    if trig and trig.lower() in raw.lower():
        return True
    return bool(_AKARI_RE.search(raw))

def ensure_trigger(text, cfg=None):
    '''提示词里提到阿卡林就补触发词，其他内容原样返回。'''
    trig = (cfg or {}).get('image_trigger', 'Akaza-Akari')
    text = (text or '').strip()
    if not trig or not text:
        return text
    if trig.lower() in text.lower():
        return text
    if _AKARI_RE.search(text):
        return trig + ', ' + text
    return text

def build_workflow(cfg, text, ratio, seed=None):
    if uses_akari(text, cfg):
        wf_path = (cfg.get('comfy_workflow')
                   or '/mnt/Comfyui/workflow/image_krea2_turbo_t2i.json')
    else:
        wf_path = (cfg.get('comfy_workflow_plain')
                   or '/mnt/Comfyui/workflow/image_krea2_turbo_t2i_plain.json')
        if not os.path.isfile(wf_path):
            wf_path = (cfg.get('comfy_workflow')
                       or '/mnt/Comfyui/workflow/image_krea2_turbo_t2i.json')
    with open(wf_path, encoding='utf-8') as f:
        wf = json.load(f)
    if seed is None:
        seed = random.randint(1, 2 ** 53)
    nt = str(cfg.get('image_node_text', '51'))
    nr = str(cfg.get('image_node_ratio', '49'))
    ns = str(cfg.get('image_node_sampler', '53'))
    for need in (nt, nr, ns):
        if need not in wf:
            raise RuntimeError('workflow node missing: ' + need)
    text = ensure_trigger(text, cfg)
    wf[nt]['inputs']['text'] = text
    wf[nr]['inputs']['aspect_ratio'] = ratio
    wf[nr]['inputs']['megapixels'] = float(cfg.get('image_megapixels', 1.0))
    wf[ns]['inputs']['seed'] = seed
    prefix = 'akari_' + time.strftime('%m%d_%H%M%S')
    for node in wf.values():
        if isinstance(node, dict) and node.get('class_type') == 'SaveImage':
            node['inputs']['filename_prefix'] = prefix
    return wf, seed


def _err_detail(h, pid):
    try:
        ent = h.get(pid) or {}
        msgs = ((ent.get('status') or {}).get('messages')) or []
        for m in msgs:
            if isinstance(m, list) and m and m[0] == 'execution_error':
                d = m[1] if len(m) > 1 and isinstance(m[1], dict) else {}
                return '%s | node=%s' % (str(d.get('exception_message'))[:200],
                                         str(d.get('node_id'))[:20])
        return json.dumps(ent.get('status'), ensure_ascii=False)[:250]
    except Exception:
        return 'unknown'


def generate(cfg, log, text, ratio='1:1', timeout=None):
    """跑一次工作流。返回 (png_bytes, seed)。失败抛异常。"""
    timeout = int(timeout or cfg.get('image_timeout', 180))
    wf, seed = build_workflow(cfg, text, ratio)
    cid = 'akari%d' % random.randint(1000, 9999)
    d = _api(cfg, '/prompt', {'prompt': wf, 'client_id': cid}, timeout=25)
    pid = d.get('prompt_id')
    if not pid:
        raise RuntimeError('comfy rejected: ' + json.dumps(d, ensure_ascii=False)[:400])
    log('comfy queued lora=%s prompt_id=%s ratio=%s seed=%d' % (
            'akari' if uses_akari(text, cfg) else 'off', pid[:12], ratio, seed))
    base = (cfg.get('comfy_url') or 'http://127.0.0.1:8190').rstrip('/')
    t0 = time.time()
    idle_logged = False
    while time.time() - t0 < timeout:
        time.sleep(1.5)
        h = _api(cfg, '/history/' + urllib.parse.quote(pid), timeout=25)
        if not h:
            if not idle_logged and time.time() - t0 > 30:
                idle_logged = True
                log('comfy still queueing after 30s')
            continue
        ent = h.get(pid) or {}
        st = ent.get('status') or {}
        if st.get('status_str') == 'error':
            raise RuntimeError('comfy error: ' + _err_detail(h, pid))
        for out in (ent.get('outputs') or {}).values():
            for img in (out.get('images') or []):
                q = urllib.parse.urlencode({
                    'filename': img['filename'],
                    'subfolder': img.get('subfolder', ''),
                    'type': img.get('type', 'output')})
                data = _req(base + '/view?' + q, timeout=90).read()
                if not data:
                    raise RuntimeError('comfy view returned empty')
                if len(data) > MAX_UPLOAD:
                    raise RuntimeError('image too large %d bytes' % len(data))
                log('comfy done %.1fs file=%s bytes=%d' % (
                    time.time() - t0, img['filename'], len(data)))
                return data, seed
        if st.get('completed'):
            raise RuntimeError('comfy completed without image output')
    raise RuntimeError('comfy timeout after %ds (prompt_id=%s)' % (timeout, pid[:12]))


def send_image_bytes(cfg, ev, token, log, data, tag='img'):
    """把图片字节发到消息所在会话。返回 ok。"""
    import akari_web
    scene = ev.get('scene') or 'group'
    target = ev.get('group_openid') if scene == 'group' else ev.get('user_openid')
    if not target:
        log('image send: no target')
        return False
    # upload once, reuse file_info (the old loop re-uploaded several MB on retry)
    try:
        _u, info = akari_web.qq_upload_image(cfg['api_base'], cfg['app_id'], token,
                                             target, data, scene)
    except Exception as e:
        log('image upload failed %r' % (e,))
        return False
    for attempt in (1, 2):
        try:
            ok, detail = akari_web.qq_send_image(cfg['api_base'], cfg['app_id'], token,
                                                 target, info,
                                                 '' if attempt == 1 else (ev.get('msg_id') or ''),
                                                 scene, _log=log)
            # send first without msg_id (media + msg_id is rejected 40034024)
            log('image %s attempt=%d ok=%s' % (tag, attempt, ok))
            if ok:
                return True
        except Exception as e:
            log('image send failed attempt=%d %r' % (attempt, e))
    return False


def request(cfg, log, ev, spec, token_get):
    """按 spec 生成并发出。spec 形如 '提示词 | 16:9'。返回 (ok, 说明)。"""
    if not cfg.get('image_enabled', True):
        return False, 'disabled'
    text, ratio = parse_spec(spec)
    if not text:
        return False, 'empty prompt'
    scope = (ev.get('scene') or 'g') + ':' + (
        ev.get('group_openid') or ev.get('user_openid') or '')
    cd = int(cfg.get('image_cooldown_secs', 120))
    max_wait = int(cfg.get('image_wait_secs', 240))
    arrived = time.time()
    deadline = arrived + max_wait
    # 冷却期内不再直接丢弃请求（此前重画请求会在这里被静默丢掉，ComfyUI 收不到任务）：
    # 等待冷却结束，总等待受 image_wait_secs 约束
    last_log = 0.0
    while cd > 0 and time.time() - _send_ts.get(scope, 0) < cd:
        left = cd - (time.time() - _send_ts.get(scope, 0))
        if time.time() > deadline:
            log('image wait gave up (cooldown) scope=%s left=%ds' % (scope[:24], int(left)))
            return False, 'wait timeout'
        if time.time() - last_log >= 15:
            last_log = time.time()
            log('image waiting cooldown scope=%s left=%ds' % (scope[:24], int(left)))
        time.sleep(2.0)
    # 排队期间同一会话已经有更新的图发出去了 -> 这张不用重复画
    if _send_ts.get(scope, 0) > arrived:
        log('image joined (newer image already sent) scope=%s' % scope[:24])
        return True, 'joined'
    if not _gen_lock.acquire(timeout=max(1.0, deadline - time.time())):
        log('image wait gave up (busy) scope=%s' % scope[:24])
        return False, 'wait timeout'
    if _send_ts.get(scope, 0) > arrived:
        _gen_lock.release()
        log('image joined (sent while waiting lock) scope=%s' % scope[:24])
        return True, 'joined'
    try:
        data, seed = generate(cfg, log, text, ratio)
        ok = send_image_bytes(cfg, ev, token_get(), log, data, tag=str(seed)[:6])
        if ok:
            _send_ts[scope] = time.time()
        return ok, 'seed=%d' % seed
    except Exception as e:
        log('image generate error %r' % (e,))
        return False, repr(e)[:160]
    finally:
        _gen_lock.release()


def parse_spec(spec):
    """'提示词 | 16:9' -> (prompt, ratio)"""
    s = (spec or '').strip()
    if not s:
        return '', RATIOS['1:1']
    parts = re.split('[|｜]', s, maxsplit=1)
    text = parts[0].strip()
    ratio = norm_ratio(parts[1]) if len(parts) > 1 else RATIOS['1:1']
    return text[:900], ratio


_IMG_RE = re.compile(r'(?i)^\s*(IMAGES?|IMAGEGEN|GENIMAGE|画图|生成图片|生图|画一张)'
                     r'\s*[:：]\s*(.*)$')


def extract(text):
    """把画图指令行从正文摘出来。返回 (干净正文, [spec...])"""
    out, specs = [], []
    for ln in (text or '').split(NL):
        m = _IMG_RE.match(ln)
        if m:
            specs.append(m.group(2).strip()[:900])
        else:
            out.append(ln)
    return NL.join(out).strip(), specs


_HINT = [
    '',
    '',
    '【画图】除了表情包，你还能用 ComfyUI 真的画一张图发到群里。',
    '调用方式：在回复的最末尾单独一行写',
    'IMAGE: 画面描述 | 比例',
    '',
    '比例只能从这几个里挑：1:1、4:3、3:2、16:9、21:9、3:4、2:3、9:16',
    '（不写就默认 1:1；手机竖图用 9:16，横向场景用 16:9）。',
    '只要对方说了横、竖、壁纸、头像、全屏这类词，就必须在竖线后面写明比例，不要漏掉。',
    '',
    '触发词：只有画阿卡林本人（阿卡林、灯里、我、粉发团子头）时才带 Akaza-Akari，放在最前面。',
    '例：IMAGE: Akaza-Akari, 粉发团子头少女坐在学校天台上看夕阳 | 3:4　不带这个词就不像我。',
    '画别人、动物、风景、食物、物品时千万别带这个词，带了我就会跑进画里。',
    '',
    '什么时候用：',
    '1. 别人明确要你画、要你生成一张图，或者想要壁纸、头像、设定图。',
    '2. 想表达的东西表情包完全装不下（具体场景、具体角色长相、具体构图）。',
    '3. 但如果对方要的是“会动的”“动图”“GIF”“让它动起来”，'
    '那就不要用 IMAGE，改用下面的 VIDEO 指令，发静图过去等于没做。',
    '除此之外的日常聊天绝对不要画图——画一张要等几十秒，很占显卡。',
    '',
    '注意：',
    '- 提示词中英文都行，写清楚主体、动作、场景、光线、风格。',
    '- 这个工作流没有负面提示词的位置，要避开什么就在正向里写清楚要什么。',
    '- 一条回复最多画一张，图和表情包同一轮只用一种。',
    '- 决定要画时，正文里先跟对方说一句你去画了，因为要等一会儿。',
]


def hint():
    return NL.join(_HINT)