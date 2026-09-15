#!/usr/bin/env python3
"""Opt-in actual Pi + music execution, without starting a QQ sender."""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from akari_tasks import TaskStore
from akari_worker import TaskService

parser=argparse.ArgumentParser()
parser.add_argument('--request',required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--timeout',type=int,default=1800)
args=parser.parse_args()
base=Path(__file__).resolve().parents[1]
cfg=json.loads((base/'config.json').read_text())
cfg.update(dry_run=False,music_enabled=True,music_config=str(base/'music/config.json'),agent_auto_artifacts=0,agent_progress_secs=0)
args.output.mkdir(parents=True,exist_ok=True)
store=TaskStore(args.output.resolve())
ev=dict(scene='c2c',user_openid='local-music-validation',group_openid='',key=str(time.time_ns()),msg_id='no-qq-send',attachments=[],ts=time.time())
job=store.submit(ev,args.request+'\n这是本地真实验证，不启动 QQ 发送器。必须使用原生 akari_music 工具实际执行；不能用 bash 自写替代模型或生成静音/正弦波。完成后核对工具返回路径即可，不需要复制同一份产物。',args.timeout,str(args.output.resolve()),operation=dict(kind='music_agent'))
print('task='+job['id'],flush=True)
TaskService(store,cfg,lambda x:print(x,flush=True)).execute(store.claim())
job=store.get(job['id'])
summary={k:job.get(k) for k in ('id','status','result','error','artifacts')}
(args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
sys.exit(0 if job['status']=='succeeded' else 1)
