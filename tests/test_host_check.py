"""Host 头校验单测（listen.host 非 loopback 时启用的 DNS rebinding 防护）。

覆盖契约（docs/designs/2026-08-26-本地三服务开放家庭局域网-v2.md §2.1）：
- loopback Host（localhost/127.0.0.1/[::1]，含带端口形式）恒放行；
- 缺失 Host / 重复 Host 拒绝（不取第一个）；
- DNS hostname ASCII 大小写不敏感（比较前统一小写）；
- 端口必须存在且与监听端口一致（host:* 不支持）；
- IPv4 / 括号 IPv6 结构化解析（禁 split(":")）；
- 拒绝 userinfo / 空 hostname / 非法端口 / 尾随点；
- 白名单命中 / 未命中；handler 层守卫置于所有路由之前（含控制接口）。

运行：cd tools/model_proxy && python3 -m unittest tests.test_host_check -v
"""

import io
import os
import sys
import unittest
from http.client import parse_headers
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.server import (  # noqa: E402
    HostHeaderValidator,
    ModelProxyHandler,
    check_request_host,
    is_host_allowed,
    parse_host_authority,
)

ALLOW = ["mba-jhhgh954yq-2000.local:18889", "192.168.1.5:18889",
         "[fe80::1]:18889", "portless.local"]


def allowed(host_value: str, listen_port: int = 18889,
            allow_hosts: list = ALLOW) -> bool:
    return is_host_allowed(host_value, listen_port=listen_port,
                           allow_hosts=allow_hosts)


class TestParseHostAuthority(unittest.TestCase):
    """结构化解析（hostname 小写 + 可选端口）。"""

    def test_dns(self):
        self.assertEqual(parse_host_authority("Mba-Local:18889"),
                         ("mba-local", 18889))
        self.assertEqual(parse_host_authority("mba.local"), ("mba.local", None))

    def test_ipv4(self):
        self.assertEqual(parse_host_authority("192.168.1.5:18889"),
                         ("192.168.1.5", 18889))

    def test_ipv6_bracketed(self):
        self.assertEqual(parse_host_authority("[::1]:18889"), ("::1", 18889))
        self.assertEqual(parse_host_authority("[FE80::1]:18889"),
                         ("fe80::1", 18889))
        self.assertEqual(parse_host_authority("[::1]"), ("::1", None))

    def test_malformed_rejected(self):
        bad = [
            "",                      # 空
            ":18889",                # 空 hostname
            "[]:18889",              # 空 IPv6 字面量
            "user@host:18889",       # userinfo
            "host:abc",              # 端口非数字
            "host:0",                # 端口越界（下）
            "host:65536",            # 端口越界（上）
            "host:18889x",           # 端口带尾巴
            "::1:18889",             # 未加括号的 IPv6
            "[::1",                  # 括号未闭合
            "1.2.3.4]:18889",        # 括号错位
            "[foo]:18889",           # 非 IPv6 字面量
            "mba.local.",            # 尾随点
            "mba.local.:18889",      # 尾随点（带端口）
            "host/path:18889",       # 路径字符
            "host:18889:1",          # 多冒号
            "host:",                 # 冒号后无端口
        ]
        for value in bad:
            with self.assertRaises(ValueError, msg=repr(value)):
                parse_host_authority(value)


