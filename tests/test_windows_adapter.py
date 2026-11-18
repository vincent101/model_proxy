"""WinAPI contract tests with fake handles only; never invokes native Windows API."""
import ctypes
import errno
import sys
import threading
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import PurePosixPath, PureWindowsPath
from unittest.mock import Mock, patch

from core import platform_windows as win
from core.runtime_paths import LEGACY_DEFAULTS, PATH_KEYS, RuntimePathsError, parse_runtime_paths


class FakeKernel:
    def __init__(self):
        self.calls = []
        self.open_result = 100
        self.close_result = 1
        self.lock_result = 1
        self.unlock_result = 1
        self.info_result = 1
        self.attrs = 0
        self.links = 1
        self.serial = 17
        self.index_high = 1
        self.index_low = 23

    def CreateFileW(self, path, access, share, security, disposition, flags, template):
        sec = ctypes.cast(security, ctypes.POINTER(win.SECURITY_ATTRIBUTES)).contents
        self.calls.append(("open", path, access, share, sec.length,
                           sec.inherit_handle, sec.security_descriptor,
                           disposition, flags, template))
        return self.open_result

    def CloseHandle(self, handle):
        self.calls.append(("close", handle))
        return self.close_result

    def LockFileEx(self, handle, flags, reserved, low, high, overlapped):
        ov = ctypes.cast(overlapped, ctypes.POINTER(win.OVERLAPPED)).contents
        self.calls.append(("lock", handle, flags, reserved, low, high,
                           ov.Offset, ov.OffsetHigh, ov.hEvent))
        return self.lock_result

    def UnlockFileEx(self, handle, reserved, low, high, overlapped):
        ov = ctypes.cast(overlapped, ctypes.POINTER(win.OVERLAPPED)).contents
        self.calls.append(("unlock", handle, reserved, low, high,
                           ov.Offset, ov.OffsetHigh, ov.hEvent))
        return self.unlock_result

    def GetFileInformationByHandle(self, handle, pointer):
        self.calls.append(("info", handle))
        info = ctypes.cast(pointer, ctypes.POINTER(win.BY_HANDLE_FILE_INFORMATION)).contents
        info.attributes = self.attrs
        info.links = self.links
        info.volume_serial = self.serial
        info.index_high = self.index_high
        info.index_low = self.index_low
        return self.info_result


