"""status 供给段：窗口统计与状态判定单测（纯函数，不读真实日志）。

覆盖（2026-11-22 设计）：
- 窗口过滤：30min 内/外、边界
- 样本口径：排除 builtin=route、provider 为空、client_disconnect
- 成功率：n>=5 才判，未达标 ok_pct 仍算出但渲染侧显示 "-"
- ttft：取中位数（p50），非流式行不计入
- tps：先聚合 Σout / Σ(ms-ttft) 再相除，不是逐请求均值
- 失败码：final_error 优先，回退 http_<status> / invalid_stream
- 状态优先级：cool > fail > flaky > ok > low > idle
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.cli import _format_ops as f  # noqa: E402


def row(*, supply="s1", minutes_ago=0, status="200", integrity="valid",
        provider="legacy_http", builtin="", first_event_ms="", ms="",
        usage_out="", final_error="", now=None):
    now = now or datetime.now()
    return {
        "ts": now - timedelta(minutes=minutes_ago),
        "req_id": "r", "status": status, "source": "anthropic",
        "operation": "messages", "target_protocol": "anthropic",
        "conversion_kind": "passthrough", "usage_in": "0", "usage_out": usage_out,
        "token": "cc", "route": "r1", "tier": "opus", "supply": supply,
        "provider": provider, "failover": "0", "session": "s",
        "route_failover": "0", "builtin": builtin, "final_error": final_error,
        "response_committed": "1", "stream_integrity": integrity,
        "terminal_status": "completed", "terminal_reason": "end_turn",
        "first_event_ms": first_event_ms, "ms": ms, "attempts": "1",
    }


class TestSampleFilter(unittest.TestCase):
    def test_excludes_builtin_route(self):
        stats = f.load_supply_stats([row(builtin="route", supply="s1")])
        self.assertEqual(stats, {})

    def test_excludes_missing_provider(self):
        stats = f.load_supply_stats([row(provider="")])
        self.assertEqual(stats, {})

    def test_excludes_client_disconnect(self):
        stats = f.load_supply_stats([row(integrity="client_disconnect")])
        self.assertEqual(stats, {})

    def test_excludes_outside_window(self):
        stats = f.load_supply_stats([row(minutes_ago=31)])
        self.assertEqual(stats, {})

    def test_includes_inside_window(self):
        stats = f.load_supply_stats([row(minutes_ago=29)])
        self.assertEqual(stats["s1"]["n"], 1)


class TestOkFail(unittest.TestCase):
    def test_200_valid_is_ok(self):
        stats = f.load_supply_stats([row()])
        self.assertEqual((stats["s1"]["n"], stats["s1"]["fail"]), (1, 0))

    def test_200_invalid_stream_is_fail(self):
        """200 + stream_integrity=invalid 与账本同口径，判失败。"""
        stats = f.load_supply_stats([row(integrity="invalid")])
        self.assertEqual(stats["s1"]["fail"], 1)
        self.assertEqual(stats["s1"]["codes"], {"invalid_stream": 1})

    def test_non_200_is_fail_with_http_code(self):
        stats = f.load_supply_stats([row(status="500")])
        self.assertEqual(stats["s1"]["codes"], {"http_500": 1})

    def test_final_error_wins_over_status(self):
        stats = f.load_supply_stats([row(status="400", final_error="upstream_model_unavailable")])
        self.assertEqual(stats["s1"]["codes"], {"upstream_model_unavailable": 1})


class TestTtft(unittest.TestCase):
    def test_median_not_mean(self):
        """中位数：长思考尾巴不把中位数拖走。"""
        rows = [row(first_event_ms="1000", ms="5000", usage_out="100"),
                row(first_event_ms="9000", ms="20000", usage_out="100"),
                row(first_event_ms="1100", ms="5000", usage_out="100")]
        self.assertEqual(f.load_supply_stats(rows)["s1"]["ttft_ms"], 1100)

    def test_non_stream_excluded(self):
        """非流式行没有 first_event_ms，不计入 ttft 也不影响 tps 分母。"""
        rows = [row(first_event_ms=""), row(first_event_ms="2000", ms="4000", usage_out="50")]
        stats = f.load_supply_stats(rows)["s1"]
        self.assertEqual(stats["ttft_ms"], 2000)
        self.assertEqual(stats["tps"], 25.0)

    def test_none_when_no_stream_sample(self):
        self.assertIsNone(f.load_supply_stats([row(first_event_ms="")])["s1"]["ttft_ms"])


class TestTps(unittest.TestCase):
    def test_aggregate_divide_not_per_request_mean(self):
        """先聚合再相除：短回答不会把 tps 抬到天上。"""
        rows = [row(first_event_ms="1000", ms="11000", usage_out="1000"),   # 100 tok/s
                row(first_event_ms="1000", ms="2000", usage_out="10")]      # 10 tok/s
        # Σout=1010, Σgen=10+1=11s → 91.8；逐请求均值会是 (100+10)/2=55 或更歪
        self.assertAlmostEqual(f.load_supply_stats(rows)["s1"]["tps"], 1010 / 11, places=6)

    def test_zero_usage_out_excluded(self):
        stats = f.load_supply_stats([row(first_event_ms="1000", ms="5000", usage_out="0")])
        self.assertIsNone(stats["s1"]["tps"])

    def test_zero_generation_time_excluded(self):
        """ms == first_event_ms（无生成阶段）不参与 tps，避免除零/无穷大。"""
        stats = f.load_supply_stats([row(first_event_ms="1000", ms="1000", usage_out="50")])
        self.assertIsNone(stats["s1"]["tps"])


class TestStateResolution(unittest.TestCase):
    def stat(self, n, fail):
        return {"n": n, "fail": fail, "ok_pct": 100.0 * (n - fail) / n,
                "ttft_ms": None, "tps": None, "codes": {}}

    def test_cool_wins(self):
        state = f.resolve_supply_state("s1", self.stat(10, 0), cooling={"s1": {"remain": 5}},
                                       stream_health=None, window_total=10)
        self.assertEqual(state, "cool")

    def test_fail_below_seventy_percent_ok(self):
        self.assertEqual(f.resolve_supply_state("s1", self.stat(10, 4), cooling={},
                                                stream_health=None, window_total=10), "fail")

    def test_exactly_seventy_percent_ok_is_ok(self):
        self.assertEqual(f.resolve_supply_state("s1", self.stat(10, 3), cooling={},
                                                stream_health=None, window_total=10), "ok")

    def test_flaky_after_consecutive_invalid(self):
        health = {"supplies": {"s1": {"consecutive": 2}}}
        self.assertEqual(f.resolve_supply_state("s1", self.stat(10, 0), cooling={},
                                                stream_health=health, window_total=10), "flaky")

    def test_low_samples_do_not_judge(self):
        self.assertEqual(f.resolve_supply_state("s1", self.stat(4, 4), cooling={},
                                                stream_health=None, window_total=4), "low")

    def test_idle_without_samples(self):
        self.assertEqual(f.resolve_supply_state("s1", None, cooling={},
                                                stream_health=None, window_total=0), "idle")


class TestRendering(unittest.TestCase):
    def test_header_merges_summary_and_legend(self):
        """标题行 = supply health: <summary> · last 30min · 图例。"""
        lines = f._format_supplies(self.CFG, {}, cooldown={}, stream_health=None,
                                   truncated=False, log_missing=True, refs={"s1": ["x"]},
                                   summary=["cooldown 0/2", "degraded 0", "overrides 1"])
        self.assertTrue(lines[0].startswith("supply health: cooldown 0/2 · degraded 0"), lines[0])
        self.assertIn("overrides 1", lines[0])
        self.assertIn("last 30min", lines[0])
        self.assertIn("ttft=p50", lines[0])
        self.assertIn("tps=输出tok/s", lines[0])

    def test_idle_rows_are_hidden(self):
        """窗口内零请求的供给不占行。"""
        stats = {"s1": {"n": 6, "fail": 0, "ok_pct": 100.0, "ttft_ms": 900,
                        "tps": 50.0, "codes": {}}}
        lines = f._format_supplies(self.CFG, stats, cooldown={}, stream_health=None,
                                   truncated=False, log_missing=False, refs={"s1": ["x"]})
        joined = "\n".join(lines)
        self.assertIn(" s1 ", joined)
        self.assertNotIn(" s2 ", joined, "idle 供给不显示")
        self.assertNotIn("idle", joined)

    def test_all_idle_shows_placeholder(self):
        """全部 idle 时给一行占位，而不是空表。"""
        cfg = {"supplies": [{"id": "s1"}],
               "routes": [{"id": "r1", "tiers": {"opus": ["s1"]}}]}
        lines = f._format_supplies(cfg, {}, cooldown={}, stream_health=None,
                                   truncated=False, log_missing=False, refs={"s1": ["x"]})
        self.assertIn("均无请求", "\n".join(lines))

    CFG = {"supplies": [{"id": "s1"}, {"id": "s2"}],
           "routes": [{"id": "r1", "tiers": {"opus": ["s1"]}}]}

    def test_low_samples_renders_dash(self):
        stats = {"s1": {"n": 3, "fail": 0, "ok_pct": 100.0, "ttft_ms": 900,
                        "tps": 50.0, "codes": {}}}
        lines = f._format_supplies(self.CFG, stats, cooldown={}, stream_health=None,
                                   truncated=False, log_missing=False, refs={"s1": ["x"]})
        line = next(l for l in lines if " s1 " in l)
        self.assertIn("low", line)
        self.assertIn(" - ", line, "样本不足不给成功率")

    def test_cool_line_shows_remaining_and_reason(self):
        stats = {"s1": {"n": 9, "fail": 0, "ok_pct": 100.0, "ttft_ms": 900,
                        "tps": 50.0, "codes": {}}}
        lines = f._format_supplies(self.CFG, stats,
                                   cooldown={"s1": {"remain": 42.4, "reason": "http_429"}},
                                   stream_health=None, truncated=False,
                                   log_missing=False, refs={"s1": ["x"]})
        line = next(l for l in lines if " s1 " in l)
        self.assertIn("cool 42s", line)
        self.assertIn("http 429", line)

    def test_unreferenced_folded_to_one_line(self):
        lines = f._format_supplies(self.CFG, {}, cooldown={}, stream_health=None,
                                   truncated=False, log_missing=False, refs={"s1": ["x"]})
        unreferenced = [l for l in lines if "unreferenced" in l]
        self.assertEqual(len(unreferenced), 1)
        self.assertIn("s2", unreferenced[0])

    def test_truncated_marker(self):
        lines = f._format_supplies(self.CFG, {}, cooldown={}, stream_health=None,
                                   truncated=True, log_missing=False, refs={})
        self.assertIn("tail", lines[0])


if __name__ == "__main__":
    unittest.main()
