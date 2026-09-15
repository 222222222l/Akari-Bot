# akari_web.py - 搜索 + QQ发图助手（放在 akari_bot.py 同目录）
import base64
import gzip
import io
import json
import urllib.error
import re
import urllib.parse
import urllib.request
import akari_delivery

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')


def _fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept-Language': 'zh-CN,zh;q=0.9,ja;q=0.6',
        'Accept-Encoding': 'gzip'})
    resp = urllib.request.urlopen(req, timeout=timeout)
    raw = resp.read()
    if resp.headers.get('Content-Encoding') == 'gzip':
        try:
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        except Exception:
            pass
    return raw.decode('utf-8', 'replace')


def strip_tags(html):
    html = re.sub(r'(?is)<(script|style).*?</\1>', ' ', html)
    html = re.sub(r'(?s)<[^>]+>', ' ', html)
    for a, b in (('&amp;', '&'), ('"', '"'), ('&#39;', "'"),
                 ('&nbsp;', ' '), ('&lt;', '<'), ('&gt;', '>'),
                 ('&#x27;', "'")):
        html = html.replace(a, b)
    return re.sub(r'\s+', ' ', html).strip()


def fetch_page(url, limit=2400):
    try:
        html = _fetch(url, timeout=18)
    except Exception:
        return ''
    m = re.search(r'(?is)<body.*?>(.*)</body>', html)
    return strip_tags(m.group(1) if m else html)[:limit]


def search(query, limit=5, logger=None):
    out = []
    q = urllib.parse.quote(query)
    try:
        html = _fetch('https://html.duckduckgo.com/html/?q=' + q)
        hrefs = re.findall(r'class="result__a"[^>]*href="([^"]+)"', html)[:limit]
        titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html)[:limit]
        snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html)[:limit]
        for i in range(len(hrefs)):
            u = hrefs[i]
            if 'uddg=' in u:
                u = urllib.parse.unquote(u.split('uddg=')[1].split('&')[0])
            out.append({'title': strip_tags(titles[i])[:120] if i < len(titles) else '',
                        'url': u[:160],
                        'snippet': strip_tags(snips[i])[:260] if i < len(snips) else ''})
    except Exception as e:
        if logger:
            logger('search ddg failed %r' % (e,))
    if not out:
        try:
            html = _fetch('https://www.bing.com/search?q=' + q + '&setlang=zh-hans')
            for href, title in re.findall(
                    r'(?is)<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>', html)[:limit]:
                out.append({'title': strip_tags(title)[:120],
                            'url': href[:160], 'snippet': ''})
        except Exception as e:
            if logger:
                logger('search bing failed %r' % (e,))
    out = [x for x in out if x.get('title')]
    for x in out[:2]:
        body = fetch_page(x['url']) if x.get('url', '').startswith('http') else ''
        if body:
            x['page'] = body
    return out


def format_results(query, items):
    if not items:
        return '【搜索失败】这次什么都没查到，老实说不知道吧。'
    lines = ['【查到的内容】关于「' + query + '」：']
    for i, it in enumerate(items, 1):
        seg = str(i) + '. ' + it['title']
        if it.get('snippet'):
            seg += ' —— ' + it['snippet']
        lines.append(seg)
        if it.get('page'):
            lines.append('   （该网页正文节选）' + it['page'][:900])
    lines.append('（请根据上面的正文节选，用地道的QQ聊天口吻回答，别念链接、别照抄大段原文，'
                 '70字以内。如果正文里确实没有答案，就老实说没查到，不要编。）')
    return '\n'.join(lines)[:6000]


def qq_upload_image(api_base, app_id, token, target, data, scene='group'):
    """上传本地图片，返回 (file_uuid, file_info)。data 可以是字节或文件路径。"""
    if isinstance(data, str):
        with open(data, 'rb') as f:
            data = f.read()
    body = {'file_type': 1,
            'file_data': base64.b64encode(data).decode(),
            'srv_send_msg': False}
    seg = 'groups' if scene == 'group' else 'users'
    url = api_base + '/v2/' + seg + '/' + target + '/files'
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                 headers={'Authorization': 'QQBot ' + token,
                                          'X-Union-Appid': str(app_id),
                                          'Content-Type': 'application/json'})
    d = json.load(urllib.request.urlopen(req, timeout=60))
    if not d.get('file_info'):
        raise RuntimeError('upload no file_info: ' + json.dumps(d, ensure_ascii=False)[:200])
    return d.get('file_uuid'), d['file_info']


def qq_send_image(api_base, app_id, token, target, file_info, msg_id='', scene='group', _log=None):
    seg = 'groups' if scene == 'group' else 'users'
    url = api_base + '/v2/' + seg + '/' + target + '/messages'
    body = {'msg_type': 7, 'media': {'file_info': file_info}}
    if msg_id:
        body.update(akari_delivery.reply_fields(dict(scene=scene, msg_id=msg_id,
                    group_openid=target if scene == 'group' else '', user_openid=target)))
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                 headers={'Authorization': 'QQBot ' + token,
                                          'X-Union-Appid': str(app_id),
                                          'Content-Type': 'application/json'})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=40))
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8', 'replace')
        except Exception:
            pass
        raise RuntimeError('http %s body=%s' % (e.code, body[:300]))
    out = json.dumps(d, ensure_ascii=False)[:200]
    if _log:
        _log('media send resp ' + out)
    return bool(d.get('id')), out
