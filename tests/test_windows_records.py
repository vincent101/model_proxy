"""Windows protected record reads using the synthetic in-memory backend."""
import ctypes
import json
import os
import threading
import unittest
from unittest.mock import patch

import core.discovery as discovery
import core.platform_windows as win
import core.windows_acl as acl
from core.windows_paths import WindowsPathBackend
from tests.test_windows_adapter import VirtualACL
from tests.test_windows_config_snapshot import ConfigFS
from tests.test_windows_discovery import PathWinBridge


class WindowsRecordReadTests(unittest.TestCase):
    ROOT = r"C:\fixture\project"
    STATE = r"C:\fixture\project\.runtime"

    def setUp(self):
        self.fs = ConfigFS()
        self.acl = VirtualACL(self.fs)
        self.backend = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        for item in (
            patch.object(os, "name", "nt"),
            patch.object(discovery, "_windows_path_backend", return_value=self.backend),
            patch.object(win, "_api", side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(acl, "_NativeBackend", return_value=self.acl),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.install = {"schema": discovery.INSTANCE_SCHEMA, "record": "install", "install_id": "a" * 32,
                        "project_root": self.ROOT, "root_identity": {"device": 17, "inode": 11},
                        "ever_started": False, "last_instance_nonce": None,
                        "initialization_source": "fake fixture"}

    def place(self, name, obj):
        path = self.STATE + "\\" + name
        self.fs.objects[path] = dict(self.fs.obj(901), data=json.dumps(obj).encode())
        return path

    def test_absent_records_are_not_created_and_root_identity_is_bound(self):
        self.assertEqual(discovery.verify_record_directory(self.STATE), (17, 11))
        self.assertIsNone(discovery.read_instance_record(self.STATE, "install.json"))
        self.assertIsNone(discovery.read_instance_set(self.STATE))
        self.assertFalse(self.fs.handles)
        self.assertNotIn(self.STATE + r"\install.json", self.fs.objects)

    def test_private_install_record_reads_and_set_consistency(self):
        self.place("install.json", self.install)
        self.assertEqual(discovery.read_instance_record(self.STATE, "install.json"), self.install)
        result = discovery.read_instance_set(self.STATE)
        self.assertEqual(result, {"install": self.install, "active": None, "last_good_paths": None})
        self.assertFalse(self.fs.handles)

    def test_bad_record_never_becomes_missing_or_overwritten(self):
        path = self.STATE + r"\install.json"
        self.fs.objects[path] = dict(self.fs.obj(901), data=b'{"fake secret":')
        with self.assertRaises(discovery.PreflightError) as caught:
            discovery.read_instance_record(self.STATE, "install.json")
        self.assertEqual(caught.exception.code, "record_unavailable")
        self.assertNotIn("fake secret", str(caught.exception))
        self.assertEqual(self.fs.objects[path]["data"], b'{"fake secret":')
        self.assertFalse(self.fs.handles)

    def test_wrong_install_identity_and_unsafe_acl_fail_closed(self):
        bad = dict(self.install, root_identity={"device": 17, "inode": 999})
        path = self.place("install.json", bad)
        with self.assertRaises(discovery.PreflightError):
            discovery.read_instance_set(self.STATE)
        self.fs.objects[path]["data"] = json.dumps(self.install).encode()
        self.fs.objects[path]["unsafe_group"] = True
        with self.assertRaises(discovery.PreflightError):
            discovery.read_instance_record(self.STATE, "install.json")
        self.assertFalse(self.fs.handles)

    def test_reparse_state_or_changed_record_refused_without_write(self):
        path = self.place("install.json", self.install)
        self.fs.objects[self.STATE]["attributes"] |= 0x400
        with self.assertRaises(discovery.PreflightError):
            discovery.read_instance_record(self.STATE, "install.json")
        self.fs.objects[self.STATE]["attributes"] &= ~0x400
        def change(handle, obj):
            if self.fs.handles[handle][0] == path:
                obj["data"] += b" "
                obj["change"] = 3000
        self.fs.on_read = change
        with self.assertRaises(discovery.PreflightError):
            discovery.read_instance_record(self.STATE, "install.json")
        self.assertFalse(self.fs.handles)


class WindowsPrivateTemporaryWriteTests(unittest.TestCase):
    def setUp(self):
        from dataclasses import replace
        from tests.test_windows_acl import FakeBackend, valid_observation
        self.fs = ConfigFS()
        self.path = WindowsRecordReadTests.STATE + r"\.record-fake"
        self.fs.objects[self.path] = dict(self.fs.obj(950), data=b"")
        self.acl = VirtualACL(self.fs)
        rights = acl.FILE_GENERIC_READ | acl.FILE_GENERIC_WRITE
        self.good = replace(valid_observation(), identity=(17, 950, 1, 0),
                            desired=rights, granted=rights)
        self.backend_class = FakeBackend
        self.writes = []
        def write(handle, data):
            self.writes.append((handle, data))
            self.fs.handles[handle][1]["data"] = data
        self.fs.write_all_and_flush = write

    def owned(self):
        handle = self.fs._open(self.path, "file")
        return win.WindowsFileHandle(handle, self.fs, created=True)

    def test_private_acl_verified_before_write_and_handle_stays_owned(self):
        owned = self.owned()
        proof = win.write_private_new_file(owned, b"fake record", acl_backend=self.backend_class(self.good))
        self.assertEqual(proof.marker[-1], len(b"fake record"))
        self.assertEqual(self.writes, [(owned.handle, b"fake record")])
        self.assertFalse(owned.closed)
        owned.close()
        self.assertFalse(self.fs.handles)

    def test_acl_denial_closes_without_writing_and_retains_empty_file(self):
        from dataclasses import replace
        denied = replace(self.good, access_granted=False, granted=0)
        owned = self.owned()
        with self.assertRaises(win.WindowsTemporaryWriteError) as caught:
            win.write_private_new_file(owned, b"fake record", acl_backend=self.backend_class(denied))
        self.assertFalse(caught.exception.content_may_exist)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.fs.objects[self.path]["data"], b"")
        self.assertTrue(owned.closed)

    def test_concurrent_close_waits_for_write_completion(self):
        owned = self.owned()
        entered, release, closed = (threading.Event() for _ in range(3))
        errors = []
        def slow_write(handle, data):
            entered.set()
            if not release.wait(2):
                raise OSError("fake timeout")
            self.fs.handles[handle][1]["data"] = data
        self.fs.write_all_and_flush = slow_write
        def write():
            try: win.write_private_new_file(owned, b"fake", acl_backend=self.backend_class(self.good))
            except Exception as exc: errors.append(exc)
        def close():
            try: owned.close()
            except Exception as exc: errors.append(exc)
            finally: closed.set()
        first = threading.Thread(target=write)
        second = threading.Thread(target=close)
        try:
            first.start()
            self.assertTrue(entered.wait(1))
            second.start()
            self.assertFalse(closed.wait(0.05))
        finally:
            release.set()
            first.join(2)
            if second.ident is not None: second.join(2)
        self.assertEqual(errors, [])
        self.assertTrue(closed.is_set())
        self.assertFalse(self.fs.handles)

    def test_unknown_close_invalidates_owner_without_a_retry(self):
        owned = self.owned()
        raw = owned.handle
        self.fs.close_fail.add(raw)
        def partial(handle, data):
            self.fs.handles[handle][1]["data"] = data[:2]
            raise OSError("fake failure")
        self.fs.write_all_and_flush = partial
        with self.assertRaises(win.WindowsTemporaryWriteError) as caught:
            win.write_private_new_file(owned, b"fake", acl_backend=self.backend_class(self.good))
        self.assertEqual(caught.exception.code, "release_unknown")
        self.assertTrue(owned.closed)
        owned.close()
        self.assertEqual(self.fs.close_calls.count(raw), 1)

    def test_partial_write_failure_is_reported_and_never_deleted(self):
        owned = self.owned()
        def partial(handle, data):
            self.fs.handles[handle][1]["data"] = data[:2]
            raise OSError("fake failure")
        self.fs.write_all_and_flush = partial
        with self.assertRaises(win.WindowsTemporaryWriteError) as caught:
            win.write_private_new_file(owned, b"fake record", acl_backend=self.backend_class(self.good))
        self.assertTrue(caught.exception.created)
        self.assertTrue(caught.exception.content_may_exist)
        self.assertEqual(self.fs.objects[self.path]["data"], b"fa")
        self.assertTrue(owned.closed)


class WindowsNativeWriteContractTests(unittest.TestCase):
    def test_partial_native_writes_are_completed_and_flushed(self):
        class Kernel:
            def __init__(self):
                self.data = bytearray()
                self.flushes = []
            def WriteFile(self, handle, buffer, length, written, overlapped):
                amount = min(length, 2)
                self.data.extend(ctypes.string_at(buffer, amount))
                ctypes.cast(written, ctypes.POINTER(win.DWORD))[0] = amount
                return 1
            def FlushFileBuffers(self, handle):
                self.flushes.append(handle)
                return 1
        kernel = Kernel()
        api = win._NativeAPI.__new__(win._NativeAPI)
        api._kernel = kernel
        api.write_all_and_flush(42, b"fake record")
        self.assertEqual(bytes(kernel.data), b"fake record")
        self.assertEqual(kernel.flushes, [42])

    def test_native_replace_and_new_move_never_use_unsafe_flags(self):
        class Kernel:
            def __init__(self): self.calls = []
            def ReplaceFileW(self, target, source, backup, flags, exclude, reserved):
                self.calls.append(("replace", target, source, backup, flags, exclude, reserved))
                return 1
            def MoveFileExW(self, source, target, flags):
                self.calls.append(("move", source, target, flags))
                return 1
        kernel = Kernel()
        api = win._NativeAPI.__new__(win._NativeAPI)
        api._kernel = kernel
        api.replace_existing(r"C:\fake\target", r"C:\fake\temp")
        api.move_new(r"C:\fake\temp", r"C:\fake\target")
        self.assertEqual(kernel.calls, [("replace", r"C:\fake\target", r"C:\fake\temp", None, 0, None, None),
                                        ("move", r"C:\fake\temp", r"C:\fake\target", 0)])

    def test_native_replace_retains_winerror_for_failure_classification(self):
        class Kernel:
            def ReplaceFileW(self, *args): return 0
        api = win._NativeAPI.__new__(win._NativeAPI)
        api._kernel = Kernel()
        for code in (1175, 1176, 1177, 5):
            with self.subTest(code=code), patch.object(ctypes, "get_last_error", return_value=code, create=True):
                with self.assertRaises(OSError) as caught:
                    api.replace_existing(r"C:\fake\target", r"C:\fake\temp")
                self.assertEqual(caught.exception.winerror, code)
                self.assertNotIn("fake", str(caught.exception))

    def test_native_flush_failure_is_not_reported_as_success(self):
        class Kernel:
            def WriteFile(self, handle, buffer, length, written, overlapped):
                ctypes.cast(written, ctypes.POINTER(win.DWORD))[0] = length
                return 1
            def FlushFileBuffers(self, handle): return 0
        api = win._NativeAPI.__new__(win._NativeAPI)
        api._kernel = Kernel()
        with patch.object(ctypes, "get_last_error", return_value=5, create=True):
            with self.assertRaises(OSError) as caught:
                api.write_all_and_flush(42, b"fake")
        self.assertEqual(caught.exception.winerror, 5)


if __name__ == "__main__":
    unittest.main()
