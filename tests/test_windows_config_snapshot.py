"""Synthetic Win32 configuration reads; no real paths, services, or network."""
import ctypes
import os
import unittest
from unittest.mock import Mock, patch

import core.config_snapshot as config
import core.discovery as discovery
import core.platform_windows as win
import core.windows_acl as acl
from core.windows_paths import WindowsPathBackend
from tests.test_windows_adapter import VirtualACL
from tests.test_windows_discovery import PathWinBridge, VirtualFS


class ConfigFS(VirtualFS):
    def __init__(self):
        super().__init__()
        self.reads = []
        self.on_read = None
    def open_read_file(self, path): return self._open(path, "read")
    def file_marker(self, handle):
        _, obj = self.handles[handle]
        return (obj["volume"], obj["index"], obj.get("write", 1000),
                obj.get("change", 2000), len(obj.get("data", b"")))
    def read_all(self, handle, *, max_bytes=None):
        self.reads.append(handle)
        _, obj = self.handles[handle]
        data = obj.get("data", b"")
        if self.on_read: self.on_read(handle, obj)
        if max_bytes is not None and len(data) > max_bytes:
            raise OSError("fake limit")
        return data


class WindowsConfigSnapshotTests(unittest.TestCase):
    PATH = r"C:\fixture\project\config.json"
    def setUp(self):
        self.fs = ConfigFS()
        self.fs.objects[self.PATH] = dict(self.fs.obj(801), data=b'{"generation":"A"}')
        self.acl = VirtualACL(self.fs)
        self.paths = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        patches = (
            patch.object(os, "name", "nt"),
            patch.object(discovery, "_windows_path_backend", return_value=self.paths),
            patch.object(win, "_api", side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(acl, "_NativeBackend", return_value=self.acl),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_success_same_handle_change_time_and_no_reads_for_marker(self):
        value = config.read_stable_config(self.PATH)
        self.assertEqual(value.config, {"generation": "A"})
        self.assertEqual(value.marker, config.FileMarker(17, 801, 1000, 2000, len(b'{"generation":"A"}')))
        self.assertEqual(len(self.fs.reads), 1)
        self.assertFalse(self.fs.handles)
        self.fs.reads.clear()
        self.assertEqual(config.current_file_marker(self.PATH), value.marker)
        self.assertEqual(self.fs.reads, [])
        self.assertFalse(self.fs.handles)

    def test_in_place_change_during_read_fails_without_committing_data(self):
        def change(handle, obj):
            obj["data"] = b'{"generation":"B and larger"}'
            obj["change"] = 3000
        self.fs.on_read = change
        with self.assertRaises(config.ConfigSnapshotError) as caught:
            config.read_stable_config(self.PATH)
        self.assertEqual(caught.exception.code, "config_unstable")
        self.assertFalse(self.fs.handles)

    def test_path_replacement_during_read_fails(self):
        def replace_target(handle, obj):
            self.fs.objects[self.PATH] = dict(self.fs.obj(802), data=b'{"generation":"B"}')
        self.fs.on_read = replace_target
        with self.assertRaises(config.ConfigSnapshotError) as caught:
            config.read_stable_config(self.PATH)
        self.assertEqual(caught.exception.code, "config_unstable")
        self.assertFalse(self.fs.handles)

    def test_invalid_json_is_sanitized_and_all_handles_close(self):
        self.fs.objects[self.PATH]["data"] = b'{"fake secret":'
        with self.assertRaises(config.ConfigSnapshotError) as caught:
            config.read_stable_config(self.PATH)
        self.assertEqual(caught.exception.code, "config_parse_error")
        self.assertNotIn("fake secret", str(caught.exception))
        self.assertFalse(self.fs.handles)

    def test_reparse_or_acl_failure_rejects_before_data_read(self):
        self.fs.objects[self.PATH]["attributes"] = 0x400
        with self.assertRaises(config.ConfigSnapshotError):
            config.read_stable_config(self.PATH)
        self.assertEqual(self.fs.reads, [])
        self.assertFalse(self.fs.handles)
        self.fs.objects[self.PATH]["attributes"] = 0
        self.fs.objects[self.PATH]["unsafe_group"] = True
        with self.assertRaises(config.ConfigSnapshotError):
            config.read_stable_config(self.PATH)
        self.assertEqual(self.fs.reads, [])
        self.assertFalse(self.fs.handles)

    def test_unknown_release_is_reported_as_filesystem_unverified(self):
        def fail_read_close(typ, path, count):
            if typ == "read" and path == self.PATH and count == 2:
                self.fs.close_fail.add(self.fs.next_handle + 1)
        self.fs.on_open = fail_read_close
        with self.assertRaises(config.ConfigSnapshotError) as caught:
            config.read_stable_config(self.PATH)
        self.assertEqual(caught.exception.code, "filesystem_unverified")
        failed = next(iter(self.fs.close_fail))
        self.assertEqual(self.fs.close_calls.count(failed), 1)


class NativeMarkerTests(unittest.TestCase):
    def test_basic_info_change_time_and_file_id_are_from_one_handle(self):
        class Kernel:
            def GetFileInformationByHandle(self, handle, output):
                value = ctypes.cast(output, ctypes.POINTER(win.BY_HANDLE_FILE_INFORMATION)).contents
                value.volume_serial, value.index_low, value.links, value.size_low = 17, 80, 1, 12
                return 1
            def GetFileInformationByHandleEx(self, handle, kind, output, length):
                self.args = (handle, kind, length)
                value = ctypes.cast(output, ctypes.POINTER(win.FILE_BASIC_INFO)).contents
                value.write, value.change, value.attributes = 111, 222, 0
                return 1
        kernel = Kernel()
        api = win._NativeAPI.__new__(win._NativeAPI)
        api._kernel = kernel
        self.assertEqual(api.file_marker(42), (17, 80, 11100, 22200, 12))
        self.assertEqual(kernel.args, (42, 0, ctypes.sizeof(win.FILE_BASIC_INFO)))


class WindowsLegacyACLAdapterTests(unittest.TestCase):
    def test_fd_uses_same_native_handle_and_rejects_reparse(self):
        from tests.test_windows_adapter import FakeCRT, FakeKernel
        from tests.test_windows_acl import FakeBackend, valid_observation
        from dataclasses import replace
        kernel = FakeKernel()
        api = win._NativeAPI.__new__(win._NativeAPI)
        api._kernel = kernel
        crt = FakeCRT()
        crt.mapping[77] = 100
        rights = acl.FILE_GENERIC_READ | acl.FILE_GENERIC_WRITE
        item = replace(valid_observation(), identity=(17, (1 << 32) | 23, 1, 0),
                       desired=rights, granted=rights)
        backend = FakeBackend(item)
        with patch.object(os, "name", "nt"), patch.object(win, "_NativeFDAPI", return_value=crt), \
             patch.object(win, "_api", return_value=api), patch.object(acl, "_NativeBackend", return_value=backend):
            first = discovery.verify_no_extended_acl(77)
            second = discovery.verify_no_extended_acl(77)
        self.assertEqual(first, second)
        self.assertTrue(all(call[0] == 100 for call in backend.handles))
        self.assertEqual(crt.close_calls, [])
        bad = replace(item, identity=(17, (1 << 32) | 23, 1, 0x400))
        with patch.object(os, "name", "nt"), patch.object(win, "_NativeFDAPI", return_value=crt), \
             patch.object(win, "_api", return_value=api), patch.object(acl, "_NativeBackend", return_value=FakeBackend(bad)):
            with self.assertRaises(discovery.PreflightError):
                discovery.verify_no_extended_acl(77)
        self.assertEqual(crt.close_calls, [])


if __name__ == "__main__":
    unittest.main()
