"""Offline restart preflight tests; all data under isolated fake HOME."""
from __future__ import annotations
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ISO = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if (Path(__file__).resolve().parents[1] != ISO / 'project'
        or not (ISO / '.model-proxy-isolation').is_file()
        or not Path(os.environ.get('HOME', '/invalid')).resolve().is_relative_to(ISO)):
    raise RuntimeError('isolated source and fake HOME required')
sys.path.insert(0, str(ISO / 'project'))
from core import platform_process
from core.cli import authority, control
from core.discovery import PreflightError, inspect_runtime_identities
from core.runtime_paths import PATH_KEYS

@unittest.skipUnless(sys.platform == 'darwin', 'native Mac ACL/FS checks required')
class RestartTargetsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ISO, prefix='targets-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'fake'
        self.root.mkdir(mode=0o700)
        for name in ('.model_proxy_runtime', 'runtime', 'config'):
            (self.root / name).mkdir(mode=0o700)
        self.paths = {k: str(self.root / 'runtime' / k) for k in PATH_KEYS}
        self.config = self.root / 'config' / 'business.json'
        self.config.write_text(json.dumps({'listen': {'host':'127.0.0.1','port':29491},
                                           'admin_token':'fake-token',
                                           'supplies':[{'id':'fake','appkey':'fake-key'}]}))
        self.config.chmod(0o600)
        self.runtime = self.root / 'config' / 'runtime_paths.json'
        self.runtime.write_text(json.dumps(self.paths)); self.runtime.chmod(0o600)
        self.log = Path(self.paths['log'])
        self.log.write_bytes(b'fake-log-existing\n'); self.log.chmod(0o600)
        self.sidecar = self.config.parent / 'session_overrides.json'
        self.snapshot = inspect_runtime_identities(self.root, self.paths)
        self.business_snap = authority._capture(self.config, private=True)
        self.runtime_snap = authority._capture(self.runtime, private=False)
        self.listen = {'host':'127.0.0.1','port':29491}
        self.active = {'runtime_paths':self.paths}
        self.good = {'runtime_paths':self.paths}
        self.records = {'install':{'ever_started':True},'active':self.active,'last_good_paths':self.good}
        self.instance = control.Instance(54321, {'fake':1}, '/fake/python3', 'a'*32,
                                         '127.0.0.1',29491,self.active,self.good)
        self.obs = mock.Mock(_root=self.root, _config_path=self.config, _paths=self.paths)
        self.obs.record_set.return_value = self.records
        self.lease = authority.MacAuthorityLease(self.obs)
        self.cli = control.MacControl(self.root,config_path=self.config,out=io.StringIO(),err=io.StringIO())
        @contextlib.contextmanager
        def locked(*args): yield self.lease
        self.cli.authority = SimpleNamespace(locked=locked)

    def restart(self):
        snapshot = inspect_runtime_identities(self.root, self.paths)
        current = (self.business_snap,self.runtime_snap,self.listen,snapshot)
        with mock.patch.object(authority, '_valid_record', return_value=self.instance), \
             mock.patch.object(self.lease, '_configuration', return_value=current), \
             mock.patch.object(self.lease, '_preflight_restart_ledger'), \
             mock.patch.object(self.cli, '_stop') as stop, \
             mock.patch.object(self.cli, '_start') as start, \
             mock.patch.object(platform_process, 'signal_verified') as signal, \
             mock.patch('socket.socket', side_effect=AssertionError('unexpected network')):
            rc = self.cli.restart()
            return rc, stop.call_count, start.call_count, signal.call_count

    def test_separate_sidecar_and_writable_log_allow_restart_preflight(self):
        before = self.log.read_bytes()
        self.assertEqual(self.restart(), (0,1,1,0))
        self.assertEqual(self.log.read_bytes(),before)

    def test_sidecar_hardlink_to_log_blocks_before_signal(self):
        os.link(self.log,self.sidecar)
        before = self.log.read_bytes()
        self.assertEqual(self.restart(), (1,0,0,0))
        self.assertIn('config_conflict',self.cli.err.getvalue())
        self.assertEqual(self.log.read_bytes(),before)

    def test_sidecar_symlink_is_unknown_and_not_followed(self):
        self.sidecar.symlink_to(self.config)
        with self.assertRaises(PreflightError):
            authority._check_restart_targets(self.root,self.config,self.snapshot)

    def test_log_append_probe_never_writes_or_truncates(self):
        before = self.log.read_bytes(); st = self.log.stat()
        real_open = os.open
        calls = []
        def checked_open(path,flags,*args,**kwargs):
            if Path(path) == self.log:
                calls.append(flags)
                self.assertFalse(flags & (os.O_TRUNC | os.O_CREAT))
            return real_open(path,flags,*args,**kwargs)
        with mock.patch.object(authority.os,'open',side_effect=checked_open), \
             mock.patch.object(authority.os,'write',side_effect=AssertionError('must not write')):
            authority._check_restart_log(self.snapshot)
        after = self.log.stat()
        self.assertEqual(self.log.read_bytes(),before)
        self.assertEqual((st.st_ino,st.st_size,st.st_mtime_ns), (after.st_ino,after.st_size,after.st_mtime_ns))
        self.assertTrue(any(f & os.O_APPEND and f & os.O_WRONLY for f in calls))

    def test_unwritable_log_blocks_before_signal(self):
        self.log.chmod(0o400)
        before = self.log.read_bytes()
        self.assertEqual(self.restart(), (1,0,0,0))
        self.assertIn('log_write_unverified',self.cli.err.getvalue())
        self.assertEqual(self.log.read_bytes(),before)

    def test_missing_log_with_verified_parent_is_not_created(self):
        self.log.unlink()
        self.assertEqual(self.restart(), (0,1,1,0))
        self.assertFalse(self.log.exists())

    def test_missing_log_with_unwritable_parent_blocks_before_signal(self):
        self.log.unlink()
        self.log.parent.chmod(0o500)
        self.addCleanup(self.log.parent.chmod, 0o700)
        self.assertEqual(self.restart(), (1,0,0,0))
        self.assertIn('log_write_unverified', self.cli.err.getvalue())
        self.assertFalse(self.log.exists())

    def test_separate_local_sidecar_symlink_allows_preflight(self):
        target = self.config.parent / 'separate.json'
        target.write_text('{}'); target.chmod(0o600)
        self.sidecar.symlink_to(target.name)
        self.assertEqual(self.restart(), (0,1,1,0))
        self.assertTrue(self.sidecar.is_symlink())
        self.assertEqual(target.read_text(), '{}')

    def test_dangling_sidecar_symlink_is_unknown(self):
        self.sidecar.symlink_to('missing.json')
        self.assertEqual(self.restart(), (1,0,0,0))

    def test_missing_log_with_unknown_parent_acl_blocks_before_signal(self):
        self.log.unlink()
        with mock.patch.object(authority, '_verify_directory',
                               side_effect=PreflightError('protection_unavailable', 'unknown ACL')):
            self.assertEqual(self.restart(), (1,0,0,0))
        self.assertIn('log_write_unverified', self.cli.err.getvalue())
        self.assertFalse(self.log.exists())

    def test_sidecar_with_writable_by_others_target_blocks_before_signal(self):
        target = self.config.parent / 'separate.json'
        target.write_text('{}'); target.chmod(0o666)
        self.sidecar.symlink_to(target.name)
        self.assertEqual(self.restart(), (1,0,0,0))
        self.assertIn('config_unverified', self.cli.err.getvalue())

    def test_sidecar_target_replaced_during_probe_blocks_before_signal(self):
        target = self.config.parent / 'separate.json'
        target.write_text('{}'); target.chmod(0o600)
        replacement = self.config.parent / 'replacement.json'
        replacement.write_text('{}'); replacement.chmod(0o600)
        self.sidecar.symlink_to(target.name)
        real_open = os.open
        def changed(path, flags, *args, **kwargs):
            if Path(path) == target:
                os.replace(replacement, target)
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(authority.os, 'open', side_effect=changed):
            self.assertEqual(self.restart(), (1,0,0,0))
        self.assertTrue(self.sidecar.is_symlink())

    def test_sidecar_symlink_to_log_blocks(self):
        self.sidecar.symlink_to(self.log)
        self.assertEqual(self.restart(), (1,0,0,0))
        self.assertIn('config_conflict', self.cli.err.getvalue())

if __name__ == '__main__': unittest.main(verbosity=2)
