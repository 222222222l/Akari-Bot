# akari_video.py - 把一张静图送进 ComfyUI 视频工作流，产出 GIF 发到 QQ
#
# 指令格式：VIDEO: [首帧来源] | 怎么动 | 比例 | 秒数
#   首帧来源三选一：
#     @附件 / @attach / @1     用对方这条消息发来的图（@2 是第二张）
#     @文件名 或 @绝对路径      显式指定已有图片（ComfyUI 的 input/output 目录）
#     留空而直接写画面描述      由我用画图工作流现画一张首帧
import base64
import io
import json
import os
import random
import re
import threading
import time
import urllib.parse
import urllib.request

NL = chr(10)
SAFE_BYTES = int(3.6 * 1024 * 1024)
MAX_FRAME_PX = 12000000
_gen_lock = threading.Semaphore(1)
_send_ts = {}

# (缩放比, 每 N 帧留 1 帧, 色数) 画质从高往低试，直到塞进 QQ 的 4MB 上限
_LADDER = [(1.0, 2, 192), (0.8, 2, 160), (0.7, 2, 128), (0.6, 2, 128),
           (0.6, 3, 96), (0.5, 3, 80)]

_DEF_MOTION = 'the subject moves gently and naturally, subtle breathing, soft motion'
_ATTACH_WORDS = ('附件', '附件图', '这张图', '这张', '这图', '上面那张', '刚才那张',
                 'attach', 'attached', 'image', 'img', 'this')
# 对方发了图又用这些词指着它 -> 首帧直接用那张附件，不用模型写 @附件
_DEICTIC = ('这张', '这图', '上面那张', '刚才那张', '我发的', '我这张',
            '帮我动', '让它动', '让她动', '让他动', '动起来', '动一下', '做成动图',
            'animate this', 'make it move', 'this image', 'this photo')


def _req(url, data=None, timeout=60, headers=None):
    body = None
    hd = {'Accept': 'application/json, image/*, */*'}
    if headers:
        hd.update(headers)
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode('utf-8')
        hd['Content-Type'] = 'application/json'
    r = urllib.request.Request(url, data=body, headers=hd,
                               method='POST' if data is not None else 'GET')
    return urllib.request.urlopen(r, timeout=timeout)


def _base(cfg):
    return (cfg.get('video_comfy_url') or cfg.get('comfy_url')
            or 'http://127.0.0.1:8190').rstrip('/')


def _api(cfg, path, data=None, timeout=30):
    return json.load(_req(_base(cfg) + path, data=data, timeout=timeout))


def _find(wf, cls):
    hit = [nid for nid, nd in wf.items()
           if isinstance(nd, dict) and nd.get('class_type') == cls]
    if not hit:
        raise RuntimeError('workflow node missing: ' + cls)
    return hit[0]


def build_workflow(cfg, png_name, motion, seconds, seed=None):
    wf_path = (cfg.get('video_workflow')
               or '/mnt/Comfyui/workflow/video_minimax_h3_i2v.json')
    with open(wf_path, encoding='utf-8') as f:
        wf = json.load(f)
    if seed is None:
        seed = random.randint(1, 2 ** 53)
    dur = float(seconds)
    lo = float(cfg.get('video_secs_min', 2))
    hi = float(cfg.get('video_secs_max', 5))
    dur = min(max(dur, lo), hi)
    wf[_find(wf, 'LoadImage')]['inputs']['image'] = png_name
    wf[_find(wf, 'MiniMaxH3ImageToVideo')]['inputs']['prompt'] = motion
    wf[_find(wf, 'RandomNoise')]['inputs']['noise_seed'] = seed
    wf[_find(wf, 'PrimitiveFloat')]['inputs']['value'] = dur
    if cfg.get('video_megapixels'):
        wf[_find(wf, 'ImageScaleToTotalPixels')]['inputs']['megapixels'] = float(
            cfg['video_megapixels'])
    prefix = 'akari_' + time.strftime('%m%d_%H%M%S')
    for node in wf.values():
        if isinstance(node, dict) and node.get('class_type') == 'VHS_VideoCombine':
            node['inputs']['filename_prefix'] = prefix
    return wf, seed, dur


def _err_detail(ent):
    try:
        msgs = ((ent.get('status') or {}).get('messages')) or []
        for m in msgs:
            if isinstance(m, list) and m and m[0] == 'execution_error':
                d = m[1] if len(m) > 1 and isinstance(m[1], dict) else {}
                return '%s | node=%s' % (str(d.get('exception_message'))[:220],
                                         str(d.get('node_id'))[:20])
        return json.dumps(ent.get('status'), ensure_ascii=False)[:250]
    except Exception:
        return 'unknown'


