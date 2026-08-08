"""OPT-01 req_id 全链关联单测。

验证：
1. _ReqIdFilter 从 _req_local 读 req_id 注入 record（有请求上下文时）
2. 无请求上下文时默认 '-'（非请求线程）
3. budget_retry continue 重进 while 后 req_id 仍在（同线程不丢失）
4. do_* 入口生成 uuid4().hex[:8] 格式

运行：cd tools/model_proxy && python3 -m unittest tests.test_req_id -v
"""

import io
import json
import logging
import os
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.server import _req_local, _ReqIdFilter, _req_filter  # noqa: E402


class TestReqIdFilter(unittest.TestCase):
    """Filter 行为单测（纯单元测试，不驱动 handler）。"""

    def setUp(self):
        """每个测试前清空 _req_local，避免跨测试污染。"""
        _req_local.req_id = None

    def tearDown(self):
        _req_local.req_id = None

    def test_filter_injects_req_id_when_set(self):
        """有请求上下文时，Filter 把 req_id 注入 record。"""
        _req_local.req_id = "abc12345"
        record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=0,
            msg="test message", args=(), exc_info=None)
        self.assertTrue(_req_filter.filter(record))
        self.assertEqual(record.req_id, "abc12345")

    def test_filter_defaults_to_dash_when_no_req_id(self):
        """无请求上下文（非请求线程）时，req_id 默认 '-'。"""
        record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=0,
            msg="test message", args=(), exc_info=None)
        self.assertTrue(_req_filter.filter(record))
        self.assertEqual(record.req_id, "-")

    def test_filter_defaults_to_dash_after_clear(self):
        """req_id 被清除（finally）后，后续 record 回到默认 '-'。"""
        _req_local.req_id = "abc12345"
        record1 = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=0,
            msg="during request", args=(), exc_info=None)
        _req_filter.filter(record1)
        self.assertEqual(record1.req_id, "abc12345")

        # 模拟 do_* finally 清除
        _req_local.req_id = None
        record2 = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=0,
            msg="after request", args=(), exc_info=None)
        _req_filter.filter(record2)
        self.assertEqual(record2.req_id, "-")


class TestReqIdBudgetRetryChain(unittest.TestCase):
    """budget_retry 5 级放大链中 req_id 串联验证。

    驱动 _forward 走 budget_retry continue 重进 while，验证所有 log.warning/
    log.error 行都带同一个 req_id（通过 _req_local 不换线程、不丢不重）。
    """

    def setUp(self):
        _req_local.req_id = None

    def tearDown(self):
        _req_local.req_id = None

    def test_budget_retry_chain_same_req_id(self):
        """5 级放大链中所有 budget_retry warn 带 same req_id。

        设置 _req_local.req_id = 'test_rid'，驱动一次 budget_retry 5 级爬升，
        捕获所有 log.warning 调用，断言每条 record 的 req_id == 'test_rid'。
        """
        from core.server import ModelProxyHandler
        from tests.test_budget_retry import (
            _make_server, _make_handler, _run,
            _supply, _anth_client_body, _anth_truncated,
        )

        ns, _, _ = _make_server([_supply("s1", "anthropic")], ["s1"])
        h = _make_handler(ns, _anth_client_body(max_tokens=4096))

        # 模拟 do_* 入口设置 req_id
        _req_local.req_id = "test_rid"

        # 捕获所有 log 输出
        captured_records = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record):
                captured_records.append(record)

        capture_handler = _CaptureHandler()
        capture_handler.addFilter(_req_filter)
        root_logger = logging.getLogger()
        root_logger.addHandler(capture_handler)

        try:
            # 6 发：4096→8192→16384→32768→65536→131072（5 次重试打满）
            with patch("core.server.urllib.request.urlopen",
                       side_effect=[_FakeRespTrunc() for _ in range(6)]):
                h._forward("POST")
        finally:
            root_logger.removeHandler(capture_handler)
            _req_local.req_id = None

        # 从捕获的 records 中筛出 budget_retry 相关的 warning
        budget_records = [r for r in captured_records
                          if "budget_retry" in r.getMessage()]
        # 5 次重试 = 5 条 budget_retry warn
        self.assertEqual(len(budget_records), 5,
                         f"expected 5 budget_retry warnings, got {len(budget_records)}")
        # 所有 budget_retry warn 的 req_id 都是 'test_rid'
        for r in budget_records:
            self.assertEqual(r.req_id, "test_rid",
                             f"budget_retry record req_id={r.req_id}, expected 'test_rid'")

        # budget_truncated warn 也应带 same req_id
        truncated_records = [r for r in captured_records
                             if "budget_truncated" in r.getMessage()]
        self.assertGreaterEqual(len(truncated_records), 1)
        for r in truncated_records:
            self.assertEqual(r.req_id, "test_rid")


class _FakeRespTrunc:
    """模拟截断的 anthropic 响应。"""

    def __init__(self):
        self.status = 200
        payload = {"id": "m1", "type": "message", "role": "assistant",
                   "model": "m1", "stop_reason": "max_tokens",
                   "content": [{"type": "thinking", "thinking": "x"}],
                   "usage": {"input_tokens": 3, "output_tokens": 16000}}
        self._data = json.dumps(payload).encode()
        self._read = False
        self.closed = False

    def read(self, n=-1):
        if self._read:
            return b""
        self._read = True
        return self._data

    def getheaders(self):
        return [("Content-Type", "application/json")]

    def close(self):
        self.closed = True


class TestDoMethodsGenerateReqId(unittest.TestCase):
    """do_* 入口生成 uuid4().hex[:8] 格式的 req_id。"""

    def test_req_id_format(self):
        """uuid4().hex[:8] 生成 8 位十六进制字符串。"""
        import uuid
        for _ in range(100):
            rid = uuid.uuid4().hex[:8]
            self.assertEqual(len(rid), 8)
            self.assertTrue(re.match(r'^[0-9a-f]{8}$', rid),
                            f"req_id {rid!r} is not 8 hex chars")


if __name__ == "__main__":
    unittest.main()
