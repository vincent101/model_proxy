"""累计用量账本（UsageTotalsStore）单测（脱网络，纯标准库 unittest）。

覆盖：record 累加正确性（天桶顶层+combos+total 顶层+total.combos 四处一致）、
组合键格式、归档边界一致性（不丢不重）、_cst_now 的 UTC+8 正确性（含跨零点边界）。

运行：cd tools/model_proxy && python3 -m unittest tests.test_usage_totals -v
"""

import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import platform_tmp  # noqa: E402  (同目录测试基建)

from core.server import (  # noqa: E402
    _cst_now,
    _CST,
    UsageTotalsStore,
    LedgerObservation,
    LedgerCommitResult,
    LedgerUnavailable,
    validate_migration_ledger_snapshot,
)
from core.config_snapshot import ConfigSnapshot, ConfigSnapshotError, FileMarker, read_stable_config
from core.discovery import PreflightError


class _FakeBusinessDiskAdapter:
    """Test-only temporary-disk fixture, NOT a platform guard or security proof.

    Business-counter regressions need a persistent fake B/C baseline, but the
    direct constructor intentionally has no production write authorization.
    Actual platform/lock/ACL behavior is tested independently in
    test_ledger_secure_io, never inferred from this fixture.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.parent = self.path.parent.resolve(strict=True)
        # 夹具必须待在专用临时目录内（防止误碰真实账本）。Windows 的私有
        # 临时目录在 %LOCALAPPDATA%，不能按系统 tempdir 判断。
        temp_root = Path(platform_tmp.base_dir() or tempfile.gettempdir()).resolve(strict=True)
        if self.parent == temp_root or temp_root not in self.parent.parents:
            raise AssertionError("fake fixture must be inside a dedicated temporary directory")

    def _check(self, path):
        if Path(path) != self.path or self.path.parent.resolve(strict=True) != self.parent:
            raise AssertionError("fake fixture path changed")

    def observe(self, path):
        self._check(path)
        parent = self.parent.stat()
        try:
            self.path.lstat()
        except FileNotFoundError:
            return LedgerObservation(None, (parent.st_dev, parent.st_ino, None, None))
        snapshot = platform_tmp.read_stable(self.path, reject_duplicate_keys=True)
        raw = self.path.read_bytes()
        return LedgerObservation(copy.deepcopy(snapshot.config),
                                 (parent.st_dev, parent.st_ino, snapshot.marker, raw))

    def bind_startup(self, path, snapshot):
        current = self.observe(path)
        if snapshot is None:
            if current.data is not None:
                raise AssertionError("fake baseline appeared")
        elif current.data != snapshot.config or current.evidence[2] != snapshot.marker:
            raise AssertionError("fake baseline changed")
        return current

    @staticmethod
    def matches(observed, prior):
        return observed.data == prior.data and observed.evidence == prior.evidence

    def commit(self, path, baseline, data):
        self._check(path)
        if not self.matches(self.observe(path), baseline):
            return LedgerCommitResult("NOT_APPLIED")
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        # 落到与目标同一目录的临时文件，再原子替换（夹具不模拟提交候选身份）。
        fd, name = tempfile.mkstemp(prefix="fake-ledger-", dir=str(self.parent))
        try:
            with os.fdopen(fd, mode="w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(name, self.path)
            name = None
        finally:
            if name is not None and os.path.exists(name):
                os.unlink(name)
        final = self.observe(path)
        # This fixture does not model precommit candidate identity or failures.
        return LedgerCommitResult("CONFIRMED", final, final)


def _business_store(path, *, mode="direct", snapshot=None):
    """Exercise direct load or verified load, then use the explicit fake disk."""
    path = Path(path)
    if snapshot is None and path.exists():
        snapshot = platform_tmp.read_stable(path, reject_duplicate_keys=True)
    adapter = _FakeBusinessDiskAdapter(path)
    if mode == "direct":
        store = UsageTotalsStore(path)
        store._init_persistence(adapter, snapshot, initial_verified=True)
        return store
    if mode == "snapshot":
        return UsageTotalsStore.from_verified_snapshot(path, snapshot, adapter=adapter)
    raise AssertionError("invalid fixture mode")


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
        self._tmpdir = platform_tmp.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "totals.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_direct_constructor_without_guard_cannot_persist(self):
        store = UsageTotalsStore(self.path)
        store.record(_acc(), 1)
        self.assertEqual(store.persistence_state, "PENDING")
        self.assertEqual(store._data["total"]["requests"], 1)
        self.assertFalse(self.path.exists())

    def test_cold_start_creates_expected_structure(self):
        store = _business_store(self.path)
        data = store._data
        self.assertEqual(data["version"], 3)
        self.assertIn("since", data)
        self.assertEqual(data["keep_days"], 400)
        self.assertEqual(data["total"], {"requests": 0, "ok": 0, "fail": 0,
                         "client_disconnect": 0, "sum_ms": 0, "max_ms": 0, "combos": {}})
        self.assertEqual(data["months_archive"], {})
        self.assertEqual(data["days"], {})
        # record 前文件不落盘（只在 record 时才写）
        self.assertFalse(self.path.exists())

    def test_record_writes_file_and_combo_key_format(self):
        store = _business_store(self.path)
        store.record(_acc(supply="claude-sonnet-sankuai-0956", route="claude", strategy="cc"), 100)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)
        self.assertTrue(self.path.exists())
        with open(self.path) as f:
            on_disk = json.load(f)
        day_key = _cst_now().strftime("%Y-%m-%d")
        self.assertIn(day_key, on_disk["days"])
        combos = on_disk["days"][day_key]["combos"]
        expected_key = "supply=claude-sonnet-sankuai-0956|route=claude|strategy=cc"
        self.assertIn(expected_key, combos)
        self.assertEqual(combos[expected_key]["requests"], 1)

    def test_client_disconnect_is_separate_from_ok_and_fail(self):
        store = _business_store(self.path)
        acc = _acc()
        acc["stream_integrity"] = "client_disconnect"
        store.record(acc, 50)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)
        bucket = store._data["total"]
        combo = next(iter(bucket["combos"].values()))
        self.assertEqual((bucket["ok"], bucket["fail"], bucket["client_disconnect"]),
                         (0, 0, 1))
        self.assertEqual((combo["ok"], combo["fail"], combo["client_disconnect"]),
                         (0, 0, 1))

    def test_observer_error_is_neither_ok_nor_upstream_failure(self):
        store = _business_store(self.path)
        acc = _acc()
        acc["stream_integrity"] = "observer_error"
        store.record(acc, 50)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)
        bucket = store._data["total"]
        self.assertEqual((bucket["ok"], bucket["fail"], bucket["client_disconnect"]),
                         (0, 0, 0))

    def test_none_dims_use_placeholder(self):
        store = _business_store(self.path)
        store.record(_acc(supply="", route="", strategy=""), 50)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)
        day_key = _cst_now().strftime("%Y-%m-%d")
        combos = store._data["days"][day_key]["combos"]
        self.assertIn("supply=(none)|route=(none)|strategy=(none)", combos)

    def test_accumulation_top_level_equals_sum_of_combos(self):
        store = _business_store(self.path)
        store.record(_acc(supply="s1", route="r1", strategy="cc", status=200), 100)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)
        store.record(_acc(supply="s1", route="r1", strategy="cc", status=200), 150)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)
        store.record(_acc(supply="s2", route="r1", strategy="codex", status=500), 200)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)

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
        store = _business_store(self.path)
        store.record(_acc(), 100)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)
        day_key = _cst_now().strftime("%Y-%m-%d")
        for v in store._data["days"][day_key]["combos"].values():
            self.assertNotIn("sum_ms", v)

    def test_reload_from_disk_preserves_data(self):
        store1 = _business_store(self.path)
        store1.record(_acc(), 100)
        self.assertEqual(store1.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store1._data)
        store2 = _business_store(self.path)  # 模拟重启：重新 load
        self.assertEqual(store2._data["total"]["requests"], 1)
        # 重启后继续 record，在原基础上累加而非从头
        store2.record(_acc(), 50)
        self.assertEqual(store2.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store2._data)
        self.assertEqual(store2._data["total"]["requests"], 2)
        self.assertEqual(json.loads(self.path.read_text())["total"]["requests"], 2)


class TestUsageTotalsStoreCorruptRecovery(unittest.TestCase):

    def setUp(self):
        self._tmpdir = platform_tmp.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "totals.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_corrupt_json_fails_without_renaming_or_resetting(self):
        self.path.write_text("{ not valid json ", encoding="utf-8")
        before, inode = self.path.read_bytes(), self.path.stat().st_ino
        with self.assertRaises(PreflightError) as caught:
            UsageTotalsStore(self.path)
        self.assertEqual(caught.exception.code, "ledger_unverified")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertEqual(list(self.path.parent.glob(self.path.name + ".corrupt.*")), [])

    def test_invalid_semantic_ledger_fails_without_renaming_or_resetting(self):
        data = {"version": 3, "total": {"requests": 1, "fail": 0, "sum_ms": 1, "combos": {}},
                "days": {}, "months_archive": {}}
        self.path.write_text(json.dumps(data), encoding="utf-8")
        before, inode = self.path.read_bytes(), self.path.stat().st_ino
        with self.assertRaises(PreflightError) as caught:
            UsageTotalsStore(self.path)
        self.assertEqual(caught.exception.code, "ledger_unverified")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertEqual(list(self.path.parent.glob(self.path.name + ".corrupt.*")), [])

    def test_existing_unreadable_ledger_never_becomes_empty_or_gets_renamed(self):
        self.path.write_text('{"fake":"sentinel"}', encoding="utf-8")
        before, inode = self.path.read_bytes(), self.path.stat().st_ino
        with patch("core.server.read_stable_config", side_effect=ConfigSnapshotError("config_read_error")):
            with self.assertRaises(PreflightError) as caught:
                UsageTotalsStore(self.path)
        self.assertEqual(caught.exception.code, "ledger_unverified")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertEqual(list(self.path.parent.glob(self.path.name + ".corrupt.*")), [])

    def test_dangling_ledger_link_is_unknown_not_empty(self):
        self.path.symlink_to(self.path.parent / "missing-target")
        with self.assertRaises(PreflightError) as caught:
            UsageTotalsStore(self.path)
        self.assertEqual(caught.exception.code, "ledger_unverified")
        self.assertTrue(self.path.is_symlink())
        self.assertFalse((self.path.parent / "missing-target").exists())



class TestArchiveBoundary(unittest.TestCase):
    """归档边界一致性：超窗最旧天桶按组合键汇总进 months_archive 且从 days 删除，不丢不重。"""

    def setUp(self):
        self._tmpdir = platform_tmp.TemporaryDirectory()
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
        self._tmpdir = platform_tmp.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "totals.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_new_combo_has_attempt_fields(self):
        """新 record 的 combo 应含 attempts/attempt_fail 字段，初始为 0。"""
        store = _business_store(self.path)
        store.record(_acc(), 100)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)
        day_key = _cst_now().strftime("%Y-%m-%d")
        combo = store._data["days"][day_key]["combos"][
            "supply=s1|route=r1|strategy=cc"]
        self.assertIn("attempts", combo)
        self.assertIn("attempt_fail", combo)

    def test_bucket_has_max_ms(self):
        """新 record 的天桶应含 max_ms 字段。"""
        store = _business_store(self.path)
        store.record(_acc(), 100)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)
        day_key = _cst_now().strftime("%Y-%m-%d")
        self.assertIn("max_ms", store._data["days"][day_key])
        self.assertEqual(store._data["days"][day_key]["max_ms"], 100)

    def test_max_ms_tracks_maximum(self):
        """多次 record 后 max_ms 应为最大值，非累加。"""
        store = _business_store(self.path)
        store.record(_acc(), 100)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)
        store.record(_acc(), 300)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)
        store.record(_acc(), 200)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)
        day_key = _cst_now().strftime("%Y-%m-%d")
        self.assertEqual(store._data["days"][day_key]["max_ms"], 300)
        self.assertEqual(store._data["total"]["max_ms"], 300)

    def test_attempts_accumulate_in_combo(self):
        """combo 的 attempts 应累加每次请求的 attempts 值。"""
        store = _business_store(self.path)
        acc1 = _acc()
        acc1["attempts"] = 3
        store.record(acc1, 100)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)
        acc2 = _acc()
        acc2["attempts"] = 2
        store.record(acc2, 50)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)
        day_key = _cst_now().strftime("%Y-%m-%d")
        combo = store._data["days"][day_key]["combos"][
            "supply=s1|route=r1|strategy=cc"]
        self.assertEqual(combo["attempts"], 5)

    def test_attempt_fail_from_errors_list(self):
        """combo 的 attempt_fail 应等于 attempt_errors 列表长度。"""
        store = _business_store(self.path)
        acc1 = _acc()
        acc1["attempts"] = 2
        acc1["attempt_errors"] = [("s1", "http_500"), ("s1", "http_503")]
        store.record(acc1, 100)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)
        day_key = _cst_now().strftime("%Y-%m-%d")
        combo = store._data["days"][day_key]["combos"][
            "supply=s1|route=r1|strategy=cc"]
        self.assertEqual(combo["attempt_fail"], 2)

    def test_attempt_fail_zero_when_no_errors(self):
        """无 attempt_errors 时 attempt_fail=0。"""
        store = _business_store(self.path)
        acc1 = _acc()
        acc1["attempts"] = 1
        acc1["attempt_errors"] = []
        store.record(acc1, 100)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)
        day_key = _cst_now().strftime("%Y-%m-%d")
        combo = store._data["days"][day_key]["combos"][
            "supply=s1|route=r1|strategy=cc"]
        self.assertEqual(combo["attempt_fail"], 0)


class TestV2ToV3Migration(unittest.TestCase):
    """OPT-10：v2→v3 迁移（旧桶补 0）。"""

    def setUp(self):
        self._tmpdir = platform_tmp.TemporaryDirectory()
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
        store = _business_store(self.path)
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
        store = _business_store(self.path)
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
        store = _business_store(self.path)
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
        store = _business_store(self.path)
        # 迁移后再 record，不应崩溃
        acc = _acc()
        acc["attempts"] = 1
        acc["attempt_errors"] = []
        store.record(acc, 150)
        self.assertEqual(store.persistence_state, "CLEAN")
        self.assertEqual(json.loads(self.path.read_text()), store._data)
        combo = store._data["total"]["combos"]["supply=s1|route=r1|strategy=cc"]
        self.assertEqual(combo["requests"], 2)
        self.assertEqual(combo["attempts"], 1)


class TestMigrationLedgerValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = platform_tmp.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "totals.json"
        self.combo_key = "supply=s1|route=r1|strategy=cc"
        self.today = _cst_now().strftime("%Y-%m-%d")

    def bucket(self, version=3):
        combo = {"requests": 2, "ok": 1, "fail": 1, "usage_in": 10, "usage_out": 20}
        result = {"requests": 2, "ok": 1, "fail": 1, "sum_ms": 30,
                  "combos": {self.combo_key: combo}}
        if version == 3:
            result.update(client_disconnect=0, max_ms=20)
            combo.update(client_disconnect=0, attempts=2, attempt_fail=1)
        return result

    def ledger(self, version=3):
        return {"version": version, "since": "2026-01-01", "keep_days": 400,
                "total": self.bucket(version), "days": {self.today: self.bucket(version)},
                "months_archive": {"2026-01": self.bucket(version)},
                "extra_metadata": {"keep": "unchanged"}}

    @staticmethod
    def snapshot(data):
        return ConfigSnapshot(data, FileMarker(1, 1, 1, 1, 1))

    def assert_invalid(self, data):
        snapshot = self.snapshot(data)
        before = repr(data)
        with patch("builtins.open", side_effect=AssertionError("unexpected I/O")), \
             patch("os.replace", side_effect=AssertionError("unexpected write")):
            with self.assertRaises(PreflightError) as caught:
                validate_migration_ledger_snapshot(snapshot)
            with self.assertRaises(PreflightError) as store_caught:
                UsageTotalsStore.from_verified_snapshot(self.path, snapshot)
        self.assertEqual(caught.exception.code, "ledger_unverified")
        self.assertEqual(store_caught.exception.code, "ledger_unverified")
        self.assertNotIn("FAKE_PRIVATE_VALUE", str(caught.exception))
        self.assertEqual(repr(data), before)

    def test_missing_snapshot_and_valid_v2_v3_are_pure(self):
        for version in (2, 3):
            with self.subTest(version=version):
                data = self.ledger(version)
                original = copy.deepcopy(data)
                snapshot = self.snapshot(data)
                with patch("builtins.open", side_effect=AssertionError("unexpected I/O")), \
                     patch("os.replace", side_effect=AssertionError("unexpected write")):
                    self.assertIsNone(validate_migration_ledger_snapshot(snapshot))
                    self.assertIsNone(validate_migration_ledger_snapshot(None))
                self.assertEqual(data, original)
        with self.assertRaises(PreflightError) as caught:
            validate_migration_ledger_snapshot({"version": 3})
        self.assertEqual(caught.exception.code, "ledger_unverified")

    def test_historical_optional_metadata_and_extension_fields_may_be_absent(self):
        data = self.ledger(3)
        del data["since"]
        del data["keep_days"]
        for bucket in [data["total"], *data["days"].values(), *data["months_archive"].values()]:
            bucket.pop("client_disconnect")
            bucket.pop("max_ms")
            for combo in bucket["combos"].values():
                combo.pop("client_disconnect")
                combo.pop("attempts")
                combo.pop("attempt_fail")
        self.assertIsNone(validate_migration_ledger_snapshot(self.snapshot(data)))

    def test_present_metadata_must_have_valid_types_and_dates(self):
        for field, bad in (("since", "2026-02-30"), ("since", "2026-1-01"),
                           ("since", 20260101), ("keep_days", 0), ("keep_days", -1),
                           ("keep_days", True), ("keep_days", "400")):
            data = self.ledger()
            data[field] = bad
            with self.subTest(field=field, bad=repr(bad)):
                self.assert_invalid(data)

    def test_unknown_versions_and_bad_root_structure_rejected(self):
        for value in (None, True, 1, 4, "2", 2.0):
            data = self.ledger()
            data["version"] = value
            with self.subTest(version=value):
                self.assert_invalid(data)
        for field in ("total", "days", "months_archive"):
            for value in ("missing", None, [], "bad", 1):
                if field != "total" and value == "missing":
                    continue
                data = self.ledger()
                if value == "missing":
                    del data[field]
                else:
                    data[field] = value
                with self.subTest(field=field, value=repr(value)):
                    self.assert_invalid(data)

    def test_v2_omitted_defaults_continue_recording_without_losing_counts(self):
        import core.server as server_module
        frozen = datetime(2026, 1, 3, tzinfo=timezone.utc)
        for missing_version in (False, True):
            for missing_days in (False, True):
                for missing_months in (False, True):
                    for mode in ("direct", "snapshot"):
                        with self.subTest(missing_version=missing_version,
                                          missing_days=missing_days,
                                          missing_months=missing_months, mode=mode):
                            data = self.ledger(2)
                            data["days"] = {
                                "2026-01-01": self.bucket(2),
                                "2026-01-02": self.bucket(2),
                            }
                            data["months_archive"] = {"2026-01": self.bucket(2)}
                            if missing_version:
                                del data["version"]
                            if missing_days:
                                del data["days"]
                            if missing_months:
                                del data["months_archive"]
                            original = copy.deepcopy(data)
                            self.path.write_text(json.dumps(data), encoding="utf-8")
                            before, inode = self.path.read_bytes(), self.path.stat().st_ino
                            snapshot = platform_tmp.read_stable(self.path)
                            with patch("builtins.open", side_effect=AssertionError("unexpected I/O")):
                                self.assertIsNone(validate_migration_ledger_snapshot(snapshot))
                            with patch.object(server_module, "KEEP_DAYS", 1), \
                                 patch.object(server_module, "_cst_now", return_value=frozen):
                                store = _business_store(self.path, mode=mode, snapshot=snapshot)
                                self.assertEqual(store._data["version"], 3)
                                self.assertEqual("days" in store._data, not missing_days)
                                self.assertEqual("months_archive" in store._data, not missing_months)
                                self.assertEqual(self.path.read_bytes(), before)
                                self.assertEqual(self.path.stat().st_ino, inode)
                                store.record(_acc(), 7)
                                self.assertEqual(store.persistence_state, "CLEAN")
                                self.assertEqual(json.loads(self.path.read_text()), store._data)
                            actual = json.loads(self.path.read_text())
                            self.assertEqual(actual["total"]["requests"], 3)
                            self.assertEqual(actual["total"]["ok"], 2)
                            self.assertEqual(actual["total"]["fail"], 1)
                            self.assertEqual(actual["total"]["sum_ms"], 37)
                            self.assertEqual(actual["total"]["combos"][self.combo_key]["usage_in"], 20)
                            self.assertEqual(actual["extra_metadata"], original["extra_metadata"])
                            self.assertEqual(snapshot.config, original)
                            expected_month = (0 if missing_months else 2) + (0 if missing_days else 4)
                            self.assertEqual(
                                actual.get("months_archive", {}).get("2026-01", {}).get("requests", 0),
                                expected_month,
                            )
                            self.assertEqual("months_archive" in actual,
                                             not missing_months or not missing_days)
                            validate_migration_ledger_snapshot(platform_tmp.read_stable(self.path))
                            self.path.unlink()

    def test_v3_omitted_optional_containers_are_not_fabricated_on_load(self):
        for fields in (("days",), ("months_archive",), ("days", "months_archive")):
            with self.subTest(missing=fields):
                data = self.ledger(3)
                for field in fields:
                    del data[field]
                self.path.write_text(json.dumps(data), encoding="utf-8")
                snapshot = platform_tmp.read_stable(self.path)
                validate_migration_ledger_snapshot(snapshot)
                store = _business_store(self.path, mode="snapshot", snapshot=snapshot)
                for field in fields:
                    self.assertNotIn(field, store._data)
                self.assertEqual(snapshot.config, data)
                store.record(_acc(), 7)
                self.assertEqual(store.persistence_state, "CLEAN")
                self.assertEqual(json.loads(self.path.read_text()), store._data)
                actual = json.loads(self.path.read_text())
                self.assertEqual(actual["total"]["requests"], 3)
                self.assertEqual(actual["total"]["combos"][self.combo_key]["usage_in"], 20)
                if "months_archive" in fields:
                    self.assertNotIn("months_archive", actual)
                self.path.unlink()

    def test_v2_missing_required_counter_is_not_fabricated(self):
        for location in ("total", "days", "months_archive"):
            for field in ("requests", "ok", "fail", "sum_ms", "combos"):
                data = self.ledger(2)
                bucket = data["total"] if location == "total" else next(iter(data[location].values()))
                del bucket[field]
                with self.subTest(location=location, field=field):
                    self.assert_invalid(data)
            for field in ("requests", "ok", "fail", "usage_in", "usage_out"):
                data = self.ledger(2)
                bucket = data["total"] if location == "total" else next(iter(data[location].values()))
                del bucket["combos"][self.combo_key][field]
                with self.subTest(location=location, combo_field=field):
                    self.assert_invalid(data)

    def test_every_bucket_requires_arithmetic_fields_and_dict_combos(self):
        for location in ("total", "days", "months_archive"):
            for field in ("requests", "ok", "fail", "sum_ms", "combos"):
                for bad in ("missing", None, "bad", -1, True):
                    data = self.ledger()
                    bucket = data["total"] if location == "total" else next(iter(data[location].values()))
                    if bad == "missing":
                        del bucket[field]
                    else:
                        bucket[field] = bad
                    with self.subTest(location=location, field=field, bad=repr(bad)):
                        self.assert_invalid(data)

    def test_every_combo_requires_counters_and_integer_nonnegative_values(self):
        for location in ("total", "days", "months_archive"):
            for field in ("requests", "ok", "fail", "usage_in", "usage_out"):
                for bad in ("missing", None, "bad", -1, True, 0.5, float("nan"), float("inf")):
                    data = self.ledger()
                    bucket = data["total"] if location == "total" else next(iter(data[location].values()))
                    combo = bucket["combos"][self.combo_key]
                    if bad == "missing":
                        del combo[field]
                    else:
                        combo[field] = bad
                    with self.subTest(location=location, field=field, bad=repr(bad)):
                        self.assert_invalid(data)

    def test_optional_fields_when_present_must_be_valid(self):
        for field in ("client_disconnect", "max_ms"):
            for bad in (None, "bad", -1, True, 0.25, float("inf")):
                data = self.ledger(2)
                data["total"][field] = bad
                with self.subTest(field=field, bad=repr(bad)):
                    self.assert_invalid(data)
        for field in ("client_disconnect", "attempts", "attempt_fail", "usage_reasoning"):
            for bad in (None, "bad", -1, True, 0.25, float("nan")):
                data = self.ledger(2)
                data["total"]["combos"][self.combo_key][field] = bad
                with self.subTest(field=field, bad=repr(bad)):
                    self.assert_invalid(data)

    def test_impossible_local_counter_relations_are_rejected(self):
        for location in ("total", "days", "months_archive"):
            for kind in ("success_failure", "disconnect", "max_duration", "combo_outcomes", "combo_disconnect"):
                data = self.ledger()
                bucket = data["total"] if location == "total" else next(iter(data[location].values()))
                combo = bucket["combos"][self.combo_key]
                if kind == "success_failure":
                    bucket["ok"] = bucket["requests"]
                elif kind == "disconnect":
                    bucket["client_disconnect"] = bucket["requests"] + 1
                elif kind == "max_duration":
                    bucket["max_ms"] = bucket["sum_ms"] + 1
                elif kind == "combo_outcomes":
                    combo["ok"] = combo["requests"]
                else:
                    combo["client_disconnect"] = combo["requests"] + 1
                with self.subTest(location=location, relation=kind):
                    self.assert_invalid(data)

    def test_disconnect_and_failure_may_overlap(self):
        data = self.ledger()
        bucket = data["total"]
        bucket.update(requests=2, ok=0, fail=2, client_disconnect=2)
        combo = bucket["combos"][self.combo_key]
        combo.update(requests=2, ok=0, fail=2, client_disconnect=2)
        self.assertIsNone(validate_migration_ledger_snapshot(self.snapshot(data)))

    def test_bad_buckets_combo_values_and_date_keys_rejected(self):
        for location in ("total", "days", "months_archive"):
            for bad in ([], None, "bad", 1):
                data = self.ledger()
                if location == "total":
                    data[location] = bad
                else:
                    key = next(iter(data[location]))
                    data[location][key] = bad
                with self.subTest(location=location, bad=repr(bad)):
                    self.assert_invalid(data)
        for bad in ([], None, "bad", 1):
            data = self.ledger()
            data["total"]["combos"][self.combo_key] = bad
            with self.subTest(combo=repr(bad)):
                self.assert_invalid(data)
        for location, key in (("days", "2026-02-30"), ("days", "2026-1-01"),
                              ("days", "not-a-date"), ("months_archive", "2026-13"),
                              ("months_archive", "2026-1")):
            data = self.ledger()
            data[location][key] = self.bucket()
            with self.subTest(location=location, key=key):
                self.assert_invalid(data)
        data = self.ledger()
        data["total"]["combos"][""] = self.bucket()["combos"][self.combo_key]
        self.assert_invalid(data)

    def test_windows_guarded_snapshot_branch_is_only_mocked_not_native(self):
        from types import SimpleNamespace
        import core.server as server_module
        identity = SimpleNamespace(canonical="fixture-path", exists=True, link_count=1, device=7, inode=9)
        preflight = SimpleNamespace(as_dict=lambda: {"totals": identity})
        valid = ConfigSnapshot(self.ledger(), FileMarker(7, 9, 1, 1, 1))
        fake_path = object()
        with patch.object(server_module, "Path", return_value=fake_path), \
             patch.object(server_module.os, "name", "nt"), \
             patch.object(server_module, "current_file_marker", return_value=valid.marker) as marker, \
             patch.object(server_module, "read_stable_config", return_value=valid) as read:
            self.assertIs(server_module._capture_migration_ledger(preflight), valid)
        # trusted_boundary 是后加的透传口（Windows 上限定读取边界），缺省 None
        read.assert_called_once_with(fake_path, reject_duplicate_keys=True,
                                     trusted_boundary=None)
        self.assertEqual(marker.call_count, 2)
        self.assertTrue(all(call.args == (fake_path,) for call in marker.call_args_list))

        invalid_data = self.ledger()
        del invalid_data["total"]["ok"]
        invalid = ConfigSnapshot(invalid_data, valid.marker)
        with patch.object(server_module, "Path", return_value=fake_path), \
             patch.object(server_module.os, "name", "nt"), \
             patch.object(server_module, "current_file_marker", return_value=valid.marker) as marker, \
             patch.object(server_module, "read_stable_config", return_value=invalid), \
             patch("os.replace", side_effect=AssertionError("unexpected write")) as replace:
            captured = server_module._capture_migration_ledger(preflight)
            self.assertIsInstance(captured, LedgerUnavailable)
            self.assertEqual(captured.marker, valid.marker)
            store = UsageTotalsStore.from_verified_snapshot(fake_path, captured)
            store.record(_acc(), 7)
            self.assertEqual(store.persistence_state, "UNAVAILABLE")
            self.assertEqual(store._data["total"]["requests"], 1)
            replace.assert_not_called()
        self.assertEqual(marker.call_count, 2)

    def test_windows_guarded_duplicate_ledger_is_rejected_before_preflight(self):
        from types import SimpleNamespace
        import core.server as server_module

        identity = SimpleNamespace(canonical=r"C:\fixture\totals.json", exists=True,
                                   link_count=1, device=7, inode=9)
        preflight = SimpleNamespace(as_dict=lambda: {"totals": identity})
        raw = b'{"version":2,"days":{"fake":1},"days":{},"secret":"FAKE_PRIVATE_VALUE"}'
        guarded = SimpleNamespace(marker=(7, 9, 1, 1, len(raw)), data=raw)
        with patch.object(server_module, "Path", return_value=identity.canonical), \
             patch.object(server_module.os, "name", "nt"), \
             patch("core.platform_windows.read_guarded_file", return_value=guarded) as read, \
             patch("os.replace", side_effect=AssertionError("unexpected write")) as replace:
            captured = server_module._capture_migration_ledger(preflight)
            self.assertIsInstance(captured, LedgerUnavailable)
            self.assertEqual(captured.marker, FileMarker(*guarded.marker))
            store = UsageTotalsStore.from_verified_snapshot(identity.canonical, captured)
            store.record(_acc(), 7)
            self.assertEqual(store.persistence_state, "UNAVAILABLE")
            self.assertEqual(store._data["total"]["requests"], 1)
            self.assertNotIn("FAKE_PRIVATE_VALUE", repr(captured))
            replace.assert_not_called()
        self.assertEqual(read.call_count, 3)
        # trusted_boundary 是后加的透传口：Windows 侧要据此把读取限制在项目边界内，
        # 缺省 None 也必须显式传给底层。
        self.assertEqual([call.kwargs for call in read.call_args_list],
                         [{"with_data": False, "trusted_boundary": None},
                          {"with_data": True, "trusted_boundary": None},
                          {"with_data": False, "trusted_boundary": None}])

    def test_windows_capture_rejects_identity_io_and_marker_drift(self):
        from types import SimpleNamespace
        import core.server as server_module
        identity = SimpleNamespace(canonical="fixture-path", exists=True, link_count=1, device=7, inode=9)
        preflight = SimpleNamespace(as_dict=lambda: {"totals": identity})
        marker = FileMarker(7, 9, 1, 1, 1)
        valid = ConfigSnapshot(self.ledger(), marker)
        fake_path = object()
        with patch.object(server_module, "Path", return_value=fake_path), \
             patch.object(server_module.os, "name", "nt"):
            with patch.object(server_module, "current_file_marker", side_effect=[marker, FileMarker(7, 9, 2, 2, 1)]), \
                 patch.object(server_module, "read_stable_config", return_value=valid):
                with self.assertRaises(PreflightError) as changed:
                    server_module._capture_migration_ledger(preflight)
                self.assertEqual(changed.exception.code, "ledger_changed")
            with patch.object(server_module, "current_file_marker", return_value=marker), \
                 patch.object(server_module, "read_stable_config", side_effect=ConfigSnapshotError("config_read_error")):
                with self.assertRaises(PreflightError) as denied:
                    server_module._capture_migration_ledger(preflight)
                self.assertEqual(denied.exception.code, "ledger_unverified")
            identity.link_count = 2
            with patch.object(server_module, "current_file_marker", return_value=marker):
                with self.assertRaises(PreflightError) as linked:
                    server_module._capture_migration_ledger(preflight)
                self.assertEqual(linked.exception.code, "ledger_changed")

    @unittest.skipUnless(os.name == "posix", "POSIX file-identity fixture")
    def test_ledger_capture_acl_and_identity_fail_closed(self):
        import core.server as server_module
        from core.discovery import inspect_runtime_identities
        from core.runtime_paths import PATH_KEYS
        project = self.path.parent / "fake-project-acl"
        runtime = self.path.parent / "fake-runtime-acl"
        project.mkdir(mode=0o700)
        runtime.mkdir(mode=0o700)
        paths = {key: runtime / key for key in PATH_KEYS}
        paths["totals"] = self.path
        self.path.write_text(json.dumps(self.ledger()), encoding="utf-8")
        os.chmod(self.path, 0o600)
        before = self.path.read_bytes()
        snapshot = inspect_runtime_identities(project, paths)
        with patch.object(server_module, "verify_no_extended_acl",
                          side_effect=PreflightError("protection_unavailable", "fake ACL")), \
             patch("os.replace", side_effect=AssertionError("unexpected write")) as replace:
            with self.assertRaises(PreflightError) as acl:
                server_module._capture_migration_ledger(snapshot)
            self.assertEqual(acl.exception.code, "protection_unavailable")
            replace.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        replacement = self.path.parent / "fake-replacement"
        replacement.write_bytes(before)
        os.chmod(replacement, 0o600)
        os.replace(replacement, self.path)
        with self.assertRaises(PreflightError) as changed:
            server_module._capture_migration_ledger(snapshot)
        self.assertEqual(changed.exception.code, "ledger_unverified")
        self.assertEqual(self.path.read_bytes(), before)

    def test_bad_snapshot_never_opens_renames_or_changes_existing_file(self):
        data = self.ledger()
        del data["total"]["ok"]
        data["FAKE_PRIVATE_VALUE"] = "FAKE_PRIVATE_VALUE"
        self.path.write_text(json.dumps(data))
        before, inode = self.path.read_bytes(), self.path.stat().st_ino
        snapshot = platform_tmp.read_stable(self.path)
        self.assert_invalid(snapshot.config)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertEqual(list(self.path.parent.glob("totals.json.corrupt.*")), [])

    def test_duplicate_keys_are_rejected_by_both_ledger_readers_without_writes(self):
        import core.server as server_module
        from core.discovery import inspect_runtime_identities
        from core.runtime_paths import PATH_KEYS

        project = self.path.parent / "fake-project"
        runtime = self.path.parent / "fake-runtime"
        project.mkdir(mode=0o700)
        runtime.mkdir(mode=0o700)
        paths = {key: runtime / key for key in PATH_KEYS}
        paths["totals"] = self.path
        valid = self.ledger(3)
        valid["private_value"] = "FAKE_PRIVATE_VALUE"
        text = json.dumps(valid, separators=(",", ":"))
        root_duplicate = text[:-1] + ',"days":{}}'
        nested_duplicate = text.replace('"usage_in":10', '"usage_in":10,"usage_in":0', 1)
        reasoning_duplicate = text.replace(
            '"usage_out":20', '"usage_out":20,"usage_reasoning":9,"usage_reasoning":0', 1)
        malformed = (
            ("root days", root_duplicate.encode()),
            ("nested counter", nested_duplicate.encode()),
            ("nested reasoning", reasoning_duplicate.encode()),
            ("bad syntax", b'{"version":2,"days":'),
            ("bad utf8", b'{"version":2,"value":"\xff"}'),
            ("non-object", b'[]'),
        )
        for label, raw in malformed:
            with self.subTest(case=label):
                self.path.write_bytes(raw)
                before, inode = self.path.read_bytes(), self.path.stat().st_ino
                snapshot = inspect_runtime_identities(project, paths)
                with patch("os.replace", side_effect=AssertionError("unexpected write")) as replace, \
                     patch.object(server_module, "read_stable_config",
                                  wraps=read_stable_config) as strict_read:
                    with self.assertRaises(PreflightError) as direct:
                        UsageTotalsStore(self.path)
                    captured = server_module._capture_migration_ledger(snapshot)
                    self.assertIsInstance(captured, LedgerUnavailable)
                    self.assertIsNotNone(captured.marker)
                    store = UsageTotalsStore.from_verified_snapshot(self.path, captured)
                    store.record(_acc(), 7)
                    self.assertEqual(store.persistence_state, "UNAVAILABLE")
                    self.assertEqual(store._data["total"]["requests"], 1)
                self.assertEqual(direct.exception.code, "ledger_unverified")
                self.assertNotIn("FAKE_PRIVATE_VALUE", str(direct.exception))
                self.assertNotIn("FAKE_PRIVATE_VALUE", repr(captured))
                self.assertEqual(strict_read.call_count, 2)
                self.assertTrue(all(call.kwargs == {"reject_duplicate_keys": True}
                                    for call in strict_read.call_args_list))
                replace.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.path.stat().st_ino, inode)
                self.assertEqual(list(self.path.parent.glob("totals.json.corrupt.*")), [])

    def test_reasoning_counts_archive_across_days_and_existing_months(self):
        import core.server as server_module
        from core.config_snapshot import read_stable_config

        ordinary = "supply=plain|route=r2|strategy=cc"
        untouched = "supply=untouched|route=r3|strategy=cc"
        zero_reasoning = "supply=zero|route=r4|strategy=cc"
        fixed_now = datetime(2026, 1, 4, tzinfo=timezone.utc)
        for declared_version in (None, 2, 3):
            with self.subTest(version=declared_version):
                version = 2 if declared_version is None else declared_version
                data = self.ledger(version)
                if declared_version is None:
                    del data["version"]
                data["total"]["combos"][self.combo_key]["usage_reasoning"] = 42
                january = self.bucket(version)
                january["requests"], january["ok"], january["fail"] = 3, 2, 1
                january["combos"][self.combo_key]["usage_reasoning"] = 3
                january["combos"][ordinary] = {
                    "requests": 0, "ok": 0, "fail": 0, "usage_in": 0, "usage_out": 0,
                }
                january["combos"][untouched] = {
                    "requests": 1, "ok": 1, "fail": 0, "usage_in": 5,
                    "usage_out": 6, "usage_reasoning": 77,
                }
                first, second, december = (self.bucket(version) for _ in range(3))
                first["combos"][self.combo_key]["usage_reasoning"] = 4
                second["combos"][self.combo_key]["usage_reasoning"] = 5
                december["combos"][self.combo_key]["usage_reasoning"] = 2
                for day in (first, second):
                    day["requests"], day["ok"], day["fail"] = 3, 2, 1
                    day["combos"][ordinary] = {
                        "requests": 1, "ok": 1, "fail": 0, "usage_in": 4, "usage_out": 5,
                    }
                december["combos"][zero_reasoning] = {
                    "requests": 0, "ok": 0, "fail": 0, "usage_in": 0,
                    "usage_out": 0, "usage_reasoning": 0,
                }
                data["days"] = {
                    "2025-12-31": december,
                    "2026-01-01": first,
                    "2026-01-02": second,
                }
                data["months_archive"] = {"2026-01": january}
                self.path.write_text(json.dumps(data), encoding="utf-8")
                original = self.path.read_bytes()
                with patch.object(server_module, "KEEP_DAYS", 1), \
                     patch.object(server_module, "_cst_now", return_value=fixed_now):
                    store = _business_store(self.path)
                    self.assertEqual(self.path.read_bytes(), original)
                    store.record(_acc(), 7)
                    self.assertEqual(store.persistence_state, "CLEAN")
                    self.assertEqual(json.loads(self.path.read_text()), store._data)
                actual = json.loads(self.path.read_text())
                january_after = actual["months_archive"]["2026-01"]
                december_after = actual["months_archive"]["2025-12"]
                self.assertEqual(january_after["combos"][self.combo_key]["usage_reasoning"], 12)
                self.assertEqual(december_after["combos"][self.combo_key]["usage_reasoning"], 2)
                self.assertEqual(december_after["combos"][zero_reasoning]["usage_reasoning"], 0)
                self.assertEqual(january_after["combos"][untouched]["usage_reasoning"], 77)
                self.assertNotIn("usage_reasoning", january_after["combos"][ordinary])
                self.assertNotIn(
                    "usage_reasoning", actual["days"]["2026-01-04"]["combos"][self.combo_key])
                self.assertEqual(actual["total"]["combos"][self.combo_key]["usage_reasoning"], 42)
                self.assertEqual(january_after["requests"], 9)
                self.assertEqual(january_after["sum_ms"], 90)
                self.assertEqual(january_after["combos"][self.combo_key]["usage_in"], 30)
                validate_migration_ledger_snapshot(
                    platform_tmp.read_stable(self.path, reject_duplicate_keys=True))
                self.path.unlink()

    def test_valid_v2_and_v3_continue_recording_after_verified_load(self):
        for version in (2, 3):
            with self.subTest(version=version):
                data = self.ledger(version)
                self.path.write_text(json.dumps(data), encoding="utf-8")
                before = self.path.read_bytes()
                snapshot = platform_tmp.read_stable(self.path, reject_duplicate_keys=True)
                store = _business_store(self.path, mode="snapshot", snapshot=snapshot)
                self.assertEqual(self.path.read_bytes(), before)
                store.record(_acc(), 7)
                self.assertEqual(store.persistence_state, "CLEAN")
                self.assertEqual(json.loads(self.path.read_text()), store._data)
                actual = json.loads(self.path.read_text())
                self.assertEqual(actual["version"], 3)
                self.assertEqual(actual["total"]["requests"], 3)
                self.assertEqual(actual["days"][self.today]["requests"], 3)
                self.assertEqual(actual["total"]["combos"][self.combo_key]["usage_in"], 20)
                self.assertEqual(snapshot.config, data)
                validate_migration_ledger_snapshot(platform_tmp.read_stable(self.path))
                self.path.unlink()

    def test_valid_v2_continues_archive_and_records_without_losing_counters(self):
        import core.server as server_module
        data = self.ledger(2)
        data["days"] = {"2026-01-01": self.bucket(2), "2026-01-02": self.bucket(2)}
        self.path.write_text(json.dumps(data), encoding="utf-8")
        before = self.path.read_bytes()
        snapshot = platform_tmp.read_stable(self.path, reject_duplicate_keys=True)
        store = _business_store(self.path, mode="snapshot", snapshot=snapshot)
        self.assertEqual(self.path.read_bytes(), before)
        with patch.object(server_module, "KEEP_DAYS", 1):
            store.record(_acc(), 7)
            self.assertEqual(store.persistence_state, "CLEAN")
            self.assertEqual(json.loads(self.path.read_text()), store._data)
        actual = json.loads(self.path.read_text())
        self.assertEqual(actual["total"]["requests"], 3)
        self.assertEqual(actual["months_archive"]["2026-01"]["requests"], 6)
        self.assertEqual(actual["months_archive"]["2026-01"]["combos"][self.combo_key]["usage_in"], 30)
        self.assertEqual(snapshot.config, data)
        validate_migration_ledger_snapshot(platform_tmp.read_stable(self.path))


if __name__ == "__main__":
    unittest.main()
