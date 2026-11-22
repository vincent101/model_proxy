"""Read-only path contracts using exclusively temporary fake projects."""
import os
import ntpath
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.discovery import (PreflightError, inspect_runtime_identities,
                            _windows_trusted_boundary, windows_initial_missing_parents)
from core.runtime_paths import DEFAULTS, LEGACY_DEFAULTS, PATH_KEYS, parse_runtime_paths


class UnifiedPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mp-path-unify-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.paths = parse_runtime_paths(dict(DEFAULTS), project_root=self.root, platform="posix")

    def test_fixed_anchor_and_shared_container_before_and_after_creation(self):
        first = inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(first.as_dict()["anchor.lock"].canonical,
                         str(self.root / ".runtime" / "anchor.lock"))
        self.assertFalse(first.as_dict()["state_dir"].exists)
        (self.root / ".runtime").mkdir(mode=0o700)
        second = inspect_runtime_identities(self.root, self.paths)
        self.assertTrue(second.as_dict()["state_dir"].exists)
        self.assertEqual(first.as_dict()["anchor.lock"].canonical,
                         second.as_dict()["anchor.lock"].canonical)

    def test_custom_paths_do_not_move_anchor(self):
        for index, key in enumerate(PATH_KEYS):
            self.paths[key] = self.root / ("external-" + str(index))
        snapshot = inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(snapshot.as_dict()["anchor.lock"].canonical,
                         str(self.root / ".runtime" / "anchor.lock"))

    def test_roles_fixed_files_and_auxiliary_directories_cannot_overlap(self):
        for value in (".runtime", ".runtime/anchor.lock", ".runtime/tmp",
                      ".runtime/tmp/marker", ".runtime/backups/backup",
                      ".runtime/model_proxy.log", ".runtime/MODEL_PROXY.LOG"):
            with self.subTest(value=value):
                paths = dict(self.paths)
                paths["totals"] = self.root / value
                with self.assertRaises(PreflightError):
                    inspect_runtime_identities(self.root, paths)

    def test_existing_hardlink_collision_is_rejected(self):
        state = self.root / ".runtime"
        state.mkdir(mode=0o700)
        Path(self.paths["log"]).write_text("fake", encoding="utf-8")
        os.link(self.paths["log"], self.paths["totals"])
        with self.assertRaises(PreflightError):
            inspect_runtime_identities(self.root, self.paths)

    def test_unresolved_unicode_alias_remains_unknown(self):
        paths = dict(self.paths)
        paths["log"] = self.root / ".runtime" / "资料-a"
        paths["totals"] = self.root / ".runtime" / "资料-b"
        with self.assertRaises(PreflightError):
            inspect_runtime_identities(self.root, paths)

    def test_auxiliary_directory_creation_does_not_change_lock_snapshot(self):
        state = self.root / ".runtime"
        state.mkdir(mode=0o700)
        before = inspect_runtime_identities(self.root, self.paths)
        (state / "tmp").mkdir(mode=0o700)
        after = inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(before, after)


