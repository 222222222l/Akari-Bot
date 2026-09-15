"""Fast QQ task controls; never waits for a model or file upload."""
from pathlib import Path
import re
import time

from akari_tasks import LABELS, TERMINAL, scope_of, split_full
import akari_music

HELP = '''【QQ 工作台】
/工作 开（或关） 开启后直接说要做什么
/工作 <内容>  直接提交一次任务，与 /任务 相同
/任务 <内容>  单次任务，保留多行、代码和附件
/任务 /超时3600 <内容>  自定义执行时限
/任务列表  查看最近任务
/进度 [任务号]  状态及发送情况
/取消 [任务号]  停止排队或执行
/继续 [任务号] [补充要求]  核对已有结果后继续
/结果 [任务号] [页码]  查看完整正文的指定页
/文件 [任务号] [序号]  列出或发送产物
/预览 [任务号] <序号> [页码]  在 QQ 查看文本附件
/适配 <任务号> <序号> [MiB]  视频分段，必要时转码；默认每段20 MiB
/下载 <任务号> <序号>  生成原文件下载链接（需配置下载站点）
/全文 [任务号]  发送完整 Markdown 文件
/补发 [任务号]  重试未送达的结果与附件
/日志 [任务号]  查看近期执行进展
/目录 [路径]  查询或设置后续任务工作目录
/新会话  后续任务使用新的上下文
/聊 <内容>  工作模式下临时闲聊
/音乐 描述需求  自动创作或找歌翻唱；/唱歌 帮助 查看参数
/画、/动、/说、/表情 等原有功能继续可用
任务默认最多30分钟；结果保存后可反复查询。'''

CONTROLS = {'/工作', '/任务列表', '/进度', '/取消', '/继续', '/结果', '/文件', '/全文', '/预览',
            '/补发', '/日志', '/目录', '/新会话', '/帮助', '/help', '/状态', '/适配', '/下载'} | akari_music.COMMANDS
LEGACY = {'/画', '/动', '/动图', '/gif', '/说', '/语音', '/voice', '/表情', '/收藏', '/删表情', '/表情列表'}


def task_text(raw):
    # Only QQ's actual leading mention markup is transport syntax. Keep @files,
    # email addresses, shell arguments, newlines and code verbatim.
    return re.sub(r'^(?:\s*<@!?[^>]+>\s*)+', '', raw or '').strip()


def timeout_of(prompt, cfg):
    timeout = int(cfg.get('agent_default_timeout', 1800))
    ceiling = int(cfg.get('agent_max_timeout', 86400))
    while True:
        match = re.match(r'^/(超长|超时|t)(\d+)?(?=\s|$)\s*', prompt)
        if not match:
            break
        flag, value = match.groups()
        if value:
            timeout = int(value)
        elif flag == '超长':
            timeout = int(cfg.get('agent_long_default_timeout', 3600))
        else:
            raise ValueError('超时参数需要秒数，例如 /超时3600')
        prompt = prompt[match.end():]
    return prompt, max(15, min(timeout, ceiling))


