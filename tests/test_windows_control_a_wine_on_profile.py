"""Bounded Wine on-preflight profiler; never spawns a service process."""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


PROFILE = r'''
import contextlib, json, subprocess, sys, time
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from core import discovery, platform_lock, windows_acl
from core.cli import dispatch, windows_control
counts = {'acl': 0}
original_acl = windows_acl.read_stable_evidence
def acl(*a, **kw):
    counts['acl'] += 1
    if counts['acl'] % 500 == 0:
        print('ACL_COUNT', counts['acl'], flush=True)
    return original_acl(*a, **kw)
windows_acl.read_stable_evidence = acl
def wrap(module, name):
    static = isinstance(vars(module).get(name), staticmethod) if isinstance(module, type) else False
    original = getattr(module, name)
    def call(*a, **kw):
        start = time.monotonic()
        before = counts['acl']
        print('BEGIN', name, flush=True)
        try:
            return original(*a, **kw)
        finally:
            print('END', name, 'seconds=%.3f' % (time.monotonic()-start),
                  'acl=%d' % (counts['acl']-before), flush=True)
    setattr(module, name, staticmethod(call) if static else call)
for module, name in (
    (windows_control.WindowsControl, '_configuration'),
    (windows_control.WindowsControl, '_active'),
    (windows_control.WindowsControl, '_probe_locks'),
    (windows_control.WindowsControl, '_probe_start_locks'),
    (windows_control.WindowsControl, '_probe_port'),
    (discovery, 'prepare_windows_runtime_directories'),
    (discovery, 'inspect_runtime_identities'),
    (discovery, '_windows_open_preflight'),
    (platform_lock, 'acquire_lock'),
):
    wrap(module, name)
original_locked = windows_control.WindowsControl._locked
@contextlib.contextmanager
def locked(*a, **kw):
    start = time.monotonic()
    print('BEGIN _locked', flush=True)
    try:
        with original_locked(*a, **kw) as held:
            print('ACQUIRED _locked', flush=True)
            yield held
    finally:
        print('END _locked seconds=%.3f' % (time.monotonic()-start), flush=True)
windows_control.WindowsControl._locked = locked
class NoSpawn:
    def __getattr__(self, name): return getattr(subprocess, name)
    def Popen(self, *a, **kw):
        print('SPAWN_BOUNDARY_REACHED', flush=True)
        raise RuntimeError('test prohibits starting a service')
windows_control.subprocess = NoSpawn()
start = time.monotonic()
print('BEGIN dispatch_on', flush=True)
rc = dispatch.main(['on'], root=root)
print('END dispatch_on seconds=%.3f rc=%s acl=%d' %
      (time.monotonic()-start, rc, counts['acl']), flush=True)
'''


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def diagnostic():
    source = Path(__file__).resolve().parents[1]
    names = ("core/windows_paths.py", "core/discovery.py", "core/platform_lock.py",
             "core/cli/windows_control.py", "core/server.py")
    before = {name: digest(source / name) for name in names}
    with tempfile.TemporaryDirectory(prefix="mp-minimal-b-") as temp:
        root = Path(temp)
        project = root / "project"
        project.mkdir()
        shutil.copytree(source / "core", project / "core",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        staged = {name: digest(project / name) for name in names}
        after = {name: digest(source / name) for name in names}
        result = {"environment": "Whisky/Wine; fixture uses precreated unprotected private objects",
                  "source_changed_during_copy": before != after or before != staged}
        (project / "config").mkdir()
        helper = source / "tests" / "wine_private_fixture.py"
        # This helper creates only objects under this newly made fake project.
        fixture = subprocess.Popen([sys.executable, "-I", "-S", "-B", str(helper), str(project)],
                                   cwd=str(project), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = fixture.communicate(timeout=90)
            timed_out = False
        except subprocess.TimeoutExpired:
            fixture.kill()  # only this directly created test helper
            out, err = fixture.communicate(timeout=10)
            timed_out = True
        result["fixture"] = {"exit_code": fixture.returncode, "timed_out": timed_out,
                             "stderr": err.decode("utf-8", "replace")}
        if timed_out or fixture.returncode != 0:
            return result
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        config = project / "config" / "fake.json"
        config.write_text(json.dumps({"listen": {"host": "127.0.0.1", "port": port},
                                      "admin_token": "fake-only", "supplies": [],
                                      "routes": [], "strategies": []}), encoding="utf-8")
        keys = ("log", "totals", "lock", "pid", "ensure_log", "start_lock")
        (project / "config" / "runtime_paths.json").write_text(
            json.dumps({key: str(project / "runtime" / key) for key in keys}), encoding="utf-8")
        home = root / "fake-home"
        home.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith("MODEL_PROXY_")}
        env.update({"HOME": str(home), "USERPROFILE": str(home), "LOCALAPPDATA": str(home),
                    "APPDATA": str(home), "MODEL_PROXY_CONFIG": str(config)})
        script = root / "profile.py"
        script.write_text(PROFILE, encoding="utf-8")
        started = time.monotonic()
        worker = subprocess.Popen([sys.executable, "-I", "-S", "-B", str(script), str(project)],
                                  cwd=str(project), env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = worker.communicate(timeout=60)
            timed_out = False
        except subprocess.TimeoutExpired:
            worker.kill()  # NoSpawn above forbids any service child.
            out, err = worker.communicate(timeout=10)
            timed_out = True
        result["profile"] = {"elapsed": round(time.monotonic() - started, 3),
                             "exit_code": worker.returncode, "timed_out": timed_out,
                             "stdout": out.decode("utf-8", "replace"),
                             "stderr": err.decode("utf-8", "replace")}
        result["service_spawn_allowed"] = False
        result["source_changed_during_profile"] = after != {name: digest(source / name) for name in names}
        return result


if __name__ == "__main__":
    if os.name != "nt" or len(sys.argv) != 2 or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit("requires Windows Python -I -S -B and a fresh result path")
    destination = Path(sys.argv[1])
    if destination.exists():
        raise SystemExit("result exists")
    destination.write_text(json.dumps(diagnostic(), ensure_ascii=False), encoding="utf-8")
