"""Simulated Python 3.10 import availability; no dependency is installed."""
import builtins
import types
import unittest
from unittest.mock import Mock, patch
from core.cli import _install_ops as install


class TomlCompatibility(unittest.TestCase):
    def imports(self, backport):
        real = builtins.__import__
        def importer(name, *args, **kwargs):
            if name == 'tomllib':
                raise ImportError('simulated 3.10')
            if name == 'tomli':
                if backport is not None:
                    return backport
                raise ImportError('backport unavailable')
            return real(name, *args, **kwargs)
        return patch('builtins.__import__', side_effect=importer)

    def test_optional_backport_success_and_invalid_toml(self):
        # Test import selection without requiring either parser to be
        # installed in the interpreter running this test (including 3.10).
        backport = types.ModuleType('tomli')
        backport.loads = Mock(side_effect=[{'model': 'fake'}, ValueError('invalid fake TOML')])
        with self.imports(backport):
            self.assertEqual(install._toml_load('model = "fake"'), {'model': 'fake'})
            with self.assertRaises(install.InstallFailure):
                install._toml_load('model = [')
        self.assertEqual(backport.loads.call_count, 2)

    def test_without_backport_fails_closed(self):
        with self.imports(None), self.assertRaises(install.InstallFailure) as caught:
            install._toml_load('model = "fake"')
        self.assertIn('tomli', str(caught.exception))

    def test_available_real_parser_as_backport(self):
        try:
            import tomllib as real_parser
        except ImportError:
            try:
                import tomli as real_parser
            except ImportError:
                self.skipTest('no real TOML parser available; simulated backport still tested')
        backport = types.ModuleType('tomli')
        backport.loads = real_parser.loads
        with self.imports(backport):
            self.assertEqual(install._toml_load('model = "fake"'), {'model': 'fake'})
            with self.assertRaises(install.InstallFailure):
                install._toml_load('model = [')
