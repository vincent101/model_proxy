"""Isolated fake-config regressions for reviewer Mac lifecycle findings."""
from __future__ import annotations

import contextlib
import errno
import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ISOLATION = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if (Path(__file__).resolve().parents[1] != ISOLATION / 'project'
        or not (ISOLATION / '.model-proxy-isolation').is_file()
        or not Path(os.environ.get('HOME', '/invalid')).resolve().is_relative_to(ISOLATION)):
    raise RuntimeError('isolated source and fake HOME required')
sys.path.insert(0, str(ISOLATION / 'project'))

from core import platform_process
from core.cli import authority, control
from core.discovery import inspect_runtime_identities
from core.runtime_paths import PATH_KEYS


class ReviewFindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ISOLATION, prefix='review-fake-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'fake-project'
        self.root.mkdir(mode=0o700)
        (self.root / '.model_proxy_runtime').mkdir(mode=0o700)
        (self.root / 'runtime').mkdir(mode=0o700)
        (self.root / 'config').mkdir(mode=0o700)
        self.config_path = self.root / 'config/business.json'
        self.paths = {k: str(self.root / 'runtime' / k) for k in PATH_KEYS}
        self.active = {'runtime_paths': self.paths}
        self.good = {'runtime_paths': self.paths}
        self.records = {'install': {'ever_started': True}, 'active': self.active,
                        'last_good_paths': self.good}
        self.instance = control.Instance(54321, {'fake': 1}, '/fake/python3', 'a'*32,
                                         '127.0.0.1', 29491, self.active, self.good)
        self.obs = mock.Mock(_root=self.root, _config_path=self.config_path, _paths=self.paths)
        self.obs.record_set.return_value = self.records
        self.obs.records.return_value = (self.active, self.good)
        self.lease = authority.MacAuthorityLease(self.obs)
        self.out, self.err = io.StringIO(), io.StringIO()
        self.cli = control.MacControl(self.root, config_path=self.config_path, out=self.out, err=self.err)
        @contextlib.contextmanager
        def locked(*args): yield self.lease
        self.cli.authority = SimpleNamespace(locked=locked, locked_for_stop=locked)
        self.listen = {'host': '127.0.0.1', 'port': 29491}
        # Restart now requires a real, existing fake log identity.
        fake_log = Path(self.paths['log'])
        fake_log.write_text('fake test log\n'); fake_log.chmod(0o600)
        self.snapshot = inspect_runtime_identities(self.root, self.paths)
        self.runtime = object()

    def config(self, headers):
        return SimpleNamespace(exists=True, contents=json.dumps({
            'supplies': [{'id': 'fake-supply', 'appkey': 'fake-key', 'extra_headers': headers}],
            'listen': self.listen,
        }).encode())

    def run_restart(self, config):
        current = (config, self.runtime, self.listen, self.snapshot)
        with mock.patch.object(authority, '_valid_record', return_value=self.instance), \
             mock.patch.object(self.lease, '_configuration', return_value=current), \
             mock.patch.object(self.lease, '_preflight_restart_ledger'), \
             mock.patch.object(self.cli, '_stop') as stop, \
             mock.patch.object(self.cli, '_start') as start, \
             mock.patch.object(platform_process, 'signal_verified') as signal:
            result = self.cli.restart()
            return result, stop.call_count, start.call_count, signal.call_count

    def test_restart_invalid_supply_rejected_before_stop(self):
        rc, stopped, started, signalled = self.run_restart(self.config(['invalid']))
        self.assertEqual((rc, stopped, started, signalled), (1, 0, 0, 0))
        self.assertNotIn('fake-key', self.err.getvalue())

    def test_restart_valid_supply_reaches_stop_preflight(self):
        self.assertEqual(self.run_restart(self.config({'x-fake-header': 'ok'})), (0, 1, 1, 0))

    def test_valid_config_still_preflights_new_listener(self):
        config = self.config({'x-fake-header': 'ok'})
        changed_listen = {'host': '127.0.0.1', 'port': 29492}
        current = (config, self.runtime, changed_listen, self.snapshot)
        with mock.patch.object(authority, '_valid_record', return_value=self.instance), \
             mock.patch.object(self.lease, '_configuration', return_value=current), \
             mock.patch.object(self.lease, '_preflight_restart_ledger') as ledger, \
             mock.patch.object(control.MacControl, '_port_free') as port, \
             mock.patch.object(platform_process, 'signal_verified') as signal:
            self.assertTrue(self.lease.assert_restart_supported())
            ledger.assert_called_once_with(current, self.records)
            port.assert_called_once_with('127.0.0.1', 29492)
            self.assertEqual(self.lease._restart_baseline, current[:3])
            signal.assert_not_called()

    def test_valid_config_ledger_rejection_clears_old_restart_authority(self):
        config = self.config({'x-fake-header': 'ok'})
        current = (config, self.runtime, self.listen, self.snapshot)
        self.lease._restart_baseline, self.lease._restart_records = ('old',), {'old': 'records'}
        with mock.patch.object(authority, '_valid_record', return_value=self.instance), \
             mock.patch.object(self.lease, '_configuration', return_value=current), \
             mock.patch.object(self.lease, '_preflight_restart_ledger',
                               side_effect=authority.PreflightError('ledger_unverified', 'fake')) as ledger, \
             mock.patch.object(platform_process, 'signal_verified') as signal:
            with self.assertRaises(authority.PreflightError):
                self.lease.assert_restart_supported()
            ledger.assert_called_once_with(current, self.records)
            self.assertIsNone(self.lease._restart_baseline)
            self.assertIsNone(self.lease._restart_records)
            signal.assert_not_called()

    def test_changed_business_snapshot_after_validation_cannot_stop(self):
        before = (self.config({'x-fake-header': 'ok'}), self.runtime, self.listen, self.snapshot)
        after = (self.config(['invalid']), self.runtime, self.listen, self.snapshot)
        with mock.patch.object(authority, '_valid_record', return_value=self.instance), \
             mock.patch.object(self.lease, '_configuration', side_effect=[before, after]), \
             mock.patch.object(authority, '_capture_restart_ledger', return_value=None), \
             mock.patch.object(self.cli, '_stop') as stop, mock.patch.object(self.cli, '_start') as start:
            self.assertEqual(self.cli.restart(), 1)
            stop.assert_not_called(); start.assert_not_called()

    def off_with_bind_error(self, bind_error):
        lease = mock.Mock(unsafe=True)
        lease.stopping = None
        lease.records.return_value = (self.active, self.good)
        lease.assert_stopped.return_value = True
        @contextlib.contextmanager
        def locked(*args): yield lease
        self.cli.authority = SimpleNamespace(locked_for_stop=locked, locked=locked)
        sock = mock.Mock()
        sock.bind.side_effect = bind_error
        with mock.patch.object(control, '_valid_record', return_value=self.instance), \
             mock.patch.object(self.cli, '_instance', return_value=self.instance), \
             mock.patch.object(self.cli, '_recheck'), \
             mock.patch.object(platform_process, 'process_gone', side_effect=[False, True]), \
             mock.patch.object(platform_process, 'signal_verified') as signal, \
             mock.patch.object(control.socket, 'socket', return_value=sock):
            result = self.cli.off()
            return result, signal.call_count, sock.close.call_count

    def test_off_port_probe_unknown_is_failure_after_confirmed_exit(self):
        rc, signals, closes = self.off_with_bind_error(OSError(errno.EACCES, 'fake'))
        self.assertEqual((rc, signals, closes), (1, 1, 1))
        self.assertIn('port', self.err.getvalue())
        self.assertIn('process exited', self.err.getvalue())

    def test_off_occupied_port_is_not_success(self):
        rc, signals, closes = self.off_with_bind_error(OSError(errno.EADDRINUSE, 'fake'))
        self.assertEqual((rc, signals, closes), (1, 1, 1))
        self.assertIn('port_unavailable', self.err.getvalue())

    def test_off_confirmed_free_is_success(self):
        self.assertEqual(self.off_with_bind_error(None), (0, 1, 1))

    def test_port_probe_distinguishes_occupied_from_unknown_failures(self):
        for phase, error, expected in (
                ('socket', OSError(errno.EMFILE, 'fake'), 'port_check_unknown'),
                ('options', OSError(errno.EADDRINUSE, 'fake'), 'port_check_unknown'),
                ('bind', OSError(errno.EADDRINUSE, 'fake'), 'port_unavailable'),
                ('bind', OSError(errno.EADDRNOTAVAIL, 'fake'), 'port_check_unknown'),
                ('listen', OSError(errno.EADDRINUSE, 'fake'), 'port_unavailable'),
                ('listen', OSError(errno.ENOBUFS, 'fake'), 'port_check_unknown'),
                ('close', OSError(errno.EIO, 'fake'), 'port_check_unknown')):
            with self.subTest(phase=phase, expected=expected):
                sock = mock.Mock()
                if phase == 'options': sock.setsockopt.side_effect = error
                if phase == 'bind': sock.bind.side_effect = error
                if phase == 'listen': sock.listen.side_effect = error
                if phase == 'close': sock.close.side_effect = error
                with mock.patch.object(control.socket, 'socket',
                        side_effect=error if phase == 'socket' else None, return_value=sock):
                    with self.assertRaises(control.ControlError) as caught:
                        self.cli._port_free('127.0.0.1', 29491)
                self.assertEqual(caught.exception.code, expected)
                if phase != 'socket': sock.close.assert_called_once()

    def test_after_exit_unavailable_interface_reports_unknown(self):
        lease = mock.Mock(unsafe=True)
        lease.stopping = None
        lease.records.return_value = (self.active, self.good)
        lease.assert_stopped.return_value = True
        @contextlib.contextmanager
        def locked(*args): yield lease
        self.cli.authority = SimpleNamespace(locked_for_stop=locked, locked=locked)
        with mock.patch.object(control, '_valid_record', return_value=self.instance), \
             mock.patch.object(self.cli, '_instance', return_value=self.instance), \
             mock.patch.object(self.cli, '_recheck'), \
             mock.patch.object(platform_process, 'process_gone', side_effect=[False, True]), \
             mock.patch.object(platform_process, 'signal_verified'), \
             mock.patch.object(self.cli, '_port_free',
                 side_effect=platform_process.ProcessEvidenceError('local_address_unavailable')):
            self.assertEqual(self.cli.off(), 1)
            self.assertIn('process exited; locks released; port release unknown', self.err.getvalue())

    def test_validator_matches_real_startup_and_never_emits_or_connects(self):
        from core import server
        cases = [
            {'supplies': [{'id': 'fake', 'appkey': 'fake-key', 'extra_headers': {'x-test': 'ok'}}]},
            {'supplies': [{'id': 'fake', 'appkey': 'fake-key', 'extra_headers': ['bad']}]},
            {'supplies': [{'id': 'fake', 'appkey': 'fake-key', 'extra_headers': {'authorization': 'fake'}}]},
            {'supplies': [{'id': 'fake', 'appkey': 'fake-key', 'system_inject': 'yes', 'protocol': 'bad'}]},
            {'supplies': [{'id': 'fake'}]},
            {'routes': None},
            {'strategies': [{'client_token': 'fake-secret', 'route_id': 'a', 'route_pool': [{'route_id': 'b'}]}]},
        ]
        for config in cases:
            with self.subTest(config=repr(config)):
                self.config_path.write_text(json.dumps(config)); self.config_path.chmod(0o600)
                snap = SimpleNamespace(exists=True, contents=json.dumps(config).encode())
                with mock.patch.object(server.log, 'warning'):
                    try:
                        server.ConfigStore(self.config_path, on_reload=None)
                        startup_ok = True
                    except Exception:
                        startup_ok = False
                with mock.patch.object(server.log, 'warning') as logs, \
                     mock.patch('builtins.open', side_effect=AssertionError('unexpected file IO')) as file_open, \
                     mock.patch('os.open', side_effect=AssertionError('unexpected file IO')) as fd_open, \
                     mock.patch('socket.socket', side_effect=AssertionError('unexpected socket')) as sock, \
                     mock.patch('socket.getaddrinfo', side_effect=AssertionError('unexpected DNS')) as dns, \
                     mock.patch('urllib.request.urlopen', side_effect=AssertionError('unexpected network')) as url, \
                     mock.patch('subprocess.Popen', side_effect=AssertionError('unexpected child')) as child:
                    try:
                        authority._validate_restart_business_config(snap)
                        preflight_ok = True
                    except authority.PreflightError:
                        preflight_ok = False
                    self.assertEqual(startup_ok, preflight_ok)
                    for action in (logs, file_open, fd_open, sock, dns, url, child):
                        action.assert_not_called()

    def test_fresh_validator_import_and_execution_have_no_io_side_effects(self):
        program = r"""
import os, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
forbidden = {'/tmp/model_proxy.lock', '/tmp/model_proxy.pid', '/tmp/model_proxy_ensure.log',
             '/tmp/model_proxy_start.lock', str(Path(sys.argv[1]) / 'config' / 'runtime_paths.json')}
def audit(event, args):
    if event == 'open' and args:
        path = args[0]
        if isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path) in forbidden:
            raise AssertionError('forbidden default access')
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        if isinstance(mode, str) and any(c in mode for c in 'wax+'):
            raise AssertionError('write prohibited')
        if isinstance(flags, int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):
            raise AssertionError('write prohibited')
    if (event.startswith('socket.') or event in ('subprocess.Popen', 'os.system', 'os.kill',
                                                  'os.remove', 'os.rename', 'os.mkdir', 'os.symlink')):
        raise AssertionError('side effect prohibited: ' + event)
sys.addaudithook(audit)
old_stat, old_lstat = os.stat, os.lstat
def guard(fn):
    def checked(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path) in forbidden:
            raise AssertionError('forbidden default stat')
        return fn(path, *args, **kwargs)
    return checked
os.stat, os.lstat = guard(old_stat), guard(old_lstat)
from core.cli.authority import _validate_restart_business_config
config = b'{"supplies":[{"id":"fake","appkey_file":"/nonexistent-fake-file"}],"strategies":[{"client_token":"fake-secret","route_id":"a","route_pool":[{"route_id":"b"}]}]}'
_validate_restart_business_config(SimpleNamespace(exists=True, contents=config))
print('safe')
"""
        env = {'HOME': os.environ['HOME'], 'TMPDIR': os.environ['TMPDIR'],
               'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', program, str(ISOLATION / 'project')],
                                cwd=self.root, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'safe')
        self.assertEqual(result.stderr, '')

    def test_unavailable_or_non_object_config_fails_closed(self):
        for data in (b'[]', b'{bad', None):
            with self.subTest(data=data):
                with self.assertRaises(authority.PreflightError):
                    authority._validate_restart_business_config(SimpleNamespace(exists=True, contents=data))

    def setup_anchor(self):
        path = self.root / '.model_proxy_runtime/anchor.lock'
        path.write_bytes(b''); path.chmod(0o600)
        st = path.stat()
        self.active['locks'] = {'anchor': {'device': st.st_dev, 'inode': st.st_ino}}
        snapshot = inspect_runtime_identities(self.root, self.paths)
        # This fixture isolates the anchor probe; config-chain validation is
        # explicitly mocked by its callers, not represented as real evidence.
        config, runtime = SimpleNamespace(evidence=SimpleNamespace(source_path=str(self.config_path))), object()
        current = (config, runtime, self.listen, snapshot)
        return path, current

    def verify_running(self, current):
        with mock.patch.object(self.lease, '_configuration', return_value=current), \
             mock.patch.object(self.lease, '_launch', return_value={}), \
             mock.patch.object(self.lease, '_launch_config_pair', (current[0], current[0])), \
             mock.patch.object(authority, '_same_business'), \
             mock.patch.object(authority, '_check_restart_targets'), \
             mock.patch.object(authority, '_capture_business', return_value=current[0]), \
             mock.patch.object(authority, '_capture', return_value=current[1]), \
             mock.patch.object(platform_process, 'check_identity'):
            return self.lease.verify_active(self.instance)

    def test_running_requires_actually_held_anchor(self):
        path, current = self.setup_anchor()
        with self.assertRaises((control.ControlError, authority.PreflightError)):
            self.verify_running(current)
        with path.open('r+') as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(self.verify_running(current))
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)


    def test_on_ready_refuses_free_anchor_before_socket_and_accepts_held_anchor(self):
        path, current = self.setup_anchor()
        self.active.update(pid=54321, state='READY', instance_nonce='a' * 32)
        entry = self.root / 'core/cli/model_proxy.py'
        entry.parent.mkdir(parents=True)
        entry.write_text('# fake only\n')
        child = mock.Mock(pid=54321)
        child.poll.return_value = None
        def capture(path, *, private):
            return current[0] if Path(path) == self.config_path else current[1]
        with mock.patch.object(self.cli, '_configuration', return_value=(self.paths, self.listen)), \
             mock.patch.object(self.cli, '_prepare_initialization'), \
             mock.patch.object(self.cli, '_port_free'), \
             mock.patch.object(self.lease, 'assert_startable', return_value=True), \
             mock.patch.object(self.lease, 'installation_state', return_value=control.INSTALL_INITIALIZED), \
             mock.patch.object(self.lease, '_configuration', return_value=current), \
             mock.patch.object(self.lease, '_launch', return_value={'MODEL_PROXY_START_NONCE': 'a' * 32}), \
             mock.patch.object(self.lease, '_launch_config_pair', (current[0], current[0])), \
             mock.patch.object(authority, '_same_business'), \
             mock.patch.object(authority, '_check_restart_targets'), \
             mock.patch.object(authority, '_capture_business', return_value=current[0]), \
             mock.patch.object(authority, '_capture', side_effect=capture), \
             mock.patch.object(control, '_valid_record', return_value=self.instance), \
             mock.patch.object(control.uuid, 'uuid4', return_value=SimpleNamespace(hex='a' * 32)), \
             mock.patch.object(control.subprocess, 'Popen', return_value=child), \
             mock.patch.object(platform_process, 'check_identity'), \
             mock.patch.object(self.cli, '_connection', return_value=contextlib.nullcontext()) as connection:
            with self.assertRaises(control.ControlError):
                self.cli._start(self.lease)
            connection.assert_not_called()
            with path.open('r+') as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.cli._start(self.lease)
                connection.assert_called_once()
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)

    def test_anchor_probe_permission_error_is_unknown(self):
        path, current = self.setup_anchor()
        with mock.patch.object(authority, 'observe_lock_conflict',
                               return_value=authority.LockConflictObservation(True, None)), \
             mock.patch.object(authority.fcntl, 'flock', side_effect=PermissionError(errno.EACCES, 'fake')):
            with self.assertRaises(control.ControlError):
                self.verify_running(current)

    def test_posix_record_lock_alone_does_not_count_as_flock(self):
        path, current = self.setup_anchor()
        program = "import fcntl,sys; f=open(sys.argv[1],'r+'); fcntl.lockf(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.readline()"
        env = {'HOME': os.environ['HOME'], 'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}
        child = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', program, str(path)],
                                 cwd=self.root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            with self.assertRaises(control.ControlError):
                self.verify_running(current)
        finally:
            if child.poll() is None:
                child.stdin.write('quit\n'); child.stdin.flush()
            child.stdin.close()
            child.wait(timeout=5)
            child.stdout.close(); child.stderr.close()

    def test_anchor_replacement_during_probe_fails_closed(self):
        path, current = self.setup_anchor()
        replacement = path.with_name('replacement')
        replacement.write_bytes(b''); replacement.chmod(0o600)
        original = authority.observe_lock_conflict
        def replace_after_observation(fd, *, candidate_pid):
            result = original(fd, candidate_pid=candidate_pid)
            os.replace(replacement, path)
            return result
        with path.open('r+') as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(authority, 'observe_lock_conflict', side_effect=replace_after_observation):
                with self.assertRaises(control.ControlError):
                    self.verify_running(current)
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)

if __name__ == '__main__':
    unittest.main(verbosity=2)
