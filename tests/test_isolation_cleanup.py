"""Fixture-only tests: no project service or production paths are opened."""
import ast
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import isolation_bootstrap as bootstrap
import worktree_regression as regression
from isolation_bootstrap import DEFAULT_PATHS, PATH_KEYS, Scratch, copy_core, overlaps, resolved_default_paths
from worktree_regression import (A2R_INPUT, A2R_OUTPUT, A2R_SYSTEM, FAKE_SOURCE,
                                 R2A_INPUT, R2A_OUTPUT, R2A_SYSTEM, TTYWorker, read_sse_event,
                                 reserve, run, stop_managed, attestation_for_test, upstream_response, upstream_sse,
                                 validate_fresh_project, validate_mock_request, verify_json_response,
                                 verify_status, verify_stream_events, write_local_json)

class ScratchTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Scratch(HERE.parent)
    def tearDown(self):
        self.scratch.cleanup()
    def test_six_paths_and_fake_config_are_under_project(self):
        self.scratch.stage(True, 29381, 29382)
        paths = json.loads((self.scratch.project / 'config/runtime_paths.json').read_text())
        self.assertEqual(set(paths), {'log', 'totals', 'lock', 'pid', 'ensure_log', 'start_lock'})
        self.assertEqual(len(set(paths.values())), 6)
        self.assertEqual(paths, {
            'log': '.runtime/model_proxy.log', 'totals': '.runtime/model_proxy_totals.json',
            'lock': '.runtime/model_proxy.lock', 'pid': '.runtime/model_proxy.pid',
            'ensure_log': '.runtime/model_proxy_ensure.log', 'start_lock': '.runtime/model_proxy_start.lock',
        })
        self.assertTrue(all(Path(p).is_relative_to(self.scratch.project / '.runtime')
                            for p in resolved_default_paths(self.scratch.project).values()))
        self.assertNotIn('/tmp/model_proxy', json.dumps(paths))
        data = (self.scratch.project / 'config/model_proxy_config.json').read_text()
        self.assertIn('fake-upstream-for-test-only', data)
        self.assertNotIn('https://', data)
        config = json.loads(data)
        self.assertEqual([s['protocol'] for s in config['supplies']], ['anthropic', 'responses', 'anthropic'])
        self.assertEqual(len(config['routes']), 3)
        self.assertEqual(len(config['strategies']), 3)
        self.assertTrue(all(s['url'].startswith('http://127.0.0.1:29382/') for s in config['supplies']))
        self.assertEqual((self.scratch.project / 'config/model_proxy_config.json').stat().st_mode & 0o077, 0)
        env = self.scratch.env(29381, 29382)
        self.assertTrue(all(Path(env[key]).is_relative_to(self.scratch.root) for key in
                            ('HOME', 'TMPDIR', 'TMP', 'TEMP', 'MODEL_PROXY_CONFIG')))
    def make_synthetic_source(self, content=None):
        # Never copy or execute the concurrently changing product source.
        source = self.scratch.root / 'synthetic-source'
        (source / 'core/cli').mkdir(parents=True)
        (source / 'tests').mkdir()
        (source / 'core/cli/main.py').write_text('# synthetic source, never executed\n')
        (source / 'VERSION').write_text('fixture-version')
        for name in ('isolation_bootstrap.py', 'isolated_runner.py'):
            (source / 'tests' / name).write_text('# synthetic guard input, never executed\n')
        prefix = (b'def initialize(stdout, stdin):\n'
                  b'    if True:\n        if True:\n            if True:\n                if True:\n')
        suffix = b'                    return source, confirmation\n'
        authority = source / 'core/cli/authority.py'
        authority.write_bytes(content if content is not None else
                              prefix + bootstrap.ORIGINAL_INITIALIZATION_CONTEXT + suffix)
        self.scratch.source = source
        return authority

    def test_real_mode_staging_uses_only_synthetic_source_and_fake_config(self):
        authority = self.make_synthetic_source()
        original = authority.read_bytes()
        entry = self.scratch.stage(False, 29481, 29482)
        copied = (self.scratch.project / 'core/cli/authority.py').read_bytes()
        literal = repr(attestation_for_test(29481, 29482, self.scratch.root.name).decode() + '\n').encode()
        self.assertEqual(original.count(bootstrap.ORIGINAL_CONFIRMATION), 1)
        self.assertEqual(copied, original.replace(bootstrap.ORIGINAL_CONFIRMATION, literal, 1))
        self.assertEqual(authority.read_bytes(), original)
        self.scratch.assert_authority_unchanged()
        # String substitution must not alter the Python AST except that one
        # concatenated prompt constant. The target code itself is never run.
        old_prompt = ('Confirm no old starter or enabled autostart, no shared ledger, '
                      'and no cloud-synchronized installation or data.\n'
                      'Installation/recovery source: ')
        new_prompt = attestation_for_test(29481, 29482, self.scratch.root.name).decode() + '\nInstallation/recovery source: '
        class Normalize(ast.NodeTransformer):
            def visit_Constant(self, node):
                if node.value in (old_prompt, new_prompt):
                    return ast.copy_location(ast.Constant(value='PROMPT'), node)
                return node
        before_ast = Normalize().visit(ast.parse(original))
        after_ast = Normalize().visit(ast.parse(copied))
        self.assertEqual(ast.dump(before_ast), ast.dump(after_ast))
        self.assertEqual(entry, self.scratch.project / 'core/cli/main.py')
        self.assertEqual(json.loads((self.scratch.project / 'config/runtime_paths.json').read_text()), DEFAULT_PATHS)
        self.assertFalse((self.scratch.project / '.model_proxy_runtime').exists())
        self.assertFalse((self.scratch.project / 'runtime').exists())
        validate_fresh_project(self.scratch, 29481, 29482)

    def test_source_change_during_copy_refuses_staging(self):
        authority = self.make_synthetic_source()
        original_copy = bootstrap.copy_core
        def copy_then_change(src, dst):
            original_copy(src, dst)
            authority.write_bytes(authority.read_bytes() + b'# changed concurrently\n')
        with patch.object(bootstrap, 'copy_core', side_effect=copy_then_change):
            with self.assertRaisesRegex(RuntimeError, 'source authority changed'):
                self.scratch.stage(False, 29481, 29482)
        self.assertFalse((self.scratch.project / 'config/model_proxy_config.json').exists())

    def test_copy_change_during_staging_is_rejected(self):
        authority = self.make_synthetic_source()
        original = authority.read_bytes()
        original_copy = bootstrap.copy_core
        def copy_then_change(src, dst):
            original_copy(src, dst)
            (dst / 'cli/authority.py').write_bytes(original + b'# changed copy\n')
        with patch.object(bootstrap, 'copy_core', side_effect=copy_then_change):
            with self.assertRaisesRegex(RuntimeError, 'exact initialization prompt differs'):
                self.scratch.stage(False, 29481, 29482)
        self.assertEqual(authority.read_bytes(), original)

    def test_exact_prompt_and_context_are_required_once(self):
        authority = self.make_synthetic_source()
        original = authority.read_bytes()
        variants = [original.replace(bootstrap.ORIGINAL_CONFIRMATION, b'"changed"'),
                    original + b'\n# duplicate literal below\n' + bootstrap.ORIGINAL_CONFIRMATION,
                    original.replace(b'stdout.flush()', b'stdout.flush  ()', 1)]
        for content in variants:
            with self.subTest(content=content[-100:]):
                authority.write_bytes(content)
                # Capture and copy only into this test's fresh isolated target.
                self.scratch.authority_source = authority
                self.scratch.authority_snapshot = bootstrap._snapshot_regular(authority)
                target = self.scratch.project / 'core/cli/authority.py'
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                with self.assertRaisesRegex(RuntimeError, 'exact initialization prompt differs'):
                    self.scratch._patch_copied_authority(29481, 29482)
                self.assertEqual(target.read_bytes(), content)
                self.assertEqual(authority.read_bytes(), content)

    def test_source_change_after_staging_is_rejected(self):
        authority = self.make_synthetic_source()
        self.scratch.stage(False, 29481, 29482)
        authority.write_bytes(authority.read_bytes() + b'# later change\n')
        with self.assertRaisesRegex(RuntimeError, 'source authority changed'):
            self.scratch.assert_authority_unchanged()

    def test_copied_authority_change_after_staging_is_rejected(self):
        self.make_synthetic_source()
        self.scratch.stage(False, 29481, 29482)
        target = self.scratch.project / 'core/cli/authority.py'
        target.write_bytes(target.read_bytes() + b'# changed after patch\n')
        with self.assertRaisesRegex(RuntimeError, 'copied authority changed'):
            self.scratch.assert_authority_unchanged()

    def test_fresh_project_rejects_ledger_records_and_other_files(self):
        self.scratch.stage(True, 29481, 29482)
        state = self.scratch.project / '.runtime'
        for name in ('install.json', 'active.json', 'last_good_paths.json',
                     'model_proxy_totals.json', 'unexpected'):
            path = state / name
            with self.subTest(name=name):
                path.write_text('{}')
                with self.assertRaisesRegex(RuntimeError, 'uninitialized'):
                    validate_fresh_project(self.scratch, 29481, 29482, preparing=True)
                path.unlink()
        for name in ('control.lock', 'anchor.lock', 'model_proxy.lock'):
            (state / name).touch()
        with self.assertRaisesRegex(RuntimeError, 'uninitialized'):
            validate_fresh_project(self.scratch, 29481, 29482)
        validate_fresh_project(self.scratch, 29481, 29482, preparing=True)

    def test_fresh_project_rejects_bad_paths_and_symlinked_lock(self):
        self.scratch.stage(True, 29481, 29482)
        paths_path = self.scratch.project / 'config/runtime_paths.json'
        paths_path.write_text(json.dumps(dict(DEFAULT_PATHS, totals='/tmp/model_proxy_totals.json')))
        with self.assertRaisesRegex(RuntimeError, 'fake-only'):
            validate_fresh_project(self.scratch, 29481, 29482)
        paths_path.write_text(json.dumps(DEFAULT_PATHS))
        lock = self.scratch.project / '.runtime/anchor.lock'
        lock.symlink_to('/tmp/never-read-sentinel')
        with self.assertRaisesRegex(RuntimeError, 'uninitialized'):
            validate_fresh_project(self.scratch, 29481, 29482, preparing=True)

    def test_fresh_project_rejects_hardlinked_config_before_reading(self):
        self.scratch.stage(True, 29481, 29482)
        config = self.scratch.project / 'config/model_proxy_config.json'
        config.unlink()
        with tempfile.TemporaryDirectory(prefix='mp-config-sentinel-') as external:
            target = Path(external) / 'sentinel'
            target.write_text('not json or a test config')
            target.chmod(0o600)
            os.link(target, config)
            with self.assertRaisesRegex(RuntimeError, 'single-link'):
                validate_fresh_project(self.scratch, 29481, 29482)
            self.assertEqual(target.read_text(), 'not json or a test config')

    def test_reload_writer_rejects_hardlinks_without_truncation(self):
        self.scratch.stage(True, 29481, 29482)
        config = self.scratch.project / 'config/model_proxy_config.json'
        config.unlink()
        with tempfile.TemporaryDirectory(prefix='mp-config-sentinel-') as external:
            target = Path(external) / 'sentinel'
            target.write_text('untouched')
            os.link(target, config)
            with self.assertRaisesRegex(RuntimeError, 'single-link'):
                write_local_json(config, {'fake': True})
            self.assertEqual(target.read_text(), 'untouched')

    def test_symlink_cleanup_does_not_touch_target(self):
        with tempfile.TemporaryDirectory(prefix='mp-sentinel-') as external:
            sentinel = Path(external) / 'sentinel'
            sentinel.write_text('keep')
            (self.scratch.root / 'link').symlink_to(external, target_is_directory=True)
            self.scratch.cleanup()
            self.assertEqual(sentinel.read_text(), 'keep')
    def test_replaced_root_never_deleted(self):
        root = self.scratch.root
        moved = root.with_name(root.name + '-original')
        root.rename(moved)
        root.mkdir()
        try:
            with self.assertRaises(RuntimeError):
                self.scratch.cleanup()
            self.assertTrue(root.exists())
        finally:
            root.rmdir()
            moved.rename(root)
    def test_python_guard_denies_network_external_files_and_nonchildren(self):
        self.scratch.stage(True, 29381, 29382)
        program = r'''
import os, socket, subprocess, sys
sys.path.insert(0, os.environ['PYTHONPATH'])
import isolation_bootstrap
isolation_bootstrap.install_guard()
assert os.environ.get('MP_TEST_GUARD_INSTALLED') == '1'
def must_deny(f):
    try: f()
    except PermissionError: return
    raise AssertionError('expected PermissionError')
must_deny(lambda: socket.getaddrinfo('example.com', 443))
must_deny(lambda: socket.gethostbyname('outside.invalid'))
must_deny(lambda: socket.gethostbyname_ex('outside.invalid'))
must_deny(lambda: socket.gethostbyaddr('203.0.113.1'))
must_deny(lambda: socket.getnameinfo(('203.0.113.1', 80), 0))
must_deny(lambda: socket.getfqdn('outside.invalid'))
assert socket.gethostbyname('localhost') == '127.0.0.1'
assert socket.gethostbyname_ex('localhost')[2] == ['127.0.0.1']
assert socket.gethostbyaddr('127.0.0.1')[2] == ['127.0.0.1']
assert socket.getnameinfo(('127.0.0.1', 29381), 0) == ('127.0.0.1', '29381')
assert socket.getfqdn('localhost') == 'localhost'
assert socket.getaddrinfo('localhost', 29381, type=socket.SOCK_STREAM)[0][4][0] == '127.0.0.1'
must_deny(lambda: socket.socket().connect(('127.0.0.1', 1)))
must_deny(lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'x', ('127.0.0.1', 29381)))
must_deny(lambda: os.stat('/tmp/model_proxy.pid'))
must_deny(lambda: open(os.environ['MP_TEST_REAL_HOME'] + '/fake-sentinel', 'w'))
ancestor = os.path.dirname(os.environ['MP_TEST_ROOT'])
fd = os.open(ancestor, os.O_RDONLY | os.O_DIRECTORY)
os.close(fd)
must_deny(lambda: os.open(ancestor, os.O_RDONLY))
must_deny(lambda: os.open(ancestor, os.O_WRONLY | os.O_DIRECTORY))
must_deny(lambda: os.kill(os.getpid(), 0))
must_deny(lambda: subprocess.Popen(['/bin/echo', 'no']))
must_deny(lambda: subprocess.Popen([sys.executable, '-I', '-c', 'pass']))
print('guard-ok')
'''
        result = subprocess.run([sys.executable, '-S', '-B', '-c', program], env=self.scratch.env(29381, 29382),
                                cwd=self.scratch.project, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'guard-ok')

    def test_confirmation_rejects_non_fake_configuration(self):
        self.scratch.stage(True, 29481, 29482)
        validate_fresh_project(self.scratch, 29481, 29482)
        path = self.scratch.project / 'config/model_proxy_config.json'
        config = json.loads(path.read_text())
        config['supplies'][0]['url'] = 'https://outside.invalid/v1/messages'
        path.write_text(json.dumps(config))
        with self.assertRaisesRegex(RuntimeError, 'fake-only'):
            validate_fresh_project(self.scratch, 29481, 29482)

    def test_confirmation_rejects_existing_installation(self):
        self.scratch.stage(True, 29481, 29482)
        state = self.scratch.project / '.runtime'
        (state / 'install.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'uninitialized'):
            validate_fresh_project(self.scratch, 29481, 29482, preparing=True)

    def test_detached_owned_service_and_exited_pid_are_scoped(self):
        self.scratch.stage(True, 29481, 29482)
        service = self.scratch.project / 'service.py'
        service.write_text("import os, site, sys\n"
                           "assert os.environ.get('MP_TEST_GUARD_INSTALLED') == '1'\n"
                           "assert not site.ENABLE_USER_SITE and sys.prefix != sys.base_prefix\n"
                           "assert len(sys.orig_argv) == 2 and sys.orig_argv[1] == __file__\n"
                           "print('guarded-exact-argv')\n")
        other = self.scratch.project / 'other.py'
        other.write_text("pass\n")
        program = r'''
import os, subprocess, sys
sys.path.insert(0, os.environ['PYTHONPATH'])
import isolation_bootstrap
isolation_bootstrap.install_guard()
def must_deny(f):
    try: f()
    except PermissionError: return
    raise AssertionError('expected PermissionError')
root = os.environ['MP_TEST_PROJECT']
service = os.path.join(root, 'service.py')
must_deny(lambda: subprocess.Popen([sys.executable, os.path.join(root, 'other.py')], start_new_session=True))
p = subprocess.Popen([sys.executable, service], start_new_session=True,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
out, err = p.communicate(timeout=5)
assert p.returncode == 0, err
assert out.strip() == b'guarded-exact-argv'
try: os.kill(p.pid, 0)
except ProcessLookupError: pass
else: raise AssertionError('exited child must report ESRCH')
must_deny(lambda: os.kill(os.getpid(), 0))
# Changing the bootstrap must be detected before any further child starts.
with open(os.path.join(os.environ['PYTHONPATH'], 'sitecustomize.py'), 'a') as f:
    f.write('\n# changed\n')
must_deny(lambda: subprocess.Popen([sys.executable, service], start_new_session=True))
print('owned-child-ok')
'''
        result = subprocess.run([str(self.scratch.python), '-S', '-B', '-c', program],
                                env=self.scratch.env(29481, 29482), cwd=self.scratch.project,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'owned-child-ok')

    def test_pty_never_answers_unexpected_prompt(self):
        entry = self.scratch.stage(True, 29481, 29482)
        # Fake executable prints a prompt without the required preceding context.
        for prompt, error in (('Type YES to confirm: ', 'without fake source prompt'),
                              ('Installation/recovery source: ', 'unexpected initialization prompt')):
            with self.subTest(prompt=prompt):
                entry.write_text('print(%r, end="", flush=True)\n' % prompt)
                worker = TTYWorker(self.scratch, entry, 29481, 29482)
                try:
                    with self.assertRaisesRegex(RuntimeError, error):
                        worker.command('on')
                finally:
                    worker.close()
                self.assertEqual(worker.prompts, 0)
        self.assertFalse((self.scratch.project / '.runtime/install.json').exists())

    def test_pty_rejects_legacy_and_extra_global_attestations(self):
        entry = self.scratch.stage(True, 29481, 29482)
        old = ('Confirm no old starter or enabled autostart, no shared ledger, '
               'and no cloud-synchronized installation or data.')
        safe = attestation_for_test(29481, 29482).decode()
        for text in (old, safe + '\n' + old, old + '\n' + safe,
                     safe.replace('29481', '29483')):
            with self.subTest(text=text):
                entry.write_text('print(%r, flush=True)\nprint(%r, end="", flush=True)\n' %
                                 (text, 'Installation/recovery source: '))
                worker = TTYWorker(self.scratch, entry, 29481, 29482)
                try:
                    with self.assertRaisesRegex(RuntimeError, 'unexpected initialization prompt'):
                        worker.command('on')
                finally:
                    worker.close()
                self.assertEqual(worker.prompts, 0)
                self.assertFalse((self.scratch.project / '.runtime/install.json').exists())

    def test_pty_accepts_only_its_random_fake_project_attestation(self):
        entry = self.scratch.stage(True, 29481, 29482)
        safe = attestation_for_test(29481, 29482, self.scratch.root.name).decode()
        entry.write_text('print(%r, flush=True)\n'
                         'source = input("Installation/recovery source: ")\n'
                         'yes = input("Type YES to confirm: ")\n'
                         'assert source == %r and yes == "YES"\n' % (safe, FAKE_SOURCE))
        source = self.scratch.root / 'source-authority.py'
        source.write_text('# synthetic only')
        copied = self.scratch.project / 'core/cli/authority.py'
        copied.parent.mkdir(parents=True)
        copied.write_text('# synthetic prompt copy')
        self.scratch.authority_source = source
        self.scratch.authority_snapshot = bootstrap._snapshot_regular(source)
        self.scratch.patched_authority_snapshot = bootstrap._snapshot_regular(copied)
        worker = TTYWorker(self.scratch, entry, 29481, 29482, fixture=False)
        try:
            worker.command('on', quiet=True)
            self.assertEqual(worker.prompts, 1)
        finally:
            worker.close()

    def test_pty_rejects_other_random_project_attestation(self):
        entry = self.scratch.stage(True, 29481, 29482)
        wrong = attestation_for_test(29481, 29482, 'mp-regression-12345678').decode()
        if wrong == attestation_for_test(29481, 29482, self.scratch.root.name).decode():
            self.skipTest('random project name happened to match')
        entry.write_text('print(%r, flush=True)\nprint(%r, end="", flush=True)\n' %
                         (wrong, 'Installation/recovery source: '))
        worker = TTYWorker(self.scratch, entry, 29481, 29482, fixture=False)
        try:
            with self.assertRaisesRegex(RuntimeError, 'unexpected initialization prompt'):
                worker.command('on')
            self.assertEqual(worker.prompts, 0)
        finally:
            worker.close()

    def test_pty_rechecks_source_and_copy_before_both_answers(self):
        entry = self.scratch.stage(True, 29481, 29482)
        safe = attestation_for_test(29481, 29482, self.scratch.root.name).decode()
        for fail_check in (1, 2):
            with self.subTest(fail_check=fail_check):
                # This synthetic CLI exits after emitting the second prompt;
                # no actual CLI or service is launched by this unit test.
                entry.write_text('print(%r, flush=True)\n'
                                 'input("Installation/recovery source: ")\n'
                                 'print("Type YES to confirm: ", end="", flush=True)\n' % safe)
                worker = TTYWorker(self.scratch, entry, 29481, 29482, fixture=False)
                results = ([RuntimeError('source changed')] if fail_check == 1 else
                           [None, RuntimeError('source changed')])
                try:
                    with patch.object(self.scratch, 'assert_authority_unchanged', side_effect=results) as check:
                        with self.assertRaisesRegex(RuntimeError, 'source changed'):
                            worker.command('on')
                        self.assertEqual(check.call_count, fail_check)
                    self.assertEqual(worker.prompts, 0)
                finally:
                    # Unblock only the synthetic CLI after the rejection check;
                    # this is not a confirmation and no service was started.
                    if fail_check == 1 and worker.process.poll() is None:
                        os.write(worker.master, b'unblock-only\n')
                    worker.close()
                    if worker.process.poll() is None:
                        worker.process.kill()  # the exact direct worker child
                        worker.process.wait(timeout=5)

    def test_random_name_and_ports_cannot_be_injected_into_literal(self):
        for bad in ('not-random', 'mp-regression-1234567;', 'mp-regression-12345678\n'):
            with self.subTest(name=bad), self.assertRaises(RuntimeError):
                attestation_for_test(29481, 29482, bad)
        self.make_synthetic_source()
        self.scratch.authority_source = self.scratch.source / 'core/cli/authority.py'
        self.scratch.authority_snapshot = bootstrap._snapshot_regular(self.scratch.authority_source)
        for ports in ((18889, 29482), (29481, 29481), (1, 29482), (29481, True)):
            with self.subTest(ports=ports), self.assertRaisesRegex(RuntimeError, 'invalid isolated test ports'):
                self.scratch._patch_copied_authority(*ports)


class ProtocolChecks(unittest.TestCase):
    def test_mock_requires_correct_cross_protocol_shape_and_credentials(self):
        requests = [
            ('a2r', '/v1/responses', 'fake-upstream-a2r', {
                'model': 'fixture-model-a2r', 'stream': True, 'instructions': A2R_SYSTEM,
                'input': [{'type': 'message', 'role': 'user',
                           'content': [{'type': 'input_text', 'text': A2R_INPUT}]}]}),
            ('r2a', '/v1/messages', 'fake-upstream-r2a', {
                'model': 'fixture-model-r2a', 'stream': False, 'system': R2A_SYSTEM,
                'messages': [{'role': 'user', 'content': R2A_INPUT}]}),
        ]
        for case, path, key, body in requests:
            headers = {'x-api-key': key, 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
            with self.subTest(case=case):
                self.assertEqual(validate_mock_request(path, headers, body), case)
                for bad_path, bad_headers, bad_body in [
                    ('/v1/wrong', headers, body),
                    (path, dict(headers, Authorization='Bearer wrong'), body),
                    (path, dict(headers, **{'Content-Type': 'text/plain'}), body),
                    (path, headers, dict(body, model='wrong')),
                    (path, headers, dict(body, stream='true')),
                    (path, headers, dict(body, **({'instructions': 'wrong'} if case == 'a2r' else {'system': 'wrong'}))),
                    (path, headers, dict(body, **({'input': []} if case == 'a2r' else {'messages': []}))),
                    (path, headers, dict(body, **({'input': [{'type': 'message', 'role': 'user',
                                                               'content': [{'type': 'input_text', 'text': 'wrong'}]}]}
                                                     if case == 'a2r' else
                                                     {'messages': [{'role': 'user', 'content': 'wrong'}]}))),
                ]:
                    with self.assertRaises(ValueError):
                        validate_mock_request(bad_path, bad_headers, bad_body)

    def test_json_checks_text_protocol_and_terminal(self):
        for api, text in (('anthropic', A2R_OUTPUT), ('responses', R2A_OUTPUT)):
            good = upstream_response(api, text, 'fixture-model')
            with self.subTest(api=api):
                verify_json_response(good, api, text)
                with self.assertRaises(RuntimeError):
                    verify_json_response(good, api, 'wrong text')
                wrong = dict(good, **({'stop_reason': 'max_tokens'} if api == 'anthropic'
                                     else {'status': 'failed'}))
                with self.assertRaises(RuntimeError):
                    verify_json_response(wrong, api, text)
                with self.assertRaises(RuntimeError):
                    verify_json_response(good, 'responses' if api == 'anthropic' else 'anthropic', text)

    def test_sse_checks_progress_text_order_and_terminal(self):
        for api, text in (('anthropic', A2R_OUTPUT), ('responses', R2A_OUTPUT)):
            first, tail = upstream_sse(api, text, 'fixture-model')
            stream = io.BytesIO(first + tail)
            events = []
            while True:
                event = read_sse_event(stream)
                if event is None:
                    break
                events.append(event)
            with self.subTest(api=api):
                verify_stream_events(events, api, text)
                with self.assertRaises(RuntimeError):
                    verify_stream_events(events, api, 'wrong text')
                with self.assertRaises(RuntimeError):
                    verify_stream_events(events[:-1], api, text)
                with self.assertRaises(RuntimeError):
                    verify_stream_events(events + [events[-1]], api, text)
                with self.assertRaises(RuntimeError):
                    verify_stream_events(events[1:], api, text)
                with self.assertRaises(RuntimeError):
                    verify_stream_events(events[:-1] + [('error', {'type': 'error'})], api, text)
        with self.assertRaises(RuntimeError):
            read_sse_event(io.BytesIO(b'event: message_stop\ndata: {"type":"message_stop"}'))
        with self.assertRaises(RuntimeError):
            read_sse_event(io.BytesIO(b'event: message_stop\ndata: {"type":"error"}\n\n'))


class LifecycleChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mp-status-test-')
        self.project = Path(self.temp.name)
        self.pid, self.port = 1234, 29381
        self.status = {'state': 'running', 'pid': self.pid,
                       'project_root': str(self.project),
                       'listen': {'host': '127.0.0.1', 'port': self.port}}
        self.state_dir = self.project / '.runtime'
        self.state_dir.mkdir()
        self.runtime_paths = resolved_default_paths(self.project)
        self.record = dict(self.status, record='active', state='READY', install_id='fixture-install',
                           instance_nonce='fixture-nonce', runtime_paths=self.runtime_paths)
        self.install = {'record': 'install', 'project_root': str(self.project),
                        'initialization_source': FAKE_SOURCE, 'install_id': 'fixture-install',
                        'ever_started': True, 'last_instance_nonce': 'fixture-nonce'}
        self.active_path = self.state_dir / 'active.json'
        self.install_path = self.state_dir / 'install.json'
        self.last_path = self.state_dir / 'last_good_paths.json'
        self.last = {key: self.record[key] for key in ('project_root', 'listen', 'runtime_paths', 'install_id', 'instance_nonce')}
        self.last['record'] = 'last_good_paths'
        self.active_path.write_text(json.dumps(self.record))
        self.install_path.write_text(json.dumps(self.install))
        self.last_path.write_text(json.dumps(self.last))

    def tearDown(self):
        self.temp.cleanup()

    def validate(self, status):
        verify_status(json.dumps(status), self.pid, self.port, self.project)

    def test_status_requires_positive_state_and_identity(self):
        self.validate(self.status)
        self.validate({'state': 'running'})
        for data in ({}, {'state': 'stopped', 'pid': self.pid},
                     {'state': 'running', 'status': 'stopped', 'pid': self.pid}):
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                self.validate(data)
        for bad in ('', 'fixture status', '{"state":'):
            with self.subTest(output=bad), self.assertRaises(RuntimeError):
                verify_status(bad, self.pid, self.port, self.project)

    def test_status_rejects_mismatched_or_malformed_identity(self):
        for update in ({'pid': self.pid + 1}, {'pid': str(self.pid)},
                       {'project_root': '/different'},
                       {'listen': {'host': '127.0.0.1', 'port': self.port + 1}},
                       {'listen': {'host': '0.0.0.0', 'port': self.port}},
                       {'listen': None}, {'port': self.port + 1},
                       {'runtime_paths': {'lock': '/tmp/model_proxy.lock'}}):
            with self.subTest(update=update), self.assertRaises(RuntimeError):
                self.validate(dict(self.status, **update))

    def test_active_record_is_required_and_checked_even_with_status_identity(self):
        self.validate(self.status)
        self.validate(dict(self.status, instance_nonce='fixture-nonce'))
        with self.assertRaisesRegex(RuntimeError, 'instance_nonce mismatch'):
            self.validate(dict(self.status, instance_nonce='different-nonce'))
        for update in ({'pid': self.pid + 1}, {'state': 'STARTING'},
                       {'record': 'last_good_paths'}, {'project_root': '/different'},
                       {'runtime_paths': dict(self.runtime_paths, lock='/different')}):
            with self.subTest(update=update):
                self.active_path.write_text(json.dumps(dict(self.record, **update)))
                with self.assertRaises(RuntimeError):
                    self.validate(self.status)
        self.active_path.unlink()
        with self.assertRaises(RuntimeError):
            self.validate(self.status)

    def test_last_good_record_required_and_bound_to_active(self):
        self.validate(self.status)
        for change in ({'record': 'active'}, {'project_root': '/other'},
                       {'install_id': 'other'}, {'instance_nonce': 'other'},
                       {'listen': {'host': '127.0.0.1', 'port': self.port + 1}},
                       {'runtime_paths': dict(self.runtime_paths, totals='/other')}):
            with self.subTest(change=change):
                self.last_path.write_text(json.dumps(dict(self.last, **change)))
                with self.assertRaises(RuntimeError):
                    self.validate(self.status)
        self.last_path.unlink()
        with self.assertRaises(RuntimeError):
            self.validate(self.status)

    def test_text_status_requires_exact_live_header_and_matching_installation(self):
        header = 'service running on port %d  pid %d' % (self.port, self.pid)
        body = '\nconfig 1 supplies / 1 routes / 1 strategies\nhealth: fixture\n'
        verify_status(header + '  up 00:03' + body, self.pid, self.port, self.project)
        for text in ('service stopped', header.replace(str(self.pid), str(self.pid + 1)) + body,
                     header.replace(str(self.port), str(self.port + 1)) + body,
                     'not ' + header + body, header + body + 'service stopped',
                     header + body + 'ERROR: status unavailable', header):
            with self.subTest(output=text), self.assertRaises(RuntimeError):
                verify_status(text, self.pid, self.port, self.project)
        for update in ({'initialization_source': 'somewhere else'},
                       {'install_id': 'different'}, {'last_instance_nonce': 'different'},
                       {'ever_started': False}):
            with self.subTest(install=update):
                self.install_path.write_text(json.dumps(dict(self.install, **update)))
                with self.assertRaises(RuntimeError):
                    verify_status(header + body, self.pid, self.port, self.project)

    def test_status_rejects_symlinked_active_record(self):
        self.active_path.unlink()
        with tempfile.TemporaryDirectory(prefix='mp-record-sentinel-') as external:
            target = Path(external) / 'sentinel'
            target.write_text('untouched')
            self.active_path.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, 'symlinked'):
                self.validate(self.status)
            self.assertEqual(target.read_text(), 'untouched')

    def test_status_rejects_hardlinked_record_before_reading(self):
        self.last_path.unlink()
        with tempfile.TemporaryDirectory(prefix='mp-record-sentinel-') as external:
            target = Path(external) / 'sentinel'
            target.write_text('untouched')
            os.link(target, self.last_path)
            with self.assertRaisesRegex(RuntimeError, 'single-link'):
                self.validate(self.status)
            self.assertEqual(target.read_text(), 'untouched')

    def test_status_rejects_symlinked_last_good_record(self):
        self.last_path.unlink()
        with tempfile.TemporaryDirectory(prefix='mp-record-sentinel-') as external:
            target = Path(external) / 'sentinel'
            target.write_text('untouched')
            self.last_path.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, 'symlinked'):
                self.validate(self.status)
            self.assertEqual(target.read_text(), 'untouched')

    def test_off_is_not_called_when_child_dead_or_listener_lost(self):
        for live, owns in (([], True), ([self.pid], False)):
            calls = []
            def command(name, quiet=False):
                calls.append(name)
                return {'children': live}
            with self.subTest(live=live, owns=owns):
                with patch('worktree_regression.owns_listener', return_value=owns):
                    with self.assertRaises(RuntimeError):
                        stop_managed(command, self.pid, self.port)
                self.assertEqual(calls, ['__probe__'])

    def test_off_called_only_after_alive_and_listener_checks(self):
        calls = []
        def command(name, quiet=False):
            calls.append(name)
            return {'children': [self.pid]}
        with patch('worktree_regression.owns_listener', return_value=True) as probe:
            stop_managed(command, self.pid, self.port)
        probe.assert_called_once_with(self.pid, self.port)
        self.assertEqual(calls, ['__probe__', 'off'])


