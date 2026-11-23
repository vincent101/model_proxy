"""临时探针：定位「tempdir 里的配置在 Windows 上读不到」的真实原因。

只在原生 Windows 上跑；结论直接进 CI 日志（失败输出），取证后删除本文件。
要回答两件事：
  A. 现状（配置在 %TEMP%）失败在哪个具体错误码；
  B. 若把项目根 patch 到该临时目录（boundary = 项目根），是否能读通。
"""
import json
import ntpath
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.server as server
from core import platform_windows as pw


def _describe(exc):
    chain = []
    current = exc
    for _ in range(6):
        if current is None:
            break
        chain.append(f'{type(current).__name__}(code={getattr(current, "code", None)!r})')
        current = current.__cause__ or current.__context__
    return ' <- '.join(chain)


@unittest.skipUnless(os.name == 'nt', 'requires native Windows')
class ConfigBoundaryProbe(unittest.TestCase):
    def test_probe(self):
        lines = []

        def record(text):
            lines.append(text)

        with tempfile.TemporaryDirectory(prefix='mp-boundary-') as temp:
            record(f'temp={temp!r}')
            record(f'package_dir={str(server._PACKAGE_DIR)!r}')
            record(f'localappdata={os.environ.get("LOCALAPPDATA")!r}')

            flat = Path(temp) / 'config.json'
            flat.write_text('{}', encoding='utf-8')
            try:
                record(f'A.boundary={server._windows_config_boundary(flat)!r}')
            except BaseException as exc:
                record(f'A.boundary raised {_describe(exc)}')
            try:
                pw.read_guarded_file(ntpath.abspath(str(flat)), with_data=True)
                record('A.read ok')
            except BaseException as exc:
                record(f'A.read raised {_describe(exc)}')

            root = Path(temp) / 'proj'
            (root / 'config').mkdir(parents=True)
            nested = root / 'config' / 'config.json'
            nested.write_text(json.dumps({'supplies': []}), encoding='utf-8')
            try:
                with patch.object(server, '_PACKAGE_DIR', root):
                    record(f'B.boundary={server._windows_config_boundary(nested)!r}')
            except BaseException as exc:
                record(f'B.boundary raised {_describe(exc)}')
            try:
                value = pw.read_guarded_file(ntpath.abspath(str(nested)), with_data=True,
                                             trusted_boundary=str(root))
                record(f'B.read ok bytes={len(value.data)}')
            except BaseException as exc:
                record(f'B.read raised {_describe(exc)}')

            try:
                with patch.object(server, '_PACKAGE_DIR', root):
                    store = server.ConfigStore(nested)
                record(f'B.ConfigStore ok supplies={len(store._config.get("supplies", []))}')
            except BaseException as exc:
                record(f'B.ConfigStore raised {_describe(exc)}')

        self.fail('PROBE RESULTS\n' + '\n'.join(lines))
