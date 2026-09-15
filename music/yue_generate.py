#!/usr/bin/env python3
"""Run only in the isolated YuE2 environment; preserve upstream artifacts."""
import json
from pathlib import Path
import sys

from yue2 import YuE2Pipeline


def generate(request_path, config_path, output):
    request = json.loads(Path(request_path).read_text(encoding='utf-8'))
    cfg = json.loads(Path(config_path).read_text(encoding='utf-8'))
    with YuE2Pipeline.from_pretrained(
        cfg['yue_model'], vae=cfg['yue_vae'], device='cuda',
        revision=cfg.get('yue_revision'), vae_revision=cfg.get('yue_vae_revision'),
        memory_budget_gib=cfg.get('memory_budget_gib', 24),
        cache_dir=str(Path(cfg.get('root', Path(config_path).resolve().parent)) / 'hf-cache' / 'hub'),
    ) as pipe:
        song = pipe(**request)
        song.save_artifacts(output)
        if any(song.truncated.values()):
            raise RuntimeError('YuE2 达到生成长度上限；保留中间结果，未将截断歌曲标记为完成。')


if __name__ == '__main__':
    generate(*sys.argv[1:])