class FirstConfigMarkerTests(unittest.TestCase):
    """Fake Windows backend and helper transport; no process or service."""

    @unittest.skipUnless(sys.platform == "win32",
                         "_windows_marker_scope 依赖 Windows 原生锁/ACL/租约证据；"
                         "POSIX 上 acquire_lock 要求显式 access_verifier，且 "
                         "write_preflight 需要 nofollow/xattr 证据，二者都无法伪造")
    def test_first_windows_write_preflights_then_prepares_fixed_marker_root(self):
        from core.cli import dispatch
        from core import discovery, platform_windows, server, windows_secure_files
        root = r"C:\fixture\project"
        config = root + r"\config\fake.json"
        paths = {key: root + r"\.runtime\\" + key for key in PATH_KEYS}
        proof = SimpleNamespace(created=True, identity=(17, 12, 1, 16), protection=("private",))
        first = Mock(exists=False)
        second = Mock(exists=True, contents=b"started")
        capture = Mock(side_effect=[first, second])
        first.__enter__ = Mock(return_value=first)
        first.__exit__ = Mock(return_value=False)
        second.__enter__ = Mock(return_value=second)
        second.__exit__ = Mock(return_value=False)
        sequence = Mock()
        sequence.attach_mock(Mock(return_value=paths), "read")
        sequence.attach_mock(Mock(), "config_check")
        sequence.attach_mock(Mock(), "prepare")
        sequence.attach_mock(Mock(return_value=proof), "ensure")
        with patch.object(dispatch, "read_runtime_paths", sequence.read), \
             patch.object(server, "_windows_check_config_targets", sequence.config_check), \
             patch.object(discovery, "prepare_windows_runtime_directories", sequence.prepare), \
             patch.object(platform_windows, "ensure_private_directory", sequence.ensure), \
             patch.object(dispatch, "inspect_runtime_identities"), \
             patch.object(windows_secure_files, "capture_target", capture), \
             patch.object(windows_secure_files, "write_target",
                          return_value=SimpleNamespace(state="VERIFIED")), \
             patch.object(dispatch, "_cleanup_windows_marker") as cleanup:
            with dispatch._windows_marker_scope(root, config) as holder:
                self.assertTrue(holder.path.startswith(root + r"\.runtime\tmp\model-proxy-cli-"))
                self.assertTrue(holder.path.endswith(r"\reload"))
                holder.cleanup_ok = True
        methods = [call[0] for call in sequence.mock_calls]
        self.assertLess(methods.index("config_check"), methods.index("prepare"))
        self.assertLess(methods.index("prepare"), methods.index("ensure"))
        self.assertEqual(sequence.ensure.call_args_list[0].args[0], root + r"\.runtime\tmp")
        self.assertTrue(all(call.kwargs["trusted_boundary"] == root for call in capture.call_args_list))
        cleanup.assert_called_once()

    def test_helper_accepts_only_the_fixed_project_tmp(self):
        from core.cli import _config_ops as ops
        from core import windows_secure_files
        root = r"C:\fixture\project"
        marker = root + r"\.runtime\tmp\model-proxy-cli-" + "a" * 32 + r"\reload"
        snap = Mock(exists=True, contents=b"started")
        fake_os = SimpleNamespace(name="nt", path=ntpath,
                                  environ={"CONFIG_OPS_RELOAD_MARKER": marker})
        fake_file = Mock()
        fake_file.resolve.return_value.parents = [None, None, root]
        with patch.object(ops, "os", fake_os), patch("pathlib.Path", return_value=fake_file), \
             patch.object(ops, "_MARKER_BASELINE", None), \
             patch.object(windows_secure_files, "capture_target", return_value=snap) as capture:
            ops._precheck_inherited_marker()
            self.assertEqual(capture.call_args.kwargs["trusted_boundary"], root)
            fake_os.environ["CONFIG_OPS_RELOAD_MARKER"] = (
                r"C:\elsewhere\model-proxy-cli-" + "a" * 32 + r"\reload")
            with self.assertRaises(ops.MarkerSafetyError):
                ops._precheck_inherited_marker()

    def test_mac_first_write_keeps_anonymous_pipe_path(self):
        from core.cli import dispatch
        with patch.object(dispatch.sys, "platform", "darwin"), \
             patch.object(dispatch.os.path, "isfile", return_value=True), \
             patch.object(dispatch, "_run_mac_write_helper", return_value=0) as helper, \
             patch.object(dispatch, "_windows_marker_scope") as marker:
            self.assertEqual(dispatch.run_helper("route-add", [], "fake-config", Mock(), root="fake-root"), 0)
        helper.assert_called_once()
        marker.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "requires native Darwin protections")
    def test_mac_first_write_prepares_private_tmp_without_service(self):
        from core.cli import dispatch
        from core.mac_config_path import capture_config
        with tempfile.TemporaryDirectory(prefix="mp-first-config-") as directory:
            root = Path(directory).resolve()
            config_dir = root / "config"
            config_dir.mkdir(mode=0o700)
            runtime_file = config_dir / "runtime_paths.json"
            runtime_file.write_text(json.dumps(DEFAULTS), encoding="utf-8")
            runtime_file.chmod(0o600)
            business = config_dir / "model_proxy_config.json"
            business.write_text('{"strategies":[],"routes":[]}', encoding="utf-8")
            business.chmod(0o600)
            snapshot = capture_config(str(business))
            baseline = dispatch._config_write_evidence(root, snapshot, "switch")
            self.assertFalse((root / ".runtime").exists())
            final = dispatch._prepare_mac_config_runtime(root, snapshot, "switch", baseline)
            self.assertTrue((root / ".runtime" / "tmp").is_dir())
            self.assertEqual((root / ".runtime" / "tmp").stat().st_mode & 0o077, 0)
            self.assertTrue(final[1][0].as_dict()["state_dir"].exists)

    @unittest.skipUnless(sys.platform == "darwin", "requires native Darwin protections")
    def test_mac_first_write_refuses_new_runtime_target_during_preparation(self):
        from core.cli import dispatch
        from core.mac_config_path import capture_config
        with tempfile.TemporaryDirectory(prefix="mp-first-race-") as directory:
            root = Path(directory).resolve()
            config_dir = root / "config"
            config_dir.mkdir(mode=0o700)
            values = dict(DEFAULTS)
            values["log"] = "raced-log"
            runtime_file = config_dir / "runtime_paths.json"
            runtime_file.write_text(json.dumps(values), encoding="utf-8")
            runtime_file.chmod(0o600)
            business = config_dir / "model_proxy_config.json"
            business.write_text('{"strategies":[],"routes":[]}', encoding="utf-8")
            business.chmod(0o600)
            snapshot = capture_config(str(business))
            baseline = dispatch._config_write_evidence(root, snapshot, "switch")
            original = dispatch._config_write_evidence
            def race(*args):
                (root / "raced-log").write_text("fake", encoding="utf-8")
                return original(*args)
            with patch.object(dispatch, "_config_write_evidence", side_effect=race):
                with self.assertRaises(PreflightError) as caught:
                    dispatch._prepare_mac_config_runtime(root, snapshot, "switch", baseline)
            self.assertEqual(caught.exception.code, "config_changed")
            self.assertFalse((root / ".runtime" / "tmp").exists())


