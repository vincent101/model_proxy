"""http_compat 抽取的等价性回归。

C0 要求：抽取前后行为逐字一致（设计记录 §4.1）。
本文件固化抽取当时用对拍脚本验证过的等价性，防止后续改动悄悄改变行为。

两类断言：
1. 与 server.py 的兼容导出同源（确保 legacy 调用方拿到的仍是同一实现）
2. 已知行为矩阵（合并矩阵/idempotency/header 优先级）
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import server as S
from core.providers import http_compat as H


class TestDelegation(unittest.TestCase):
    """server.py 的同名导出必须委托到 http_compat（单一真相源）。"""

    def test_forbidden_keys_same_object(self):
        self.assertIs(S._EXTRA_HEADER_FORBIDDEN_KEYS, H.FORBIDDEN_EXTRA_HEADERS)

    def test_inject_delegates(self):
        a, b = {"system": "x"}, {"system": "x"}
        self.assertEqual(S.inject_system_block(a, "INJ"),
                         H.inject_system_block(b, "INJ"))
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_resolve_appkey_delegates(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "c.yaml")
        with open(p, "w") as f:
            f.write("AUTHORIZATION: t1\n")
        supply = {"id": "s", "appkey_file": p}
        self.assertEqual(S.resolve_appkey(supply), H.resolve_appkey(supply))


class TestInjectSystem(unittest.TestCase):
    def test_none_becomes_list(self):
        b = {}
        self.assertTrue(H.inject_system_block(b, "T"))
        self.assertEqual(b["system"], [{"type": "text", "text": "T"}])

    def test_empty_list_becomes_list(self):
        b = {"system": []}
        H.inject_system_block(b, "T")
        self.assertEqual(len(b["system"]), 1)

    def test_string_wrapped(self):
        b = {"system": "orig"}
        H.inject_system_block(b, "T")
        self.assertEqual(b["system"], [{"type": "text", "text": "T"},
                                       {"type": "text", "text": "orig"}])

    def test_idempotent_when_first_matches(self):
        b = {"system": [{"type": "text", "text": "T"}]}
        self.assertFalse(H.inject_system_block(b, "T"))
        self.assertEqual(len(b["system"]), 1)

    def test_prepend_when_different(self):
        b = {"system": [{"type": "text", "text": "other"}]}
        self.assertTrue(H.inject_system_block(b, "T"))
        self.assertEqual(b["system"][0]["text"], "T")
        self.assertEqual(len(b["system"]), 2)

    def test_malformed_first_block_prepends(self):
        b = {"system": ["notadict"]}
        H.inject_system_block(b, "T")
        self.assertEqual(len(b["system"]), 2)

    def test_unknown_shape_untouched(self):
        b = {"system": 42}
        self.assertFalse(H.inject_system_block(b, "T"))
        self.assertEqual(b["system"], 42)

    def test_repeated_calls_are_stable(self):
        """budget_retry/failover 复用同一 body，重复注入不得叠加。"""
        b = {"system": "orig"}
        for _ in range(3):
            H.inject_system_block(b, "T")
        self.assertEqual(len(b["system"]), 2)


class TestResolveAppkey(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _write(self, name, content):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(content)
        return p

    def test_static_appkey_wins(self):
        self.assertEqual(H.resolve_appkey({"appkey": "k"}), "k")

    def test_file_read_each_time(self):
        p = self._write("a.yaml", "AUTHORIZATION: t1\n")
        supply = {"id": "s", "appkey_file": p}
        self.assertEqual(H.resolve_appkey(supply), "t1")
        # 模拟 keeper 重铸写回
        self._write("a.yaml", "AUTHORIZATION: t2\n")
        self.assertEqual(H.resolve_appkey(supply), "t2", "必须无缓存")

    def test_missing_file_raises(self):
        with self.assertRaises(ValueError) as cm:
            H.resolve_appkey({"id": "s", "appkey_file": os.path.join(self.tmp, "no.yaml")})
        self.assertIn("s", str(cm.exception))

    def test_empty_value_raises(self):
        p = self._write("e.yaml", "AUTHORIZATION:   \n")
        with self.assertRaises(ValueError):
            H.resolve_appkey({"id": "s", "appkey_file": p})

    def test_no_line_raises(self):
        p = self._write("n.yaml", "FOO: bar\n")
        with self.assertRaises(ValueError):
            H.resolve_appkey({"id": "s", "appkey_file": p})

    def test_both_missing_returns_empty(self):
        """历史行为：缺配置返回空串照发（必填由 validate_supply_ext 把关）。"""
        self.assertEqual(H.resolve_appkey({"id": "s"}), "")


class TestBuildHeaders(unittest.TestCase):
    def test_client_auth_stripped(self):
        out = H.build_standard_headers(
            {"Authorization": "Bearer old", "X-Api-Key": "old", "Host": "h"},
            "NEW", 10, converted=False, extra_headers=None)
        self.assertEqual(out["Authorization"], "Bearer NEW")
        self.assertEqual(out["x-api-key"], "NEW")
        self.assertNotIn("Host", out)

    def test_content_length_from_body(self):
        out = H.build_standard_headers({"Content-Length": "999"}, "K", 42,
                                       converted=False, extra_headers=None)
        self.assertEqual(out["Content-Length"], "42")

    def test_content_type_only_when_converted(self):
        a = H.build_standard_headers({"Content-Type": "text/plain"}, "K", 1,
                                     converted=False, extra_headers=None)
        self.assertEqual(a["Content-Type"], "text/plain")
        b = H.build_standard_headers({"Content-Type": "text/plain"}, "K", 1,
                                     converted=True, extra_headers=None)
        self.assertEqual(b["Content-Type"], "application/json")

    def test_extra_headers_highest_priority(self):
        out = H.build_standard_headers(
            {}, "K", 1, converted=True,
            extra_headers={"Content-Type": "custom/x", "X-Working-Dir": "/"})
        self.assertEqual(out["Content-Type"], "custom/x")
        self.assertEqual(out["X-Working-Dir"], "/")

    def test_extra_content_length_ignored(self):
        out = H.build_standard_headers({}, "K", 7, converted=False,
                                       extra_headers={"content-length": "999"})
        self.assertEqual(out["Content-Length"], "7")

    def test_does_not_mutate_input(self):
        client = {"Authorization": "old"}
        H.build_standard_headers(client, "K", 1, converted=False, extra_headers=None)
        self.assertEqual(client, {"Authorization": "old"})


if __name__ == "__main__":
    unittest.main()
