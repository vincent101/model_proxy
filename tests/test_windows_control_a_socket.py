"""Real owned loopback sockets, fake Win32 evidence, fake credentials only."""
import contextlib
import json
import socket
import threading
import unittest
from unittest.mock import Mock, patch

from core import windows_process as wp
from core.cli import windows_control as wc
from tests.test_windows_control_a_contract import ROOT, CONFIG, PATHS, RECORD
from tests.test_windows_process import FakeBackend, row4, table


class SameSocketControlTests(unittest.TestCase):
    def exercise(self, *, wrong_owner):
        received, errors = [], []
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(5)
            record = dict(RECORD, listen={"host": "127.0.0.1", "port": listener.getsockname()[1]})
            backend = FakeBackend()
            backend.exe = record["executable"]
            def serve():
                try:
                    conn, _ = listener.accept()
                    with conn:
                        conn.settimeout(5)
                        data = bytearray()
                        while b"\r\n\r\n" not in data:
                            part = conn.recv(4096)
                            if not part:
                                break
                            data.extend(part)
                        received.append(bytes(data))
                        if data:
                            body = b'{"supplies":[],"routes":[],"strategies":[],"cooldown":{}}'
                            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: "
                                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            control = wc.WindowsControl(ROOT, config_path=CONFIG)
            original_verify = wp.verify_socket_owner
            def verify(sock, pid, start, executable):
                local, peer = sock.getsockname(), sock.getpeername()
                backend.rows = wp.parse_owner_pid_table(
                    socket.AF_INET, table(row4(local=peer[0], lport=peer[1],
                                               remote=local[0], rport=local[1],
                                               pid=pid + 1 if wrong_owner else pid)))
                return original_verify(sock, pid, start, executable, backend=backend, timeout=0)
            snapshot = Mock(exists=True, contents=json.dumps({"admin_token": "fake-only"}).encode())
            def entry(active, revalidate):
                self.assertEqual(active, record)
                revalidate()
            try:
                with patch.object(wp, "verify_socket_owner", side_effect=verify), \
                     patch.object(control, "_verify_entry", side_effect=entry), \
                     patch("core.windows_secure_files.capture_target",
                           return_value=contextlib.nullcontext(snapshot)):
                    if wrong_owner:
                        with self.assertRaises(wp.ProcessEvidenceError):
                            control._request(record, PATHS, "status")
                    else:
                        control._request(record, PATHS, "status")
            finally:
                thread.join(7)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        return received

    def test_credential_sent_only_to_verified_same_connection(self):
        received = self.exercise(wrong_owner=False)
        self.assertEqual(len(received), 1)
        self.assertIn(b"X-Proxy-Admin-Token: fake-only\r\n", received[0])

    def test_foreign_connection_receives_no_bytes(self):
        self.assertEqual(self.exercise(wrong_owner=True), [b""])


if __name__ == "__main__":
    unittest.main()
