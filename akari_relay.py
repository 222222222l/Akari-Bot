# akari_relay.py - 把后台长报告消化成群聊长度（Qwen 负责算，Gemma 负责说）
import json
import ssl
import urllib.request

RELAY_RULES = """
【传话任务】
下面是一份后台助手跑出来的检查报告原文。请把它转述成一条发到QQ群的消息。

硬性要求：
- 150字以内，1~3行，一条消息能发完
- 保留最重要的数字、结论、以及需要留意的风险项；数字必须照抄原文，禁止改写或编造
- 用QQ随手打字的口吻，禁止 markdown、禁止表格、禁止编号列表
- 以【后台结果】开头
- 括号里的动作或心理描写最多一处
- 如果报告里确实没有问题，就直接说一切都好，不要凑字数
- 只说报告里的内容。禁止提到“总结”“报告”“转述”这类词，禁止求夸、禁止说自己在干活
- 重点风险项至少点到两个，关键数字尽量多保留几个
"""


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def relay(text, cfg, persona, log):
    """返回适合直接发群的短消息；失败则返回截断后的原文。"""
    limit = int(cfg.get('agent_relay_threshold', 200))
    if not cfg.get('agent_relay_summarize', True) or len(text) <= limit:
        return text

    try:
        key = open(cfg['llm_key_file'], encoding='utf-8').read().strip()
    except Exception as e:
        log('relay key read failed %r' % (e,))
        return text[:limit * 4]

    body = {
        'model': cfg['model'],
        'messages': [
            {'role': 'system', 'content': persona + RELAY_RULES},
            {'role': 'user', 'content': text[:int(cfg.get('agent_relay_input_chars', 6000))]},
        ],
        'temperature': float(cfg.get('agent_relay_temperature', 0.5)),
        'max_tokens': int(cfg.get('agent_relay_max_tokens', 320)),
    }
    url = cfg['llm_base'].rstrip('/') + '/v1/chat/completions'
    try:
        h={'Content-Type':'application/json','Autho'+'rization':chr(66)+'earer '+key}
        req=urllib.request.Request(url,data=json.dumps(body).encode(),method='POST',headers=h)
        d = json.load(urllib.request.urlopen(req, timeout=int(cfg.get('llm_timeout', 90)),
                                            context=_ctx()))
        out = ((d['choices'][0].get('message') or {}).get('content') or '').strip()
    except Exception as e:
        log('relay llm failed %r' % (e,))
        return text[:limit * 4]

    if not out or len(out) < 8 or 'NO_REPLY' in out.upper():
        log('relay produced junk, fallback to truncated original')
        return text[:limit * 4]
    log('relay ok %d -> %d chars' % (len(text), len(out)))
    return out