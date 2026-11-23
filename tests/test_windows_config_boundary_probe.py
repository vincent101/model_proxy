"""临时探针：直接复刻 platform_tmp 的用法，对比手写 mkdir。"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import core.server as server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import platform_tmp  # noqa: E402


def acl(path):
    done = subprocess.run(['icacls', str(path)], capture_output=True, text=True)
    return [line.strip() for line in (done.stdout or '').splitlines()[1:] if ':' in line][:6]


def attempt(record, label, directory):
    p = Path(directory) / 'config.json'
    p.write_text(json.dumps({'supplies': []}), encoding='utf-8')
    record(f'{label}.dir={str(directory)!r}')
    record(f'{label}.dir_acl={acl(directory)!r}')
    record(f'{label}.file_acl={acl(p)!r}')
    try:
        store = server.ConfigStore(p)
        record(f'{label}.ok supplies={len(store._config.get("supplies", []))}')
    except BaseException as exc:
        record(f'{label}.raised {type(exc).__name__}({getattr(exc, "code", None)!r})')


@unittest.skipUnless(os.name == 'nt', 'requires native Windows')
class Probe(unittest.TestCase):
    def test_probe(self):
        lines = []

        def record(text):
            lines.append(text)

        record(f'base_dir={platform_tmp.base_dir()!r}')
        td = platform_tmp.TemporaryDirectory(prefix='mp-helper-')
        try:
            attempt(record, 'helper', td.name)
        finally:
            td.cleanup()
        manual = Path(os.environ['LOCALAPPDATA']) / 'mp-manual-probe'
        manual.mkdir(exist_ok=True)
        attempt(record, 'manual', manual)
        record(f'manual_mode={oct(manual.stat().st_mode & 0o777)!r}')
        self.fail('PROBE RESULTS\n' + '\n'.join(lines))
