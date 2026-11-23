"""Isolated fake-authority tests, temporary project and own subprocesses only."""
from __future__ import annotations

import contextlib
import fcntl
import io
import ipaddress
import json
import os
import pty
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_ISOLATION = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if (Path(__file__).resolve().parents[1] != _ISOLATION / 'project'
        or not (_ISOLATION / '.model-proxy-isolation').is_file()
        or not Path(os.environ.get('HOME', '/invalid')).resolve().is_relative_to(_ISOLATION)):
    raise RuntimeError('an isolated snapshot and isolated HOME are required')

from core.cli.control import (ControlError, Instance, MacControl, StartupPending, _endpoint,
                              INSTALL_INITIALIZED, INSTALL_UNINITIALIZED_OR_RECORD_LOST)
from core import discovery, platform_process
from core.discovery import (inspect_runtime_identities, process_executable, process_start_identity,
                            verify_record_directory, write_instance_record)
from core.platform_lock import acquire_lock, verify_local_access


class LocalNumericEndpointTests(unittest.TestCase):
    """LAN cases are simulated only: no physical LAN interface is contacted."""

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin native interface enumeration')
    def test_native_interface_enumeration_contains_loopback(self):
        # Reads interface metadata only; does not connect to a LAN address.
        self.assertIn(ipaddress.ip_address("127.0.0.1"),
                      platform_process._darwin_interface_addresses())

    def test_local_numeric_ipv4_ipv6_and_wildcard_without_dns(self):
        local = {ipaddress.ip_address("192.0.2.10"), ipaddress.ip_address("fd00::10")}
        with mock.patch.object(platform_process, "_darwin_interface_addresses", return_value=local), \
                mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS prohibited")):
            self.assertEqual(_endpoint("192.0.2.10", 32001), (socket.AF_INET, "192.0.2.10", 32001))
            self.assertEqual(_endpoint("fd00::10", 32001), (socket.AF_INET6, "fd00::10", 32001))
            self.assertEqual(_endpoint("0.0.0.0", 32001), (socket.AF_INET, "127.0.0.1", 32001))
            self.assertEqual(_endpoint("::", 32001), (socket.AF_INET6, "::1", 32001))
            self.assertEqual(_endpoint("0:0:0:0:0:0:0:0", 32001),
                             (socket.AF_INET6, "::1", 32001))
            self.assertEqual(_endpoint("LOCALHOST", 32001), (socket.AF_INET, "127.0.0.1", 32001))
            for host in ("192.0.2.11", "fd00::11", "example.invalid", "fe80::1%1",
                         "ff02::1", "255.255.255.255"):
                with self.subTest(host=host), self.assertRaises(ControlError):
                    _endpoint(host, 32001)

    def test_ipv6_unspecified_probe_matches_server_v6only(self):
        # Pure mocks: do not actually bind a wildcard or contact the LAN.
        for host, v6only in (("::", 0), ("0:0:0:0:0:0:0:0", 1)):
            with self.subTest(host=host), mock.patch("socket.socket") as factory:
                sock = factory.return_value
                MacControl._port_free(host, 32001)
                sock.bind.assert_called_once_with(("::", 32001))
                sock.setsockopt.assert_any_call(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)
                sock.listen.assert_called_once_with(1)

    def test_ipv6_unspecified_overlap_matches_new_listener_dual_stack(self):
        ipv4 = mock.Mock(host="127.0.0.1", port=32001)
        ipv6 = mock.Mock(host="::1", port=32001)
        self.assertTrue(MacControl._listeners_overlap(ipv4, {"host": "::", "port": 32001}))
        self.assertFalse(MacControl._listeners_overlap(
            ipv4, {"host": "0:0:0:0:0:0:0:0", "port": 32001}))
        self.assertTrue(MacControl._listeners_overlap(
            ipv6, {"host": "0:0:0:0:0:0:0:0", "port": 32001}))

    def test_local_lan_socket_still_requires_exact_reverse_connection(self):
        local = {ipaddress.ip_address("192.0.2.10"), ipaddress.ip_address("192.0.2.20"),
                 ipaddress.ip_address("fd00::10"), ipaddress.ip_address("fd00::20")}
        cases = [
            (socket.AF_INET, ("192.0.2.10", 43001), ("192.0.2.20", 32001)),
            (socket.AF_INET6, ("fd00::10", 43001, 0, 0), ("fd00::20", 32001, 0, 0)),
        ]
        with mock.patch.object(platform_process, "_darwin_interface_addresses", return_value=local), \
                mock.patch.object(platform_process, "check_identity") as identity, \
                mock.patch.object(platform_process, "_socket_open_without_data"), \
                mock.patch.object(platform_process, "_darwin_has_reverse_connection", return_value=True) as reverse:
            for family, local_addr, peer_addr in cases:
                sock = mock.Mock()
                sock.family, sock.type = family, socket.SOCK_STREAM
                sock.getsockname.return_value, sock.getpeername.return_value = local_addr, peer_addr
                platform_process.verify_connected_peer(sock, 1234, {"fake": 1}, "/fake/python", timeout=0)
                target = reverse.call_args.args[1]
                self.assertEqual(target, (family, ipaddress.ip_address(local_addr[0]).packed, local_addr[1],
                                          ipaddress.ip_address(peer_addr[0]).packed, peer_addr[1]))
                sock.sendall.assert_not_called()
            self.assertGreaterEqual(identity.call_count, 4)

    def test_nonlocal_or_unavailable_interface_never_sends(self):
        sock = mock.Mock()
        sock.family, sock.type = socket.AF_INET, socket.SOCK_STREAM
        sock.getsockname.return_value = ("192.0.2.10", 43001)
        sock.getpeername.return_value = ("192.0.2.99", 32001)
        with mock.patch.object(platform_process, "_darwin_interface_addresses",
                               return_value={ipaddress.ip_address("192.0.2.10")}), \
                mock.patch.object(platform_process, "check_identity"), \
                mock.patch.object(platform_process, "_darwin_has_reverse_connection") as reverse:
            with self.assertRaises(platform_process.ProcessEvidenceError):
                platform_process.verify_connected_peer(sock, 1234, {"fake": 1}, "/fake/python", timeout=0)
            reverse.assert_not_called()
            sock.sendall.assert_not_called()
        with mock.patch.object(platform_process, "_darwin_interface_addresses",
                               side_effect=platform_process.ProcessEvidenceError("local_address_unavailable")):
            with self.assertRaises(platform_process.ProcessEvidenceError):
                _endpoint("192.0.2.10", 32001)

    def test_local_lan_control_uses_loopback_host_header_after_peer_check(self):
        with tempfile.TemporaryDirectory(prefix="mock-lan-", dir=_ISOLATION) as tmp:
            root = Path(tmp).resolve()
            out = io.StringIO()
            control = MacControl(root, config_path=root / "fake-config", out=out, err=io.StringIO())
            active = {"runtime_paths": {"totals": str(root / "totals"), "log": str(root / "log")}}
            instance = Instance(1234, {"fake": 1}, "/fake/python", "a" * 32,
                                "192.0.2.10", 32001, active, {})
            sock = mock.MagicMock()
            sock.__enter__.return_value = sock
            response = mock.Mock(status=200, length=0)
            response.read.return_value = b'{"supplies":[],"routes":[],"strategies":[],"cooldown":{}}'
            lease = mock.Mock()
            lease.admin_token.return_value = "fake-admin-token"
            lease.verify_admin_token.return_value = True
            with mock.patch.object(control, "_instance", return_value=instance), \
                    mock.patch.object(control, "_connection", return_value=sock), \
                    mock.patch.object(control, "_recheck"), \
                    mock.patch("core.cli.control._endpoint",
                               return_value=(socket.AF_INET, "192.0.2.10", 32001)), \
                    mock.patch.object(platform_process, "verify_connected_peer") as peer, \
                    mock.patch("http.client.HTTPResponse", return_value=response), \
                    mock.patch("core.cli._format_ops._format_status_from_json", return_value=["health: ok"]):
                control._request(lease, "status")
            peer.assert_called_once()
            payload = sock.sendall.call_args.args[0]
            self.assertIn(b"Host: 127.0.0.1:32001\r\n", payload)
            self.assertNotIn("fake-admin-token", out.getvalue())

