#!/usr/bin/env python3
"""Deterministic singing pipeline supervised by TaskService's process group."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from akari_adapt import emit
from akari_music import catalog_source, settings

AUDIO = {'.wav', '.flac', '.mp3', '.m4a', '.ogg', '.opus', '.aac', '.mp4', '.webm'}


def probe(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
        '-show_entries', 'stream=sample_rate,channels:format=duration', '-of', 'json', str(path)],
        capture_output=True, text=True, check=True, timeout=30)
    data = json.loads(result.stdout)
    duration = float(data.get('format', {}).get('duration', 0))
    if not data.get('streams') or not math.isfinite(duration) or duration <= 0:
        raise ValueError('文件不包含可读取的音频或有效时长。')
    return dict(duration=duration, **data['streams'][0])


def read_text(path):
    if path.stat().st_size > 100000:
        raise ValueError('歌词/谱面文件过大，应小于100 KB。')
    return path.read_text(encoding='utf-8-sig').strip()


def inputs(operation, task, cfg):
    index = json.loads((task / 'inputs' / 'index.json').read_text())
    if index['errors']:
        raise ValueError('歌曲附件未完整下载：' + '；'.join(index['errors']))
    files = []
    for name in index['files']:
        path = Path(name)
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to((task / 'inputs').resolve()):
            raise ValueError('歌曲附件路径无效。')
        files.append(path)
    audio_files = [p for p in files if p.suffix.lower() in AUDIO]
    text_files = [p for p in files if p.suffix.lower() == '.txt']
    scores = [p for p in files if p.suffix.lower() == '.abc']
    if len(audio_files) > 1 or len(text_files) > 1 or len(scores) > 1:
        raise ValueError('每次请提供一首歌曲、至多一份歌词和一份旋律谱。')
    if operation.get('song'):
        if audio_files:
            raise ValueError('请在曲库歌名与歌曲附件之间选择一种输入。')
        source = catalog_source(operation['song'], cfg['root'])
    elif audio_files:
        source = audio_files[0]
    else:
        raise ValueError('缺少歌曲音频，请使用 /唱歌 并在同条消息附上音频。')
    lyrics = operation.get('lyrics') or (read_text(text_files[0]) if text_files else '')
    if len(lyrics) > 30000:
        raise ValueError('歌词过长。')
    if operation['mode'] == 'yue' and not lyrics:
        raise ValueError('YuE2 重编曲需要歌词：从命令下一行填写，或附带 UTF-8 .txt 文件。')
    return source, lyrics, scores[0] if scores else None


def require_file(path, label):
    if not Path(path).is_file():
        raise ValueError(label + '尚未安装：' + str(path) + '；请运行 music/setup.sh。')


def run_stage(name, command, work, env, cwd=None):
    emit('akari_progress', text=name)
    log = work / ('stage-%02d.log' % len(list(work.glob('stage-*.log'))))
    started = time.monotonic()
    with log.open('w') as out:
        result = subprocess.run([str(x) for x in command], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError('%s失败（退出码%d），诊断日志：%s' % (name, result.returncode, log.name))
    return dict(stage=name, seconds=round(time.monotonic() - started, 2), log=log.name)


def align_and_mix(vocal, accompaniment, work, env):
    duration = probe(accompaniment)['duration']
    converted = probe(vocal)['duration']
    if abs(converted - duration) > 0.25:
        raise ValueError('转换人声与伴奏时长不一致（%.3f/%.3f秒），已停止混音。' % (converted, duration))
    mixed = work / 'akari-cover.wav'
    # Preserve tempo and pitch. Pad only codec rounding; limiter compensates its lookahead.
    run_stage('混合阿卡林人声与伴奏', ['ffmpeg', '-nostdin', '-v', 'error', '-y',
        '-i', vocal, '-i', accompaniment, '-filter_complex',
        '[0:a]apad[v];[v][1:a]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95:level=false:latency=true[a]',
        '-map', '[a]', '-t', str(duration), '-ar', '44100', '-ac', '2', '-c:a', 'pcm_s16le', mixed], work, env)
    return mixed


def perform(operation, task, output, cfg):
    if operation.get('kind') != 'sing' or operation.get('mode') not in ('cover', 'yue'):
        raise ValueError('未知唱歌操作。')
    seconds = operation.get('seconds')
    if seconds is not None and (type(seconds) is not int or not 5 <= seconds <= 120 or operation['mode'] != 'cover'):
        raise ValueError('无效的试唱片段参数。')
    source, lyrics, score = inputs(operation, task, cfg)
    metadata = probe(source)
    if metadata['duration'] > float(cfg.get('max_seconds', 600)) and seconds is None:
        raise ValueError('歌曲超过时长上限，请先用 /片段 30 试唱或提供短片段。')
    reference = Path(cfg['reference'])
    require_file(reference, '阿卡林参考音频')
    if not 3 <= probe(reference)['duration'] <= 25:
        raise ValueError('阿卡林参考音频应为3至25秒的干净独唱/语音。')
    require_file(cfg['svc_python'], '歌声转换环境')
    require_file(Path(cfg['svc_repo']) / 'inference.py', 'Seed-VC')
    if bool(cfg.get('svc_checkpoint')) != bool(cfg.get('svc_model_config')):
        raise ValueError('自定义 Seed-VC 权重和配置必须同时提供。')
    for key in ('svc_checkpoint', 'svc_model_config'):
        if cfg.get(key):
            require_file(cfg[key], key)
    if operation['mode'] == 'yue':
        require_file(cfg['yue_python'], 'YuE2环境')
        if score is None:
            require_file(cfg['sheetsage_python'], '转谱环境')
            require_file(Path(cfg['sheetsage_repo']) / 'infer.py', 'SheetSage2')
    work = task / 'music-work'
    work.mkdir(exist_ok=False)
    env = dict(os.environ, HF_HOME=str(Path(cfg['root']) / 'hf-cache'), PYTHONUNBUFFERED='1', OMP_NUM_THREADS='4')
    env.setdefault('HF_HUB_DOWNLOAD_TIMEOUT', '600')
    if cfg.get('cuda_visible_devices') is not None:
        env['CUDA_VISIBLE_DEVICES'] = str(cfg['cuda_visible_devices'])
    timings = []
    def run(name, cmd, cwd=None):
        timings.append(run_stage(name, cmd, work, env, cwd))
    # All music subprocesses share the supervisor's process group: cancellation kills descendants.
    with (Path(cfg['root']) / 'inference.lock').open('a') as lock:
        emit('akari_progress', text='等待音乐推理资源')
        fcntl.flock(lock, fcntl.LOCK_EX)
        run('检查歌声转换 GPU 环境', [cfg['svc_python'], '-c',
            'import torch,demucs; assert torch.cuda.is_available(), "CUDA unavailable"'])
        normalized = work / 'source.wav'
        cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', source]
        if seconds:
            cmd += ['-t', str(seconds)]
        run('读取歌曲音轨', cmd + ['-map', '0:a:0', '-ar', '44100', '-ac', '2', '-c:a', 'pcm_s16le', normalized])
        if operation['mode'] == 'yue':
            if score is None:
                run('SheetSage2 提取原曲旋律', [cfg['sheetsage_python'], Path(cfg['sheetsage_repo']) / 'infer.py',
                    normalized, '--output', work / 'score', '--melody-only'], cfg['sheetsage_repo'])
                score = work / 'score' / 'score.abc'
            abc = read_text(score)
            if not abc:
                raise ValueError('转谱未产生旋律。')
            request = dict(style=operation.get('style') or cfg['style'], lyrics=lyrics, abc=abc, cot='melody', seed=42)
            request_path = work / 'yue-request.json'
            request_path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
            config_path = work / 'runtime.json'
            # Only music settings, never the bot credentials.
            config_path.write_text(json.dumps(cfg), encoding='utf-8')
            run('YuE2 按旋律重新编曲演唱', [cfg['yue_python'], Path(__file__).parent / 'music/yue_generate.py',
                request_path, config_path, work / 'yue'])
            normalized = work / 'yue' / 'audio.flac'
            if probe(normalized)['duration'] > float(cfg.get('max_seconds', 600)):
                raise ValueError('YuE2 生成超过歌曲时长上限。')
        run('分离歌曲人声与伴奏', [cfg['svc_python'], '-m', 'demucs', '--two-stems', 'vocals',
            '-n', 'htdemucs', '--float32', '--clip-mode', 'clamp', '-d', 'cuda', '-o', work / 'stems', normalized])
        stems = work / 'stems' / 'htdemucs' / normalized.stem
        vocal, accompaniment = stems / 'vocals.wav', stems / 'no_vocals.wav'
        probe(vocal)
        probe(accompaniment)
        svc_command = [cfg['svc_python'], Path(cfg['svc_repo']) / 'inference.py',
            '--source', vocal, '--target', reference, '--output', work / 'converted',
            '--diffusion-steps', str(cfg.get('diffusion_steps', 40)), '--length-adjust', '1.0',
            '--inference-cfg-rate', '0.7', '--f0-condition', 'True', '--auto-f0-adjust', 'False',
            '--semi-tone-shift', '0', '--fp16', 'True']
        if cfg.get('svc_checkpoint'):
            svc_command += ['--checkpoint', cfg['svc_checkpoint'], '--config', cfg['svc_model_config']]
        run('转换为阿卡林歌声音色', svc_command, cfg['svc_repo'])
        converted = list((work / 'converted').glob('*.wav'))
        if len(converted) != 1:
            raise ValueError('歌声转换未生成唯一的人声音频。')
        mixed = align_and_mix(converted[0], accompaniment, work, env)
        encoded = work / 'akari-cover.mp3'
        run('导出可播放的翻唱音频', ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', mixed,
            '-c:a', 'libmp3lame', '-b:a', '192k', encoded])
        final_meta = probe(encoded)
        manifest = dict(operation=operation, source=str(source), source_audio=metadata,
            reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(), output_audio=final_meta,
            timings=timings, config=cfg)
        (work / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        for path in (encoded, mixed):
            dest = output / path.name
            shutil.copyfile(path, dest)
            emit('akari_artifact', path=str(dest))
        if operation['mode'] == 'yue':
            shutil.copyfile(score, output / 'melody.abc')
    label = 'YuE2 重编曲翻唱' if operation['mode'] == 'yue' else '保留原伴奏翻唱'
    return '阿卡林%s已生成（%.1f秒%s）。MP3 可试听，WAV 保留完整音质。文件已登记；发送情况请用 /进度 查询。' % (
        label, final_meta['duration'], '，试唱片段' if seconds else '')


def main():
    operation = json.loads(sys.argv[1])
    cfg = settings(os.environ.get('AKARI_MUSIC_CONFIG'))
    result = perform(operation, Path(os.environ['AKARI_TASK_DIR']).resolve(), Path(os.environ['AKARI_OUTPUT_DIR']).resolve(), cfg)
    emit('message_end', message=dict(role='assistant', stopReason='stop', content=[dict(type='text', text=result)]))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        text = '唱歌失败：' + type(exc).__name__ + ' ' + str(exc)[:500]
        emit('message_end', message=dict(role='assistant', stopReason='error', errorMessage=text,
                                       content=[dict(type='text', text=text)]))
        sys.exit(1)