class WorkerConstructionCleanupTests(unittest.TestCase):
    """Pure mocks: model descriptor failures without creating any process or socket."""

    def exercise_post_popen_failure(self, bad_fd, mode):
        reservation, server, thread = MagicMock(), MagicMock(), MagicMock()
        scratch = MagicMock()
        scratch.root = Path('/mock-only/scratch')
        scratch.project = scratch.root / 'project'
        scratch.python = scratch.root / 'interpreter/bin/python'
        scratch.env.return_value = {}
        scratch.stage.return_value = scratch.project / 'cli.py'
        process = MagicMock(name='direct-child')
        if mode == 'exited':
            process.poll.side_effect = [None, 0]
            process.wait.return_value = 0
        elif mode == 'still_alive':
            process.poll.side_effect = [None, None, None]
            process.wait.side_effect = [subprocess.TimeoutExpired('fake', 8),
                                        subprocess.TimeoutExpired('fake', 8)]
        elif mode == 'poll_error':
            process.poll.side_effect = [OSError('poll unavailable'), OSError('poll unavailable')]
        else:
            process.poll.side_effect = [None, None]
            process.wait.side_effect = RuntimeError('wait failed')
        closed = []
        def close(fd):
            closed.append(fd)
            if fd == bad_fd:
                raise OSError('injected descriptor close failure')
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            for name, replacement in (
                ('reserve', MagicMock(return_value=reservation)),
                ('ThreadingHTTPServer', MagicMock(return_value=server)),
                ('Scratch', MagicMock(return_value=scratch)),
                ('validate_fresh_project', MagicMock()),
            ):
                stack.enter_context(patch.object(regression, name, replacement))
            stack.enter_context(patch.object(regression.threading, 'Thread', return_value=thread))
            stack.enter_context(patch.object(regression.pty, 'openpty', return_value=(100, 101)))
            stack.enter_context(patch.object(regression.os, 'pipe', side_effect=[(102, 103), (104, 105)]))
            stack.enter_context(patch.object(regression.os, 'close', side_effect=close))
            write = stack.enter_context(patch.object(regression.os, 'write'))
            kill = stack.enter_context(patch.object(regression.os, 'kill'))
            killpg = stack.enter_context(patch.object(regression.os, 'killpg'))
            stack.enter_context(patch.object(regression.termios, 'tcgetattr', return_value=[0, 0, 0, 0]))
            stack.enter_context(patch.object(regression.termios, 'tcsetattr'))
            popen = stack.enter_context(patch.object(regression.subprocess, 'Popen', return_value=process))
            stack.enter_context(patch.object(sys, 'argv', ['worktree_regression.py', '--fixture',
                                                           '--proxy-port', '29481', '--mock-port', '29482']))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            self.assertEqual(regression.main(), 1)
        popen.assert_called_once()
        if mode == 'poll_error':
            write.assert_not_called()
        else:
            write.assert_called_once_with(103, b'"quit"\n')
        kill.assert_not_called()
        killpg.assert_not_called()
        process.kill.assert_not_called()
        self.assertEqual(closed, [101, 102, 105, 103, 104, 100])
        reservation.close.assert_called_once_with()
        server.shutdown.assert_called_once_with()
        server.server_close.assert_called_once_with()
        self.assertIn('FAIL: worker construction failed: injected descriptor close failure', stderr.getvalue())
        if mode == 'exited':
            process.terminate.assert_not_called()
            scratch.cleanup.assert_called_once_with()
            self.assertIn('PASS scratch cleanup', stdout.getvalue())
        else:
            scratch.cleanup.assert_not_called()
            self.assertIn('scratch retained: /mock-only/scratch', stderr.getvalue())
            self.assertNotIn('PASS scratch cleanup', stdout.getvalue())
            if mode == 'still_alive':
                process.terminate.assert_called_once_with()
            else:
                process.terminate.assert_not_called()
                if mode == 'poll_error':
                    self.assertIn('worker status check failed: poll unavailable', stderr.getvalue())
                else:
                    self.assertIn('worker close failed: wait failed', stderr.getvalue())

    def test_child_descriptor_failures_transfer_exact_worker_handle(self):
        for bad_fd in (101, 102, 105):
            for mode in ('exited', 'still_alive'):
                with self.subTest(bad_fd=bad_fd, mode=mode):
                    self.exercise_post_popen_failure(bad_fd, mode)

    def test_worker_close_error_still_retains_scratch_and_closes_parent_fds(self):
        self.exercise_post_popen_failure(101, 'close_error')

    def test_unavailable_worker_status_retains_scratch(self):
        self.exercise_post_popen_failure(101, 'poll_error')

    def test_pre_popen_failure_closes_all_allocated_descriptors(self):
        scratch = MagicMock()
        scratch.root = Path('/mock-only/scratch')
        scratch.project = scratch.root / 'project'
        scratch.python = scratch.root / 'python'
        scratch.env.return_value = {}
        with ExitStack() as stack:
            stack.enter_context(patch.object(regression.pty, 'openpty', return_value=(100, 101)))
            stack.enter_context(patch.object(regression.os, 'pipe', side_effect=[(102, 103), (104, 105)]))
            closer = stack.enter_context(patch.object(regression.os, 'close'))
            stack.enter_context(patch.object(regression.termios, 'tcgetattr', return_value=[0, 0, 0, 0]))
            stack.enter_context(patch.object(regression.termios, 'tcsetattr'))
            stack.enter_context(patch.object(regression.subprocess, 'Popen', side_effect=OSError('spawn failed')))
            with self.assertRaisesRegex(OSError, 'spawn failed'):
                TTYWorker(scratch, scratch.project / 'cli.py', 29481, 29482)
        self.assertEqual([call.args[0] for call in closer.call_args_list], [101, 102, 105, 103, 104, 100])


