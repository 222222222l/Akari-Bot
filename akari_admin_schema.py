"""Public editable settings. Secrets and executable paths never enter this schema."""
import math
import re

GROUPS = [
    {'id': 'chat', 'name': '对话与回复', 'description': '调整灯里的表达方式和群聊互动节奏。'},
    {'id': 'media', 'name': '图像与视频', 'description': '平衡生成质量、等待时间与调用频率。'},
    {'id': 'voice', 'name': '语音与翻唱', 'description': '管理语音回复，以及阿卡林音色的音乐生成。'},
    {'id': 'agent', 'name': '任务工作台', 'description': '控制长任务队列、执行时限与结果推送。'},
    {'id': 'connection', 'name': '连接与调试', 'description': '网关重连和调试设置，修改后需要重启 bot。'},
]
FIELDS = []

def field(key, label, kind, default, group, minimum=None, maximum=None, help='', options=None, scope='bot'):
    FIELDS.append(dict(key=key, label=label, type=kind, default=default, group=group,
                       min=minimum, max=maximum, help=help, options=options, scope=scope))

FEATURES = [
    ('agent_enabled', '任务工作台', '执行指令、管理长任务与发送结果', 'terminal', 'agent'),
    ('vision_enabled', '图片理解', '识别聊天图片，结合画面进行回复', 'scan', 'chat'),
    ('search_enabled', '联网搜索', '查询实时信息，补充对话上下文', 'globe', 'chat'),
    ('sticker_enabled', '表情互动', '让灯里在合适的时候发送表情', 'smile', 'chat'),
    ('image_enabled', '图片生成', '通过 ComfyUI 绘制图片', 'image', 'media'),
    ('video_enabled', '视频生成', '根据图片生成短视频', 'film', 'media'),
    ('voice_enabled', '语音回复', '用灯里的声音说出回复', 'mic', 'voice'),
    ('music_enabled', '阿卡林音乐', '原创生成、自动找歌翻唱与重编曲', 'music', 'voice'),
]
for key, label, desc, icon, group in FEATURES:
    field(key, label, 'bool', True, group, help=desc)
    FIELDS[-1]['feature'] = icon

field('bot_name', '显示名称', 'str', '赤座灯里', 'chat', 1, 40)
field('model', '对话模型', 'str', 'Qwen3.8-27B', 'chat', 1, 160, '使用当前 LLM 服务已加载的模型名称。')
field('group_reply_mode', '群聊回复模式', 'select', 'all', 'chat', options=[['all', '参与所有群消息'], ['at_only', '仅被 @ 时回复'], ['at_or_keyword', '@ 或关键词触发']])
field('keyword_regex', '触发关键词', 'str', '灯里|阿卡林|あかり|akari', 'chat', 1, 240, '使用 | 分隔关键词，支持正则表达式。')
field('temperature', '回复随机度', 'float', .85, 'chat', 0, 2, '越低越稳定，越高越有变化。')
field('top_p', '采样范围 · Top P', 'float', .95, 'chat', .01, 1)
field('max_tokens', '回复 Token 上限', 'int', 420, 'chat', 32, 32768)
field('history_max', '历史消息条数', 'int', 24, 'chat', 0, 200)
field('llm_timeout', '对话超时（秒）', 'int', 90, 'chat', 5, 600)
field('reply_max_chars', '回复字符上限', 'int', 2600, 'chat', 100, 20000)
field('split_len', '单条消息字符数', 'int', 900, 'chat', 100, 2000)
field('max_chunks', '单次最多消息数', 'int', 3, 'chat', 1, 10)
field('sticker_cooldown_secs', '表情冷却（秒）', 'int', 90, 'chat', 0, 86400)
field('sticker_max_per_reply', '每次最多表情数', 'int', 1, 'chat', 1, 5)
for key, label, default, lo, hi in [
    ('image_megapixels', '图片分辨率（百万像素）', 1., .1, 4),
    ('image_timeout', '图片生成超时（秒）', 180, 30, 1800),
    ('image_cooldown_secs', '图片冷却（秒）', 120, 0, 86400),
    ('image_wait_secs', '图片等待上限（秒）', 240, 30, 3600),
    ('video_megapixels', '视频分辨率（百万像素）', .4, .1, 2),
    ('video_timeout', '视频生成超时（秒）', 600, 30, 3600),
    ('video_cooldown_secs', '视频冷却（秒）', 240, 0, 86400),
    ('video_wait_secs', '视频等待上限（秒）', 300, 30, 3600),
    ('video_default_secs', '默认视频时长（秒）', 3, 1, 30),
    ('video_secs_min', '最短视频时长（秒）', 2, 1, 30),
    ('video_secs_max', '最长视频时长（秒）', 5, 1, 30),
]:
    field(key, label, 'float' if isinstance(default, float) else 'int', default, 'media', lo, hi)
for key, label, default, lo, hi in [
    ('voice_cooldown_secs', '语音冷却（秒）', 240, 0, 86400),
    ('voice_max_chars', '语音文本上限', 160, 10, 1000),
    ('voice_timeout', '语音生成超时（秒）', 180, 10, 1800),
    ('music_timeout', '翻唱任务超时（秒）', 3600, 60, 86400),
]:
    field(key, label, 'int', default, 'voice', lo, hi)
