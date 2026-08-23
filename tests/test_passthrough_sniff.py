"""PASSTHROUGH 流式 usage 旁路嗅探单测（脱网络，纯标准库 unittest）。

覆盖设计文档 §7（2026-07-22-access-log-and-latency.md）的核心正确性风险点：
- 字节预筛跳过无关块（不触发 json.loads）
- anthropic / responses 两种 source 分支的 usage 提取
- usage 事件关键字被拆到两个 chunk 之间（跨 chunk 边界）时不丢不误判
- 转发字节本身与嗅探开启前完全一致（透传不受影响）
- 上游未以 \n\n 收尾时，残余块兜底补一次嗅探

运行：cd tools/model_proxy && python3 -m unittest tests.test_passthrough_sniff -v
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.server import ModelProxyHandler  # noqa: E402


class _FakeUpstreamResp:
    """模拟 http.client 的响应对象：按预设 chunk 列表逐次返回，read(n) 忽略 n。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def read(self, n):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self):
        self.closed = True


def _make_handler():
    h = ModelProxyHandler.__new__(ModelProxyHandler)
    h.wfile = io.BytesIO()
    h._acc = {"status": 0, "usage_in": 0, "usage_out": 0}
    h.send_response = lambda status: None
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    return h


def _decode_chunked(raw: bytes) -> bytes:
    """把 _write_streaming_response 写出的 chunked 编码字节还原为原始 body，用于比对透传完整性。"""
    body = b""
    buf = raw
    while True:
        i = buf.find(b"\r\n")
        size = int(buf[:i], 16)
        if size == 0:
            break
        body += buf[i + 2:i + 2 + size]
        buf = buf[i + 2 + size + 2:]
    return body


class TestSniffPassthroughUsageUnit(unittest.TestCase):
    """直接测 _sniff_passthrough_usage 本身（不经过流转发）。"""

    def test_anthropic_message_delta_extracts_usage(self):
        h = _make_handler()
        block = b'event:message_delta\ndata:{"type":"message_delta","delta":{},"usage":{"input_tokens":16,"output_tokens":50}}'
        h._sniff_passthrough_usage(block, "anthropic")
        self.assertEqual(h._acc["usage_in"], 16)
        self.assertEqual(h._acc["usage_out"], 50)

    def test_anthropic_non_target_block_prefiltered_noop(self):
        h = _make_handler()
        block = b'event:content_block_delta\ndata:{"type":"content_block_delta","delta":{"text":"hi"}}'
        h._sniff_passthrough_usage(block, "anthropic")
        # 未命中字节预筛，_acc 应保持默认值不变
        self.assertEqual(h._acc["usage_in"], 0)
        self.assertEqual(h._acc["usage_out"], 0)

    def test_responses_completed_extracts_usage_and_reasoning(self):
        h = _make_handler()
        block = (b'data: {"type":"response.completed","response":{"usage":'
                 b'{"input_tokens":17,"output_tokens":41,'
                 b'"output_tokens_details":{"reasoning_tokens":33}}}}')
        h._sniff_passthrough_usage(block, "responses")
        self.assertEqual(h._acc["usage_in"], 17)
        self.assertEqual(h._acc["usage_out"], 41)

    def test_responses_non_target_block_prefiltered_noop(self):
        h = _make_handler()
        block = b'data: {"type":"response.output_text.delta","delta":"hi"}'
        h._sniff_passthrough_usage(block, "responses")
        self.assertEqual(h._acc["usage_in"], 0)
        self.assertEqual(h._acc["usage_out"], 0)

    def test_missing_fields_default_to_zero_not_none(self):
        h = _make_handler()
        block = b'data: {"type":"response.completed","response":{"usage":{}}}'
        h._sniff_passthrough_usage(block, "responses")
        self.assertEqual(h._acc["usage_in"], 0)
        self.assertEqual(h._acc["usage_out"], 0)

    def test_malformed_json_does_not_raise(self):
        h = _make_handler()
        block = b'event:message_delta\ndata:{not valid json'
        # 不应抛异常（_parse_anthropic_sse_block 内部已 catch，返回 None,None）
        h._sniff_passthrough_usage(block, "anthropic")
        self.assertEqual(h._acc["usage_in"], 0)


