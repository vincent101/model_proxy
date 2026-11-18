"""Shared Windows control launch checks with fake sockets, processes and config."""
import contextlib
import copy
import io
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from core import windows_process as wp
from core.cli import windows_command_line as cmd
from core.cli import windows_control as wc
from tests.test_windows_control_a_contract import CONFIG, PATHS, RECORD, ROOT
from tests.test_windows_process import FakeBackend, FakeSocket, row4, row6, table
from tests.test_windows_process_command_line import DIRECTORY, FakeProcess, payload


class FakeConnectedSocket(FakeSocket):
    created = []

    def __init__(self, *args):
        super().__init__()
        self.fail_close = False
        self.created.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, endpoint):
        if endpoint != self.peer:
            raise AssertionError("unexpected fake endpoint")

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        super().close()
        if self.fail_close:
            raise OSError("fake socket close failure")


class SharedLaunchSecurityTests(unittest.TestCase):
    @contextlib.contextmanager
    def harness(self):
        active = copy.deepcopy(RECORD)
        active["listen"]["port"] = 19431
        backend = FakeBackend()
        backend.exe = active["executable"]
        controller = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO(), err=io.StringIO())
        reader = Mock()
        reader.read_command_line.return_value = "fake observed command line"
        argv = (active["executable"], "-B", active["entry"], "--config",
                active["config"], "--port", str(active["listen"]["port"]))
        snapshot = Mock(exists=True, contents=b'{"admin_token":"fake-only"}')
        response = Mock(status=200, length=0)
        response.read.return_value = b'{"ok":true}'
        original_verify = wp.verify_socket_owner
        original_reader = cmd.PowerShellWmiReader
        checked = []

        def verify(sock, pid, start, executable):
            checked.append(sock)
            return original_verify(sock, pid, start, executable, backend=backend, timeout=0)

        FakeConnectedSocket.created = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(wc.socket, "socket", FakeConnectedSocket))
            stack.enter_context(patch.object(wp, "socket_open_without_data"))
            stack.enter_context(patch.object(wp, "verify_socket_owner", side_effect=verify))
            reader_factory = stack.enter_context(
                patch.object(cmd, "PowerShellWmiReader", return_value=reader))
            parser = stack.enter_context(patch.object(cmd, "parse_windows_argv", return_value=argv))
            active_read = stack.enter_context(patch.object(controller, "_active", return_value=active))
            capture = stack.enter_context(patch(
                "core.windows_secure_files.capture_target",
                return_value=contextlib.nullcontext(snapshot)))
            http = stack.enter_context(patch.object(wc.http.client, "HTTPResponse", return_value=response))
            yield SimpleNamespace(
                active=active, backend=backend, controller=controller, reader=reader,
                argv=argv, parser=parser, active_read=active_read, capture=capture,
                checked=checked, http=http, snapshot=snapshot,
                reader_factory=reader_factory, original_reader=original_reader,
            )

    def assert_denied_without_send(self, h):
        sock = FakeConnectedSocket.created[-1]
        self.assertEqual(sock.sent, [])
        self.assertTrue(sock.closed)
        self.assertEqual(h.backend.opened, [4321])
        self.assertEqual(h.backend.closed, [100])

    def test_shared_entry_verified_twice_before_send_on_same_socket(self):
        with self.harness() as h:
            h.controller._request(h.active, PATHS, "reload")
            sock = FakeConnectedSocket.created[-1]
            self.assertEqual(h.checked, [sock])
            h.http.assert_called_once_with(sock)
            self.assertEqual(h.reader.read_command_line.call_args_list, [call(4321), call(4321)])
            self.assertEqual(h.parser.call_count, 2)
            self.assertEqual(len(sock.sent), 1)
            self.assertIn(b"X-Proxy-Admin-Token: fake-only\r\n", sock.sent[0])
            self.assertTrue(sock.closed)
            self.assertEqual(h.backend.opened, [4321])
            self.assertEqual(h.backend.closed, [100])

    def test_same_owner_wrong_peer_endpoint_never_reads_credentials_or_sends(self):
        for kind, command in ((kind, command) for kind in ("port", "ip", "family")
                              for command in ("status", "reload")):
            with self.subTest(kind=kind, command=command), self.harness() as h:
                if kind == "family":
                    h.backend.rows = wp.parse_owner_pid_table(
                        socket.AF_INET6, table(row6(lport=19431, rport=50123)))
                else:
                    h.backend.rows = wp.parse_owner_pid_table(
                        socket.AF_INET, table(row4(
                            local="127.0.0.2" if kind == "ip" else "127.0.0.1",
                            lport=19432 if kind == "port" else 19431, rport=50123)))

                def redirected(sock, requested):
                    self.assertEqual(requested, ("127.0.0.1", 19431))
                    if kind == "family":
                        sock.family = socket.AF_INET6
                        sock.local = ("::1", 50123, 0, 0)
                        sock.peer = ("::1", 19431, 0, 0)
                    elif kind == "ip":
                        sock.peer = ("127.0.0.2", 19431)
                    else:
                        sock.peer = ("127.0.0.1", 19432)

                with patch.object(FakeConnectedSocket, "connect", new=redirected):
                    with self.assertRaises(wc.WindowsControlError) as caught:
                        h.controller._request(h.active, PATHS, command)
                self.assertEqual(caught.exception.code, "record_endpoint_changed")
                self.assert_denied_without_send(h)
                h.reader.read_command_line.assert_not_called()
                h.capture.assert_not_called()
                h.http.assert_not_called()

    def test_matching_wildcard_and_local_lan_endpoints_remain_usable(self):
        for kind in ("ipv4-wildcard", "ipv6-wildcard", "local-lan"):
            with self.subTest(kind=kind), self.harness() as h:
                if kind == "ipv6-wildcard":
                    h.active["listen"]["host"] = "::"
                    h.backend.rows = wp.parse_owner_pid_table(
                        socket.AF_INET6, table(row6(lport=19431, rport=50123)))
                elif kind == "local-lan":
                    h.active["listen"]["host"] = "192.0.2.10"
                    h.backend.rows = wp.parse_owner_pid_table(
                        socket.AF_INET, table(row4(local="192.0.2.10", lport=19431,
                                                  remote="192.0.2.10", rport=50123)))
                else:
                    h.active["listen"]["host"] = "0.0.0.0"

                def connect_target(sock, requested):
                    if kind == "ipv6-wildcard":
                        self.assertEqual(requested, ("::1", 19431))
                        sock.family = socket.AF_INET6
                        sock.local = ("::1", 50123, 0, 0)
                        sock.peer = ("::1", 19431, 0, 0)
                    elif kind == "local-lan":
                        self.assertEqual(requested, ("192.0.2.10", 19431))
                        sock.local = ("192.0.2.10", 50123)
                        sock.peer = ("192.0.2.10", 19431)
                    else:
                        self.assertEqual(requested, ("127.0.0.1", 19431))

                with patch.object(FakeConnectedSocket, "connect", new=connect_target):
                    h.controller._request(h.active, PATHS, "reload")
                sock = FakeConnectedSocket.created[-1]
                self.assertEqual(h.checked, [sock])
                self.assertEqual(len(sock.sent), 1)
                self.assertIn(b"X-Proxy-Admin-Token: fake-only\r\n", sock.sent[0])
                self.assertEqual(h.backend.closed, [100])

    def test_real_reader_with_fake_helpers_is_checked_twice_before_send(self):
        with self.harness() as h:
            processes = [FakeProcess(payload("observed")) for _ in range(2)]
            pending = list(processes)
            h.reader_factory.side_effect = lambda: h.original_reader(
                timeout=1, _process_factory=lambda *args, **kwargs: pending.pop(0),
                _directory_provider=lambda: DIRECTORY)
            h.controller._request(h.active, PATHS, "reload")
            sock = FakeConnectedSocket.created[-1]
            self.assertEqual(pending, [])
            self.assertEqual([p.stdin.writes for p in processes], [[b"4321\n"], [b"4321\n"]])
            self.assertEqual(h.checked, [sock])
            self.assertEqual(len(sock.sent), 1)
            self.assertEqual(h.backend.closed, [100])

    def test_process_change_while_real_reader_consumes_fake_helper_is_denied(self):
        with self.harness() as h:
            class MutatingStream(io.BytesIO):
                def read(self, size=-1):
                    h.backend.ticks += 1
                    return super().read(size)

            process = FakeProcess(b"")
            process.stdout = MutatingStream(payload("observed"))
            h.reader_factory.side_effect = lambda: h.original_reader(
                timeout=1, _process_factory=lambda *args, **kwargs: process,
                _directory_provider=lambda: DIRECTORY)
            with self.assertRaises(wp.ProcessEvidenceError):
                h.controller._request(h.active, PATHS, "reload")
            self.assert_denied_without_send(h)

    def test_unreadable_command_line_fails_before_sending_on_either_lookup(self):
        for lookup in (1, 2):
            with self.subTest(lookup=lookup), self.harness() as h:
                failure = cmd.CommandLineObservationError("command_line_unavailable")
                h.reader.read_command_line.side_effect = (
                    [failure] if lookup == 1 else ["observed", failure])
                with self.assertRaises(cmd.CommandLineObservationError):
                    h.controller._request(h.active, PATHS, "reload")
                self.assert_denied_without_send(h)
                self.assertEqual(h.capture.call_count, lookup - 1)

    def test_wrong_entry_config_port_or_extra_option_never_sends(self):
        for lookup in (1, 2):
            for kind in ("entry", "config", "port", "extra"):
                with self.subTest(lookup=lookup, kind=kind), self.harness() as h:
                    bad = list(h.argv)
                    if kind == "entry":
                        bad[2] = ROOT + r"\other.py"
                    elif kind == "config":
                        bad[4] = ROOT + r"\other.json"
                    elif kind == "port":
                        bad[6] = "19432"
                    else:
                        bad.append("--extra")
                    h.parser.side_effect = ([tuple(bad)] if lookup == 1
                                            else [h.argv, tuple(bad)])
                    with self.assertRaises(cmd.CommandLineObservationError):
                        h.controller._request(h.active, PATHS, "reload")
                    self.assert_denied_without_send(h)

    def test_identity_or_record_change_during_lookup_never_sends(self):
        for lookup in (1, 2):
            for kind in ("creation", "exit", "image", "owner", "socket", "record"):
                with self.subTest(lookup=lookup, kind=kind), self.harness() as h:
                    calls = 0

                    def read(_pid):
                        nonlocal calls
                        calls += 1
                        if calls == lookup:
                            if kind == "creation":
                                h.backend.ticks += 1
                            elif kind == "exit":
                                h.backend.alive = False
                            elif kind == "image":
                                h.backend.exe = r"C:\other\python.exe"
                            elif kind == "owner":
                                h.backend.rows = wp.parse_owner_pid_table(
                                    FakeConnectedSocket.family, table(row4(pid=7777)))
                            elif kind == "socket":
                                FakeConnectedSocket.created[-1].peer = ("127.0.0.1", 19432)
                            else:
                                changed = copy.deepcopy(h.active)
                                changed["instance_nonce"] = "b" * 32
                                h.active_read.return_value = changed
                        return "fake observed command line"

                    h.reader.read_command_line.side_effect = read
                    with self.assertRaises((wp.ProcessEvidenceError, wc.WindowsControlError)):
                        h.controller._request(h.active, PATHS, "reload")
                    self.assert_denied_without_send(h)

    def test_process_release_failure_after_lookup_failure_never_sends(self):
        with self.harness() as h:
            h.backend.fail_close = True
            h.reader.read_command_line.side_effect = cmd.CommandLineObservationError(
                "command_line_unavailable")
            with self.assertRaises(wp.ProcessEvidenceError) as caught:
                h.controller._request(h.active, PATHS, "reload")
            self.assertEqual(caught.exception.code, "process_release_unknown")
            self.assert_denied_without_send(h)

    def test_socket_close_failure_still_releases_process(self):
        with self.harness() as h:
            def fail_lookup(_pid):
                FakeConnectedSocket.created[-1].fail_close = True
                raise cmd.CommandLineObservationError("command_line_unavailable")

            h.reader.read_command_line.side_effect = fail_lookup
            with self.assertRaises(OSError):
                h.controller._request(h.active, PATHS, "reload")
            self.assert_denied_without_send(h)


if __name__ == "__main__":
    unittest.main()
