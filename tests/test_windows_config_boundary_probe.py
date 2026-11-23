"""临时探针：拆开 read_guarded_file 的 read_unavailable，定位真实失败点。

只在原生 Windows 上跑；结论进 CI 日志（失败输出），取证后删除本文件。
"""
import json
import ntpath
import os
import tempfile
import unittest
from pathlib import Path

import core.server as server
from core import platform_windows as pww
from core import windows_paths


def describe(exc):
    chain, current = [], exc
    for _ in range(6):
        if current is None:
            break
        chain.append(f'{type(current).__name__}(code={getattr(current, "code", None)!r}: {current})')
        current = current.__cause__ or current.__context__
    return ' <- '.join(chain)


def step(record, label, fn):
    try:
        value = fn()
    except BaseException as exc:
        record(f'{label} raised {describe(exc)}')
        return None
    record(f'{label} ok = {value!r}')
    return value


@unittest.skipUnless(os.name == 'nt', 'requires native Windows')
class ConfigBoundaryProbe(unittest.TestCase):
    def test_probe(self):
        lines = []

        def record(text):
            lines.append(text)

        # 不用 TemporaryDirectory：探针会故意保留打开的句柄，自动清理会先抛
        # WinError 32 把结论吞掉（实测）。这里用 mkdtemp 且不清理。
        temp = tempfile.mkdtemp(prefix='mp-boundary-')
        if True:
            flat = Path(temp) / 'config.json'
            flat.write_text('{}', encoding='utf-8')
            target = ntpath.abspath(str(flat))
            record(f'target={target!r}')
            record(f'package_dir={str(server._PACKAGE_DIR)!r}')

            step(record, '1.local_absolute', lambda: pww._local_absolute_target(target))
            api = step(record, '2.api', pww._api)
            step(record, '3.volume_info(C:\\)', lambda: api.volume_info('C:\\'))
            step(record, '3b.volume_info(D:\\)', lambda: api.volume_info('D:\\'))
            step(record, '4.fixed_drive',
                 lambda: pww._check_fixed_drive(api, pww._local_absolute_target(target)))
            from core.discovery import _WindowsPathMonitor, _windows_path_backend
            monitor = step(record, '5.monitor', lambda: _WindowsPathMonitor(_windows_path_backend()))
            step(record, '6.inspect',
                 lambda: windows_paths.inspect(target, backend=monitor, kind='file',
                                               private=True, single_link=True,
                                               trusted_boundary=str(Path(temp))))
            handle = step(record, '7.open_read_file', lambda: pww.open_read_file(target, backend=api))
            if handle is not None:
                try:
                    handle.close()
                    record('7b.handle closed')
                except BaseException as exc:
                    record(f'7b.close raised {describe(exc)}')
            step(record, '8.read_guarded_file',
                 lambda: pww.read_guarded_file(target, with_data=True,
                                               trusted_boundary=str(Path(temp))))

        self.fail('PROBE RESULTS\n' + '\n'.join(lines))
