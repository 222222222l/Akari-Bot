#!/usr/bin/env python3
"""Task-local feedback helper; never sends directly to QQ."""
import argparse
import json
import os
from pathlib import Path
import shutil
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description='报告任务进展或登记需要发回 QQ 的文件')
    parser.add_argument('kind', choices=['progress', 'file'])
    parser.add_argument('content')
    args = parser.parse_args()
    task = Path(os.environ['AKARI_TASK_DIR']).resolve()
    output = task / 'artifacts'
    output.mkdir(exist_ok=True)
    if args.kind == 'file':
        source = Path(args.content).expanduser().resolve(strict=True)
        if not source.is_file():
            parser.error('只能登记普通文件；目录请先打包')
        if source.is_relative_to(output):
            target = source
        else:
            target = output / source.name
            if target.exists():
                target = output / (source.stem + '-' + uuid.uuid4().hex[:6] + source.suffix)
            temp = target.with_name('.' + target.name + '.uploading')
            shutil.copyfile(source, temp)
            os.replace(temp, target)
        event = dict(type='akari_artifact', path=str(target))
    else:
        event = dict(type='akari_progress', text=args.content[:1000])
    event['ts'] = time.time()
    data = (json.dumps(event, ensure_ascii=False) + '\n').encode()
    fd = os.open(task / 'feedback.jsonl', os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    print('已登记，交由 bot 发送。')


if __name__ == '__main__':
    main()
