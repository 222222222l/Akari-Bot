#!/usr/bin/env python3
"""Run the bot's actual singing worker locally, with no QQ sender."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from akari_music import parse_request
from akari_tasks import TaskStore
from akari_worker import TaskService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', default='残酷天使的行动纲领 /片段 30')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
    parser.add_argument('--timeout', type=int, default=3600)
    parser.add_argument('--lyrics-file', type=Path)
    parser.add_argument('--score', type=Path)
    args = parser.parse_args()
    store = TaskStore(args.output)
    ev = dict(scene='c2c', user_openid='local-test', group_openid='',
              msg_id=str(time.time_ns()), key=str(time.time_ns()), attachments=[], ts=time.time())
    operation = parse_request(args.request)
    if args.lyrics_file:
        operation['lyrics'] = args.lyrics_file.read_text(encoding='utf-8-sig')
    job = store.submit(ev, '本地阿卡林翻唱验证', args.timeout, str(args.output.resolve()), operation=operation)
    cfg = dict(dry_run=False, music_config=str(args.config.resolve()), agent_progress_secs=0)
    class LocalService(TaskService):
        def input_files(self, job):
            # Local CLI inputs use the same index as downloaded QQ attachments.
            if not args.score:
                return super().input_files(job)
            folder = Path(job['directory']) / 'inputs'
            destination = folder / 'melody.abc'
            shutil.copyfile(args.score, destination)
            files = [str(destination)]
            (folder / 'index.json').write_text(json.dumps(dict(files=files, errors=[])))
            return files, []
    service = LocalService(store, cfg, print)
    claimed = store.claim()
    if claimed['id'] != job['id']:
        raise RuntimeError('测试目录中存在旧排队任务，请使用新的输出目录。')
    thread = threading.Thread(target=service.execute, args=(claimed,))
    thread.start()
    previous = ''
    try:
        while thread.is_alive():
            state = store.get(job['id'])
            progress = state['progress']
            if previous != progress:
                print(progress, flush=True)
                previous = progress
            thread.join(1)
    except KeyboardInterrupt:
        store.cancel(job['id'], job['scope'])
        thread.join(15)
        raise
    state = store.get(job['id'])
    summary = {k: state[k] for k in ('id', 'status', 'result', 'error', 'directory', 'artifacts')}
    (args.output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if state['status'] == 'succeeded' else 1


if __name__ == '__main__':
    sys.exit(main())