class WindowsAdapterTests(unittest.TestCase):
    def setUp(self):
        self.kernel = FakeKernel()
        self.api = win._NativeAPI.__new__(win._NativeAPI)
        self.api._kernel = self.kernel

    def test_win32_layout_uses_fixed_width_fields(self):
        self.assertEqual(ctypes.sizeof(win.DWORD), 4)
        self.assertEqual(ctypes.sizeof(win.BY_HANDLE_FILE_INFORMATION), 52)
        self.assertEqual(win.OVERLAPPED.Offset.offset, 2 * ctypes.sizeof(ctypes.c_size_t))

    def test_open_existing_and_new_never_truncate_or_inherit(self):
        for create, disposition in ((False, 3), (True, 1)):
            with self.subTest(create=create):
                with win.open_file(r"C:\isolated\锁.lock", create=create, backend=self.api) as h:
                    self.assertEqual(h.handle, 100)
                self.assertTrue(h.closed)
                event = self.kernel.calls[-3]
                self.assertEqual(event, ("open", r"C:\isolated\锁.lock", 0xC0000000,
                                         3, ctypes.sizeof(win.SECURITY_ATTRIBUTES), 0,
                                         None, disposition, 0x00200000, None))
                self.assertEqual(self.kernel.calls[-1], ("close", 100))
                h.close()
                self.assertEqual(self.kernel.calls[-1], ("close", 100))
                with self.assertRaises(OSError):
                    h.handle

    def test_failed_open_never_closes_invalid_handle(self):
        for invalid in (None, win.INVALID_HANDLE_VALUE):
            self.kernel.open_result = invalid
            with patch.object(ctypes, "get_last_error", return_value=5, create=True):
                with self.assertRaises(OSError) as caught:
                    win.open_file(r"C:\isolated\fake", backend=self.api)
            self.assertEqual(caught.exception.winerror, 5)
            self.assertEqual(caught.exception.errno, errno.EIO)
            self.assertFalse(any(c[0] == "close" for c in self.kernel.calls))

    def test_close_failure_remains_owned_and_can_be_retried(self):
        h = win.open_file(r"C:\isolated\fake", backend=self.api)
        self.kernel.close_result = 0
        with patch.object(ctypes, "get_last_error", return_value=6, create=True):
            with self.assertRaises(OSError) as caught:
                h.close()
        self.assertEqual(caught.exception.winerror, 6)
        self.assertFalse(h.closed)
        self.kernel.close_result = 1
        h.close()
        self.assertTrue(h.closed)
        self.assertEqual(self.kernel.calls.count(("close", 100)), 2)

    def test_context_closes_even_if_identity_fails(self):
        self.kernel.info_result = 0
        with patch.object(ctypes, "get_last_error", return_value=5, create=True):
            with self.assertRaises(OSError) as caught:
                win.open_file(r"C:\isolated\fake", create=True, backend=self.api)
        self.assertTrue(caught.exception.created)
        self.assertEqual(self.kernel.calls[-1], ("close", 100))

    def test_identity_rejection_and_close_failure_report_retryable_handle(self):
        self.kernel.info_result = 0
        self.kernel.close_result = 0
        with patch.object(ctypes, "get_last_error", return_value=5, create=True):
            with self.assertRaises(OSError) as caught:
                win.open_file(r"C:\isolated\fake", create=True, backend=self.api)
        self.assertTrue(caught.exception.created)
        owned = caught.exception.owned_handle
        self.assertFalse(owned.closed)
        self.kernel.close_result = 1
        owned.close()
        self.assertTrue(owned.closed)

    def test_native_binding_uses_typed_functions_and_last_error(self):
        kernel = Mock()
        with patch.object(win.os, "name", "nt"), \
             patch.object(ctypes, "WinDLL", return_value=kernel, create=True) as loader:
            api = win._NativeAPI()
        loader.assert_called_once_with("kernel32", use_last_error=True)
        self.assertIs(api._kernel, kernel)
        self.assertIs(kernel.CreateFileW.restype, win.HANDLE)
        self.assertIs(kernel.LockFileEx.restype, win.BOOL)
        self.assertEqual(len(kernel.LockFileEx.argtypes), 6)
        self.assertEqual(len(kernel.UnlockFileEx.argtypes), 5)
        self.assertEqual(len(kernel.GetFileInformationByHandle.argtypes), 2)
        self.assertEqual(len(kernel.QueryDosDeviceW.argtypes), 3)
        self.assertEqual(len(kernel.GetVolumeInformationW.argtypes), 8)

    def test_nonblocking_exclusive_and_matching_unlock(self):
        win.lock_handle(100, backend=self.api)
        win.lock_handle(100, unlock=True, backend=self.api)
        self.assertEqual(self.kernel.calls, [
            ("lock", 100, 3, 0, 1, 0, 0, 0, None),
            ("unlock", 100, 0, 1, 0, 0, 0, None)])

    def test_conflict_codes_only_apply_to_lock(self):
        for code, expected in ((32, errno.EAGAIN), (33, errno.EAGAIN),
                               (5, errno.EIO), (158, errno.EIO), (999, errno.EIO)):
            with self.subTest(code=code), patch.object(ctypes, "get_last_error", return_value=code, create=True):
                self.kernel.lock_result = 0
                with self.assertRaises(OSError) as caught:
                    win.lock_handle(100, backend=self.api)
                self.assertEqual((caught.exception.errno, caught.exception.winerror), (expected, code))
                self.kernel.unlock_result = 0
                with self.assertRaises(OSError) as caught:
                    win.lock_handle(100, unlock=True, backend=self.api)
                self.assertEqual((caught.exception.errno, caught.exception.winerror), (errno.EIO, code))

    def test_borrowed_descriptor_never_closes_handle(self):
        resolve = Mock(return_value=100)
        win.lock_fd(7, backend=self.api, handle_from_fd=resolve)
        win.lock_fd(7, unlock=True, backend=self.api, handle_from_fd=resolve)
        self.assertEqual(resolve.call_count, 2)
        self.assertFalse(any(c[0] == "close" for c in self.kernel.calls))

    def test_invalid_handle_fails_before_kernel(self):
        for handle in (None, -1, 0, win.INVALID_HANDLE_VALUE):
            with self.subTest(handle=handle), self.assertRaises(OSError):
                win.lock_handle(handle, backend=self.api)
        self.assertEqual(self.kernel.calls, [])

    def test_identity_rejects_reparse_directory_hardlinks_and_unknown_index(self):
        identity = win.file_identity(100, backend=self.api)
        self.assertEqual(identity, win.FileIdentity(17, (1 << 32) | 23, 1, 0))
        for attrs, links, high, low in ((0x400, 1, 1, 2), (0x10, 1, 1, 2),
                                        (0, 2, 1, 2), (0, 0, 1, 2), (0, 1, 0, 0)):
            self.kernel.attrs, self.kernel.links = attrs, links
            self.kernel.index_high, self.kernel.index_low = high, low
            with self.subTest(attrs=attrs, links=links, high=high, low=low):
                with self.assertRaises(OSError):
                    win.file_identity(100, backend=self.api)

    def test_acl_is_not_proven_by_mode_or_mock_handle(self):
        self.assertFalse(win.verify_local_access(r"C:\isolated\fake", "file"))
        self.assertFalse(win.verify_local_access(r"C:\isolated", "directory"))

    def test_fake_mapping_is_pure_and_only_maps_exact_defaults(self):
        raw = dict(LEGACY_DEFAULTS)
        with patch("os.stat", side_effect=AssertionError("stat forbidden")), \
             patch("os.lstat", side_effect=AssertionError("lstat forbidden")), \
             patch("builtins.open", side_effect=AssertionError("open forbidden")):
            mapped = parse_runtime_paths(raw, project_root=r"C:\isolated\工程", platform="windows",
                                         local_appdata=r"C:\isolated\local")
        for key in ("lock", "pid", "ensure_log", "start_lock"):
            self.assertEqual(mapped[key], PureWindowsPath(r"C:\isolated\local\model_proxy\runtime") /
                             PurePosixPath(LEGACY_DEFAULTS[key]).name)
        self.assertEqual(mapped["log"], PureWindowsPath(r"C:\isolated\工程\.model_proxy.log"))
        self.assertEqual(mapped["totals"], PureWindowsPath(r"C:\isolated\工程\.model_proxy_totals.json"))
        raw["lock"] = r"D:\isolated\custom.lock"
        self.assertEqual(parse_runtime_paths(raw, project_root=r"C:\isolated\工程", platform="windows",
                                           local_appdata=r"C:\isolated\local")["lock"],
                         PureWindowsPath(raw["lock"]))
        raw["lock"] = "/tmp/custom.lock"  # value only; never opened/stat'ed
        with self.assertRaises(RuntimePathsError):
            parse_runtime_paths(raw, project_root=r"C:\isolated\工程", platform="windows",
                                local_appdata=r"C:\isolated\local")


class FakeCRT:
    def __init__(self):
        self.adopt_calls = []
        self.close_calls = []
        self.mapping = {}
        self.fail_adopt = False
        self.ambiguous_adopt = False
        self.fail_inheritance = False
        self.fail_close = False
        self.handle_override = None
        self.inherit_calls = []

    def adopt(self, handle):
        self.adopt_calls.append(handle)
        if self.fail_adopt:
            raise OSError(errno.EIO, "fake adopt")
        if self.ambiguous_adopt:
            raise RuntimeError("unknown transfer outcome")
        self.mapping[77] = handle
        return 77

    def handle(self, fd):
        return self.handle_override if self.handle_override is not None else self.mapping[fd]

    def make_noninheritable(self, fd):
        self.inherit_calls.append(fd)
        if self.fail_inheritance:
            raise OSError(errno.EIO, "fake inheritance")

    def close(self, fd):
        self.close_calls.append(fd)
        if self.fail_close:
            raise OSError(errno.EIO, "fake close")
        self.mapping.pop(fd, None)