# ---------------- 首帧归一化与取材 ----------------

def to_png(data, tag='frame'):
    """任何输入（png/jpg/webp/gif 的字节或 data URL）统一成 RGB PNG 字节。"""
    if isinstance(data, str):
        s = data.strip()
        if s.startswith('data:'):
            s = s.split(',', 1)[-1]
        try:
            data = base64.b64decode(s)
        except Exception:
            raise RuntimeError('bad base64 ' + tag)
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.seek(0)                      # GIF/动图取首帧
    im = im.convert('RGB')
    if im.width * im.height > MAX_FRAME_PX:
        k = (MAX_FRAME_PX / float(im.width * im.height)) ** 0.5
        im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))),
                       Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format='PNG')
    return buf.getvalue()


def _dirs(cfg):
    inp = cfg.get('comfy_input_dir') or '/mnt/Comfyui/input'
    out = cfg.get('comfy_output_dir') or '/mnt/Comfyui/output'
    return inp, out


def _from_local(cfg, ref):
    """按文件名或路径找图；input 找不到就去 output 挑 mtime 最新的匹配项。"""
    inp, out = _dirs(cfg)
    ref = ref.strip().strip('"').strip("'")
    if os.path.isabs(ref):
        if os.path.isfile(ref):
            return open(ref, 'rb').read(), ref
        raise RuntimeError('file not found: ' + ref[:80])
    for d in (inp, out):
        p = os.path.join(d, ref)
        if os.path.isfile(p):
            return open(p, 'rb').read(), p
    stem = re.sub(r'\.(png|jpg|jpeg|webp|gif|bmp)$', '', ref, flags=re.I)
    best = None
    for d in (inp, out):
        try:
            names = os.listdir(d)
        except Exception:
            continue
        for n in names:
            if not n.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif')):
                continue
            if stem.lower() in n.lower():
                p = os.path.join(d, n)
                if best is None or os.path.getmtime(p) > os.path.getmtime(best):
                    best = p
    if best:
        return open(best, 'rb').read(), best
    raise RuntimeError('no image matching: ' + ref[:80])


def _download(url, token_get, cfg):
    cap = int(cfg.get('vision_max_bytes', 8000000))
    raw = b''
    for attempt in (1, 2):
        try:
            hd = {'User-Agent': 'Mozilla/5.0'}
            if attempt == 2 and token_get:
                hdr = {'Authorization': 'QQBot ' + token_get()}
                hd.update(hdr)
            raw = _req(url, timeout=int(cfg.get('vision_dl_timeout', 25)),
                       headers=hd).read(cap + 1)
            break
        except Exception:
            raw = b''
    if not raw:
        raise RuntimeError('download frame failed: ' + url[:90])
    if len(raw) > cap:
        raise RuntimeError('frame image too large')
    return raw


def _from_attach(cfg, log, ev, idx, token_get):
    att = [a for a in (ev.get('attachments') or []) if (a or {}).get('url')]
    if not att:
        raise RuntimeError('no attached image in this message')
    i = idx if 0 < idx <= len(att) else 1
    if i > len(att):
        i = 1
    a = att[i - 1] or {}
    url = a.get('url') or ''
    if not url.startswith('http'):
        raise RuntimeError('attached image has no url')
    log('video frame from attachment #%d/%d kind=%s' % (i, len(att), a.get('url_type')))
    return _download(url, token_get, cfg), 'attachment#' + str(i)


def pick_frame(cfg, log, ev, ref, token_get):
    """返回 (png_bytes, 来源说明)。ref 为空表示要用文字现画，交给调用方处理。"""
    r = (ref or '').strip()
    if not r:
        return None, ''
    low = r.lower().lstrip('@').strip()
    if r.startswith('http://') or r.startswith('https://'):
        return to_png(_download(r, token_get, cfg)), 'url'
    m = re.match(r'^(附件图|附件|这张图|这张|这图|上面那张|刚才那张|attach\w*|image|img|this)$',
                 low) or low in _ATTACH_WORDS
    if m or low.isdigit():
        idx = int(low) if low.isdigit() else 1
        return to_png(_from_attach(cfg, log, ev, idx, token_get)[0]), 'attachment'
    data, path = _from_local(cfg, low)
    log('video frame from file ' + os.path.basename(path))
    return to_png(data), 'file:' + os.path.basename(path)[:60]


# ---------------- 落盘 / 生成 / 压缩 / 发送 ----------------

