"""_format_ops.py 单测：覆盖 S4/S3/S7/S10/脱敏统一/菜单冒烟。

运行：cd tools/model_proxy && python3 -m unittest tests.test_format_ops -v
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _format_ops import (
    display_width,
    format_routes,
    format_strategies,
    format_supplies,
    mask_appkey,
    normalize_supply,
    strategy_route_desc,
    _format_status_offline,
    _format_status_from_json,
)


# ---------------------------------------------------------------------------
# S4 回归：覆盖计数无条件拼接（单值 route_id 与 pool 写法一视同仁）
# ---------------------------------------------------------------------------

class TestS4OverrideCount(unittest.TestCase):

    def test_single_route_id_with_count_shows_override(self):
        """单值 route_id strategy + count>0 → 输出含覆盖计数（S4 回归）。"""
        strategies = [{"client_token": "cc", "route_id": "nation1", "note": "test"}]
        counts = {"cc": 2}
        lines = format_strategies(strategies, style="status", override_counts=counts)
        joined = "\n".join(lines)
        self.assertIn("覆盖: 2个session", joined)
        self.assertIn("cc", joined)
        self.assertIn("nation1", joined)

    def test_pool_route_with_count_shows_override(self):
        """pool 写法 strategy + count>0 → 输出含覆盖计数。"""
        strategies = [{"client_token": "cc", "route_pool": [{"route_id": "nation1", "weight": 1}], "note": "test"}]
        counts = {"cc": 3}
        lines = format_strategies(strategies, style="status", override_counts=counts)
        joined = "\n".join(lines)
        self.assertIn("覆盖: 3个session", joined)
        self.assertIn("pool[nation1:1]", joined)

    def test_single_route_id_count_zero_shows_no_override(self):
        """单值 route_id strategy + count=0 → 输出 (无)（S3: 无条件展示）。"""
        strategies = [{"client_token": "cc", "route_id": "nation1", "note": "test"}]
        counts = {"cc": 0}
        lines = format_strategies(strategies, style="status", override_counts=counts)
        joined = "\n".join(lines)
        self.assertIn("覆盖: (无)", joined)


# ---------------------------------------------------------------------------
# S3：覆盖行无条件展示
# ---------------------------------------------------------------------------

class TestS3UnconditionalOverride(unittest.TestCase):

    def test_count_zero_shows_wu(self):
        """count=0 → 含 (无)。"""
        strategies = [{"client_token": "cc", "route_id": "nation1"}]
        lines = format_strategies(strategies, style="status", override_counts={"cc": 0})
        self.assertIn("覆盖: (无)", "\n".join(lines))

    def test_count_positive_shows_source(self):
        """count>0 → 含来源标注。"""
        strategies = [{"client_token": "cc", "route_id": "nation1"}]
        lines = format_strategies(strategies, style="status", override_counts={"cc": 1})
        joined = "\n".join(lines)
        self.assertIn("覆盖: 1个session", joined)
        self.assertIn("sidecar", joined)
        self.assertIn("$route", joined)

    def test_no_override_counts_still_shows_wu(self):
        """override_counts=None → 仍打印覆盖行 (无)（无条件展示）。"""
        strategies = [{"client_token": "cc", "route_id": "nation1"}]
        lines = format_strategies(strategies, style="status", override_counts=None)
        self.assertIn("覆盖: (无)", "\n".join(lines))


# ---------------------------------------------------------------------------
# S7：80 列约束
# ---------------------------------------------------------------------------

class TestS7ColumnWidth(unittest.TestCase):

    def test_status_preset_max_width_le_80(self):
        """镜像真实 config 极端值的 fixture → status preset 每行 display_width <= 80。

        实测 max sid=26 字符（如 claude-opus-sankuai-0956），max model=17（deepseek-v4-flash），
        max protocol=9（responses）。STATUS preset 裸值无标签，最坏约 71 列。
        """
        # 使用真实 config 里的最长 id + 最长 model
        supplies = [
            {"id": "claude-opus-sankuai-0956", "protocol": "anthropic",
             "target_model": "claude-opus-5", "appkey": "1234567890956",
             "reasoning_capability": {"effort_enum": ["high"]}},
            {"id": "deepseek-v4-flash-sankuai-0956", "protocol": "responses",
             "target_model": "deepseek-v4-flash", "appkey": "1234567890",
             "reasoning_capability": {"effort_enum": ["high"]}},
        ]
        lines = format_supplies(supplies, preset="STATUS")
        for line in lines:
            self.assertLessEqual(display_width(line), 80,
                                 f"line exceeds 80: {line!r} (width={display_width(line)})")

    def test_routes_vertical_layout_le_80(self):
        """nation 式 3×20 字符档 → 竖排每行 <= 80。"""
        # 模拟极端长 id 列表（3 个 supply id 各 ~26 字符）
        long_ids = [f"kimi-k3-sankuai-{i:04d}-extra" for i in range(100, 103)]
        routes = [{
            "id": "nation1",
            "tiers": {"opus": long_ids, "sonnet": long_ids, "haiku": long_ids},
            "failover": "on",
        }]
        lines = format_routes(routes)
        for line in lines:
            self.assertLessEqual(display_width(line), 80,
                                 f"line exceeds 80: {line!r} (width={display_width(line)})")

    def test_routes_fold_continuation_le_80_and_restorable(self):
        """5+ 长 id 单档 → 折行续行全 <=80，去缩进拼回 == 原逗号串。

        还原方式：去掉每行的缩进 + tier label 前缀（如 `opus:     `），
        只保留 id 串部分，直接拼接 == 原逗号串。
        """
        # 构造单档 6 个长 id，使单行必超 80
        ids = [f"very-long-supply-id-name-{i:04d}-suffix" for i in range(1, 7)]
        routes = [{
            "id": "nation1",
            "tiers": {"opus": ids, "sonnet": [], "haiku": []},
            "failover": "on",
        }]
        lines = format_routes(routes)
        # 找到 opus 段的折行行（含 id 的行）
        opus_lines = [l for l in lines if "very-long-supply-id" in l]
        self.assertGreater(len(opus_lines), 1, "expected folding for long id list")
        for line in opus_lines:
            self.assertLessEqual(display_width(line), 80,
                                 f"folded line exceeds 80: {line!r}")
        # 去缩进 + 去 tier label 前缀，拼回 == 原逗号串
        # 第一行格式: "    opus:     id1"，续行格式: "    ,id2"
        # strip 后: "opus:     id1" + ",id2" → 需再去 label 前缀
        import re
        id_parts = []
        for l in opus_lines:
            s = l.strip()
            # 去掉行首的 tier label（如 "opus:" + 空格）
            s = re.sub(r'^(?:opus|sonnet|haiku):\s*', '', s)
            id_parts.append(s)
        joined = "".join(id_parts)
        self.assertEqual(joined, ",".join(ids),
                         f"folded ids not restorable: {joined!r} != {','.join(ids)!r}")


# ---------------------------------------------------------------------------
# S10：停机降级
# ---------------------------------------------------------------------------

class TestS10OfflineMode(unittest.TestCase):

    def test_offline_shows_static_segments_and_not_running(self):
        """status-offline：tmp config → 含三段 + cooldown 标未运行。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            cfg = {
                "default_cooldown_seconds": 60,
                "supplies": [{"id": "s1", "protocol": "anthropic", "appkey": "1234", "target_model": "m1"}],
                "routes": [{"id": "r1", "tiers": {"opus": ["s1"], "sonnet": [], "haiku": []}, "failover": "on"}],
                "strategies": [{"client_token": "cc", "route_id": "r1", "note": "n"}],
            }
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            lines = _format_status_offline(str(cfg_path))
            joined = "\n".join(lines)
            self.assertIn("supplies:", joined)
            self.assertIn("routes", joined)
            self.assertIn("strategies", joined)
            self.assertIn("cooldown: (代理未运行)", joined)
            self.assertIn("default_cooldown_seconds: 60", joined)

    def test_offline_sidecar_missing_shows_wu(self):
        """sidecar 文件缺失 → 覆盖: (无) 不崩。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            cfg = {
                "default_cooldown_seconds": 60,
                "supplies": [],
                "routes": [],
                "strategies": [{"client_token": "cc", "route_id": "r1"}],
            }
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            # 不创建 session_overrides.json
            lines = _format_status_offline(str(cfg_path))
            joined = "\n".join(lines)
            self.assertIn("覆盖: (无)", joined)

    def test_offline_sidecar_with_override_shows_count(self):
        """sidecar 有 1 条 override → 计数正确。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            cfg = {
                "default_cooldown_seconds": 60,
                "supplies": [],
                "routes": [],
                "strategies": [{"client_token": "cc", "route_id": "r1"}],
            }
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            sidecar_path = Path(td) / "session_overrides.json"
            sidecar_path.write_text(json.dumps({
                "cc": {"session-abc": {"route_id": "nation1", "last_seen": 0, "created": 0}}
            }), encoding="utf-8")
            lines = _format_status_offline(str(cfg_path))
            joined = "\n".join(lines)
            self.assertIn("覆盖: 1个session", joined)


