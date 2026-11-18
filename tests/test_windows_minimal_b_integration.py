"""Opt-in Windows/Whisky acceptance: five commands, one local mock request.

The real .bat and shared Python CLI run from a fresh copy of the worktree's
core, with fake config/keys, new runtime paths, and OS-selected loopback ports.
Fresh-install and precreated-private-object scenarios are independently
opted in. The latter does not establish first-install or native-Windows success.
Nothing is deployed; skipped tests are not an acceptance result.

The CLI starts a detached child, so a small sitecustomize hook retains its
original Popen handle in the CLI process for failure cleanup. It also captures
service logs. Product identity, locking, HTTP and lifecycle logic are not
mocked. Cleanup never finds/kills a process by PID or port. This is a practical
fixture for cooperative local code, not a sandbox or a native-Windows claim.
On timeout the owned command handle and local fixtures remain held until the
guard returns and reaps its children; a stuck OS call may delay completion.
All temporary evidence is retained, including on failure.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import platform
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent
ADMIN = 'fake-admin-only'
CLIENT = 'fake-client-only'
UPSTREAM = 'fake-upstream-only'
COMMAND_TIMINGS = []

# Run inside the real CLI interpreter. Keep each service Popen alive after
# dispatch returns; a release file requests cleanup of those handles only.
GUARD = r'''
import _thread, atexit, json, os, pathlib, subprocess, sys, threading, time, traceback
base = os.environ.get('MP_TEST_GUARD')
if base and len(sys.argv) >= 2 and pathlib.Path(sys.argv[0]).name.lower() == 'main.py':
    from core.cli import dispatch, windows_control
    base = pathlib.Path(base)
    entry = os.path.normcase(os.path.abspath(os.environ['MP_TEST_ENTRY']))
    orig_popen, orig_main = subprocess.Popen, dispatch.main
    children, logs = [], []
    spawn_lock = threading.Lock()
    finished = threading.Event()
    def cancelled():
        return (base / 'cancel').exists() or (base / 'release').exists()
    class ControllerSubprocess:
        def __getattr__(self, name):
            return getattr(subprocess, name)
        def Popen(self, args, *a, **kw):
            if (not isinstance(args, (list, tuple)) or not any(isinstance(arg, str)
                    and os.path.normcase(os.path.abspath(arg)) == entry for arg in args)):
                raise RuntimeError('test guard refuses an untracked controller subprocess')
            # No interrupt may arrive during Popen: once returned, its
            # original handle is retained even if cancellation raced spawn.
            with spawn_lock:
                if cancelled():
                    raise RuntimeError('test command cancelled before service spawn')
                log = open(base / 'service.log', 'ab', buffering=0)
                logs.append(log); kw['stdout'] = log; kw['stderr'] = log
                child = orig_popen(args, *a, **kw)
                children.append(child)
                if cancelled():
                    raise RuntimeError('test command cancelled during service spawn')
                return child
    # Only the controller reference is changed. Other modules, including the
    # identity reader and subprocess.Popen itself, retain the original class.
    windows_control.subprocess = ControllerSubprocess()
    def cleanup():
        errors, reaped = [], []
        try:
            if children:
                deadline = time.monotonic() + 600
                while not (base / 'release').exists() and time.monotonic() < deadline:
                    request = base / 'inspect'
                    if request.exists():
                        try:
                            nonce = request.read_text(encoding='utf-8'); request.unlink()
                            tmp = base / 'live.tmp'
                            tmp.write_text(json.dumps({'nonce': nonce, 'children': [
                                {'pid': c.pid, 'exit': c.poll()} for c in children]}), encoding='utf-8')
                            os.replace(tmp, base / 'live.json')
                        except FileNotFoundError:
                            pass
                    time.sleep(.1)
                if not (base / 'release').exists():
                    errors.append('guard timed out waiting for release')
        except BaseException:
            errors.append(traceback.format_exc())
        finally:
            for child in children:
                try:
                    if child.poll() is None: child.terminate()
                    child.wait(timeout=15)
                except BaseException:
                    errors.append(traceback.format_exc())
                    try:
                        if child.poll() is None: child.kill()
                        child.wait(timeout=15)
                    except BaseException: errors.append(traceback.format_exc())
                try:
                    reaped.append({'pid': child.pid, 'returncode': child.poll()})
                except BaseException:
                    errors.append(traceback.format_exc())
                    reaped.append({'pid': child.pid, 'returncode': None})
            for log in logs:
                try: log.close()
                except BaseException: errors.append(traceback.format_exc())
            try:
                (base / 'cleanup.json').write_text(
                    json.dumps({'errors': errors, 'children': reaped}), encoding='utf-8')
            except BaseException:
                traceback.print_exc()  # missing marker is also a test failure
    atexit.register(cleanup)
    def main(*a, **kw):
        def watch_cancel():
            while not finished.wait(.05):
                if (base / 'cancel').exists():
                    # Interrupt only this test CLI's own main thread, never
                    # another process. An in-flight Popen first returns its
                    # handle under the same lock.
                    with spawn_lock:
                        if not finished.is_set():
                            _thread.interrupt_main()
                    return
        watcher = threading.Thread(target=watch_cancel, daemon=True)
        watcher.start()
        result, error = None, None
        try:
            if cancelled():
                raise RuntimeError('test command cancelled before dispatch')
            result = orig_main(*a, **kw)
            return result
        except BaseException:
            error = traceback.format_exc()
            raise
        finally:
            while True:
                try:
                    finished.set()
                    watcher.join()
                    sys.stdout.flush(); sys.stderr.flush()
                    tmp = base / 'result.tmp'
                    tmp.write_text(json.dumps({'returncode': result, 'error': error,
                        'children': [c.pid for c in children]}), encoding='utf-8')
                    os.replace(tmp, base / 'result.json')
                    break
                except KeyboardInterrupt:
                    error = error or 'test cancellation during CLI finalization'
                    result = None
    dispatch.main = main
'''

# A foreign listener created by this test, never an existing user process.
FOREIGN = r'''
import socket, sys
from pathlib import Path
root = Path(sys.argv[1]); s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
s.bind(('127.0.0.1', int(sys.argv[2]))); s.listen(8); s.settimeout(.2)
(root / 'ready').touch()
while not (root / 'release').exists():
    try: conn, _ = s.accept()
    except socket.timeout: continue
    with conn:
        conn.settimeout(1); parts = []
        while True:
            try: chunk = conn.recv(16384)
            except (socket.timeout, OSError): break
            if not chunk: break
            parts.append(chunk)
        with (root / 'traffic').open('ab') as f: f.write(b''.join(parts) + b'\n')
s.close()
'''


def wait_for(check, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise TimeoutError('timed out after %ss' % timeout)


def read_json(path, timeout=5):
    # Wine can briefly report a sharing violation just after the guard's
    # atomic rename. Retry only transient file access; preserve a final error.
    deadline = time.monotonic() + timeout
    while True:
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (FileNotFoundError, PermissionError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(.05)


def wait_owned(child, label):
    """Keep the original Popen and fixtures alive until the child is reaped."""
    while True:
        try:
            return child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            print('Still waiting for owned command; Popen and fixtures retained: ' + str(label), flush=True)
        except KeyboardInterrupt:
            print('Interrupt deferred while owned Popen is pending: ' + str(label), flush=True)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def run_private_helper(project, evidence, *, verify=False, tag='private-fixture'):
    """Run the staged helper in a separate Windows interpreter."""
    helper = SOURCE / 'tests' / 'wine_private_fixture.py'
    # Preserve the exact helper used alongside its stdout/stderr, including on
    # partial creation or failure. The helper itself checks the fresh layout.
    staged = evidence / 'wine_private_fixture.py'
    if not verify:
        shutil.copy2(helper, staged)
    allowed = {'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'TEMP', 'TMP', 'USERPROFILE',
               'PATH', 'PATHEXT', 'COMSPEC'}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    args = [sys.executable, '-I', '-S', '-B', str(staged)]
    if verify: args.append('--verify-config')
    args.append(str(project))
    (evidence / (tag + '.command.json')).write_text(
        json.dumps({'argv': args, 'cwd': str(project)}), encoding='utf-8')
    child = subprocess.Popen(args,
                             cwd=project, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timed_out, cleanup_error = False, None
    try:
        stdout, stderr = child.communicate(timeout=90)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout, stderr = exc.output or b'', exc.stderr or b''
        try:
            if child.poll() is None:
                child.terminate()  # only this fixture helper's original Popen
            stdout, stderr = child.communicate(timeout=15)
        except Exception as error:
            cleanup_error = repr(error)
            try:
                if child.poll() is None:
                    child.kill()  # same Popen, never a PID or process search
                stdout, stderr = child.communicate(timeout=15)
            except Exception as retry_error:
                cleanup_error += '; recovery unconfirmed: ' + repr(retry_error)
    (evidence / (tag + '.stdout.log')).write_bytes(stdout)
    (evidence / (tag + '.stderr.log')).write_bytes(stderr)
    if cleanup_error:
        (evidence / (tag + '.cleanup.log')).write_text(cleanup_error, encoding='utf-8')
    if timed_out or child.returncode != 0:
        raise RuntimeError('private fixture %s (exit=%s); evidence: %s; stderr: %s; cleanup: %s' %
                           ('timed out' if timed_out else 'failed', child.returncode,
                            evidence, stderr.decode('utf-8', errors='replace'), cleanup_error))
    return json.loads(stdout.decode('utf-8'))


def prepare_private_objects(project, evidence):
    result = run_private_helper(project, evidence)
    expected = {'.model_proxy_runtime', 'runtime', r'.model_proxy_runtime\control.lock',
                r'.model_proxy_runtime\anchor.lock', r'runtime\lock', r'runtime\log',
                r'config\runtime_paths.json', r'config\model_proxy_config.json'}
    objects = result.get('objects', {})
    if (set(objects) != expected or result.get('runtime_preflight') != 'verified'
            or result.get('record_directory') != 'verified'
            or any(info.get('private_acl') is not True or info.get('effective_access') is not True
                   or info.get('protected') is not False for info in objects.values())
            or any(objects[name].get('empty') is not True for name in expected - {
                '.model_proxy_runtime', 'runtime'})):
        raise RuntimeError('private fixture evidence incomplete: ' + repr(result))
    return result


def verify_private_configs(project, evidence, initial, contents, tag):
    result = run_private_helper(project, evidence, verify=True, tag=tag)
    objects = result.get('objects', {})
    if set(objects) != set(contents):
        raise RuntimeError('private config evidence incomplete: ' + repr(result))
    for name, text in contents.items():
        item = objects[name]
        if (item.get('private_acl') is not True or item.get('protected') is not False
                or item.get('identity') != initial['objects'][name]['identity']
                or item.get('sha256') != hashlib.sha256(text.encode('utf-8')).hexdigest()):
            raise RuntimeError('private config changed or unverified: ' + name)
    return result


class MockUpstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append((self.path, dict(self.headers), body))
        if (self.path != '/v1/messages' or self.headers.get('x-api-key') != UPSTREAM
                or self.headers.get('Authorization') != 'Bearer ' + UPSTREAM):
            self.send_error(400)
            return
        data = json.dumps({'id': 'fake', 'type': 'message', 'role': 'assistant',
            'model': body['model'], 'content': [{'type': 'text', 'text': 'mock-reply'}],
            'stop_reason': 'end_turn', 'stop_sequence': None,
            'usage': {'input_tokens': 2, 'output_tokens': 2}}).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)


class HarnessSelfTest(unittest.TestCase):
    def run_fake_guard(self, fail_inspect=False):
        # Fake CLI and service; use the real identity-reader module to check its
        # Popen factory after the controller-only proxy has been installed.
        with tempfile.TemporaryDirectory(prefix='mp-b-selftest-') as td:
            root = Path(td); (root / 'core/cli').mkdir(parents=True)
            for name in ('core/__init__.py', 'core/cli/__init__.py'):
                (root / name).write_text('', encoding='utf-8')
            (root / 'core/windows_process.py').write_text('class NativeBackend: pass\n', encoding='utf-8')
            (root / 'core/cli/windows_control.py').write_text(
                'import subprocess\ndef _local_absolute(value): return True\n', encoding='utf-8')
            shutil.copy2(SOURCE / 'core/cli/windows_command_line.py', root / 'core/cli/windows_command_line.py')
            (root / 'core/cli/dispatch.py').write_text(
                'import os, subprocess, sys\nfrom core.cli import windows_control\n'
                'def main(*a):\n'
                ' from core.cli import windows_command_line as identity\n'
                ' assert isinstance(subprocess.Popen, type)\n'
                ' assert identity._DIRECT_POPEN is subprocess.Popen\n'
                ' reader = identity.PowerShellWmiReader()\n'
                ' assert reader._process_factory is subprocess.Popen\n'
                ' helper = reader._process_factory([sys.executable, \"-c\", \"pass\"])\n'
                ' assert type(helper) is subprocess.Popen\n'
                ' assert helper.wait(timeout=15) == 0\n'
                ' windows_control.subprocess.Popen([sys.executable, os.environ[\"MP_TEST_ENTRY\"]])\n'
                ' return 0\n', encoding='utf-8')
            main = root / 'core/cli/main.py'
            main.write_text('from core.cli.dispatch import main\nraise SystemExit(main())\n', encoding='utf-8')
            entry = root / 'core/cli/model_proxy.py'
            entry.write_text('import time\ntime.sleep(60)\n', encoding='utf-8')
            (root / 'sitecustomize.py').write_text(GUARD, encoding='utf-8')
            env = dict(os.environ, PYTHONPATH=str(root), MP_TEST_GUARD=str(root), MP_TEST_ENTRY=str(entry))
            child = subprocess.Popen([sys.executable, '-B', str(main), 'on'], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                wait_for(lambda: (root / 'result.json').exists() or child.poll() is not None, 20)
                self.assertTrue((root / 'result.json').exists(), 'hook was not reached')
                data = read_json(root / 'result.json')
                self.assertEqual(data['returncode'], 0, data)
                self.assertEqual(len(data['children']), 1, data)
                self.assertIsNone(child.poll())
                if fail_inspect:
                    (root / 'inspect').mkdir()  # induce a real I/O error in the guard loop
                    wait_for(lambda: (root / 'cleanup.json').exists() or child.poll() is not None, 20)
            finally:
                (root / 'release').touch()
                output, _ = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, output.decode(errors='replace'))
            cleanup = json.loads((root / 'cleanup.json').read_text(encoding='utf-8'))
            self.assertEqual(len(cleanup['children']), 1, cleanup)
            self.assertIsNotNone(cleanup['children'][0]['returncode'], cleanup)
            if fail_inspect:
                self.assertTrue(cleanup['errors'], cleanup)
            else:
                self.assertEqual(cleanup['errors'], [], cleanup)
            with (root / 'parent.log').open('wb') as stream:
                fake = type('FakeTest', (), {})()
                fake.commands = [[child, root, stream, data]]
                if fail_inspect:
                    with self.assertRaises(RuntimeError):
                        WindowsMinimalBAcceptance.cleanup_commands(fake)
                else:
                    WindowsMinimalBAcceptance.cleanup_commands(fake)

    def test_guard_keeps_real_identity_popen_and_reaps_own_child(self):
        self.run_fake_guard()

    def test_guard_exception_still_reaps_own_child_and_reports_failure(self):
        self.run_fake_guard(fail_inspect=True)

    def run_timeout_fake(self, service_started, use_batch=False):
        with tempfile.TemporaryDirectory(prefix='mp-b-timeout-selftest-') as td:
            root = Path(td); project = root / 'project'; (project / 'core/cli').mkdir(parents=True)
            for name in ('core/__init__.py', 'core/cli/__init__.py'):
                (project / name).write_text('', encoding='utf-8')
            (project / 'core/cli/windows_control.py').write_text('import subprocess\n', encoding='utf-8')
            (project / 'core/cli/dispatch.py').write_text(
                'import os, sys, time\nfrom pathlib import Path\nfrom core.cli import windows_control\n'
                'def main(*a):\n'
                ' root=Path(os.environ["MP_FAKE_ROOT"])\n'
                ' if os.environ["MP_FAKE_STARTED"] == "1":\n'
                '  windows_control.subprocess.Popen([sys.executable, os.environ["MP_TEST_ENTRY"]])\n'
                ' (root / "entered").touch()\n'
                ' try:\n'
                '  time.sleep(float(os.environ["MP_FAKE_SLEEP"]))\n'
                ' except KeyboardInterrupt:\n'
                '  pass\n'
                ' assert (root / "fixture-alive").exists()\n'
                ' (root / "fixture-seen").touch()\n'
                ' if os.environ["MP_FAKE_STARTED"] == "0":\n'
                '  windows_control.subprocess.Popen([sys.executable, os.environ["MP_TEST_ENTRY"]])\n'
                ' return 0\n', encoding='utf-8')
            main = project / 'core/cli/main.py'
            main.write_text('from core.cli.dispatch import main\nraise SystemExit(main())\n', encoding='utf-8')
            if use_batch:
                shutil.copy2(SOURCE / 'model_proxy_cli.bat', project / 'model_proxy_cli.bat')
            entry = project / 'core/cli/model_proxy.py'
            entry.write_text('import time\ntime.sleep(60)\n', encoding='utf-8')
            hook = root / 'hook'; hook.mkdir()
            (hook / 'sitecustomize.py').write_text(GUARD, encoding='utf-8')
            (root / 'fixture-alive').touch()
            fake = WindowsMinimalBAcceptance('test_five_commands_and_one_mock_request')
            fake.root, fake.project, fake.commands = root, project, []
            fake.env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(hook), str(project))),
                            MP_TEST_ENTRY=str(entry), MP_FAKE_ROOT=str(root),
                            MP_FAKE_STARTED='1' if service_started else '0',
                            MP_FAKE_SLEEP='15' if use_batch else '8')
            if use_batch:
                systemroot = os.environ.get('SystemRoot', r'C:\Windows')
                fake.env['PATH'] = os.pathsep.join((str(Path(sys.executable).parent),
                    str(Path(systemroot) / 'System32'), systemroot))
            timeout = 12 if use_batch else 5
            try:
                with self.assertRaisesRegex(AssertionError, 'timed out'):
                    fake.call('on', direct=not use_batch, timeout=timeout)
                self.assertTrue((root / 'entered').exists(), 'fake dispatch did not start before timeout')
                self.assertTrue((root / 'fixture-seen').exists(), 'fixture was released before CLI exited')
                child, directory, _, _ = fake.commands[0]
                self.assertIsNotNone(child.poll(), 'original command Popen was not reaped')
                cleanup = read_json(directory / 'cleanup.json')
                self.assertEqual(cleanup['errors'], [], cleanup)
                self.assertEqual(len(cleanup['children']), 1 if service_started else 0, cleanup)
                self.assertTrue(all(c['returncode'] is not None for c in cleanup['children']), cleanup)
                timing = read_json(directory / 'timing.json')
                self.assertEqual(timing['state'], 'timed_out_reaped', timing)
                self.assertGreaterEqual(timing['dispatch_seconds'], timeout)
            finally:
                # The same original handles are still retained by fake.commands.
                fake.cleanup_commands()

    def test_timeout_blocks_late_spawn_and_keeps_fixture(self):
        self.run_timeout_fake(service_started=False)

    def test_timeout_reaps_already_spawned_child(self):
        self.run_timeout_fake(service_started=True)

    @unittest.skipUnless(os.name == 'nt', 'real batch wrapper requires Windows/Whisky')
    def test_batch_timeout_reaps_original_command_and_blocks_late_spawn(self):
        self.run_timeout_fake(service_started=False, use_batch=True)

    def test_missing_cleanup_marker_fails(self):
        with tempfile.TemporaryDirectory(prefix='mp-b-missing-cleanup-') as td:
            root = Path(td)
            code = ('import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\n'
                    'while not (p / "release").exists(): time.sleep(.05)\n')
            child = subprocess.Popen([sys.executable, '-c', code, str(root)])
            stream = (root / 'cli.log').open('wb')
            fake = type('FakeTest', (), {})()
            fake.commands = [[child, root, stream, {'returncode': 0, 'children': []}]]
            try:
                with self.assertRaisesRegex(RuntimeError, 'missing cleanup.json'):
                    WindowsMinimalBAcceptance.cleanup_commands(fake)
                self.assertIsNotNone(child.poll())
            finally:
                if child.poll() is None:
                    child.terminate()  # direct Popen created by this test
                    child.wait(timeout=10)
                stream.close()

    def test_bridge_reports_skipped_failed_and_passing_fake_suites(self):
        with tempfile.TemporaryDirectory(prefix='mp-b-bridge-') as td:
            for name, success, skips, fails in (
                    ('skipped', False, 1, 0), ('failed', False, 0, 1), ('passed', True, 0, 0)):
                target = Path(td) / (name + '.json')
                self.assertEqual(bridge(str(target), 'BridgeFixtures.' + name), success)
                result = json.loads(target.read_text(encoding='utf-8'))
                self.assertEqual((result['tests_run'], result['skipped'], result['failures']), (1, skips, fails))
                self.assertEqual(result['executed'], 1 - skips)
                self.assertEqual(result['success'], success)
                self.assertIn('runtime', result['environment'])
                self.assertTrue(result['not_validated'])
                if fails: self.assertIn('intentional', result['failure_details'][0]['traceback'])

    def test_fresh_and_precreated_opt_ins_are_independent(self):
        flags = ('MP_RUN_WINDOWS_B_ACCEPTANCE', 'MP_RUN_WINDOWS_B_PRIVATE',
                 'MP_RUN_PRIVATE_FIXTURE_SELFTEST')
        saved = {key: os.environ.get(key) for key in flags}
        try:
            for selected in (None, *flags):
                for key in flags:
                    os.environ.pop(key, None)
                if selected:
                    os.environ[selected] = '1'
                definitions = runpy.run_path(str(Path(__file__).resolve()), run_name='optin_check')
                for key, name in zip(flags, ('WindowsMinimalBAcceptance', 'WindowsMinimalBPrecreated',
                                             'WinePrivateFixtureSelfTest')):
                    enabled = not getattr(definitions[name], '__unittest_skip__', False)
                    self.assertEqual(enabled, os.name == 'nt' and key == selected, (selected, name))
        finally:
            for key, value in saved.items():
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value


class BridgeFixtures(unittest.TestCase):
    # Explicitly selected by the self-test; not part of normal test discovery.
    @unittest.skip('intentional all-skipped fixture')
    def skipped(self): pass
    def failed(self): self.fail('intentional fake failure')
    def passed(self): pass


@unittest.skipUnless(os.name == 'nt' and os.environ.get('MP_RUN_PRIVATE_FIXTURE_SELFTEST') == '1',
                     'explicit private-fixture self-test only')
class WinePrivateFixtureSelfTest(unittest.TestCase):
    def test_precreated_private_objects_without_service(self):
        root = Path(tempfile.mkdtemp(prefix='mp-minimal-b-')).resolve()
        print('\nRetained private-fixture evidence:', root, flush=True)
        project = root / 'project'; project.mkdir()
        shutil.copytree(SOURCE / 'core', project / 'core',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (project / 'config').mkdir()
        self.assertFalse((project / 'runtime').exists())
        self.assertFalse((project / '.model_proxy_runtime').exists())
        result = prepare_private_objects(project, root)
        self.assertEqual(result['runtime_preflight'], 'verified')
        self.assertEqual(result['record_directory'], 'verified')
        self.assertFalse((project / '.model_proxy_runtime/active.json').exists())
        contents = {r'config\runtime_paths.json': json.dumps({'fake': 'runtime'}),
                    r'config\model_proxy_config.json': json.dumps({'fake': 'config'})}
        for name, text in contents.items():
            (project / name).write_text(text, encoding='utf-8')
        verify_private_configs(project, root, result, contents, 'config-check-1')
        # A second write, as in reload, must keep the same validated objects.
        contents[r'config\model_proxy_config.json'] = json.dumps({'fake': 'updated'})
        (project / r'config\model_proxy_config.json').write_text(
            contents[r'config\model_proxy_config.json'], encoding='utf-8')
        verify_private_configs(project, root, result, contents, 'config-check-2')
        wrong = dict(contents)
        wrong[r'config\model_proxy_config.json'] = 'wrong'
        with self.assertRaisesRegex(RuntimeError, 'changed or unverified'):
            verify_private_configs(project, root, result, wrong, 'config-check-mismatch')

    def test_existing_config_is_not_modified(self):
        root = Path(tempfile.mkdtemp(prefix='mp-minimal-b-')).resolve()
        print('Retained collision evidence:', root, flush=True)
        project = root / 'project'; project.mkdir()
        shutil.copytree(SOURCE / 'core', project / 'core',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (project / 'config').mkdir()
        existing = project / 'config/runtime_paths.json'
        existing.write_text('untouched fake', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'fixture_target_already_exists'):
            prepare_private_objects(project, root)
        self.assertEqual(existing.read_text(encoding='utf-8'), 'untouched fake')
        self.assertFalse((project / 'runtime').exists())
        self.assertFalse((project / '.model_proxy_runtime').exists())

    def test_real_bat_help_with_private_fake_config(self):
        # Exercise only setUp's real .bat --help, never an acceptance test or on.
        harness = WindowsMinimalBPrecreated('test_five_commands_and_one_mock_request')
        try:
            harness.setUp()
            self.assertEqual(len(harness.commands), 1)
            result = harness.commands[0][3]
            self.assertEqual(result['returncode'], 0)
            self.assertEqual(result['children'], [])
        finally:
            self.assertTrue(harness.doCleanups(), 'owned fixture cleanup failed')


@unittest.skipUnless(os.name == 'nt' and os.environ.get('MP_RUN_WINDOWS_B_ACCEPTANCE') == '1',
                     'opt-in Windows/Whisky integration only')
class WindowsMinimalBAcceptance(unittest.TestCase):
    private_objects = False

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='mp-minimal-b-')).resolve()
        print('\nRetained fake-project evidence:', self.root, flush=True)
        self.project = self.root / 'project'; self.project.mkdir()
        self.hook = self.root / 'hook'; self.hook.mkdir()
        (self.hook / 'sitecustomize.py').write_text(GUARD, encoding='utf-8')
        shutil.copytree(SOURCE / 'core', self.project / 'core',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('model_proxy_cli.bat', 'VERSION'):
            shutil.copy2(SOURCE / name, self.project / name)
        (self.project / 'config').mkdir()
        if self.private_objects:
            print('Scenario: precreated, unprotected private objects; not first-install proof.', flush=True)
            self.private_initial = prepare_private_objects(self.project, self.root)
            self.private_check_count = 0
        else:
            print('Scenario: fresh install; product creates its state directory.', flush=True)
            (self.project / 'runtime').mkdir()
        for name in ('home', 'appdata', 'localappdata', 'temp'):
            (self.root / name).mkdir()
        runtime = {k: str(self.project / 'runtime' / k) for k in
                   ('log', 'totals', 'lock', 'pid', 'ensure_log', 'start_lock')}
        self.runtime_config_text = json.dumps(runtime)
        (self.project / 'config/runtime_paths.json').write_text(self.runtime_config_text, encoding='utf-8')
        self.config = self.project / 'config/model_proxy_config.json'
        self.mock = ThreadingHTTPServer(('127.0.0.1', 0), MockUpstream)
        self.mock.calls = []
        self.thread = threading.Thread(target=self.mock.serve_forever, daemon=True)
        self.thread.start(); self.addCleanup(self.stop_mock)
        self.foreign = None
        # LIFO: command handles first, then the foreign listener, then mock.
        self.addCleanup(self.stop_foreign)
        self.port = free_port()
        while self.port == self.mock.server_port: self.port = free_port()
        self.write_config('fake-model-v1')
        systemroot = os.environ.get('SystemRoot', r'C:\Windows')
        # Do not inherit real credentials, config or proxy environment.
        self.env = {k: v for k, v in os.environ.items() if k.upper() in
                    ('SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'SYSTEMDRIVE', 'NUMBER_OF_PROCESSORS')}
        self.env.update(HOME=str(self.root / 'home'), USERPROFILE=str(self.root / 'home'),
            APPDATA=str(self.root / 'appdata'), LOCALAPPDATA=str(self.root / 'localappdata'),
            TEMP=str(self.root / 'temp'), TMP=str(self.root / 'temp'),
            MODEL_PROXY_CONFIG=str(self.config), PYTHONDONTWRITEBYTECODE='1',
            PYTHONPATH=os.pathsep.join((str(self.hook), str(self.project))),
            MP_TEST_ENTRY=str(self.project / 'core/cli/model_proxy.py'),
            PATH=os.pathsep.join((str(Path(sys.executable).parent), str(Path(systemroot) / 'System32'),
                                  systemroot, str(Path(systemroot) / 'System32/WindowsPowerShell/v1.0'))))
        self.commands = []; self.addCleanup(self.cleanup_commands)
        # Prove the .bat actually reaches the hook before attempting on.
        result = self.call('--help')
        self.assertEqual(result['returncode'], 0, self.details(result))

    def stop_mock(self):
        self.mock.shutdown(); self.mock.server_close(); self.thread.join(10)
        if self.thread.is_alive(): raise RuntimeError('mock thread did not stop')

    def stop_foreign(self):
        if self.foreign is None:
            return
        foreign, directory, log = self.foreign
        (directory / 'release').touch()
        try:
            try:
                foreign.wait(timeout=10)
            except subprocess.TimeoutExpired:
                foreign.terminate()  # only this test's original Popen
                wait_owned(foreign, directory)
        finally:
            log.close()

    def cleanup_commands(self):
        errors = []
        for child, directory, output, result in self.commands:
            for marker in ('cancel', 'release'):
                try: (directory / marker).touch()
                except Exception as exc: errors.append('cannot create %s in %s: %r' % (marker, directory, exc))
        for child, directory, output, result in self.commands:
            try:
                wait_owned(child, directory)
                timing = directory / 'timing.json'
                if timing.exists():
                    data = read_json(timing)
                    if 'process_exit_seconds' not in data:
                        data['process_exit_seconds'] = round(time.monotonic() - data['started_monotonic'], 3)
                    data['process_exit_code'] = child.returncode
                    timing.write_text(json.dumps(data, indent=2), encoding='utf-8')
                    for entry in COMMAND_TIMINGS:
                        if entry.get('directory') == str(directory):
                            entry.update(data)
                            break
                if result is not None and result.get('error') is None and child.returncode != result['returncode']:
                    errors.append('CLI exit mismatch: ' + str(directory))
                cleanup = directory / 'cleanup.json'
                if not cleanup.is_file():
                    errors.append('missing cleanup.json; service cleanup unconfirmed: ' + str(directory))
                else:
                    state = json.loads(cleanup.read_text(encoding='utf-8'))
                    if not isinstance(state, dict) or not isinstance(state.get('errors'), list) \
                            or not isinstance(state.get('children'), list):
                        raise ValueError('invalid cleanup.json')
                    errors.extend(str(item) for item in state['errors'])
                    expected = result['children'] if result is not None else []
                    observed = state['children']
                    if result is not None and [item['pid'] for item in observed] != expected:
                        errors.append('cleanup child mismatch: ' + str(directory))
                    if any(item.get('returncode') is None for item in observed):
                        errors.append('owned child exit unconfirmed: ' + str(directory))
            except Exception as exc:
                errors.append('cleanup check failed for %s: %r' % (directory, exc))
            finally:
                try: output.close()
                except Exception as exc: errors.append('cannot close log %s: %r' % (directory, exc))
        if errors: raise RuntimeError('\n'.join(errors))

    def write_config(self, model):
        config = {'listen': {'host': '127.0.0.1', 'port': self.port}, 'admin_token': ADMIN,
            'upstream_timeout_seconds': 5,
            'supplies': [{'id': 'fake', 'url': 'http://127.0.0.1:%d/v1/messages' % self.mock.server_port,
                          'protocol': 'anthropic', 'appkey': UPSTREAM, 'target_model': model}],
            'routes': [{'id': 'fake-route', 'tiers': {'sonnet': ['fake']}, 'failover': 'off'}],
            'strategies': [{'client_token': CLIENT, 'route_id': 'fake-route'}]}
        text = json.dumps(config)
        self.config.write_text(text, encoding='utf-8')
        if self.private_objects:
            self.private_check_count += 1
            verify_private_configs(self.project, self.root, self.private_initial,
                                   {r'config\runtime_paths.json': self.runtime_config_text,
                                    r'config\model_proxy_config.json': text},
                                   'config-check-%d' % self.private_check_count)

    def call(self, command, direct=False, timeout=90):
        directory = self.root / ('cmd-%03d' % len(self.commands)); directory.mkdir()
        env = dict(self.env, MP_TEST_GUARD=str(directory))
        if direct:
            args = [sys.executable, '-B', str(self.project / 'core/cli/main.py'), command]
        else:
            # cmd.exe handles /c quoting; a list adds quotes Wine treats literally.
            args = '"%s" /d /s /c ""%s" %s"' % (env.get('COMSPEC', 'cmd.exe'),
                                                    self.project / 'model_proxy_cli.bat', command)
        output = (directory / 'cli.log').open('wb')
        started = time.monotonic()
        timing = {'command': command, 'entry': 'python' if direct else 'bat',
                  'directory': str(directory), 'started_monotonic': started,
                  'timeout_seconds': timeout, 'state': 'running'}
        COMMAND_TIMINGS.append(timing)
        timing_path = directory / 'timing.json'
        timing_path.write_text(json.dumps(timing, indent=2), encoding='utf-8')
        child = subprocess.Popen(args, cwd=self.project, env=env, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT)
        self.commands.append([child, directory, output, None])
        try:
            wait_for(lambda: (directory / 'result.json').exists() or child.poll() is not None, timeout)
        except TimeoutError:
            timing.update(state='timed_out_cancelling', dispatch_seconds=round(time.monotonic() - started, 3))
            timing_path.write_text(json.dumps(timing, indent=2), encoding='utf-8')
            marker_errors = []
            for marker in ('cancel', 'release'):
                try: (directory / marker).touch()
                except Exception as exc: marker_errors.append('%s: %r' % (marker, exc))
            # Never release the Popen or mock/foreign fixture merely because a
            # deadline expired. The guard interrupts its own CLI and reaps its
            # child; if an OS call is stuck, keep waiting with the handle held.
            wait_owned(child, directory)
            if (directory / 'result.json').exists():
                late = read_json(directory / 'result.json')
                late['directory'] = str(directory); self.commands[-1][3] = late
            timing.update(state='timed_out_reaped', process_exit_seconds=round(time.monotonic() - started, 3),
                          process_exit_code=child.returncode, marker_errors=marker_errors)
            timing_path.write_text(json.dumps(timing, indent=2), encoding='utf-8')
            self.fail('CLI timed out; owned command reaped after cancellation: ' + str(directory) +
                      ('; marker errors: ' + repr(marker_errors) if marker_errors else ''))
        if not (directory / 'result.json').exists():
            self.fail('CLI hook not reached, exit=%s; log=%s' %
                      (child.poll(), (directory / 'cli.log').read_text(errors='replace')))
        result = read_json(directory / 'result.json')
        result['directory'] = str(directory); self.commands[-1][3] = result
        timing.update(state='returned', dispatch_seconds=round(time.monotonic() - started, 3),
                      returncode=result['returncode'])
        timing_path.write_text(json.dumps(timing, indent=2), encoding='utf-8')
        self.assertIsNone(result['error'], self.details(result))
        if command not in ('on', 'restart'):
            self.assertEqual(result['children'], [], self.details(result))
        if not result['children']:
            child.wait(timeout=15)
            timing.update(process_exit_seconds=round(time.monotonic() - started, 3),
                          process_exit_code=child.returncode)
            timing_path.write_text(json.dumps(timing, indent=2), encoding='utf-8')
            self.assertEqual(child.returncode, result['returncode'], self.details(result))
        return result

    def details(self, result):
        directory = Path(result['directory']); parts = [repr(result)]
        for name in ('cli.log', 'service.log'):
            path = directory / name
            if path.exists(): parts.append(name + ':\n' + path.read_text(encoding='utf-8', errors='replace'))
        return '\n'.join(parts)

    def live_children(self, result):
        directory = Path(result['directory']); nonce = str(time.monotonic_ns())
        (directory / 'inspect').write_text(nonce, encoding='utf-8')
        def read():
            try:
                data = json.loads((directory / 'live.json').read_text(encoding='utf-8'))
                return data if data['nonce'] == nonce else None
            except (OSError, ValueError, KeyError): return None
        return wait_for(read, 10)['children']

    def require_owned_service(self, result):
        active = json.loads((self.project / '.model_proxy_runtime/active.json').read_text(encoding='utf-8'))
        pid = active.get('pid')
        self.assertIn(pid, result['children'], 'refusing to control an uncaptured process')
        self.assertTrue(any(c['pid'] == pid and c['exit'] is None for c in self.live_children(result)),
                        'refusing to control an exited Popen')

    def test_five_commands_and_one_mock_request(self):
        with socket.socket() as probe: probe.bind(('127.0.0.1', self.port))
        started = self.call('on')
        self.assertEqual(started['returncode'], 0, self.details(started))
        self.assertEqual(len(started['children']), 1, self.details(started))
        self.require_owned_service(started)
        for direct in (False, True):  # real .bat and shared Python CLI
            status = self.call('status', direct=direct)
            self.assertEqual(status['returncode'], 0, self.details(status))
            text = Path(status['directory'], 'cli.log').read_text(errors='replace').lower()
            self.assertIn('running', text)
            self.assertNotIn('not running', text)
        self.write_config('fake-model-v2')
        reload_result = self.call('reload')
        self.assertEqual(reload_result['returncode'], 0, self.details(reload_result))
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            body = json.dumps({'model': 'claude-sonnet', 'max_tokens': 16,
                               'messages': [{'role': 'user', 'content': 'hello'}]})
            conn.request('POST', '/v1/messages', body, {'Content-Type': 'application/json',
                         'x-api-key': CLIENT, 'anthropic-version': '2023-06-01'})
            response = conn.getresponse(); payload = response.read()
            self.assertEqual(response.status, 200, payload)
            self.assertEqual(json.loads(payload)['content'][0]['text'], 'mock-reply')
        finally: conn.close()
        self.assertEqual(len(self.mock.calls), 1)
        path, headers, request = self.mock.calls[0]
        self.assertEqual((path, headers.get('x-api-key'), request['model']),
                         ('/v1/messages', UPSTREAM, 'fake-model-v2'))
        self.require_owned_service(started)
        restarted = self.call('restart')
        self.assertEqual(restarted['returncode'], 0, self.details(restarted))
        self.assertEqual(len(restarted['children']), 1, self.details(restarted))
        self.assertNotEqual(started['children'][0], restarted['children'][0])
        self.assertIsNotNone(self.live_children(started)[0]['exit'])
        self.require_owned_service(restarted)
        stopped = self.call('off')
        self.assertEqual(stopped['returncode'], 0, self.details(stopped))
        self.assertIsNotNone(self.live_children(restarted)[0]['exit'])
        with socket.socket() as probe: probe.bind(('127.0.0.1', self.port))

    def test_foreign_port_is_not_claimed_or_stopped(self):
        directory = self.root / 'foreign'; directory.mkdir()
        log = (directory / 'foreign.log').open('wb')
        foreign = subprocess.Popen([sys.executable, '-B', '-c', FOREIGN, str(directory), str(self.port)],
                                   env=self.env, stdout=log, stderr=subprocess.STDOUT)
        self.foreign = (foreign, directory, log)
        wait_for(lambda: (directory / 'ready').exists() or foreign.poll() is not None, 15)
        self.assertIsNone(foreign.poll(), (directory / 'foreign.log').read_text(errors='replace'))
        for command in ('on', 'status', 'reload', 'off', 'restart'):
            result = self.call(command)
            self.assertNotEqual(result['returncode'], 0, self.details(result))
            self.assertEqual(result['children'], [], self.details(result))
            self.assertIsNone(foreign.poll(), command + ' stopped the foreign fixture')
        # Drain the fixture's already-accepted connections before checking logs.
        sentinel = b'MP-TEST-END'
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as conn: conn.sendall(sentinel)
        traffic = directory / 'traffic'
        wait_for(lambda: traffic.exists() and sentinel in traffic.read_bytes(), 10)
        self.assertNotIn(ADMIN.encode(), traffic.read_bytes(), 'admin token sent to foreign listener')


@unittest.skipUnless(os.name == 'nt' and os.environ.get('MP_RUN_WINDOWS_B_PRIVATE') == '1',
                     'explicit precreated-private-object integration only')
class WindowsMinimalBPrecreated(WindowsMinimalBAcceptance):
    # The base suite's fresh-install opt-in must not gate this independent one.
    __unittest_skip__ = False
    private_objects = True


def observed_environment():
    """Describe observable runtime, without claiming native Windows validation."""
    runtime, wine_version = sys.platform, None
    if os.name == 'nt':
        try:
            import ctypes
            ntdll = ctypes.WinDLL('ntdll')
            try:
                wine = ntdll.wine_get_version
            except AttributeError:
                runtime = 'windows_wine_export_not_detected'
            else:
                wine.restype = ctypes.c_char_p
                value = wine()
                runtime = 'wine'
                wine_version = value.decode('ascii', errors='replace') if value else None
        except Exception:
            runtime = 'windows_detection_unknown'
    return {'os_name': os.name, 'sys_platform': sys.platform,
            'platform': platform.platform(), 'python_version': sys.version,
            'python_executable': sys.executable, 'runtime': runtime, 'wine_version': wine_version}


def summarize_result(suite, result):
    skipped = [{'test': test.id(), 'reason': reason} for test, reason in result.skipped]
    failures = [{'test': test.id(), 'traceback': text} for test, text in result.failures]
    errors = [{'test': test.id(), 'traceback': text} for test, text in result.errors]
    expected = [{'test': test.id(), 'traceback': text} for test, text in result.expectedFailures]
    unexpected = [test.id() for test in result.unexpectedSuccesses]
    executed = result.testsRun - len(skipped)
    success = bool(executed > 0 and not (skipped or failures or errors or expected or unexpected))
    not_validated = ['native Windows is not established by this result',
                     'untested platforms, network modes and protocol cases']
    integration = suite in ('WindowsMinimalBAcceptance', 'WindowsMinimalBPrecreated')
    precreated = suite in ('WindowsMinimalBPrecreated', 'WinePrivateFixtureSelfTest')
    if precreated:
        not_validated.append('first-install creation of protected DACL objects was not tested')
    if not integration:
        not_validated.append('product five-command integration was not run')
    elif not success:
        not_validated.append('five-command integration has not passed')
    scope = {'WindowsMinimalBAcceptance': 'fresh state-directory creation and five-command integration',
             'WindowsMinimalBPrecreated': 'precreated unprotected private objects and five-command integration',
             'WinePrivateFixtureSelfTest': 'private fixture and real bat --help only; no product service started'}.get(
                 suite, 'fixture/self-test only')
    return {'suite': suite, 'environment': observed_environment(),
            'tests_run': result.testsRun, 'executed': executed, 'skipped': len(skipped),
            'failures': len(failures), 'errors': len(errors),
            'expected_failures': len(expected), 'unexpected_successes': len(unexpected),
            'skipped_details': skipped, 'failure_details': failures, 'error_details': errors,
            'expected_failure_details': expected, 'unexpected_success_details': unexpected,
            'success': success, 'commands': COMMAND_TIMINGS,
            'scope': scope,
            'not_validated': not_validated}


def bridge_child(suite, metadata):
    tests = unittest.defaultTestLoader.loadTestsFromName(suite, sys.modules['__main__'])
    planned = tests.countTestCases()
    result = unittest.TextTestRunner(
        verbosity=2, failfast=suite in ('WindowsMinimalBAcceptance', 'WindowsMinimalBPrecreated')).run(tests)
    summary = summarize_result(suite, result)
    summary['planned'] = planned
    Path(metadata).write_text(json.dumps(summary, ensure_ascii=False), encoding='utf-8')
    return summary['success']


def bridge(result_name, suite):
    """Wait for the child; preserve its counts, environment, exit code and log."""
    result = Path(result_name); log = result.with_suffix('.log')
    metadata = result.with_suffix('.child.json')
    if any(path.exists() for path in (result, log, metadata)):
        raise SystemExit('choose a fresh result path')
    env = dict(os.environ)
    flags = {'WindowsMinimalBAcceptance': 'MP_RUN_WINDOWS_B_ACCEPTANCE',
             'WindowsMinimalBPrecreated': 'MP_RUN_WINDOWS_B_PRIVATE',
             'WinePrivateFixtureSelfTest': 'MP_RUN_PRIVATE_FIXTURE_SELFTEST'}
    for flag in flags.values():
        env.pop(flag, None)
    if suite in flags:
        env[flags[suite]] = '1'
    with log.open('xb') as stream:
        child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()),
                                  '--bridge-child', suite, str(metadata)],
                                 env=env, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT)
        code = child.wait()  # never kill a parent that may hold service handles
    try:
        summary = json.loads(metadata.read_text(encoding='utf-8'))
        if not isinstance(summary, dict) or summary.get('suite') != suite:
            raise ValueError('invalid child metadata')
        summary['success'] = summary.get('success') is True and code == 0
        if code != 0:
            summary.setdefault('not_validated', []).append('test subprocess exited nonzero')
        summary['completion'] = 'complete'
    except (OSError, ValueError, TypeError):
        summary = {'suite': suite, 'environment': observed_environment(), 'tests_run': None,
                   'executed': None, 'skipped': None, 'failures': None, 'errors': None,
                   'success': False, 'completion': 'incomplete: child metadata unavailable or invalid',
                   'not_validated': ['test results and all platform claims unconfirmed']}
    summary.update(exit_code=code, log=str(log))
    tmp = result.with_suffix('.tmp')
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, result)
    return summary['success']


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--bridge-child':
        raise SystemExit(0 if bridge_child(sys.argv[2], sys.argv[3]) else 1)
    modes = {'--whisky-bridge': 'WindowsMinimalBAcceptance',
             '--whisky-private-bridge': 'WindowsMinimalBPrecreated',
             '--self-test-bridge': 'HarnessSelfTest',
             '--private-fixture-self-test-bridge': 'WinePrivateFixtureSelfTest'}
    if len(sys.argv) == 3 and sys.argv[1] in modes:
        raise SystemExit(0 if bridge(sys.argv[2], modes[sys.argv[1]]) else 1)
    else:
        unittest.main()

# After both developers deliver, replace paths with those of the py-test bottle:
# whisky run py-test 'C:\path\python.exe' -- -B \
#   'Z:\path\test_windows_minimal_b_integration.py' --whisky-bridge \
#   'Z:\path\fresh-test-dir\result.json'
# Use --whisky-private-bridge with a different fresh result path for the
# separate precreated-object scenario. --private-fixture-self-test-bridge
# exercises B's helper and real .bat --help, never starts the product service.
# Wait for result.json, then inspect its exit_code and result.log.
