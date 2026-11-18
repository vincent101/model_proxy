"""Fake Win32 backend only: finite product writes and native commit calls."""
from __future__ import annotations

from dataclasses import replace
import os
import unittest
from unittest.mock import patch

import core.discovery as discovery
import core.platform_windows as win
import core.windows_acl as acl
import core.windows_secure_files as secure
from core.windows_paths import WindowsPathBackend
from core.windows_secure_native import CandidateError, CommitOutcomeUnknown, _CommitCalls, _CommitEvidence
from tests.test_windows_adapter import VirtualACL
from tests.test_windows_config_snapshot import ConfigFS
from tests.test_windows_discovery import PathWinBridge


class WritingFS(ConfigFS):
    def write_all_and_flush(self, handle, data):
        self.calls.append(("write_flush", handle, len(data)))
        self.handles[handle][1]["data"] = data
        self.handles[handle][1]["write"] = 1001
        self.handles[handle][1]["change"] = 2001


class NativeCandidateTests(unittest.TestCase):
    ROOT = r"C:\fixture\project"
    TARGET = ROOT + r"\client.json"
    TMP = ROOT + r"\candidate"

    def setUp(self):
        self.fs = WritingFS()
        self.fs.objects[self.TARGET] = dict(self.fs.obj(801), data=b"old-fake")
        self.acl = VirtualACL(self.fs)
        self.paths = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        for item in (
            patch.object(os, "name", "nt"),
            patch.object(secure, "_windows_path_backend", return_value=self.paths),
            patch.object(discovery, "_windows_path_backend", return_value=self.paths),
            patch.object(win, "_api", side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(acl, "_NativeBackend", return_value=self.acl),
        ):
            item.start(); self.addCleanup(item.stop)
        self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024)
        self.addCleanup(self.snapshot.close)


    def test_public_writer_uses_finite_backend_with_conflict_checks(self):
        from tests.test_windows_secure_finite import Backend
        backend = Backend()
        checks = []
        def preflight():
            checks.append(True)
            return True
        with patch("core.windows_secure_native.ProductWriteBackend", return_value=backend) as factory:
            result = secure.write_target(self.snapshot, b"new-fake", preflight=preflight)
        factory.assert_called_once_with(self.snapshot)
        self.assertEqual(result.state, "VERIFIED")
        self.assertEqual(result.backup_status, "VERIFIED")
        self.assertGreaterEqual(len(checks), 3)
        self.assertEqual(backend.events[-5:], ["release", "commit", "postcheck", "inspect-backup", "finish"])

    def test_public_writer_backend_unavailable_fails_closed(self):
        with patch("core.windows_secure_native.ProductWriteBackend",
                   side_effect=CandidateError("creation_policy_unverified", created=False)):
            with self.assertRaises(secure.SecureWriteError) as caught:
                secure.write_target(self.snapshot, b"new-fake", preflight=lambda: True)
        self.assertEqual(caught.exception.code, "creation_policy_unverified")
        self.assertEqual(caught.exception.state, "NOT_REPLACED")
        self.assertEqual(self.fs.objects[self.TARGET]["data"], b"old-fake")
        self.assertFalse(any(e[0] == "write_flush" for e in self.fs.calls))

    def test_public_writer_conflict_check_refuses_before_backup(self):
        from tests.test_windows_secure_finite import Backend
        backend = Backend()
        checks = iter((True, False))
        with patch("core.windows_secure_native.ProductWriteBackend", return_value=backend):
            with self.assertRaises(secure.SecureWriteError) as caught:
                secure.write_target(self.snapshot, b"new-fake", preflight=lambda: next(checks))
        self.assertEqual(caught.exception.code, "preflight_failed")
        self.assertEqual(caught.exception.state, "NOT_REPLACED")
        self.assertNotIn("create-backup", backend.events)

    def test_public_writer_reports_close_failure_as_unknown(self):
        from tests.test_windows_secure_finite import Backend
        backend = Backend(fail="finish")
        with patch("core.windows_secure_native.ProductWriteBackend", return_value=backend):
            with self.assertRaises(secure.SecureWriteError) as caught:
                secure.write_target(self.snapshot, b"new-fake", preflight=lambda: True)
        self.assertEqual(caught.exception.target_state, "UNKNOWN")
        self.assertEqual(caught.exception.state, "UNKNOWN")
        self.assertTrue(caught.exception.target_was_verified)
        self.assertTrue(caught.exception.release_unknown)
        self.assertEqual(caught.exception.backup_status, "UNKNOWN")

    def test_product_backend_real_orchestration_with_synthetic_os(self):
        # This exercises the product adapter with fake Win32 observations. It
        # does not establish any native Windows ACL/rename behavior.
        from core.windows_secure_native import ProductWriteBackend
        fs = self.fs
        next_index = iter((9001, 9002))
        def create(path, *, original_handle, user_sid):
            handle = fs.open_file(path, True)
            fs.handles[handle][1]["index"] = next(next_index)
            return handle
        fs.open_private_candidate = create
        fs.replace_existing = lambda target, source: fs.objects.__setitem__(target, fs.objects.pop(source))
        backend = ProductWriteBackend(self.snapshot, api=fs, acl_backend=self.acl)
        result = secure._finite_write(self.snapshot, b"new-fake", backend, lambda: True)
        self.assertEqual(result.state, "VERIFIED")
        self.assertEqual(fs.objects[self.TARGET]["data"], b"new-fake")
        self.assertEqual(fs.objects[result.backup]["data"], b"old-fake")
        self.assertFalse(fs.handles)

    def test_product_backend_new_file_uses_no_overwrite_move(self):
        from core.windows_secure_native import ProductWriteBackend
        self.snapshot.close()
        self.fs.objects.pop(self.TARGET)
        self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024)
        self.addCleanup(self.snapshot.close)
        fs = self.fs
        def create(path, *, original_handle, user_sid):
            self.assertIsNone(original_handle)
            handle = fs.open_file(path, True)
            fs.handles[handle][1]["index"] = 9001
            return handle
        def move(source, target):
            if target in fs.objects:
                raise OSError("no-overwrite")
            fs.objects[target] = fs.objects.pop(source)
        fs.open_private_candidate = create
        fs.move_new = move
        backend = ProductWriteBackend(self.snapshot, api=fs, acl_backend=self.acl)
        result = secure._finite_write(self.snapshot, b"new-fake", backend, lambda: True)
        self.assertEqual(result.state, "VERIFIED")
        self.assertEqual(result.backup_status, "NONE")
        self.assertEqual(fs.objects[self.TARGET]["data"], b"new-fake")
        self.assertFalse(fs.handles)

    def test_product_backend_allows_matching_inherited_private_acl(self):
        from core.windows_secure_native import ProductWriteBackend
        self.snapshot.close()
        self.fs.objects[self.TARGET]["control"] = 0x8004
        self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024)
        self.addCleanup(self.snapshot.close)
        fs = self.fs
        indices = iter((9001, 9002))
        def create(path, *, original_handle, user_sid):
            handle = fs.open_file(path, True)
            fs.handles[handle][1]["index"] = next(indices)
            fs.handles[handle][1]["control"] = 0x8004
            return handle
        fs.open_private_candidate = create
        fs.replace_existing = lambda target, source: fs.objects.__setitem__(target, fs.objects.pop(source))
        backend = ProductWriteBackend(self.snapshot, api=fs, acl_backend=self.acl)
        outcome = secure._finite_write(self.snapshot, b"new-fake", backend, lambda: True)
        self.assertEqual(outcome.state, "VERIFIED")
        self.assertEqual(fs.objects[self.TARGET]["control"], 0x8004)
        self.assertFalse(fs.handles)

    def test_product_backend_rejects_lost_protected_flag(self):
        from core.windows_secure_native import ProductWriteBackend
        fs = self.fs
        def create(path, *, original_handle, user_sid):
            handle = fs.open_file(path, True)
            fs.handles[handle][1]["index"] = 9001
            fs.handles[handle][1]["control"] = 0x8004
            return handle
        fs.open_private_candidate = create
        backend = ProductWriteBackend(self.snapshot, api=fs, acl_backend=self.acl)
        with self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b"new-fake", backend, lambda: True)
        self.assertEqual(caught.exception.code, "candidate_protection_unverified")
        self.assertEqual(caught.exception.target_state, "NOT_REPLACED")
        self.assertFalse(any(event[0] == "write_flush" for event in fs.calls))
        self.assertFalse(fs.handles)

    def test_product_backend_rejects_untrusted_candidate_before_content(self):
        from core.windows_secure_native import ProductWriteBackend
        fs = self.fs
        def create(path, *, original_handle, user_sid):
            handle = fs.open_file(path, True)
            fs.handles[handle][1]["index"] = 9001
            fs.handles[handle][1]["unsafe_group"] = True
            return handle
        fs.open_private_candidate = create
        backend = ProductWriteBackend(self.snapshot, api=fs, acl_backend=self.acl)
        with self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b"new-fake", backend, lambda: True)
        self.assertEqual(caught.exception.target_state, "NOT_REPLACED")
        self.assertFalse(caught.exception.creation_unknown)
        self.assertEqual(fs.objects[self.TARGET]["data"], b"old-fake")
        self.assertFalse(any(event[0] == "write_flush" for event in fs.calls))
        self.assertFalse(fs.handles)

    def test_product_partial_backup_write_never_commits_or_retries(self):
        from core.windows_secure_native import ProductWriteBackend
        fs = self.fs
        def create(path, *, original_handle, user_sid):
            handle = fs.open_file(path, True)
            fs.handles[handle][1]["index"] = 9001
            return handle
        def partial(handle, data):
            fs.handles[handle][1]["data"] = data[:2]
            raise OSError("fixture partial write")
        fs.open_private_candidate = create
        fs.write_all_and_flush = partial
        fs.replace_existing = lambda *args: self.fail("must not commit")
        backend = ProductWriteBackend(self.snapshot, api=fs, acl_backend=self.acl)
        with self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b"new-fake", backend, lambda: True)
        error = caught.exception
        self.assertEqual(error.target_state, "NOT_REPLACED")
        self.assertEqual(error.backup_status, "UNVERIFIED")
        self.assertFalse(error.creation_unknown)
        self.assertEqual(fs.objects[error.backup]["data"], b"ol")
        self.assertEqual(fs.objects[self.TARGET]["data"], b"old-fake")
        self.assertFalse(fs.handles)

    def test_product_detects_stage_change_before_commit(self):
        from core.windows_secure_native import ProductWriteBackend
        fs = self.fs
        indices = iter((9001, 9002))
        def create(path, *, original_handle, user_sid):
            handle = fs.open_file(path, True)
            fs.handles[handle][1]["index"] = next(indices)
            return handle
        fs.open_private_candidate = create
        fs.replace_existing = lambda *args: self.fail("must not commit")
        backend = ProductWriteBackend(self.snapshot, api=fs, acl_backend=self.acl)
        inspect = backend.inspect
        stage_reads = 0
        def changed(path):
            nonlocal stage_reads
            value = inspect(path)
            if "-stage-" in path:
                stage_reads += 1
                if stage_reads == 2:
                    return replace(value, data=b"changed-fake")
            return value
        backend.inspect = changed
        with self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b"new-fake", backend, lambda: True)
        self.assertEqual(caught.exception.code, "stage_changed")
        self.assertEqual(caught.exception.target_state, "NOT_REPLACED")
        self.assertEqual(caught.exception.backup_status, "VERIFIED")
        self.assertEqual(fs.objects[self.TARGET]["data"], b"old-fake")
        self.assertFalse(fs.handles)

    def test_product_new_target_created_by_other_writer_is_not_overwritten(self):
        from core.windows_secure_native import ProductWriteBackend
        self.snapshot.close()
        self.fs.objects.pop(self.TARGET)
        self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024)
        self.addCleanup(self.snapshot.close)
        fs = self.fs
        def create(path, *, original_handle, user_sid):
            handle = fs.open_file(path, True)
            fs.handles[handle][1]["index"] = 9001
            return handle
        def move(source, target):
            fs.objects[target] = dict(fs.obj(9003), data=b"other-fake")
            raise OSError("fixture target exists")
        fs.open_private_candidate = create
        fs.move_new = move
        backend = ProductWriteBackend(self.snapshot, api=fs, acl_backend=self.acl)
        with self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b"new-fake", backend, lambda: True)
        self.assertEqual(caught.exception.state, "UNKNOWN")
        self.assertEqual(caught.exception.backup_status, "NONE")
        self.assertEqual(fs.objects[self.TARGET]["data"], b"other-fake")
        self.assertFalse(fs.handles)

    def test_commit_call_adapter_argument_order_and_uncertain_failure(self):
        class FakeCalls:
            def __init__(self): self.events = []
            def replace_existing(self, target, source): self.events.append(("replace", target, source))
            def move_new(self, source, target): self.events.append(("move", source, target))
        api = FakeCalls()
        raw = _CommitCalls(api=api)
        volume = (3, "NTFS", 17, r"\Device\HarddiskVolume1")
        existing = _CommitEvidence(self.TARGET, self.TMP, volume, True)
        missing = _CommitEvidence(self.TARGET, self.TMP, volume, False)
        with self.assertRaises(CandidateError):
            raw.replace(self.TARGET, self.TMP)
        raw.replace(self.TARGET, self.TMP, existing)
        raw.move_new(self.TARGET, self.TMP, missing)
        self.assertEqual(api.events, [("replace", self.TARGET, self.TMP),
                                      ("move", self.TMP, self.TARGET)])
        with self.assertRaises(CandidateError):
            raw.replace(self.TARGET, r"D:\other\temp", existing)
        self.assertEqual(len(api.events), 2)
        with patch.object(api, "replace_existing", side_effect=OSError("may have changed")):
            with self.assertRaises(CommitOutcomeUnknown):
                raw.replace(self.TARGET, self.TMP, existing)
        with patch.object(api, "move_new", side_effect=OSError("may have changed")):
            with self.assertRaises(CommitOutcomeUnknown):
                raw.move_new(self.TARGET, self.TMP, missing)

    def test_commit_adapter_rejects_unknown_or_invalid_volume_evidence(self):
        class FakeCalls:
            def __init__(self): self.events = []
            def replace_existing(self, *args): self.events.append(args)
            def move_new(self, *args): self.events.append(args)
        api = FakeCalls()
        raw = _CommitCalls(api=api)
        for volume in ((None, None, None, None), (True, 'NTFS', 17, r'\Device\HarddiskVolume1'),
                       (3, 'FAT32', 17, r'\Device\HarddiskVolume1'),
                       (3, 'NTFS', None, r'\Device\HarddiskVolume1'),
                       (3, 'NTFS', -1, r'\Device\HarddiskVolume1'),
                       (3, 'NTFS', 0x100000000, r'\Device\HarddiskVolume1'),
                       (3, 'NTFS', 17, None), (3, 'NTFS', 17, 'C:'),
                       (3, 'NTFS', 17, r'\Device\Unknown')):
            for exists in (True, False):
                with self.subTest(volume=volume, exists=exists):
                    evidence = _CommitEvidence(self.TARGET, self.TMP, volume, exists)
                    with self.assertRaises(CandidateError):
                        (raw.replace if exists else raw.move_new)(self.TARGET, self.TMP, evidence)
        self.assertEqual(api.events, [])

    def test_sidecar_and_ledger_no_backup_close_failure_cannot_publish(self):
        from tests.test_windows_secure_finite import Backend
        for role in ("sidecar", "ledger"):
            with self.subTest(role=role):
                backend = Backend(fail="finish")
                with patch("core.windows_secure_native.ProductWriteBackend", return_value=backend):
                    with self.assertRaises(secure.SecureWriteError) as caught:
                        secure.write_target(self.snapshot, b"new-fake", preflight=lambda: True,
                                            make_backup=False)
                error = caught.exception
                self.assertEqual((error.state, error.target_state), ("UNKNOWN", "UNKNOWN"))
                self.assertTrue(error.target_was_verified)
                self.assertTrue(error.release_unknown)
                self.assertEqual(error.backup_status, "NONE")
                self.assertEqual(backend.events.count("commit"), 1)

    def test_backup_failure_after_target_observation_is_not_success(self):
        from types import SimpleNamespace
        from tests.test_windows_secure_finite import Backend, observed
        for mode in ("CHANGED", "UNKNOWN"):
            with self.subTest(mode=mode):
                backend = Backend()
                inspect = backend.inspect
                def inspect_backup(path):
                    if path == "backup" and "commit" in backend.events:
                        if mode == "UNKNOWN":
                            raise RuntimeError("synthetic backup observation failure")
                        return observed(b"changed-fake", "backup")
                    return inspect(path)
                backend.inspect = inspect_backup
                with self.assertRaises(secure.SecureWriteError) as caught:
                    secure._finite_write(SimpleNamespace(exists=True, contents=b"old-fake"),
                                         b"new-fake", backend, lambda: True)
                error = caught.exception
                self.assertEqual((error.state, error.target_state),
                                 ("UNKNOWN", "UNKNOWN"))
                self.assertTrue(error.target_was_verified)
                self.assertEqual(error.backup_status, mode)


class SidecarFailureReceiptTests(unittest.TestCase):
    def test_sidecar_does_not_publish_after_writer_close_failure(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from core.commands import SessionOverridesSidecar, SidecarWriteError
        from tests.test_route_sidecar_unknown import FakeSecureIO
        from tests.test_windows_secure_finite import Backend

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic-sidecar.json"
            io = FakeSecureIO()
            store = SessionOverridesSidecar(path, secure_io=io)
            store.apply_command("fake-client", "fake-session", "set", "B")
            commit = io.commit

            def close_failure(path, baseline, content):
                commit(path, baseline, content)
                secure._finite_write(SimpleNamespace(exists=False, contents=None), b"new-fake",
                                     Backend(exists=False, fail="finish"), lambda: True,
                                     make_backup=False)

            io.commit = close_failure
            with self.assertRaises(SidecarWriteError) as caught:
                store.apply_command("fake-client", "fake-session", "set", "C")
            self.assertEqual(caught.exception.state, "UNKNOWN")
            self.assertEqual(store.get_overrides_for("fake-client"), {"fake-session": "B"})
            self.assertIsNotNone(store._unknown)


if __name__ == "__main__":
    unittest.main()
