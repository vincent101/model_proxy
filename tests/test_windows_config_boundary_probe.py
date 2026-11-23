"""临时探针：platform_tmp 换到 LOCALAPPDATA 后仍失败，定位到底差在哪。"""
import json
import ntpath
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.server as server
from core import platform_windows as pww
from core import windows_paths

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import platform_tmp  # noqa: E402


def describe(exc):
    chain, current = [], exc
    for _ in range(8):
        if current is None:
            break
        chain.append(f'{type(current).__name__}({getattr(current, "code", None)!r})')
        current = current.__cause__ or current.__context__
    return ' <- '.join(chain)


def acl(path):
    done = subprocess.run(['icacls', str(path)], capture_output=True, text=True)
    return [line.strip() for line in (done.stdout or '').splitlines()[1:] if ':' in line][:6]


def probe(record, label, base, patch_package):
    root = Path(base) / 'proj'
    (root / 'config').mkdir(parents=True, exist_ok=True)
    flat = root / 'config' / 'config.json'
    flat.write_text(json.dumps({'supplies': []}), encoding='utf-8')
    record(f'{label}.flat={str(flat)!r}')
    record(f'{label}.acl_flat={acl(flat)!r}')
    try:
        boundary = server._windows_config_boundary(flat)
        record(f'{label}.boundary={boundary!r}')
    except BaseException as exc:
        record(f'{label}.boundary raised {describe(exc)}')
        boundary = None
    if boundary is not None:
        try:
            value = pww.read_guarded_file(ntpath.abspath(str(flat)), with_data=True,
                                          trusted_boundary=boundary)
            record(f'{label}.read ok bytes={len(value.data)}')
        except BaseException as exc:
            record(f'{label}.read raised {describe(exc)}')
    try:
        if patch_package:
            with patch.object(server, '_PACKAGE_DIR', root):
                store = server.ConfigStore(flat)
        else:
            store = server.ConfigStore(flat)
        record(f'{label}.ConfigStore ok supplies={len(store._config.get("supplies", []))}')
    except BaseException as exc:
        record(f'{label}.ConfigStore raised {describe(exc)}')


@unittest.skipUnless(os.name == 'nt', 'requires native Windows')
class Probe(unittest.TestCase):
    def test_probe(self):
        lines = []

        def record(text):
            lines.append(text)

        record(f'localappdata={os.environ.get("LOCALAPPDATA")!r}')
        record(f'temp={tempfile.gettempdir()!r}')

        a = platform_tmp.TemporaryDirectory(prefix='mp-a-')
        probe(record, 'A.localappdata_no_patch', a.name, False)
        b = platform_tmp.TemporaryDirectory(prefix='mp-b-')
        probe(record, 'B.localappdata_patch', b.name, True)
        c = tempfile.mkdtemp(prefix='mp-c-')
        probe(record, 'C.temp_no_patch', c, False)
        record(f'C.temp_acl={acl(c)!r}')

        self.fail('PROBE RESULTS\n' + '\n'.join(lines))
