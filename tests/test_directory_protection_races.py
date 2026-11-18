"""Real Darwin directory races, restricted to a copied fake project."""
import contextlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = Path(os.environ.get("MODEL_PROXY_TEST_ISOLATION", "/invalid")).resolve()
if (ROOT != SANDBOX / "project" or not (SANDBOX / ".model-proxy-isolation").is_file()
        or not Path(os.environ.get("HOME", "/invalid")).is_relative_to(SANDBOX)):
    raise RuntimeError("isolated copied project and fake HOME required")

from core import darwin_acl, discovery, platform_lock, secure_files
from core.runtime_paths import PATH_KEYS


@unittest.skipUnless(sys.platform == "darwin", "real Darwin directory tests required")
class DirectoryProtectionRaces(unittest.TestCase):
    def setUp(self):
        self.case = Path(tempfile.mkdtemp(prefix="dir-race-", dir=SANDBOX / "cases")).resolve()
        self.shared = self.case / "shared"
        self.project = self.shared / "project"
        self.state = self.project / ".model_proxy_runtime"
        self.runtime = self.project / "runtime"
        self.shared.mkdir(mode=0o700)
        self.project.mkdir(mode=0o700)
        self.state.mkdir(mode=0o700)
        self.runtime.mkdir(mode=0o700)
        self.paths = {name: self.runtime / name for name in PATH_KEYS}
        self.noise = self.shared / "unrelated"
        self.moved = self.case / "relocated"
        self.shared_flags = self.shared.stat().st_flags
        self.state_flags = self.state.stat().st_flags
        # The test observes native ACLs only inside the fake sandbox.
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
        self.ancestor_patch = mock.patch.object(discovery, "_verify_directory", side_effect=verify)
        self.ancestor_patch.start()

    def tearDown(self):
        self.ancestor_patch.stop()
        if self.moved.exists():
            if self.shared.exists():
                self.shared.rmdir()
            os.rename(self.moved, self.shared)
        if self.shared.exists():
            if self.shared.stat().st_flags != self.shared_flags:
                os.chflags(self.shared, self.shared_flags)
            subprocess.run(["/bin/chmod", "-N", str(self.shared)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if self.state.exists() and self.state.stat().st_flags != self.state_flags:
            os.chflags(self.state, self.state_flags)
        shutil.rmtree(self.case)

    @contextlib.contextmanager
    def record(self):
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

    def make_noise(self):
        time.sleep(0.005)
        self.noise.write_text("FAKE SIBLING", encoding="utf-8")

    def test_unrelated_siblings_before_precommit_and_before_after_replace(self):
        # Separate installations keep all lock and record identities independent.
        for phase in ("precommit", "before_replace", "after_replace"):
            with self.subTest(phase=phase):
                other = type(self)(methodName="runTest")
                other.setUp()
                try:
                    with other.record() as (anchor, legacy, record):
                        old_ctime = other.shared.stat().st_ctime_ns
                        real_fsync, real_replace = os.fsync, os.replace
                        changed = []
                        def sync(fd):
                            real_fsync(fd)
                            if phase == "precommit" and not changed:
                                other.make_noise()
                                changed.append(True)
                        def replace(src, dst, *args, **kwargs):
                            if not Path(src).is_relative_to(other.case) or not Path(dst).is_relative_to(other.case):
                                raise AssertionError("non-fake replacement")
                            if phase == "before_replace":
                                other.make_noise()
                                changed.append(True)
                            result = real_replace(src, dst, *args, **kwargs)
                            if phase == "after_replace":
                                other.make_noise()
                                changed.append(True)
                            return result
                        with mock.patch.object(discovery.os, "fsync", side_effect=sync), \
                             mock.patch.object(discovery.os, "replace", side_effect=replace):
                            other.write(anchor, legacy, record)
                        self.assertEqual(changed, [True])
                        self.assertNotEqual(old_ctime, other.shared.stat().st_ctime_ns)
                        self.assertEqual(json.loads((other.state / "install.json").read_text()), record)
                finally:
                    other.tearDown()

    def test_unrelated_sibling_deletion_after_replace(self):
        self.noise.write_text("FAKE", encoding="utf-8")
        with self.record() as (anchor, legacy, record):
            before = self.shared.stat().st_ctime_ns
            replace = os.replace
            def inject(src, dst, *args, **kwargs):
                result = replace(src, dst, *args, **kwargs)
                time.sleep(0.005)
                self.noise.unlink()
                return result
            with mock.patch.object(discovery.os, "replace", side_effect=inject):
                self.write(anchor, legacy, record)
            self.assertNotEqual(before, self.shared.stat().st_ctime_ns)
            self.assertEqual(json.loads((self.state / "install.json").read_text()), record)

    def test_parallel_sibling_create_delete_at_precommit_and_postcommit(self):
        for phase in ("precommit", "postcommit"):
            for operation in ("create", "delete"):
                with self.subTest(phase=phase, operation=operation):
                    other = type(self)(methodName="runTest")
                    other.setUp()
                    worker = None
                    request = threading.Event()
                    finished = threading.Event()
                    errors = []
                    actor = []
                    try:
                        with other.record() as (anchor, legacy, record):
                            if operation == "delete":
                                other.noise.write_text("FAKE", encoding="utf-8")
                            before = other.shared.stat().st_ctime_ns
                            def mutate():
                                try:
                                    if not request.wait(5):
                                        raise RuntimeError("fake mutation was not requested")
                                    actor.append(threading.get_ident())
                                    time.sleep(0.005)
                                    if operation == "create":
                                        other.noise.write_text("FAKE", encoding="utf-8")
                                    else:
                                        other.noise.unlink()
                                except BaseException as exc:
                                    errors.append(exc)
                                finally:
                                    finished.set()
                            worker = threading.Thread(target=mutate, name="fake-sibling")
                            worker.start()
                            def rendezvous():
                                request.set()
                                if not finished.wait(5) or errors:
                                    raise RuntimeError("fake sibling mutation did not complete")
                            real_fsync, real_replace = os.fsync, os.replace
                            def sync(fd):
                                real_fsync(fd)
                                if phase == "precommit" and not request.is_set():
                                    rendezvous()
                            def replace(src, dst, *args, **kwargs):
                                if not Path(src).is_relative_to(other.case) or not Path(dst).is_relative_to(other.case):
                                    raise AssertionError("non-fake replacement")
                                result = real_replace(src, dst, *args, **kwargs)
                                if phase == "postcommit":
                                    rendezvous()
                                return result
                            with mock.patch.object(discovery.os, "fsync", side_effect=sync), \
                                 mock.patch.object(discovery.os, "replace", side_effect=replace):
                                other.write(anchor, legacy, record)
                            self.assertEqual(len(actor), 1)
                            self.assertNotEqual(actor[0], threading.get_ident())
                            self.assertNotEqual(before, other.shared.stat().st_ctime_ns)
                            self.assertEqual(json.loads((other.state / "install.json").read_text()), record)
                    finally:
                        request.set()
                        if worker is not None:
                            worker.join(5)
                        # Never delete a fixture while its worker is still active.
                        if worker is None or not worker.is_alive():
                            other.tearDown()
                        else:
                            self.fail("fake mutation worker did not exit")

    def _postcommit_change(self, change):
        with self.record() as (anchor, legacy, record):
            replace = os.replace
            def inject(src, dst, *args, **kwargs):
                result = replace(src, dst, *args, **kwargs)
                change()
                return result
            with mock.patch.object(discovery.os, "replace", side_effect=inject):
                with self.assertRaises(discovery.PreflightError) as caught:
                    self.write(anchor, legacy, record)
            self.assertEqual(caught.exception.code, "record_postcheck_failed")
            # Restore a moved fake ancestor before reading or releasing fixtures.
            if self.moved.exists():
                self.shared.rmdir()
                os.rename(self.moved, self.shared)
            self.assertEqual(json.loads((self.state / "install.json").read_text()), record)

    def test_real_mode_change_is_rejected(self):
        self._postcommit_change(lambda: os.chmod(self.shared, 0o750))

    def test_real_deny_and_allow_acl_changes_are_rejected(self):
        for rule in ("group:everyone deny delete", "group:everyone allow list"):
            with self.subTest(rule=rule):
                other = type(self)(methodName="runTest")
                other.setUp()
                try:
                    def add_acl():
                        result = subprocess.run(["/bin/chmod", "+a", rule, str(other.shared)],
                                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        self.assertEqual(result.returncode, 0, "cannot create fake ACL")
                    other._postcommit_change(add_acl)
                finally:
                    other.tearDown()

    def test_real_flags_change_on_ancestor_and_state_is_rejected(self):
        if not hasattr(os, "chflags") or not hasattr(stat, "UF_IMMUTABLE"):
            self.skipTest("Darwin immutable flags unavailable")
        for name in ("shared", "state"):
            with self.subTest(directory=name):
                other = type(self)(methodName="runTest")
                other.setUp()
                try:
                    target = getattr(other, name)
                    other._postcommit_change(lambda: os.chflags(target, target.stat().st_flags | stat.UF_IMMUTABLE))
                finally:
                    other.tearDown()

    def test_real_group_change_is_rejected_when_permitted(self):
        original = self.shared.stat().st_gid
        eligible = [gid for gid in os.getgroups() if gid != original]
        if not eligible:
            self.skipTest("no alternative permitted group")
        probe = self.case / "group-probe"
        probe.mkdir(mode=0o700)
        permitted = None
        for gid in eligible:
            try:
                os.chown(probe, -1, gid)
            except PermissionError:
                continue
            permitted = gid
            break
        probe.rmdir()
        if permitted is None:
            self.skipTest("group change unavailable")
        # Some Darwin identities may change to a supplemental group but cannot
        # change back to the inherited group. The owned fake fixture is
        # disposable; do not require or attempt a second chown.
        self._postcommit_change(lambda: os.chown(self.shared, -1, permitted))

    def test_replaced_ancestor_is_rejected(self):
        def replace_ancestor():
            os.rename(self.shared, self.moved)
            self.shared.mkdir(mode=0o700)
        self._postcommit_change(replace_ancestor)

    def test_directory_checks_allow_same_object_sibling_churn(self):
        checks = ("discovery", "lock", "acl", "secure")
        for kind in checks:
            with self.subTest(kind=kind):
                target = self.shared
                if self.noise.exists():
                    self.noise.unlink()
                initial = target.lstat()
                triggered = []
                if kind == "acl":
                    original = darwin_acl._read_once
                    def read(fd, lib, *, directory):
                        result = original(fd, lib, directory=directory)
                        if not triggered:
                            triggered.append(True)
                            self.make_noise()
                        return result
                    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        with mock.patch.object(darwin_acl, "_read_once", side_effect=read):
                            self.assertIsNone(darwin_acl.read_acl_snapshot(fd))
                    finally:
                        os.close(fd)
                elif kind == "secure":
                    original = secure_files._mac_fd_protection
                    def read(fd, *, directory):
                        result = original(fd, directory=directory)
                        if not triggered:
                            triggered.append(True)
                            self.make_noise()
                        return result
                    with mock.patch.object(secure_files, "_mac_fd_protection", side_effect=read):
                        self.assertTrue(secure_files.mac_private_protection(str(target), initial).secure)
                else:
                    module = discovery if kind == "discovery" else platform_lock
                    original = module.verify_no_extended_acl
                    def read(fd):
                        result = original(fd)
                        if not triggered:
                            triggered.append(True)
                            self.make_noise()
                        return result
                    with mock.patch.object(module, "verify_no_extended_acl", side_effect=read):
                        if kind == "discovery":
                            discovery._verify_directory(target, private=True)
                        else:
                            self.assertTrue(platform_lock.verify_local_access(target, "directory"))
                self.assertEqual(triggered, [True])
                self.assertNotEqual(initial.st_ctime_ns, target.stat().st_ctime_ns)

    def test_directory_acl_change_during_each_adapter_is_rejected(self):
        for kind in ("discovery", "lock", "acl", "secure"):
            with self.subTest(kind=kind):
                target = self.shared
                initial = target.lstat()
                triggered = []
                def change_acl():
                    result = subprocess.run(["/bin/chmod", "+a", "group:everyone deny delete", str(target)],
                                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self.assertEqual(result.returncode, 0, "cannot create fake ACL")
                    triggered.append(True)
                try:
                    if kind == "acl":
                        original = darwin_acl._read_once
                        def read(fd, lib, *, directory):
                            result = original(fd, lib, directory=directory)
                            if not triggered:
                                change_acl()
                            return result
                        fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                        try:
                            with mock.patch.object(darwin_acl, "_read_once", side_effect=read):
                                with self.assertRaises(darwin_acl.ACLVerificationError):
                                    darwin_acl.read_acl_snapshot(fd)
                        finally:
                            os.close(fd)
                    elif kind == "secure":
                        original = secure_files._mac_fd_protection
                        def read(fd, *, directory):
                            result = original(fd, directory=directory)
                            if not triggered:
                                change_acl()
                            return result
                        with mock.patch.object(secure_files, "_mac_fd_protection", side_effect=read):
                            with self.assertRaises(secure_files.SecureSnapshotError):
                                secure_files.mac_private_protection(str(target), initial)
                    else:
                        module = discovery if kind == "discovery" else platform_lock
                        original = module.verify_no_extended_acl
                        def read(fd):
                            result = original(fd)
                            if not triggered:
                                change_acl()
                            return result
                        with mock.patch.object(module, "verify_no_extended_acl", side_effect=read):
                            if kind == "discovery":
                                with self.assertRaises(discovery.PreflightError):
                                    discovery._verify_directory(target, private=True)
                            else:
                                self.assertFalse(platform_lock.verify_local_access(target, "directory"))
                    self.assertEqual(triggered, [True])
                finally:
                    subprocess.run(["/bin/chmod", "-N", str(target)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_directory_acl_allows_unrelated_subdirectory_creation(self):
        target = self.shared
        noise = target / "unrelated-directory"
        initial = target.lstat()
        original = darwin_acl._read_once
        triggered = []
        def read(fd, lib, *, directory):
            result = original(fd, lib, directory=directory)
            if not triggered:
                time.sleep(0.005)
                noise.mkdir(mode=0o700)
                triggered.append(True)
            return result
        fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with mock.patch.object(darwin_acl, "_read_once", side_effect=read):
                self.assertIsNone(darwin_acl.read_acl_snapshot(fd))
        finally:
            os.close(fd)
        self.assertEqual(triggered, [True])
        self.assertNotEqual(initial.st_ctime_ns, target.stat().st_ctime_ns)

    def test_directory_mode_and_flags_changes_during_each_adapter_are_rejected(self):
        if not hasattr(os, "chflags") or not hasattr(stat, "UF_IMMUTABLE"):
            self.skipTest("Darwin immutable flags unavailable")
        for kind in ("discovery", "lock", "acl", "secure"):
            for change in ("mode", "flags"):
                with self.subTest(kind=kind, change=change):
                    target = self.shared
                    initial = target.lstat()
                    triggered = []
                    def mutate():
                        if change == "mode":
                            os.chmod(target, 0o750)
                        else:
                            os.chflags(target, initial.st_flags | stat.UF_IMMUTABLE)
                        triggered.append(True)
                    try:
                        if kind == "acl":
                            original = darwin_acl._read_once
                            def read(fd, lib, *, directory):
                                result = original(fd, lib, directory=directory)
                                if not triggered:
                                    mutate()
                                return result
                            fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                            try:
                                with mock.patch.object(darwin_acl, "_read_once", side_effect=read):
                                    with self.assertRaises(darwin_acl.ACLVerificationError):
                                        darwin_acl.read_acl_snapshot(fd)
                            finally:
                                os.close(fd)
                        elif kind == "secure":
                            original = secure_files._mac_fd_protection
                            def read(fd, *, directory):
                                result = original(fd, directory=directory)
                                if not triggered:
                                    mutate()
                                return result
                            with mock.patch.object(secure_files, "_mac_fd_protection", side_effect=read):
                                with self.assertRaises(secure_files.SecureSnapshotError):
                                    secure_files.mac_private_protection(str(target), initial)
                        else:
                            module = discovery if kind == "discovery" else platform_lock
                            original = module.verify_no_extended_acl
                            def read(fd):
                                result = original(fd)
                                if not triggered:
                                    mutate()
                                return result
                            with mock.patch.object(module, "verify_no_extended_acl", side_effect=read):
                                if kind == "discovery":
                                    with self.assertRaises(discovery.PreflightError):
                                        discovery._verify_directory(target, private=True)
                                else:
                                    self.assertFalse(platform_lock.verify_local_access(target, "directory"))
                        self.assertEqual(triggered, [True])
                    finally:
                        if target.lstat().st_flags != initial.st_flags:
                            os.chflags(target, initial.st_flags)
                        if target.lstat().st_mode != initial.st_mode:
                            os.chmod(target, stat.S_IMODE(initial.st_mode))

    def test_open_directory_owner_and_group_change_are_rejected(self):
        # Changing uid is not permitted for an unprivileged test user. The
        # directory and FD are real; only the second fstat result is altered.
        from types import SimpleNamespace
        for field in ("st_uid", "st_gid"):
            with self.subTest(field=field):
                real_fstat = os.fstat
                target = self.shared.stat()
                target_id = (target.st_dev, target.st_ino)
                calls = []
                def changed(fd):
                    value = real_fstat(fd)
                    if (value.st_dev, value.st_ino) != target_id:
                        return value
                    calls.append(True)
                    if len(calls) == 2:
                        names = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid",
                                 "st_nlink", "st_flags")
                        fields = {name: getattr(value, name) for name in names}
                        fields[field] += 1
                        return SimpleNamespace(**fields)
                    return value
                with mock.patch.object(discovery.os, "fstat", side_effect=changed), \
                     mock.patch.object(discovery, "verify_no_extended_acl", return_value=None):
                    with self.assertRaises(discovery.PreflightError):
                        discovery._verify_directory(self.shared, private=True)
                self.assertGreaterEqual(len(calls), 2)

    def test_unlinked_directory_fd_is_rejected_by_each_adapter(self):
        for kind in ("acl", "secure", "discovery", "lock"):
            with self.subTest(kind=kind):
                target = self.shared / ("gone-" + kind)
                target.mkdir(mode=0o700)
                initial = target.lstat()
                triggered = []
                if kind == "acl":
                    original = darwin_acl._read_once
                    def read(fd, lib, *, directory):
                        result = original(fd, lib, directory=directory)
                        if not triggered:
                            target.rmdir()
                            triggered.append(True)
                        return result
                    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        with mock.patch.object(darwin_acl, "_read_once", side_effect=read):
                            with self.assertRaises(darwin_acl.ACLVerificationError):
                                darwin_acl.read_acl_snapshot(fd)
                    finally:
                        os.close(fd)
                elif kind == "secure":
                    original = secure_files._mac_fd_protection
                    def read(fd, *, directory):
                        result = original(fd, directory=directory)
                        if not triggered:
                            target.rmdir()
                            triggered.append(True)
                        return result
                    with mock.patch.object(secure_files, "_mac_fd_protection", side_effect=read):
                        with self.assertRaises(secure_files.SecureSnapshotError):
                            secure_files.mac_private_protection(str(target), initial)
                else:
                    module = discovery if kind == "discovery" else platform_lock
                    original = module.verify_no_extended_acl
                    def read(fd):
                        result = original(fd)
                        if not triggered:
                            target.rmdir()
                            triggered.append(True)
                        return result
                    with mock.patch.object(module, "verify_no_extended_acl", side_effect=read):
                        if kind == "discovery":
                            with self.assertRaises(discovery.PreflightError):
                                discovery._verify_directory(target, private=True)
                        else:
                            self.assertFalse(platform_lock.verify_local_access(target, "directory"))
                self.assertEqual(triggered, [True])
                self.assertFalse(target.exists())

    def test_renamed_directory_fd_is_rejected(self):
        target = self.shared / "renamed-before"
        replacement = self.shared / "renamed-after"
        target.mkdir(mode=0o700)
        original = darwin_acl._read_once
        triggered = []
        def read(fd, lib, *, directory):
            result = original(fd, lib, directory=directory)
            if not triggered:
                target.rename(replacement)
                triggered.append(True)
            return result
        fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with mock.patch.object(darwin_acl, "_read_once", side_effect=read):
                with self.assertRaises(darwin_acl.ACLVerificationError):
                    darwin_acl.read_acl_snapshot(fd)
        finally:
            os.close(fd)
        self.assertEqual(triggered, [True])
        self.assertFalse(target.exists())
        self.assertTrue(replacement.is_dir())

    def test_unavailable_fd_path_fails_closed(self):
        fd = os.open(self.shared, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with mock.patch("fcntl.fcntl", side_effect=OSError("fake path unavailable")):
                with self.assertRaises(darwin_acl.ACLVerificationError):
                    darwin_acl.read_acl_snapshot(fd)
        finally:
            os.close(fd)

    def test_regular_file_change_remains_rejected(self):
        target = self.runtime / "fake-file"
        target.write_text("FAKE-A", encoding="utf-8")
        target.chmod(0o600)
        original = darwin_acl._read_once
        triggered = []
        def read(fd, lib, *, directory):
            result = original(fd, lib, directory=directory)
            if not triggered:
                triggered.append(True)
                time.sleep(0.005)
                target.write_text("FAKE-B", encoding="utf-8")
            return result
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with mock.patch.object(darwin_acl, "_read_once", side_effect=read):
                with self.assertRaises(darwin_acl.ACLVerificationError):
                    darwin_acl.read_acl_snapshot(fd)
        finally:
            os.close(fd)
        self.assertEqual(triggered, [True])


if __name__ == "__main__":
    unittest.main()
