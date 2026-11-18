"""Private Mac config path proof tests; only an isolated fake tree."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ISO = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if (Path(__file__).resolve().parents[1] != ISO / 'project'
        or not (ISO / '.model-proxy-isolation').is_file()
        or not Path(os.environ.get('HOME', '/invalid')).resolve().is_relative_to(ISO)):
    raise RuntimeError('isolated source and fake HOME required')
sys.path.insert(0, str(ISO / 'project'))
from core.cli.authority import _capture
from core.discovery import PreflightError


@unittest.skipUnless(sys.platform == 'darwin', 'native Mac ACL and local filesystem required')
class PrivateConfigPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ISO, prefix='private-config-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config_dir = self.root / 'config'
        self.config_dir.mkdir(mode=0o700)
        self.inner = self.config_dir / 'inner'
        self.inner.mkdir(mode=0o700)
        self.config = self.config_dir / 'business.json'
        self.config.write_text('{"fake":true}')
        self.config.chmod(0o600)

    def test_parent_traversal_has_same_target_and_protection(self):
        direct = _capture(self.config, private=True)
        via_parent = _capture(self.inner / '..' / 'business.json', private=True)
        self.assertEqual(via_parent.identity, direct.identity)
        self.assertEqual(via_parent.contents, direct.contents)
        self.assertEqual(via_parent.protection, direct.protection)

    def test_cancelled_directory_replacement_changes_snapshot(self):
        raw = self.inner / '..' / 'business.json'
        before = _capture(raw, private=True)
        self.inner.rename(self.config_dir / 'old-inner')
        self.inner.mkdir(mode=0o700)
        after = _capture(raw, private=True)
        self.assertEqual(before.identity, after.identity)
        self.assertNotEqual(before, after)

    def test_uncertain_permissions_or_missing_cancelled_directory_fail(self):
        raw = self.inner / '..' / 'business.json'
        self.inner.chmod(0o777)
        with self.assertRaises(PreflightError): _capture(raw, private=True)
        self.inner.chmod(0o700)
        self.inner.rmdir()
        with self.assertRaises(PreflightError): _capture(raw, private=True)

    def test_final_symlink_and_symlink_parent_dotdot_rejected(self):
        alias = self.config_dir / 'linked.json'
        alias.symlink_to(self.config.name)
        with self.assertRaises(PreflightError): _capture(alias, private=True)
        other = self.root / 'other'
        other.mkdir(mode=0o700)
        decoy = other / 'business.json'
        decoy.write_text('{"decoy":true}'); decoy.chmod(0o600)
        link = other / 'pivot'
        link.symlink_to(self.inner, target_is_directory=True)
        raw = link / '..' / 'business.json'
        # Actual filesystem traversal chooses config/business; normpath would
        # wrongly select other/business. Neither may be captured by guessing.
        self.assertEqual(raw.read_text(), self.config.read_text())
        self.assertNotEqual(raw.read_text(), decoy.read_text())
        with self.assertRaises(PreflightError): _capture(raw, private=True)
        self.assertEqual(decoy.read_text(), '{"decoy":true}')

    def test_unsafe_target_permissions_and_hardlink_rejected(self):
        self.config.chmod(0o644)
        with self.assertRaises(PreflightError): _capture(self.config, private=True)
        self.config.chmod(0o600)
        os.link(self.config, self.config_dir / 'hardlink')
        with self.assertRaises(PreflightError): _capture(self.config, private=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
