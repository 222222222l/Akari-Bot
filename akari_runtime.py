"""Best-effort observability; a failed status write must never stop the bot."""
import hashlib
import json
import os
from pathlib import Path
import threading
import time


def fingerprint(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


class Runtime:
    def __init__(self, state, config):
        self.path = Path(state) / 'runtime.json'
        self.lock = threading.Lock()
        self.data = dict(pid=os.getpid(), started=time.time(), updated=0, gateway='connecting',
                         config_revision=fingerprint(config))

    def set(self, **values):
        with self.lock:
            self.data.update(values, updated=time.time())
            try:
                tmp = self.path.with_suffix('.tmp')
                tmp.write_text(json.dumps(self.data), encoding='utf-8')
                os.replace(tmp, self.path)
            except OSError:
                pass

    def run(self, stop):
        self.set()
        while not stop.wait(5):
            self.set()
