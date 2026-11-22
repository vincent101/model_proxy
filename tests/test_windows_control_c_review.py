"""Control-line regressions: fake authority, temporary files and owned children."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import windows_process
from core.cli import windows_control as wc
from tests.test_windows_control_a_contract import ROOT, CONFIG, PATHS, RECORD


class StatusStructureTests(unittest.TestCase):
    def test_secret_named_supply_id_is_not_a_secret_field(self):
        data = {'supplies': [{'id': 'service_secret'}], 'routes': [], 'strategies': [],
                'cooldown': {'service_secret': {'remain': 12, 'reason': '429'}},
                'stream_health': {'supplies': {'service_secret': {'consecutive': 1}}},
                'admin_token': 'private', 'password': 'private'}
        safe = wc._sanitize_status(data)
        self.assertEqual(safe['cooldown'], data['cooldown'])
        self.assertEqual(safe['stream_health'], data['stream_health'])
        self.assertEqual(safe['admin_token'], '[REDACTED]')
        self.assertEqual(safe['password'], '[REDACTED]')
        self.assertTrue(wc._valid_status_response(safe))
        with tempfile.TemporaryDirectory(prefix='mp-review-status-') as td:
            cfg = Path(td) / 'config.json'
            cfg.write_text(json.dumps({'supplies': [{'id': 'service_secret'}]}), encoding='utf-8')
            control = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO())
            control.config_path = str(cfg)
            lines = control._display_status(RECORD, {'totals': str(Path(td)/'totals'),
                                                      'log': str(Path(td)/'log')},
                                            (safe, 'fake-admin-secret'))
        output = '\n'.join(lines)
        self.assertIn('cooldown (剩余秒)', output)
        self.assertIn('service_secret', output)

    def test_numeric_admin_token_does_not_match_normal_zero_counts(self):
        data = {'supplies': [], 'routes': [], 'strategies': [], 'cooldown': {}}
        with tempfile.TemporaryDirectory(prefix='mp-review-status-') as td:
            cfg = Path(td) / 'config.json'
            cfg.write_text('{}', encoding='utf-8')
            control = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO())
            control.config_path = str(cfg)
            lines = control._display_status(RECORD, {'totals': str(Path(td)/'totals'),
                                                      'log': str(Path(td)/'log')}, (data, '0'))
        # 计数项已移除；用供给段标题作为「status 正常产出」的稳定锚点
        self.assertIn('supply health', '\n'.join(lines))
        self.assertIn('config 0 supplies', '\n'.join(lines))

    def test_short_admin_token_does_not_match_ordinary_status_word(self):
        control = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO())
        data = {'supplies': [], 'routes': [], 'strategies': [], 'cooldown': {}}
        with patch('core.cli._format_ops._format_status_from_json',
                   return_value=['active sessions (30min): 1 (1 ok)', '  session1 ok n=1']):
            lines = control._display_status(RECORD, PATHS, (data, 'ok'))
        self.assertIn('1 ok', '\n'.join(lines))

    @staticmethod
    def _session(sid='session1', tokens=None):
        from datetime import datetime
        agg = {'n': 1, 'fail': 0, 'fo': 0, 'last_ts': datetime.now(),
               'last_status': '200', 'last_route': 'r1', 'last_tier': 'sonnet',
               'last_supply': 's1', 'last_error': '', 'last_req_id': 'x',
               'builtin_only': False, 'chains': {('sonnet', 's1'): 1},
               'routes': {'r1': 1}, 'tokens': tokens or {}}
        return {'sessions': {sid: agg}, 'truncated': False, 'log_missing': False}

    def _display_real(self, data, token, sessions=None):
        from core.cli import _format_ops
        with tempfile.TemporaryDirectory(prefix='mp-visible-status-') as temp:
            root = Path(temp)
            config = root / 'config.json'; config.write_text('{}', encoding='utf-8')
            control = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO())
            control.config_path = str(config)
            empty = {'sessions': {}, 'truncated': False, 'log_missing': True}
            with patch.object(_format_ops, 'load_session_names', return_value={}), \
                 patch.object(_format_ops, 'load_active_sessions', return_value=sessions or empty):
                return control._display_status(RECORD, {'totals': str(root/'totals'),
                                                        'log': str(root/'log')}, (data, token))

    def test_short_admin_token_in_client_identity_is_rejected(self):
        data = {'supplies': [], 'routes': [], 'strategies': [], 'cooldown': {}}
        with self.assertRaises(wc.WindowsControlError) as caught:
            self._display_real(data, 'cc', self._session(tokens={'cc': 1}))
        self.assertEqual(caught.exception.code, 'status_sensitive_conflict')

    def test_numeric_admin_token_in_session_identity_is_rejected(self):
        data = {'supplies': [], 'routes': [], 'strategies': [], 'cooldown': {}}
        for sid, tokens in (('0', {}), ('session1', {'cc': 1, '0': 1})):
            with self.subTest(sid=sid, tokens=tokens), self.assertRaises(wc.WindowsControlError) as caught:
                self._display_real(data, '0', self._session(sid=sid, tokens=tokens))
            self.assertEqual(caught.exception.code, 'status_sensitive_conflict')

    def test_numeric_admin_token_equal_to_real_visible_identity_is_rejected(self):
        data = {'supplies': [{'id': '0'}], 'routes': [], 'strategies': [],
                'cooldown': {'0': {'remain': 3, 'reason': '429'}}}
        with self.assertRaises(wc.WindowsControlError) as caught:
            self._display_real(data, '0')
        self.assertEqual(caught.exception.code, 'status_sensitive_conflict')


class StatusDynamicConfigReferenceTests(unittest.TestCase):
    def run_status(self, *, actual_token, config_token, disk_admin='fake-new-admin',
                   pool=False, cooldown=True, degraded=False):
        from core.cli import _format_ops
        with tempfile.TemporaryDirectory(prefix='mp-ref-review-') as temp:
            root = Path(temp)
            config = root / 'config.json'
            old_strategy = {'client_token': 'old-client', 'route_id': 'route1'}
            route = {'id': 'route1', 'tiers': {'sonnet': ['s1']}}
            old_config = {'admin_token': actual_token, 'routes': [route], 'strategies': [old_strategy]}
            config.write_text(json.dumps(old_config), encoding='utf-8')
            # The authenticated service still returns the old in-memory
            # strategy, while its formatter reads the now-updated disk config.
            data = {'supplies': [{'id': 's1'}], 'routes': [route], 'strategies': [old_strategy],
                    'cooldown': {'s1': {'remain': 3, 'reason': '429'}} if cooldown else {}}
            new_strategy = ({'client_token': config_token, 'route_pool': [{'route_id': 'route1'}]}
                            if pool else {'client_token': config_token, 'route_id': 'route1'})
            disk_config = {'admin_token': disk_admin, 'routes': [route], 'strategies': [new_strategy]}
            config.write_text(json.dumps(disk_config), encoding='utf-8')
            paths = dict(PATHS, totals=str(root / 'totals'), log=str(root / 'access.log'))
            active = dict(RECORD, config=str(config), runtime_paths=paths)
            out, err = io.StringIO(), io.StringIO()
            control = wc.WindowsControl(ROOT, config_path=CONFIG, out=out, err=err)
            control.config_path = str(config)
            health = {'s1': {'requests': 10, 'fail': 4}} if degraded else {}
            sessions = {'sessions': {}, 'truncated': False, 'log_missing': True}
            with patch.object(wc.os, 'name', 'nt'), \
                 patch.object(control, '_configuration', return_value=(paths, RECORD['listen'])), \
                 patch.object(control, '_locator_hint', return_value=True), \
                 patch.object(control, '_active', return_value=active), \
                 patch.object(windows_process, 'recorded_process_state', return_value='running'), \
                 patch.object(control, '_request', return_value=(data, actual_token)), \
                 patch.object(_format_ops, 'load_supply_health', return_value=health), \
                 patch.object(_format_ops, 'load_session_names', return_value={}), \
                 patch.object(_format_ops, 'load_active_sessions', return_value=sessions):
                rc = control.status()
            # Default formatter has no Windows-only guard; Mac/menu behavior
            # stays unchanged when no token guard is requested.
            with patch.object(_format_ops, 'load_supply_health', return_value=health):
                baseline = _format_ops._format_status_from_json(data, str(config), paths['totals'])
            return rc, out.getvalue(), err.getvalue(), '\n'.join(baseline)

    def test_visible_disk_reference_equal_to_actual_token_fails_without_output(self):
        """引用标注只在 cooldown 段渲染 → 只有该路径需要 fail-closed。"""
        for token, pool, cooldown in (('0', False, True),
                                      ('cc', True, True),
                                      ('a,b', False, True)):
            with self.subTest(token=token, pool=pool):
                rc, out, err, baseline = self.run_status(actual_token=token, config_token=token,
                                                          pool=pool, cooldown=cooldown,
                                                          degraded=False)
                self.assertEqual(rc, 1)
                self.assertEqual(out, '')
                self.assertIn('status_sensitive_conflict', err)
                self.assertIn(f'route1({token}) sonnet', baseline)

    def test_degraded_section_no_longer_exposes_reference_token(self):
        """供给段不再显示引用关系 → degraded 场景不泄露 token，正常输出。"""
        rc, out, err, _ = self.run_status(actual_token='z', config_token='z',
                                           pool=False, cooldown=False, degraded=True)
        self.assertEqual(rc, 0, err)
        self.assertNotIn('route1(z) sonnet', out)

    def test_owned_same_socket_response_with_new_disk_reference_is_not_printed(self):
        import socket
        import threading
        from core import windows_process as wp
        from core.cli import _format_ops
        from tests.test_windows_process import FakeBackend, row4, table
        token = '0'
        old = {'supplies': [{'id': 's1'}], 'routes': [{'id': 'route1'}],
               'strategies': [{'client_token': 'old-client', 'route_id': 'route1'}],
               'cooldown': {'s1': {'remain': 3, 'reason': '429'}}}
        snapshot = Mock(exists=True, contents=json.dumps({'admin_token': token}).encode())
        received, failures = [], []
        with tempfile.TemporaryDirectory(prefix='mp-ref-socket-') as temp, \
             socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            root = Path(temp)
            config = root / 'config.json'
            config.write_text(json.dumps({'routes': [], 'strategies': []}), encoding='utf-8')
            listener.bind(('127.0.0.1', 0)); listener.listen(1); listener.settimeout(5)
            active = dict(RECORD, listen={'host': '127.0.0.1', 'port': listener.getsockname()[1]})
            def serve():
                try:
                    conn, _ = listener.accept()
                    with conn:
                        conn.settimeout(5); request = b''
                        while b'\r\n\r\n' not in request:
                            part = conn.recv(4096)
                            if not part: break
                            request += part
                        received.append(request)
                        body = json.dumps(old).encode()
                        conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode()
                                     + b'\r\nConnection: close\r\n\r\n' + body)
                except Exception as exc:
                    failures.append(exc)
            thread = threading.Thread(target=serve, daemon=True); thread.start()
            backend = FakeBackend(); backend.exe = active['executable']
            original_verify = wp.verify_socket_owner
            def verify(sock, pid, start, executable):
                local, peer = sock.getsockname(), sock.getpeername()
                backend.rows = wp.parse_owner_pid_table(socket.AF_INET, table(row4(
                    local=peer[0], lport=peer[1], remote=local[0], rport=local[1], pid=pid)))
                return original_verify(sock, pid, start, executable, backend=backend, timeout=0)
            control = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO(), err=io.StringIO())
            try:
                with patch.object(wp, 'verify_socket_owner', side_effect=verify), \
                     patch.object(control, '_verify_entry', side_effect=lambda _active, revalidate: revalidate()), \
                     patch('core.windows_secure_files.capture_target', return_value=contextlib.nullcontext(snapshot)):
                    result = control._request(active, PATHS, 'status')
                # Only disk changes; the already-authenticated response still
                # contains the older in-memory strategy.
                config.write_text(json.dumps({'admin_token': 'different-disk-admin',
                    'routes': [{'id': 'route1', 'tiers': {'sonnet': ['s1']}}],
                    'strategies': [{'client_token': token, 'route_id': 'route1'}]}), encoding='utf-8')
                control.config_path = str(config)
                with patch.object(_format_ops, 'load_session_names', return_value={}), \
                     patch.object(_format_ops, 'load_active_sessions',
                                  return_value={'sessions': {}, 'truncated': False, 'log_missing': True}):
                    with self.assertRaises(wc.WindowsControlError) as caught:
                        control._display_status(active, {'totals': str(root/'totals'),
                                                         'log': str(root/'log')}, result)
                self.assertEqual(caught.exception.code, 'status_sensitive_conflict')
            finally:
                thread.join(7)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            self.assertIn(b'X-Proxy-Admin-Token: 0\r\n', received[0])
            self.assertEqual(backend.opened, [active['pid']])
            self.assertEqual(backend.closed, [100])
            self.assertEqual(control.out.getvalue(), '')

    def test_only_actual_token_is_guarded_and_normal_reference_remains_visible(self):
        rc, out, err, baseline = self.run_status(actual_token='fake-auth-only', config_token='0',
                                                  disk_admin='0')
        self.assertEqual(rc, 0, err)
        self.assertIn('route1(0) sonnet', out)
        self.assertIn('cooldown (剩余秒)', out)
        self.assertIn('route1(0) sonnet', baseline)

    def test_unused_matching_reference_does_not_block_zero_counts(self):
        rc, out, err, _ = self.run_status(actual_token='0', config_token='0', cooldown=False)
        self.assertEqual(rc, 0, err)
        self.assertIn('supply health', out)
        self.assertNotIn('route1(0)', out)


class ControlLockOutputTests(unittest.TestCase):
    def setUp(self):
        self.out, self.err = io.StringIO(), io.StringIO()
        self.control = wc.WindowsControl(ROOT, config_path=CONFIG, out=self.out, err=self.err)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for item in (patch.object(wc.os, 'name', 'nt'),
                     patch.object(self.control, '_configuration', return_value=(PATHS, RECORD['listen'])),
                     patch.object(self.control, '_locator_hint', return_value=True),
                     patch.object(self.control, '_active', return_value=RECORD),
                     patch.object(windows_process, 'recorded_process_state', return_value='running'),
                     patch.object(self.control, '_request', return_value=None),
                     patch('core.discovery.prepare_windows_runtime_directories')):
            self.stack.enter_context(item)

    @contextlib.contextmanager
    def failed_release(self, _paths):
        yield None
        raise wc.WindowsControlError('lock_release_unknown')

    def test_release_failure_never_emits_success_for_on_reload_or_restart(self):
        for command in ('on', 'reload', 'restart'):
            with self.subTest(command=command):
                self.out.seek(0); self.out.truncate(); self.err.seek(0); self.err.truncate()
                def fake_off(*args, **kwargs):
                    print('Recorded model_proxy process exited; Windows forced stop may interrupt in-flight requests.',
                          file=self.control.out)
                    print('model_proxy stopped; verified free', file=self.control.out)
                def fake_on(*args, **kwargs):
                    print('model_proxy started and verified.', file=self.control.out)
                with patch.object(self.control, '_locked', side_effect=self.failed_release), \
                     patch.object(self.control, '_off_locked', side_effect=fake_off), \
                     patch.object(self.control, '_on_locked', side_effect=(fake_on if command == 'restart'
                                                                           else self.control._on_locked)):
                    self.assertEqual(getattr(self.control, command)(), 1)
                self.assertEqual(self.out.getvalue(), '')
                self.assertIn('lock_release_unknown', self.err.getvalue())
                if command == 'restart':
                    self.assertIn('process exited', self.err.getvalue())
                    self.assertNotIn('verified free', self.err.getvalue())

    def test_success_output_is_published_only_after_lock_exit(self):
        state = {'released': False}
        @contextlib.contextmanager
        def locked(_paths):
            yield None
            state['released'] = True
        class Output(io.StringIO):
            def write(self, text):
                if not state['released']:
                    raise AssertionError('success output before release')
                return super().write(text)
        out = Output()
        self.control.out = out
        with patch.object(self.control, '_locked', side_effect=locked):
            self.assertEqual(self.control.on(), 0, self.err.getvalue())
        self.assertIn('already running and verified', out.getvalue())


class HandleReleaseAfterStopTests(unittest.TestCase):
    def run_off(self, *, close_failure=False, stop_failure=False):
        from core.cli import windows_command_line as cmd
        from tests.test_windows_process_a_stop import StopBackend
        backend = StopBackend()
        backend.exe = RECORD['executable']
        backend.fail_close = close_failure
        if stop_failure:
            backend.terminate_and_wait = Mock(side_effect=windows_process.ProcessEvidenceError('stop_timeout'))
        real_open = windows_process.open_verified_process
        out, err = io.StringIO(), io.StringIO()
        control = wc.WindowsControl(ROOT, config_path=CONFIG, out=out, err=err)
        argv = [RECORD['executable'], '-B', RECORD['entry'], '--config', CONFIG,
                '--port', str(RECORD['listen']['port'])]
        def open_fake(pid, start, image, *, terminate=False):
            return real_open(pid, start, image, backend=backend, terminate=terminate)
        with patch.object(wc.os, 'name', 'nt'), \
             patch.object(control, '_locator_hint', return_value=True), \
             patch.object(control, '_active', return_value=RECORD), \
             patch.object(control, '_current_listen_for_off', return_value=RECORD['listen']), \
             patch.object(wc.os.path, 'lexists', return_value=True), \
             patch.object(control, '_locked', return_value=contextlib.nullcontext()), \
             patch.object(windows_process, 'recorded_process_state', return_value='running'), \
             patch.object(windows_process, 'open_verified_process', side_effect=open_fake), \
             patch.object(cmd.PowerShellWmiReader, 'read_command_line', return_value='fake argv'), \
             patch.object(cmd, 'parse_windows_argv', return_value=tuple(argv)), \
             patch.object(control, '_idle_readonly') as idle:
            rc = control.off()
        idle.assert_not_called()
        return rc, out.getvalue(), err.getvalue(), backend

    def test_successful_stop_then_handle_release_failure_reports_partial_fact(self):
        rc, out, err, backend = self.run_off(close_failure=True)
        self.assertEqual(rc, 1)
        self.assertEqual(out, '')
        self.assertEqual(backend.opened, [RECORD['pid']])
        self.assertEqual(backend.stops, [(100, 8.0)])
        self.assertEqual(backend.closed, [100])
        self.assertIn('process_release_unknown', err)
        self.assertIn('Recorded model_proxy process exited', err)
        self.assertIn('may interrupt in-flight requests', err)
        self.assertNotIn('verified free', err)

    def test_failed_stop_does_not_claim_process_exited(self):
        rc, out, err, backend = self.run_off(stop_failure=True)
        self.assertEqual(rc, 1)
        self.assertEqual(out, '')
        self.assertEqual(backend.closed, [100])
        self.assertIn('stop_timeout', err)
        self.assertNotIn('process exited', err)
        self.assertNotIn('verified free', err)


class EntryStopCompositionTests(unittest.TestCase):
    def test_wrong_observed_argv_never_terminates_held_target(self):
        from core.cli import windows_command_line as cmd
        from tests.test_windows_process_a_stop import StopBackend

        good = [RECORD['executable'], '-B', RECORD['entry'], '--config', CONFIG,
                '--port', str(RECORD['listen']['port'])]
        wrong_cases = {
            'entry': good[:2] + [ROOT + r'\core\cli\other.py'] + good[3:],
            'config': good[:4] + [ROOT + r'\config\other.json'] + good[5:],
            'port': good[:-1] + ['12346'],
        }
        for name, argv in wrong_cases.items():
            with self.subTest(case=name):
                backend = StopBackend()
                backend.exe = RECORD['executable']
                real_open = windows_process.open_verified_process
                control = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO(), err=io.StringIO())
                def open_fake(pid, start, image, *, terminate=False):
                    return real_open(pid, start, image, backend=backend, terminate=terminate)
                raw = subprocess.list2cmdline(argv)
                # On Wine/Windows exercise CommandLineToArgvW too; on macOS
                # only the OS argv decoder is a substitute, not either verifier.
                parser = (contextlib.nullcontext() if os.name == 'nt' else
                          patch.object(cmd, 'parse_windows_argv', return_value=tuple(argv)))
                with parser, \
                     patch.object(control, '_active', return_value=RECORD), \
                     patch.object(windows_process, 'recorded_process_state', return_value='running'), \
                     patch.object(windows_process, 'open_verified_process', side_effect=open_fake), \
                     patch.object(cmd.PowerShellWmiReader, 'read_command_line', return_value=raw) as reader, \
                     patch.object(control, '_idle_readonly') as idle:
                    with self.assertRaises(cmd.CommandLineObservationError) as caught:
                        control._off_locked(PATHS, RECORD['listen'])
                self.assertEqual(caught.exception.code, 'launch_mismatch')
                reader.assert_called_once_with(RECORD['pid'])
                idle.assert_not_called()
                self.assertEqual(backend.opened, [RECORD['pid']])
                self.assertEqual(backend.stops, [])
                self.assertEqual(backend.closed, [100])
                self.assertEqual(control.out.getvalue(), '')


class StartupDiagnosticsTests(unittest.TestCase):
    def test_owned_child_stderr_is_bounded_and_only_known_code_is_forwarded(self):
        script = ("import sys; sys.stderr.write('model_proxy: startup failed (config_path_unknown): "
                  "C:/private/FAKE_SECRET\\n'); sys.stderr.flush()")
        child = subprocess.Popen([sys.executable, '-B', '-c', script], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE)
        capture = wc._capture_startup_stderr(child.stderr)
        self.assertEqual(child.wait(timeout=5), 0)
        self.assertEqual(wc._startup_diagnostic(capture), 'config_path_unknown')
        self.assertNotIn('FAKE_SECRET', wc._startup_diagnostic(capture))

    def test_noisy_child_is_drained_without_unbounded_capture(self):
        script = "import sys; sys.stderr.write('x' * 100000); sys.stderr.flush()"
        child = subprocess.Popen([sys.executable, '-B', '-c', script], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE)
        capture = wc._capture_startup_stderr(child.stderr)
        self.assertEqual(child.wait(timeout=5), 0)
        self.assertIsNone(wc._startup_diagnostic(capture))
        self.assertLessEqual(len(capture[0]), 8192)

    def test_diagnostic_pipe_detach_failure_is_unknown(self):
        from core import server, discovery
        with patch.object(server.os, 'name', 'nt'), \
             patch.dict(server.os.environ, {'MODEL_PROXY_STARTUP_DIAGNOSTIC_PIPE': '1'}), \
             patch.object(server.os, 'dup2', side_effect=OSError('FAKE_SECRET')):
            with self.assertRaises(discovery.PreflightError) as caught:
                server._close_windows_startup_diagnostic_pipe()
        self.assertEqual(caught.exception.code, 'startup_diagnostics_unavailable')
        self.assertNotIn('FAKE_SECRET', str(caught.exception))

    def test_unrecognized_or_partial_child_errors_are_not_forwarded(self):
        for raw in (b'model_proxy: startup failed (FAKE_SECRET)\n',
                    b'model_proxy: startup failed (config_path_unknown',
                    b'prefix model_proxy: startup failed (config_path_unknown)\n'):
            capture = wc._capture_startup_stderr(io.BytesIO(raw))
            self.assertIsNone(wc._startup_diagnostic(capture))

    def test_child_startup_failure_reports_only_allowlisted_code(self):
        control = wc.WindowsControl(ROOT, config_path=CONFIG, startup_timeout=5,
                                    out=io.StringIO(), err=io.StringIO())
        spawned = []
        real_popen = subprocess.Popen
        def owned_popen(*_args, **_kwargs):
            child = real_popen([sys.executable, '-B', '-c',
                "import sys; sys.stderr.write('model_proxy: startup failed (config_path_unknown): C:/private/FAKE_SECRET\\n')"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            spawned.append(child)
            return child
        with patch.object(wc.os, 'name', 'nt'), \
             patch.object(control, '_configuration', return_value=(PATHS, RECORD['listen'])), \
             patch.object(control, '_locator_hint', return_value=False), \
             patch.object(control, '_active', return_value=None), \
             patch.object(control, '_reject_busy_without_locator'), \
             patch.object(control, '_probe_start_locks'), \
             patch.object(control, '_probe_port'), \
             patch.object(control, '_locked', return_value=contextlib.nullcontext()), \
             patch('core.discovery.prepare_windows_runtime_directories'), \
             patch.object(wc.subprocess, 'Popen', side_effect=owned_popen):
            self.assertEqual(control.on(), 1)
        self.assertEqual(len(spawned), 1)
        spawned[0].wait(timeout=5)
        self.assertIn('startup_failed; child reported config_path_unknown', control.err.getvalue())
        self.assertNotIn('FAKE_SECRET', control.err.getvalue())
        self.assertNotIn('C:/private', control.err.getvalue())
        self.assertEqual(control.out.getvalue(), '')

    def test_success_pipe_is_detached_without_changing_parent_stderr(self):
        # The child is ours.  Exercise the real dup2 path only in that child.
        script = ("import os,sys; from core import server; "
                  "sys.stderr.write('before\\n'); sys.stderr.flush(); "
                  "os.environ['MODEL_PROXY_STARTUP_DIAGNOSTIC_PIPE']='1'; "
                  "server.os.name='nt'; server._close_windows_startup_diagnostic_pipe(); "
                  "sys.stderr.write('after\\n'); sys.stderr.flush()")
        child = subprocess.Popen([sys.executable, '-B', '-c', script], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE)
        _, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, err)
        self.assertEqual(err.splitlines(), [b'before'])


if __name__ == '__main__':
    unittest.main()
