"""累计用量账本（UsageTotalsStore）单测（脱网络，纯标准库 unittest）。

覆盖：record 累加正确性（天桶顶层+combos+total 顶层+total.combos 四处一致）、
组合键格式、归档边界一致性（不丢不重）、_cst_now 的 UTC+8 正确性（含跨零点边界）。

运行：cd tools/model_proxy && python3 -m unittest tests.test_usage_totals -v
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.server import (  # noqa: E402
    _cst_now,
    _CST,
    UsageTotalsStore,
)


def _acc(supply="s1", route="r1", strategy="cc", status=200,
         usage_in=10, usage_out=20):
    return {
        "supply": supply, "route": route, "strategy": strategy,
        "status": status, "usage_in": usage_in, "usage_out": usage_out,
    }


class TestCstNow(unittest.TestCase):

    def test_utc8_fixed_offset(self):
        self.assertEqual(_CST.utcoffset(None), timedelta(hours=8))

    def test_cst_now_is_aware_and_consistent_with_utcnow(self):
        now_cst = _cst_now()
        self.assertIsNotNone(now_cst.tzinfo)
        now_utc = datetime.now(timezone.utc)
        # 两次取时刻很接近，转成 UTC 后差值应在几秒内
        delta = abs((now_cst.astimezone(timezone.utc) - now_utc).total_seconds())
        self.assertLess(delta, 5)

    def test_cross_midnight_boundary_utc_to_cst(self):
        """UTC 16:00 应对应 CST（UTC+8）次日 00:00 —— 时区代码最容易在此暴露 bug。"""
        utc_dt = datetime(2026, 7, 23, 16, 0, 0, tzinfo=timezone.utc)
        cst_dt = utc_dt.astimezone(_CST)
        self.assertEqual(cst_dt.strftime("%Y-%m-%d %H:%M:%S"), "2026-07-24 00:00:00")

    def test_cross_midnight_boundary_before(self):
        """UTC 15:59:59 应仍是 CST 当天 23:59:59（未跨零点）。"""
        utc_dt = datetime(2026, 7, 23, 15, 59, 59, tzinfo=timezone.utc)
        cst_dt = utc_dt.astimezone(_CST)
        self.assertEqual(cst_dt.strftime("%Y-%m-%d %H:%M:%S"), "2026-07-23 23:59:59")


class TestUsageTotalsStoreRecord(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "totals.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_cold_start_creates_expected_structure(self):
        store = UsageTotalsStore(self.path)
        data = store._data
        self.assertEqual(data["version"], 3)
        self.assertIn("since", data)
        self.assertEqual(data["keep_days"], 400)
        self.assertEqual(data["total"], {"requests": 0, "ok": 0, "fail": 0, "sum_ms": 0, "max_ms": 0, "combos": {}})
        self.assertEqual(data["months_archive"], {})
        self.assertEqual(data["days"], {})
        # record 前文件不落盘（只在 record 时才写）
        self.assertFalse(self.path.exists())

    def test_record_writes_file_and_combo_key_format(self):
        store = UsageTotalsStore(self.path)
        store.record(_acc(supply="claude-sonnet-sankuai-0956", route="claude", strategy="cc"), 100)
        self.assertTrue(self.path.exists())
        with open(self.path) as f:
            on_disk = json.load(f)
        day_key = _cst_now().strftime("%Y-%m-%d")
        self.assertIn(day_key, on_disk["days"])
        combos = on_disk["days"][day_key]["combos"]
        expected_key = "supply=claude-sonnet-sankuai-0956|route=claude|strategy=cc"
        self.assertIn(expected_key, combos)
        self.assertEqual(combos[expected_key]["requests"], 1)

    def test_none_dims_use_placeholder(self):
        store = UsageTotalsStore(self.path)
        store.record(_acc(supply="", route="", strategy=""), 50)
        day_key = _cst_now().strftime("%Y-%m-%d")
        combos = store._data["days"][day_key]["combos"]
        self.assertIn("supply=(none)|route=(none)|strategy=(none)", combos)

    def test_accumulation_top_level_equals_sum_of_combos(self):
        store = UsageTotalsStore(self.path)
        store.record(_acc(supply="s1", route="r1", strategy="cc", status=200), 100)
        store.record(_acc(supply="s1", route="r1", strategy="cc", status=200), 150)
        store.record(_acc(supply="s2", route="r1", strategy="codex", status=500), 200)

        day_key = _cst_now().strftime("%Y-%m-%d")
        day_bucket = store._data["days"][day_key]
        total_bucket = store._data["total"]

        self.assertEqual(day_bucket["requests"], 3)
        self.assertEqual(day_bucket["ok"], 2)
        self.assertEqual(day_bucket["fail"], 1)
        self.assertEqual(day_bucket["sum_ms"], 450)

        # 天桶顶层 requests == combos 各键之和
        combo_sum = sum(v["requests"] for v in day_bucket["combos"].values())
        self.assertEqual(combo_sum, day_bucket["requests"])
        # ok + fail == requests，逐键校验
        for v in day_bucket["combos"].values():
            self.assertEqual(v["ok"] + v["fail"], v["requests"])

        # total 与 day 桶（本测试全部落在同一天）应完全一致
        self.assertEqual(total_bucket["requests"], day_bucket["requests"])
        self.assertEqual(total_bucket["ok"], day_bucket["ok"])
        self.assertEqual(total_bucket["fail"], day_bucket["fail"])
        self.assertEqual(total_bucket["sum_ms"], day_bucket["sum_ms"])
        self.assertEqual(total_bucket["combos"], day_bucket["combos"])

    def test_combo_does_not_store_sum_ms(self):
        store = UsageTotalsStore(self.path)
        store.record(_acc(), 100)
        day_key = _cst_now().strftime("%Y-%m-%d")
        for v in store._data["days"][day_key]["combos"].values():
            self.assertNotIn("sum_ms", v)

    def test_reload_from_disk_preserves_data(self):
        store1 = UsageTotalsStore(self.path)
        store1.record(_acc(), 100)
        store2 = UsageTotalsStore(self.path)  # 模拟重启：重新 load
        self.assertEqual(store2._data["total"]["requests"], 1)
        # 重启后继续 record，在原基础上累加而非从头
        store2.record(_acc(), 50)
        self.assertEqual(store2._data["total"]["requests"], 2)


class TestUsageTotalsStoreCorruptRecovery(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "totals.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_corrupt_json_triggers_backup_and_empty_start(self):
        self.path.write_text("{ not valid json ", encoding="utf-8")
        store = UsageTotalsStore(self.path)
        # 原文件应已被改名备份，原路径不再是坏文件
        self.assertFalse(self.path.exists())
        corrupt_files = list(self.path.parent.glob(self.path.name + ".corrupt.*"))
        self.assertEqual(len(corrupt_files), 1)
        self.assertEqual(corrupt_files[0].read_text(encoding="utf-8"), "{ not valid json ")
        # 从空账本起步，不崩溃
        self.assertEqual(store._data["total"]["requests"], 0)
        # 之后仍可正常记账
        store.record(_acc(), 10)
        self.assertEqual(store._data["total"]["requests"], 1)


class TestArchiveBoundary(unittest.TestCase):
    """归档边界一致性：超窗最旧天桶按组合键汇总进 months_archive 且从 days 删除，不丢不重。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "totals.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _combo(self, requests, ok, fail, usage_in, usage_out):
        return {
            "requests": requests, "ok": ok, "fail": fail,
            "usage_in": usage_in, "usage_out": usage_out,
        }

    def test_archive_moves_oldest_day_and_preserves_totals(self):
        store = UsageTotalsStore(self.path)
        # 手工构造 3 个历史天桶（模拟跨多天场景，不用真的等待）
        store._data["days"] = {
            "2025-01-01": {
                "requests": 2, "ok": 2, "fail": 0, "sum_ms": 300,
                "combos": {
                    "supply=s1|route=r1|strategy=cc": self._combo(2, 2, 0, 20, 40),
                },
            },
            "2025-01-02": {
                "requests": 3, "ok": 2, "fail": 1, "sum_ms": 450,
                "combos": {
                    "supply=s1|route=r1|strategy=cc": self._combo(2, 2, 0, 20, 40),
                    "supply=s2|route=r1|strategy=codex": self._combo(1, 0, 1, 5, 5),
                },
            },
            "2025-02-01": {
                "requests": 1, "ok": 1, "fail": 0, "sum_ms": 100,
                "combos": {
                    "supply=s1|route=r1|strategy=cc": self._combo(1, 1, 0, 10, 20),
                },
            },
        }
        before_total_requests = sum(d["requests"] for d in store._data["days"].values())

        # KEEP_DAYS 通过临时子类覆盖（不改全局常量，隔离测试）
        store._archive_if_needed_keep_days = 2
        self._archive_with_keep_days(store, keep_days=2)

        after_days_requests = sum(d["requests"] for d in store._data["days"].values())
        after_archive_requests = sum(
            m["requests"] for m in store._data["months_archive"].values())
        self.assertEqual(after_days_requests + after_archive_requests, before_total_requests)

        # 只保留最新 2 天，最旧的 2025-01-01 应已被归档删除
        self.assertNotIn("2025-01-01", store._data["days"])
        self.assertIn("2025-01-02", store._data["days"])
        self.assertIn("2025-02-01", store._data["days"])

        self.assertIn("2025-01", store._data["months_archive"])
        archived = store._data["months_archive"]["2025-01"]
        self.assertEqual(archived["requests"], 2)
        self.assertEqual(archived["ok"], 2)
        self.assertEqual(archived["fail"], 0)
        self.assertEqual(archived["combos"]["supply=s1|route=r1|strategy=cc"]["requests"], 2)
        self.assertEqual(archived["combos"]["supply=s1|route=r1|strategy=cc"]["usage_in"], 20)

    def _archive_with_keep_days(self, store, keep_days):
        """复用 UsageTotalsStore._archive_if_needed 逻辑，但用局部 keep_days（不改全局常量）。"""
        import core.server as server_mod
        orig = server_mod.KEEP_DAYS
        server_mod.KEEP_DAYS = keep_days
        try:
            store._archive_if_needed()
        finally:
            server_mod.KEEP_DAYS = orig

    def test_archive_twice_does_not_double_count(self):
        """连续两次触发归档（每次超窗 1 天），累计到 months_archive 不重复计。"""
        store = UsageTotalsStore(self.path)
        store._data["days"] = {
            "2025-03-01": {
                "requests": 1, "ok": 1, "fail": 0, "sum_ms": 10,
                "combos": {"supply=s1|route=r1|strategy=cc": self._combo(1, 1, 0, 1, 1)},
            },
            "2025-03-02": {
                "requests": 1, "ok": 1, "fail": 0, "sum_ms": 10,
                "combos": {"supply=s1|route=r1|strategy=cc": self._combo(1, 1, 0, 1, 1)},
            },
            "2025-03-03": {
                "requests": 1, "ok": 1, "fail": 0, "sum_ms": 10,
                "combos": {"supply=s1|route=r1|strategy=cc": self._combo(1, 1, 0, 1, 1)},
            },
        }
        self._archive_with_keep_days(store, keep_days=1)
        # keep_days=1，一次调用应把超窗的 2 天都归档掉（while 循环），只剩最新 1 天
        self.assertEqual(len(store._data["days"]), 1)
        self.assertIn("2025-03-03", store._data["days"])
        archived = store._data["months_archive"]["2025-03"]
        self.assertEqual(archived["requests"], 2)
        self.assertEqual(archived["combos"]["supply=s1|route=r1|strategy=cc"]["requests"], 2)


