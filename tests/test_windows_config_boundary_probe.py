"""临时探针：确认「用 icacls 收紧 ACL 的项目根」能否让真实 Windows 读到配置。

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


def harden(path):
    """把目录 ACL 收成仅当前用户：移除继承 + 只授当前用户完全控制。"""
    user = os.environ.get('USERNAME', '')
    done = subprocess.run(['icacls', str(path), '/inheritance:r',
                           '/grant:r', f'{user}:(OI)(CI)F'],
                          capture_output=True, text=True)
    return done.returncode, (done.stdout or '') + (done.stderr or '')


@unittest.skipUnless(os.name == 'nt', 'requires native Windows')
class PrivateRootProbe(unittest.TestCase):
    def test_probe(self):
        lines = []

        def record(text):
            lines.append(text)

        temp = tempfile.mkdtemp(prefix='mp-probe-')
        root = Path(temp) / 'proj'
        (root / 'config').mkdir(parents=True)
        flat = root / 'config' / 'config.json'
        flat.write_text(json.dumps({'supplies': []}), encoding='utf-8')
        record(f'root={str(root)!r}')

        record(f'icacls(proj)={harden(root)[0]}')
        record(f'icacls(config)={harden(root / "config")[0]}')
        record(f'icacls(file)={harden(flat)[0]}')
        code, text = harden(root)
        record(f'icacls output sample={text.strip().splitlines()[:2]!r}')

        target = ntpath.abspath(str(flat))
        try:
            value = pww.read_guarded_file(target, with_data=True)
            record(f'A.read ok bytes={len(value.data)}')
        except BaseException as exc:
            record(f'A.read raised {describe(exc)}')

        try:
            with patch.object(server, '_PACKAGE_DIR', root):
                store = server.ConfigStore(flat)
                record(f'B.ConfigStore ok supplies={len(store._config.get("supplies", []))}')
        except BaseException as exc:
            record(f'B.ConfigStore raised {describe(exc)}')

        self.fail('PROBE RESULTS\n' + '\n'.join(lines))