def stage_frame(cfg, log, png_bytes):
    inp, _o = _dirs(cfg)
    name = ('akari_frame_' + time.strftime('%m%d_%H%M%S') + '_'
            + str(random.randint(100, 999)) + '.png')
    with open(os.path.join(inp, name), 'wb') as f:
        f.write(png_bytes)
    log('video frame staged ' + name + ' bytes=%d' % len(png_bytes))
    return name


def unstage(cfg, name):
    try:
        inp, _o = _dirs(cfg)
        if name:
            os.remove(os.path.join(inp, name))
    except Exception:
        pass


def generate(cfg, log, png_name, motion, seconds, timeout=None):
    """跑视频工作流。返回 (gif_bytes, seed)。失败抛异常。"""
    timeout = int(timeout or cfg.get('video_timeout', 420))
    wf, seed, dur = build_workflow(cfg, png_name, motion, seconds)
    cid = 'akari%d' % random.randint(1000, 9999)
    d = _api(cfg, '/prompt', {'prompt': wf, 'client_id': cid}, timeout=30)
    pid = d.get('prompt_id')
    if not pid:
        raise RuntimeError('comfy rejected: ' + json.dumps(d, ensure_ascii=False)[:400])
    log('video queued prompt_id=%s dur=%ss seed=%d' % (pid[:12], dur, seed))
    base = _base(cfg)
    t0 = time.time()
    slow = False
    while time.time() - t0 < timeout:
        time.sleep(2)
        try:
            h = _api(cfg, '/history/' + urllib.parse.quote(pid), timeout=30)
        except Exception as e:
            log('video poll err %r' % (e,))
            continue
        if not h:
            if not slow and time.time() - t0 > 60:
                slow = True
                log('video still loading models after 60s')
            continue
        ent = h.get(pid) or {}
        st = ent.get('status') or {}
        if st.get('status_str') == 'error':
            raise RuntimeError('comfy error: ' + _err_detail(ent))
        for out in (ent.get('outputs') or {}).values():
            for key in ('gifs', 'videos', 'images'):
                for item in (out.get(key) or []):
                    q = urllib.parse.urlencode({
                        'filename': item['filename'],
                        'subfolder': item.get('subfolder', ''),
                        'type': item.get('type', 'output')})
                    data = _req(base + '/view?' + q, timeout=120).read()
                    if not data:
                        raise RuntimeError('comfy view returned empty')
                    log('video raw %.1fs file=%s bytes=%d' % (
                        time.time() - t0, item['filename'], len(data)))
                    return data, seed
        if st.get('completed'):
            raise RuntimeError('comfy completed without video output')
    raise RuntimeError('video timeout after %ds (prompt_id=%s)' % (timeout, pid[:12]))


def shrink_gif(cfg, log, data):
    """GIF 通常远超 QQ 的 4MB 上限，按阶梯压到能发。返回 (bytes, 说明)。"""
    if len(data) <= SAFE_BYTES:
        return data, 'raw %.2fMB ok' % (len(data) / 1048576.0)
    from PIL import Image, ImageSequence
    src = Image.open(io.BytesIO(data))
    frames = [f.convert('RGB') for f in ImageSequence.Iterator(src)]
    one = int(src.info.get('duration') or 40)
    tried = []
    for scale, keep, colors in _LADDER:
        picked = list(range(0, len(frames), keep))
        if len(picked) < 2:
            continue
        small = []
        for i in picked:
            f = frames[i]
            small.append(f.resize((max(1, int(f.width * scale)),
                                   max(1, int(f.height * scale))), Image.LANCZOS))
        # 全片共用一张调色板，避免逐帧量化导致的闪烁
        pal = small[0].quantize(colors=colors, method=2, dither=1)
        conv = [pal] + [f.quantize(palette=pal, dither=0) for f in small[1:]]
        buf = io.BytesIO()
        conv[0].save(buf, format='GIF', save_all=True, append_images=conv[1:],
                     duration=[one * keep] * len(conv), loop=0, optimize=True)
        out = buf.getvalue()
        tried.append('%.1fx%d=%.1fMB' % (scale, keep, len(out) / 1048576.0))
        if len(out) <= SAFE_BYTES:
            log('gif shrunk %dx%d frames=%d %d bytes' % (
                conv[0].width, conv[0].height, len(conv), len(out)))
            return out, '%.2fMB [%s]' % (len(out) / 1048576.0, ' > '.join(tried))
    raise RuntimeError('gif too large even after ladder: ' + ' > '.join(tried))


