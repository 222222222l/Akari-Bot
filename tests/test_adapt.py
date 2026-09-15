import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from akari_adapt import split_video, probe, share_file
from akari_commands import Commands
from akari_tasks import TaskStore
from akari_worker import TaskService
from test_tasks import event


class AdaptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):self.tmp.cleanup()

    @unittest.skipUnless(shutil.which('ffmpeg'),'ffmpeg not installed')
    def test_real_video_parts_are_playable_under_budget_and_original_is_preserved(self):
        source = self.root/'original.mp4'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc2=size=320x240:rate=25',
            '-t','6','-c:v','libx264','-threads','2','-preset','ultrafast','-g','150','-crf','12',str(source)],check=True)
        before = source.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            files,transcoded = split_video(source,self.root/'artifacts',100000)
        self.assertGreater(len(files),1)
        self.assertTrue(transcoded)
        self.assertTrue(all(0<p.stat().st_size<=100000 for p in files))
        self.assertAlmostEqual(sum(probe(p) for p in files),6,delta=0.7)
        self.assertEqual(source.read_bytes(),before)

    def test_share_uses_unguessable_filename_and_copies_exact_original(self):
        source = self.root/'private file.csv';source.write_bytes(b'a,b\n1,2\n')
        public = self.root/'public';public.mkdir()
        url = share_file(source,public,'https://download.invalid/files')
        self.assertTrue(url.startswith('https://download.invalid/files/'))
        exported = public/url.rsplit('/',1)[1]
        self.assertEqual(exported.read_bytes(),source.read_bytes())
        self.assertEqual(len(exported.stem),32)

    def test_adapt_queues_deterministic_job_and_missing_download_site_does_not_fake_url(self):
        db = TaskStore(self.root/'state')
        cfg = dict(dry_run=False,agent_allowed_openids=['owner'],agent_workdir=str(self.root))
        cmd = Commands(db,cfg,lambda _:None)
        ev = dict(event(),ts=time.time())
        job = db.submit(ev,'video',30,str(self.root))
        source = Path(job['directory'])/'artifacts'/'video.mp4';source.write_bytes(b'fake')
        db.update(job['id'],status='succeeded',artifacts=[dict(path=str(source),name=source.name,size=4)])
        cmd.handle(dict(ev,msg_id='adapt',key='adapt',content='/适配 %s 1 20'%job['id']))
        adapted = db.get('最近',job['scope'])
        self.assertEqual(adapted['operation']['kind'],'adapt')
        self.assertEqual(adapted['operation']['max_bytes'],20*1048576)
        cmd.handle(dict(ev,msg_id='download',key='download',content='/下载 %s 1'%job['id']))
        with db.connect() as connection:
            payload = json.loads(connection.execute('SELECT payload FROM outbox ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertIn('尚未配置',payload['text'])
        self.assertNotIn('https://',payload['text'])

    def test_share_job_runs_through_worker_without_calling_model(self):
        db = TaskStore(self.root/'state')
        cfg = dict(dry_run=False,agent_script='/nonexistent/no-model-allowed',agent_download_root=str(self.root/'public'),
                   agent_download_base_url='https://download.invalid/files')
        (self.root/'public').mkdir()
        source = self.root/'source.csv';source.write_text('value\n42\n')
        job = db.submit(dict(event(),ts=time.time()),'download',10,str(self.root),
            operation=dict(kind='share',source=str(source),root=str(self.root)))
        service = TaskService(db,cfg,lambda _:None)
        service.execute(db.claim())
        result = db.get(job['id'])
        self.assertEqual(result['status'],'succeeded',result.get('error'))
        self.assertIn('https://download.invalid/files/',result['result'])


if __name__ == '__main__':unittest.main()
