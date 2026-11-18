"""Real-Git migration contract tests; every project, home and account is fake.

Run on macOS only: python3 -m unittest tests.test_macos_switch_worktree -v
These tests are NOT a production safety proof. A fixture-only interpreter shim
loads the copied product module and patches its fixed lock/base paths, the
minimum production OID, /dev/tty and the process listing. The product shell
entries and product transaction/Git code otherwise run unchanged. Checkpoints
are monkeypatches local to that same disposable child, not product switches.
The sandbox denies access to the real lock, network, signals to other
processes, and writes outside the fixture. A full success control is required
before accepting any negative test: blanket refusal is never credited.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1]
SCRIPTS = ('macos_switch.sh', 'macos_rollback.sh', 'macos_switch_impl.py')
COLLISION = Path('docs/designs/2026-11-12-model-proxy统一内核与原生Windows-CLI设计.md')
A = b'fake original untracked A\n'
B = b'fake target tracked B\n'
OLD_PATHS = {'log': '.model_proxy.log', 'totals': '.model_proxy_totals.json',
             'lock': '/tmp/model_proxy.lock', 'pid': '/tmp/model_proxy.pid',
             'ensure_log': '/tmp/model_proxy_ensure.log',
             'start_lock': '/tmp/model_proxy_start.lock'}
NEW_PATHS = {'log': '.runtime/model_proxy.log', 'totals': '.runtime/model_proxy_totals.json',
             'lock': '.runtime/model_proxy.lock', 'pid': '.runtime/model_proxy.pid',
             'ensure_log': '.runtime/model_proxy_ensure.log',
             'start_lock': '.runtime/model_proxy_start.lock'}

DRIVER = r'''import builtins, importlib.util, io, json, os, subprocess, sys, time
from pathlib import Path
specpath = Path(os.environ.pop("FAKE_DRIVER_SPEC"))
cfg = json.loads(specpath.read_text())
assert str(specpath.resolve()).startswith(cfg["base"] + "/")
assert all(str(Path(cfg[key]).resolve()).startswith(cfg["base"] + "/")
           for key in ("impl", "migration", "lock"))
assert os.getpid() != 3586
module_spec = importlib.util.spec_from_file_location("_fake_macos_switch", cfg["impl"])
m = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(m)
real_open = builtins.open
def fake_open(file, *args, **kwargs):
    if str(file) == "/dev/tty":
        try:
            return io.TextIOWrapper(io.FileIO(os.dup(cfg["tty_fd"]), "r+", closefd=True),
                                    encoding="utf-8", write_through=True)
        except OSError as exc:
            print("fake PTY setup failed:", repr(exc), file=sys.stderr)
            raise
    return real_open(file, *args, **kwargs)
builtins.open = fake_open
m._legacy_lock_path = lambda: Path(cfg["lock"])
m._migration_base = lambda *, create=True: Path(cfg["migration"])
# Negative tests set a fake third-party subtree. Instrument the product's
# filesystem helpers so an attempted dereference is recorded and stopped
# before the helper reads the fake target; no production paths are involved.
if cfg.get("no_follow_root"):
    protected = Path(cfg["no_follow_root"]).resolve()
    def guard_foreign(path):
        resolved = Path(path).resolve(strict=False)
        if resolved == protected or protected in resolved.parents:
            with real_open(cfg["audit"], "a", encoding="utf-8") as audit:
                audit.write("attempted foreign-target access\n")
            raise m.SafetyError("fixture blocked foreign-target access")
    original_open_dir = m._open_directory
    original_read_file = m._read_stable_file
    def open_dir(path, *args, **kwargs):
        guard_foreign(path)
        return original_open_dir(path, *args, **kwargs)
    def read_file(path, *args, **kwargs):
        guard_foreign(path)
        return original_read_file(path, *args, **kwargs)
    m._open_directory = open_dir
    m._read_stable_file = read_file
def forbidden_signal(*args):
    with real_open(cfg["audit"], "a", encoding="utf-8") as audit:
        audit.write("python signal attempt\n")
    raise m.SafetyError("fixture blocked a signal attempt")
os.kill = forbidden_signal
os.killpg = forbidden_signal
original_switch = m.switch_operation
def switch(**kwargs):
    return original_switch(minimum_oid=cfg["old_oid"], **kwargs)
m.switch_operation = switch
# Only the process list is synthetic; the real lsof checks the test-owned port.
# No production PID is inspected or signalled.
original_run = m._run_without_signals
def fake_process_list(command):
    if command == ["/bin/ps", "-ww", "-axo", "pid=,uid=,command="]:
        return subprocess.CompletedProcess(command, 0, b"", b"")
    if command and command[0] == "/usr/sbin/lsof":
        assert command[2] == "-iTCP:" + str(cfg["port"]) and cfg["port"] != 3586
    elif command and command[0] != "/usr/bin/git":
        with real_open(cfg["audit"], "a", encoding="utf-8") as audit:
            audit.write("unexpected product subprocess\n")
        raise m.SafetyError("fixture blocked an unexpected subprocess")
    return original_run(command)
m._run_without_signals = fake_process_list
point = cfg.get("point")
def rendezvous():
    hook = Path(cfg["hook"])
    (hook / "ready").write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 30
    while not (hook / "release").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("fake checkpoint timeout")
        time.sleep(.01)
    if cfg.get("abort"):
        raise InterruptedError("test-only interruption")
if point == "precheck.migration_mutex_held":
    original_enter = m.HeldLock.__enter__
    def enter(self):
        result = original_enter(self)
        if self.path == Path(cfg["migration"]) / "migration.lock":
            rendezvous()
        return result
    m.HeldLock.__enter__ = enter
elif point in ("merge.pre_apply", "rollback.pre_apply"):
    original_git = m._git
    def git(root, *args, **kwargs):
        if ((point == "merge.pre_apply" and args[:1] == ("merge",))
            or (point == "rollback.pre_apply" and args[:2] == ("reset", "--keep"))):
            rendezvous()
        return original_git(root, *args, **kwargs)
    m._git = git
elif point == "ledger.after_apply_before_done":
    original_done = m.Manifest.done
    def done(self, action, *args, **kwargs):
        if action == "INSTALL_NEW_LEDGER":
            rendezvous()
        return original_done(self, action, *args, **kwargs)
    m.Manifest.done = done
args = sys.argv[1:]
assert args[:2] == ["-I", "-S"] and Path(args[2]).resolve() == Path(cfg["impl"]).resolve()
raise SystemExit(m.main(args[3:]))
'''


def ledger(n):
    return (json.dumps({'version': 3, 'since': '2026-01-01', 'keep_days': 400,
                        'total': {'requests': n, 'ok': n, 'fail': 0,
                                  'client_disconnect': 0, 'sum_ms': 0,
                                  'max_ms': 0, 'combos': {}},
                        'months_archive': {}, 'days': {}},
                       ensure_ascii=False, indent=2) + '\n').encode()


def put(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    path.chmod(mode)


def replace_fake(path, data, mode=0o600):
    """Model an authorized business writer's ordinary atomic replacement."""
    candidate = path.with_name(path.name + '.fake-business-replacement')
    put(candidate, data, mode)
    os.replace(candidate, path)


