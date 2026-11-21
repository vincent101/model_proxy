"""Stable snapshot primitives using fake configs in an isolated temp tree."""

import json
import os
import tempfile
import stat
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config_snapshot import ConfigSnapshotError, _marker, current_file_marker, read_stable_config


class StableConfigSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "fake-config.json"
        self.path.write_text('{"generation":"A","secret":"FAKE_SECRET"}', encoding="utf-8")

    def _replacement(self, text='{"generation":"B"}'):
        other = self.root / "replacement.json"
        other.write_text(text, encoding="utf-8")
        return other

    def test_snapshot_and_current_path_share_the_same_marker(self):
        result = read_stable_config(self.path)
        self.assertEqual(result.config["generation"], "A")
        self.assertEqual(result.marker, current_file_marker(self.path))

    def test_duplicate_keys_are_opt_in_and_fail_without_disclosing_data(self):
        cases = (
            b'{"days":{"2026-01-01":{"x":1}},"days":{},"secret":"FAKE_SECRET"}',
            b'{"outer":{"FAKE_PRIVATE_KEY":1,"FAKE_PRIVATE_KEY":2},"secret":"FAKE_SECRET"}',
            b'{"days":{},"d\\u0061ys":{},"secret":"FAKE_SECRET"}',
            b'{"outer":[{"x":1,"x":2}],"secret":"FAKE_SECRET"}',
        )
        for raw in cases:
            with self.subTest(case=cases.index(raw)):
                self.path.write_bytes(raw)
                inode = self.path.stat().st_ino
                # Existing business-config behavior remains last-key-wins.
                self.assertIsInstance(read_stable_config(self.path).config, dict)
                with self.assertRaises(ConfigSnapshotError) as caught:
                    read_stable_config(self.path, reject_duplicate_keys=True)
                self.assertEqual(caught.exception.code, "config_parse_error")
                self.assertNotIn("FAKE_SECRET", str(caught.exception))
                self.assertNotIn("FAKE_PRIVATE_KEY", str(caught.exception))
                self.assertNotIn(str(self.path), str(caught.exception))
                self.assertEqual(self.path.read_bytes(), raw)
                self.assertEqual(self.path.stat().st_ino, inode)

    def test_strict_mode_allows_same_key_in_distinct_objects(self):
        self.path.write_text('{"first":{"x":1},"second":{"x":2}}', encoding="utf-8")
        result = read_stable_config(self.path, reject_duplicate_keys=True)
        self.assertEqual(result.config, {"first": {"x": 1}, "second": {"x": 2}})

    def test_strict_flag_must_be_a_boolean_before_any_read(self):
        with patch("builtins.open") as opened:
            with self.assertRaises(TypeError):
                read_stable_config(self.path, reject_duplicate_keys="yes")
        opened.assert_not_called()

    def test_strict_mode_keeps_descriptor_replacement_detection(self):
        replacement = self._replacement()
        original_stat = os.stat
        calls = 0

        def replace_before_final_check(path, *args, **kwargs):
            nonlocal calls
            if os.fspath(path) == os.fspath(self.path):
                calls += 1
                if calls == 2:
                    os.replace(replacement, self.path)
            return original_stat(path, *args, **kwargs)

        with patch("core.config_snapshot.os.stat", side_effect=replace_before_final_check):
            with self.assertRaises(ConfigSnapshotError) as caught:
                read_stable_config(self.path, reject_duplicate_keys=True)
        self.assertEqual(caught.exception.code, "config_unstable")

    def test_windows_guarded_bytes_strict_mode_is_mock_only(self):
        from core.config_snapshot import _windows_snapshot
        raw = b'{"days":{"fake":1},"days":{},"secret":"FAKE_SECRET"}'
        guarded = SimpleNamespace(marker=(1, 2, 3, 4, len(raw)), data=raw)
        with patch("core.platform_windows.read_guarded_file", return_value=guarded) as read:
            result = _windows_snapshot(r"C:\fixture\config.json", with_data=True)
            self.assertEqual(result.config["days"], {})
            with self.assertRaises(ConfigSnapshotError) as caught:
                _windows_snapshot(r"C:\fixture\config.json", with_data=True,
                                  reject_duplicate_keys=True)
        self.assertEqual(caught.exception.code, "config_parse_error")
        self.assertNotIn("FAKE_SECRET", str(caught.exception))
        self.assertEqual(read.call_count, 2)

    def test_windows_public_reader_forwards_strict_flag_without_native_io(self):
        import core.config_snapshot as module
        sentinel = object()
        with patch.object(module.os, "name", "nt"), \
             patch.object(module, "_windows_snapshot", return_value=sentinel) as read:
            self.assertIs(read_stable_config(r"C:\fixture\config.json",
                                             reject_duplicate_keys=True), sentinel)
            # trusted_boundary 是后加的透传口（默认 None 也要显式传下去，
            # 否则 Windows 侧无法把配置读取限制在项目目录内）。
            read.assert_called_once_with(r"C:\fixture\config.json", with_data=True,
                                         reject_duplicate_keys=True, trusted_boundary=None)

    def test_windows_public_reader_forwards_trusted_boundary(self):
        import core.config_snapshot as module
        sentinel = object()
        boundary = r"C:\fixture"
        with patch.object(module.os, "name", "nt"), \
             patch.object(module, "_windows_snapshot", return_value=sentinel) as read:
            self.assertIs(read_stable_config(r"C:\fixture\config.json",
                                             trusted_boundary=boundary), sentinel)
            read.assert_called_once_with(r"C:\fixture\config.json", with_data=True,
                                         reject_duplicate_keys=False,
                                         trusted_boundary=boundary)

    def test_invalid_json_non_object_and_invalid_utf8_are_sanitized(self):
        for contents in (b'{"secret":"FAKE_SECRET",', b'[]', b'null', b'\xff'):
            with self.subTest(contents=contents):
                self.path.write_bytes(contents)
                with self.assertRaises(ConfigSnapshotError) as caught:
                    read_stable_config(self.path)
                self.assertEqual(caught.exception.code, "config_parse_error")
                self.assertNotIn("FAKE_SECRET", str(caught.exception))
                self.assertNotIn(str(self.path), str(caught.exception))

    def test_missing_and_permission_errors_do_not_leak_path(self):
        self.path.unlink()
        with self.assertRaises(ConfigSnapshotError) as caught:
            read_stable_config(self.path)
        self.assertEqual(caught.exception.code, "config_read_error")
        self.assertNotIn(str(self.path), str(caught.exception))
        with patch("builtins.open", side_effect=PermissionError("secret path")):
            with self.assertRaises(ConfigSnapshotError) as caught:
                read_stable_config(self.path)
        self.assertNotIn("secret path", str(caught.exception))

    def test_descriptor_change_during_read_is_not_committed(self):
        original_fstat = os.fstat
        calls = 0

        def change_before_second_fstat(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.path.write_text('{"generation":"changed in place with different length"}',
                                     encoding="utf-8")
            return original_fstat(fd)

        with patch("core.config_snapshot.os.fstat", side_effect=change_before_second_fstat):
            with self.assertRaises(ConfigSnapshotError) as caught:
                read_stable_config(self.path)
        self.assertEqual(caught.exception.code, "config_unstable")

    def test_replacement_before_first_path_check_is_not_accepted(self):
        replacement = self._replacement()
        original_stat = os.stat
        calls = 0

        def replace_before_check(path, *args, **kwargs):
            nonlocal calls
            if os.fspath(path) == os.fspath(self.path):
                calls += 1
                if calls == 1:
                    os.replace(replacement, self.path)
            return original_stat(path, *args, **kwargs)

        with patch("core.config_snapshot.os.stat", side_effect=replace_before_check):
            with self.assertRaises(ConfigSnapshotError) as caught:
                read_stable_config(self.path)
        self.assertEqual(caught.exception.code, "config_unstable")

    def test_replacement_before_final_path_check_is_not_accepted(self):
        replacement = self._replacement()
        original_stat = os.stat
        calls = 0

        def replace_before_final_check(path, *args, **kwargs):
            nonlocal calls
            if os.fspath(path) == os.fspath(self.path):
                calls += 1
                if calls == 2:
                    os.replace(replacement, self.path)
            return original_stat(path, *args, **kwargs)

        with patch("core.config_snapshot.os.stat", side_effect=replace_before_final_check):
            with self.assertRaises(ConfigSnapshotError) as caught:
                read_stable_config(self.path)
        self.assertEqual(caught.exception.code, "config_unstable")
        self.assertEqual(calls, 2)

    def test_replacement_after_snapshot_has_a_different_next_probe(self):
        result = read_stable_config(self.path)
        replacement = self._replacement()
        os.replace(replacement, self.path)
        self.assertEqual(result.config["generation"], "A")
        self.assertNotEqual(result.marker, current_file_marker(self.path))
        self.assertEqual(read_stable_config(self.path).config["generation"], "B")

    def test_atomic_replacement_with_equal_or_earlier_mtime_changes_marker(self):
        for delta in (0, -10_000_000_000):
            with self.subTest(delta=delta):
                self.path.write_text('{"generation":"A"}', encoding="utf-8")
                old = current_file_marker(self.path)
                replacement = self._replacement()
                os.utime(replacement, ns=(old.mtime_ns + delta, old.mtime_ns + delta))
                os.replace(replacement, self.path)
                new = current_file_marker(self.path)
                self.assertNotEqual(old.inode, new.inode)
                self.assertNotEqual(old, new)

    def test_missing_path_does_not_advance_any_existing_marker(self):
        old = read_stable_config(self.path)
        self.path.unlink()
        with self.assertRaises(ConfigSnapshotError):
            current_file_marker(self.path)
        self.assertEqual(old.config["generation"], "A")
        self.path.write_text('{"generation":"recovered"}', encoding="utf-8")
        self.assertNotEqual(old.marker, current_file_marker(self.path))

    def test_invalid_device_identity_is_not_trusted(self):
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_dev=-1, st_ino=1,
                               st_mtime_ns=1, st_ctime_ns=1, st_size=0)
        with self.assertRaises(ConfigSnapshotError) as caught:
            _marker(info)
        self.assertEqual(caught.exception.code, "file_identity_unavailable")

    def test_unknown_filesystem_rejects_real_read(self):
        from core.discovery import PreflightError
        with patch("core.config_snapshot.verify_local_filesystem_fd",
                   side_effect=PreflightError("filesystem_unverified", "fake")):
            for operation in (read_stable_config, current_file_marker):
                with self.assertRaises(ConfigSnapshotError) as caught:
                    operation(self.path)
                self.assertEqual(caught.exception.code, "filesystem_unverified")

    def test_unsupported_platform_does_not_open(self):
        with patch("core.config_snapshot.os.name", "unsupported"), patch("builtins.open") as opened:
            with self.assertRaises(ConfigSnapshotError) as caught:
                read_stable_config(self.path)
        opened.assert_not_called()
        self.assertEqual(caught.exception.code, "file_identity_unavailable")

    def test_real_snapshot_needs_no_mocked_filesystem_gate(self):
        self.assertEqual(read_stable_config(self.path).config["generation"], "A")


if __name__ == "__main__":
    unittest.main()
