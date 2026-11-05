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


class TestPassthroughFramingIsolation(unittest.TestCase):
    SOURCES = ("anthropic", "responses", "chat")
    ENDINGS = (b"\n", b"\r\n", b"\r", None)

    @staticmethod
    def _wire(source, ending):
        if source == "anthropic":
            lines = [
                b": ping", b"",
                b'event: content_block_delta',
                'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"合成🙂"}}'.encode(),
                b"", b"event: message_delta",
                b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}',
                b"", b"event: message_stop", b'data: {"type":"message_stop"}', b"",
            ]
        elif source == "responses":
            lines = [
                b": ping", b"",
                'data: {"type":"response.output_text.delta","delta":"合成🙂"}'.encode(), b"",
                b'data: {"type":"response.completed","response":{"status":"completed","usage":{"output_tokens":7}}}',
                b"",
            ]
        else:
            lines = [
                b": ping", b"",
                'data: {"choices":[{"delta":{"content":"合成🙂"},"finish_reason":null}]}'.encode(),
                b"", b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                b"", b"data: [DONE]", b"",
            ]
        # 非空行用 LF/CRLF 交替；空行以 CR 即时结束，不拼出伪 CR+LF 空行。
        return b"".join(line + (ending if ending is not None else
                               (b"\r" if not line else (b"\n" if i % 2 else b"\r\n")))
                        for i, line in enumerate(lines))

    def _run(self, source, chunks, observer=None):
        from types import SimpleNamespace
        from unittest.mock import Mock

        h = _make_handler()
        h.server = SimpleNamespace(
            config_store=SimpleNamespace(get_body_error_rules=lambda: []),
            cooldown_store=Mock(), stream_health_store=Mock())
        resp = _FakeUpstreamResp([c for c in chunks if c != b""])
        observed = observer or pt.PassthroughStreamObserver(source, h._acc)
        with patch("core.server.pt.PassthroughStreamObserver", return_value=observed):
            h._write_streaming_response(200, [], resp, source)
        self.assertTrue(resp.closed)
        h.server.cooldown_store.cooldown.assert_not_called()
        self.assertEqual(h.server.stream_health_store.mock_calls, [])
        self.assertNotIn("body_error", h._acc)
        return h

    def test_three_protocols_all_endings_every_cut_and_bytewise_are_byte_exact(self):
        for source in self.SOURCES:
            for ending in self.ENDINGS:
                wire = self._wire(source, ending)
                for cut in range(len(wire) + 1):
                    with self.subTest(source=source, ending=ending, cut=cut):
                        h = self._run(source, [wire[:cut], wire[cut:]])
                        self.assertEqual(_decode_chunked(h.wfile.getvalue()), wire)
                        # Chat observer 原本不支持 business payload；不扩大其控制权/协议。
                        self.assertEqual(h._acc["stream_integrity"],
                                         "observer_error" if source == "chat" else "valid")
                with self.subTest(source=source, ending=ending, bytewise=True):
                    h = self._run(source, [wire[i:i + 1] for i in range(len(wire))])
                    self.assertEqual(_decode_chunked(h.wfile.getvalue()), wire)

    def test_observer_feed_and_finish_errors_never_change_forwarded_bytes(self):
        for source in self.SOURCES:
            for operation in ("feed", "finish"):
                with self.subTest(source=source, operation=operation):
                    wire = self._wire(source, None)
                    observer = pt.PassthroughStreamObserver(source, {})
                    with patch.object(observer._framer, operation,
                                      side_effect=RuntimeError("synthetic observer error")):
                        h = self._run(source, [wire[:13], wire[13:]], observer)
                    self.assertEqual(_decode_chunked(h.wfile.getvalue()), wire)
                    self.assertEqual(h._acc["stream_integrity"], "observer_error")
                    self.assertEqual(h._acc["final_error"], "")

    def test_observer_bad_json_tail_does_not_add_error_or_replace_bytes(self):
        for source in self.SOURCES:
            with self.subTest(source=source):
                wire = self._wire(source, b"\r") + b"data: {bad}\r\r"
                h = self._run(source, [wire])
                self.assertEqual(_decode_chunked(h.wfile.getvalue()), wire)
                self.assertEqual(h._acc["stream_integrity"], "observer_error")

    def test_normal_eof_complete_terminal_without_blank_line_remains_byte_exact(self):
        for source in ("anthropic", "responses"):
            with self.subTest(source=source):
                wire = self._wire(source, b"\r").rstrip(b"\r")
                h = self._run(source, [wire])
                self.assertEqual(_decode_chunked(h.wfile.getvalue()), wire)
                self.assertEqual(h._acc["stream_integrity"], "valid")

    def test_missing_terminal_eof_still_repairs_only_after_original_prefix(self):
        prefixes = {
            "anthropic": b'data: {"type":"message_start","message":{"usage":{}}}\r\r',
            "responses": b'data: {"type":"response.created","response":{"status":"in_progress"}}\r\r',
        }
        for source, prefix in prefixes.items():
            with self.subTest(source=source):
                h = self._run(source, [prefix])
                body = _decode_chunked(h.wfile.getvalue())
                self.assertTrue(body.startswith(prefix))
                self.assertIn(b"event: error" if source == "anthropic" else b"response.failed",
                              body[len(prefix):])
                self.assertEqual(h._acc["terminal_reason"], "unexpected_eof")

    def test_read_exception_does_not_finish_observer_or_append_error(self):
        for source in self.SOURCES:
            with self.subTest(source=source):
                prefix = b": ping\r\r"
                observer = pt.PassthroughStreamObserver(source, {})
                with patch.object(observer, "finish", wraps=observer.finish) as finish:
                    h = self._run(source, [prefix, OSError("synthetic read error")], observer)
                finish.assert_not_called()
                self.assertEqual(_decode_chunked(h.wfile.getvalue()), prefix)
                self.assertEqual(h._acc["terminal_reason"], "upstream_read_error")

    def test_unknown_nonterminal_passthrough_keeps_existing_tolerance(self):
        for source in ("anthropic", "responses"):
            with self.subTest(source=source):
                wire = b'data: {"type":"future_ping"}\r\r' + self._wire(source, b"\r")
                h = self._run(source, [wire])
                self.assertEqual(_decode_chunked(h.wfile.getvalue()), wire)
                self.assertEqual(h._acc["stream_integrity"], "valid")


class TestEventOnlyPassthroughIsolation(unittest.TestCase):
    def test_typed_empty_remains_observer_error_byte_exact(self):
        wire = (b'event: ping\nid: x\n\n'
                b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
        h = _make_handler()
        h._acc.update(failover=0, route_failover=0, attempts=1)
        resp = _FakeUpstreamResp([wire])
        h._write_streaming_response(200, [], resp, "anthropic")
        self.assertEqual(_decode_chunked(h.wfile.getvalue()), wire)
        self.assertEqual(h._acc["stream_integrity"], "observer_error")
        self.assertEqual((h._acc["failover"], h._acc["route_failover"], h._acc["attempts"]),
                         (0, 0, 1))
        self.assertTrue(resp.closed)


if __name__ == "__main__":
    unittest.main()
