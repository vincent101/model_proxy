"""Regression tests with synthetic objects only; not native Windows evidence."""
import os
import traceback
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from core import discovery, platform_windows as win, windows_acl as acl
from core import windows_secure_files as secure
from core.windows_paths import WindowsPathBackend
from tests.test_windows_acl import raw_ace
from tests.test_windows_adapter import VirtualACL
from tests.test_windows_discovery import PathWinBridge
from tests.test_windows_same_volume_commit import MultiVolumeFS
from tests.test_windows_secure_finite import Backend, observed


FAKE_KEY = "FAKE_KEY_NEVER_PRINT_13579"


class PoisonedException(RuntimeError):
    """Fake untrusted exception whose metadata accessor raises a secret."""
    def __init__(self, poisoned, error_type=RuntimeError):
        super().__init__(FAKE_KEY)
        self.poisoned = poisoned
        self.error_type = error_type

    def __getattribute__(self, name):
        if name in ("release_unknown", "code", "created"):
            poisoned = object.__getattribute__(self, "poisoned")
            if name == poisoned:
                error_type = object.__getattribute__(self, "error_type")
                raise error_type(FAKE_KEY)
            if name == "release_unknown":
                return False
            if name == "code":
                return "stage_changed"
            return None
        return super().__getattribute__(name)


class PropertyPoison(RuntimeError):
    @property
    def code(self):
        # A real property raising AttributeError must not be mistaken for an
        # absent attribute and quietly discard the unknown condition.
        raise AttributeError(FAKE_KEY)


class HostileValue:
    def __bool__(self):
        raise AssertionError(FAKE_KEY)
    def __eq__(self, other):
        raise AssertionError(FAKE_KEY)
    def __hash__(self):
        raise AssertionError(FAKE_KEY)


