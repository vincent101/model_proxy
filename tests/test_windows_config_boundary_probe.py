"""临时探针：mkdtemp 与 mkdir 在同一父目录下产生的 ACL 差异。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

import core.server as server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def acl(path):
    done = subprocess.run(['icacls', str(path)], capture_output=True, text=True)
    return [line.strip() for line in (done.stdout or '').splitlines()[1:] if ':' in line][:8]


def full(record, label, parent):
    base = Path(parent)
    mt = Path(tempfile.mkdtemp(prefix='mp-mkdtemp-', dir=str(base)))
    mk = base / ('mp-mkdir-' + uuid.uuid4().hex[:8])
    mk.mkdir()
    for tag, root in (('mkdtemp', mt), ('mkdir', mk)):
        cfg_dir = root / 'config'
        cfg_dir.mkdir()
        flat = cfg_dir / 'config.json'
        flat.write_text(json.dumps({'supplies': []}), encoding='utf-8')
        record(f'{label}.{tag}.dir_acl={acl(root)!r}')
        record(f'{label}.{tag}.file_acl={acl(flat)!r}')
        try:
            store = server.ConfigStore(flat)
            record(f'{label}.{tag}.ConfigStore ok supplies={len(store._config.get("supplies", []))}')
        except BaseException as exc:
            record(f'{label}.{tag}.ConfigStore raised {type(exc).__name__}({getattr(exc, "code", None)!r})')


@unittest.skipUnless(os.name == 'nt', 'requires native Windows')
class Probe(unittest.TestCase):
    def test_probe(self):
        lines = []

        def record(text):
            lines.append(text)

        record(f'user={os.environ.get("USERNAME")!r}')
        record(f'localappdata_acl={acl(os.environ["LOCALAPPDATA"])!r}')
        record(f'userprofile_acl={acl(os.environ["USERPROFILE"])!r}')
        full(record, 'L', os.environ['LOCALAPPDATA'])
        full(record, 'P', os.environ['USERPROFILE'])
        self.fail('PROBE RESULTS\n' + '\n'.join(lines))