class AliveWorkerCleanupTests(unittest.TestCase):
    """Pure mocks: no processes, sockets, threads, scratch paths or services."""

    def exercise(self, *, cli=False, body_failure=False, worker_error=None,
                 shutdown_error=None, server_close_error=None, poll_error=None,
                 worker_exit=None, scratch_error=None):
        proxy, mock_port, pid = 29481, 29482, 12345
        reservation = MagicMock(name='reservation')
        server = MagicMock(name='server')
        thread = MagicMock(name='thread')
        scratch = MagicMock(name='scratch')
        scratch.root = Path('/mock-only/retained-scratch')
        scratch.project = scratch.root / 'project'
        scratch.stage.return_value = scratch.project / 'cli.py'
        worker = MagicMock(name='worker')
        worker.process.poll.return_value = worker_exit
        worker.process.poll.side_effect = poll_error
        worker.process.returncode = worker_exit
        worker.close.side_effect = worker_error
        server.shutdown.side_effect = shutdown_error
        server.server_close.side_effect = server_close_error
        scratch.cleanup.side_effect = scratch_error
        stopped = False

        def command(name, quiet=False):
            if name == 'on':
                return {'children': [pid]}
            if name == '__probe__':
                return {'children': [] if stopped else [pid]}
            return {'children': [pid], 'output': 'synthetic status'}

        def request(_port, case, streaming):
            if body_failure:
                raise RuntimeError('original test failure')
            if case == 'a2a':
                regression.Mock.calls.append({'model': 'fixture-model-reloaded' if streaming else 'fixture-model'})

        def stop(_command, _pid, _port):
            nonlocal stopped
            stopped = True

        worker.command.side_effect = command
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            for name, replacement in (
                ('reserve', MagicMock(return_value=reservation)),
                ('ThreadingHTTPServer', MagicMock(return_value=server)),
                ('Scratch', MagicMock(return_value=scratch)),
                ('TTYWorker', MagicMock(return_value=worker)),
                ('validate_fresh_project', MagicMock()),
                ('owns_listener', MagicMock(return_value=True)),
                ('require_managed_listener', MagicMock()),
                ('verify_status', MagicMock()),
                ('request_case', MagicMock(side_effect=request)),
                ('read_local_json', MagicMock(return_value=regression.fake_config(proxy, mock_port))),
                ('write_local_json', MagicMock()),
                ('stop_managed', MagicMock(side_effect=stop)),
            ):
                stack.enter_context(patch.object(regression, name, replacement))
            stack.enter_context(patch.object(regression.threading, 'Thread', return_value=thread))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            if cli:
                stack.enter_context(patch.object(sys, 'argv', ['worktree_regression.py', '--fixture',
                                                               '--proxy-port', str(proxy), '--mock-port', str(mock_port)]))
                self.assertEqual(regression.main(), 1)
            elif body_failure:
                with self.assertRaisesRegex(RuntimeError, 'original test failure'):
                    regression.run(True, proxy, mock_port)
            else:
                with self.assertRaisesRegex(RuntimeError, 'worker cleanup failed: worker still alive or exit unconfirmed'):
                    regression.run(True, proxy, mock_port)

        worker.close.assert_called_once_with()
        if worker_exit is None or poll_error:
            scratch.cleanup.assert_not_called()
        else:
            scratch.cleanup.assert_called_once_with()
        reservation.close.assert_called_once_with()
        server.shutdown.assert_called_once_with()
        server.server_close.assert_called_once_with()
        thread.start.assert_called_once_with()
        if worker_exit is None or poll_error:
            self.assertIn('worker still alive or exit unconfirmed; scratch retained: /mock-only/retained-scratch', stderr.getvalue())
            self.assertNotIn('PASS scratch cleanup', stdout.getvalue())
        elif not scratch_error:
            self.assertIn('PASS scratch cleanup', stdout.getvalue())
        for label, error in (('worker close failed', worker_error), ('mock shutdown failed', shutdown_error),
                             ('mock server close failed', server_close_error), ('worker status check failed', poll_error),
                             ('scratch cleanup failed', scratch_error)):
            if error:
                self.assertIn('ERROR: %s: %s' % (label, error), stderr.getvalue())
        if not body_failure:
            self.assertTrue(stopped)
            self.assertIn('PASS cli off', stdout.getvalue())
        if cli:
            if body_failure:
                self.assertIn('FAIL: original test failure', stderr.getvalue())
            else:
                self.assertIn('FAIL: ', stderr.getvalue())
                if not any((worker_error, shutdown_error, server_close_error, poll_error, scratch_error)):
                    self.assertIn('FAIL: worker cleanup failed: worker still alive', stderr.getvalue())

    def test_successful_cases_but_live_worker_raise_and_preserve_scratch(self):
        self.exercise()

    def test_cli_returns_nonzero_after_live_worker(self):
        self.exercise(cli=True)

    def test_original_failure_remains_visible_with_live_worker(self):
        self.exercise(body_failure=True)

    def test_cli_shows_primary_and_both_cleanup_failures_and_continues(self):
        self.exercise(cli=True, body_failure=True,
                      worker_error=RuntimeError('worker close exploded'),
                      shutdown_error=RuntimeError('mock shutdown exploded'),
                      server_close_error=RuntimeError('mock close exploded'))

    def test_unknown_worker_exit_retains_scratch_and_reports_primary(self):
        self.exercise(cli=True, body_failure=True,
                      poll_error=OSError('poll unavailable'),
                      shutdown_error=RuntimeError('mock shutdown exploded'))

    def test_cleanup_failures_alone_make_successful_run_fail(self):
        self.exercise(cli=True, worker_exit=0,
                      worker_error=RuntimeError('worker close exploded'),
                      shutdown_error=RuntimeError('mock shutdown exploded'))

    def test_scratch_cleanup_failure_does_not_mask_primary(self):
        self.exercise(cli=True, body_failure=True, worker_exit=0,
                      scratch_error=OSError('scratch removal exploded'))

    def test_failed_mock_thread_start_closes_server_without_shutdown(self):
        reservation, server, thread = MagicMock(), MagicMock(), MagicMock()
        scratch = MagicMock()
        scratch.root = Path('/mock-only/scratch')
        scratch.project = scratch.root / 'project'
        scratch.stage.return_value = scratch.project / 'cli.py'
        thread.start.side_effect = RuntimeError('thread start failed')
        stderr = io.StringIO()
        with ExitStack() as stack:
            for name, replacement in (
                ('reserve', MagicMock(return_value=reservation)),
                ('ThreadingHTTPServer', MagicMock(return_value=server)),
                ('Scratch', MagicMock(return_value=scratch)),
                ('TTYWorker', MagicMock()),
                ('validate_fresh_project', MagicMock()),
            ):
                stack.enter_context(patch.object(regression, name, replacement))
            stack.enter_context(patch.object(regression.threading, 'Thread', return_value=thread))
            stack.enter_context(patch.object(sys, 'argv', ['worktree_regression.py', '--fixture',
                                                           '--proxy-port', '29481', '--mock-port', '29482']))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(stderr))
            self.assertEqual(regression.main(), 1)
        self.assertIn('FAIL: thread start failed', stderr.getvalue())
        reservation.close.assert_called_once_with()
        server.shutdown.assert_not_called()  # serve_forever never started
        server.server_close.assert_called_once_with()
        scratch.cleanup.assert_called_once_with()


