"""Disposable browser fixture: never controls the real bot or sends messages."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from akari_admin import make_server
from akari_runtime import Runtime
from akari_tasks import TaskStore

tmp=tempfile.TemporaryDirectory(prefix='akari-admin-browser-')
base=Path(tmp.name)
(base/'music').mkdir()
config=dict(bot_name='赤座灯里', model='Qwen3.8-27B', app_secret='fixture-secret-hidden', temperature=.85,
            llm_base='http://127.0.0.1:9',comfy_url='http://127.0.0.1:9',voice_url='http://127.0.0.1:9')
(base/'config.json').write_text(json.dumps(config))
(base/'music/config.json').write_text(json.dumps(dict(diffusion_steps=40,cuda_visible_devices='7',style='Japanese anime opening')))
store=TaskStore(base/'state')
with store.connect() as db:
    for i,(title,status) in enumerate([('《残酷天使的行动纲领》· 阿卡林翻唱','succeeded'),('生成一张春日樱花下的灯里','succeeded'),('检查模型服务连接','failed')]):
        job=dict(id='fixture'+str(i),status=status,prompt=title,created=time.time()-i*500,finished=time.time()-i*500+30,error='ConnectionError: service unavailable' if status=='failed' else '')
        db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',(job['id'],'test','test',str(i),status,job['created'],json.dumps(job)))
for uid,nick in [('Owner-001','测试管理员'),('Guest-002','<测试群友>')]:
    store.enqueue(dict(scene='group',user_openid=uid,group_openid='test-group',nick=nick,key=uid,msg_id=uid,ts=time.time()),dict(kind='text',text='fixture'))
(base/'state/bot.log').write_text('[09-15 12:00:00] READY session_id=hidden-session\n[09-15 12:01:00] fixture error fixture-secret-hidden\nTraceback (most recent call last):\n  sample frame\nValueError: browser-test\n[09-15 12:02:00] task completed\n')
server=make_server(base,0)
server.app.service=lambda:dict(active='active',sub='running',pid=os.getpid())
server.app.control=lambda action:dict(ok=True,message='测试服务操作已完成')
runtime=Runtime(base/'state',config)
runtime.set(gateway='ready',last_ack=time.time())
threading.Thread(target=runtime.run,args=(threading.Event(),),daemon=True).start()
Path('/tmp/akari-admin-browser.json').write_text(json.dumps(dict(port=server.server_address[1],base=str(base))))
print('browser fixture ready',flush=True)
server.serve_forever()
