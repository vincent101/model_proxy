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
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _format_ops import (
    DEGRADED_FAIL_PCT,
    DEGRADED_MIN_REQUESTS,
    _format_status_from_json,
    _format_status_offline,
    compute_config_anomalies,
    display_width,
    find_damaged_routes,
    format_routes,
    format_strategies,
    format_supplies,
    load_supply_health,
    mask_appkey,
    normalize_supply,
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

class TestComputeConfigAnomalies(unittest.TestCase):

    def test_orphan_supplies(self):
        """在 supplies 但未被任何 route 引用的 supply → orphan。"""
        cfg = {
            "supplies": [
                {"id": "s1"}, {"id": "s2"}, {"id": "unused-sid"},
            ],
            "routes": [
                {"id": "r1", "tiers": {"opus": ["s1"], "sonnet": ["s2"], "haiku": []}},
            ],
        }
        anomalies = compute_config_anomalies(cfg)
        self.assertEqual(anomalies["orphan_supplies"], ["unused-sid"])

    def test_missing_tiers(self):
        """route 的空档 → missing_tiers。"""
        cfg = {
            "supplies": [{"id": "s1"}],
            "routes": [
                {"id": "eval-x", "tiers": {"opus": [], "sonnet": ["s1"], "haiku": []}},
            ],
        }
        anomalies = compute_config_anomalies(cfg)
        self.assertIn("eval-x 缺 opus/haiku", anomalies["missing_tiers"])

    def test_dangling_refs(self):
        """route tier 引用了不存在的 supply → dangling_refs。"""
        cfg = {
            "supplies": [{"id": "s1"}],
            "routes": [
                {"id": "r1", "tiers": {"opus": ["s1", "ghost"], "sonnet": [], "haiku": []}},
            ],
        }
        anomalies = compute_config_anomalies(cfg)
        self.assertEqual(anomalies["dangling_refs"], ["ghost"])

    def test_no_anomalies(self):
        """正常配置 → 全空。"""
        cfg = {
            "supplies": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}],
            "routes": [
                {"id": "r1", "tiers": {"opus": ["s1"], "sonnet": ["s2"], "haiku": ["s3"]}},
            ],
        }
        anomalies = compute_config_anomalies(cfg)
        self.assertEqual(anomalies["orphan_supplies"], [])
        self.assertEqual(anomalies["missing_tiers"], [])
        self.assertEqual(anomalies["dangling_refs"], [])


# ---------------------------------------------------------------------------
# find_damaged_routes
# ---------------------------------------------------------------------------

class TestFindDamagedRoutes(unittest.TestCase):

    def test_degraded_supply_in_route(self):
        cfg = {"routes": [{"id": "r1", "tiers": {"opus": ["bad-sid"], "sonnet": [], "haiku": []}}]}
        result = find_damaged_routes(cfg, {"bad-sid"}, {})
        self.assertEqual(len(result), 1)
        self.assertIn("r1", result[0])
        self.assertIn("bad-sid degraded", result[0])

    def test_cooling_supply_in_route(self):
        cfg = {"routes": [{"id": "r1", "tiers": {"opus": ["cool-sid"], "sonnet": [], "haiku": []}}]}
        result = find_damaged_routes(cfg, set(), {"cool-sid": 30})
        self.assertEqual(len(result), 1)
        self.assertIn("cool-sid cooling(30s)", result[0])

    def test_no_damage(self):
        cfg = {"routes": [{"id": "r1", "tiers": {"opus": ["good-sid"], "sonnet": [], "haiku": []}}]}
        result = find_damaged_routes(cfg, set(), {})
        self.assertEqual(result, [])


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

    def test_unmatched_none_shown_when_fail(self):
        """(none) supply 有 fail 时单列 unmatched 行。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td)
            combos = {
                "supply=(none)|route=(none)|strategy=(none)": {"requests": 5, "ok": 0, "fail": 5},
            }
            totals_path = self._make_totals(td, combos)
            data = {
                "supplies": [{"id": "s1"}], "routes": [], "strategies": [],
                "cooldown": {}, "default_cooldown_seconds": 60,
            }
            lines = _format_status_from_json(data, cfg_path, totals_path)
            joined = "\n".join(lines)
            self.assertIn("unmatched:", joined)
            self.assertIn("(none)", joined)

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

    def test_config_notices_orphan(self):
        """orphan supply 出现在 config notices 段。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td, {
                "default_cooldown_seconds": 60,
                "supplies": [{"id": "s1"}, {"id": "orphan-sid"}],
                "routes": [{"id": "r1", "tiers": {"opus": ["s1"], "sonnet": [], "haiku": []}}],
                "strategies": [],
            })
            totals_path = self._make_totals(td)
            data = {
                "supplies": [{"id": "s1"}, {"id": "orphan-sid"}],
                "routes": [], "strategies": [],
                "cooldown": {}, "default_cooldown_seconds": 60,
            }
            lines = _format_status_from_json(data, cfg_path, totals_path)
            joined = "\n".join(lines)
            self.assertIn("config notices:", joined)
            self.assertIn("orphan supplies:", joined)
            self.assertIn("orphan-sid", joined)

    def test_config_count_row(self):
        """config 计数行含 supplies/routes/strategies 数 + default_cooldown。"""
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
            self.assertIn("default_cooldown=60s", joined)

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
            self.assertNotIn("damaged routes:", joined)
            self.assertNotIn("config notices:", joined)

    def test_damaged_routes_shown(self):
        """degraded supply 被某 route tier 引用时，damaged routes 段列出。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td, {
                "default_cooldown_seconds": 60,
                "supplies": [{"id": "s1"}, {"id": "s2"}],
                "routes": [{"id": "r1", "tiers": {"opus": ["s1", "s2"], "sonnet": [], "haiku": []}}],
                "strategies": [],
            })
            combos = {"supply=s1|route=r1|strategy=cc": {"requests": 10, "ok": 2, "fail": 8}}
            totals_path = self._make_totals(td, combos)
            data = {
                "supplies": [{"id": "s1"}, {"id": "s2"}],
                "routes": [{"id": "r1"}],
                "strategies": [],
                "cooldown": {}, "default_cooldown_seconds": 60,
            }
            lines = _format_status_from_json(data, cfg_path, totals_path)
            joined = "\n".join(lines)
            self.assertIn("damaged routes:", joined)
            self.assertIn("r1", joined)
            self.assertIn("s1 degraded", joined)


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

    def test_offline_config_notices_shown(self):
        """停机时 config notices（orphan/缺档）静态照常展示。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td, {
                "default_cooldown_seconds": 60,
                "supplies": [{"id": "s1"}, {"id": "orphan-sid"}],
                "routes": [{"id": "r1", "tiers": {"opus": ["s1"], "sonnet": [], "haiku": []}}],
                "strategies": [],
            })
            totals_path = self._make_totals(td)
            lines = _format_status_offline(cfg_path, totals_path)
            joined = "\n".join(lines)
            self.assertIn("config notices:", joined)
            self.assertIn("orphan-sid", joined)

    def test_offline_config_count_row(self):
        """停机时 config 计数行照常。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._make_config(td)
            totals_path = self._make_totals(td)
            lines = _format_status_offline(cfg_path, totals_path)
            joined = "\n".join(lines)
            self.assertIn("config: 1 supplies / 1 routes / 1 strategies", joined)
            self.assertIn("default_cooldown=60s", joined)

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


if __name__ == "__main__":
    unittest.main()
