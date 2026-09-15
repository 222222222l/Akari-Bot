"""Bounded public music search/download. No bot credentials, cookies or shell interpolation."""
import ipaddress
import json
import os
import re
import time
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
        if 'Sign in to confirm' in error or 'not a bot' in error:
            raise ValueError('YouTube 要求登录/人机验证，此音源当前不可下载；请换候选或使用 bilibili 搜索。完整原因保留在下载日志。')
        raise ValueError('公开歌曲获取失败：' + error[-500:])
    return result.stdout


def search(query, cfg, platform='youtube'):
    if not cfg.get('auto_download', True):
        raise ValueError('自动搜索下载已关闭，请提供本地歌曲或音频附件。')
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 240 or query.startswith('-'):
        raise ValueError('歌曲搜索词须为1至240字。')
    prefixes = {'youtube': 'ytsearch5:', 'bilibili': 'bilisearch5:'}
    if platform not in prefixes:
        raise ValueError('搜索平台须为 youtube 或 bilibili。')
    started = time.monotonic()
    raw = run(command(cfg) + ['--flat-playlist', '--dump-single-json', '--skip-download',
                             '--', prefixes[platform] + query], int(cfg.get('download_timeout', 180)))
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
    # BiliBili's flat search may only expose URLs. Enrich independently so one
    # unavailable video cannot discard all other candidates.
    metadata_deadline = min(time.monotonic() + 30, started + int(cfg.get('download_timeout', 180)))
    for candidate in found[:5]:
        remaining = int(metadata_deadline - time.monotonic())
        if candidate['title'] or remaining < 1:
            continue
        try:
            public_url(candidate['url'])
            detail = json.loads(run(command(cfg) + ['--dump-single-json', '--skip-download', '--', candidate['url']], min(8, remaining)))
            candidate.update(title=str(detail.get('title') or '')[:240],
                             uploader=str(detail.get('uploader') or '')[:160], duration=detail.get('duration'))
        except (ValueError, OSError) as exc:
            candidate['metadata_error'] = str(exc)[-300:]
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
                              int(cfg.get('download_timeout', 180)), folder / 'metadata.log'))
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
    # User prose can reach structured /翻唱; remove only the request prefix.
    query = re.sub(r'^(?:请)?(?:为我|帮我|给我)?(?:翻唱|唱一下|唱一首)\s*', '', value).strip('《》「」 ')
    attempts, candidates, search_errors = [], [], []
    record = Path(task) / 'source-selection.json'
    record.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 300
    def save(selected=None):
        record.write_text(json.dumps(dict(query=query, selected=selected, attempts=attempts,
            candidates=candidates, search_errors=search_errors), ensure_ascii=False, indent=2))
    seen = set()
    for platform in ('youtube', 'bilibili'):
        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            break
        bounded = dict(cfg, download_timeout=min(int(cfg.get('download_timeout', 180)), 45, remaining))
        try:
            results = search(query, bounded, platform=platform)
        except (ValueError, OSError) as exc:
            search_errors.append(dict(platform=platform, error=str(exc)[-600:]))
            save()
            continue
        candidates.extend(results)
        for candidate in results[:3]:
            url = candidate['url']
            remaining = int(deadline - time.monotonic())
            if url in seen or remaining < 2:
                continue
            seen.add(url)
            # Metadata + extraction each consume at most this timeout.
            bounded = dict(cfg, download_timeout=min(int(cfg.get('download_timeout', 180)), 45, remaining // 2))
            attempt = dict(url=url, platform=platform)
            attempts.append(attempt)
            save()
            try:
                source, provenance = download(url, Path(task) / 'download' / ('candidate-%02d' % len(attempts)), bounded)
            except (ValueError, OSError) as exc:
                attempt.update(status='failed', error=str(exc)[-600:])
                save()
                continue
            attempt['status'] = 'succeeded'
            provenance.update(query=query, selection='first downloadable candidate', platform=platform)
            save(provenance)
            return source, provenance
    save()
    raise ValueError('已尝试 %d 个候选，仍未取得可用音源（可能需要登录验证或当前网络不可用）。请提供明确的公开链接或音频附件；换源记录见 source-selection.json。' % len(attempts))
