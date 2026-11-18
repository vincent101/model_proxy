"""Offline fault injection for private installation files; all data is synthetic."""
import os
import contextlib
import io
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
from core import secure_files as sf


def isolated_secure_replace(protection):
    """Run the real writer code with private, test-only protection globals."""
    namespace = dict(sf.secure_replace.__globals__)
    namespace['_mac_fd_protection'] = lambda fd, *, directory: ('fake', ())
    namespace['mac_private_protection'] = protection
    for name in ('_open_private', 'secure_replace'):
        original = namespace[name]
        cloned = types.FunctionType(original.__code__, namespace, original.__name__,
                                    original.__defaults__, original.__closure__)
        cloned.__kwdefaults__ = original.__kwdefaults__
        namespace[name] = cloned
    return namespace['secure_replace']


class BackupCloseFailure(unittest.TestCase):
    def setUp(self):
        super().setUp()
        from core.cli import _install_ops as install
        original_events, original_values = install._EVENTS, list(install._EVENTS)
        original_cancelled = install._CANCELLED
        original_probe, original_probe_values = install._AUDIT_PROBE_STATE, list(install._AUDIT_PROBE_STATE)
        def restore_state():
            original_events[:] = original_values
            install._EVENTS = original_events
            install._CANCELLED = original_cancelled
            original_probe[:] = original_probe_values
            install._AUDIT_PROBE_STATE = original_probe
        self.addCleanup(restore_state)
        install._EVENTS = []
        install._CANCELLED = False
        guard_token = install._COMMIT_GUARD.set(None)
        self.addCleanup(install._COMMIT_GUARD.reset, guard_token)
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        self.tmp = tempfile.TemporaryDirectory(prefix='mp-backup-close-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        project = self.root / 'project'
        (project / 'config').mkdir(parents=True)
        (project / 'runtime').mkdir()
        config = project / 'config' / 'fake.json'
        config.write_text('{"supplies":[],"routes":[],"strategies":[]}')
        config.chmod(0o600)
        (project / 'config' / 'runtime_paths.json').write_text(json.dumps({
            key: str(project / 'runtime' / key)
            for key in ('log', 'totals', 'lock', 'pid', 'ensure_log', 'start_lock')
        }))
        token = install._WRITE_CONTEXT.set({'root': project, 'config': config, 'targets': []})
        self.addCleanup(install._WRITE_CONTEXT.reset, token)
        for patcher in (
            mock.patch.dict(os.environ, {'HOME': str(self.root),
                                         'USERPROFILE': str(self.root),
                                         'LOCALAPPDATA': str(self.root / 'appdata')}),
            mock.patch.object(install, '_MODEL_PROXY_DIR', project),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.target = self.root / 'client.json'
        self.original = b'FAKE_PRIVATE_CONTENT_DO_NOT_PRINT'
        self.target.write_bytes(self.original)
        self.target.chmod(0o600)
        self.snapshot = sf.capture_target(self.target, authorized_root=self.root,
                                          protection_reader=self.protection, max_bytes=4096)
        native_fd_protection = sf._mac_fd_protection
        native_path_protection = sf.mac_private_protection
        native_writer = sf.secure_replace
        native_open_private = sf._open_private
        native_install_replace = install.secure_replace
        native_install_protection = install.mac_private_protection
        self.assertEqual(native_fd_protection.__module__, 'core.secure_files')
        self.assertIs(native_install_replace, native_writer)
        self.assertIs(native_install_protection, native_path_protection)
        self.secure_replace = isolated_secure_replace(self.protection)
        def assert_protection_restored():
            import sys
            self.assertIs(sf._mac_fd_protection, native_fd_protection)
            self.assertIs(sf.mac_private_protection, native_path_protection)
            self.assertIs(sf.secure_replace, native_writer)
            self.assertIs(sf._open_private, native_open_private)
            self.assertIs(install.secure_replace, native_install_replace)
            self.assertIs(install.mac_private_protection, native_install_protection)
            consumers = {
                'core.mac_config_path': ('SecureSnapshotError', '_mac_fd_protection',
                                         '_mac_local_apfs', '_mac_native'),
                'core.mac_config_writer': ('_open_private', '_mac_fd_protection'),
                'core.cli.authority': ('ProtectionEvidence', 'SecureSnapshotError',
                                       'capture_target', 'mac_private_protection'),
            }
            for name, attributes in consumers.items():
                module = sys.modules.get(name)
                if module is not None:
                    for attribute in attributes:
                        self.assertIs(getattr(module, attribute), getattr(sf, attribute))
        self.addCleanup(assert_protection_restored)

    @staticmethod
    def protection(path, info):
        return sf.ProtectionEvidence(True, (stat.S_IMODE(info.st_mode), info.st_uid,
                                             info.st_gid, None, None, None, ()))

    def run_fault(self, *, close='no_close', cleanup='normal', creation_fails=False,
                  first_fstat_fails=False, writing_fails=False, parent_close_fails=False,
                  via_cli=False, on_replace=None):
        real_open, real_close = os.open, os.close
        real_unlink, real_fstat, real_write, real_stat = os.unlink, os.fstat, os.write, os.stat
        seen = {'fd': None, 'name': None, 'closes': 0, 'sentinel_fd': None, 'parent_fd': None}
        moved = self.root / 'moved-original-backup'

        def open_file(path, flags, *args, **kw):
            if isinstance(path, str) and '.bak.' in path:
                seen['name'] = path
                if creation_fails:
                    raise OSError('FAKE_PRIVATE_CONTENT_DO_NOT_PRINT')
                seen['fd'] = real_open(path, flags, *args, **kw)
                seen['parent_fd'] = kw['dir_fd']
                return seen['fd']
            return real_open(path, flags, *args, **kw)

        def close_file(fd):
            if fd == seen['fd']:
                seen['closes'] += 1
                if close == 'no_close':
                    raise OSError('FAKE_PRIVATE_CONTENT_DO_NOT_PRINT')
                if close == 'already_closed':
                    real_close(fd)
                    seen['sentinel_fd'] = real_open(str(self.target), os.O_RDONLY)
                    self.assertEqual(seen['sentinel_fd'], fd)
                    raise OSError('FAKE_PRIVATE_CONTENT_DO_NOT_PRINT')
            if parent_close_fails and fd == seen['parent_fd']:
                real_close(fd)
                raise OSError('FAKE_PRIVATE_CONTENT_DO_NOT_PRINT')
            return real_close(fd)

        def unlink_file(path, *args, **kw):
            if path == seen['name'] and cleanup == 'fails':
                raise PermissionError('FAKE_PRIVATE_CONTENT_DO_NOT_PRINT')
            return real_unlink(path, *args, **kw)

        def fstat_file(fd):
            if first_fstat_fails and fd == seen['fd']:
                raise OSError('FAKE_PRIVATE_CONTENT_DO_NOT_PRINT')
            return real_fstat(fd)

        def write_file(fd, data):
            if writing_fails and fd == seen['fd']:
                raise OSError('FAKE_PRIVATE_CONTENT_DO_NOT_PRINT')
            return real_write(fd, data)

        def stat_file(path, *args, **kw):
            if path == seen['name'] and cleanup == 'identity_changed':
                os.rename(path, moved.name, src_dir_fd=kw['dir_fd'], dst_dir_fd=kw['dir_fd'])
                fd = real_open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=kw['dir_fd'])
                real_write(fd, b'UNRELATED')
                real_close(fd)
            if path == seen['name'] and cleanup == 'already_gone':
                real_unlink(path, dir_fd=kw['dir_fd'])
            return real_stat(path, *args, **kw)

        try:
            with mock.patch.object(os, 'open', side_effect=open_file), \
                 mock.patch.object(os, 'close', side_effect=close_file), \
                 mock.patch.object(os, 'unlink', side_effect=unlink_file), \
                 mock.patch.object(os, 'fstat', side_effect=fstat_file), \
                 mock.patch.object(os, 'write', side_effect=write_file), \
                 mock.patch.object(os, 'stat', side_effect=stat_file):
                if via_cli:
                    from core.cli import _install_ops as install
                    errors = []
                    output = io.StringIO()
                    def checked_replace(*args, **kwargs):
                        if on_replace is not None:
                            on_replace()
                        try:
                            return self.secure_replace(*args, **kwargs)
                        except sf.SecureWriteError as exc:
                            errors.append(exc)
                            raise
                    with mock.patch.object(install, 'confirm', return_value=True), \
                         mock.patch.object(install, 'secure_replace', side_effect=checked_replace) as replace, \
                         mock.patch.object(install, '_EVENTS', []), \
                         contextlib.redirect_stdout(output):
                        with self.assertRaises(install.InstallFailure) as caught:
                            install.preview_confirm_write(
                                self.target, self.original.decode(), 'FAKE_REPLACEMENT',
                                'test', [], snapshot=self.snapshot)
                        replace.assert_called_once_with(
                            self.snapshot, b'FAKE_REPLACEMENT', max_bytes=install._MAX_CLIENT_BYTES)
                        self.assertEqual(len(errors), 1, 'secure_replace did not raise the injected error')
                        err = errors[0]
                        self.assertEqual(str(caught.exception), '安全提交失败 (' + err.code + ')')
                        self.assertEqual(install._EVENTS, [('test', 'NOT_REPLACED', err.backup)])
                        self.assertIn('目标未替换', output.getvalue())
                        if err.backup:
                            self.assertIn(err.backup, output.getvalue())
                        self.assertNotIn(self.original.decode(), output.getvalue())
                        self.assertNotIn(self.original.decode(), str(caught.exception))
                else:
                    with self.assertRaises(sf.SecureWriteError) as caught:
                        self.secure_replace(self.snapshot, b'FAKE_REPLACEMENT', max_bytes=4096)
                    err = caught.exception
        finally:
            # The double knows what it left open; real code cannot safely retry.
            if seen['sentinel_fd'] is not None:
                real_fstat(seen['sentinel_fd'])
                real_close(seen['sentinel_fd'])
            elif seen['fd'] is not None and close == 'no_close':
                real_close(seen['fd'])
        self.assertEqual(err.state, 'NOT_REPLACED')
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertNotIn(self.original.decode(), str(err))
        self.assertNotIn(self.original.decode(), repr(err))
        self.assertIsNone(err.staging)
        return err, seen, moved

    def test_close_failure_not_closed_but_cleanup_succeeds(self):
        err, seen, _ = self.run_fault()
        self.assertEqual(seen['closes'], 1)
        self.assertEqual(err.code, 'staging_close_unknown')
        self.assertTrue(err.close_unknown)
        self.assertFalse(err.cleanup_unknown)
        self.assertIsNone(err.backup)
        self.assertFalse((self.root / seen['name']).exists())

    def test_close_failure_already_closed_and_fd_reused(self):
        err, seen, _ = self.run_fault(close='already_closed')
        self.assertEqual(seen['closes'], 1)
        self.assertTrue(err.close_unknown)
        self.assertFalse(err.cleanup_unknown)
        self.assertIsNone(err.backup)

    def test_close_and_cleanup_fail_reports_exact_backup_path(self):
        err, seen, _ = self.run_fault(cleanup='fails')
        self.assertEqual(seen['closes'], 1)
        self.assertEqual(err.code, 'staging_close_and_cleanup_unknown')
        self.assertTrue(err.close_unknown)
        self.assertTrue(err.cleanup_unknown)
        self.assertEqual(err.backup, str(self.root / seen['name']))
        self.assertEqual(Path(err.backup).read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(Path(err.backup).stat().st_mode), 0o600)

    def test_close_failure_identity_changed_never_unlinks_foreign_file(self):
        err, seen, moved = self.run_fault(cleanup='identity_changed')
        self.assertEqual(err.backup, str(self.root / seen['name']))
        self.assertTrue(err.close_unknown and err.cleanup_unknown)
        self.assertEqual(Path(err.backup).read_bytes(), b'UNRELATED')
        self.assertEqual(moved.read_bytes(), self.original)

    def test_close_failure_already_absent_reports_no_backup(self):
        err, seen, _ = self.run_fault(cleanup='already_gone')
        self.assertIsNone(err.backup)
        self.assertFalse(err.cleanup_unknown)
        self.assertFalse((self.root / seen['name']).exists())

    def test_creation_failure_does_not_claim_or_delete_a_backup(self):
        err, seen, _ = self.run_fault(creation_fails=True)
        self.assertEqual(err.state, 'NOT_REPLACED')
        self.assertIsNone(err.backup)
        self.assertIsNone(seen['fd'])
        self.assertEqual(seen['closes'], 0)
        self.assertTrue(err.close_unknown)
        self.assertFalse(err.cleanup_unknown)

    def test_first_fstat_failure_leaves_unknown_identity_and_reports_path(self):
        err, seen, _ = self.run_fault(close='normal', first_fstat_fails=True)
        self.assertEqual(err.backup, str(self.root / seen['name']))
        self.assertTrue(err.cleanup_unknown)
        self.assertFalse(err.close_unknown)
        self.assertTrue(Path(err.backup).exists())

    def test_write_failure_and_close_failure_preserve_both_unknowns(self):
        err, seen, _ = self.run_fault(writing_fails=True, cleanup='fails')
        self.assertEqual(err.backup, str(self.root / seen['name']))
        self.assertTrue(err.close_unknown and err.cleanup_unknown)
        self.assertEqual(seen['closes'], 1)

    def test_write_failure_cleanup_success_keeps_original_error(self):
        err, seen, _ = self.run_fault(close='normal', writing_fails=True)
        self.assertEqual(err.code, 'filesystem_error')
        self.assertIsNone(err.backup)
        self.assertFalse(err.close_unknown or err.cleanup_unknown)
        self.assertEqual(seen['closes'], 1)

    def test_write_failure_cleanup_failure_is_reported_even_if_close_succeeds(self):
        err, seen, _ = self.run_fault(close='normal', writing_fails=True, cleanup='fails')
        self.assertEqual(err.code, 'staging_cleanup_failed')
        self.assertEqual(err.backup, str(self.root / seen['name']))
        self.assertFalse(err.close_unknown)
        self.assertTrue(err.cleanup_unknown)

    def test_parent_close_failure_preserves_backup_and_unknown_flags(self):
        err, seen, _ = self.run_fault(cleanup='fails', parent_close_fails=True)
        self.assertEqual(err.code, 'parent_close_and_cleanup_unknown')
        self.assertEqual(err.backup, str(self.root / seen['name']))
        self.assertTrue(err.close_unknown and err.cleanup_unknown)

    def test_backup_name_collision_does_not_delete_foreign_file(self):
        collision = self.root / 'client.json.bak.abcdef'
        collision.write_bytes(b'UNRELATED')
        with mock.patch('secrets.token_hex', return_value='abcdef'):
            with self.assertRaises(sf.SecureWriteError) as caught:
                self.secure_replace(self.snapshot, b'FAKE_REPLACEMENT', max_bytes=4096)
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertIsNone(caught.exception.backup)
        self.assertEqual(collision.read_bytes(), b'UNRELATED')
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_parent_close_failure_after_replacement_reports_replaced(self):
        real_open, real_close = os.open, os.close
        seen = {'opens': 0, 'parent_fd': None, 'closes': 0}
        def open_file(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if path == str(self.root) and flags & os.O_DIRECTORY:
                seen['opens'] += 1
                if seen['opens'] == 2:
                    seen['parent_fd'] = fd
            return fd
        def close_file(fd):
            if fd == seen['parent_fd']:
                seen['closes'] += 1
                real_close(fd)
                raise OSError('FAKE_PRIVATE_CONTENT_DO_NOT_PRINT')
            return real_close(fd)
        with mock.patch.object(os, 'open', side_effect=open_file), \
             mock.patch.object(os, 'close', side_effect=close_file):
            with self.assertRaises(sf.SecureWriteError) as caught:
                self.secure_replace(self.snapshot, b'FAKE_REPLACEMENT', max_bytes=4096)
        err = caught.exception
        self.assertEqual(seen['closes'], 1)
        self.assertEqual(err.code, 'parent_close_unknown_after_verified')
        self.assertEqual(err.state, 'POSTCHECK_FAILED')
        self.assertTrue(err.close_unknown)
        self.assertEqual(self.target.read_bytes(), b'FAKE_REPLACEMENT')
        self.assertEqual(Path(err.backup).read_bytes(), self.original)

    def test_install_summary_verified_release_unknown_stops_all_later_writes(self):
        import sys
        from core.cli import _install_ops as install

        old = '{"env":{"PRIVATE":"FAKE_OLD_SECRET"}}'
        token = 'FAKE_CLIENT_TOKEN_NEVER_PRINT'
        self.target.write_text(old)
        config = self.root / 'proxy-config.json'
        config.write_text('{"admin_token":"FAKE_ADMIN_SECRET"}')
        later = self.root / 'later.json'
        later.write_text('{"untouched":true}')
        sdks = dict(install.SDKS)
        sdks['claude'] = dict(sdks['claude'], config_path=self.target)
        sdks['openclaw'] = dict(sdks['openclaw'], config_path=later)
        next_installer = mock.Mock(side_effect=AssertionError('later installer must not run'))
        installers = dict(install._INSTALLERS, openclaw=next_installer)
        real_open, real_close = os.open, os.close
        seen = {'parent_fd': None, 'closes': 0}

        def open_file(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if isinstance(path, str) and '.bak.' in path:
                seen['parent_fd'] = kwargs['dir_fd']
            return fd

        def close_file(fd):
            if fd == seen['parent_fd']:
                seen['closes'] += 1
                real_close(fd)
                raise OSError('FAKE_INTERNAL_SECRET')
            return real_close(fd)

        output = io.StringIO()
        with mock.patch.dict(os.environ, {'HOME': str(self.root)}), \
             mock.patch.object(install, 'SDKS', sdks), \
             mock.patch.object(install, '_INSTALLERS', installers), \
             mock.patch.object(install, 'mac_private_protection', self.protection), \
             mock.patch.object(install, 'ensure_session_hook'), \
             mock.patch.object(install, '_ensure_onboarding_completed') as onboarding, \
             mock.patch.object(install, 'choose_token', return_value=token) as chooser, \
             mock.patch.object(install, 'confirm', return_value=True), \
             mock.patch.object(install, 'secure_replace', wraps=self.secure_replace) as replace, \
             mock.patch.object(os, 'open', side_effect=open_file), \
             mock.patch.object(os, 'close', side_effect=close_file), \
             mock.patch.object(sys, 'argv', ['install', 'install', str(config), '28371', 'claude,openclaw']), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            rc = install.main()
            replace.assert_called_once()
            self.assertEqual(Path(replace.call_args.args[0].path), self.target)

        text = output.getvalue()
        self.assertEqual(rc, 1)
        self.assertEqual(seen['closes'], 1)
        self.assertEqual(chooser.call_count, 1)
        onboarding.assert_not_called()
        next_installer.assert_not_called()
        self.assertEqual(later.read_text(), '{"untouched":true}')
        self.assertIn(token, self.target.read_text())
        backups = list(self.root.glob('client.json.bak.*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), old)
        self.assertEqual(install._EVENTS, [('claude', 'VERIFIED_RELEASE_UNKNOWN', str(backups[0]))])
        self.assertIn('目标已替换、后验通过、目录句柄关闭状态未知，停止后续写入', text)
        self.assertIn('claude: VERIFIED_RELEASE_UNKNOWN', text)
        self.assertIn(str(backups[0]), text)
        self.assertIn('安装未完成', text)
        self.assertNotIn('后验验证失败', text)
        self.assertNotIn('Claude 客户端配置已更新', text)
        for secret in (token, 'FAKE_OLD_SECRET', 'FAKE_ADMIN_SECRET', 'FAKE_INTERNAL_SECRET'):
            self.assertNotIn(secret, text)

    def test_other_postcheck_errors_keep_original_failure_summary(self):
        from core.cli import _install_ops as install

        for code in ('postcheck_failed', 'parent_close_unknown', 'parent_close_and_cleanup_unknown'):
            with self.subTest(code=code):
                backup = str(self.root / 'possible-backup')
                error = sf.SecureWriteError(code, state='POSTCHECK_FAILED', backup=backup)
                output = io.StringIO()
                with mock.patch.object(install, '_EVENTS', []), \
                     mock.patch.object(install, 'confirm', return_value=True), \
                     mock.patch.object(install, 'secure_replace', side_effect=error) as replace, \
                     contextlib.redirect_stdout(output):
                    with self.assertRaises(install.InstallFailure):
                        install.preview_confirm_write(self.target, self.original.decode(),
                                                      'FAKE_NEW_TOKEN', 'test', [],
                                                      snapshot=self.snapshot)
                    replace.assert_called_once_with(
                        self.snapshot, b'FAKE_NEW_TOKEN', max_bytes=install._MAX_CLIENT_BYTES)
                    self.assertEqual(install._EVENTS, [('test', 'POSTCHECK_FAILED', backup)])
                text = output.getvalue()
                self.assertIn('目标已替换，后验验证失败', text)
                self.assertNotIn('后验通过', text)
                self.assertNotIn('VERIFIED_RELEASE_UNKNOWN', text)
                self.assertNotIn('FAKE_NEW_TOKEN', text)
                self.assertNotIn(self.original.decode(), text)

    def test_release_unknown_without_backup_does_not_claim_one(self):
        from core.cli import _install_ops as install

        error = sf.SecureWriteError('parent_close_unknown_after_verified',
                                    state='POSTCHECK_FAILED', backup=None, close_unknown=True)
        output = io.StringIO()
        with mock.patch.object(install, '_EVENTS', []), \
             mock.patch.object(install, 'confirm', return_value=True), \
             mock.patch.object(install, 'secure_replace', side_effect=error) as replace, \
             contextlib.redirect_stdout(output):
            with self.assertRaises(install.InstallFailure):
                install.preview_confirm_write(self.target, self.original.decode(),
                                              'FAKE_NEW_TOKEN', 'test', [], snapshot=self.snapshot)
            replace.assert_called_once_with(
                self.snapshot, b'FAKE_NEW_TOKEN', max_bytes=install._MAX_CLIENT_BYTES)
            self.assertEqual(install._EVENTS, [('test', 'VERIFIED_RELEASE_UNKNOWN', None)])
        self.assertIn('后验通过', output.getvalue())
        self.assertNotIn('备份或残留文件:', output.getvalue())
        self.assertNotIn('后验验证失败', output.getvalue())
        self.assertNotIn('FAKE_NEW_TOKEN', output.getvalue())

    def test_install_record_reports_residual_without_secrets(self):
        err, seen, _ = self.run_fault(cleanup='fails', via_cli=True)
        self.assertEqual(err.code, 'staging_close_and_cleanup_unknown')
        self.assertTrue(err.close_unknown)
        self.assertTrue(err.cleanup_unknown)
        self.assertEqual(seen['closes'], 1)
        self.assertEqual(err.backup, str(self.root / seen['name']))
        self.assertEqual(Path(err.backup).read_bytes(), self.original)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertIsNone(err.staging)
        self.assertEqual(list(self.root.glob('.model-proxy-install-*')), [])
        self.assertEqual(list(self.root.glob('client.json.bak.*')), [Path(err.backup)])

    def test_staging_close_failure_preserves_backup_and_staging_path(self):
        real_open, real_close, real_unlink = os.open, os.close, os.unlink
        seen = {'fd': None, 'name': None, 'closes': 0, 'sentinel': None}
        def open_file(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if isinstance(path, str) and path.startswith('.model-proxy-install-'):
                seen.update(fd=fd, name=path)
            return fd
        def close_file(fd):
            if fd == seen['fd']:
                seen['closes'] += 1
                real_close(fd)
                seen['sentinel'] = real_open(str(self.target), os.O_RDONLY)
                self.assertEqual(seen['sentinel'], fd)
                raise OSError('FAKE_PRIVATE_CONTENT_DO_NOT_PRINT')
            return real_close(fd)
        def unlink_file(path, *args, **kwargs):
            if path == seen['name']:
                raise OSError('FAKE_PRIVATE_CONTENT_DO_NOT_PRINT')
            return real_unlink(path, *args, **kwargs)
        try:
            with mock.patch.object(os, 'open', side_effect=open_file), \
                 mock.patch.object(os, 'close', side_effect=close_file), \
                 mock.patch.object(os, 'unlink', side_effect=unlink_file):
                with self.assertRaises(sf.SecureWriteError) as caught:
                    self.secure_replace(self.snapshot, b'FAKE_REPLACEMENT', max_bytes=4096)
        finally:
            if seen['sentinel'] is not None:
                os.fstat(seen['sentinel'])
                real_close(seen['sentinel'])
        err = caught.exception
        self.assertEqual(seen['closes'], 1)
        self.assertEqual(err.state, 'NOT_REPLACED')
        self.assertTrue(err.close_unknown and err.cleanup_unknown)
        self.assertEqual(err.staging, str(self.root / seen['name']))
        self.assertEqual(Path(err.staging).read_bytes(), b'FAKE_REPLACEMENT')
        self.assertEqual(Path(err.backup).read_bytes(), self.original)
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_success_and_private_permissions(self):
        outcome = self.secure_replace(self.snapshot, b'FAKE_REPLACEMENT', max_bytes=4096)
        self.assertEqual(outcome.state, 'VERIFIED')
        self.assertEqual(self.target.read_bytes(), b'FAKE_REPLACEMENT')
        self.assertEqual(Path(outcome.backup).read_bytes(), self.original)
        for path in (self.target, Path(outcome.backup)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipUnless(sys.platform == 'darwin', 'native macOS protection required')
    def test_late_consumers_keep_native_protection_after_cleanup(self):
        """Fresh children import consumers during a fault, then verify real 0644 rejection."""
        script = r'''
import fcntl, importlib, os, socket, subprocess, sys, sysconfig, unittest
from pathlib import Path
sys.dont_write_bytecode = True
project, scratch, scenario = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), sys.argv[3]
canary = Path(sys.argv[4]).resolve()
assert project == scratch / 'project'
assert Path(os.environ['HOME']).is_relative_to(scratch)
assert Path(os.environ['TMPDIR']).is_relative_to(scratch)
assert not canary.is_relative_to(scratch)
sys.path.insert(0, str(project))
stdlib = Path(sysconfig.get_path('stdlib')).resolve()

def denied(*args, **kwargs):
    raise PermissionError('offline child: network/process denied')
socket.socket.connect = socket.socket.connect_ex = socket.socket.bind = denied
socket.getaddrinfo = socket.gethostbyname = socket.gethostbyname_ex = denied
socket.socket.sendto = denied
if hasattr(socket.socket, 'sendmsg'):
    socket.socket.sendmsg = denied
subprocess.Popen = denied
for name in ('kill', 'killpg', 'system', 'popen', 'fork', 'forkpty', 'posix_spawn', 'posix_spawnp'):
    if hasattr(os, name):
        setattr(os, name, denied)

real_open = os.open
def permitted(path, writing, directory_only=False):
    path = Path(path).resolve()
    if path.is_relative_to(scratch):
        return True
    if writing:
        return False
    return path.is_relative_to(stdlib) or (directory_only and path in scratch.parents)
def guarded_open(path, flags, *args, **kwargs):
    p = Path(os.fsdecode(path))
    if not p.is_absolute() and kwargs.get('dir_fd') is not None:
        parent = fcntl.fcntl(kwargs['dir_fd'], 50, b'\0' * 1024).split(b'\0', 1)[0]
        p = Path(os.fsdecode(parent)) / p
    writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
    if not permitted(p, writing, bool(flags & os.O_DIRECTORY)):
        raise PermissionError('offline child: file outside scratch')
    return real_open(path, flags, *args, **kwargs)
os.open = guarded_open
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        path, mode, flags = os.fsdecode(args[0]), args[1], args[2]
        writing = ((isinstance(mode, str) and any(c in mode for c in 'wax+')) or
                   (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR |
                    os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
        directory_only = isinstance(flags, int) and bool(flags & os.O_DIRECTORY)
        if not permitted(path, writing, directory_only):
            raise PermissionError('offline child: file outside scratch')
sys.addaudithook(audit)

# Exercise both Python file APIs against a fake file outside this child's root.
for attempt in (lambda: open(canary, 'rb'), lambda: open(canary, 'wb'),
                lambda: os.open(canary, os.O_RDONLY), lambda: os.open(canary, os.O_WRONLY)):
    try:
        resource = attempt()
    except PermissionError:
        pass
    else:
        if isinstance(resource, int):
            os.close(resource)
        else:
            resource.close()
        raise AssertionError('outside-root canary was opened')

from core import secure_files as sf
from tests.test_backup_close_failure import BackupCloseFailure
assert Path(sf.__file__).resolve().is_relative_to(project)
assert Path(sys.modules[BackupCloseFailure.__module__].__file__).resolve().is_relative_to(project)
native_fd, native_path, native_writer = sf._mac_fd_protection, sf.mac_private_protection, sf.secure_replace
assert native_fd.__module__ == 'core.secure_files'
names = ('core.mac_config_path', 'core.mac_config_writer', 'core.cli.authority')
attributes = (('SecureSnapshotError', '_mac_fd_protection', '_mac_local_apfs', '_mac_native'),
              ('_open_private', '_mac_fd_protection'),
              ('ProtectionEvidence', 'SecureSnapshotError', 'capture_target', 'mac_private_protection'))
native_imports = {attribute: getattr(sf, attribute) for imported in attributes for attribute in imported}
assert all(name not in sys.modules for name in names), 'consumers preloaded'
observed = []
def check_imports(modules):
    for module, imported in zip(modules, attributes):
        assert Path(module.__file__).resolve().is_relative_to(project)
        for attribute in imported:
            assert getattr(module, attribute) is native_imports[attribute], (module.__name__, attribute)

class Probe(BackupCloseFailure):
    def runTest(self):
        self.assertIs(self.secure_replace.__code__, native_writer.__code__)
        self.assertIsNot(self.secure_replace.__globals__['_mac_fd_protection'], native_fd)
        def import_during_fault():
            self.assertIn(names[0], sys.modules)  # real write preflight
            self.assertNotIn(names[1], sys.modules)
            self.assertNotIn(names[2], sys.modules)
            path, writer, authority = (importlib.import_module(name) for name in names)
            check_imports((path, writer, authority))
            self.assertIs(path._mac_fd_protection, native_fd)
            self.assertIs(writer._mac_fd_protection, native_fd)
            self.assertIs(authority.mac_private_protection, native_path)
            observed.append(True)
        err, seen, _ = self.run_fault(cleanup='fails', via_cli=True, on_replace=import_during_fault)
        self.assertEqual(err.code, 'staging_close_and_cleanup_unknown')
        self.assertTrue(err.close_unknown and err.cleanup_unknown)
        self.assertEqual(seen['closes'], 1)
        self.assertEqual(Path(err.backup).read_bytes(), self.original)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertIsNone(err.staging)
        if scenario == 'exception':
            raise RuntimeError('FAKE_EXPECTED_BODY_ERROR')

result = unittest.TestResult()
Probe().run(result)
assert result.testsRun == 1 and not result.failures, (result.failures, result.errors)
assert observed == [True], ('late-import callback not completed', result.errors)
if scenario == 'normal':
    assert not result.errors, result.errors
else:
    assert len(result.errors) == 1 and 'FAKE_EXPECTED_BODY_ERROR' in result.errors[0][1], result.errors
path, writer, authority = (sys.modules[name] for name in names)
check_imports((path, writer, authority))
assert sf._mac_fd_protection is native_fd and sf.mac_private_protection is native_path
assert sf.secure_replace is native_writer
assert path._mac_fd_protection is native_fd and writer._mac_fd_protection is native_fd
assert authority.mac_private_protection is native_path
from core.cli import _install_ops as install
assert install.secure_replace is native_writer and install.mac_private_protection is native_path
negative = scratch / 'home' / 'negative.json'
negative.write_text('{}')
negative.chmod(0o644)
try:
    path.capture_config(negative)
except path.MacConfigError as exc:
    assert exc.code == 'file_permissions_unsafe', exc.code
else:
    raise AssertionError('native 0644 protection did not reject')
print('CHILD_OK:' + scenario)
'''
        source = Path(__file__).resolve().parent.parent
        for scenario in ('normal', 'exception'):
            with self.subTest(scenario=scenario):
                scratch = self.root / ('child-' + scenario)
                home, tmp = scratch / 'home', scratch / 'tmp'
                home.mkdir(parents=True, mode=0o700)
                tmp.mkdir(mode=0o700)
                project = scratch / 'project'
                core = source / 'core'
                self.assertFalse(core.is_symlink())
                for directory, dirs, files in os.walk(core, followlinks=False):
                    directory = Path(directory)
                    for name in dirs:
                        self.assertFalse((directory / name).is_symlink())
                    dirs[:] = [name for name in dirs if name != '__pycache__']
                    for name in files:
                        if name.endswith('.py'):
                            src = directory / name
                            self.assertFalse(src.is_symlink())
                            dest = project / src.relative_to(source)
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(src, dest)
                for name in ('__init__.py', 'test_backup_close_failure.py'):
                    src = source / 'tests' / name
                    self.assertFalse(src.is_symlink())
                    dest = project / 'tests' / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dest)
                canary = self.root / ('outside-canary-' + scenario)
                canary.write_bytes(b'FAKE_OUTSIDE_CANARY')
                env = {'HOME': str(home), 'USERPROFILE': str(home),
                       'LOCALAPPDATA': str(home / 'appdata'), 'APPDATA': str(home / 'appdata'),
                       'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_CACHE_HOME': str(home / 'cache'),
                       'TMPDIR': str(tmp), 'TMP': str(tmp), 'TEMP': str(tmp), 'PATH': os.defpath}
                completed = subprocess.run(
                    [sys.executable, '-I', '-S', '-B', '-c', script, str(project), str(scratch),
                     scenario, str(canary)],
                    cwd=str(scratch), env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertIn('CHILD_OK:' + scenario, completed.stdout)
                self.assertEqual(canary.read_bytes(), b'FAKE_OUTSIDE_CANARY')


if __name__ == '__main__':
    unittest.main()