class Commands:
    def __init__(self, store, cfg, log):
        self.store, self.cfg, self.log = store, cfg, log

    def allowed(self, ev):
        return bool(ev.get('user_openid')) and (ev['user_openid'] in self.cfg.get('agent_allowed_openids', [])
                                               or self.cfg.get('agent_allow_all', False))

    def prefixes(self):
        return {p.strip() for p in self.cfg.get('agent_prefixes', '/任务,/task,/pi').split(',') if p.strip()}

    def recognizes(self, ev):
        if not self.cfg.get('agent_enabled', True):
            return False
        text = task_text(ev.get('content', ''))
        command = text.split(maxsplit=1)[0] if text else ''
        if command in CONTROLS | self.prefixes():
            return True
        if command == '/聊' or command in LEGACY:
            return False
        return self.allowed(ev) and (self.store.session(scope_of(ev)).get('mode') == 'work' or
                                    (self.cfg.get('music_enabled', False) and self.cfg.get('music_auto_route', True) and akari_music.is_request(text)))

    def reply(self, ev, text, job=None):
        self.store.enqueue(ev, {'kind': 'text', 'text': text}, job['id'] if job else None)

    def submit(self, ev, prompt, parent=None, operation=None, ack=True):
        prompt, timeout = timeout_of(prompt, self.cfg)
        if not prompt and ev.get('attachments'):
            prompt = '请查看本条消息的附件，说明内容和可进行的处理。'
        if not prompt:
            raise ValueError('请附上任务内容，例如 /任务 分析这个项目并给我修改后的文件')
        if len(prompt) > int(self.cfg.get('agent_prompt_max_chars', 30000)):
            raise ValueError('任务正文过长，请把详细要求作为附件发送。')
        if self.cfg.get('dry_run', True):
            self.log('DRYRUN task accepted without execution')
            self.reply(ev, 'dry_run 已开启：已解析任务，但不会执行或发送 QQ 消息。')
            return
        job = self.store.submit(ev, prompt, timeout, self.cfg.get('agent_workdir', str(Path.home() / 'pi-tasks')),
                                parent=parent, max_queued=int(self.cfg.get('agent_queue_limit', 30)), operation=operation)
        if ack:
            self.store.enqueue(ev, dict(kind='text', text='收到，任务 %s 已登记（最多 %d 分钟）。\n/进度 %s 查询；/取消 %s 停止。' %
                               (job['id'], (timeout + 59) // 60, job['id'], job['id'])), job['id'], job['id'] + ':ack')
        return job

    def handle(self, ev):
        text = task_text(ev.get('content', ''))
        bits = text.split(maxsplit=1)
        command, rest = (bits[0], bits[1] if len(bits) > 1 else '') if bits else ('', '')
        scope = scope_of(ev)
        if command in ('/帮助', '/help'):
            for part in split_full(HELP, 850):
                self.reply(ev, part)
            return
        if not self.allowed(ev):
            self.reply(ev, '后台工作指令仅限已授权的主人使用。')
            return
        try:
            # A fresh interaction refreshes reply opportunities for existing results.
            self.store.refresh(ev)
            if command in akari_music.COMMANDS:
                akari_music.handle(self, ev, rest)
            elif self.cfg.get('music_enabled', False) and self.cfg.get('music_auto_route', True) and akari_music.is_request(text):
                akari_music.handle(self, ev, text, natural=True)
            elif command in self.prefixes():
                self.submit(ev, rest)
            elif command == '/工作':
                if rest in ('开', '开启', 'on'):
                    self.store.session(scope, mode='work')
                    self.reply(ev, '工作模式已开启。直接说要做什么即可；可附带文件或图片。用 /聊 临时闲聊，/工作 关 退出。')
                elif rest in ('关', '关闭', 'off'):
                    self.store.session(scope, mode='chat')
                    self.reply(ev, '已切回聊天模式；已登记的任务继续执行。')
                elif rest and rest not in ('状态', 'status'):
                    self.submit(ev, rest)
                else:
                    self.reply(ev, '当前模式：' + self.store.session(scope).get('mode', 'chat') + '。用 /工作 开 或 /工作 关 切换；/工作 任务内容 可直接提交任务。')
            elif command == '/新会话':
                session = self.store.session(scope)
                self.store.session(scope, generation=session.get('generation', 0) + 1)
                self.reply(ev, '后续任务会使用新上下文；已有任务及结果仍可查询。')
            elif command == '/目录':
                session = self.store.session(scope)
                if rest:
                    path = Path(rest).expanduser().resolve()
                    if not path.is_dir():
                        raise ValueError('工作目录不存在或不是目录。')
                    session = self.store.session(scope, cwd=str(path))
                self.reply(ev, '任务工作目录：' + session.get('cwd', self.cfg.get('agent_workdir', str(Path.home() / 'pi-tasks'))))
            elif command in ('/任务列表', '/状态'):
                jobs = self.store.list(scope, limit=6)
                lines = ['【任务列表】模式=' + self.store.session(scope).get('mode', 'chat')]
                for job in jobs:
                    lines.append('%s %s %s' % (job['id'], LABELS[job['status']], job['prompt'][:45].replace('\n', ' ')))
                self.reply(ev, '\n'.join(lines) if jobs else '在线，当前会话还没有任务。/工作 开 可开启工作模式。')
            elif command in CONTROLS:
                self.job_command(ev, command, rest)
            else:
                self.submit(ev, text)
        except (ValueError, OSError) as exc:
            self.reply(ev, str(exc)[:600])

    def job_command(self, ev, command, rest):
        args = rest.split(maxsplit=1)
        jid, tail = (args[0], args[1] if len(args) > 1 else '') if args else ('最近', '')
        job = self.store.get(jid, scope_of(ev))
        if not job:
            raise ValueError('找不到当前会话中属于你的任务；用 /任务列表 查看任务号。')
        jid = job['id']
        if command == '/进度':
            elapsed = int((job.get('finished') or time.time()) - job.get('started', job['created']))
            info = self.store.delivery_info(jid)
            children = self.store.delivery_children(jid)
            child_media = {child['id']:self.store.media_status(child['id']) for child in children}
            for child in children:
                info.extend(self.store.delivery_info(child['id']))
            lines = ['【%s · %s】%d 秒' % (jid, LABELS[job['status']], elapsed), job.get('progress', '')]
            if job.get('error'):
                lines.append(job['error'][:250])
            sent = sum(x['n'] for x in info if x['status'] == 'sent')
            waiting = sum(x['n'] for x in info if x['status'] not in ('sent', 'replaced'))
            lines.append('消息送达 %d 条，待发送 %d 条；/补发 %s 可重试。' % (sent, waiting, jid))
            media = self.store.media_status(jid)
            for parts in child_media.values():
                media.extend(parts)
            lines.append('附件 QQ 接收确认 %d 个，待发送 %d 个。' % (
                sum(x['n'] for x in media if x['status']=='sent'),
                sum(x['n'] for x in media if x['status'] not in ('sent','replaced'))))
            for child in children:
                parts = child_media[child['id']]
                lines.append('自动适配 %s：%s，已确认 %d 个分段；/进度 %s' % (child['id'], LABELS[child['status']],
                    sum(x['n'] for x in parts if x['status']=='sent'), child['id']))
            for progress in self.store.upload_progress(jid):
                phase = {'prepare':'申请上传', 'parts':'传输分片', 'complete':'确认文件', 'message':'发送消息'}.get(progress.get('phase'), '上传')
                lines.append('%s %s：%.1f/%.1f MiB，%d/%d 片' % (phase, progress['name'],
                    progress['bytes']/1048576, progress['total']/1048576, progress['parts'], progress['total_parts']))
            errors = [x['error'] for x in info if x['error'] and x['status'] not in ('sent','replaced')]
            if errors:
                lines.append('发送情况：' + errors[0][:180])
            self.reply(ev, '\n'.join(lines))
        elif command == '/取消':
            updated = self.store.cancel(jid, job['scope'])
            self.reply(ev, '任务 %s：%s' % (jid, '已请求停止，将保留已有产物。' if updated['status'] == 'running' else LABELS[updated['status']]))
        elif command == '/继续':
            if job['status'] not in TERMINAL:
                raise ValueError('这个任务还在执行或排队；请先等待或取消。')
            if (job.get('operation') or {}).get('kind') == 'sing':
                raise ValueError('唱歌任务请用 /唱歌 和原歌曲重新提交；已有音频可用 /文件 取回。')
            self.submit(ev, tail or '继续完成前次任务，并核实最终结果。', parent=job)
        elif command == '/结果':
            result = job.get('result')
            if not result:
                path = Path(job['directory']) / 'partial.txt'
                result = path.read_text(errors='replace') if path.exists() else '暂时还没有正文，可用 /进度 查询。'
            pages = split_full(result, int(self.cfg.get('agent_page_chars', 700)))
            page = int(tail or 1)
            if not 1 <= page <= len(pages):
                raise ValueError('页码应在 1 到 %d 之间。' % len(pages))
            self.reply(ev, '【%s 正文 %d/%d】\n%s%s' % (jid, page, len(pages), pages[page - 1],
                       '\n下一页：/结果 %s %d' % (jid, page + 1) if page < len(pages) else ''))
        elif command in ('/全文', '/文件'):
            files = job.get('artifacts', [])
            if command == '/全文':
                path = Path(job['directory']) / 'result.md'
                if not path.exists():
                    raise ValueError('任务尚未生成完整结果。可先用 /结果 查看部分正文。')
                self.store.enqueue(ev, dict(kind='media', path=str(path), root=job['directory'], file_type=4), jid)
            elif tail:
                index = int(tail)
                if not 1 <= index <= len(files):
                    raise ValueError('文件序号应在 1 到 %d 之间。' % len(files))
                self.store.enqueue(ev, dict(kind='media', **files[index - 1]), jid)
            else:
                # Keep every artifact addressable, even when the listing needs pages.
                text = '\n'.join('%d. %s (%d 字节)' % (i, a['name'], a['size']) for i, a in enumerate(files, 1))
                text = text or '尚无产物；任务结束后可再次查询。'
                for child in self.store.delivery_children(jid):
                    text += '\n自动分段文件：/文件 %s（%s）' % (child['id'], LABELS[child['status']])
                listing = split_full(text, 700)
                for part in listing:
                    self.reply(ev, '【%s 附件】\n%s\n发送：/文件 %s 序号' % (jid, part, jid))
        elif command in ('/适配', '/下载'):
            values = tail.split()
            if not values:
                raise ValueError('用法：%s %s 文件序号%s' % (command, jid, ' [每段 MiB]' if command == '/适配' else ''))
            files = job.get('artifacts', [])
            index = int(values[0])
            if not 1 <= index <= len(files):
                raise ValueError('文件序号不存在；用 /文件 %s 查看。' % jid)
            path = Path(files[index - 1]['path'])
            root = Path(job['directory']) / 'artifacts'
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('产物路径已改变，请重新登记最终文件。')
            operation = dict(kind='adapt' if command == '/适配' else 'share', source=str(path), root=str(root))
            if command == '/适配':
                from akari_delivery import media_type
                if media_type(path) != 2:
                    raise ValueError('目前 /适配 支持视频分段；其他文件可用 /下载 或下达压缩分卷任务。')
                size = int(values[1]) if len(values) > 1 else 20
                if not 1 <= size <= 512:
                    raise ValueError('每段大小应为 1–512 MiB；这只是目标大小，不代表 QQ 平台允许值。')
                operation['max_bytes'] = size * 1048576
                prompt = '将 %s 分成每段不超过 %d MiB 的独立视频，必要时转码，保留原文件。' % (path.name, size)
            else:
                if not self.cfg.get('agent_download_root') or not self.cfg.get('agent_download_base_url'):
                    raise ValueError('尚未配置可访问的下载站点，不能生成有效外链。视频可用 /适配 %s %d 20 分段发送；管理员需配置 agent_download_root 和 agent_download_base_url。' % (jid, index))
                prompt = '为 %s 生成原文件下载链接。' % path.name
            self.submit(ev, prompt, parent=job, operation=operation)
        elif command == '/补发':
            self.store.refresh(ev, jid=jid, retry=True)
            self.reply(ev, '已重试任务 %s 尚未送达的内容。正文也可用 /结果 %s 按页查看。' % (jid, jid))
        elif command == '/预览':
            values = tail.split()
            if not values:
                raise ValueError('用法：/预览 %s 文件序号 [页码]' % jid)
            index, page = int(values[0]), int(values[1]) if len(values) > 1 else 1
            files = job.get('artifacts', [])
            if not 1 <= index <= len(files):
                raise ValueError('文件序号不存在；用 /文件 %s 查看。' % jid)
            path = Path(files[index - 1]['path'])
            root = Path(job['directory']) / 'artifacts'
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('产物路径已改变，请重新生成或登记该文件。')
            text_extensions = {'.txt', '.md', '.csv', '.tsv', '.json', '.jsonl', '.py', '.js', '.ts',
                               '.html', '.css', '.xml', '.yaml', '.yml', '.toml', '.ini', '.log', '.sh', '.sql', '.svg'}
            if path.suffix.lower() not in text_extensions:
                raise ValueError('这是二进制附件，请用 /文件 发送；如需文字解读，可下达新任务处理。')
            if path.stat().st_size > int(self.cfg.get('agent_preview_max_bytes', 2 * 1024 * 1024)):
                raise ValueError('文件较大，请下达任务提取需要的部分，或直接发送文件。')
            pages = split_full(path.read_text(encoding='utf-8', errors='replace'), int(self.cfg.get('agent_page_chars', 700)))
            if not 1 <= page <= len(pages):
                raise ValueError('页码应在 1 到 %d 之间。' % len(pages))
            text = '【%s · %d/%d】\n%s' % (path.name, page, len(pages), pages[page - 1])
            if page < len(pages):
                text += '\n下一页：/预览 %s %d %d' % (jid, index, page + 1)
            self.reply(ev, text)
        elif command == '/日志':
            path = Path(job['directory']) / 'progress.log'
            notes = ''
            if path.exists():
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size - 3000))
                    notes = stream.read().decode('utf-8', 'replace')[-700:]
            self.reply(ev, '【%s 进展】\n%s' % (jid, notes or job.get('progress', '暂无进展')))
