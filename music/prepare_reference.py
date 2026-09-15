#!/usr/bin/env python3
"""Prepare the configured singing reference once, without creating/sending a QQ task."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from akari_music import settings
from akari_sing import run_stage, probe
from akari_music_reference import prepare_reference

parser=argparse.ArgumentParser()
parser.add_argument('--reference',type=Path)
args=parser.parse_args()
cfg=settings()
if args.reference:
    cfg['reference']=str(args.reference.resolve())
cfg['reference_extract_vocals']=True
work=Path(cfg['root'])/'validation'/('reference-'+str(time.time_ns()))
work.mkdir(parents=True)
env=dict(os.environ,CUDA_VISIBLE_DEVICES=str(cfg.get('cuda_visible_devices','0')),HF_HOME=str(Path(cfg['root'])/'hf-cache'),OMP_NUM_THREADS='4')
with (Path(cfg['root'])/'inference.lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    result=prepare_reference(cfg,work,env,lambda name,cmd,cwd=None:run_stage(name,cmd,work,env,cwd))
print(json.dumps(dict(reference=str(result),audio=probe(result)),ensure_ascii=False),flush=True)
