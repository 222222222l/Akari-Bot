"""Singing command contract. Standard library only; no models in the QQ process."""
import json
import re
from pathlib import Path
import shlex

ROOT = Path(__file__).resolve().parent / 'music'
COMMANDS = {'/唱歌', '/翻唱', '/sing', '/音乐', '/作曲', '/生成音乐'}
HELP = '【阿卡林音乐】\n/音乐 写一首关于雨后放学的日语动漫歌：Pi 自动写歌词、选风格并生成\n/作曲 一首轻快的原创歌曲：无需提供音频\n/唱歌 帮我翻唱《某首歌》：Pi 自动找歌、选源并执行\n/翻唱 歌名或公开链接：本地曲库优先，缺少时自动搜索下载\n/翻唱 /片段 30 + 附件：保留原伴奏的30秒试听\n/唱歌 /原创 /风格 "Japanese, anime pop"\n下一行提供歌词：直接调用 YuE2 原创生成\n/唱歌 /重编曲 + 歌曲和歌词：沿用旋律重新编曲\n/唱歌 状态：检查安装与默认参考音色\n/进度、/取消、/文件、/补发：追踪、停止与取回音乐任务\n默认音色使用已配置的参考歌曲自动提取人声，无需再次提供。'


def is_request(text):
    text = re.sub(r'^(?:阿卡林|灯里|あかり|akari)[，,、：:\s]*', '', text.strip(), flags=re.I)
    if text.startswith('/'):
        return False
    if re.match(r'^(?:请|帮我|给我|为我|我想听|我想让你)', text):
        return bool(re.search(r'(?:唱|翻唱|写|创作|生成|作曲).{0,80}(?:歌|曲)|(?:翻唱|作曲)', text))
    return bool(re.match(r'^(?:唱|写|创作|生成|来)(?:一首|首|一段).{0,80}(?:歌|曲)|^翻唱\s*[^?？]+$', text))


def settings(path=None):
    path = Path(path or ROOT / 'config.json').resolve()
    cfg = json.loads(path.read_text(encoding='utf-8'))
    for key in ('reference', 'svc_python', 'svc_repo', 'yue_python', 'sheetsage_python', 'sheetsage_repo'):
        cfg[key] = str((path.parent / cfg[key]).absolute())
    for key in ('svc_checkpoint', 'svc_model_config'):
        if cfg.get(key):
            cfg[key] = str((path.parent / cfg[key]).absolute())
    if cfg.get('downloader') and '/' in cfg['downloader']:
        cfg['downloader'] = str((path.parent / cfg['downloader']).absolute())
    cfg['root'] = str(path.parent)
    return cfg


def installation_status(cfg):
    paths = {'阿卡林参考音频': cfg['reference'], '歌声转换环境': cfg['svc_python'],
             'Seed-VC': Path(cfg['svc_repo']) / 'inference.py', 'YuE2环境': cfg['yue_python'],
             '转谱环境': cfg['sheetsage_python'], 'SheetSage2': Path(cfg['sheetsage_repo']) / 'infer.py'}
    return '\n'.join('%s：%s' % (label, '文件已安装' if Path(path).is_file() else '未安装')
                     for label, path in paths.items()) + '\n此检查仅核对安装文件；GPU、权重和生成效果在执行时检查。'


def parse_request(text):
    """Options belong to the first line; preserve lyrics verbatim below it."""
    head, _, lyrics = text.partition('\n')
    tokens = shlex.split(head)
    mode, style, seconds, names = 'cover', '', None, []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in ('/重编曲', '/原创'):
            mode = 'yue' if token == '/重编曲' else 'generate'
        elif token in ('/风格', '/片段'):
            i += 1
            if i == len(tokens):
                raise ValueError(token + ' 后需要参数。')
            if token == '/风格':
                style = tokens[i]
                if not style or len(style) > 2000:
                    raise ValueError('风格描述需为1至2000字。')
            else:
                seconds = int(tokens[i])
                if not 5 <= seconds <= 120:
                    raise ValueError('试唱片段应在5至120秒之间。')
        elif token.startswith('/'):
            raise ValueError('不支持的唱歌参数：' + token)
        else:
            names.append(token)
        i += 1
    if style and mode not in ('yue', 'generate'):
        raise ValueError('/风格 需配合 /重编曲 或 /原创 使用。')
    if seconds and mode in ('yue', 'generate'):
        raise ValueError('/片段 用于原伴奏翻唱；重编曲请提供对应片段的音频和歌词。')
    return dict(kind='sing', mode=mode, song=' '.join(names), lyrics=lyrics.strip(), style=style, seconds=seconds)


def catalog_source(name, root=ROOT):
    catalog = (Path(root) / 'songs').resolve()
    if not name or Path(name).name != name or name in ('.', '..'):
        raise ValueError('请使用曲库歌名，或同条消息附上歌曲音频。')
    candidates = [p for p in catalog.glob('*') if p.is_file() and p.resolve().is_relative_to(catalog)
                  and p.suffix.lower() in {'.wav', '.flac', '.mp3', '.m4a', '.ogg', '.opus'}
                  and (p.name == name or p.stem == name)]
    if len(candidates) != 1:
        raise ValueError('曲库中未找到唯一的歌曲「%s」；请同条消息附上音频，或检查 music/songs。' % name)
    return candidates[0].resolve()


def handle(commands, ev, rest, natural=False):
    if not commands.cfg.get('music_enabled', True):
        raise ValueError('音乐功能已关闭。')
    if rest in ('帮助', 'help') or (not rest and not ev.get('attachments')):
        commands.reply(ev, HELP)
        return
    config_path = commands.cfg.get('music_config', str(ROOT / 'config.json'))
    cfg = settings(config_path)
    if rest == '状态':
        commands.reply(ev, installation_status(cfg) + '\n默认参考：' + Path(cfg['reference']).name)
        return
    timeout = min(int(commands.cfg.get('music_timeout', 3600)), int(commands.cfg.get('agent_max_timeout', 86400)))
    command = (ev.get('content') or '').split(maxsplit=1)[0]
    structured = any(flag in rest.split('\n',1)[0] for flag in ('/重编曲','/原创','/片段','/风格'))
    # Free-form requests go to the actual Pi agent, which has the native music tool.
    agent = natural or command in ('/音乐','/作曲','/生成音乐') or (
        command in ('/唱歌','/sing') and rest and not structured and not ev.get('attachments'))
    if agent:
        prompt = ('请实际完成以下音乐需求，使用原生 akari_music 工具自行规划并执行。'
                  '原创自动填写原创歌词与风格；翻唱自动核对歌曲并搜索/下载公开音源。'
                  '默认使用已配置的阿卡林参考音色，最终交付可播放音频。需求：\n' + rest)
        commands.submit(ev, '/超时%d %s' % (timeout, prompt), operation=dict(kind='music_agent'))
        return
    operation = parse_request(rest)
    if operation['mode'] == 'generate':
        if not operation['lyrics']:
            raise ValueError('/原创 的下一行需要歌词；想自动写词请用 /音乐 描述需求。')
        if operation['song']:
            raise ValueError('直接原创不需要歌名输入；用 /音乐 可描述歌曲主题。')
    elif not operation['song'] and not ev.get('attachments'):
        raise ValueError('请提供歌名、公开链接或歌曲附件；原创需求可直接使用 /音乐。')
    commands.submit(ev, '/超时%d 阿卡林音乐：%s' % (timeout, rest or '本条歌曲附件'), operation=operation)