class FiniteReviewTests(unittest.TestCase):
    def assert_sanitized(self, error):
        self.assertNotIn(FAKE_KEY, str(error))
        self.assertNotIn(FAKE_KEY, "".join(traceback.format_exception(error)))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_ordinary_missing_fields_are_not_release_unknown(self):
        for error, expected in ((RuntimeError(FAKE_KEY), None),
                                (OSError(FAKE_KEY), None),
                                (acl.ACLVerificationError("access_unverified"), "access_unverified")):
            with self.subTest(error=type(error).__name__):
                code, unknown, created = secure._write_exception_info(error, include_created=True)
                self.assertEqual(code, expected)
                self.assertFalse(unknown)
                self.assertIsNone(created)

    def test_static_false_metadata_with_dynamic_accessors_is_unobservable(self):
        for hook in ("__getattribute__", "__getattr__"):
            with self.subTest(hook=hook):
                accesses = []
                def hostile(self, name):
                    if hook == "__getattr__" or name in ("release_unknown", "created", "code", "owned_handle"):
                        accesses.append(name)
                        raise KeyboardInterrupt(FAKE_KEY)
                    return super(type(self), self).__getattribute__(name)
                error_type = type("MaskedError", (RuntimeError,), {hook: hostile})
                error = error_type(FAKE_KEY)
                error.release_unknown = False
                error.created = False
                error.code = "creation_policy_unverified"
                error.owned_handle = None
                code, unknown, created = secure._write_exception_info(error, include_created=True)
                self.assertIsNone(code)
                self.assertTrue(unknown)
                self.assertIsNone(created)
                self.assertEqual(accesses, [])

    def test_ordinary_exception_static_false_metadata_remains_observable(self):
        error = RuntimeError(FAKE_KEY)
        error.release_unknown = False
        error.created = False
        error.code = "creation_policy_unverified"
        error.owned_handle = None
        code, unknown, created = secure._write_exception_info(error, include_created=True)
        self.assertEqual(code, "creation_policy_unverified")
        self.assertFalse(unknown)
        self.assertIs(created, False)

    def test_dynamic_getattr_is_never_executed_for_missing_fields(self):
        accesses = []
        class DynamicFailure(RuntimeError):
            def __getattr__(self, name):
                accesses.append(name)
                raise AssertionError(FAKE_KEY)
        backend = Backend()
        def fail():
            raise DynamicFailure(FAKE_KEY)
        with self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(SimpleNamespace(exists=False, contents=None), b"new", backend, fail)
        self.assertEqual(accesses, [])
        self.assertTrue(caught.exception.release_unknown)
        self.assertEqual(backend.events.count("finish"), 1)
        self.assert_sanitized(caught.exception)

    def test_malicious_exception_metadata_always_finishes_and_redacts(self):
        for phase in ("preflight", "artifact", "revalidate", "create", "backup_recheck"):
            for field in ("release_unknown", "code", "created"):
                if field == "created" and phase != "create":
                    continue
                for exception_type in (RuntimeError, KeyboardInterrupt, AttributeError):
                    with self.subTest(phase=phase, field=field, error=exception_type.__name__):
                        backend = Backend()
                        def fail(*args, **kwargs):
                            raise PoisonedException(field, exception_type)
                        preflight = fail if phase == "preflight" else lambda: True
                        artifact = fail if phase == "artifact" else None
                        if phase == "revalidate":
                            backend.revalidate = fail
                        if phase == "create":
                            backend.create_and_write = fail
                        if phase == "backup_recheck":
                            inspect = backend.inspect
                            def inspect_after_commit(path):
                                if path == "backup" and "commit" in backend.events:
                                    fail()
                                return inspect(path)
                            backend.inspect = inspect_after_commit
                        with self.assertRaises(secure.SecureWriteError) as caught:
                            secure._finite_write(SimpleNamespace(exists=True, contents=b"old"), b"new",
                                                 backend, preflight, artifact_preflight=artifact)
                        error = caught.exception
                        self.assertEqual(error.code, "release_unknown")
                        self.assertTrue(error.release_unknown)
                        self.assertEqual(backend.events.count("finish"), 1)
                        self.assert_sanitized(error)
                        if phase == "create":
                            self.assertTrue(error.creation_unknown)
                        if phase == "backup_recheck":
                            self.assertEqual(error.state, "UNKNOWN")
                            self.assertTrue(error.target_was_verified)
                            self.assertEqual(error.backup_status, "UNKNOWN")
                        else:
                            self.assertEqual(error.state, "NOT_REPLACED")
                            self.assertFalse(error.target_was_verified)
                            self.assertNotIn("commit", backend.events)

    def test_exception_attributeerror_property_and_hostile_values_are_unknown(self):
        cases = [(PropertyPoison(FAKE_KEY), "preflight")]
        for name in ("code", "release_unknown", "created"):
            exc = RuntimeError(FAKE_KEY)
            setattr(exc, name, HostileValue())
            cases.append((exc, "create" if name == "created" else "preflight"))
        for exc, phase in cases:
            with self.subTest(phase=phase):
                backend = Backend()
                def fail(*args):
                    raise exc
                if phase == "create":
                    backend.create_and_write = fail
                with self.assertRaises(secure.SecureWriteError) as caught:
                    secure._finite_write(SimpleNamespace(exists=False, contents=None), b"new", backend,
                                         fail if phase == "preflight" else lambda: True)
                self.assertEqual(caught.exception.code, "release_unknown")
                self.assertTrue(caught.exception.release_unknown)
                self.assertEqual(backend.events.count("finish"), 1)
                self.assert_sanitized(caught.exception)

    def test_exception_attributes_are_never_executed_and_finish_runs(self):
        for phase in ("preflight", "artifact", "create", "backup_recheck"):
            with self.subTest(phase=phase):
                backend = Backend()
                reads = []
                class BlockingFailure(RuntimeError):
                    def __getattribute__(self, name):
                        if name in ("release_unknown", "code", "created"):
                            reads.append(name)
                            raise AssertionError(FAKE_KEY)  # a real accessor could block indefinitely
                        return super().__getattribute__(name)
                def fail(*args):
                    raise BlockingFailure(FAKE_KEY)
                preflight = fail if phase == "preflight" else lambda: True
                artifact = fail if phase == "artifact" else None
                if phase == "create":
                    backend.create_and_write = fail
                if phase == "backup_recheck":
                    inspect = backend.inspect
                    def inspect_backup(path):
                        if path == "backup" and "commit" in backend.events:
                            fail()
                        return inspect(path)
                    backend.inspect = inspect_backup
                with self.assertRaises(secure.SecureWriteError) as caught:
                    secure._finite_write(SimpleNamespace(exists=True, contents=b"old"), b"new",
                                         backend, preflight, artifact_preflight=artifact)
                self.assertEqual(reads, [])
                self.assertEqual(caught.exception.code, "release_unknown")
                self.assertEqual(backend.events.count("finish"), 1)
                self.assert_sanitized(caught.exception)

    def test_unexpected_exception_handler_failure_still_finishes(self):
        backend = Backend(fail="revalidate")
        with patch.object(secure, "_write_exception_info", side_effect=RuntimeError(FAKE_KEY)):
            with self.assertRaises(secure.SecureWriteError) as caught:
                secure._finite_write(SimpleNamespace(exists=False, contents=None), b"new", backend, lambda: True)
        self.assertEqual(caught.exception.code, "release_unknown")
        self.assertTrue(caught.exception.release_unknown)
        self.assertEqual(backend.events.count("finish"), 1)
        self.assert_sanitized(caught.exception)

    def test_active_caller_exception_is_not_attached_to_public_error(self):
        backend = Backend(fail="revalidate")
        try:
            raise ValueError(FAKE_KEY)
        except ValueError:
            with self.assertRaises(secure.SecureWriteError) as caught:
                secure._finite_write(SimpleNamespace(exists=False, contents=None), b"new", backend, lambda: True)
        self.assertEqual(backend.events.count("finish"), 1)
        self.assert_sanitized(caught.exception)

    def test_error_codes_are_whitelisted_and_finish_cannot_leak(self):
        for code, expected in ((FAKE_KEY, "write_unverified"), ("stage_changed", "stage_changed")):
            backend = Backend()
            def fail():
                error = RuntimeError(FAKE_KEY)
                error.code = code
                raise error
            with self.assertRaises(secure.SecureWriteError) as caught:
                secure._finite_write(SimpleNamespace(exists=False, contents=None), b"new", backend, fail)
            self.assertEqual(caught.exception.code, expected)
            self.assertFalse(caught.exception.release_unknown)
            self.assertEqual(backend.events.count("finish"), 1)
            self.assert_sanitized(caught.exception)
        backend = Backend()
        def finish():
            backend.step("finish")
            raise PoisonedException("code")
        backend.finish = finish
        with self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(SimpleNamespace(exists=False, contents=None), b"new", backend, lambda: True)
        self.assertEqual(caught.exception.code, "release_unknown")
        self.assertTrue(caught.exception.target_was_verified)
        self.assertEqual(backend.events.count("finish"), 1)
        self.assert_sanitized(caught.exception)

    def test_artifact_release_uncertainty_all_phases_is_sanitized(self):
        for failure_call in range(1, 5):
            for mode in ("code", "attribute"):
                with self.subTest(failure_call=failure_call, mode=mode):
                    backend = Backend()
                    calls = []
                    def check(role, path):
                        calls.append((role, path))
                        if len(calls) == failure_call:
                            exc = RuntimeError("sensitive fake exception content")
                            if mode == "code":
                                exc.code = "handle_close_failed"
                            else:
                                exc.release_unknown = True
                            raise exc
                        return True
                    with self.assertRaises(secure.SecureWriteError) as caught:
                        secure._finite_write(SimpleNamespace(exists=True, contents=b"old"), b"new",
                                             backend, lambda: True, artifact_preflight=check)
                    error = caught.exception
                    self.assertEqual(error.code, "release_unknown")
                    self.assertTrue(error.release_unknown)
                    self.assertEqual(error.target_state, "NOT_REPLACED")
                    self.assertFalse(error.target_was_verified)
                    self.assertNotIn("commit", backend.events)
                    self.assertEqual(backend.events.count("finish"), 1)
                    self.assertNotIn("sensitive", "".join(traceback.format_exception(error)))
                    self.assertFalse(error.creation_unknown)
                    if failure_call == 1:
                        self.assertNotIn("create-backup", backend.events)
                    if failure_call == 2:
                        self.assertNotIn("create-stage", backend.events)

    def test_ordinary_callback_failures_stay_sanitized_without_release_signal(self):
        for outcome in (False, None, RuntimeError("private data")):
            with self.subTest(outcome=type(outcome)):
                backend = Backend()
                def check(*args):
                    if isinstance(outcome, BaseException):
                        raise outcome
                    return outcome
                with self.assertRaises(secure.SecureWriteError) as caught:
                    secure._finite_write(SimpleNamespace(exists=False, contents=None), b"new", backend,
                                         lambda: True, artifact_preflight=check)
                self.assertEqual(caught.exception.code, "artifact_preflight_failed")
                self.assertFalse(caught.exception.release_unknown)
                self.assertNotIn("private data", "".join(traceback.format_exception(caught.exception)))
                self.assertNotIn("create-stage", backend.events)

    def test_backup_recheck_failure_keeps_historical_target_evidence(self):
        for mode in ("changed", "unavailable", "release"):
            with self.subTest(mode=mode):
                backend = Backend()
                inspect = backend.inspect
                def failing(path):
                    if path == "backup" and "commit" in backend.events:
                        if mode == "changed":
                            return observed(b"changed", "backup")
                        error = RuntimeError("private backup details")
                        if mode == "release":
                            error.code = "close_failed"
                        raise error
                    return inspect(path)
                backend.inspect = failing
                with self.assertRaises(secure.SecureWriteError) as caught:
                    secure._finite_write(SimpleNamespace(exists=True, contents=b"old"), b"new",
                                         backend, lambda: True)
                error = caught.exception
                self.assertEqual((error.state, error.target_state), ("UNKNOWN", "UNKNOWN"))
                self.assertTrue(error.target_was_verified)
                self.assertTrue(error.backup_was_verified)
                self.assertEqual(error.backup_status, "CHANGED" if mode == "changed" else "UNKNOWN")
                self.assertEqual(error.code, {"changed": "backup_changed", "unavailable": "backup_unverified",
                                              "release": "release_unknown"}[mode])
                self.assertEqual(error.release_unknown, mode == "release")
                self.assertEqual(backend.events.count("commit"), 1)
                self.assertEqual(backend.events.count("postcheck"), 1)
                self.assertEqual(backend.events.count("finish"), 1)

    def test_actual_target_postcheck_failure_is_still_reported(self):
        backend = Backend(fail="postcheck")
        with self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(SimpleNamespace(exists=True, contents=b"old"), b"new", backend, lambda: True)
        self.assertEqual(caught.exception.target_state, "POSTCHECK_FAILED")
        self.assertFalse(caught.exception.target_was_verified)


