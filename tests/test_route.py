"""三阶段路由匹配单测（脱网络，纯标准库 unittest）。

覆盖新架构 route(家族模板) + strategies(token 绑定) 分层：
resolve_route（token→strategy→route）、resolve_tier（model 精确查表）、
select_supply_list（按 tier 取 supplies）、select_supply（列表签名 + cooling/tried
跳过）、tier 内 failover、以及端到端串联。

运行：cd tools/model_proxy && python3 -m unittest tests.test_route
"""

import os
import sys
import unittest

# tests/ 与 core/ 同级，sys.path 指向 tools/model_proxy/ 以便 from core.server import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.server import (  # noqa: E402
    resolve_route,
    resolve_tier,
    select_supply_list,
    select_supply,
)


class _FakeCooldown:
    """脱离真实 CooldownStore，只按集合判定是否 cooling。"""

    def __init__(self, cooling=None):
        self._cooling = set(cooling or [])

    def is_cooling(self, sid: str) -> bool:
        return sid in self._cooling


def _routes_map():
    return {
        "claude": {"id": "claude", "tiers": {
            "opus": ["claude-opus-k0"],
            "sonnet": ["claude-sonnet-k0"],
            "haiku": ["claude-haiku-k0"],
        }, "failover": "on"},
        "glm": {"id": "glm", "tiers": {
            "opus": ["glm-opus-k1", "glm-opus-k0"],
        }, "failover": "on"},
    }


class TestResolveRoute(unittest.TestCase):

    def test_hit(self):
        strategies = [{"client_token": "cc", "route_id": "claude"}]
        rm = _routes_map()
        route = resolve_route(strategies, rm, "cc")
        self.assertIs(route, rm["claude"])

    def test_token_missing(self):
        strategies = [{"client_token": "cc", "route_id": "claude"}]
        self.assertIsNone(resolve_route(strategies, _routes_map(), "other"))

    def test_route_id_dangling(self):
        # strategy 指向不存在的 route → None
        strategies = [{"client_token": "cc", "route_id": "nope"}]
        self.assertIsNone(resolve_route(strategies, _routes_map(), "cc"))


class TestResolveTier(unittest.TestCase):

    def test_exact_opus(self):
        self.assertEqual(resolve_tier("claude-opus"), "opus")

    def test_exact_sonnet(self):
        self.assertEqual(resolve_tier("claude-sonnet"), "sonnet")

    def test_exact_haiku(self):
        self.assertEqual(resolve_tier("claude-haiku"), "haiku")

    def test_non_preset_miss(self):
        # 精确查表：含关键字但非精确值必须 miss（不是子串猜测）
        self.assertIsNone(resolve_tier("claude-opus-20240229"))
        self.assertIsNone(resolve_tier("claude-3-opus"))
        self.assertIsNone(resolve_tier("gpt-4"))
        self.assertIsNone(resolve_tier(None))
        self.assertIsNone(resolve_tier(""))


class TestSelectSupplyList(unittest.TestCase):

    def test_hit(self):
        route = _routes_map()["claude"]
        self.assertEqual(select_supply_list(route, "opus"), ["claude-opus-k0"])

    def test_missing_tier(self):
        route = _routes_map()["glm"]  # 只有 opus 档
        self.assertIsNone(select_supply_list(route, "sonnet"))


class TestSelectSupply(unittest.TestCase):

    def test_first_available(self):
        supply_map = {"k0": {"id": "k0"}, "k1": {"id": "k1"}}
        cd = _FakeCooldown()
        supply = select_supply(["k0", "k1"], supply_map, cd, set())
        self.assertEqual(supply["id"], "k0")

    def test_skip_tried(self):
        supply_map = {"k0": {"id": "k0"}, "k1": {"id": "k1"}}
        cd = _FakeCooldown()
        supply = select_supply(["k0", "k1"], supply_map, cd, {"k0"})
        self.assertEqual(supply["id"], "k1")

    def test_skip_missing_in_map(self):
        supply_map = {"k1": {"id": "k1"}}  # k0 缺失
        cd = _FakeCooldown()
        supply = select_supply(["k0", "k1"], supply_map, cd, set())
        self.assertEqual(supply["id"], "k1")

    def test_all_unavailable(self):
        supply_map = {"k0": {"id": "k0"}}
        cd = _FakeCooldown(cooling={"k0"})
        self.assertIsNone(select_supply(["k0"], supply_map, cd, set()))

    def test_failover_skip_cooling(self):
        # tier 内 failover：k1 在 cooling → 返回 k0
        supply_map = {"k0": {"id": "k0"}, "k1": {"id": "k1"}}
        cd = _FakeCooldown(cooling={"k1"})
        supply = select_supply(["k1", "k0"], supply_map, cd, set())
        self.assertEqual(supply["id"], "k0")


class TestEndToEnd(unittest.TestCase):

    def test_full_chain(self):
        strategies = [{"client_token": "cc", "route_id": "claude"}]
        rm = _routes_map()
        supply_map = {
            "claude-opus-k0": {"id": "claude-opus-k0", "target_model": "aws.claude-opus"},
        }
        cd = _FakeCooldown()

        route = resolve_route(strategies, rm, "cc")
        self.assertIsNotNone(route)
        tier = resolve_tier("claude-opus")
        self.assertEqual(tier, "opus")
        supplies_list = select_supply_list(route, tier)
        self.assertEqual(supplies_list, ["claude-opus-k0"])
        supply = select_supply(supplies_list, supply_map, cd, set())
        self.assertEqual(supply["id"], "claude-opus-k0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
