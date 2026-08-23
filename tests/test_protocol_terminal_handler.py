"""P0 跨协议终态契约与 handler 流写回回归测试。"""

import io
import json
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import translate as pt  # noqa: E402
from core.server import ModelProxyHandler, translation_error_for_upstream  # noqa: E402


class _Upstream:
    def __init__(self, payload: bytes, tail_error=None):
        self.payload = payload
        self.done = False
        self.closed = False
        self.tail_error = tail_error

    def read(self, _n=-1):
        if self.done:
            if self.tail_error is not None:
                error, self.tail_error = self.tail_error, None
                raise error
            return b""
        self.done = True
        return self.payload

    def close(self):
        self.closed = True


class _Handler(ModelProxyHandler):
    def __init__(self):
        self.wfile = io.BytesIO()
        self._acc = {"status": 0}
        self.headers_sent = []

    def send_response(self, status):
        self.headers_sent.append(("status", status))

    def send_header(self, key, value):
        self.headers_sent.append((key, value))

    def end_headers(self):
        pass


def _decode_chunked(raw: bytes) -> bytes:
    out = bytearray()
    pos = 0
    while pos < len(raw):
        end = raw.index(b"\r\n", pos)
        size = int(raw[pos:end], 16)
        pos = end + 2
        if size == 0:
            break
        out.extend(raw[pos:pos + size])
        pos += size + 2
    return bytes(out)


class TestTerminalMappings(unittest.TestCase):
    def test_all_terminal_mappings_and_unknowns(self):
        self.assertEqual(pt.map_anthropic_terminal("max_tokens").status, pt.TerminalStatus.INCOMPLETE)
        self.assertEqual(pt.map_responses_terminal("incomplete", "max_output_tokens").reason, "max_tokens")
        self.assertEqual(pt.map_chat_terminal("content_filter").status, pt.TerminalStatus.REFUSED)
        for fn, value in ((pt.map_anthropic_terminal, "future"),
                          (pt.map_responses_terminal, "future"),
                          (pt.map_chat_terminal, "future")):
            with self.assertRaises(pt.TranslationError):
                fn(value)

    def test_error_classification(self):
        cases = [
            ({"type": "invalid_prompt"}, (400, "none")),
            ({"type": "authentication_error"}, (401, "configured")),
            ({"type": "rate_limit_error"}, (429, "configured")),
            ({"type": "overloaded_error"}, (503, "configured")),
            ({"type": "unsupported_capability"}, (422, "capability_mismatch")),
            ({"type": "mystery"}, (502, "none")),
        ]
        for error, expected in cases:
            exc = translation_error_for_upstream(error)
            self.assertEqual((exc.http_status, exc.retry_class), expected)


class TestResponsesToAnthropicHandler(unittest.TestCase):
    def _run(self, events):
        payload = b"".join(
            b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
        h = _Handler()
        adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        h._write_translated_stream_from_responses(_Upstream(payload), adapter)
        return _decode_chunked(h.wfile.getvalue()).decode(), adapter

    def test_failed_becomes_error_without_message_stop(self):
        wire, adapter = self._run([
            {"type": "response.created", "response": {"status": "in_progress"}},
            {"type": "response.failed", "response": {"status": "failed", "error": {
                "type": "invalid_prompt", "code": "invalid_prompt", "message": "bad tools"}}},
        ])
        self.assertIn("event: error", wire)
        self.assertIn("bad tools", wire)
        self.assertNotIn("message_stop", wire)
        self.assertTrue(adapter._failed)

    def test_incomplete_max_tokens_finishes_once(self):
        wire, _ = self._run([
            {"type": "response.created", "response": {"status": "in_progress"}},
            {"type": "response.incomplete", "response": {"status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"}, "usage": {}}},
        ])
        self.assertIn('"stop_reason":"max_tokens"', wire)
        self.assertEqual(wire.count("message_stop"), 2)  # event line + JSON type

    def test_malformed_frame_is_error(self):
        h = _Handler()
        adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        h._write_translated_stream_from_responses(_Upstream(b"data: {bad}\n\n"), adapter)
        wire = _decode_chunked(h.wfile.getvalue()).decode()
        self.assertIn("event: error", wire)
        self.assertNotIn("message_stop", wire)

    def test_eof_without_terminal_is_error(self):
        wire, _ = self._run([{"type": "response.created", "response": {"status": "in_progress"}}])
        self.assertIn("before a terminal event", wire)
        self.assertNotIn("message_stop", wire)

    def test_completed_then_tail_read_error_has_no_second_terminal(self):
        payload = b"".join([
            b'data: {"type":"response.created","response":{"status":"in_progress"}}\n\n',
            b'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}\n\n',
        ])
        h = _Handler()
        adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        h._write_translated_stream_from_responses(
            _Upstream(payload, OSError("tail garbage")), adapter)
        wire = _decode_chunked(h.wfile.getvalue()).decode()
        self.assertEqual(wire.count("message_stop"), 2)
        self.assertNotIn("event: error", wire)

    def test_sse_comment_is_ignored(self):
        payload = b"".join([
            b': keep-alive\n\n',
            b'data: {"type":"response.created","response":{"status":"in_progress"}}\n\n',
            b'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}\n\n',
        ])
        h = _Handler()
        adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        h._write_translated_stream_from_responses(_Upstream(payload), adapter)
        wire = _decode_chunked(h.wfile.getvalue()).decode()
        self.assertIn("message_stop", wire)
        self.assertNotIn("event: error", wire)


