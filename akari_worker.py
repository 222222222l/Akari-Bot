"""Bounded task workers with stream progress and process-group cancellation."""
import fcntl
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from akari_tasks import LABELS, TERMINAL, atomic_text, split_full
from akari_delivery import media_type


def process_stamp(pid):
    try:
        # Linux starttime disambiguates PID reuse during recovery.
        return Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def kill_group(pid):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        if sig == signal.SIGTERM:
            time.sleep(0.3)


def artifact_key(job, path):
    return job['id'] + ':artifact:' + hashlib.sha256(str(path).encode()).hexdigest()[:20]


class EventReader:
    def __init__(self, path, on_event):
        self.file = Path(path).open('rb')
        self.buffer = b''
        self.on_event = on_event
        self.plain = []
        self.json_seen = False

    def read(self, final=False):
        self.buffer += self.file.read()
        lines = self.buffer.split(b'\n')
        self.buffer = lines.pop()
        if final and self.buffer:
            lines.append(self.buffer)
            self.buffer = b''
        for line in lines:
            text = line.decode('utf-8', 'replace')
            try:
                event = json.loads(text)
                if not isinstance(event, dict) or 'type' not in event:
                    raise ValueError()
            except ValueError:
                if not text.startswith('Warning: '):
                    self.plain.append(text)
                continue
            self.json_seen = True
            self.on_event(event)

    def close(self):
        self.file.close()