def send_gif(cfg, ev, token, log, data, tag='gif'):
    import akari_web
    scene = ev.get('scene') or 'group'
    target = ev.get('group_openid') if scene == 'group' else ev.get('user_openid')
    if not target:
        log('gif send: no target')
        return False
    # upload once, reuse file_info (the old loop re-uploaded several MB on retry)
    try:
        _u, info = akari_web.qq_upload_image(cfg['api_base'], cfg['app_id'], token,
                                             target, data, scene)
    except Exception as e:
        log('gif upload failed %r' % (e,))
        return False
    for attempt in (1, 2):
        try:
            ok, _d = akari_web.qq_send_image(cfg['api_base'], cfg['app_id'], token,
                                             target, info,
                                             # send first without msg_id (media + msg_id -> 40034024)
                                             '' if attempt == 1 else (ev.get('msg_id') or ''),
                                             scene, _log=log)
            log('gif %s attempt=%d ok=%s' % (tag, attempt, ok))
            if ok:
                return True
        except Exception as e:
            log('gif send failed attempt=%d %r' % (attempt, e))
    return False


# ---------------- 指令解析 ----------------

_SPLIT = re.compile('[|｜]')
_RATIO = re.compile(r'^\s*([0-9]{1,2})\s*[:：xX/]\s*([0-9]{1,2})\s*$')
_SECS = re.compile(r'^\s*([0-9]+(?:\.[0-9]+)?)\s*(?:s|S|秒)?\s*$')
_REF = re.compile(r'^\s*(?:@|/|https?://|[A-Za-z0-9_\-\.]+\.(?:png|jpe?g|webp|gif)\s*$)')


def split_spec(spec, cfg=None):
    """-> dict(ref, frame_prompt, motion, ratio, secs)"""
    cfg = cfg or {}
    spec = re.sub(r'^\s*(?:/动图|/动|/gif)\s*', '', spec or '', flags=re.I)
    parts = [x.strip() for x in _SPLIT.split(spec) if x.strip()]
    ratio, secs, ref, texts = '', float(cfg.get('video_default_secs', 3)), '', []
    for p in parts:
        m = _RATIO.match(p)
        if m:
            ratio = m.group(1) + ':' + m.group(2)
            continue
        m = _SECS.match(p)
        if m and len(p) <= 6:
            secs = float(m.group(1))
            continue
        if not ref and _REF.match(p):
            ref = p
            continue
        texts.append(p)
    if ref:
        frame_prompt = ''
        motion = ' '.join(texts) if texts else _DEF_MOTION
    elif len(texts) >= 2:
        frame_prompt, motion = texts[0], ' '.join(texts[1:])
    elif len(texts) == 1:
        frame_prompt, motion = texts[0], _DEF_MOTION
    else:
        frame_prompt, motion = '', _DEF_MOTION
    return {'ref': ref, 'frame_prompt': frame_prompt, 'motion': motion,
            'ratio': ratio, 'secs': secs}