class WindowsObjectWiringTests(unittest.TestCase):
    """No path authority: only a fake object, fake CRT, and fake ACL backend."""
    def setUp(self):
        from core import windows_acl as acl
        from tests.test_windows_acl import FakeBackend, valid_observation
        self.acl = acl
        self.kernel = FakeKernel()
        self.api = win._NativeAPI.__new__(win._NativeAPI)
        self.api._kernel = self.kernel
        self.crt = FakeCRT()
        access = acl.FILE_GENERIC_READ | acl.FILE_GENERIC_WRITE
        self.observation = replace(valid_observation(),
                                   identity=(17, (1 << 32) | 23, 1, 0),
                                   desired=access, granted=access)
        self.backend_class = FakeBackend
        self.acl_backend = FakeBackend(self.observation)

    def opened(self, create=False):
        return win.open_file(r"C:\isolated\lock", create=create, backend=self.api)

    def test_fd_transfer_same_handle_acl_lock_unlock_and_one_close(self):
        owned = self.opened(create=True)
        locked = win.lock_private_object(owned, fd_backend=self.crt, acl_backend=self.acl_backend)
        self.assertTrue(owned.closed)
        self.assertTrue(locked.evidence.created)
        self.assertEqual(locked.fileno(), 77)
        self.assertEqual(self.crt.adopt_calls, [100])
        self.assertEqual(self.crt.inherit_calls, [77])
        self.assertEqual([event for event in self.kernel.calls if event[0] == "lock"],
                         [("lock", 100, 3, 0, 1, 0, 0, 0, None)])
        self.assertEqual(len(self.acl_backend.handles), 4)
        self.assertTrue(all(event[0] == 100 for event in self.acl_backend.handles))
        locked.close()
        locked.close()
        self.assertEqual(self.crt.close_calls, [77])
        self.assertEqual([event for event in self.kernel.calls if event[0] == "unlock"],
                         [("unlock", 100, 0, 1, 0, 0, 0, None)])
        self.assertFalse(any(event[0] == "close" for event in self.kernel.calls))

    def test_transferred_fd_remains_compatible_with_existing_heldlock(self):
        import core.platform_lock as locks
        owned = self.opened()
        locked = win.lock_private_object(owned, fd_backend=self.crt, acl_backend=self.acl_backend)
        fd = locked.detach()
        self.assertTrue(locked.closed)
        # This tests ownership mechanics only, not the still-blocked path gate.
        with patch.object(locks.os, "name", "nt"), \
             patch.object(locks.os, "close", side_effect=self.crt.close), \
             patch.object(locks, "_lock_fd", side_effect=lambda n, unlock=False:
                          win.lock_fd(n, unlock=unlock, backend=self.api, handle_from_fd=self.crt.handle)):
            held = locks.HeldLock(fd, "control.lock", None)
            self.assertEqual(held.fileno(), 77)
            held.close()
        self.assertEqual(self.crt.close_calls, [77])
        self.assertEqual(len([e for e in self.kernel.calls if e[0] == "unlock"]), 1)

    def test_real_conflict_codes_only_and_no_unlock_on_failed_acquisition(self):
        for code, expected in ((32, "lock_conflict"), (33, "lock_conflict"),
                               (5, "lock_unavailable"), (158, "lock_unavailable")):
            self.setUp()
            self.kernel.lock_result = 0
            owned = self.opened(create=True)
            with self.subTest(code=code), patch.object(ctypes, "get_last_error", return_value=code, create=True):
                with self.assertRaises(win.WindowsObjectLockError) as caught:
                    win.lock_private_object(owned, fd_backend=self.crt, acl_backend=self.acl_backend)
            self.assertEqual(caught.exception.code, expected)
            self.assertTrue(caught.exception.created)
            self.assertEqual(self.crt.close_calls, [77])
            self.assertFalse(any(e[0] == "unlock" for e in self.kernel.calls))
            self.assertFalse(any(e[0] == "close" for e in self.kernel.calls))

    def test_acl_change_after_lock_unlocks_then_closes(self):
        changed = replace(self.observation, control=self.observation.control ^ 0x1000)
        self.acl_backend = self.backend_class(self.observation, self.observation, changed, changed)
        with self.assertRaises(win.WindowsObjectLockError) as caught:
            win.lock_private_object(self.opened(), fd_backend=self.crt, acl_backend=self.acl_backend)
        self.assertEqual(caught.exception.code, "security_changed")
        self.assertEqual(self.crt.close_calls, [77])
        self.assertEqual(len([e for e in self.kernel.calls if e[0] == "unlock"]), 1)

    def test_acl_resource_release_failure_is_unknown_not_permission_denial(self):
        backend = self.backend_class(self.acl.ACLVerificationError("resource_release_failed"))
        with self.assertRaises(win.WindowsObjectLockError) as caught:
            win.lock_private_object(self.opened(), fd_backend=self.crt, acl_backend=backend)
        self.assertEqual(caught.exception.code, "lock_release_unknown")
        self.assertEqual(self.crt.close_calls, [77])
        self.assertFalse(any(e[0] == "lock" for e in self.kernel.calls))

    def test_acl_denial_before_lock_closes_once(self):
        denied = replace(self.observation, access_granted=False, granted=0)
        with self.assertRaises(win.WindowsObjectLockError) as caught:
            win.lock_private_object(self.opened(), fd_backend=self.crt,
                                    acl_backend=self.backend_class(denied))
        self.assertEqual(caught.exception.code, "access_unverified")
        self.assertEqual(self.crt.close_calls, [77])
        self.assertFalse(any(e[0] == "lock" for e in self.kernel.calls))

    def test_target_reparse_after_open_fails_before_lock_and_closes(self):
        owned = self.opened()
        self.kernel.attrs = win.FILE_ATTRIBUTE_REPARSE_POINT
        with self.assertRaises(win.WindowsObjectLockError):
            win.lock_private_object(owned, fd_backend=self.crt, acl_backend=self.acl_backend)
        self.assertEqual(self.crt.close_calls, [77])
        self.assertFalse(any(e[0] == "lock" for e in self.kernel.calls))

    def test_target_identity_change_after_lock_is_detected(self):
        owned = self.opened()
        original = self.kernel.LockFileEx
        def change_identity(*args):
            result = original(*args)
            self.kernel.index_low = 24
            return result
        self.kernel.LockFileEx = change_identity
        with self.assertRaises(win.WindowsObjectLockError) as caught:
            win.lock_private_object(owned, fd_backend=self.crt, acl_backend=self.acl_backend)
        self.assertEqual(caught.exception.code, "identity_changed")
        self.assertEqual(self.crt.close_calls, [77])
        self.assertEqual(len([e for e in self.kernel.calls if e[0] == "unlock"]), 1)

    def test_concurrent_transfer_has_one_owner(self):
        owned = self.opened()
        start = threading.Barrier(2)
        successes, failures = [], []
        def run():
            start.wait()
            try:
                successes.append(win.lock_private_object(owned, fd_backend=self.crt,
                                                          acl_backend=self.acl_backend))
            except win.WindowsObjectLockError as exc:
                failures.append(exc.code)
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(successes), 1)
        self.assertEqual(failures, ["handle_invalid"])
        self.assertEqual(self.crt.adopt_calls, [100])
        successes[0].close()
        self.assertEqual(self.crt.close_calls, [77])

    def test_concurrent_close_unlocks_and_closes_exactly_once(self):
        locked = win.lock_private_object(self.opened(), fd_backend=self.crt, acl_backend=self.acl_backend)
        start = threading.Barrier(4)
        errors = []
        def run():
            start.wait()
            try:
                locked.close()
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(self.crt.close_calls, [77])
        self.assertEqual(len([e for e in self.kernel.calls if e[0] == "unlock"]), 1)

    def test_native_crt_uses_noinherit_and_checks_handle_inheritance(self):
        fake_crt = Mock()
        fake_crt.open_osfhandle.return_value = 77
        fake_crt.get_osfhandle.return_value = 100
        with patch.object(win.os, "name", "nt"), \
             patch.object(win.os, "O_NOINHERIT", 0x80, create=True), \
             patch.object(win.os, "O_BINARY", 0x8000, create=True), \
             patch.dict(sys.modules, {"msvcrt": fake_crt}), \
             patch.object(win.os, "set_inheritable") as set_inherit, \
             patch.object(win.os, "get_inheritable", return_value=False):
            api = win._NativeFDAPI()
            self.assertEqual(api.adopt(100), 77)
            self.assertEqual(api.handle(77), 100)
            api.make_noninheritable(77)
        fake_crt.open_osfhandle.assert_called_once_with(100, win.os.O_RDWR | 0x80 | 0x8000)
        set_inherit.assert_called_once_with(77, False)

    def test_unlock_failure_closes_once_reports_unknown_without_retry(self):
        locked = win.lock_private_object(self.opened(), fd_backend=self.crt, acl_backend=self.acl_backend)
        self.kernel.unlock_result = 0
        with patch.object(ctypes, "get_last_error", return_value=158, create=True):
            with self.assertRaises(win.WindowsObjectLockError) as caught:
                locked.close()
        self.assertEqual(caught.exception.code, "lock_release_unknown")
        locked.close()
        self.assertEqual(self.crt.close_calls, [77])
        self.assertEqual(len([e for e in self.kernel.calls if e[0] == "unlock"]), 1)

    def test_changed_fd_after_lock_is_never_blindly_unlocked_or_closed(self):
        owned = self.opened()
        original = self.kernel.LockFileEx
        def replace_fd(*args):
            result = original(*args)
            self.crt.handle_override = 999
            return result
        self.kernel.LockFileEx = replace_fd
        with self.assertRaises(win.WindowsObjectLockError) as caught:
            win.lock_private_object(owned, fd_backend=self.crt, acl_backend=self.acl_backend)
        self.assertEqual(caught.exception.code, "lock_release_unknown")
        self.assertEqual(self.crt.close_calls, [])
        self.assertFalse(any(e[0] == "unlock" for e in self.kernel.calls))

    def test_concurrent_close_waits_until_fd_release_finishes(self):
        locked = win.lock_private_object(self.opened(), fd_backend=self.crt, acl_backend=self.acl_backend)
        entered, release, started, finished = (threading.Event() for _ in range(4))
        original = self.crt.close
        def slow_close(fd):
            entered.set()
            if not release.wait(2):
                raise OSError(errno.EIO, "fake close timeout")
            original(fd)
        self.crt.close = slow_close
        errors = []
        def close_first():
            try: locked.close()
            except Exception as exc: errors.append(exc)
        def close_second():
            started.set()
            try: locked.close()
            except Exception as exc: errors.append(exc)
            finally: finished.set()
        first = threading.Thread(target=close_first)
        second = threading.Thread(target=close_second)
        try:
            first.start()
            self.assertTrue(entered.wait(1))
            second.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(0.05))
        finally:
            release.set()
            first.join(2)
            if second.ident is not None: second.join(2)
        self.assertEqual(errors, [])
        self.assertTrue(finished.is_set())
        self.assertEqual(self.crt.close_calls, [77])

    def test_transfer_failure_closes_raw_handle_exactly_once(self):
        owned = self.opened()
        self.crt.fail_adopt = True
        with self.assertRaises(win.WindowsObjectLockError):
            win.lock_private_object(owned, fd_backend=self.crt, acl_backend=self.acl_backend)
        self.assertTrue(owned.closed)
        self.assertEqual(self.kernel.calls.count(("close", 100)), 1)
        self.assertEqual(self.crt.close_calls, [])

    def test_ambiguous_transfer_does_not_guess_which_owner_to_close(self):
        owned = self.opened()
        self.crt.ambiguous_adopt = True
        with self.assertRaises(win.WindowsObjectLockError) as caught:
            win.lock_private_object(owned, fd_backend=self.crt, acl_backend=self.acl_backend)
        self.assertEqual(caught.exception.code, "lock_release_unknown")
        self.assertTrue(owned.closed)
        self.assertEqual(self.crt.close_calls, [])
        self.assertFalse(any(e[0] == "close" for e in self.kernel.calls))

    def test_failed_noninherit_check_fails_before_lock_and_closes(self):
        self.crt.fail_inheritance = True
        with self.assertRaises(win.WindowsObjectLockError):
            win.lock_private_object(self.opened(), fd_backend=self.crt, acl_backend=self.acl_backend)
        self.assertEqual(self.crt.close_calls, [77])
        self.assertFalse(any(e[0] == "lock" for e in self.kernel.calls))

    def test_fd_handle_mismatch_is_unknown_and_not_blindly_closed(self):
        self.crt.handle_override = 999
        with self.assertRaises(win.WindowsObjectLockError) as caught:
            win.lock_private_object(self.opened(), fd_backend=self.crt, acl_backend=self.acl_backend)
        self.assertEqual(caught.exception.code, "lock_release_unknown")
        self.assertEqual(self.crt.close_calls, [])
        self.assertFalse(any(e[0] in ("lock", "unlock", "close") for e in self.kernel.calls))

    def test_close_failure_reports_unknown_and_is_not_retried(self):
        locked = win.lock_private_object(self.opened(), fd_backend=self.crt, acl_backend=self.acl_backend)
        self.crt.fail_close = True
        with self.assertRaises(win.WindowsObjectLockError) as caught:
            locked.close()
        self.assertEqual(caught.exception.code, "lock_release_unknown")
        self.assertTrue(locked.closed)
        locked.close()
        self.assertEqual(self.crt.close_calls, [77])
        self.assertEqual(len([e for e in self.kernel.calls if e[0] == "unlock"]), 1)

    def test_failed_cleanup_close_does_not_retry(self):
        self.kernel.lock_result = 0
        self.crt.fail_close = True
        with patch.object(ctypes, "get_last_error", return_value=33, create=True):
            with self.assertRaises(win.WindowsObjectLockError) as caught:
                win.lock_private_object(self.opened(), fd_backend=self.crt, acl_backend=self.acl_backend)
        self.assertEqual(caught.exception.code, "lock_release_unknown")
        self.assertEqual(self.crt.close_calls, [77])


class DirectoryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.kernel = FakeKernel()
        self.kernel.attrs = win.FILE_ATTRIBUTE_DIRECTORY
        self.kernel.links = 3
        self.api = win._NativeAPI.__new__(win._NativeAPI)
        self.api._kernel = self.kernel

    def test_directory_noninheritable_open_no_share_delete_and_identity(self):
        with win.open_directory(r"C:\isolated", backend=self.api) as opened:
            identity = win.directory_identity(opened.handle, backend=self.api)
            self.assertEqual(identity, win.DirectoryIdentity(17, (1 << 32) | 23, 3, 0x10))
        event = self.kernel.calls[0]
        self.assertEqual(event, ("open", r"C:\isolated", 0x20081, 3,
                                 ctypes.sizeof(win.SECURITY_ATTRIBUTES), 0, None, 3, 0x2200000, None))
        self.assertEqual(self.kernel.calls[-1], ("close", 100))

    def test_directory_final_reparse_is_rejected_and_closed(self):
        self.kernel.attrs = 0x10 | 0x400
        with self.assertRaises(OSError):
            win.open_directory(r"C:\isolated", backend=self.api)
        self.assertEqual(self.kernel.calls[-1], ("close", 100))

    def test_immutable_chain_checks_observed_acl_but_does_not_claim_path_binding(self):
        from core import windows_acl as acl
        from tests.test_windows_acl import valid_observation
        access = 0x200A1
        entries = {
            201: replace(valid_observation(kind="directory"), identity=(1, 201, 1, 0x10),
                         desired=access, granted=access),
            202: replace(valid_observation(kind="directory"), identity=(1, 202, 1, 0x10),
                         desired=access, granted=access),
        }
        class PerHandle:
            def read_observation(self, handle, kind, required): return entries[handle]
        backend = PerHandle()
        first = acl.read_stable_evidence(201, kind="directory", required_access=access,
                                         policy="ancestor", backend=backend)
        second = acl.read_stable_evidence(202, kind="directory", required_access=access,
                                          policy="ancestor", backend=backend)
        chain = win.ParentChainEvidence(r"C:\fake\lock", (
            win.DirectoryEvidence("C:\\", first), win.DirectoryEvidence(r"C:\fake", second)))
        win.revalidate_parent_chain(chain, acl_backend=backend)
        with self.assertRaises(FrozenInstanceError):
            chain.target = "other"
        bad = win.ParentChainEvidence(chain.target, chain.parents[:1])
        with self.assertRaises(ValueError):
            win.revalidate_parent_chain(bad, acl_backend=backend)
        entries[202] = replace(entries[202], control=entries[202].control ^ 0x1000)
        with self.assertRaises(acl.ACLVerificationError):
            win.revalidate_parent_chain(chain, acl_backend=backend)