class TestIsHostAllowed(unittest.TestCase):
    """loopback 恒放行 + 白名单匹配语义。"""

    def test_loopback_always_allowed(self):
        # 无需出现在白名单；含带端口形式；端口值不参与 loopback 判定
        for value in ("localhost", "LOCALHOST", "localhost:18889",
                      "127.0.0.1", "127.0.0.1:18889", "[::1]", "[::1]:18889",
                      "[::1]:9999"):
            self.assertTrue(allowed(value), value)
        # 空白名单 + 绑非 loopback 场景下 loopback 仍放行（loopback 不进配置）
        self.assertTrue(allowed("localhost:18889", allow_hosts=[]))

    def test_case_insensitive_dns(self):
        # 大写变体应通过（比较前两侧统一小写）
        self.assertTrue(allowed("MBA-JHHGH954YQ-2000.LOCAL:18889"))
        self.assertTrue(allowed("Mba-Jhhgh954yq-2000.Local:18889"))

    def test_whitelist_hit(self):
        self.assertTrue(allowed("mba-jhhgh954yq-2000.local:18889"))
        self.assertTrue(allowed("192.168.1.5:18889"))
        self.assertTrue(allowed("[fe80::1]:18889"))
        self.assertTrue(allowed("[FE80::1]:18889"))

    def test_whitelist_miss(self):
        self.assertFalse(allowed("evil.example:18889"))
        self.assertFalse(allowed("mba-jhhgh954yq-2000.local:18889",
                                 allow_hosts=["other.local:18889"]))

    def test_portless_entry_matches_any_port(self):
        # 条目不带端口 → 该主机任意端口放行（请求端口仍须等于监听端口）
        self.assertTrue(allowed("portless.local:18889"))
        self.assertTrue(allowed("portless.local:18889", listen_port=18889))

    def test_port_must_exist_and_match_listen(self):
        # 端口缺失（非 loopback）→ 拒绝
        self.assertFalse(allowed("mba-jhhgh954yq-2000.local"))
        # 端口与监听不一致 → 拒绝（即使主机名在白名单）
        self.assertFalse(allowed("mba-jhhgh954yq-2000.local:9999"))
        # 监听端口换 30000 后，18889 端口的 Host 拒绝
        self.assertFalse(allowed("mba-jhhgh954yq-2000.local:18889",
                                 listen_port=30000,
                                 allow_hosts=["mba-jhhgh954yq-2000.local:30000"]))
        self.assertTrue(allowed("mba-jhhgh954yq-2000.local:30000",
                                listen_port=30000,
                                allow_hosts=["mba-jhhgh954yq-2000.local:30000"]))

    def test_entry_port_must_be_exact(self):
        # 条目端口 9999 ≠ 监听端口 → 永不匹配
        self.assertFalse(allowed("mba.local:18889",
                                 allow_hosts=["mba.local:9999"]))

    def test_trailing_dot_rejected(self):
        self.assertFalse(allowed("mba-jhhgh954yq-2000.local.:18889"))
        # 尾随点条目自身也无法匹配（条目解析失败即跳过）
        self.assertFalse(allowed("mba.local:18889",
                                 allow_hosts=["mba.local.:18889"]))

    def test_malformed_authority_not_allowed(self):
        for value in ("user@host:18889", ":18889", "host:abc", "::1:18889",
                      "[::1:18889", "host:65536"):
            self.assertFalse(allowed(value), value)

    def test_empty_whitelist_rejects_non_loopback(self):
        # 绑非 loopback 且 allow_hosts 空：启动已被 listen_config 拦截，
        # 但校验函数本身语义是"仅 loopback 放行"
        self.assertFalse(allowed("mba.local:18889", allow_hosts=[]))


class TestCheckRequestHost(unittest.TestCase):
    """缺失 / 重复 Host 头语义。"""

    def test_missing_host_rejected(self):
        self.assertFalse(check_request_host([], listen_port=18889,
                                            allow_hosts=ALLOW))

    def test_duplicate_host_rejected_not_first(self):
        # 重复 Host 拒绝，不得只取第一个（第一个恰好合法也不行）
        values = ["mba-jhhgh954yq-2000.local:18889",
                  "evil.example:18889"]
        self.assertFalse(check_request_host(values, listen_port=18889,
                                            allow_hosts=ALLOW))
        values = ["localhost:18889", "evil.example:18889"]
        self.assertFalse(check_request_host(values, listen_port=18889,
                                            allow_hosts=ALLOW))

    def test_single_value_passes_through(self):
        self.assertTrue(check_request_host(["mba-jhhgh954yq-2000.local:18889"],
                                           listen_port=18889, allow_hosts=ALLOW))
        self.assertFalse(check_request_host(["evil.example:18889"],
                                            listen_port=18889,
                                            allow_hosts=ALLOW))


class TestHostHeaderValidator(unittest.TestCase):
    def test_validator_wraps_check(self):
        v = HostHeaderValidator(18889, ALLOW)
        self.assertTrue(v.check(["localhost:18889"]))
        self.assertFalse(v.check(["evil.example:18889"]))
        self.assertFalse(v.check([]))
        self.assertFalse(v.check(["a:18889", "b:18889"]))
        # allow_hosts 拷贝隔离：外部改 list 不影响 validator
        src = ["mba.local:18889"]
        v2 = HostHeaderValidator(18889, src)
        src.append("evil.example:18889")
        self.assertFalse(v2.check(["evil.example:18889"]))


