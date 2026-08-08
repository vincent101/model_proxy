"""_format_ops.py 单测：P0 status 重设计后的覆盖。

运行：cd tools/model_proxy && python3 -m unittest tests.test_format_ops -v
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _format_ops import (
    DEGRADED_FAIL_PCT,
    DEGRADED_MIN_REQUESTS,
    _format_active_sessions,
    _format_status_from_json,
    _format_status_offline,
    _supply_refs,
    display_width,
    format_routes,
    format_strategies,
    format_supplies,
    load_active_sessions,
    load_supply_health,
    mask_appkey,
    normalize_supply,
    parse_access_line,
    strategy_route_desc,
)


# ---------------------------------------------------------------------------
# load_supply_health
# ---------------------------------------------------------------------------

class TestLoadSupplyHealth(unittest.TestCase):

    def _write_totals(self, td: str, day_str: str, combos: dict) -> str:
        """写一个最小 totals 文件，返回路径。"""
        path = os.path.join(td, "totals.json")
        data = {
            "version": 3,
            "days": {day_str: {"requests": 0, "ok": 0, "fail": 0, "combos": combos}},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def test_aggregate_by_supply(self):
        """combos 按 supply 聚合 requests/ok/fail 正确。"""
        from datetime import datetime, timezone, timedelta
        cst = timezone(timedelta(hours=8))
        today = datetime.now(cst).strftime("%Y-%m-%d")
        combos = {
            "supply=s1|route=r1|strategy=cc": {"requests": 10, "ok": 8, "fail": 2},
            "supply=s1|route=r2|strategy=cc": {"requests": 5, "ok": 5, "fail": 0},
            "supply=s2|route=r1|strategy=cc": {"requests": 3, "ok": 1, "fail": 2},
        }
        with tempfile.TemporaryDirectory() as td:
            path = self._write_totals(td, today, combos)
            health = load_supply_health(path)
        self.assertEqual(health["s1"], {"requests": 15, "ok": 13, "fail": 2})
        self.assertEqual(health["s2"], {"requests": 3, "ok": 1, "fail": 2})

    def test_file_missing_returns_empty(self):
        """文件缺失 → 返回 {}。"""
        health = load_supply_health("/nonexistent/path/totals.json")
        self.assertEqual(health, {})

    def test_json_corrupt_returns_empty(self):
        """JSON 损坏 → 返回 {}。"""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "totals.json")
            with open(path, "w") as f:
                f.write("{not valid json")
            health = load_supply_health(path)
        self.assertEqual(health, {})

    def test_none_supply_included_in_health(self):
        """(none) supply 也在 health 里（供 unmatched 段用）。"""
        from datetime import datetime, timezone, timedelta
        cst = timezone(timedelta(hours=8))
        today = datetime.now(cst).strftime("%Y-%m-%d")
        combos = {
            "supply=(none)|route=(none)|strategy=(none)": {"requests": 5, "ok": 0, "fail": 5},
        }
        with tempfile.TemporaryDirectory() as td:
            path = self._write_totals(td, today, combos)
            health = load_supply_health(path)
        self.assertIn("(none)", health)
        self.assertEqual(health["(none)"]["fail"], 5)


# ---------------------------------------------------------------------------
# compute_config_anomalies
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _supply_refs
# ---------------------------------------------------------------------------

class TestSupplyRefs(unittest.TestCase):

    def test_route_id_and_pool(self):
        """route_id 单值与 route_pool 两种写法的 strategy 引用都能标出。"""
        cfg = {
            "routes": [
                {"id": "r1", "tiers": {"opus": ["s1"], "sonnet": [], "haiku": []}},
                {"id": "r2", "tiers": {"opus": ["s1"], "sonnet": [], "haiku": []}},
            ],
            "strategies": [
                {"client_token": "cc", "route_pool": [{"route_id": "r1", "weight": 1}, {"route_id": "r2", "weight": 1}]},
                {"client_token": "codex", "route_id": "r2"},
            ],
        }
        refs = _supply_refs(cfg)
        self.assertEqual(sorted(refs["s1"]), ["r1.opus(cc)", "r2.opus(cc,codex)"])

    def test_unreferenced_supply(self):
        """未被引用的 supply 不在 refs 里（展示侧回退"未被引用"）。"""
        cfg = {
            "routes": [{"id": "r1", "tiers": {"opus": ["s1"], "sonnet": [], "haiku": []}}],
            "strategies": [{"client_token": "cc", "route_id": "r1"}],
        }
        refs = _supply_refs(cfg)
        self.assertNotIn("s-orphan", refs)
        self.assertEqual(refs["s1"], ["r1.opus(cc)"])


# ---------------------------------------------------------------------------
# S7：80 列约束（routes 竖排/折行保留；STATUS preset 已下线）
# ---------------------------------------------------------------------------

class TestS7ColumnWidth(unittest.TestCase):

    def test_routes_vertical_layout_le_80(self):
        """nation 式 3×20 字符档 → 竖排每行 <= 80。"""
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
        """5+ 长 id 单档 → 折行续行全 <=80，去缩进拼回 == 原逗号串。"""
        ids = [f"very-long-supply-id-name-{i:04d}-suffix" for i in range(1, 7)]
        routes = [{
            "id": "nation1",
            "tiers": {"opus": ids, "sonnet": [], "haiku": []},
            "failover": "on",
        }]
        lines = format_routes(routes)
        opus_lines = [l for l in lines if "very-long-supply-id" in l]
        self.assertGreater(len(opus_lines), 1, "expected folding for long id list")
        for line in opus_lines:
            self.assertLessEqual(display_width(line), 80,
                                 f"folded line exceeds 80: {line!r}")
        import re
        id_parts = []
        for l in opus_lines:
            s = l.strip()
            s = re.sub(r'^(?:opus|sonnet|haiku):\s*', '', s)
            id_parts.append(s)
        joined = "".join(id_parts)
        self.assertEqual(joined, ",".join(ids),
                         f"folded ids not restorable: {joined!r} != {','.join(ids)!r}")


# ---------------------------------------------------------------------------
# _format_status_from_json 新布局
# ---------------------------------------------------------------------------

class TestStatusFormatFromJson(unittest.TestCase):

    def _make_config(self, td, cfg_dict=None):
        """写 config 文件，返回路径。"""
        path = os.path.join(td, "config.json")
        cfg = cfg_dict or {
            "default_cooldown_seconds": 60,
            "supplies": [{"id": "s1", "protocol": "anthropic", "appkey": "1234", "target_model": "m1"}],
            "routes": [{"id": "r1", "tiers": {"opus": ["s1"], "sonnet": [], "haiku": []}, "failover": "on"}],
            "strategies": [{"client_token": "cc", "route_id": "r1", "note": "n"}],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return path

    def _make_totals(self, td, combos=None):
        """写 totals 文件，返回路径。"""
        path = os.path.join(td, "totals.json")
        from datetime import datetime, timezone, timedelta
        cst = timezone(timedelta(hours=8))
        today = datetime.now(cst).strftime("%Y-%m-%d")
        data = {"version": 3, "days": {today: {"combos": combos or {}}}}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def test_health_row_counts(self):
        """health 行含 cooldown/supplies/degraded/overrides/orphan 计数。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td)
            totals_path = self._make_totals(td)
            data = {
                "supplies": [{"id": "s1"}],
                "routes": [{"id": "r1", "tiers": {"opus": ["s1"], "sonnet": [], "haiku": []}}],
                "strategies": [{"client_token": "cc", "route_id": "r1", "sidecar_overrides_count": 1}],
                "cooldown": {},
                "default_cooldown_seconds": 60,
            }
            lines = _format_status_from_json(data, cfg_path, totals_path)
            joined = "\n".join(lines)
            self.assertIn("health:", joined)
            self.assertIn("cooldown 0/1", joined)
            self.assertIn("overrides 1", joined)

    def test_degraded_listed_sorted_by_fail_pct(self):
        """degraded supply 按 fail% 降序列出，格式 fail X% (f/r)。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td, {
                "default_cooldown_seconds": 60,
                "supplies": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}],
                "routes": [{"id": "r1", "tiers": {"opus": ["s1", "s2", "s3"], "sonnet": [], "haiku": []}}],
                "strategies": [],
            })
            combos = {
                "supply=s1|route=r1|strategy=cc": {"requests": 10, "ok": 2, "fail": 8},   # 80%
                "supply=s2|route=r1|strategy=cc": {"requests": 10, "ok": 5, "fail": 5},   # 50%
                "supply=s3|route=r1|strategy=cc": {"requests": 10, "ok": 9, "fail": 1},   # 10% (not degraded)
            }
            totals_path = self._make_totals(td, combos)
            data = {
                "supplies": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}],
                "routes": [], "strategies": [],
                "cooldown": {}, "default_cooldown_seconds": 60,
            }
            lines = _format_status_from_json(data, cfg_path, totals_path)
            joined = "\n".join(lines)
            self.assertIn("degraded supplies", joined)
            self.assertIn("s1", joined)
            self.assertIn("fail 80.0% (8/10)", joined)
            self.assertIn("s2", joined)
            self.assertIn("fail 50.0% (5/10)", joined)
            self.assertNotIn("s3", joined.split("config:")[0])  # s3 not in degraded section
            # s1 在 s2 之前（fail% 降序）
            self.assertLess(joined.index("s1"), joined.index("s2"))

    def test_threshold_boundary_fail_pct_eq_30_not_degraded(self):
        """fail% == 30.0 时不报 degraded（阈值是 >30，严格大于）。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td, {
                "default_cooldown_seconds": 60,
                "supplies": [{"id": "s1"}],
                "routes": [{"id": "r1", "tiers": {"opus": ["s1"], "sonnet": [], "haiku": []}}],
                "strategies": [],
            })
            # 10 req, 3 fail = 30.0%（不满足 >30）
            combos = {"supply=s1|route=r1|strategy=cc": {"requests": 10, "ok": 7, "fail": 3}}
            totals_path = self._make_totals(td, combos)
            data = {
                "supplies": [{"id": "s1"}], "routes": [], "strategies": [],
                "cooldown": {}, "default_cooldown_seconds": 60,
            }
            lines = _format_status_from_json(data, cfg_path, totals_path)
            joined = "\n".join(lines)
            self.assertNotIn("degraded supplies", joined)

    def test_threshold_boundary_requests_eq_5_is_degraded(self):
        """requests == 5 且 fail% > 30 时报 degraded（阈值 n>=5）。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td, {
                "default_cooldown_seconds": 60,
                "supplies": [{"id": "s1"}],
                "routes": [{"id": "r1", "tiers": {"opus": ["s1"], "sonnet": [], "haiku": []}}],
                "strategies": [],
            })
            # 5 req, 2 fail = 40%
            combos = {"supply=s1|route=r1|strategy=cc": {"requests": 5, "ok": 3, "fail": 2}}
            totals_path = self._make_totals(td, combos)
            data = {
                "supplies": [{"id": "s1"}], "routes": [], "strategies": [],
                "cooldown": {}, "default_cooldown_seconds": 60,
            }
            lines = _format_status_from_json(data, cfg_path, totals_path)
            joined = "\n".join(lines)
            self.assertIn("degraded supplies", joined)
            self.assertIn("fail 40.0% (2/5)", joined)

    def test_cooldown_listed(self):
        """有 cooldown 时列 supply 剩余秒。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td, {
                "default_cooldown_seconds": 60,
                "supplies": [{"id": "s1"}],
                "routes": [{"id": "r1", "tiers": {"opus": ["s1"], "sonnet": [], "haiku": []}}],
                "strategies": [],
            })
            totals_path = self._make_totals(td)
            data = {
                "supplies": [{"id": "s1"}], "routes": [], "strategies": [],
                "cooldown": {"s1": 45}, "default_cooldown_seconds": 60,
            }
            lines = _format_status_from_json(data, cfg_path, totals_path)
            joined = "\n".join(lines)
            self.assertIn("cooldown (剩余秒):", joined)
            self.assertIn("s1", joined)
            self.assertIn("45s", joined)

    def test_config_count_row(self):
        """config 计数行含 supplies/routes/strategies 数。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td)
            totals_path = self._make_totals(td)
            data = {
                "supplies": [{"id": "s1"}, {"id": "s2"}],
                "routes": [{"id": "r1"}],
                "strategies": [{"client_token": "cc", "route_id": "r1"}],
                "cooldown": {}, "default_cooldown_seconds": 60,
            }
            lines = _format_status_from_json(data, cfg_path, totals_path)
            joined = "\n".join(lines)
            self.assertIn("config: 2 supplies / 1 routes / 1 strategies", joined)
            self.assertNotIn("default_cooldown", joined)

    def test_all_zero_no_anomaly_sections(self):
        """全 0（无 cooldown/degraded/overrides/orphan/缺档）时无异常段。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td, {
                "default_cooldown_seconds": 60,
                "supplies": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}],
                "routes": [{"id": "r1", "tiers": {"opus": ["s1"], "sonnet": ["s2"], "haiku": ["s3"]}}],
                "strategies": [{"client_token": "cc", "route_id": "r1"}],
            })
            totals_path = self._make_totals(td)
            data = {
                "supplies": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}],
                "routes": [{"id": "r1"}],
                "strategies": [{"client_token": "cc", "route_id": "r1", "sidecar_overrides_count": 0}],
                "cooldown": {}, "default_cooldown_seconds": 60,
            }
            lines = _format_status_from_json(data, cfg_path, totals_path)
            joined = "\n".join(lines)
            # health 行有
            self.assertIn("health:", joined)
            self.assertIn("cooldown 0/3", joined)
            self.assertIn("degraded 0", joined)
            self.assertIn("overrides 0", joined)
            # 无异常段标题
            self.assertNotIn("degraded supplies", joined)
            self.assertNotIn("unmatched:", joined)

    def test_degraded_rows_annotated_with_refs(self):
        """degraded supply 行尾标出被哪个 route.tier(strategy) 引用。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td, {
                "default_cooldown_seconds": 60,
                "supplies": [{"id": "s1"}, {"id": "s2"}],
                "routes": [{"id": "r1", "tiers": {"opus": ["s1", "s2"], "sonnet": [], "haiku": []}}],
                "strategies": [{"client_token": "cc", "route_id": "r1"}],
            })
            combos = {"supply=s1|route=r1|strategy=cc": {"requests": 10, "ok": 2, "fail": 8}}
            totals_path = self._make_totals(td, combos)
            data = {
                "supplies": [{"id": "s1"}, {"id": "s2"}],
                "routes": [{"id": "r1"}],
                "strategies": [{"client_token": "cc", "route_id": "r1"}],
                "cooldown": {}, "default_cooldown_seconds": 60,
            }
            lines = _format_status_from_json(data, cfg_path, totals_path)
            joined = "\n".join(lines)
            self.assertIn("s1", joined)
            self.assertIn("fail 80.0% (8/10)  ← r1.opus(cc)", joined)


# ---------------------------------------------------------------------------
# _format_status_offline 降级
# ---------------------------------------------------------------------------

class TestStatusOffline(unittest.TestCase):

    def _make_config(self, td, cfg_dict=None):
        path = os.path.join(td, "config.json")
        cfg = cfg_dict or {
            "default_cooldown_seconds": 60,
            "supplies": [{"id": "s1", "protocol": "anthropic", "appkey": "1234", "target_model": "m1"}],
            "routes": [{"id": "r1", "tiers": {"opus": ["s1"], "sonnet": [], "haiku": []}, "failover": "on"}],
            "strategies": [{"client_token": "cc", "route_id": "r1", "note": "n"}],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return path

    def _make_totals(self, td, combos=None):
        path = os.path.join(td, "totals.json")
        from datetime import datetime, timezone, timedelta
        cst = timezone(timedelta(hours=8))
        today = datetime.now(cst).strftime("%Y-%m-%d")
        data = {"version": 3, "days": {today: {"combos": combos or {}}}}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def test_offline_shows_not_running_for_cooldown_degraded(self):
        """停机时 health 行显 (代理未运行)，不读账本。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td)
            totals_path = self._make_totals(td, {
                "supply=s1|route=r1|strategy=cc": {"requests": 10, "ok": 2, "fail": 8}
            })
            lines = _format_status_offline(cfg_path, totals_path)
            joined = "\n".join(lines)
            self.assertIn("health:", joined)
            self.assertIn("cooldown (代理未运行)", joined)
            self.assertIn("degraded (代理未运行)", joined)
            # 不应列出 degraded supply 明细（不读账本）
            self.assertNotIn("degraded supplies", joined)
            self.assertNotIn("fail 80.0%", joined)

    def test_offline_config_count_row(self):
        """停机时 config 计数行照常。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td)
            totals_path = self._make_totals(td)
            lines = _format_status_offline(cfg_path, totals_path)
            joined = "\n".join(lines)
            self.assertIn("config: 1 supplies / 1 routes / 1 strategies", joined)
            self.assertNotIn("default_cooldown", joined)

    def test_offline_sidecar_overrides_counted(self):
        """停机时 sidecar overrides 静态可读并求和。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td, {
                "default_cooldown_seconds": 60,
                "supplies": [],
                "routes": [],
                "strategies": [{"client_token": "cc", "route_id": "r1"}],
            })
            totals_path = self._make_totals(td)
            sidecar_path = os.path.join(td, "session_overrides.json")
            with open(sidecar_path, "w", encoding="utf-8") as f:
                json.dump({"cc": {"session-abc": {"route_id": "nation1", "last_seen": 0, "created": 0}}}, f)
            lines = _format_status_offline(cfg_path, totals_path)
            joined = "\n".join(lines)
            self.assertIn("overrides 1", joined)


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
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(cfg, f)
            cfg_path = f.name
        try:
            buf = io.StringIO()
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

    def test_format_supplies_menu_preset(self):
        """MENU preset 直接调用不崩。"""
        supplies = [{"id": "s1", "protocol": "anthropic", "appkey": "1234", "target_model": "m1"}]
        lines = format_supplies(supplies, preset="MENU")
        self.assertEqual(len(lines), 1)
        self.assertIn("s1", lines[0])

    def test_format_strategies_menu_style(self):
        """menu style 直接调用不崩。"""
        strategies = [{"client_token": "cc", "route_id": "r1", "note": "test"}]
        lines = format_strategies(strategies, style="menu")
        self.assertEqual(len(lines), 1)
        self.assertIn("cc", lines[0])


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
# parse_access_line
# ---------------------------------------------------------------------------