# ---------------------------------------------------------------------------
# 脱敏统一
# ---------------------------------------------------------------------------

class TestMaskAppkey(unittest.TestCase):

    def test_empty_appkey_shows_kong(self):
        self.assertEqual(mask_appkey(""), "(空)")

    def test_short_appkey_shows_tail4(self):
        self.assertEqual(mask_appkey("1234"), "...1234")

    def test_long_appkey_shows_tail4(self):
        self.assertEqual(mask_appkey("1907340802784210956"), "...0956")

    def test_normalize_supply_config_source(self):
        """config 原生 supply（含 appkey）→ 脱敏走 mask_appkey。"""
        d = {"id": "s1", "protocol": "anthropic", "appkey": "1234567890956", "target_model": "m1"}
        norm = normalize_supply(d)
        self.assertEqual(norm["key_masked"], "...0956")

    def test_normalize_supply_server_source(self):
        """server status JSON（appkey_tail4）→ 脱敏一致。"""
        d = {"id": "s1", "protocol": "anthropic", "appkey_tail4": "0956", "target_model": "m1"}
        norm = normalize_supply(d)
        self.assertEqual(norm["key_masked"], "...0956")

    def test_normalize_supply_empty_appkey(self):
        """空 appkey → (空)。"""
        d = {"id": "s1", "protocol": "anthropic", "appkey": "", "target_model": "m1"}
        norm = normalize_supply(d)
        self.assertEqual(norm["key_masked"], "(空)")