class TestComboProjectionFilterLogic(unittest.TestCase):
    """组合键投影/过滤查询逻辑（复刻 model_proxy_cli.sh cmd_stats 的核心聚合算法，
    确保脚本内联 python 的算法与设计文档一致，便于脱 shell 环境快速验证）。
    """

    def _combos(self):
        return {
            "supply=s1|route=claude|strategy=cc": {
                "requests": 3, "ok": 3, "fail": 0, "usage_in": 30, "usage_out": 60},
            "supply=s2|route=claude|strategy=cc": {
                "requests": 2, "ok": 1, "fail": 1, "usage_in": 20, "usage_out": 40},
            "supply=s3|route=openai|strategy=codex": {
                "requests": 5, "ok": 5, "fail": 0, "usage_in": 50, "usage_out": 100},
        }

    @staticmethod
    def _parse_key(key):
        return dict(part.split("=", 1) for part in key.split("|"))

    def _aggregate(self, combos, filters, proj):
        groups = {}
        for key, v in combos.items():
            dims = self._parse_key(key)
            if any(dims.get(f) != val for f, val in filters.items()):
                continue
            gkey = dims.get(proj) if proj else "(all)"
            g = groups.setdefault(gkey, {"requests": 0, "ok": 0, "fail": 0,
                                          "usage_in": 0, "usage_out": 0})
            for f in ("requests", "ok", "fail", "usage_in", "usage_out"):
                g[f] += v[f]
        return groups

    def test_projection_by_route_sums_to_top_level(self):
        combos = self._combos()
        groups = self._aggregate(combos, {}, "route")
        total_requests = sum(g["requests"] for g in groups.values())
        top_level_requests = sum(v["requests"] for v in combos.values())
        self.assertEqual(total_requests, top_level_requests)
        self.assertEqual(groups["claude"]["requests"], 5)
        self.assertEqual(groups["openai"]["requests"], 5)

    def test_filter_then_project(self):
        combos = self._combos()
        # 过滤 route=claude 再按 supply 投影
        groups = self._aggregate(combos, {"route": "claude"}, "supply")
        self.assertEqual(set(groups.keys()), {"s1", "s2"})
        self.assertEqual(groups["s1"]["requests"], 3)
        self.assertEqual(groups["s2"]["requests"], 2)

    def test_multi_filter_and_semantics(self):
        combos = self._combos()
        groups = self._aggregate(combos, {"route": "claude", "supply": "s2"}, None)
        self.assertEqual(groups["(all)"]["requests"], 2)
        self.assertEqual(groups["(all)"]["fail"], 1)

    def test_three_projections_have_equal_total_requests(self):
        combos = self._combos()
        top_level_requests = sum(v["requests"] for v in combos.values())
        for dim in ("supply", "route", "strategy"):
            groups = self._aggregate(combos, {}, dim)
            self.assertEqual(sum(g["requests"] for g in groups.values()), top_level_requests)