class WindowsBoundarySelectionTests(unittest.TestCase):
    def test_exact_project_prefix_uses_project_boundary(self):
        self.assertEqual(_windows_trusted_boundary(r"C:\Proj", r"c:\Proj\sub\file"),
                         r"C:\Proj")

    def test_case_short_name_and_unicode_uncertainty_cannot_be_external(self):
        for target in (r"c:\proj\sub\file", r"C:\PROJ~1\sub\file",
                       "C:\\项目\\sub\\file"):
            with self.subTest(target=target), self.assertRaises(PreflightError) as caught:
                _windows_trusted_boundary(r"C:\Proj", target)
            self.assertEqual(caught.exception.code, "windows_boundary_unknown")

    def test_provably_distinct_external_path_uses_only_direct_parent(self):
        self.assertEqual(_windows_trusted_boundary(r"C:\Proj", r"C:\external\sub\file"),
                         r"C:\external\sub")

    def test_only_actual_legacy_spelling_can_create_mapped_external_parent(self):
        root = r"C:\fixture\project"
        local = r"C:\fixture\Local"
        raw = dict(DEFAULTS)
        raw["lock"] = LEGACY_DEFAULTS["lock"]
        legacy = parse_runtime_paths(raw, project_root=root, platform="windows",
                                     local_appdata=local)
        expected = ntpath.normcase(ntpath.join(local, "model_proxy", "runtime"))
        self.assertEqual(windows_initial_missing_parents(legacy, local), (expected,))
        raw["lock"] = str(legacy["lock"])
        explicit = parse_runtime_paths(raw, project_root=root, platform="windows",
                                       local_appdata=local)
        self.assertEqual(dict(explicit), dict(legacy))
        self.assertEqual(windows_initial_missing_parents(explicit, local), ())