# ---------------------------------------------------------------------------
# 菜单冒烟（单源化回归）
# ---------------------------------------------------------------------------

class TestMenuSmoke(unittest.TestCase):

    def _run_list(self, func_name, cfg):
        """redirect_stdout 调 _config_ops 的 list 函数，返回输出字符串。"""
        import _config_ops
        # 创建 tmp config 文件
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(cfg, f)
            cfg_path = f.name
        try:
            buf = io.StringIO()
            # patch done() 避免它输出 __RELOAD__ 或 sys.exit
            with redirect_stdout(buf):
                with patch.object(_config_ops, "done", lambda x: None):
                    func = getattr(_config_ops, func_name)
                    func(cfg_path)
            return buf.getvalue()
        finally:
            os.unlink(cfg_path)

    def test_supply_list_non_empty_with_ids(self):
        cfg = {"supplies": [{"id": "s1", "protocol": "anthropic", "appkey": "1234", "target_model": "m1"}],
               "routes": [], "strategies": []}
        out = self._run_list("supply_list", cfg)
        self.assertIn("s1", out)
        self.assertIn("anthropic", out)

    def test_route_list_non_empty_with_ids(self):
        cfg = {"supplies": [],
               "routes": [{"id": "r1", "tiers": {"opus": ["s1"], "sonnet": [], "haiku": []}, "failover": "on"}],
               "strategies": []}
        out = self._run_list("route_list", cfg)
        self.assertIn("r1", out)
        self.assertIn("s1", out)

    def test_strategy_list_non_empty_with_tokens(self):
        cfg = {"supplies": [], "routes": [],
               "strategies": [{"client_token": "cc", "route_id": "r1", "note": "test"}]}
        out = self._run_list("strategy_list", cfg)
        self.assertIn("cc", out)
        self.assertIn("r1", out)


# ---------------------------------------------------------------------------
# strategy_route_desc 迁入验证
# ---------------------------------------------------------------------------

class TestStrategyRouteDesc(unittest.TestCase):

    def test_single_route_id(self):
        self.assertEqual(strategy_route_desc({"route_id": "nation1"}), "nation1")

    def test_pool(self):
        st = {"route_pool": [{"route_id": "nation1", "weight": 1}, {"route_id": "nation2", "weight": 2}]}
        self.assertEqual(strategy_route_desc(st), "pool[nation1:1,nation2:2]")

    def test_no_route(self):
        self.assertEqual(strategy_route_desc({}), "?")


# ---------------------------------------------------------------------------
# status-format 从 server JSON 格式化冒烟
# ---------------------------------------------------------------------------

class TestStatusFormatFromJson(unittest.TestCase):

    def test_full_status_format(self):
        """server JSON → 五段输出。"""
        data = {
            "supplies": [{"id": "s1", "protocol": "anthropic", "target_model": "m1", "appkey_tail4": "0956"}],
            "routes": [{"id": "r1", "tiers": {"opus": ["s1"], "sonnet": [], "haiku": []}, "failover": "on"}],
            "strategies": [{"client_token": "cc", "route_id": "r1", "sidecar_overrides_count": 0}],
            "cooldown": {},
            "default_cooldown_seconds": 60,
        }
        lines = _format_status_from_json(data)
        joined = "\n".join(lines)
        self.assertIn("supplies:", joined)
        self.assertIn("routes", joined)
        self.assertIn("strategies", joined)
        self.assertIn("cooldown: (无)", joined)
        self.assertIn("default_cooldown_seconds: 60", joined)

    def test_status_format_with_cooldown(self):
        """有 cooldown 时展示剩余秒。"""
        data = {
            "supplies": [],
            "routes": [],
            "strategies": [],
            "cooldown": {"s1": 30},
            "default_cooldown_seconds": 60,
        }
        lines = _format_status_from_json(data)
        joined = "\n".join(lines)
        self.assertIn("cooldown (剩余秒):", joined)
        self.assertIn("s1", joined)
        self.assertIn("30s", joined)


if __name__ == "__main__":
    unittest.main()
