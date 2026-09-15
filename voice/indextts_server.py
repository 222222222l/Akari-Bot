#!/usr/bin/env python3
# indextts_server.py - IndexTTS-2.5 TTS service for Akari bot (JAPANESE voice)
# Reference: akari_ref.wav (Japanese, Akari's voice) - native JA synthesis.
# Pipeline: bot's Chinese text -> local Qwen gateway -> natural Japanese
#           -> IndexTTS-2.5 infer(lang="JA") -> wav bytes (22050Hz int16)
# POST /tts {"text": "...", "duration_factor": 1.0} -> wav bytes
# GET  /health -> {"ok": true, ...}
# Run with: cd index-tts && uv run ../indextts_server.py
import os, sys, io, json, time, ssl, logging, urllib.request, re

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(BASE, 'index-tts')
MODEL_DIR = os.path.join(BASE, 'models', 'IndexTTS-2.5')
REF_WAV = os.path.join(BASE, 'ref', 'akari_ref.wav')
BOT_CFG_PATH = os.path.join(os.environ.get('AKARI_BASE', os.path.dirname(BASE)), 'config.json')

sys.path.insert(0, REPO)
os.environ.setdefault('CUDA_VISIBLE_DEVICES', '6')

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('indextts')

from indextts.infer_v2_5 import IndexTTS2
log.info('loading IndexTTS-2.5 from %s', MODEL_DIR)
TTS = IndexTTS2(cfg_path=os.path.join(MODEL_DIR, 'config.yaml'),
                model_dir=MODEL_DIR, use_bf16=True, use_qwen_emo=True)
log.info('model loaded')

with open(BOT_CFG_PATH, encoding='utf-8') as f:
    BOTCFG = json.load(f)
LLM_URL = BOTCFG['llm_base'].rstrip('/') + '/v1/chat/completions'
with open(BOTCFG['llm_key_file'], encoding='utf-8') as f:
    LLM_KEY = f.read().strip()
LLM_MODEL = BOTCFG.get('model', 'Qwen3.8-27B')


def _llm_chat(messages, temperature=0.6, max_tokens=512, timeout=120):
    body = json.dumps({'model': LLM_MODEL, 'messages': messages,
                       'temperature': temperature, 'max_tokens': max_tokens,
                       'chat_template_kwargs': {'enable_thinking': False}}).encode()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(LLM_URL, data=body, method='POST',
                                 headers={'Authorization': 'Bearer ' + LLM_KEY,
                                          'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        d = json.loads(r.read().decode() or '{}')
    c = ((d.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
    c = re.sub(r'<think.*?>.*?</think.*?>', '', c, flags=re.S)
    return c.strip()


TRANSLATE_PROMPT = ('あなたは声優のセリフ台本を日本語に訳す担当です。'
                    'ユーザーのテキスト（中国語または日本語）を、13歳の元気な少女（赤座灯里）の口調の自然な日本語にしてください。'
                    '・普段の口語体、かわいい感じ（〜だよ、〜かも、〜だぜ などの語尾をうまく使う）'
                    '・漢字・ひらがな・カタカナは使い分けて自然な日本語にする（カタカナ一択は不要）'
                    '・翻訳した日本語テキストだけを出力し、引用符・説明・前置きは付けません。')


def to_japanese(text):
    t = ' '.join((text or '').split())
    if not t:
        return t, False
    # fast path: already contains Japanese kana
    if any('\u3040' <= ch <= '\u30ff' for ch in t):
        return t, False
    raw = ''
    for attempt in (1, 2):
        raw = _llm_chat([{'role': 'system', 'content': TRANSLATE_PROMPT},
                         {'role': 'user', 'content': t}])
        raw = raw.strip().strip('\u300c\u300d"\'`')
        if raw and any('\u3040' <= ch <= '\u30ff' for ch in raw):
            return raw, True
        log.info('translate attempt=%d unsuitable: %s', attempt, raw[:100])
    return (raw or t), True


app = FastAPI()


class TtsReq(BaseModel):
    text: str
    duration_factor: Optional[float] = None
    emo_text: Optional[str] = None  # 显式情感描述，如"平静自然"、"开心"（不传则按内容自动分析）


@app.get('/health')
def health():
    return {'ok': True, 'model': 'IndexTTS-2.5', 'lang': 'JA', 'ref': 'akari'}


@app.post('/tts')
def tts(req: TtsReq):
    text = ' '.join((req.text or '').split())[:240]
    if not text:
        return Response(status_code=400, content=b'{"error":"empty text"}')
    t0 = time.time()
    ja, translated = to_japanese(text)
    log.info('tts in=%r -> ja=%r (llm=%s)', text[:60], ja[:60], translated)
    try:
        factor = float(req.duration_factor if req.duration_factor is not None else 1.15)
        factor = min(2.0, max(0.5, factor))
        emo = (req.emo_text or '').strip() or None  # None => 用主文本让 QwenEmotion 自动分析情感
        result = TTS.infer(spk_audio_prompt=REF_WAV, text=ja,
                           output_path=None, lang='JA',
                           duration_factor=factor,
                           text_normalization=True,
                           use_emo_text=True,
                           emo_text=emo)
        if not result:
            return Response(status_code=500, content=b'{"error":"no audio"}')
        sr, wav = result  # int16 numpy
        wav = np.asarray(wav)
        if wav.ndim == 2:
            wav = wav[:, 0] if wav.shape[1] >= 1 else wav[0]
        buf = io.BytesIO()
        sf.write(buf, wav, int(sr), format='WAV', subtype='PCM_16')
        data = buf.getvalue()
    except Exception as e:
        log.exception('tts failed: %r', e)
        return Response(status_code=500, content=('tts error: %r' % e).encode())
    dur = len(wav) / sr
    log.info('tts ok chars=%d audio=%.1fs rtf=%.2f bytes=%d',
             len(text), dur, (time.time() - t0) / max(1e-6, dur), len(data))
    return Response(content=data, media_type='audio/wav')


if __name__ == '__main__':
    port = int(os.environ.get('VOICE_PORT', '8903'))
    uvicorn.run(app, host='127.0.0.1', port=port, log_level='info')