# Standalone fake service. All credentials/configuration are fabricated.
_FAKE_SERVER = r'''import fcntl, json, os, socket, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.discovery import process_start_identity, process_executable
root=Path(__file__).resolve().parents[2]
state=root/'.runtime'
config=json.loads(Path(os.environ['MODEL_PROXY_CONFIG']).read_text())
paths=json.loads((root/'config/runtime_paths.json').read_text())
locks=[]
for path in (state/'anchor.lock', Path(paths['lock'])):
    f=open(path,'r+')
    fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
    locks.append(f)
s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind((config['listen']['host'],config['listen']['port']));s.listen(16)
lockinfo=lambda f: {'device':os.fstat(f.fileno()).st_dev,'inode':os.fstat(f.fileno()).st_ino}
common={'schema':1,'project_root':str(root),'instance_nonce':os.environ['MODEL_PROXY_START_NONCE'],
        'listen':config['listen'],'runtime_paths':paths,'trust':'observation_only','install_id':None}
active=dict(common,record='active',state='READY',pid=os.getpid(),process_start=process_start_identity(os.getpid()),
            executable=process_executable(os.getpid()),locks={'anchor':lockinfo(locks[0]),'legacy':lockinfo(locks[1])})
for name,data in [('last_good_paths.json',dict(common,record='last_good_paths')),('active.json',active)]:
    target=state/name
    temp=target.with_suffix('.tmp')
    with open(temp,'w') as f: json.dump(data,f)
    os.chmod(temp,0o600)
    os.replace(temp,target)
while True:
    conn,_=s.accept()
    with conn:
        conn.settimeout(4)
        try: data=conn.recv(65536)
        except OSError: continue
        if not data: continue
        with open(root/'received.log','ab') as f: f.write(data+b'\n---\n')
        if b'/model_proxy/reload ' in data:
            setting=root/'reload-response.json'
            opts=json.loads(setting.read_text()) if setting.exists() else {'code':200,'body':{'ok':True}}
        else: opts={'code':200,'body':{'status':'test','echo':config['admin_token'],
                    'cooldown':{'fake':{'remain':1,'reason':config['admin_token']}}}}
        if opts.get('disconnect'): continue
        body=json.dumps(opts['body']).encode()
        headers=('HTTP/1.1 %s Fake\r\nContent-Length: %s\r\nConnection: close\r\n\r\n' % (opts['code'],len(body))).encode()
        conn.sendall(headers+body)
'''


def unused_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def locked_by_other(path):
    with open(path, 'r+') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


