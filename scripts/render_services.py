#!/usr/bin/env python3
"""Render systemd units for the current checkout without installing/restarting anything."""
import argparse
import getpass
import json
from pathlib import Path
import re
import sys


def quoted(value):
    return json.dumps(str(value).replace('%', '%%'), ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', default=getpass.getuser())
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--port', type=int, default=18763)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]*\$?', args.user) or not 1 <= args.port <= 65535:
        parser.error('用户名或端口无效')
    base = Path(__file__).resolve().parents[1]
    out = base / '.local/systemd'
    out.mkdir(parents=True, exist_ok=True)
    for name, script in [('bot', 'akari_bot.py'), ('admin', 'akari_admin.py')]:
        command = quoted(Path(args.python).resolve()) + ' ' + quoted(base / script)
        if name == 'admin':
            command += ' --port ' + str(args.port)
        unit = f'''[Unit]
Description=Akari {name.title()}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=10

[Service]
Type=simple
User={args.user}
WorkingDirectory={str(base).replace('%', '%%')}
Environment={quoted('AKARI_BASE=' + str(base))}
ExecStart={command}
Restart=on-failure
RestartSec=10
UMask=0077
StandardOutput=append:{base}/state/{'console' if name == 'bot' else 'admin-service'}.log
StandardError=append:{base}/state/{'console' if name == 'bot' else 'admin-service'}.log

[Install]
WantedBy=multi-user.target
'''
        # systemd log paths are not shell strings; percent specifiers must be escaped.
        unit = unit.replace('append:' + str(base), 'append:' + str(base).replace('%', '%%'))
        (out / f'akari-{name}.service').write_text(unit)
    print('已生成服务文件：', out)
    print('检查内容后，按 docs/deployment.md 安装；此脚本没有操作系统服务。')


if __name__ == '__main__':
    main()
