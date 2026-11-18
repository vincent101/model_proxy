"""Mac config chain/control tests, exclusively in an isolated source copy."""
import contextlib
import io
import json
import os
import pty
import socket
import subprocess
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

from core.cli import authority, control, dispatch
from core.mac_config_path import capture_config
from core.discovery import inspect_runtime_identities
from core.runtime_paths import PATH_KEYS


@unittest.skipUnless(sys.platform == 'darwin', 'native Mac path evidence required')
class MacConfigControlLinks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ISO, prefix='config-control-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'project'
        for item in ('config/inner', 'other', 'runtime', 'core/cli', 'service'):
            (self.root / item).mkdir(parents=True, mode=0o700)
        self.target = self.root / 'config/business.json'
        self.target.write_text(json.dumps({'admin_token': 'fake-token', 'listen': {'host': '127.0.0.1', 'port': 29389}}))
        self.target.chmod(0o600)
        self.alias = self.root / 'other/pivot'
        self.alias.symlink_to(self.root / 'config/inner', target_is_directory=True)
        self.cli = self.alias / '..' / 'business.json'
        self.service = self.root / 'service/alias.json'
        self.service.symlink_to(self.target)
        self.entry = self.root / 'core/cli/model_proxy.py'
        self.entry.write_text('# fake entry, never executed\n')
        self.obs = SimpleNamespace(_root=self.root, _config_path=self.cli)
        self.lease = authority.MacAuthorityLease(self.obs)
        self.env = {'MODEL_PROXY_CONFIG': str(self.service)}
        self.paths = {key: str(self.root / 'runtime' / key) for key in PATH_KEYS}
        (self.root / 'config/runtime_paths.json').write_text(json.dumps(self.paths))

    def launch(self):
        with mock.patch.object(authority, '_capture', return_value=SimpleNamespace(exists=True)), \
             mock.patch.object(authority.platform_process, 'darwin_launch_evidence',
                               return_value=(['/fake/python3', str(self.entry)], self.env)):
            return self.lease._launch(123, {'fake': True}, '/fake/python3', 'a' * 32)

    def test_launch_compares_two_distinct_valid_chains(self):
        self.assertEqual(self.launch(), self.env)
        service, cli = self.lease._launch_config_pair
        self.assertNotEqual(service.evidence.source_path, cli.evidence.source_path)
        self.assertEqual(service.evidence.target_path, str(self.target))
        self.assertEqual(cli.evidence.target_path, str(self.target))
        self.assertNotEqual(Path(service.evidence.source_path).parent, Path(cli.evidence.source_path).parent)

    def test_large_business_config_remains_usable_by_authority(self):
        cfg = json.loads(self.target.read_text())
        cfg['fake_padding'] = 'x' * (2 * 1024 * 1024 + 1024)
        self.target.write_text(json.dumps(cfg))
        self.launch()
        self.lease._verified = object()
        with mock.patch.object(self.lease, 'verify_active', return_value=True):
            self.assertEqual(self.lease.admin_token(), 'fake-token')
        self.assertTrue(self.lease.verify_admin_token())

    def test_service_chain_untrusted_or_different_target_fails(self):
        other = self.root / 'service/other.json'
        other.write_bytes(self.target.read_bytes()); other.chmod(0o600)
        self.service.unlink(); self.service.symlink_to(other)
        with self.assertRaises(control.ControlError): self.launch()
        self.service.unlink(); self.service.symlink_to(self.target)
        (self.root / 'service').chmod(0o777)
        try:
            with self.assertRaises((control.ControlError, authority.PreflightError)): self.launch()
        finally:
            (self.root / 'service').chmod(0o700)

    def test_last_good_stop_does_not_read_current_missing_config(self):
        self.lease._stopping = True
        self.env['MODEL_PROXY_CONFIG'] = str(self.cli)
        self.target.unlink()
        with mock.patch.object(authority, 'capture_config', side_effect=AssertionError('config read')):
            self.assertEqual(self.launch(), self.env)

    def test_preflight_checks_actual_dotdot_target_not_decoy(self):
        decoy = self.root / 'other/business.json'
        decoy.write_text('{}'); decoy.chmod(0o600)
        self.paths['log'] = str(self.target)
        (self.root / 'config/runtime_paths.json').write_text(json.dumps(self.paths))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(dispatch.write_preflight(self.root, self.cli, operation='switch'))
        self.paths['log'] = str(decoy)
        (self.root / 'config/runtime_paths.json').write_text(json.dumps(self.paths))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(dispatch.write_preflight(self.root, self.cli, operation='switch'))

    def test_preflight_checks_original_sidecar_against_runtime(self):
        self.paths['totals'] = str(self.cli.parent / 'session_overrides.json')
        (self.root / 'config/runtime_paths.json').write_text(json.dumps(self.paths))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(dispatch.write_preflight(self.root, self.cli, operation='switch'))

    def test_service_and_cli_sidecar_paths_are_checked_separately(self):
        self.paths['log'] = str(self.service.parent / 'session_overrides.json')
        snapshot = inspect_runtime_identities(self.root, self.paths)
        authority._check_restart_targets(self.root, self.cli, snapshot)
        with self.assertRaises(authority.PreflightError):
            authority._check_restart_targets(self.root, self.service, snapshot)

    def test_raw_spelling_reaches_control_and_preflight(self):
        raw = str(self.root / 'config') + '/./business.json'
        cli = control.MacControl(self.root, config_path=raw)
        self.assertEqual(cli._config_source, raw)
        with mock.patch.dict(os.environ, {'MODEL_PROXY_CONFIG': raw}):
            self.assertEqual(dispatch.config_path(self.root), raw)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(dispatch.write_preflight(self.root, raw, operation='switch'))
            self.assertFalse(dispatch.write_preflight(self.root, raw + '/', operation='switch'))
            self.assertFalse(dispatch.write_preflight(self.root, raw + '/.', operation='switch'))

    def test_write_recheck_binds_chain_runtime_and_sidecar(self):
        snapshot = capture_config(self.cli)
        recheck = dispatch._config_write_recheck(self.root, snapshot, 'switch')
        self.assertTrue(recheck(snapshot))
        sidecar = self.cli.parent / 'session_overrides.json'
        sidecar.write_text('{}')
        self.assertFalse(recheck(snapshot))
        sidecar.unlink()
        self.assertTrue(recheck(snapshot))
        runtime_file = self.root / 'config/runtime_paths.json'
        self.paths['log'] = str(self.root / 'runtime/new-log')
        runtime_file.write_text(json.dumps(self.paths))
        self.assertFalse(recheck(snapshot))

    def test_internal_link_write_authorization_needs_no_tty_and_rechecks_parent(self):
        snapshot = capture_config(self.cli)
        with mock.patch.object(sys, 'stdin', io.StringIO()), \
             mock.patch.object(sys, 'stdout', io.StringIO()):
            grant, recheck = dispatch.prepare_config_write(self.root, snapshot, operation='switch')
        self.assertEqual(grant.target_path, str(self.target))
        self.assertTrue(recheck(snapshot))
        self.target.parent.chmod(0o777)
        try:
            self.assertFalse(recheck(snapshot))
        finally:
            self.target.parent.chmod(0o700)

    def test_parent_xattr_change_invalidates_grant_and_recheck(self):
        from core.mac_config_writer import validate_write_authorization
        snapshot = capture_config(self.cli)
        grant, recheck = dispatch.prepare_config_write(self.root, snapshot, operation='switch')
        name = 'com.apple.metadata:_kMDItemUserTags'
        env = {'HOME': str(ISO / 'home'), 'PATH': '/usr/bin:/bin'}
        subprocess.run(['/usr/bin/xattr', '-w', name, 'fake-test-tag', str(self.target.parent)],
                       check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            changed = capture_config(self.cli)
            self.assertNotEqual(snapshot.target_parent_protection, changed.target_parent_protection)
            self.assertFalse(recheck(snapshot))
            with self.assertRaises(RuntimeError):
                validate_write_authorization(snapshot, grant)
        finally:
            subprocess.run(['/usr/bin/xattr', '-d', name, str(self.target.parent)],
                           check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_internal_scope_uses_directory_identity_on_case_insensitive_volume(self):
        alternate = self.root / 'CONFIG'
        if not alternate.is_dir():
            self.skipTest('case-sensitive filesystem')
        alias = self.root / 'other/case-alias.json'
        alias.symlink_to(alternate / self.target.name)
        snapshot = capture_config(alias)
        with mock.patch.object(sys, 'stdin', io.StringIO()), \
             mock.patch.object(sys, 'stdout', io.StringIO()):
            grant, recheck = dispatch.prepare_config_write(self.root, snapshot, operation='switch')
        self.assertEqual(grant.target_identity, tuple(snapshot.final_identity[:2]))
        self.assertTrue(recheck(snapshot))

    def test_write_recheck_fails_on_link_change_even_same_target(self):
        snapshot = capture_config(self.cli)
        recheck = dispatch._config_write_recheck(self.root, snapshot, 'switch')
        self.alias.unlink()
        self.alias.symlink_to(self.root / 'config/inner', target_is_directory=True)
        self.assertFalse(recheck(snapshot))

    def test_absent_runtime_staging_or_backup_name_rejected_before_creation(self):
        snapshot = capture_config(self.cli)
        for name in ('.model-proxy-config-' + 'a' * 24,
                     self.target.name + '.bak.' + 'a' * 16,
                     '.MODEL-PROXY-CONFIG-' + 'A' * 24,
                     self.target.name.upper() + '.BAK.' + 'A' * 16):
            with self.subTest(name=name):
                path = self.target.parent / name
                self.paths['log'] = str(path)
                (self.root / 'config/runtime_paths.json').write_text(json.dumps(self.paths))
                with self.assertRaises(authority.PreflightError):
                    dispatch._config_write_recheck(self.root, snapshot, 'switch')
                self.assertFalse(path.exists())

    def test_explicit_tty_write_grant_binds_actual_object_and_parent(self):
        snapshot = capture_config(self.cli)
        recheck = dispatch._config_write_recheck(self.root, snapshot, 'switch')
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        with os.fdopen(os.dup(slave), 'r') as terminal_in, \
             os.fdopen(os.dup(slave), 'w') as terminal_out:
            os.write(master, b'YES\n')
            with mock.patch.object(sys, 'stdin', terminal_in), \
                 mock.patch.object(sys, 'stdout', terminal_out):
                grant = dispatch._confirm_config_write(snapshot, recheck)
        parent = self.target.parent.stat()
        self.assertEqual(grant.target_path, str(self.target))
        self.assertEqual(grant.parent_identity, (parent.st_dev, parent.st_ino))
        self.assertEqual(grant.target_identity, tuple(snapshot.final_identity[:2]))
        self.assertTrue(grant.allow_backup_and_staging)
        self.assertEqual(self.target.read_bytes(), snapshot.data)

    def test_explicit_write_grant_refuses_non_tty(self):
        snapshot = capture_config(self.cli)
        recheck = dispatch._config_write_recheck(self.root, snapshot, 'switch')
        with mock.patch.object(sys, 'stdin', io.StringIO('YES\n')), \
             mock.patch.object(sys, 'stdout', io.StringIO()):
            with self.assertRaises(authority.PreflightError):
                dispatch._confirm_config_write(snapshot, recheck)
        self.assertEqual(self.target.read_bytes(), snapshot.data)

    def test_confirmed_writer_replaces_real_target_and_retains_link(self):
        from core.mac_config_writer import replace_config
        snapshot = capture_config(self.cli)
        recheck = dispatch._config_write_recheck(self.root, snapshot, 'switch')
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        with os.fdopen(os.dup(slave), 'r') as terminal_in, \
             os.fdopen(os.dup(slave), 'w') as terminal_out:
            os.write(master, b'YES\n')
            with mock.patch.object(sys, 'stdin', terminal_in), \
                 mock.patch.object(sys, 'stdout', terminal_out):
                grant = dispatch._confirm_config_write(snapshot, recheck)
        events = []
        replacement = b'{\"admin_token\":\"next-fake\"}\\n'
        result = replace_config(snapshot, replacement, grant, preflight=recheck,
                                before_replace=lambda: events.append('writing'))
        self.assertEqual(result.state, 'VERIFIED')
        self.assertEqual(events, ['writing'])
        self.assertTrue(self.alias.is_symlink())
        self.assertEqual(self.target.read_bytes(), replacement)
        self.assertEqual((self.target.parent / result.backup_name).read_bytes(), snapshot.data)

    def test_token_bound_to_original_chains_on_connected_socket(self):
        self.launch()
        pair = self.lease._launch_config_pair
        self.lease._verified = object()
        # This is a genuine local connected socket and an owned subprocess.
        # Process/PID/four-tuple checks are mocked here, not claimed as tested.
        parent, child = socket.socketpair()
        self.addCleanup(parent.close)
        script = "import os,sys; fd=int(sys.argv[1]); data=b''\nwhile True:\n b=os.read(fd,8192)\n if not b: break\n data+=b\nprint(len(data))"
        proc = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', script, str(child.fileno())],
                                pass_fds=(child.fileno(),), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env={'HOME': str(ISO / 'home'), 'PATH': os.defpath})
        child.close()
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        instance = control.Instance(123, {'fake': True}, '/fake/python3', 'a' * 32,
                                    '127.0.0.1', 29389, {}, {})
        cli = control.MacControl(self.root, config_path=self.cli)
        calls = []
        def recheck(*args):
            calls.append(1)
            if len(calls) == 2:
                # Change only service link, retaining its final target and bytes.
                self.service.unlink(); self.service.symlink_to(self.target)
        with mock.patch.object(self.lease, 'verify_active', return_value=True), \
             mock.patch.object(cli, '_instance', return_value=instance), \
             mock.patch.object(cli, '_connection', return_value=parent) as connect, \
             mock.patch.object(cli, '_recheck', side_effect=recheck), \
             mock.patch.object(control.platform_process, 'verify_connected_peer'):
            with self.assertRaises(control.ControlError): cli._request(self.lease, 'status')
            self.assertEqual(connect.call_count, 1)
        parent.close()
        out, err = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(out.strip(), b'0')
        self.assertIs(self.lease._token_config_pair, pair)

    def test_snapshot_content_change_invalidates_token(self):
        self.launch()
        self.lease._verified = object()
        with mock.patch.object(self.lease, 'verify_active', return_value=True):
            self.lease.admin_token()
        self.target.write_text('{"admin_token":"different-fake"}')
        with self.assertRaises(control.ControlError): self.lease.verify_admin_token()

    def test_cli_link_change_after_peer_check_sends_nothing(self):
        self.launch()
        self.lease._verified = object()
        instance = control.Instance(123, {'fake': True}, '/fake/python3', 'a' * 32,
                                    '127.0.0.1', 29389, {}, {})
        cli = control.MacControl(self.root, config_path=self.cli)
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        checks = []
        def recheck(*args):
            checks.append(1)
            if len(checks) == 2:
                self.alias.unlink()
                self.alias.symlink_to(self.root / 'config/inner', target_is_directory=True)
        with mock.patch.object(self.lease, 'verify_active', return_value=True), \
             mock.patch.object(cli, '_instance', return_value=instance), \
             mock.patch.object(cli, '_connection', return_value=sock) as connect, \
             mock.patch.object(cli, '_recheck', side_effect=recheck), \
             mock.patch.object(control.platform_process, 'verify_connected_peer'):
            with self.assertRaises(control.ControlError):
                cli._request(self.lease, 'status')
            connect.assert_called_once()
            sock.sendall.assert_not_called()


if __name__ == '__main__':
    unittest.main()
