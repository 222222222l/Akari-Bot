import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock
from akari_commands import Commands
from akari_tasks import TaskStore
from akari_worker import TaskService
import akari_role_tasks as role
import akari_role_results as results
from test_tasks import event

class RoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.db=TaskStore(self.root)
        self.cfg=dict(agent_enabled=True,role_task_dispatch=True,role_task_results=True,music_enabled=True,
                      agent_allowed_openids=['owner'],agent_workdir=str(self.root),dry_run=False)
        self.cmd=Commands(self.db,self.cfg,lambda x:None)
        self.ev=dict(event(),content='帮我唱刚才那首，保留日语，先做30秒')
    def tearDown(self):self.tmp.cleanup()
    def submit(self,**kw):return role.invoke(dict(action='submit',request='翻唱恋爱循环，日语30秒',kind='music',**kw),self.ev,self.cmd)
    def test_dispatch_idempotency_and_exact_input_preserved(self):
        self.ev['content']='/任务 /超时90 分析\n@input.py\n不要改源码'
        a=self.submit();b=self.submit()
        self.assertEqual(a['id'],b['id']);job=self.db.get(a['id'])
        self.assertIn(self.ev['content'],job['prompt']);self.assertEqual(job['timeout'],90)
        self.assertEqual(len(self.db.list()),1)
    def test_permission_dry_run_and_feature_gates(self):
        for changes in ({'agent_enabled':False},{'role_task_dispatch':False},{'music_enabled':False},{'agent_allowed_openids':[]}):
            old=self.cfg.copy();self.cfg.update(changes)
            with self.assertRaises(ValueError):self.submit()
            self.cfg.clear();self.cfg.update(old)
        self.cfg['dry_run']=True;self.assertFalse(self.submit()['ok']);self.assertEqual(self.db.list(),[])
    def test_scope_lookup_cannot_read_other_users_task(self):
        a=self.submit();stranger=Commands(self.db,dict(self.cfg,agent_allowed_openids=['other']),lambda x:None)
        with self.assertRaises(ValueError):role.invoke(dict(action='status',task_id=a['id']),dict(event(user='other')),stranger)
    def test_role_receives_tool_receipt_before_accepting(self):
        call=dict(id='call1',type='function',function=dict(name='akari_task',arguments=json.dumps(dict(action='submit',kind='music',request='恋爱循环日语30秒'))))
        complete=Mock(side_effect=[{'tool_calls':[call]},'嗯！灯里去准备这首歌啦。'])
        text,job=role.respond([],self.ev,self.cmd,complete)
        self.assertIn('灯里',text);self.assertTrue(job['ok'])
        self.assertEqual(complete.call_args.args[0][-1]['role'],'tool')
        self.assertEqual(len(self.db.list()),1)
    def test_chat_does_not_queue_and_model_failure_after_submit_keeps_receipt(self):
        text,job=role.respond([],self.ev,self.cmd,lambda *a:'会唱哦，你想听什么呢？')
        self.assertIsNone(job);self.assertEqual(self.db.list(),[])
        call=dict(id='c',function=dict(name='akari_task',arguments=json.dumps(dict(action='submit',request='测试'))))
        text,job=role.respond([],self.ev,self.cmd,Mock(side_effect=[{'tool_calls':[call]},'']))
        self.assertTrue(job['ok']);self.assertIn('安排',text)
    def test_queued_task_ack_cannot_claim_file_already_written(self):
        call=dict(id='c',function=dict(name='akari_task',arguments=json.dumps(dict(action='submit',request='写 result.txt'))))
        text,job=role.respond([],self.ev,self.cmd,Mock(side_effect=[{'tool_calls':[call]},'灯里已经把结果写进 result.txt 啦～']))
        self.assertNotIn('写进',text);self.assertIn('安排',text)
        self.assertEqual(job['status'],'queued')

    def test_unauthorized_pseudo_tool_and_waiting_claim_are_rejected(self):
        self.ev['user_openid']='not-authorized'
        for text in ('我去准备翻唱啦，等唱好了就拿给你听。akari_task: submit', '后台说唱好了会自己发过来，我还在等呢'):
            reply,job=role.respond([],self.ev,self.cmd,lambda *args:text)
            self.assertIsNone(job);self.assertIn('没有任务权限',reply)
            self.assertNotIn('akari_task',reply)
        self.assertEqual(self.db.list(),[])

    def test_authorized_pseudo_tool_is_repaired_with_native_call(self):
        call=dict(id='c',function=dict(name='akari_task',arguments=json.dumps(dict(action='submit',request='翻唱恋爱循环',kind='music'))))
        model=Mock(side_effect=['akari_task: submit, kind=music',{'tool_calls':[call]},'灯里已经安排好啦。'])
        text,job=role.respond([],self.ev,self.cmd,model)
        self.assertTrue(job['ok']);self.assertEqual(len(self.db.list()),1)
        self.assertEqual(model.call_count,3)

    def test_unauthorized_native_call_cannot_become_a_false_acceptance(self):
        self.ev['user_openid']='not-authorized'
        call=dict(id='c',function=dict(name='akari_task',arguments=json.dumps(dict(action='submit',request='翻唱'))))
        text,job=role.respond([],self.ev,self.cmd,Mock(side_effect=[{'tool_calls':[call]},'好的交给我吧！']))
        self.assertIn('没有任务权限',text);self.assertEqual(self.db.list(),[])

    def test_routing_preserves_fast_controls_and_chat_escape(self):
        for text in ('/任务 分析文件','/音乐 写首歌','/翻唱 恋爱循环','就按刚才说的做'):
            self.assertTrue(role.route(dict(self.ev,content=text),self.cmd),text)
        for text in ('/取消','/进度','/文件','/工作 开','/聊 唱首歌','/唱歌 状态','/唱歌 帮助'):
            self.assertFalse(role.route(dict(self.ev,content=text),self.cmd),text)
        with self.assertRaises(ValueError):role.invoke(dict(action='submit',request='x'),dict(self.ev,role_chat_only=True),self.cmd)
    def test_full_result_preserved_but_published_once_as_short_role_receipt(self):
        a=self.submit();job=self.db.get(a['id']);service=TaskService(self.db,self.cfg,lambda x:None)
        service.result_presenter=Mock(return_value='唱好啦！这次是日语的现场版，你听听看哦。')
        report='详细分析\n'*1000
        service.finish(job,'succeeded',report)
        service.publish(self.db.get(job['id']))
        self.assertEqual(self.db.get(job['id'])['result'],report)
        with self.db.connect() as db:rows=db.execute("SELECT payload FROM outbox WHERE job_id=?",(job['id'],)).fetchall()
        self.assertEqual(len(rows),1);self.assertLess(len(json.loads(rows[0][0])['text']),300)
        self.assertEqual(service.result_presenter.call_count,1)
    def test_result_model_failure_and_audio_format_dedup(self):
        a=self.submit();job=self.db.get(a['id']);service=TaskService(self.db,self.cfg,lambda x:None)
        service.result_presenter=Mock(side_effect=RuntimeError('offline'))
        service.finish(job,'failed','重复原始错误'*100,'下载失败')
        self.assertIn('没能完成',self.db.get(job['id'])['role_summary'])
        self.assertNotIn('发过去',results.clean('文件也发过去了，收一下～',dict(job,status='succeeded')))
        self.assertIn('没能完成',results.clean('已经完成啦！',dict(job,status='failed')))
        files=[{'path':'/tmp/a.mp3'},{'path':'/tmp/a.wav'},{'path':'/tmp/lyrics.txt'}]
        self.assertEqual(results.auto_files({'artifacts':files},2),[files[0],files[2]])