class TestOtherDirections(unittest.TestCase):
    def test_anthropic_completed_then_tail_error_has_no_failed(self):
        payload = b"".join([
            b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ])
        h = _Handler()
        adapter = pt.AnthropicToResponsesStreamAdapter("m")
        h._write_responses_stream(_Upstream(payload, OSError("tail garbage")), adapter)
        wire = _decode_chunked(h.wfile.getvalue()).decode()
        self.assertIn("response.completed", wire)
        self.assertNotIn("response.failed", wire)

    def test_anthropic_eof_failed_and_no_completed(self):
        adapter = pt.AnthropicToResponsesStreamAdapter("m")
        events = adapter.feed("message_start", {"message": {"usage": {}}}) + adapter.finalize()
        self.assertEqual(events[-1]["type"], "response.failed")
        self.assertNotIn("response.completed", [e["type"] for e in events])

    def test_anthropic_max_tokens_incomplete(self):
        adapter = pt.AnthropicToResponsesStreamAdapter("m")
        events = []
        events += adapter.feed("message_delta", {"delta": {"stop_reason": "max_tokens"}, "usage": {}})
        events += adapter.feed("message_stop", {})
        events += adapter.finalize()
        terminal = [e for e in events if e["type"].startswith("response.") and
                    e["type"] in ("response.completed", "response.incomplete", "response.failed")]
        self.assertEqual([e["type"] for e in terminal], ["response.incomplete"])

    def test_chat_eof_and_unknown_finish_fail(self):
        self.assertEqual(pt.OpenAIToAnthropicStreamAdapter({}, "m").finalize()[0]["type"], "error")
        adapter = pt.OpenAIToAnthropicStreamAdapter({}, "m")
        with self.assertRaises(pt.TranslationError):
            adapter.feed({"choices": [{"delta": {}, "finish_reason": "future"}]})

    def test_chat_malformed_frame_is_error(self):
        h = _Handler()
        adapter = pt.OpenAIToAnthropicStreamAdapter({}, "m")
        h._write_translated_stream(_Upstream(b"data: {bad}\n\n"), adapter)
        wire = _decode_chunked(h.wfile.getvalue()).decode()
        self.assertIn("event: error", wire)
        self.assertNotIn("message_stop", wire)

    def test_usage_only_message_delta_preserves_stop_reason(self):
        adapter = pt.AnthropicToResponsesStreamAdapter("m")
        events = []
        events += adapter.feed("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {}})
        events += adapter.feed("message_delta", {"delta": {}, "usage": {"output_tokens": 1}})
        events += adapter.feed("message_stop", {})
        self.assertEqual(events[-1]["type"], "response.completed")

    def test_nonstream_envelopes_and_failed(self):
        with self.assertRaises(pt.TranslationError):
            pt.openai_to_anthropic_response({"choices": []})
        with self.assertRaises(pt.TranslationError):
            pt.responses_to_anthropic_response({"status": "failed", "output": [],
                "error": {"type": "invalid_prompt", "message": "bad"}})
        failed = pt.anthropic_to_responses_response(
            {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}, "m")
        self.assertEqual(failed["status"], "failed")

    def test_client_disconnect_does_not_finalize(self):
        h = _Handler()
        h.wfile = SimpleNamespace(write=lambda _data: (_ for _ in ()).throw(BrokenPipeError()), flush=lambda: None)
        upstream = _Upstream(b'data: {"type":"response.created"}\n\n')
        adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        h._write_translated_stream_from_responses(upstream, adapter)
        self.assertFalse(adapter._failed)
        self.assertTrue(upstream.closed)


class TestPassthroughTailErrors(unittest.TestCase):
    def test_terminal_then_upstream_rst_stays_valid_and_chunked_ends(self):
        payload = b''.join([
            b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ])
        h = _Handler()
        h._acc.update({"usage_in": 0, "usage_out": 0})
        h._write_streaming_response(
            200, [], _Upstream(payload, ConnectionResetError("rst")), "anthropic")
        self.assertEqual(h._acc["stream_integrity"], "valid")
        self.assertEqual(h._acc["terminal_status"], "completed")
        self.assertTrue(h.wfile.getvalue().endswith(b"0\r\n\r\n"))

    def test_unconfirmed_then_upstream_rst_is_invalid_error(self):
        payload = b'event: message_start\ndata: {"type":"message_start","message":{"usage":{}}}\n\n'
        h = _Handler()
        h._acc.update({"usage_in": 0, "usage_out": 0})
        h._write_streaming_response(
            200, [], _Upstream(payload, ConnectionResetError("rst")), "anthropic")
        self.assertEqual(h._acc["stream_integrity"], "invalid")
        self.assertIn(b"event: error", _decode_chunked(h.wfile.getvalue()))


class TestStreamProbe(unittest.TestCase):
    def test_empty_stream_fails_before_commit(self):
        h = _Handler()
        result = h._probe_upstream_stream(_Upstream(b""), "passthrough", "anthropic")
        self.assertFalse(result.ok)
        self.assertEqual(result.error.reason, "empty_stream")
        self.assertEqual(h.headers_sent, [])

    def test_probe_connection_reset_is_retryable_failure(self):
        class RstUpstream(_Upstream):
            def read(self, _n=-1):
                raise ConnectionResetError("rst")
        h = _Handler()
        result = h._probe_upstream_stream(RstUpstream(b""), "passthrough", "anthropic")
        self.assertFalse(result.ok)
        self.assertEqual(result.error.reason, "network_error")
        self.assertEqual(result.error.http_status, 502)

    def test_first_business_event_stops_without_waiting_for_eof(self):
        class BlockingTail(_Upstream):
            def read(self, _n=-1):
                if not self.done:
                    self.done = True
                    return b'event: message_start\ndata: {"type":"message_start","message":{"usage":{}}}\n\n'
                raise AssertionError("probe read past first event")
        h = _Handler()
        result = h._probe_upstream_stream(BlockingTail(b""), "passthrough", "anthropic")
        self.assertTrue(result.ok)
        self.assertGreaterEqual(result.first_event_ms, 0)

    def test_responses_probe_without_event_line(self):
        h = _Handler()
        payload = b'data: {"type":"response.created","response":{"status":"in_progress"}}\n\n'
        adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        result = h._probe_upstream_stream(_Upstream(payload), "anthropic_to_responses",
                                          "responses", adapter)
        self.assertTrue(result.ok)
        self.assertIn(b"message_start", result.encoded_prefix)

    def test_chat_done_is_valid_after_finish_reason(self):
        h = _Handler()
        payload = (b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                   b'data: [DONE]\n\n')
        adapter = pt.OpenAIToAnthropicStreamAdapter({}, "m")
        result = h._probe_upstream_stream(_Upstream(payload), "anthropic_to_chat",
                                          "chat", adapter)
        self.assertTrue(result.ok)

    def test_total_buffer_budget(self):
        h = _Handler()
        old = h._STREAM_PROBE_MAX_BYTES
        h._STREAM_PROBE_MAX_BYTES = 8
        try:
            result = h._probe_upstream_stream(_Upstream(b': heartbeat too large'),
                                              "passthrough", "anthropic")
        finally:
            h._STREAM_PROBE_MAX_BYTES = old
        self.assertFalse(result.ok)
        self.assertEqual(result.error.reason, "frame_too_large")


class TestSSEFramerAndPassthroughTracker(unittest.TestCase):
    def test_crlf_multiline_comment_and_type_fallback(self):
        framer = pt.SSEFramer()
        events = framer.feed(
            b': ping\r\n\r\ndata: {"type":"response.created",\r\ndata: "response":{"status":"in_progress"}}\r\n\r\n')
        self.assertTrue(events[0].is_comment)
        self.assertEqual(events[1].event_type, "response.created")

    def test_event_data_type_conflict_is_malformed(self):
        with self.assertRaises(pt.TranslationError) as ctx:
            pt.SSEFramer().feed(
                b'event: response.created\ndata: {"type":"response.completed"}\n\n')
        self.assertEqual(ctx.exception.reason, "malformed_stream")

    def test_unknown_anthropic_nonterminal_and_pause_turn(self):
        acc = {}
        tracker = pt.PassthroughTerminalTracker("anthropic", acc)
        framer = pt.SSEFramer()
        wire = b''.join([
            b'event: future_ping\ndata: {"type":"future_ping"}\n\n',
            b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":7}}}\n\n',
            b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"pause_turn"},"usage":{"output_tokens":3}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n'])
        for event in framer.feed(wire):
            tracker.feed(event)
        state = tracker.finalize()
        self.assertEqual(state.status, pt.TerminalStatus.PAUSED)
        self.assertEqual((acc["usage_in"], acc["usage_out"]), (7, 3))

    def test_responses_without_event_line_completes(self):
        tracker = pt.PassthroughTerminalTracker("responses", {})
        events = pt.SSEFramer().feed(
            b'data: {"type":"response.created","response":{"status":"in_progress"}}\n\n'
            b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n')
        for event in events:
            tracker.feed(event)
        self.assertEqual(tracker.finalize().status, pt.TerminalStatus.COMPLETED)

    def test_empty_stream_is_invalid(self):
        tracker = pt.PassthroughTerminalTracker("anthropic", {})
        with self.assertRaises(pt.TranslationError) as ctx:
            tracker.finalize()
        self.assertEqual(ctx.exception.reason, "empty_stream")


if __name__ == "__main__":
    unittest.main(verbosity=2)