class TestWriteStreamingResponseCrossChunk(unittest.TestCase):
    """端到端测 _write_streaming_response：转发字节完整性 + 跨 chunk 边界嗅探正确性。"""

    def test_anthropic_marker_split_across_chunk_boundary(self):
        sse = (b'event:message_start\ndata:{"type":"message_start"}\n\n'
               b'event:message_delta\ndata:{"type":"message_delta","delta":{"stop_reason":"end_turn"},'
               b'"usage":{"input_tokens":16,"output_tokens":50}}\n\n'
               b'event:message_stop\ndata:{"type":"message_stop"}\n\n')
        idx = sse.find(b"message_delta")
        split_point = idx + 5  # 把 "message_delta" 关键字本身切在两个 chunk 之间
        h = _make_handler()
        resp = _FakeUpstreamResp([sse[:split_point], sse[split_point:]])
        h._write_streaming_response(200, [], resp, "anthropic")
        self.assertEqual(h._acc["usage_in"], 16)
        self.assertEqual(h._acc["usage_out"], 50)
        self.assertEqual(_decode_chunked(h.wfile.getvalue()), sse)
        self.assertTrue(resp.closed)

    def test_responses_marker_split_across_chunk_boundary(self):
        sse = (b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
               b'data: {"type":"response.completed","response":{"usage":'
               b'{"input_tokens":17,"output_tokens":41,'
               b'"output_tokens_details":{"reasoning_tokens":33}}}}\n\n')
        idx = sse.find(b"response.completed")
        split_point = idx + 6
        h = _make_handler()
        resp = _FakeUpstreamResp([sse[:split_point], sse[split_point:]])
        h._write_streaming_response(200, [], resp, "responses")
        self.assertEqual(h._acc["usage_in"], 17)
        self.assertEqual(h._acc["usage_out"], 41)
        self.assertEqual(_decode_chunked(h.wfile.getvalue()), sse)

    def test_split_exactly_inside_double_newline_separator(self):
        sse = (b'event:message_delta\ndata:{"type":"message_delta",'
               b'"usage":{"input_tokens":5,"output_tokens":9}}\n\n')
        split_point = sse.find(b"\n\n") + 1  # 切在两个 \n 中间
        h = _make_handler()
        resp = _FakeUpstreamResp([sse[:split_point], sse[split_point:]])
        h._write_streaming_response(200, [], resp, "anthropic")
        self.assertEqual(h._acc["usage_in"], 5)
        self.assertEqual(h._acc["usage_out"], 9)
        wire = _decode_chunked(h.wfile.getvalue())
        self.assertTrue(wire.startswith(sse))
        self.assertIn(b"event: error", wire)
        self.assertEqual(h._acc["stream_integrity"], "invalid")

    def test_residual_block_without_trailing_double_newline_still_sniffed(self):
        """上游最后一个事件块未以 \n\n 收尾，靠 finally 里的残余块补检兜底。"""
        sse = (b'event:message_delta\ndata:{"type":"message_delta",'
               b'"usage":{"input_tokens":3,"output_tokens":4}}')  # 无结尾空行
        h = _make_handler()
        resp = _FakeUpstreamResp([sse])
        h._write_streaming_response(200, [], resp, "anthropic")
        self.assertEqual(h._acc["usage_in"], 3)
        self.assertEqual(h._acc["usage_out"], 4)
        wire = _decode_chunked(h.wfile.getvalue())
        self.assertTrue(wire.startswith(sse))
        self.assertIn(b"event: error", wire)
        self.assertEqual(h._acc["terminal_reason"], "unexpected_eof")

    def test_no_source_keeps_usage_zero_and_forwarding_unaffected(self):
        """source="" （非 PASSTHROUGH 场景理论上不会传 source，但防御性验证不崩不误判）。"""
        sse = b'event:message_delta\ndata:{"type":"message_delta","usage":{"input_tokens":1,"output_tokens":2}}\n\n'
        h = _make_handler()
        resp = _FakeUpstreamResp([sse])
        h._write_streaming_response(200, [], resp)  # source 用默认值 ""
        self.assertEqual(h._acc["usage_in"], 0)
        self.assertEqual(h._acc["usage_out"], 0)
        self.assertEqual(_decode_chunked(h.wfile.getvalue()), sse)

    def test_multiple_chunks_many_small_reads(self):
        """模拟多个远小于 8192 的 chunk（比单次 8192 更极端的碎片化），usage 仍被正确嗅探。"""
        sse = (b'event:content_block_delta\ndata:{"type":"content_block_delta","delta":{"text":"a"}}\n\n'
               b'event:message_delta\ndata:{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":10,"output_tokens":20}}\n\n'
               b'event:message_stop\ndata:{"type":"message_stop"}\n\n')
        # 拆成很多个 3 字节的小 chunk，制造大量跨边界场景
        chunks = [sse[i:i + 3] for i in range(0, len(sse), 3)]
        h = _make_handler()
        resp = _FakeUpstreamResp(chunks)
        h._write_streaming_response(200, [], resp, "anthropic")
        self.assertEqual(h._acc["usage_in"], 10)
        self.assertEqual(h._acc["usage_out"], 20)
        self.assertEqual(_decode_chunked(h.wfile.getvalue()), sse)


if __name__ == "__main__":
    unittest.main()
