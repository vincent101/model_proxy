"""Test-only core.server.main experiment: NOT a Windows CLI or acceptance test.

Run standalone with Windows Python -I -S -B and --native or --injected.
Whisky's detached launcher needs tests/wine_service_runner.py to report results.
Each run creates one fresh fake project, fake keys, owned mock and OS-allocated
loopback ports. --native leaves startup guards intact. --injected uses only
explicit test-process substitutes in wine_service_fixture.py; it does not
establish Windows filesystem/ACL, process ownership, or exclusivity safety.
Ports are allocated by binding to port 0 and released just before main binds:
there is a race, so the test connects only after its own server reaches serve.
No OS sandbox is claimed; cooperative local code/users are assumed.
"""
import contextlib
import hashlib
import http.client
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import traceback
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'tests'))


def source_digest():
    """Detect concurrent edits, including additions/removals, during this run."""
    h = hashlib.sha256()
    files = sorted((REPO / 'core').rglob('*.py'))
    files += [p for p in (REPO / 'model_proxy_cli.bat', REPO / 'model_proxy_cli.sh',
                          REPO / 'hooker' / 'ensure_model_proxy.bat',
                          REPO / 'hooker' / 'ensure_model_proxy.sh',
                          REPO / 'tests' / 'test_wine_proxy_integration.py',
                          REPO / 'tests' / 'test_wine_service_startup.py',
                          REPO / 'tests' / 'wine_service_fixture.py',
                          REPO / 'tests' / 'wine_service_runner.py') if p.is_file()]
    for p in files:
        h.update(str(p.relative_to(REPO)).replace('\\', '/').encode())
        h.update(b'\0')
        h.update(p.read_bytes())
        h.update(b'\0')
    return '%s (%d core/CLI/test files)' % (h.hexdigest(), len(files))


