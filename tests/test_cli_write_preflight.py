"""Isolated CLI write-preflight tests; never use the project configuration."""
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from core.cli import dispatch
from core.discovery import PreflightError
from core.runtime_paths import PATH_KEYS


class Control:
    def __init__(self, result=0):
        self.result = result
        self.calls = []

    def reload(self):
        self.calls.append('reload')
        return self.result


class WritePreflightTests(unittest.TestCase):
    def setUp(self):
        # The macOS per-user temp hierarchy may carry rootless attributes
        # rejected by the real config-path protection check.
        self.temp = tempfile.TemporaryDirectory(
            prefix='mp-write-preflight-',
            dir='/private/tmp' if sys.platform == 'darwin' else None)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'project'
        self.home = self.base / 'home'
        self.tmp = self.base / 'tmp'
        for directory in (self.root / 'config', self.root / 'runtime', self.home, self.tmp):
            directory.mkdir(parents=True)
        self.tmp.chmod(0o700)
        self.config = self.root / 'config' / 'fake.json'
        self.config.write_text(json.dumps({
            'routes': [{'id': 'old'}, {'id': 'new'}],
            'strategies': [{'client_token': 'fake-token', 'route_id': 'old'}],
            'supplies': [],
        }))
        self.config.chmod(0o600)
        self.path_file = self.root / 'config' / 'runtime_paths.json'
        self.paths = {name: str(self.root / 'runtime' / name) for name in PATH_KEYS}
        self.write_paths()
        self.original_tempdir = tempfile.tempdir
        tempfile.tempdir = str(self.tmp)
        self.addCleanup(setattr, tempfile, 'tempdir', self.original_tempdir)
        env = {
            'HOME': str(self.home), 'USERPROFILE': str(self.home),
            'LOCALAPPDATA': str(self.home / 'Local'),
            'TMPDIR': str(self.tmp), 'TEMP': str(self.tmp), 'TMP': str(self.tmp),
            'MODEL_PROXY_CONFIG': str(self.config), 'PYTHONDONTWRITEBYTECODE': '1',
            'PATH': os.defpath,
        }
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.control = Control()

    def write_paths(self):
        self.path_file.write_text(json.dumps(self.paths))

    def main(self, args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return dispatch.main(args, root=self.root, control=self.control)

    def install_fake_helper(self):
        shutil.copytree(Path(dispatch.__file__).resolve().parents[1], self.root / 'core',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))

    def menu_flow_identity(self):
        # These menu-flow tests mock every helper, so no write can occur.
        # Windows temp directories need not satisfy the native ACL policy;
        # retain real path/config parsing without claiming ACL verification.
        if os.name == 'nt':
            return mock.patch.object(dispatch, 'inspect_runtime_identities')
        return contextlib.nullcontext()

    @contextlib.contextmanager
    def prepared_write(self):
        from core.cli import _config_ops
        with mock.patch.object(dispatch, 'ROOT', self.root), \
             mock.patch.multiple(_config_ops, _WRITE_BASELINE=None, _WRITE_STARTED=False,
                                 _WRITE_OPERATION='switch', _WRITE_AUTHORIZATION=None,
                                 _WRITE_PREFLIGHT=None, _WRITE_INPUT_PATH=None,
                                 _WRITE_COMMITTED=None, _MARKER_BASELINE=None, _MARKER_CONTENT=None,
                                 _MARKER_REQUIRED=False):
            _config_ops._prepare_write(str(self.config), 'switch')
            yield _config_ops

    def test_switch_success_executes_real_helper_and_reloads_once(self):
        self.install_fake_helper()
        self.assertEqual(self.main(['switch', 'fake-token', 'new']), 0)
        self.assertEqual(json.loads(self.config.read_text())['strategies'][0]['route_id'], 'new')
        self.assertEqual(self.control.calls, ['reload'])

    def test_switch_reload_failure_is_reported_after_save(self):
        self.install_fake_helper()
        self.control.result = 9
        self.assertEqual(self.main(['switch', 'fake-token', 'new']), 9)
        self.assertEqual(json.loads(self.config.read_text())['strategies'][0]['route_id'], 'new')
        self.assertEqual(self.control.calls, ['reload'])

    def test_bad_runtime_file_blocks_install_and_switch_without_helper(self):
        for content in (None, '{', '[]', '{}', '{"log": null}'):
            with self.subTest(content=content):
                if content is None:
                    self.path_file.unlink(missing_ok=True)
                else:
                    self.path_file.write_text(content)
                before = self.config.read_bytes()
                with mock.patch.object(dispatch, 'run_helper') as helper:
                    self.assertNotEqual(self.main(['switch', 'fake-token', 'new']), 0)
                    self.assertNotEqual(self.main(['install']), 0)
                    helper.assert_not_called()
                self.assertEqual(self.config.read_bytes(), before)
                self.assertEqual(self.control.calls, [])

    def test_missing_or_corrupt_business_config_blocks_writes(self):
        for content in (None, '{', '[]', 'null', b'\xff'):
            with self.subTest(content=content):
                if content is None:
                    self.config.unlink(missing_ok=True)
                elif isinstance(content, bytes):
                    self.config.write_bytes(content)
                else:
                    self.config.write_text(content)
                before = self.config.read_bytes() if self.config.exists() else None
                with mock.patch.object(dispatch, 'run_helper') as helper:
                    self.assertNotEqual(self.main(['switch', 'fake-token', 'new']), 0)
                    self.assertNotEqual(self.main(['install']), 0)
                    helper.assert_not_called()
                self.assertEqual(self.config.read_bytes() if self.config.exists() else None, before)
                self.assertEqual(self.control.calls, [])

    def test_all_pairs_of_runtime_roles_conflict(self):
        for i, left in enumerate(PATH_KEYS):
            for right in PATH_KEYS[i + 1:]:
                with self.subTest(left=left, right=right):
                    self.paths[right] = self.paths[left]
                    self.write_paths()
                    with mock.patch.object(dispatch, 'run_helper') as helper:
                        self.assertNotEqual(self.main(['switch', 'fake-token', 'new']), 0)
                        self.assertNotEqual(self.main(['install']), 0)
                        helper.assert_not_called()
                    self.paths[right] = str(self.root / 'runtime' / right)
        self.assertEqual(self.control.calls, [])

    def test_fixed_state_nested_hardlink_and_symlink_conflicts(self):
        first = self.root / 'runtime' / 'log'
        first.write_text('fake runtime data')
        hardlink = self.root / 'runtime' / 'hardlink'
        os.link(first, hardlink)
        symlink = self.root / 'runtime' / 'alias'
        symlink.symlink_to(first)
        cases = [
            ('lock', self.root / '.runtime' / 'anchor.lock'),
            ('start_lock', self.root / '.runtime'),
            ('totals', hardlink),
            ('pid', symlink),
            ('pid', first / 'nested'),
        ]
        for role, path in cases:
            with self.subTest(role=role, path=path.name):
                old = self.paths[role]
                self.paths[role] = str(path)
                self.write_paths()
                with mock.patch.object(dispatch, 'run_helper') as helper:
                    self.assertNotEqual(self.main(['switch', 'fake-token', 'new']), 0)
                    helper.assert_not_called()
                self.paths[role] = old
        self.assertEqual(first.read_text(), 'fake runtime data')

    def test_config_or_runtime_paths_file_cannot_alias_runtime_roles(self):
        for role in PATH_KEYS:
            for candidate in (self.config, self.path_file):
                with self.subTest(role=role, candidate=candidate.name):
                    self.paths[role] = str(candidate)
                    self.write_paths()
                    before = self.config.read_bytes()
                    with mock.patch.object(dispatch, 'run_helper') as helper:
                        self.assertNotEqual(self.main(['switch', 'fake-token', 'new']), 0)
                        helper.assert_not_called()
                    self.assertEqual(self.config.read_bytes(), before)
                    self.paths[role] = str(self.root / 'runtime' / role)

    def test_unprovable_identity_fails_closed(self):
        with mock.patch.object(dispatch, 'inspect_runtime_identities',
                               side_effect=OSError('fake failure')), \
             mock.patch.object(dispatch, 'run_helper') as helper:
            self.assertNotEqual(self.main(['switch', 'fake-token', 'new']), 0)
            self.assertNotEqual(self.main(['install']), 0)
            helper.assert_not_called()
        self.assertEqual(self.control.calls, [])

    def test_conflicts_report_the_identity_failure_without_config_content(self):
        cases = (
            ('pid', self.paths['lock'], 'same_identity'),
            # A runtime role aimed at a fixed record is the same object; the
            # role-collision check reports that identity, not a directory overlap.
            ('pid', str(self.root / '.runtime' / 'anchor.lock'), 'same_identity'),
            ('pid', str(self.config), 'same_identity'),
        )
        for role, target, expected in cases:
            with self.subTest(expected=expected):
                self.paths[role] = target
                self.write_paths()
                error = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(error), \
                     mock.patch.object(dispatch, 'run_helper') as helper:
                    self.assertNotEqual(dispatch.main(['switch', 'fake-token', 'new'],
                                                      root=self.root, control=self.control), 0)
                    helper.assert_not_called()
                self.assertIn(expected, error.getvalue())
                self.assertNotIn('fake-token', error.getvalue())

    def test_list_and_quit_remain_available_with_bad_paths(self):
        self.path_file.write_text('{')
        for kind in ('supply', 'route', 'strategy'):
            with self.subTest(kind=kind), mock.patch.object(dispatch, 'run_helper', return_value=0) as helper, \
                 mock.patch.object(dispatch, '_interactive_stdin', return_value=True), \
                 mock.patch('builtins.input', return_value='q'):
                self.assertEqual(self.main([kind]), 0)
                self.assertEqual([c.args[0] for c in helper.call_args_list], [kind + '-list'])
        self.assertEqual(self.main(['--help']), 0)
        self.assertNotEqual(self.main(['config']), 0)

    def test_noninteractive_real_lists_work_with_bad_paths(self):
        self.install_fake_helper()
        self.path_file.write_text('{')
        for kind in ('supply', 'route', 'strategy'):
            with self.subTest(kind=kind), mock.patch.object(dispatch.sys.stdin, 'isatty', return_value=False):
                self.assertEqual(self.main([kind]), 0)
        self.assertEqual(self.control.calls, [])

    def test_each_menu_mutating_action_fails_before_helper(self):
        self.path_file.write_text('{')
        for kind in ('supply', 'route', 'strategy'):
            for action in ('a', 'e', 'd') + (('t',) if kind == 'supply' else ()):
                with self.subTest(kind=kind, action=action), \
                     mock.patch.object(dispatch, 'run_helper', return_value=0) as helper, \
                     mock.patch.object(dispatch, '_interactive_stdin', return_value=True), \
                     mock.patch('builtins.input', side_effect=[action, 'fake-id']):
                    self.assertNotEqual(self.main([kind]), 0)
                    self.assertEqual([c.args[0] for c in helper.call_args_list], [kind + '-list'])
        self.assertEqual(self.control.calls, [])

    def test_unavailable_identity_rejects_menu_write_before_helper(self):
        before = self.config.read_bytes()
        with mock.patch.object(dispatch, '_interactive_stdin', return_value=True), \
             mock.patch.object(dispatch, 'inspect_runtime_identities',
                               side_effect=PreflightError('windows_evidence_unavailable',
                                                          'Windows path evidence unavailable')) as inspect, \
             mock.patch.object(dispatch, 'run_helper', return_value=0) as helper, \
             mock.patch('builtins.input', return_value='a'):
            self.assertNotEqual(self.main(['route']), 0)
        inspect.assert_called_once()
        self.assertEqual([c.args[0] for c in helper.call_args_list], ['route-list'])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.control.calls, [])

    def test_preflight_is_repeated_when_paths_change_between_actions(self):
        def run_helper(command, args, config, control, *, root):
            if command == 'route-add':
                self.path_file.write_text('{')
            return 0

        with self.menu_flow_identity() as identity, \
             mock.patch.object(dispatch, 'run_helper', side_effect=run_helper) as helper, \
             mock.patch.object(dispatch, '_interactive_stdin', return_value=True), \
             mock.patch('builtins.input', side_effect=['a', 'a']):
            self.assertNotEqual(self.main(['route']), 0)
        if identity is not None:
            identity.assert_called()
        self.assertEqual([c.args[0] for c in helper.call_args_list],
                         ['route-list', 'route-add', 'route-list'])

    def test_valid_menu_actions_reach_helper(self):
        for kind in ('supply', 'route', 'strategy'):
            for action, suffix in (('a', 'add'), ('e', 'edit'), ('d', 'del')) + (
                    (('t', 'check'),) if kind == 'supply' else ()):
                inputs = [action] + (['fake-id'] if action != 'a' else []) + ['q']
                with self.subTest(kind=kind, action=action), \
                     self.menu_flow_identity() as identity, \
                     mock.patch.object(dispatch, 'run_helper', return_value=0) as helper, \
                     mock.patch.object(dispatch, '_interactive_stdin', return_value=True), \
                     mock.patch('builtins.input', side_effect=inputs):
                    self.assertEqual(self.main([kind]), 0)
                    self.assertEqual([c.args[0] for c in helper.call_args_list],
                                     [kind + '-list', kind + '-' + suffix, kind + '-list'])
                    if identity is not None:
                        identity.assert_called()

    def test_valid_install_reaches_existing_installer(self):
        from core.cli import _install_ops
        with self.install_targets(_install_ops), \
             mock.patch.object(_install_ops, 'cmd_install', return_value=0) as install:
            self.assertEqual(self.main(['install']), 0)
            install.assert_called_once()
            self.assertEqual(install.call_args.args[0], str(self.config))

    def install_targets(self, install, *, claude=None):
        stack = contextlib.ExitStack()
        sdks = {name: {'config_path': self.home / name / 'config'}
                for name in ('claude', 'codex', 'hermes', 'openclaw')}
        if claude is not None:
            sdks['claude']['config_path'] = claude
        stack.enter_context(mock.patch.object(install, 'SDKS', sdks))
        stack.enter_context(mock.patch.object(install, '_CLAUDE_ONBOARDING', self.home / 'onboarding'))
        stack.enter_context(mock.patch.object(install, '_CODEX_CATALOG_TARGET', self.home / 'catalog'))
        stack.enter_context(mock.patch.object(install, '_CLAUDE_SETTINGS', None))
        return stack

    def test_actual_install_target_alias_fails_closed_at_commit(self):
        from core.cli import _install_ops
        for target in (self.root / 'runtime' / 'log', self.config, self.path_file):
            with self.subTest(target=target):
                token = _install_ops._WRITE_CONTEXT.set(
                    {'root': self.root, 'config': self.config, 'targets': []})
                try:
                    with mock.patch.object(_install_ops, 'confirm', return_value=True), \
                         mock.patch.object(_install_ops, 'secure_replace') as replace, \
                         contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                        with self.assertRaises(_install_ops.InstallFailure):
                            _install_ops.preview_confirm_write(target, 'old', 'new', 'claude', [],
                                                               snapshot=SimpleNamespace(contents=b'old'))
                        replace.assert_not_called()
                finally:
                    _install_ops._WRITE_CONTEXT.reset(token)
        self.assertEqual(self.control.calls, [])

    def test_invalid_internal_shapes_are_rejected_without_full_schema(self):
        for bad in ({'routes': [None]}, {'strategies': 'bad'}):
            with self.subTest(bad=bad):
                self.config.write_text(json.dumps(bad))
                with mock.patch.object(dispatch, 'run_helper') as helper:
                    self.assertNotEqual(self.main(['switch', 'fake-token', 'new']), 0)
                    self.assertNotEqual(self.main(['install']), 0)
                    helper.assert_not_called()
        # Missing optional values, unknown extensions and tolerated null pool
        # are not promoted into a strict business schema.
        dispatch.validate_editable_config({'unknown': 3, 'routes': [{}],
                                           'strategies': [{'route_pool': None}]})

    def test_switch_preserves_unrelated_legacy_shapes(self):
        self.install_fake_helper()
        cfg = json.loads(self.config.read_text())
        cfg['supplies'] = {'legacy': 'not used by switch'}
        cfg['routes'][0]['tiers'] = {'opus': 'legacy'}
        cfg['strategies'].append({'client_token': 'other', 'route_pool': [1]})
        self.config.write_text(json.dumps(cfg))
        self.assertEqual(self.main(['switch', 'fake-token', 'new']), 0)
        result = json.loads(self.config.read_text())
        self.assertEqual(result['strategies'][0]['route_id'], 'new')
        self.assertEqual(result['supplies'], cfg['supplies'])
        self.assertEqual(result['routes'][0]['tiers'], cfg['routes'][0]['tiers'])
        self.assertEqual(self.control.calls, ['reload'])

    def test_config_rechecks_after_marker_before_real_replace(self):
        from core.cli import _config_ops
        from core.mac_config_writer import MacWriteError
        before = self.config.read_bytes()
        cfg = json.loads(before)
        cfg['strategies'][0]['route_id'] = 'new'
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        original = _config_ops._set_marker
        def mark_then_conflict(value):
            original(value)
            if value == 'writing':
                self.paths['pid'] = self.paths['lock']
                self.write_paths()
        with self.prepared_write(), \
             mock.patch.dict(os.environ, {'CONFIG_OPS_RELOAD_MARKER_FD': str(write_fd)}), \
             mock.patch.object(_config_ops, '_MARKER_REQUIRED', True), \
             mock.patch.object(_config_ops, '_set_marker', side_effect=mark_then_conflict), \
             contextlib.redirect_stderr(io.StringIO()):
            _config_ops._precheck_inherited_marker()
            with self.assertRaises(MacWriteError) as failure:
                _config_ops.atomic_write(str(self.config), cfg)
            self.assertEqual(failure.exception.state, 'NOT_REPLACED')
        self.assertEqual(self.config.read_bytes(), before)
        os.set_blocking(read_fd, False)
        try:
            frames = os.read(read_fd, 128)
        except BlockingIOError:
            frames = b''
        self.assertEqual(frames, b'writing\nno\n')
        self.assertEqual(list(self.config.parent.glob('*.tmp')), [])

    def test_windows_direct_helper_fails_before_config_staging(self):
        from core.cli import _config_ops
        from core import mac_config_writer, mac_config_path
        before = self.config.read_bytes()
        argv = ['helper', 'switch', str(self.config), 'fake-token', 'new']
        fields = ('_WRITE_BASELINE', '_WRITE_STARTED', '_WRITE_OPERATION',
                  '_WRITE_AUTHORIZATION', '_WRITE_PREFLIGHT', '_WRITE_INPUT_PATH',
                  '_WRITE_COMMITTED', '_WRITE_LOCATIONS', '_WRITE_BACKUP_BASELINE',
                  '_MARKER_BASELINE', '_MARKER_CONTENT', '_MARKER_REQUIRED')
        original_state = {field: getattr(_config_ops, field) for field in fields}
        error = io.StringIO()
        with mock.patch.multiple(_config_ops, _WRITE_BASELINE=None,
                                 _WRITE_STARTED=False, _WRITE_OPERATION=None,
                                 _WRITE_AUTHORIZATION=None, _WRITE_PREFLIGHT=None,
                                 _WRITE_INPUT_PATH=None, _WRITE_COMMITTED=None,
                                 _WRITE_LOCATIONS=None, _WRITE_BACKUP_BASELINE=None,
                                 _MARKER_BASELINE=None, _MARKER_CONTENT=None,
                                 _MARKER_REQUIRED=False), \
             mock.patch.object(_config_ops.os, 'name', 'nt'), \
             mock.patch.object(_config_ops.sys, 'argv', argv), \
             mock.patch.object(mac_config_path, 'capture_config') as snapshot, \
             mock.patch.object(mac_config_writer, 'replace_config') as write, \
             contextlib.redirect_stderr(error):
            with self.assertRaises(SystemExit) as caught:
                _config_ops.main()
            self.assertNotEqual(caught.exception.code, 0)
            self.assertIn('reload 标记不可用', error.getvalue())
            self.assertIn('未提交，未触发 reload', error.getvalue())
            snapshot.assert_not_called()
            write.assert_not_called()
        for field in fields:
            self.assertIs(getattr(_config_ops, field), original_state[field], field)
        self.assertEqual(self.config.read_bytes(), before)

    def test_runtime_file_change_during_final_preflight_blocks_replace(self):
        from core.cli import _config_ops
        from core.mac_config_writer import MacWriteError
        before = self.config.read_bytes()
        cfg = json.loads(before)
        cfg['strategies'][0]['route_id'] = 'new'
        calls = 0
        def change_after_check(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original(*args, **kwargs)
            if calls == 3:
                self.paths['pid'] = str(self.root / 'runtime' / 'other-pid')
                self.write_paths()
            return result
        with self.prepared_write():
            original = _config_ops._WRITE_PREFLIGHT
            with mock.patch.object(_config_ops, '_WRITE_PREFLIGHT', side_effect=change_after_check), \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(MacWriteError) as failure:
                    _config_ops.atomic_write(str(self.config), cfg)
                self.assertEqual(failure.exception.state, 'NOT_REPLACED')
        self.assertGreaterEqual(calls, 4)
        self.assertEqual(self.config.read_bytes(), before)

    def test_config_commit_rechecks_runtime_after_staging(self):
        from core.cli import _config_ops
        from core import mac_config_writer
        before = self.config.read_bytes()
        cfg = json.loads(before)
        cfg['strategies'][0]['route_id'] = 'new'
        original = mac_config_writer._open_private
        def race(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[1].startswith('.model-proxy-config-'):
                self.paths['pid'] = self.paths['lock']
                self.write_paths()
            return result
        with self.prepared_write(), \
             mock.patch.object(mac_config_writer, '_open_private', side_effect=race), \
             mock.patch.object(mac_config_writer.os, 'replace') as replace, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(mac_config_writer.MacWriteError) as failure:
                _config_ops.atomic_write(str(self.config), cfg)
            self.assertEqual(failure.exception.state, 'NOT_REPLACED')
            replace.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(list(self.config.parent.glob('*.tmp')), [])

    def test_config_target_change_after_read_refuses_replace(self):
        from core.cli import _config_ops
        from core.mac_config_writer import MacWriteError
        cfg = json.loads(self.config.read_text())
        changed = b'{"external": true}'
        with self.prepared_write(), \
             mock.patch.object(_config_ops.os, 'replace') as replace:
            self.config.write_bytes(changed)
            with self.assertRaises(MacWriteError) as failure:
                _config_ops.atomic_write(str(self.config), cfg)
            self.assertEqual(failure.exception.state, 'NOT_REPLACED')
            replace.assert_not_called()
        self.assertEqual(self.config.read_bytes(), changed)

    def test_install_rechecks_after_confirmation_and_before_secure_replace(self):
        from core.cli import _install_ops
        target = self.home / 'client.json'
        target.write_text('old')
        token = _install_ops._WRITE_CONTEXT.set(
            {'root': self.root, 'config': self.config, 'targets': []})
        def confirm(*_):
            self.paths['pid'] = self.paths['lock']
            self.write_paths()
            return True
        try:
            with mock.patch.object(_install_ops, 'confirm', side_effect=confirm), \
                 mock.patch.object(_install_ops, 'secure_replace') as replace, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(_install_ops.InstallFailure):
                    _install_ops.preview_confirm_write(target, 'old', 'new', 'claude', [],
                                                       snapshot=SimpleNamespace(contents=b'old'))
                replace.assert_not_called()
            self.assertEqual(target.read_text(), 'old')
        finally:
            _install_ops._WRITE_CONTEXT.reset(token)

    @unittest.skipUnless(sys.platform == 'darwin', 'native macOS secure write required')
    def test_real_install_secure_commit_uses_isolated_target(self):
        from core.cli import _install_ops
        self.home.chmod(0o700)
        target = self.home / 'client.json'
        target.write_text('old')
        target.chmod(0o600)
        token = _install_ops._WRITE_CONTEXT.set(
            {'root': self.root, 'config': self.config, 'targets': []})
        try:
            with mock.patch.object(_install_ops, 'confirm', return_value=True), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertTrue(_install_ops.preview_confirm_write(target, 'old', 'new', 'claude', []))
            self.assertEqual(target.read_text(), 'new')
            backups = list((self.root / '.runtime' / 'backups').glob('client.json.bak.*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), 'old')
        finally:
            _install_ops._WRITE_CONTEXT.reset(token)

    @unittest.skipUnless(sys.platform == 'darwin', 'native macOS secure write required')
    def test_runtime_conflict_after_install_staging_blocks_real_replace(self):
        from core import secure_files
        from core.cli import _install_ops
        self.home.chmod(0o700)
        target = self.home / 'client.json'
        target.write_text('old')
        target.chmod(0o600)
        original = secure_files._open_private
        def stage_then_conflict(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[1].startswith('.model-proxy-runtime-'):
                self.paths['pid'] = self.paths['lock']
                self.write_paths()
            return result
        token = _install_ops._WRITE_CONTEXT.set(
            {'root': self.root, 'config': self.config, 'targets': []})
        try:
            with mock.patch.object(_install_ops, 'confirm', return_value=True), \
                 mock.patch.object(secure_files, '_open_private', side_effect=stage_then_conflict), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(_install_ops.InstallFailure):
                    _install_ops.preview_confirm_write(target, 'old', 'new', 'claude', [])
            self.assertEqual(target.read_text(), 'old')
            self.assertEqual(len(list((self.root / '.runtime' / 'backups').glob('client.json.bak.*'))), 1)
            self.assertEqual(list(self.home.glob('client.json.bak.*')), [])
            self.assertEqual(list(self.home.glob('.model-proxy-*')), [])
        finally:
            _install_ops._WRITE_CONTEXT.reset(token)

    @unittest.skipUnless(sys.platform == 'darwin', 'native macOS secure write required')
    def test_runtime_conflict_after_staging_blocks_new_install_file(self):
        from core import secure_files
        from core.cli import _install_ops
        self.home.chmod(0o700)
        target = self.home / 'new-client.json'
        original = secure_files._open_private
        def stage_then_conflict(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[1].startswith('.model-proxy-runtime-'):
                self.paths['pid'] = self.paths['lock']
                self.write_paths()
            return result
        token = _install_ops._WRITE_CONTEXT.set(
            {'root': self.root, 'config': self.config, 'targets': []})
        try:
            with mock.patch.object(_install_ops, 'confirm', return_value=True), \
                 mock.patch.object(secure_files, '_open_private', side_effect=stage_then_conflict), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(_install_ops.InstallFailure):
                    _install_ops.preview_confirm_write(target, '', 'new', 'claude', [])
            self.assertFalse(target.exists())
            self.assertEqual(list(self.home.glob('.model-proxy-*')), [])
        finally:
            _install_ops._WRITE_CONTEXT.reset(token)

    def test_unavailable_install_commit_guard_fails_closed(self):
        from core.cli import _install_ops
        target = self.home / 'client.json'
        target.write_text('old')
        token = _install_ops._WRITE_CONTEXT.set(
            {'root': self.root, 'config': self.config, 'targets': []})
        try:
            with mock.patch.object(_install_ops, 'confirm', return_value=True), \
                 mock.patch.object(_install_ops, '_ensure_commit_audit',
                                   side_effect=_install_ops.InstallFailure('unavailable')), \
                 mock.patch.object(_install_ops, 'secure_replace') as replace, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(_install_ops.InstallFailure):
                    _install_ops.preview_confirm_write(
                        target, 'old', 'new', 'claude', [], snapshot=SimpleNamespace(contents=b'old'))
                replace.assert_not_called()
            self.assertEqual(target.read_text(), 'old')
        finally:
            _install_ops._WRITE_CONTEXT.reset(token)

    def test_manual_sdk_ignores_unselected_unusable_targets(self):
        from core.cli import _install_ops
        sdks = {
            'hermes': {'config_path': self.home / 'hermes' / 'config.yaml',
                       'label': 'hermes', 'protocol': None},
            'claude': {'config_path': self.root / 'runtime' / 'log'},
            'codex': {'config_path': ''},
            'openclaw': {'config_path': self.path_file},
        }
        with mock.patch.object(_install_ops, 'SDKS', sdks), \
             mock.patch.object(_install_ops, 'ensure_session_hook'), \
             mock.patch.object(_install_ops, 'choose_token', return_value='fake'), \
             mock.patch.object(_install_ops, 'secure_replace') as replace, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(_install_ops.cmd_install(str(self.config), '28371', ['hermes'],
                                                     preflight_root=self.root), 0)
            replace.assert_not_called()

    def test_install_aliases_reject_distinct_roles_but_allow_codex_second_write(self):
        from core.cli import _install_ops
        target = self.home / 'client.json'
        target.write_text('old')
        alias = self.home / 'alias.json'
        os.link(target, alias)
        def fake_commit(*args, **kwargs):
            sys.audit('os.rename', 'fake-staging', 'fake-target', -1, -1)
            return SimpleNamespace(state='VERIFIED', backup=None)
        for previous_role, previous_path, label, expected in (
                ('claude', target, 'openclaw', False),
                ('claude', alias, 'openclaw', False),
                ('codex config', target, 'codex catalog reference', True),
                ('codex config', alias, 'codex catalog reference', False)):
            with self.subTest(previous_role=previous_role, previous_path=previous_path, label=label):
                token = _install_ops._WRITE_CONTEXT.set(
                    {'root': self.root, 'config': self.config,
                     'targets': [(previous_role, previous_path)]})
                try:
                    with mock.patch.object(_install_ops, 'confirm', return_value=True), \
                         mock.patch.object(_install_ops, '_mac_install_locations',
                                           return_value=(object(), lambda: None)), \
                         mock.patch('core.secure_files.secure_replace_runtime',
                                           side_effect=fake_commit) as replace, \
                         contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        if expected:
                            self.assertTrue(_install_ops.preview_confirm_write(
                                target, 'old', 'new', label, [], snapshot=SimpleNamespace(contents=b'old')))
                            replace.assert_called_once()
                        else:
                            with self.assertRaises(_install_ops.InstallFailure):
                                _install_ops.preview_confirm_write(
                                    target, 'old', 'new', label, [], snapshot=SimpleNamespace(contents=b'old'))
                            replace.assert_not_called()
                finally:
                    _install_ops._WRITE_CONTEXT.reset(token)

    def test_unchanged_role_still_blocks_another_role_alias(self):
        from core.cli import _install_ops
        target = self.home / 'same.json'
        target.write_text('old')
        token = _install_ops._WRITE_CONTEXT.set(
            {'root': self.root, 'config': self.config, 'targets': []})
        try:
            with mock.patch.object(_install_ops, 'confirm', return_value=True), \
                 mock.patch.object(_install_ops, 'secure_replace') as replace, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(_install_ops.preview_confirm_write(
                    target, 'old', 'old', 'claude', [], snapshot=SimpleNamespace(contents=b'old')))
                with self.assertRaises(_install_ops.InstallFailure):
                    _install_ops.preview_confirm_write(
                        target, 'old', 'new', 'openclaw', [], snapshot=SimpleNamespace(contents=b'old'))
                replace.assert_not_called()
        finally:
            _install_ops._WRITE_CONTEXT.reset(token)

    def test_unchanged_target_is_itself_checked_against_prior_role(self):
        from core.cli import _install_ops
        target = self.home / 'same.json'
        target.write_text('old')
        token = _install_ops._WRITE_CONTEXT.set(
            {'root': self.root, 'config': self.config, 'targets': [('claude', target)]})
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(_install_ops.InstallFailure):
                    _install_ops.preview_confirm_write(
                        target, 'old', 'old', 'openclaw', [], snapshot=SimpleNamespace(contents=b'old'))
            self.assertEqual(target.read_text(), 'old')
        finally:
            _install_ops._WRITE_CONTEXT.reset(token)

    @unittest.skipUnless(sys.platform == 'darwin', 'native macOS secure write required')
    def test_install_reports_partial_success_when_later_unchanged_target_aliases(self):
        from core.cli import _install_ops
        self.home.chmod(0o700)
        target = self.home / 'client.json'
        target.write_text('old')
        target.chmod(0o600)
        def first(*_):
            _install_ops.preview_confirm_write(target, 'old', 'new', 'claude', [])
        def second(*_):
            _install_ops.preview_confirm_write(target, 'new', 'new', 'openclaw', [])
        sdks = {name: {'label': name, 'protocol': None} for name in ('claude', 'openclaw')}
        out = io.StringIO()
        with mock.patch.object(_install_ops, 'SDKS', sdks), \
             mock.patch.object(_install_ops, '_INSTALLERS', {'claude': first, 'openclaw': second}), \
             mock.patch.object(_install_ops, 'ensure_session_hook'), \
             mock.patch.object(_install_ops, 'choose_token', return_value='fake'), \
             mock.patch.object(_install_ops, 'confirm', return_value=True), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = _install_ops.cmd_install(str(self.config), '28371', ['claude', 'openclaw'],
                                          preflight_root=self.root)
        self.assertNotEqual(rc, 0)
        self.assertEqual(target.read_text(), 'new')
        self.assertIn('claude: VERIFIED', out.getvalue())
        self.assertIn('openclaw: PREPARE_FAILED', out.getvalue())

    @unittest.skipUnless(sys.platform == 'darwin', 'native macOS secure write required')
    def test_real_install_unchanged_hook_aliases_claude_settings(self):
        from core.cli import _install_ops
        self.home.chmod(0o700)
        (self.home / '.obsidian').mkdir()
        (self.home / '.claude').mkdir(mode=0o700)
        project = self.home / 'tools' / 'model_proxy'
        (project / 'hooker').mkdir(parents=True)
        (project / 'hooker' / 'ensure_model_proxy.sh').write_text('# fake\n')
        (project / 'config').mkdir()
        (project / 'runtime').mkdir()
        (project / 'config' / 'runtime_paths.json').write_text(json.dumps({
            key: str(project / 'runtime' / key) for key in PATH_KEYS
        }))
        settings = self.home / '.claude' / 'settings.json'
        contents = json.dumps({'hooks': {'SessionStart': [{'hooks': [{
            'type': 'command', 'command': _install_ops._expected_hook_command()
        }]}]}})
        settings.write_text(contents)
        settings.chmod(0o600)
        self.assertEqual(_install_ops._recognize_vault(project), self.home)
        out = io.StringIO()
        with mock.patch.object(_install_ops, '_VAULT_ROOT', self.home), \
             mock.patch.object(_install_ops, '_CLAUDE_SETTINGS', settings), \
             mock.patch.dict(_install_ops.SDKS['claude'], {'config_path': settings}), \
             mock.patch.object(_install_ops, 'choose_token', return_value='fake-token'), \
             mock.patch.object(_install_ops, 'confirm', return_value=True), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = _install_ops.cmd_install(str(self.config), '28371', ['claude'],
                                          preflight_root=project)
        self.assertNotEqual(rc, 0)
        self.assertEqual(settings.read_text(), contents)
        self.assertEqual(list(settings.parent.glob('*.bak.*')), [])
        self.assertIn('model_proxy hook: UNCHANGED', out.getvalue())
        self.assertIn('claude: PREPARE_FAILED', out.getvalue())

    @unittest.skipUnless(sys.platform == 'darwin', 'native macOS secure snapshot required')
    def test_real_unchanged_onboarding_checks_prior_alias(self):
        from core.cli import _install_ops
        self.home.chmod(0o700)
        target = self.home / '.claude.json'
        contents = '{"hasCompletedOnboarding":true}'
        target.write_text(contents)
        target.chmod(0o600)
        for previous, allowed in (('claude', False), ('claude onboarding', True)):
            with self.subTest(previous=previous):
                context = {'root': self.root, 'config': self.config,
                           'targets': [(previous, target)]}
                token = _install_ops._WRITE_CONTEXT.set(context)
                try:
                    with mock.patch.object(_install_ops, '_EVENTS', []), \
                         contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        if allowed:
                            _install_ops._ensure_onboarding_completed(target)
                            self.assertIn(('claude onboarding', target), context['targets'])
                        else:
                            with self.assertRaises(_install_ops.InstallFailure):
                                _install_ops._ensure_onboarding_completed(target)
                    self.assertEqual(target.read_text(), contents)
                finally:
                    _install_ops._WRITE_CONTEXT.reset(token)

    @unittest.skipUnless(sys.platform == 'darwin', 'native macOS secure snapshot required')
    def test_real_unchanged_catalog_checks_prior_alias(self):
        from core.cli import _install_ops
        self.home.chmod(0o700)
        template = self.home / 'template.json'
        template.write_text(json.dumps({'models': [{'base_instructions': 'old'}]}))
        target = self.home / 'catalog.json'
        contents = json.dumps({'models': [{'base_instructions': 'fake prompt'}]},
                              ensure_ascii=False, indent=2)
        target.write_text(contents)
        target.chmod(0o600)
        for previous, allowed in (('claude', False), ('codex catalog', True)):
            with self.subTest(previous=previous):
                context = {'root': self.root, 'config': self.config,
                           'targets': [(previous, target)]}
                token = _install_ops._WRITE_CONTEXT.set(context)
                try:
                    with mock.patch.object(_install_ops, '_EVENTS', []), \
                         mock.patch.object(_install_ops.urllib.request, 'urlopen',
                                           return_value=SimpleNamespace(read=lambda: b'fake prompt')), \
                         contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        if allowed:
                            self.assertTrue(_install_ops._install_catalog_asset(template, target))
                            self.assertIn(('codex catalog', target), context['targets'])
                        else:
                            with self.assertRaises(_install_ops.InstallFailure):
                                _install_ops._install_catalog_asset(template, target)
                    self.assertEqual(target.read_text(), contents)
                finally:
                    _install_ops._WRITE_CONTEXT.reset(token)

    def test_marker_pipe_frames_control_reload(self):
        cases = [(b'', 0, 1, 0), (b'no\n', 0, 0, 0),
                 (b'writing\nyes\n', 0, 0, 1),
                 (b'writing\nyes\nyes\n', 0, 0, 1),
                 (b'writing\nyes\nyes\n', 7, 7, 0),
                 (b'writing\nyes\n', 7, 7, 0),
                 (b'writing\n', 130, 130, 0),
                 (b'writing\nno\n', 130, 130, 0),
                 (b'yes\n', 0, 1, 0), (b'writing\nyes\njunk\n', 0, 1, 0)]
        for frames, rc, expected, reloads in cases:
            with self.subTest(frames=frames, rc=rc):
                control = Control()
                def run(args, env, pass_fds):
                    fd = int(env['CONFIG_OPS_RELOAD_MARKER_FD'])
                    self.assertEqual(pass_fds, (fd,))
                    self.assertNotIn('CONFIG_OPS_RELOAD_MARKER', env)
                    self.assertEqual(os.fstat(fd).st_nlink, 0)
                    if frames:
                        os.write(fd, frames)
                    return SimpleNamespace(returncode=rc)
                with mock.patch.object(dispatch, '_marker_fd_protocol_available', return_value=True), \
                     mock.patch.object(dispatch.subprocess, 'run', side_effect=run), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(dispatch.run_helper('switch', [], self.config, control,
                                                         root=self.root), expected)
                self.assertEqual(len(control.calls), reloads)
                self.assertEqual(list(self.tmp.glob('model-proxy-cli-*')), [])

    def test_readonly_sentinel_never_triggers_reload(self):
        for output, rc, expected in ((b'route\n__RELOAD__=no\n', 0, 0),
                                     (b'route\n', 0, 1),
                                     (b'__RELOAD__=yes\n', 0, 1),
                                     (b'__RELOAD__=yes\n', 7, 7),
                                     (b'__RELOAD__=yes\n__RELOAD__=no\n', 0, 0)):
            with self.subTest(output=output, rc=rc):
                control = Control()
                result = SimpleNamespace(returncode=rc, stdout=output)
                with mock.patch.object(dispatch.subprocess, 'run', return_value=result) as run, \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(dispatch.run_helper('route-list', [], self.config, control,
                                                         root=self.root), expected)
                self.assertNotIn('CONFIG_OPS_RELOAD_MARKER', run.call_args.kwargs['env'])
                self.assertNotIn('CONFIG_OPS_RELOAD_MARKER_FD', run.call_args.kwargs['env'])
                self.assertEqual(control.calls, [])

    def test_pipe_rejects_regular_file_and_named_fifo(self):
        target = self.tmp / 'ordinary-user-file'
        target.write_bytes(b'started'); target.chmod(0o600)
        with target.open('r+b') as stream:
            with self.assertRaises(ValueError):
                dispatch._pipe_endpoint_protection(stream.fileno(), write=True)
        self.assertEqual(target.read_bytes(), b'started')
        named = self.tmp / 'named-pipe'
        os.mkfifo(named, 0o600)
        reader = os.open(named, os.O_RDONLY | os.O_NONBLOCK)
        writer = os.open(named, os.O_WRONLY | os.O_NONBLOCK)
        try:
            with self.assertRaises(ValueError):
                dispatch._pipe_endpoint_protection(writer, write=True)
        finally:
            os.close(writer); os.close(reader)

    def test_missing_fd_protocol_fails_before_pipe_or_helper(self):
        error = io.StringIO()
        with mock.patch.object(dispatch, '_marker_fd_protocol_available', return_value=False), \
             mock.patch.object(dispatch.os, 'pipe') as pipe, \
             mock.patch.object(dispatch.subprocess, 'run') as helper, \
             contextlib.redirect_stderr(error):
            self.assertEqual(dispatch.run_helper('switch', [], self.config, self.control,
                                                 root=self.root), 1)
        pipe.assert_not_called(); helper.assert_not_called()
        self.assertEqual(self.control.calls, [])
        self.assertIn('FD protocol unavailable', error.getvalue())

    def test_fd_protocol_requires_version_two_and_matching_helper_source(self):
        from core.cli import _config_ops
        self.install_fake_helper()
        with mock.patch.object(_config_ops, 'ANONYMOUS_MARKER_FD_PROTOCOL', 1, create=True):
            self.assertFalse(dispatch._marker_fd_protocol_available(self.root))
        with mock.patch.object(_config_ops, 'ANONYMOUS_MARKER_FD_PROTOCOL', 2, create=True), \
             mock.patch.object(_config_ops, 'ANONYMOUS_MARKER_FD_PRECHECK', None, create=True):
            self.assertFalse(dispatch._marker_fd_protocol_available(self.root))
        with mock.patch.object(_config_ops, 'ANONYMOUS_MARKER_FD_PROTOCOL', 2, create=True), \
             mock.patch.object(_config_ops, 'ANONYMOUS_MARKER_FD_PRECHECK', 1, create=True):
            self.assertTrue(dispatch._marker_fd_protocol_available(self.root))
            helper = self.root / 'core/cli/_config_ops.py'
            helper.write_text(helper.read_text() + '\n# mismatched fake source\n')
            self.assertFalse(dispatch._marker_fd_protocol_available(self.root))

    def test_runtime_changes_after_initial_preflight_blocks_helper(self):
        original = dispatch._config_write_evidence
        changed = False
        def race(root, snapshot, operation):
            nonlocal changed
            result = original(root, snapshot, operation)
            if not changed:
                changed = True
                self.paths['log'] = str(self.tmp / 'reserved-fake-log')
                self.write_paths()
            return result
        error = io.StringIO()
        with mock.patch.object(dispatch, '_marker_fd_protocol_available', return_value=True), \
             mock.patch.object(dispatch, '_config_write_evidence', side_effect=race), \
             mock.patch.object(dispatch.subprocess, 'run') as helper, \
             contextlib.redirect_stderr(error):
            self.assertEqual(dispatch.run_helper('switch', [], self.config, self.control,
                                                 root=self.root), 1)
        self.assertTrue(changed); helper.assert_not_called()
        self.assertEqual(self.control.calls, [])
        self.assertIn('runtime directory preparation unconfirmed', error.getvalue())
        self.assertIn('helper not started', error.getvalue())

    def test_runtime_changes_after_pipe_creation_blocks_helper(self):
        original = os.pipe
        def race():
            fds = original()
            self.paths['log'] = str(self.tmp / 'reserved-fake-log')
            self.write_paths()
            return fds
        error = io.StringIO()
        with mock.patch.object(dispatch, '_marker_fd_protocol_available', return_value=True), \
             mock.patch.object(dispatch.os, 'pipe', side_effect=race), \
             mock.patch.object(dispatch.subprocess, 'run') as helper, \
             contextlib.redirect_stderr(error):
            self.assertEqual(dispatch.run_helper('switch', [], self.config, self.control,
                                                 root=self.root), 1)
        helper.assert_not_called(); self.assertEqual(self.control.calls, [])
        # Runtime preparation succeeded here; the race is injected at the
        # marker pipe, so that later stage must be the one reported.
        self.assertIn('pipe created; close confirmed; helper not started', error.getvalue())

    def test_no_marker_files_created_even_when_old_path_is_runtime_log(self):
        path = self.tmp / 'model-proxy-cli-predicted' / 'reload'
        self.paths['log'] = str(path); self.write_paths()
        def run(args, env, pass_fds):
            self.assertNotIn('CONFIG_OPS_RELOAD_MARKER', env)
            os.write(pass_fds[0], b'no\n')
            return SimpleNamespace(returncode=0)
        with mock.patch.object(dispatch, '_marker_fd_protocol_available', return_value=True), \
             mock.patch.object(dispatch.tempfile, 'TemporaryDirectory',
                               side_effect=AssertionError('filesystem marker forbidden')), \
             mock.patch.object(dispatch.subprocess, 'run', side_effect=run):
            self.assertEqual(dispatch.run_helper('switch', [], self.config, self.control,
                                                 root=self.root), 0)
        self.assertFalse(path.exists()); self.assertEqual(self.control.calls, [])

    def test_open_second_pipe_writer_is_unknown_and_never_reloads(self):
        held = []
        def run(args, env, pass_fds):
            held.append(os.dup(pass_fds[0]))
            os.write(pass_fds[0], b'writing\nyes\nyes\n')
            return SimpleNamespace(returncode=0)
        try:
            with mock.patch.object(dispatch, '_marker_fd_protocol_available', return_value=True), \
                 mock.patch.object(dispatch.subprocess, 'run', side_effect=run), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(dispatch.run_helper('switch', [], self.config, self.control,
                                                     root=self.root), 1)
            self.assertEqual(self.control.calls, [])
        finally:
            for fd in held: os.close(fd)

    def test_link_changed_after_helper_success_never_triggers_reload(self):
        alias = self.root / 'config/alias.json'
        alias.symlink_to(self.config.name)
        def run(args, env, pass_fds):
            os.write(pass_fds[0], b'writing\nyes\nyes\n')
            alias.unlink()
            alias.symlink_to(self.config.name)
            return SimpleNamespace(returncode=0)
        error = io.StringIO()
        with mock.patch.object(dispatch, '_marker_fd_protocol_available', return_value=True), \
             mock.patch.object(dispatch.subprocess, 'run', side_effect=run), \
             contextlib.redirect_stderr(error):
            self.assertEqual(dispatch.run_helper('switch', [], alias, self.control,
                                                 root=self.root), 1)
        self.assertEqual(self.control.calls, [])
        self.assertIn('configuration may have been saved', error.getvalue())

    def test_interrupt_closes_pipe_without_reload(self):
        error = io.StringIO()
        with mock.patch.object(dispatch, '_marker_fd_protocol_available', return_value=True), \
             mock.patch.object(dispatch.subprocess, 'run', side_effect=KeyboardInterrupt), \
             contextlib.redirect_stderr(error):
            self.assertEqual(dispatch.run_helper('switch', [], self.config, self.control,
                                                 root=self.root), 130)
        self.assertEqual(self.control.calls, [])
        self.assertIn('pipe created; close confirmed', error.getvalue())

    def test_pipe_create_exception_after_effect_reports_unknown(self):
        original = os.pipe
        leaked = []
        def uncertain_pipe():
            leaked.extend(original())
            raise OSError(errno.EIO, 'injected after-effect')
        error = io.StringIO()
        try:
            with mock.patch.object(dispatch, '_marker_fd_protocol_available', return_value=True), \
                 mock.patch.object(dispatch.os, 'pipe', side_effect=uncertain_pipe), \
                 mock.patch.object(dispatch.subprocess, 'run') as helper, \
                 contextlib.redirect_stderr(error):
                self.assertEqual(dispatch.run_helper('switch', [], self.config, self.control,
                                                     root=self.root), 1)
            helper.assert_not_called(); self.assertEqual(self.control.calls, [])
            self.assertIn('pipe creation unknown; close unknown', error.getvalue())
        finally:
            for fd in leaked: os.close(fd)

    def test_failed_pipe_close_is_not_retried_after_fd_reuse(self):
        target = self.home / 'fake-user-file'
        target.write_bytes(b'untouched')
        original_close = os.close
        state = {'pipe_fd': None, 'reused': None, 'retry': False}
        def run(args, env, pass_fds):
            state['pipe_fd'] = pass_fds[0]
            os.write(pass_fds[0], b'writing\nyes\nyes\n')
            return SimpleNamespace(returncode=0)
        def close(fd):
            if fd == state['pipe_fd'] and state['reused'] is None:
                original_close(fd)
                state['reused'] = os.open(target, os.O_RDWR)
                raise OSError(errno.EIO, 'injected close after-effect')
            if fd == state['reused']:
                state['retry'] = True
            return original_close(fd)
        error = io.StringIO()
        try:
            with mock.patch.object(dispatch, '_marker_fd_protocol_available', return_value=True), \
                 mock.patch.object(dispatch.subprocess, 'run', side_effect=run), \
                 mock.patch.object(dispatch.os, 'close', side_effect=close), \
                 contextlib.redirect_stderr(error):
                self.assertEqual(dispatch.run_helper('switch', [], self.config, self.control,
                                                     root=self.root), 1)
            self.assertEqual(state['reused'], state['pipe_fd'])
            self.assertFalse(state['retry'])
            self.assertEqual(os.pread(state['reused'], 32, 0), b'untouched')
            self.assertEqual(self.control.calls, [])
            self.assertIn('close not confirmed', error.getvalue())
            self.assertIn('no reload', error.getvalue())
        finally:
            if state['reused'] is not None:
                original_close(state['reused'])

    def test_interrupted_pipe_close_is_not_retried_after_fd_reuse(self):
        target = self.home / 'fake-user-file'
        target.write_bytes(b'untouched')
        original_close = os.close
        state = {'pipe_fd': None, 'reused': None, 'retry': False}
        def run(args, env, pass_fds):
            state['pipe_fd'] = pass_fds[0]
            os.write(pass_fds[0], b'writing\nyes\nyes\n')
            return SimpleNamespace(returncode=0)
        def close(fd):
            if fd == state['pipe_fd'] and state['reused'] is None:
                original_close(fd)
                state['reused'] = os.open(target, os.O_RDWR)
                raise KeyboardInterrupt()
            if fd == state['reused']:
                state['retry'] = True
            return original_close(fd)
        error = io.StringIO()
        try:
            with mock.patch.object(dispatch, '_marker_fd_protocol_available', return_value=True), \
                 mock.patch.object(dispatch.subprocess, 'run', side_effect=run), \
                 mock.patch.object(dispatch.os, 'close', side_effect=close), \
                 contextlib.redirect_stderr(error):
                self.assertEqual(dispatch.run_helper('switch', [], self.config, self.control,
                                                     root=self.root), 130)
            self.assertEqual(state['reused'], state['pipe_fd'])
            self.assertFalse(state['retry'])
            self.assertEqual(os.pread(state['reused'], 32, 0), b'untouched')
            self.assertEqual(self.control.calls, [])
            self.assertIn('close not confirmed', error.getvalue())
            self.assertIn('no reload', error.getvalue())
        finally:
            if state['reused'] is not None:
                original_close(state['reused'])

    def test_failed_read_end_close_is_not_retried_after_fd_reuse(self):
        target = self.home / 'fake-user-file'
        target.write_bytes(b'untouched')
        original_close, original_pipe = os.close, os.pipe
        state = {'read_fd': None, 'reused': None, 'retry': False}
        def pipe():
            pair = original_pipe()
            state['read_fd'] = pair[0]
            return pair
        def run(args, env, pass_fds):
            os.write(pass_fds[0], b'writing\nyes\nyes\n')
            return SimpleNamespace(returncode=0)
        def close(fd):
            if fd == state['read_fd'] and state['reused'] is None:
                original_close(fd)
                state['reused'] = os.open(target, os.O_RDWR)
                raise OSError(errno.EIO, 'injected close after-effect')
            if fd == state['reused']:
                state['retry'] = True
            return original_close(fd)
        error = io.StringIO()
        try:
            with mock.patch.object(dispatch, '_marker_fd_protocol_available', return_value=True), \
                 mock.patch.object(dispatch.os, 'pipe', side_effect=pipe), \
                 mock.patch.object(dispatch.subprocess, 'run', side_effect=run), \
                 mock.patch.object(dispatch.os, 'close', side_effect=close), \
                 contextlib.redirect_stderr(error):
                self.assertEqual(dispatch.run_helper('switch', [], self.config, self.control,
                                                     root=self.root), 1)
            self.assertEqual(state['reused'], state['read_fd'])
            self.assertFalse(state['retry'])
            self.assertEqual(os.pread(state['reused'], 32, 0), b'untouched')
            self.assertEqual(self.control.calls, [])
            self.assertIn('close not confirmed', error.getvalue())
        finally:
            if state['reused'] is not None:
                original_close(state['reused'])

    def test_marker_failure_after_replace_warns_and_preserves_saved_file(self):
        from core.cli import _config_ops
        from core.mac_config_writer import MacWriteError
        cfg = json.loads(self.config.read_text())
        cfg['strategies'][0]['route_id'] = 'new'
        error = io.StringIO()
        def marker(value):
            if value == 'yes':
                raise OSError('fake marker failure')
        with self.prepared_write(), \
             mock.patch.object(_config_ops, '_set_marker', side_effect=marker), \
             contextlib.redirect_stderr(error):
            with self.assertRaises(MacWriteError) as failure:
                _config_ops.atomic_write(str(self.config), cfg)
            self.assertEqual(failure.exception.state, 'POSTCHECK_FAILED')
            self.assertEqual(failure.exception.code, 'marker_unverified_after_commit')
        self.assertIn('配置已保存', error.getvalue())
        self.assertEqual(json.loads(self.config.read_text())['strategies'][0]['route_id'], 'new')

    def test_single_missing_log_uses_isolated_legacy_default(self):
        del self.paths['log']
        self.write_paths()
        with mock.patch.object(dispatch, 'run_helper', return_value=0) as helper:
            self.assertEqual(self.main(['switch', 'fake-token', 'new']), 0)
            helper.assert_called_once()


if __name__ == '__main__':
    unittest.main()
