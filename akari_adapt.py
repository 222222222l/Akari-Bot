#!/usr/bin/env python3
"""Deterministic background delivery adaptation; supervised like other tasks."""
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.parse
import uuid


def emit(kind, **values):
    print(json.dumps(dict(type=kind, **values), ensure_ascii=False), flush=True)


def probe(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(path)],
                            capture_output=True, text=True, check=True, timeout=30)
    duration = float(json.loads(result.stdout)['format']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('无法获取有效视频时长')
    return duration


def split_video(source, output, max_bytes):
    """Try stream copy first; oversized pieces are re-encoded with bounded bitrate."""
    duration = probe(source)
    output.mkdir(parents=True, exist_ok=True)
    work = output.parent / 'adapt-work'
    work.mkdir(exist_ok=True)
    segment_time = max(1, min(duration, duration * max_bytes / source.stat().st_size * 0.75))
    emit('akari_progress', text='正在按关键帧分段，保留原始视频和音频编码')
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(source), '-map', '0:v:0', '-map', '0:a?',
        '-c', 'copy', '-f', 'segment', '-segment_time', str(segment_time), '-reset_timestamps', '1',
        str(work / 'copy-%05d.mp4')], check=True)
    pieces = sorted(work.glob('copy-*.mp4'))
    if not pieces:
        raise ValueError('没有生成视频分段')
    finished, transcoded = [], False

    def publish(piece):
        # Every published piece is complete, playable, and checked against requested bytes.
        probe(piece)
        if not 0 < piece.stat().st_size <= max_bytes:
            raise ValueError('分段仍超过目标大小，原文件已保留；请使用更大的目标或下载链接')
        dest = output / ('part-%03d.mp4' % (len(finished) + 1))
        os.replace(piece, dest)
        finished.append(dest)
        emit('akari_artifact', path=str(dest))

    for piece in pieces:
        if piece.stat().st_size <= max_bytes:
            publish(piece)
            continue
        transcoded = True
        length = probe(piece)
        # Conservative headroom for audio/container and rate-control variation.
        target_duration = max(0.25, max_bytes * 8 * 0.60 / 1400000)
        count = math.ceil(length / target_duration)
        for index in range(count):
            start = index * target_duration
            dest = work / ('encode-%s-%04d.mp4' % (piece.stem, index))
            emit('akari_progress', text='部分关键帧间隔过长，正在转码分段 %d/%d' % (index + 1, count))
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-ss', str(start), '-i', str(piece),
                '-t', str(min(target_duration, length-start)), '-map', '0:v:0', '-map', '0:a?',
                '-vf', r'scale=w=min(1280\,iw):h=-2', '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast',
                '-b:v', '1000k', '-maxrate', '1200k', '-bufsize', '1200k', '-c:a', 'aac', '-b:a', '96k',
                '-movflags', '+faststart', str(dest)], check=True)
            publish(dest)
    return finished, transcoded


def share_file(source, root, base_url):
    url = urllib.parse.urlsplit(base_url)
    if url.scheme != 'https' or not url.netloc or url.query or url.fragment or url.username:
        raise ValueError('下载站点需配置无查询参数的 HTTPS 基础地址')
    root = Path(root).expanduser().resolve(strict=True)
    target = root / (uuid.uuid4().hex + source.suffix.lower())
    temp = target.with_suffix(target.suffix + '.tmp')
    try:
        shutil.copyfile(source, temp)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return base_url.rstrip('/') + '/' + urllib.parse.quote(target.name)


def main():
    operation = json.loads(sys.argv[1])
    source = Path(operation['source'])
    if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(Path(operation['root']).resolve()):
        raise ValueError('产物路径已改变，拒绝读取原任务目录之外的文件')
    output = Path(os.environ['AKARI_OUTPUT_DIR'])
    if operation['kind'] == 'adapt':
        limit = int(operation['max_bytes'])
        if not 1048576 <= limit <= 512 * 1048576:
            raise ValueError('目标大小超出允许范围')
        files, transcoded = split_video(source, output, limit)
        result = '已生成 %d 个独立 MP4，每段不超过 %.1f MiB。%s原文件保留在前次任务中；文件已登记，发送结果请用 /进度 查询。' % (
            len(files), limit / 1048576, '部分片段已转码，画质可能降低；' if transcoded else '保持原始音视频编码；')
    elif operation['kind'] == 'share':
        url = share_file(source, os.environ['AKARI_DOWNLOAD_ROOT'], os.environ['AKARI_DOWNLOAD_BASE_URL'])
        result = '原文件下载：%s\n%s\n链接由已配置站点提供，持有链接的人可下载；如打不开请检查该站点的外网映射。' % (source.name, url)
    else:
        raise ValueError('未知交付操作')
    emit('message_end', message=dict(role='assistant', stopReason='stop', content=[dict(type='text', text=result)]))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        emit('message_end', message=dict(role='assistant', stopReason='error', content=[],
             errorMessage='交付处理失败：' + type(exc).__name__ + ' ' + str(exc)[:250]))
        sys.exit(1)
