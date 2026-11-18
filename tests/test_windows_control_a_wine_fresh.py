"""Bounded Wine first-run status/off on a fake project, without OS substitutes."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def diagnostic():
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="mp-a-fresh-") as temp:
        root = Path(temp) / "fake project"
        root.mkdir()
        shutil.copytree(source / "core", root / "core",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (root / "config").mkdir()
        home = root / "home"
        local = home / "Local"
        local.mkdir(parents=True)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        config = root / "config" / "fake.json"
        config.write_text(json.dumps({"listen": {"host": "127.0.0.1", "port": port},
                                      "admin_token": "fresh-test-only"}), encoding="utf-8")
        defaults = {"log": ".model_proxy.log", "totals": ".model_proxy_totals.json",
                    "lock": "/tmp/model_proxy.lock", "pid": "/tmp/model_proxy.pid",
                    "ensure_log": "/tmp/model_proxy_ensure.log",
                    "start_lock": "/tmp/model_proxy_start.lock"}
        (root / "config" / "runtime_paths.json").write_text(json.dumps(defaults), encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if not key.startswith("MODEL_PROXY_")}
        env.update({"HOME": str(home), "USERPROFILE": str(home), "LOCALAPPDATA": str(local),
                    "APPDATA": str(home), "MODEL_PROXY_CONFIG": str(config)})
        targets = {"state": root / ".model_proxy_runtime",
                   "default_parent": local / "model_proxy",
                   "log": root / ".model_proxy.log", "totals": root / ".model_proxy_totals.json"}
        before = {name: value.exists() for name, value in targets.items()}
        results = {}
        for command in ("status", "off"):
            started = time.monotonic()
            child = subprocess.Popen([sys.executable, "-I", "-S", "-B",
                                      str(root / "core" / "cli" / "main.py"), command],
                                     cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                stdout, stderr = child.communicate(timeout=20)
                timed_out = False
            except subprocess.TimeoutExpired:
                child.kill()  # only the directly created test CLI
                stdout, stderr = child.communicate(timeout=10)
                timed_out = True
            results[command] = {"elapsed": round(time.monotonic() - started, 3),
                                "exit_code": child.returncode, "timed_out": timed_out,
                                "stdout": stdout.decode("utf-8", "replace"),
                                "stderr": stderr.decode("utf-8", "replace")}
        after = {name: value.exists() for name, value in targets.items()}
        return {"environment": "Whisky/Wine, not native Windows",
                "before": before, "after": after, "commands": results}


if __name__ == "__main__":
    if os.name != "nt" or len(sys.argv) != 2 or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit("requires Windows Python -I -S -B and a fresh result path")
    destination = Path(sys.argv[1])
    if destination.exists():
        raise SystemExit("result exists")
    destination.write_text(json.dumps(diagnostic(), ensure_ascii=False), encoding="utf-8")