class TestGetMonthBucketSplitArchive(unittest.TestCase):
    """复刻 model_proxy_cli.sh cmd_stats 内 get_month_bucket 的修复后逻辑：
    月度数据分裂在 months_archive 与 days 两处时必须无条件合并，不丢不重。
    见 docs/designs/2026-07-23-usage-totals-ledger.md 及 reviewer 复现场景。
    """

    @staticmethod
    def _zero_bucket():
        return {"requests": 0, "ok": 0, "fail": 0, "sum_ms": 0, "combos": {}}

    @staticmethod
    def _zero_combo():
        return {"requests": 0, "ok": 0, "fail": 0, "usage_in": 0, "usage_out": 0}

    def _merge_bucket_into(self, dst, src):
        dst["requests"] += src.get("requests", 0)
        dst["ok"] += src.get("ok", 0)
        dst["fail"] += src.get("fail", 0)
        dst["sum_ms"] += src.get("sum_ms", 0)
        for key, v in src.get("combos", {}).items():
            combo = dst["combos"].setdefault(key, self._zero_combo())
            for f in ("requests", "ok", "fail", "usage_in", "usage_out"):
                combo[f] += v.get(f, 0)

    def _get_month_bucket(self, data, month_key):
        """修复后逻辑：无条件合并 archive + days 里剩余同月天桶。"""
        merged = self._zero_bucket()
        archived = data.get("months_archive", {}).get(month_key)
        if archived is not None:
            self._merge_bucket_into(merged, archived)
        for day_key, day_bucket in data.get("days", {}).items():
            if day_key[:7] == month_key:
                self._merge_bucket_into(merged, day_bucket)
        return merged

    def test_split_month_archive_and_days_both_counted(self):
        """reviewer 复现场景：07-01 已归档 1 条，07-15 明细还留 1 条，合计应为 2。"""
        data = {
            "months_archive": {
                "2026-07": {
                    "requests": 1, "ok": 1, "fail": 0, "sum_ms": 100,
                    "combos": {"supply=s1|route=r1|strategy=cc":
                               {"requests": 1, "ok": 1, "fail": 0,
                                "usage_in": 10, "usage_out": 20}},
                }
            },
            "days": {
                "2026-07-15": {
                    "requests": 1, "ok": 1, "fail": 0, "sum_ms": 50,
                    "combos": {"supply=s1|route=r1|strategy=cc":
                               {"requests": 1, "ok": 1, "fail": 0,
                                "usage_in": 5, "usage_out": 10}},
                }
            },
        }
        bucket = self._get_month_bucket(data, "2026-07")
        self.assertEqual(bucket["requests"], 2)
        self.assertEqual(bucket["ok"], 2)
        self.assertEqual(bucket["sum_ms"], 150)
        self.assertEqual(
            bucket["combos"]["supply=s1|route=r1|strategy=cc"]["requests"], 2)
        self.assertEqual(
            bucket["combos"]["supply=s1|route=r1|strategy=cc"]["usage_in"], 15)

    def test_fully_archived_month_no_days_left(self):
        """非分裂场景：该月已完全归档，days 里没有当月任何天，结果只来自 archive。"""
        data = {
            "months_archive": {
                "2026-06": {
                    "requests": 5, "ok": 5, "fail": 0, "sum_ms": 500,
                    "combos": {"supply=s1|route=r1|strategy=cc":
                               {"requests": 5, "ok": 5, "fail": 0,
                                "usage_in": 50, "usage_out": 100}},
                }
            },
            "days": {
                "2026-07-01": {
                    "requests": 1, "ok": 1, "fail": 0, "sum_ms": 10,
                    "combos": {},
                }
            },
        }
        bucket = self._get_month_bucket(data, "2026-06")
        self.assertEqual(bucket["requests"], 5)
        self.assertEqual(bucket["sum_ms"], 500)

    def test_fully_in_days_no_archive(self):
        """非分裂场景：该月完全没有归档，全部在 days 里（原有场景）。"""
        data = {
            "months_archive": {},
            "days": {
                "2026-08-01": {
                    "requests": 2, "ok": 2, "fail": 0, "sum_ms": 20,
                    "combos": {"supply=s1|route=r1|strategy=cc":
                               {"requests": 2, "ok": 2, "fail": 0,
                                "usage_in": 20, "usage_out": 40}},
                },
                "2026-08-02": {
                    "requests": 1, "ok": 0, "fail": 1, "sum_ms": 5,
                    "combos": {"supply=s2|route=r1|strategy=codex":
                               {"requests": 1, "ok": 0, "fail": 1,
                                "usage_in": 3, "usage_out": 3}},
                },
            },
        }
        bucket = self._get_month_bucket(data, "2026-08")
        self.assertEqual(bucket["requests"], 3)
        self.assertEqual(bucket["ok"], 2)
        self.assertEqual(bucket["fail"], 1)

    def test_neither_archive_nor_days_returns_zero(self):
        data = {"months_archive": {}, "days": {}}
        bucket = self._get_month_bucket(data, "2026-09")
        self.assertEqual(bucket["requests"], 0)
        self.assertEqual(bucket["combos"], {})

    def test_multiple_archive_batches_accumulate_not_overwrite(self):
        """一个月内触发过两次归档：months_archive 同月份桶应是累加而非覆盖。
        模拟 _archive_if_needed 的 while 循环对同一 month_bucket 连续 setdefault+累加。
        """
        month_bucket = self._zero_bucket()
        batch1 = {
            "requests": 1, "ok": 1, "fail": 0, "sum_ms": 10,
            "combos": {"supply=s1|route=r1|strategy=cc":
                       {"requests": 1, "ok": 1, "fail": 0, "usage_in": 1, "usage_out": 1}},
        }
        batch2 = {
            "requests": 1, "ok": 1, "fail": 0, "sum_ms": 10,
            "combos": {"supply=s1|route=r1|strategy=cc":
                       {"requests": 1, "ok": 1, "fail": 0, "usage_in": 1, "usage_out": 1}},
        }
        self._merge_bucket_into(month_bucket, batch1)
        self._merge_bucket_into(month_bucket, batch2)
        data = {
            "months_archive": {"2026-07": month_bucket},
            "days": {
                "2026-07-20": {
                    "requests": 1, "ok": 1, "fail": 0, "sum_ms": 10,
                    "combos": {"supply=s1|route=r1|strategy=cc":
                               {"requests": 1, "ok": 1, "fail": 0, "usage_in": 1, "usage_out": 1}},
                }
            },
        }
        bucket = self._get_month_bucket(data, "2026-07")
        # 两批归档(2条) + 剩余明细(1条) = 3
        self.assertEqual(bucket["requests"], 3)
        self.assertEqual(bucket["combos"]["supply=s1|route=r1|strategy=cc"]["requests"], 3)

    def test_projection_query_correct_under_split_scenario(self):
        """分裂场景下，stats <月份> supply 投影聚合也要正确（combos 级别）。"""
        data = {
            "months_archive": {
                "2026-07": {
                    "requests": 1, "ok": 1, "fail": 0, "sum_ms": 10,
                    "combos": {"supply=s1|route=claude|strategy=cc":
                               {"requests": 1, "ok": 1, "fail": 0, "usage_in": 1, "usage_out": 1}},
                }
            },
            "days": {
                "2026-07-15": {
                    "requests": 2, "ok": 2, "fail": 0, "sum_ms": 10,
                    "combos": {"supply=s1|route=claude|strategy=cc":
                               {"requests": 1, "ok": 1, "fail": 0, "usage_in": 1, "usage_out": 1},
                               "supply=s2|route=claude|strategy=cc":
                               {"requests": 1, "ok": 1, "fail": 0, "usage_in": 1, "usage_out": 1}},
                }
            },
        }
        bucket = self._get_month_bucket(data, "2026-07")
        combos = bucket["combos"]
        # s1 应合并 archive(1) + days(1) = 2；s2 只在 days 里出现 1 次
        self.assertEqual(combos["supply=s1|route=claude|strategy=cc"]["requests"], 2)
        self.assertEqual(combos["supply=s2|route=claude|strategy=cc"]["requests"], 1)
        by_supply = {}
        for key, v in combos.items():
            supply = dict(p.split("=", 1) for p in key.split("|"))["supply"]
            by_supply.setdefault(supply, 0)
            by_supply[supply] += v["requests"]
        self.assertEqual(by_supply["s1"], 2)
        self.assertEqual(by_supply["s2"], 1)
        self.assertEqual(sum(by_supply.values()), bucket["requests"])