class VirtualWindows:
    """In-memory path/handle namespace; all reopens are synthetic."""
    def __init__(self):
        self.objects = {
            "C:\\": self.obj(1, directory=True),
            r"C:\fake": self.obj(2, directory=True),
            r"C:\fake\lock": self.obj(3),
        }
        self.handles = {}
        self.next_handle = 100
        self.calls = []
        self.close_calls = []
        self.counts = {}
        self.locked = set()
        self.drive = 3
        self.filesystem = "NTFS"
        self.serial = 17
        self.device = r"\Device\HarddiskVolume1"
        self.on_open = None
        self.close_fail = set()

    @staticmethod
    def obj(index, directory=False):
        return {"index": index, "volume": 17, "attributes": 0x10 if directory else 0,
                "links": 1, "control": 0x9004, "unsafe_group": False}

    def volume_info(self, root):
        self.calls.append(("drive", root))
        return (self.drive, self.filesystem, self.serial, self.device)

    def _open(self, path, typ, create=False):
        self.counts[(typ, path)] = self.counts.get((typ, path), 0) + 1
        count = self.counts[(typ, path)]
        self.calls.append(("open", typ, path, create, count))
        if self.on_open:
            self.on_open(typ, path, count)
        if create:
            if path in self.objects: raise OSError(errno.EEXIST, "fake exists")
            self.objects[path] = self.obj(3)
        if path not in self.objects:
            raise OSError(errno.ENOENT, "fake missing")
        self.next_handle += 1
        self.handles[self.next_handle] = (path, self.objects[path])
        return self.next_handle

    def open_directory(self, path): return self._open(path, "directory")
    def open_file(self, path, create): return self._open(path, "file", create)
    def open_private_candidate(self, path, *, user_sid):
        return self.open_file(path, True)

    def file_info(self, handle):
        _, data = self.handles[handle]
        result = win.BY_HANDLE_FILE_INFORMATION()
        result.volume_serial = data["volume"]
        result.index_low = data["index"]
        result.links = data["links"]
        result.attributes = data["attributes"]
        return result

    def lock(self, handle, *, unlock):
        _, data = self.handles[handle]
        key = data["index"]
        self.calls.append(("unlock" if unlock else "lock", handle, key))
        if unlock:
            self.locked.remove(key)
        elif key in self.locked:
            raise win._error(33, operation="lock", conflict=True)
        else:
            self.locked.add(key)

    def close(self, handle):
        self.close_calls.append(handle)
        if handle in self.close_fail:
            raise OSError(errno.EIO, "fake close")
        del self.handles[handle]


