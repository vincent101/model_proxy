"""Held-lock protection tests; run only in a disposable copied project."""

import contextlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = Path(os.environ.get("MODEL_PROXY_TEST_ISOLATION", "/invalid")).resolve()
if (ROOT != SANDBOX / "project" or not (SANDBOX / ".model-proxy-isolation").is_file()
        or not Path(os.environ.get("HOME", "/invalid")).resolve().is_relative_to(SANDBOX)):
    raise RuntimeError("isolated copied project and fake HOME required")

from core import discovery, platform_lock
from core.runtime_paths import PATH_KEYS


class StatView:
    def __init__(self, original, **changes):
        self.original = original
        self.changes = changes

    def __getattr__(self, name):
        return self.changes[name] if name in self.changes else getattr(self.original, name)


@unittest.skipUnless(sys.platform == "darwin", "native Darwin ACL and filesystem required")
class HeldLockPermissionsTests(unittest.TestCase):
    def setUp(self):
        self.case = Path(tempfile.mkdtemp(prefix="held-lock-", dir=SANDBOX / "cases")).resolve()
        self.addCleanup(shutil.rmtree, self.case)
        self.project = self.case / "project"
        self.state = self.project / ".model_proxy_runtime"
        self.runtime = self.project / "runtime"
        self.project.mkdir(mode=0o700)
        self.state.mkdir(mode=0o700)
        self.runtime.mkdir(mode=0o700)
        self.paths = {name: self.runtime / name for name in PATH_KEYS}
        self.target = self.state / "install.json"
        self.anchor_path = self.state / "anchor.lock"
        self.legacy_path = self.paths["lock"]

        # Never inspect ACLs of the real user's ancestors: check only fixture
        # directories and use synthetic evidence for ancestors outside it.
        original = discovery._verify_directory
        synthetic = SANDBOX.stat()
        def verify(path, *, private, _capture=None):
            path = Path(path)
            if path == SANDBOX or path.is_relative_to(SANDBOX):
                return original(path, private=private, _capture=_capture)
            if private:
                raise discovery.PreflightError("protection_unavailable", "outside fake root")
            if _capture is not None:
                _capture.append((str(path), (synthetic.st_dev, synthetic.st_ino,
                    synthetic.st_mode, synthetic.st_uid, synthetic.st_gid, synthetic.st_flags), None))
            return synthetic
        self.directory_patch = mock.patch.object(discovery, "_verify_directory", side_effect=verify)
        self.directory_patch.start()
        self.addCleanup(self.directory_patch.stop)

    @contextlib.contextmanager
    def locks(self, legacy_mode=None):
        if legacy_mode is not None:
            self.legacy_path.touch(mode=0o600)
            self.legacy_path.chmod(legacy_mode)
        snapshot = discovery.inspect_runtime_identities(self.project, self.paths)
        with platform_lock.acquire_lock(snapshot, "anchor.lock", create=True,
                                        access_verifier=platform_lock.verify_local_access) as anchor:
            with platform_lock.acquire_lock(anchor.snapshot, "lock", create=True, anchor=anchor,
                                            access_verifier=platform_lock.verify_local_access) as legacy:
                info = self.project.stat()
                record = {"schema": discovery.INSTANCE_SCHEMA, "record": "install",
                          "install_id": "a" * 32, "project_root": str(self.project),
                          "root_identity": {"device": info.st_dev, "inode": info.st_ino},
                          "initialization_source": "fake test", "ever_started": False,
                          "last_instance_nonce": None}
                yield anchor, legacy, record

    def write(self, anchor, legacy, record):
        discovery.write_instance_record(self.state, "install.json", record,
                                        anchor=anchor, legacy=legacy)

    def assert_denied_without_record(self, anchor, legacy, record):
        with self.assertRaises(discovery.PreflightError):
            self.write(anchor, legacy, record)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.state.glob(".record-*")), [])

    def test_anchor_lost_privacy_after_both_locks_acquired(self):
        with self.locks() as (anchor, legacy, record):
            for mode in (0o666, 0o644):
                with self.subTest(mode=oct(mode)):
                    self.anchor_path.chmod(mode)
                    self.assert_denied_without_record(anchor, legacy, record)
            self.anchor_path.chmod(0o600)
            self.write(anchor, legacy, record)
            self.assertTrue(self.target.exists())

    def test_anchor_owner_read_and_write_are_required(self):
        with self.locks() as (anchor, legacy, record):
            for mode in (0o400, 0o200, 0o000):
                with self.subTest(mode=oct(mode)):
                    self.anchor_path.chmod(mode)
                    self.assert_denied_without_record(anchor, legacy, record)
            self.anchor_path.chmod(0o600)
            self.write(anchor, legacy, record)
            self.assertTrue(self.target.exists())

    def test_existing_record_is_not_replaced_after_anchor_loses_privacy(self):
        with self.locks() as (anchor, legacy, record):
            self.write(anchor, legacy, record)
            previous = self.target.read_bytes()
            self.anchor_path.chmod(0o644)
            with self.assertRaises(discovery.PreflightError):
                self.write(anchor, legacy, record)
            self.assertEqual(self.target.read_bytes(), previous)
            self.assertEqual(list(self.state.glob(".record-*")), [])

    def test_existing_legacy_0644_remains_accepted(self):
        with self.locks(legacy_mode=0o644) as (anchor, legacy, record):
            self.write(anchor, legacy, record)
            self.assertTrue(self.target.exists())

    def test_legacy_missing_owner_read_or_write_is_rejected(self):
        with self.locks(legacy_mode=0o644) as (anchor, legacy, record):
            for mode in (0o444, 0o244, 0o044):
                with self.subTest(mode=oct(mode)):
                    self.legacy_path.chmod(mode)
                    self.assert_denied_without_record(anchor, legacy, record)

    def test_permission_changes_during_verification_are_rejected_for_both_roles(self):
        with self.locks() as (anchor, legacy, record):
            real_acl = discovery.verify_no_extended_acl
            for held, path, mode in ((anchor, self.anchor_path, 0o644),
                                     (legacy, self.legacy_path, 0o644)):
                with self.subTest(role=held.role):
                    changed = []
                    def change_after_acl(fd):
                        result = real_acl(fd)
                        if fd == held.fileno() and not changed:
                            path.chmod(mode)
                            changed.append(True)
                        return result
                    with mock.patch.object(discovery, "verify_no_extended_acl", side_effect=change_after_acl):
                        self.assert_denied_without_record(anchor, legacy, record)
                    self.assertEqual(changed, [True])
                    path.chmod(0o600)

    def test_permission_change_on_other_lock_during_verification_is_rejected(self):
        with self.locks() as (anchor, legacy, record):
            real_acl = discovery.verify_no_extended_acl
            for checked, changed_path in ((anchor, self.legacy_path),
                                          (legacy, self.anchor_path)):
                with self.subTest(checked=checked.role):
                    changed = []
                    def change_other_lock(fd):
                        result = real_acl(fd)
                        if fd == checked.fileno() and not changed:
                            changed_path.chmod(0o644)
                            changed.append(True)
                        return result
                    with mock.patch.object(discovery, "verify_no_extended_acl", side_effect=change_other_lock):
                        self.assert_denied_without_record(anchor, legacy, record)
                    self.assertEqual(changed, [True])
                    changed_path.chmod(0o600)

    def test_path_and_fd_protection_metadata_must_match(self):
        with self.locks() as (anchor, legacy, record):
            original = Path.lstat
            for lock_path in (self.anchor_path, self.legacy_path):
                for field in ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink", "st_ctime_ns", "st_flags"):
                    with self.subTest(role=lock_path.name, field=field):
                        def altered(path, *args, **kwargs):
                            info = original(path, *args, **kwargs)
                            if path == lock_path:
                                return StatView(info, **{field: getattr(info, field) ^ 1})
                            return info
                        with mock.patch.object(Path, "lstat", autospec=True, side_effect=altered):
                            self.assert_denied_without_record(anchor, legacy, record)

    def test_fd_protection_metadata_must_match_path(self):
        with self.locks() as (anchor, legacy, record):
            real_fstat = os.fstat
            for field in ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink", "st_ctime_ns", "st_flags"):
                with self.subTest(field=field):
                    def altered(fd):
                        info = real_fstat(fd)
                        if fd == anchor.fileno():
                            return StatView(info, **{field: getattr(info, field) ^ 1})
                        return info
                    with mock.patch.object(os, "fstat", side_effect=altered):
                        self.assert_denied_without_record(anchor, legacy, record)

    def test_wrong_owner_of_both_path_and_fd_rejected(self):
        with self.locks() as (anchor, legacy, record):
            real_fstat, real_lstat = os.fstat, Path.lstat
            def fstat(fd):
                info = real_fstat(fd)
                return StatView(info, st_uid=os.geteuid() + 1) if fd == anchor.fileno() else info
            def lstat(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                return StatView(info, st_uid=os.geteuid() + 1) if path == self.anchor_path else info
            with mock.patch.object(os, "fstat", side_effect=fstat), \
                 mock.patch.object(Path, "lstat", autospec=True, side_effect=lstat):
                self.assert_denied_without_record(anchor, legacy, record)

    def test_extra_hard_link_is_rejected(self):
        with self.locks() as (anchor, legacy, record):
            extra = self.runtime / "fake-hardlink"
            os.link(self.anchor_path, extra)
            self.assert_denied_without_record(anchor, legacy, record)

    def test_fake_acl_and_local_filesystem_failures_are_rejected(self):
        with self.locks() as (anchor, legacy, record):
            for held in (anchor, legacy):
                for name in ("verify_no_extended_acl", "verify_local_filesystem_fd"):
                    with self.subTest(role=held.role, check=name):
                        original = getattr(discovery, name)
                        called = []
                        def reject(fd):
                            if fd == held.fileno():
                                called.append(True)
                                raise discovery.PreflightError("fake_unverified", "fake")
                            return original(fd)
                        with mock.patch.object(discovery, name, side_effect=reject):
                            self.assert_denied_without_record(anchor, legacy, record)
                        self.assertEqual(called, [True])

    def test_real_extended_acl_on_held_file_is_rejected(self):
        with self.locks() as (anchor, legacy, record):
            # Add an actual ACL only to a newly created fake lock, never HOME.
            import pwd
            user = pwd.getpwuid(os.geteuid()).pw_name
            subprocess.run(["/bin/chmod", "+a", f"user:{user} allow read", str(self.anchor_path)],
                           check=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assert_denied_without_record(anchor, legacy, record)


if __name__ == "__main__":
    unittest.main()
