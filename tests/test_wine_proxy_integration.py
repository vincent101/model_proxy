"""Test-only, loopback real-handler experiment, NOT a Windows startup test.

Run standalone with an isolated interpreter, e.g. `python -I -S -B
 tests/test_wine_proxy_integration.py -v` (intended for Whisky Python too).
It creates one temporary fake HOME/project, config, six runtime paths and two
OS-assigned 127.0.0.1 listeners; no product main/CLI, subprocess or stop command.
The test wires real stores/handler itself, so startup locks, ACLs, discovery,
process identity, installation records, Windows gate and service lifecycle are
NOT exercised. File snapshot checks are replaced for the fake files only; on
macOS the Mac-specific config path is also disabled for this fake process.
Session-name and title lookup roots are redirected to the fake HOME.
Native ProxyHTTPServer binding is tried without replacement. Any failure is
reported, not silently changed into a pass. URL opening is restricted to the
owned mock (no proxy or redirects); this is not an OS security sandbox and
assumes cooperative local users/source code. Never use real credentials/config.
"""
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
CLIENT = 'fake-client-only'
UPSTREAM = 'fake-upstream-only'
ADMIN = 'fake-admin-only'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('redirects forbidden in test')


class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = 8
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append((self.path, dict(self.headers), body))
        if (self.path != '/v1/messages' or self.headers.get('x-api-key') != UPSTREAM
                or self.headers.get('Authorization') != 'Bearer ' + UPSTREAM):
            self.send_error(400)
            return
        if body.get('stream'):
            events = [
                ('message_start', {'type': 'message_start', 'message': {'id': 'msg_fake',
                    'type': 'message', 'role': 'assistant', 'model': body['model'],
                    'content': [], 'stop_reason': None, 'usage': {'input_tokens': 2, 'output_tokens': 0}}}),
                ('content_block_start', {'type': 'content_block_start', 'index': 0,
                    'content_block': {'type': 'text', 'text': ''}}),
                ('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                    'delta': {'type': 'text_delta', 'text': 'fake-reply'}}),
                ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn',
                    'stop_sequence': None}, 'usage': {'output_tokens': 2}}),
                ('message_stop', {'type': 'message_stop'})]
            data = ''.join('event: ' + e + '\ndata: ' + json.dumps(d) + '\n\n' for e, d in events).encode()
            content_type = 'text/event-stream'
        else:
            data = json.dumps({'id': 'msg_fake', 'type': 'message', 'role': 'assistant',
                'model': body['model'], 'content': [{'type': 'text', 'text': 'fake-reply'}],
                'stop_reason': 'end_turn', 'stop_sequence': None,
                'usage': {'input_tokens': 2, 'output_tokens': 2}}).encode()
            content_type = 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()


