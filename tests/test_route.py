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
    pick_translator,
    PASSTHROUGH,
    ANTHROPIC_TO_CHAT,
    RESPONSES_TO_ANTHROPIC,
    ANTHROPIC_TO_RESPONSES,
    UNSUPPORTED,
    _sanitize_forward_query,
    extract_client_token,
    detect_source,
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


class TestPickTranslator(unittest.TestCase):

    def test_combinations(self):
        self.assertEqual(pick_translator("anthropic", "anthropic"), PASSTHROUGH)
        self.assertEqual(pick_translator("responses", "responses"), PASSTHROUGH)
        self.assertEqual(pick_translator("anthropic", "chat"), ANTHROPIC_TO_CHAT)
        self.assertEqual(pick_translator("responses", "anthropic"), RESPONSES_TO_ANTHROPIC)
        self.assertEqual(pick_translator("anthropic", "responses"), ANTHROPIC_TO_RESPONSES)

    def test_anthropic_to_responses(self):
        self.assertEqual(pick_translator("anthropic", "responses"), ANTHROPIC_TO_RESPONSES)
        self.assertEqual(ANTHROPIC_TO_RESPONSES, "anthropic_to_responses")

    def test_unsupported(self):
        self.assertEqual(pick_translator("chat", "anthropic"), UNSUPPORTED)
        self.assertEqual(pick_translator("responses", "chat"), UNSUPPORTED)


class TestSanitizeForwardQuery(unittest.TestCase):
    """出站 URL 的 query 净化：统一丢弃客户端 path，只保留剔除 beta 后的 query。

    supply["url"] 现在语义是完整终态端点，四个转发分支的 target_url 均由
    base_url + _sanitize_forward_query(path) 统一计算（原
    _build_passthrough_target_url，见 core/server.py 改名说明）。
    """

    def test_anthropic_client_path_dropped(self):
        base_url = "https://xxx/v1/anthropic/v1/messages"
        result = base_url + _sanitize_forward_query("/v1/messages")
        self.assertEqual(result, base_url)
        self.assertNotIn("/v1/messages/v1/messages", result)

    def test_responses_client_path_dropped_regression(self):
        base_url = "https://xxx/v1/responses"
        result = base_url + _sanitize_forward_query("/v1/responses")
        self.assertEqual(result, base_url)

    def test_query_kept_beta_stripped(self):
        base_url = "https://xxx/v1/anthropic/v1/messages"
        result = base_url + _sanitize_forward_query("/v1/messages?beta=xxx&foo=1")
        self.assertEqual(result, base_url + "?foo=1")

    def test_root_path_no_error(self):
        base_url = "https://xxx/v1/anthropic/v1/messages"
        result = base_url + _sanitize_forward_query("/")
        self.assertEqual(result, base_url)


class TestExtractClientToken(unittest.TestCase):
    """入站 client_token 提取：Authorization: Bearer 优先，回退 x-api-key。"""

    def test_only_authorization_bearer(self):
        headers = {"Authorization": "Bearer xxx"}
        self.assertEqual(extract_client_token(headers), "xxx")

    def test_only_x_api_key(self):
        # 本次修复的核心复现场景：客户端只发 x-api-key，无 Authorization
        headers = {"x-api-key": "xxx"}
        self.assertEqual(extract_client_token(headers), "xxx")

    def test_both_same_value(self):
        headers = {"Authorization": "Bearer xxx", "x-api-key": "xxx"}
        self.assertEqual(extract_client_token(headers), "xxx")

    def test_both_different_value_authorization_wins(self):
        headers = {"Authorization": "Bearer a", "x-api-key": "b"}
        self.assertEqual(extract_client_token(headers), "a")

    def test_neither_present(self):
        headers = {}
        self.assertEqual(extract_client_token(headers), "")

    def test_authorization_not_bearer_falls_back_to_x_api_key(self):
        headers = {"Authorization": "Basic xxx", "x-api-key": "cc"}
        self.assertEqual(extract_client_token(headers), "cc")

    def test_bearer_scheme_case_insensitive(self):
        # RFC 6750：Bearer scheme 大小写不敏感
        headers = {"Authorization": "bearer cc"}
        self.assertEqual(extract_client_token(headers), "cc")
        headers = {"Authorization": "BEARER cc"}
        self.assertEqual(extract_client_token(headers), "cc")

    def test_x_api_key_value_stripped(self):
        # 带首尾空白的 x-api-key 取值要 strip，否则查表会找不到 strategy
        headers = {"x-api-key": " cc "}
        self.assertEqual(extract_client_token(headers), "cc")

    def test_bearer_value_stripped(self):
        headers = {"Authorization": "Bearer  cc  "}
        self.assertEqual(extract_client_token(headers), "cc")


class TestDetectSourceCaseInsensitive(unittest.TestCase):
    """detect_source 路径尾缀大小写归一。"""

    def test_v1_messages_uppercase(self):
        self.assertEqual(detect_source("/V1/MESSAGES", None), "anthropic")

    def test_v1_messages_mixed_case(self):
        self.assertEqual(detect_source("/V1/Messages", None), "anthropic")

    def test_v1_responses_mixed_case(self):
        self.assertEqual(detect_source("/V1/Responses", None), "responses")

    def test_chat_completions_mixed_case(self):
        self.assertEqual(detect_source("/Chat/Completions", None), "chat")

    def test_lowercase_regression(self):
        # 回归：现有全小写用例仍通过
        self.assertEqual(detect_source("/v1/messages", None), "anthropic")
        self.assertEqual(detect_source("/v1/responses", None), "responses")
        self.assertEqual(detect_source("/chat/completions", None), "chat")


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
