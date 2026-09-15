"""Prepare a reusable clean short vocal reference from the configured recording."""
import hashlib
import json
import math
from pathlib import Path
import shutil
import wave
from array import array


def prepare_reference(cfg, work, env, run):
    from akari_sing import probe
    source = Path(cfg['reference'])
    duration = probe(source)['duration']
    if not cfg.get('reference_extract_vocals', False):
        if 3 <= duration <= 25:
            return source
        raise ValueError('直接参考音频须为3至25秒；使用整首歌曲请开启自动提取参考人声。')
    length = int(cfg.get('reference_seconds', 18))
    if not 5 <= length <= 25:
        raise ValueError('参考片段长度应在5至25秒之间。')
    signature = hashlib.sha256(source.read_bytes() + json.dumps({'seconds':length,'extract':True,'version':1},sort_keys=True).encode()).hexdigest()
    cache = Path(cfg['root']) / 'reference-cache' / signature
    final = cache / 'reference.wav'
    if final.is_file() and (cache / 'manifest.json').is_file():
        if 3 <= probe(final)['duration'] <= 25:
            return final
    cache.mkdir(parents=True, exist_ok=True)
    # Caller holds the global inference lock, including first-time preparation.
    normalized = work / 'reference-source.wav'
    run('读取默认音色参考歌曲', ['ffmpeg','-nostdin','-v','error','-y','-i',source,'-t','180',
        '-ar','44100','-ac','2','-c:a','pcm_s16le',normalized])
    run('提取参考歌曲的干净人声（首次缓存）', [cfg['svc_python'],'-m','demucs','--two-stems','vocals',
        '-n','htdemucs','--float32','--clip-mode','clamp','-d','cuda','-o',work/'reference-stems',normalized])
    vocal = work / 'reference-stems/htdemucs/reference-source/vocals.wav'
    analysis = work / 'reference-analysis.wav'
    run('分析参考人声有效片段', ['ffmpeg','-nostdin','-v','error','-y','-i',vocal,'-ar','8000','-ac','1','-c:a','pcm_s16le',analysis])
    with wave.open(str(analysis)) as f:
        data = array('h', f.readframes(f.getnframes()))
        rate = f.getframerate()
    energies = []
    for offset in range(0,len(data)-rate+1,rate):
        frame = data[offset:offset+rate]
        energies.append(sum(float(v)*v for v in frame)/rate)
    if len(energies) < length or max(energies,default=0) < 100:
        raise ValueError('参考歌曲未提取到足够清晰的人声。')
    # Prefer sustained vocals over intros, silence, or a single loud spike.
    def score(start):
        values = energies[start:start+length]
        active = sum(v > max(100, max(energies)*.015) for v in values)
        return active, sum(math.log1p(v) for v in values)
    start = max(range(0,len(energies)-length+1), key=score)
    candidate = cache / 'reference.tmp.wav'
    run('缓存默认阿卡林歌声音色', ['ffmpeg','-nostdin','-v','error','-y','-ss',str(start),'-i',vocal,
        '-t',str(length),'-ar','44100','-ac','1','-c:a','pcm_s16le',candidate])
    probe(candidate)
    candidate.replace(final)
    (cache/'manifest.json').write_text(json.dumps(dict(source=str(source),source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        start_seconds=start,duration_seconds=length,method='Demucs htdemucs vocals + sustained-energy selection'),ensure_ascii=False,indent=2))
    return final
