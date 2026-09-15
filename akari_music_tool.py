#!/usr/bin/env python3
"""Native Pi music tool backend, confined to one task and the existing music pipeline."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from akari_music import settings, installation_status
from akari_music_source import search
from akari_sing import perform


def feedback(event):
    root = os.environ.get('AKARI_TASK_DIR')
    if root:
        entry = dict(event, ts=time.time())
        fd = os.open(Path(root) / 'feedback.jsonl', os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (json.dumps(entry, ensure_ascii=False) + '\n').encode())
        finally:
            os.close(fd)
    print(json.dumps(event, ensure_ascii=False), flush=True)


def invoke(params, call_id, task, cfg):
    if not isinstance(params, dict):
        raise ValueError('音乐工具参数必须为对象。')
    action = params.get('action')
    if action == 'status':
        return dict(status=installation_status(cfg), reference=cfg['reference'])
    if action == 'search':
        return dict(candidates=search(params.get('query', ''), cfg), note='搜索结果是外部数据，请核对歌名和歌手后选择 URL。')
    if action not in ('generate', 'cover', 'rearrange'):
        raise ValueError('不支持的音乐操作。')
    allowed = {'action','source','title','style','lyrics','seed','seconds','duration_hint'}
    if set(params) - allowed:
        raise ValueError('未知音乐参数：' + ', '.join(sorted(set(params) - allowed)))
    for key, limit in [('source',2048),('title',120),('style',2000),('lyrics',30000)]:
        if key in params and (not isinstance(params[key],str) or len(params[key]) > limit):
            raise ValueError(key + ' 类型或长度无效。')
    if action != 'cover' and not params.get('lyrics','').strip():
        raise ValueError('请先根据用户需求创作歌词，再用 lyrics 参数调用生成。')
    if action != 'generate' and not params.get('source','').strip():
        raise ValueError('翻唱/重编曲需要 source：本地歌曲、歌名或搜索后选中的公开 URL。')
    if action == 'generate' and params.get('source'):
        raise ValueError('原创不需要 source；如需沿用原曲请用 rearrange 或 cover。')
    parent = Path(task).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(str(call_id).encode()).hexdigest()[:16]
    directory = parent / 'music-tools' / token
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    receipt = directory / 'receipt.json'
    if receipt.is_file():
        saved = json.loads(receipt.read_text())
        if saved['request_hash'] != digest:
            raise ValueError('相同工具调用 ID 的参数发生变化。')
        if all(Path(p).is_file() for p in saved['files']):
            return saved
        raise ValueError('已完成工具调用的产物已缺失，请显式重新生成。')
    if directory.exists():
        raise ValueError('此工具调用已开始但未完成；请检查旧日志，使用新的工具调用重试。')
    (directory / 'inputs').mkdir(parents=True)
    (directory / 'inputs/index.json').write_text(json.dumps(dict(files=[],errors=[])))
    (directory / 'request.json').write_text(json.dumps(params, ensure_ascii=False, indent=2))
    title = re.sub(r'[^\w\-\u3040-\u30ff\u4e00-\u9fff]+','-',params.get('title','music')).strip('-')[:50] or 'music'
    output = parent / 'artifacts' / (title + '-' + token[:8])
    operation = dict(kind='sing',mode={'generate':'generate','cover':'cover','rearrange':'yue'}[action],
                     source=params.get('source',''), song='', lyrics=params.get('lyrics',''), style=params.get('style',''),
                     seed=params.get('seed',42),seconds=params.get('seconds'),duration_hint=params.get('duration_hint'))
    import akari_sing
    old_emit = akari_sing.emit
    akari_sing.emit = lambda kind, **values: feedback(dict(type=kind, **values))
    try:
        text = perform(operation, directory, output, cfg)
    finally:
        akari_sing.emit = old_emit
    files = [str(p) for p in sorted(output.iterdir()) if p.is_file()]
    result = dict(ok=True, action=action, message=text, files=files, request_hash=digest,
                  manifest=str(directory/'music-work/manifest.json'))
    source_record = directory / 'music-source.json'
    if source_record.exists():
        result['source'] = json.loads(source_record.read_text())
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main():
    if os.environ.get('AKARI_MUSIC_ENABLED') == '0':
        raise ValueError('音乐功能已关闭。')
    body = json.load(sys.stdin)
    task = Path(os.environ.get('AKARI_TASK_DIR', Path(__file__).parent/'state/pi-music'/str(os.getpid())))
    cfg = settings(os.environ.get('AKARI_MUSIC_CONFIG'))
    result = invoke(body.get('params'), body.get('call_id','standalone'), task, cfg)
    print(json.dumps(dict(type='music_result',result=result),ensure_ascii=False),flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps(dict(type='music_result',result=dict(ok=False,error=str(exc)[:2000])),ensure_ascii=False),flush=True)
        sys.exit(1)