class VirtualCRT:
    def __init__(self, winapi):
        self.api = winapi
        self.mapping = {}
        self.next_fd = 200
        self.close_calls = []
        self.fail_close = False
    def adopt(self, handle):
        self.next_fd += 1
        self.mapping[self.next_fd] = handle
        return self.next_fd
    def handle(self, fd): return self.mapping[fd]
    def make_noninheritable(self, fd): pass
    def close(self, fd):
        self.close_calls.append(fd)
        if self.fail_close: raise OSError(errno.EIO, "fake fd close")
        handle = self.mapping.pop(fd)
        self.api.close(handle)


class VirtualACL:
    def __init__(self, winapi):
        self.api = winapi
        self.calls = []
        self.on_read = None
    def read_observation(self, handle, kind, required):
        from core import windows_acl as acl
        from tests.test_windows_acl import valid_observation, raw_ace
        self.calls.append((handle, kind, required))
        _, data = self.api.handles[handle]
        observation = valid_observation(kind=kind)
        aces = observation.aces
        if data["unsafe_group"]:
            aces += (acl._parse_ace(raw_ace(acl._BUILTIN_USERS, mask=0x2)),)
        desired = acl._map_mask(required)
        result = replace(observation, identity=(data["volume"], data["index"], data["links"], data["attributes"]),
                         control=data["control"], desired=desired, granted=desired, aces=aces)
        if self.on_read:
            self.on_read(handle, kind)
        return result


