"""CLI binding of fake dispatch policy to the Mac writer; isolated tests only."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from core.cli import _config_ops as ops, dispatch
from core.mac_config_path import open_target_parent
from core.mac_config_writer import MacWriteError, WriteAuthorization


class ConfigOpsWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['MP_TEST_ROOT'])
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.json'
        self.config.write_text('{"routes": []}\n')
        self.config.chmod(0o600)
        self.marker = self.root / 'marker'
        self.marker.write_text('started')  # unrelated named file for rejection tests
        self.pipe_read, self.pipe_write = os.pipe()
        os.set_blocking(self.pipe_read, False)
        self.pipe_frames = bytearray()
        self.project = self.root / 'project'
        (self.project / 'config').mkdir(parents=True, mode=0o700)
        (self.project / 'runtime').mkdir(mode=0o700)
        from core.runtime_paths import PATH_KEYS
        self.runtime = {key: str(self.project / 'runtime' / key) for key in PATH_KEYS}
        (self.project / 'config' / 'runtime_paths.json').write_text(json.dumps(self.runtime))
        self.project_patch = patch.object(dispatch, 'ROOT', self.project)
        self.project_patch.start()
        self.state = patch.multiple(ops, _WRITE_BASELINE=None, _WRITE_STARTED=False,
                                    _WRITE_OPERATION='route-add', _WRITE_AUTHORIZATION=None,
                                    _WRITE_PREFLIGHT=None, _WRITE_INPUT_PATH=None,
                                    _WRITE_COMMITTED=None, _MARKER_BASELINE=None, _MARKER_CONTENT=None,
                                    _MARKER_REQUIRED=False)
        self.state.start()
        self.env = patch.dict(os.environ, {'CONFIG_OPS_RELOAD_MARKER_FD': str(self.pipe_write)})
        self.env.start()
        os.environ.pop('CONFIG_OPS_RELOAD_MARKER', None)
        ops._precheck_inherited_marker()

    def tearDown(self):
        self.env.stop()
        if self.pipe_read is not None:
            os.close(self.pipe_read)
        os.close(self.pipe_write)
        self.state.stop()
        self.project_patch.stop()
        self.tmp.cleanup()

    def close_pipe_reader(self):
        fd, self.pipe_read = self.pipe_read, None
        os.close(fd)

    def marker_status(self):
        while True:
            try:
                data = os.read(self.pipe_read, 256)
            except BlockingIOError:
                break
            if not data:
                break
            self.pipe_frames.extend(data)
        frames = bytes(self.pipe_frames).splitlines()
        return frames[-1].decode('ascii') if frames else 'started'

    def plan(self, _root, snapshot, *, operation):
        with open_target_parent(snapshot) as (fd, _):
            st = os.fstat(fd)
        grant = WriteAuthorization(snapshot.target_path, (st.st_dev, st.st_ino),
                                   tuple(snapshot.final_identity[:2]), True)
        return grant, lambda actual: actual == snapshot

    @contextlib.contextmanager
    def ready(self):
        with patch.object(dispatch, 'prepare_config_write', self.plan, create=True):
            ops._prepare_write(str(self.config), 'route-add')
            yield

    def _assert_main_gate_rejected(self, env):
        before = self.config.read_bytes()
        action = unittest.mock.Mock()
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sys, 'argv', ['helper', 'supply-check', str(self.config), 'fake']), \
             patch.dict(ops._DISPATCH, {'supply-check': (action, 1)}), \
             patch('core.mac_config_path.capture_config') as capture, \
             patch.object(dispatch, 'prepare_config_write') as prepare, \
             patch.object(ops.urllib.request, 'urlopen') as upstream, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                ops.main()
        self.assertEqual(caught.exception.code, 1)
        capture.assert_not_called()
        prepare.assert_not_called()
        action.assert_not_called()
        upstream.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(list(self.root.glob('config.json.bak.*')))
        self.assertFalse(list(self.root.glob('.model-proxy-config-*')))

    def test_main_rejects_missing_malformed_or_standard_fd_before_work(self):
        base = dict(os.environ)
        base.pop('CONFIG_OPS_RELOAD_MARKER', None)
        for raw in (None, '', '0', '1', '2', '-1', '0003', 'x', '9' * 100):
            env = dict(base)
            if raw is None:
                env.pop('CONFIG_OPS_RELOAD_MARKER_FD', None)
            else:
                env['CONFIG_OPS_RELOAD_MARKER_FD'] = raw
            with self.subTest(raw=raw):
                self._assert_main_gate_rejected(env)

    def test_main_rejects_legacy_path_even_with_valid_pipe(self):
        before = self.marker.read_bytes()
        for legacy in ('', str(self.marker)):
            env = dict(os.environ)
            env['CONFIG_OPS_RELOAD_MARKER'] = legacy
            with self.subTest(legacy=legacy):
                self._assert_main_gate_rejected(env)
        self.assertEqual(self.marker.read_bytes(), before)

    def test_main_rejects_regular_file_read_end_and_named_fifo(self):
        regular = self.root / 'ordinary'
        regular.write_bytes(b'started')
        regular_fd = os.open(regular, os.O_RDWR)
        named = self.root / 'named-pipe-gate'
        os.mkfifo(named, 0o600)
        fifo_fd = os.open(named, os.O_RDWR | os.O_NONBLOCK)
        try:
            for fd in (regular_fd, self.pipe_read, fifo_fd):
                env = dict(os.environ)
                env['CONFIG_OPS_RELOAD_MARKER_FD'] = str(fd)
                with self.subTest(fd=fd):
                    self._assert_main_gate_rejected(env)
            self.assertEqual(regular.read_bytes(), b'started')
        finally:
            os.close(regular_fd)
            os.close(fifo_fd)

    def test_main_rejects_closed_fd_before_work(self):
        fd = os.dup(self.pipe_write)
        os.close(fd)
        env = dict(os.environ)
        env['CONFIG_OPS_RELOAD_MARKER_FD'] = str(fd)
        self._assert_main_gate_rejected(env)

    def test_precheck_rejects_fd_swap_during_binding(self):
        saved = os.dup(self.pipe_write)
        reader, writer = os.pipe()
        real_set = os.set_inheritable
        def swap(fd, inheritable):
            real_set(fd, inheritable)
            os.dup2(writer, fd, inheritable=False)
        env = dict(os.environ)
        try:
            with patch.object(os, 'set_inheritable', side_effect=swap):
                self._assert_main_gate_rejected(env)
        finally:
            os.dup2(saved, self.pipe_write, inheritable=False)
            os.close(saved)
            os.close(reader)
            os.close(writer)
        self.assertEqual(self.marker_status(), 'started')

    def test_precheck_inherit_flag_failure_never_closes_fd(self):
        inode = os.fstat(self.pipe_write).st_ino
        os.set_inheritable(self.pipe_write, True)
        real_set = os.set_inheritable
        def fail_after_setting(fd, inheritable):
            real_set(fd, inheritable)
            raise OSError('synthetic close-on-exec failure')
        env = dict(os.environ)
        with patch.object(os, 'set_inheritable', side_effect=fail_after_setting) as setter, \
             patch.object(os, 'close', wraps=os.close) as close:
            self._assert_main_gate_rejected(env)
        setter.assert_called_once_with(self.pipe_write, False)
        close.assert_not_called()
        self.assertFalse(os.get_inheritable(self.pipe_write))
        self.assertEqual(os.fstat(self.pipe_write).st_ino, inode)
        self.assertEqual(self.marker_status(), 'started')

    def test_main_rejects_pipe_without_reader_before_work(self):
        reader, writer = os.pipe()
        os.close(reader)
        try:
            env = dict(os.environ)
            env['CONFIG_OPS_RELOAD_MARKER_FD'] = str(writer)
            self._assert_main_gate_rejected(env)
        finally:
            os.close(writer)

    def test_real_helper_rejects_untrusted_transports_before_config_access(self):
        src = Path(dispatch.__file__).resolve().parents[1]
        shutil.copytree(src, self.project / 'core', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        config = self.project / 'config' / 'business.json'
        config.write_text(json.dumps({'supplies': [{'id': 'fake', 'appkey': 'fake',
            'url': 'http://127.0.0.1/v1/messages', 'protocol': 'anthropic', 'target_model': 'fake'}]}))
        config.chmod(0o600)
        original = config.read_bytes()
        unrelated = self.root / 'ordinary-file'
        unrelated.write_bytes(b'started')
        regular = os.open(unrelated, os.O_RDWR)
        dead_reader, dead_writer = os.pipe()
        os.close(dead_reader)
        guard = self.root / 'gate-guard'
        guard.mkdir()
        (guard / 'sitecustomize.py').write_text(
            "import os,sys,socket,urllib.request\n"
            "def deny(*a, **k): os._exit(98)\n"
            "socket.socket.connect=deny; socket.socket.connect_ex=deny; "
            "socket.socket.bind=deny; socket.socket.sendto=deny; socket.getaddrinfo=deny; "
            "urllib.request.urlopen=deny\n"
            "def audit(event,args):\n"
            " if event == 'open' and args and isinstance(args[0],str) and "
            "args[0] in (os.environ['MP_FAKE_CONFIG'], 'business.json'): os._exit(99)\n"
            "sys.addaudithook(audit)\n")
        base = {'PATH': '/usr/bin:/bin', 'HOME': str(self.root), 'TMPDIR': str(self.root),
                'PYTHONPATH': str(guard), 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                'MP_FAKE_CONFIG': str(config)}
        helper = self.project / 'core' / 'cli' / 'helper_entry.py'
        cases = [({}, ()),
                 ({'CONFIG_OPS_RELOAD_MARKER_FD': str(regular)}, (regular,)),
                 ({'CONFIG_OPS_RELOAD_MARKER_FD': str(self.pipe_read)}, (self.pipe_read,)),
                 ({'CONFIG_OPS_RELOAD_MARKER_FD': str(dead_writer)}, (dead_writer,)),
                 ({'CONFIG_OPS_RELOAD_MARKER': str(unrelated),
                   'CONFIG_OPS_RELOAD_MARKER_FD': str(self.pipe_write)}, (self.pipe_write,))]
        try:
            for values, inherited in cases:
                env = dict(base, **values)
                with self.subTest(values=tuple(values)):
                    result = subprocess.run([sys.executable, str(helper), 'supply-check',
                                             str(config), 'fake'], env=env, pass_fds=inherited,
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(config.read_bytes(), original)
                    self.assertEqual(unrelated.read_bytes(), b'started')
                    self.assertFalse(list(config.parent.glob('business.json.bak.*')))
                    self.assertFalse(list(config.parent.glob('.model-proxy-config-*')))
        finally:
            os.close(regular)
            os.close(dead_writer)

    def test_precheck_sets_cloexec_and_emits_no_frame(self):
        import fcntl
        os.set_inheritable(self.pipe_write, True)
        ops._precheck_inherited_marker()
        self.assertFalse(os.get_inheritable(self.pipe_write))
        self.assertTrue(fcntl.fcntl(self.pipe_write, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)
        self.assertEqual(self.marker_status(), 'started')
        st = os.fstat(self.pipe_write)
        code = ("import os,sys\n"
                "fd,dev,ino=map(int,sys.argv[1:])\n"
                "try: s=os.fstat(fd); same=(s.st_dev,s.st_ino)==(dev,ino)\n"
                "except OSError: same=False\n"
                "print('inherited' if same else 'closed')\n")
        result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', code,
                                 str(self.pipe_write), str(st.st_dev), str(st.st_ino)],
                                close_fds=False, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), 'closed')

    def test_changed_inheritable_flag_blocks_before_staging(self):
        with self.ready(), patch.object(ops, '_MARKER_REQUIRED', True), \
             patch('core.mac_config_writer._open_private') as stage:
            os.set_inheritable(self.pipe_write, True)
            try:
                with self.assertRaises(ops.MarkerSafetyError):
                    ops.atomic_write(str(self.config), {'routes': [{'id': 'new'}]})
            finally:
                os.set_inheritable(self.pipe_write, False)
        stage.assert_not_called()
        self.assertEqual(json.loads(self.config.read_text()), {'routes': []})
        self.assertEqual(self.marker_status(), 'started')

    def test_swapped_pipe_after_precheck_blocks_before_authorization(self):
        saved = os.dup(self.pipe_write)
        reader, writer = os.pipe()
        try:
            os.dup2(writer, self.pipe_write, inheritable=False)
            with patch.object(ops, '_MARKER_REQUIRED', True), \
                 patch.object(dispatch, 'prepare_config_write') as prepare:
                with self.assertRaises(ops.MarkerSafetyError):
                    ops._prepare_write(str(self.config), 'route-add')
            prepare.assert_not_called()
        finally:
            os.dup2(saved, self.pipe_write, inheritable=False)
            os.close(saved)
            os.close(reader)
            os.close(writer)
        self.assertEqual(json.loads(self.config.read_text()), {'routes': []})

    def test_lost_pipe_reader_after_precheck_blocks_probe(self):
        reader, writer = os.pipe()
        try:
            with patch.dict(os.environ, {'CONFIG_OPS_RELOAD_MARKER_FD': str(writer)}), \
                 patch.object(ops, '_MARKER_BASELINE', None), \
                 patch.object(ops, '_MARKER_REQUIRED', True):
                ops._precheck_inherited_marker()
                os.close(reader)
                reader = None
                supply = {'url': 'http://127.0.0.1/v1/messages', 'protocol': 'anthropic',
                          'appkey': 'fake', 'target_model': 'fake'}
                with patch.object(ops.urllib.request, 'urlopen') as upstream:
                    with self.assertRaises(ops.MarkerSafetyError):
                        ops.probe_effort(supply)
                upstream.assert_not_called()
        finally:
            if reader is not None:
                os.close(reader)
            os.close(writer)

    def test_readonly_helper_requires_no_marker_and_emits_sentinel(self):
        env = dict(os.environ)
        env.pop('CONFIG_OPS_RELOAD_MARKER', None)
        env.pop('CONFIG_OPS_RELOAD_MARKER_FD', None)
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sys, 'argv', ['helper', 'route-list', str(self.config)]), \
             contextlib.redirect_stdout(output):
            ops.main()
        self.assertEqual(output.getvalue().splitlines()[-1], '__RELOAD__=no')

    def test_readonly_helper_rejects_unexpected_marker_before_preview(self):
        with patch.object(sys, 'argv', ['helper', 'route-list', str(self.config)]), \
             patch.object(ops, '_format_routes') as formatter, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                ops.main()
        formatter.assert_not_called()
        self.assertEqual(self.marker_status(), 'started')

    def test_cancelled_mutation_emits_no_status_and_does_not_write(self):
        self.config.write_text(json.dumps({'routes': [{'id': 'fake'}]}))
        before = self.config.read_bytes()
        with patch.object(sys, 'argv', ['helper', 'route-del', str(self.config), 'fake']), \
             patch.object(dispatch, 'prepare_config_write', self.plan, create=True), \
             patch.object(ops, 'confirm', return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                ops.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(self.marker_status(), 'no')
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(list(self.root.glob('config.json.bak.*')))

    def test_no_dispatch_integration_fails_before_edit(self):
        with patch.object(dispatch, 'prepare_config_write', None, create=True):
            with self.assertRaisesRegex(ops.WritePreflightFailed, '未接通'):
                ops._prepare_write(str(self.config), 'route-add')
        self.assertEqual(self.marker_status(), 'started')
        self.assertEqual(json.loads(self.config.read_text()), {'routes': []})

    def test_snapshot_used_before_edit_then_success_sets_marker(self):
        with self.ready():
            self.assertEqual(ops.load_config(str(self.config)), {'routes': []})
            ops.atomic_write(str(self.config), {'routes': [{'id': 'synthetic'}]})
            self.assertEqual(ops.load_config(str(self.config)), {'routes': [{'id': 'synthetic'}]})
        self.assertEqual(self.marker_status(), 'yes')

    def test_large_ordinary_config_edit_and_marker_without_default_limit(self):
        value = 'a' * (2 * 1024 * 1024 + 97)
        self.config.write_text(json.dumps({'routes': [], 'blob': value}))
        original = self.config.read_bytes()
        with self.ready():
            self.assertIsNone(ops._WRITE_BASELINE.max_bytes)
            cfg = ops.load_config(str(self.config))
            cfg['blob'] = 'b' * (len(value) + 23)
            ops.atomic_write(str(self.config), cfg)
        self.assertEqual(self.marker_status(), 'yes')
        self.assertGreater(self.config.stat().st_size, 2 * 1024 * 1024)
        self.assertEqual(json.loads(self.config.read_text()), cfg)
        backups = list(self.root.glob('config.json.bak.*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_link_change_after_writer_postcheck_stops_yes(self):
        from core.mac_config_writer import replace_config as real_replace
        alias = self.root / 'business-link'
        alias.symlink_to(self.config.name)
        other = self.root / 'other.json'
        other.write_text('{"routes": []}\n')
        other.chmod(0o600)
        def replace_then_repoint(*args, **kwargs):
            result = real_replace(*args, **kwargs)
            alias.unlink()
            alias.symlink_to(other.name)
            return result
        with patch.object(dispatch, 'prepare_config_write', self.plan, create=True):
            ops._prepare_write(str(alias), 'route-add')
        with patch('core.mac_config_writer.replace_config', side_effect=replace_then_repoint):
            with self.assertRaises(MacWriteError) as caught:
                ops.atomic_write(str(alias), {'routes': [{'id': 'saved'}]})
        self.assertEqual(caught.exception.state, 'POSTCHECK_FAILED')
        self.assertEqual(json.loads(self.config.read_text()), {'routes': [{'id': 'saved'}]})
        self.assertEqual(json.loads(other.read_text()), {'routes': []})
        self.assertEqual(self.marker_status(), 'writing')

    def test_marker_fd_close_error_is_reported_once(self):
        original_close = ops._close_marker_fd
        pipe_inode = os.fstat(self.pipe_write).st_ino
        close_calls = []
        def close_then_fail(fd):
            if os.fstat(fd).st_ino == pipe_inode:
                close_calls.append(fd)
                os.close(fd)
                raise ops.MarkerSafetyError('fake close failure', close_unknown=True)
            original_close(fd)
        with self.ready(), patch.object(ops, '_close_marker_fd', side_effect=close_then_fail):
            with self.assertRaises(MacWriteError) as caught:
                ops.atomic_write(str(self.config), {'routes': [{'id': 'new'}]})
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertTrue(caught.exception.close_unknown)
        self.assertTrue(caught.exception.marker_unknown)
        self.assertEqual(len(close_calls), 1)
        self.assertEqual(self.marker_status(), 'writing')
        self.assertEqual(json.loads(self.config.read_text()), {'routes': []})

    def test_marker_close_unknown_is_retained_when_resetting_no(self):
        original_marker = ops._set_marker
        original_close = ops._close_marker_fd
        closes = []
        def marker(text):
            original_marker(text)
            if text == 'writing':
                self.config.write_text('{"external": true}')
        def close_then_fail(fd):
            closes.append(fd)
            if len(closes) == 2:
                os.close(fd)
                raise ops.MarkerSafetyError('fake close failure', close_unknown=True)
            original_close(fd)
        with self.ready(), patch.object(ops, '_set_marker', side_effect=marker), \
             patch.object(ops, '_close_marker_fd', side_effect=close_then_fail), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(MacWriteError) as caught:
                ops.atomic_write(str(self.config), {'routes': []})
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertTrue(caught.exception.close_unknown)
        self.assertTrue(caught.exception.marker_unknown)
        self.assertEqual(len(closes), 2)
        self.assertEqual(self.marker_status(), 'no')
        self.assertEqual(json.loads(self.config.read_text()), {'external': True})

    def test_untrusted_direct_helper_marker_is_never_overwritten(self):
        unrelated = self.root / 'unrelated'
        unrelated.write_text('started')
        before = self.config.read_bytes()
        with patch.dict(os.environ, {'CONFIG_OPS_RELOAD_MARKER': str(unrelated),
                                     'CONFIG_OPS_RELOAD_MARKER_FD': ''}):
            with self.assertRaises(ops.MarkerSafetyError):
                ops._set_marker('no')
            with self.ready():
                with self.assertRaises(MacWriteError) as caught:
                    ops.atomic_write(str(self.config), {'routes': [{'id': 'new'}]})
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertEqual(unrelated.read_text(), 'started')
        self.assertEqual(self.config.read_bytes(), before)

    def test_untrusted_regular_file_fd_is_never_written(self):
        unrelated = self.root / 'unrelated-fd'
        unrelated.write_bytes(b'started')
        fd = os.open(unrelated, os.O_RDWR)
        try:
            with patch.dict(os.environ, {'CONFIG_OPS_RELOAD_MARKER_FD': str(fd)}):
                with self.assertRaises(ops.MarkerSafetyError):
                    ops._set_marker('no')
            self.assertEqual(unrelated.read_bytes(), b'started')
        finally:
            os.close(fd)

    def test_named_fifo_fd_is_not_an_anonymous_parent_pipe(self):
        fifo = self.root / 'named-fifo'
        os.mkfifo(fifo, 0o600)
        fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        try:
            with patch.dict(os.environ, {'CONFIG_OPS_RELOAD_MARKER_FD': str(fd)}):
                with self.assertRaises(ops.MarkerSafetyError):
                    ops._set_marker('no')
        finally:
            os.close(fd)

    def test_probe_refuses_if_reader_closes_during_snapshot_verification(self):
        from core.mac_config_path import verify_config as real_verify
        supply = {'url': 'http://127.0.0.1/v1/messages', 'protocol': 'anthropic',
                  'appkey': 'fake-key', 'target_model': 'fake'}
        closed = []
        def verify_and_close(snapshot):
            if not closed:
                self.close_pipe_reader()
                closed.append(True)
            return real_verify(snapshot)
        with self.ready(), patch.object(ops, '_MARKER_REQUIRED', True), \
             patch('core.mac_config_path.verify_config', side_effect=verify_and_close), \
             patch.object(ops.urllib.request, 'urlopen') as upstream:
            with self.assertRaises(ops.MarkerSafetyError):
                ops.probe_effort(supply)
        self.assertEqual(closed, [True])
        upstream.assert_not_called()
        self.assertEqual(json.loads(self.config.read_text()), {'routes': []})

    def test_probe_refuses_if_reader_closes_during_authorization_recheck(self):
        supply = {'url': 'http://127.0.0.1/v1/messages', 'protocol': 'anthropic',
                  'appkey': 'fake-key', 'target_model': 'fake'}
        with self.ready(), patch.object(ops, '_MARKER_REQUIRED', True):
            real_preflight = ops._WRITE_PREFLIGHT
            def preflight_and_close(snapshot):
                result = real_preflight(snapshot)
                self.close_pipe_reader()
                return result
            with patch.object(ops, '_WRITE_PREFLIGHT', side_effect=preflight_and_close), \
                 patch.object(ops.urllib.request, 'urlopen') as upstream:
                with self.assertRaises(ops.MarkerSafetyError):
                    ops.probe_effort(supply)
        upstream.assert_not_called()
        self.assertEqual(json.loads(self.config.read_text()), {'routes': []})

    def test_probe_last_check_catches_reader_close_after_credentials(self):
        from core.mac_config_path import verify_config as real_verify
        supply = {'url': 'http://127.0.0.1/v1/messages', 'protocol': 'anthropic',
                  'appkey': 'fake-key', 'target_model': 'fake'}
        armed, closed = [], []
        def credential(_supply):
            armed.append(True)
            return 'fake-key'
        def verify_and_close(snapshot):
            if armed and not closed:
                self.close_pipe_reader()
                closed.append(True)
            return real_verify(snapshot)
        with self.ready(), patch.object(ops, '_MARKER_REQUIRED', True), \
             patch('core.mac_config_path.verify_config', side_effect=verify_and_close), \
             patch.object(ops, 'resolve_appkey', side_effect=credential), \
             patch.object(ops.urllib.request, 'urlopen') as upstream:
            with self.assertRaises(ops.MarkerSafetyError):
                ops.probe_effort(supply)
        self.assertEqual(armed, [True])
        self.assertEqual(closed, [True])
        upstream.assert_not_called()

    def test_probe_with_live_peer_and_valid_authorization_still_sends_once(self):
        supply = {'url': 'http://127.0.0.1/v1/messages', 'protocol': 'anthropic',
                  'appkey': 'fake-key', 'target_model': 'fake'}
        class FakeResponse:
            status = 200
            def read(self): return b'{"fake": true}'
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with self.ready(), patch.object(ops, '_MARKER_REQUIRED', True), \
             patch.object(ops.urllib.request, 'urlopen', return_value=FakeResponse()) as upstream:
            result = ops.probe_effort(supply)
        self.assertEqual(result[0], 200)
        upstream.assert_called_once()

    def test_peer_closes_during_preflight_before_backup_creates_nothing(self):
        original = self.config.read_bytes()
        with self.ready(), patch.object(ops, '_MARKER_REQUIRED', True):
            real_preflight = ops._WRITE_PREFLIGHT
            calls = []
            def preflight(snapshot):
                calls.append(True)
                if len(calls) == 2:
                    self.close_pipe_reader()
                return real_preflight(snapshot)
            with patch.object(ops, '_WRITE_PREFLIGHT', side_effect=preflight), \
                 patch('core.mac_config_writer._open_private') as create:
                with self.assertRaises(MacWriteError) as caught:
                    ops.atomic_write(str(self.config), {'routes': [{'id': 'new'}]})
        self.assertEqual(len(calls), 2)
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertIsNone(caught.exception.backup_name)
        create.assert_not_called()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(list(self.root.glob('config.json.bak.*')))
        self.assertFalse(list(self.root.glob('.model-proxy-config-*')))

    def test_peer_closes_after_backup_blocks_staging_and_replace(self):
        from core.mac_config_writer import _open_private as real_create
        original = self.config.read_bytes()
        calls = []
        def create_then_close(*args, **kwargs):
            result = real_create(*args, **kwargs)
            calls.append(True)
            self.close_pipe_reader()
            return result
        with self.ready(), patch.object(ops, '_MARKER_REQUIRED', True), \
             patch('core.mac_config_writer._open_private', side_effect=create_then_close), \
             patch('core.mac_config_writer.os.replace') as replace:
            with self.assertRaises(MacWriteError) as caught:
                ops.atomic_write(str(self.config), {'routes': [{'id': 'new'}]})
        self.assertEqual(calls, [True])
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertTrue(caught.exception.backup_name)
        self.assertEqual((self.root / caught.exception.backup_name).read_bytes(), original)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(list(self.root.glob('.model-proxy-config-*')))
        replace.assert_not_called()

    def test_peer_closes_during_final_preflight_blocks_replace(self):
        original = self.config.read_bytes()
        with self.ready(), patch.object(ops, '_MARKER_REQUIRED', True):
            real_preflight = ops._WRITE_PREFLIGHT
            calls = []
            def preflight(snapshot):
                calls.append(True)
                if len(calls) == 4:
                    self.close_pipe_reader()
                return real_preflight(snapshot)
            with patch.object(ops, '_WRITE_PREFLIGHT', side_effect=preflight), \
                 patch('core.mac_config_writer.os.replace') as replace, \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(MacWriteError) as caught:
                    ops.atomic_write(str(self.config), {'routes': [{'id': 'new'}]})
        self.assertEqual(len(calls), 4)
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertTrue(caught.exception.marker_unknown)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(list(self.root.glob('.model-proxy-config-*')))
        replace.assert_not_called()

    def test_probe_rechecks_after_credential_resolution_before_send(self):
        from core.mac_config_path import MacConfigError
        self.config.write_text(json.dumps({'supplies': [{'id': 'fake',
            'url': 'http://127.0.0.1/v1/messages', 'protocol': 'anthropic',
            'appkey': 'fake-key', 'target_model': 'fake'}]}))
        alias = self.root / 'business-link'
        alias.symlink_to(self.config.name)
        with patch.object(dispatch, 'prepare_config_write', self.plan, create=True):
            ops._prepare_write(str(alias), 'supply-check')
        cfg = ops.load_config(str(alias))
        def fake_credential(_supply):
            alias.unlink()
            alias.symlink_to('./config.json')
            return 'fake-key'
        with patch.object(ops, 'resolve_appkey', side_effect=fake_credential), \
             patch.object(ops.urllib.request, 'urlopen') as upstream:
            with self.assertRaises(MacConfigError):
                ops.probe_effort(cfg['supplies'][0])
        upstream.assert_not_called()

    def test_change_during_yes_marker_fails_after_commit_without_success(self):
        alias = self.root / 'business-link'
        alias.symlink_to(self.config.name)
        other = self.root / 'other.json'
        other.write_text('{"routes": []}\n')
        other.chmod(0o600)
        with patch.object(dispatch, 'prepare_config_write', self.plan, create=True):
            ops._prepare_write(str(alias), 'route-add')
        original_marker = ops._set_marker
        def changing_marker(text):
            original_marker(text)
            if text == 'yes':
                alias.unlink()
                alias.symlink_to(other.name)
        with patch.object(ops, '_set_marker', side_effect=changing_marker):
            with self.assertRaises(MacWriteError) as caught:
                ops.atomic_write(str(alias), {'routes': [{'id': 'saved'}]})
        self.assertEqual(caught.exception.state, 'POSTCHECK_FAILED')
        self.assertEqual(self.marker_status(), 'yes')
        self.assertEqual(json.loads(self.config.read_text()), {'routes': [{'id': 'saved'}]})
        self.assertEqual(json.loads(other.read_text()), {'routes': []})

    def test_edit_interaction_chain_change_blocks_upstream(self):
        from core.mac_config_path import MacConfigError
        self.config.write_text(json.dumps({'supplies': [{'id': 'fake',
            'url': 'http://127.0.0.1/v1/messages', 'protocol': 'anthropic',
            'appkey': 'fake-key', 'target_model': 'fake'}]}))
        alias = self.root / 'business-link'
        alias.symlink_to(self.config.name)
        with patch.object(dispatch, 'prepare_config_write', self.plan, create=True):
            ops._prepare_write(str(alias), 'supply-edit')
        calls = []
        def answer(_prompt):
            calls.append(True)
            if len(calls) == 5:
                alias.unlink()
                alias.symlink_to('./config.json')
            return ''
        with patch('builtins.input', side_effect=answer), \
             patch.object(ops, 'confirm', return_value=True), \
             patch.object(ops.urllib.request, 'urlopen') as upstream, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(MacConfigError):
                ops.supply_edit(str(alias), 'fake')
        upstream.assert_not_called()
        self.assertEqual(self.marker_status(), 'started')

    def test_explicit_probe_with_stable_config_still_calls_upstream(self):
        self.config.write_text(json.dumps({'supplies': [{'id': 'fake',
            'url': 'http://127.0.0.1/v1/messages', 'protocol': 'anthropic',
            'appkey': 'fake-key', 'target_model': 'fake'}]}))
        with patch.object(dispatch, 'prepare_config_write', self.plan, create=True):
            ops._prepare_write(str(self.config), 'supply-edit')
        class FakeResponse:
            status = 401
            def read(self): return b'fake unauthorized'
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with patch('builtins.input', return_value=''), \
             patch.object(ops, 'confirm', return_value=True), \
             patch.object(ops.urllib.request, 'urlopen', return_value=FakeResponse()) as upstream, \
             patch.object(ops, 'atomic_write') as write, patch.object(ops, 'done'), \
             contextlib.redirect_stdout(io.StringIO()):
            ops.supply_edit(str(self.config), 'fake')
        upstream.assert_called_once()
        write.assert_called_once()

    def test_edit_race_never_commits_or_requests_reload(self):
        with self.ready():
            self.config.write_text('{"external": true}')
            with self.assertRaises(MacWriteError) as caught:
                ops.atomic_write(str(self.config), {'routes': []})
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertEqual(self.marker_status(), 'started')
        self.assertEqual(json.loads(self.config.read_text()), {'external': True})

    def test_after_writing_marker_race_resets_to_no(self):
        with self.ready():
            original = ops._set_marker
            def mutate(text):
                original(text)
                if text == 'writing':
                    self.config.write_text('{"external": true}')
            with patch.object(ops, '_set_marker', side_effect=mutate):
                with self.assertRaises(MacWriteError) as caught:
                    ops.atomic_write(str(self.config), {'routes': []})
            self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertEqual(self.marker_status(), 'no')
        self.assertEqual(json.loads(self.config.read_text()), {'external': True})

    def test_postcheck_failure_reports_committed_and_no_reload_marker(self):
        with self.ready(), patch('core.mac_config_writer.capture_config', side_effect=RuntimeError('test')):
            with self.assertRaises(MacWriteError) as caught:
                ops.atomic_write(str(self.config), {'routes': [{'id': 'x'}]})
        self.assertEqual(caught.exception.state, 'POSTCHECK_FAILED')
        self.assertEqual(self.marker_status(), 'writing')
        self.assertEqual(json.loads(self.config.read_text()), {'routes': [{'id': 'x'}]})

    def test_main_reports_postcheck_failure_as_committed_without_reload(self):
        def edit(path):
            ops.atomic_write(path, {'routes': [{'id': 'saved'}]})
        errors = io.StringIO()
        with patch.object(dispatch, 'prepare_config_write', self.plan, create=True), \
             patch.dict(ops._DISPATCH, {'synthetic-edit': (edit, 0)}), \
             patch.object(sys, 'argv', ['helper', 'synthetic-edit', str(self.config)]), \
             patch('core.mac_config_writer.capture_config', side_effect=RuntimeError('test')), \
             contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as caught:
                ops.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn('目标已改变，后验验证失败、保护状态未知', errors.getvalue())
        self.assertIn('未触发 reload', errors.getvalue())
        self.assertEqual(self.marker_status(), 'writing')
        self.assertEqual(json.loads(self.config.read_text()), {'routes': [{'id': 'saved'}]})

    def test_main_reports_replace_exception_as_unknown_not_uncommitted(self):
        def edit(path):
            ops.atomic_write(path, {'routes': [{'id': 'new'}]})
        errors = io.StringIO()
        with patch.object(dispatch, 'prepare_config_write', self.plan, create=True), \
             patch.dict(ops._DISPATCH, {'synthetic-edit': (edit, 0)}), \
             patch.object(sys, 'argv', ['helper', 'synthetic-edit', str(self.config)]), \
             patch('core.mac_config_writer.os.replace', side_effect=OSError('test')), \
             contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as caught:
                ops.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn('提交结果未知', errors.getvalue())
        self.assertNotIn('文件描述符关闭状态未知', errors.getvalue())
        self.assertEqual(self.marker_status(), 'writing')
        self.assertEqual(json.loads(self.config.read_text()), {'routes': []})

    def test_yes_marker_failure_reports_saved_not_reload(self):
        with self.ready():
            original = ops._set_marker
            def marker(text):
                if text == 'yes':
                    raise OSError('fake marker failure')
                original(text)
            with patch.object(ops, '_set_marker', side_effect=marker), \
                 contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(MacWriteError) as caught:
                    ops.atomic_write(str(self.config), {'routes': [{'id': 'saved'}]})
            self.assertEqual(caught.exception.state, 'POSTCHECK_FAILED')
            self.assertIn('已保存', err.getvalue())
        self.assertEqual(self.marker_status(), 'writing')
        self.assertEqual(json.loads(self.config.read_text()), {'routes': [{'id': 'saved'}]})

    def _marker_attempt(self, path):
        with patch.object(ops, '_WRITE_INPUT_PATH', str(self.config)), \
             patch.dict(os.environ, {'CONFIG_OPS_RELOAD_MARKER': str(path),
                                     'CONFIG_OPS_RELOAD_MARKER_FD': ''}):
            ops._set_marker('no')

    def test_standalone_named_marker_is_rejected_without_writing(self):
        original = self.config.read_bytes()
        with tempfile.TemporaryDirectory(prefix='model-proxy-cli-', dir=self.root) as directory:
            marker = Path(directory) / 'reload'
            marker.write_text('started', encoding='utf-8')
            with self.assertRaises(ops.MarkerSafetyError):
                self._marker_attempt(marker)
            self.assertEqual(marker.read_bytes(), b'started')
        self.assertEqual(self.config.read_bytes(), original)

    def test_named_marker_parent_symlink_is_rejected_and_retained(self):
        real = self.root / 'marker-directory'
        real.mkdir(mode=0o700)
        (real / 'reload').write_text('started')
        alias = self.root / 'marker-directory-link'
        alias.symlink_to(real.name)
        before = os.lstat(alias)
        with self.assertRaises(ops.MarkerSafetyError):
            self._marker_attempt(alias / 'reload')
        after = os.lstat(alias)
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self.assertEqual(os.readlink(alias), real.name)
        self.assertEqual((real / 'reload').read_text(), 'started')

    def test_dispatch_anonymous_pipe_with_in_process_helper(self):
        observed = []
        def helper(args, *, env, pass_fds):
            self.assertNotIn('CONFIG_OPS_RELOAD_MARKER', env)
            self.assertEqual(pass_fds, (int(env['CONFIG_OPS_RELOAD_MARKER_FD']),))
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(sys, 'argv', ['helper', *args[2:]]), \
                 contextlib.redirect_stdout(io.StringIO()):
                ops.main()
            observed.append(True)
            return SimpleNamespace(returncode=0)
        class NoReload:
            def reload(self):
                self_called.append(True)
                return 0
        self_called = []
        def cancel(path):
            ops.load_config(path)
            ops.done(False)
        with patch.object(dispatch.subprocess, 'run', side_effect=helper), \
             patch.object(dispatch, '_marker_fd_protocol_available', return_value=True), \
             patch.object(dispatch, 'prepare_config_write', self.plan, create=True), \
             patch.dict(ops._DISPATCH, {'synthetic-edit': (cancel, 0)}):
            result = dispatch.run_helper('synthetic-edit', [], str(self.config), NoReload(), root=self.project)
        self.assertEqual(result, 0)
        self.assertEqual(observed, [True])
        self.assertEqual(self_called, [])
        self.assertEqual(self.marker.read_text(), 'started')

    def test_dispatch_does_not_reload_after_postcommit_link_swap(self):
        from core.mac_config_writer import replace_config as real_replace
        alias = self.root / 'business-link'
        alias.symlink_to(self.config.name)
        other = self.root / 'other.json'
        other.write_text('{"routes": []}\n')
        other.chmod(0o600)
        def replace_then_repoint(*args, **kwargs):
            result = real_replace(*args, **kwargs)
            alias.unlink()
            alias.symlink_to(other.name)
            return result
        def edit(path):
            ops.atomic_write(path, {'routes': [{'id': 'saved'}]})
        def child(args, *, env, pass_fds):
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(sys, 'argv', ['helper', *args[2:]]), \
                 patch('core.mac_config_writer.replace_config', side_effect=replace_then_repoint), \
                 contextlib.redirect_stderr(io.StringIO()):
                try:
                    ops.main()
                except SystemExit as exc:
                    return SimpleNamespace(returncode=exc.code)
            return SimpleNamespace(returncode=0)
        class Control:
            calls = []
            def reload(self):
                self.calls.append('reload')
                return 0
        control = Control()
        with patch.object(dispatch.subprocess, 'run', side_effect=child), \
             patch.object(dispatch, '_marker_fd_protocol_available', return_value=True), \
             patch.object(dispatch, 'prepare_config_write', self.plan, create=True), \
             patch.dict(ops._DISPATCH, {'synthetic-edit': (edit, 0)}), \
             contextlib.redirect_stderr(io.StringIO()):
            result = dispatch.run_helper('synthetic-edit', [], str(alias), control, root=self.project)
        self.assertEqual(result, 1)
        self.assertEqual(control.calls, [])
        self.assertEqual(json.loads(self.config.read_text()), {'routes': [{'id': 'saved'}]})
        self.assertEqual(json.loads(other.read_text()), {'routes': []})

    def test_dispatch_real_child_switch_with_anonymous_pipe(self):
        src = Path(dispatch.__file__).resolve().parents[1]
        shutil.copytree(src, self.project / 'core', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        config = self.project / 'config' / 'business.json'
        config.write_text(json.dumps({'routes': [{'id': 'old'}, {'id': 'new'}],
                                      'strategies': [{'client_token': 'fake-token', 'route_id': 'old'}]}))
        config.chmod(0o600)
        guard = self.root / 'guard'
        guard.mkdir()
        (guard / 'sitecustomize.py').write_text(
            "import socket\n"
            "def deny(*a, **k): raise PermissionError('network forbidden')\n"
            "socket.socket.connect=deny; socket.socket.connect_ex=deny; "
            "socket.socket.bind=deny; socket.socket.sendto=deny; socket.getaddrinfo=deny\n")
        class Control:
            calls = []
            def reload(self):
                self.calls.append('reload')
                return 0
        control = Control()
        frames = []
        real_drain = dispatch._drain_marker_pipe
        def record_frames(fd, proof):
            result = real_drain(fd, proof)
            frames.append(result)
            return result
        with patch.dict(os.environ, {'PYTHONPATH': str(guard), 'PYTHONNOUSERSITE': '1',
                                     'HOME': str(self.root), 'TMPDIR': str(self.root)}), \
             patch.object(dispatch, '_drain_marker_pipe', side_effect=record_frames), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = dispatch.run_helper('switch', ['fake-token', 'new'], str(config), control,
                                         root=self.project)
            failed = dispatch.run_helper('switch', ['fake-token', 'missing-route'], str(config),
                                         control, root=self.project)
        self.assertEqual(result, 0)
        self.assertEqual(failed, 1)
        self.assertEqual(frames, [b'writing\nyes\nyes\n', b'no\n'])
        self.assertEqual(control.calls, ['reload'])
        self.assertEqual(json.loads(config.read_text())['strategies'][0]['route_id'], 'new')

    def test_marker_cannot_be_actual_config_or_its_final_link(self):
        original = self.config.read_bytes()
        alias = self.root / 'config-alias'
        alias.symlink_to(self.config.name)
        before = (os.lstat(alias).st_dev, os.lstat(alias).st_ino, os.readlink(alias))
        parent_alias = self.root / 'parent-alias'
        parent_alias.symlink_to('.')
        for marker in (self.config, alias, parent_alias / self.config.name):
            with self.subTest(marker=marker), self.assertRaises(ops.MarkerSafetyError):
                self._marker_attempt(marker)
            self.assertEqual(self.config.read_bytes(), original)
            st = os.lstat(alias)
            self.assertEqual((st.st_dev, st.st_ino, os.readlink(alias)), before)

    def test_atomic_write_with_config_marker_never_truncates_config(self):
        original = self.config.read_bytes()
        alias = self.root / 'config-alias'
        alias.symlink_to(self.config.name)
        before = os.lstat(alias)
        for marker in (self.config, alias):
            with self.subTest(marker=marker), \
                 patch.dict(os.environ, {'CONFIG_OPS_RELOAD_MARKER': str(marker)}):
                with self.ready():
                    with self.assertRaises(MacWriteError) as caught:
                        ops.atomic_write(str(self.config), {'routes': [{'id': 'new'}]})
                self.assertEqual(caught.exception.state, 'NOT_REPLACED')
                self.assertFalse(caught.exception.marker_unknown)
                self.assertEqual(self.config.read_bytes(), original)
                after = os.lstat(alias)
                self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
                self.assertEqual(os.readlink(alias), self.config.name)

    def test_marker_pipe_write_failure_reports_marker_unknown(self):
        original = self.config.read_bytes()
        real_write = os.write
        def fail_frame(fd, data):
            if data == b'writing\n':
                raise OSError('fake pipe failure')
            return real_write(fd, data)
        with self.ready(), patch.object(os, 'write', side_effect=fail_frame):
            with self.assertRaises(MacWriteError) as caught:
                ops.atomic_write(str(self.config), {'routes': [{'id': 'new'}]})
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertTrue(caught.exception.marker_unknown)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.marker_status(), 'started')

    def test_marker_cannot_be_hardlink_to_config(self):
        original = self.config.read_bytes()
        alias = self.root / 'hardlink'
        os.link(self.config, alias)
        with self.assertRaises(ops.MarkerSafetyError):
            self._marker_attempt(alias)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(alias.read_bytes(), original)

    def test_marker_cannot_alias_protected_roles_sidecar_or_backup(self):
        targets = [Path(value) for value in self.runtime.values()]
        targets += [self.root / 'session_overrides.json',
                    self.root / 'config.json.bak.0123456789abcdef',
                    self.root / 'CONFIG.JSON.BAK.0123456789ABCDEF',
                    self.root / '.model-proxy-config-0123456789abcdef01234567']
        state = self.project / '.model_proxy_runtime'
        state.mkdir(mode=0o700)
        targets.append(state / 'install.json')
        original = self.config.read_bytes()
        for marker in targets:
            marker.write_bytes(b'started')
            with self.subTest(marker=marker), self.assertRaises(ops.MarkerSafetyError):
                self._marker_attempt(marker)
            self.assertEqual(marker.read_bytes(), b'started')
            self.assertEqual(self.config.read_bytes(), original)
            marker.unlink()

    def test_marker_cannot_alias_runtime_paths_file(self):
        marker = self.project / 'config' / 'runtime_paths.json'
        original, config = marker.read_bytes(), self.config.read_bytes()
        with self.assertRaises(ops.MarkerSafetyError):
            self._marker_attempt(marker)
        self.assertEqual(marker.read_bytes(), original)
        self.assertEqual(self.config.read_bytes(), config)

    def test_untrusted_marker_path_is_rejected_before_preflight(self):
        original = self.config.read_bytes()
        with patch.object(dispatch, 'write_preflight') as preflight:
            with self.assertRaises(ops.MarkerSafetyError):
                self._marker_attempt(self.marker)
        preflight.assert_not_called()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.marker.read_text(), 'started')

    def test_untrusted_changed_marker_parent_is_never_written(self):
        real = self.root / 'marker-directory'
        real.mkdir(mode=0o700)
        marker = real / 'reload'
        marker.write_text('started')
        alias = self.root / 'marker-directory-link'
        alias.symlink_to(real.name)
        with self.assertRaises(ops.MarkerSafetyError):
            self._marker_attempt(alias / 'reload')
        alias.unlink()
        alias.symlink_to('./marker-directory')
        with self.assertRaises(ops.MarkerSafetyError):
            self._marker_attempt(alias / 'reload')
        self.assertEqual(marker.read_text(), 'started')
        self.assertEqual(self.config.read_text(), '{"routes": []}\n')

    def test_marker_missing_is_never_created(self):
        missing = self.root / 'missing'
        with self.assertRaises(ops.MarkerSafetyError):
            self._marker_attempt(missing)
        self.assertFalse(missing.exists())

    def test_mutating_link_after_preflight_prevents_preview_and_probe(self):
        from core.mac_config_path import MacConfigError
        self.config.write_text(json.dumps({'supplies': [{'id': 'fake', 'url': 'http://127.0.0.1',
                               'protocol': 'anthropic', 'appkey': 'fake', 'target_model': 'fake'}]}))
        alias = self.root / 'config-alias'
        alias.symlink_to(self.config.name)
        def plan_and_change(root, snap, *, operation):
            grant, _ = self.plan(root, snap, operation=operation)
            def changed(_snapshot):
                alias.unlink()
                alias.symlink_to('./config.json')
                return True
            return grant, changed
        with patch.object(dispatch, 'prepare_config_write', plan_and_change, create=True):
            ops._prepare_write(str(alias), 'supply-check')
        output = io.StringIO()
        with patch.object(ops, 'probe_effort') as probe, \
             patch.object(ops.urllib.request, 'urlopen') as upstream, \
             contextlib.redirect_stdout(output):
            with self.assertRaises(MacConfigError):
                ops.supply_check(str(alias), 'fake')
        probe.assert_not_called()
        upstream.assert_not_called()
        self.assertEqual(output.getvalue(), '')

    def test_main_reports_marker_fd_close_unknown_after_commit(self):
        def edit(path):
            ops.atomic_write(path, {'routes': [{'id': 'saved'}]})
            ops.done(True)
        real_close = ops._close_marker_fd
        closes = []
        def close_then_fail(fd):
            closes.append(fd)
            if len(closes) == 3:
                os.close(fd)
                raise ops.MarkerSafetyError('fake close failure', close_unknown=True)
            real_close(fd)
        errors = io.StringIO()
        with patch.object(dispatch, 'prepare_config_write', self.plan, create=True), \
             patch.dict(ops._DISPATCH, {'synthetic-edit': (edit, 0)}), \
             patch.object(sys, 'argv', ['helper', 'synthetic-edit', str(self.config)]), \
             patch.object(ops, '_close_marker_fd', side_effect=close_then_fail), \
             contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as caught:
                ops.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(len(closes), 3)
        self.assertIn('FD 关闭状态未知', errors.getvalue())
        self.assertIn('未触发 reload', errors.getvalue())
        self.assertEqual(self.marker_status(), 'yes')
        self.assertEqual(json.loads(self.config.read_text()), {'routes': [{'id': 'saved'}]})

    def test_main_reports_marker_unknown_after_partial_marker_write(self):
        original = self.config.read_bytes()
        def edit(path):
            ops.atomic_write(path, {'routes': [{'id': 'new'}]})
        errors = io.StringIO()
        real_write = os.write
        def fail_frame(fd, data):
            if data == b'writing\n':
                raise OSError('fake pipe failure')
            return real_write(fd, data)
        with patch.object(dispatch, 'prepare_config_write', self.plan, create=True), \
             patch.dict(ops._DISPATCH, {'synthetic-edit': (edit, 0)}), \
             patch.object(sys, 'argv', ['helper', 'synthetic-edit', str(self.config)]), \
             patch.object(os, 'write', side_effect=fail_frame), \
             contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit):
                ops.main()
        self.assertIn('目标未替换', errors.getvalue())
        self.assertIn('reload 标记写入状态未知', errors.getvalue())
        self.assertEqual(self.config.read_bytes(), original)

    def test_backup_error_does_not_expose_sensitive_filename(self):
        def edit(path):
            ops.atomic_write(path, {'routes': []})
        errors = io.StringIO()
        with patch.object(dispatch, 'prepare_config_write', self.plan, create=True), \
             patch.dict(ops._DISPATCH, {'synthetic-edit': (edit, 0)}), \
             patch.object(sys, 'argv', ['helper', 'synthetic-edit', str(self.config)]), \
             patch('core.mac_config_writer.replace_config', side_effect=MacWriteError(
                 'test', backup_name='fake-secret-customer-config.json.bak.123')), \
             contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit):
                ops.main()
        self.assertIn('可能已保留备份', errors.getvalue())
        self.assertNotIn('fake-secret', errors.getvalue())

    def test_windows_requires_preflight_and_wires_writer(self):
        from core import windows_secure_files as secure

        path = str(self.config)
        original = self.config.read_bytes()
        with patch.object(ops.os, 'name', 'nt'):
            # This POSIX fixture is not a Windows path; capture fails closed.
            with patch.object(secure, 'write_target') as writer:
                with self.assertRaises(secure.SecureSnapshotError) as caught:
                    ops._prepare_write(path, 'route-add')
                self.assertEqual(caught.exception.code, 'invalid_path')
                with self.assertRaises(ops.WritePreflightFailed):
                    ops.atomic_write(path, {'routes': []})
                writer.assert_not_called()

                denied = unittest.mock.Mock(spec=secure.TargetSnapshot)
                denied.exists = True
                with patch.object(secure, 'capture_target', return_value=denied) as capture, \
                     patch.object(dispatch, 'write_preflight', return_value=False) as check:
                    with self.assertRaises(ops.WritePreflightFailed):
                        ops._prepare_write(path, 'route-add')
                capture.assert_called_once_with(os.path.abspath(path),
                                                authorized_root=os.path.dirname(os.path.abspath(path)),
                                                max_bytes=16 * 1024 * 1024)
                check.assert_called_once_with(self.project, os.path.abspath(path), operation='route-add')
                denied.revalidate.assert_not_called()
                denied.close.assert_called_once()
                self.assertIsNone(ops._WRITE_BASELINE)
                self.assertIsNone(ops._WRITE_PREFLIGHT)
                with self.assertRaises(ops.WritePreflightFailed):
                    ops.atomic_write(path, {'routes': []})
                writer.assert_not_called()

            # The real write coordinator must recheck before constructing a
            # backend or staging anything, even after initial authorization.
            stale = unittest.mock.Mock(spec=secure.TargetSnapshot)
            stale.exists, stale._max_bytes = True, 16 * 1024 * 1024
            with patch.object(secure, 'capture_target', return_value=stale), \
                 patch.object(dispatch, 'write_preflight', side_effect=[True, False]) as check, \
                 patch.object(ops, 'confirm', return_value=True), \
                 patch('core.windows_secure_native.ProductWriteBackend') as backend, \
                 patch.object(secure, '_finite_write') as finite, \
                 patch.object(ops, '_set_marker') as marker, \
                 contextlib.redirect_stdout(io.StringIO()):
                ops._prepare_write(path, 'route-add')
                with self.assertRaises(secure.SecureWriteError) as caught:
                    ops.atomic_write(path, {'routes': []})
                self.assertEqual(caught.exception.code, 'preflight_failed')
                self.assertEqual(check.call_count, 2)
                backend.assert_not_called()
                finite.assert_not_called()
                marker.assert_not_called()
            stale.close()

            # Substitute the OS writer only to check the successful wiring;
            # this is not a native Windows or ACL result.
            snapshot = unittest.mock.Mock(spec=secure.TargetSnapshot)
            snapshot.exists = True
            outcome = secure.WriteOutcome('VERIFIED', None, False, 'NONE')
            def fake_write(actual, payload, *, preflight):
                self.assertIs(actual, snapshot)
                self.assertIs(preflight(), True)
                self.assertEqual(json.loads(payload.decode('utf-8')), {'routes': []})
                actual.close()
                return outcome
            with patch.multiple(ops, _WRITE_BASELINE=None, _WRITE_AUTHORIZATION=None,
                                _WRITE_PREFLIGHT=None, _WRITE_INPUT_PATH=None,
                                _WRITE_STARTED=False, _WRITE_COMMITTED=None), \
                 patch.object(secure, 'capture_target', return_value=snapshot) as capture, \
                 patch.object(dispatch, 'write_preflight', return_value=True) as check, \
                 patch.object(secure, 'write_target', side_effect=fake_write) as writer, \
                 patch.object(ops, 'confirm', return_value=True), \
                 patch.object(ops, '_set_marker') as marker, \
                 contextlib.redirect_stdout(io.StringIO()):
                ops._prepare_write(path, 'route-add')
                self.assertIs(ops._WRITE_BASELINE, snapshot)
                self.assertTrue(ops._WRITE_AUTHORIZATION)
                self.assertTrue(callable(ops._WRITE_PREFLIGHT))
                snapshot.revalidate.assert_called_once()
                ops.atomic_write(path, {'routes': []})
                capture.assert_called_once()
                self.assertEqual(check.call_count, 2)
                writer.assert_called_once()
                snapshot.close.assert_called_once()
                marker.assert_called_once_with('yes')
                self.assertIs(ops._WRITE_COMMITTED, outcome)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.marker_status(), 'started')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['config.json', 'marker', 'project'])
