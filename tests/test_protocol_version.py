"""protocol_version = HTTP/1.1 及流式断连 finalize 收尾单测。

覆盖设计文档（2026-08-17-codex流式断连修复方案.md）的验证清单：
- 类属性：protocol_version == "HTTP/1.1", timeout == 30
- 非流式响应含 Content-Length 头（HTTP/1.1 不挂起）
- 流式响应含 Transfer-Encoding: chunked 头（HTTP/1.1 标准组合）
- BrokenPipe 时 adapter.finalize 被调（防御性收尾）

运行：cd tools/model_proxy && python3 -m unittest tests.test_protocol_version -v
"""

import io
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.server import ModelProxyHandler  # noqa: E402
from core import translate as pt  # noqa: E402


def _make_handler():
    """构造一个不经过真实 socket 的 handler 实例。"""
    h = ModelProxyHandler.__new__(ModelProxyHandler)
    h.wfile = io.BytesIO()
    h._acc = {"status": 0, "usage_in": 0, "usage_out": 0}
    h.send_response = lambda status: None
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    return h


def _make_handler_with_header_capture():
    """同 _make_handler，但捕获 send_header 调用。"""
    h = _make_handler()
    captured = []
    h.send_response = lambda status: None
    h.send_header = lambda k, v: captured.append((k, v))
    h.end_headers = lambda: None
    return h, captured


class TestClassAttributes(unittest.TestCase):
    def test_protocol_version_is_http11(self):
        self.assertEqual(ModelProxyHandler.protocol_version, "HTTP/1.1")

    def test_timeout_is_30(self):
        self.assertEqual(ModelProxyHandler.timeout, 30)


class TestBufferedResponseHasContentLength(unittest.TestCase):
    """非流式响应必须带 Content-Length，HTTP/1.1 下客户端靠它判定结束。"""

    def test_content_length_header_present(self):
        h, captured = _make_handler_with_header_capture()
        body = b'{"ok": true}'
        h._write_buffered_response(200, [("Content-Type", "application/json")], body)
        cl_headers = [v for k, v in captured if k == "Content-Length"]
        self.assertEqual(len(cl_headers), 1)
        self.assertEqual(cl_headers[0], str(len(body)))


class TestSSEChunkedHasTransferEncoding(unittest.TestCase):
    """流式 SSE 响应必须带 Transfer-Encoding: chunked。"""

    def test_transfer_encoding_chunked_header_present(self):
        h, captured = _make_handler_with_header_capture()
        h._begin_sse_chunked()
        te_headers = [v for k, v in captured if k == "Transfer-Encoding"]
        self.assertEqual(len(te_headers), 1)
        self.assertEqual(te_headers[0], "chunked")


class TestBrokenPipeFinalizeCalled(unittest.TestCase):
    """客户端断连(BrokenPipe)时，adapter.finalize 仍被调用。"""

    def test_responses_stream_finalize_on_broken_pipe(self):
        """_write_responses_stream: wfile.write 抛 BrokenPipeError 后 finalize 被调。"""
        h = _make_handler()
        # wfile.write 在 _write_sse_chunk 第一次写 chunk size 时就抛
        h.wfile = MagicMock()
        h.wfile.write.side_effect = BrokenPipeError("client gone")

        upstream_resp = MagicMock()
        # 返回一段有效的 Anthropic SSE 事件块，让 adapter.feed 产出事件
        upstream_resp.read.side_effect = [
            b'event: message_start\ndata: {"type":"message_start"}\n\n',
            b"",
        ]

        adapter = MagicMock()
        # feed 返回一个事件，触发 _write_sse_chunk → wfile.write → BrokenPipeError
        adapter.feed.return_value = [{"type": "response.created", "response": {"id": "test"}}]
        adapter.finalize.return_value = []

        # 调 _write_responses_stream；BrokenPipe 应在 _write_sse_chunk 内触发
        h._write_responses_stream(upstream_resp, adapter)

        # 断言 finalize 被调
        adapter.finalize.assert_called()

    def test_translated_stream_finalize_on_broken_pipe(self):
        """_write_translated_stream: 同上，用 anthropic_sse_bytes 路径。"""
        h = _make_handler()
        h.wfile = MagicMock()
        h.wfile.write.side_effect = BrokenPipeError("client gone")

        upstream_resp = MagicMock()
        upstream_resp.read.side_effect = [
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            b"",
        ]

        adapter = MagicMock()
        adapter.feed.return_value = [{"type": "content_block_delta", "delta": {"text": "hi"}}]
        adapter.finalize.return_value = []

        h._write_translated_stream(upstream_resp, adapter)

        adapter.finalize.assert_called()

    def test_translated_stream_from_responses_finalize_on_broken_pipe(self):
        """_write_translated_stream_from_responses: 同上。"""
        h = _make_handler()
        h.wfile = MagicMock()
        h.wfile.write.side_effect = BrokenPipeError("client gone")

        upstream_resp = MagicMock()
        upstream_resp.read.side_effect = [
            b'event: message_start\ndata: {"type":"message_start"}\n\n',
            b"",
        ]

        adapter = MagicMock()
        adapter.feed.return_value = [{"type": "content_block_delta", "delta": {"text": "hi"}}]
        adapter.finalize.return_value = []

        h._write_translated_stream_from_responses(upstream_resp, adapter)

        adapter.finalize.assert_called()


if __name__ == "__main__":
    unittest.main()