class FakeAuthority:
    """Tests only: intentionally does NOT implement production trust."""
    def __init__(self, root, token):
        self.root, self.token = root, token
        self.allow = True
        self.permit_start = True
        self.permit_release = True
        self.token_reads = 0
        self.state = INSTALL_INITIALIZED
        self.initialization_calls = 0
        self.initialization_safe = True
        self.commit_on_confirm = True
        self.lock_evidence_captures = 0

    def installation_state(self):
        return self.state

    def installation_identity(self):
        """Judge the fixture's real install record with the production rule."""
        try:
            install = json.loads((self.root / '.runtime/install.json').read_text())
        except FileNotFoundError:
            return None
        return discovery.installation_identity(self.root / '.runtime', install).verdict

    def initialize_interactively(self, stdin, stdout):
        # Testing double only; no real record, lock-ownership or ACL proof.
        self.initialization_calls += 1
        if not self.initialization_safe:
            return False
        stdout.write('Installation/recovery source: '); stdout.flush()
        source = stdin.readline().strip()
        stdout.write('No legacy/shared/autostart risk; type YES: '); stdout.flush()
        confirm = stdin.readline().strip()
        if not source or confirm != 'YES':
            return False
        if self.commit_on_confirm:
            self.state = INSTALL_INITIALIZED
        return True

    @contextlib.contextmanager
    def locked(self, root, config_path):
        if root != self.root or Path(config_path) != self.root/'config/model_proxy_config.json':
            raise RuntimeError('fake root mismatch')
        yield self

    def records(self):
        state = self.root / '.runtime'
        def read(name):
            try:
                return json.loads((state/name).read_text())
            except FileNotFoundError:
                return None
        return read('active.json'), read('last_good_paths.json')

    def verify_active(self, instance, *, require_identity_evidence=True):
        return self.allow

    def assert_startable(self):
        return (self.permit_start and not locked_by_other(self.root/'.runtime/anchor.lock')
                and not locked_by_other(self.root/'runtime/lock'))

    def stop_lock_evidence(self, instance):
        # Real fixed lock objects from this fixture; production additionally
        # verifies protection and consumes the evidence in assert_stopped.
        self.lock_evidence_captures += 1
        evidence = {}
        for name, path in (('anchor', self.root/'.runtime/anchor.lock'),
                           ('legacy', self.root/'runtime/lock')):
            info = os.lstat(path)
            evidence[name] = (info.st_dev, info.st_ino)
        return evidence

    def assert_stopped(self, instance, *, require_identity_evidence=True):
        return (self.permit_release and not locked_by_other(self.root/'.runtime/anchor.lock')
                and not locked_by_other(self.root/'runtime/lock'))

    def assert_restart_supported(self):
        return True

    def admin_token(self):
        self.token_reads += 1
        return self.token

    def verify_admin_token(self):
        return True  # This test double does not model configuration evidence.


