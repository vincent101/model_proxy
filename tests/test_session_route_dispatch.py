"""session 级多 route 分配单测（脱网络，纯标准库 unittest）。

覆盖 extract_session_key（从 metadata.user_id 解析 session_id）与
extract_route_candidates（strategy → 候选 route 列表：旧单值 route_id 兼容、
route_pool 一致性哈希、session_overrides 优先、fallback、脏配置容错、
route_id 与 route_pool 互斥非法态的运行时兜底），见设计文档
docs/solutionDesigns/2026-07-28-session-route-dispatch-design.md。

运行：cd tools/model_proxy && python3 -m unittest tests.test_session_route_dispatch
"""

import json
import os
import sys
import unittest

# tests/ 与 core/ 同级，sys.path 指向 tools/model_proxy/ 以便 from core.server import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.server import (  # noqa: E402
    extract_session_key,
    extract_route_candidates,
)


def _routes_map():
    return {
        "claude": {"id": "claude", "tiers": {"opus": ["claude-opus-k0"]}},
        "deepseek": {"id": "deepseek", "tiers": {"opus": ["deepseek-opus-k0"]}},
        "nation": {"id": "nation", "tiers": {"opus": ["nation-opus-k0"]}},
    }


class TestExtractSessionKey(unittest.TestCase):
    """真实格式：metadata.user_id 是一个 JSON 字符串，需二次 json.loads 取 session_id。"""

    def test_real_format_hit(self):
        session_id = "3f2a9c1e-aaaa-bbbb-cccc-000000000000"
        user_id = json.dumps({
            "device_id": "devhash",
            "account_uuid": "",
            "session_id": session_id,
        })
        body = {"metadata": {"user_id": user_id}}
        self.assertEqual(extract_session_key(body), session_id)

    def test_metadata_missing(self):
        self.assertIsNone(extract_session_key({}))

    def test_user_id_missing(self):
        self.assertIsNone(extract_session_key({"metadata": {}}))

    def test_user_id_not_json_string(self):
        body = {"metadata": {"user_id": "not-a-json-string"}}
        self.assertIsNone(extract_session_key(body))

    def test_user_id_json_but_no_session_id_field(self):
        body = {"metadata": {"user_id": json.dumps({"device_id": "x"})}}
        self.assertIsNone(extract_session_key(body))

    def test_body_json_not_dict(self):
        self.assertIsNone(extract_session_key(None))
        self.assertIsNone(extract_session_key("not a dict"))
        self.assertIsNone(extract_session_key([1, 2, 3]))

    def test_user_id_not_a_string_type(self):
        # metadata.user_id 本身若不是字符串（如直接是个 dict），也应安全返回 None
        body = {"metadata": {"user_id": {"session_id": "x"}}}
        self.assertIsNone(extract_session_key(body))

    def test_session_id_empty_string(self):
        user_id = json.dumps({"session_id": ""})
        body = {"metadata": {"user_id": user_id}}
        self.assertIsNone(extract_session_key(body))

    def test_inner_json_not_dict(self):
        # 内层 json.loads 成功但结果不是 dict（比如是个列表）
        user_id = json.dumps([1, 2, 3])
        body = {"metadata": {"user_id": user_id}}
        self.assertIsNone(extract_session_key(body))


class TestExtractRouteCandidatesLegacy(unittest.TestCase):
    """旧写法：只有单值 route_id，无 route_pool。"""

    def test_legacy_single_route(self):
        strategy = {"client_token": "cc", "route_id": "claude"}
        rm = _routes_map()
        candidates = extract_route_candidates(strategy, "any-session", rm)
        self.assertEqual(len(candidates), 1)
        self.assertIs(candidates[0], rm["claude"])

    def test_legacy_dangling_route_id(self):
        strategy = {"client_token": "cc", "route_id": "nope"}
        candidates = extract_route_candidates(strategy, "any-session", _routes_map())
        self.assertEqual(candidates, [])

    def test_strategy_none(self):
        self.assertEqual(extract_route_candidates(None, "any-session", _routes_map()), [])


