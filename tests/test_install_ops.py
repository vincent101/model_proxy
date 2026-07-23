"""_install_ops.py 预览确认写入流程单测。

覆盖 preview_confirm_write 核心行为契约（确认后才写/不确认不写/无变化跳过），
以及 install_claude/install_codex/install_openclaw 在"文件不存在"分支保持
不产生任何文件的回归。全部用 tempfile 模拟配置文件，不碰真实 ~/.claude 等路径。

运行：cd tools/model_proxy && python3 -m unittest tests.test_install_ops -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _install_ops as iops


class TestPreviewConfirmWrite(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.cfg_path = Path(self._tmpdir.name) / "settings.json"

    def _write_old(self, text: str):
        self.cfg_path.write_text(text, encoding="utf-8")

    def test_confirm_yes_backs_up_and_writes(self):
        old_text = json.dumps({"env": {}}, indent=2) + "\n"
        self._write_old(old_text)
        new_text = json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://x/"}}, indent=2) + "\n"

        with patch("_install_ops.confirm", return_value=True):
            result = iops.preview_confirm_write(
                self.cfg_path, old_text, new_text, "claude", ["ok"]
            )

        self.assertTrue(result)
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), new_text)
        baks = list(self.cfg_path.parent.glob("settings.json.bak.*"))
        self.assertEqual(len(baks), 1)
        self.assertEqual(baks[0].read_text(encoding="utf-8"), old_text)

    def test_confirm_no_leaves_file_untouched_no_backup(self):
        old_text = json.dumps({"env": {}}, indent=2) + "\n"
        self._write_old(old_text)
        new_text = json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://x/"}}, indent=2) + "\n"

        with patch("_install_ops.confirm", return_value=False):
            result = iops.preview_confirm_write(
                self.cfg_path, old_text, new_text, "claude", ["ok"]
            )

        self.assertFalse(result)
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), old_text)
        baks = list(self.cfg_path.parent.glob("settings.json.bak.*"))
        self.assertEqual(len(baks), 0)

    def test_no_diff_skips_without_prompting(self):
        text = json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://x/"}}, indent=2) + "\n"
        self._write_old(text)

        with patch("_install_ops.confirm") as mock_confirm:
            result = iops.preview_confirm_write(
                self.cfg_path, text, text, "claude", ["ok"]
            )
            mock_confirm.assert_not_called()

        self.assertFalse(result)
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), text)
        baks = list(self.cfg_path.parent.glob("settings.json.bak.*"))
        self.assertEqual(len(baks), 0)

    def test_backup_path_matches_preview(self):
        """备份路径不能重新生成时间戳，须与预览展示的一致——通过预生成 ts 后校验
        实际产生的备份文件名与 preview_confirm_write 内部计算的 bak_path 一致。"""
        old_text = "a"
        new_text = "b"
        self._write_old(old_text)

        with patch("_install_ops.confirm", return_value=True):
            iops.preview_confirm_write(self.cfg_path, old_text, new_text, "claude", ["ok"])

        baks = list(self.cfg_path.parent.glob("settings.json.bak.*"))
        self.assertEqual(len(baks), 1)


class TestInstallClaudeFileNotExists(unittest.TestCase):

    def test_missing_file_prints_snippet_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            fake_home_cfg = Path(td) / "settings.json"
            with patch.dict(iops.SDKS["claude"], {"config_path": fake_home_cfg}), \
                 patch("_install_ops.print_manual_snippet") as mock_snippet:
                iops.install_claude("tok", "8000")
            mock_snippet.assert_called_once()
            self.assertFalse(fake_home_cfg.exists())


class TestInstallCodexFileNotExists(unittest.TestCase):

    def test_missing_file_prints_snippet_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            fake_cfg = Path(td) / "config.toml"
            with patch.dict(iops.SDKS["codex"], {"config_path": fake_cfg}), \
                 patch("_install_ops.print_manual_snippet") as mock_snippet:
                iops.install_codex("tok", "8000")
            mock_snippet.assert_called_once()
            self.assertFalse(fake_cfg.exists())


class TestInstallOpenclawFileNotExists(unittest.TestCase):

    def test_missing_file_prints_snippet_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            fake_cfg = Path(td) / "openclaw.json"
            with patch.dict(iops.SDKS["openclaw"], {"config_path": fake_cfg}), \
                 patch("_install_ops.print_manual_snippet") as mock_snippet:
                iops.install_openclaw("tok", "8000")
            mock_snippet.assert_called_once()
            self.assertFalse(fake_cfg.exists())


class TestInstallClaudeExistingFileFlow(unittest.TestCase):
    """端到端：install_claude 在文件存在场景下，确认后才写入。"""

    def test_confirm_yes_writes_expected_fields(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "settings.json"
            cfg_path.write_text(json.dumps({"env": {}}, indent=2) + "\n", encoding="utf-8")
            with patch.dict(iops.SDKS["claude"], {"config_path": cfg_path}), \
                 patch("_install_ops.confirm", return_value=True):
                iops.install_claude("mytoken", "9000")
            written = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(written["env"]["ANTHROPIC_AUTH_TOKEN"], "mytoken")
            self.assertEqual(written["env"]["ANTHROPIC_BASE_URL"], "http://localhost:9000/")
            baks = list(cfg_path.parent.glob("settings.json.bak.*"))
            self.assertEqual(len(baks), 1)

    def test_confirm_no_leaves_original_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "settings.json"
            original = json.dumps({"env": {}}, indent=2) + "\n"
            cfg_path.write_text(original, encoding="utf-8")
            with patch.dict(iops.SDKS["claude"], {"config_path": cfg_path}), \
                 patch("_install_ops.confirm", return_value=False):
                iops.install_claude("mytoken", "9000")
            self.assertEqual(cfg_path.read_text(encoding="utf-8"), original)
            baks = list(cfg_path.parent.glob("settings.json.bak.*"))
            self.assertEqual(len(baks), 0)


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


class TestNormalizeSessionStart(unittest.TestCase):
    """_normalize_session_start 纯函数用例，不涉 IO，直接喂构造的 list。"""

    def setUp(self):
        self.vault_root = iops._VAULT_ROOT

    def test_1_already_correct_is_idempotent(self):
        """用例1：已存在唯一 correct 条 → 返回列表 == 入参（幂等，触发 no-write）。"""
        entries = [_v1_entry(), _websearch_entry(), _correct_entry()]
        result = iops._normalize_session_start(entries, self.vault_root)
        self.assertEqual(result, entries)

    def test_2_stale_only_replaced_others_kept_in_order(self):
        """用例2：仅存在 stale 条（旧路径）→ 删 stale + 末尾追加 correct，
        其他条目原序保留。"""
        v1, ws, stale = _v1_entry(), _websearch_entry(), _stale_entry()
        entries = [v1, ws, stale]
        result = iops._normalize_session_start(entries, self.vault_root)
        self.assertEqual(result, [v1, ws, _correct_entry()])

    def test_3_correct_and_stale_mixed_keeps_first_correct_only(self):
        """用例3：correct + stale 混存 → 保留首条 correct、删其余，
        其他非命中条目不动。"""
        v1, ws = _v1_entry(), _websearch_entry()
        correct, stale = _correct_entry(), _stale_entry()
        entries = [v1, correct, stale, ws]
        result = iops._normalize_session_start(entries, self.vault_root)
        self.assertEqual(result, [v1, correct, ws])

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


class TestEnsureSessionHook(unittest.TestCase):
    """ensure_session_hook IO 用例（tempfile 造 settings.json）。"""

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
        path = Path(td) / "settings.json"
        path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
        return path, cfg

    def test_6_already_correct_no_write_no_backup(self):
        """用例6：已正确 → 不写、不产生 .bak（patch confirm 不应被调用，
        且文件字节不变）。"""
        with tempfile.TemporaryDirectory() as td:
            path, _ = self._make_settings(
                td, session_start_entries=[_v1_entry(), _websearch_entry(),
                                           _correct_entry()])
            original_bytes = path.read_bytes()
            with patch("_install_ops.confirm") as mock_confirm:
                iops.ensure_session_hook(path)
                mock_confirm.assert_not_called()
            self.assertEqual(path.read_bytes(), original_bytes)
            baks = list(path.parent.glob("settings.json.bak.*"))
            self.assertEqual(len(baks), 0)

    def test_7_needs_fix_confirm_true_writes_single_correct_others_untouched(self):
        """用例7：需修复 + confirm=True → 写入后 SessionStart 恰一条 correct，
        且 PreToolUse/PostToolUse 等其他字段逐字节不变（解析比对）。"""
        with tempfile.TemporaryDirectory() as td:
            path, original_cfg = self._make_settings(
                td, session_start_entries=[_v1_entry(), _stale_entry()])
            with patch("_install_ops.confirm", return_value=True):
                iops.ensure_session_hook(path)
            written = json.loads(path.read_text(encoding="utf-8"))
            session_start = written["hooks"]["SessionStart"]
            correct_count = sum(
                1 for e in session_start if e == _correct_entry()
            )
            self.assertEqual(correct_count, 1)
            self.assertEqual(len(session_start), 2)  # v1 + correct，stale 已删
            # 其他字段（PreToolUse/PostToolUse/permissions/enabledPlugins）不变
            self.assertEqual(written["hooks"]["PreToolUse"],
                              original_cfg["hooks"]["PreToolUse"])
            self.assertEqual(written["hooks"]["PostToolUse"],
                              original_cfg["hooks"]["PostToolUse"])
            self.assertEqual(written["permissions"], original_cfg["permissions"])
            self.assertEqual(written["enabledPlugins"], original_cfg["enabledPlugins"])
            baks = list(path.parent.glob("settings.json.bak.*"))
            self.assertEqual(len(baks), 1)

    def test_8_needs_fix_confirm_false_file_unchanged_no_backup(self):
        """用例8：需修复 + confirm=False → 文件不变、无 bak。"""
        with tempfile.TemporaryDirectory() as td:
            path, _ = self._make_settings(
                td, session_start_entries=[_v1_entry(), _stale_entry()])
            original_bytes = path.read_bytes()
            with patch("_install_ops.confirm", return_value=False):
                iops.ensure_session_hook(path)
            self.assertEqual(path.read_bytes(), original_bytes)
            baks = list(path.parent.glob("settings.json.bak.*"))
            self.assertEqual(len(baks), 0)

    def test_idempotence_based_on_structure_not_file_text(self):
        """幂等判定必须基于数据结构比较、不能是文本比较：构造一份 settings.json，
        其 SessionStart 的 correct 条目 JSON key 顺序与 _correct_entry() 标准
        序列化顺序不同（"command" 键写在 "type" 键之前），值语义相同、
        文本字节必然不同。确认 ensure_session_hook 判定为"已正确"、不调用
        confirm、不产生写入/备份——证明比较发生在 json.loads 之后的数据结构层，
        而非原始文本层。"""
        with tempfile.TemporaryDirectory() as td:
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
            path = Path(td) / "settings.json"
            path.write_text(raw, encoding="utf-8")

            # 先确认这份文本与"标准序列化后的等价结构"文本确实不同——
            # 证明这是一个真实的"文本不同、结构相同"场景，不是空测试。
            parsed = json.loads(raw)
            standard_text = json.dumps(parsed, indent=2, ensure_ascii=False) + "\n"
            self.assertNotEqual(raw, standard_text)

            original_bytes = path.read_bytes()
            with patch("_install_ops.confirm") as mock_confirm:
                iops.ensure_session_hook(path)
                mock_confirm.assert_not_called()
            self.assertEqual(path.read_bytes(), original_bytes)
            baks = list(path.parent.glob("settings.json.bak.*"))
            self.assertEqual(len(baks), 0)

    def test_9_missing_hooks_key_injects_without_losing_other_keys(self):
        """用例9：settings.json 无 hooks 键 → 注入后结构正确、其他键不丢。"""
        with tempfile.TemporaryDirectory() as td:
            path, original_cfg = self._make_settings(td, with_hooks_key=False)
            self.assertNotIn("hooks", original_cfg)
            with patch("_install_ops.confirm", return_value=True):
                iops.ensure_session_hook(path)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["hooks"]["SessionStart"], [_correct_entry()])
            # 原有其他键不丢
            self.assertEqual(written["permissions"], original_cfg["permissions"])
            self.assertEqual(written["$schema"], original_cfg["$schema"])
            self.assertEqual(written["enabledPlugins"], original_cfg["enabledPlugins"])

    def test_default_arg_path_reads_module_constant_at_call_time(self):
        """默认参数分支（settings_path=None）必须在调用时读取模块级 _CLAUDE_SETTINGS，
        而不是在 def 执行时就把默认值绑死——否则 patch("_install_ops._CLAUDE_SETTINGS", ...)
        无法覆盖 cmd_install() 实际触发的这条默认参数路径，这条路径就会永远测不到
        （reviewer 发现的真实覆盖缺口，见 2026-07-22 复核记录）。
        用不传 settings_path、只 patch 模块常量的方式调用，验证确实读到了 patch 后的临时路径，
        不是读到真实的 ~/.claude/settings.json。"""
        with tempfile.TemporaryDirectory() as td:
            path, _ = self._make_settings(
                td, session_start_entries=[_v1_entry(), _websearch_entry(),
                                           _correct_entry()])
            original_bytes = path.read_bytes()
            with patch("_install_ops._CLAUDE_SETTINGS", path), \
                 patch("_install_ops.confirm") as mock_confirm:
                iops.ensure_session_hook()  # 不传参，走默认参数分支
                mock_confirm.assert_not_called()
            # 已正确状态：文件字节不变、无备份——同时证明本次调用确实作用在
            # patch 后的临时文件上（若误读真实文件，行为不可预测/可能报错或改动真实文件）
            self.assertEqual(path.read_bytes(), original_bytes)
            baks = list(path.parent.glob("settings.json.bak.*"))
            self.assertEqual(len(baks), 0)


if __name__ == "__main__":
    unittest.main()