@unittest.skipUnless(__name__ == '__main__', 'run standalone to isolate environment and module globals')
class WineProxyIntegration(unittest.TestCase):
    def setUp(self):
        if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
            self.fail('run standalone with -I -S -B')
        self.tmp = tempfile.TemporaryDirectory(prefix='mp-wine-test-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        home = root / 'home'
        project = root / 'project'
        home.mkdir(); project.mkdir(); (project / 'config').mkdir()
        old_cwd = os.getcwd()
        os.chdir(project)
        self.addCleanup(os.chdir, old_cwd)
        runtime = project / 'runtime'; runtime.mkdir()
        self.config = project / 'config' / 'fake.json'
        self.paths = {k: runtime / k for k in ('log', 'totals', 'lock', 'pid', 'ensure_log', 'start_lock')}
        pathsfile = project / 'config' / 'runtime_paths.json'
        pathsfile.write_text(json.dumps({k: str(p) for k, p in self.paths.items()}), encoding='utf-8')
        env = {'HOME': str(home), 'USERPROFILE': str(home), 'APPDATA': str(home),
               'LOCALAPPDATA': str(home), 'MODEL_PROXY_CONFIG': str(self.config),
               'MODEL_PROXY_PORT': '', 'NO_PROXY': '*', 'no_proxy': '*'}
        for key in list(os.environ):
            if key.lower().endswith('_proxy') and key.lower() != 'no_proxy':
                env[key] = ''
        self.envpatch = patch.dict(os.environ, env)
        self.envpatch.start(); self.addCleanup(self.envpatch.stop)
        if 'core.server' in sys.modules:
            self.fail('core.server already imported: standalone isolation required')
        sys.path.insert(0, str(REPO))
        self.addCleanup(sys.path.remove, str(REPO))
        from core import server
        from core.config_snapshot import ConfigSnapshot, ConfigSnapshotError, FileMarker
        self.core = server
        # Strictly fake config/ledger only. This does NOT establish Windows file identity or ACLs.
        allowed = {self.config, self.paths['totals']}
        def read_fake(path, **kwargs):
            p = Path(path)
            if p not in allowed or p.is_symlink():
                raise AssertionError('unexpected snapshot path')
            try:
                with p.open('rb') as f:
                    stat = os.fstat(f.fileno())
                    data = json.load(f)
                marker = FileMarker(stat.st_dev, stat.st_ino, stat.st_mtime_ns,
                                    stat.st_ctime_ns, stat.st_size)
                return ConfigSnapshot(data, marker)
            except (OSError, ValueError):
                raise ConfigSnapshotError('config_read_error') from None
        patches = [patch.object(server, '_mac_business_enabled', return_value=False),
                   patch.object(server, '_read_business_config', side_effect=read_fake),
                   patch.object(server, '_business_marker', side_effect=lambda p: read_fake(p).marker),
                   patch.object(server, 'read_stable_config', side_effect=read_fake),
                   patch.object(server.session_identity, 'DEFAULT_SESSIONS_DIR', home / 'sessions'),
                   patch.object(server.claudian_titles, 'DEFAULT_CLAUDIAN_SESSIONS_DIR', home / 'titles')]
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        resolved = server.resolve_runtime_paths(pathsfile)
        self.assertEqual(resolved, self.paths)
        server._runtime_paths = resolved
        self.addCleanup(setattr, server, '_runtime_paths', None)
        self.mock = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.addCleanup(self.mock.server_close)
        self.mock.daemon_threads = False  # server_close joins only our request threads
        self.mock.calls = []
        self.start_server(self.mock)
        mockport = self.mock.server_address[1]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        def guarded_open(req, *args, **kwargs):
            parts = urllib.parse.urlsplit(req.full_url)
            if (parts.scheme != 'http' or parts.hostname != '127.0.0.1'
                    or parts.port != mockport or parts.username is not None):
                raise AssertionError('outbound destination is not the owned local mock')
            return opener.open(req, *args, **kwargs)
        self.guarded_open = guarded_open
        urlpatch = patch.object(server.urllib.request, 'urlopen', side_effect=guarded_open)
        urlpatch.start(); self.addCleanup(urlpatch.stop)
        self.cfg = {'admin_token': ADMIN, 'upstream_timeout_seconds': 5,
                    'supplies': [{'id': 'fake', 'url': 'http://127.0.0.1:%d/v1/messages' % mockport,
                                  'protocol': 'anthropic', 'appkey': UPSTREAM, 'target_model': 'fake-model'}],
                    'routes': [{'id': 'fake-route', 'tiers': {'sonnet': ['fake']}, 'failover': 'off'}],
                    'strategies': [{'client_token': CLIENT, 'route_id': 'fake-route'}]}
        self.save_config()
        self.proxy = server.ProxyHTTPServer(('127.0.0.1', 0), server.ModelProxyHandler)
        self.addCleanup(self.proxy.server_close)
        self.proxy.daemon_threads = False  # no request thread survives cleanup
        self.proxy.config_store = server.ConfigStore(self.config)
        self.proxy.cooldown_store = server.CooldownStore()
        self.proxy.stream_health_store = server.StreamHealthStore()
        self.proxy.pref_store = server.SyntaxPreferenceStore()
        self.proxy.thinking_strip_store = server.ThinkingStripStore()
        self.proxy.tool_strict_store = server.ToolStrictStore()
        self.proxy.count_tokens_store = server.CountTokensUnsupportedStore()
        self.proxy.sidecar_store = server.SessionOverridesSidecar(project / 'config' / 'session_overrides.json')
        self.proxy.host_validator = None
        server.usage_totals = server.UsageTotalsStore(self.paths['totals'])
        self.addCleanup(setattr, server, 'usage_totals', None)
        self.start_server(self.proxy)
        print('test listeners: mock=%s proxy=%s; real ProxyHTTPServer binding' %
              (self.mock.server_address, self.proxy.server_address), flush=True)

    def start_server(self, service):
        thread = threading.Thread(target=service.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        def stop():
            service.shutdown()
            thread.join(5)
            if thread.is_alive():
                raise RuntimeError('owned service thread did not stop')
        self.addCleanup(stop)

    def save_config(self):
        self.config.write_text(json.dumps(self.cfg), encoding='utf-8')

    def request(self, path, body, headers):
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_address[1], timeout=8)
        try:
            conn.request('POST', path, json.dumps(body).encode(), headers)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def check_control_connections(self):
        port = self.proxy.server_address[1]
        good_host = '127.0.0.1:%d' % port
        admin = 'X-Proxy-Admin-Token: %s\r\n' % ADMIN
        next_request = ('GET /model_proxy/status HTTP/1.1\r\nHost: %s\r\n%s\r\n'
                        % (good_host, admin)).encode('ascii')

        def exchange(method, path, headers, body, expected, cooldown_cleared=False,
                     reconnect_status=200):
            self.proxy.cooldown_store.clear_all()
            self.proxy.cooldown_store.cooldown('fake', 60, 'test')
            request = ('%s %s HTTP/1.1\r\n%s\r\n' % (method, path, headers)).encode('ascii')
            with socket.create_connection(('127.0.0.1', port), timeout=4) as sock:
                sock.settimeout(4)
                sock.sendall(request + body + next_request)
                with sock.makefile('rb') as response:
                    status = response.readline()
                    self.assertTrue(status.startswith(('HTTP/1.1 %d ' % expected).encode()), status)
                    hdr = {}
                    while True:
                        line = response.readline()
                        if line == b'\r\n':
                            break
                        self.assertTrue(line, 'incomplete response headers')
                        key, value = line.decode('iso-8859-1').split(':', 1)
                        hdr[key.lower()] = value.strip()
                    response.read(int(hdr['content-length']))
                    if hdr.get('connection', '').lower() != 'close':
                        # Evidence for the original defect: the pipelined request can
                        # be parsed with the unconsumed body prepended to its method.
                        extra = response.readline()
                        self.fail('missing Connection: close; next response: %r' % extra)
                    try:
                        extra = response.read(1)
                    except ConnectionResetError:
                        extra = b''  # an unread pipelined request may cause a reset
                    self.assertEqual(extra, b'', 'unexpected response on closed connection')
            self.assertEqual('fake' not in self.proxy.cooldown_store.snapshot(), cooldown_cleared)
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=4)
            try:
                conn.request('GET', '/model_proxy/status', headers={'X-Proxy-Admin-Token': ADMIN})
                resp = conn.getresponse()
                self.assertEqual(resp.status, reconnect_status, resp.read())
                resp.read()
            finally:
                conn.close()

        # Host rejection occurs before both control dispatch and body consumption.
        self.proxy.host_validator = self.core.HostHeaderValidator(port, [])
        rejected = 'Host: evil.invalid:%d\r\nContent-Length: 2\r\n%s' % (port, admin)
        exchange('POST', '/model_proxy/reload', rejected, b'{}', 403)
        for method in ('GET', 'PUT', 'DELETE', 'PATCH'):
            exchange(method, '/v1/messages', rejected, b'{}', 403)
        self.proxy.host_validator = None

        host = 'Host: %s\r\n' % good_host
        frames = [
            ('Content-Length: 2\r\n', b'{}'),
            ('Content-Length: 999999999999999999999999999999999\r\n', b''),
            ('Content-Length: nope\r\n', b'{}'),
            ('Content-Length: -1\r\n', b'{}'),
            ('Content-Length: +0\r\n', b'{}'),
            ('Content-Length: \r\n', b'{}'),
            ('Content-Length: 0\r\nContent-Length: 0\r\n', b'{}'),
            ('Content-Length: 0\r\nContent-Length: 2\r\n', b'{}'),
            ('Content-Length: 0, 0\r\n', b'{}'),
            ('Content-Length: 0\r\n 1\r\n', b'{}'),
            ('Transfer-Encoding: chunked\r\n', b'2\r\n{}\r\n0\r\n\r\n'),
            ('Transfer-Encoding: identity\r\nContent-Length: 0\r\n', b'{}'),
            ('Transfer-Encoding: \r\n', b'{}'),
        ]
        for framing, body in frames:
            for method, path, token, status in (
                ('POST', '/model_proxy/reload', 'wrong', 401),
                ('POST', '/model_proxy/reload', ADMIN, 200),
                ('POST', '/model_proxy/no-such-endpoint', ADMIN, 404),
                ('GET', '/model_proxy/status', ADMIN, 200),
            ):
                with self.subTest(framing=framing, method=method, status=status):
                    headers = host + framing + 'X-Proxy-Admin-Token: %s\r\n' % token
                    exchange(method, path, headers, body, status,
                             cooldown_cleared=(method == 'POST' and status == 200))

        # Absent and unambiguous zero length retain the same live connection,
        # including errors and Host rejection, not merely successful status.
        self.proxy.host_validator = self.core.HostHeaderValidator(port, [])
        for framing in (None, '0', '00'):
            for method, path, token, host_value, expected in (
                ('GET', '/model_proxy/status', ADMIN, good_host, 200),
                ('POST', '/model_proxy/reload', 'wrong', good_host, 401),
                ('POST', '/model_proxy/reload', ADMIN, good_host, 200),
                ('POST', '/model_proxy/missing', ADMIN, good_host, 404),
                ('POST', '/v1/messages', ADMIN, 'evil.invalid:%d' % port, 403),
            ):
                with self.subTest(zero=framing, status=expected):
                    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=4)
                    try:
                        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
                        conn.putheader('Host', host_value)
                        conn.putheader('X-Proxy-Admin-Token', token)
                        if framing is not None:
                            conn.putheader('Content-Length', framing)
                        conn.endheaders()
                        first_sock = conn.sock
                        resp = conn.getresponse()
                        self.assertEqual(resp.status, expected)
                        self.assertNotEqual((resp.getheader('Connection') or '').lower(), 'close')
                        resp.read()
                        conn.request('GET', '/model_proxy/status',
                                     headers={'X-Proxy-Admin-Token': ADMIN})
                        self.assertIs(conn.sock, first_sock)
                        resp = conn.getresponse()
                        self.assertEqual(resp.status, 200)
                        resp.read()
                    finally:
                        conn.close()
        self.proxy.host_validator = None

        # No admin configured is still 503, and does not enter the reload handler.
        self.cfg['admin_token'] = ''
        self.save_config()
        self.assertTrue(self.proxy.config_store.reload())
        exchange('POST', '/model_proxy/reload',
                 host + 'Content-Length: 2\r\n' + admin, b'{}', 503,
                 reconnect_status=503)
        self.cfg['admin_token'] = ADMIN
        self.save_config()
        self.assertTrue(self.proxy.config_store.reload())
        self.proxy.cooldown_store.clear_all()

        # A zero-body control request must not disturb model forwarding on the
        # very same connection.
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=4)
        try:
            conn.request('GET', '/model_proxy/status', headers={'X-Proxy-Admin-Token': ADMIN})
            first_sock = conn.sock
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            resp.read()
            body = json.dumps({'model': 'claude-sonnet', 'max_tokens': 16,
                               'messages': [{'role': 'user', 'content': 'hello'}]}).encode()
            conn.request('POST', '/v1/messages', body,
                         {'Content-Type': 'application/json', 'x-api-key': CLIENT,
                          'anthropic-version': '2023-06-01'})
            self.assertIs(conn.sock, first_sock)
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read())['content'][0]['text'], 'fake-reply')
            self.assertEqual(self.mock.calls[-1][2]['model'], 'fake-model-reloaded')
        finally:
            conn.close()

    def check_body_control_close_reconnects_same_httpconnection(self):
        port = self.proxy.server_address[1]
        store = self.proxy.config_store
        self.assertEqual(store.get_supply_map()['fake']['target_model'], 'fake-model-reloaded')
        previous_calls = len(self.mock.calls)
        self.cfg['supplies'][0]['target_model'] = 'fake-model-after-reconnect'
        self.save_config()
        self.proxy.cooldown_store.cooldown('fake', 60, 'test')
        self.assertIn('fake', self.proxy.cooldown_store.snapshot())
        self.assertEqual(store.get_supply_map()['fake']['target_model'], 'fake-model-reloaded')

        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=8)
        try:
            # A real nonempty body triggers close; the same client object must
            # create a fresh transport on its next request without intervention.
            conn.request('POST', '/model_proxy/reload', b'{}',
                         {'Content-Type': 'application/json', 'X-Proxy-Admin-Token': ADMIN})
            first_sock = conn.sock
            self.assertIsNotNone(first_sock)
            resp = conn.getresponse()
            payload = resp.read()
            self.assertEqual(resp.status, 200, payload)
            self.assertEqual((resp.getheader('Connection') or '').lower(), 'close')
            self.assertEqual(json.loads(payload), {'ok': True})
            self.assertIsNone(conn.sock)
            self.assertEqual(store.get_supply_map()['fake']['target_model'],
                             'fake-model-after-reconnect')
            self.assertNotIn('fake', self.proxy.cooldown_store.snapshot())
            self.assertEqual(len(self.mock.calls), previous_calls)

            conn.request('GET', '/model_proxy/status', headers={'X-Proxy-Admin-Token': ADMIN})
            self.assertIsNotNone(conn.sock)
            self.assertIsNot(conn.sock, first_sock)
            resp = conn.getresponse()
            payload = resp.read()
            self.assertEqual(resp.status, 200, payload)
            status = json.loads(payload)
            self.assertEqual(status['supplies'][0]['target_model'], 'fake-model-after-reconnect')
            self.assertNotIn('fake', status['cooldown'])
            self.assertEqual(len(self.mock.calls), previous_calls)

            body = {'model': 'claude-sonnet', 'max_tokens': 16,
                    'messages': [{'role': 'user', 'content': 'hello'}]}
            conn.request('POST', '/v1/messages', json.dumps(body).encode(),
                         {'Content-Type': 'application/json', 'x-api-key': CLIENT,
                          'anthropic-version': '2023-06-01'})
            resp = conn.getresponse()
            payload = resp.read()
            self.assertEqual(resp.status, 200, payload)
            self.assertEqual(json.loads(payload)['content'][0]['text'], 'fake-reply')
            self.assertEqual(len(self.mock.calls), previous_calls + 1)
            path, headers, forwarded = self.mock.calls[-1]
            self.assertEqual(path, '/v1/messages')
            self.assertEqual(headers.get('X-Api-Key'), UPSTREAM)
            self.assertEqual(forwarded['model'], 'fake-model-after-reconnect')
            self.assertEqual(forwarded['messages'], body['messages'])
        finally:
            conn.close()

    def test_json_sse_reload_and_control_auth(self):
        body = {'model': 'claude-sonnet', 'max_tokens': 16,
                'messages': [{'role': 'user', 'content': 'hello'}], 'stream': False}
        headers = {'Content-Type': 'application/json', 'x-api-key': CLIENT,
                   'anthropic-version': '2023-06-01'}
        with self.assertRaises(AssertionError):
            self.guarded_open(urllib.request.Request('https://example.invalid/v1/messages'))
        bad_headers = dict(headers, **{'x-api-key': 'wrong-client'})
        status, _, _ = self.request('/v1/messages', body, bad_headers)
        self.assertEqual(status, 401)
        self.assertEqual(len(self.mock.calls), 0)
        status, _, payload = self.request('/v1/messages', body, headers)
        self.assertEqual(status, 200, payload)
        self.assertEqual(json.loads(payload)['content'][0]['text'], 'fake-reply')
        self.assertEqual(len(self.mock.calls), 1)
        path, upheaders, upstream = self.mock.calls[-1]
        self.assertEqual(path, '/v1/messages')
        self.assertEqual(upheaders.get('X-Api-Key'), UPSTREAM)
        self.assertEqual(upstream['model'], 'fake-model')
        self.assertEqual(upstream['messages'], body['messages'])
        status, _, _ = self.request('/model_proxy/reload', {}, {'X-Proxy-Admin-Token': 'wrong'})
        self.assertEqual(status, 401)
        self.cfg['supplies'][0]['target_model'] = 'fake-model-reloaded'
        self.save_config()
        status, _, payload = self.request('/model_proxy/reload', {}, {'X-Proxy-Admin-Token': ADMIN})
        self.assertEqual(status, 200, payload)
        self.assertIs(json.loads(payload).get('ok'), True, payload)
        self.assertEqual(self.proxy.config_store.get_supply_map()['fake']['target_model'],
                         'fake-model-reloaded')
        body['stream'] = True
        status, hdr, payload = self.request('/v1/messages', body, headers)
        self.assertEqual(status, 200, payload)
        self.assertIn('text/event-stream', hdr.get('Content-Type', ''))
        self.assertIn(b'fake-reply', payload)
        events = [line[7:].strip() for line in payload.splitlines() if line.startswith(b'event: ')]
        self.assertEqual(events[-1], b'message_stop')
        self.assertEqual(events.count(b'message_stop'), 1)
        self.assertIn(b'content_block_delta', events)
        self.assertEqual(len(self.mock.calls), 2)
        self.assertEqual(self.mock.calls[-1][2]['model'], 'fake-model-reloaded')
        self.assertTrue(self.mock.calls[-1][2]['stream'])
        # Accounting happens in the handler's finally block, after the client can
        # receive the final byte. The request count includes the 401, not three
        # successful upstream forwards (the mock received only two).
        deadline = time.monotonic() + 5
        totals = {}
        while time.monotonic() < deadline:
            if self.paths['totals'].exists():
                try:
                    totals = json.loads(self.paths['totals'].read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    pass
                if totals.get('total', {}).get('requests', 0) >= 3:
                    break
            time.sleep(0.02)
        self.assertGreaterEqual(totals.get('total', {}).get('requests', 0), 3,
                                'real usage accounting did not persist the request count')
        self.check_control_connections()
        with self.subTest(same_httpconnection_reconnect=True):
            self.check_body_control_close_reconnects_same_httpconnection()


if __name__ == '__main__':
    unittest.main()
