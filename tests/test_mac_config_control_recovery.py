"""Real Darwin identity/last-good recovery with owned fake children only."""
import contextlib
import errno
import fcntl
import io
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ISO = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if (Path(__file__).resolve().parents[1] != ISO / 'project'
        or not (ISO / '.model-proxy-isolation').is_file()
        or not Path(os.environ.get('HOME', '/invalid')).resolve().is_relative_to(ISO)):
    raise RuntimeError('isolated source and fake HOME required')
sys.path.insert(0, str(ISO / 'project'))

from core import platform_process
from core.cli import authority, control
from core.discovery import (PreflightError, inspect_runtime_identities, process_executable,
                            process_start_identity, verify_record_directory, write_instance_record)
from core.platform_lock import acquire_lock, verify_local_access
from core.runtime_paths import PATH_KEYS


_CHILD = '''import fcntl,os,sys\nprint("OWNED_FAKE_STARTED",flush=True)\nfds={}\nmode=fcntl.LOCK_SH if os.environ.get("FAKE_SHARED") == "1" else fcntl.LOCK_EX\nfor name in ("FAKE_ANCHOR", "FAKE_LEGACY"):\n fd=os.open(os.environ[name],os.O_RDWR|os.O_NOFOLLOW)\n fcntl.flock(fd,mode)\n fds[name]=fd\nprint("OWNED_FAKE_READY",flush=True)\nfor line in sys.stdin:\n name=line.strip()\n if name in fds:\n  fcntl.flock(fds[name],fcntl.LOCK_UN)\n  print("RELEASED",flush=True)\n elif name == "QUIT":\n  break\n'''