class PublicReadACL(VirtualACL):
    """Grant fake Builtin Users read/traverse, never creation, on selected dirs."""
    def __init__(self, api, public_dirs):
        super().__init__(api)
        self.public_dirs = public_dirs

    def read_observation(self, handle, kind, required):
        result = super().read_observation(handle, kind, required)
        path, _ = self.api.handles[handle]
        if path in self.public_dirs:
            ace = acl._parse_ace(raw_ace(acl._BUILTIN_USERS, mask=1))
            result = replace(result, aces=result.aces + (ace,))
        return result


class TargetParentStageTests(unittest.TestCase):
    ROOT = r"C:\fixture\project"
    TARGET = ROOT + r"\target.json"
    RUNTIME = r"C:\fixture\runtime"
    BACKUP = r"D:\backups"

    def setUp(self):
        self.fs = MultiVolumeFS()
        self.fs.objects[self.TARGET] = dict(self.fs.obj(801), data=b"old")
        self.acl = PublicReadACL(self.fs, {self.ROOT, self.RUNTIME, self.BACKUP})
        paths = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        for item in (
            patch.object(os, "name", "nt"),
            patch.object(secure, "_windows_path_backend", return_value=paths),
            patch.object(discovery, "_windows_path_backend", return_value=paths),
            patch.object(win, "_api", side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(acl, "_NativeBackend", return_value=self.acl),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024,
                                              trusted_boundary=self.ROOT)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.fs.close_fail.clear()
        self.snapshot.close()
        for handle in list(self.fs.handles):
            self.fs.close(handle)

    def write(self, **kwargs):
        return secure.write_target(self.snapshot, b"new", preflight=lambda: True,
                                   artifact_preflight=lambda role, path: True,
                                   make_backup=False, **kwargs)

    def test_product_writer_closes_candidate_before_untrusted_metadata(self):
        for release_flag in (True, False):
            with self.subTest(release_flag=release_flag):
                reads, candidates = [], []
                class PoisonedCode(RuntimeError):
                    @property
                    def code(self):
                        reads.append("code")
                        raise RuntimeError(FAKE_KEY)  # also models an accessor that would block
                original_create = self.fs.open_private_candidate
                def create(path, **kwargs):
                    handle = original_create(path, **kwargs)
                    candidates.append(handle)
                    return handle
                def fail_write(handle, data):
                    error = PoisonedCode(FAKE_KEY)
                    error.release_unknown = release_flag
                    raise error
                self.fs.open_private_candidate = create
                self.fs.write_all_and_flush = fail_write
                with self.assertRaises(secure.SecureWriteError) as caught:
                    secure.write_target(self.snapshot, b"new", preflight=lambda: True, make_backup=False,
                                        stage_directory=self.ROOT, stage_boundary=self.ROOT,
                                        stage_at_target_parent=True, artifact_preflight=lambda *_: True)
                error = caught.exception
                self.assertEqual(error.code, "release_unknown")
                self.assertTrue(error.release_unknown)
                self.assertEqual(error.state, "NOT_REPLACED")
                self.assertFalse(error.creation_unknown)
                self.assertEqual(len(candidates), 1)
                self.assertEqual(self.fs.close_calls.count(candidates[0]), 1)
                self.assertEqual(reads, [])
                self.assertFalse(self.fs.handles)
                self.assertFalse(self.fs.commits)
                self.assertEqual(self.fs.objects[self.TARGET]["data"], b"old")
                self.assertIsNone(error.__context__)
                self.assertNotIn(FAKE_KEY, "".join(traceback.format_exception(error)))
                self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024,
                                                      trusted_boundary=self.ROOT)
                self.fs.open_private_candidate = original_create

    def test_untransferred_created_candidate_and_masked_false_are_unknown(self):
        accesses, leaked = [], []
        class MaskedError(RuntimeError):
            def __getattribute__(self, name):
                if name in ("release_unknown", "created", "code", "owned_handle"):
                    accesses.append(name)
                    raise KeyboardInterrupt(FAKE_KEY)
                return super().__getattribute__(name)
        create = self.fs.open_private_candidate
        def create_then_interrupt(path, **kwargs):
            handle = create(path, **kwargs)  # fake candidate exists, handle not transferred
            leaked.append((path, handle))
            error = MaskedError(FAKE_KEY)
            error.release_unknown = False
            error.created = False
            error.code = "creation_policy_unverified"
            error.owned_handle = None
            raise error
        self.fs.open_private_candidate = create_then_interrupt
        with self.assertRaises(secure.SecureWriteError) as caught:
            secure.write_target(self.snapshot, b"new", preflight=lambda: True, make_backup=False,
                                stage_directory=self.ROOT, stage_boundary=self.ROOT,
                                stage_at_target_parent=True, artifact_preflight=lambda *_: True)
        error = caught.exception
        self.assertEqual(error.code, "release_unknown")
        self.assertTrue(error.release_unknown)
        self.assertTrue(error.creation_unknown)
        self.assertEqual(error.target_state, "NOT_REPLACED")
        self.assertEqual(accesses, [])
        self.assertEqual(len(leaked), 1)
        path, handle = leaked[0]
        self.assertIn(path, self.fs.objects)
        self.assertIn(handle, self.fs.handles)
        self.assertEqual(self.fs.close_calls.count(handle), 0)  # not a transferred handle
        self.assertFalse(self.fs.commits)
        self.assertEqual(self.fs.objects[self.TARGET]["data"], b"old")
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        self.assertNotIn(FAKE_KEY, "".join(traceback.format_exception(error)))
        # The fixture's cleanup owns and closes this synthetic handle only.

    def test_candidate_closed_before_native_exception_inspection(self):
        from core import windows_secure_native as native
        candidates, order = [], []
        original_create = self.fs.open_private_candidate
        original_info = native._write_exception_info
        class OriginalFailure(RuntimeError):
            pass
        def create(path, **kwargs):
            handle = original_create(path, **kwargs)
            candidates.append(handle)
            return handle
        def fail_write(handle, data):
            error = OriginalFailure(FAKE_KEY)
            error.release_unknown = True
            error.code = "stage_changed"
            raise error
        def inspect_error(exc, **kwargs):
            if type(exc) is OriginalFailure:
                order.append(self.fs.close_calls.count(candidates[0]))
            return original_info(exc, **kwargs)
        self.fs.open_private_candidate = create
        self.fs.write_all_and_flush = fail_write
        with patch.object(native, "_write_exception_info", side_effect=inspect_error):
            with self.assertRaises(secure.SecureWriteError) as caught:
                secure.write_target(self.snapshot, b"new", preflight=lambda: True, make_backup=False,
                                    stage_directory=self.ROOT, stage_boundary=self.ROOT,
                                    stage_at_target_parent=True, artifact_preflight=lambda *_: True)
        self.assertTrue(order)
        self.assertTrue(all(closed == 1 for closed in order), order)
        self.assertTrue(caught.exception.release_unknown)
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.fs.commits)
        self.assertNotIn(FAKE_KEY, "".join(traceback.format_exception(caught.exception)))

    def test_product_backend_preserves_known_candidate_observations(self):
        from core.windows_secure_native import CandidateError, ProductWriteBackend
        candidate = object()
        backend = ProductWriteBackend(self.snapshot, api=self.fs, acl_backend=self.acl)
        path = backend.reserve("stage")
        def fail_open(*args, **kwargs):
            raise CandidateError("creation_policy_unverified", created=False,
                                 candidate=candidate, content_may_exist=True)
        self.fs.open_private_candidate = fail_open
        try:
            with self.assertRaises(CandidateError) as caught:
                backend.create_and_write(path, b"new")
        finally:
            backend.finish()
        self.assertEqual(caught.exception.code, "creation_policy_unverified")
        self.assertFalse(caught.exception.created)
        self.assertFalse(caught.exception.release_unknown)
        self.assertIs(caught.exception.candidate, candidate)
        self.assertTrue(caught.exception.content_may_exist)
        self.assertFalse(self.fs.handles)

    def test_candidate_close_failure_merges_original_unknown_without_retry(self):
        candidates = []
        class PoisonedCode(RuntimeError):
            @property
            def code(self):
                raise AssertionError(FAKE_KEY)
        original_create = self.fs.open_private_candidate
        def create(path, **kwargs):
            handle = original_create(path, **kwargs)
            candidates.append(handle)
            self.fs.close_fail.add(handle)
            return handle
        def fail_write(handle, data):
            error = PoisonedCode(FAKE_KEY)
            error.release_unknown = True
            raise error
        self.fs.open_private_candidate = create
        self.fs.write_all_and_flush = fail_write
        with self.assertRaises(secure.SecureWriteError) as caught:
            secure.write_target(self.snapshot, b"new", preflight=lambda: True, make_backup=False,
                                stage_directory=self.ROOT, stage_boundary=self.ROOT,
                                stage_at_target_parent=True, artifact_preflight=lambda *_: True)
        self.assertEqual(caught.exception.code, "release_unknown")
        self.assertTrue(caught.exception.release_unknown)
        self.assertEqual(self.fs.close_calls.count(candidates[0]), 1)
        self.assertFalse(self.fs.commits)
        self.assertNotIn(FAKE_KEY, "".join(traceback.format_exception(caught.exception)))

    def test_poisoned_artifact_preflight_closes_fake_product_handles(self):
        for failure_call in (1, 2):
            with self.subTest(failure_call=failure_call):
                calls = []
                def artifact(role, path):
                    calls.append((role, path))
                    if len(calls) == failure_call:
                        raise PoisonedException("code")
                    return True
                with self.assertRaises(secure.SecureWriteError) as caught:
                    secure.write_target(self.snapshot, b"new", preflight=lambda: True, make_backup=False,
                                        stage_directory=self.ROOT, stage_boundary=self.ROOT,
                                        stage_at_target_parent=True, artifact_preflight=artifact)
                self.assertEqual(caught.exception.code, "release_unknown")
                self.assertTrue(caught.exception.release_unknown)
                self.assertEqual(caught.exception.state, "NOT_REPLACED")
                self.assertFalse(self.fs.handles)
                self.assertFalse(self.fs.commits)
                self.assertEqual(self.fs.objects[self.TARGET]["data"], b"old")
                self.assertIsNone(caught.exception.__context__)
                self.assertNotIn(FAKE_KEY, "".join(traceback.format_exception(caught.exception)))
                self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024,
                                                      trusted_boundary=self.ROOT)

    def test_initial_preflight_release_codes_redacted_without_constructing_backend(self):
        for code in ("release_unknown", "close_failed", "handle_close_failed", "resource_release_failed"):
            with self.subTest(code=code):
                def fail():
                    error = RuntimeError(FAKE_KEY)
                    error.code = code
                    raise error
                with patch("core.windows_secure_native.ProductWriteBackend") as factory:
                    with self.assertRaises(secure.SecureWriteError) as caught:
                        secure.write_target(self.snapshot, b"new", preflight=fail)
                factory.assert_not_called()
                self.assertEqual(caught.exception.code, "release_unknown")
                self.assertTrue(caught.exception.release_unknown)
                self.assertEqual(caught.exception.state, "NOT_REPLACED")
                self.assertIsNone(caught.exception.__context__)
                self.assertIsNone(caught.exception.__cause__)
                self.assertNotIn(FAKE_KEY, "".join(traceback.format_exception(caught.exception)))
                self.snapshot.revalidate()  # still caller-owned

    def test_initial_preflight_codes_and_poisoned_attributes_are_sanitized(self):
        cases = [RuntimeError(FAKE_KEY), secure.SecureWriteError(FAKE_KEY),
                 PoisonedException("code"), PoisonedException("release_unknown"), PropertyPoison(FAKE_KEY)]
        cases[0].code = FAKE_KEY
        for exc in cases:
            with self.subTest(kind=type(exc).__name__, poison=getattr(exc, "poisoned", "none")):
                def fail():
                    raise exc
                with patch("core.windows_secure_native.ProductWriteBackend") as factory:
                    with self.assertRaises(secure.SecureWriteError) as caught:
                        secure.write_target(self.snapshot, b"new", preflight=fail)
                factory.assert_not_called()
                error = caught.exception
                self.assertIn(error.code, ("preflight_failed", "release_unknown"))
                self.assertNotIn(FAKE_KEY, str(error))
                self.assertNotIn(FAKE_KEY, "".join(traceback.format_exception(error)))
                self.assertIsNone(error.__context__)
                self.assertIsNone(error.__cause__)
                self.snapshot.revalidate()

    def test_backend_constructor_code_is_sanitized_and_snapshot_stays_owned(self):
        failure = RuntimeError(FAKE_KEY)
        failure.code = FAKE_KEY
        with patch("core.windows_secure_native.ProductWriteBackend", side_effect=failure):
            with self.assertRaises(secure.SecureWriteError) as caught:
                secure.write_target(self.snapshot, b"new", preflight=lambda: True)
        self.assertEqual(caught.exception.code, "backend_unavailable")
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(FAKE_KEY, "".join(traceback.format_exception(caught.exception)))
        self.snapshot.revalidate()

    def test_opted_in_target_parent_succeeds_with_ancestor_not_private_acl(self):
        calls = []
        result = secure.write_target(self.snapshot, b"new", preflight=lambda: True,
                                     artifact_preflight=lambda role, path: calls.append(role) or True,
                                     make_backup=False, stage_directory=self.ROOT, stage_boundary=self.ROOT,
                                     stage_at_target_parent=True)
        self.assertEqual(result.state, "VERIFIED")
        self.assertEqual(self.fs.objects[self.TARGET]["data"], b"new")
        self.assertEqual(calls, ["stage", "stage"])
        self.assertEqual(len(self.fs.commits), 1)

    def test_target_parent_default_behavior_remains_ancestor(self):
        result = secure.write_target(self.snapshot, b"new", preflight=lambda: True, make_backup=False)
        self.assertEqual(result.state, "VERIFIED")
        self.assertEqual(self.fs.objects[self.TARGET]["data"], b"new")

    def test_new_target_with_explicit_parent_and_private_file(self):
        self.snapshot.close()
        del self.fs.objects[self.TARGET]
        self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024,
                                              trusted_boundary=self.ROOT)
        result = self.write(stage_directory=self.ROOT, stage_boundary=self.ROOT,
                            stage_at_target_parent=True)
        self.assertEqual(result.state, "VERIFIED")
        self.assertEqual(self.fs.objects[self.TARGET]["data"], b"new")
        self.assertEqual(self.fs.commits[0][0], "move")

    def test_target_parent_with_separately_authorized_private_backup(self):
        self.acl.public_dirs.remove(self.BACKUP)
        calls = []
        result = secure.write_target(self.snapshot, b"new", preflight=lambda: True,
                                     stage_directory=self.ROOT, stage_boundary=self.ROOT,
                                     stage_at_target_parent=True, backup_directory=self.BACKUP,
                                     backup_boundary=self.BACKUP,
                                     artifact_preflight=lambda role, path: calls.append(role) or True)
        self.assertEqual(result.state, "VERIFIED")
        self.assertEqual(self.fs.objects[result.backup]["data"], b"old")
        self.assertEqual(calls, ["backup", "stage", "backup", "stage"])

    def test_target_parent_permission_drift_prevents_commit(self):
        calls = []
        def check(role, path):
            calls.append(role)
            if len(calls) == 2:
                self.fs.objects[self.ROOT]["unsafe_group"] = True
            return True
        with self.assertRaises(secure.SecureWriteError) as caught:
            secure.write_target(self.snapshot, b"new", preflight=lambda: True, make_backup=False,
                                stage_directory=self.ROOT, stage_boundary=self.ROOT,
                                stage_at_target_parent=True, artifact_preflight=check)
        self.assertEqual(caught.exception.target_state, "NOT_REPLACED")
        self.assertFalse(self.fs.commits)
        self.assertEqual(self.fs.objects[self.TARGET]["data"], b"old")

    def test_target_preflight_remains_independent(self):
        checks = []
        with self.assertRaises(secure.SecureWriteError):
            secure.write_target(self.snapshot, b"new", preflight=lambda: False, make_backup=False,
                                stage_directory=self.ROOT, stage_at_target_parent=True,
                                artifact_preflight=lambda *args: checks.append(args) or True)
        self.assertFalse(checks)
        self.assertFalse(self.fs.commits)

    def test_explicit_parent_and_runtime_are_private_without_exception(self):
        for directory in (self.ROOT, self.RUNTIME):
            with self.subTest(directory=directory):
                with self.assertRaises(secure.SecureWriteError) as caught:
                    self.write(stage_directory=directory)
                self.assertEqual(caught.exception.target_state, "NOT_REPLACED")
                self.assertFalse(self.fs.commits)
                # Recapture after the finite transaction closes its snapshot.
                self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024,
                                                      trusted_boundary=self.ROOT)

    def test_exception_rejects_other_directory_even_if_authorized(self):
        with self.assertRaises(secure.SecureWriteError) as caught:
            self.write(stage_directory=self.RUNTIME, stage_at_target_parent=True)
        self.assertEqual(caught.exception.code, "candidate_parent_unverified")
        self.assertFalse(self.fs.commits)
        self.assertFalse(any(event[0] == "write_flush" for event in self.fs.calls))

    def test_exception_does_not_authorize_artifacts(self):
        with self.assertRaises(secure.SecureWriteError) as caught:
            secure.write_target(self.snapshot, b"new", preflight=lambda: True, make_backup=False,
                                stage_directory=self.ROOT, stage_at_target_parent=True,
                                artifact_preflight=lambda role, path: False)
        self.assertEqual(caught.exception.code, "artifact_preflight_failed")
        self.assertFalse(self.fs.commits)
        self.assertFalse(any(event[0] == "write_flush" for event in self.fs.calls))

    def test_target_parent_exception_never_weakens_candidate_acl(self):
        create = self.fs.open_private_candidate
        def unsafe_candidate(path, **kwargs):
            handle = create(path, **kwargs)
            self.fs.handles[handle][1]["unsafe_group"] = True
            return handle
        self.fs.open_private_candidate = unsafe_candidate
        with self.assertRaises(secure.SecureWriteError) as caught:
            self.write(stage_directory=self.ROOT, stage_boundary=self.ROOT,
                       stage_at_target_parent=True)
        self.assertEqual(caught.exception.target_state, "NOT_REPLACED")
        self.assertFalse(self.fs.commits)
        self.assertFalse(any(event[0] == "write_flush" for event in self.fs.calls))

    def test_backup_directory_still_requires_private_acl(self):
        with self.assertRaises(secure.SecureWriteError):
            secure.write_target(self.snapshot, b"new", preflight=lambda: True,
                                stage_directory=self.ROOT, stage_at_target_parent=True,
                                backup_directory=self.BACKUP, artifact_preflight=lambda *_: True)
        self.assertFalse(self.fs.commits)
        self.assertFalse(any(event[0] == "write_flush" for event in self.fs.calls))

    def test_explicit_flag_needs_directory_and_artifact_preflight(self):
        for options in ({"stage_at_target_parent": True},
                        {"stage_directory": self.ROOT, "stage_at_target_parent": True}):
            with self.subTest(options=options):
                with self.assertRaises(secure.SecureWriteError):
                    secure.write_target(self.snapshot, b"new", preflight=lambda: True, **options)
        self.assertFalse(self.fs.commits)


if __name__ == "__main__":
    unittest.main()
