"""Whisky/native smoke: only children and loopback endpoints created here.

Run under Windows Python -I -S -B with one fresh result JSON path. A failed
native capability is reported as a failed test, never replaced with a fake.
"""
import io
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import discovery, windows_process as wp
from core.cli.windows_command_line import (parse_windows_argv, verify_shared_launch_argv,
                                           PowerShellWmiReader)


@unittest.skipUnless(os.name == "nt", "requires Windows Python")
class NativeOwnedChildTests(unittest.TestCase):
    def spawn(self, program):
        child = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c", program],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        def cleanup():
            if child.poll() is None:
                child.kill()  # only the Popen child created in this test
            child.communicate(timeout=15)
        self.addCleanup(cleanup)
        return child

    def identity(self, child):
        start = discovery.process_start_identity(child.pid)
        image = discovery.process_executable(child.pid)
        self.assertIsNotNone(start)
        self.assertIsNotNone(image)
        return start, image

    def test_same_handle_stops_only_owned_child(self):
        child = self.spawn("import time; time.sleep(60)")
        start, image = self.identity(child)
        with wp.open_verified_process(child.pid, start, image, terminate=True) as process:
            # The test harness knows this Popen child; this is not a test of
            # the product's WMI service-entry authorization.
            process.terminate_checked(lambda identity: identity.pid == child.pid, timeout=10)
        self.assertIsNotNone(child.wait(timeout=10))

    def test_real_connected_peer_owner(self):
        child = self.spawn(
            "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); "
            "s.listen(1); print(s.getsockname()[1],flush=True); c,_=s.accept(); time.sleep(30)")
        line = queue.Queue()
        threading.Thread(target=lambda: line.put(child.stdout.readline()), daemon=True).start()
        port = int(line.get(timeout=15))
        start, image = self.identity(child)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            with wp.verify_socket_owner(sock, child.pid, start, image, timeout=4) as peer:
                peer.revalidate()

    def test_windows_argv_parser(self):
        entry = r"C:\test project\core\cli\model_proxy.py"
        config = r"C:\test project\fake config.json"
        argv = [r"C:\Python\python.exe", "-B", entry, "--config", config, "--port", "12345"]
        parsed = parse_windows_argv(subprocess.list2cmdline(argv))
        verify_shared_launch_argv(parsed, entry=entry, config=config, port=12345)

    def test_actual_command_line_observer_for_owned_child(self):
        child = self.spawn("import time; time.sleep(60)")
        start, image = self.identity(child)
        with wp.open_verified_process(child.pid, start, image) as process:
            process.revalidate()
            # 只验证「读取能力」本身；CI 里 PowerShell 首次启动 + WMI 查询在并发下
            # 会超过 8s（生产默认 5s 面向常态机器），这里给足预算以免误报。
            observed = PowerShellWmiReader(timeout=25).read_command_line(child.pid)
            process.revalidate()
        self.assertIn("-c", parse_windows_argv(observed))


if __name__ == "__main__":
    if len(sys.argv) != 2 or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit("requires -I -S -B and a fresh result path")
    result_path = Path(sys.argv[1])
    if result_path.exists():
        raise SystemExit("result exists")
    output = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(NativeOwnedChildTests)
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    result_path.write_text(json.dumps({"tests": result.testsRun,
                                      "failures": len(result.failures),
                                      "errors": len(result.errors),
                                      "skipped": len(result.skipped),
                                      "output": output.getvalue()}, ensure_ascii=False),
                           encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)
