"""Exercise the real batch bootstrap on Windows (including Wine), without a server."""
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'requires a Windows command processor')
class BatchEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='model-proxy-bat-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # The spaces also exercise the %~dp0 quoting in the batch entry.
        self.project = self.root / 'project with spaces'
        self.project.mkdir()
        shutil.copy(ROOT / 'model_proxy_cli.bat', self.project)
        shutil.copytree(ROOT / 'core', self.project / 'core',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        self.home = self.root / 'home'
        self.home.mkdir()
        system_root = Path(os.environ.get('SystemRoot', r'C:\Windows'))
        self.env = os.environ.copy()
        for name in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP',
                     'PYTHONUSERBASE', 'PYTHONINSPECT'):
            self.env.pop(name, None)
        self.env.update({
            'HOME': str(self.home),
            'USERPROFILE': str(self.home),
            'APPDATA': str(self.home),
            'LOCALAPPDATA': str(self.home),
            'TEMP': str(self.home),
            'TMP': str(self.home),
            'MODEL_PROXY_CONFIG': str(self.home / 'nonexistent.json'),
            'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONNOUSERSITE': '1',
            # A Western Windows code page cannot encode the Chinese help.
            'PYTHONIOENCODING': 'cp1252',
            'PYTHONUTF8': '0',
            'PATH': os.pathsep.join((str(Path(sys.executable).parent),
                                    str(system_root / 'System32'))),
        })
        self.cmd = str(system_root / 'System32' / 'cmd.exe')

    def invoke(self, *args, devnull=False):
        # cmd /s /c needs an additional outer quote pair when both the batch
        # path and individual arguments may contain spaces.
        command = subprocess.list2cmdline([str(self.project / 'model_proxy_cli.bat'), *args])
        stdin = {'stdin': subprocess.DEVNULL} if devnull else {'input': ''}
        return subprocess.run(
            f'"{self.cmd}" /d /s /c "{command}"',
            # On Windows NUL is a character device and can report isatty=True.
            # A closed pipe guarantees non-interactive input with real EOF.
            cwd=self.root, env=self.env, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=180, **stdin,
        )

    def test_help_works_from_another_directory(self):
        for args in ((), ('--help',), ('-h',)):
            with self.subTest(args=args):
                result = self.invoke(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.startswith('用法: model_proxy_cli.bat '))
                self.assertEqual(result.stderr, '')

    def test_invalid_command_propagates_failure(self):
        result = self.invoke('not-a-command')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Unknown command: not-a-command.', result.stdout)

    def test_batch_forwards_arguments_once_and_propagates_exit_code(self):
        bootstrap = self.project / 'core' / 'cli' / 'main.py'
        bootstrap.write_text(
            'import json, pathlib, sys\n'
            "p = pathlib.Path(__file__).with_name('invocations.txt')\n"
            "with p.open('a', encoding='utf-8') as f: f.write('run\\n')\n"
            'print(json.dumps(sys.argv[1:]))\n'
            'sys.exit(23)\n',
            encoding='utf-8',
        )
        args = ('first', 'two words', 'bang!value')
        result = self.invoke(*args)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(json.loads(result.stdout), list(args))
        self.assertEqual(bootstrap.with_name('invocations.txt').read_text().splitlines(), ['run'])

    def test_readonly_menus_work_without_writing_or_reloading(self):
        config = self.home / 'fake-config.json'
        data = {
            'supplies': [{'id': 'fake-supply', 'url': 'http://127.0.0.1/fake',
                          'target_model': 'fake-model'}],
            'routes': [{'id': 'fake-route'}],
            'strategies': [{'client_token': 'fake-token', 'route_id': 'fake-route'}],
        }
        config.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        original = config.read_bytes()
        self.env['MODEL_PROXY_CONFIG'] = str(config)
        # A read-only list must not inherit marker transports or create one,
        # even if the parent environment contains stale values.
        marker = self.home / 'unexpected-reload-marker'
        self.env['CONFIG_OPS_RELOAD_MARKER'] = str(marker)
        self.env['CONFIG_OPS_RELOAD_MARKER_FD'] = '999'
        for command, expected in (('supply', 'fake-supply'),
                                  ('route', 'fake-route'),
                                  ('strategy', 'fake-token')):
            for devnull in (False, True):
                with self.subTest(command=command, stdin='NUL' if devnull else 'closed pipe'):
                    result = self.invoke(command, devnull=devnull)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, '')
                    self.assertIn(expected, result.stdout)
                    self.assertIn('stdin 非 TTY', result.stdout)
                    self.assertNotIn('__RELOAD__', result.stdout)
                    self.assertNotIn('Configuration changed; attempting reload.', result.stdout)
                    self.assertFalse(marker.exists())
                    self.assertEqual(config.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
