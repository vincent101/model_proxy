"""临时探针：找出 Windows 上「ACL 被判 private」的可写目录位置。

只在原生 Windows 上跑；结论进 CI 日志（失败输出），取证后删除本文件。
"""
import json
import ntpath
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.server as server
from core import platform_windows as pww


def describe(exc):
    chain, current = [], exc
    for _ in range(6):
        if current is None:
            break
        chain.append(f'{type(current).__name__}(code={getattr(current, "code", None)!r})')
        current = current.__cause__ or current.__context__
    return ' <- '.join(chain)


def acl_lines(path):
    done = subprocess.run(['icacls', str(path)], capture_output=True, text=True)
    keep = [line.strip() for line in (done.stdout or '').splitlines()[1:] if ':' in line]
    return keep[:8]


def try_read(record, label, base):
    root = Path(base) / 'proj'
    (root / 'config').mkdir(parents=True, exist_ok=True)
    flat = root / 'config' / 'config.json'
    flat.write_text(json.dumps({'supplies': []}), encoding='utf-8')
    record(f'{label}.root={str(root)!r}')
    record(f'{label}.acl={acl_lines(root)!r}')
    try:
        with patch.object(server, '_PACKAGE_DIR', root):
            store = server.ConfigStore(flat)
        record(f'{label}.ConfigStore ok supplies={len(store._config.get("supplies", []))}')
    except BaseException as exc:
        record(f'{label}.ConfigStore raised {describe(exc)}')


@unittest.skipUnless(os.name == 'nt', 'requires native Windows')
class AclLocationProbe(unittest.TestCase):
    def test_probe(self):
        lines = []

        def record(text):
            lines.append(text)

        record(f'username={os.environ.get("USERNAME")!r}')
        record(f'temp={tempfile.gettempdir()!r}')
        record(f'localappdata={os.environ.get("LOCALAPPDATA")!r}')
        record(f'userprofile={os.environ.get("USERPROFILE")!r}')

        try_read(record, 'A.temp', tempfile.mkdtemp(prefix='mp-probe-'))
        local = Path(os.environ['LOCALAPPDATA']) / 'mp-probe-local'
        try_read(record, 'B.localappdata', local)
        profile = Path(os.environ['USERPROFILE']) / 'mp-probe-profile'
        try_read(record, 'C.userprofile', profile)
        workspace = Path(os.environ.get('GITHUB_WORKSPACE') or os.getcwd())
        try_read(record, 'D.workspace', workspace)

        self.fail('PROBE RESULTS\n' + '\n'.join(lines))
