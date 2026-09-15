#!/usr/bin/env python3
"""Create private local configs with absolute paths; never overwrite existing files."""
import json
import os
from pathlib import Path
import sys


def write_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print('保留已有文件：', path)
        return
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print('已创建：', path)


def main():
    base = Path(__file__).resolve().parents[1]
    config = json.loads((base / 'config.example.json').read_text())
    for key in ('llm_key_file', 'agent_script', 'agent_workdir', 'music_config', 'comfy_workflow',
                'comfy_workflow_plain', 'video_workflow', 'comfy_input_dir', 'comfy_output_dir'):
        config[key] = str(base / config[key])
    for name in ('state', 'secrets', 'work', 'music/songs', 'voice/ref'):
        (base / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    write_new(base / 'config.json', config)
    write_new(base / 'music/config.json', json.loads((base / 'music/config.example.json').read_text()))
    print('请填写 QQ 凭据、模型配置，并自行创建 secrets/llm.key。')
    print('手动运行 Bot 时请设置 AKARI_BASE 为本仓库的绝对路径。')


if __name__ == '__main__':
    main()
