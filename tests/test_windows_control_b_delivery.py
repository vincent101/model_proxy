"""B-control regressions: owned loopback sockets and fake Windows evidence only."""
import contextlib
import io
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import windows_process as wp
from core.cli import windows_control as wc
from tests.test_windows_control_a_contract import ROOT, CONFIG, PATHS, RECORD
from tests.test_windows_process import FakeBackend, FakeSocket, row4, table


def fake_verified_endpoint(listen):
    family, packed, port = wc._control_endpoint(listen["host"], listen["port"])
    return SimpleNamespace(family=family, peer_ip=packed, peer_port=port)


class DeliveryControlTests(unittest.TestCase):
    def test_status_uses_verified_socket_and_real_formatter_without_secrets(self):
        token, appkey = 'fake-admin-cool', 'fake-appkey-ABCD'
        config = {'admin_token': token, 'supplies': [{'id': 'test', 'appkey': appkey}], 'strategies': [{'client_token': 'a', 'route_id': 'r1'}]}
        response = {'supplies': [{'id': 'test', 'appkey_tail4': 'ABCD'}],
                    'routes': [{'id': 'r1'}], 'strategies': [{'client_token': 'a', 'route_id': 'r1'}],
                    'cooldown': {'test': {'remain': 3, 'reason': '429'}}, 'admin_token': token}
        received, errors = [], []
        with tempfile.TemporaryDirectory(prefix='mp-status-') as temp, \
             socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            root = Path(temp)
            local_config = root / 'config.json'
            local_config.write_text(json.dumps(config), encoding='utf-8')
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            listener.settimeout(5)
            active = dict(RECORD, listen={'host': '127.0.0.1', 'port': listener.getsockname()[1]})
            def serve():
                try:
                    conn, _ = listener.accept()
                    with conn:
                        conn.settimeout(5)
                        data = b''
                        while b'\r\n\r\n' not in data:
                            data += conn.recv(4096)
                        received.append(data)
                        body = json.dumps(response).encode()
                        conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode()
                                     + b'\r\nConnection: close\r\n\r\n' + body)
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            backend = FakeBackend()
            backend.exe = active['executable']
            original = wp.verify_socket_owner
            def verify(sock, pid, start, executable):
                local, peer = sock.getsockname(), sock.getpeername()
                backend.rows = wp.parse_owner_pid_table(socket.AF_INET, table(row4(
                    local=peer[0], lport=peer[1], remote=local[0], rport=local[1], pid=pid)))
                return original(sock, pid, start, executable, backend=backend, timeout=0)
            snapshot = Mock(exists=True, contents=json.dumps(config).encode())
            out = io.StringIO()
            control = wc.WindowsControl(ROOT, config_path=CONFIG, out=out)
            try:
                with patch.object(wp, 'verify_socket_owner', side_effect=verify), \
                     patch.object(control, '_verify_entry', side_effect=lambda active, revalidate: revalidate()), \
                     patch('core.windows_secure_files.capture_target', return_value=contextlib.nullcontext(snapshot)):
                    result = control._request(active, PATHS, 'status')
                control.config_path = str(local_config)
                lines = control._display_status(active, {'totals': str(root / 'totals'),
                                                         'log': str(root / 'log')}, result)
            finally:
                thread.join(7)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertIn(('X-Proxy-Admin-Token: ' + token).encode(), received[0])
            output = '\n'.join(lines)
            self.assertIn('cooldown 1/1', output)
            self.assertIn('1 strategies', output)
            self.assertEqual(result[0]['strategies'][0]['client_token'], 'a')
            self.assertEqual(result[0]['supplies'][0]['appkey_tail4'], 'ABCD')
            self.assertEqual(result[0]['admin_token'], '[REDACTED]')
            for secret in (token, appkey):
                self.assertNotIn(secret, output)

    def test_local_literal_selection_and_remote_tuple_fails_closed(self):
        self.assertEqual(wc._control_endpoint('192.0.2.10', 12345)[0], socket.AF_INET)
        for host in ('example.invalid', 'fe80::1%3', '::ffff:127.0.0.1', '224.0.0.1'):
            with self.subTest(host=host), self.assertRaises(wc.WindowsControlError):
                wc._control_endpoint(host, 12345)
        sock = FakeSocket()
        sock.local = ('192.0.2.10', 50123)
        sock.peer = ('192.0.2.10', 12345)
        with patch.object(wp.socket, 'socket', FakeSocket):
            self.assertEqual(wp.socket_endpoint(sock).peer_port, 12345)
            sock.peer = ('192.0.2.11', 12345)
            with self.assertRaises(wp.ProcessEvidenceError) as caught:
                wp.socket_endpoint(sock)
        self.assertEqual(caught.exception.code, 'non_loopback_unsupported')

    def test_field_aware_redaction_does_not_change_keys_or_identity(self):
        value = {'cooldown': {'cool-supply': {'remain': 3}},
                 'strategies': [{'client_token': 'a', 'route_id': 'cool-route'}],
                 'admin_token': 'fake-cool', 'password': 'private',
                 'supplies': [{'appkey_tail4': 'cool'}]}
        result = wc._sanitize_status(value)
        self.assertEqual(result['cooldown'], value['cooldown'])
        self.assertEqual(result['strategies'], value['strategies'])
        self.assertEqual(result['supplies'], value['supplies'])
        self.assertEqual(result['admin_token'], '[REDACTED]')
        self.assertEqual(result['password'], '[REDACTED]')

    def test_short_admin_token_does_not_replace_substrings(self):
        value = {'supplies': [{'id': 'alpha', 'protocol': 'anthropic'}],
                 'routes': [{'id': 'route-a'}],
                 'strategies': [{'client_token': 'a', 'route_id': 'route-a'}],
                 'cooldown': {'alpha': {'remain': 3, 'reason': 'network'}}}
        self.assertEqual(wc._sanitize_status(value), value)

    def test_admin_credential_collision_in_visible_identity_fails_closed(self):
        from core.cli import _format_ops
        control = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO())
        with patch.object(_format_ops, '_format_status_from_json', return_value=['route fake-admin-secret']):
            with self.assertRaises(wc.WindowsControlError) as caught:
                control._display_status(RECORD, PATHS, ({}, 'fake-admin-secret'))
        self.assertEqual(caught.exception.code, 'status_sensitive_conflict')
        self.assertEqual(control.out.getvalue(), '')

    def test_common_header_matches_previous_mac_calculation(self):
        from core.cli import _format_ops
        for elapsed in (0, 125, 4000, 90000, -1):
            now, start = 1_700_000_000, 1_700_000_000 - elapsed
            expected = 'service running on port 12345  pid 99'
            if elapsed >= 0:
                days, secs = divmod(elapsed, 86400)
                hours, secs = divmod(secs, 3600)
                minutes, secs = divmod(secs, 60)
                uptime = (f'{days}-{hours:02d}:{minutes:02d}:{secs:02d}' if days else
                          f'{hours:02d}:{minutes:02d}:{secs:02d}' if hours else
                          f'{minutes:02d}:{secs:02d}')
                expected += f"  up {uptime}  (started {_format_ops.time.strftime('%b %d %H:%M', _format_ops.time.localtime(start))})"
            with patch.object(_format_ops.time, 'time', return_value=now):
                self.assertEqual(_format_ops._format_status_header(12345, 99, start), expected)

    def test_verified_filetime_displays_same_header_and_uptime(self):
        from core.cli import _format_ops
        started, now = 1_700_000_000, 1_700_000_125
        ticks = (started + 11_644_473_600) * 10_000_000
        active = dict(RECORD, process_start={'kind': 'windows_filetime', 'ticks_100ns': ticks})
        out = io.StringIO()
        control = wc.WindowsControl(ROOT, config_path=CONFIG, out=out)
        with patch.object(_format_ops.time, 'time', return_value=now), \
             patch.object(_format_ops, '_format_status_from_json', return_value=['fake detail']):
            lines = control._display_status(active, PATHS, ({}, 'fake-admin-token'))
        output = '\n'.join(lines)
        self.assertIn('up 02:05', output)
        self.assertIn('started ', output)
        self.assertIn('fake detail', output)

    def test_cooldown_counts_and_client_labels_preserve_mac_semantics(self):
        from datetime import datetime
        from core.cli import _format_ops
        agg = {'n': 5, 'fail': 0, 'fo': 0, 'last_ts': datetime.now(),
               'last_status': '200', 'last_route': 'r1', 'last_tier': 'opus',
               'last_supply': 's1', 'last_error': '', 'last_req_id': 'x',
               'builtin_only': False, 'chains': {('opus', 's1'): 5},
               'routes': {'r1': 5}, 'tokens': {'cc': 3, 'codex': 2}}
        sessions = {'sessions': {'abc12345-xxxx': agg}, 'truncated': False, 'log_missing': False}
        config = {'admin_token': 'fake-admin-cool', 'strategies': [{'client_token': 'a', 'route_id': 'r1'}]}
        data = {'supplies': [{'id': 's1', 'appkey_tail4': 'cool'}], 'routes': [{'id': 'r1'}],
                'strategies': [{'client_token': 'a', 'route_id': 'r1'}],
                'cooldown': {'s1': {'remain': 3, 'reason': '429'}}}
        safe = wc._sanitize_status(data)
        with tempfile.TemporaryDirectory(prefix='mp-status-') as temp:
            root = Path(temp)
            cfg = root / 'config.json'
            cfg.write_text(json.dumps(config), encoding='utf-8')
            control = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO())
            control.config_path = str(cfg)
            with patch.object(_format_ops, 'load_active_sessions', return_value=sessions), \
                 patch.object(_format_ops, 'load_session_names', return_value={}):
                lines = control._display_status(RECORD, {'totals': str(root / 'totals'),
                                                         'log': str(root / 'log')},
                                                (safe, config['admin_token']))
        output = '\n'.join(lines)
        self.assertIn('cooldown 1/1', output)
        self.assertIn('1 strategies', output)
        self.assertIn('[cc,codex]', output)
        self.assertIn('port 12345', output)
        self.assertEqual(safe['strategies'][0]['client_token'], 'a')
        self.assertEqual(safe['supplies'][0]['appkey_tail4'], 'cool')

    def test_http_200_requires_actual_status_contract(self):
        control = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO(), err=io.StringIO())
        sock, peer = Mock(), Mock()
        sock.__enter__ = Mock(return_value=sock); sock.__exit__ = Mock(return_value=False)
        peer.__enter__ = Mock(return_value=peer); peer.__exit__ = Mock(return_value=False)
        peer.endpoint = fake_verified_endpoint(RECORD['listen'])
        snapshot = Mock(exists=True, contents=b'{"admin_token":"fake-admin"}')
        response = Mock(status=200, length=0)
        with patch.object(wc.socket, 'socket', return_value=sock), \
             patch.object(wp, 'verify_socket_owner', return_value=peer), \
             patch.object(control, '_verify_entry'), \
             patch('core.windows_secure_files.capture_target', return_value=contextlib.nullcontext(snapshot)), \
             patch.object(wc.http.client, 'HTTPResponse', return_value=response):
            for value in ({}, {'supplies': [], 'routes': [], 'strategies': [], 'cooldown': []},
                          {'supplies': [], 'routes': [], 'strategies': [1], 'cooldown': {}},
                          {'supplies': [], 'routes': [], 'strategies': [], 'cooldown': {'x': {}}}):
                response.read.return_value = json.dumps(value).encode()
                with self.subTest(value=value), self.assertRaises(wc.WindowsControlError) as caught:
                    control._request(RECORD, PATHS, 'status')
                self.assertEqual(caught.exception.code, 'response_unknown')
            response.read.return_value = b'{"supplies":[],"routes":[],"strategies":[],"cooldown":{}}'
            data, token = control._request(RECORD, PATHS, 'status')
            self.assertEqual(data['cooldown'], {})
            self.assertEqual(token, 'fake-admin')
        self.assertEqual(control.out.getvalue(), '')

    def test_empty_http_200_status_lifecycle_is_unknown(self):
        control = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO(), err=io.StringIO())
        sock, peer = Mock(), Mock()
        sock.__enter__ = Mock(return_value=sock); sock.__exit__ = Mock(return_value=False)
        peer.__enter__ = Mock(return_value=peer); peer.__exit__ = Mock(return_value=False)
        peer.endpoint = fake_verified_endpoint(RECORD['listen'])
        snapshot = Mock(exists=True, contents=b'{"admin_token":"fake-admin"}')
        response = Mock(status=200, length=0)
        response.read.return_value = b'{}'
        with patch.object(wc.os, 'name', 'nt'), \
             patch.object(control, '_configuration', return_value=(PATHS, RECORD['listen'])), \
             patch.object(control, '_locator_hint', return_value=True), \
             patch.object(control, '_active', return_value=RECORD), \
             patch.object(wp, 'recorded_process_state', return_value='running'), \
             patch.object(wc.socket, 'socket', return_value=sock), \
             patch.object(wp, 'verify_socket_owner', return_value=peer), \
             patch.object(control, '_verify_entry'), \
             patch('core.windows_secure_files.capture_target', return_value=contextlib.nullcontext(snapshot)), \
             patch.object(wc.http.client, 'HTTPResponse', return_value=response):
            self.assertEqual(control.status(), 1)
        self.assertEqual(control.out.getvalue(), '')
        self.assertIn('response_unknown', control.err.getvalue())

    def test_final_status_line_cannot_echo_admin_credential(self):
        control = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO(), err=io.StringIO())
        data = {'supplies': [], 'routes': [], 'strategies': [], 'cooldown': {}}
        with patch.object(wc.os, 'name', 'nt'), \
             patch.object(control, '_configuration', return_value=(PATHS, RECORD['listen'])), \
             patch.object(control, '_locator_hint', return_value=True), \
             patch.object(control, '_active', return_value=RECORD), \
             patch.object(wp, 'recorded_process_state', return_value='running'), \
             patch.object(control, '_request', return_value=(data, 'verified')), \
             patch.object(control, '_display_status', return_value=['service status']):
            self.assertEqual(control.status(), 1)
        self.assertEqual(control.out.getvalue(), '')
        self.assertIn('status_sensitive_conflict', control.err.getvalue())

    def test_lan_literal_targets_exact_address_and_checked_socket(self):
        active = dict(RECORD, listen={'host': '192.0.2.10', 'port': 12345})
        control = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO(), err=io.StringIO())
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        peer = Mock()
        peer.__enter__ = Mock(return_value=peer)
        peer.__exit__ = Mock(return_value=False)
        peer.endpoint = fake_verified_endpoint(active['listen'])
        snapshot = Mock(exists=True, contents=b'{"admin_token":"fake-lan-token"}')
        response = Mock(status=200, length=0)
        response.read.return_value = b'{"supplies":[],"routes":[],"strategies":[],"cooldown":{}}'
        with patch.object(wc.socket, 'socket', return_value=sock), \
             patch.object(wp, 'verify_socket_owner', return_value=peer) as verify, \
             patch.object(control, '_verify_entry'), \
             patch('core.windows_secure_files.capture_target', return_value=contextlib.nullcontext(snapshot)), \
             patch.object(wc.http.client, 'HTTPResponse', return_value=response):
            control._request(active, PATHS, 'status')
        sock.connect.assert_called_once_with(('192.0.2.10', 12345))
        self.assertIs(verify.call_args.args[0], sock)
        self.assertIn(b'Host: 127.0.0.1:12345', sock.sendall.call_args.args[0])

    def test_lan_probe_binds_configured_literal_not_loopback(self):
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        with patch.object(wc.socket, 'socket', return_value=sock), \
             patch.object(wc.socket, 'SO_EXCLUSIVEADDRUSE', 123, create=True):
            wc.WindowsControl._probe_port({'host': '192.0.2.10', 'port': 12345})
        sock.bind.assert_called_once_with(('192.0.2.10', 12345))

    def test_reload_requires_ok_and_reports_partial_flags(self):
        control = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO(), err=io.StringIO())
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        peer = Mock()
        peer.__enter__ = Mock(return_value=peer)
        peer.__exit__ = Mock(return_value=False)
        peer.endpoint = fake_verified_endpoint(RECORD['listen'])
        snapshot = Mock(exists=True, contents=b'{"admin_token":"fake-only"}')
        response = Mock(status=422, length=0)
        response.read.return_value = b'{"ok":false,"loaded":false,"cooldown_cleared":true,"error":"fake-only"}'
        with patch.object(wc.socket, 'socket', return_value=sock), \
             patch.object(wp, 'verify_socket_owner', return_value=peer), \
             patch.object(control, '_verify_entry'), \
             patch('core.windows_secure_files.capture_target', return_value=contextlib.nullcontext(snapshot)), \
             patch.object(wc.http.client, 'HTTPResponse', return_value=response):
            with self.assertRaises(wc.WindowsControlError) as caught:
                control._request(RECORD, PATHS, 'reload')
            self.assertEqual(caught.exception.code, 'reload_failed')
            self.assertIn('loaded=no, cooldown_cleared=yes', control.err.getvalue())
            self.assertNotIn('fake-only', control.err.getvalue())
            response.status = 200
            response.read.return_value = b'{"ok":true,"loaded":true,"cooldown_cleared":true}'
            self.assertIsNone(control._request(RECORD, PATHS, 'reload'))

    def test_failed_status_never_displays_response(self):
        control = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO(), err=io.StringIO())
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        peer = Mock()
        peer.__enter__ = Mock(return_value=peer)
        peer.__exit__ = Mock(return_value=False)
        peer.endpoint = fake_verified_endpoint(RECORD['listen'])
        snapshot = Mock(exists=True, contents=b'{"admin_token":"fake-secret"}')
        response = Mock(status=403, length=0)
        response.read.return_value = b'{"reason":"fake-secret"}'
        with patch.object(wc.socket, 'socket', return_value=sock), \
             patch.object(wp, 'verify_socket_owner', return_value=peer), \
             patch.object(control, '_verify_entry'), \
             patch('core.windows_secure_files.capture_target', return_value=contextlib.nullcontext(snapshot)), \
             patch.object(wc.http.client, 'HTTPResponse', return_value=response):
            with self.assertRaises(wc.WindowsControlError):
                control._request(RECORD, PATHS, 'status')
        self.assertEqual(control.out.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
