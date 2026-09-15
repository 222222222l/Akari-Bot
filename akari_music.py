"""Singing command contract. Standard library only; no models in the QQ process."""
import json
from pathlib import Path
import shlex

ROOT = Path(__file__).resolve().parent / 'music'
COMMANDS = {'/唱歌', '/翻唱', '/sing'}
HELP = '''【阿卡林唱歌】
/唱歌 + 同条消息的歌曲音频：保留旋律和伴奏，换成阿卡林音色
/唱歌 歌名：翻唱管理员已放入 music/songs 的歌曲
/唱歌 /片段 30：先试唱前30秒
/唱歌 /重编曲 /风格 "Japanese, anime rock"
下一行开始填写歌词；也可附带 UTF-8 歌词 .txt 和旋律 .abc
重编曲使用 YuE2；没有 .abc 时先自动转谱，音色转换使用阿卡林参考声音。
/唱歌 状态：检查环境安装情况
/进度、/取消、/文件、/补发：查询、停止和取回唱歌任务
仅歌曲名称不会自动搜索或下载原曲。'''


def settings(path=None):
    path = Path(path or ROOT / 'config.json').resolve()
    cfg = json.loads(path.read_text(encoding='utf-8'))
    for key in ('reference', 'svc_python', 'svc_repo', 'yue_python', 'sheetsage_python', 'sheetsage_repo'):
        cfg[key] = str((path.parent / cfg[key]).absolute())
    for key in ('svc_checkpoint', 'svc_model_config'):
        if cfg.get(key):
            cfg[key] = str((path.parent / cfg[key]).absolute())
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
        if token == '/重编曲':
            mode = 'yue'
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
    if style and mode != 'yue':
        raise ValueError('/风格 需配合 /重编曲 使用。')
    if seconds and mode == 'yue':
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


def handle(commands, ev, rest):
    if not commands.cfg.get('music_enabled', True):
        raise ValueError('唱歌功能已关闭。')
    if rest in ('帮助', 'help') or (not rest and not ev.get('attachments')):
        commands.reply(ev, HELP)
        return
    config_path = commands.cfg.get('music_config', str(ROOT / 'config.json'))
    cfg = settings(config_path)
    if rest == '状态':
        commands.reply(ev, installation_status(cfg))
        return
    operation = parse_request(rest)
    if operation['song']:
        catalog_source(operation['song'], cfg['root'])
    elif not ev.get('attachments'):
        raise ValueError('请同条消息附上歌曲音频，或填写曲库歌名。')
    timeout = min(int(commands.cfg.get('music_timeout', 3600)), int(commands.cfg.get('agent_max_timeout', 86400)))
    commands.submit(ev, '/超时%d 阿卡林翻唱：%s' % (timeout, rest or '本条歌曲附件'), operation=operation)