def exercise(server, stack, root, project, config, pathsfile, paths, mock):
    import wine_service_fixture
    wine_service_fixture.install(stack, server, root, project, config, pathsfile, paths)
    stack.enter_context(patch.object(server.session_identity, 'DEFAULT_SESSIONS_DIR', root / 'home' / 'sessions'))
    stack.enter_context(patch.object(server.claudian_titles, 'DEFAULT_CLAUDIAN_SESSIONS_DIR', root / 'home' / 'titles'))

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def only_mock(req, *args, **kwargs):
        parts = urllib.parse.urlsplit(req.full_url)
        if (parts.scheme != 'http' or parts.hostname != '127.0.0.1' or
                parts.port != mock.server_address[1] or parts.username is not None):
            raise AssertionError('outbound destination is not this test\'s mock')
        return opener.open(req, *args, **kwargs)
    stack.enter_context(patch.object(server.urllib.request, 'urlopen', side_effect=only_mock))

    # Capture the actual product server object without replacing the bind or handler.
    ready = threading.Event()
    captured = []
    errors = []
    original_server = server.ProxyHTTPServer
    def create_server(address, handler):
        instance = original_server(address, handler)
        instance.daemon_threads = False
        captured.append(instance)
        serve = instance.serve_forever
        def mark_and_serve():
            ready.set()
            return serve(poll_interval=0.05)
        instance.serve_forever = mark_and_serve
        return instance
    stack.enter_context(patch.object(server, 'ProxyHTTPServer', side_effect=create_server))

    def run_main():
        try:
            server.main()
        except BaseException:
            errors.append(traceback.format_exc())
        finally:
            ready.set()
    thread = threading.Thread(target=run_main, name='owned-test-server', daemon=True)
    thread.start()
    try:
        if not ready.wait(25):
            raise AssertionError('startup timed out before serve')
        if errors:
            raise AssertionError('main failed before serving:\n' + errors[0])
        if not captured or not thread.is_alive():
            raise AssertionError('main did not reach serve_forever')
        instance = captured[0]
        host, port = instance.server_address[:2]
        assert host == '127.0.0.1' and port == json.loads(config.read_text(encoding='utf-8'))['listen']['port']
        conn = http.client.HTTPConnection(host, port, timeout=8)
        try:
            body = json.dumps({'model': 'claude-sonnet', 'max_tokens': 16,
                               'messages': [{'role': 'user', 'content': 'hello'}], 'stream': False})
            conn.request('POST', '/v1/messages', body,
                         {'Content-Type': 'application/json', 'x-api-key': 'wrong-fake-key',
                          'anthropic-version': '2023-06-01'})
            rejected = conn.getresponse()
            assert rejected.status == 401
            rejected.read()
            assert not mock.calls
            conn.request('POST', '/v1/messages', body,
                         {'Content-Type': 'application/json', 'x-api-key': CLIENT,
                          'anthropic-version': '2023-06-01'})
            response = conn.getresponse()
            payload = response.read()
            assert response.status == 200, (response.status, payload)
            assert json.loads(payload)['content'][0]['text'] == 'fake-reply'
        finally:
            conn.close()
        assert len(mock.calls) == 1
        assert mock.calls[0][0] == '/v1/messages'
        assert mock.calls[0][2]['model'] == 'fake-model'
        record = json.loads((project / '.runtime' / 'active.json').read_text(encoding='utf-8'))
        assert record['state'] == 'READY' and record['pid'] == os.getpid()
        assert record['process_start'] is not None and record['executable'] is not None
        print('Real current-process probes returned:', record['process_start'].get('kind'),
              'and executable path (not ownership/liveness proof)', flush=True)
        print('PASS: core.server.main -> real ProxyHTTPServer bind/serve -> authenticated '
              'POST (401 wrong key, then 200) -> owned loopback mock -> fake-reply; fixture READY record', flush=True)
        print('Owned listeners: proxy=%d mock=%d' % (port, mock.server_address[1]), flush=True)
    finally:
        if captured and thread.is_alive() and ready.is_set() and not errors:
            captured[0].shutdown()
        thread.join(15)
        if thread.is_alive():
            raise AssertionError('owned startup thread did not stop; cleanup unconfirmed')
        if errors:
            raise AssertionError('main failed:\n' + errors[0])
        print('Owned server thread stopped and main returned', flush=True)