class TestExtractRouteCandidatesPool(unittest.TestCase):
    """新写法：route_pool + dispatch（session_overrides / 一致性哈希 / fallback）。"""

    def _strategy(self, overrides=None):
        return {
            "client_token": "cc-multi",
            "route_pool": [
                {"route_id": "claude", "weight": 2},
                {"route_id": "deepseek", "weight": 1},
                {"route_id": "nation", "weight": 1},
            ],
            "dispatch": {
                "type": "session_hash",
                "session_key_source": "metadata.user_id",
                "fallback": "on_missing_first",
                "session_overrides": overrides or {},
            },
        }

    def test_session_overrides_hit_outside_pool_allowed(self):
        # override 指向的 route（nation）本就在示例 pool 内，这里改造一个不在 pool 内的
        # override 目标，验证设计文档 §4b："overrides 允许指向不在 route_pool 内的 route"。
        rm = _routes_map()
        strategy = {
            "client_token": "cc-multi",
            "route_pool": [
                {"route_id": "claude", "weight": 2},
                {"route_id": "deepseek", "weight": 1},
            ],
            "dispatch": {
                "session_overrides": {"sess-A": "nation"},  # nation 不在 route_pool 内
            },
        }
        candidates = extract_route_candidates(strategy, "sess-A", rm)
        self.assertTrue(len(candidates) >= 1)
        self.assertIs(candidates[0], rm["nation"])

    def test_session_overrides_miss_falls_back_to_hash(self):
        rm = _routes_map()
        strategy = self._strategy(overrides={"other-session": "nation"})
        candidates = extract_route_candidates(strategy, "sess-B", rm)
        # 未命中 override，应走一致性哈希，候选集合仍应是 pool 内三个 route 的某种排列
        self.assertEqual(len(candidates), 3)
        ids = {r["id"] for r in candidates}
        self.assertEqual(ids, {"claude", "deepseek", "nation"})

    def test_hash_deterministic_same_session_key(self):
        rm = _routes_map()
        strategy = self._strategy()
        c1 = extract_route_candidates(strategy, "fixed-session-key", rm)
        c2 = extract_route_candidates(strategy, "fixed-session-key", rm)
        self.assertEqual([r["id"] for r in c1], [r["id"] for r in c2])

    def test_hash_varies_across_session_keys(self):
        rm = _routes_map()
        strategy = self._strategy()
        keys = [f"session-{i}" for i in range(20)]
        orders = {tuple(r["id"] for r in extract_route_candidates(strategy, k, rm)) for k in keys}
        # 不要求严格均匀分布，只要求不是所有 session_key 都落到同一顺序
        self.assertGreater(len(orders), 1)

    def test_session_key_none_uses_fallback_first_item(self):
        rm = _routes_map()
        strategy = self._strategy()
        candidates = extract_route_candidates(strategy, None, rm)
        self.assertEqual([r["id"] for r in candidates], ["claude", "deepseek", "nation"])

    def test_session_key_empty_string_uses_fallback(self):
        rm = _routes_map()
        strategy = self._strategy()
        candidates = extract_route_candidates(strategy, "", rm)
        self.assertEqual([r["id"] for r in candidates], ["claude", "deepseek", "nation"])

    def test_invalid_route_pool_entry_skipped(self):
        rm = _routes_map()
        strategy = {
            "client_token": "cc-multi",
            "route_pool": [
                {"route_id": "claude", "weight": 1},
                {"route_id": "not-exist", "weight": 1},
            ],
            "dispatch": {},
        }
        candidates = extract_route_candidates(strategy, None, rm)
        ids = [r["id"] for r in candidates]
        self.assertNotIn("not-exist", ids)
        self.assertEqual(ids, ["claude"])

    def test_empty_route_pool_returns_empty(self):
        strategy = {"client_token": "cc-multi", "route_pool": [], "dispatch": {}}
        self.assertEqual(extract_route_candidates(strategy, "sess", _routes_map()), [])

    def test_all_invalid_route_pool_entries_returns_empty(self):
        strategy = {
            "client_token": "cc-multi",
            "route_pool": [{"route_id": "not-a", "weight": 1}, {"route_id": "not-b", "weight": 1}],
            "dispatch": {},
        }
        candidates = extract_route_candidates(strategy, "sess", _routes_map())
        self.assertEqual(candidates, [])


class TestExtractRouteCandidatesMutuallyExclusiveField(unittest.TestCase):
    """本次新修：同时含 route_id 与 route_pool 的非法态，运行时兜底按 route_pool 处理。"""

    def test_both_fields_present_prefers_route_pool_no_crash(self):
        rm = _routes_map()
        strategy = {
            "client_token": "illegal-test",
            "route_id": "claude",
            "route_pool": [{"route_id": "deepseek", "weight": 1}],
            "dispatch": {},
        }
        candidates = extract_route_candidates(strategy, None, rm)
        # 不抛异常，且按 route_pool 处理（忽略 route_id="claude"）
        self.assertEqual([r["id"] for r in candidates], ["deepseek"])

    def test_both_fields_present_logs_warning(self):
        rm = _routes_map()
        strategy = {
            "client_token": "illegal-test",
            "route_id": "claude",
            "route_pool": [{"route_id": "nation", "weight": 1}],
            "dispatch": {},
        }
        with self.assertLogs("core.server", level="WARNING") as cm:
            extract_route_candidates(strategy, None, rm)
        joined = "\n".join(cm.output)
        self.assertIn("illegal-test", joined)
        self.assertIn("route_id", joined)
        self.assertIn("route_pool", joined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