field('music_auto_route', '自然语言音乐任务', 'bool', True, 'voice', help='授权用户的明确音乐创作/翻唱需求自动进入 Pi 任务。')
field('reference', '默认音色参考文件', 'str', 'songs/三上枝織 - ハイっ!アッカリーン.mp3', 'voice', 1, 2048, '相对于 music/config.json 的路径，或绝对路径；整首歌可自动提取短人声。', scope='music')
field('reference_extract_vocals', '自动提取参考人声', 'bool', True, 'voice', help='首次分离伴奏、选取有效演唱片段并缓存。', scope='music')
field('reference_seconds', '参考人声长度（秒）', 'int', 18, 'voice', 5, 25, scope='music')
field('auto_download', '自动搜索下载歌曲', 'bool', True, 'voice', help='仅获取公开音源，失败时报告原因；新任务生效。', scope='music')
field('download_timeout', '歌曲获取超时（秒）', 'int', 180, 'voice', 30, 900, scope='music')
field('download_max_seconds', '允许下载歌曲时长（秒）', 'int', 900, 'voice', 10, 1800, scope='music')
field('max_seconds', '最长翻唱时长（秒）', 'int', 600, 'voice', 10, 1800, '新翻唱任务生效，无需重启。', scope='music')
field('diffusion_steps', '音色转换步数', 'int', 40, 'voice', 1, 200, '步数越多，生成时间越长。新任务生效。', scope='music')
field('memory_budget_gib', '音乐显存预算（GiB）', 'int', 24, 'voice', 8, 80, 'YuE2 推理显存预算，新任务生效。', scope='music')
field('cuda_visible_devices', '音乐使用 GPU', 'str', '7', 'voice', 1, 40, '物理 GPU 编号，如 7。新任务生效。', scope='music')
field('style', '重编曲风格', 'text', 'Japanese, anime opening, J-pop rock, bright youthful female lead vocal', 'voice', 1, 2000, '仅用于 YuE2 重编曲，新任务生效。', scope='music')
for key, label, default, lo, hi in [
    ('agent_workers', '同时执行任务数', 2, 1, 8),
    ('agent_queue_limit', '等待队列容量', 30, 1, 200),
    ('agent_default_timeout', '普通任务默认超时（秒）', 1800, 30, 86400),
    ('agent_max_timeout', '普通任务最大超时（秒）', 86400, 30, 86400),
    ('agent_long_default_timeout', '长任务默认超时（秒）', 3600, 30, 86400),
    ('agent_long_max_timeout', '长任务最大超时（秒）', 86400, 30, 86400),
    ('agent_progress_secs', '进度推送间隔（秒）', 60, 10, 3600),
    ('agent_auto_result_pages', '自动发送结果页数', 1, 0, 10),
    ('agent_auto_artifacts', '自动发送附件数', 2, 0, 10),
    ('agent_upload_concurrency', '附件上传并发数', 2, 1, 8),
    ('agent_max_output_tokens', '任务输出 Token 上限', 16384, 512, 65536),
]:
    field(key, label, 'int', default, 'agent', lo, hi)
for key, label in [('agent_ack', '任务接收回执'), ('agent_active_push', '主动推送任务结果'), ('agent_chunked_upload', '大附件分块上传'), ('agent_video_auto_adapt', '视频自动适配')]:
    field(key, label, 'bool', True, 'agent')
for key, label, default, lo, hi in [
    ('ready_timeout', '网关握手超时（秒）', 75, 10, 300),
    ('stale_hb_limit', '允许缺失心跳次数', 4, 2, 20),
    ('reconnect_min', '最短重连等待（秒）', 30, 1, 300),
    ('reconnect_max', '最长重连等待（秒）', 300, 1, 3600),
    ('session_refresh_secs', '会话主动刷新间隔（秒）', 1500, 120, 3600),
]:
    field(key, label, 'int', default, 'connection', lo, hi)
field('wire_dump', '网关调试日志', 'bool', False, 'connection', help='记录网关事件类型与长度，日志量会增加。')
field('dry_run', '演练模式', 'bool', False, 'connection', help='用于调试；开启后部分发送与任务执行会被跳过。')
INDEX = {(f['scope'], f['key']): f for f in FIELDS}


def validate(scope, patch, current):
    if not isinstance(patch, dict):
        raise ValueError('配置修改必须为对象')
    for key, value in patch.items():
        f = INDEX.get((scope, key))
        if not f:
            raise ValueError('不允许修改此配置项：' + key)
        kind = f['type']
        valid = ((kind == 'bool' and type(value) is bool) or
                 (kind == 'int' and type(value) is int) or
                 (kind == 'float' and type(value) in (int, float) and math.isfinite(value)) or
                 (kind in ('str', 'text', 'select') and isinstance(value, str)))
        if not valid:
            raise ValueError(f['label'] + '：类型不正确')
        measured = len(value) if isinstance(value, str) else value
        if f['min'] is not None and not f['min'] <= measured <= f['max']:
            raise ValueError(f"{f['label']}：须在 {f['min']} 到 {f['max']} 之间")
        if kind == 'select' and value not in [v[0] for v in f['options']]:
            raise ValueError(f['label'] + '：选项无效')
    merged = {f['key']: f['default'] for f in FIELDS if f['scope'] == scope}
    merged.update(current)
    merged.update(patch)
    if scope == 'bot':
        for lo, hi in [('video_secs_min', 'video_default_secs'), ('video_default_secs', 'video_secs_max'),
                       ('agent_default_timeout', 'agent_max_timeout'), ('agent_long_default_timeout', 'agent_long_max_timeout'),
                       ('reconnect_min', 'reconnect_max')]:
            if merged[lo] > merged[hi]:
                raise ValueError(INDEX[scope, lo]['label'] + ' 不能大于 ' + INDEX[scope, hi]['label'])
        if 'keyword_regex' in patch:
            try:
                re.compile(patch['keyword_regex'])
            except re.error:
                raise ValueError('触发关键词：正则表达式无效') from None
    if scope == 'music' and not re.fullmatch(r'\d+(,\d+)*', merged['cuda_visible_devices']):
        raise ValueError('GPU 编号应为数字，多个编号用英文逗号分隔')
