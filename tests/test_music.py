import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from akari_commands import Commands
from akari_music import catalog_source, parse_request, settings
from akari_sing import align_and_mix, inputs, perform, probe, run_stage
from akari_tasks import TaskStore, scope_of
from akari_worker import TaskService
from test_tasks import event


class MusicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = TaskStore(self.root / 'state')
        self.music_config = self.root / 'music.json'
        shutil.copyfile(Path(__file__).resolve().parents[1] / 'music/config.example.json', self.music_config)
        self.cfg = dict(dry_run=False, agent_allowed_openids=['owner'], agent_workdir=str(self.root),
                        music_config=str(self.music_config))
        self.commands = Commands(self.db, self.cfg, lambda msg: None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_command_is_routed_in_chat_mode_and_preserves_lyrics(self):
        ev = dict(event(), content='/唱歌 /重编曲 /风格 "Japanese, rock"\n[Verse]\nあかり\n[Chorus]\nこんにちは',
                  attachments=[dict(filename='song.mp3')])
        self.assertTrue(self.commands.recognizes(ev))
        self.commands.handle(ev)
        job = self.db.get('最近', scope_of(ev))
        self.assertEqual(job['operation']['mode'], 'yue')
        self.assertEqual(job['operation']['lyrics'], '[Verse]\nあかり\n[Chorus]\nこんにちは')
        self.assertEqual(job['operation']['style'], 'Japanese, rock')
        self.assertEqual(job['timeout'], 3600)

    def test_dry_run_and_authorization_do_not_launch_singing(self):
        ev = dict(event(user='stranger'), content='/翻唱', attachments=[dict(filename='song.mp3')])
        self.commands.handle(ev)
        self.assertFalse(self.db.list())
        self.cfg['dry_run'] = True
        self.commands.handle(dict(ev, user_openid='owner'))
        self.assertFalse(self.db.list())

    def test_help_and_status_do_not_create_jobs(self):
        for content in ('/唱歌', '/唱歌 帮助', '/唱歌 状态'):
            self.commands.handle(dict(event(), content=content))
        self.assertFalse(self.db.list())

    def test_invalid_options_and_catalog_escape(self):
        for value in ('/片段 0', '/片段 121', '/片段 nan', '/风格', '/bad', '/风格 rock', '/重编曲 /片段 30'):
            with self.assertRaises(ValueError):
                parse_request(value)
        songs = self.root / 'songs'
        songs.mkdir()
        (songs / 'outside.mp3').symlink_to('/etc/passwd')
        for name in ('../secrets', '/etc/passwd', 'outside'):
            with self.assertRaises(ValueError):
                catalog_source(name, self.root)

    def prepare(self):
        task = self.root / 'task'
        (task / 'inputs').mkdir(parents=True)
        (task / 'artifacts').mkdir()
        source = task / 'inputs' / 'song.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=5',
                        '-ar', '44100', '-ac', '2', str(source)], check=True)
        (task / 'inputs' / 'index.json').write_text(json.dumps(dict(files=[str(source)], errors=[])))
        cfg = settings(self.music_config)
        cfg.pop('svc_checkpoint', None)
        cfg.pop('svc_model_config', None)
        cfg.update(root=str(self.root), reference=str(source), svc_python='/usr/bin/python3', svc_repo=str(self.root))
        (self.root / 'inference.py').touch()
        return task, cfg, source

    def test_failed_download_and_missing_lyrics_stop_before_models(self):
        task, cfg, source = self.prepare()
        with self.assertRaisesRegex(ValueError, '需要歌词'):
            inputs(parse_request('/重编曲'), task, cfg)
        index = task / 'inputs/index.json'
        index.write_text(json.dumps(dict(files=[str(source)], errors=['network failure'])))
        with self.assertRaisesRegex(ValueError, '未完整下载'):
            inputs(parse_request(''), task, cfg)

    def test_pipeline_real_audio_mix_with_stubbed_model_stages(self):
        task, cfg, source = self.prepare()
        called = []
        def fake_models(name, command, work, env, cwd=None):
            called.append(name)
            if name == '检查歌声转换 GPU 环境':
                return {}
            if name == '分离歌曲人声与伴奏':
                stems = work / 'stems/htdemucs/source'
                stems.mkdir(parents=True)
                for filename in ('vocals.wav', 'no_vocals.wav'):
                    shutil.copyfile(source, stems / filename)
                return {}
            if name == '转换为阿卡林歌声音色':
                self.assertEqual(command[command.index('--f0-condition') + 1], 'True')
                self.assertEqual(command[command.index('--auto-f0-adjust') + 1], 'False')
                converted = work / 'converted'
                converted.mkdir()
                shutil.copyfile(source, converted / 'voice.wav')
                return {}
            return run_stage(name, command, work, env, cwd)
        with patch('akari_sing.run_stage', side_effect=fake_models), patch('akari_sing.emit'):
            result = perform(parse_request(''), task, task / 'artifacts', cfg)
        self.assertIn('已生成', result)
        self.assertAlmostEqual(probe(task / 'artifacts/akari-cover.wav')['duration'], 5, places=2)
        self.assertTrue((task / 'artifacts/akari-cover.mp3').is_file())
        self.assertTrue((task / 'music-work/manifest.json').is_file())
        self.assertIn('转换为阿卡林歌声音色', called)

    def test_audio_duration_mismatch_rejected(self):
        task, cfg, source = self.prepare()
        with patch('akari_sing.probe', side_effect=[{'duration': 5}, {'duration': 2}]):
            with self.assertRaisesRegex(ValueError, '时长不一致'):
                align_and_mix(source, source, task, os.environ)

    def test_yue_renders_score_then_converts_generated_song(self):
        task, cfg, source = self.prepare()
        cfg.update(yue_python='/usr/bin/python3')
        score = task / 'inputs' / 'melody.abc'
        score.write_text('X:1\nM:4/4\nK:C\nC D E F|')
        (task / 'inputs/index.json').write_text(json.dumps(dict(files=[str(source), str(score)], errors=[])))
        def models(name, command, work, env, cwd=None):
            if name == '检查歌声转换 GPU 环境':
                return {}
            if name == 'YuE2 按旋律重新编曲演唱':
                request = json.loads((work / 'yue-request.json').read_text())
                self.assertEqual(request['cot'], 'melody')
                self.assertEqual(request['abc'], score.read_text())
                (work / 'yue').mkdir()
                run_stage('fixture', ['ffmpeg', '-v', 'error', '-i', source, work / 'yue/audio.flac'], work, env)
                return {}
            if name == '分离歌曲人声与伴奏':
                self.assertEqual(command[-1], work / 'yue/audio.flac')
                stems = work / 'stems/htdemucs/audio'
                stems.mkdir(parents=True)
                for filename in ('vocals.wav', 'no_vocals.wav'):
                    shutil.copyfile(source, stems / filename)
                return {}
            if name == '转换为阿卡林歌声音色':
                self.assertEqual(command[command.index('--source') + 1], work / 'stems/htdemucs/audio/vocals.wav')
                (work / 'converted').mkdir()
                shutil.copyfile(source, work / 'converted/voice.wav')
                return {}
            return run_stage(name, command, work, env, cwd)
        with patch('akari_sing.run_stage', side_effect=models), patch('akari_sing.emit'):
            result = perform(parse_request('/重编曲\n[Verse]\nTest lyrics'), task, task / 'artifacts', cfg)
        self.assertIn('YuE2', result)
        self.assertTrue((task / 'artifacts/melody.abc').is_file())

    def test_worker_uses_singing_runner_and_records_actionable_error(self):
        job = self.db.submit(dict(event(), content='/唱歌'), '阿卡林翻唱', 30, str(self.root), operation=parse_request(''))
        svc = TaskService(self.db, self.cfg, lambda msg: None)
        svc.execute(self.db.claim())
        updated = self.db.get(job['id'])
        self.assertEqual(updated['status'], 'failed')
        self.assertIn('缺少歌曲音频', updated['result'])
        self.assertFalse(updated['artifacts'])


if __name__ == '__main__':
    unittest.main()
