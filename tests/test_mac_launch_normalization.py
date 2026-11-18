"""Mac bootstrap rejection/transition tests; isolated snapshot only."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ISO = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if (Path(__file__).resolve().parents[1] != ISO / 'project'
        or not (ISO / '.model-proxy-isolation').is_file()
        or not Path(os.environ.get('HOME', '/invalid')).resolve().is_relative_to(ISO)):
    raise RuntimeError('isolated source and fake HOME required')
sys.path.insert(0, str(ISO / 'project'))
from core.cli import model_proxy, authority, control
from core import platform_process


@unittest.skipUnless(sys.platform == 'darwin', 'native Mac launch semantics')
class LaunchNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.root = ISO / 'project'
        self.old_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.old_cwd)
        self.launcher = sys.executable
        self.env = {'HOME': str(ISO / 'home'), 'PATH': '/usr/bin:/bin',
                    'MODEL_PROXY_CONFIG': 'link/../business.json',
                    'MODEL_PROXY_START_NONCE': 'b' * 32,
                    'MODEL_PROXY_PORT': '29571', 'SENTINEL': 'retained'}
        self.initial = {k: v for k, v in self.env.items() if k.startswith('MODEL_PROXY_')}

    @contextlib.contextmanager
    def launch(self, args, argv=None, initial=None):
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(sys, 'orig_argv', args), \
             mock.patch.object(sys, 'argv', argv if argv is not None else [args[-1]]), \
             mock.patch.object(model_proxy, '_darwin_initial_launch', return_value=(args, initial if initial is not None else self.initial)), \
             mock.patch.object(platform_process, 'process_executable', return_value=self.launcher), \
             mock.patch('core.discovery.process_executable', return_value=self.launcher):
            yield

    def test_import_does_not_export_unchecked_mac_start(self):
        self.assertFalse(hasattr(model_proxy, 'main'))

    def test_relative_entry_config_and_exec_failure_preserve_options_and_values(self):
        args = [self.launcher, '-B', 'core/cli/model_proxy.py']
        with self.launch(args), mock.patch.object(os, 'execve', side_effect=OSError('denied')) as execute:
            with self.assertRaises(OSError):
                model_proxy._normalize_darwin_launch()
            path, argv, env = execute.call_args.args
            self.assertEqual(path, self.launcher)
            self.assertEqual(argv, [self.launcher, '-B', str(self.root / 'core/cli/model_proxy.py')])
            self.assertEqual(env['MODEL_PROXY_CONFIG'], str(self.root) + '/link/../business.json')
            self.assertEqual(env['MODEL_PROXY_START_NONCE'], 'b' * 32)
            self.assertEqual(env['SENTINEL'], 'retained')
            self.assertIn('MODEL_PROXY_INTERNAL_REEXEC', env)
            self.assertNotIn('MODEL_PROXY_INTERNAL_REEXEC', os.environ)

    def test_second_pass_detects_environment_or_venv_change(self):
        args = [self.launcher, '-B', 'core/cli/model_proxy.py']
        with self.launch(args), mock.patch.object(os, 'execve', side_effect=OSError('stop')) as execute:
            with self.assertRaises(OSError):
                model_proxy._normalize_darwin_launch()
            new_args, new_env = execute.call_args.args[1:]
        self.env['MODEL_PROXY_CONFIG'] = new_env['MODEL_PROXY_CONFIG']
        self.env['MODEL_PROXY_INTERNAL_REEXEC'] = new_env['MODEL_PROXY_INTERNAL_REEXEC']
        self.initial = {k: v for k, v in self.env.items() if k.startswith('MODEL_PROXY_')}
        with self.launch(new_args):
            model_proxy._normalize_darwin_launch()
            self.assertNotIn('MODEL_PROXY_INTERNAL_REEXEC', os.environ)
        self.env['SENTINEL'] = 'changed'
        with self.launch(new_args):
            with self.assertRaises(ValueError):
                model_proxy._normalize_darwin_launch()
        self.env['SENTINEL'] = 'retained'
        with self.launch(new_args), mock.patch.object(sys, 'prefix', '/different-venv'):
            with self.assertRaises(ValueError):
                model_proxy._normalize_darwin_launch()

    def test_marker_never_causes_an_exec_loop(self):
        args = [self.launcher, 'core/cli/model_proxy.py']
        self.env['MODEL_PROXY_INTERNAL_REEXEC'] = '{}'
        self.initial['MODEL_PROXY_INTERNAL_REEXEC'] = '{}'
        with self.launch(args), mock.patch.object(os, 'execve') as execute:
            with self.assertRaises(ValueError):
                model_proxy._normalize_darwin_launch()
            execute.assert_not_called()

    def test_cloexec_standard_descriptor_blocks_before_exec(self):
        import fcntl
        args = [self.launcher, 'core/cli/model_proxy.py']
        with self.launch(args), mock.patch.object(fcntl, 'fcntl', return_value=fcntl.FD_CLOEXEC), \
             mock.patch.object(os, 'execve') as execute:
            with self.assertRaises(ValueError):
                model_proxy._normalize_darwin_launch()
            execute.assert_not_called()

    def test_kernel_environment_mismatch_fails_before_exec(self):
        args = [self.launcher, 'core/cli/model_proxy.py']
        initial = dict(self.initial, MODEL_PROXY_START_NONCE='c' * 32)
        with self.launch(args, initial=initial), mock.patch.object(os, 'execve') as execute:
            with self.assertRaises(ValueError):
                model_proxy._normalize_darwin_launch()
            execute.assert_not_called()

    def test_entry_replaced_before_exec_fails_closed(self):
        args = [self.launcher, 'core/cli/model_proxy.py']
        real_stat = os.stat
        def changed(path, *a, **kw):
            result = real_stat(path, *a, **kw)
            if path == 'core/cli/model_proxy.py':
                values = list(result)
                values[1] += 1  # inode
                return os.stat_result(values)
            return result
        with self.launch(args), mock.patch.object(os, 'stat', side_effect=changed), \
             mock.patch.object(os, 'execve') as execute:
            with self.assertRaises(ValueError):
                model_proxy._normalize_darwin_launch()
            execute.assert_not_called()

    def test_existing_absolute_launch_does_not_reexec(self):
        self.env['MODEL_PROXY_CONFIG'] = str(self.root / 'config/fake.json')
        self.initial['MODEL_PROXY_CONFIG'] = self.env['MODEL_PROXY_CONFIG']
        args = [self.launcher, str(self.root / 'core/cli/model_proxy.py')]
        with self.launch(args), mock.patch.object(os, 'execve') as execute:
            model_proxy._normalize_darwin_launch()
            execute.assert_not_called()

    def test_unsupported_options_and_arguments_fail_before_exec(self):
        for args in ([self.launcher, '-m', 'core.cli.model_proxy'],
                     [self.launcher, '-I', 'core/cli/model_proxy.py'],
                     [self.launcher, 'core/cli/model_proxy.py', 'extra']):
            with self.subTest(args=args), self.launch(args), mock.patch.object(os, 'execve') as execute:
                with self.assertRaises(ValueError):
                    model_proxy._normalize_darwin_launch()
                execute.assert_not_called()

    def test_control_accepts_only_documented_minus_b(self):
        with tempfile.TemporaryDirectory(dir=ISO) as directory:
            root = Path(directory).resolve()
            entry = root / 'core/cli/model_proxy.py'
            entry.parent.mkdir(parents=True)
            entry.write_text('# fixture')
            config = root / 'config.json'; config.write_text('{}'); config.chmod(0o600)
            obs = SimpleNamespace(_root=root, _config_path=config)
            lease = authority.MacAuthorityLease(obs)
            snapshot = SimpleNamespace(exists=True)
            with mock.patch.object(authority, '_capture', return_value=snapshot), \
                 mock.patch.object(platform_process, 'darwin_launch_evidence') as evidence:
                for flags in ([], ['-B']):
                    evidence.return_value = (tuple(['/fake/python3', *flags, str(entry)]),
                                             {'MODEL_PROXY_CONFIG': str(config)})
                    self.assertEqual(lease._launch(12345, {'fake': 1}, '/fake/python3', 'a'*32),
                                     {'MODEL_PROXY_CONFIG': str(config)})
                for flags in (['-I'], ['-B', '-B'], ['-m']):
                    evidence.return_value = (tuple(['/fake/python3', *flags, str(entry)]),
                                             {'MODEL_PROXY_CONFIG': str(config)})
                    with self.assertRaises(control.ControlError):
                        lease._launch(12345, {'fake': 1}, '/fake/python3', 'a'*32)


if __name__ == '__main__':
    unittest.main(verbosity=2)