class PreflightTests(unittest.TestCase):
    def test_overlap_and_distinct_paths(self):
        self.assertTrue(overlaps('/tmp/example', '/tmp/example/sub'))
        self.assertFalse(overlaps('/tmp/example-a', '/tmp/example-b'))
    def test_occupied_proxy_stops_before_staging(self):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0)); sock.listen(1)
        try:
            with self.assertRaisesRegex(RuntimeError, 'port occupied'):
                run(True, sock.getsockname()[1], 29382)
        finally:
            sock.close()
    def test_occupied_mock_stops_before_staging(self):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0)); sock.listen(1)
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        free = probe.getsockname()[1]
        probe.close()
        try:
            with self.assertRaisesRegex(RuntimeError, 'mock port occupied'):
                run(True, free, sock.getsockname()[1])
        finally:
            sock.close()
    def test_hardlinked_sources_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix='mp-copytest-') as d:
            root = Path(d)
            (root / 'core').mkdir()
            (root / 'other.py').write_text('sentinel')
            os.link(root / 'other.py', root / 'core/x.py')
            with self.assertRaisesRegex(RuntimeError, 'single-link regular file'):
                copy_core(root / 'core', root / 'out')
            self.assertEqual((root / 'other.py').read_text(), 'sentinel')
    def test_copy_rejects_symlink(self):
        with tempfile.TemporaryDirectory(prefix='mp-copytest-') as d:
            root = Path(d)
            (root / 'core').mkdir()
            (root / 'other.py').write_text('sentinel')
            (root / 'core/x.py').symlink_to(root / 'other.py')
            with self.assertRaisesRegex(RuntimeError, 'non-symlink'):
                copy_core(root / 'core', root / 'out')
            self.assertEqual((root / 'other.py').read_text(), 'sentinel')
    def test_reserve_is_loopback_only(self):
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        sock = reserve(port)
        try:
            self.assertEqual(sock.getsockname()[0], '127.0.0.1')
        finally:
            sock.close()

if __name__ == '__main__':
    unittest.main()
