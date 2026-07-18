"""match_route 路由匹配单测（脱网络，纯标准库 unittest）。

覆盖 client_model 精确匹配语义：精确命中/不命中、通配兜底、有序遍历
（通配排在精确前会抢先命中）、client_token 不匹配、routes 为空。

运行：cd tools/model_proxy && python3 -m unittest tests.test_route
"""

import os
import sys
import unittest

# tests/ 与 core/ 同级，sys.path 指向 tools/model_proxy/ 以便 from core.server import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.server import match_route  # noqa: E402


class TestMatchRoute(unittest.TestCase):

    def test_exact_match_hit(self):
        routes = [
            {"match": {"client_token": "cc", "client_model": "claude-opus"},
             "supplies": ["s1"]},
        ]
        route = match_route(routes, "cc", "claude-opus")
        self.assertIs(route, routes[0])

    def test_exact_match_miss(self):
        routes = [
            {"match": {"client_token": "cc", "client_model": "claude-opus"},
             "supplies": ["s1"]},
        ]
        route = match_route(routes, "cc", "claude-sonnet")
        self.assertIsNone(route)

    def test_wildcard_match(self):
        routes = [
            {"match": {"client_token": "cc"}, "supplies": ["s1"]},
        ]
        route = match_route(routes, "cc", "any-model-name")
        self.assertIs(route, routes[0])

    def test_order_wildcard_before_exact_wins(self):
        routes = [
            {"match": {"client_token": "cc"}, "supplies": ["wildcard"]},
            {"match": {"client_token": "cc", "client_model": "claude-opus"},
             "supplies": ["exact"]},
        ]
        route = match_route(routes, "cc", "claude-opus")
        self.assertIs(route, routes[0])
        self.assertEqual(route["supplies"], ["wildcard"])

    def test_client_token_mismatch(self):
        routes = [
            {"match": {"client_token": "cc", "client_model": "claude-opus"},
             "supplies": ["s1"]},
        ]
        route = match_route(routes, "other", "claude-opus")
        self.assertIsNone(route)

    def test_empty_routes(self):
        route = match_route([], "cc", "claude-opus")
        self.assertIsNone(route)


if __name__ == "__main__":
    unittest.main(verbosity=2)
