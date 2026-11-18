"""Bounded Wine diagnostic: fake project, own listener, no product bypasses."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


TRACE = r'''
import contextlib, json, sys, time
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from core.cli import dispatch, windows_control
from core import discovery, windows_acl, windows_paths, platform_lock, windows_process
command = sys.argv[2] if len(sys.argv) > 2 else 'status'
class NoSpawn:
    def __getattr__(self, name):
        return getattr(__import__('subprocess'), name)
    def Popen(self, *args, **kwargs):
        raise AssertionError('test forbids spawning on a foreign port')
windows_control.subprocess = NoSpawn()
def no_process(*args, **kwargs):
    raise AssertionError('test forbids observing or stopping a foreign process')
windows_process.open_verified_process = no_process
windows_process.recorded_process_state = no_process
windows_process.verify_socket_owner = no_process
stats = {}
def wrap(module, name):
    original = getattr(module, name)
    def call(*a, **kw):
        t = time.monotonic()
        verbose = name in ('_configuration', 'inspect_runtime_identities', 'acquire_lock')
        if verbose: print("BEGIN " + name, flush=True)
        try:
            return original(*a, **kw)
        finally:
            elapsed = time.monotonic() - t
            count, total = stats.get(name, (0, 0))
            stats[name] = (count + 1, total + elapsed)
            if verbose: print("END " + name + " %.3f" % elapsed, flush=True)
    setattr(module, name, call)
for module, name in ((windows_control.WindowsControl, '_configuration'),
                     (discovery, 'inspect_runtime_identities'),
                     (windows_paths, 'inspect'),
                     (windows_acl, 'read_stable_evidence'),
                     (platform_lock, 'acquire_lock')):
    wrap(module, name)
original = windows_control.WindowsControl._locked
@contextlib.contextmanager
def locked(*a, **kw):
    t = time.monotonic()
    print('BEGIN _locked', flush=True)
    try:
        with original(*a, **kw):
            print('ACQUIRED _locked', flush=True)
            yield
    finally:
        print('END _locked %.3f' % (time.monotonic() - t), flush=True)
windows_control.WindowsControl._locked = locked
t = time.monotonic()
print('BEGIN dispatch', flush=True)
rc = dispatch.main([command], root=root)
print('END dispatch %.3f rc=%s' % (time.monotonic() - t, rc), flush=True)
print('TRACE_STATS ' + json.dumps(stats), flush=True)
raise SystemExit(rc)
'''


def diagnostic():
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="mp-a-timeout-") as temp:
        root = Path(temp) / "fake 项目"
        root.mkdir()
        shutil.copytree(source / "core", root / "core",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (root / "config").mkdir()
        (root / "runtime").mkdir()
        (root / "home").mkdir()
        state = root / ".model_proxy_runtime"
        state.mkdir()
        (state / "control.lock").write_bytes(b"")
        stop, received = threading.Event(), []
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(4)
            listener.settimeout(.2)
            port = listener.getsockname()[1]
            def serve():
                while not stop.is_set():
                    try:
                        conn, _ = listener.accept()
                    except socket.timeout:
                        continue
                    with conn:
                        conn.settimeout(.5)
                        try:
                            received.append(conn.recv(8192))
                        except (OSError, socket.timeout):
                            received.append(b"")
            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            config = root / "config" / "fake.json"
            config.write_text(json.dumps({"listen": {"host": "127.0.0.1", "port": port},
                                          "admin_token": "timeout-fake-admin"}), encoding="utf-8")
            keys = ("log", "totals", "lock", "pid", "ensure_log", "start_lock")
            (root / "config" / "runtime_paths.json").write_text(
                json.dumps({key: str(root / "runtime" / key) for key in keys}), encoding="utf-8")
            env = {key: value for key, value in os.environ.items() if not key.startswith("MODEL_PROXY_")}
            env.update({"HOME": str(root / "home"), "USERPROFILE": str(root / "home"),
                        "LOCALAPPDATA": str(root / "home"), "APPDATA": str(root / "home"),
                        "MODEL_PROXY_CONFIG": str(config)})
            trace = root / "trace.py"
            trace.write_text(TRACE, encoding="utf-8")
            outputs = {}
            try:
                for label, args in (
                    ("direct_no_guard", [sys.executable, "-I", "-S", "-B",
                                         str(root / "core" / "cli" / "main.py"), "status"]),
                    ("direct_off_no_guard", [sys.executable, "-I", "-S", "-B",
                                             str(root / "core" / "cli" / "main.py"), "off"]),
                    ("timed_wrappers", [sys.executable, "-I", "-S", "-B", str(trace), str(root)]),
                    ("timed_restart_guard", [sys.executable, "-I", "-S", "-B",
                                             str(trace), str(root), "restart"]),
                ):
                    started = time.monotonic()
                    child = subprocess.Popen(args, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    timed_out = False
                    try:
                        stdout, stderr = child.communicate(timeout=20)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        child.kill()  # only the directly created test Popen
                        stdout, stderr = child.communicate(timeout=10)
                    outputs[label] = {"elapsed": round(time.monotonic() - started, 3),
                                      "timed_out": timed_out, "exit_code": child.returncode,
                                      "stdout": stdout.decode("utf-8", "replace"),
                                      "stderr": stderr.decode("utf-8", "replace")}
            finally:
                stop.set()
                worker.join(5)
            outputs["listener_thread_stopped"] = not worker.is_alive()
            outputs["foreign_connections"] = len(received)
            outputs["admin_sent"] = any(b"timeout-fake-admin" in value for value in received)
            return outputs


if __name__ == "__main__":
    if os.name != "nt" or len(sys.argv) != 2 or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit("requires Windows Python -I -S -B and a fresh result path")
    result = Path(sys.argv[1])
    if result.exists():
        raise SystemExit("result exists")
    result.write_text(json.dumps(diagnostic(), ensure_ascii=False), encoding="utf-8")