class TestParseAccessLine(unittest.TestCase):

    def _make_line(self, *, ts=None, req_id="abc12345", status="200",
                   session="2896beec-d221-4013-a073-1ae74010a865",
                   route="nation1", tier="opus", supply="kimi-k3-sankuai-3339",
                   failover="0", route_failover="0", builtin="",
                   final_error="", source="anthropic"):
        if ts is None:
            ts = datetime.now()
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S,") + ts.strftime("%f")[:3]
        return (f"{ts_str} req_id={req_id} ACCESS ms=1000 status={status}"
                f" source={source} route={route} tier={tier} supply={supply}"
                f" failover={failover} attempts=1 usage_in=10 usage_out=5"
                f" token=cc session={session}"
                f" route_failover={route_failover} builtin={builtin}"
                f" budget_retried= budget_truncated=0 stop_reason=end_turn"
                f" final_error={final_error}")

    def test_normal_line_parses_all_fields(self):
        line = self._make_line(status="200", session="abc12345-6789",
                               route="nation2", tier="sonnet",
                               supply="glm-52-sankuai-3339", failover="1",
                               route_failover="0", final_error="")
        r = parse_access_line(line)
        self.assertIsNotNone(r)
        self.assertEqual(r["status"], "200")
        self.assertEqual(r["session"], "abc12345-6789")
        self.assertEqual(r["route"], "nation2")
        self.assertEqual(r["tier"], "sonnet")
        self.assertEqual(r["supply"], "glm-52-sankuai-3339")
        self.assertEqual(r["failover"], "1")
        self.assertEqual(r["route_failover"], "0")
        self.assertEqual(r["req_id"], "abc12345")
        self.assertEqual(r["builtin"], "")
        self.assertEqual(r["final_error"], "")
        self.assertIsInstance(r["ts"], datetime)

    def test_non_access_line_returns_none(self):
        """WARNING/INFO 行 → None。"""
        line = "2026-08-08 22:55:00,785 WARNING something happened"
        self.assertIsNone(parse_access_line(line))

    def test_bad_timestamp_returns_none(self):
        """坏时间戳 → None。"""
        line = "not-a-date req_id=abc ACCESS ms=1000 status=200"
        self.assertIsNone(parse_access_line(line))

    def test_truncated_tail_line_returns_none(self):
        """缺尾字段的截尾行 → None（找不到 ACCESS 标记）或容忍缺字段。"""
        # 有 ACCESS 标记但缺尾字段 → 仍解析，缺字段返回空串
        line = "2026-08-08 22:55:00,785 req_id=abc ACCESS ms=1000 status=200"
        r = parse_access_line(line)
        self.assertIsNotNone(r)
        self.assertEqual(r["status"], "200")
        self.assertEqual(r["session"], "")  # 缺失字段
        self.assertEqual(r["route"], "")

    def test_route_failover_not_mistaken_for_failover(self):
        """route_failover= 含子串 failover=——精确 key 匹配不误命中。"""
        line = self._make_line(failover="0", route_failover="1")
        r = parse_access_line(line)
        self.assertIsNotNone(r)
        self.assertEqual(r["failover"], "0")
        self.assertEqual(r["route_failover"], "1")

    def test_builtin_route_field(self):
        """builtin=route 行正确解析。"""
        line = self._make_line(builtin="route", supply="(builtin)",
                               session="abc-def")
        r = parse_access_line(line)
        self.assertIsNotNone(r)
        self.assertEqual(r["builtin"], "route")

    def test_stacktrace_line_returns_none(self):
        """多行 stacktrace 非 ACCESS 行 → None。"""
        line = '  File "/some/path.py", line 123, in handle'
        self.assertIsNone(parse_access_line(line))

    def test_empty_session(self):
        """空 session= 行正确解析为空串。"""
        line = self._make_line(session="")
        r = parse_access_line(line)
        self.assertIsNotNone(r)
        self.assertEqual(r["session"], "")


