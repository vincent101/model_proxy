"""core/cli/_install_ops.py 预览确认写入流程单测。

覆盖 preview_confirm_write 核心行为契约（确认后才写/不确认不写/无变化跳过），
以及 install_claude/install_codex/install_openclaw 在"文件不存在"分支保持
不产生任何文件的回归。全部用 tempfile 模拟配置文件，不碰真实 ~/.claude 等路径。

运行：cd tools/model_proxy && python3 -m unittest tests.test_install_ops -v
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.cli import _install_ops as iops


class IsolatedInstallCase(unittest.TestCase):
    """Each test authorizes a private fake HOME; no production HOME is used."""
    def setUp(self):
        super().setUp()
        original_events, original_values = iops._EVENTS, list(iops._EVENTS)
        original_cancelled = iops._CANCELLED
        original_probe, original_probe_values = iops._AUDIT_PROBE_STATE, list(iops._AUDIT_PROBE_STATE)
        def restore_state():
            original_events[:] = original_values
            iops._EVENTS = original_events
            iops._CANCELLED = original_cancelled
            original_probe[:] = original_probe_values
            iops._AUDIT_PROBE_STATE = original_probe
        self.addCleanup(restore_state)
        iops._EVENTS = []
        iops._CANCELLED = False
        guard_token = iops._COMMIT_GUARD.set(None)
        self.addCleanup(iops._COMMIT_GUARD.reset, guard_token)
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fake_home = Path(temporary.name).resolve() / 'home'
        self.fake_home.mkdir(mode=0o700)
        project = Path(temporary.name).resolve() / 'project'
        self.project = project
        (project / 'config').mkdir(parents=True)
        (project / 'runtime').mkdir()
        config = project / 'config' / 'fake.json'
        config.write_text('{"supplies":[],"routes":[],"strategies":[]}')
        config.chmod(0o600)
        self.fake_config = config
        (project / 'config' / 'runtime_paths.json').write_text(json.dumps({
            key: str(project / 'runtime' / key)
            for key in ('log', 'totals', 'lock', 'pid', 'ensure_log', 'start_lock')
        }))
        token = iops._WRITE_CONTEXT.set({'root': project, 'config': config, 'targets': []})
        self.addCleanup(iops._WRITE_CONTEXT.reset, token)
        env = patch.dict(os.environ, {'HOME': str(self.fake_home),
                                      'USERPROFILE': str(self.fake_home),
                                      'LOCALAPPDATA': str(self.fake_home / 'appdata')})
        env.start()
        self.addCleanup(env.stop)
        offline = patch("urllib.request.urlopen", side_effect=OSError("fake offline"))
        offline.start()
        self.addCleanup(offline.stop)

    def backups_for(self, target):
        """Backups now live in the project runtime, not beside the target."""
        directory = self.project / '.runtime' / 'backups'
        return sorted(directory.glob(Path(target).name + '.bak.*')) if directory.exists() else []


def private_tempdir():
    return tempfile.TemporaryDirectory(dir=Path.home())


class TestModulePaths(IsolatedInstallCase):
    def test_model_proxy_dir_is_project_root_after_cli_move(self):
        project_root = Path(__file__).resolve().parent.parent
        self.assertEqual(Path(iops.__file__).resolve(),
                         project_root / "core" / "cli" / "_install_ops.py")
        self.assertEqual(iops._MODEL_PROXY_DIR, project_root)
        self.assertEqual(iops._CODEX_CATALOG_TEMPLATE,
                         project_root / "assets" / "codex_catalog_template.json")


class TestInstallPreflight(IsolatedInstallCase):
    @unittest.skipUnless(sys.platform == 'darwin', 'macOS private-config policy')
    def test_unverifiable_target_is_rejected_before_any_write(self):
        """目标身份无法验证时，`_snapshot_text` 必须 fail-closed 且不产生备份。

        历史背景：本用例原名 test_world_readable_fake_config_is_rejected…，
        断言 0644 的**配置**会被拒。自 2450c38 起业务配置读取为兼容 macOS
        自动附着的 xattr 而刻意放宽了 mode 检查，所以那条前提已不成立。
        但「目标不可验证 → 拒写、无备份」这条不变量仍然重要，改用**目录**
        作为目标来验证（不是普通文件 → not_regular_file）。
        """
        from core import mac_config_path, secure_files

        # 生产者仍是同一个原生实现（防 mock 掉真实验证）
        self.assertIs(mac_config_path._mac_fd_protection, secure_files._mac_fd_protection)
        self.assertEqual(secure_files._mac_fd_protection.__module__, 'core.secure_files')

        # 业务配置读取的负例：目录不是普通文件
        not_a_file = self.fake_config.with_name('not-a-file.json')
        not_a_file.mkdir()
        with self.assertRaises(mac_config_path.MacConfigError) as caught:
            mac_config_path.capture_config(not_a_file)
        self.assertEqual(caught.exception.code, 'not_regular_file')

        # 安装目标不可验证：不写、不备份、记 PREPARE_FAILED
        target = self.fake_home / 'client.json'
        target.mkdir()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(iops.InstallFailure):
            iops._snapshot_text(target)
        self.assertEqual(iops._EVENTS, [('client.json', 'PREPARE_FAILED', None)])
        self.assertTrue(target.is_dir(), '目标不得被替换')
        self.assertEqual(list(self.fake_home.glob('client.json.bak.*')), [])

    def _make_cfg(self, strategies, routes, supplies=None):
        return {
            "routes": routes,
            "supplies": supplies or [],
            "strategies": strategies,
        }

    def _make_anthropic_route(self, rid="r1"):
        return {"id": rid, "tiers": {"t0": ["s1"]}}

    def _make_anthropic_supply(self, sid="s1"):
        return {"id": sid, "url": "https://api.anthropic.com/v1/messages",
                "appkey": "sk-test"}

    def test_route_pool_returns_candidates(self):
        """route_pool 写法：strategy 含 route_pool（无 route_id）→ 应返回候选。"""
        cfg = self._make_cfg(
            strategies=[{
                "client_token": "tok_a",
                "route_pool": [{"route_id": "r1", "weight": 1}],
                "note": "pool strategy",
            }],
            routes=[self._make_anthropic_route("r1")],
            supplies=[self._make_anthropic_supply("s1")],
        )
        cands = iops.candidate_tokens(cfg, "anthropic")
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["client_token"], "tok_a")
        self.assertEqual(cands[0]["protocol"], "anthropic")
        self.assertIn("r1", cands[0]["route_desc"])

    def test_route_id_single_returns_candidates(self):
        """route_id 单值写法：strategy 含 route_id（无 route_pool）→ 应返回候选。"""
        cfg = self._make_cfg(
            strategies=[{
                "client_token": "tok_b",
                "route_id": "r2",
                "note": "single strategy",
            }],
            routes=[self._make_anthropic_route("r2")],
            supplies=[self._make_anthropic_supply("s1")],
        )
        cands = iops.candidate_tokens(cfg, "anthropic")
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["client_token"], "tok_b")
        self.assertEqual(cands[0]["protocol"], "anthropic")
        self.assertEqual(cands[0]["route_desc"], "r2")

    def test_protocol_filter_excludes_non_matching(self):
        """协议不匹配的 strategy 应被过滤掉。"""
        openai_supply = {"id": "s2", "url": "https://api.openai.com/v1/chat/completions",
                         "appkey": "sk-oai"}
        cfg = self._make_cfg(
            strategies=[
                {"client_token": "tok_anthropic", "route_pool": [{"route_id": "r1", "weight": 1}]},
                {"client_token": "tok_openai", "route_pool": [{"route_id": "r3", "weight": 1}]},
            ],
            routes=[self._make_anthropic_route("r1"),
                    {"id": "r3", "tiers": {"t0": ["s2"]}}],
            supplies=[self._make_anthropic_supply("s1"), openai_supply],
        )
        cands = iops.candidate_tokens(cfg, "anthropic")
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["client_token"], "tok_anthropic")

    def test_protocol_none_returns_all(self):
        """protocol=None 时不过滤，返回所有候选。"""
        openai_supply = {"id": "s2", "url": "https://api.openai.com/v1/chat/completions",
                         "appkey": "sk-oai"}
        cfg = self._make_cfg(
            strategies=[
                {"client_token": "tok_a", "route_pool": [{"route_id": "r1", "weight": 1}]},
                {"client_token": "tok_b", "route_pool": [{"route_id": "r3", "weight": 1}]},
            ],
            routes=[self._make_anthropic_route("r1"),
                    {"id": "r3", "tiers": {"t0": ["s2"]}}],
            supplies=[self._make_anthropic_supply("s1"), openai_supply],
        )
        cands = iops.candidate_tokens(cfg, None)
        self.assertEqual(len(cands), 2)

    def test_route_pool_with_missing_route_skipped(self):
        """route_pool 内 route_id 在 routes_map 不存在 → 跳过该 strategy。"""
        cfg = self._make_cfg(
            strategies=[{
                "client_token": "tok_missing",
                "route_pool": [{"route_id": "no_such_route", "weight": 1}],
                "note": "",
            }],
            routes=[self._make_anthropic_route("r1")],
            supplies=[self._make_anthropic_supply("s1")],
        )
        cands = iops.candidate_tokens(cfg, "anthropic")
        self.assertEqual(cands, [])

    def test_route_pool_multi_routes_uses_first_valid(self):
        """route_pool 内多个 route_id → 取第一个合法 route 做协议推断。"""
        cfg = self._make_cfg(
            strategies=[{
                "client_token": "tok_multi",
                "route_pool": [
                    {"route_id": "missing_r", "weight": 1},
                    {"route_id": "r1", "weight": 1},
                ],
                "note": "multi pool",
            }],
            routes=[self._make_anthropic_route("r1")],
            supplies=[self._make_anthropic_supply("s1")],
        )
        cands = iops.candidate_tokens(cfg, "anthropic")
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["client_token"], "tok_multi")


class TestPreviewConfirmWrite(IsolatedInstallCase):

    def setUp(self):
        super().setUp()
        self._tmpdir = private_tempdir()
        self.addCleanup(self._tmpdir.cleanup)
        self.cfg_path = Path(self._tmpdir.name) / "settings.json"

    def _write_old(self, text: str):
        self.cfg_path.write_text(text, encoding="utf-8")

    def test_confirm_yes_backs_up_and_writes(self):
        old_text = json.dumps({"env": {}}, indent=2) + "\n"
        self._write_old(old_text)
        new_text = json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://x/"}}, indent=2) + "\n"

        with patch("core.cli._install_ops.confirm", return_value=True):
            result = iops.preview_confirm_write(
                self.cfg_path, old_text, new_text, "claude", ["ok"]
            )

        self.assertTrue(result)
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), new_text)
        baks = self.backups_for(self.cfg_path)
        self.assertEqual(len(baks), 1)
        self.assertEqual(baks[0].read_text(encoding="utf-8"), old_text)

    def test_confirm_no_leaves_file_untouched_no_backup(self):
        old_text = json.dumps({"env": {}}, indent=2) + "\n"
        self._write_old(old_text)
        new_text = json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://x/"}}, indent=2) + "\n"

        with patch("core.cli._install_ops.confirm", return_value=False):
            result = iops.preview_confirm_write(
                self.cfg_path, old_text, new_text, "claude", ["ok"]
            )

        self.assertFalse(result)
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), old_text)
        baks = self.backups_for(self.cfg_path)
        self.assertEqual(len(baks), 0)

    def test_no_diff_skips_without_prompting(self):
        text = json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://x/"}}, indent=2) + "\n"
        self._write_old(text)

        with patch("core.cli._install_ops.confirm") as mock_confirm:
            result = iops.preview_confirm_write(
                self.cfg_path, text, text, "claude", ["ok"]
            )
            mock_confirm.assert_not_called()

        self.assertFalse(result)
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), text)
        baks = self.backups_for(self.cfg_path)
        self.assertEqual(len(baks), 0)

    def test_backup_record_and_redacted_summary(self):
        """预览只显示脱敏摘要；确认后唯一备份与结果记录一致。"""
        old_text = "FAKE_OLD_SECRET"
        new_text = "FAKE_NEW_SECRET"
        self._write_old(old_text)
        output = io.StringIO()
        preview = []

        def confirm_after_preview(_prompt):
            preview.append(output.getvalue())
            return True

        with patch("core.cli._install_ops.confirm", side_effect=confirm_after_preview), \
             contextlib.redirect_stdout(output):
            result = iops.preview_confirm_write(self.cfg_path, old_text, new_text, "claude", ["ok"])

        baks = self.backups_for(self.cfg_path)
        self.assertTrue(result)
        self.assertEqual(len(baks), 1)
        self.assertEqual(baks[0].read_text(encoding="utf-8"), old_text)
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), new_text)
        self.assertEqual(iops._EVENTS, [('claude', 'VERIFIED', str(baks[0]))])
        self.assertEqual(len(preview), 1)
        self.assertIn('字段变更摘要', preview[0])
        self.assertIn(str(baks[0]), output.getvalue())
        for secret in (old_text, new_text):
            self.assertNotIn(secret, preview[0])
            self.assertNotIn(secret, output.getvalue())

    def test_no_original_file_skips_backup_and_writes(self):
        """原文件不存在（old_text=""）时跳过备份、直接写入，不产生 .bak 文件。"""
        new_text = json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://x/"}}) + "\n"
        # 不创建 self.cfg_path，模拟首次写入

        with patch("core.cli._install_ops.confirm", return_value=True):
            result = iops.preview_confirm_write(
                self.cfg_path, "", new_text, "claude", ["ok"]
            )

        self.assertTrue(result)
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), new_text)
        baks = self.backups_for(self.cfg_path)
        self.assertEqual(len(baks), 0)

    def test_no_original_file_confirm_no_creates_nothing(self):
        """原文件不存在 + 用户不确认 → 不创建文件、不产生备份。"""
        new_text = json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://x/"}}) + "\n"

        with patch("core.cli._install_ops.confirm", return_value=False):
            result = iops.preview_confirm_write(
                self.cfg_path, "", new_text, "claude", ["ok"]
            )

        self.assertFalse(result)
        self.assertFalse(self.cfg_path.exists())
        baks = self.backups_for(self.cfg_path)
        self.assertEqual(len(baks), 0)

    def test_empty_old_text_shows_redacted_summary(self):
        """首次写入只显示脱敏摘要，不展示新增配置值或创建备份。"""
        secret = "FAKE_NEW_TOKEN_DO_NOT_PRINT"
        new_text = f'experimental_bearer_token = "{secret}"\n'
        output = io.StringIO()
        preview = []

        def confirm_after_preview(_prompt):
            preview.append(output.getvalue())
            return True

        with patch("core.cli._install_ops.confirm", side_effect=confirm_after_preview), \
             contextlib.redirect_stdout(output):
            result = iops.preview_confirm_write(
                self.cfg_path, "", new_text, "codex", ["ok"]
            )

        self.assertTrue(result)
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), new_text)
        self.assertEqual(self.backups_for(self.cfg_path), [])
        self.assertEqual(list(self.cfg_path.parent.glob("settings.json.bak.*")), [])
        self.assertEqual(len(preview), 1)
        self.assertIn('字段变更摘要', preview[0])
        self.assertNotIn(secret, preview[0])
        self.assertNotIn(secret, output.getvalue())


class TestInstallClaudeFileNotExists(IsolatedInstallCase):

    def test_missing_file_prints_snippet_and_creates_nothing(self):
        with private_tempdir() as td:
            fake_home_cfg = Path(td) / "settings.json"
            with patch.dict(iops.SDKS["claude"], {"config_path": fake_home_cfg}), \
                 patch("core.cli._install_ops.print_manual_snippet") as mock_snippet:
                iops.install_claude("tok", "8000")
            mock_snippet.assert_called_once()
            # snippet 内容含 hasCompletedOnboarding 提示
            snippet_text = mock_snippet.call_args[0][1]
            self.assertIn("hasCompletedOnboarding", snippet_text)
            self.assertFalse(fake_home_cfg.exists())


class TestEnsureOnboardingCompleted(IsolatedInstallCase):
    """_ensure_onboarding_completed 各分支用例，全部用 tempfile 模拟
    ~/.claude.json，绝不碰真实文件。"""

    def setUp(self):
        super().setUp()
        self._tmpdir = private_tempdir()
        self.addCleanup(self._tmpdir.cleanup)
        self.onboarding_path = Path(self._tmpdir.name) / ".claude.json"

    def test_file_not_exists_creates_minimal(self):
        """文件不存在 + confirm=True → 建文件 {"hasCompletedOnboarding": true}，无 .bak。"""
        with patch("core.cli._install_ops.confirm", return_value=True):
            iops._ensure_onboarding_completed(self.onboarding_path)
        self.assertTrue(self.onboarding_path.exists())
        written = json.loads(self.onboarding_path.read_text(encoding="utf-8"))
        self.assertTrue(written["hasCompletedOnboarding"])
        baks = self.backups_for(self.onboarding_path)
        self.assertEqual(len(baks), 0)

    def test_file_not_exists_confirm_no_creates_nothing(self):
        """文件不存在 + confirm=False → 文件不存在，无 .bak。"""
        with patch("core.cli._install_ops.confirm", return_value=False):
            iops._ensure_onboarding_completed(self.onboarding_path)
        self.assertFalse(self.onboarding_path.exists())
        baks = self.backups_for(self.onboarding_path)
        self.assertEqual(len(baks), 0)

    def test_already_true_skips(self):
        """文件存在 + hasCompletedOnboarding=true → confirm 不被调用，文件不变。"""
        original = json.dumps({"hasCompletedOnboarding": True}, indent=2) + "\n"
        self.onboarding_path.write_text(original, encoding="utf-8")
        with patch("core.cli._install_ops.confirm") as mock_confirm:
            iops._ensure_onboarding_completed(self.onboarding_path)
            mock_confirm.assert_not_called()
        self.assertEqual(self.onboarding_path.read_text(encoding="utf-8"), original)
        baks = self.backups_for(self.onboarding_path)
        self.assertEqual(len(baks), 0)

    def test_missing_key_adds_and_preserves_others(self):
        """文件存在 + 缺 hasCompletedOnboarding 键 + 有其他键 → 补 true，其他键保留，有 .bak。"""
        original_cfg = {
            "mcpServers": {"foo": {"command": "bar"}},
            "projects": {"/tmp/proj": {"allowedTools": ["Read"]}},
        }
        original = json.dumps(original_cfg, indent=2, ensure_ascii=False) + "\n"
        self.onboarding_path.write_text(original, encoding="utf-8")
        with patch("core.cli._install_ops.confirm", return_value=True):
            iops._ensure_onboarding_completed(self.onboarding_path)
        written = json.loads(self.onboarding_path.read_text(encoding="utf-8"))
        self.assertTrue(written["hasCompletedOnboarding"])
        # 其他键结构保留
        self.assertEqual(written["mcpServers"], original_cfg["mcpServers"])
        self.assertEqual(written["projects"], original_cfg["projects"])
        baks = self.backups_for(self.onboarding_path)
        self.assertEqual(len(baks), 1)

    def test_false_overwritten_to_true(self):
        """文件存在 + hasCompletedOnboarding=false → confirm=True 覆盖为 true，有 .bak。"""
        original = json.dumps({"hasCompletedOnboarding": False}, indent=2) + "\n"
        self.onboarding_path.write_text(original, encoding="utf-8")
        with patch("core.cli._install_ops.confirm", return_value=True):
            iops._ensure_onboarding_completed(self.onboarding_path)
        written = json.loads(self.onboarding_path.read_text(encoding="utf-8"))
        self.assertTrue(written["hasCompletedOnboarding"])
        baks = self.backups_for(self.onboarding_path)
        self.assertEqual(len(baks), 1)

    def test_false_confirm_no_unchanged(self):
        """文件存在 + hasCompletedOnboarding=false + confirm=False → 文件不变，无 .bak。"""
        original = json.dumps({"hasCompletedOnboarding": False}, indent=2) + "\n"
        self.onboarding_path.write_text(original, encoding="utf-8")
        with patch("core.cli._install_ops.confirm", return_value=False):
            iops._ensure_onboarding_completed(self.onboarding_path)
        self.assertEqual(self.onboarding_path.read_text(encoding="utf-8"), original)
        baks = self.backups_for(self.onboarding_path)
        self.assertEqual(len(baks), 0)

    def test_invalid_json_degrades_no_write(self):
        """文件存在但非合法 JSON → 不调 confirm，不写入，打印手动片段。"""
        self.onboarding_path.write_text("not valid json {{{", encoding="utf-8")
        original_bytes = self.onboarding_path.read_bytes()
        with patch("core.cli._install_ops.confirm") as mock_confirm, \
             patch("builtins.print") as mock_print:
            iops._ensure_onboarding_completed(self.onboarding_path)
            mock_confirm.assert_not_called()
        self.assertEqual(self.onboarding_path.read_bytes(), original_bytes)
        baks = self.backups_for(self.onboarding_path)
        self.assertEqual(len(baks), 0)
        # 打印了手动片段提示
        printed = " ".join(str(c) for c, _ in mock_print.call_args_list)
        self.assertIn("hasCompletedOnboarding", printed)

    def test_default_arg_reads_module_constant(self):
        """不传 onboarding_path，patch 模块级 _CLAUDE_ONBOARDING → 走默认参数分支读到临时路径。"""
        with patch("core.cli._install_ops._CLAUDE_ONBOARDING", self.onboarding_path), \
             patch("core.cli._install_ops.confirm", return_value=True):
            iops._ensure_onboarding_completed()  # 不传参
        self.assertTrue(self.onboarding_path.exists())
        written = json.loads(self.onboarding_path.read_text(encoding="utf-8"))
        self.assertTrue(written["hasCompletedOnboarding"])


class TestInstallCodexFileNotExists(IsolatedInstallCase):

    def test_missing_file_writes_via_preview_confirm_write(self):
        """首次文件不存在时不再走 print_manual_snippet，而是直接调
        preview_confirm_write 写入（old_text=""），确认后文件被创建，
        内容含 experimental_bearer_token 直填 token；第二次追加 catalog 引用时
        保留第一次写入的安全备份。mock _install_catalog_asset 返回 True，断言含
        model_catalog_json 行。"""
        with private_tempdir() as td:
            fake_cfg = Path(td) / "config.toml"
            with patch.dict(iops.SDKS["codex"], {"config_path": fake_cfg}), \
                 patch("core.cli._install_ops.confirm", return_value=True), \
                 patch("core.cli._install_ops.print_manual_snippet") as mock_snippet, \
                 patch("core.cli._install_ops._install_catalog_asset", return_value=True) as mock_catalog:
                iops.install_codex("codex", "8000")
            mock_snippet.assert_not_called()
            mock_catalog.assert_called_once()
            self.assertTrue(fake_cfg.exists())
            content = fake_cfg.read_text(encoding="utf-8")
            self.assertIn('experimental_bearer_token = "codex"', content)
            self.assertIn('wire_api = "responses"', content)
            self.assertIn('model = "claude-sonnet"', content)
            self.assertIn('model_provider = "model_proxy"', content)
            self.assertIn('# env_key =', content)
            self.assertIn('model_catalog_json', content)
            # 首次创建无备份；第二次添加 catalog 引用前备份第一次的完整配置。
            baks = self.backups_for(fake_cfg)
            self.assertEqual(len(baks), 1)
            first_version = baks[0].read_text(encoding="utf-8")
            self.assertIn('experimental_bearer_token = "codex"', first_version)
            self.assertNotIn('model_catalog_json', first_version)
            self.assertEqual(baks[0].stat().st_mode & 0o777, 0o600)

    def test_missing_file_confirm_no_creates_nothing(self):
        """首次文件不存在 + 用户不确认 → 不创建文件、不产生备份。
        追加断言：catalog 未调用（config.toml 没写入就不会调 catalog）。"""
        with private_tempdir() as td:
            fake_cfg = Path(td) / "config.toml"
            with patch.dict(iops.SDKS["codex"], {"config_path": fake_cfg}), \
                 patch("core.cli._install_ops.confirm", return_value=False), \
                 patch("core.cli._install_ops._install_catalog_asset") as mock_catalog:
                iops.install_codex("codex", "8000")
            self.assertFalse(fake_cfg.exists())
            baks = self.backups_for(fake_cfg)
            self.assertEqual(len(baks), 0)
            mock_catalog.assert_not_called()

    def test_missing_file_catalog_fetch_fails_no_catalog_key(self):
        """首次写入 + catalog 装失败（网络失败）→ config.toml 写入成功但
        不含 model_catalog_json 行。"""
        with private_tempdir() as td:
            fake_cfg = Path(td) / "config.toml"
            with patch.dict(iops.SDKS["codex"], {"config_path": fake_cfg}), \
                 patch("core.cli._install_ops.confirm", return_value=True), \
                 patch("core.cli._install_ops._install_catalog_asset", return_value=False):
                iops.install_codex("codex", "8000")
            self.assertTrue(fake_cfg.exists())
            content = fake_cfg.read_text(encoding="utf-8")
            self.assertNotIn("model_catalog_json", content)


class TestInstallCatalogAsset(IsolatedInstallCase):
    """_install_catalog_asset 各分支用例，全部用 tempfile，绝不碰真实
    ~/.codex/；mock urllib.request.urlopen 控制网络结果。"""

    def setUp(self):
        super().setUp()
        self._tmpdir = private_tempdir()
        self.addCleanup(self._tmpdir.cleanup)
        self.template_path = Path(self._tmpdir.name) / "template.json"
        self.target_path = Path(self._tmpdir.name) / "target.json"
        # 写一个最小模板
        self.template_data = {
            "_codex_version": "0.145.0",
            "models": [
                {"slug": "claude-opus", "base_instructions": "__PROMPT_MD__",
                 "model_messages": {"instructions_template": "__PROMPT_MD__"}},
                {"slug": "claude-sonnet", "base_instructions": "__PROMPT_MD__",
                 "model_messages": {"instructions_template": "__PROMPT_MD__"}},
                {"slug": "claude-haiku", "base_instructions": "__PROMPT_MD__",
                 "model_messages": {"instructions_template": "__PROMPT_MD__"}},
            ],
        }
        self.template_path.write_text(
            json.dumps(self.template_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _mock_urlopen(self, prompt_content="FAKE_PROMPT_MD_CONTENT"):
        """构造一个 mock urlopen，返回含 prompt_content 的响应。"""
        from io import BytesIO
        resp = BytesIO(prompt_content.encode("utf-8"))
        m = patch("urllib.request.urlopen", return_value=resp)
        return m

    def test_template_not_exists_warns_and_skips(self):
        """仓库模板不存在 → 打 warning，return False，不创建目标文件。"""
        bad_template = Path(self._tmpdir.name) / "no_such.json"
        with patch("builtins.print") as mock_print:
            result = iops._install_catalog_asset(
                template_path=bad_template, target_path=self.target_path
            )
        self.assertFalse(result)
        self.assertFalse(self.target_path.exists())
        printed = " ".join(str(c) for c, _ in mock_print.call_args_list)
        self.assertIn("不存在", printed)

    def test_null_and_empty_object_model_messages_remain_valid(self):
        self.template_path.write_text(
            '{"models": [{"model_messages": null}, {"model_messages": {}}]}', encoding='utf-8')
        with self._mock_urlopen('fake prompt'):
            prepared = iops._prepare_catalog_asset(self.template_path)
        models = json.loads(prepared)['models']
        self.assertIsNone(models[0]['model_messages'])
        self.assertEqual(models[1]['model_messages'], {})
        self.assertEqual([item['base_instructions'] for item in models],
                         ['fake prompt', 'fake prompt'])

    def test_first_install_creates_private_catalog_directory(self):
        codex = self.fake_home / '.codex'
        codex.mkdir(mode=0o700)
        target = codex / 'model-catalogs' / 'model_proxy_catalog.json'
        with self._mock_urlopen(), patch('core.cli._install_ops.confirm', return_value=True):
            self.assertTrue(iops._install_catalog_asset(self.template_path, target))
        self.assertTrue(target.is_file())
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(target.stat().st_mode & 0o077, 0)

    def test_first_catalog_install_obeys_umask_022_and_keeps_file_private(self):
        codex = self.fake_home / '.codex'
        codex.mkdir(mode=0o700)
        target = codex / 'model-catalogs' / 'model_proxy_catalog.json'
        previous = os.umask(0o022)
        try:
            with self._mock_urlopen('fake-first'), patch('core.cli._install_ops.confirm', return_value=True):
                self.assertTrue(iops._install_catalog_asset(self.template_path, target))
            old_contents = target.read_bytes()
            with self._mock_urlopen('fake-second'), patch('core.cli._install_ops.confirm', return_value=True):
                self.assertTrue(iops._install_catalog_asset(self.template_path, target))
        finally:
            os.umask(previous)
        backups = self.backups_for(target)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), old_contents)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_catalog_directory_obeys_umask_027(self):
        codex = self.fake_home / '.codex'
        codex.mkdir(mode=0o700)
        directory = codex / 'model-catalogs'
        previous = os.umask(0o027)
        try:
            iops._create_catalog_directory(directory)
        finally:
            os.umask(previous)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o750)
        self.assertEqual(list(directory.iterdir()), [])

    def test_group_writable_umask_creates_safe_new_catalog_directory(self):
        codex = self.fake_home / '.codex'
        codex.mkdir(mode=0o700)
        target = codex / 'model-catalogs' / 'model_proxy_catalog.json'
        previous = os.umask(0o002)
        try:
            with self._mock_urlopen(), patch('core.cli._install_ops.confirm', return_value=True):
                self.assertTrue(iops._install_catalog_asset(self.template_path, target))
        finally:
            os.umask(previous)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_existing_group_writable_catalog_directory_is_not_repaired(self):
        codex = self.fake_home / '.codex'
        codex.mkdir(mode=0o700)
        directory = codex / 'model-catalogs'
        directory.mkdir(mode=0o700)
        directory.chmod(0o775)
        target = directory / 'model_proxy_catalog.json'
        with self._mock_urlopen(), self.assertRaises(iops.InstallFailure):
            iops._install_catalog_asset(self.template_path, target)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o775)
        self.assertFalse(target.exists())
        self.assertEqual(list(directory.iterdir()), [])

    def test_catalog_postcheck_failure_reports_possible_empty_directory(self):
        from core.secure_files import ProtectionEvidence
        codex = self.fake_home / '.codex'
        codex.mkdir(mode=0o700)
        directory = codex / 'model-catalogs'
        original = iops.mac_private_protection
        def protection(path, info):
            if Path(path) == directory:
                return ProtectionEvidence(False, None)
            return original(path, info)
        with patch.object(iops, 'mac_private_protection', side_effect=protection):
            with self.assertRaises(iops.InstallFailure) as caught:
                iops._create_catalog_directory(directory)
        self.assertIn('空目录可能已创建', str(caught.exception))
        self.assertTrue(directory.is_dir())
        self.assertEqual(list(directory.iterdir()), [])

    def test_catalog_directory_parent_symlink_is_not_followed(self):
        actual = self.fake_home / 'other'
        actual.mkdir(mode=0o700)
        (self.fake_home / '.codex').symlink_to(actual, target_is_directory=True)
        target = self.fake_home / '.codex' / 'model-catalogs' / 'model_proxy_catalog.json'
        with self._mock_urlopen(), self.assertRaises(iops.InstallFailure):
            iops._install_catalog_asset(self.template_path, target)
        self.assertFalse((actual / 'model-catalogs').exists())

    def test_missing_catalog_directory_outside_fake_home_is_refused(self):
        target = Path(self._tmpdir.name) / 'other' / 'model-catalogs' / 'asset.json'
        with self._mock_urlopen(), self.assertRaises(iops.InstallFailure):
            iops._install_catalog_asset(self.template_path, target)
        self.assertFalse(target.parent.exists())

    def test_prompt_md_fetch_fails_degrades(self):
        """模板存在 + mock urlopen 抛 URLError → 降级 warning，return False，
        不写目标文件。"""
        from urllib.error import URLError
        with patch("urllib.request.urlopen", side_effect=URLError("mock network fail")), \
             patch("builtins.print") as mock_print:
            result = iops._install_catalog_asset(
                template_path=self.template_path, target_path=self.target_path
            )
        self.assertFalse(result)
        self.assertFalse(self.target_path.exists())
        printed = " ".join(str(c) for c, _ in mock_print.call_args_list)
        self.assertIn("拉取 prompt.md 失败", printed)

    def test_target_not_exists_writes(self):
        """模板存在 + prompt 拉取成功 + 目标不存在 + confirm=True →
        mkdir + 写入，内容含 prompt.md 全文，无 .bak，return True。"""
        prompt = "FAKE_PROMPT_MD_CONTENT"
        with self._mock_urlopen(prompt), \
             patch("core.cli._install_ops.confirm", return_value=True):
            result = iops._install_catalog_asset(
                template_path=self.template_path, target_path=self.target_path
            )
        self.assertTrue(result)
        self.assertTrue(self.target_path.exists())
        written = json.loads(self.target_path.read_text(encoding="utf-8"))
        self.assertEqual(
            written["models"][0]["base_instructions"], prompt
        )
        self.assertEqual(
            written["models"][1]["base_instructions"], prompt
        )
        self.assertEqual(
            written["models"][2]["base_instructions"], prompt
        )
        self.assertEqual(
            written["models"][0]["model_messages"]["instructions_template"], prompt
        )
        self.assertEqual(
            written["models"][1]["model_messages"]["instructions_template"], prompt
        )
        self.assertEqual(
            written["models"][2]["model_messages"]["instructions_template"], prompt
        )
        baks = self.backups_for(self.target_path)
        self.assertEqual(len(baks), 0)

    def test_target_not_exists_confirm_no_creates_nothing(self):
        """目标不存在 + confirm=False → 不创建文件，无 .bak，return False。"""
        with self._mock_urlopen(), \
             patch("core.cli._install_ops.confirm", return_value=False):
            result = iops._install_catalog_asset(
                template_path=self.template_path, target_path=self.target_path
            )
        self.assertFalse(result)
        self.assertFalse(self.target_path.exists())
        baks = self.backups_for(self.target_path)
        self.assertEqual(len(baks), 0)

    def test_target_same_content_skips(self):
        """目标存在 + bytes 相同（拼装后）→ 不调 confirm，文件不变，
        无 .bak，return True。"""
        prompt = "FAKE_PROMPT_MD_CONTENT"
        # 预先构造与拼装结果完全一致的目标文件
        expected = json.loads(self.template_path.read_text(encoding="utf-8"))
        for m in expected["models"]:
            m["base_instructions"] = prompt
            if m.get("model_messages") and m["model_messages"].get("instructions_template") is not None:
                m["model_messages"]["instructions_template"] = prompt
        expected_bytes = json.dumps(expected, ensure_ascii=False, indent=2).encode("utf-8")
        self.target_path.write_bytes(expected_bytes)

        with self._mock_urlopen(prompt), \
             patch("core.cli._install_ops.confirm") as mock_confirm:
            result = iops._install_catalog_asset(
                template_path=self.template_path, target_path=self.target_path
            )
            mock_confirm.assert_not_called()
        self.assertTrue(result)
        self.assertEqual(self.target_path.read_bytes(), expected_bytes)
        baks = self.backups_for(self.target_path)
        self.assertEqual(len(baks), 0)

    def test_target_different_content_backs_up_and_overwrites(self):
        """目标存在 + bytes 不同 + confirm=True → 备份 .bak + 覆盖，return True。"""
        old_content = '{"old": true}'
        self.target_path.write_text(old_content, encoding="utf-8")
        with self._mock_urlopen(), \
             patch("core.cli._install_ops.confirm", return_value=True):
            result = iops._install_catalog_asset(
                template_path=self.template_path, target_path=self.target_path
            )
        self.assertTrue(result)
        baks = self.backups_for(self.target_path)
        self.assertEqual(len(baks), 1)
        self.assertEqual(baks[0].read_text(encoding="utf-8"), old_content)
        written = json.loads(self.target_path.read_text(encoding="utf-8"))
        self.assertEqual(
            written["models"][0]["base_instructions"], "FAKE_PROMPT_MD_CONTENT"
        )
        self.assertEqual(
            written["models"][0]["model_messages"]["instructions_template"], "FAKE_PROMPT_MD_CONTENT"
        )

    def test_target_different_content_confirm_no_unchanged(self):
        """目标存在 + bytes 不同 + confirm=False → 文件不变，无 .bak，return False。"""
        old_content = '{"old": true}'
        self.target_path.write_text(old_content, encoding="utf-8")
        with self._mock_urlopen(), \
             patch("core.cli._install_ops.confirm", return_value=False):
            result = iops._install_catalog_asset(
                template_path=self.template_path, target_path=self.target_path
            )
        self.assertFalse(result)
        self.assertEqual(self.target_path.read_text(encoding="utf-8"), old_content)
        baks = self.backups_for(self.target_path)
        self.assertEqual(len(baks), 0)

    def test_default_arg_reads_module_constant(self):
        """不传参，patch 模块级常量 → 走默认参数分支读到临时路径。"""
        prompt = "FAKE_PROMPT_MD_CONTENT"
        with self._mock_urlopen(prompt), \
             patch("core.cli._install_ops.confirm", return_value=True), \
             patch("core.cli._install_ops._CODEX_CATALOG_TEMPLATE", self.template_path), \
             patch("core.cli._install_ops._CODEX_CATALOG_TARGET", self.target_path):
            result = iops._install_catalog_asset()
        self.assertTrue(result)
        self.assertTrue(self.target_path.exists())
        written = json.loads(self.target_path.read_text(encoding="utf-8"))
        self.assertEqual(
            written["models"][0]["base_instructions"], prompt
        )
        self.assertEqual(
            written["models"][0]["model_messages"]["instructions_template"], prompt
        )


class TestInstallCodexExistingFile(IsolatedInstallCase):
    """install_codex 在 config.toml 已存在场景下的端到端用例。
    全部用 tempfile 模拟配置文件，mock _install_catalog_asset 控制 catalog 结果。"""

    def _make_config(self, td, extra_lines=""):
        """构造一份已有 config.toml，含 provider 段但不含/含 catalog 行。"""
        cfg = Path(td) / "config.toml"
        content = (
            'model = "old-model"\n'
            'model_provider = "old-provider"\n'
            '\n'
            '[model_providers.old-provider]\n'
            'base_url = "http://old"\n'
        )
        if extra_lines:
            content = extra_lines + content
        cfg.write_text(content, encoding="utf-8")
        return cfg

    def test_existing_file_adds_catalog_key(self):
        """config.toml 已存在 + 无 model_catalog_json 行 + mock catalog 返回 True
        + confirm=True → 写入后含 model_catalog_json 行。"""
        with private_tempdir() as td:
            cfg = self._make_config(td)
            with patch.dict(iops.SDKS["codex"], {"config_path": cfg}), \
                 patch("core.cli._install_ops.confirm", return_value=True), \
                 patch("core.cli._install_ops._install_catalog_asset", return_value=True):
                iops.install_codex("codex", "8000")
            content = cfg.read_text(encoding="utf-8")
            self.assertIn("model_catalog_json", content)
            self.assertIn("~/.codex/model-catalogs/model_proxy_catalog.json", content)
            self.assertIn('model = "claude-sonnet"', content)
            self.assertIn('model_provider = "model_proxy"', content)

    def test_existing_file_replaces_catalog_key(self):
        """config.toml 已存在 + 已有 model_catalog_json 行（旧值）+ mock catalog
        返回 True + confirm=True → 写入后 model_catalog_json 值=新路径。"""
        with private_tempdir() as td:
            cfg = self._make_config(
                td,
                extra_lines='model_catalog_json = "/old/path/catalog.json"\n'
            )
            with patch.dict(iops.SDKS["codex"], {"config_path": cfg}), \
                 patch("core.cli._install_ops.confirm", return_value=True), \
                 patch("core.cli._install_ops._install_catalog_asset", return_value=True):
                iops.install_codex("codex", "8000")
            content = cfg.read_text(encoding="utf-8")
            self.assertIn("~/.codex/model-catalogs/model_proxy_catalog.json", content)
            self.assertNotIn("/old/path/catalog.json", content)

    def test_existing_file_catalog_fails_keeps_old_key(self):
        """config.toml 已存在 + 已有 model_catalog_json 行 + mock catalog 返回 False
        + confirm=True → 该行保留不动（指向旧文件），打提示。"""
        with private_tempdir() as td:
            old_catalog_line = 'model_catalog_json = "/old/path/catalog.json"\n'
            cfg = self._make_config(td, extra_lines=old_catalog_line)
            with patch.dict(iops.SDKS["codex"], {"config_path": cfg}), \
                 patch("core.cli._install_ops.confirm", return_value=True), \
                 patch("core.cli._install_ops._install_catalog_asset", return_value=False), \
                 patch("builtins.print") as mock_print:
                iops.install_codex("codex", "8000")
            content = cfg.read_text(encoding="utf-8")
            self.assertIn("/old/path/catalog.json", content)
            printed = " ".join(str(c) for c, _ in mock_print.call_args_list)
            self.assertIn("保留现有", printed)

    def test_existing_file_catalog_fails_no_key_not_added(self):
        """config.toml 已存在 + 无 model_catalog_json 行 + mock catalog 返回 False
        + confirm=True → 不加 model_catalog_json 行。"""
        with private_tempdir() as td:
            cfg = self._make_config(td)
            with patch.dict(iops.SDKS["codex"], {"config_path": cfg}), \
                 patch("core.cli._install_ops.confirm", return_value=True), \
                 patch("core.cli._install_ops._install_catalog_asset", return_value=False):
                iops.install_codex("codex", "8000")
            content = cfg.read_text(encoding="utf-8")
            self.assertNotIn("model_catalog_json", content)

    def test_existing_file_confirm_no_no_catalog_install(self):
        """config.toml 已存在 + confirm=False → catalog 未安装。"""
        with private_tempdir() as td:
            cfg = self._make_config(td)
            with patch.dict(iops.SDKS["codex"], {"config_path": cfg}), \
                 patch("core.cli._install_ops.confirm", return_value=False), \
                 patch("core.cli._install_ops._install_catalog_asset") as mock_catalog:
                iops.install_codex("codex", "8000")
            mock_catalog.assert_not_called()


class TestInstallOpenclawFileNotExists(IsolatedInstallCase):

    def test_missing_file_prints_snippet_and_creates_nothing(self):
        with private_tempdir() as td:
            fake_cfg = Path(td) / "openclaw.json"
            with patch.dict(iops.SDKS["openclaw"], {"config_path": fake_cfg}), \
                 patch("core.cli._install_ops.print_manual_snippet") as mock_snippet:
                iops.install_openclaw("tok", "8000")
            mock_snippet.assert_called_once()
            self.assertFalse(fake_cfg.exists())


class TestInstallClaudeExistingFileFlow(IsolatedInstallCase):
    """端到端：install_claude 在文件存在场景下，确认后才写入。"""

    def test_confirm_yes_writes_expected_fields(self):
        with private_tempdir() as td:
            cfg_path = Path(td) / "settings.json"
            cfg_path.write_text(json.dumps({"env": {}}, indent=2) + "\n", encoding="utf-8")
            with patch.dict(iops.SDKS["claude"], {"config_path": cfg_path}), \
                 patch("core.cli._install_ops.confirm", return_value=True), \
                 patch("core.cli._install_ops._ensure_onboarding_completed") as mock_onboarding:
                iops.install_claude("mytoken", "9000")
            written = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(written["env"]["ANTHROPIC_AUTH_TOKEN"], "mytoken")
            self.assertEqual(written["env"]["ANTHROPIC_BASE_URL"], "http://localhost:9000/")
            baks = self.backups_for(cfg_path)
            self.assertEqual(len(baks), 1)
            # settings.json 写入后无条件调用了 _ensure_onboarding_completed
            mock_onboarding.assert_called_once()

    def test_confirm_no_leaves_original_untouched(self):
        with private_tempdir() as td:
            cfg_path = Path(td) / "settings.json"
            original = json.dumps({"env": {}}, indent=2) + "\n"
            cfg_path.write_text(original, encoding="utf-8")
            with patch.dict(iops.SDKS["claude"], {"config_path": cfg_path}), \
                 patch("core.cli._install_ops.confirm", return_value=False), \
                 patch("core.cli._install_ops._ensure_onboarding_completed") as mock_onboarding:
                iops.install_claude("mytoken", "9000")
            self.assertEqual(cfg_path.read_text(encoding="utf-8"), original)
            baks = self.backups_for(cfg_path)
            self.assertEqual(len(baks), 0)
            # 用户取消后不得继续写 onboarding。
            mock_onboarding.assert_not_called()


# ---------------------------------------------------------------------------
# SessionStart hook（ensure_model_proxy.sh）检测/归一化/修复
# 全部用 tempfile 构造的临时 settings.json，绝不碰真实 .claude/settings.json。
# ---------------------------------------------------------------------------

def _v1_entry():
    return {
        "hooks": [{
            "type": "command",
            "command": 'bash "${CLAUDE_PROJECT_DIR}/tools/ensure_proxy.sh"',
        }]
    }


def _websearch_entry():
    return {
        "hooks": [{
            "type": "command",
            "command": 'bash "${CLAUDE_PROJECT_DIR}/.claude/skills/websearch-router/'
                       'runtime/ensure_websearch.sh"',
        }]
    }


def _correct_entry():
    return {
        "hooks": [{"type": "command", "command": iops._expected_hook_command()}]
    }


def _stale_entry(rel_path="tools/OLD_LOCATION/hooker/ensure_model_proxy.sh"):
    return {
        "hooks": [{
            "type": "command",
            "command": f'bash "${{CLAUDE_PROJECT_DIR}}/{rel_path}"',
        }]
    }


class TestNormalizeSessionStart(IsolatedInstallCase):
    """_normalize_session_start 纯函数用例，不涉 IO，直接喂构造的 list。"""

    def setUp(self):
        super().setUp()
        tmpdir = private_tempdir()
        self.addCleanup(tmpdir.cleanup)
        self.vault_root = Path(tmpdir.name)

    def test_1_already_correct_is_idempotent(self):
        """用例1：已存在唯一 correct 条 → 返回列表 == 入参（幂等，触发 no-write）。"""
        entries = [_v1_entry(), _websearch_entry(), _correct_entry()]
        result = iops._normalize_session_start(entries, self.vault_root)
        self.assertEqual(result, entries)

    def test_2_stale_only_replaced_others_kept_in_order(self):
        """不可证明归属的旧路径须保留，并追加当前受管 hook。"""
        v1, ws, stale = _v1_entry(), _websearch_entry(), _stale_entry()
        entries = [v1, ws, stale]
        result = iops._normalize_session_start(entries, self.vault_root)
        self.assertEqual(result, [v1, ws, stale, _correct_entry()])

    def test_3_correct_and_stale_mixed_keeps_first_correct_only(self):
        """correct + 未证实归属路径混存时，保留两者及其他条目。"""
        v1, ws = _v1_entry(), _websearch_entry()
        correct, stale = _correct_entry(), _stale_entry()
        entries = [v1, correct, stale, ws]
        result = iops._normalize_session_start(entries, self.vault_root)
        self.assertEqual(result, [v1, correct, stale, ws])

    def test_4_multiple_correct_dedup_keeps_first(self):
        """用例4：多条 correct 重复 → 只留第一条。"""
        correct1, correct2 = _correct_entry(), _correct_entry()
        v1 = _v1_entry()
        entries = [correct1, v1, correct2]
        result = iops._normalize_session_start(entries, self.vault_root)
        self.assertEqual(result, [correct1, v1])
        self.assertEqual(len(result), 2)

    def test_5_no_hits_appends_correct_others_untouched(self):
        """用例5：完全无命中 → 末尾追加 correct，原有 v1/websearch 两条不动、顺序不变。"""
        v1, ws = _v1_entry(), _websearch_entry()
        entries = [v1, ws]
        result = iops._normalize_session_start(entries, self.vault_root)
        self.assertEqual(result, [v1, ws, _correct_entry()])

    def test_idempotence_is_structural_not_textual(self):
        """幂等判定必须基于数据结构比较，不能是文本比较：构造一个值相同、但
        JSON key 顺序不同（"command" 键在 "type" 键之前，文本序列化结果必然
        不同）的 correct 条目，确认 _normalize_session_start 仍判定它已是
        correct（值相等），不会因为 key 顺序不同而误判为需要改动。"""
        reordered_correct = {
            "hooks": [{
                "command": iops._expected_hook_command(),  # command 在前
                "type": "command",                          # type 在后（顺序反了）
            }]
        }
        entries = [_v1_entry(), _websearch_entry(), reordered_correct]
        result = iops._normalize_session_start(entries, self.vault_root)

        # 数据结构比较：dict 值相等（顺序不影响 == 判定），应视为已是 correct，
        # 原列表原样返回，不发生任何删除/追加。
        self.assertEqual(result, entries)

        # 反证：若误用文本比较（先序列化再比字符串），reordered_correct 序列化
        # 后的文本会与 _correct_entry() 的标准序列化文本不同——说明这两者
        # "文本不同、结构相同"，恰好验证了比较必须基于结构而非文本。
        self.assertNotEqual(
            json.dumps(reordered_correct),
            json.dumps(_correct_entry()),
        )
        self.assertEqual(reordered_correct, _correct_entry())


class TestEnsureSessionHook(IsolatedInstallCase):
    """ensure_session_hook IO 用例（tempfile 造 settings.json）。"""

    def setUp(self):
        super().setUp()
        tmpdir = private_tempdir()
        self.addCleanup(tmpdir.cleanup)
        vault = Path(tmpdir.name)
        (vault / '.claude').mkdir(mode=0o700)
        self.settings_path = vault / '.claude' / 'settings.json'
        vault_patcher = patch.object(iops, "_VAULT_ROOT", vault)
        settings_patcher = patch.object(iops, "_CLAUDE_SETTINGS", self.settings_path)
        vault_patcher.start()
        settings_patcher.start()
        self.addCleanup(vault_patcher.stop)
        self.addCleanup(settings_patcher.stop)

    def _make_settings(self, td, session_start_entries=None, with_hooks_key=True):
        cfg = {
            "$schema": "https://json.schemastore.org/claude-code-settings.json",
            "permissions": {"allow": [], "deny": [], "ask": []},
        }
        if with_hooks_key:
            cfg["hooks"] = {
                "PreToolUse": [{
                    "matcher": "mcp__open-websearch__search",
                    "hooks": [{
                        "type": "command",
                        "command": 'bash "${CLAUDE_PROJECT_DIR}/.claude/skills/'
                                   'websearch-router/runtime/ensure_websearch.sh"',
                    }],
                }],
                "PostToolUse": [{
                    "matcher": "Agent",
                    "hooks": [{
                        "type": "command",
                        "command": 'bash "${CLAUDE_PROJECT_DIR}/tools/guard_hooks/'
                                   'verify_progress.sh"',
                    }],
                }],
            }
            if session_start_entries is not None:
                cfg["hooks"]["SessionStart"] = session_start_entries
        cfg["enabledPlugins"] = {"claude-hud@claude-hud": True}
        path = self.settings_path
        path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
        return path, cfg

    def test_6_already_correct_no_write_no_backup(self):
        """用例6：已正确 → 不写、不产生 .bak（patch confirm 不应被调用，
        且文件字节不变）。"""
        with private_tempdir() as td:
            path, _ = self._make_settings(
                td, session_start_entries=[_v1_entry(), _websearch_entry(),
                                           _correct_entry()])
            original_bytes = path.read_bytes()
            with patch("core.cli._install_ops.confirm") as mock_confirm:
                iops.ensure_session_hook(path)
                mock_confirm.assert_not_called()
            self.assertEqual(path.read_bytes(), original_bytes)
            baks = self.backups_for(path)
            self.assertEqual(len(baks), 0)

    def test_world_readable_correct_hook_does_not_block_client_install(self):
        path, _ = self._make_settings(None, session_start_entries=[_correct_entry()])
        path.chmod(0o644)
        original = path.read_bytes()
        client = self.fake_home / '.openclaw' / 'openclaw.json'
        client.parent.mkdir(mode=0o700)
        client.write_text('{}', encoding='utf-8')
        client.chmod(0o600)
        output = io.StringIO()
        root = iops._WRITE_CONTEXT.get()['root']
        with patch.dict(iops.SDKS['openclaw'], {'config_path': client}), \
             patch.object(iops, 'choose_token', return_value='fake-token'), \
             patch.object(iops, 'confirm', return_value=True), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = iops.cmd_install(str(self.fake_config), '12345', ['openclaw'],
                                      preflight_root=root)
        self.assertEqual(result, 0, output.getvalue())
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.backups_for(path), [])
        self.assertEqual(list(path.parent.glob('settings.json.bak.*')), [])
        self.assertEqual(json.loads(client.read_text())['models']['providers']['model_proxy']['apiKey'],
                         'fake-token')
        self.assertNotIn('fake-token', output.getvalue())

    def test_world_readable_hook_needing_edit_remains_write_protected(self):
        path, _ = self._make_settings(None, session_start_entries=[])
        path.chmod(0o644)
        original = path.read_bytes()
        with patch.object(iops, 'confirm') as confirm:
            with self.assertRaises(iops.InstallFailure):
                iops.ensure_session_hook(path)
        confirm.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.backups_for(path), [])
        self.assertEqual(list(path.parent.glob('settings.json.bak.*')), [])
    def test_group_writable_correct_hook_does_not_gain_readonly_exception(self):
        path, _ = self._make_settings(None, session_start_entries=[_correct_entry()])
        path.chmod(0o664)
        original = path.read_bytes()
        with patch.object(iops, 'confirm') as confirm:
            with self.assertRaises(iops.InstallFailure):
                iops.ensure_session_hook(path)
        confirm.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mode & 0o777, 0o664)

    def test_readonly_hook_symlink_does_not_gain_readonly_exception(self):
        path, _ = self._make_settings(None, session_start_entries=[_correct_entry()])
        actual = path.with_name('fake-target.json')
        path.rename(actual)
        actual.chmod(0o644)
        original = actual.read_bytes()
        path.symlink_to(actual.name)
        with patch.object(iops, 'confirm') as confirm:
            with self.assertRaises(iops.InstallFailure):
                iops.ensure_session_hook(path)
        confirm.assert_not_called()
        self.assertTrue(path.is_symlink())
        self.assertEqual(actual.read_bytes(), original)

    def test_readonly_hook_still_rejects_unknown_file_acl(self):
        from core import secure_files
        path, _ = self._make_settings(None, session_start_entries=[_correct_entry()])
        path.chmod(0o644)
        original = path.read_bytes()
        native_protection = secure_files._mac_fd_protection
        def protection(fd, *, directory):
            if not directory and os.fstat(fd).st_mode & 0o777 == 0o644:
                raise secure_files.SecureSnapshotError('acl_unavailable')
            return native_protection(fd, directory=directory)
        with patch.object(secure_files, '_mac_fd_protection', side_effect=protection), \
             patch.object(iops, 'confirm') as confirm:
            with self.assertRaises(iops.InstallFailure):
                iops.ensure_session_hook(path)
        confirm.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_readonly_hook_snapshot_failure_does_not_retry_as_write(self):
        from core import secure_files
        path, _ = self._make_settings(None, session_start_entries=[_correct_entry()])
        path.chmod(0o644)
        original = path.read_bytes()
        with patch.object(iops, 'capture_target',
                          side_effect=secure_files.SecureSnapshotError('release_unknown')) as capture, \
             patch.object(iops, 'confirm') as confirm:
            with self.assertRaises(iops.InstallFailure) as caught:
                iops.ensure_session_hook(path)
        self.assertIn('release_unknown', str(caught.exception))
        capture.assert_called_once()
        confirm.assert_not_called()
        self.assertEqual(path.read_bytes(), original)

    def test_7_needs_fix_confirm_true_writes_single_correct_others_untouched(self):
        """用例7：需修复 + confirm=True → 写入后 SessionStart 恰一条 correct，
        且 PreToolUse/PostToolUse 等其他字段逐字节不变（解析比对）。"""
        with private_tempdir() as td:
            path, original_cfg = self._make_settings(
                td, session_start_entries=[_v1_entry(), _stale_entry()])
            with patch("core.cli._install_ops.confirm", return_value=True):
                iops.ensure_session_hook(path)
            written = json.loads(path.read_text(encoding="utf-8"))
            session_start = written["hooks"]["SessionStart"]
            correct_count = sum(
                1 for e in session_start if e == _correct_entry()
            )
            self.assertEqual(correct_count, 1)
            # 未能证明旧路径归属，须保留，且准确新增唯一受管 hook。
            self.assertEqual(session_start, [_v1_entry(), _stale_entry(), _correct_entry()])
            # 其他字段（PreToolUse/PostToolUse/permissions/enabledPlugins）不变
            self.assertEqual(written["hooks"]["PreToolUse"],
                              original_cfg["hooks"]["PreToolUse"])
            self.assertEqual(written["hooks"]["PostToolUse"],
                              original_cfg["hooks"]["PostToolUse"])
            self.assertEqual(written["permissions"], original_cfg["permissions"])
            self.assertEqual(written["enabledPlugins"], original_cfg["enabledPlugins"])
            baks = self.backups_for(path)
            self.assertEqual(len(baks), 1)

    def test_8_needs_fix_confirm_false_file_unchanged_no_backup(self):
        """用例8：需修复 + confirm=False → 文件不变、无 bak。"""
        with private_tempdir() as td:
            path, _ = self._make_settings(
                td, session_start_entries=[_v1_entry(), _stale_entry()])
            original_bytes = path.read_bytes()
            with patch("core.cli._install_ops.confirm", return_value=False):
                iops.ensure_session_hook(path)
            self.assertEqual(path.read_bytes(), original_bytes)
            baks = self.backups_for(path)
            self.assertEqual(len(baks), 0)

    def test_idempotence_based_on_structure_not_file_text(self):
        """幂等判定必须基于数据结构比较、不能是文本比较：构造一份 settings.json，
        其 SessionStart 的 correct 条目 JSON key 顺序与 _correct_entry() 标准
        序列化顺序不同（"command" 键写在 "type" 键之前），值语义相同、
        文本字节必然不同。确认 ensure_session_hook 判定为"已正确"、不调用
        confirm、不产生写入/备份——证明比较发生在 json.loads 之后的数据结构层，
        而非原始文本层。"""
        with private_tempdir() as td:
            reordered_correct_text = (
                '{\n  "command": "%s",\n  "type": "command"\n}'
                % iops._expected_hook_command().replace('"', '\\"')
            )
            # 手写一份 settings.json，SessionStart 的第三条 hook 键顺序反了
            # （command 在 type 之前），但 json.loads 后值与标准 correct 条目相等。
            raw = (
                '{\n'
                '  "hooks": {\n'
                '    "SessionStart": [\n'
                '      {"hooks": [{"type": "command", "command": '
                '"bash \\"${CLAUDE_PROJECT_DIR}/tools/ensure_proxy.sh\\""}]},\n'
                '      {"hooks": [' + reordered_correct_text + ']}\n'
                '    ]\n'
                '  }\n'
                '}\n'
            )
            path = self.settings_path
            path.write_text(raw, encoding="utf-8")

            # 先确认这份文本与"标准序列化后的等价结构"文本确实不同——
            # 证明这是一个真实的"文本不同、结构相同"场景，不是空测试。
            parsed = json.loads(raw)
            standard_text = json.dumps(parsed, indent=2, ensure_ascii=False) + "\n"
            self.assertNotEqual(raw, standard_text)

            original_bytes = path.read_bytes()
            with patch("core.cli._install_ops.confirm") as mock_confirm:
                iops.ensure_session_hook(path)
                mock_confirm.assert_not_called()
            self.assertEqual(path.read_bytes(), original_bytes)
            baks = self.backups_for(path)
            self.assertEqual(len(baks), 0)

    def test_9_missing_hooks_key_injects_without_losing_other_keys(self):
        """用例9：settings.json 无 hooks 键 → 注入后结构正确、其他键不丢。"""
        with private_tempdir() as td:
            path, original_cfg = self._make_settings(td, with_hooks_key=False)
            self.assertNotIn("hooks", original_cfg)
            with patch("core.cli._install_ops.confirm", return_value=True):
                iops.ensure_session_hook(path)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["hooks"]["SessionStart"], [_correct_entry()])
            # 原有其他键不丢
            self.assertEqual(written["permissions"], original_cfg["permissions"])
            self.assertEqual(written["$schema"], original_cfg["$schema"])
            self.assertEqual(written["enabledPlugins"], original_cfg["enabledPlugins"])

    def test_default_arg_path_reads_module_constant_at_call_time(self):
        """默认参数分支（settings_path=None）必须在调用时读取模块级 _CLAUDE_SETTINGS，
        而不是在 def 执行时就把默认值绑死——否则 patch("core.cli._install_ops._CLAUDE_SETTINGS", ...)
        无法覆盖 cmd_install() 实际触发的这条默认参数路径，这条路径就会永远测不到
        （reviewer 发现的真实覆盖缺口，见 2026-07-22 复核记录）。
        用不传 settings_path、只 patch 模块常量的方式调用，验证确实读到了 patch 后的临时路径，
        不是读到真实的 ~/.claude/settings.json。"""
        with private_tempdir() as td:
            path, _ = self._make_settings(
                td, session_start_entries=[_v1_entry(), _websearch_entry(),
                                           _correct_entry()])
            original_bytes = path.read_bytes()
            with patch("core.cli._install_ops._CLAUDE_SETTINGS", path), \
                 patch("core.cli._install_ops.confirm") as mock_confirm:
                iops.ensure_session_hook()  # 不传参，走默认参数分支
                mock_confirm.assert_not_called()
            # 已正确状态：文件字节不变、无备份——同时证明本次调用确实作用在
            # patch 后的临时文件上（若误读真实文件，行为不可预测/可能报错或改动真实文件）
            self.assertEqual(path.read_bytes(), original_bytes)
            baks = self.backups_for(path)
            self.assertEqual(len(baks), 0)


if __name__ == "__main__":
    unittest.main()
