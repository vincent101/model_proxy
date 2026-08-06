"""迁移脚本 migrate_session_overrides.py 的单测（脱网络，纯标准库 unittest）。

设计文档：docs/designs/2026-08-06-session-overrides-single-storage.md §5 第1步

覆盖点：
    1. 幂等性：跑两次不重复写、不丢数据
    2. sidecar 已有记录不被旧数据覆盖
    3. dispatch 变空字典后被整体移除
    4. 迁移前自动备份主 config
    5. now_iso 参数化验证时间戳写入
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from migrate_session_overrides import migrate  # noqa: E402


def _make_config(overrides=None, extra_dispatch=False):
    """构造测试用主 config。
    overrides: {session_id: route_id} 旧式纯字符串映射
    extra_dispatch: True 时在 dispatch 里额外塞一个 type 字段，测试 dispatch 非空时只删 session_overrides
    """
    dispatch = {}
    if overrides:
        dispatch["session_overrides"] = dict(overrides)
    if extra_dispatch:
        dispatch["type"] = "hash"
    strategy = {
        "client_token": "cc",
        "route_pool": [{"route_id": "nation", "weight": 1}],
    }
    if dispatch:
        strategy["dispatch"] = dispatch
    return {
        "admin_token": "x",
        "supplies": [],
        "routes": [{"id": "nation", "tiers": {}}],
        "strategies": [strategy],
    }


class TestMigrateIdempotent(unittest.TestCase):
    """幂等性：跑两次不重复写、不丢数据。"""

    def test_run_twice_no_duplication(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "model_proxy_config.json"
            sidecar_path = Path(d) / "session_overrides.json"
            legacy = {f"sess-{i:02d}": "nation" for i in range(5)}
            cfg_path.write_text(json.dumps(_make_config(legacy)), encoding="utf-8")

            # 第一次迁移
            r1 = migrate(cfg_path, sidecar_path, now_iso="2026-08-06T10:00:00Z")
            self.assertEqual(r1["migrated"], 5)
            self.assertEqual(r1["skipped"], 0)

            # 第二次迁移（幂等：主 config 已无该字段，nothing to do）
            r2 = migrate(cfg_path, sidecar_path, now_iso="2026-08-07T10:00:00Z")
            self.assertEqual(r2["migrated"], 0)
            self.assertEqual(r2["skipped"], 0)
            self.assertEqual(r2["strategies_touched"], 0)

            # sidecar 数据未丢失、未重复
            sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))
            self.assertEqual(len(sidecar_data["cc"]), 5)
            for sid in legacy:
                self.assertIn(sid, sidecar_data["cc"])
                # 时间戳应是第一次的，不是第二次的
                self.assertEqual(sidecar_data["cc"][sid]["created"], "2026-08-06T10:00:00Z")

    def test_run_twice_with_reinserted_field(self):
        """如果主 config 被人工重新塞入字段再跑一次，已有 sidecar 记录不被覆盖。"""
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "model_proxy_config.json"
            sidecar_path = Path(d) / "session_overrides.json"
            legacy = {"sess-a": "nation", "sess-b": "nation"}
            cfg_path.write_text(json.dumps(_make_config(legacy)), encoding="utf-8")

            # 第一次迁移
            migrate(cfg_path, sidecar_path, now_iso="2026-08-06T10:00:00Z")

            # 人工重新塞入字段（模拟迁移后又被手工加回主 config 的边界场景）
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["strategies"][0].setdefault("dispatch", {})["session_overrides"] = {
                "sess-a": "nation",  # sidecar 已有
                "sess-c": "nation",  # sidecar 没有
            }
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

            r2 = migrate(cfg_path, sidecar_path, now_iso="2026-08-07T10:00:00Z")
            self.assertEqual(r2["migrated"], 1)   # 只迁了 sess-c
            self.assertEqual(r2["skipped"], 1)    # sess-a 被跳过

            sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))
            # sess-a 时间戳不被覆盖
            self.assertEqual(sidecar_data["cc"]["sess-a"]["created"], "2026-08-06T10:00:00Z")
            # sess-c 是新写入的
            self.assertEqual(sidecar_data["cc"]["sess-c"]["created"], "2026-08-07T10:00:00Z")


class TestMigrateSidecarPriority(unittest.TestCase):
    """sidecar 已有记录不被旧数据覆盖。"""

    def test_existing_sidecar_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "model_proxy_config.json"
            sidecar_path = Path(d) / "session_overrides.json"
            legacy = {"sess-a": "nation", "sess-b": "nation"}
            cfg_path.write_text(json.dumps(_make_config(legacy)), encoding="utf-8")

            # sidecar 已有 sess-a（新式 dict，route_id 不同）
            sidecar_path.write_text(json.dumps({
                "cc": {"sess-a": {"route_id": "claude", "last_seen": "2026-08-05T00:00:00Z",
                                   "created": "2026-08-05T00:00:00Z"}}
            }), encoding="utf-8")

            r = migrate(cfg_path, sidecar_path, now_iso="2026-08-06T10:00:00Z")
            self.assertEqual(r["migrated"], 1)   # 只迁了 sess-b
            self.assertEqual(r["skipped"], 1)    # sess-a 被跳过

            sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))
            # sess-a 保持 sidecar 原值（claude），不被旧数据（nation）覆盖
            self.assertEqual(sidecar_data["cc"]["sess-a"]["route_id"], "claude")
            self.assertEqual(sidecar_data["cc"]["sess-a"]["created"], "2026-08-05T00:00:00Z")
            # sess-b 是新写入的
            self.assertEqual(sidecar_data["cc"]["sess-b"]["route_id"], "nation")
            self.assertEqual(sidecar_data["cc"]["sess-b"]["created"], "2026-08-06T10:00:00Z")


class TestMigrateDispatchCleanup(unittest.TestCase):
    """dispatch 变空字典后被整体移除；非空时只删 session_overrides。"""

    def test_dispatch_removed_when_empty(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "model_proxy_config.json"
            sidecar_path = Path(d) / "session_overrides.json"
            legacy = {"sess-a": "nation"}
            cfg_path.write_text(json.dumps(_make_config(legacy)), encoding="utf-8")

            migrate(cfg_path, sidecar_path, now_iso="2026-08-06T10:00:00Z")

            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            strategy = cfg["strategies"][0]
            self.assertNotIn("dispatch", strategy)

    def test_dispatch_kept_when_other_fields_remain(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "model_proxy_config.json"
            sidecar_path = Path(d) / "session_overrides.json"
            legacy = {"sess-a": "nation"}
            cfg_path.write_text(json.dumps(_make_config(legacy, extra_dispatch=True)),
                                encoding="utf-8")

            migrate(cfg_path, sidecar_path, now_iso="2026-08-06T10:00:00Z")

            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            strategy = cfg["strategies"][0]
            self.assertIn("dispatch", strategy)
            self.assertNotIn("session_overrides", strategy["dispatch"])
            self.assertEqual(strategy["dispatch"]["type"], "hash")

    def test_no_dispatch_no_change(self):
        """strategy 没有 dispatch 时不报错、不改动。"""
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "model_proxy_config.json"
            sidecar_path = Path(d) / "session_overrides.json"
            cfg = _make_config(overrides=None)
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

            r = migrate(cfg_path, sidecar_path, now_iso="2026-08-06T10:00:00Z")
            self.assertEqual(r["migrated"], 0)
            self.assertEqual(r["strategies_touched"], 0)
            self.assertIsNone(r["backup_path"])

            # 主 config 不变
            cfg_after = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertNotIn("dispatch", cfg_after["strategies"][0])


class TestMigrateBackup(unittest.TestCase):
    """迁移前自动备份主 config。"""

    def test_backup_created(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "model_proxy_config.json"
            sidecar_path = Path(d) / "session_overrides.json"
            legacy = {"sess-a": "nation"}
            original_cfg = _make_config(legacy)
            cfg_path.write_text(json.dumps(original_cfg), encoding="utf-8")

            r = migrate(cfg_path, sidecar_path, now_iso="2026-08-06T10:00:00Z")
            self.assertIsNotNone(r["backup_path"])
            backup_path = Path(r["backup_path"])
            self.assertTrue(backup_path.exists())
            self.assertIn(".bak.", backup_path.name)

            # 备份内容 = 迁移前的原始内容
            backup_data = json.loads(backup_path.read_text(encoding="utf-8"))
            self.assertEqual(
                backup_data["strategies"][0]["dispatch"]["session_overrides"],
                legacy)

    def test_no_backup_when_nothing_to_migrate(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "model_proxy_config.json"
            sidecar_path = Path(d) / "session_overrides.json"
            cfg_path.write_text(json.dumps(_make_config(overrides=None)), encoding="utf-8")

            r = migrate(cfg_path, sidecar_path, now_iso="2026-08-06T10:00:00Z")
            self.assertIsNone(r["backup_path"])
            # 没有备份文件产生
            bak_files = list(cfg_path.parent.glob("*.bak.*"))
            self.assertEqual(len(bak_files), 0)


class TestMigrateNowIso(unittest.TestCase):
    """now_iso 参数化验证时间戳写入。"""

    def test_custom_timestamp_written(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "model_proxy_config.json"
            sidecar_path = Path(d) / "session_overrides.json"
            legacy = {"sess-a": "nation"}
            cfg_path.write_text(json.dumps(_make_config(legacy)), encoding="utf-8")

            ts = "2026-01-15T08:30:00Z"
            migrate(cfg_path, sidecar_path, now_iso=ts)

            sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))
            entry = sidecar_data["cc"]["sess-a"]
            self.assertEqual(entry["last_seen"], ts)
            self.assertEqual(entry["created"], ts)

    def test_different_timestamps_on_separate_runs(self):
        """两次迁移用不同 now_iso，验证时间戳确实来自参数而非写死。"""
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "model_proxy_config.json"
            sidecar_path = Path(d) / "session_overrides.json"
            cfg_path.write_text(json.dumps(_make_config({"sess-a": "nation"})), encoding="utf-8")

            migrate(cfg_path, sidecar_path, now_iso="2026-01-01T00:00:00Z")
            sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))
            self.assertEqual(sidecar_data["cc"]["sess-a"]["created"], "2026-01-01T00:00:00Z")

            # 重新塞入新 session
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["strategies"][0].setdefault("dispatch", {})["session_overrides"] = {"sess-b": "nation"}
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

            migrate(cfg_path, sidecar_path, now_iso="2026-06-15T12:00:00Z")
            sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))
            self.assertEqual(sidecar_data["cc"]["sess-a"]["created"], "2026-01-01T00:00:00Z")
            self.assertEqual(sidecar_data["cc"]["sess-b"]["created"], "2026-06-15T12:00:00Z")


class TestMigrateMultipleStrategies(unittest.TestCase):
    """多 strategy 场景：各自独立迁移。"""

    def test_two_strategies_both_migrated(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "model_proxy_config.json"
            sidecar_path = Path(d) / "session_overrides.json"
            cfg = {
                "admin_token": "x",
                "supplies": [],
                "routes": [{"id": "nation", "tiers": {}}, {"id": "claude", "tiers": {}}],
                "strategies": [
                    {
                        "client_token": "cc",
                        "route_pool": [{"route_id": "nation", "weight": 1}],
                        "dispatch": {"session_overrides": {"sess-1": "nation"}},
                    },
                    {
                        "client_token": "codex",
                        "route_pool": [{"route_id": "claude", "weight": 1}],
                        "dispatch": {"session_overrides": {"sess-2": "claude"}},
                    },
                ],
            }
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

            r = migrate(cfg_path, sidecar_path, now_iso="2026-08-06T10:00:00Z")
            self.assertEqual(r["migrated"], 2)
            self.assertEqual(r["strategies_touched"], 2)

            sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))
            self.assertIn("cc", sidecar_data)
            self.assertIn("codex", sidecar_data)
            self.assertEqual(sidecar_data["cc"]["sess-1"]["route_id"], "nation")
            self.assertEqual(sidecar_data["codex"]["sess-2"]["route_id"], "claude")

            # 两个 strategy 的 dispatch 都应被移除
            cfg_after = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertNotIn("dispatch", cfg_after["strategies"][0])
            self.assertNotIn("dispatch", cfg_after["strategies"][1])


if __name__ == "__main__":
    unittest.main()
