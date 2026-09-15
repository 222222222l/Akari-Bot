"""Opt-in real pi smoke test. It never starts QQ transport or sends a message."""
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from akari_tasks import TaskStore
from akari_worker import TaskService


def main():
    base = Path(__file__).resolve().parents[1]
    cfg = json.loads((base / 'config.json').read_text())
    cfg.update(dry_run=False, agent_progress_secs=0, agent_auto_artifacts=0)
    with tempfile.TemporaryDirectory(prefix='akari-smoke-') as directory:
        store = TaskStore(directory)
        ev = dict(scene='c2c', user_openid='offline-validation', group_openid='', key='smoke',
                  msg_id='never-send-this', attachments=[], ts=time.time())
        job = store.submit(ev, '仅验证本地任务执行链路：计算 6*7，把字符串 42 写入产物目录中的 smoke.txt，'
                           '最终正文回复“本地验证完成：42”。不要联网，不要发送消息，不要修改其他文件。',
                           120, directory)
        service = TaskService(store, cfg, lambda msg: print(msg, flush=True))
        print('Running real pi locally; QQ transport is not started.', flush=True)
        service.execute(store.claim())
        result = store.get(job['id'])
        print(json.dumps({key:result.get(key) for key in ['status','error','result']}, ensure_ascii=False), flush=True)
        if result['status'] != 'succeeded':
            for name in ('stderr.log', 'pi.stderr.log'):
                path = Path(result['directory']) / name
                if path.exists():
                    # Operational errors are local only; credentials are never printed by the bridge.
                    print(name + ': ' + path.read_text(errors='replace')[-1500:])
            return 1
        path = Path(result['directory']) / 'artifacts' / 'smoke.txt'
        assert path.read_text().strip() == '42', 'model did not create the expected artifact'
        assert '42' in result['result']
        print('PASS: real model + tool write + full result + artifact collection; 0 QQ sends.', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
