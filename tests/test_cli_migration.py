"""Run only from an explicitly prepared isolated project, never the worktree."""
import contextlib
import io
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ISOLATION = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if ROOT != ISOLATION / 'project' or os.environ.get('HOME') != str(ISOLATION / 'home'):
    raise RuntimeError('refusing to run outside the explicitly isolated temporary project')
if not (ISOLATION / '.model-proxy-isolation').is_file():
    raise RuntimeError('missing isolated-project sentinel')

from core.cli import dispatch, _install_ops
from core.cli.help_text import HELP

class Control:
    def __init__(self, result=0):
        self.calls = []
        self.result = result
    def reload(self):
        self.calls.append('reload')
        return self.result
    def on(self): self.calls.append('on'); return self.result
    def off(self): self.calls.append('off'); return self.result
    def restart(self): self.calls.append('restart'); return self.result
    def status(self): self.calls.append('status'); return self.result

class TestCliMigration(unittest.TestCase):
    def setUp(self):
        self.config = ROOT / 'config' / 'model_proxy_config.json'
        self.env = os.environ.copy()
        self.env['MODEL_PROXY_CONFIG'] = str(self.config)

    def cli(self, *args, cwd=None):
        return subprocess.run([str(ROOT / 'model_proxy_cli.sh'), *args],
                              env=self.env, cwd=cwd or ROOT, stdin=subprocess.DEVNULL,
                              text=True, capture_output=True, timeout=15)

    def test_help_and_unknown_from_other_cwd(self):
        p = self.cli('--help', cwd=ISOLATION)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, HELP)
        self.assertEqual(p.stdout, (ROOT / 'tests/golden/legacy-help.txt').read_text(encoding='utf-8'))
        p = self.cli('config')
        self.assertEqual(p.returncode, 1)
        self.assertIn('Unknown command: config', p.stdout)

    def test_control_fails_closed_and_never_uses_socket_or_process(self):
        for command in ('on', 'off', 'restart', 'status', 'reload'):
            with self.subTest(command=command), mock.patch('socket.socket', side_effect=AssertionError('socket')), \
                 mock.patch('subprocess.Popen', side_effect=AssertionError('spawn')), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertNotEqual(dispatch.main([command]), 0)

    def test_injected_control_routes_once(self):
        for command in ('on', 'off', 'restart', 'status', 'reload'):
            c = Control(7)
            self.assertEqual(dispatch.main([command], control=c), 7)
            self.assertEqual(c.calls, [command])

    def assert_isolated_runtime(self):
        # Unified layout: every runtime role lives under the project `.runtime/`,
        # not in a separate `runtime/` directory or in /tmp.
        runtime_dir = ROOT / '.runtime'
        self.assertTrue(runtime_dir.is_dir())
        self.assertFalse(runtime_dir.is_symlink())
        self.assertFalse((ROOT / 'config').is_symlink())
        values = json.loads((ROOT / 'config' / 'runtime_paths.json').read_text())
        keys = ('log', 'totals', 'lock', 'pid', 'ensure_log', 'start_lock')
        self.assertEqual(set(values), set(keys))
        # runtime_paths.json stores project-relative defaults; resolve them
        # against the installation root exactly as the CLI does.
        paths = []
        for key in keys:
            path = Path(values[key])
            paths.append(path if path.is_absolute() else ROOT / path)
        self.assertEqual(len(set(paths)), 6)
        for path in paths:
            self.assertEqual(path.parent, runtime_dir)
            self.assertFalse(path.is_symlink())

    @staticmethod
    def fake_switch_config(path):
        cfg = {'supplies': [], 'routes': [{'id': 'old'}, {'id': 'new'}],
               'strategies': [{'client_token': 'fake-token', 'route_id': 'old'}]}
        path.write_text(json.dumps(cfg), encoding='utf-8')
        path.chmod(0o600)
        return cfg

    @unittest.skipUnless(sys.platform == 'darwin' and os.name == 'posix',
                         'anonymous-pipe v2 and Mac config writer required')
    def test_reload_marker_only_after_success(self):
        import errno
        import fcntl
        import stat

        self.assert_isolated_runtime()
        actual_run = subprocess.run
        cases = (
            ('no-write', 'no', 0, 0, 0, 0, False),
            ('write', 'write', 0, 0, 0, 1, True),
            ('interrupted-after-write', 'write', 130, 0, 130, 0, True),
            ('failed-without-write', 'no', 1, 0, 1, 0, False),
            ('aborted-with-writing-no', 'abort', 1, 0, 1, 0, False),
            ('success-with-writing-no', 'abort', 0, 0, 1, 0, False),
            ('incomplete-writing', 'start_only', 0, 0, 1, 0, False),
            ('invalid', 'bad', 0, 0, 1, 0, False),
            ('unpreceded-yes', 'bare_yes', 0, 0, 1, 0, False),
            ('missing', 'empty', 0, 0, 1, 0, False),
            ('repeated-yes', 'double_yes', 0, 0, 0, 1, True),
            ('reload-failure', 'write', 0, 1, 1, 1, True),
        )
        truncated_frames = (
            ('partial-no', b'n'),
            ('partial-writing', b'writ'),
            ('partial-yes', b'writing\nye'),
            # A valid prefix must not hide a truncated trailing frame.
            ('partial-repeated-yes', b'writing\nyes\nye'),
        )
        cases += tuple((f'{label}-exit-{rc}', 'truncated_' + frame.hex(),
                        rc, 0, 1, 0, False)
                       for label, frame in truncated_frames for rc in (0, 1))
        with tempfile.TemporaryDirectory(prefix='migration-pipe-', dir=ROOT / 'config') as directory:
            fixture = Path(directory)
            helper = fixture / 'controlled_helper.py'
            helper.write_text('''\
import fcntl, os, sys
def deny(event, args):
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo",
                 "socket.gethostbyname", "subprocess.Popen", "os.system",
                 "os.fork", "os.posix_spawn", "os.exec"):
        raise PermissionError("isolated helper: operation denied")
sys.addaudithook(deny)
sys.path.insert(0, sys.argv[1])
from core.cli import _config_ops
fd = int(os.environ["CONFIG_OPS_RELOAD_MARKER_FD"])
assert "CONFIG_OPS_RELOAD_MARKER" not in os.environ
assert os.get_inheritable(fd)
assert not (fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)
_config_ops._MARKER_REQUIRED = True
_config_ops._precheck_inherited_marker()
assert not os.get_inheritable(fd)
assert fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
print("PIPE_V2_FD_OK", flush=True)
config, mode, exit_code = sys.argv[2], sys.argv[3], int(sys.argv[4])
if mode == "no":
    _config_ops._set_marker("no")
elif mode in ("abort", "start_only"):
    _config_ops._WRITE_OPERATION = "switch"
    _config_ops._prepare_write(config, "switch")
    _config_ops._set_marker("writing")
    if mode == "abort":
        _config_ops._set_marker("no")
elif mode in ("write", "double_yes"):
    _config_ops._WRITE_OPERATION = "switch"
    _config_ops._prepare_write(config, "switch")
    cfg = _config_ops.load_config(config)
    cfg["strategies"][0]["route_id"] = "new"
    _config_ops.atomic_write(config, cfg)
    if mode == "double_yes":
        _config_ops.done(True)
elif mode == "bad":
    os.write(fd, b"bad\\n")
elif mode == "bare_yes":
    os.write(fd, b"yes\\n")
elif mode.startswith("truncated_"):
    frame = bytes.fromhex(mode[len("truncated_"):])
    assert os.write(fd, frame) == len(frame)
elif mode != "empty":
    raise RuntimeError("unexpected test mode")
raise SystemExit(exit_code)
''', encoding='utf-8')

            for index, (label, mode, child_rc, reload_rc, result_rc, reloads, changed) in enumerate(cases):
                with self.subTest(case=label):
                    config = fixture / f'fake-{index}.json'
                    self.fake_switch_config(config)
                    control = Control(reload_rc)
                    descriptors = []
                    observed_frames = []
                    real_drain = dispatch._drain_marker_pipe

                    def observe_frames(fd, proof):
                        frames = real_drain(fd, proof)
                        observed_frames.append(frames)
                        return frames

                    drain_patch = (mock.patch.object(dispatch, '_drain_marker_pipe',
                                                     side_effect=observe_frames)
                                   if mode.startswith('truncated_') else contextlib.nullcontext())

                    def run_controlled(args, *, env, pass_fds):
                        self.assertEqual(args, [sys.executable, str(ROOT / 'core' / 'cli' / 'helper_entry.py'),
                                                'switch', str(config), 'fake-token', 'new'])
                        self.assertNotIn('CONFIG_OPS_RELOAD_MARKER', env)
                        fd = int(env['CONFIG_OPS_RELOAD_MARKER_FD'])
                        self.assertEqual(pass_fds, (fd,))
                        self.assertTrue(stat.S_ISFIFO(os.fstat(fd).st_mode))
                        self.assertEqual(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE, os.O_WRONLY)
                        self.assertFalse(os.get_inheritable(fd))
                        self.assertTrue(fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)
                        descriptors.append(fd)
                        child = actual_run([sys.executable, '-I', '-S', '-B', str(helper),
                                            str(ROOT), str(config), mode, str(child_rc)],
                                           cwd=ROOT, env=env, pass_fds=pass_fds,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
                        diagnostic = child.stderr.decode('utf-8', errors='replace')
                        lines = child.stdout.splitlines()
                        self.assertEqual(lines[:1], [b'PIPE_V2_FD_OK'], diagnostic)
                        # A successful commit reports where the backup went;
                        # it must be the project runtime, never a sibling file.
                        extra = lines[1:]
                        if extra:
                            self.assertEqual(len(extra), 1, diagnostic)
                            self.assertIn(b'.runtime/backups', extra[0], diagnostic)
                        self.assertEqual(child.returncode, child_rc, diagnostic)
                        return subprocess.CompletedProcess(args, child.returncode)

                    with drain_patch, \
                         mock.patch.object(dispatch.subprocess, 'run', side_effect=run_controlled), \
                         contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(dispatch.run_helper('switch', ['fake-token', 'new'],
                                                             config, control, root=ROOT), result_rc)
                    self.assertEqual(control.calls, ['reload'] * reloads)
                    if mode.startswith('truncated_'):
                        self.assertEqual(observed_frames, [bytes.fromhex(mode[len('truncated_'):])])
                    self.assertEqual(len(descriptors), 1)
                    with self.assertRaises(OSError) as closed:
                        os.fstat(descriptors[0])
                    self.assertEqual(closed.exception.errno, errno.EBADF)
                    current = json.loads(config.read_text(encoding='utf-8'))
                    self.assertEqual(current['strategies'][0]['route_id'], 'new' if changed else 'old')

    @unittest.skipUnless(sys.platform == 'darwin' and os.name == 'posix',
                         'anonymous-pipe v2 and Mac config writer required')
    def test_real_switch_helper_pipe_commits_and_reloads_once(self):
        self.assert_isolated_runtime()
        with tempfile.TemporaryDirectory(prefix='migration-real-', dir=ROOT / 'config') as directory:
            config = Path(directory) / 'fake.json'
            original = self.fake_switch_config(config)
            control = Control()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = dispatch.run_helper('switch', ['fake-token', 'new'], config, control, root=ROOT)
            self.assertEqual(rc, 0)
            self.assertEqual(control.calls, ['reload'])
            current = json.loads(config.read_text(encoding='utf-8'))
            self.assertEqual(current['strategies'][0]['route_id'], 'new')
            # Backups moved to the project runtime; nothing may be left beside
            # the edited target.
            backup_dir = ROOT / '.runtime' / 'backups'
            backups = list(backup_dir.glob(config.name + '.bak.*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text(encoding='utf-8')), original)
            self.assertEqual(list(config.parent.glob(config.name + '.bak.*')), [])

    def test_six_paths_logs_and_stats(self):
        paths = dispatch.runtime_paths(ROOT)
        self.assertEqual(len(paths), 6)
        for path in paths.values():
            self.assertTrue(Path(path).is_relative_to(ISOLATION))
        Path(paths['log']).write_text('one INFO blah\ntwo ACCESS req_id=abc\nthree ERROR problem\n')
        p = self.cli('logs', 'req=abc')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('req_id=abc', p.stdout)
        p = self.cli('logs', '0')
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, '')
        Path(paths['totals']).write_text(json.dumps({'total': {'requests': 0, 'ok': 0, 'fail': 0, 'combos': {}}}))
        p = self.cli('stats')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('period: total', p.stdout)

    def test_noninteractive_menus(self):
        for name in ('supply', 'route', 'strategy'):
            p = self.cli(name)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn('非交互', p.stdout)

    def test_install_redacts_candidates_and_snippets(self):
        secret = 'FAKE-client-token-private'
        out = io.StringIO()
        cands = [{'client_token': secret, 'route_desc': secret, 'note': secret}]
        with contextlib.redirect_stdout(out), mock.patch.object(_install_ops, 'candidate_tokens', return_value=cands), \
             mock.patch('builtins.input', return_value=''):
            self.assertEqual(_install_ops.choose_token({}, None, 'test'), secret)
        self.assertNotIn(secret, out.getvalue())
        for sdk in ('claude', 'hermes', 'openclaw'):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                getattr(_install_ops, 'install_' + sdk)(secret, '28371')
            self.assertNotIn(secret, out.getvalue())
            self.assertIn('<CLIENT_TOKEN>', out.getvalue())

    def test_install_diff_hides_existing_secrets(self):
        out = io.StringIO()
        target = ISOLATION / 'home' / 'placeholder.json'
        target.write_text('old-FAKE-secret')
        target.chmod(0o600)
        with contextlib.redirect_stdout(out), mock.patch.object(_install_ops, 'confirm', return_value=False):
            self.assertFalse(_install_ops.preview_confirm_write(
                target, 'old-FAKE-secret', 'new-FAKE-secret', 'test', [],
                snapshot=SimpleNamespace(contents=b'old-FAKE-secret')))
        self.assertNotIn('FAKE-secret', out.getvalue())

    def test_interpreter_discovery_fallback_does_not_repeat_command(self):
        fakebin = ISOLATION / 'fakebin'
        fakebin.mkdir(exist_ok=True)
        counter = ISOLATION / 'interpreter-calls'
        (fakebin / 'python3').write_text('#!/bin/sh\nexit 1\n')
        (fakebin / 'python').write_text(
            '#!/bin/sh\nif [ "$1" = "-c" ]; then exit 0; fi\n'
            + 'echo invoked >> ' + shlex.quote(str(counter)) + '\n'
            + 'exec ' + shlex.quote(sys.executable) + ' "$@"\n')
        (fakebin / 'python3').chmod(0o755)
        (fakebin / 'python').chmod(0o755)
        env = self.env.copy()
        env['PATH'] = str(fakebin) + ':/usr/bin:/bin'
        p = subprocess.run([str(ROOT / 'model_proxy_cli.sh'), 'config'], env=env,
                           cwd=ROOT, stdin=subprocess.DEVNULL, text=True, capture_output=True)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(counter.read_text().splitlines(), ['invoked'])

    def test_old_python_bootstrap_reports_version_before_import(self):
        old_python = Path('/usr/bin/python3')
        if not old_python.exists():
            self.skipTest('no system Python to test')
        version = subprocess.run([str(old_python), '-c', 'import sys; print(sys.version_info[:2])'],
                                 env=self.env, cwd=ROOT, capture_output=True, text=True)
        if version.returncode or version.stdout.strip() != '(3, 9)':
            self.skipTest('system Python is not 3.9')
        p = subprocess.run([str(old_python), str(ROOT / 'core/cli/main.py'), '--help'],
                           env=self.env, cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('Python >= 3.10', p.stderr)
        self.assertNotIn('Traceback', p.stderr)

    def test_switch_records_change_but_reports_reload_failure(self):
        original = self.config.read_text()
        try:
            cfg = json.loads(original)
            cfg['routes'] = [{'id': 'r1'}, {'id': 'r2'}]
            cfg['strategies'] = [{'client_token': 'fake-token', 'route_id': 'r1'}]
            self.config.write_text(json.dumps(cfg))
            p = self.cli('switch', 'fake-token', 'r2')
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('reload was not confirmed', p.stderr)
            self.assertEqual(json.loads(self.config.read_text())['strategies'][0]['route_id'], 'r2')
        finally:
            self.config.write_text(original)

    def test_no_python_files_in_project_root(self):
        self.assertEqual([p.name for p in ROOT.iterdir() if p.name.casefold().endswith('.py')], [])

if __name__ == '__main__':
    unittest.main()