def diagnostic(p):
    return f'exit={p.returncode}\nstdout={p.stdout}\nstderr={p.stderr}'


def run_without_signal(args, *, timeout, **kwargs):
    """Do not signal even our own child on timeout; report its PID instead."""
    p = subprocess.Popen(args, **kwargs)
    try:
        stdout, stderr = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(f'fixture child PID {p.pid} timed out; no signal sent') from exc
    return subprocess.CompletedProcess(args, p.returncode, stdout, stderr)


class FakeWorktree:
    """No imports or config from the real project; copy only the three deliverables."""
    def __init__(self, tc):
        self.tc = tc
        self.temp = tempfile.TemporaryDirectory(prefix='macos-switch-fake-')
        tc.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'project'
        self.home = self.base / 'home'
        self.tmp = self.base / 'tmp'
        self.old_cwd = self.base / 'old-cwd'
        self.new_cwd = self.base / 'new-cwd'
        self.bin = self.base / 'bin'
        for d in (self.root, self.home, self.tmp, self.old_cwd, self.new_cwd, self.bin):
            d.mkdir(mode=0o700)
        self.audit = self.base / 'forbidden-commands.log'
        # Fail closed for common shell-level signal/launch helpers. Sandbox also
        # blocks direct signal syscalls; it cannot prove no attempt was made.
        for command in ('kill', 'killall', 'pkill', 'launchctl', 'open', 'osascript'):
            put(self.bin / command,
                '#!/bin/sh\nprintf "%s\\n" "$(basename "$0")" >> "$AUDIT_FILE"\nexit 97\n', 0o700)
        self.old_lock = self.base / 'old-model-proxy.lock'
        put(self.old_lock, b'fake lock bytes; never a PID\n')
        self.env = {'HOME': str(self.home), 'TMPDIR': str(self.tmp),
                    'PATH': f'{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin',
                    'LC_ALL': 'C', 'LANG': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
                    'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
                    'PYTHONDONTWRITEBYTECODE': '1', 'AUDIT_FILE': str(self.audit)}
        self.git('init', '-b', 'master')
        self.git('config', 'user.name', 'Fake Test')
        self.git('config', 'user.email', 'fake@example.invalid')
        put(self.root / '.gitignore', '.runtime/\n.model_proxy.log\n.model_proxy_totals.json\n'
            'config/model_proxy_config.json\nconfig/session_overrides.json\n.claude/\nignored/\n')
        put(self.root / 'config/runtime_paths.json', json.dumps(OLD_PATHS, indent=2) + '\n')
        put(self.root / 'docs/worktrees.md', b'old tracked unchanged by target\n')
        put(self.root / 'code/intersect.txt', b'old code\n')
        put(self.root / 'old-only/removed.txt', b'old tracked removed in target\n')
        put(self.root / 'ignored/restored.txt', b'old ignored tracked restored on rollback\n')
        self.git('add', '-A')
        self.git('add', '-f', 'ignored/restored.txt')
        self.git('commit', '-m', 'fake old')
        self.old_oid = self.git('rev-parse', 'HEAD').stdout.strip()
        self.worktree = self.root / '.claude/worktrees/qw/feat-windows-unified-cli'
        self.worktree.parent.mkdir(parents=True)
        self.git('worktree', 'add', '-b', 'fake-target', str(self.worktree))
        put(self.worktree / 'config/runtime_paths.json', json.dumps(NEW_PATHS, indent=2) + '\n')
        put(self.worktree / 'code/intersect.txt', b'new code\n')
        (self.worktree / 'old-only/removed.txt').unlink()
        (self.worktree / 'ignored/restored.txt').unlink()
        put(self.worktree / COLLISION, B, 0o644)
        put(self.worktree / 'extra/new-file.txt', b'new tracked\n', 0o644)
        put(self.worktree / 'ignored/new-file.txt', b'new tracked in ignored directory\n', 0o644)
        missing = [s for s in SCRIPTS if not (SOURCE / 'tools' / s).is_file()]
        tc.assertFalse(missing, f'并行交付尚未就绪，不能执行假测试: {missing}')
        (self.worktree / 'tools').mkdir()
        for name in SCRIPTS:
            shutil.copyfile(SOURCE / 'tools' / name, self.worktree / 'tools' / name)
        self.git('add', '-A', cwd=self.worktree)
        self.git('add', '-f', 'ignored/new-file.txt', cwd=self.worktree)
        self.git('commit', '-m', 'fake target with actual deliverables', cwd=self.worktree)
        self.target = self.git('rev-parse', 'HEAD', cwd=self.worktree).stdout.strip()
        self.git_file = (self.worktree / '.git').read_bytes()
        self.git_identity = (self.root / '.git').stat().st_ino
        self.worktree_list = self.git('worktree', 'list', '--porcelain').stdout
        put(self.root / 'docs/worktrees.md', b'user edit outside changed paths\n')
        put(self.root / COLLISION, A, 0o640)
        put(self.root / '.model_proxy.log', b'fake old log must remain\n')
        put(self.root / '.model_proxy_totals.json', ledger(100))
        self.key = self.base / 'fake-appkey.yaml'
        put(self.key, b'fake-key: fake-value\n')
        self.config = self.root / 'config/model_proxy_config.json'
        self.config_json = {'admin_token': 'fake-admin', 'listen': {'host': '127.0.0.1', 'port': 1},
                            'supplies': [{'id': 'fake', 'url': 'http://127.0.0.1:1/',
                                          'protocol': 'anthropic', 'appkey_file': str(self.key),
                                          'target_model': 'fake'}],
                            'routes': [], 'strategies': []}
        self.save_config()
        self.sidecar = self.root / 'config/session_overrides.json'
        put(self.sidecar, b'{}\n')
        self.env['MODEL_PROXY_CONFIG'] = str(self.config)
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            self.port = s.getsockname()[1]
        self.tc.assertNotEqual(self.port, 3586)
        self.config_json['listen']['port'] = self.port
        self.save_config()
        self.migration = self.home / 'Library/Application Support/model-proxy-migration'
        self.migration.mkdir(parents=True, mode=0o700)
        self.driver = self.base / 'fixture-driver.py'
        put(self.driver, DRIVER)
        # The shell wrapper invokes this shim only in the fake worktree.
        put(self.bin / 'python3', '''#!/bin/sh
if [ "$3" = "-c" ]; then exec /opt/homebrew/bin/python3 "$@"; fi
exec /opt/homebrew/bin/python3 -I -S "$FAKE_DRIVER" "$@"
''', 0o700)
        # Deny reads of real users and the real legacy lock; allow writing ONLY
        # this canonical temp tree and /dev/null. No listener may be opened.
        self.profile = self.base / 'sandbox.sb'
        p = str(self.base).replace('\\', '\\\\').replace('"', '\\"')
        put(self.profile, f'''(version 1)
(allow default)
(deny file-read* (subpath "/Users") (literal "/tmp/model_proxy.lock")
                  (literal "/private/tmp/model_proxy.lock"))
(deny file-write* (require-all (require-not (subpath "{p}"))
                                (require-not (literal "/dev/null"))))
(deny signal (target others))
(deny network*)
''')

    def save_config(self):
        put(self.config, json.dumps(self.config_json, indent=2) + '\n')

    def git(self, *args, cwd=None):
        p = run_without_signal(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', *args],
                               cwd=cwd or self.root, env=self.env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.tc.assertEqual(p.returncode, 0, diagnostic(p))
        return p

    def args(self, mode, transaction=None, port=None, recover=False):
        first, second = ((self.old_cwd, self.new_cwd) if mode == 'switch'
                         else (self.new_cwd, self.old_cwd))
        return ['/bin/bash', str(self.worktree / 'tools' / f'macos_{mode}.sh'),
                '--root', str(self.root),
                *(['--target', self.target] if mode == 'switch' else ['--transaction', transaction]),
                '--port', str(self.port if port is None else port),
                '--old-cwd', str(first), '--new-cwd', str(second),
                '--old-home', str(self.home), '--new-home', str(self.home),
                *(['--recover'] if recover else []),
                '--manual-stopped', '--auto-start-disabled']

    def _start(self, mode, transaction=None, port=None, recover=False, point=None, hook=None,
               abort=False, answers=None):
        master, slave = os.openpty()
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        actual_port = self.port if port is None else port
        metadata = {'base': str(self.base), 'impl': str(self.worktree / 'tools/macos_switch_impl.py'),
                    'migration': str(self.migration), 'lock': str(self.old_lock),
                    'audit': str(self.audit),
                    'no_follow_root': str(getattr(self, 'no_follow_root', '')),
                    'old_oid': self.old_oid, 'tty_fd': slave, 'port': actual_port,
                    'point': point, 'hook': str(hook) if hook else '', 'abort': abort}
        fd, spec = tempfile.mkstemp(prefix='driver-spec-', suffix='.json', dir=self.base)
        with os.fdopen(fd, 'w') as fp:
            json.dump(metadata, fp)
        env = dict(self.env, FAKE_DRIVER=str(self.driver), FAKE_DRIVER_SPEC=spec)
        p = subprocess.Popen(['/usr/bin/sandbox-exec', '-f', str(self.profile),
                              *self.args(mode, transaction, port, recover)],
                             cwd=self.root, env=env, text=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(slave,))
        os.close(slave)
        output = []
        def read_terminal():
            while True:
                try:
                    chunk = os.read(master, 4096)
                    if not chunk:
                        return
                    output.append(chunk)
                except OSError:
                    return
        thread = threading.Thread(target=read_terminal, daemon=True)
        thread.start()
        if answers is None:
            if mode == 'switch':
                answers = [env.get('MODEL_PROXY_CONFIG', 'UNSET'), 'UNSET', 'UNSET',
                           '一致', '接受可能缺账']
            else:
                answers = ['一致', '接受可能缺账']
        os.write(master, ('\n'.join(answers) + '\n').encode())
        p.fixture_terminal = (master, thread, output)
        return p

    def _finish(self, p):
        try:
            out, err = p.communicate(timeout=60)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(f'fixture child PID {p.pid} timed out; no signal sent') from exc
        master, thread, output = p.fixture_terminal
        thread.join(timeout=2)
        os.close(master)
        return subprocess.CompletedProcess(p.args, p.returncode,
                                           out + b''.join(output).decode('utf-8', errors='replace'), err)

    def command(self, mode, transaction=None, port=None, recover=False, answers=None):
        return self._finish(self._start(mode, transaction, port, recover, answers=answers))

    @contextlib.contextmanager
    def paused(self, mode, point, transaction=None, abort=False):
        """Rendezvous is installed by the isolated test driver, not product.

        The monkeypatch writes ``ready`` at the named point, then waits for
        ``release``. abort raises InterruptedError immediately after release.
        Names: precheck.migration_mutex_held (after taking the shared
        transaction mutex but *before* taking any service lock),
        merge.pre_apply, rollback.pre_apply, ledger.after_apply_before_done.
        Without this seam the test fails explicitly. No signal injection is
        used; a timed-out subprocess is reported without sending it a signal.
        """
        hook = self.base / ('hook-' + point.replace('.', '-'))
        hook.mkdir(mode=0o700)
        p = self._start(mode, transaction, point=point, hook=hook, abort=abort)
        finished = None
        try:
            deadline = time.monotonic() + 10
            while not (hook / 'ready').exists():
                if p.poll() is not None:
                    finished = self._finish(p)
                    self.tc.fail(f'test-only checkpoint {point!r} not reached: ' + diagnostic(finished))
                if time.monotonic() >= deadline:
                    self.tc.fail(f'test-only checkpoint {point!r} timed out')
                time.sleep(0.02)
            yield p
        finally:
            put(hook / 'release', b'continue\n')
            if finished is None:
                finished = self._finish(p)
            p.test_output = (finished.stdout, finished.stderr)

    def txn(self, p):
        parent = self.home / 'Library/Application Support/model-proxy-migration'
        dirs = [d for d in parent.iterdir() if d.is_dir()] if parent.is_dir() else []
        self.tc.assertEqual(len(dirs), 1, f'expected one private transaction directory; {diagnostic(p)}')
        self.tc.assertIn(dirs[0].name, p.stdout + p.stderr, 'transaction ID must be reported')
        self.tc.assertEqual(stat.S_IMODE(dirs[0].stat().st_mode), 0o700)
        return dirs[0]

    def unchanged_guard(self):
        self.tc.assertEqual(self.git('rev-parse', 'HEAD').stdout.strip(), self.old_oid)
        self.tc.assertEqual((self.root / '.model_proxy_totals.json').read_bytes(), ledger(100))
        self.tc.assertEqual((self.root / '.model_proxy.log').read_bytes(), b'fake old log must remain\n')
        self.tc.assertEqual((self.root / COLLISION).read_bytes(), A)
        self.tc.assertEqual((self.root / 'docs/worktrees.md').read_bytes(), b'user edit outside changed paths\n')
        self.tc.assertEqual((self.worktree / '.git').read_bytes(), self.git_file)
        self.tc.assertEqual((self.root / '.git').stat().st_ino, self.git_identity)
        self.tc.assertEqual(self.git('worktree', 'list', '--porcelain').stdout, self.worktree_list)
        self.tc.assertFalse(self.audit.exists(), 'signal/launch helper invoked')

    def successful_switch(self):
        p = self.command('switch')
        self.tc.assertEqual(p.returncode, 0, 'pristine control did not switch; negative results cannot be credited\n' + diagnostic(p))
        self.tc.assertEqual(self.git('rev-parse', 'HEAD').stdout.strip(), self.target)
        self.tc.assertEqual((self.root / '.runtime/model_proxy_totals.json').read_bytes(), ledger(100))
        self.tc.assertEqual((self.root / COLLISION).read_bytes(), B)
        self.tc.assertFalse(self.audit.exists(), 'signal/launch helper invoked')
        return p, self.txn(p)


@unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').exists(),
                     'requires native macOS sandbox and Git worktree')
class MacosSwitchWorktreeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Enforce the *full* 100 -> 103 -> rollback 100 control before any
        # negative test, even under unittest's alphabetical ordering.
        control = cls('test_upgrade_and_rollback_preserve_business_data_and_git')
        result = unittest.TestResult()
        control.run(result)
        if not result.wasSuccessful():
            failures = result.failures + result.errors
            raise AssertionError('full positive control failed; negatives not executed:\n' +
                                 '\n'.join(text for _, text in failures))

    def fixture(self):
        return FakeWorktree(self)

    def test_fixture_is_real_nested_git_worktree_with_committed_deliverables(self):
        f = self.fixture()
        self.assertTrue((f.worktree / '.git').is_file())
        self.assertEqual(f.git('rev-parse', 'HEAD').stdout.strip(), f.old_oid)
        self.assertEqual(f.git('rev-parse', 'HEAD', cwd=f.worktree).stdout.strip(), f.target)
        self.assertEqual(f.git('rev-parse', '--git-common-dir', cwd=f.worktree).stdout.strip(),
                         str(f.root / '.git'))
        self.assertEqual(f.git('merge-base', f.old_oid, f.target).stdout.strip(), f.old_oid)
        names = set(f.git('ls-tree', '-r', '-z', '--name-only', f.target).stdout.split('\0'))
        for script in SCRIPTS:
            self.assertIn('tools/' + script, names)
        self.assertIn(str(COLLISION), names)
        self.assertEqual((f.root / COLLISION).read_bytes(), A)
        self.assertEqual((f.worktree / COLLISION).read_bytes(), B)

    def test_sandbox_denies_outside_write_and_listener(self):
        f = self.fixture()
        outside = tempfile.TemporaryDirectory(prefix='macos-switch-outside-')
        self.addCleanup(outside.cleanup)
        target = Path(outside.name).resolve() / 'must-not-be-created'
        code = '''import pathlib,socket,sys
try:
 pathlib.Path(sys.argv[1]).write_text("bad")
except PermissionError:
 print("WRITE_DENIED")
else:
 print("WRITE_ALLOWED")
try:
 s=socket.socket();s.bind(("127.0.0.1",0))
except PermissionError:
 print("BIND_DENIED")
else:
 print("BIND_ALLOWED")
'''
        p = run_without_signal(['/usr/bin/sandbox-exec', '-f', str(f.profile),
                                '/usr/bin/python3', '-c', code, str(target)],
                               cwd=f.root, env=f.env, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(p.returncode, 0, diagnostic(p))
        self.assertIn('WRITE_DENIED', p.stdout)
        self.assertIn('BIND_DENIED', p.stdout)
        self.assertFalse(target.exists())

    def test_plain_git_reset_removes_an_empty_new_parent_directory(self):
        # Control experiment: without the product's parent sentinel, real
        # Git removes this parent after removing the target-only tracked file.
        f = self.fixture()
        (f.root / COLLISION).unlink()
        f.git('merge', '--ff-only', f.target)
        parent = (f.root / COLLISION).parent
        self.assertTrue(parent.is_dir())
        f.git('reset', '--keep', f.old_oid)
        self.assertFalse(parent.exists(), 'the control must exercise Git removing the empty parent')

    def test_upgrade_and_rollback_preserve_business_data_and_git(self):
        f = self.fixture()
        put(f.sidecar, b'{"fake-client":{"fake-session":"old-route"}}\n')
        original_config, sidecar = f.config.read_bytes(), f.sidecar.read_bytes()
        original_config_mode = stat.S_IMODE(f.config.stat().st_mode)
        original_sidecar_mode = stat.S_IMODE(f.sidecar.stat().st_mode)
        original_key = f.key.read_bytes()
        unrelated = f.root / 'docs/unrelated-user-note.md'
        put(unrelated, b'unrelated user data survives\n')
        old_index = f.git('write-tree').stdout.strip()
        parent = (f.root / COLLISION).parent
        parent_inode = parent.stat().st_ino
        p, txn = f.successful_switch()
        output = p.stdout + p.stderr
        self.assertIn('持久化', output, 'must limit accounting claim to persisted ledger')
        self.assertIn('最后', output, 'must identify last verifiable persisted state')
        self.assertIn('请求', output, 'must warn user to stop new requests')
        self.assertIn('等待', output, 'must warn user to wait for in-flight requests')
        self.assertTrue('未持久化' in output or
                        ('不证明' in output and '持久化' in output and '可能缺账' in output),
                        'must acknowledge potentially unpersisted data')
        self.assertIn('接受可能缺账', output, 'must ask for explicit risk acceptance')
        for false_claim in ('确保统计完整', '保证统计完整', '统计已完整', '统计完整无损'):
            self.assertNotIn(false_claim, output, 'cannot claim complete accounting')
        self.assertEqual((f.root / '.model_proxy_totals.json').read_bytes(), ledger(100))
        self.assertEqual(f.config.read_bytes(), original_config)
        self.assertEqual(f.sidecar.read_bytes(), sidecar)
        self.assertEqual((f.root / 'docs/worktrees.md').read_bytes(), b'user edit outside changed paths\n')
        self.assertEqual((f.root / '.model_proxy.log').read_bytes(), b'fake old log must remain\n')
        self.assertTrue(any(x.is_file() and x.read_bytes() == A for x in txn.rglob('*')),
                        'untracked A must have a retained private copy')
        self.assertTrue(any(x.is_file() and x.read_bytes() == ledger(100) for x in txn.rglob('*')),
                        'original 100 ledger backup missing')
        self.assertTrue(any(x.is_file() and x.read_bytes() == b'fake old log must remain\n'
                            for x in txn.rglob('*')), 'old log must be saved before proceeding')
        put(f.root / '.runtime/model_proxy_totals.json', ledger(103))
        put(f.root / '.runtime/model_proxy.log', b'fake new log must remain\n')
        # New-version business writes are allowed; rollback must restore the
        # old snapshots, even when normal atomic replacement changes inodes.
        newer_config = dict(f.config_json, admin_token='fake-admin-new-version')
        replace_fake(f.config, json.dumps(newer_config, indent=2) + '\n')
        replace_fake(f.sidecar, b'{"fake-client":{"fake-session":"new-route"}}\n')
        self.assertNotEqual(f.config.read_bytes(), original_config)
        self.assertNotEqual(f.sidecar.read_bytes(), sidecar)
        manifest = json.loads((txn / 'manifest.json').read_text())
        pins = [event for event in manifest['events']
                if event.get('action') == 'PIN_CONFLICT_PARENT' and event.get('state') == 'DONE']
        self.assertEqual(len(pins), 1, 'parent must be protected against Git removing it')
        sentinel = parent / pins[0]['name']
        with f.paused('rollback', 'rollback.pre_apply', txn.name) as rollback:
            self.assertTrue(sentinel.is_file())
            self.assertEqual(parent.stat().st_ino, parent_inode)
        self.assertEqual(rollback.returncode, 0, str(rollback.test_output))
        self.assertEqual(f.git('rev-parse', 'HEAD').stdout.strip(), f.old_oid)
        self.assertEqual(f.git('write-tree').stdout.strip(), old_index)
        self.assertEqual((f.root / 'code/intersect.txt').read_bytes(), b'old code\n')
        self.assertEqual(json.loads((f.root / 'config/runtime_paths.json').read_text()), OLD_PATHS)
        self.assertEqual((f.root / '.model_proxy_totals.json').read_bytes(), ledger(100))
        self.assertEqual((f.root / '.runtime/model_proxy_totals.json').read_bytes(), ledger(103))
        self.assertEqual((f.root / '.runtime/model_proxy.log').read_bytes(), b'fake new log must remain\n')
        self.assertEqual((f.root / '.model_proxy.log').read_bytes(), b'fake old log must remain\n')
        self.assertEqual((f.root / COLLISION).read_bytes(), A)
        self.assertEqual(stat.S_IMODE((f.root / COLLISION).stat().st_mode), 0o640)
        self.assertEqual(parent.stat().st_ino, parent_inode)
        self.assertFalse(sentinel.exists(), 'only the verified temporary sentinel may be removed')
        self.assertTrue(any(x.is_file() and x.read_bytes() == ledger(100) for x in txn.rglob('*')))
        self.assertTrue(any(x.is_file() and x.read_bytes() == original_config for x in txn.rglob('*')),
                        'original business config snapshot missing')
        self.assertTrue(any(x.is_file() and x.read_bytes() == sidecar for x in txn.rglob('*')),
                        'original sidecar snapshot missing')
        self.assertEqual(f.config.read_bytes(), original_config)
        self.assertEqual(f.sidecar.read_bytes(), sidecar)
        self.assertEqual(stat.S_IMODE(f.config.stat().st_mode), original_config_mode)
        self.assertEqual(stat.S_IMODE(f.sidecar.stat().st_mode), original_sidecar_mode)
        self.assertEqual(f.key.read_bytes(), original_key)
        self.assertEqual(unrelated.read_bytes(), b'unrelated user data survives\n')
        self.assertEqual((f.root / 'docs/worktrees.md').read_bytes(), b'user edit outside changed paths\n')
        self.assertEqual((f.worktree / '.git').read_bytes(), f.git_file)
        self.assertEqual((f.root / '.git').stat().st_ino, f.git_identity)
        self.assertEqual(f.git('worktree', 'list', '--porcelain').stdout, f.worktree_list)
        self.assertFalse(f.audit.exists())

    def test_preflight_conflicts_refuse_without_business_changes(self):
        # The independent positive control prevents a stub or unrelated failure
        # from making any negative case pass.
        self.fixture().successful_switch()
        for case in ('dirty-tracked', 'untracked', 'ignored', 'runtime-unknown',
                     'old-lock-held', 'port-held', 'appkey-cwd-drift',
                     'config-raw-sidecar-drift'):
            with self.subTest(case=case):
                f = self.fixture()
                with contextlib.ExitStack() as stack:
                    if case == 'dirty-tracked':
                        put(f.root / 'code/intersect.txt', b'user modified tracked\n')
                    elif case == 'untracked':
                        put(f.root / 'extra/new-file.txt', b'unknown untracked\n')
                    elif case == 'ignored':
                        put(f.root / 'ignored/new-file.txt', b'unknown ignored\n')
                    elif case == 'runtime-unknown':
                        put(f.root / '.runtime/unknown', b'unknown runtime\n')
                    elif case == 'old-lock-held':
                        lock = stack.enter_context(f.old_lock.open('r+b'))
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    elif case == 'port-held':
                        sock = stack.enter_context(socket.socket())
                        sock.bind(('127.0.0.1', f.port))
                        sock.listen(1)
                    elif case == 'appkey-cwd-drift':
                        put(f.old_cwd / 'key.yaml', b'fake old key\n')
                        put(f.new_cwd / 'key.yaml', b'fake different key\n')
                        f.config_json['supplies'][0]['appkey_file'] = 'key.yaml'
                        f.save_config()
                    else:
                        # Same target inode, distinct raw config parents and
                        # hence distinct sidecar paths: inode-only checks fail.
                        for cwd, data in ((f.old_cwd, b'{"old":{}}\n'),
                                          (f.new_cwd, b'{"new":{}}\n')):
                            parent = cwd / 'config-link'
                            parent.mkdir()
                            (parent / 'model_proxy_config.json').symlink_to(f.config)
                            put(parent / 'session_overrides.json', data)
                        f.env['MODEL_PROXY_CONFIG'] = 'config-link/model_proxy_config.json'
                    config_before, sidecar_before = f.config.read_bytes(), f.sidecar.read_bytes()
                    index_before = f.git('write-tree').stdout.strip()
                    p = f.command('switch')
                    self.assertNotEqual(p.returncode, 0, diagnostic(p))
                    reasons = {'dirty-tracked': 'tracked 文件已修改',
                               'untracked': '未跟踪或 ignored 对象冲突',
                               'ignored': '未跟踪或 ignored 对象冲突',
                               'runtime-unknown': '.runtime 已存在',
                               'old-lock-held': '相关锁被占用',
                               'port-held': '指定端口仍有监听',
                               'appkey-cwd-drift': 'appkey_file 在新旧上下文中展开结果不同',
                               'config-raw-sidecar-drift': '原始配置路径不同'}
                    self.assertIn(reasons[case], p.stdout + p.stderr, diagnostic(p))
                    f.unchanged_guard()
                    self.assertEqual(f.git('write-tree').stdout.strip(), index_before)
                    self.assertEqual(f.config.read_bytes(), config_before)
                    self.assertEqual(f.sidecar.read_bytes(), sidecar_before)
                    if case == 'dirty-tracked':
                        self.assertEqual((f.root / 'code/intersect.txt').read_bytes(), b'user modified tracked\n')
                    elif case in ('untracked', 'ignored'):
                        path = 'extra/new-file.txt' if case == 'untracked' else 'ignored/new-file.txt'
                        self.assertEqual((f.root / path).read_bytes(), f'unknown {case}\n'.encode())
                    elif case == 'runtime-unknown':
                        self.assertEqual((f.root / '.runtime/unknown').read_bytes(), b'unknown runtime\n')
                    elif case == 'old-lock-held':
                        self.assertEqual(f.old_lock.read_bytes(), b'fake lock bytes; never a PID\n')
                    elif case == 'config-raw-sidecar-drift':
                        self.assertEqual((f.old_cwd / 'config-link/session_overrides.json').read_bytes(),
                                         b'{"old":{}}\n')
                        self.assertEqual((f.new_cwd / 'config-link/session_overrides.json').read_bytes(),
                                         b'{"new":{}}\n')

    def test_rollback_restores_authorized_changed_old_ledger_to_snapshot(self):
        for update in ('in-place', 'atomic-replace'):
            with self.subTest(update=update):
                f = self.fixture()
                original_config, sidecar = f.config.read_bytes(), f.sidecar.read_bytes()
                old_ledger = f.root / '.model_proxy_totals.json'
                old_mode = stat.S_IMODE(old_ledger.stat().st_mode)
                unrelated = f.root / 'docs/unrelated-user-note.md'
                put(unrelated, b'unrelated user data survives\n')
                _, txn = f.successful_switch()
                put(f.root / '.runtime/model_proxy_totals.json', ledger(103))
                if update == 'in-place':
                    put(old_ledger, ledger(101))
                else:
                    replace_fake(old_ledger, ledger(101))
                self.assertEqual(old_ledger.read_bytes(), ledger(101))
                p = f.command('rollback', txn.name)
                self.assertEqual(p.returncode, 0, diagnostic(p))
                self.assertEqual(old_ledger.read_bytes(), ledger(100))
                self.assertEqual(stat.S_IMODE(old_ledger.stat().st_mode), old_mode)
                self.assertEqual((f.root / '.runtime/model_proxy_totals.json').read_bytes(), ledger(103))
                self.assertEqual(f.git('rev-parse', 'HEAD').stdout.strip(), f.old_oid)
                self.assertEqual((f.root / COLLISION).read_bytes(), A)
                self.assertEqual(f.config.read_bytes(), original_config)
                self.assertEqual(f.sidecar.read_bytes(), sidecar)
                self.assertEqual(unrelated.read_bytes(), b'unrelated user data survives\n')
                self.assertFalse(f.audit.exists())

    def test_rollback_succeeds_when_sidecar_remains_absent(self):
        f = self.fixture()
        f.sidecar.unlink()
        self.assertFalse(os.path.lexists(f.sidecar))
        original_config = f.config.read_bytes()
        _, txn = f.successful_switch()
        self.assertFalse(os.path.lexists(f.sidecar))
        put(f.root / '.runtime/model_proxy_totals.json', ledger(103))
        p = f.command('rollback', txn.name)
        self.assertEqual(p.returncode, 0, diagnostic(p))
        self.assertFalse(os.path.lexists(f.sidecar), 'old absence must be restored')
        self.assertEqual(f.config.read_bytes(), original_config)
        self.assertEqual((f.root / '.model_proxy_totals.json').read_bytes(), ledger(100))
        self.assertEqual((f.root / '.runtime/model_proxy_totals.json').read_bytes(), ledger(103))
        self.assertEqual(f.git('rev-parse', 'HEAD').stdout.strip(), f.old_oid)
        self.assertEqual((f.root / COLLISION).read_bytes(), A)
        self.assertFalse(f.audit.exists())

    def test_rollback_refuses_new_unproven_sidecar_and_preserves_it(self):
        f = self.fixture()
        f.sidecar.unlink()
        self.assertFalse(os.path.lexists(f.sidecar))
        original_config = f.config.read_bytes()
        _, txn = f.successful_switch()
        self.assertFalse(os.path.lexists(f.sidecar))
        put(f.root / '.runtime/model_proxy_totals.json', ledger(103))
        unknown = b'{"fake-client":{"fake-session":"new-route"}}\n'
        put(f.sidecar, unknown)
        before = f.sidecar.lstat()
        index_before = f.git('write-tree').stdout.strip()
        p = f.command('rollback', txn.name)
        self.assertNotEqual(p.returncode, 0, diagnostic(p))
        self.assertIn('sidecar', p.stderr.lower(), diagnostic(p))
        self.assertIn('来源未证明', p.stderr, diagnostic(p))
        after = f.sidecar.lstat()
        self.assertTrue(stat.S_ISREG(after.st_mode))
        self.assertEqual((after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_gid),
                         (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_gid))
        self.assertEqual(f.sidecar.read_bytes(), unknown)
        self.assertEqual(f.config.read_bytes(), original_config)
        self.assertEqual((f.root / '.model_proxy_totals.json').read_bytes(), ledger(100))
        self.assertEqual((f.root / '.runtime/model_proxy_totals.json').read_bytes(), ledger(103))
        self.assertEqual(f.git('rev-parse', 'HEAD').stdout.strip(), f.target)
        self.assertEqual(f.git('write-tree').stdout.strip(), index_before)
        self.assertEqual((f.root / COLLISION).read_bytes(), B)
        self.assertTrue(any(x.is_file() and x.read_bytes() == A for x in txn.rglob('*')))
        self.assertEqual((f.root / 'docs/worktrees.md').read_bytes(),
                         b'user edit outside changed paths\n')
        self.assertEqual((f.worktree / '.git').read_bytes(), f.git_file)
        self.assertEqual((f.root / '.git').stat().st_ino, f.git_identity)
        self.assertFalse(f.audit.exists())

    def test_rollback_refuses_unknown_targets_and_unsafe_parent(self):
        cases = ('ledger-symlink', 'config-symlink', 'sidecar-symlink',
                 'config-parent-symlink', 'ledger-hardlink', 'config-hardlink',
                 'sidecar-hardlink', 'ledger-directory')
        for case in cases:
            with self.subTest(case=case):
                f = self.fixture()
                original_config, original_sidecar = f.config.read_bytes(), f.sidecar.read_bytes()
                original_key = f.key.read_bytes()
                unrelated = f.root / 'docs/unrelated-user-note.md'
                put(unrelated, b'unrelated user data survives\n')
                _, txn = f.successful_switch()
                put(f.root / '.runtime/model_proxy_totals.json', ledger(103))
                foreign = f.base / 'fake-third-party'
                foreign.mkdir(mode=0o700)
                f.no_follow_root = foreign
                foreign_files = {}
                saved_config = None

                if case == 'config-parent-symlink':
                    saved_config = f.root / 'fake-saved-config'
                    (f.root / 'config').rename(saved_config)
                    foreign_files = {
                        foreign / 'model_proxy_config.json': original_config,
                        foreign / 'session_overrides.json': original_sidecar,
                        foreign / 'runtime_paths.json': json.dumps(NEW_PATHS).encode(),
                    }
                    for file, data in foreign_files.items():
                        put(file, data)
                    attacked = f.root / 'config'
                    attacked.symlink_to(foreign, target_is_directory=True)
                    expected_link = str(foreign)
                elif case == 'ledger-directory':
                    attacked = f.root / '.model_proxy_totals.json'
                    attacked.unlink()
                    attacked.mkdir(mode=0o700)
                    put(attacked / 'unknown', b'unknown object survives\n')
                else:
                    role, kind = case.split('-')
                    attacked = {'ledger': f.root / '.model_proxy_totals.json',
                                'config': f.config, 'sidecar': f.sidecar}[role]
                    foreign_file = foreign / 'other-user-fake-data'
                    data = {'ledger': ledger(7), 'config': original_config,
                            'sidecar': b'{"foreign":{"session":"route"}}\n'}[role]
                    put(foreign_file, data)
                    foreign_files[foreign_file] = data
                    attacked.unlink()
                    if kind == 'symlink':
                        attacked.symlink_to(foreign_file)
                        expected_link = str(foreign_file)
                    else:
                        os.link(foreign_file, attacked)
                        self.assertEqual(attacked.stat().st_nlink, 2)

                p = f.command('rollback', txn.name)
                self.assertNotEqual(p.returncode, 0, diagnostic(p))
                self.assertEqual(f.git('rev-parse', 'HEAD').stdout.strip(), f.target)
                self.assertEqual((f.root / COLLISION).read_bytes(), B)
                self.assertEqual((f.root / '.runtime/model_proxy_totals.json').read_bytes(), ledger(103))
                self.assertEqual((f.root / '.model_proxy.log').read_bytes(), b'fake old log must remain\n')
                self.assertEqual((f.root / 'docs/worktrees.md').read_bytes(),
                                 b'user edit outside changed paths\n')
                self.assertEqual(unrelated.read_bytes(), b'unrelated user data survives\n')
                self.assertEqual(f.key.read_bytes(), original_key)
                self.assertEqual((f.worktree / '.git').read_bytes(), f.git_file)
                self.assertEqual((f.root / '.git').stat().st_ino, f.git_identity)
                self.assertTrue(any(x.is_file() and x.read_bytes() == A for x in txn.rglob('*')))
                for file, data in foreign_files.items():
                    self.assertEqual(file.read_bytes(), data, 'third-party fake data must not change')
                if 'symlink' in case:
                    self.assertTrue(attacked.is_symlink())
                    self.assertEqual(os.readlink(attacked), expected_link)
                elif case.endswith('hardlink'):
                    self.assertFalse(attacked.is_symlink())
                    self.assertEqual(attacked.stat().st_ino, foreign_file.stat().st_ino)
                    self.assertEqual(attacked.stat().st_nlink, 2)
                else:
                    self.assertTrue(attacked.is_dir())
                    self.assertEqual((attacked / 'unknown').read_bytes(), b'unknown object survives\n')
                if saved_config is not None:
                    self.assertEqual((saved_config / 'model_proxy_config.json').read_bytes(), original_config)
                    self.assertEqual((saved_config / 'session_overrides.json').read_bytes(), original_sidecar)
                if not case.startswith('config-'):
                    self.assertEqual(f.config.read_bytes(), original_config)
                if not case.startswith(('sidecar-', 'config-parent-')):
                    self.assertEqual(f.sidecar.read_bytes(), original_sidecar)
                if not case.startswith('ledger-'):
                    self.assertEqual((f.root / '.model_proxy_totals.json').read_bytes(), ledger(100))
                self.assertFalse(f.audit.exists(), 'must not attempt to access fake third-party targets')

    def test_untracked_and_ignored_created_after_git_precheck_are_not_overwritten(self):
        self.fixture().successful_switch()
        for mode in ('switch', 'rollback'):
            for ignored in (False, True):
                with self.subTest(mode=mode, ignored=ignored):
                    f = self.fixture()
                    txn = None
                    if mode == 'rollback':
                        _, txn_dir = f.successful_switch()
                        txn = txn_dir.name
                        relative = 'ignored/restored.txt' if ignored else 'old-only/removed.txt'
                    else:
                        relative = 'ignored/new-file.txt' if ignored else 'extra/new-file.txt'
                    unknown = f.root / relative
                    self.assertFalse(unknown.exists())
                    point = 'merge.pre_apply' if mode == 'switch' else 'rollback.pre_apply'
                    with f.paused(mode, point, txn) as p:
                        put(unknown, b'concurrent unknown must survive\n')
                    observed = unknown.read_bytes() if unknown.exists() else None
                    self.assertEqual((p.returncode != 0, observed),
                                     (True, b'concurrent unknown must survive\n'),
                                     str(p.test_output))
                    self.assertEqual(f.git('rev-parse', 'HEAD').stdout.strip(),
                                     f.old_oid if mode == 'switch' else f.target)
                    self.assertFalse(f.audit.exists())

    def test_rollback_precheck_rejects_existing_untracked_and_ignored(self):
        for ignored in (False, True):
            with self.subTest(ignored=ignored):
                f = self.fixture()
                _, txn = f.successful_switch()
                unknown = f.root / ('ignored/restored.txt' if ignored else 'old-only/removed.txt')
                self.assertFalse(unknown.exists())
                put(unknown, b'rollback unknown must survive\n')
                p = f.command('rollback', txn.name)
                self.assertNotEqual(p.returncode, 0, diagnostic(p))
                self.assertEqual(unknown.read_bytes(), b'rollback unknown must survive\n')
                self.assertEqual(f.git('rev-parse', 'HEAD').stdout.strip(), f.target)
                self.assertEqual((f.root / '.model_proxy_totals.json').read_bytes(), ledger(100))
                self.assertFalse(f.audit.exists())

    def test_concurrent_transaction_is_refused_by_shared_lock(self):
        self.fixture().successful_switch()
        for mode, competing in (('switch', 'switch'), ('switch', 'rollback'),
                                ('rollback', 'rollback'), ('rollback', 'switch')):
            with self.subTest(mode=mode, competing=competing):
                f = self.fixture()
                txn = None
                if mode == 'rollback':
                    _, txn_dir = f.successful_switch()
                    txn = txn_dir.name
                with f.paused(mode, 'precheck.migration_mutex_held', txn) as first:
                    second_txn = (txn or '0' * 32) if competing == 'rollback' else None
                    second = f.command(competing, second_txn)
                    self.assertNotEqual(second.returncode, 0, diagnostic(second))
                    self.assertTrue('锁' in (second.stdout + second.stderr) or
                                    'lock' in (second.stdout + second.stderr).lower(),
                                    'second transaction must identify lock contention: ' + diagnostic(second))
                    self.assertEqual(f.git('rev-parse', 'HEAD').stdout.strip(),
                                     f.old_oid if mode == 'switch' else f.target)
                    self.assertEqual((f.root / '.model_proxy_totals.json').read_bytes(), ledger(100))
                self.assertEqual(first.returncode, 0, str(first.test_output))
                self.assertFalse(f.audit.exists())

    def test_interrupted_ledger_commit_with_foreign_identity_stays_unknown(self):
        self.fixture().successful_switch()
        f = self.fixture()
        with f.paused('switch', 'ledger.after_apply_before_done', abort=True) as p:
            installed = f.root / '.runtime/model_proxy_totals.json'
            self.assertEqual(installed.read_bytes(), ledger(100))
            migration = f.home / 'Library/Application Support/model-proxy-migration'
            manifests = list(migration.rglob('*manifest*.json'))
            self.assertEqual(len(manifests), 1, 'intent manifest must exist before committing')
            manifest = json.loads(manifests[0].read_text())

            def intents(value):
                if isinstance(value, dict):
                    if any(str(k).lower() in ('status', 'state', 'phase')
                           and str(v).lower() == 'intent' for k, v in value.items()):
                        yield value
                    for key, child in value.items():
                        if 'intent' in str(key).lower() and isinstance(child, (dict, list)):
                            yield child
                        yield from intents(child)
                elif isinstance(value, list):
                    for child in value:
                        yield from intents(child)

            entries = list(intents(manifest))
            self.assertTrue(entries, 'manifest must persist intent before ledger submission')
            serialized = [json.dumps(item, ensure_ascii=False) for item in entries]
            digest = hashlib.sha256(ledger(100)).hexdigest()
            parent_ino, file_ino = installed.parent.stat().st_ino, installed.stat().st_ino
            self.assertTrue(any(digest in x and str(parent_ino) in x and str(file_ino) in x
                                for x in serialized),
                            'ledger intent must bind candidate digest, parent and staging identity')
        self.assertNotEqual(p.returncode, 0, str(p.test_output))
        # Replacing the installed file changes both identity and digest.
        installed.unlink()
        put(installed, ledger(101))
        parent = f.home / 'Library/Application Support/model-proxy-migration'
        dirs = [d for d in parent.iterdir() if d.is_dir()]
        self.assertEqual(len(dirs), 1)
        recovery = f.command('rollback', dirs[0].name, recover=True)
        self.assertNotEqual(recovery.returncode, 0, diagnostic(recovery))
        self.assertIn('UNKNOWN', (recovery.stdout + recovery.stderr).upper())
        self.assertEqual(installed.read_bytes(), ledger(101))
        self.assertEqual((f.root / '.model_proxy_totals.json').read_bytes(), ledger(100))
        self.assertFalse(f.audit.exists())

    def test_risk_acceptance_is_required_before_changes(self):
        f = self.fixture()
        p = f.command('switch', answers=[str(f.config), 'UNSET', 'UNSET', '一致', '拒绝'])
        self.assertNotEqual(p.returncode, 0, diagnostic(p))
        self.assertIn('未明确接受可能缺账', p.stdout + p.stderr)
        f.unchanged_guard()
        self.assertFalse((f.root / '.runtime').exists())


if __name__ == '__main__':
    unittest.main()