class GuardedPathLockTests(unittest.TestCase):
    ROOT = "C:\\"
    PARENT = r"C:\fake"
    TARGET = r"C:\fake\lock"

    def setUp(self):
        self.api = VirtualWindows()
        self.crt = VirtualCRT(self.api)
        self.acl = VirtualACL(self.api)

    def acquire(self, *, create=False):
        return win.acquire_guarded_path_lock(self.TARGET, create=create, backend=self.api,
                                             fd_backend=self.crt, acl_backend=self.acl)

    def test_holds_all_parent_handles_rewalks_and_closes_in_reverse(self):
        guard = self.acquire()
        original_root, original_parent = sorted(self.api.handles)[:2]
        self.assertEqual(self.api.handles[original_root][0], self.ROOT)
        self.assertEqual(self.api.handles[original_parent][0], self.PARENT)
        self.assertEqual(self.api.counts[("directory", self.ROOT)], 4)
        self.assertEqual(self.api.counts[("directory", self.PARENT)], 4)
        self.assertEqual(self.api.counts[("file", self.TARGET)], 2)
        self.assertEqual(len(guard.parent_evidence.parents), 2)
        self.assertEqual(guard.object_evidence.identity.file_index, 3)
        self.assertEqual(guard.volume_evidence, (3, "NTFS", 17, r"\Device\HarddiskVolume1"))
        fd = guard.fileno()
        guard.revalidate()
        self.assertIn(original_root, self.api.handles)
        self.assertIn(original_parent, self.api.handles)
        guard.close()
        guard.close()
        self.assertEqual(self.crt.close_calls, [fd])
        self.assertEqual(self.api.close_calls[-2:], [original_parent, original_root])
        self.assertEqual(self.api.handles, {})
        self.assertEqual(self.api.locked, set())

    def test_contention_keeps_first_lock_and_releases_second_resources(self):
        first = self.acquire()
        with self.assertRaises(win.WindowsPathGuardError) as caught:
            self.acquire()
        self.assertEqual(caught.exception.code, "lock_conflict")
        self.assertFalse(first.closed)
        self.assertEqual(self.api.locked, {3})
        first.close()
        third = self.acquire()
        third.close()
        self.assertEqual(self.api.handles, {})
        self.assertEqual(self.api.locked, set())

    def test_remote_unc_unknown_drive_rejected_before_any_path_open(self):
        for drive in (0, 2, 4, 5):
            self.setUp()
            self.api.drive = drive
            with self.subTest(drive=drive), self.assertRaises(win.WindowsPathGuardError) as caught:
                self.acquire()
            self.assertEqual(caught.exception.code, "remote_unsupported" if drive == 4 else "drive_unverified")
            self.assertFalse(any(e[0] == "open" for e in self.api.calls))
        with self.assertRaises(win.WindowsPathGuardError) as caught:
            win.acquire_guarded_path_lock(r"\\server\share\lock", backend=self.api,
                                          fd_backend=self.crt, acl_backend=self.acl)
        self.assertEqual(caught.exception.code, "remote_unsupported")

    def test_unknown_filesystem_or_subst_device_fails_before_any_open(self):
        for fs, device, serial in (("ReFS", r"\Device\HarddiskVolume1", 17),
                                   ("NTFS", r"\??\C:\fake", 17),
                                   ("NTFS", r"\Device\HarddiskVolume1", -1)):
            self.setUp()
            self.api.filesystem, self.api.device, self.api.serial = fs, device, serial
            with self.subTest(fs=fs, device=device, serial=serial), self.assertRaises(win.WindowsPathGuardError):
                self.acquire()
            self.assertFalse(any(event[0] == "open" for event in self.api.calls))

    def test_native_volume_binding_uses_fixed_drive_dos_device_and_serial(self):
        class VolumeKernel:
            def GetDriveTypeW(self, root):
                self.root = root
                return 3
            def QueryDosDeviceW(self, drive, output, size):
                self.drive = drive
                output.value = r"\Device\HarddiskVolume7"
                return len(output.value)
            def GetVolumeInformationW(self, root, label, label_size, serial, component, flags, fs, fs_size):
                ctypes.cast(serial, ctypes.POINTER(win.DWORD))[0] = 42
                ctypes.cast(flags, ctypes.POINTER(win.DWORD))[0] = 0x8
                fs.value = "NTFS"
                return 1
        kernel = VolumeKernel()
        api = win._NativeAPI.__new__(win._NativeAPI)
        api._kernel = kernel
        self.assertEqual(api.volume_info("C:\\"), (3, "NTFS", 42, r"\Device\HarddiskVolume7"))
        self.assertEqual(kernel.drive, "C:")

    def test_native_volume_without_persistent_acls_fails_closed(self):
        class MissingACLKernel:
            def GetDriveTypeW(self, root): return 3
            def QueryDosDeviceW(self, drive, output, size):
                output.value = r"\Device\HarddiskVolume7"
                return 1
            def GetVolumeInformationW(self, root, label, label_size, serial, component, flags, fs, fs_size):
                fs.value = "NTFS"
                return 1  # flags are left zero
        api = win._NativeAPI.__new__(win._NativeAPI)
        api._kernel = MissingACLKernel()
        with self.assertRaises(OSError):
            api.volume_info("C:\\")

    def test_native_volume_lookup_failure_is_sanitized(self):
        class FailureKernel:
            def GetDriveTypeW(self, root): return 3
            def QueryDosDeviceW(self, drive, output, size): return 0
        api = win._NativeAPI.__new__(win._NativeAPI)
        api._kernel = FailureKernel()
        with patch.object(ctypes, "get_last_error", return_value=5, create=True):
            with self.assertRaises(OSError) as caught:
                api.volume_info("C:\\")
        self.assertEqual(caught.exception.winerror, 5)
        self.assertNotIn("C:", str(caught.exception))

    def test_create_new_success_retains_file_after_guard_close(self):
        self.api.objects.pop(self.TARGET)
        guard = self.acquire(create=True)
        self.assertTrue(guard.created)
        self.assertIn(self.TARGET, self.api.objects)
        guard.close()
        self.assertIn(self.TARGET, self.api.objects)
        self.assertEqual(self.api.handles, {})

    def test_target_other_volume_fails_before_lock_and_closes_handle(self):
        self.api.objects[self.TARGET]["volume"] = 42
        with self.assertRaises(win.WindowsPathGuardError) as caught:
            self.acquire()
        self.assertEqual(caught.exception.code, "volume_changed")
        self.assertFalse(any(e[0] == "lock" for e in self.api.calls))
        self.assertEqual(self.api.handles, {})

    def test_volume_changed_after_creation_reports_partial_creation(self):
        self.api.objects.pop(self.TARGET)
        def change(typ, path, count):
            if typ == "file" and path == self.TARGET and count == 1:
                self.api.serial = 18
        self.api.on_open = change
        with self.assertRaises(win.WindowsPathGuardError) as caught:
            self.acquire(create=True)
        self.assertEqual(caught.exception.code, "volume_changed")
        self.assertTrue(caught.exception.created)
        self.assertIn(self.TARGET, self.api.objects)
        self.assertEqual(self.api.handles, {})
        self.assertEqual(self.api.locked, set())

    def test_zero_link_count_directory_fails_before_target(self):
        self.api.objects[self.PARENT]["links"] = 0
        with self.assertRaises(win.WindowsPathGuardError):
            self.acquire()
        self.assertFalse(any(e[0] == "lock" for e in self.api.calls))
        self.assertEqual(self.api.handles, {})

    def test_target_reparse_rejected_and_parent_handles_closed(self):
        self.api.objects[self.TARGET]["attributes"] = 0x400
        with self.assertRaises(win.WindowsPathGuardError):
            self.acquire()
        self.assertFalse(any(e[0] == "lock" for e in self.api.calls))
        self.assertEqual(self.api.handles, {})

    def test_reopened_target_close_failure_is_unknown_without_retry(self):
        def fail_temp_close(typ, path, count):
            if typ == "file" and path == self.TARGET and count == 2:
                self.api.close_fail.add(self.api.next_handle + 1)
        self.api.on_open = fail_temp_close
        with self.assertRaises(win.WindowsPathGuardError) as caught:
            self.acquire()
        self.assertEqual(caught.exception.code, "release_unknown")
        failed_handle = next(iter(self.api.close_fail))
        self.assertEqual(self.api.close_calls.count(failed_handle), 1)
        self.assertEqual(len(self.crt.close_calls), 1)
        self.assertEqual(self.api.locked, set())
        self.assertEqual(set(self.api.handles), {failed_handle})

    def test_parent_close_failure_reports_unknown_and_never_retries(self):
        guard = self.acquire()
        original_parents = sorted(self.api.handles)[:2]
        self.api.close_fail.add(original_parents[-1])
        with self.assertRaises(win.WindowsPathGuardError) as caught:
            guard.close()
        self.assertEqual(caught.exception.code, "release_unknown")
        guard.close()
        self.assertEqual(self.api.close_calls.count(original_parents[-1]), 1)
        self.assertEqual(len(self.crt.close_calls), 1)
        self.assertNotIn(original_parents[0], self.api.handles)

    def test_ambiguous_or_device_paths_rejected_before_backend(self):
        for path in (r"C:fake\lock", r"C:\fake\..\lock", r"C:\fake\nul.txt", r"C:\fake\COM¹.txt", r"C:\fake\CONIN$",
                     r"\\?\C:\fake\lock", "C:/fake/lock", r"C:\fake\lock:stream"):
            with self.subTest(path=path), self.assertRaises(win.WindowsPathGuardError):
                win.acquire_guarded_path_lock(path, backend=self.api, fd_backend=self.crt,
                                              acl_backend=self.acl)
        self.assertEqual(self.api.calls, [])

    def test_parent_identity_change_before_open_refuses_creation(self):
        self.api.objects.pop(self.TARGET)
        def change(typ, path, count):
            if typ == "directory" and path == self.PARENT and count == 2:
                self.api.objects[path] = self.api.obj(20, directory=True)
        self.api.on_open = change
        with self.assertRaises(win.WindowsPathGuardError) as caught:
            self.acquire(create=True)
        self.assertEqual(caught.exception.code, "parent_changed")
        self.assertFalse(caught.exception.created)
        self.assertNotIn(self.TARGET, self.api.objects)
        self.assertEqual(self.api.handles, {})

    def test_parent_reparse_change_fails_and_closes_without_lock(self):
        def change(typ, path, count):
            if typ == "directory" and path == self.PARENT and count == 2:
                replacement = self.api.obj(20, directory=True)
                replacement["attributes"] |= 0x400
                self.api.objects[path] = replacement
        self.api.on_open = change
        with self.assertRaises(win.WindowsPathGuardError):
            self.acquire()
        self.assertEqual(self.api.handles, {})
        self.assertEqual(self.api.locked, set())
        self.assertFalse(any(e[0] == "lock" for e in self.api.calls))

    def test_parent_group_create_permission_rejected_before_target(self):
        self.api.objects[self.PARENT]["unsafe_group"] = True
        with self.assertRaises(win.WindowsPathGuardError):
            self.acquire()
        self.assertFalse(any(e[0] == "lock" for e in self.api.calls))
        self.assertEqual(self.api.handles, {})

    def test_parent_acl_change_after_lock_releases_fd_and_records_created(self):
        self.api.objects.pop(self.TARGET)
        def change(typ, path, count):
            if typ == "directory" and path == self.PARENT and count == 3:
                self.api.objects[path]["control"] ^= 0x1000
        self.api.on_open = change
        with self.assertRaises(win.WindowsPathGuardError) as caught:
            self.acquire(create=True)
        self.assertTrue(caught.exception.created)
        self.assertIn(self.TARGET, self.api.objects)  # never delete created lock
        self.assertEqual(self.api.locked, set())
        self.assertEqual(len(self.crt.close_calls), 1)
        self.assertEqual(self.api.handles, {})

    def test_target_acl_change_on_reopen_rejects_and_releases_lock(self):
        def change(typ, path, count):
            if typ == "file" and path == self.TARGET and count == 2:
                self.api.objects[path]["control"] ^= 0x1000
        self.api.on_open = change
        with self.assertRaises(win.WindowsPathGuardError) as caught:
            self.acquire()
        self.assertEqual(caught.exception.code, "target_changed")
        self.assertEqual(self.api.locked, set())
        self.assertEqual(len(self.crt.close_calls), 1)
        self.assertEqual(self.api.handles, {})

    def test_parent_acl_change_during_target_reopen_fails_final_bracket(self):
        def change(typ, path, count):
            if typ == "file" and path == self.TARGET and count == 2:
                self.api.objects[self.PARENT]["control"] ^= 0x1000
        self.api.on_open = change
        with self.assertRaises(win.WindowsPathGuardError):
            self.acquire()
        self.assertEqual(self.api.handles, {})
        self.assertEqual(self.api.locked, set())
        self.assertEqual(len(self.crt.close_calls), 1)

    def test_target_path_replacement_after_lock_is_rejected(self):
        def change(typ, path, count):
            if typ == "file" and path == self.TARGET and count == 2:
                self.api.objects[path] = self.api.obj(30)
        self.api.on_open = change
        with self.assertRaises(win.WindowsPathGuardError) as caught:
            self.acquire()
        self.assertEqual(caught.exception.code, "target_changed")
        self.assertEqual(self.api.locked, set())
        self.assertEqual(len(self.crt.close_calls), 1)
        self.assertEqual(self.api.handles, {})

    def test_original_target_rechecked_after_new_handle_observation(self):
        guard = self.acquire()
        original_handle = self.crt.handle(guard.fileno())
        seen = {}
        def change_after_fresh(handle, kind):
            path, obj = self.api.handles[handle]
            if path == self.TARGET and handle != original_handle:
                seen[handle] = seen.get(handle, 0) + 1
                if seen[handle] == 2:
                    obj["control"] ^= 0x1000
        self.acl.on_read = change_after_fresh
        with self.assertRaises(win.WindowsPathGuardError):
            guard.revalidate()
        self.assertTrue(guard.closed)
        self.assertEqual(self.api.handles, {})
        self.assertEqual(self.api.locked, set())

    def test_revalidate_acl_failure_closes_lock_and_all_parent_handles(self):
        guard = self.acquire()
        self.api.objects[self.PARENT]["control"] ^= 0x1000
        with self.assertRaises(win.WindowsPathGuardError):
            guard.revalidate()
        self.assertTrue(guard.closed)
        self.assertEqual(self.api.handles, {})
        self.assertEqual(self.api.locked, set())
        guard.close()  # no second close attempt

    def test_fd_close_failure_is_unknown_and_never_retried(self):
        guard = self.acquire()
        self.crt.fail_close = True
        with self.assertRaises(win.WindowsPathGuardError) as caught:
            guard.close()
        self.assertEqual(caught.exception.code, "release_unknown")
        self.assertTrue(guard.closed)
        guard.close()
        self.assertEqual(len(self.crt.close_calls), 1)
        # Parent handles are still released; the failed fd is deliberately retained.
        self.assertEqual(len(self.api.handles), 1)

    def test_drive_change_before_lock_rejected_and_parents_released(self):
        def change(typ, path, count):
            if typ == "directory" and path == self.PARENT and count == 1:
                self.api.drive = 4
        self.api.on_open = change
        with self.assertRaises(win.WindowsPathGuardError) as caught:
            self.acquire()
        self.assertEqual(caught.exception.code, "remote_unsupported")
        self.assertFalse(any(e[0] == "lock" for e in self.api.calls))
        self.assertEqual(self.api.handles, {})


if __name__ == "__main__":
    unittest.main()
