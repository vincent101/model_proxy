"""Mac config-link integration regression, against a temporary source copy only.

Never run the live suite while core/ or the test harness is changing. After the
coordinator freezes both, invoke (with two explicitly selected, unused ports):

  python3.13 tests/test_mac_config_links_integration.py --live-frozen \
      --proxy-port PORT --mock-port PORT

The opt-in asserts operator confirmation, not a source freeze. It copies only
core Python files, VERSION, test code and same-prefix guard fixtures, never production data.
CLI and service are real copied source; the upstream is an HTTP test double.
No OS-level sandbox is claimed. Test processes must be owned direct children;
on abnormal runner exit the temporary root is retained for manual inspection.
Run --fixture-only now to check the path/decoy fixture without starting either
CLI or service. Explicit skips exit 77; failures exit nonzero. Ordinary test
discovery skips the live suite outside its runner. The real CLI is dispatch.main;
its actual helper runs as a child. The upstream is a fake local HTTP server.
"""
from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import io
import importlib.util
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import select
try:
    import pty
    import termios
except ImportError:  # Unsupported platform: report explicit skip, not success.
    pty = termios = None
import inspect
import types
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
# LiveMacCliTests patches the subprocess module globally while CLI on runs.
# Bypass that patch for our own lsof probes and explicitly managed direct child.
_RAW_POPEN = subprocess.Popen
ADMIN = 'fake-live-admin-token'
CLIENT = 'fake-link-client'
UPSTREAM = 'fake-link-upstream'


def make_layout(root: Path, original: bytes, decoy: bytes):
    """Return raw relative spellings; never normalize before exercising them."""
    config = root / 'config'
    (config / 'inner').mkdir(parents=True, exist_ok=True)
    (root / 'other').mkdir(exist_ok=True)
    target = config / 'business.json'
    target.write_bytes(original)
    target.chmod(0o600)
    bait = root / 'other' / 'business.json'
    bait.write_bytes(decoy)
    bait.chmod(0o600)
    (root / 'file-link.json').symlink_to('config/business.json')
    (root / 'dir-link').symlink_to('config', target_is_directory=True)
    (root / 'abs-hop').symlink_to(root / 'config', target_is_directory=True)
    (root / 'multi-hop').symlink_to('abs-hop', target_is_directory=True)
    (root / 'other' / 'pivot').symlink_to('../config/inner', target_is_directory=True)
    return target, bait, {
        'final': 'file-link.json',
        'parent': 'dir-link/business.json',
        'multi-relative-absolute': 'multi-hop/business.json',
        'dotdot': 'other/pivot/../business.json',
    }


@contextlib.contextmanager
def in_directory(directory):
    old = os.open('.', os.O_RDONLY)
    try:
        os.chdir(directory)
        yield
    finally:
        os.fchdir(old)
        os.close(old)


