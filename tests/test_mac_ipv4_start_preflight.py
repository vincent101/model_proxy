"""Mac new-listener preflight; fake paths and local sockets only."""
import contextlib
import io
import ipaddress
import json
import os
import pty
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ISO = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if (Path(__file__).resolve().parents[1] != ISO / 'project'
        or not (ISO / '.model-proxy-isolation').is_file()
        or not Path(os.environ.get('HOME', '/invalid')).resolve().is_relative_to(ISO)):
    raise RuntimeError('isolated source and fake HOME required')
sys.path.insert(0, str(ISO / 'project'))
from core import platform_process
from core.cli import authority, control
from core.runtime_paths import PATH_KEYS


@unittest.skipUnless(sys.platform == 'darwin', 'Mac listener policy')
class MacNewListenerPreflight(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ISO, prefix='ipv4-start-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'project'
        for name in ('config', 'runtime', 'home', 'tmp'):
            (self.root / name).mkdir(parents=True, mode=0o700)
        self.config = self.root / 'config/model_proxy_config.json'
        self.paths = {key: str(self.root / 'runtime' / key) for key in PATH_KEYS}
        (self.root / 'config/runtime_paths.json').write_text(json.dumps(self.paths))
        self.runtime = self.root / 'config/runtime_paths.json'
        self.runtime.chmod(0o600)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(('127.0.0.1', 0)); self.port = probe.getsockname()[1]
        self.assertNotEqual(self.port, 18889)
        self.write_config('127.0.0.1')

    def write_config(self, host):
        cfg = {'admin_token': 'FAKE_ONLY', 'listen': {'host': host, 'port': self.port},
               'supplies': [], 'routes': [], 'strategies': []}
        if host not in ('127.0.0.1', 'localhost', '::1'):
            cfg['allow_hosts'] = ['localhost']
        self.config.write_text(json.dumps(cfg)); self.config.chmod(0o600)

    def test_new_start_rejects_ipv6_but_keeps_ipv4_and_no_dns(self):
        local = {ipaddress.ip_address('192.0.2.10'), ipaddress.ip_address('fd00::10')}
        with mock.patch.object(platform_process, '_darwin_interface_addresses', return_value=local), \
             mock.patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS forbidden')):
            for host in ('127.0.0.1', '127.0.0.2', '0.0.0.0', 'localhost', 'LOCALHOST', '192.0.2.10'):
                with self.subTest(host=host):
                    control._check_mac_new_listener({'host': host, 'port': self.port})
            for host in ('::1', '::', '0:0:0:0:0:0:0:0', 'fd00::10'):
                with self.subTest(host=host), self.assertRaises(control.ControlError) as caught:
                    control._check_mac_new_listener({'host': host, 'port': self.port})
                self.assertEqual(caught.exception.code, 'mac_ipv6_listen_unsupported')
            with self.assertRaises(control.ControlError):
                control._check_mac_new_listener({'host': 'example.invalid', 'port': self.port})
            # Observation/recovery still accepts IPv6 endpoints; no global gate.
            self.assertEqual(control._endpoint('::1', self.port)[0], socket.AF_INET6)
            control._stop_record_host('::1', self.port)

    def test_non_mac_gate_is_noop(self):
        with mock.patch.object(control.sys, 'platform', 'win32'), \
             mock.patch.object(control, '_endpoint', side_effect=AssertionError('not a Mac start')):
            control._check_mac_new_listener({'host': '::1', 'port': self.port})

    def test_real_mac_server_baseline_is_af_inet_and_cannot_bind_ipv6_literal(self):
        from core.server import ProxyHTTPServer
        self.assertEqual(ProxyHTTPServer.address_family, socket.AF_INET)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            with self.assertRaises(OSError):
                probe.bind(('::1', self.port))

    def test_on_rejects_before_initialization_or_child(self):
        self.write_config('::1')
        cli = control.MacControl(self.root, config_path=self.config, out=io.StringIO(), err=io.StringIO())
        lease = mock.Mock()
        with mock.patch.object(cli, '_prepare_initialization') as initialize, \
             mock.patch.object(control.subprocess, 'Popen') as spawn, \
             mock.patch.object(platform_process, 'signal_verified') as signal:
            with self.assertRaises(control.ControlError) as caught:
                cli._start(lease)
            self.assertEqual(caught.exception.code, 'mac_ipv6_listen_unsupported')
            initialize.assert_not_called(); spawn.assert_not_called(); signal.assert_not_called()
        self.assertFalse((self.root / '.model_proxy_runtime/install.json').exists())

    def test_initialization_rechecks_ipv6_drift_before_record_commit(self):
        # Real PTY and local listener; the input wrapper mutates only fake config.
        class Input:
            def __init__(self, stream, change): self.stream, self.change, self.calls = stream, change, 0
            def fileno(self): return self.stream.fileno()
            def readline(self, limit):
                self.calls += 1
                if self.calls == 2: self.change()
                return self.stream.readline(limit)
        master, slave = pty.openpty()
        self.addCleanup(os.close, master); self.addCleanup(os.close, slave)
        with os.fdopen(os.dup(slave), 'r') as raw_in, os.fdopen(os.dup(slave), 'w') as out:
            os.write(master, b'fake-source\nYES\n')
            inp = Input(raw_in, lambda: self.write_config('::1'))
            with mock.patch.dict(os.environ, {'HOME': str(self.root / 'home'),
                                              'TMPDIR': str(self.root / 'tmp')}, clear=True), \
                 authority.locked_observation(self.root, self.paths, self.config) as obs, \
                 mock.patch.object(authority, 'initialize_installation') as initialize:
                with self.assertRaises(control.ControlError) as caught:
                    obs.initialize_interactively(inp, out)
                self.assertEqual(caught.exception.code, 'mac_ipv6_listen_unsupported')
                initialize.assert_not_called()
        self.assertFalse((self.root / '.model_proxy_runtime/install.json').exists())


if __name__ == '__main__': unittest.main()