@unittest.skipUnless(sys.platform == 'darwin', 'native Darwin process evidence required')
class RealOwnedRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ISO, prefix='owned-recovery-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'project'
        for name in ('config', 'aliases', 'runtime', 'home', 'tmp', 'core/cli', '.runtime'):
            (self.root / name).mkdir(parents=True, mode=0o700)
        self.state = self.root / '.runtime'
        self.entry = self.root / 'core/cli/model_proxy.py'
        self.entry.write_text(_CHILD)
        self.entry.chmod(0o600)
        self.business = self.root / 'config/business.json'
        self.service = self.root / 'config/service-link.json'
        self.cli_path = self.root / 'aliases/cli-link.json'
        self.service.symlink_to(self.business)
        self.cli_path.symlink_to(self.business)
        self.paths = {key: str(self.root / 'runtime' / key) for key in PATH_KEYS}
        self.runtime = self.root / 'config/runtime_paths.json'
        self.runtime.write_text(json.dumps(self.paths)); self.runtime.chmod(0o600)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.business.write_text(json.dumps({'admin_token': 'FAKE_ADMIN_ONLY',
                                             'listen': {'host': '127.0.0.1', 'port': self.port},
                                             'supplies': [], 'routes': [], 'strategies': []}))
        self.business.chmod(0o600)
        self.child = None
        self.cli = control.MacControl(self.root, authority=authority.MacInstallationAuthority(),
                                      config_path=self.cli_path, out=io.StringIO(), err=io.StringIO(),
                                      stop_timeout=5, request_timeout=1)

    def cleanup_child(self):
        child = self.child
        if child is None:
            return
        if child.poll() is None:
            child.terminate()  # Only the Popen child created by this fixture.
            try:
                child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=4)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()

    def start_child_and_records(self, *, wrong_start=False, wrong_nonce=False,
                                wrong_entry=False, wrong_lock=False, wrong_image=False,
                                wrong_root=False, inconsistent=False, shared=False):
        self.assertTrue(os.path.samefile(self.service, self.cli_path))
        snapshot = inspect_runtime_identities(self.root, self.paths)
        nonce = 'a' * 32
        launch_nonce = 'b' * 32 if wrong_nonce else nonce
        entry = self.entry
        if wrong_entry:
            entry = self.root / 'core/cli/unrelated.py'
            entry.write_text(_CHILD); entry.chmod(0o600)
        with acquire_lock(snapshot, 'anchor.lock', create=True, access_verifier=verify_local_access) as anchor:
            with acquire_lock(anchor.snapshot, 'lock', create=True, access_verifier=verify_local_access,
                              anchor=anchor) as legacy:
                env = {'HOME': str(self.root / 'home'), 'TMPDIR': str(self.root / 'tmp'),
                       'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1',
                       'MODEL_PROXY_CONFIG': str(self.service), 'MODEL_PROXY_START_NONCE': launch_nonce,
                       'FAKE_ANCHOR': str(self.state / 'anchor.lock'), 'FAKE_LEGACY': self.paths['lock'],
                       'FAKE_SHARED': '1' if shared else '0'}
                self.child = subprocess.Popen([sys.executable, '-B', str(entry)], cwd=self.root, env=env,
                                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, text=True)
                self.addCleanup(self.cleanup_child)
                ready, _, _ = select.select([self.child.stdout], [], [], 5)
                self.assertTrue(ready, 'owned fake child did not enter its script')
                self.assertEqual(self.child.stdout.readline().strip(), 'OWNED_FAKE_STARTED')
                deadline = time.monotonic() + 5
                while True:
                    if self.child.poll() is not None or time.monotonic() >= deadline:
                        self.fail('owned fake child did not expose launch evidence')
                    start = process_start_identity(self.child.pid)
                    executable = process_executable(self.child.pid)
                    if start and executable:
                        try:
                            args, observed = platform_process.darwin_launch_evidence(self.child.pid, start, executable)
                            if args[-1] == str(entry) and observed.get('MODEL_PROXY_START_NONCE') == launch_nonce:
                                break
                        except platform_process.ProcessEvidenceError:
                            pass
                    time.sleep(0.02)
                self.actual_start, self.actual_executable = start, executable
                if wrong_start:
                    start = dict(start, seconds=start['seconds'] + 1)
                dev, ino = verify_record_directory(self.state)
                install = {'schema': 2, 'record': 'install', 'project_root': str(self.root),
                           'install_id': 'c' * 32, 'root_identity': {'device': dev, 'inode': ino},
                           'initialization_source': 'owned fake test only', 'ever_started': True,
                           'last_instance_nonce': nonce}
                if wrong_root:
                    install['root_identity'] = dict(install['root_identity'], inode=ino + 1)
                common = {'schema': 2, 'project_root': str(self.root), 'install_id': install['install_id'],
                          'instance_nonce': nonce, 'listen': {'host': '127.0.0.1', 'port': self.port},
                          'runtime_paths': self.paths}
                locks = {}
                for name, held in (('anchor', anchor), ('legacy', legacy)):
                    st = os.fstat(held.fileno())
                    locks[name] = {'device': st.st_dev, 'inode': st.st_ino}
                if wrong_lock:
                    locks['legacy'] = dict(locks['legacy'], inode=locks['legacy']['inode'] + 1)
                active = dict(common, record='active', state='READY', pid=self.child.pid,
                              process_start=start, executable='/fake/python3' if wrong_image else executable,
                              locks=locks)
                good = dict(common, record='last_good_paths')
                if inconsistent:
                    good['instance_nonce'] = 'd' * 32
                for filename, record in (('last_good_paths.json', good), ('active.json', active),
                                         ('install.json', install)):
                    write_instance_record(self.state, filename, record, anchor=anchor, legacy=legacy)
                self.active, self.good = active, good
        ready, _, _ = select.select([self.child.stdout], [], [], 5)
        self.assertTrue(ready, 'owned fake child did not acquire its fake locks')
        self.assertEqual(self.child.stdout.readline().strip(), 'OWNED_FAKE_READY')
        self.assertIsNone(self.child.poll())
        self.assertEqual(process_start_identity(self.child.pid), self.actual_start)
        self.assertEqual(process_executable(self.child.pid), self.actual_executable)

    def release_child_lock(self, role):
        self.assertIsNone(self.child.poll())
        self.child.stdin.write('FAKE_ANCHOR\n' if role == 'anchor' else 'FAKE_LEGACY\n')
        self.child.stdin.flush()
        ready, _, _ = select.select([self.child.stdout], [], [], 5)
        self.assertTrue(ready, 'owned fake child did not acknowledge lock release')
        self.assertEqual(self.child.stdout.readline().strip(), 'RELEASED')
        self.assertIsNone(self.child.poll())

    @contextlib.contextmanager
    def no_unowned_signal_or_token(self):
        sent = []
        real_signal = platform_process.signal_verified
        def only_owned(pid, start, executable):
            self.assertIsNotNone(self.child)
            self.assertEqual(pid, self.child.pid, 'refuse to signal anything but our own child')
            self.assertIsNone(self.child.poll())
            sent.append(pid)
            return real_signal(pid, start, executable)
        with mock.patch.object(platform_process, 'signal_verified', side_effect=only_owned) as signal, \
             mock.patch.object(authority.MacAuthorityLease, 'admin_token',
                               side_effect=AssertionError('token must not be read')) as token, \
             mock.patch.object(control.MacControl, '_connection',
                               side_effect=AssertionError('no connection permitted')) as connection:
            self.signal_mock = signal
            yield sent, token, connection

    def exercise_success(self, missing=None, *, damage_runtime=False, damage_business=False,
                         remove_both=False, shared=False):
        self.start_child_and_records(shared=shared)
        if missing is not None:
            missing.unlink()
        if remove_both:
            other = self.cli_path if missing == self.service else self.service
            other.unlink()
        if damage_runtime:
            self.runtime.write_text('{broken fake runtime')
        if damage_business:
            self.business.write_text('{broken fake business config')
        original_resolve = Path.resolve
        original_realpath = os.path.realpath
        prohibited = {str(self.service), str(self.cli_path), str(self.business)}
        def resolve_without_config(path, *args, **kwargs):
            if os.fspath(path) in prohibited:
                raise AssertionError('off must not resolve a business config path')
            return original_resolve(path, *args, **kwargs)
        def realpath_without_config(path, *args, **kwargs):
            if os.fspath(path) in prohibited:
                raise AssertionError('off must not resolve a business config path')
            return original_realpath(path, *args, **kwargs)
        reaper = threading.Thread(target=self.child.wait, daemon=True)
        reaper.start()
        with self.no_unowned_signal_or_token() as (sent, token, connection), \
             mock.patch.object(authority, '_capture_business',
                               side_effect=AssertionError('off must not capture config')), \
             mock.patch.object(authority, 'capture_config',
                               side_effect=AssertionError('off must not read config')), \
             mock.patch.object(Path, 'resolve', resolve_without_config), \
             mock.patch.object(os.path, 'realpath', realpath_without_config):
            self.assertEqual(self.cli.off(), 0, self.cli.err.getvalue())
            self.assertEqual(sent, [self.child.pid])
            token.assert_not_called(); connection.assert_not_called()
        reaper.join(timeout=5)
        self.assertFalse(reaper.is_alive())
        self.assertIsNotNone(self.child.poll())

    def test_off_recovers_when_service_alias_is_removed(self):
        self.exercise_success(self.service)

    def test_off_recovers_when_cli_alias_is_removed(self):
        self.exercise_success(self.cli_path)

    def test_off_recovers_when_service_alias_and_runtime_config_are_damaged(self):
        self.exercise_success(self.service, damage_runtime=True)

    def test_off_recovers_when_both_aliases_are_removed(self):
        self.exercise_success(self.service, remove_both=True)

    def test_off_recovers_when_business_json_is_corrupt(self):
        self.exercise_success(damage_business=True)

    def test_off_accepts_shared_lock_conflicts_without_claiming_exclusive_ownership(self):
        self.exercise_success(self.service, shared=True)

    def wait_unreaped_child(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = os.waitid(os.P_PID, self.child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            if result is not None:
                self.assertEqual(result.si_pid, self.child.pid)
                return result
            time.sleep(0.01)
        self.fail('owned fake child did not reach unreaped exit')

    def test_off_succeeds_on_unreaped_child_after_exactly_one_signal(self):
        self.start_child_and_records()
        self.service.unlink()
        with self.no_unowned_signal_or_token() as (sent, token, connection):
            # Deliberately no Popen.poll()/wait/reaper until after off returns.
            self.assertEqual(self.cli.off(), 0, self.cli.err.getvalue())
            self.assertEqual(sent, [self.child.pid])
            self.signal_mock.assert_called_once()
            token.assert_not_called(); connection.assert_not_called()
        self.wait_unreaped_child()
        os.kill(self.child.pid, 0)
        self.assertTrue(platform_process.process_gone(self.child.pid, self.actual_start,
                                                      self.actual_executable))
        self.child.wait(timeout=3)  # Reap only the child created by this test.

    def test_off_does_not_resignal_already_unreaped_zombie(self):
        self.start_child_and_records()
        self.service.unlink()
        self.child.send_signal(signal.SIGTERM)  # Only this fixture's owned child.
        self.wait_unreaped_child()
        with self.no_unowned_signal_or_token() as (sent, token, connection):
            self.assertEqual(self.cli.off(), 0, self.cli.err.getvalue())
            self.signal_mock.assert_not_called()
            self.assertEqual(sent, [])
            token.assert_not_called(); connection.assert_not_called()
        self.wait_unreaped_child()
        self.child.wait(timeout=3)

    def test_unreaped_exit_with_reacquired_anchor_is_not_success(self):
        self.start_child_and_records()
        self.service.unlink()
        real_signal = platform_process.signal_verified
        held = []
        def signal_then_hold(pid, start, executable):
            self.assertEqual(pid, self.child.pid)
            real_signal(pid, start, executable)
            self.wait_unreaped_child()
            fd = os.open(self.state / 'anchor.lock', os.O_RDWR | os.O_NOFOLLOW)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(fd)
        try:
            with mock.patch.object(platform_process, 'signal_verified', side_effect=signal_then_hold) as sent, \
                 mock.patch.object(authority.MacAuthorityLease, 'admin_token') as token, \
                 mock.patch.object(control.MacControl, '_connection') as connection:
                self.assertEqual(self.cli.off(), 1)
                sent.assert_called_once()
                token.assert_not_called(); connection.assert_not_called()
            self.wait_unreaped_child()
        finally:
            for fd in held:
                fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
            self.child.wait(timeout=3)

    def test_unreaped_exit_with_reacquired_legacy_lock_is_not_success(self):
        self.start_child_and_records()
        self.service.unlink()
        real_signal = platform_process.signal_verified
        held = []
        def signal_then_hold(pid, start, executable):
            self.assertEqual(pid, self.child.pid)
            real_signal(pid, start, executable)
            self.wait_unreaped_child()
            fd = os.open(self.paths['lock'], os.O_RDWR | os.O_NOFOLLOW)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(fd)
        try:
            with mock.patch.object(platform_process, 'signal_verified', side_effect=signal_then_hold) as sent, \
                 mock.patch.object(authority.MacAuthorityLease, 'admin_token') as token, \
                 mock.patch.object(control.MacControl, '_connection') as connection:
                self.assertEqual(self.cli.off(), 1)
                sent.assert_called_once()
                token.assert_not_called(); connection.assert_not_called()
            self.wait_unreaped_child()
        finally:
            for fd in held:
                fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
            self.child.wait(timeout=3)

    def test_unreaped_exit_with_occupied_recorded_port_is_not_success(self):
        self.start_child_and_records()
        self.service.unlink()
        real_signal = platform_process.signal_verified
        listeners = []
        def signal_then_bind(pid, start, executable):
            self.assertEqual(pid, self.child.pid)
            real_signal(pid, start, executable)
            self.wait_unreaped_child()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listeners.append(listener)
            listener.bind(('127.0.0.1', self.port))
            listener.listen(1)
        try:
            with mock.patch.object(platform_process, 'signal_verified', side_effect=signal_then_bind) as sent, \
                 mock.patch.object(authority.MacAuthorityLease, 'admin_token') as token, \
                 mock.patch.object(control.MacControl, '_connection') as connection:
                self.assertEqual(self.cli.off(), 1)
                sent.assert_called_once()
                token.assert_not_called(); connection.assert_not_called()
            self.wait_unreaped_child()
        finally:
            for listener in listeners: listener.close()
            self.child.wait(timeout=3)

    def test_restart_starts_new_owned_server_while_old_child_is_unreaped(self):
        # The old child is a fake lock holder. The new child runs the copied
        # real server; the only peer is the CLI's own loopback connection.
        source = ISO / 'project/core'
        bootstrap = (source / 'cli/model_proxy.py').read_text()
        shutil.copytree(source, self.root / 'core', dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        import textwrap
        prefix = ('import os\nif os.environ.get("FAKE_ANCHOR"):\n'
                  + textwrap.indent(_CHILD, ' ') + '\n raise SystemExit(0)\n')
        self.entry.write_text(prefix + bootstrap)
        self.entry.chmod(0o600)
        (self.root / 'VERSION').write_text('0.0-test')
        self.start_child_and_records()
        real_popen = subprocess.Popen
        real_signal = platform_process.signal_verified
        children, sent = [], []
        log_path = self.root / 'home/fake-restart-stderr.log'
        def cleanup_new():
            for child in children:
                if child.poll() is None:
                    child.terminate()  # Only the child spawned by this fixture.
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait(timeout=5)
        self.addCleanup(cleanup_new)
        def spawn_owned(*args, **kwargs):
            self.assertEqual(Path(args[0][-1]), self.entry)
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                kwargs['stderr'] = fd
                child = real_popen(*args, **kwargs)
            finally:
                os.close(fd)
            children.append(child)
            return child
        def signal_old(pid, start, executable):
            self.assertEqual(pid, self.child.pid, 'refuse to signal any other process')
            sent.append(pid)
            return real_signal(pid, start, executable)
        env = {'HOME': str(self.root / 'home'), 'TMPDIR': str(self.root / 'tmp'),
               'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1',
               'MODEL_PROXY_CONFIG': str(self.cli_path)}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(control.subprocess, 'Popen', side_effect=spawn_owned), \
             mock.patch.object(platform_process, 'signal_verified', side_effect=signal_old), \
             mock.patch.object(authority.MacAuthorityLease, 'admin_token',
                               side_effect=AssertionError('restart must not read token')) as token:
            result = self.cli.restart()
        diagnostic = self.cli.err.getvalue()
        if log_path.exists() and result != 0:
            diagnostic += '\\n' + log_path.read_text(errors='replace').replace('FAKE_ADMIN_ONLY', '[REDACTED]')
        self.assertEqual(result, 0, diagnostic)
        self.assertEqual(sent, [self.child.pid])
        self.assertEqual(len(children), 1)
        token.assert_not_called()
        self.wait_unreaped_child()
        os.kill(self.child.pid, 0)  # Old child is still an unreaped zombie.
        active = json.loads((self.state / 'active.json').read_text())
        self.assertEqual(active['pid'], children[0].pid)
        self.assertEqual(active['state'], 'READY')
        self.assertIsNone(children[0].poll())
        self.child.wait(timeout=3)

    def exercise_restart_blocked_after_unreaped_exit(self, conflict):
        self.start_child_and_records()
        real_signal = platform_process.signal_verified
        held, listeners = [], []
        def signal_then_conflict(pid, start, executable):
            self.assertEqual(pid, self.child.pid)
            real_signal(pid, start, executable)
            self.wait_unreaped_child()
            if conflict == 'lock':
                fd = os.open(self.state / 'anchor.lock', os.O_RDWR | os.O_NOFOLLOW)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(fd)
            else:
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listeners.append(listener)
                listener.bind(('127.0.0.1', self.port)); listener.listen(1)
        env = {'HOME': str(self.root / 'home'), 'TMPDIR': str(self.root / 'tmp'),
               'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1',
               'MODEL_PROXY_CONFIG': str(self.cli_path)}
        try:
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(platform_process, 'signal_verified', side_effect=signal_then_conflict) as sent, \
                 mock.patch.object(control.subprocess, 'Popen') as spawn, \
                 mock.patch.object(authority.MacAuthorityLease, 'admin_token') as token:
                self.assertEqual(self.cli.restart(), 1)
                sent.assert_called_once(); spawn.assert_not_called(); token.assert_not_called()
            self.wait_unreaped_child()
        finally:
            for fd in held:
                fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
            for listener in listeners: listener.close()
            self.child.wait(timeout=3)

    def test_restart_does_not_spawn_if_anchor_is_held_after_zombie(self):
        self.exercise_restart_blocked_after_unreaped_exit('lock')

    def test_restart_does_not_spawn_if_port_is_occupied_after_zombie(self):
        self.exercise_restart_blocked_after_unreaped_exit('port')

    def test_restart_unknown_process_state_never_signals_or_spawns(self):
        self.start_child_and_records()
        with self.no_unowned_signal_or_token() as (sent, token, connection), \
             mock.patch.object(platform_process, '_darwin_kinfo_one',
                               side_effect=platform_process.ProcessEvidenceError('process_state_unavailable')), \
             mock.patch.object(control.subprocess, 'Popen') as spawn:
            self.assertEqual(self.cli.restart(), 1)
            self.assertEqual(sent, [])
            self.signal_mock.assert_not_called()
            spawn.assert_not_called(); token.assert_not_called(); connection.assert_not_called()
        self.assertIsNone(self.child.poll())

    def exercise_released_lock(self, role):
        self.start_child_and_records()
        self.service.unlink()
        self.release_child_lock(role)
        with self.no_unowned_signal_or_token() as (sent, token, connection):
            self.assertEqual(self.cli.off(), 1)
            self.signal_mock.assert_not_called()
            self.assertEqual(sent, [])
            token.assert_not_called(); connection.assert_not_called()
        self.assertIsNone(self.child.poll())

    def test_off_rejects_released_anchor_without_signal(self):
        self.exercise_released_lock('anchor')

    def test_off_rejects_released_legacy_lock_without_signal(self):
        self.exercise_released_lock('legacy')

    def exercise_release_before_final_recheck(self, role):
        self.start_child_and_records()
        self.service.unlink()
        original_recheck = self.cli._recheck
        checks = []
        def release_before_final(lease, instance, **kwargs):
            checks.append(1)
            if len(checks) == 2:
                self.release_child_lock(role)
            return original_recheck(lease, instance, **kwargs)
        with self.no_unowned_signal_or_token() as (sent, token, connection), \
             mock.patch.object(self.cli, '_recheck', side_effect=release_before_final):
            self.assertEqual(self.cli.off(), 1)
            self.assertEqual(len(checks), 2)
            self.signal_mock.assert_not_called()
            self.assertEqual(sent, [])
            token.assert_not_called(); connection.assert_not_called()
        self.assertIsNone(self.child.poll())

    def test_anchor_release_before_final_recheck_never_signals(self):
        self.exercise_release_before_final_recheck('anchor')

    def test_legacy_release_before_final_recheck_never_signals(self):
        self.exercise_release_before_final_recheck('legacy')

    def exercise_replaced_lock_path(self, role):
        self.start_child_and_records()
        self.service.unlink()
        path = self.state / 'anchor.lock' if role == 'anchor' else Path(self.paths['lock'])
        moved = path.with_name(path.name + '.owned-old')
        path.rename(moved)
        path.write_text('fake replacement')
        path.chmod(0o600)
        with self.no_unowned_signal_or_token() as (sent, token, connection):
            self.assertEqual(self.cli.off(), 1)
            self.signal_mock.assert_not_called()
            self.assertEqual(sent, [])
            token.assert_not_called(); connection.assert_not_called()
        self.assertIsNone(self.child.poll())

    def test_off_rejects_replaced_anchor_path_without_signal(self):
        self.exercise_replaced_lock_path('anchor')

    def test_off_rejects_replaced_legacy_path_without_signal(self):
        self.exercise_replaced_lock_path('legacy')

    def test_off_rejects_unobservable_legacy_lock_without_signal(self):
        self.start_child_and_records()
        self.service.unlink()
        real_verify = authority.verify_local_access
        def inaccessible(path, kind):
            if Path(path) == Path(self.paths['lock']) and kind == 'file':
                return False
            return real_verify(path, kind)
        with self.no_unowned_signal_or_token() as (sent, token, connection), \
             mock.patch.object(authority, 'verify_local_access', side_effect=inaccessible):
            self.assertEqual(self.cli.off(), 1)
            self.signal_mock.assert_not_called()
            self.assertEqual(sent, [])
            token.assert_not_called(); connection.assert_not_called()
        self.assertIsNone(self.child.poll())

    def test_off_rejects_unobservable_legacy_flock_probe(self):
        self.start_child_and_records()
        self.service.unlink()
        actual_flock = fcntl.flock
        expected = Path(self.paths['lock']).stat()
        probes = []
        def flock(fd, operation):
            current = os.fstat(fd)
            if ((current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino)
                    and operation == fcntl.LOCK_EX | fcntl.LOCK_NB):
                probes.append(1)
                raise OSError(errno.EIO, 'fake lock observation failure')
            return actual_flock(fd, operation)
        with self.no_unowned_signal_or_token() as (sent, token, connection), \
             mock.patch.object(authority.fcntl, 'flock', side_effect=flock):
            self.assertEqual(self.cli.off(), 1)
            self.assertEqual(probes, [1])
            self.signal_mock.assert_not_called()
            self.assertEqual(sent, [])
            token.assert_not_called(); connection.assert_not_called()
        self.assertIsNone(self.child.poll())

    def test_off_rejects_unobservable_anchor_conflict(self):
        self.start_child_and_records()
        self.service.unlink()
        with self.no_unowned_signal_or_token() as (sent, token, connection), \
             mock.patch.object(authority, 'observe_lock_conflict',
                               side_effect=PreflightError('locks_unverified', 'fake')):
            self.assertEqual(self.cli.off(), 1)
            self.signal_mock.assert_not_called()
            self.assertEqual(sent, [])
            token.assert_not_called(); connection.assert_not_called()
        self.assertIsNone(self.child.poll())

    def test_off_rejects_legacy_path_replacement_during_conflict_probe(self):
        self.start_child_and_records()
        self.service.unlink()
        actual_flock = fcntl.flock
        path = Path(self.paths['lock'])
        expected = path.stat()
        changed = []
        def flock(fd, operation):
            current = os.fstat(fd)
            try:
                return actual_flock(fd, operation)
            except OSError as exc:
                if ((current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino)
                        and operation == (fcntl.LOCK_EX | fcntl.LOCK_NB)
                        and exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK)):
                    path.rename(path.with_name('lock.old-owned'))
                    path.write_text('fake replacement')
                    path.chmod(0o600)
                    changed.append(1)
                raise
        with self.no_unowned_signal_or_token() as (sent, token, connection), \
             mock.patch.object(authority.fcntl, 'flock', side_effect=flock):
            self.assertEqual(self.cli.off(), 1)
            self.assertEqual(changed, [1])
            self.signal_mock.assert_not_called()
            self.assertEqual(sent, [])
            token.assert_not_called(); connection.assert_not_called()
        self.assertIsNone(self.child.poll())

    def test_off_rejects_wrong_pid_start_evidence(self):
        self.start_child_and_records(wrong_start=True)
        self.service.unlink()
        with self.no_unowned_signal_or_token() as (sent, token, connection):
            self.assertEqual(self.cli.off(), 1)
            self.assertEqual(sent, [])
            token.assert_not_called(); connection.assert_not_called()
        self.assertIsNone(self.child.poll())

    def test_off_rejects_wrong_launch_nonce(self):
        self.start_child_and_records(wrong_nonce=True)
        self.service.unlink()
        with self.no_unowned_signal_or_token() as (sent, token, connection):
            self.assertEqual(self.cli.off(), 1)
            self.assertEqual(sent, [])
            token.assert_not_called(); connection.assert_not_called()
        self.assertIsNone(self.child.poll())

    def test_off_rejects_wrong_service_entry(self):
        self.start_child_and_records(wrong_entry=True)
        self.service.unlink()
        with self.no_unowned_signal_or_token() as (sent, token, connection):
            self.assertEqual(self.cli.off(), 1)
            self.assertEqual(sent, [])
            token.assert_not_called(); connection.assert_not_called()
        self.assertIsNone(self.child.poll())

    def test_off_rejects_wrong_recorded_image(self):
        self.start_child_and_records(wrong_image=True)
        self.service.unlink()
        with self.no_unowned_signal_or_token() as (sent, token, connection):
            self.assertEqual(self.cli.off(), 1)
            self.assertEqual(sent, [])
            token.assert_not_called(); connection.assert_not_called()
        self.assertIsNone(self.child.poll())

    def test_off_rejects_wrong_installation_root_identity(self):
        self.start_child_and_records(wrong_root=True)
        self.service.unlink()
        with self.no_unowned_signal_or_token() as (sent, token, connection):
            self.assertEqual(self.cli.off(), 1)
            self.assertEqual(sent, [])
            token.assert_not_called(); connection.assert_not_called()
        self.assertIsNone(self.child.poll())

    def test_off_rejects_wrong_recorded_lock(self):
        self.start_child_and_records(wrong_lock=True)
        self.service.unlink()
        with self.no_unowned_signal_or_token() as (sent, token, connection):
            self.assertEqual(self.cli.off(), 1)
            self.assertEqual(sent, [])
            token.assert_not_called(); connection.assert_not_called()
        self.assertIsNone(self.child.poll())

    def test_off_rejects_inconsistent_generation_records(self):
        self.start_child_and_records(inconsistent=True)
        self.service.unlink()
        with self.no_unowned_signal_or_token() as (sent, token, connection):
            self.assertEqual(self.cli.off(), 1)
            self.assertEqual(sent, [])
            token.assert_not_called(); connection.assert_not_called()
        self.assertIsNone(self.child.poll())

    def test_off_rechecks_protected_record_immediately_before_signal(self):
        self.start_child_and_records()
        self.service.unlink()
        original_recheck = self.cli._recheck
        calls = []
        def tamper_before_final(lease, instance, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                (self.state / 'last_good_paths.json').write_text('{}')
            return original_recheck(lease, instance, **kwargs)
        with self.no_unowned_signal_or_token() as (sent, token, connection), \
             mock.patch.object(self.cli, '_recheck', side_effect=tamper_before_final):
            self.assertEqual(self.cli.off(), 1)
            self.assertEqual(len(calls), 2)
            self.assertEqual(sent, [])
            token.assert_not_called(); connection.assert_not_called()
        self.assertIsNone(self.child.poll())

    def test_other_operations_and_restart_stop_keep_service_chain_requirement(self):
        self.start_child_and_records()
        self.service.unlink()
        with self.no_unowned_signal_or_token() as (sent, token, connection), \
             mock.patch.object(control.subprocess, 'Popen',
                               side_effect=AssertionError('must not spawn')) as spawn:
            self.assertEqual(self.cli.status(), 1)
            self.assertEqual(self.cli.reload(), 1)
            self.assertEqual(self.cli.restart(), 1)
            self.assertEqual(sent, [])
            token.assert_not_called(); connection.assert_not_called(); spawn.assert_not_called()
        with authority.MacInstallationAuthority().locked_for_stop(self.root, self.cli_path) as lease:
            active, good = lease.records()
            instance = control._valid_record(self.root, active, good, allow_unavailable_host=True)
            lease._restart_baseline = object()
            with lease.stopping(), self.assertRaises((PreflightError, control.ControlError)):
                lease.verify_active(instance)
        self.assertIsNone(self.child.poll())


if __name__ == '__main__':
    unittest.main()
