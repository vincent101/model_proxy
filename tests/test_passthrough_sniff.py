"""PASSTHROUGH 即时透传、旁路观察与 EOF 善后测试。"""

import io
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import translate as pt  # noqa: E402
from core.server import ModelProxyHandler  # noqa: E402


class _FakeUpstreamResp:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def read(self, _n):
        if not self._chunks:
            return b""
        item = self._chunks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True


def _make_handler(wfile=None):
    h = ModelProxyHandler.__new__(ModelProxyHandler)
    h.wfile = wfile or io.BytesIO()
    h._acc = {"status": 0, "usage_in": 0, "usage_out": 0,
              "response_committed": 0, "stream_integrity": "",
              "terminal_status": "", "terminal_reason": "", "final_error": ""}
    h.send_response = lambda _status: None
    h.send_header = lambda _k, _v: None
    h.end_headers = lambda: None
    return h


def _decode_chunked(raw: bytes) -> bytes:
    body = bytearray()
    pos = 0
    while pos < len(raw):
        end = raw.index(b"\r\n", pos)
        size = int(raw[pos:end], 16)
        pos = end + 2
        if not size:
            break
        body.extend(raw[pos:pos + size])
        pos += size + 2
    return bytes(body)


class TestPassthroughObserver(unittest.TestCase):
    def test_normal_stream_is_byte_exact_and_valid(self):
        wire = (b'event: message_delta\ndata: {"type":"message_delta","delta":'
                b'{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n'
                b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
        h = _make_handler()
        resp = _FakeUpstreamResp([wire[:17], wire[17:]])
        h._write_streaming_response(200, [], resp, "anthropic")
        self.assertEqual(_decode_chunked(h.wfile.getvalue()), wire)
        self.assertEqual(h._acc["stream_integrity"], "valid")
        self.assertEqual(h._acc["usage_out"], 3)
        self.assertIsInstance(h._acc["first_event_ms"], int)
        self.assertTrue(resp.closed)

    def test_empty_stream_appends_protocol_error(self):
        h = _make_handler()
        h._write_streaming_response(200, [], _FakeUpstreamResp([]), "anthropic")
        body = _decode_chunked(h.wfile.getvalue())
        self.assertIn(b"event: error", body)
        self.assertEqual(h._acc["stream_integrity"], "invalid")
        self.assertEqual(h._acc["terminal_reason"], "empty_stream")

    def test_missing_terminal_preserves_prefix_and_appends_error(self):
        prefix = b'event: message_start\ndata: {"type":"message_start","message":{"usage":{}}}\n\n'
        h = _make_handler()
        h._write_streaming_response(200, [], _FakeUpstreamResp([prefix]), "anthropic")
        body = _decode_chunked(h.wfile.getvalue())
        self.assertTrue(body.startswith(prefix))
        self.assertIn(b"event: error", body[len(prefix):])
        self.assertEqual(h._acc["terminal_reason"], "unexpected_eof")

    def test_observer_error_does_not_append_error(self):
        wire = b"data: {bad}\n\n"
        h = _make_handler()
        h._write_streaming_response(200, [], _FakeUpstreamResp([wire]), "anthropic")
        self.assertEqual(_decode_chunked(h.wfile.getvalue()), wire)
        self.assertEqual(h._acc["stream_integrity"], "observer_error")

    def test_feed_exception_isolated_from_forwarding(self):
        wire = b"data: anything\n\n"
        h = _make_handler()
        observer = pt.PassthroughStreamObserver("anthropic", h._acc)
        with patch.object(observer._framer, "feed", side_effect=Exception("boom")), \
                patch("core.server.pt.PassthroughStreamObserver", return_value=observer):
            h._write_streaming_response(200, [], _FakeUpstreamResp([wire]), "anthropic")
        self.assertEqual(_decode_chunked(h.wfile.getvalue()), wire)
        self.assertEqual(h._acc["stream_integrity"], "observer_error")

    def test_upstream_read_error_does_not_append_sse_error(self):
        prefix = b': ping\n\n'
        h = _make_handler()
        h._write_streaming_response(
            200, [], _FakeUpstreamResp([prefix, ConnectionResetError("rst")]), "anthropic")
        self.assertEqual(_decode_chunked(h.wfile.getvalue()), prefix)
        self.assertEqual(h._acc["terminal_reason"], "upstream_read_error")
        self.assertTrue(h.close_connection)

    def test_eof_repair_client_disconnect_is_swallowed(self):
        class DisconnectingFile:
            def __init__(self):
                self.writes = 0

            def write(self, _data):
                self.writes += 1
                raise BrokenPipeError()

            def flush(self):
                pass

        h = _make_handler(DisconnectingFile())
        h._write_streaming_response(200, [], _FakeUpstreamResp([]), "anthropic")
        self.assertEqual(h._acc["stream_integrity"], "client_disconnect")
        self.assertTrue(h.close_connection)


if __name__ == "__main__":
    unittest.main()
