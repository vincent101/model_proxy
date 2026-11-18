"""Mac record-replacement classification in an explicitly copied fake project."""
from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = Path(os.environ.get("MODEL_PROXY_TEST_ISOLATION", "/invalid")).resolve()
if (ROOT != SANDBOX / "project" or not (SANDBOX / ".model-proxy-isolation").is_file()
        or not Path(os.environ.get("HOME", "/invalid")).resolve().is_relative_to(SANDBOX)):
    raise RuntimeError("isolated copied project and fake HOME required")

from core import darwin_acl, discovery
from core.cli import authority


@unittest.skipUnless(os.name == "posix", "POSIX record checks required")
class PosixRecordOpenRaceTests(unittest.TestCase):
    """Exercise the open boundary on non-Darwin POSIX, in a fake project."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="posix-record-", dir=SANDBOX / "cases")
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.path = self.state / "install.json"
        self.record = {"schema": discovery.INSTANCE_SCHEMA, "record": "install",
                       "install_id": "a" * 32, "project_root": str(self.state.parent),
                       "root_identity": {"device": 1, "inode": 1},
                       "ever_started": False, "last_instance_nonce": None,
                       "initialization_source": "fake"}
        self.path.write_text(json.dumps(self.record), encoding="utf-8")
        self.path.chmod(0o600)

    def test_non_darwin_posix_enoent_retries_only_the_full_observation(self):
        real_open = os.open
        parked = self.state / "parked.json"
        original = self.path.read_bytes()
        attempts = []

        def disappear_once(path, flags, *args, **kwargs):
            attempts.append("open")
            if len(attempts) == 1:
                os.rename(self.path, parked)
            return real_open(path, flags, *args, **kwargs)

        def restore(_delay):
            os.rename(parked, self.path)

        # Non-Darwin protection backends are not implemented here: mock only
        # those checks, and verify they are repeated after a successful open.
        with patch.object(sys, "platform", "linux"), \
                patch.object(discovery.os, "open", side_effect=disappear_once), \
                patch.object(authority.time, "sleep", side_effect=restore) as sleep, \
                patch.object(discovery, "verify_no_extended_acl") as acl, \
                patch.object(discovery, "verify_local_filesystem_fd") as filesystem, \
                patch.object(discovery, "write_instance_record") as writer:
            value = authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(value, self.record)
        self.assertEqual(attempts, ["open", "open"])
        sleep.assert_called_once_with(0.01)
        acl.assert_called_once()
        filesystem.assert_called_once()
        writer.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(parked.exists())

    def test_non_darwin_posix_persistent_open_enoent_is_bounded(self):
        original = self.path.read_bytes()
        with patch.object(sys, "platform", "linux"), \
                patch.object(discovery.os, "open",
                             side_effect=FileNotFoundError(errno.ENOENT, "fake")) as opened, \
                patch.object(authority.time, "sleep") as sleep, \
                patch.object(discovery, "verify_no_extended_acl") as acl, \
                patch.object(discovery, "write_instance_record") as writer:
            with self.assertRaises(discovery.PreflightError) as caught:
                authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(caught.exception.code, "record_changed")
        self.assertEqual(opened.call_count, 4)
        self.assertEqual(sleep.call_count, 3)
        acl.assert_not_called()
        writer.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_non_darwin_posix_other_errors_remain_unavailable(self):
        errors = (PermissionError(errno.EACCES, "fake"),
                  OSError(errno.EIO, "fake"),
                  PermissionError(errno.ENOENT, "wrong exception type"),
                  FileNotFoundError(errno.EACCES, "wrong errno"),
                  FileNotFoundError("missing errno"))
        for error in errors:
            with self.subTest(error=repr(error)), \
                    patch.object(sys, "platform", "linux"), \
                    patch.object(discovery.os, "open", side_effect=error) as opened, \
                    patch.object(authority.time, "sleep") as sleep:
                with self.assertRaises(discovery.PreflightError) as caught:
                    authority._read_retry(lambda: discovery._check_record_target(self.path))
                self.assertEqual(caught.exception.code, "record_unavailable")
                opened.assert_called_once()
                sleep.assert_not_called()

    def test_non_darwin_posix_only_open_enoent_is_retryable(self):
        with patch.object(sys, "platform", "linux"), \
                patch.object(discovery, "verify_no_extended_acl"), \
                patch.object(discovery, "verify_local_filesystem_fd",
                             side_effect=FileNotFoundError(errno.ENOENT, "later")) as filesystem, \
                patch.object(authority.time, "sleep") as sleep:
            with self.assertRaises(discovery.PreflightError) as caught:
                authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(caught.exception.code, "record_unavailable")
        filesystem.assert_called_once()
        sleep.assert_not_called()

    def test_non_darwin_posix_unprotected_file_is_not_opened(self):
        self.path.chmod(0o644)
        with patch.object(sys, "platform", "linux"), \
                patch.object(discovery.os, "open") as opened, \
                patch.object(authority.time, "sleep") as sleep:
            with self.assertRaises(discovery.PreflightError) as caught:
                authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(caught.exception.code, "record_unavailable")
        opened.assert_not_called()
        sleep.assert_not_called()


@unittest.skipUnless(sys.platform == "darwin", "real Darwin unlink and ACL behavior required")
class DarwinRecordReplacementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="record-retry-", dir=SANDBOX / "cases")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / ".model_proxy_runtime"
        self.state.mkdir(mode=0o700)
        self.path = self.state / "install.json"
        self.replacement = self.state / "replacement.json"
        self.old = self.record("old")
        self.new = self.record("new")
        self.write(self.path, self.old)
        self.write(self.replacement, self.new)

    def record(self, source):
        return {"schema": discovery.INSTANCE_SCHEMA, "record": "install", "install_id": "a" * 32,
                "project_root": str(self.root), "root_identity": {"device": 1, "inode": 1},
                "ever_started": False, "last_instance_nonce": None, "initialization_source": source}

    @staticmethod
    def write(path, value, mode=0o600):
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(mode)

    def test_open_enoent_after_protected_lstat_is_changed_and_read_only(self):
        original = self.path.read_bytes()
        with patch.object(discovery.os, "open",
                          side_effect=FileNotFoundError(errno.ENOENT, "fake missing")) as opened, \
                patch.object(discovery, "verify_no_extended_acl") as acl, \
                patch.object(discovery, "verify_local_filesystem_fd") as filesystem, \
                patch.object(discovery, "write_instance_record") as writer:
            with self.assertRaises(discovery.PreflightError) as caught:
                discovery._check_record_target(self.path)
        self.assertEqual(caught.exception.code, "record_changed")
        opened.assert_called_once()
        acl.assert_not_called()
        filesystem.assert_not_called()
        writer.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_open_enoent_disappears_then_restores_and_rechecks_acl(self):
        original = self.path.read_bytes()
        identity = self.path.lstat()
        parked = self.state / "parked-install.json"
        real_open = os.open
        calls = []

        def disappear_before_open(path, flags, *args, **kwargs):
            if Path(path) == self.path:
                calls.append("open")
                if len(calls) == 1:
                    os.rename(self.path, parked)
            return real_open(path, flags, *args, **kwargs)

        def restore_on_retry(delay):
            self.assertEqual(delay, 0.01)
            os.rename(parked, self.path)

        with patch.object(discovery.os, "open", side_effect=disappear_before_open), \
                patch.object(authority.time, "sleep", side_effect=restore_on_retry) as sleep, \
                patch.object(discovery, "verify_no_extended_acl",
                             wraps=discovery.verify_no_extended_acl) as acl, \
                patch.object(discovery, "verify_local_filesystem_fd",
                             wraps=discovery.verify_local_filesystem_fd) as filesystem, \
                patch.object(discovery, "write_instance_record") as writer:
            result = authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(result, self.old)
        self.assertEqual(calls, ["open", "open"])
        sleep.assert_called_once_with(0.01)
        acl.assert_called_once()
        filesystem.assert_called_once()
        writer.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual((self.path.stat().st_dev, self.path.stat().st_ino),
                         (identity.st_dev, identity.st_ino))
        self.assertFalse(parked.exists())

    def test_open_enoent_with_permanent_absence_does_not_authorize_action(self):
        original = self.path.read_bytes()
        parked = self.state / "parked-install.json"
        calls = []
        real_open = os.open

        def disappear_before_open(path, flags, *args, **kwargs):
            if Path(path) == self.path:
                calls.append("open")
                if len(calls) == 1:
                    os.rename(self.path, parked)
            return real_open(path, flags, *args, **kwargs)

        with patch.object(discovery.os, "open", side_effect=disappear_before_open), \
                patch.object(authority.time, "sleep") as sleep, \
                patch.object(discovery, "write_instance_record") as writer:
            observation = authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertIsNone(observation)
        self.assertEqual(calls, ["open"])
        sleep.assert_called_once_with(0.01)
        writer.assert_not_called()
        self.assertFalse(self.path.exists())
        self.assertEqual(parked.read_bytes(), original)

        # Absence remains an observation. It is neither permission to start
        # nor a valid active record for stopping/signalling.
        fake_observation = Mock()
        fake_observation.record_set.return_value = None
        lease = authority.MacAuthorityLease(fake_observation)
        with patch.object(authority.platform_process, "signal_verified") as signal, \
                patch.object(authority.MacAuthorityLease, "admin_token") as token, \
                patch.object(subprocess, "Popen") as spawn:
            with self.assertRaises(authority.ControlError) as start:
                lease.assert_startable()
            self.assertEqual(start.exception.code, "installation_identity_unknown")
            with self.assertRaises(authority.ControlError) as stop:
                authority._valid_record(self.root, None, None)
            self.assertEqual(stop.exception.code, "records_missing")
            signal.assert_not_called()
            token.assert_not_called()
            spawn.assert_not_called()

    def test_open_enoent_with_partial_records_stays_inconsistent(self):
        original = self.path.read_bytes()
        parked = self.state / "parked-install.json"
        paths = {key: str(self.root / ("fake-" + key)) for key in authority.PATH_KEYS}
        common = {"schema": discovery.INSTANCE_SCHEMA, "project_root": str(self.root),
                  "install_id": "a" * 32, "instance_nonce": "b" * 32,
                  "listen": {"host": "127.0.0.1", "port": 32001}, "runtime_paths": paths}
        active = dict(common, record="active", state="READY", pid=12345,
                      process_start={"kind": "darwin_proc_bsdinfo", "seconds": 1,
                                     "microseconds": 0},
                      executable=str(self.root / "fake-python"),
                      locks={"anchor": {"device": 1, "inode": 1},
                             "legacy": {"device": 1, "inode": 2}})
        good = dict(common, record="last_good_paths")
        active_path, good_path = self.state / "active.json", self.state / "last_good_paths.json"
        self.write(active_path, active)
        self.write(good_path, good)
        active_bytes, good_bytes = active_path.read_bytes(), good_path.read_bytes()
        real_open = os.open
        calls = []

        def disappear_before_open(path, flags, *args, **kwargs):
            if Path(path) == self.path:
                calls.append("open")
                if len(calls) == 1:
                    os.rename(self.path, parked)
            return real_open(path, flags, *args, **kwargs)

        with patch.object(discovery.os, "open", side_effect=disappear_before_open), \
                patch.object(authority.time, "sleep") as sleep, \
                patch.object(discovery, "write_instance_record") as writer, \
                patch.object(authority.platform_process, "signal_verified") as signal, \
                patch.object(authority.MacAuthorityLease, "admin_token") as token, \
                patch.object(subprocess, "Popen") as spawn:
            with self.assertRaises(discovery.PreflightError) as caught:
                authority._read_retry(lambda: discovery.read_instance_set(self.state))
        self.assertEqual(caught.exception.code, "records_inconsistent")
        self.assertEqual(calls, ["open"])
        sleep.assert_called_once_with(0.01)
        writer.assert_not_called()
        signal.assert_not_called()
        token.assert_not_called()
        spawn.assert_not_called()
        self.assertEqual(parked.read_bytes(), original)
        self.assertEqual(active_path.read_bytes(), active_bytes)
        self.assertEqual(good_path.read_bytes(), good_bytes)
        self.assertFalse(self.path.exists())

    def test_repeated_open_enoent_stops_after_four_attempts(self):
        original = self.path.read_bytes()
        parked = self.state / "parked-install.json"
        identity = self.path.lstat()
        real_open = os.open
        calls = []

        def disappear_and_restore(path, flags, *args, **kwargs):
            if Path(path) != self.path:
                return real_open(path, flags, *args, **kwargs)
            calls.append("open")
            os.rename(self.path, parked)
            try:
                return real_open(path, flags, *args, **kwargs)
            finally:
                os.rename(parked, self.path)

        with patch.object(discovery.os, "open", side_effect=disappear_and_restore), \
                patch.object(authority.time, "sleep") as sleep, \
                patch.object(discovery, "verify_no_extended_acl") as acl, \
                patch.object(discovery, "write_instance_record") as writer:
            with self.assertRaises(discovery.PreflightError) as caught:
                authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(caught.exception.code, "record_changed")
        self.assertEqual(len(calls), 4)
        self.assertEqual(sleep.call_count, 3)
        acl.assert_not_called()
        writer.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual((self.path.stat().st_dev, self.path.stat().st_ino),
                         (identity.st_dev, identity.st_ino))
        self.assertFalse(parked.exists())

    def test_non_enoent_open_errors_and_wrong_types_are_not_retried(self):
        original = self.path.read_bytes()
        cases = (PermissionError(errno.EACCES, "fake permission"),
                 OSError(errno.EIO, "fake io"),
                 PermissionError(errno.EPERM, "fake permission"),
                 OSError(errno.ELOOP, "fake symlink"),
                 NotADirectoryError(errno.ENOTDIR, "fake type"),
                 PermissionError(errno.ENOENT, "fake mismatched type"),
                 FileNotFoundError(errno.EACCES, "fake mismatched errno"),
                 FileNotFoundError("fake missing errno"))
        for error in cases:
            with self.subTest(error_type=type(error).__name__, error_no=error.errno), \
                    patch.object(discovery.os, "open", side_effect=error) as opened, \
                    patch.object(authority.time, "sleep") as sleep, \
                    patch.object(discovery, "verify_no_extended_acl") as acl, \
                    patch.object(discovery, "write_instance_record") as writer:
                with self.assertRaises(discovery.PreflightError) as caught:
                    authority._read_retry(lambda: discovery._check_record_target(self.path))
                self.assertEqual(caught.exception.code, "record_unavailable")
                opened.assert_called_once()
                sleep.assert_not_called()
                acl.assert_not_called()
                writer.assert_not_called()
            self.assertEqual(self.path.read_bytes(), original)

    def test_unprotected_lstat_is_rejected_before_open(self):
        self.path.chmod(0o644)
        original = self.path.read_bytes()
        with patch.object(discovery.os, "open",
                          side_effect=FileNotFoundError(errno.ENOENT, "fake")) as opened, \
                patch.object(authority.time, "sleep") as sleep:
            with self.assertRaises(discovery.PreflightError) as caught:
                authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(caught.exception.code, "record_unavailable")
        opened.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_later_enoent_does_not_become_record_changed(self):
        original = self.path.read_bytes()
        with patch.object(discovery, "verify_local_filesystem_fd",
                          side_effect=FileNotFoundError(errno.ENOENT, "fake later error")) as filesystem, \
                patch.object(authority.time, "sleep") as sleep, \
                patch.object(discovery, "write_instance_record") as writer:
            with self.assertRaises(discovery.PreflightError) as caught:
                authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(caught.exception.code, "record_unavailable")
        filesystem.assert_called_once()
        sleep.assert_not_called()
        writer.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_non_posix_open_enoent_keeps_existing_error(self):
        original = self.path.read_bytes()
        with patch.object(sys, "platform", "win32"), \
                patch.object(discovery.os, "name", "nt"), \
                patch.object(discovery.os, "open",
                             side_effect=FileNotFoundError(errno.ENOENT, "fake")) as opened, \
                patch.object(authority.time, "sleep") as sleep:
            with self.assertRaises(discovery.PreflightError) as caught:
                authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(caught.exception.code, "record_unavailable")
        opened.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_real_replace_maps_only_to_record_changed_before_any_read(self):
        real = discovery.verify_no_extended_acl
        observed = []
        def replace_then_verify(fd):
            before = os.fstat(fd)
            os.replace(self.replacement, self.path)
            after = os.fstat(fd)
            observed.append((before.st_nlink, after.st_nlink, before.st_ino, after.st_ino))
            return real(fd)
        with patch.object(discovery, "verify_no_extended_acl", side_effect=replace_then_verify), \
             patch.object(discovery, "verify_local_filesystem_fd") as filesystem, \
             patch("json.loads", side_effect=AssertionError("no record data may be parsed")):
            with self.assertRaises(discovery.PreflightError) as caught:
                discovery._check_record_target(self.path)
        self.assertEqual(caught.exception.code, "record_changed")
        self.assertEqual(observed[0][:2], (1, 0))
        self.assertEqual(observed[0][2], observed[0][3])
        filesystem.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()), self.new)

    def test_original_retry_reopens_and_fully_verifies_new_record(self):
        real = discovery.verify_no_extended_acl
        seen = []
        def replace_once(fd):
            seen.append(os.fstat(fd).st_ino)
            if len(seen) == 1:
                os.replace(self.replacement, self.path)
            return real(fd)
        with patch.object(discovery, "verify_no_extended_acl", side_effect=replace_once), \
             patch.object(authority.time, "sleep") as sleep:
            value = authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(value, self.new)
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])
        sleep.assert_called_once()

    def test_replace_after_open_before_first_fstat_retries_full_read(self):
        real_open = os.open
        real_acl = discovery.verify_no_extended_acl
        real_filesystem = discovery.verify_local_filesystem_fd
        observations, acl_links = [], []

        def open_then_replace(target, flags, *args, **kwargs):
            fd = real_open(target, flags, *args, **kwargs)
            if Path(target) == self.path and not observations:
                before = os.fstat(fd)
                os.replace(self.replacement, self.path)
                after = os.fstat(fd)
                observations.append((before, after, self.path.lstat()))
            return fd

        def acl(fd):
            acl_links.append(os.fstat(fd).st_nlink)
            return real_acl(fd)

        with patch.object(discovery.os, "open", side_effect=open_then_replace), \
                patch.object(discovery, "verify_no_extended_acl", side_effect=acl), \
                patch.object(discovery, "verify_local_filesystem_fd", wraps=real_filesystem) as fs, \
                patch.object(authority.time, "sleep") as sleep:
            value = authority._read_retry(lambda: discovery._check_record_target(self.path))
        before, after, current = observations[0]
        self.assertEqual((before.st_nlink, after.st_nlink), (1, 0))
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self.assertNotEqual((after.st_dev, after.st_ino), (current.st_dev, current.st_ino))
        self.assertEqual(value, self.new)
        self.assertEqual(acl_links, [0, 1])
        fs.assert_called_once()
        sleep.assert_called_once_with(0.01)

    def test_early_replace_does_not_accept_unknown_new_acl(self):
        real_open = os.open
        real_acl = darwin_acl.read_acl_snapshot
        opened, acl_links = [], []

        def open_then_replace(target, flags, *args, **kwargs):
            fd = real_open(target, flags, *args, **kwargs)
            if Path(target) == self.path and not opened:
                os.replace(self.replacement, self.path)
                opened.append(fd)
            return fd

        def acl(fd):
            acl_links.append(os.fstat(fd).st_nlink)
            if len(acl_links) == 1:
                return real_acl(fd)
            raise darwin_acl.ACLVerificationError("acl_unavailable")

        with patch.object(discovery.os, "open", side_effect=open_then_replace), \
                patch.object(darwin_acl, "read_acl_snapshot", side_effect=acl), \
                patch.object(discovery, "verify_local_filesystem_fd") as fs, \
                patch.object(authority.time, "sleep") as sleep:
            with self.assertRaises(discovery.PreflightError) as caught:
                authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(caught.exception.code, "protection_unavailable")
        self.assertEqual(acl_links, [0, 1])
        fs.assert_not_called()
        sleep.assert_called_once_with(0.01)

    def test_early_replace_rejects_real_allow_acl_on_new_file(self):
        result = subprocess.run(["/bin/chmod", "+a", "group:everyone allow read",
                                 str(self.replacement)],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, "fake ACL fixture could not be created")
        real_open, real_acl = os.open, discovery.verify_no_extended_acl
        seen, acl_links = [], []

        def open_then_replace(target, flags, *args, **kwargs):
            fd = real_open(target, flags, *args, **kwargs)
            if Path(target) == self.path and not seen:
                os.replace(self.replacement, self.path)
                seen.append(fd)
            return fd

        def acl(fd):
            acl_links.append(os.fstat(fd).st_nlink)
            return real_acl(fd)

        with patch.object(discovery.os, "open", side_effect=open_then_replace), \
                patch.object(discovery, "verify_no_extended_acl", side_effect=acl), \
                patch.object(authority.time, "sleep") as sleep:
            with self.assertRaises(discovery.PreflightError) as caught:
                authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(caught.exception.code, "protection_unavailable")
        self.assertEqual(acl_links, [0, 1])
        sleep.assert_called_once_with(0.01)

    def test_early_replacement_retry_remains_bounded(self):
        real_open = os.open
        attempts = []

        def replace_on_every_open(target, flags, *args, **kwargs):
            fd = real_open(target, flags, *args, **kwargs)
            if Path(target) == self.path:
                attempts.append(os.fstat(fd).st_ino)
                replacement = self.state / ("early-next-" + str(len(attempts)))
                self.write(replacement, self.record("next"))
                os.replace(replacement, self.path)
            return fd

        with patch.object(discovery.os, "open", side_effect=replace_on_every_open), \
                patch.object(authority.time, "sleep") as sleep:
            with self.assertRaises(discovery.PreflightError) as caught:
                authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(caught.exception.code, "record_changed")
        self.assertEqual(len(attempts), 4)
        self.assertEqual(sleep.call_count, 3)

    def test_early_replacement_with_changed_old_protection_is_unknown(self):
        real_open = os.open

        def alter_then_replace(target, flags, *args, **kwargs):
            fd = real_open(target, flags, *args, **kwargs)
            if Path(target) == self.path:
                os.fchmod(fd, 0o400)
                os.replace(self.replacement, self.path)
            return fd

        with patch.object(discovery.os, "open", side_effect=alter_then_replace), \
                patch.object(authority.time, "sleep") as sleep:
            with self.assertRaises(discovery.PreflightError) as caught:
                authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(caught.exception.code, "protection_unavailable")
        sleep.assert_not_called()

    def test_changed_opened_mode_is_not_hidden_by_later_restore(self):
        real_open = os.open
        real_acl = discovery.verify_no_extended_acl

        def open_change_replace(target, flags, *args, **kwargs):
            fd = real_open(target, flags, *args, **kwargs)
            if Path(target) == self.path:
                os.fchmod(fd, 0o400)
                os.replace(self.replacement, self.path)
            return fd

        def restore_then_verify(fd):
            os.fchmod(fd, 0o600)
            return real_acl(fd)

        with patch.object(discovery.os, "open", side_effect=open_change_replace), \
                patch.object(discovery, "verify_no_extended_acl", side_effect=restore_then_verify), \
                patch.object(authority.time, "sleep") as sleep:
            with self.assertRaises(discovery.PreflightError) as caught:
                authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(caught.exception.code, "protection_unavailable")
        sleep.assert_not_called()

    def test_opened_multiple_links_is_not_hidden_by_later_unlink(self):
        real_open = os.open
        real_acl = discovery.verify_no_extended_acl
        alias = self.state / "temporary-old-hardlink"

        def open_then_link(target, flags, *args, **kwargs):
            fd = real_open(target, flags, *args, **kwargs)
            if Path(target) == self.path:
                os.link(self.path, alias)
            return fd

        def unlink_then_replace(fd):
            alias.unlink()
            os.replace(self.replacement, self.path)
            return real_acl(fd)

        with patch.object(discovery.os, "open", side_effect=open_then_link), \
                patch.object(discovery, "verify_no_extended_acl", side_effect=unlink_then_replace), \
                patch.object(authority.time, "sleep") as sleep:
            with self.assertRaises(discovery.PreflightError) as caught:
                authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(caught.exception.code, "protection_unavailable")
        sleep.assert_not_called()

    def test_early_unlinked_pipe_spoof_is_not_retryable(self):
        target_stat = self.path.lstat()
        values = list(target_stat)
        values[stat.ST_NLINK] = 0
        forged_opened = os.stat_result(values)
        read_fd, write_fd = os.pipe()
        duplicate = os.dup(read_fd)
        real_fstat = os.fstat
        calls = []

        def forged_then_real(fd):
            calls.append(fd)
            return forged_opened if len(calls) == 1 else real_fstat(fd)

        try:
            with patch.object(discovery.os, "open", return_value=duplicate), \
                    patch.object(discovery.os, "fstat", side_effect=forged_then_real), \
                    patch.object(authority.time, "sleep") as sleep:
                with self.assertRaises(discovery.PreflightError) as caught:
                    authority._read_retry(lambda: discovery._check_record_target(self.path))
            self.assertEqual(caught.exception.code, "protection_unavailable")
            self.assertGreaterEqual(len(calls), 2)
            sleep.assert_not_called()
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_early_unlink_with_unprotected_or_missing_new_path_is_unknown(self):
        for scenario in ("world_readable", "symlink", "hardlinked", "missing", "directory"):
            with self.subTest(scenario=scenario), \
                    tempfile.TemporaryDirectory(prefix="early-negative-", dir=self.root) as temp:
                area = Path(temp)
                target, replacement, alias = (area / "install.json", area / "replacement",
                                               area / "alias")
                self.write(target, self.old)
                if scenario == "directory":
                    replacement.mkdir()
                elif scenario == "symlink":
                    self.write(alias, self.new)
                    replacement.symlink_to(alias)
                else:
                    self.write(replacement, self.new, 0o644 if scenario == "world_readable" else 0o600)
                    if scenario == "hardlinked":
                        os.link(replacement, alias)
                real_open = os.open

                def open_then_replace(path, flags, *args, **kwargs):
                    fd = real_open(path, flags, *args, **kwargs)
                    if Path(path) == target:
                        if scenario == "directory":
                            target.unlink()
                            os.rename(replacement, target)
                        else:
                            os.replace(replacement, target)
                            if scenario == "missing":
                                target.unlink()
                    return fd

                with patch.object(discovery.os, "open", side_effect=open_then_replace), \
                        patch.object(authority.time, "sleep") as sleep:
                    with self.assertRaises(discovery.PreflightError) as caught:
                        authority._read_retry(lambda: discovery._check_record_target(target))
                self.assertEqual(caught.exception.code, "protection_unavailable")
                sleep.assert_not_called()

    def test_retry_remains_bounded_under_repeated_real_replacements(self):
        real = discovery.verify_no_extended_acl
        attempts = []
        def replace_every_time(fd):
            attempts.append(os.fstat(fd).st_ino)
            replacement = self.state / ("next-" + str(len(attempts)))
            self.write(replacement, self.record("next"))
            os.replace(replacement, self.path)
            return real(fd)
        with patch.object(discovery, "verify_no_extended_acl", side_effect=replace_every_time), \
             patch.object(authority.time, "sleep") as sleep:
            with self.assertRaises(discovery.PreflightError) as caught:
                authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(caught.exception.code, "record_changed")
        self.assertEqual(len(attempts), 4)
        self.assertEqual(sleep.call_count, 3)

    def test_new_unknown_acl_is_not_accepted_on_retry(self):
        real_acl = darwin_acl.read_acl_snapshot
        calls = []
        def changing_acl(fd):
            calls.append(os.fstat(fd).st_ino)
            if len(calls) == 1:
                os.replace(self.replacement, self.path)
                return real_acl(fd)  # actual unlinked-fd identity_unverified
            raise darwin_acl.ACLVerificationError("file_acl_present")
        with patch.object(darwin_acl, "read_acl_snapshot", side_effect=changing_acl), \
             patch.object(authority.time, "sleep"):
            with self.assertRaises(discovery.PreflightError) as caught:
                authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(caught.exception.code, "protection_unavailable")
        self.assertEqual(len(calls), 2)

    def test_real_new_allow_acl_is_rejected_by_full_retry(self):
        changed = subprocess.run(["/bin/chmod", "+a", "group:everyone allow read", str(self.replacement)],
                                 capture_output=True, text=True)
        self.assertEqual(changed.returncode, 0, "fake ACL fixture could not be created")
        real = discovery.verify_no_extended_acl
        calls = []
        def replace_once(fd):
            calls.append(os.fstat(fd).st_ino)
            if len(calls) == 1:
                os.replace(self.replacement, self.path)
            return real(fd)
        with patch.object(discovery, "verify_no_extended_acl", side_effect=replace_once), \
             patch.object(authority.time, "sleep") as sleep:
            with self.assertRaises(discovery.PreflightError) as caught:
                authority._read_retry(lambda: discovery._check_record_target(self.path))
        self.assertEqual(caught.exception.code, "protection_unavailable")
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])
        sleep.assert_called_once()

    def test_reused_descriptor_with_different_unlinked_inode_is_rejected(self):
        original_fd = os.open(self.path, os.O_RDONLY)
        original = os.fstat(original_fd)
        os.close(original_fd)
        other = self.state / "other-file"
        self.write(other, self.old)
        reused = os.open(other, os.O_RDONLY)
        if reused != original_fd:
            os.dup2(reused, original_fd)
            os.close(reused)
        try:
            os.unlink(other)
            os.replace(self.replacement, self.path)
            current = os.fstat(original_fd)
            self.assertEqual(current.st_nlink, 0)
            self.assertNotEqual(current.st_ino, original.st_ino)
            self.assertFalse(discovery._darwin_unlinked_record_replaced(original_fd, self.path, original))
        finally:
            os.close(original_fd)

    def test_old_fd_protection_change_prevents_retry_classification(self):
        fd = os.open(self.path, os.O_RDONLY)
        before = os.fstat(fd)
        try:
            os.fchmod(fd, 0o400)
            os.replace(self.replacement, self.path)
            self.assertEqual(os.fstat(fd).st_nlink, 0)
            self.assertFalse(discovery._darwin_unlinked_record_replaced(fd, self.path, before))
        finally:
            os.close(fd)

    def test_other_acl_errors_do_not_become_retryable_even_after_replace(self):
        for code in ("acl_unavailable", "acl_changed", "file_acl_present"):
            with self.subTest(code=code):
                replacement = self.state / ("bad-" + code)
                self.write(replacement, self.new)
                def fail_after_replace(fd):
                    os.replace(replacement, self.path)
                    raise darwin_acl.ACLVerificationError(code)
                with patch.object(darwin_acl, "read_acl_snapshot", side_effect=fail_after_replace):
                    with self.assertRaises(discovery.PreflightError) as caught:
                        discovery._check_record_target(self.path)
                self.assertEqual(caught.exception.code, "protection_unavailable")

    def test_forged_preflight_error_without_original_acl_error_is_not_retried(self):
        def fail_after_replace(fd):
            os.replace(self.replacement, self.path)
            raise discovery.PreflightError("protection_unavailable", "unknown")
        with patch.object(discovery, "verify_no_extended_acl", side_effect=fail_after_replace):
            with self.assertRaises(discovery.PreflightError) as caught:
                discovery._check_record_target(self.path)
        self.assertEqual(caught.exception.code, "protection_unavailable")

    def test_record_reader_preserves_acl_failure_when_fd_becomes_unavailable(self):
        real_fstat = os.fstat
        calls = []
        def fail_after_initial(fd):
            calls.append(fd)
            if len(calls) == 1:
                return real_fstat(fd)
            raise OSError(errno.EBADF, "fake closed fd")
        def replace_and_fail(fd):
            os.replace(self.replacement, self.path)
            raise darwin_acl.ACLVerificationError("identity_unverified")
        with patch.object(discovery.os, "fstat", side_effect=fail_after_initial), \
             patch.object(darwin_acl, "read_acl_snapshot", side_effect=replace_and_fail):
            with self.assertRaises(discovery.PreflightError) as caught:
                discovery._check_record_target(self.path)
        self.assertEqual(caught.exception.code, "protection_unavailable")
        self.assertGreaterEqual(len(calls), 2)

    def test_record_reader_preserves_acl_failure_for_pipe_descriptor(self):
        target_stat = self.path.lstat()
        read_fd, write_fd = os.pipe()
        duplicate = os.dup(read_fd)
        real_fstat = os.fstat
        calls = []
        def initial_spoof_then_real(fd):
            calls.append(fd)
            return target_stat if len(calls) == 1 else real_fstat(fd)
        try:
            with patch.object(discovery.os, "open", return_value=duplicate), \
                 patch.object(discovery.os, "fstat", side_effect=initial_spoof_then_real):
                with self.assertRaises(discovery.PreflightError) as caught:
                    discovery._check_record_target(self.path)
            self.assertEqual(caught.exception.code, "protection_unavailable")
            self.assertGreaterEqual(len(calls), 2)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_original_inode_still_linked_is_not_classified_as_replacement(self):
        extra = self.state / "old-hardlink"
        def fail_after_replace(fd):
            os.link(self.path, extra)
            os.replace(self.replacement, self.path)
            self.assertEqual(os.fstat(fd).st_nlink, 1)
            raise darwin_acl.ACLVerificationError("identity_unverified")
        with patch.object(darwin_acl, "read_acl_snapshot", side_effect=fail_after_replace):
            with self.assertRaises(discovery.PreflightError) as caught:
                discovery._check_record_target(self.path)
        self.assertEqual(caught.exception.code, "protection_unavailable")

    def test_missing_or_unprotected_replacement_is_never_retry_evidence(self):
        for scenario in ("missing", "world_readable", "symlink", "hardlinked", "directory"):
            with self.subTest(scenario=scenario):
                self.write(self.path, self.old)
                replacement = self.state / ("target-" + scenario)
                alias = self.state / ("alias-" + scenario)
                if scenario == "directory":
                    replacement.mkdir()
                elif scenario == "symlink":
                    self.write(alias, self.new)
                    replacement.symlink_to(alias)
                else:
                    self.write(replacement, self.new, 0o644 if scenario == "world_readable" else 0o600)
                    if scenario == "hardlinked":
                        os.link(replacement, alias)
                real_verify = discovery.verify_no_extended_acl
                def mutate_then_call(fd):
                    if scenario == "directory":
                        self.path.unlink()
                        os.rename(replacement, self.path)
                    else:
                        os.replace(replacement, self.path)
                        if scenario == "missing":
                            self.path.unlink()
                    return real_verify(fd)
                with patch.object(discovery, "verify_no_extended_acl", side_effect=mutate_then_call):
                    with self.assertRaises(discovery.PreflightError) as caught:
                        discovery._check_record_target(self.path)
                self.assertEqual(caught.exception.code, "protection_unavailable")
                if self.path.is_dir() and not self.path.is_symlink(): self.path.rmdir()
                elif self.path.is_symlink() or self.path.exists(): self.path.unlink()
                if alias.exists() or alias.is_symlink(): alias.unlink()

    def test_helper_rejects_pipe_closed_fd_unchanged_path_and_untrusted_old(self):
        fd = os.open(self.path, os.O_RDONLY)
        before = os.fstat(fd)
        try:
            self.assertFalse(discovery._darwin_unlinked_record_replaced(fd, self.path, before))
            os.replace(self.replacement, self.path)
            self.assertTrue(discovery._darwin_unlinked_record_replaced(fd, self.path, before))
        finally:
            os.close(fd)
        self.assertFalse(discovery._darwin_unlinked_record_replaced(fd, self.path, before))
        read_fd, write_fd = os.pipe()
        try:
            pipe_stat = os.fstat(read_fd)
            self.assertTrue(stat.S_ISFIFO(pipe_stat.st_mode))
            self.assertFalse(discovery._darwin_unlinked_record_replaced(read_fd, self.path, pipe_stat))
        finally:
            os.close(read_fd)
            os.close(write_fd)
        old = self.state / "untrusted-old"
        new = self.state / "untrusted-new"
        self.write(old, self.old, 0o644)
        self.write(new, self.new)
        old_fd = os.open(old, os.O_RDONLY)
        try:
            old_stat = os.fstat(old_fd)
            os.replace(new, old)
            self.assertFalse(discovery._darwin_unlinked_record_replaced(old_fd, old, old_stat))
        finally:
            os.close(old_fd)


if __name__ == "__main__":
    unittest.main()