class FixtureOnly(unittest.TestCase):
    def test_os_component_semantics_not_lexical_normalization(self):
        with tempfile.TemporaryDirectory(prefix='mp-links-fixture-', dir=str(Path('/tmp').resolve())) as tmp:
            root = Path(tmp)
            target, bait, paths = make_layout(root, b'actual', b'decoy')
            with in_directory(root):
                for path in paths.values():
                    self.assertEqual(Path(path).read_bytes(), b'actual')
                spelling = paths['dotdot']
                self.assertEqual(Path(os.path.normpath(spelling)).read_bytes(), b'decoy')
            self.assertEqual(target.read_bytes(), b'actual')
            self.assertEqual(bait.read_bytes(), b'decoy')
            self.assertTrue((root / 'other/pivot').is_symlink())

    def test_port_validation_fails_before_socket_or_staging(self):
        with mock.patch.object(sys, 'platform', 'darwin'), \
             mock.patch.object(sys, 'version_info', (3, 13)), \
             mock.patch.object(shutil, 'which', return_value='/fake/lsof'), \
             mock.patch.object(socket, 'socket', side_effect=AssertionError('must not bind')):
            for ports in ((18889, 30000), (30000, 18889), (30000, 30000), (0, 30000)):
                with self.subTest(ports=ports), self.assertRaises(ValueError):
                    live_runner(*ports)

    def test_raw_popen_is_not_replaced_by_cli_capture_patch(self):
        with mock.patch.object(subprocess, 'Popen', return_value=None):
            self.assertIsNot(_RAW_POPEN, subprocess.Popen)

    def test_no_token_instrumentation_blocks_and_detects_attempt(self):
        case = MacLinksIntegration('test_cli_final_file_link')
        case._control = lambda *_args: (1, '', 'config_unverified')
        case._assert_no_token('status')
        def attempted(*_args):
            try:
                socket.socket.sendall(None, b'X-Proxy-Admin-Token: ' + ADMIN.encode())
            except AssertionError:
                pass
            return 1, '', 'config_unverified'
        case._control = attempted
        with self.assertRaisesRegex(AssertionError, 'attempted to send'):
            case._assert_no_token('status')

    def test_guard_policy_rejects_production_and_non_loopback_without_connecting(self):
        path = HERE / 'fixtures/test_mac_config_links_guard.py'
        spec = importlib.util.spec_from_file_location('links_guard_fixture', path)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        for address in (('127.0.0.1', 18889), ('8.8.8.8', 30001),
                        ('example.com', 30001), ('127.0.0.1', 30003)):
            with self.subTest(address=address), self.assertRaises(PermissionError):
                guard.endpoint(address, (30001, 30002))
        self.assertEqual(guard.endpoint(('127.0.0.1', 30001), (30001, 30002)), 30001)
        with tempfile.TemporaryDirectory(prefix='mp-links-policy-', dir=str(Path('/tmp').resolve())) as tmp:
            root = Path(tmp).resolve()
            self.assertTrue(guard.path_allowed(root / 'config/fake.json', root, (), write=True))
            self.assertFalse(guard.path_allowed(Path.home() / 'production-config.json', root, (), write=False))
            self.assertFalse(guard.path_allowed('/tmp/model_proxy.log', root, (), write=True))

    def test_guard_in_fake_child_blocks_outside_file_and_nested_process(self):
        # Only a disposable Python fixture, never project CLI/service or network.
        with tempfile.TemporaryDirectory(prefix='mp-links-guard-', dir=str(Path('/tmp').resolve())) as tmp:
            outer = Path(tmp).resolve()
            root = outer / 'mp-regression-fake'
            (root / 'guard').mkdir(parents=True)
            (root / 'home').mkdir()
            outside = outer / 'outside-fake.json'
            outside.write_text('fake-canary')
            shutil.copyfile(HERE / 'fixtures/test_mac_config_links_guard.py',
                            root / 'guard/test_mac_config_links_guard.py')
            shutil.copyfile(HERE / 'fixtures/test_mac_config_links_fdguard.py',
                            root / 'guard/test_mac_config_links_fdguard.py')
            shutil.copyfile(HERE / 'fixtures/test_mac_config_links_sitecustomize.py',
                            root / 'guard/sitecustomize.py')
            env = {'HOME': str(root / 'home'), 'PATH': '/usr/bin:/bin', 'TMPDIR': str(root),
                   'PYTHONPATH': str(root / 'guard'), 'PYTHONNOUSERSITE': '1',
                   'PYTHONDONTWRITEBYTECODE': '1', 'MP_LINKS_GUARD_ROOT': str(root),
                   'MP_LINKS_GUARD_PORTS': '30001,30002', 'MP_LINKS_GUARD_ROLE': 'helper',
                   'MP_LINKS_OWNER_PID': str(os.getpid()), 'OUTSIDE_FAKE': str(outside)}
            script = """import os, subprocess
assert os.environ.get('MP_LINKS_GUARD_ACTIVE') == '1'
try:
    open(os.environ['OUTSIDE_FAKE']).read()
    raise AssertionError('outside read allowed')
except PermissionError:
    pass
fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
os.close(fd)
os.symlink(os.environ['OUTSIDE_FAKE'], 'outside-alias')
try:
    open('outside-alias').read()
    raise AssertionError('symlink escape allowed')
except PermissionError:
    pass
try:
    os.system('echo must-not-run')
    raise AssertionError('shell allowed')
except PermissionError:
    pass
try:
    subprocess.Popen(['/usr/bin/false'])
    raise AssertionError('nested process allowed')
except PermissionError:
    pass
print('fixture-guard-ok')
"""
            result = subprocess.run([sys.executable, '-B', '-c', script], env=env, cwd=root,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'fixture-guard-ok')
            self.assertEqual(outside.read_text(), 'fake-canary')
            self.assertEqual(len(list((root / 'guard').glob('attest-*'))), 1)

    def test_dirfd_guard_in_root_success_and_external_canary_refusal(self):
        if sys.platform == 'darwin':
            try:
                import fcntl
            except ImportError:
                self.skipTest('F_GETPATH unavailable')
            if not hasattr(fcntl, 'F_GETPATH'):
                self.skipTest('F_GETPATH unavailable')
        elif not sys.platform.startswith('linux') or not Path('/proc/self/fd').is_dir():
            self.skipTest('verifiable descriptor path unavailable')
        # FDs inherited into the fake child are intentionally unknown to guard.
        # No production path, service, network, or project code is used.
        with tempfile.TemporaryDirectory(prefix='mp-links-dirfd-', dir=str(Path('/tmp').resolve())) as tmp:
            outer = Path(tmp).resolve()
            root = outer / 'mp-regression-fake'
            inside, outside = root / 'inside', outer / 'outside'
            for directory in (root / 'guard', root / 'home', inside, outside):
                directory.mkdir(parents=True, exist_ok=True)
            (inside / 'data').write_text('inside-data')
            (root / 'actual/inner').mkdir(parents=True)
            (root / 'actual/legit').write_text('kernel-target')
            (inside / 'legit').write_text('lexical-decoy')
            (inside / 'pivot').symlink_to('../actual/inner', target_is_directory=True)
            (inside / 'local-link').symlink_to('data')
            (inside / 'escape').symlink_to('../../outside', target_is_directory=True)
            canary = outside / 'canary'
            canary.write_text('outside-canary')
            (outside / 'outside-link').symlink_to('canary')
            for name, dest in (('test_mac_config_links_guard.py', 'test_mac_config_links_guard.py'),
                               ('test_mac_config_links_fdguard.py', 'test_mac_config_links_fdguard.py'),
                               ('test_mac_config_links_sitecustomize.py', 'sitecustomize.py')):
                shutil.copyfile(HERE / 'fixtures' / name, root / 'guard' / dest)
            ext_fd = os.open(outside, os.O_RDONLY | os.O_DIRECTORY)
            unknown_inside_fd = os.open(inside, os.O_RDONLY | os.O_DIRECTORY)
            env = {'HOME': str(root / 'home'), 'PATH': '/usr/bin:/bin', 'TMPDIR': str(root),
                   'PYTHONPATH': str(root / 'guard'), 'PYTHONNOUSERSITE': '1',
                   'PYTHONDONTWRITEBYTECODE': '1', 'MP_LINKS_GUARD_ROOT': str(root),
                   'MP_LINKS_GUARD_PORTS': '30001,30002', 'MP_LINKS_GUARD_ROLE': 'helper',
                   'MP_LINKS_OWNER_PID': str(os.getpid()), 'FAKE_OUTER': str(outer),
                   'EXT_FD': str(ext_fd), 'UNKNOWN_INSIDE_FD': str(unknown_inside_fd)}
            script = """import os
assert os.environ.get('MP_LINKS_GUARD_ACTIVE') == '1'
external, unknown_inside = int(os.environ['EXT_FD']), int(os.environ['UNKNOWN_INSIDE_FD'])
good = os.open('inside', os.O_RDONLY | os.O_DIRECTORY)
assert os.stat('data', dir_fd=good).st_size == len('inside-data')
fd = os.open('data', os.O_RDONLY, dir_fd=good)
assert os.read(fd, 64) == b'inside-data'
os.close(fd)
assert os.readlink('local-link', dir_fd=good) == 'data'
fd = os.open('pivot/../legit', os.O_RDONLY, dir_fd=good)
assert os.read(fd, 64) == b'kernel-target'
os.close(fd)
dup = os.dup(good)
os.rename('data', 'renamed', src_dir_fd=good, dst_dir_fd=dup)
os.link('renamed', 'hard', src_dir_fd=good, dst_dir_fd=dup)
os.replace('hard', 'replaced', src_dir_fd=dup, dst_dir_fd=good)
os.unlink('replaced', dir_fd=good)
assert os.stat('renamed', dir_fd=good).st_size == len('inside-data')
ancestor = os.open(os.environ['FAKE_OUTER'], os.O_RDONLY | os.O_DIRECTORY)
def denied(fn):
    try:
        fn()
    except PermissionError:
        return
    raise AssertionError('dir_fd escape accepted')
for fd in (external, unknown_inside):
    denied(lambda: os.open('canary', os.O_RDONLY, dir_fd=fd))
    denied(lambda: os.stat('canary', dir_fd=fd))
    denied(lambda: os.readlink('outside-link', dir_fd=fd))
    denied(lambda: os.unlink('canary', dir_fd=fd))
    denied(lambda: os.mkdir('new-directory', dir_fd=fd))
    denied(lambda: os.symlink('canary', 'new-link', dir_fd=fd))
    denied(lambda: os.chmod('canary', 0o600, dir_fd=fd))
    denied(lambda: os.utime('canary', None, dir_fd=fd))
    denied(lambda: os.stat(os.environ['MP_LINKS_GUARD_ROOT'] + '/inside/data', dir_fd=fd))
denied(lambda: os.open('escape/canary', os.O_RDONLY, dir_fd=good))
denied(lambda: os.stat('outside/canary', dir_fd=ancestor))
denied(lambda: os.open('outside/canary', os.O_RDONLY, dir_fd=ancestor))
denied(lambda: os.readlink('outside/outside-link', dir_fd=ancestor))
denied(lambda: os.rename('outside/canary', 'stolen', src_dir_fd=ancestor, dst_dir_fd=good))
denied(lambda: os.replace('renamed', 'outside/canary', src_dir_fd=good, dst_dir_fd=ancestor))
denied(lambda: os.link('outside/canary', 'stolen', src_dir_fd=ancestor, dst_dir_fd=good))
denied(lambda: os.unlink('outside/canary', dir_fd=ancestor))
denied(lambda: os.rename('canary', 'stolen', src_dir_fd=external, dst_dir_fd=good))
denied(lambda: os.replace('renamed', 'canary', src_dir_fd=good, dst_dir_fd=external))
denied(lambda: os.link('canary', 'stolen', src_dir_fd=external, dst_dir_fd=good))
assert os.readlink('escape', dir_fd=good) == '../../outside'
os.close(dup)
os.close(good)
os.dup2(external, good)  # unknown source must not inherit stale trusted binding
denied(lambda: os.stat('canary', dir_fd=good))
os.close(good)
os.close(ancestor)
print('fake-dirfd-ok')
"""
            try:
                result = subprocess.run([sys.executable, '-B', '-c', script], env=env, cwd=root,
                                        pass_fds=(ext_fd, unknown_inside_fd), text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            finally:
                os.close(ext_fd)
                os.close(unknown_inside_fd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'fake-dirfd-ok')
            self.assertEqual(canary.read_text(), 'outside-canary')
            self.assertEqual((inside / 'renamed').read_text(), 'inside-data')
            self.assertFalse((inside / 'stolen').exists())

    def test_worker_guard_precedes_business_import_and_rejects_unguarded_children(self):
        # A synthetic worker and synthetic helper exercise the same bootstrap
        # contract; neither imports any model-proxy business code.
        with tempfile.TemporaryDirectory(prefix='mp-links-worker-', dir=str(Path('/tmp').resolve())) as tmp:
            root = Path(tmp).resolve() / 'mp-regression-fake'
            for directory in (root / 'guard', root / 'home', root / 'core/cli'):
                directory.mkdir(parents=True, exist_ok=True)
            for name, dest in (('test_mac_config_links_guard.py', 'test_mac_config_links_guard.py'),
                               ('test_mac_config_links_fdguard.py', 'test_mac_config_links_fdguard.py'),
                               ('test_mac_config_links_sitecustomize.py', 'sitecustomize.py')):
                shutil.copyfile(HERE / 'fixtures' / name, root / 'guard' / dest)
            (root / 'business_probe.py').write_text("import os\nassert os.environ.get('MP_LINKS_WORKER_GUARD_ACTIVE') == '1'\n")
            helper = root / 'core/cli/helper_entry.py'
            helper.write_text("import os\nassert os.environ.get('MP_LINKS_GUARD_ACTIVE') == '1'\nprint('fake-helper-guarded')\n")
            env = {'HOME': str(root / 'home'), 'PATH': '/usr/bin:/bin', 'TMPDIR': str(root),
                   'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                   'MP_LINKS_GUARD_ROOT': str(root), 'MP_LINKS_GUARD_PORTS': '30001,30002'}
            script = """import os, sys, subprocess
root = os.environ['MP_LINKS_GUARD_ROOT']
sys.path.insert(0, root + '/guard')
os.environ.pop('MP_LINKS_WORKER_GUARD_ACTIVE', None)
import test_mac_config_links_guard as guard
guard.install(root, (30001, 30002), 'worker')
sys.path.insert(0, root)
import business_probe
child = dict(os.environ, PYTHONPATH=root+'/guard', MP_LINKS_GUARD_ROLE='helper',
             MP_LINKS_OWNER_PID=str(os.getpid()), MP_LINKS_OWNED_HISTORY='[]')
entry = root + '/core/cli/helper_entry.py'
for key in ('PYTHONPATH', 'PYTHONNOUSERSITE', 'PYTHONDONTWRITEBYTECODE', 'MP_LINKS_GUARD_ROOT',
            'MP_LINKS_GUARD_PORTS', 'MP_LINKS_GUARD_ROLE', 'MP_LINKS_OWNER_PID', 'MP_LINKS_OWNED_HISTORY'):
    bad = dict(child)
    del bad[key]
    try:
        subprocess.Popen([sys.executable, entry], env=bad)
        raise AssertionError('missing bootstrap env accepted: ' + key)
    except PermissionError:
        pass
for flag in ('-I', '-S', '-E'):
    try:
        subprocess.Popen([sys.executable, flag, entry], env=child)
        raise AssertionError('guard-disabling flag accepted')
    except PermissionError:
        pass
try:
    subprocess.Popen([sys.executable, entry])
    raise AssertionError('implicit child env accepted')
except PermissionError:
    pass
ok = subprocess.run([sys.executable, entry], env=child, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=10)
assert ok.returncode == 0, ok.stderr
assert ok.stdout.strip() == 'fake-helper-guarded'
print('fake-worker-bootstrap-ok')
"""
            result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', script], env=env, cwd=root,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'fake-worker-bootstrap-ok')
            attest = list((root / 'guard').glob('attest-*'))
            self.assertEqual(len(attest), 1)
            self.assertEqual(attest[0].read_text().splitlines(), ['helper'])

    def test_owned_stale_pid_zero_probe_allowed_but_signals_and_reuse_denied(self):
        # Synthetic worker/services only. The first Popen child really exits;
        # signal checks use sys.audit so no nonzero signal is sent to any PID.
        with tempfile.TemporaryDirectory(prefix='mp-links-history-', dir=str(Path('/tmp').resolve())) as tmp:
            root = Path(tmp).resolve() / 'mp-regression-fake'
            for directory in (root / 'guard', root / 'home', root / 'core/cli'):
                directory.mkdir(parents=True, exist_ok=True)
            for name, dest in (('test_mac_config_links_guard.py', 'test_mac_config_links_guard.py'),
                               ('test_mac_config_links_fdguard.py', 'test_mac_config_links_fdguard.py'),
                               ('test_mac_config_links_sitecustomize.py', 'sitecustomize.py')):
                shutil.copyfile(HERE / 'fixtures' / name, root / 'guard' / dest)
            (root / 'core/discovery.py').write_text(
                "import os\ndef process_start_identity(pid):\n return {'kind':'reused'} if os.environ.get('FAKE_REUSED') else None\n")
            service = root / 'core/cli/model_proxy.py'
            service.write_text("""import os, signal, sys
sys.path.insert(0, os.environ['MP_LINKS_GUARD_ROOT'])
if os.environ.get('FAKE_PREVIOUS'):
    old = int(os.environ['FAKE_PREVIOUS'])
    try:
        os.kill(old, 0)
    except ProcessLookupError:
        assert not os.environ.get('FAKE_REUSED')
        print('owned-stale-esrch')
    except PermissionError:
        assert os.environ.get('FAKE_REUSED')
        print('reused-pid-denied')
    else:
        raise AssertionError('unexpected process at stale fake PID')
    for pid, signum in ((old, signal.SIGTERM), (os.getppid(), 0)):
        try:
            sys.audit('os.kill', pid, signum)
            raise AssertionError('nonzero or non-owned probe accepted')
        except PermissionError:
            pass
print('fake-service-finished')
""")
            env = {'HOME': str(root / 'home'), 'PATH': '/usr/bin:/bin', 'TMPDIR': str(root),
                   'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                   'MP_LINKS_GUARD_ROOT': str(root), 'MP_LINKS_GUARD_PORTS': '30001,30002'}
            script = """import json, os, subprocess, sys
root = os.environ['MP_LINKS_GUARD_ROOT']
sys.path.insert(0, root + '/guard')
import test_mac_config_links_guard as guard
owned = []
def history():
    return json.dumps([{'pid': p.pid, 'start': {'kind':'fake-start'}} for p in owned if p.poll() is not None],
                      sort_keys=True, separators=(',', ':'))
guard.install(root, (30001,30002), 'worker', owned_history=history)
entry = root + '/core/cli/model_proxy.py'
child_env = dict(os.environ, PYTHONPATH=root+'/guard', MP_LINKS_GUARD_ROLE='service',
                 MP_LINKS_OWNER_PID=str(os.getpid()), MP_LINKS_OWNED_HISTORY=history())
first = subprocess.Popen([sys.executable, entry], env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
owned.append(first)
out, err = first.communicate(timeout=10)
assert first.returncode == 0, err
assert out == b'fake-service-finished\\n'
child_env.update(MP_LINKS_OWNED_HISTORY=history(), FAKE_PREVIOUS=str(first.pid))
for reused in (False, True):
    run_env = dict(child_env)
    if reused: run_env['FAKE_REUSED'] = '1'
    proc = subprocess.Popen([sys.executable, entry], env=run_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Keep the approved history stable while this child runs.
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 0, err
    assert (b'reused-pid-denied' if reused else b'owned-stale-esrch') in out
forged = dict(child_env, MP_LINKS_OWNED_HISTORY='[]')
try:
    subprocess.Popen([sys.executable, entry], env=forged)
    raise AssertionError('removed owned history accepted')
except PermissionError:
    pass
print('owned-history-guard-ok')
"""
            result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', script], env=env, cwd=root,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'owned-history-guard-ok')

    def test_fake_service_reexec_preserves_guard_and_attestation(self):
        # Fake local script only: prove same-PID re-exec does not drop the guard.
        with tempfile.TemporaryDirectory(prefix='mp-links-exec-', dir=str(Path('/tmp').resolve())) as tmp:
            root = Path(tmp).resolve() / 'mp-regression-fake'
            (root / 'guard').mkdir(parents=True)
            (root / 'home').mkdir()
            (root / 'core/cli').mkdir(parents=True)
            shutil.copyfile(HERE / 'fixtures/test_mac_config_links_guard.py',
                            root / 'guard/test_mac_config_links_guard.py')
            shutil.copyfile(HERE / 'fixtures/test_mac_config_links_fdguard.py',
                            root / 'guard/test_mac_config_links_fdguard.py')
            shutil.copyfile(HERE / 'fixtures/test_mac_config_links_sitecustomize.py',
                            root / 'guard/sitecustomize.py')
            script = root / 'core/cli/model_proxy.py'
            script.write_text("""import os, sys
assert os.environ.get('MP_LINKS_GUARD_ACTIVE') == '1'
if not os.environ.get('FAKE_REEXEC_DONE'):
    env = dict(os.environ, FAKE_REEXEC_DONE='1')
    for key in ('PYTHONPATH', 'PYTHONNOUSERSITE', 'PYTHONDONTWRITEBYTECODE', 'MP_LINKS_GUARD_ROOT',
                'MP_LINKS_GUARD_PORTS', 'MP_LINKS_GUARD_ROLE', 'MP_LINKS_OWNER_PID', 'MP_LINKS_OWNED_HISTORY'):
        bad = dict(env)
        del bad[key]
        try:
            os.execve(sys.executable, [sys.executable, __file__], bad)
            raise AssertionError('unguarded re-exec accepted: ' + key)
        except PermissionError:
            pass
    for flag in ('-I', '-S', '-E'):
        try:
            os.execve(sys.executable, [sys.executable, flag, __file__], env)
            raise AssertionError('guard-disabling re-exec accepted')
        except PermissionError:
            pass
    saved = os.environ.pop('PYTHONPATH')
    try:
        try:
            os.execv(sys.executable, [sys.executable, __file__])
            raise AssertionError('unguarded inherited re-exec accepted')
        except PermissionError:
            pass
    finally:
        os.environ['PYTHONPATH'] = saved
    os.execve(sys.executable, [sys.executable, __file__], env)
print('fake-reexec-ok')
""")
            env = {'HOME': str(root / 'home'), 'PATH': '/usr/bin:/bin', 'TMPDIR': str(root),
                   'PYTHONPATH': str(root / 'guard'), 'PYTHONNOUSERSITE': '1',
                   'PYTHONDONTWRITEBYTECODE': '1', 'MP_LINKS_GUARD_ROOT': str(root),
                   'MP_LINKS_GUARD_PORTS': '30001,30002', 'MP_LINKS_GUARD_ROLE': 'service',
                   'MP_LINKS_OWNER_PID': str(os.getpid()), 'MP_LINKS_OWNED_HISTORY': '[]'}
            result = subprocess.run([sys.executable, str(script)], env=env, cwd=root,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'fake-reexec-ok')
            attest = list((root / 'guard').glob('attest-*'))
            self.assertEqual(len(attest), 1)
            self.assertEqual(attest[0].read_text().splitlines(), ['service', 'service'])

    def test_live_worker_source_installs_guard_before_business_import(self):
        import ast
        tree = ast.parse(Path(__file__).read_text(encoding='utf-8'))
        live = next(node for node in tree.body if isinstance(node, ast.If)
                    and isinstance(node.test, ast.Name) and node.test.id == 'LIVE')
        install_line = min(node.lineno for node in ast.walk(live)
                           if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                           and isinstance(node.func.value, ast.Name)
                           and node.func.value.id == 'cooperative_guard' and node.func.attr == 'install')
        import_lines = [node.lineno for node in ast.walk(live)
                        if isinstance(node, ast.ImportFrom) and node.module
                        and (node.module == 'test_cli_live' or node.module == 'core'
                             or node.module.startswith('core.'))]
        self.assertTrue(import_lines)
        self.assertTrue(all(install_line < line for line in import_lines))

    def test_fake_upstream_guard_checks_same_socket_and_waits_for_accept(self):
        # This tests the guard callback only; the live case separately uses
        # native peer evidence and must still reject a wrong owner.
        spec = importlib.util.spec_from_file_location(
            'links_guard_peer_fixture', HERE / 'fixtures/test_mac_config_links_guard.py')
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        core, discovery, platform = (types.ModuleType(name) for name in
                                     ('core', 'core.discovery', 'core.platform_process'))
        discovery.process_start_identity = lambda pid: {'kind': 'fake', 'pid': pid}
        discovery.process_executable = lambda _pid: '/fake/python'
        observed = []
        def verify(sock, pid, start, image, *, timeout):
            observed.append((sock, pid, start, image, timeout))
        platform.verify_connected_peer = verify
        core.discovery, core.platform_process = discovery, platform
        class FakeSocket:
            def getpeername(self): return ('127.0.0.1', 30002)
        sock = FakeSocket()
        _listener, peer = guard.service_checks(12345, 30002)
        with mock.patch.object(guard.os, 'getppid', return_value=12345), \
             mock.patch.dict(sys.modules, {'core': core, 'core.discovery': discovery,
                                         'core.platform_process': platform}):
            self.assertTrue(peer(sock))
            self.assertEqual(observed, [(sock, 12345, {'kind': 'fake', 'pid': 12345}, '/fake/python', 2.0)])
            platform.verify_connected_peer = mock.Mock(side_effect=RuntimeError('wrong peer'))
            self.assertFalse(peer(sock))

    def test_unsupported_environment_is_skip_exit_77_not_success(self):
        args = ['test', '--live-frozen', '--proxy-port', '30001', '--mock-port', '30002']
        with mock.patch.object(sys, 'argv', args), \
             mock.patch.dict(globals(), {'live_runner': mock.Mock(side_effect=unittest.SkipTest('unsupported'))}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(), 77)
        self.assertIn('SKIP', output.getvalue())

    @unittest.skipIf(pty is None or termios is None, 'PTY unavailable')
    def test_fake_dispatch_tty_pipe_frames_and_reload_bookkeeping(self):
        # This is a synthetic dispatcher, not the product or a service. It only
        # self-tests this file's PTY/FD capture and result accounting.
        with tempfile.TemporaryDirectory(prefix='mp-links-driver-', dir=str(Path('/tmp').resolve())) as tmp:
            root = Path(tmp).resolve()
            (root / 'guard').mkdir()
            case = MacLinksIntegration('test_cli_final_file_link')
            case.root, case.helpers, case.pipe_observations = root, [], []
            state = {'frames': b'writing\nyes\n', 'tty': True, 'reloads': 0}
            class FakeControl:
                def __init__(self, *_args, **_kwargs): pass
                def reload(self):
                    state['reloads'] += 1
                    return 0
            class FakeAuthority: pass
            dispatch = types.ModuleType('core.cli.dispatch')
            control = types.ModuleType('core.cli.control')
            authority = types.ModuleType('core.cli.authority')
            cli = types.ModuleType('core.cli')
            core = types.ModuleType('core')
            control.MacControl, authority.MacInstallationAuthority = FakeControl, FakeAuthority
            dispatch._drain_marker_pipe = lambda _fd, _proof: state['frames']
            def fake_main(_argv, *, root, control):
                if sys.stdin.isatty() != state['tty'] or sys.stdout.isatty() != state['tty']:
                    raise AssertionError('wrong fake terminal mode')
                if state['tty'] and sys.stdin.readline().strip() != 'YES':
                    raise AssertionError('fake confirmation not delivered')
                child = types.SimpleNamespace(pid=123456)
                case.helpers.append(child)
                case.pipe_observations.append((4, (1, 2, 3, 4, 5, 0, 0, 1), (4,)))
                (root / 'guard/attest-123456').write_text('helper\n')
                dispatch._drain_marker_pipe(4, None)
                if state['frames']:
                    control.reload()
                    print('fixture-success')
                    return 0
                print('fixture-denied')
                return 1
            dispatch.main = fake_main
            cli.dispatch, core.cli = dispatch, cli
            modules = {'core': core, 'core.cli': cli, 'core.cli.dispatch': dispatch,
                       'core.cli.control': control, 'core.cli.authority': authority}
            with mock.patch.dict(sys.modules, modules), mock.patch.dict(globals(), {'iso': root}):
                rc, out, frames, reloads = case._cli_switch('fake', tty=True, answer=b'YES')
                self.assertEqual((rc, frames, reloads), (0, [b'writing\nyes\n'], ['reload']))
                self.assertIn('fixture-success', out)
                state.update(frames=b'', tty=False)
                rc, out, frames, reloads = case._cli_switch('fake', tty=False)
                self.assertEqual((rc, frames, reloads), (1, [b''], []))
                self.assertIn('fixture-denied', out)
                self.assertEqual(state['reloads'], 1)

    def test_child_attestation_must_exist_match_role_and_be_private(self):
        with tempfile.TemporaryDirectory(prefix='mp-links-attest-', dir=str(Path('/tmp').resolve())) as tmp:
            root = Path(tmp).resolve()
            (root / 'guard').mkdir()
            case = MacLinksIntegration('test_cli_final_file_link')
            child = types.SimpleNamespace(pid=123456, poll=lambda: 0)
            path = root / 'guard/attest-123456'
            with mock.patch.dict(globals(), {'iso': root}):
                with self.assertRaisesRegex(AssertionError, 'not attested'):
                    case._await_guard_attestation(child, 'helper', timeout=.1)
                path.write_text('helper\n')
                path.chmod(0o600)
                self.assertEqual(case._await_guard_attestation(child, 'helper'), ['helper'])
                with self.assertRaisesRegex(AssertionError, 'role mismatch'):
                    case._await_guard_attestation(child, 'service')
                path.chmod(0o644)
                with self.assertRaisesRegex(AssertionError, 'protection unknown'):
                    case._await_guard_attestation(child, 'helper')
                path.unlink()
                target = root / 'fake-other'
                target.write_text('helper\n')
                path.symlink_to(target)
                with self.assertRaisesRegex(AssertionError, 'protection unknown'):
                    case._await_guard_attestation(child, 'helper')

    def test_cleanup_uses_owned_handle_and_does_not_follow_links(self):
        if not shutil.rmtree.avoids_symlink_attacks:
            self.skipTest('fd-based rmtree unavailable')
        with tempfile.TemporaryDirectory(prefix='mp-links-cleanup-', dir=str(Path('/tmp').resolve())) as tmp:
            root = Path(tmp)
            test_dir = root / 'owned-test'
            test_dir.mkdir()
            outside = root / 'outside'
            outside.write_text('must survive')
            (test_dir / 'link').symlink_to(outside)
            identity = test_dir.lstat()
            signals = []
            class Child:
                pid = 123456789  # never sent to the OS
                def poll(self): return None
                def wait(self, timeout): signals.append(('wait', timeout))
            class Evidence:
                @staticmethod
                def signal_verified(pid, start, image):
                    signals.append(('verified', pid, start, image))
            child = Child()
            case = MacLinksIntegration('test_cli_final_file_link')
            case.children = [child]
            case.child_identity = {child: ('fake-start', 'fake-image')}
            case.temp = type('FakeTemp', (), {'name': str(test_dir)})()
            case._temp_identity = (identity.st_dev, identity.st_ino)
            with mock.patch.dict(globals(), {'platform_process': Evidence}):
                case._cleanup()
            self.assertEqual(signals, [('verified', child.pid, 'fake-start', 'fake-image'), ('wait', 10)])
            self.assertFalse(test_dir.exists())
            self.assertEqual(outside.read_text(), 'must survive')

    def test_cleanup_retains_directory_on_unknown_child_or_changed_identity(self):
        with tempfile.TemporaryDirectory(prefix='mp-links-cleanup-', dir=str(Path('/tmp').resolve())) as tmp:
            test_dir = Path(tmp) / 'owned-test'
            test_dir.mkdir()
            identity = test_dir.lstat()
            class Child:
                pid = 123456789  # never sent to the OS
                def poll(self): return None
            case = MacLinksIntegration('test_cli_final_file_link')
            case.children = [Child()]
            case.child_identity = {}
            case.temp = type('FakeTemp', (), {'name': str(test_dir)})()
            case._temp_identity = (identity.st_dev, identity.st_ino)
            with self.assertRaisesRegex(AssertionError, 'identity unknown'):
                case._cleanup()
            self.assertTrue(test_dir.is_dir())
            case.children = []
            case._temp_identity = (identity.st_dev, identity.st_ino + 1)
            with self.assertRaisesRegex(AssertionError, 'identity changed'):
                case._cleanup()
            self.assertTrue(test_dir.is_dir())


# Refuse importing copied service/CLI unless the operator explicitly opted in
# and the test was started from inside the temporary project by our runner.
LIVE = os.environ.get('MP_LINKS_LIVE_CHILD') == '1'
Base = None
if LIVE:
    iso = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
    if (HERE.parent != iso / 'project' or not (iso / '.model-proxy-isolation').is_file()
            or not Path(os.environ.get('HOME', '/invalid')).resolve().is_relative_to(iso)):
        raise RuntimeError('live tests require the newly staged isolated project')
    sys.path.insert(0, str(iso / 'guard'))
    import test_mac_config_links_guard as cooperative_guard
    _active_case = None
    def _guard_listener(port):
        if _active_case is None:
            return False
        if port == _active_case.mock_port:
            return _active_case.mock_server.socket.fileno() >= 0
        if port == _active_case.port:
            if getattr(_active_case, '_fake_peer', False):
                child = getattr(_active_case, '_fake_peer_child', None)
                return child is not None and child.poll() is None and cooperative_guard.owned_listener(child.pid, port)
            _active_case._assert_owned_listener()
            return True
        return False
    def _guard_peer(sock):
        try:
            peer = sock.getpeername()
            if peer[1] != _active_case.port:
                return True
            if getattr(_active_case, '_fake_peer', False):
                child = _active_case._fake_peer_child
                start, image = _active_case.child_identity[child]
                platform_process.verify_connected_peer(sock, child.pid, start, image, timeout=2.0)
                return True
            children = [p for p in _active_case.children if p.poll() is None]
            if len(children) != 1:
                return False
            child = children[0]
            start, image = _active_case.child_identity[child]
            platform_process.verify_connected_peer(sock, child.pid, start, image, timeout=2.0)
            return True
        except (OSError, RuntimeError, ValueError, KeyError, AttributeError):
            return False
    def _guard_owned_history():
        # Only Popen children of the current test, conclusively exited with a
        # captured OS start identity, may be queried via signal 0 by a new
        # service. This never authorizes sending a nonzero signal.
        records = []
        if _active_case is not None:
            for child in _active_case.children:
                start, image = _active_case.child_identity.get(child, (None, None))
                if child.poll() is not None and isinstance(start, dict) and start and isinstance(image, str) and image:
                    records.append({'pid': child.pid, 'start': start})
        records.sort(key=lambda value: value['pid'])
        return json.dumps(records, sort_keys=True, separators=(',', ':'))
    def _guard_owned_pid(pid):
        return (pid == os.getpid() or (_active_case is not None
                and any(p.pid == pid and p.poll() is None for p in _active_case.children)))
    def _guard_owned_signal(pid, signal_number):
        if _active_case is None:
            return False
        children = (_active_case.children + getattr(_active_case, 'helpers', [])
                    + getattr(_active_case, 'probes', []))
        return any(p.pid == pid and (signal_number == 0 or p.poll() is None) for p in children)
    os.environ.pop('MP_LINKS_WORKER_GUARD_ACTIVE', None)
    cooperative_guard.install(iso, (int(os.environ['MP_LINKS_PROXY_PORT']),
                                     int(os.environ['MP_LINKS_MOCK_PORT'])), 'worker',
                              listener_check=_guard_listener, peer_check=_guard_peer,
                              allowed_pid=_guard_owned_pid, allowed_signal=_guard_owned_signal,
                              owned_history=_guard_owned_history)
    if os.environ.get('MP_LINKS_WORKER_GUARD_ACTIVE') != '1':
        raise RuntimeError('worker guard did not install before imports')
    sys.path.insert(0, str(HERE))
    from test_cli_live import LiveMacCliTests as Base
    from core import platform_process
    from core.discovery import process_executable, process_start_identity


class LocalUpstream(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size <= 0 or size > 65536:
                raise ValueError('bad length')
            obj = json.loads(self.rfile.read(size))
            self.calls.append((self.path, self.headers.get('x-api-key'), obj.get('model')))
            payload = json.dumps({'id': 'mock', 'type': 'message', 'role': 'assistant',
                                  'model': obj['model'], 'content': [{'type': 'text', 'text': 'mock-reply'}],
                                  'stop_reason': 'end_turn', 'usage': {'input_tokens': 1, 'output_tokens': 1}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (ValueError, KeyError, TypeError):
            self.send_error(400)


@unittest.skipUnless(LIVE and sys.platform == 'darwin', 'requires opt-in isolated native macOS run')
class MacLinksIntegration(unittest.TestCase):
    # Reuse the existing real-CLI test's child capture and verified cleanup,
    # without inheriting/discovering its unrelated historical tests.
    def setUp(self):
        Base.setUp(self)
        # If ownership proof fails, retain the root even after this object is GC'd.
        self.temp._finalizer.detach()
        temp_info = Path(self.temp.name).lstat()
        self._temp_identity = (temp_info.st_dev, temp_info.st_ino)
        self.env.update({'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
                         'TMP': str(self.root), 'TEMP': str(self.root),
                         'XDG_CONFIG_HOME': str(self.home / 'config'),
                         'XDG_CACHE_HOME': str(self.home / 'cache'),
                         'NO_PROXY': '*', 'no_proxy': '*', 'PYTHONNOUSERSITE': '1'})
        self.env.update({'PYTHONPATH': str((iso / 'guard')),
                         'MP_LINKS_GUARD_ROOT': str(iso),
                         'MP_LINKS_GUARD_PORTS': os.environ['MP_LINKS_GUARD_PORTS'],
                         'MP_LINKS_OWNER_PID': str(os.getpid()),
                         'MP_LINKS_GUARD_ROLE': 'service', 'MP_LINKS_OWNED_HISTORY': '[]'})
        self.helpers = []
        self.probes = []
        self.pipe_observations = []
        self.port = int(os.environ['MP_LINKS_PROXY_PORT'])
        self.mock_port = int(os.environ['MP_LINKS_MOCK_PORT'])
        self.assertNotIn(18889, (self.port, self.mock_port))
        LocalUpstream.calls = []
        self.mock_server = ThreadingHTTPServer(('127.0.0.1', self.mock_port), LocalUpstream)
        self.mock_server.daemon_threads = True
        self.mock_thread = threading.Thread(target=self.mock_server.serve_forever, daemon=True)
        self.mock_thread.start()
        global _active_case
        _active_case = self
        self.addCleanup(self._stop_mock)
        cfg = {'listen': {'host': '127.0.0.1', 'port': self.port}, 'admin_token': ADMIN,
               'supplies': [{'id': sid, 'url': f'http://127.0.0.1:{self.mock_port}/v1/messages',
                             'protocol': 'anthropic', 'appkey': UPSTREAM, 'target_model': model}
                            for sid, model in (('mock', 'actual-model'), ('mock-r2', 'cli-reloaded-model'))],
               'routes': [{'id': rid, 'tiers': {t: [sid] for t in ('opus', 'sonnet', 'haiku')},
                           'failover': 'on'} for rid, sid in (('r', 'mock'), ('r2', 'mock-r2'))],
               'strategies': [{'client_token': CLIENT, 'route_id': 'r'}]}
        self.config.write_text(json.dumps(cfg))
        self.config.chmod(0o600)
        bait = dict(cfg)
        bait['supplies'] = [dict(cfg['supplies'][0], target_model='DECOY-model'), dict(cfg['supplies'][1])]
        self.target, self.bait, self.spellings = make_layout(
            self.root, json.dumps(cfg).encode(), json.dumps(bait).encode())

    def _cleanup(self):
        for child in self.children + getattr(self, 'helpers', []) + getattr(self, 'probes', []):
            if child.poll() is None:
                start, image = self.child_identity.get(child, (None, None))
                if start is None or image is None:
                    raise AssertionError('owned child identity unknown; test directory retained')
                platform_process.signal_verified(child.pid, start, image)
            child.wait(timeout=10)
        directory = Path(self.temp.name)
        info = directory.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or (info.st_dev, info.st_ino) != self._temp_identity):
            raise AssertionError('test directory identity changed; retained')
        if not shutil.rmtree.avoids_symlink_attacks:
            raise AssertionError('fd-based directory cleanup unavailable; retained')
        shutil.rmtree(directory)

    def _await_guard_attestation(self, child, role, timeout=5):
        path = iso / 'guard' / f'attest-{child.pid}'
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                    raise AssertionError('child guard attestation protection unknown')
                text = path.read_text().splitlines()
                again = path.lstat()
                if (info.st_dev, info.st_ino) != (again.st_dev, again.st_ino):
                    raise AssertionError('child guard attestation identity changed')
                if text:
                    if any(line != role for line in text):
                        raise AssertionError('child guard attestation role mismatch')
                    return text
            except FileNotFoundError:
                pass
            if child.poll() is not None:
                break
            time.sleep(.02)
        raise AssertionError(f'{role} child guard startup not attested')

    _run = lambda self, command: Base._run(self, command)
    @contextlib.contextmanager
    def _managed_environment(self, *, capture_stderr=False):
        from core.cli import dispatch
        self._validate_envelope()
        def capture(args, *pos, **kwargs):
            if (not isinstance(args, (tuple, list)) or len(args) != 2
                    or os.path.realpath(args[0]) != os.path.realpath(sys.executable)
                    or kwargs.get('shell') or pos):
                raise PermissionError('test guard: unsupported child command')
            script = Path(args[1])
            service = self.root / 'core/cli/model_proxy.py'
            helper = self.root / 'core/cli/helper_entry.py'
            # helper has command/config arguments: see separate capture below
            if script != service:
                raise PermissionError('test guard: unsupported child script')
            env = dict(kwargs.get('env', {}))
            if env.get('MP_LINKS_GUARD_ROLE') != 'service':
                raise PermissionError('test guard: child bootstrap role removed')
            env['MP_LINKS_GUARD_ROLE'] = 'service'
            env['MP_LINKS_OWNED_HISTORY'] = _guard_owned_history()
            kwargs['env'] = env
            if capture_stderr:
                fd = os.open(self.home / 'fake-child-stderr.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    kwargs['stderr'] = fd
                    child = _RAW_POPEN(args, **kwargs)
                finally:
                    os.close(fd)
            else:
                child = _RAW_POPEN(args, **kwargs)
            self.children.append(child)
            self.child_identity[child] = (process_start_identity(child.pid), process_executable(child.pid))
            self._await_guard_attestation(child, 'service')
            threading.Thread(target=child.wait, daemon=True).start()
            return child
        def capture_all(args, *pos, **kwargs):
            helper = self.root / 'core/cli/helper_entry.py'
            if isinstance(args, (tuple, list)) and len(args) >= 2 and Path(args[1]) == helper:
                if (pos or kwargs.get('shell') or os.path.realpath(args[0]) != os.path.realpath(sys.executable)):
                    raise PermissionError('test guard: invalid helper child')
                env = dict(kwargs.get('env', {}))
                if env.get('MP_LINKS_GUARD_ROLE') != 'service':
                    raise PermissionError('test guard: child bootstrap role removed')
                env['MP_LINKS_GUARD_ROLE'] = 'helper'
                env['MP_LINKS_OWNED_HISTORY'] = _guard_owned_history()
                if env.get('CONFIG_OPS_RELOAD_MARKER'):
                    raise AssertionError('named marker unexpectedly used')
                fd_text = env.get('CONFIG_OPS_RELOAD_MARKER_FD', '')
                if not fd_text.isdecimal() or kwargs.get('pass_fds') != (int(fd_text),):
                    raise AssertionError('helper did not inherit exactly its anonymous marker FD')
                proof = dispatch._pipe_endpoint_protection(int(fd_text), write=True)
                self.pipe_observations.append((int(fd_text), proof, tuple(kwargs['pass_fds'])))
                kwargs['env'] = env
                child = _RAW_POPEN(args, **kwargs)
                self.helpers.append(child)
                self.child_identity[child] = (process_start_identity(child.pid), process_executable(child.pid))
                self._await_guard_attestation(child, 'helper')
                return child
            return capture(args, *pos, **kwargs)
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch('core.cli.control.subprocess.Popen', side_effect=capture_all):
            yield

    def _validate_envelope(self):
        self.assertNotIn(18889, (self.port, self.mock_port))
        self.assertNotEqual(self.port, self.mock_port)
        self.assertTrue(self.home.is_relative_to(iso))
        paths = json.loads((self.root / 'config/runtime_paths.json').read_text())
        self.assertEqual(paths, self.paths)
        self.assertEqual(len(set(paths.values())), 6)
        for path in paths.values():
            self.assertTrue(Path(path).is_relative_to(self.root / 'runtime'))
        self.assertTrue((iso / '.model-proxy-isolation').is_file())
        raw = self.env['MODEL_PROXY_CONFIG']
        selected = Path(raw) if os.path.isabs(raw) else self.root / raw
        actual = selected.resolve(strict=True)
        self.assertTrue(actual.is_relative_to(iso), 'configuration escapes fake test root')
        config = json.loads(actual.read_text())
        self.assertEqual(config['listen'], {'host': '127.0.0.1', 'port': self.port})
        self.assertEqual(config['admin_token'], ADMIN)
        self.assertEqual(len(config['supplies']), 2)
        for supply in config['supplies']:
            self.assertEqual(supply['url'], f'http://127.0.0.1:{self.mock_port}/v1/messages')
            self.assertEqual(supply['appkey'], UPSTREAM)
            self.assertNotIn('appkey_file', supply)

    _start_interactively = lambda self, **kw: Base._start_interactively(self, **kw)

    def _stop_mock(self):
        self.mock_server.shutdown()
        self.mock_server.server_close()
        self.mock_thread.join(timeout=5)
        self.assertFalse(self.mock_thread.is_alive())

    def _assert_owned_listener(self):
        # Never send even fake tokens to an unknown process on a raced port.
        alive = [p for p in self.children if p.poll() is None]
        self.assertEqual(len(alive), 1, 'expected exactly one owned live service child')
        lsof = shutil.which('lsof')
        self.assertIsNotNone(lsof, 'native listener ownership check unavailable')
        p = _RAW_POPEN([lsof, '-nP', '-a', '-p', str(alive[0].pid),
                        f'-iTCP@127.0.0.1:{self.port}', '-sTCP:LISTEN', '-t'],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.probes.append(p)
        try:
            try:
                out, _err = p.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()  # only the lsof child just created here
                p.communicate()
                raise
        finally:
            if p.poll() is not None:
                self.probes.remove(p)
        self.assertEqual(p.returncode, 0, 'owned listener proof unavailable')
        self.assertIn(str(alive[0].pid), out.splitlines())

    def _request_model(self, expected):
        self._assert_owned_listener()
        payload = json.dumps({'model': 'claude-sonnet', 'max_tokens': 8,
                              'messages': [{'role': 'user', 'content': 'hello'}], 'stream': False})
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=8)
        try:
            conn.request('POST', '/v1/messages', payload, {'Content-Type': 'application/json',
                         'x-api-key': CLIENT, 'anthropic-version': '2023-06-01'})
            res = conn.getresponse()
            data = res.read()
            self.assertEqual(res.status, 200, data[:500])
            self.assertIn(b'mock-reply', data)
        finally:
            conn.close()
        self.assertEqual(LocalUpstream.calls[-1], ('/v1/messages', UPSTREAM, expected))

    def _set_model(self, model):
        cfg = json.loads(self.target.read_text())
        cfg['supplies'][0]['target_model'] = model
        self.target.write_text(json.dumps(cfg))
        self.target.chmod(0o600)

    def _control(self, name, spelling=None):
        with mock.patch.dict(os.environ, {'MODEL_PROXY_CONFIG': spelling or self.env['MODEL_PROXY_CONFIG']}):
            return self._run(name)

    def _assert_no_token(self, command, spelling=None, *, expected='config_unverified'):
        attempted = []
        def deny(sock, data, *args, **kwargs):
            attempted.append(bytes(data))
            raise AssertionError('unexpected control socket send blocked by test')
        def deny_messages(sock, buffers, *args, **kwargs):
            attempted.append(b''.join(bytes(part) for part in buffers))
            raise AssertionError('unexpected control socket sendmsg blocked by test')
        # Instrument Python socket sends only. Do not forward even a fake token
        # if there is a defect, and fail on any attempted bytes, not just on a
        # token substring that could otherwise be split across writes.
        with contextlib.ExitStack() as stack:
            for method in ('send', 'sendall', 'sendto'):
                stack.enter_context(mock.patch.object(socket.socket, method, deny))
            if hasattr(socket.socket, 'sendmsg'):
                stack.enter_context(mock.patch.object(socket.socket, 'sendmsg', deny_messages))
            rc, out, err = self._control(command, spelling)
        self.assertFalse(attempted, 'unsafe control attempted to send socket bytes')
        self.assertNotEqual(rc, 0, f'{command} unexpectedly accepted unsafe config')
        self.assertIn(expected, out + err, 'incorrect failure category')
        self.assertNotIn(ADMIN, out + err, 'admin token appeared in CLI diagnostics')

    def _cli_cycle(self, spelling):
        self.env['MODEL_PROXY_CONFIG'] = spelling
        with in_directory(self.root), self._managed_environment(capture_stderr=True):
            self._start_interactively()
            self._request_model('actual-model')
            self.assertEqual(self._control('status')[0], 0)
            self._set_model('manual-model')
            self.assertEqual(self._control('reload')[0], 0)
            self._request_model('manual-model')
            self._set_model('automatic-model')
            deadline = time.monotonic() + 10
            while True:
                self._request_model_any()
                if LocalUpstream.calls[-1][2] == 'automatic-model':
                    break
                if time.monotonic() >= deadline:
                    self.fail('automatic reload did not apply actual target')
                time.sleep(.1)
            self.assertEqual(self._control('off')[0], 0)
        self.assertTrue((self.root / 'file-link.json').is_symlink())
        self.assertEqual(json.loads(self.bait.read_text())['supplies'][0]['target_model'], 'DECOY-model')

    def _request_model_any(self):
        self._assert_owned_listener()
        body = json.dumps({'model': 'claude-sonnet', 'max_tokens': 8,
                           'messages': [{'role': 'user', 'content': 'hello'}], 'stream': False})
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=8)
        try:
            conn.request('POST', '/v1/messages', body, {'Content-Type': 'application/json',
                         'x-api-key': CLIENT, 'anthropic-version': '2023-06-01'})
            response = conn.getresponse()
            self.assertEqual(response.status, 200, response.read()[:500])
            response.read()
        finally:
            conn.close()
        self.assertTrue(LocalUpstream.calls)
        self.assertEqual(LocalUpstream.calls[-1][:2], ('/v1/messages', UPSTREAM))

    def test_cli_final_file_link(self):
        self._cli_cycle(self.spellings['final'])

    def test_cli_parent_directory_link(self):
        self._cli_cycle(self.spellings['parent'])

    def test_cli_multiple_relative_and_absolute_links(self):
        self._cli_cycle(self.spellings['multi-relative-absolute'])

    def test_cli_symlink_dotdot_uses_kernel_target_not_decoy(self):
        self._cli_cycle(self.spellings['dotdot'])

    def test_automatic_reload_invalid_bytes_keep_last_good_then_recover(self):
        self.env['MODEL_PROXY_CONFIG'] = self.spellings['parent']
        with in_directory(self.root), self._managed_environment(capture_stderr=True):
            self._start_interactively()
            self._request_model('actual-model')
            valid = self.target.read_bytes()
            self.target.write_bytes(b'{invalid json')
            self._request_model('actual-model')
            self.target.write_bytes(valid)
            self._set_model('automatic-model')
            deadline = time.monotonic() + 10
            while True:
                self._request_model_any()
                if LocalUpstream.calls[-1][2] == 'automatic-model':
                    break
                if time.monotonic() >= deadline:
                    self.fail('valid target was not loaded after invalid bytes')
                time.sleep(.1)
            self.assertEqual(self._control('off')[0], 0)

    def test_direct_relative_link_entry_and_control(self):
        self.env['MODEL_PROXY_CONFIG'] = self.spellings['dotdot']
        with in_directory(self.root), self._managed_environment(capture_stderr=True):
            # Create the installation using the same linked target, then stop.
            self._start_interactively()
            self.assertEqual(self._control('off')[0], 0)
            self.children[-1].wait(timeout=10)
            env = dict(self.env, MP_LINKS_OWNED_HISTORY=_guard_owned_history())
            self.assertNotEqual(env['MP_LINKS_OWNED_HISTORY'], '[]', 'previous owned child identity missing')
            child = _RAW_POPEN([sys.executable, 'core/cli/model_proxy.py'], cwd=self.root,
                                     env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            self.children.append(child)
            self.child_identity[child] = (process_start_identity(child.pid), process_executable(child.pid))
            self._await_guard_attestation(child, 'service')
            active_file = self.root / '.runtime/active.json'
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                self.assertIsNone(child.poll(), 'direct service exited before READY')
                try:
                    active = json.loads(active_file.read_text())
                    if active.get('pid') == child.pid and active.get('state') == 'READY':
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(.05)
            else:
                self.fail('direct service not READY')
            attest = self._await_guard_attestation(child, 'service')
            self.assertGreaterEqual(len(attest), 2, 'relative direct entry re-exec did not reinstall guard')
            self._request_model('actual-model')
            for command in ('status', 'reload', 'off'):
                self.assertEqual(self._control(command)[0], 0, command)
            child.wait(timeout=10)

    def test_unsafe_permissions_refuse_tokens_but_off_recovers(self):
        self.env['MODEL_PROXY_CONFIG'] = self.spellings['final']
        with in_directory(self.root), self._managed_environment(capture_stderr=True):
            self._start_interactively()
            self._assert_owned_listener()
            self.assertEqual(self._control('status')[0], 0, 'same-environment positive control')
            try:
                for mode in (0o666, 0o000):
                    self.target.chmod(mode)
                    for command in ('status', 'reload'):
                        self._assert_no_token(command)
                self.assertEqual(self._control('off')[0], 0, 'last-good off must not require bad config')
            finally:
                self.target.chmod(0o600)

    def test_distinct_chain_same_target_then_retarget_refuses_token(self):
        self.env['MODEL_PROXY_CONFIG'] = self.spellings['final']
        alias = self.root / 'second-alias.json'
        alias.symlink_to('config/business.json')
        with in_directory(self.root), self._managed_environment(capture_stderr=True):
            self._start_interactively()
            # Same target and sidecar, independently protected link chains.
            self.assertEqual(self._control('status', 'second-alias.json')[0], 0)
            alias.unlink()
            alias.symlink_to('other/business.json')  # real decoy, not missing
            self._assert_no_token('status', 'second-alias.json', expected='installation_identity_unknown')
            self._assert_no_token('reload', 'second-alias.json', expected='installation_identity_unknown')
            self.assertEqual(self._control('off')[0], 0)

    def test_protection_unknown_fails_closed_without_sending_token(self):
        from core.mac_config_path import MacConfigError
        self.env['MODEL_PROXY_CONFIG'] = self.spellings['final']
        with in_directory(self.root), self._managed_environment(capture_stderr=True):
            self._start_interactively()
            self.assertEqual(self._control('status')[0], 0, 'same-environment positive control')
            # Fault injection is restricted to the evidence acquisition error;
            # all control, peer and socket code remains the real implementation.
            with mock.patch('core.cli.authority.capture_config',
                            side_effect=MacConfigError('protection_unknown')):
                self._assert_no_token('status')
                self._assert_no_token('reload')
            self.assertEqual(self._control('off')[0], 0)

    def test_chain_changed_after_peer_check_never_sends_token(self):
        self.env['MODEL_PROXY_CONFIG'] = self.spellings['final']
        link = self.root / 'file-link.json'
        from core.cli.control import MacControl
        original_check = platform_process.verify_connected_peer
        changed = []
        def change_after_verified(*args, **kwargs):
            result = original_check(*args, **kwargs)
            caller = inspect.currentframe().f_back
            if caller.f_code is MacControl._request.__code__ and not changed:
                link.unlink()
                link.symlink_to('other/business.json')
                changed.append(True)
            return result
        with in_directory(self.root), self._managed_environment(capture_stderr=True):
            self._start_interactively()
            self.assertEqual(self._control('status')[0], 0, 'same-socket positive control')
            try:
                with mock.patch.object(platform_process, 'verify_connected_peer',
                                       new=change_after_verified):
                    self._assert_no_token('status', expected='installation_identity_unknown')
                self.assertTrue(changed, 'race injection missed final _request peer verification')
            finally:
                link.unlink(missing_ok=True)
                link.symlink_to('config/business.json')
            self.assertEqual(self._control('off')[0], 0)

    def test_fake_peer_receives_no_bytes_after_final_peer_check_chain_change(self):
        # The peer is a genuine owned child, not this worker PID. Its listener
        # and the same connected socket receive native process verification.
        # A fake lease isolates this boundary; full authority has its own test.
        from core.cli.control import MacControl, ControlError
        from core.mac_config_path import capture_config, verify_config, MacConfigError
        link = self.root / 'file-link.json'
        before, bait = self.target.read_bytes(), self.bait.read_bytes()
        peer_file = self.root / 'fake-peer-received.bin'
        fixture = self.root / 'fake-peer-fixture/core/cli'
        fixture.mkdir(parents=True, mode=0o700)
        peer_script = fixture / 'model_proxy.py'
        peer_script.write_text("""import os, socket
port = int(os.environ['MP_FAKE_PEER_PORT'])
assert port != 18889
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', port))
    listener.listen(1)
    listener.settimeout(12)
    print('FAKE_PEER_READY', flush=True)
    peer, _ = listener.accept()
    with peer:
        peer.settimeout(12)
        payload = peer.recv(8192)
    with open(os.environ['MP_FAKE_PEER_OUTPUT'], 'wb') as out:
        out.write(payload)
print('FAKE_PEER_FINISHED', flush=True)
""")
        self._fake_peer = True
        try:
            with in_directory(self.root), self._managed_environment():
                snapshot = capture_config(self.spellings['final'])
                env = dict(self.env, MP_LINKS_GUARD_ROLE='service',
                           MP_LINKS_OWNED_HISTORY=_guard_owned_history(),
                           MP_FAKE_PEER_PORT=str(self.port), MP_FAKE_PEER_OUTPUT=str(peer_file))
                peer = _RAW_POPEN([sys.executable, str(peer_script)], cwd=self.root, env=env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.children.append(peer)
                self.child_identity[peer] = (process_start_identity(peer.pid), process_executable(peer.pid))
                self._fake_peer_child = peer
                self._await_guard_attestation(peer, 'service')
                self.assertIsNotNone(self.child_identity[peer][0])
                self.assertIsNotNone(self.child_identity[peer][1])
                ready = select.select([peer.stdout], [], [], 12)[0]
                self.assertTrue(ready, 'owned fake peer did not become ready')
                self.assertEqual(peer.stdout.readline(), b'FAKE_PEER_READY\n')
                instance = types.SimpleNamespace(pid=peer.pid, start=self.child_identity[peer][0],
                                                 executable=self.child_identity[peer][1],
                                                 host='127.0.0.1', port=self.port)
                class FakeLease:
                    def admin_token(_self): return ADMIN
                    def verify_admin_token(_self):
                        verify_config(snapshot)
                        return True
                lease = FakeLease()
                def recheck(*_args, **_kwargs):
                    try:
                        verify_config(snapshot)
                    except MacConfigError:
                        raise ControlError('config_changed') from None
                original = platform_process.verify_connected_peer
                checks, injected = [], []
                def after_actual_peer_check(*args, **kwargs):
                    result = original(*args, **kwargs)
                    caller = inspect.currentframe().f_back
                    checks.append((args[0].fileno(), caller.f_code.co_name))
                    if caller.f_code is MacControl._request.__code__:
                        link.unlink()
                        link.symlink_to('other/business.json')
                        injected.append(args[0].fileno())
                    return result
                control = MacControl(self.root, config_path=self.spellings['final'])
                with mock.patch.object(control, '_instance', return_value=instance), \
                     mock.patch.object(control, '_recheck', side_effect=recheck), \
                     mock.patch.object(platform_process, 'verify_connected_peer', new=after_actual_peer_check):
                    with self.assertRaises(ControlError) as failure:
                        control._request(lease, 'status')
                self.assertEqual(failure.exception.code, 'config_changed')
                peer.wait(timeout=12)
                err = peer.stderr.read().decode('utf-8', errors='replace')
                self.assertEqual(peer.returncode, 0, 'owned fake peer failed: ' + err)
                self.assertTrue(peer_file.is_file(), 'fake peer did not finish receiving')
                received = peer_file.read_bytes()
                self.assertEqual(received, b'', 'fake peer received bytes')
                self.assertNotIn(ADMIN.encode(), received)
                self.assertEqual(len(injected), 1, 'final peer-check injection missed or repeated')
                self.assertIn((injected[0], '_request'), checks)
                self.assertGreaterEqual(len(checks), 3)
                self.assertEqual(self.target.read_bytes(), before)
                self.assertEqual(self.bait.read_bytes(), bait)
        finally:
            self._fake_peer = False

    def test_broken_and_cyclic_links_fail_direct_entry_before_listening(self):
        from core.mac_config_path import capture_config
        with in_directory(self.root):
            self.assertEqual(capture_config(self.spellings['final']).data, self.target.read_bytes())
        before = self.target.read_bytes()
        (self.root / 'broken').symlink_to('missing-file')
        (self.root / 'loop-a').symlink_to('loop-b')
        (self.root / 'loop-b').symlink_to('loop-a')
        for path in ('broken', 'loop-a'):
            with self.subTest(path=path):
                env = dict(self.env, MODEL_PROXY_CONFIG=path,
                           MP_LINKS_OWNED_HISTORY=_guard_owned_history())
                child = _RAW_POPEN([sys.executable, 'core/cli/model_proxy.py'],
                                   cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.children.append(child)
                self.child_identity[child] = (process_start_identity(child.pid), process_executable(child.pid))
                self._await_guard_attestation(child, 'service')
                output, error = child.communicate(timeout=15)
                self.assertNotEqual(child.returncode, 0)
                self.assertIn(b'config_read_error', output + error)
                self.assertNotIn(ADMIN.encode(), output + error)
                self.assertFalse((self.root / '.runtime/active.json').exists())
                self.assertEqual(self.target.read_bytes(), before)
                self.assertEqual(list(self.target.parent.glob(self.target.name + '.bak.*')), [])
                # Bind, never connect, to detect an unexpected lingering listener.
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', self.port))

    def test_sidecar_path_is_not_inferred_from_same_target(self):
        self.env['MODEL_PROXY_CONFIG'] = self.spellings['final']
        alias = self.root / 'other/alias.json'
        alias.symlink_to('../config/business.json')
        # Distinct sidecar conflicts with a protected runtime file.
        (self.root / 'other/session_overrides.json').symlink_to(self.paths['log'])
        with in_directory(self.root), self._managed_environment(capture_stderr=True):
            self._start_interactively()
            self.assertEqual(self._control('status')[0], 0, 'service chain positive control')
            self._assert_no_token('status', 'other/alias.json', expected='config_conflict')
            self.assertEqual(self._control('off')[0], 0)

    def _cli_switch(self, spelling, *, tty, answer=b''):
        """Actual dispatch -> actual helper pipe -> actual MacControl.reload."""
        from core.cli import dispatch
        from core.cli.control import MacControl
        from core.cli.authority import MacInstallationAuthority
        frames, reloads = [], []
        before_helpers, before_pipes = len(self.helpers), len(self.pipe_observations)
        real_drain, real_reload = dispatch._drain_marker_pipe, MacControl.reload
        def observe_drain(fd, proof):
            data = real_drain(fd, proof)
            frames.append(data)
            return data
        def observe_reload(control):
            reloads.append('reload')
            return real_reload(control)
        saved = [os.dup(fd) for fd in (0, 1, 2)]
        master = slave = None
        tempfiles = []
        streams = []
        failure = None
        try:
            if tty:
                master, slave = pty.openpty()
                settings = termios.tcgetattr(slave)
                settings[3] &= ~termios.ECHO
                termios.tcsetattr(slave, termios.TCSANOW, settings)
                if answer:
                    os.write(master, answer + b'\n')
                for fd in (0, 1, 2):
                    os.dup2(slave, fd)
            else:
                input_file = tempfile.TemporaryFile(dir=self.root)
                output_file = tempfile.TemporaryFile(dir=self.root)
                tempfiles.extend((input_file, output_file))
                if answer:
                    input_file.write(answer + b'\n')
                    input_file.seek(0)
                os.dup2(input_file.fileno(), 0)
                os.dup2(output_file.fileno(), 1)
                os.dup2(output_file.fileno(), 2)
            streams = [os.fdopen(os.dup(0), 'r', encoding='utf-8'),
                       os.fdopen(os.dup(1), 'w', encoding='utf-8', buffering=1),
                       os.fdopen(os.dup(2), 'w', encoding='utf-8', buffering=1)]
            with mock.patch('sys.stdin', streams[0]), mock.patch('sys.stdout', streams[1]), \
                 mock.patch('sys.stderr', streams[2]), \
                 mock.patch.object(dispatch, '_drain_marker_pipe', side_effect=observe_drain), \
                 mock.patch.object(MacControl, 'reload', new=observe_reload), \
                 mock.patch.dict(os.environ, {'MODEL_PROXY_CONFIG': spelling}):
                control = MacControl(self.root, authority=MacInstallationAuthority(), config_path=spelling)
                rc = dispatch.main(['switch', CLIENT, 'r2'], root=self.root, control=control)
                streams[1].flush(); streams[2].flush()
        except BaseException as exc:
            failure = (exc, exc.__traceback__)
        finally:
            for stream in streams:
                stream.close()
            for fd, old in enumerate(saved):
                os.dup2(old, fd)
                os.close(old)
        try:
            if tty:
                output = bytearray()
                # PTY delivery is asynchronous even after the slave closes.
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    ready = select.select([master], [], [], 0.05)[0]
                    if not ready:
                        if output:
                            break
                        continue
                    try:
                        block = os.read(master, 65536)
                    except OSError:
                        break
                    if not block:
                        break
                    output.extend(block)
                output = bytes(output).decode('utf-8', errors='replace')
            else:
                tempfiles[1].seek(0)
                output = tempfiles[1].read().decode('utf-8', errors='replace')
        finally:
            if slave is not None:
                os.close(slave)
            if master is not None:
                os.close(master)
            for file in tempfiles:
                file.close()
        if failure is not None:
            raise failure[0].with_traceback(failure[1])
        helpers = self.helpers[before_helpers:]
        pipes = self.pipe_observations[before_pipes:]
        self.assertEqual(len(helpers), 1, 'real helper was not spawned exactly once')
        self.assertEqual(len(pipes), 1, 'real anonymous marker pipe missing')
        self.assertEqual(pipes[0][1][5], 0, 'marker FD is not an anonymous pipe')
        helper_attest = (iso / 'guard' / f'attest-{helpers[0].pid}').read_text().splitlines()
        self.assertTrue(helper_attest and all(line == 'helper' for line in helper_attest))
        return rc, output, frames, reloads

    def _assert_committed_edit(self, target, alias, before, mode, protection, result):
        rc, output, frames, reloads = result
        self.assertEqual(rc, 0, output)
        self.assertEqual(reloads, ['reload'])
        self.assertIn(frames, ([b'writing\nyes\n'], [b'writing\nyes\nyes\n']))
        self.assertIn('Configuration changed; attempting reload.', output)
        self.assertEqual(json.loads(target.read_text())['strategies'][0]['route_id'], 'r2')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), mode)
        from core.mac_config_path import capture_config
        # Includes mode, uid/gid, link count, ACL, filesystem and attributes;
        # inode/device may legitimately change after atomic replacement.
        self.assertEqual(capture_config(str(alias)).protection[1][2:], protection)
        self.assertTrue(alias.is_symlink())
        backups = list(target.parent.glob(target.name + '.bak.*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)

    def test_real_cli_internal_root_non_tty_no_authorization_prompt(self):
        alias = self.root / 'file-link.json'
        spelling = self.spellings['final']
        self.env['MODEL_PROXY_CONFIG'] = spelling
        before, mode, link = self.target.read_bytes(), stat.S_IMODE(self.target.stat().st_mode), os.readlink(alias)
        with in_directory(self.root), self._managed_environment(capture_stderr=True):
            from core.mac_config_path import capture_config
            protection = capture_config(spelling).protection[1][2:]
            self._start_interactively()
            child = next(p for p in self.children if p.poll() is None)
            service_attest = (iso / 'guard' / f'attest-{child.pid}').read_text().splitlines()
            self.assertTrue(service_attest and all(line == 'service' for line in service_attest))
            result = self._cli_switch(spelling, tty=False)
            self._assert_committed_edit(self.target, alias, before, mode, protection, result)
            self.assertNotIn('输入 YES 确认', result[1])
            self.assertEqual(os.readlink(alias), link)
            self._request_model('cli-reloaded-model')
            self.assertEqual(self._control('off')[0], 0)

    def test_real_cli_external_non_tty_denied_tty_denied_then_explicit_yes(self):
        outside = self.root.parent / 'outside-business.json'
        outside.write_bytes(self.target.read_bytes())
        outside.chmod(0o600)
        alias = self.root / 'external-link.json'
        alias.symlink_to(outside)
        spelling = 'external-link.json'
        self.env['MODEL_PROXY_CONFIG'] = spelling
        before, mode, link = outside.read_bytes(), stat.S_IMODE(outside.stat().st_mode), os.readlink(alias)
        with in_directory(self.root), self._managed_environment(capture_stderr=True):
            from core.mac_config_path import capture_config
            protection = capture_config(spelling).protection[1][2:]
            self._start_interactively()
            self._request_model('actual-model')  # same environment positive control
            for tty, answer, error in ((False, b'', 'external config target requires explicit terminal authorization'),
                                       (True, b'NO', '配置读取或路径校验失败')):
                with self.subTest(tty=tty):
                    rc, output, frames, reloads = self._cli_switch(spelling, tty=tty, answer=answer)
                    self.assertNotEqual(rc, 0)
                    self.assertIn(error, output, 'wrong rejection category')
                    if tty:
                        self.assertIn('输入 YES 确认', output)
                    self.assertEqual(frames, [b''])
                    self.assertEqual(reloads, [])
                    self.assertEqual(outside.read_bytes(), before)
                    self.assertEqual(list(outside.parent.glob(outside.name + '.bak.*')), [])
                    self.assertEqual(list(outside.parent.glob('.model-proxy-config-*')), [])
                    self.assertTrue(alias.is_symlink())
                    self.assertEqual(os.readlink(alias), link)
            result = self._cli_switch(spelling, tty=True, answer=b'YES')
            self.assertIn('输入 YES 确认', result[1])
            self.assertIn(json.dumps(str(outside.resolve()), ensure_ascii=True), result[1])
            self._assert_committed_edit(outside, alias, before, mode, protection, result)
            self.assertEqual(os.readlink(alias), link)
            self._request_model('cli-reloaded-model')
            self.assertEqual(self._control('off')[0], 0)

    def _write_with_explicit_grant(self, spelling):
        from core.mac_config_path import capture_config
        from core.mac_config_writer import WriteAuthorization, replace_config
        from core.cli.dispatch import write_preflight
        # The fixture grants precisely this known-created target and its backup
        # siblings, not whichever target the selected symlink happens to name.
        allowed = self.target.resolve(strict=True)
        before = allowed.read_bytes()
        target_info = allowed.stat()
        parent_info = allowed.parent.stat()
        grant = WriteAuthorization(str(allowed), (parent_info.st_dev, parent_info.st_ino),
                                   (target_info.st_dev, target_info.st_ino), True)
        link = self.root / 'file-link.json'
        link_text = os.readlink(link)
        mode = stat.S_IMODE(target_info.st_mode)
        bait = self.bait.read_bytes()
        obj = json.loads(before)
        obj['strategies'] = []
        marker = []
        with in_directory(self.root):
            snapshot = capture_config(spelling)
            self.assertEqual(snapshot.target_path, str(allowed))
            def preflight(snap):
                return snap.target_path == str(allowed) and write_preflight(
                    self.root, spelling, operation='strategy-del') is True
            outcome = replace_config(snapshot, json.dumps(obj).encode(), grant,
                                     preflight=preflight, before_replace=lambda: marker.append('writing'))
        self.assertEqual(marker, ['writing'])
        self.assertEqual((allowed.parent / outcome.backup_name).read_bytes(), before)
        self.assertEqual(json.loads(allowed.read_bytes()), obj)
        self.assertEqual(stat.S_IMODE(allowed.stat().st_mode), mode)
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), link_text)
        self.assertEqual(self.bait.read_bytes(), bait)

    def test_real_writer_granted_final_link_and_backup(self):
        self._write_with_explicit_grant(self.spellings['final'])

    def test_real_writer_granted_dotdot_and_backup(self):
        self._write_with_explicit_grant(self.spellings['dotdot'])

    def test_dotdot_preflight_rejects_real_target_collision_not_lexical_decoy(self):
        from core.cli.dispatch import write_preflight
        paths_file = self.root / 'config/runtime_paths.json'
        paths = json.loads(paths_file.read_text())
        before, bait = self.target.read_bytes(), self.bait.read_bytes()
        with in_directory(self.root):
            self.assertTrue(write_preflight(self.root, self.spellings['dotdot'], operation='strategy-del'))
            paths['log'] = str(self.target)
            paths_file.write_text(json.dumps(paths))
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertFalse(write_preflight(self.root, self.spellings['dotdot'],
                                                 operation='strategy-del'))
            self.assertIn('same_identity', err.getvalue())
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.bait.read_bytes(), bait)

    def test_real_writer_rejects_other_target_outside_explicit_grant(self):
        from core.mac_config_path import capture_config
        from core.mac_config_writer import (WriteAuthorization, MacWriteError, replace_config,
                                            validate_write_authorization)
        outside = self.root.parent / 'ungranted-target.json'
        outside.write_bytes(self.target.read_bytes())
        outside.chmod(0o600)
        alias = self.root / 'outside-alias.json'
        alias.symlink_to(outside)
        before = outside.read_bytes()
        target, parent = self.target.stat(), self.target.parent.stat()
        grant = WriteAuthorization(str(self.target.resolve()), (parent.st_dev, parent.st_ino),
                                   (target.st_dev, target.st_ino), True)
        with in_directory(self.root):
            valid = capture_config(self.spellings['final'])
            validate_write_authorization(valid, grant)
            snapshot = capture_config('outside-alias.json')
            with self.assertRaises(MacWriteError) as failure:
                replace_config(snapshot, b'{}', grant, preflight=lambda _snapshot: True,
                               before_replace=lambda: self.fail('marker must not run'))
            self.assertEqual(failure.exception.code, 'target_outside_authorization')
            self.assertEqual(failure.exception.state, 'NOT_REPLACED')
        self.assertEqual(outside.read_bytes(), before)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(list(outside.parent.glob(outside.name + '.bak.*')), [])

    def test_explicit_grant_still_rejects_hardlink_and_unprotected_target(self):
        from core.mac_config_path import capture_config, MacConfigError
        from core.mac_config_writer import WriteAuthorization, MacWriteError, replace_config
        target, parent = self.target.stat(), self.target.parent.stat()
        grant = WriteAuthorization(str(self.target.resolve()), (parent.st_dev, parent.st_ino),
                                   (target.st_dev, target.st_ino), True)
        before = self.target.read_bytes()
        marker = []
        with in_directory(self.root):
            # Same-environment positive proof, before introducing each defect.
            self.assertEqual(capture_config(self.spellings['final']).data, before)
            os.link(self.target, self.root / 'config/hardlink.json')
            try:
                snapshot = capture_config(self.spellings['final'])
                with self.assertRaises(MacWriteError) as failure:
                    replace_config(snapshot, b'{}', grant, preflight=lambda _snapshot: True,
                                   before_replace=lambda: marker.append('writing'))
                self.assertEqual(failure.exception.code, 'hardlinks_not_preserved')
                self.assertEqual(failure.exception.state, 'NOT_REPLACED')
                self.assertEqual(self.target.read_bytes(), before)
                self.assertEqual(marker, [])
                self.assertEqual(list(self.target.parent.glob(self.target.name + '.bak.*')), [])
            finally:
                (self.root / 'config/hardlink.json').unlink()
            self.assertEqual(capture_config(self.spellings['final']).data, before)
            self.target.chmod(0o666)
            try:
                with self.assertRaises(MacConfigError) as failure:
                    capture_config(self.spellings['final'])
                self.assertEqual(failure.exception.code, 'file_permissions_unsafe')
                self.assertEqual(self.target.read_bytes(), before)
                self.assertEqual(marker, [])
                self.assertEqual(list(self.target.parent.glob(self.target.name + '.bak.*')), [])
            finally:
                self.target.chmod(0o600)

    def test_explicit_grant_rejects_unverifiable_protection(self):
        from core.mac_config_path import capture_config, MacConfigError
        from core.secure_files import SecureSnapshotError
        before = self.target.read_bytes()
        with in_directory(self.root):
            self.assertEqual(capture_config(self.spellings['final']).data, before)
        # Inject only the unavailable protection evidence, not a successful proof.
        with in_directory(self.root), mock.patch('core.mac_config_path._mac_fd_protection',
                                                 side_effect=SecureSnapshotError('protection_unknown')):
            with self.assertRaises(MacConfigError) as failure:
                capture_config(self.spellings['final'])
            self.assertEqual(failure.exception.code, 'protection_unknown')
        self.assertEqual(self.target.read_bytes(), before)
        self.assertTrue((self.root / 'file-link.json').is_symlink())
        self.assertEqual(list(self.target.parent.glob(self.target.name + '.bak.*')), [])

    def test_writer_rechecks_link_changed_before_commit(self):
        from core.mac_config_path import capture_config
        from core.mac_config_writer import WriteAuthorization, MacWriteError, replace_config
        target, parent = self.target.stat(), self.target.parent.stat()
        grant = WriteAuthorization(str(self.target.resolve()), (parent.st_dev, parent.st_ino),
                                   (target.st_dev, target.st_ino), True)
        before, bait = self.target.read_bytes(), self.bait.read_bytes()
        link = self.root / 'file-link.json'
        calls, marker = [], []
        def change_on_second_preflight(_snapshot):
            calls.append(1)
            if len(calls) == 2:
                link.unlink()
                link.symlink_to('other/business.json')
            return True
        with in_directory(self.root):
            snapshot = capture_config(self.spellings['final'])
            with self.assertRaises(MacWriteError) as failure:
                replace_config(snapshot, b'{}', grant, preflight=change_on_second_preflight,
                               before_replace=lambda: marker.append('writing'))
            self.assertEqual(failure.exception.code, 'config_changed')
            self.assertEqual(failure.exception.state, 'NOT_REPLACED')
        self.assertGreaterEqual(len(calls), 2, 'test never reached injected change')
        self.assertEqual(marker, [])
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.bait.read_bytes(), bait)
        self.assertTrue(link.is_symlink())



def live_runner(proxy_port, mock_port):
    if sys.platform != 'darwin' or sys.version_info < (3, 10):
        raise unittest.SkipTest('requires macOS and Python >= 3.10')
    if not shutil.which('lsof'):
        raise unittest.SkipTest('lsof is required')
    if pty is None or termios is None or not shutil.rmtree.avoids_symlink_attacks:
        raise unittest.SkipTest('PTY or fd-based safe cleanup unavailable')
    try:
        import fcntl
    except ImportError:
        raise unittest.SkipTest('F_GETPATH unavailable') from None
    if not hasattr(fcntl, 'F_GETPATH'):
        raise unittest.SkipTest('F_GETPATH unavailable')
    if proxy_port == mock_port or 18889 in (proxy_port, mock_port) or any(
            p < 1024 or p > 65535 for p in (proxy_port, mock_port)):
        raise ValueError('choose two distinct non-production ports, 1024–65535, excluding 18889')
    sys.path.insert(0, str(HERE))
    from isolation_bootstrap import Scratch, _copy_file
    # Reserve both first; do not connect to or probe any existing listener.
    reservations = []
    scratch = None
    completed = False
    try:
        for port in (proxy_port, mock_port):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            reservations.append(sock)
            sock.bind(('127.0.0.1', port))
            sock.listen(1)
        scratch = Scratch(HERE.parent)
        scratch.stage(False, proxy_port, mock_port)
        guard = scratch.root / 'guard'
        (guard / 'sitecustomize.py').unlink()
        _copy_file(HERE / 'fixtures/test_mac_config_links_guard.py', guard / 'test_mac_config_links_guard.py')
        _copy_file(HERE / 'fixtures/test_mac_config_links_fdguard.py', guard / 'test_mac_config_links_fdguard.py')
        _copy_file(HERE / 'fixtures/test_mac_config_links_sitecustomize.py', guard / 'sitecustomize.py')
        tests = scratch.project / 'tests'
        tests.mkdir()
        _copy_file(HERE / 'test_cli_live.py', tests / 'test_cli_live.py')
        _copy_file(Path(__file__), tests / Path(__file__).name)
        (scratch.root / '.model-proxy-isolation').write_text('ephemeral test root\n')
        env = scratch.env(proxy_port, mock_port)
        env.update({'MODEL_PROXY_TEST_ISOLATION': str(scratch.root), 'MP_LINKS_LIVE_CHILD': '1',
                    'MP_LINKS_PROXY_PORT': str(proxy_port), 'MP_LINKS_MOCK_PORT': str(mock_port),
                    'MP_LINKS_GUARD_ROOT': str(scratch.root),
                    'MP_LINKS_GUARD_PORTS': f'{proxy_port},{mock_port}'})
        # Worker uses -I -S and installs the guard explicitly; service/helper
        # receive the staged sitecustomize. This is not an OS sandbox.
        env.pop('PYTHONPATH', None)
        for sock in reservations:
            sock.close()
        reservations.clear()
        proc = subprocess.Popen([str(scratch.python), '-I', '-S', '-B',
                                 str(tests / Path(__file__).name), '--child-live'],
                                cwd=scratch.project, env=env)
        try:
            rc = proc.wait(timeout=600)
        except subprocess.TimeoutExpired:
            proc.terminate()  # only our direct child; its descendants are not guessed
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise RuntimeError(f'live test timed out; retain {scratch.root} and inspect owned processes')
        if rc == 77:
            raise unittest.SkipTest(f'live child skipped; retained {scratch.root} for inspection')
        if rc:
            raise RuntimeError(f'live test failed ({rc}); retain {scratch.root} for inspection')
        completed = True
    finally:
        for sock in reservations:
            sock.close()
        if scratch is not None and completed:
            scratch.cleanup()
        elif scratch is not None:
            print('Test root retained:', scratch.root, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture-only', action='store_true')
    parser.add_argument('--live-frozen', action='store_true')
    parser.add_argument('--child-live', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--proxy-port', type=int)
    parser.add_argument('--mock-port', type=int)
    args = parser.parse_args()
    if args.child_live:
        if not LIVE:
            parser.error('child mode only available inside an isolated live runner')
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(MacLinksIntegration)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 1 if not result.wasSuccessful() else (77 if result.skipped else 0)
    if args.fixture_only:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(FixtureOnly)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 1 if not result.wasSuccessful() else (77 if result.skipped else 0)
    if args.live_frozen and args.proxy_port is not None and args.mock_port is not None:
        try:
            live_runner(args.proxy_port, args.mock_port)
        except unittest.SkipTest as exc:
            print('SKIP (unsupported environment):', exc)
            return 77
        return 0
    parser.error('choose --fixture-only or --live-frozen with two explicit ports')


if __name__ == '__main__':
    sys.exit(main())