def main():
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode) or sys.flags.optimize:
        raise SystemExit('requires -I -S -B')
    if os.name != 'nt':
        raise SystemExit('requires Windows Python (Whisky/Wine or native Windows)')
    if sys.argv[1:] not in (['--native'], ['--injected']):
        raise SystemExit('choose --native or --injected')
    before = source_digest()
    print('SOURCE BEFORE:', before, flush=True)
    try:
        global Upstream, NoRedirect, CLIENT, UPSTREAM, ADMIN
        from test_wine_proxy_integration import Upstream, NoRedirect, CLIENT, UPSTREAM, ADMIN
        with tempfile.TemporaryDirectory(prefix='mp-wine-startup-') as temp:
            root = Path(temp).resolve()
            print('Owned fixture:', root, flush=True)
            project = root / 'project'
            configdir = project / 'config'
            runtime = project / 'runtime'
            home = root / 'home'
            for path in (configdir, runtime, home, project / '.runtime'):
                path.mkdir(parents=True, exist_ok=True)
            config = configdir / 'fake.json'
            pathsfile = configdir / 'runtime_paths.json'
            paths = {k: runtime / k for k in ('log', 'totals', 'lock', 'pid', 'ensure_log', 'start_lock')}
            pathsfile.write_text(json.dumps({k: str(v) for k, v in paths.items()}), encoding='utf-8')
            for key in list(os.environ):
                if key.startswith('MODEL_PROXY_') or key.lower().endswith('_proxy'):
                    del os.environ[key]
            for key in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA'):
                os.environ[key] = str(home)
            mock = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
            mock.daemon_threads = False
            mock.calls = []
            mock_thread = threading.Thread(target=mock.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
            mock_thread.start()
            try:
                with socket.socket() as reservation:
                    reservation.bind(('127.0.0.1', 0))
                    port = reservation.getsockname()[1]
                cfg = {'listen': {'host': '127.0.0.1', 'port': port}, 'admin_token': ADMIN,
                       'upstream_timeout_seconds': 5,
                       'supplies': [{'id': 'fake', 'url': 'http://127.0.0.1:%d/v1/messages' % mock.server_address[1],
                                     'protocol': 'anthropic', 'appkey': UPSTREAM, 'target_model': 'fake-model'}],
                       'routes': [{'id': 'fake-route', 'tiers': {'sonnet': ['fake']}, 'failover': 'off'}],
                       'strategies': [{'client_token': CLIENT, 'route_id': 'fake-route'}]}
                config.write_text(json.dumps(cfg), encoding='utf-8')
                from core import server
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(server, '_PACKAGE_DIR', project))
                    stack.enter_context(patch.object(server, '_RUNTIME_PATHS_FILE', pathsfile))
                    stack.enter_context(patch.object(server, '_DEFAULT_CONFIG_PATH', config))
                    original_resolve = server.resolve_runtime_paths
                    stack.enter_context(patch.object(server, 'resolve_runtime_paths', side_effect=lambda: original_resolve(pathsfile)))
                    print('MODE:', sys.argv[1], 'platform:', sys.platform, 'python:', sys.version.split()[0], flush=True)
                    try:
                        if sys.argv[1] == '--native':
                            # Read-only diagnostics on this fixture; no startup guard is patched.
                            from core import discovery, windows_paths
                            class DiagnosticBackend:
                                def __init__(self):
                                    self.inner = discovery._windows_path_backend()
                                def __getattr__(self, name):
                                    return getattr(self.inner, name)
                                def observe(self, handle, policy, creation_kind=None):
                                    try:
                                        return self.inner.observe(handle, policy, creation_kind)
                                    except Exception as exc:
                                        position = 'root' if handle.parent is None else 'descendant'
                                        print('NATIVE observation detail:', position, policy,
                                              type(exc).__name__, getattr(exc, 'code', '(no code)'), flush=True)
                                        raise
                            try:
                                lease = windows_paths.inspect(str(project),
                                    backend=DiagnosticBackend(),
                                    kind='directory', private=False)
                            except windows_paths.PathEvidenceError as exc:
                                print('NATIVE path evidence:', exc.code, flush=True)
                            else:
                                lease.close()
                                print('NATIVE path evidence: project inspection passed', flush=True)
                            try:
                                server.inspect_runtime_identities(project, paths)
                            except Exception as exc:
                                print('NATIVE runtime preflight:', getattr(exc, 'code', type(exc).__name__), flush=True)
                            try:
                                server._require_windows_listener_validation()
                            except Exception as exc:
                                print('NATIVE listener gate:', getattr(exc, 'code', type(exc).__name__), flush=True)
                            server.main()
                        else:
                            exercise(server, stack, root, project, config, pathsfile, paths, mock)
                    finally:
                        # init_logging owns file handlers; close only ours before temp removal.
                        for logger in (logging.getLogger(), server.access_log):
                            for handler in list(logger.handlers):
                                if getattr(handler, 'baseFilename', None) == str(paths['log']):
                                    logger.removeHandler(handler)
                                    handler.close()
            finally:
                mock.shutdown()
                mock_thread.join(15)
                mock.server_close()
                if mock_thread.is_alive():
                    raise AssertionError('owned mock cleanup unconfirmed')
                print('Owned mock stopped', flush=True)
    finally:
        after = source_digest()
        print('SOURCE AFTER: ', after, flush=True)
        if after != before:
            raise AssertionError('concurrent core source change; rerun required')


if __name__ == '__main__':
    main()
