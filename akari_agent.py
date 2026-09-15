#!/usr/bin/env python3
"""pi bridge: stream events, isolate stderr, retry only before tool execution."""
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import ssl


def model_plan():
    root = Path(os.environ.get('PI_CODING_AGENT_DIR', str(Path.home() / '.pi/agent')))
    try:
        settings = json.loads((root / 'settings.json').read_text())
    except (OSError, ValueError):
        settings = {}
    provider = os.environ.get('AKARI_AGENT_PROVIDER', 'sglang')
    preferred = os.environ.get('AKARI_AGENT_MODEL') or settings.get('defaultModel', 'Qwen3.8-27B')
    try:
        catalog = json.loads((root / 'models.json').read_text())['providers'].get(provider, {})
        known = [m['id'] for m in catalog.get('models', [])]
        base = os.environ.get('AKARI_AGENT_BASE') or catalog.get('baseUrl', '')
        if base and base.startswith(('https://', 'http://')):
            ctx = ssl.create_default_context(cafile=os.environ.get('SSL_CERT_FILE') or None)
            headers = {}
            if os.environ.get('AKARI_AGENT_KEY_FILE'):
                key = Path(os.environ['AKARI_AGENT_KEY_FILE']).read_text().strip()
                headers['Authorization'] = 'Bearer ' + key
            req = urllib.request.Request(base.rstrip('/') + '/models', headers=headers)
            with urllib.request.urlopen(req, timeout=8, context=ctx) as res:
                live = [m['id'] for m in json.load(res).get('data', [])]
            candidates = [m for m in [preferred] + known if m in live]
            if candidates:
                return provider, list(dict.fromkeys(candidates))[:3]
    except (OSError, ValueError, KeyError):
        pass
    return provider, [preferred]


def main():
    if len(sys.argv) < 2:
        print('usage: agent_run.sh prompt_file [session_id] [timeout_sec]', file=sys.stderr)
        return 2
    prompt_file = Path(sys.argv[1]).resolve()
    prompt = prompt_file.read_text(encoding='utf-8')
    if not prompt.strip():
        return 2
    session = sys.argv[2] if len(sys.argv) > 2 else ''
    budget = float(sys.argv[3]) if len(sys.argv) > 3 else 1800
    if os.getpgrp() != os.getpid():
        os.setsid()
    os.environ['PATH'] = os.environ.get('PATH', '') + ':' + str(Path.home() / '.local/bin')
    ca = '/mnt/data/sglang-qwen38-service/pki/server/root-ca.crt'
    if Path(ca).is_file():
        os.environ.setdefault('SSL_CERT_FILE', ca)
        os.environ.setdefault('NODE_EXTRA_CA_CERTS', ca)
    started = time.monotonic()
    provider, models = model_plan()
    if len(models) == 1:
        models = models * 2
    stream_json = os.environ.get('AKARI_AGENT_FORMAT') == 'json'
    task_dir = Path(os.environ.get('AKARI_TASK_DIR', str(prompt_file.parent)))
    error_path = task_dir / 'pi.stderr.log'
    seen_tool, last_code, final = False, 1, ''
    for attempt, model in enumerate(models):
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            return 124
        if attempt:
            time.sleep(min(5, remaining))
        cmd = [os.environ.get('AKARI_PI_BIN', 'pi'), '--provider', provider, '--model', model,
               '--mode', 'json', '-p', '-e', str(Path(__file__).with_name('akari_context.mjs'))]
        if session:
            cmd += ['--session-id', session]
            if os.environ.get('AKARI_SESSION_DIR'):
                cmd += ['--session-dir', os.environ['AKARI_SESSION_DIR']]
        else:
            cmd += ['--no-session']
        cmd += ['--', prompt]
        print('pi attempt=%s provider=%s model=%s' % (attempt + 1, provider, model), file=sys.stderr, flush=True)
        events, last_error = queue.Queue(maxsize=128), ''
        with error_path.open('ab') as err:
            offset = err.tell()
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err)
            def pump():
                try:
                    for line in proc.stdout:
                        events.put(line)
                finally:
                    proc.stdout.close()
                    events.put(None)
            threading.Thread(target=pump, daemon=True).start()
            while True:
                if time.monotonic() - started >= budget:
                    print('pi budget exhausted', file=sys.stderr, flush=True)
                    os.killpg(os.getpgrp(), signal.SIGTERM)
                    return 124
                try:
                    line = events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    break
                if stream_json:
                    sys.stdout.buffer.write(line)
                    sys.stdout.buffer.flush()
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('type') == 'tool_execution_start':
                    seen_tool = True
                if event.get('type') == 'message_end':
                    msg = event.get('message') or {}
                    if msg.get('role') == 'assistant':
                        if msg.get('stopReason') in ('error', 'aborted'):
                            last_error = str(msg.get('errorMessage') or msg['stopReason'])
                        else:
                            last_error = ''
                        content = msg.get('content') or []
                        text = '\n'.join(x.get('text', '') for x in content if x.get('type') == 'text')
                        if text:
                            final = text
            try:
                last_code = proc.wait(timeout=max(0.1, budget - (time.monotonic() - started)))
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgrp(), signal.SIGTERM)
                return 124
        if last_code == 0 and not last_error:
            if not stream_json:
                print(final)
            return 0
        if last_code == 0:
            last_code = 1
        with error_path.open('rb') as err:
            err.seek(offset)
            failure = (err.read(16384).decode(errors='replace') + last_error).lower()
        retryable = any(x in failure for x in ('503', 'no_available_workers', 'circuit', 'model not found', 'unknown model'))
        if seen_tool or not retryable:
            break
        print('retrying model before any tool execution', file=sys.stderr, flush=True)
    if not stream_json and final:
        print(final)
    return last_code


if __name__ == '__main__':
    sys.exit(main())
