"""Bounded public music search/download. No bot credentials, cookies or shell interpolation."""
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from urllib.parse import urlsplit

PLATFORMS = ('youtube.com', 'youtu.be', 'bilibili.com', 'b23.tv', 'soundcloud.com', 'archive.org', 'music.163.com')


def public_url(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise ValueError('歌曲地址必须为公开 HTTP(S) 链接，不可包含凭据或自定义端口。')
    if not any(host == domain or host.endswith('.' + domain) for domain in PLATFORMS):
        raise ValueError('该站点暂不支持直接下载。请提供 YouTube/B站/SoundCloud/Archive/网易云公开链接，或音频附件。')
    try:
        addresses = socket.getaddrinfo(host, parsed.port or 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except OSError as e:
        raise ValueError('歌曲网站域名解析失败。') from e
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('不允许访问本机或私有网络歌曲地址。')
    return url


def command(cfg):
    binary = cfg.get('downloader') or shutil.which('yt-dlp')
    if not binary:
        raise ValueError('尚未安装 yt-dlp，请先安装音乐下载工具。')
    return [str(binary), '--ignore-config', '--force-ipv4', '--no-playlist', '--no-cache-dir', '--socket-timeout', '20',
            '--retries', '2', '--fragment-retries', '2', '--js-runtimes', 'node', '--no-warnings']


def run(args, timeout, log=None):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError('歌曲搜索/下载超时，请换一个来源或提供音频附件。') from None
    if log is not None:
        Path(log).write_text((result.stdout + '\n' + result.stderr)[-100000:], encoding='utf-8')
    if result.returncode:
        # No automatic attempt to bypass paid/login/DRM restrictions.
        error = result.stderr[-1800:]
        raise ValueError('公开歌曲获取失败：' + error)
    return result.stdout


def search(query, cfg):
    if not cfg.get('auto_download', True):
        raise ValueError('自动搜索下载已关闭，请提供本地歌曲或音频附件。')
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 240 or query.startswith('-'):
        raise ValueError('歌曲搜索词须为1至240字。')
    raw = run(command(cfg) + ['--flat-playlist', '--dump-single-json', '--skip-download',
                             '--', 'ytsearch5:' + query], int(cfg.get('download_timeout', 180)))
    result = json.loads(raw)
    entries = result.get('entries') or [result]
    found = []
    for entry in entries:
        if not entry or entry.get('live_status') in ('is_live', 'is_upcoming'):
            continue
        duration = entry.get('duration')
        if duration is not None and not 5 <= duration <= float(cfg.get('download_max_seconds', 900)):
            continue
        url = entry.get('webpage_url') or entry.get('url') or ''
        if not url.startswith(('https://', 'http://')):
            continue
        found.append(dict(title=str(entry.get('title') or '')[:240], url=url, duration=duration,
                          uploader=str(entry.get('channel') or entry.get('uploader') or '')[:160]))
    if not found:
        raise ValueError('没有找到合适的公开歌曲结果，请补充歌手/原文歌名或提供链接。')
    return found[:5]


def download(url, destination, cfg):
    if not cfg.get('auto_download', True):
        raise ValueError('自动下载已关闭。')
    public_url(url)
    folder = Path(destination)
    folder.mkdir(parents=True, exist_ok=True)
    metadata = json.loads(run(command(cfg) + ['--dump-single-json', '--skip-download', '--', url],
                              int(cfg.get('download_timeout', 180))))
    duration = metadata.get('duration')
    if metadata.get('is_live') or not isinstance(duration, (int, float)) or not 5 <= duration <= float(cfg.get('download_max_seconds', 900)):
        raise ValueError('歌曲时长未知、过长或为直播，未开始下载。')
    if metadata.get('availability') in ('private', 'premium_only', 'subscriber_only', 'needs_auth'):
        raise ValueError('该歌曲需要登录、付费或订阅，请换公开来源或上传音频。')
    limit = int(cfg.get('download_max_bytes', 80 * 1024 * 1024))
    template = str(folder / 'source.%(ext)s')
    run(command(cfg) + ['-f', 'bestaudio/best', '--max-filesize', str(limit), '--no-keep-video',
        '-x', '--audio-format', 'wav', '-o', template, '--', url],
        int(cfg.get('download_timeout', 180)), folder / 'download.log')
    audio = folder / 'source.wav'
    if not audio.is_file() or not 44 < audio.stat().st_size <= max(limit, duration * 48000 * 2 * 2 + 4096):
        raise ValueError('下载后没有得到有效音频，或音频超过大小限制。')
    provenance = dict(url=url, title=str(metadata.get('title') or '')[:300],
                      uploader=str(metadata.get('uploader') or '')[:160], duration=duration)
    (folder / 'source.json').write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding='utf-8')
    return audio, provenance


def resolve_source(value, task, cfg):
    from akari_music import catalog_source
    value = str(value or '').strip()
    if value.startswith(('https://', 'http://')):
        return download(value, Path(task) / 'download', cfg)
    if Path(value).is_absolute():
        p = Path(value).resolve(strict=True)
        roots = [Path(cfg['root']).resolve() / 'songs', Path(task).resolve()]
        if os.environ.get('AKARI_TASK_DIR'):
            roots.append(Path(os.environ['AKARI_TASK_DIR']).resolve())
        if not p.is_file() or not any(p.is_relative_to(root) for root in roots):
            raise ValueError('歌曲路径须位于当前任务目录或本地曲库。')
        return p, dict(local=str(p))
    try:
        return catalog_source(value, cfg['root']), dict(catalog=value)
    except ValueError:
        if not value or '/' in value or '\\' in value or value in ('.', '..'):
            raise
    candidates = search(value + ' official audio', cfg)
    # The native agent can inspect candidates and choose a URL; direct commands use the top result.
    source, provenance = download(candidates[0]['url'], Path(task) / 'download', cfg)
    provenance['query'] = value
    provenance['selection'] = 'top search result'
    (Path(task) / 'source-selection.json').write_text(json.dumps(dict(selected=provenance, candidates=candidates), ensure_ascii=False, indent=2))
    return source, provenance
