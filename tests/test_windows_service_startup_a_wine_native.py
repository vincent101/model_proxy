"""Real product startup in one fake project, with no ACL/OS substitutes."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path


def diagnostic():
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="mp-a-native-") as temp:
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
        (root / "config" / "runtime_paths.json").write_text(
            json.dumps({name: str(runtime / name) for name in names}), encoding="utf-8")
        env = os.environ.copy()
        for key in tuple(env):
            if key.startswith("MODEL_PROXY_"):
                del env[key]
        env.update({"HOME": str(home), "USERPROFILE": str(home), "LOCALAPPDATA": str(home),
                    "APPDATA": str(home), "MODEL_PROXY_CONFIG": str(config)})
        entry = root / "core" / "cli" / "model_proxy.py"
        child = subprocess.Popen([sys.executable, "-I", "-S", "-B", str(entry),
                                  "--config", str(config), "--port", str(port)],
                                 cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output, error = child.communicate(timeout=15)
            completion = "owned service child exited and reaped"
        except subprocess.TimeoutExpired:
            # Popen holds the original handle of exactly the child created here.
            child.kill()
            output, error = child.communicate(timeout=10)
            completion = "owned service child still running; terminated and reaped"
        return {"environment": "Whisky/Wine, not native Windows",
                "completion": completion, "exit_code": child.returncode,
                "stdout": output.decode("utf-8", "replace"),
                "stderr": error.decode("utf-8", "replace")}


if __name__ == "__main__":
    if os.name != "nt" or len(sys.argv) != 2 or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit("requires Windows Python -I -S -B and a fresh result path")
    destination = Path(sys.argv[1])
    if destination.exists():
        raise SystemExit("result exists")
    destination.write_text(json.dumps(diagnostic(), ensure_ascii=False), encoding="utf-8")
