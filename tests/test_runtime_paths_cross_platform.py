"""Offline parser contracts; legacy defaults are never opened or stat'ed."""

import json
import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest.mock import patch

from core.runtime_paths import (
    DEFAULTS,
    LEGACY_DEFAULTS,
    PATH_KEYS,
    RuntimePathsError,
    parse_runtime_paths,
    read_runtime_paths,
)


def explicit_values():
    return {key: f"runtime/{key}" for key in PATH_KEYS}


class RuntimePathParserTests(unittest.TestCase):
    def test_relative_values_use_project_root_without_filesystem_access(self):
        raw = explicit_values()
        raw["ignored"] = {"unexpected": "ignored"}
        with patch("os.stat", side_effect=AssertionError("target stat forbidden")), \
             patch("os.lstat", side_effect=AssertionError("target lstat forbidden")), \
             patch("builtins.open", side_effect=AssertionError("target open forbidden")):
            paths = parse_runtime_paths(raw, project_root="/isolated/project", platform="posix")
        self.assertEqual(paths["log"], PurePosixPath("/isolated/project/runtime/log"))
        self.assertEqual(set(paths), set(PATH_KEYS))

    def test_each_single_missing_field_uses_only_its_new_default(self):
        for missing in PATH_KEYS:
            raw = explicit_values()
            del raw[missing]
            # Merely constructing/comparing PurePath strings is intentional.
            with self.subTest(missing=missing), \
                 patch("os.stat", side_effect=AssertionError("target stat forbidden")), \
                 patch("os.lstat", side_effect=AssertionError("target lstat forbidden")), \
                 patch("builtins.open", side_effect=AssertionError("target open forbidden")):
                paths = parse_runtime_paths(raw, project_root="/isolated/project", platform="posix")
                value = PurePosixPath(DEFAULTS[missing])
                expected = value if value.is_absolute() else PurePosixPath("/isolated/project") / value
                self.assertEqual(paths[missing], expected)
                for key in set(PATH_KEYS) - {missing}:
                    self.assertEqual(paths[key], PurePosixPath("/isolated/project/runtime") / key)

    def test_more_than_one_missing_is_not_silently_approved(self):
        raw = explicit_values()
        del raw["pid"]
        del raw["lock"]
        with self.assertRaises(RuntimePathsError) as caught:
            parse_runtime_paths(raw, project_root="/isolated/project", platform="posix")
        self.assertEqual(caught.exception.code, "multiple_missing")

    def test_explicit_bad_values_are_not_defaults(self):
        for bad in (None, 0, False, [], {}, "", " \t", "\0"):
            with self.subTest(bad=bad):
                raw = explicit_values()
                raw["lock"] = bad
                with self.assertRaises(RuntimePathsError):
                    parse_runtime_paths(raw, project_root="/isolated/project", platform="posix")

    def test_injected_defaults_can_keep_real_os_tests_in_the_fixture(self):
        raw = explicit_values()
        del raw["lock"]
        injected = {key: f"safe/{key}" for key in PATH_KEYS}
        paths = parse_runtime_paths(raw, project_root="/isolated/project", platform="posix",
                                    defaults=injected)
        self.assertEqual(paths["lock"], PurePosixPath("/isolated/project/safe/lock"))

    def test_windows_maps_only_four_exact_legacy_values(self):
        raw = {key: LEGACY_DEFAULTS[key] for key in PATH_KEYS}
        with patch("os.stat", side_effect=AssertionError("target stat forbidden")), \
             patch("os.lstat", side_effect=AssertionError("target lstat forbidden")), \
             patch("builtins.open", side_effect=AssertionError("target open forbidden")):
            paths = parse_runtime_paths(raw, project_root=r"C:\isolated\工程",
                                        platform="windows", local_appdata=r"C:\fake home\Local")
        self.assertEqual(paths["log"], PureWindowsPath(r"C:\isolated\工程\.model_proxy.log"))
        self.assertEqual(paths["totals"], PureWindowsPath(r"C:\isolated\工程\.model_proxy_totals.json"))
        for key in ("lock", "pid", "ensure_log", "start_lock"):
            self.assertEqual(paths[key], PureWindowsPath(r"C:\fake home\Local\model_proxy\runtime") /
                             PurePosixPath(LEGACY_DEFAULTS[key]).name)

    def test_windows_custom_paths_and_unc_are_not_reinterpreted_as_legacy(self):
        raw = explicit_values()
        raw["lock"] = r"D:\my dir\lock"
        raw["totals"] = r"\\server\share\stats.json"
        paths = parse_runtime_paths(raw, project_root=r"C:\isolated\project", platform="windows")
        self.assertEqual(paths["lock"], PureWindowsPath(r"D:\my dir\lock"))
        self.assertEqual(paths["totals"], PureWindowsPath(r"\\server\share\stats.json"))
        raw["lock"] = "/tmp/custom.lock"
        with self.assertRaises(RuntimePathsError):
            parse_runtime_paths(raw, project_root=r"C:\isolated\project", platform="windows")

    def test_windows_unavailable_localappdata_is_not_guessed(self):
        raw = explicit_values()
        raw["lock"] = LEGACY_DEFAULTS["lock"]
        for value in (None, "", "relative"):
            with self.subTest(local_appdata=value), self.assertRaises(RuntimePathsError):
                parse_runtime_paths(raw, project_root=r"C:\isolated\project", platform="windows",
                                    local_appdata=value)

    def test_windows_rejects_ambiguous_or_unusable_paths(self):
        for value in (r"C:relative", r"\root-relative", r"\\?\C:\device", r"relative\NUL.txt",
                      "relative\\trailing.", "relative\\bad?name"):
            with self.subTest(value=value):
                raw = explicit_values()
                raw["log"] = value
                with self.assertRaises(RuntimePathsError):
                    parse_runtime_paths(raw, project_root=r"C:\isolated\project", platform="windows")

    def test_root_must_be_absolute(self):
        for platform, root in (("posix", "relative"), ("windows", r"C:relative")):
            with self.subTest(platform=platform), self.assertRaises(RuntimePathsError):
                parse_runtime_paths(explicit_values(), project_root=root, platform=platform)


class RuntimePathFileTests(unittest.TestCase):
    def test_file_failures_never_fall_back_to_production_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime_paths.json"
            for content in (None, "{", "[]", "null", "\udcff"):
                with self.subTest(content=repr(content)):
                    if path.exists():
                        path.unlink()
                    if content == "\udcff":
                        path.write_bytes(b"\xff")
                    elif content is not None:
                        path.write_text(content, encoding="utf-8")
                    with self.assertRaises(RuntimePathsError):
                        read_runtime_paths(path, project_root=root, platform="posix")

    def test_deeply_malformed_json_is_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime_paths.json"
            path.write_text("{}", encoding="utf-8")
            with patch("core.runtime_paths.json.load", side_effect=RecursionError("FAKE_SECRET")):
                with self.assertRaises(RuntimePathsError) as caught:
                    read_runtime_paths(path, project_root=root, platform="posix")
            self.assertEqual(caught.exception.code, "unreadable_file")
            self.assertNotIn("FAKE_SECRET", str(caught.exception))

    def test_valid_file_is_parsed_with_injected_fixture_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime_paths.json"
            raw = explicit_values()
            del raw["pid"]
            path.write_text(json.dumps(raw), encoding="utf-8")
            paths = read_runtime_paths(path, project_root=root, platform="posix",
                                       defaults={key: f"fake/{key}" for key in PATH_KEYS})
            self.assertEqual(paths["pid"], PurePosixPath(root) / "fake/pid")


if __name__ == "__main__":
    unittest.main()