# ---------------------------------------------------------------------------
# load_active_sessions
# ---------------------------------------------------------------------------

class TestLoadActiveSessions(unittest.TestCase):

    def _make_access_line(self, *, ts, req_id="abc12345", status="200",
                          session="2896beec-d221-4013-a073-1ae74010a865",
                          route="nation1", tier="opus",
                          supply="kimi-k3-sankuai-3339",
                          failover="0", route_failover="0", builtin="",
                          final_error="", source="anthropic"):
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S,") + ts.strftime("%f")[:3]
        return (f"{ts_str} req_id={req_id} ACCESS ms=1000 status={status}"
                f" source={source} route={route} tier={tier} supply={supply}"
                f" failover={failover} attempts=1 usage_in=10 usage_out=5"
                f" token=cc session={session}"
                f" route_failover={route_failover} builtin={builtin}"
                f" budget_retried= budget_truncated=0 stop_reason=end_turn"
                f" final_error={final_error}")

    def _write_log(self, td, lines_text):
        path = os.path.join(td, "test.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines_text))
        return path

    def test_window_filter_excludes_old_lines(self):
        """31min 前的行被排除，29min 内的行保留。"""
        now = datetime.now()
        old_ts = now - timedelta(minutes=31)
        new_ts = now - timedelta(minutes=5)
        with tempfile.TemporaryDirectory() as td:
            path = self._write_log(td, [
                self._make_access_line(ts=old_ts, req_id="old001"),
                self._make_access_line(ts=new_ts, req_id="new001"),
            ])
            result = load_active_sessions(path, now=now)
        self.assertFalse(result["log_missing"])
        sessions = result["sessions"]
        self.assertEqual(len(sessions), 1)
        self.assertIn("2896beec-d221-4013-a073-1ae74010a865", sessions)
        self.assertEqual(sessions["2896beec-d221-4013-a073-1ae74010a865"]["n"], 1)

    def test_grouping_aggregates_n_fail_fo(self):
        """多行同 session 聚合 n/fail/fo 正确。"""
        now = datetime.now()
        sid = "test-sess-001"
        with tempfile.TemporaryDirectory() as td:
            path = self._write_log(td, [
                self._make_access_line(ts=now - timedelta(minutes=10),
                                       req_id="r1", status="200",
                                       session=sid, failover="0"),
                self._make_access_line(ts=now - timedelta(minutes=8),
                                       req_id="r2", status="501",
                                       session=sid, failover="1"),
                self._make_access_line(ts=now - timedelta(minutes=5),
                                       req_id="r3", status="200",
                                       session=sid, failover="0",
                                       route_failover="1"),
            ])
            result = load_active_sessions(path, now=now)
        agg = result["sessions"][sid]
        self.assertEqual(agg["n"], 3)
        self.assertEqual(agg["fail"], 1)  # 只有一行 501
        self.assertEqual(agg["fo"], 2)   # failover=1 + route_failover=1
        self.assertEqual(agg["last_status"], "200")  # 最后一行 200
        self.assertEqual(agg["last_req_id"], "r3")

    def test_empty_session_grouped_as_none(self):
        """空串 session 聚成 (none) 桶。"""
        now = datetime.now()
        with tempfile.TemporaryDirectory() as td:
            path = self._write_log(td, [
                self._make_access_line(ts=now - timedelta(minutes=5),
                                       session="", status="501",
                                       req_id="none001"),
            ])
            result = load_active_sessions(path, now=now)
        self.assertIn("(none)", result["sessions"])
        agg = result["sessions"]["(none)"]
        self.assertEqual(agg["n"], 1)
        self.assertEqual(agg["fail"], 1)

    def test_builtin_route_counts_active_not_stats(self):
        """builtin=route 行计入活跃、不计入 n/fail/fo。"""
        now = datetime.now()
        sid = "builtin-only-sess"
        with tempfile.TemporaryDirectory() as td:
            path = self._write_log(td, [
                self._make_access_line(ts=now - timedelta(minutes=5),
                                       session=sid, builtin="route",
                                       supply="(builtin)", req_id="b1"),
            ])
            result = load_active_sessions(path, now=now)
        self.assertIn(sid, result["sessions"])
        agg = result["sessions"][sid]
        self.assertEqual(agg["n"], 0)
        self.assertEqual(agg["fail"], 0)
        self.assertEqual(agg["fo"], 0)
        self.assertTrue(agg["builtin_only"])
        # last_ts 应有值（用于活跃判定）
        self.assertIsNotNone(agg["last_ts"])

    def test_builtin_then_normal_makes_not_builtin_only(self):
        """builtin 行 + 非 builtin 行 → builtin_only=False，统计走非 builtin。"""
        now = datetime.now()
        sid = "mixed-sess"
        with tempfile.TemporaryDirectory() as td:
            path = self._write_log(td, [
                self._make_access_line(ts=now - timedelta(minutes=10),
                                       session=sid, builtin="route",
                                       supply="(builtin)", req_id="b1"),
                self._make_access_line(ts=now - timedelta(minutes=5),
                                       session=sid, status="200",
                                       req_id="n1"),
            ])
            result = load_active_sessions(path, now=now)
        agg = result["sessions"][sid]
        self.assertFalse(agg["builtin_only"])
        self.assertEqual(agg["n"], 1)
        self.assertEqual(agg["last_req_id"], "n1")

    def test_file_missing_returns_log_missing(self):
        """文件缺失 → log_missing=True。"""
        result = load_active_sessions("/nonexistent/path/test.log")
        self.assertTrue(result["log_missing"])
        self.assertEqual(result["sessions"], {})

    def test_truncated_flag_when_buffer_starts_in_window(self):
        """文件 > tail_bytes 且首条可解析行在窗口内 → truncated=True。"""
        now = datetime.now()
        new_ts = now - timedelta(minutes=5)
        # 写多行日志，tail_bytes 设小使 buffer 内首条行仍在窗口内
        lines = [
            self._make_access_line(ts=new_ts, req_id=f"trunc{i:02d}")
            for i in range(5)
        ]
        with tempfile.TemporaryDirectory() as td:
            path = self._write_log(td, lines)
            # 每行约 328 字节，5 行约 1640 字节；tail_bytes 须 > 单行长，
            # 否则 seek 后 readline 吞掉残行、buffer 无完整行可解析。
            # 700 → 保留末尾 2 条完整行（ts 在窗口内）→ truncated=True
            result = load_active_sessions(path, now=now, tail_bytes=700)
        self.assertTrue(result["truncated"])

    def test_not_truncated_when_buffer_starts_before_window(self):
        """文件 > tail_bytes 但首条行在窗口外 → truncated=False。"""
        now = datetime.now()
        old_ts = now - timedelta(minutes=60)
        new_ts = now - timedelta(minutes=5)
        with tempfile.TemporaryDirectory() as td:
            path = self._write_log(td, [
                self._make_access_line(ts=old_ts, req_id="old01"),
                self._make_access_line(ts=new_ts, req_id="new01"),
            ])
            result = load_active_sessions(path, now=now, tail_bytes=50)
        self.assertFalse(result["truncated"])

    def test_non_access_lines_ignored(self):
        """WARNING/stacktrace 行被忽略，不崩。"""
        now = datetime.now()
        ts_str = now.strftime("%Y-%m-%d %H:%M:%S,") + now.strftime("%f")[:3]
        with tempfile.TemporaryDirectory() as td:
            path = self._write_log(td, [
                f"{ts_str} WARNING some warning message",
                "  File '/path.py', line 123, in func",
                self._make_access_line(ts=now - timedelta(minutes=5),
                                       req_id="good01"),
            ])
            result = load_active_sessions(path, now=now)
        self.assertEqual(len(result["sessions"]), 1)
        agg = list(result["sessions"].values())[0]
        self.assertEqual(agg["n"], 1)


# ---------------------------------------------------------------------------
# _format_active_sessions（状态判定 + 排序 + 渲染 + 80 列）
# ---------------------------------------------------------------------------

class TestFormatActiveSessions(unittest.TestCase):

    def _make_result(self, sessions_dict, *, truncated=False, log_missing=False):
        return {"sessions": sessions_dict, "truncated": truncated,
                "log_missing": log_missing}

    def _make_agg(self, *, n=1, fail=0, fo=0, last_status="200",
                  last_ts=None, last_route="nation1", last_tier="opus",
                  last_supply="kimi-k3-sankuai-3339",
                  last_error="", last_req_id="abc12345",
                  builtin_only=False):
        if last_ts is None:
            last_ts = datetime.now() - timedelta(minutes=5)
        return {
            "n": n, "fail": fail, "fo": fo,
            "last_ts": last_ts, "last_status": last_status,
            "last_route": last_route, "last_tier": last_tier,
            "last_supply": last_supply, "last_error": last_error,
            "last_req_id": last_req_id, "builtin_only": builtin_only,
        }

    def test_fail_state_when_last_non_200(self):
        """末次非 200 → FAIL。"""
        sessions = {"abc12345-xxxx": self._make_agg(
            n=2, fail=2, last_status="501",
            last_error="unsupported_source=chat_target=anthropic",
            last_req_id="deadbeef")}
        lines = _format_active_sessions(self._make_result(sessions))
        joined = "\n".join(lines)
        self.assertIn("FAIL", joined)
        self.assertIn("deadbeef", joined)

    def test_warn_state_when_fail_but_last_200(self):
        """末次 200 但窗口内有 fail → warn。"""
        sessions = {"abc12345-xxxx": self._make_agg(
            n=3, fail=1, last_status="200")}
        lines = _format_active_sessions(self._make_result(sessions))
        joined = "\n".join(lines)
        self.assertIn("warn", joined)

    def test_warn_state_when_fo_gt_zero(self):
        """末次 200 全无 fail 但 fo>0 → warn。"""
        sessions = {"abc12345-xxxx": self._make_agg(
            n=2, fail=0, fo=1, last_status="200")}
        lines = _format_active_sessions(self._make_result(sessions))
        joined = "\n".join(lines)
        self.assertIn("warn", joined)

    def test_ok_state_all_200(self):
        """全 200 无 failover → ok。"""
        sessions = {"abc12345-xxxx": self._make_agg(
            n=5, fail=0, fo=0, last_status="200")}
        lines = _format_active_sessions(self._make_result(sessions))
        joined = "\n".join(lines)
        self.assertIn("ok", joined)
        self.assertNotIn("warn", joined)
        self.assertNotIn("FAIL", joined)

    def test_builtin_only_state_ok_with_note(self):
        """仅 builtin → ok + 行尾注（仅 $route)。"""
        sessions = {"abc12345-xxxx": self._make_agg(
            builtin_only=True, last_status="200",
            last_supply="(builtin)")}
        lines = _format_active_sessions(self._make_result(sessions))
        joined = "\n".join(lines)
        self.assertIn("ok", joined)
        self.assertIn("n=0", joined)
        self.assertIn("（仅 $route)", joined)

    def test_fail_sorted_before_ok(self):
        """FAIL 排在 ok 之前。"""
        sessions = {
            "aaa11111-xxxx": self._make_agg(n=5, fail=0, last_status="200"),
            "bbb22222-xxxx": self._make_agg(
                n=2, fail=2, last_status="501",
                last_error="err", last_req_id="r1"),
        }
        lines = _format_active_sessions(self._make_result(sessions))
        joined = "\n".join(lines)
        # bbb22222 (FAIL) 在 aaa11111 (ok) 之前
        self.assertLess(joined.index("bbb22222"), joined.index("aaa11111"))

    def test_warn_sorted_between_fail_and_ok(self):
        """warn 排在 FAIL 和 ok 之间。"""
        sessions = {
            "aaa11111-xxxx": self._make_agg(n=5, fail=0, last_status="200"),
            "bbb22222-xxxx": self._make_agg(
                n=2, fail=2, last_status="501",
                last_error="err", last_req_id="r1"),
            "ccc33333-xxxx": self._make_agg(n=3, fail=1, last_status="200"),
        }
        lines = _format_active_sessions(self._make_result(sessions))
        joined = "\n".join(lines)
        self.assertLess(joined.index("bbb22222"), joined.index("ccc33333"))
        self.assertLess(joined.index("ccc33333"), joined.index("aaa11111"))

    def test_same_state_sorted_by_ts_desc(self):
        """同档按最近请求时间倒序（新的在前）。"""
        now = datetime.now()
        sessions = {
            "aaa11111-xxxx": self._make_agg(
                n=5, last_ts=now - timedelta(minutes=10)),
            "bbb22222-xxxx": self._make_agg(
                n=3, last_ts=now - timedelta(minutes=2)),
        }
        lines = _format_active_sessions(self._make_result(sessions))
        joined = "\n".join(lines)
        # bbb22222 (2min ago) 在 aaa11111 (10min ago) 之前
        self.assertLess(joined.index("bbb22222"), joined.index("aaa11111"))

    def test_err_continuation_line_has_req_id(self):
        """FAIL 行的 err 续行含 req=短id。"""
        sessions = {"abc12345-xxxx": self._make_agg(
            n=2, fail=2, last_status="501",
            last_error="unsupported_source=chat_target=anthropic",
            last_req_id="deadbeef")}
        lines = _format_active_sessions(self._make_result(sessions))
        joined = "\n".join(lines)
        self.assertIn("err:", joined)
        self.assertIn("req=deadbeef", joined)

    def test_none_bucket_shown_as_none(self):
        """(none) 桶显示为 (none)。"""
        sessions = {"(none)": self._make_agg(
            n=2, fail=2, last_status="501",
            last_error="err", last_req_id="r1")}
        lines = _format_active_sessions(self._make_result(sessions))
        joined = "\n".join(lines)
        self.assertIn("(none)", joined)

    def test_zero_active_shows_no_activity(self):
        """零活跃 → 单行降级文案。"""
        lines = _format_active_sessions(self._make_result({}))
        self.assertEqual(len(lines), 1)
        self.assertIn("无活跃请求", lines[0])

    def test_log_missing_shows_missing(self):
        """日志缺失 → 单行降级文案。"""
        lines = _format_active_sessions(self._make_result(
            {}, log_missing=True))
        self.assertEqual(len(lines), 1)
        self.assertIn("日志文件缺失", lines[0])

    def test_truncated_header_has_hint(self):
        """截断时 header 追加提示。"""
        sessions = {"abc12345-xxxx": self._make_agg(n=1)}
        lines = _format_active_sessions(self._make_result(
            sessions, truncated=True))
        self.assertIn("窗口数据可能被截断", lines[0])

    def test_header_counts(self):
        """header 含总数和分档计数。"""
        sessions = {
            "aaa11111-xxxx": self._make_agg(n=5, fail=0, last_status="200"),
            "bbb22222-xxxx": self._make_agg(
                n=2, fail=2, last_status="501",
                last_error="err", last_req_id="r1"),
            "ccc33333-xxxx": self._make_agg(n=3, fail=1, last_status="200"),
        }
        lines = _format_active_sessions(self._make_result(sessions))
        header = lines[0]
        self.assertIn("3", header)
        self.assertIn("1 ok", header)
        self.assertIn("1 warn", header)
        self.assertIn("1 FAIL", header)

    def test_all_lines_le_80_width(self):
        """所有 session 行 display_width ≤ 80。"""
        now = datetime.now()
        sessions = {
            "2896beec-d221-4013-a073-1ae74010a865": self._make_agg(
                n=87, fail=0, fo=0, last_status="200",
                last_ts=now - timedelta(minutes=2),
                last_route="nation1", last_tier="sonnet",
                last_supply="glm-52-sankuai-3339"),
            "1a09afa5-7300-4f41-b661-b6b7e7058026": self._make_agg(
                n=15, fail=2, fo=1, last_status="200",
                last_ts=now - timedelta(minutes=5),
                last_route="nation2", last_tier="sonnet",
                last_supply="glm-52-sankuai-3339"),
            "(none)": self._make_agg(
                n=2, fail=2, fo=0, last_status="501",
                last_ts=now - timedelta(minutes=3),
                last_error="unsupported_source=chat_target=anthropic",
                last_req_id="a9bc1b0e",
                last_route="nation1", last_tier="opus",
                last_supply="kimi-k3-sankuai-3339"),
        }
        lines = _format_active_sessions(self._make_result(sessions))
        for line in lines:
            self.assertLessEqual(
                display_width(line), 80,
                f"line exceeds 80: {line!r} (width={display_width(line)})")

    def test_fo_shown_only_when_gt_zero(self):
        """fo>0 时显示 fo=，fo=0 时不显示。"""
        sessions_with_fo = {
            "aaa11111-xxxx": self._make_agg(n=3, fail=1, fo=2,
                                            last_status="200"),
        }
        lines = _format_active_sessions(self._make_result(sessions_with_fo))
        joined = "\n".join(lines)
        self.assertIn("fo=2", joined)

        sessions_no_fo = {
            "bbb22222-xxxx": self._make_agg(n=3, fail=0, fo=0,
                                            last_status="200"),
        }
        lines = _format_active_sessions(self._make_result(sessions_no_fo))
        joined = "\n".join(lines)
        self.assertNotIn("fo=", joined)

    def test_max_20_rows_cap(self):
        """超过 20 个 session 时显示上限 + 另有 N 个。"""
        sessions = {}
        for i in range(25):
            sid = f"sess{i:04d}-xxxx-yyyy-zzzz"
            sessions[sid] = self._make_agg(n=1)
        lines = _format_active_sessions(self._make_result(sessions))
        joined = "\n".join(lines)
        self.assertIn("另有 5 个 session", joined)


# ---------------------------------------------------------------------------
# _format_status_from_json 端到端（session 段位置）
# ---------------------------------------------------------------------------

class TestStatusFormatWithSessions(unittest.TestCase):

    def _make_config(self, td, cfg_dict=None):
        path = os.path.join(td, "config.json")
        cfg = cfg_dict or {
            "default_cooldown_seconds": 60,
            "supplies": [{"id": "s1", "protocol": "anthropic",
                          "appkey": "1234", "target_model": "m1"}],
            "routes": [{"id": "r1", "tiers": {"opus": ["s1"],
                        "sonnet": [], "haiku": []}, "failover": "on"}],
            "strategies": [{"client_token": "cc", "route_id": "r1",
                            "note": "n"}],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return path

    def _make_totals(self, td, combos=None):
        path = os.path.join(td, "totals.json")
        cst = timezone(timedelta(hours=8))
        today = datetime.now(cst).strftime("%Y-%m-%d")
        data = {"version": 3, "days": {today: {"combos": combos or {}}}}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def _make_log(self, td, lines_text):
        path = os.path.join(td, "test.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines_text))
        return path

    def _make_access_line(self, *, ts, req_id="abc12345", status="200",
                          session="2896beec-d221-4013-a073-1ae74010a865",
                          route="nation1", tier="opus",
                          supply="kimi-k3-sankuai-3339",
                          failover="0", route_failover="0", builtin="",
                          final_error="", source="anthropic"):
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S,") + ts.strftime("%f")[:3]
        return (f"{ts_str} req_id={req_id} ACCESS ms=1000 status={status}"
                f" source={source} route={route} tier={tier} supply={supply}"
                f" failover={failover} attempts=1 usage_in=10 usage_out=5"
                f" token=cc session={session}"
                f" route_failover={route_failover} builtin={builtin}"
                f" budget_retried= budget_truncated=0 stop_reason=end_turn"
                f" final_error={final_error}")

    def test_session_section_after_health_before_degraded(self):
        """session 段位于 health 行之后、异常段之前。"""
        now = datetime.now()
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td)
            totals_path = self._make_totals(td)
            log_path = self._make_log(td, [
                self._make_access_line(
                    ts=now - timedelta(minutes=5),
                    session="abc12345-6789-abcd",
                    status="200", req_id="r1"),
            ])
            data = {
                "supplies": [{"id": "s1"}],
                "routes": [{"id": "r1", "tiers": {"opus": ["s1"],
                            "sonnet": [], "haiku": []}}],
                "strategies": [{"client_token": "cc", "route_id": "r1",
                                "sidecar_overrides_count": 0}],
                "cooldown": {}, "default_cooldown_seconds": 60,
            }
            lines = _format_status_from_json(
                data, cfg_path, totals_path, log_path)
        joined = "\n".join(lines)
        # health 行在 session 段之前
        self.assertLess(joined.index("health:"), joined.index("active sessions"))
        # session 段在 config 计数行之前
        self.assertLess(joined.index("active sessions"), joined.index("config:"))

    def test_no_log_path_no_session_section(self):
        """不传 log_path 时不展示 session 段（兼容已有调用）。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td)
            totals_path = self._make_totals(td)
            data = {
                "supplies": [{"id": "s1"}], "routes": [], "strategies": [],
                "cooldown": {}, "default_cooldown_seconds": 60,
            }
            lines = _format_status_from_json(data, cfg_path, totals_path)
        joined = "\n".join(lines)
        self.assertNotIn("active sessions", joined)

    def test_log_missing_shows_missing_line(self):
        """log_path 指向不存在文件 → 展示"日志文件缺失"。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td)
            totals_path = self._make_totals(td)
            data = {
                "supplies": [{"id": "s1"}], "routes": [], "strategies": [],
                "cooldown": {}, "default_cooldown_seconds": 60,
            }
            lines = _format_status_from_json(
                data, cfg_path, totals_path, "/nonexistent/log.log")
        joined = "\n".join(lines)
        self.assertIn("日志文件缺失", joined)

    def test_zero_active_shows_no_activity(self):
        """有日志但无活跃 session → 展示"无活跃请求"。"""
        now = datetime.now()
        old_ts = now - timedelta(minutes=60)
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td)
            totals_path = self._make_totals(td)
            log_path = self._make_log(td, [
                self._make_access_line(ts=old_ts, req_id="old01"),
            ])
            data = {
                "supplies": [{"id": "s1"}], "routes": [], "strategies": [],
                "cooldown": {}, "default_cooldown_seconds": 60,
            }
            lines = _format_status_from_json(
                data, cfg_path, totals_path, log_path)
        joined = "\n".join(lines)
        self.assertIn("无活跃请求", joined)


if __name__ == "__main__":
    unittest.main()
