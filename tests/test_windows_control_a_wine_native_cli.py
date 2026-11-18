"""Real Windows CLI calls in one fake project; only own children may be opened."""
import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


def diagnostic(*, fresh_restart=False):
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="mp-cli-native-") as temp:
        root = Path(temp) / "项目 space"
        root.mkdir()
        shutil.copytree(source / "core", root / "core",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (root / "config").mkdir()
        runtime = root / "runtime"
        runtime.mkdir()
        home = root / "home"
        home.mkdir()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        config = root / "config" / "fake.json"
        config.write_text(json.dumps({"listen": {"host": "127.0.0.1", "port": port},
                                      "admin_token": "test-only-admin",
                                      "supplies": [], "routes": [], "strategies": []}),
                          encoding="utf-8")
        names = ("log", "totals", "lock", "pid", "ensure_log", "start_lock")
        defaults = {"log": ".model_proxy.log", "totals": ".model_proxy_totals.json",
                    "lock": "/tmp/model_proxy.lock", "pid": "/tmp/model_proxy.pid",
                    "ensure_log": "/tmp/model_proxy_ensure.log",
                    "start_lock": "/tmp/model_proxy_start.lock"}
        (root / "config" / "runtime_paths.json").write_text(json.dumps(
            defaults if fresh_restart else {name: str(runtime / name) for name in names}), encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if not key.startswith("MODEL_PROXY_")}
        env.update({"HOME": str(home), "USERPROFILE": str(home), "LOCALAPPDATA": str(home),
                    "APPDATA": str(home), "MODEL_PROXY_CONFIG": str(config)})
        sys.path.insert(0, str(root))
        with patch.dict(os.environ, env, clear=True):
            from core.cli import dispatch
            from core import windows_process
            owned = []
            real_popen = subprocess.Popen
            real_open = windows_process.open_verified_process
            real_state = windows_process.recorded_process_state
            real_peer = windows_process.verify_socket_owner
            def spawn(*args, **kwargs):
                child = real_popen(*args, **kwargs)
                owned.append(child)
                return child
            def require_owned(pid):
                if not any(child.pid == pid for child in owned):
                    raise AssertionError("test forbids observing or stopping non-owned process")
            def open_owned(pid, *args, **kwargs):
                require_owned(pid)
                return real_open(pid, *args, **kwargs)
            def state_owned(pid, *args, **kwargs):
                require_owned(pid)
                return real_state(pid, *args, **kwargs)
            def peer_owned(sock, pid, *args, **kwargs):
                require_owned(pid)
                return real_peer(sock, pid, *args, **kwargs)
            results, cleanup = {}, []
            try:
                with patch.object(subprocess, "Popen", side_effect=spawn), \
                     patch.object(windows_process, "open_verified_process", side_effect=open_owned), \
                     patch.object(windows_process, "recorded_process_state", side_effect=state_owned), \
                     patch.object(windows_process, "verify_socket_owner", side_effect=peer_owned):
                    commands = ("status", "off", "restart") if fresh_restart else (
                        "on", "status", "reload", "off", "restart")
                    for command in commands:
                        output, error = io.StringIO(), io.StringIO()
                        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                            code = dispatch.main([command], root=root)
                        results[command] = {"exit_code": code, "stdout": output.getvalue(),
                                            "stderr": error.getvalue()}
            finally:
                for child in owned:
                    try:
                        if child.poll() is None:
                            child.kill()  # the original Popen handle, created above
                        child.wait(timeout=10)
                        cleanup.append("owned child exited and reaped")
                    except Exception:
                        cleanup.append("owned child cleanup unconfirmed")
                sys.path.remove(str(root))
            return {"environment": "Whisky/Wine, not native Windows",
                    "commands": results, "owned_children": len(owned), "cleanup": cleanup,
                    "state_exists": (root / ".model_proxy_runtime").exists(),
                    "default_runtime_exists": (home / "model_proxy" / "runtime").exists()}


if __name__ == "__main__":
    if (os.name != "nt" or len(sys.argv) not in (2, 3)
            or len(sys.argv) == 3 and sys.argv[2] != "--fresh-restart"
            or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode)):
        raise SystemExit("requires Windows Python -I -S -B, a fresh result path and optional --fresh-restart")
    destination = Path(sys.argv[1])
    if destination.exists():
        raise SystemExit("result exists")
    destination.write_text(json.dumps(diagnostic(fresh_restart=len(sys.argv) == 3), ensure_ascii=False),
                           encoding="utf-8")