@unittest.skipUnless(sys.platform == 'darwin', 'native Darwin proc socket ABI')
class IsolatedMacControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mp-control-test-')
        self.root = Path(self.temp.name).resolve()
        for path in ('config', 'runtime', 'core/cli', '.runtime'):
            (self.root/path).mkdir(parents=True, exist_ok=True)
        (self.root/'.runtime').chmod(0o700)
        source = Path(__file__).resolve().parents[1]
        for name in ('__init__.py', 'discovery.py', 'runtime_paths.py'):
            shutil.copyfile(source/'core'/name, self.root/'core'/name)
        (self.root/'core/cli/model_proxy.py').write_text(_FAKE_SERVER)
        for path in (self.root/'.runtime/anchor.lock', self.root/'runtime/lock'):
            path.touch(mode=0o600)
        self.paths = {key: str(self.root/'runtime'/key) for key in
                      ('log','totals','lock','pid','ensure_log','start_lock')}
        (self.root/'config/runtime_paths.json').write_text(json.dumps(self.paths))
        # Reload is a live operation: the fixture provides a real install
        # record, judged by the production rule, instead of faking a verdict.
        device, inode = verify_record_directory(self.root/'.runtime')
        snapshot = inspect_runtime_identities(self.root, self.paths)
        with acquire_lock(snapshot, 'anchor.lock', create=True,
                          access_verifier=verify_local_access) as anchor:
            with acquire_lock(anchor.snapshot, 'lock', create=True,
                              access_verifier=verify_local_access, anchor=anchor) as legacy:
                write_instance_record(self.root/'.runtime', 'install.json', {
                    'schema': discovery.INSTANCE_SCHEMA, 'record': 'install',
                    'project_root': str(self.root), 'install_id': 'b' * 32,
                    'root_identity': {'device': device, 'inode': inode},
                    'initialization_source': 'isolated test fixture',
                    'ever_started': True, 'last_instance_nonce': 'a' * 32,
                }, anchor=anchor, legacy=legacy)
        self.port = unused_port()
        self.token = 'FAKE_CONTROL_TOKEN_"_NOT_A_REAL_CREDENTIAL'
        self.config = self.root/'config/model_proxy_config.json'
        self.config.write_text(json.dumps({'listen': {'host':'127.0.0.1','port':self.port},
                                           'admin_token':self.token}))
        self.out, self.err = io.StringIO(), io.StringIO()
        self.auth = FakeAuthority(self.root, self.token)
        self.control = MacControl(self.root, authority=self.auth, config_path=self.config,
                                  startup_timeout=3, stop_timeout=3, request_timeout=2,
                                  out=self.out, err=self.err)
        self.children = []
        real_popen = subprocess.Popen
        def spawn(*a, **kw):
            p = real_popen(*a, **kw)
            self.children.append(p)
            return p
        self.spawn_patch = mock.patch('core.cli.control.subprocess.Popen', side_effect=spawn)
        self.spawn_patch.start()

    def tearDown(self):
        self.spawn_patch.stop()
        # Only Popen objects created by this test are eligible for cleanup.
        for p in self.children:
            if p.poll() is None:
                p.terminate()
            p.wait(timeout=4)
            if p.stdout is not None: p.stdout.close()
            if p.stderr is not None: p.stderr.close()
        self.temp.cleanup()

    def start(self):
        self.assertEqual(self.control.on(), 0, self.err.getvalue())
        self.assertEqual(len(self.children), 1)
        return self.children[0]

    def start_reaper(self, p):
        thread = threading.Thread(target=p.wait, daemon=True)
        thread.start()
        return thread

    def test_full_lifecycle_same_socket_and_fake_token_redacted(self):
        p = self.start()
        self.assertEqual(self.control.status(), 0, self.err.getvalue())
        self.assertNotIn(self.token, self.out.getvalue())
        self.assertIn('[REDACTED]', self.out.getvalue())
        self.assertEqual(self.control.reload(), 0, self.err.getvalue())
        received = (self.root/'received.log').read_text()
        self.assertEqual(received.count('X-Proxy-Admin-Token:'), 2)
        self.assertIn(self.token, received)
        self.start_reaper(p)
        self.assertEqual(self.control.off(), 0, self.err.getvalue())
        self.assertIsNotNone(p.poll())

    def test_status_formatter_failure_is_sanitized(self):
        self.start()
        with mock.patch('core.cli._format_ops._format_status_from_json',
                        side_effect=RuntimeError(self.token)):
            self.assertEqual(self.control.status(), 1)
        self.assertNotIn(self.token, self.out.getvalue() + self.err.getvalue())
        self.assertIn('status_failed', self.err.getvalue())

    def test_reload_failure_and_disconnection_do_not_retry(self):
        self.start()
        target = self.root/'reload-response.json'
        target.write_text(json.dumps({'code':422,'body':{'ok':False,'loaded':False,'cooldown_cleared':True}}))
        self.assertEqual(self.control.reload(), 1)
        self.assertIn('loaded=no, cooldown_cleared=yes', self.err.getvalue())
        target.write_text(json.dumps({'disconnect':True}))
        self.assertEqual(self.control.reload(), 1)
        self.assertIn('response_unknown', self.err.getvalue())
        self.assertEqual((self.root/'received.log').read_text().count('POST /model_proxy/reload'), 2)

    def test_actual_server_requires_tty_for_first_initialization(self):
        from core.server import _confirm_initialization
        from core.discovery import PreflightError
        with open(os.devnull, 'r') as devnull, mock.patch('sys.stdin', devnull):
            with self.assertRaises(PreflightError) as failure:
                _confirm_initialization()
        self.assertEqual(failure.exception.code, 'initialization_required')

    def test_real_server_copy_non_tty_first_launch_fails_before_records(self):
        # A separate temporary copy, fake HOME/config and six isolated paths.
        # No management request or upstream connection is made.
        source = Path(__file__).resolve().parents[1]
        project = self.root/'actual-project'
        project.mkdir()
        shutil.copytree(source/'core', project/'core',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (project/'config').mkdir(); (project/'runtime').mkdir(); (project/'home').mkdir()
        port = unused_port()
        self.assertNotEqual(port, 18889)
        paths = {key: str(project/'runtime'/key) for key in
                 ('log','totals','lock','pid','ensure_log','start_lock')}
        (project/'config/runtime_paths.json').write_text(json.dumps(paths))
        config = project/'config/model_proxy_config.json'
        config.write_text(json.dumps({'listen': {'host': '127.0.0.1', 'port': port},
                                      'admin_token': 'FAKE_ONLY', 'supplies': [],
                                      'routes': [], 'strategies': []}))
        config.chmod(0o600)
        env = {'HOME': str(project/'home'), 'TMPDIR': os.environ['TMPDIR'],
               'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1',
               'MODEL_PROXY_CONFIG': str(config)}
        child = subprocess.Popen([sys.executable, '-B', str(project/'core/cli/model_proxy.py')],
                                 cwd=project, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = child.communicate(timeout=10)
        self.assertNotEqual(child.returncode, 0)
        self.assertIn('initialization_required', stderr)
        self.assertFalse((project/'.runtime/install.json').exists())
        self.assertFalse((project/'.runtime/active.json').exists())
        self.assertFalse((project/'.runtime/last_good_paths.json').exists())

    def test_real_preinitialized_server_copy_starts_with_devnull(self):
        # Real core helper + real service, but only a disposable test installation.
        # The PTY responses below are fabricated operator consent for this fixture;
        # they are never an authorization or fallback for a real installation.
        from core.discovery import (PreflightError, initialize_installation,
                                    inspect_runtime_identities, read_instance_set)
        from core.platform_lock import acquire_lock, verify_local_access
        from core.server import _confirm_initialization
        source = Path(__file__).resolve().parents[1]
        project = self.root/'preinitialized-project'
        project.mkdir()
        shutil.copytree(source/'core', project/'core',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (project/'config').mkdir(); (project/'runtime').mkdir(); (project/'home').mkdir()
        state = project/'.runtime'; state.mkdir(mode=0o700)
        port = unused_port()
        self.assertNotEqual(port, 18889)
        paths = {key: str(project/'runtime'/key) for key in
                 ('log','totals','lock','pid','ensure_log','start_lock')}
        (project/'config/runtime_paths.json').write_text(json.dumps(paths))
        config = project/'config/model_proxy_config.json'
        listen = {'host': '127.0.0.1', 'port': port}
        config.write_text(json.dumps({'listen': listen, 'admin_token': 'FAKE_ONLY',
                                      'supplies': [], 'routes': [], 'strategies': []}))
        config.chmod(0o600)
        snapshot = inspect_runtime_identities(project, paths)
        with contextlib.ExitStack() as stack:
            anchor = stack.enter_context(acquire_lock(snapshot, 'anchor.lock', create=True,
                                                       access_verifier=verify_local_access))
            legacy = stack.enter_context(acquire_lock(anchor.snapshot, 'lock', create=True,
                                                       access_verifier=verify_local_access, anchor=anchor))
            listener = stack.enter_context(socket.socket())
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', port)); listener.listen(1)
            master, slave = pty.openpty()
            stack.callback(os.close, master); stack.callback(os.close, slave)
            term_in = stack.enter_context(os.fdopen(os.dup(slave), 'r', buffering=1))
            term_out = stack.enter_context(os.fdopen(os.dup(slave), 'w', buffering=1))
            os.write(master, b'isolated-test-fixture\nYES\n')
            with mock.patch('sys.stdin', term_in), contextlib.redirect_stdout(term_out):
                attestation = _confirm_initialization()
            install = initialize_installation(anchor, legacy, listener, listen=listen,
                                                source=attestation, confirmed=True)
            self.assertEqual(install['initialization_source'], 'isolated-test-fixture')
            self.assertFalse(install['ever_started'])
        env = {'HOME': str(project/'home'), 'TMPDIR': os.environ['TMPDIR'],
               'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1',
               'MODEL_PROXY_CONFIG': str(config), 'MODEL_PROXY_START_NONCE': 'a'*32}
        child = subprocess.Popen([sys.executable, '-B', str(project/'core/cli/model_proxy.py')],
                                 cwd=project, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 8
        found = None
        while time.monotonic() < deadline:
            if child.poll() is not None:
                self.fail('fixture server exited: ' + child.stderr.read())
            try:
                records = read_instance_set(state)
            except PreflightError:
                records = None  # atomic publication may be temporarily incomplete
            if records and records['active'] and records['active']['state'] == 'READY':
                found = records
                break
            time.sleep(0.05)
        self.assertIsNotNone(found, 'fixture server did not publish READY')
        self.assertEqual(found['active']['pid'], child.pid)
        self.assertEqual(found['active']['instance_nonce'], 'a'*32)
        self.assertEqual(found['active']['install_id'], install['install_id'])
        self.assertTrue(found['install']['ever_started'])
        # No token and no HTTP bytes: verify this own child accepted the socket.
        with socket.create_connection(('127.0.0.1', port), timeout=2) as connection:
            platform_process.verify_connected_peer(connection, child.pid,
                found['active']['process_start'], found['active']['executable'])

    def _terminal_control(self, answers):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        instream = os.fdopen(os.dup(slave), 'r', buffering=1)
        outstream = os.fdopen(os.dup(slave), 'w', buffering=1)
        self.addCleanup(instream.close)
        self.addCleanup(outstream.close)
        os.write(master, answers.encode('utf-8'))
        return MacControl(self.root, authority=self.auth, config_path=self.config,
                          startup_timeout=3, stop_timeout=3, request_timeout=2,
                          terminal_in=instream, terminal_out=outstream,
                          out=self.out, err=self.err)

    def test_uninitialized_non_tty_fails_before_spawn(self):
        self.auth.state = INSTALL_UNINITIALIZED_OR_RECORD_LOST
        control = MacControl(self.root, authority=self.auth, config_path=self.config,
                             terminal_in=io.StringIO(), terminal_out=io.StringIO(),
                             out=self.out, err=self.err)
        self.assertEqual(control.on(), 1)
        self.assertIn('initialization_interactive_required', self.err.getvalue())
        self.assertEqual(self.auth.initialization_calls, 0)
        self.assertFalse(self.children)
        self.assertEqual(self.auth.token_reads, 0)

    def test_initialization_requires_source_and_yes_before_spawn(self):
        self.auth.state = INSTALL_UNINITIALIZED_OR_RECORD_LOST
        control = self._terminal_control('temporary fixture\nNO\n')
        self.assertEqual(control.on(), 1)
        self.assertIn('initialization_unconfirmed', self.err.getvalue())
        self.assertEqual(self.auth.state, INSTALL_UNINITIALIZED_OR_RECORD_LOST)
        self.assertFalse(self.children)
        self.assertEqual(self.auth.token_reads, 0)

    def test_empty_initialization_source_never_spawns(self):
        self.auth.state = INSTALL_UNINITIALIZED_OR_RECORD_LOST
        control = self._terminal_control('\nYES\n')
        self.assertEqual(control.on(), 1)
        self.assertEqual(self.auth.state, INSTALL_UNINITIALIZED_OR_RECORD_LOST)
        self.assertFalse(self.children)

    def test_interrupted_initialization_is_unknown_and_does_not_spawn(self):
        self.auth.state = INSTALL_UNINITIALIZED_OR_RECORD_LOST
        control = self._terminal_control('unused\n')
        with mock.patch.object(self.auth, 'initialize_interactively', side_effect=KeyboardInterrupt):
            self.assertEqual(control.on(), 1)
        self.assertIn('initialization_interrupted', self.err.getvalue())
        self.assertIn('outcome unknown', self.err.getvalue())
        self.assertFalse(self.children)

    def test_different_terminal_input_output_are_rejected(self):
        self.auth.state = INSTALL_UNINITIALIZED_OR_RECORD_LOST
        with contextlib.ExitStack() as stack:
            first, first_slave = pty.openpty(); second, second_slave = pty.openpty()
            for fd in (first, first_slave, second, second_slave):
                stack.callback(os.close, fd)
            input_stream = stack.enter_context(os.fdopen(os.dup(first_slave), 'r'))
            output_stream = stack.enter_context(os.fdopen(os.dup(second_slave), 'w'))
            control = MacControl(self.root, authority=self.auth, config_path=self.config,
                                 terminal_in=input_stream, terminal_out=output_stream,
                                 out=self.out, err=self.err)
            self.assertEqual(control.on(), 1)
        self.assertIn('initialization_interactive_required', self.err.getvalue())
        self.assertEqual(self.auth.initialization_calls, 0)
        self.assertFalse(self.children)

    def test_interactive_initialization_then_detached_launch(self):
        self.auth.state = INSTALL_UNINITIALIZED_OR_RECORD_LOST
        control = self._terminal_control('temporary fixture\nYES\n')
        self.assertEqual(control.on(), 0, self.err.getvalue())
        self.assertEqual(self.auth.initialization_calls, 1)
        self.assertEqual(self.auth.state, INSTALL_INITIALIZED)
        self.assertEqual(len(self.children), 1)
        self.assertEqual(self.auth.token_reads, 0)

    def test_initialization_unknown_or_incomplete_never_spawns(self):
        self.auth.state = 'UNKNOWN'
        self.assertEqual(self.control.on(), 1)
        self.assertIn('installation_state_unknown', self.err.getvalue())
        self.assertFalse(self.children)
        self.assertEqual(self.auth.initialization_calls, 0)

    def test_cannot_accept_confirmation_without_verified_record(self):
        self.auth.state = INSTALL_UNINITIALIZED_OR_RECORD_LOST
        self.auth.commit_on_confirm = False
        control = self._terminal_control('temporary fixture\nYES\n')
        self.assertEqual(control.on(), 1)
        self.assertIn('initialization_verification_failed', self.err.getvalue())
        self.assertFalse(self.children)

    def test_unsafe_preflight_does_not_prompt_or_spawn(self):
        self.auth.state = INSTALL_UNINITIALIZED_OR_RECORD_LOST
        self.auth.initialization_safe = False
        control = self._terminal_control('temporary fixture\nYES\n')
        self.assertEqual(control.on(), 1)
        self.assertEqual(self.auth.state, INSTALL_UNINITIALIZED_OR_RECORD_LOST)
        self.assertFalse(self.children)

    def test_unsupported_python_fails_before_initialization_or_spawn(self):
        self.auth.state = INSTALL_UNINITIALIZED_OR_RECORD_LOST
        control = self._terminal_control('temporary fixture\nYES\n')
        with mock.patch('core.cli.control.sys.version_info', (3, 9, 6)):
            self.assertEqual(control.on(), 1)
        self.assertIn('python_version_unsupported', self.err.getvalue())
        self.assertEqual(self.auth.initialization_calls, 0)
        self.assertFalse(self.children)

    def test_missing_initialization_contract_fails_before_spawn(self):
        with mock.patch.object(self.auth, 'installation_state', None):
            self.assertEqual(self.control.on(), 1)
        self.assertIn('initialization_authority_unavailable', self.err.getvalue())
        self.assertFalse(self.children)

    def test_initialized_record_disappears_before_spawn(self):
        with mock.patch.object(self.auth, 'installation_state',
                               side_effect=[INSTALL_INITIALIZED, INSTALL_UNINITIALIZED_OR_RECORD_LOST]):
            self.assertEqual(self.control.on(), 1)
        self.assertIn('initialization_verification_failed', self.err.getvalue())
        self.assertFalse(self.children)

    def test_restart_never_reinitializes_after_stopping(self):
        child = self.start()
        self.start_reaper(child)
        original = self.auth.assert_stopped
        def stopped(instance, **kwargs):
            ok = original(instance, **kwargs)
            self.auth.state = INSTALL_UNINITIALIZED_OR_RECORD_LOST
            return ok
        with mock.patch.object(self.auth, 'assert_stopped', side_effect=stopped):
            self.assertEqual(self.control.restart(), 1)
        self.assertIn('initialization_after_stop_forbidden', self.err.getvalue())
        self.assertEqual(self.auth.initialization_calls, 0)
        self.assertEqual(len(self.children), 1)

    def test_no_authority_no_credentials_no_signal_no_spawn(self):
        control = MacControl(self.root, config_path=self.config, out=self.out, err=self.err)
        for command in ('on','off','restart','status','reload'):
            self.assertEqual(getattr(control,command)(), 1)
        self.assertFalse(self.children)
        self.assertEqual(self.auth.token_reads, 0)
        self.assertIn('authority not integrated', self.err.getvalue())

    def test_authority_rejects_different_config_before_token_or_spawn(self):
        other = self.root/'config/other.json'
        other.write_text(self.config.read_text())
        control = MacControl(self.root, authority=self.auth, config_path=other,
                             out=self.out, err=self.err)
        self.assertEqual(control.on(), 1)
        self.assertEqual(control.status(), 1)
        self.assertFalse(self.children)
        self.assertEqual(self.auth.token_reads, 0)

    def test_authority_exception_never_prints_its_details(self):
        with mock.patch.object(self.auth, 'locked', side_effect=ControlError(self.token, self.token)):
            self.assertEqual(self.control.on(), 1)
        self.assertNotIn(self.token, self.err.getvalue())
        self.assertIn('control_failed', self.err.getvalue())

    def test_untrusted_record_never_authorizes_signal_or_token(self):
        self.start()
        self.auth.allow = False
        with mock.patch('core.platform_process.signal_verified') as signal_call:
            self.assertEqual(self.control.off(), 1)
            self.assertEqual(self.control.status(), 1)
            self.assertEqual(self.control.restart(), 1)
            signal_call.assert_not_called()
        self.assertEqual(self.auth.token_reads, 0)
        self.assertEqual(len(self.children), 1)
        self.assertFalse((self.root/'received.log').exists())

    def test_last_good_mismatch_fails_closed_and_off_ignores_a_recorded_lock(self):
        self.start()
        good = self.root/'.runtime/last_good_paths.json'
        saved = json.loads(good.read_text())
        bad = dict(saved, instance_nonce='a'*32)
        good.write_text(json.dumps(bad))
        # A structurally inconsistent record set still fails closed for status.
        self.assertEqual(self.control.status(), 1)
        good.write_text(json.dumps(saved))
        active = self.root/'.runtime/active.json'
        value = json.loads(active.read_text())
        value['locks']['anchor']['inode'] += 1
        active.write_text(json.dumps(value))
        with mock.patch('core.platform_process.signal_verified') as signal_call:
            # Stop judges structure plus live process/lock evidence; a changed
            # recorded lock identity no longer blocks the signal. The mocked
            # signal leaves the fixture child alive, so the confirmed exit
            # times out instead of reporting an identity failure.
            self.assertEqual(self.control.off(), 1)
            signal_call.assert_called_once()
            self.assertIn('stop_timeout', self.err.getvalue())
            self.assertNotIn('lock_identity_changed', self.err.getvalue())
        self.assertEqual(self.auth.token_reads, 0)

    def test_restart_rejects_changed_lock_identity_while_off_still_stops(self):
        child = self.start()
        self.start_reaper(child)
        active = self.root/'.runtime/active.json'
        value = json.loads(active.read_text())
        value['locks']['anchor']['inode'] += 1
        active.write_text(json.dumps(value))
        # Restart keeps the strict identity gate: the drifted recorded lock is
        # rejected before any signal.
        with mock.patch('core.platform_process.signal_verified') as signal:
            self.assertEqual(self.control.restart(), 1)
            signal.assert_not_called()
        self.assertIn('lock_identity_changed', self.err.getvalue())
        self.assertIsNone(child.poll())
        self.assertEqual(self.auth.lock_evidence_captures, 0)
        # Plain off uses structure plus live evidence; the same drifted record
        # does not block stopping the real fixture process, and the original
        # lock objects are recorded before the signal is sent.
        real_signal = platform_process.signal_verified
        captured_at_signal = []
        def signal_after_capture(pid, start, executable):
            captured_at_signal.append(self.auth.lock_evidence_captures)
            real_signal(pid, start, executable)
        with mock.patch('core.platform_process.signal_verified',
                        side_effect=signal_after_capture):
            self.assertEqual(self.control.off(), 0, self.err.getvalue())
        self.assertEqual(captured_at_signal, [1])
        self.assertIsNotNone(child.poll())

    def test_restart_pre_signal_rechecks_never_downgrade_identity(self):
        child = self.start()
        original = self.auth.records
        calls = []
        def drifting_records():
            calls.append(1)
            active, good = original()
            if len(calls) >= 4 and isinstance(active, dict):
                active = json.loads(json.dumps(active))
                active['locks']['legacy']['inode'] += 1
            return active, good
        # The drift appears only after the initial record read and instance
        # check; a relaxed pre-signal recheck would signal the process.
        with mock.patch.object(self.auth, 'records', side_effect=drifting_records), \
                mock.patch('core.platform_process.signal_verified') as signal:
            self.assertEqual(self.control.restart(), 1)
            signal.assert_not_called()
        self.assertIn('lock_identity_changed', self.err.getvalue())
        self.assertIsNone(child.poll())

    def test_pid_reuse_or_permissions_never_signal(self):
        self.start()
        with mock.patch('core.platform_process.process_start_identity', return_value=None), \
             mock.patch('core.platform_process.os.kill') as kill:
            self.assertEqual(self.control.off(), 1)
            self.assertEqual(self.control.status(), 1)
            self.assertTrue(all(call.args[1] == 0 for call in kill.call_args_list))
        with mock.patch('core.platform_process.process_start_identity', return_value={'kind':'fake-reused'}), \
             mock.patch('core.platform_process.os.kill') as kill:
            self.assertEqual(self.control.off(), 1)
            self.assertTrue(all(call.args[1] == 0 for call in kill.call_args_list))
        self.assertEqual(self.auth.token_reads, 0)

    def test_peer_refusal_before_token_read(self):
        self.start()
        with mock.patch('core.platform_process.verify_connected_peer',
                        side_effect=platform_process.ProcessEvidenceError('permission')):
            self.assertEqual(self.control.status(), 1)
            self.assertEqual(self.control.reload(), 1)
        self.assertEqual(self.auth.token_reads, 0)
        self.assertFalse((self.root/'received.log').exists())

    def test_off_can_finish_when_local_interface_enumeration_is_unavailable(self):
        child = self.start()
        self.start_reaper(child)
        with mock.patch.object(MacControl, '_port_free',
                side_effect=platform_process.ProcessEvidenceError('local_address_unavailable')), \
                mock.patch.object(platform_process, 'verify_connected_peer') as peer, \
                mock.patch.object(platform_process, 'signal_verified',
                                  wraps=platform_process.signal_verified) as signal:
            self.assertEqual(self.control.off(), 1, self.err.getvalue())
            self.assertIn('port_check_unknown', self.err.getvalue())
            self.assertIn('process exited; locks released; port release unknown', self.err.getvalue())
            self.assertNotIn('locks and port verified free', self.out.getvalue())
            peer.assert_not_called()
            signal.assert_called_once()
            self.assertEqual(signal.call_args.args[0], child.pid)
        self.assertIsNotNone(child.poll())
        self.assertEqual(self.auth.token_reads, 0)

    def test_actual_wrong_process_never_receives_token(self):
        self.start()
        sleeper = subprocess.Popen([sys.executable, '-B', '-u', '-c',
                                    'import time; print("ready", flush=True); time.sleep(30)'],
                                   stdout=subprocess.PIPE, text=True)
        self.assertEqual(sleeper.stdout.readline().strip(), 'ready')
        self.assertIsNotNone(process_start_identity(sleeper.pid))
        self.assertIsNotNone(process_executable(sleeper.pid))
        active = self.root/'.runtime/active.json'
        value = json.loads(active.read_text())
        value.update(pid=sleeper.pid, process_start=process_start_identity(sleeper.pid),
                     executable=process_executable(sleeper.pid))
        active.write_text(json.dumps(value))
        with mock.patch('core.platform_process.verify_connected_peer',
                        wraps=platform_process.verify_connected_peer) as peer_check:
            self.assertEqual(self.control.status(), 1)
            peer_check.assert_called()
        self.assertEqual(self.auth.token_reads, 0)
        self.assertFalse((self.root/'received.log').exists())

    def test_on_is_idempotent_only_for_verified_unchanged_instance(self):
        self.start()
        self.assertEqual(self.control.on(), 0, self.err.getvalue())
        self.assertEqual(len(self.children), 1)
        self.assertIn('already running and verified', self.out.getvalue())
        self.paths['totals'] = str(self.root/'runtime'/'different-totals')
        (self.root/'config/runtime_paths.json').write_text(json.dumps(self.paths))
        self.assertEqual(self.control.on(), 1)
        self.assertIn('running_configuration_changed', self.err.getvalue())
        self.assertEqual(len(self.children), 1)

    def test_transient_record_publication_only_retried_after_spawn(self):
        original = self.auth.records
        calls = 0
        def read_records():
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise StartupPending()
            return original()
        with mock.patch.object(self.auth, 'records', side_effect=read_records):
            self.start()
        self.assertGreaterEqual(calls, 3)
        self.assertEqual(self.auth.token_reads, 0)

    def test_one_final_fresh_observation_after_slow_stale_record(self):
        original = self.auth.records
        calls = 0
        def stale_then_ready():
            nonlocal calls
            calls += 1
            if calls == 1:
                limit = time.monotonic() + 3
                while time.monotonic() < limit:
                    active, _ = original()
                    if isinstance(active, dict) and active.get('state') == 'READY':
                        break
                    time.sleep(0.02)
                else:
                    self.fail('fake child did not publish READY')
                time.sleep(0.08)
                return None, None
            return original()
        self.control.startup_timeout = 0.05
        with mock.patch.object(self.auth, 'records', side_effect=stale_then_ready):
            child = self.start()
        self.assertGreaterEqual(calls, 2)
        self.start_reaper(child)
        self.assertEqual(self.control.off(), 0, self.err.getvalue())

    def test_live_unconfirmed_child_is_never_retried_or_signalled(self):
        child = mock.Mock(pid=12345)
        child.poll.return_value = None
        retry = mock.Mock(return_value=True)
        self.control.startup_timeout = 0.05
        with mock.patch('core.cli.control.subprocess.Popen', return_value=child) as spawn, \
                mock.patch.object(self.auth, 'records', return_value=(None, None)), \
                mock.patch.object(self.auth, 'retry_exited_child', retry, create=True), \
                mock.patch('core.platform_process.signal_verified') as signal:
            self.assertEqual(self.control.on(), 1)
            self.assertIn('startup_unconfirmed', self.err.getvalue())
            self.assertEqual(spawn.call_count, 1)
            retry.assert_not_called()
            signal.assert_not_called()
        self.assertEqual(self.auth.token_reads, 0)

    def test_candidate_schema_two_remains_gated_by_authority(self):
        self.start()
        for name in ('active.json', 'last_good_paths.json'):
            path = self.root/'.runtime'/name
            record = json.loads(path.read_text())
            record['schema'], record['install_id'] = 2, 'b'*32
            del record['trust']
            path.write_text(json.dumps(record))
        self.auth.allow = False
        self.assertEqual(self.control.status(), 1)
        self.assertEqual(self.auth.token_reads, 0)
        self.auth.allow = True
        self.assertEqual(self.control.status(), 0, self.err.getvalue())

    def test_native_ipv6_reverse_connection(self):
        program = ('import socket,time; s=socket.socket(socket.AF_INET6); '
                   's.bind(("::1",0)); s.listen(1); print(s.getsockname()[1],flush=True); '
                   'c,_=s.accept(); time.sleep(30)')
        child = subprocess.Popen([sys.executable, '-B', '-u', '-c', program],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        port = child.stdout.readline().strip()
        if not port:
            self.skipTest('IPv6 loopback unavailable')
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(('::1', int(port)))
            platform_process.verify_connected_peer(connection, child.pid,
                process_start_identity(child.pid), process_executable(child.pid))
        self.assertIsNone(child.poll())

    def test_peer_dies_after_connection_proof_before_token(self):
        child = self.start()
        original = platform_process.verify_connected_peer
        def die_after_proof(*args, **kwargs):
            original(*args, **kwargs)
            # The test owns this exact child handle, never an arbitrary PID.
            child.terminate()
            child.wait(timeout=3)
        with mock.patch('core.platform_process.verify_connected_peer', side_effect=die_after_proof):
            self.assertEqual(self.control.status(), 1)
        self.assertEqual(self.auth.token_reads, 0)
        self.assertFalse((self.root/'received.log').exists())

    def test_pid_changes_at_final_signal_check(self):
        self.start()
        original = platform_process.check_identity
        count = 0
        def identity(pid, start, executable):
            nonlocal count
            count += 1
            if count == 3:
                raise platform_process.ProcessEvidenceError('process_identity_changed')
            return original(pid, start, executable)
        with mock.patch('core.platform_process.check_identity', side_effect=identity), \
             mock.patch('core.platform_process.os.kill') as kill:
            self.assertEqual(self.control.off(), 1)
            self.assertTrue(all(call.args[1] == 0 for call in kill.call_args_list))
        self.assertEqual(count, 3)
        self.assertIsNone(self.children[0].poll())

    def test_restart_never_starts_when_release_unknown(self):
        p = self.start()
        self.auth.permit_release = False
        self.start_reaper(p)
        self.assertEqual(self.control.restart(), 1)
        self.assertEqual(len(self.children), 1)
        self.assertIn('release_unverified', self.err.getvalue())

    def test_restart_without_preflight_contract_fails_before_signal(self):
        child = self.start()
        with mock.patch.object(self.auth, 'assert_restart_supported', None), \
                mock.patch.object(platform_process, 'signal_verified') as signal:
            self.assertEqual(self.control.restart(), 1)
            self.assertIn('restart_preflight_unavailable', self.err.getvalue())
            signal.assert_not_called()
            self.assertIsNone(child.poll())

    def test_restart_stop_then_start_new_nonce(self):
        p = self.start()
        before = self.auth.records()[0]['instance_nonce']
        self.start_reaper(p)
        self.assertEqual(self.control.restart(), 0, self.err.getvalue())
        self.assertEqual(len(self.children), 2)
        self.assertNotEqual(before, self.auth.records()[0]['instance_nonce'])

    def test_port_occupied_and_unverified_start_do_not_spawn(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', self.port)); s.listen()
            self.assertEqual(self.control.on(), 1)
        self.assertFalse(self.children)
        self.auth.permit_start = False
        self.assertEqual(self.control.on(), 1)
        self.assertFalse(self.children)

    def test_windows_always_refuses(self):
        with mock.patch('core.cli.control.sys.platform', 'win32'), \
             mock.patch('core.platform_process.signal_verified') as signal_call:
            for command in ('on','off','restart','status','reload'):
                self.assertEqual(getattr(self.control,command)(), 1)
            signal_call.assert_not_called()
        self.assertFalse(self.children)
        self.assertEqual(self.auth.token_reads, 0)


if __name__ == '__main__':
    unittest.main()