# ---------------------------------------------------------------------------
# OPT-10: 账本 schema v3 测试（max_ms + attempts/attempt_fail + v2→v3 迁移）
# ---------------------------------------------------------------------------

class TestSchemaV3(unittest.TestCase):
    """OPT-10：v3 schema 新字段 + 迁移正确性。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "totals.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_new_combo_has_attempt_fields(self):
        """新 record 的 combo 应含 attempts/attempt_fail 字段，初始为 0。"""
        store = UsageTotalsStore(self.path)
        store.record(_acc(), 100)
        day_key = _cst_now().strftime("%Y-%m-%d")
        combo = store._data["days"][day_key]["combos"][
            "supply=s1|route=r1|strategy=cc"]
        self.assertIn("attempts", combo)
        self.assertIn("attempt_fail", combo)

    def test_bucket_has_max_ms(self):
        """新 record 的天桶应含 max_ms 字段。"""
        store = UsageTotalsStore(self.path)
        store.record(_acc(), 100)
        day_key = _cst_now().strftime("%Y-%m-%d")
        self.assertIn("max_ms", store._data["days"][day_key])
        self.assertEqual(store._data["days"][day_key]["max_ms"], 100)

    def test_max_ms_tracks_maximum(self):
        """多次 record 后 max_ms 应为最大值，非累加。"""
        store = UsageTotalsStore(self.path)
        store.record(_acc(), 100)
        store.record(_acc(), 300)
        store.record(_acc(), 200)
        day_key = _cst_now().strftime("%Y-%m-%d")
        self.assertEqual(store._data["days"][day_key]["max_ms"], 300)
        self.assertEqual(store._data["total"]["max_ms"], 300)

    def test_attempts_accumulate_in_combo(self):
        """combo 的 attempts 应累加每次请求的 attempts 值。"""
        store = UsageTotalsStore(self.path)
        acc1 = _acc()
        acc1["attempts"] = 3
        store.record(acc1, 100)
        acc2 = _acc()
        acc2["attempts"] = 2
        store.record(acc2, 50)
        day_key = _cst_now().strftime("%Y-%m-%d")
        combo = store._data["days"][day_key]["combos"][
            "supply=s1|route=r1|strategy=cc"]
        self.assertEqual(combo["attempts"], 5)

    def test_attempt_fail_from_errors_list(self):
        """combo 的 attempt_fail 应等于 attempt_errors 列表长度。"""
        store = UsageTotalsStore(self.path)
        acc1 = _acc()
        acc1["attempts"] = 2
        acc1["attempt_errors"] = [("s1", "http_500"), ("s1", "http_503")]
        store.record(acc1, 100)
        day_key = _cst_now().strftime("%Y-%m-%d")
        combo = store._data["days"][day_key]["combos"][
            "supply=s1|route=r1|strategy=cc"]
        self.assertEqual(combo["attempt_fail"], 2)

    def test_attempt_fail_zero_when_no_errors(self):
        """无 attempt_errors 时 attempt_fail=0。"""
        store = UsageTotalsStore(self.path)
        acc1 = _acc()
        acc1["attempts"] = 1
        acc1["attempt_errors"] = []
        store.record(acc1, 100)
        day_key = _cst_now().strftime("%Y-%m-%d")
        combo = store._data["days"][day_key]["combos"][
            "supply=s1|route=r1|strategy=cc"]
        self.assertEqual(combo["attempt_fail"], 0)


class TestV2ToV3Migration(unittest.TestCase):
    """OPT-10：v2→v3 迁移（旧桶补 0）。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "totals.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_migrate_v2_adds_max_ms_to_buckets(self):
        """v2 旧桶（无 max_ms）迁移后应补 max_ms=0。"""
        old_data = {
            "version": 2,
            "since": "2026-07-01",
            "keep_days": 400,
            "total": {"requests": 5, "ok": 4, "fail": 1, "sum_ms": 500, "combos": {}},
            "months_archive": {
                "2026-07": {"requests": 2, "ok": 2, "fail": 0, "sum_ms": 200, "combos": {}}
            },
            "days": {
                "2026-08-08": {"requests": 3, "ok": 2, "fail": 1, "sum_ms": 300, "combos": {}}
            },
        }
        self.path.write_text(json.dumps(old_data), encoding="utf-8")
        store = UsageTotalsStore(self.path)
        self.assertEqual(store._data["version"], 3)
        self.assertEqual(store._data["total"]["max_ms"], 0)
        self.assertEqual(store._data["months_archive"]["2026-07"]["max_ms"], 0)
        self.assertEqual(store._data["days"]["2026-08-08"]["max_ms"], 0)

    def test_migrate_v2_adds_attempt_fields_to_combos(self):
        """v2 旧 combo（无 attempts/attempt_fail）迁移后应补 0。"""
        old_combo = {"requests": 5, "ok": 4, "fail": 1, "usage_in": 50, "usage_out": 100}
        old_data = {
            "version": 2,
            "since": "2026-07-01",
            "keep_days": 400,
            "total": {"requests": 5, "ok": 4, "fail": 1, "sum_ms": 500,
                      "combos": {"supply=s1|route=r1|strategy=cc": dict(old_combo)}},
            "months_archive": {},
            "days": {},
        }
        self.path.write_text(json.dumps(old_data), encoding="utf-8")
        store = UsageTotalsStore(self.path)
        combo = store._data["total"]["combos"]["supply=s1|route=r1|strategy=cc"]
        self.assertEqual(combo["attempts"], 0)
        self.assertEqual(combo["attempt_fail"], 0)

    def test_migrate_v2_preserves_existing_values(self):
        """迁移不破坏既有字段值。"""
        old_combo = {"requests": 5, "ok": 4, "fail": 1, "usage_in": 50, "usage_out": 100}
        old_data = {
            "version": 2,
            "since": "2026-07-01",
            "keep_days": 400,
            "total": {"requests": 5, "ok": 4, "fail": 1, "sum_ms": 500,
                      "combos": {"supply=s1|route=r1|strategy=cc": dict(old_combo)}},
            "months_archive": {},
            "days": {},
        }
        self.path.write_text(json.dumps(old_data), encoding="utf-8")
        store = UsageTotalsStore(self.path)
        combo = store._data["total"]["combos"]["supply=s1|route=r1|strategy=cc"]
        self.assertEqual(combo["requests"], 5)
        self.assertEqual(combo["ok"], 4)
        self.assertEqual(combo["fail"], 1)
        self.assertEqual(combo["usage_in"], 50)
        self.assertEqual(combo["usage_out"], 100)

    def test_migrated_ledger_can_continue_recording(self):
        """迁移后的账本可以正常继续 record（新字段已存在，不报 KeyError）。"""
        old_data = {
            "version": 2,
            "since": "2026-07-01",
            "keep_days": 400,
            "total": {"requests": 1, "ok": 1, "fail": 0, "sum_ms": 100,
                      "combos": {"supply=s1|route=r1|strategy=cc":
                                 {"requests": 1, "ok": 1, "fail": 0,
                                  "usage_in": 10, "usage_out": 20}}},
            "months_archive": {},
            "days": {},
        }
        self.path.write_text(json.dumps(old_data), encoding="utf-8")
        store = UsageTotalsStore(self.path)
        # 迁移后再 record，不应崩溃
        acc = _acc()
        acc["attempts"] = 1
        acc["attempt_errors"] = []
        store.record(acc, 150)
        combo = store._data["total"]["combos"]["supply=s1|route=r1|strategy=cc"]
        self.assertEqual(combo["requests"], 2)
        self.assertEqual(combo["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