def request(cfg, log, ev, spec, token_get):
    """一条龙：定首帧 -> 跑视频 -> 压 GIF -> 发出。返回 (ok, 说明)。"""
    if not cfg.get('video_enabled', True):
        return False, 'disabled'
    sp = split_spec(spec, cfg)
    # 兜底：对方发了图又指着它要动图，模型却忘了写 @附件 -> 直接用那张图
    if (not sp['ref'] and cfg.get('video_prefer_attach', True)
            and any((a or {}).get('url') for a in (ev.get('attachments') or []))):
        ask = ((ev.get('content') or '') + ' ' + sp['frame_prompt']).lower()
        for w in _DEICTIC:
            if w in ask:
                log('video auto-use attachment (deictic ' + w + ')')
                # 模型写的那段画面描述当成动作留着，别丢
                if sp['frame_prompt']:
                    if sp['motion'] == _DEF_MOTION:
                        sp['motion'] = sp['frame_prompt']
                    else:
                        sp['motion'] = sp['frame_prompt'] + ', ' + sp['motion']
                sp['ref'] = '@附件'
                sp['frame_prompt'] = ''
                break
    scope = (ev.get('scene') or 'g') + ':' + (
        ev.get('group_openid') or ev.get('user_openid') or '')
    cd = int(cfg.get('video_cooldown_secs', 240))
    max_wait = int(cfg.get('video_wait_secs', 300))
    arrived = time.time()
    deadline = arrived + max_wait
    # 冷却期内不再直接丢弃请求：等待冷却结束，总等待受 video_wait_secs 约束
    last_log = 0.0
    while cd > 0 and time.time() - _send_ts.get(scope, 0) < cd:
        left = cd - (time.time() - _send_ts.get(scope, 0))
        if time.time() > deadline:
            log('video wait gave up (cooldown) scope=%s left=%ds' % (scope[:24], int(left)))
            return False, 'wait timeout'
        if time.time() - last_log >= 15:
            last_log = time.time()
            log('video waiting cooldown scope=%s left=%ds' % (scope[:24], int(left)))
        time.sleep(2.0)
    if _send_ts.get(scope, 0) > arrived:
        log('video joined (newer gif already sent) scope=%s' % scope[:24])
        return True, 'joined'
    import akari_image
    # borrowed lock: draw and gif must never share the GPU at once
    lk = getattr(akari_image, '_gen_lock', _gen_lock)
    if not lk.acquire(timeout=max(1.0, deadline - time.time())):
        log('video wait gave up (busy) scope=%s' % scope[:24])
        return False, 'wait timeout'
    if _send_ts.get(scope, 0) > arrived:
        lk.release()
        log('video joined (sent while waiting lock) scope=%s' % scope[:24])
        return True, 'joined'
    name = ''
    try:
        png, src = pick_frame(cfg, log, ev, sp['ref'], token_get)
        if png is None:
            if not sp['frame_prompt']:
                return False, 'empty frame'
            import akari_image
            png, _fs = akari_image.generate(cfg, log, sp['frame_prompt'],
                                            akari_image.norm_ratio(sp['ratio']))
            src = 'drawn'
        name = stage_frame(cfg, log, png)
        t0 = time.time()
        raw, seed = generate(cfg, log, name, sp['motion'], sp['secs'])
        data, note = shrink_gif(cfg, log, raw)
        log('video total %.1fs frame=%s' % (time.time() - t0, src))
        ok = send_gif(cfg, ev, token_get(), log, data, tag=str(seed)[:6])
        if ok:
            _send_ts[scope] = time.time()
        return ok, 'seed=%d frame=%s %s' % (seed, src, note)
    except Exception as e:
        log('video error %r' % (e,))
        return False, repr(e)[:160]
    finally:
        unstage(cfg, name)
        lk.release()


_VID_RE = re.compile(r'(?i)^\s*(VIDEOS?|GIFS?|ANIMATED|动图|生成动图|做张动图|让它动)'
                     r'\s*[:：]\s*(.*)$')


def extract(text):
    """把动图指令行从正文摘出来。返回 (干净正文, [spec...])"""
    out, specs = [], []
    for ln in (text or '').split(NL):
        m = _VID_RE.match(ln)
        if m:
            specs.append(m.group(2).strip()[:600])
        else:
            out.append(ln)
    return NL.join(out).strip(), specs


_HINT = [
    '',
    '',
    '【动图】你还能把一张静图变成会动的 GIF 发出去（首帧可以现画，也可以用现成的图）。',
    '用法：回复末尾单独一行写',
    'VIDEO: [首帧] | 怎么动 | 比例 | 秒数',
    '',
    '首帧三种给法（从左到右挑一种）：',
    'A. 对方这条消息发了图、要你让它动起来 -> 首帧写 @附件（第二张写 @2）',
    '   例：VIDEO: @附件 | 她眨眨眼然后对镜头挥手 | 3秒',
    'B. 用已有的一张图 -> 首帧写 @文件名 或 @图片路径',
    '   例：VIDEO: @akari_0830_161821_00001_.png | 慢慢下雪，树枝轻摆 | 4秒',
    'C. 没有现成的图，你现画一张首帧 -> 首帧直接写画面描述（要写清主体、场景、光线、风格）',
    '   例：VIDEO: 樱花树下的粉发少女回头微笑，动漫风 | 她回头并挥手，头发被风吹起 | 3:4 | 3秒',
    '',
    '第二段只写动作（挥手、眨眼、回头、下雪、风吹），别重复长相，那是首帧的事。',
    '比例只在 C 有用（1:1、4:3、3:2、16:9、21:9、3:4、2:3、9:16）；用现成图时会自动跟原图同比例。',
    '秒数 2 到 5，不写算 3 秒。',
    '',
    '什么时候用：',
    '1. 对方明确要动图、GIF、会动的图、"让她动起来"。',
    '2. 对方发来一张图、指着它让你做成动图 —— 这时候首帧一定要写 @附件，别自己重画。',
    '3. 要表现的就是动作本身，静态图装不下。',
    '平时绝对不要用！跑一次比画图慢十倍、极占显卡。用了 VIDEO 就别再补 IMAGE 行。',
    '',
    '动作写英文更稳，中文也认。一条回复最多一个动图。',
    '决定要做了，正文里先跟对方说一声，这个要等一小会儿。',
]


def hint():
    return NL.join(_HINT)