class _GuardHandler(ModelProxyHandler):
    """脱离 socket 的最小 handler 桩：只实现 _host_check 用到的响应原语。"""

    def __init__(self, raw_headers: bytes, path: str = "/"):
        self.wfile = io.BytesIO()
        self.headers_sent = []
        self.headers = parse_headers(io.BytesIO(raw_headers))
        self.path = path
        self.dispatched = []

    def send_response(self, status):
        self.headers_sent.append(("status", status))

    def send_header(self, key, value):
        self.headers_sent.append((key, value))

    def end_headers(self):
        pass

    def _dispatch_control(self, method):
        self.dispatched.append(("control", method))

    def _forward_logged(self, method):
        self.dispatched.append(("forward", method))


def _server_with(validator):
    return SimpleNamespace(host_validator=validator)


class TestHandlerGuard(unittest.TestCase):
    """守卫位于所有路由之前（含控制接口），do_GET/do_POST 全覆盖。"""

    def _handler(self, raw_headers: bytes, validator, path="/v1/messages"):
        h = _GuardHandler(raw_headers, path)
        h.server = _server_with(validator)
        return h

    def test_validator_none_passes(self):
        # 绑 loopback 时未装配 validator，行为与旧版一致
        h = self._handler(b"Host: anything.example\r\n\r\n", None)
        h.do_POST()
        self.assertEqual(h.dispatched, [("forward", "POST")])

    def test_rejected_before_control_and_forward(self):
        v = HostHeaderValidator(18889, ALLOW)
        for path in ("/model_proxy/status", "/v1/messages"):
            h = self._handler(b"Host: evil.example:18889\r\n\r\n", v, path)
            h.do_GET()
            self.assertEqual(h.dispatched, [], path)
            self.assertIn(("status", 403), h.headers_sent, path)
            h2 = self._handler(b"Host: evil.example:18889\r\n\r\n", v, path)
            h2.do_POST()
            self.assertEqual(h2.dispatched, [], path)
            self.assertIn(("status", 403), h2.headers_sent, path)

    def test_allowed_reaches_dispatch(self):
        v = HostHeaderValidator(18889, ALLOW)
        h = self._handler(b"Host: mba-jhhgh954yq-2000.local:18889\r\n\r\n",
                          v, "/model_proxy/status")
        h.do_GET()
        self.assertEqual(h.dispatched, [("control", "GET")])

    def test_loopback_host_passes_guard(self):
        v = HostHeaderValidator(18889, ALLOW)
        h = self._handler(b"Host: 127.0.0.1:18889\r\n\r\n",
                          v, "/model_proxy/status")
        h.do_GET()
        self.assertEqual(h.dispatched, [("control", "GET")])

    def test_missing_host_header_rejected(self):
        v = HostHeaderValidator(18889, ALLOW)
        h = self._handler(b"\r\n", v, "/model_proxy/status")
        h.do_GET()
        self.assertEqual(h.dispatched, [])
        self.assertIn(("status", 403), h.headers_sent)

    def test_duplicate_host_header_rejected(self):
        v = HostHeaderValidator(18889, ALLOW)
        h = self._handler(b"Host: localhost:18889\r\n"
                          b"Host: evil.example:18889\r\n\r\n",
                          v, "/model_proxy/status")
        h.do_GET()
        self.assertEqual(h.dispatched, [])
        self.assertIn(("status", 403), h.headers_sent)

    def test_forward_methods_guarded(self):
        # PUT/DELETE/PATCH 转发路径同样先过守卫
        v = HostHeaderValidator(18889, ALLOW)
        for method in ("do_PUT", "do_DELETE", "do_PATCH"):
            h = self._handler(b"Host: evil.example:18889\r\n\r\n",
                              v, "/v1/messages")
            getattr(h, method)()
            self.assertEqual(h.dispatched, [], method)
            self.assertIn(("status", 403), h.headers_sent, method)


if __name__ == "__main__":
    unittest.main()
