#!/usr/bin/env python3
"""Opt-in real role-model intent/receipt checks; isolated queue, zero QQ sends."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import akari_role_tasks as role
import akari_role_results as results
from akari_tasks import TaskStore
from akari_commands import Commands


def main():
    base=Path(__file__).resolve().parents[1]
    cfg=json.loads((base/'config.json').read_text())
    cfg.update(agent_enabled=True,role_task_dispatch=True,role_task_results=True,music_enabled=True,
               agent_allowed_openids=['local-role-test'],agent_allow_all=False,dry_run=False)
    with tempfile.TemporaryDirectory(prefix='akari-role-') as tmp:
        root=Path(tmp);cfg['agent_workdir']=tmp
        (root/'config.json').write_text(json.dumps(cfg));(root/'config.json').chmod(0o600)
        (root/'persona.txt').write_text((base/'persona.txt').read_text())
        old=os.environ.get('AKARI_BASE');os.environ['AKARI_BASE']=tmp
        try:
            spec=importlib.util.spec_from_file_location('role_test_bot',base/'akari_bot.py')
            bot=importlib.util.module_from_spec(spec);spec.loader.exec_module(bot)
        finally:
            if old is None:os.environ.pop('AKARI_BASE',None)
            else:os.environ['AKARI_BASE']=old
        cases=[('阿卡林，你会唱歌吗？',None,[]),
               ('阿卡林，帮我翻唱花泽香菜的恋爱循环，日语，先听30秒。','music',[]),
               ('就唱刚才那首吧，先来30秒。','music',[{'role':'user','content':'想听花泽香菜的恋爱循环。'},{'role':'assistant','content':'好呀，你想先听一小段还是整首呢？'}]),
               ('先不要执行，我只是想了解怎么整理 CSV 文件。',None,[]),
               ('灯里，计算6乘7，把结果写成 result.txt 给我。','task',[])]
        evidence=[]
        for n,(text,expected,history) in enumerate(cases):
            ev=dict(scene='c2c',user_openid='local-role-test',group_openid='',key=str(n),msg_id='no-send',attachments=[],content=text,ts=time.time())
            msgs=[{'role':'system','content':bot.system_prompt()+role.prompt(ev,bot.COMMANDS)}]+history+[{'role':'user','content':text}]
            reply,job=role.respond(msgs,ev,bot.COMMANDS,bot.call_llm)
            actual=job.get('kind') if job else None
            evidence.append(dict(request=text,expected=expected,actual=actual,reply=reply))
            print(json.dumps(evidence[-1],ensure_ascii=False),flush=True)
            assert actual==expected,(text,actual,expected)
        # Run exactly the harmless file task, not queued music requests.
        target=next(j for j in bot.TASK_STORE.list() if 'result.txt' in j['prompt'])
        bot.TASK_STORE.update(target['id'],status='running',started=time.time())
        bot.TASK_SERVICE.execute(bot.TASK_STORE.get(target['id']))
        finished=bot.TASK_STORE.get(target['id'])
        assert finished['status']=='succeeded',finished.get('error')
        artifacts=list((Path(finished['directory'])/'artifacts').rglob('result.txt'))
        assert artifacts and artifacts[0].read_text().strip()=='42'
        assert finished.get('role_summary') and len(finished['role_summary'])<=220
        assert '发过去了' not in finished['role_summary']
        print(json.dumps(dict(execution=finished['status'],summary=finished['role_summary'],result=finished['result']),ensure_ascii=False),flush=True)
        failed=dict(finished,status='failed',result='ERROR: Sign in to confirm you are not a bot. '*10,error='YouTube 要求登录验证，备用来源也不可用',artifacts=[])
        short=bot.render_role_result(failed)
        assert len(short)<=220
        print(json.dumps(dict(failure_summary=short,qq_sends=0),ensure_ascii=False),flush=True)
    return 0
if __name__=='__main__':sys.exit(main())