class TaskService:
    def __init__(self, store, cfg, log):
        self.store, self.cfg, self.log = store, cfg, log
        self.stop = threading.Event()
        self.threads = []
        self.lock_file = None

    def start(self):
        self.lock_file = (self.store.root / 'task-worker.lock').open('a')
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            self.lock_file = None
            raise RuntimeError('另一个阿卡林任务执行器已经运行')
        self.recover()
        for i in range(max(1, min(8, int(self.cfg.get('agent_workers', 2))))):
            thread = threading.Thread(target=self.loop, name='task-worker-%s' % i, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=3)
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    def recover(self):
        self.store.recover_outbox()
        for job in self.store.list(limit=100000):
            if job['status'] == 'running':
                pid = job.get('pid')
                if pid and job.get('process_stamp') and process_stamp(pid) == job['process_stamp']:
                    kill_group(pid)
                directory = Path(job['directory'])
                result = (directory / 'partial.txt').read_text(errors='replace') if (directory / 'partial.txt').exists() else ''
                self.finish(job, 'interrupted', result, '服务重启导致任务中断；可用 /继续 %s 继续。' % job['id'])
            elif job['status'] in TERMINAL:
                # Covers a crash between final state commit and outbox insertion.
                self.publish(job)

    def loop(self):
        while not self.stop.is_set():
            job = None
            try:
                if not self.cfg.get('dry_run', True):
                    job = self.store.claim()
                if job:
                    self.execute(job)
            except Exception as exc:
                self.log('task worker error %r' % (exc,))
                if job:
                    self.finish(job, 'failed', '', '执行器异常：' + type(exc).__name__)
            if not job:
                self.stop.wait(0.3)

    def input_files(self, job):
        directory = Path(job['directory']) / 'inputs'
        saved, errors = [], []
        attachments = job['event'].get('attachments') or []
        cap = int(self.cfg.get('agent_attachment_max_bytes', 20 * 1024 * 1024))
        if len(attachments) > 10:
            errors.append('本次最多处理前 10 个附件，其余请分开发送。')
        for i, item in enumerate(attachments[:10], 1):
            if self.stop.is_set() or self.store.get(job['id'])['cancel_requested']:
                break
            url = item.get('url') or item.get('proxy_url') or ''
            if url.startswith('//'):
                url = 'https:' + url
            if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
                errors.append('附件 %s 没有可下载地址' % i)
                continue
            name = Path(item.get('filename') or urllib.parse.unquote(urllib.parse.urlsplit(url).path).split('/')[-1] or 'attachment').name
            name = re.sub(r'[^\w.\-]', '_', name).encode('utf-8')[:180].decode('utf-8', 'ignore') or 'attachment'
            if not Path(name).suffix:
                name += mimetypes.guess_extension(item.get('content_type', '')) or ''
            path = directory / ('%02d-' % i + name)
            try:
                if int(item.get('size') or 0) > cap:
                    raise ValueError('附件超过大小上限')
                # QQ attachment URLs carry their own access signature. Never forward bot secrets to a CDN.
                req = urllib.request.Request(url, headers={'User-Agent': 'Akari/2'})
                with urllib.request.urlopen(req, timeout=10) as res, path.open('wb') as out:
                    total = 0
                    while True:
                        block = res.read(65536)
                        if not block:
                            break
                        total += len(block)
                        if total > cap:
                            raise ValueError('附件超过大小上限')
                        out.write(block)
                        if self.stop.is_set() or self.store.get(job['id'])['cancel_requested']:
                            raise InterruptedError('附件下载被取消')
                saved.append(str(path))
            except Exception as exc:
                path.unlink(missing_ok=True)
                errors.append('附件 %s 下载失败：%s' % (i, type(exc).__name__))
        atomic_text(directory / 'index.json', json.dumps({'files': saved, 'errors': errors}, ensure_ascii=False, indent=2))
        return saved, errors

    def execute(self, job):
        directory = Path(job['directory'])
        proc = None
        reader = feedback = None
        result, delta, stream_error, progress = '', '', '', '准备执行'
        status, error = 'failed', ''
        start = time.monotonic()
        last_partial, last_persist = '', 0.0
        next_notice = float(self.cfg.get('agent_progress_secs', 60))
        notice_sent = False

        def on_event(event):
            nonlocal result, delta, stream_error, progress
            kind = event.get('type')
            if kind == 'message_start' and (event.get('message') or {}).get('role') == 'assistant':
                delta = ''
            elif kind == 'message_update':
                part = event.get('assistantMessageEvent') or {}
                if part.get('type') == 'text_delta':
                    delta += part.get('delta', '')
            elif kind == 'message_end':
                msg = event.get('message') or {}
                if msg.get('role') == 'assistant':
                    text = '\n'.join(x.get('text', '') for x in msg.get('content', []) if x.get('type') == 'text')
                    if text:
                        result = text
                        atomic_text(directory / 'partial.txt', result)
                    stream_error = str(msg.get('errorMessage') or msg.get('stopReason')) if msg.get('stopReason') in ('error', 'aborted') else ''
            elif kind in ('tool_execution_start', 'tool_execution_end'):
                progress = ('正在执行：' if kind.endswith('start') else '已返回：') + str(event.get('toolName', '工具'))
                if event.get('isError'):
                    progress += '（工具报错，等待处理）'
                self.store.update(job['id'], progress=progress)
                with (directory / 'progress.log').open('a', encoding='utf-8') as timeline:
                    timeline.write(time.strftime('%H:%M:%S ') + progress + '\n')
            elif kind == 'akari_progress':
                progress = str(event.get('text', ''))[:500]
                self.store.update(job['id'], progress=progress)
                with (directory / 'progress.log').open('a', encoding='utf-8') as timeline:
                    timeline.write(time.strftime('%H:%M:%S ') + progress + '\n')
            elif kind == 'akari_artifact':
                # The helper copies finished files into the per-task artifact directory.
                progress = '已生成产物：' + Path(str(event.get('path', ''))).name
                artifacts = self.artifacts(job)
                self.store.update(job['id'], progress=progress, artifacts=artifacts)
                for artifact in artifacts:
                    if artifact['path'] == event.get('path'):
                        ev = self.store.session(job['scope']).get('event') or job['event']
                        self.store.enqueue(ev, dict(kind='media', **artifact), job['id'], artifact_key(job, artifact['path']))

        try:
            if self.cfg.get('dry_run', True):
                status, error = 'cancelled', 'dry_run：未执行任务'
                return
            files, input_errors = self.input_files(job)
            helper = Path(__file__).with_name('akari_report.py')
            instruction = ('\n\n【QQ 任务交付约定】\n'
                '这是一个由 QQ 用户下达的实际工作任务。使用你已有的工具完成并核实结果。'
                '正文完整回答，可包含代码、表格和链接，不受聊天人设的字数限制。'
                '不要只承诺稍后处理后就结束；无法完成时说明已做、失败原因和可继续的步骤。\n'
                '产物目录：' + str(directory / 'artifacts') + '\n'
                '把需要交付的图片、视频、音频、PDF、CSV、代码、HTML 等文件保存到产物目录。'
                '仅放需要发给用户的最终产物，不放密钥或内部临时日志。\n'
                '长步骤可运行：python3 ' + str(helper) + ' progress "已完成的进展"\n'
                '要交付其他目录中的文件，可运行：python3 ' + str(helper) + ' file /绝对路径/文件\n'
                '该 helper 只登记交付，由 bot 统一发送；最终答案不得声称 QQ 已收到。\n'
                '大型工具输出会节选并给出完整文件路径；每轮仅保留最近两张图片，需要旧图时重新读取。'
                '视频下载先用 ffprobe 核实并合并音视频轨道，尽早保存可播放产物；不要反复截图耗尽上下文。'
                '下载原始轨道和临时数据请放当前任务目录内，以便中断后恢复。'
                'QQ 已接入分片上传；不需要为了旧的 20 MiB 本地限制先压缩。'
                '发送超限时用户可用 /适配 任务号 文件序号 每段MiB 分段，或 /下载 获取已配置站点的下载链接。\n'
                '本条 QQ 附件：' + json.dumps(files, ensure_ascii=False) + '\n'
                '附件读取情况：' + json.dumps(input_errors, ensure_ascii=False))
            if self.cfg.get('music_enabled', False):
                instruction += ('\n【音乐能力】已加载原生 akari_music 工具和默认阿卡林参考音色。'
                    '歌曲创作无需用户提供源音频：自行写原创歌词、风格、种子并调用 generate。'
                    '翻唱先核对本条输入文件或本地曲库，找不到时用 search 搜索公开来源，核对歌名/歌手后 cover。'
                    '不得把歌曲名称、网页描述当作已下载音频；不得只给方案或要求用户手填可自行决定的参数。'
                    '下载失败可换候选，不能绕过付费/登录/DRM；生成失败如实说明。最终音频由工具登记，勿重复复制登记同一文件。\n')
            prompt = job['prompt'] + instruction
            if job.get('parent'):
                parent = self.store.get(job['parent'], job['scope'])
                if parent:
                    prompt += '\n前次任务：' + parent['prompt'] + '\n前次状态：' + parent['status']
                    prompt += '\n前次结果目录：' + parent['directory']
                    prompt += '\n先核对已有结果和已执行的操作，再继续；不要盲目重复有外部影响的操作。'
            atomic_text(directory / 'execution.txt', prompt)
            env = dict(os.environ, AKARI_TASK_DIR=str(directory), AKARI_OUTPUT_DIR=str(directory / 'artifacts'),
                       AKARI_AGENT_FORMAT='json', AKARI_SESSION_DIR=str(self.store.root / 'pi-sessions'))
            env['AKARI_MUSIC_ONLY'] = '1' if (job.get('operation') or {}).get('kind') == 'music_agent' else '0'
            env['AKARI_MUSIC_ENABLED'] = '1' if self.cfg.get('music_enabled', False) else '0'
            env['AKARI_MUSIC_CONFIG'] = str(self.cfg.get('music_config', Path(__file__).parent / 'music/config.json'))
            if self.cfg.get('llm_key_file'):
                env['AKARI_AGENT_KEY_FILE'] = self.cfg['llm_key_file']
            if self.cfg.get('agent_base'):
                env['AKARI_AGENT_BASE'] = self.cfg['agent_base']
            for key, value in (('agent_provider', 'AKARI_AGENT_PROVIDER'), ('agent_model', 'AKARI_AGENT_MODEL')):
                if self.cfg.get(key):
                    env[value] = self.cfg[key]
            for key, value in (('agent_max_output_tokens', 'AKARI_MAX_OUTPUT_TOKENS'), ('agent_context_images', 'AKARI_CONTEXT_IMAGES'),
                               ('agent_download_root', 'AKARI_DOWNLOAD_ROOT'), ('agent_download_base_url', 'AKARI_DOWNLOAD_BASE_URL')):
                if key in self.cfg:
                    env[value] = str(self.cfg[key])
            command = ['bash', self.cfg.get('agent_script', str(Path(__file__).with_name('agent_run.sh'))),
                       str(directory / 'execution.txt'), job['session_id'], str(job['timeout'])]
            if job.get('operation') and job['operation'].get('kind') != 'music_agent':
                if job['operation'].get('kind') == 'sing':
                    env['AKARI_MUSIC_CONFIG'] = str(self.cfg.get('music_config', Path(__file__).parent / 'music' / 'config.json'))
                    command = ['/usr/bin/python3', str(Path(__file__).with_name('akari_sing.py')), json.dumps(job['operation'])]
                else:
                    command = ['/usr/bin/python3', str(Path(__file__).with_name('akari_adapt.py')), json.dumps(job['operation'])]
            if self.store.get(job['id'])['cancel_requested'] or self.stop.is_set():
                status = 'cancelled' if not self.stop.is_set() else 'interrupted'
                return
            with (directory / 'events.jsonl').open('wb') as stdout, (directory / 'stderr.log').open('wb') as stderr:
                proc = subprocess.Popen(command, cwd=job['cwd'], env=env, stdin=subprocess.DEVNULL,
                                        stdout=stdout, stderr=stderr, start_new_session=True)
                self.store.update(job['id'], pid=proc.pid, process_stamp=process_stamp(proc.pid), progress='任务已启动')
                (directory / 'feedback.jsonl').touch()
                reader = EventReader(directory / 'events.jsonl', on_event)
                feedback = EventReader(directory / 'feedback.jsonl', on_event)
                while True:
                    reader.read()
                    feedback.read()
                    elapsed = time.monotonic() - start
                    current = self.store.get(job['id'])
                    if self.stop.is_set():
                        status, error = 'interrupted', '服务停止，任务已中断；可用 /继续 恢复。'
                        break
                    if current['cancel_requested']:
                        status, error = 'cancelled', '用户取消了任务。'
                        break
                    if elapsed >= job['timeout']:
                        status, error = 'timed_out', '达到执行时限，部分结果已保存。'
                        break
                    if (directory / 'events.jsonl').stat().st_size > int(self.cfg.get('agent_log_max_bytes', 64 * 1024 * 1024)):
                        status, error = 'failed', '执行输出超过日志大小上限，部分结果已保存。'
                        break
                    if proc.poll() is not None:
                        reader.read(final=True)
                        feedback.read(final=True)
                        if not reader.json_seen:
                            result = '\n'.join(reader.plain)
                        status = 'succeeded' if proc.returncode == 0 and not stream_error else 'failed'
                        if proc.returncode in (124, 137, -15) and elapsed >= job['timeout'] - 1:
                            status = 'timed_out'
                        if status != 'succeeded':
                            error = '执行退出码 %s%s' % (proc.returncode, '；' + stream_error[:500] if stream_error else '')
                        break
                    if delta and delta != last_partial and elapsed - last_persist >= 1:
                        atomic_text(directory / 'partial.txt', delta)
                        last_partial, last_persist = delta, elapsed
                    if next_notice > 0 and elapsed >= next_notice and not notice_sent:
                        self.store.enqueue(job['event'], dict(kind='text', text='【任务 %s】已运行 %d 秒，%s。可用 /进度 %s 查询。' %
                            (job['id'], elapsed, current['progress'], job['id'])), job['id'], job['id'] + ':progress')
                        notice_sent = True
                    self.stop.wait(0.3)
        except Exception as exc:
            error = '执行异常：' + type(exc).__name__ + ' ' + str(exc)[:300]
            status = 'failed'
            self.log('task failed id=%s %r' % (job['id'], exc))
        finally:
            if proc:
                kill_group(proc.pid)
                proc.wait(timeout=3)
            if reader:
                reader.read(final=True)
                if not reader.json_seen:
                    result = '\n'.join(reader.plain)
                reader.close()
            if feedback:
                feedback.read(final=True)
                feedback.close()
            if status != 'succeeded' and delta and delta != result:
                result = (result + '\n\n【中断时的部分输出】\n' + delta).lstrip()
            self.finish(job, status, result or delta, error)

    def artifacts(self, job):
        root = Path(job['directory']) / 'artifacts'
        files = []
        for path in sorted(root.rglob('*')):
            if path.name.startswith('.') or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
                continue
            files.append(dict(path=str(path), root=str(root), name=str(path.relative_to(root)), size=path.stat().st_size, file_type=media_type(path)))
            if len(files) >= int(self.cfg.get('agent_max_artifacts', 200)):
                break
        # A file announced during execution keeps its QQ index when later files
        # sort before it. Otherwise /文件 task 1 could return a different file.
        latest = self.store.get(job['id']) or job
        known = latest.get('artifacts', [])
        by_path = {a['path']: a for a in files}
        ordered = [by_path.pop(a['path'], a) for a in known]
        ordered.extend(by_path.values())
        return ordered

    def finish(self, job, status, result, error=''):
        if not result:
            result = '（没有生成正文；请查看任务状态和附件。）'
        atomic_text(Path(job['directory']) / 'result.md', result)
        artifacts = self.artifacts(job)
        job = self.store.update(job['id'], status=status, finished=time.time(), result=result,
                                error=error, artifacts=artifacts, progress=LABELS[status])
        self.publish(job)

    def publish(self, job):
        ev = self.store.session(job['scope']).get('event') or job['event']
        pages = split_full(job.get('result') or '（没有正文）', int(self.cfg.get('agent_page_chars', 700)))
        header = '【任务 %s · %s】\n' % (job['id'], LABELS[job['status']])
        if job.get('error'):
            header += job['error'][:300] + '\n'
        auto = max(1, int(self.cfg.get('agent_auto_result_pages', 1)))
        for i, page in enumerate(pages[:auto], 1):
            text = (header if i == 1 else '') + page
            if len(pages) > auto and i == auto:
                text += '\n正文共 %d 页，继续查看：/结果 %s %d' % (len(pages), job['id'], auto + 1)
            if i == 1:
                text += '\n附件 %d 个：/文件 %s；全文文件：/全文 %s' % (len(job.get('artifacts', [])), job['id'], job['id'])
            self.store.enqueue(ev, dict(kind='text', text=text), job['id'], job['id'] + ':result:' + str(i))
        for i, artifact in enumerate(job.get('artifacts', [])[:int(self.cfg.get('agent_auto_artifacts', 2))], 1):
            self.store.enqueue(ev, dict(kind='media', **artifact), job['id'], artifact_key(job, artifact['path']))
