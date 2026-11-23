"""Hook contract tests using only temporary fake CLI scripts, never a server."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
SHELL_HOOK = SOURCE_ROOT / 'hooker' / 'ensure_model_proxy.sh'
WINDOWS_HOOK = SOURCE_ROOT / 'hooker' / 'ensure_model_proxy.bat'


@unittest.skipIf(os.name == 'nt', 'Bash hook requires POSIX')
class ShellHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hook-compat-')
        self.root = Path(self.temp.name) / 'fake project with spaces'
        self.hook_dir = self.root / 'hooker'
        self.hook_dir.mkdir(parents=True)
        self.hook = self.hook_dir / SHELL_HOOK.name
        shutil.copyfile(SHELL_HOOK, self.hook)
        self.fake_bin = self.root / 'bin'
        self.fake_bin.mkdir()
        self.home = self.root / 'fake home'
        self.home.mkdir()
        self.calls = self.root / 'calls.txt'
        self.forbidden_calls = self.root / 'forbidden.txt'
        # Guard the commands used by the old hook: they must never reach the
        # real default lock, even when testing a regressed implementation.
        for name in ('python3', 'mkdir', 'rmdir', 'sleep'):
            path = self.fake_bin / name
            path.write_text('#!/bin/bash\nprintf "%s\\n" "$0 $*" >> "$FORBIDDEN_CALLS"\nexit 99\n')
            path.chmod(0o755)
        self.cli = self.root / 'model_proxy_cli.sh'
        self.cli.write_text(
            '#!/bin/bash\n'
            'printf "%s\\n" "$#:$*" >> "$HOOK_CALLS"\n'
            'if [ "${FAKE_DELAY:-0}" = 1 ]; then /bin/sleep 0.2; fi\n'
            'printf "%s\\n" "fake cli stdout"\n'
            'printf "%s\\n" "${FAKE_ERROR:-fake cli stderr}" >&2\n'
            'exit "$FAKE_CODE"\n'
        )
        self.env = {
            'PATH': str(self.fake_bin) + os.pathsep + '/usr/bin:/bin',
            'HOME': str(self.home),
            'TMPDIR': str(self.root),
            'HOOK_CALLS': str(self.calls),
            'FORBIDDEN_CALLS': str(self.forbidden_calls),
            'FAKE_CODE': '0',
        }

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *args, code=0, error=None):
        env = dict(self.env, FAKE_CODE=str(code))
        if error is not None:
            env['FAKE_ERROR'] = error
        return subprocess.run(['/bin/bash', str(self.hook), *args], cwd=self.home,
                              env=env, stdin=subprocess.DEVNULL, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)

    def assert_no_lock_helpers(self):
        self.assertFalse(self.forbidden_calls.exists(), 'hook invoked legacy lock/config helper')

    def test_forwards_only_on_and_exact_exit_status(self):
        for code in (0, 1, 37, 127):
            with self.subTest(code=code):
                result = self.invoke('ignored-hook-argument', code=code)
                self.assertEqual(result.returncode, code)
                self.assertEqual(result.stdout, 'fake cli stdout\n')
                self.assertEqual(result.stderr, 'fake cli stderr\n')
        self.assertEqual(self.calls.read_text().splitlines(), ['1:on'] * 4)
        self.assert_no_lock_helpers()

    def test_non_tty_initialization_failure_is_not_masked(self):
        message = 'initialization_interactive_required: run on interactively'
        result = self.invoke(code=1, error=message)
        self.assertEqual(result.returncode, 1)
        self.assertIn(message, result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), ['1:on'])
        self.assert_no_lock_helpers()

    def test_missing_or_damaged_paths_config_is_not_read_by_hook(self):
        config = self.root / 'config'
        for contents in (None, '{broken json', '{"start_lock":"/tmp/model_proxy_start.lock"}'):
            with self.subTest(contents=contents):
                if contents is not None:
                    config.mkdir(exist_ok=True)
                    (config / 'runtime_paths.json').write_text(contents)
                result = self.invoke(code=23)
                self.assertEqual(result.returncode, 23)
                self.assert_no_lock_helpers()
        self.assertEqual(self.calls.read_text().splitlines(), ['1:on'] * 3)

    def test_simultaneous_calls_all_reach_cli_and_preserve_failures(self):
        env = dict(self.env, FAKE_CODE='37', FAKE_DELAY='1')
        processes = [subprocess.Popen(['/bin/bash', str(self.hook)], cwd=self.home,
                                      env=env, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True) for _ in range(8)]
        try:
            for proc in processes:
                stdout, stderr = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 37)
                self.assertEqual(stdout, 'fake cli stdout\n')
                self.assertEqual(stderr, 'fake cli stderr\n')
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate()
        self.assertEqual(self.calls.read_text().splitlines(), ['1:on'] * 8)
        self.assert_no_lock_helpers()

    def test_missing_cli_is_failure(self):
        self.cli.unlink()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls.exists())
        self.assert_no_lock_helpers()

    def test_no_hardcoded_default_lock_or_config_in_hook(self):
        source = self.hook.read_text()
        self.assertNotIn('/tmp/model_proxy_start.lock', source)
        self.assertNotIn('runtime_paths.json', source)


class WindowsHookTests(unittest.TestCase):
    def test_batch_delegation_contract_statically(self):
        lines = [line.strip().lower() for line in WINDOWS_HOOK.read_text().splitlines()]
        self.assertIn('setlocal disabledelayedexpansion', lines)
        self.assertIn('call "%~dp0..\\model_proxy_cli.bat" on', lines)
        self.assertIn('exit /b %errorlevel%', lines)
        self.assertNotIn('/tmp/model_proxy_start.lock', '\n'.join(lines))
        self.assertNotIn('runtime_paths.json', '\n'.join(lines))

    @unittest.skipUnless(os.name == 'nt', 'requires native Windows cmd.exe')
    def test_batch_calls_fake_cli_and_forwards_status(self):
        with tempfile.TemporaryDirectory(prefix='hook-compat-') as temp:
            root = Path(temp) / 'fake project with spaces'
            hook_dir = root / 'hooker'
            hook_dir.mkdir(parents=True)
            hook = hook_dir / WINDOWS_HOOK.name
            shutil.copyfile(WINDOWS_HOOK, hook)
            cli = root / 'model_proxy_cli.bat'
            # 不能写成 `echo %*`：参数正好是 `on` 时 `echo on` 是 cmd 的开关
            # 命令，什么都不打印（实测 calls.txt 为空）。用括号包一层强制按文本输出。
            cli.write_text('@echo off\n> "%HOOK_CALLS%" echo [%*]\n'
                           'echo fake cli stdout\n'
                           'echo initialization_interactive_required 1>&2\n'
                           'exit /b %FAKE_CODE%\n')
            home = root / 'fake home'
            home.mkdir()
            system_root = os.environ['SystemRoot']
            env = {'SystemRoot': system_root, 'PATH': str(Path(system_root) / 'System32'),
                   'HOME': str(home), 'USERPROFILE': str(home), 'APPDATA': str(home),
                   'LOCALAPPDATA': str(home), 'TEMP': str(root), 'TMP': str(root),
                   'HOOK_CALLS': str(root / 'calls.txt')}
            for code in (0, 1, 37):
                with self.subTest(code=code):
                    env['FAKE_CODE'] = str(code)
                    result = subprocess.run([str(Path(system_root) / 'System32' / 'cmd.exe'),
                                             '/d', '/c', str(hook), 'ignored'], cwd=home,
                                            env=env, stdin=subprocess.DEVNULL, text=True,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
                    self.assertEqual(result.returncode, code)
                    self.assertEqual((root / 'calls.txt').read_text().strip(), '[on]')
                    self.assertIn('initialization_interactive_required', result.stderr)


if __name__ == '__main__':
    unittest.main()
