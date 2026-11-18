"""Bounded real Win32/Wine path benchmark on an owned fake directory only.

Run with Windows Python -I -S -B --supervise <fresh-result.json>. The parent
reaps only its own timed-out child. No service, real config or fake ACLs.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

SOURCE = Path(__file__).resolve().parent.parent


def worker(root):
    sys.path.insert(0, str(SOURCE))
    from core import discovery, windows_acl, windows_paths

    class CountingACL:
        def __init__(self):
            self.real = windows_acl._NativeBackend()
            self.reads = 0
        def read_observation(self, handle, kind, required):
            self.reads += 1
            return self.real.read_observation(handle, kind, required)

    directory = Path(root)
    for name in ('a', 'b', 'c', 'd'):
        directory = directory / name
        os.mkdir(directory, 0o700)
    counting = CountingACL()
    backend = windows_paths.WindowsPathBackend(
        win32=discovery._WindowsNativeBridge(), acl_backend=counting)
    results = []
    for label, target, kind in (
            ('existing_directory', str(directory), 'directory'),
            ('missing_file', str(directory / 'missing.json'), 'file')):
        before = counting.reads
        start = time.monotonic()
        with windows_paths.inspect(target, backend=backend, kind=kind, private=True,
                                   single_link=(kind == 'file')) as lease:
            inspect_elapsed = time.monotonic() - start
            inspect_reads = counting.reads - before
            before = counting.reads
            start = time.monotonic()
            lease.revalidate()
            revalidate_elapsed = time.monotonic() - start
            revalidate_reads = counting.reads - before
            results.append({'case': label, 'exists': lease.evidence.exists,
                'components': len(lease.evidence.components),
                'missing_components': len(lease.evidence.missing),
                'inspect_acl_reads': inspect_reads, 'inspect_seconds': round(inspect_elapsed, 4),
                'revalidate_acl_reads': revalidate_reads,
                'revalidate_seconds': round(revalidate_elapsed, 4)})
    hashes = {}
    for name in ('windows_paths.py', 'windows_acl.py'):
        hashes[name] = hashlib.sha256((SOURCE / 'core' / name).read_bytes()).hexdigest()
    return {'environment': 'Wine/Whisky, not native Windows', 'cases': results, 'sha256': hashes}


def supervise(result_file):
    root = tempfile.mkdtemp(prefix='mp-path-bench-')
    start = time.monotonic()
    output = {'completion': 'unknown', 'timeout_seconds': 120}
    try:
        try:
            result = subprocess.run([sys.executable, '-I', '-S', '-B', __file__, '--worker', root],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            output['returncode'] = result.returncode
            if result.returncode == 0:
                output['data'] = json.loads(result.stdout.decode('utf-8'))
                output['completion'] = 'owned child exited and reaped'
            else:
                output['completion'] = 'worker_failed'
                output['stderr'] = result.stderr.decode('utf-8', 'replace')[-2000:]
        except subprocess.TimeoutExpired:
            # subprocess.run killed and reaped only the child it launched.
            output['completion'] = 'owned child timed out and was reaped'
    finally:
        try:
            shutil.rmtree(root)
            output['owned_fixture_cleanup'] = 'complete'
        except OSError:
            output['owned_fixture_cleanup'] = 'unknown'
        output['wall_seconds'] = round(time.monotonic() - start, 4)
    with open(result_file, 'x', encoding='utf-8') as stream:
        json.dump(output, stream, indent=2)


if __name__ == '__main__':
    if os.name != 'nt' or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit('Windows Python -I -S -B required')
    if len(sys.argv) != 3:
        raise SystemExit('supply --worker <owned-root> or --supervise <fresh-result>')
    if sys.argv[1] == '--worker':
        print(json.dumps(worker(sys.argv[2])))
    elif sys.argv[1] == '--supervise':
        supervise(sys.argv[2])
    else:
        raise SystemExit('unknown mode')
