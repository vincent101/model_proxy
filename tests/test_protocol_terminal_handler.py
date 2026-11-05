"""P0 跨协议终态契约与 handler 流写回回归测试。"""

import io
import json
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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


def _chat_chunks(finish):
    delta = ({"tool_calls": [{"index": 0, "id": "call_synthetic", "type": "function",
                              "function": {"name": "lookup", "arguments": '{"x":1}'}}]}
             if finish == "tool_calls" else {"content": "synthetic"})
    return [
        {"choices": [{"delta": delta, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": finish}]},
        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7}},
    ]


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

    def test_completed_tool_then_text_handoff(self):
        wire, adapter = self._run([
            {"type": "response.output_item.added", "output_index": 0,
             "item": {"type": "function_call", "id": "item_synthetic",
                      "call_id": "call_synthetic", "name": "lookup", "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "item_id": "item_synthetic",
             "output_index": 0, "delta": '{"x":1}'},
            {"type": "response.output_text.delta", "delta": "tail"},
            {"type": "response.completed", "response": {"status": "completed"}},
        ])
        events = [json.loads(line[6:]) for line in wire.splitlines() if line.startswith("data: ")]
        blocks = [event["content_block"]["type"] for event in events
                  if event["type"] == "content_block_start"]
        self.assertEqual(blocks, ["tool_use", "text"])
        self.assertEqual([event["delta"]["stop_reason"] for event in events
                          if event["type"] == "message_delta"], ["tool_use"])
        self.assertEqual(wire.count("event: message_stop"), 1)
        self.assertEqual(adapter.terminal_state(),
                         pt.TerminalState(pt.TerminalStatus.COMPLETED, "tool_use"))
        self.assertEqual(adapter.finalize(), [])

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
    def test_terminal_then_normal_eof_stays_valid_and_chunked_ends(self):
        payload = b''.join([
            b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ])
        h = _Handler()
        h._acc.update({"usage_in": 0, "usage_out": 0})
        h._write_streaming_response(200, [], _Upstream(payload), "anthropic")
        self.assertEqual(h._acc["stream_integrity"], "valid")
        self.assertEqual(h._acc["terminal_status"], "completed")
        self.assertTrue(h.wfile.getvalue().endswith(b"0\r\n\r\n"))

    def test_unconfirmed_then_upstream_rst_closes_without_synthetic_error(self):
        payload = b'event: message_start\ndata: {"type":"message_start","message":{"usage":{}}}\n\n'
        h = _Handler()
        h._acc.update({"usage_in": 0, "usage_out": 0})
        h._write_streaming_response(
            200, [], _Upstream(payload, ConnectionResetError("rst")), "anthropic")
        self.assertEqual(h._acc["stream_integrity"], "invalid")
        self.assertEqual(_decode_chunked(h.wfile.getvalue()), payload)


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

    def test_anthropic_confirmed_then_redundant_done_tail_is_ignored(self):
        # mcli 实测样本：语义完整的 anthropic 流之后追加 OpenAI 风格 data: [DONE] 尾巴。
        # 终态已确认（message_stop）→ 该事件被忽略，观察结果仍为 valid 终态。
        acc = {}
        observer = pt.PassthroughStreamObserver("anthropic", acc)
        observer.feed(b''.join([
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":15}}}\n\n',
            b'event: content_block_start\n'
            b'data: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"text","text":""}}\n\n',
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"hi"}}\n\n',
            b'event: message_delta\n'
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
            b'"usage":{"output_tokens":265}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            b'data: [DONE]\n\n',
        ]))
        result = observer.finish()
        self.assertEqual(result.kind, "terminal")
        self.assertTrue(observer._tracker.confirmed)
        self.assertEqual(result.terminal_state.status, pt.TerminalStatus.COMPLETED)
        self.assertEqual(acc["stop_reason"], "end_turn")
        self.assertEqual((acc["usage_in"], acc["usage_out"]), (15, 265))

    def test_anthropic_done_before_terminal_still_malformed(self):
        # [DONE] 出现在 message_stop 之前（终态未确认）→ 仍是 observer_error/malformed。
        observer = pt.PassthroughStreamObserver("anthropic", {})
        observer.feed(b''.join([
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{"usage":{}}}\n\n',
            b'data: [DONE]\n\n',
        ]))
        result = observer.finish()
        self.assertEqual(result.kind, "observer_error")
        self.assertEqual(result.reason, "observer_error")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestAdapterTerminalState(unittest.TestCase):
    """统一 adapter.terminal_state() 契约：修复原 _adapter_terminal_state 缺失。"""

    def test_chat_adapter_terminal_states(self):
        cases = [("stop", pt.TerminalStatus.COMPLETED, "end_turn"),
                 ("tool_calls", pt.TerminalStatus.COMPLETED, "tool_use"),
                 ("length", pt.TerminalStatus.INCOMPLETE, "max_tokens"),
                 ("content_filter", pt.TerminalStatus.REFUSED, "refusal")]
        for finish, status, reason in cases:
            with self.subTest(finish=finish):
                a = pt.OpenAIToAnthropicStreamAdapter({}, "m")
                self.assertEqual(a.terminal_state().status, pt.TerminalStatus.OPEN)
                events = []
                for chunk in _chat_chunks(finish):
                    events.extend(a.feed(chunk))
                events.extend(a.finalize())
                self.assertEqual(a.finalize(), [])
                self.assertEqual(a.usage_tuple(), (11, 7, 0))
                self.assertEqual(events[-2]["delta"]["stop_reason"], reason)
                self.assertEqual(events[-1]["type"], "message_stop")
                self.assertEqual(a.terminal_state(), pt.TerminalState(status, reason))

    def test_chat_adapter_error_and_missing_finish_paths(self):
        adapter = pt.OpenAIToAnthropicStreamAdapter({}, "m")
        with self.assertRaises(pt.TranslationError):
            adapter.feed({"choices": [{"delta": {}, "finish_reason": "future"}]})
        self.assertEqual(adapter.finalize()[0]["type"], "error")
        self.assertEqual(adapter.terminal_state().status, pt.TerminalStatus.FAILED)

        for chunks in (
            [{"choices": [{"delta": {"content": "unfinished"}, "finish_reason": None}]}],
            [{"error": {"type": "server_error", "message": "synthetic"}}],
            [{"choices": [{"delta": {}, "finish_reason": "stop"}]},
             {"error": {"type": "server_error", "message": "late synthetic"}}],
            [{"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "one", "function": {"name": "f", "arguments": "{}"}}]},
                "finish_reason": None}]},
             {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "different", "function": {"arguments": ""}}]},
                "finish_reason": None}]}],
        ):
            with self.subTest(chunks=chunks):
                adapter = pt.OpenAIToAnthropicStreamAdapter({}, "m")
                events = []
                for chunk in chunks:
                    events.extend(adapter.feed(chunk))
                events.extend(adapter.finalize())
                self.assertEqual(sum(e["type"] == "error" for e in events), 1)
                self.assertNotIn("message_stop", [e["type"] for e in events])
                self.assertEqual(adapter.terminal_state().status, pt.TerminalStatus.FAILED)
                self.assertEqual(adapter.finalize(), [])

    def test_chat_handler_wire_and_terminal_accounting(self):
        cases = [("stop", "completed", "end_turn"),
                 ("tool_calls", "completed", "tool_use"),
                 ("length", "incomplete", "max_tokens"),
                 ("content_filter", "refused", "refusal")]
        for finish, status, reason in cases:
            with self.subTest(finish=finish), patch.object(pt, "gen_msg_id", return_value="msg_synthetic"):
                chunks = _chat_chunks(finish)
                # 独立固定 wire oracle：不调用当前 adapter 或生产 serializer 生成期望。
                expected_start = (
                    b'event: message_start\ndata: {"type":"message_start","message":'
                    b'{"id":"msg_synthetic","type":"message","role":"assistant",'
                    b'"content":[],"model":"m","stop_reason":null,"stop_sequence":null,'
                    b'"usage":{"input_tokens":0,"output_tokens":0,'
                    b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n\n'
                    b'event: ping\ndata: {"type":"ping"}\n\n')
                if finish == "tool_calls":
                    expected_content = (
                        b'event: content_block_start\ndata: {"type":"content_block_start",'
                        b'"index":0,"content_block":{"type":"tool_use","id":"call_synthetic",'
                        b'"name":"lookup","input":{}}}\n\n'
                        b'event: content_block_delta\ndata: {"type":"content_block_delta",'
                        b'"index":0,"delta":{"type":"input_json_delta",'
                        b'"partial_json":"{\\"x\\":1}"}}\n\n')
                else:
                    expected_content = (
                        b'event: content_block_start\ndata: {"type":"content_block_start",'
                        b'"index":0,"content_block":{"type":"text","text":""}}\n\n'
                        b'event: content_block_delta\ndata: {"type":"content_block_delta",'
                        b'"index":0,"delta":{"type":"text_delta","text":"synthetic"}}\n\n')
                expected_end = (
                    b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
                    b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"'
                    + reason.encode("ascii") +
                    b'","stop_sequence":null},"usage":{"output_tokens":7,"input_tokens":11}}\n\n'
                    b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
                expected_wire = expected_start + expected_content + expected_end
                payload = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
                payload += b"data: [DONE]\n\n"
                h = _Handler()
                adapter = pt.OpenAIToAnthropicStreamAdapter({}, "m")
                upstream = _Upstream(payload)
                h._write_translated_stream(upstream, adapter)
                wire = _decode_chunked(h.wfile.getvalue())
                self.assertEqual(wire, expected_wire)
                actual_events = [json.loads(line[6:]) for line in wire.splitlines()
                                 if line.startswith(b"data: ")]
                self.assertEqual(actual_events[0]["message"]["id"], "msg_synthetic")
                self.assertEqual(actual_events[3]["delta"],
                                 {"type": "input_json_delta", "partial_json": '{"x":1}'}
                                 if finish == "tool_calls" else
                                 {"type": "text_delta", "text": "synthetic"})
                self.assertEqual(actual_events[-2], {
                    "type": "message_delta",
                    "delta": {"stop_reason": reason, "stop_sequence": None},
                    "usage": {"input_tokens": 11, "output_tokens": 7}})
                self.assertEqual(actual_events[-1], {"type": "message_stop"})
                self.assertIn(("status", 200), h.headers_sent)
                self.assertEqual(h._acc["status"], 200)
                self.assertEqual(h._acc["stream_integrity"], "valid")
                self.assertEqual(h._acc["terminal_status"], status)
                self.assertEqual(h._acc["terminal_reason"], reason)
                self.assertTrue(upstream.closed)

    def test_anthropic_to_responses_terminal_states(self):
        a = pt.AnthropicToResponsesStreamAdapter("m")
        self.assertEqual(a.terminal_state().status, pt.TerminalStatus.OPEN)
        a.final_stop_reason = "end_turn"
        a.completed = True
        self.assertEqual(a.terminal_state().status, pt.TerminalStatus.COMPLETED)
        self.assertEqual(a.terminal_state().reason, "end_turn")
        a.failed = True
        self.assertEqual(a.terminal_state().status, pt.TerminalStatus.FAILED)

    def test_responses_to_anthropic_terminal_states(self):
        a = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        self.assertEqual(a.terminal_state().status, pt.TerminalStatus.OPEN)
        a.final_stop_reason = "tool_use"
        a._completed = True
        self.assertEqual(a.terminal_state().status, pt.TerminalStatus.COMPLETED)
        self.assertEqual(a.terminal_state().reason, "tool_use")
        a._failed = True
        self.assertEqual(a.terminal_state().status, pt.TerminalStatus.FAILED)

    def test_handler_records_open_terminal_as_invalid(self):
        h = _Handler()
        h._record_stream_terminal(pt.TerminalState(pt.TerminalStatus.OPEN, ""))
        self.assertEqual(h._acc["stream_integrity"], "invalid")
        self.assertEqual(h._acc["terminal_reason"], "unexpected_eof")


class TestTranslatedStreamRedundantDoneTail(unittest.TestCase):
    """mcli 实测兼容：转换流终态后追加 data: [DONE] 应被忽略，不判 malformed。"""

    def test_responses_to_anthropic_done_tail_after_completed(self):
        payload = b"".join([
            b'data: {"type":"response.created","response":{"status":"in_progress"}}\n\n',
            b'data: {"type":"response.output_item.added","item":{"type":"message","status":"in_progress","content":[]},"output_index":0}\n\n',
            b'data: {"type":"response.output_text.delta","delta":"OK","output_index":0}\n\n',
            b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":1,"output_tokens":1}}}\n\n',
            b'data: [DONE]\n\n',
        ])
        h = _Handler()
        adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        h._write_translated_stream_from_responses(_Upstream(payload), adapter)
        wire = _decode_chunked(h.wfile.getvalue()).decode()
        self.assertIn("message_stop", wire)
        self.assertNotIn("event: error", wire)
        self.assertEqual(h._acc["stream_integrity"], "valid")
        self.assertEqual(h._acc["terminal_status"], "completed")

    def test_responses_to_anthropic_done_before_terminal_is_malformed(self):
        payload = b"".join([
            b'data: {"type":"response.created","response":{"status":"in_progress"}}\n\n',
            b'data: [DONE]\n\n',
        ])
        h = _Handler()
        adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        h._write_translated_stream_from_responses(_Upstream(payload), adapter)
        self.assertEqual(h._acc["stream_integrity"], "invalid")
        self.assertEqual(h._acc["terminal_reason"], "malformed_stream")

    def test_anthropic_to_responses_done_tail_after_completed(self):
        payload = b"".join([
            b'event: message_start\ndata: {"type":"message_start","message":{"usage":{}}}\n\n',
            b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            b'data: [DONE]\n\n',
        ])
        h = _Handler()
        adapter = pt.AnthropicToResponsesStreamAdapter("m")
        h._write_responses_stream(_Upstream(payload), adapter)
        wire = _decode_chunked(h.wfile.getvalue()).decode()
        self.assertIn("response.completed", wire)
        self.assertNotIn("response.failed", wire)
        self.assertEqual(h._acc["stream_integrity"], "valid")

    def test_anthropic_to_responses_done_before_terminal_is_malformed(self):
        payload = b"".join([
            b'event: message_start\ndata: {"type":"message_start","message":{"usage":{}}}\n\n',
            b'data: [DONE]\n\n',
        ])
        h = _Handler()
        adapter = pt.AnthropicToResponsesStreamAdapter("m")
        h._write_responses_stream(_Upstream(payload), adapter)
        self.assertEqual(h._acc["stream_integrity"], "invalid")
        self.assertEqual(h._acc["terminal_reason"], "malformed_stream")


class _FramingUpstream:
    """只交付合成 read chunks；下一次 read 是首帧已写出的 happens-before 检查点。"""

    def __init__(self, chunks, on_second_read=None):
        self.chunks = list(chunks)
        self.on_second_read = on_second_read
        self.read_sizes = []
        self.checked = False
        self.closed = False

    def read(self, size):
        self.read_sizes.append(size)
        if len(self.read_sizes) == 2 and self.on_second_read is not None:
            self.on_second_read()
            self.checked = True
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def close(self):
        self.closed = True


class TestConvertedSSEFraming(unittest.TestCase):
    PROTOCOLS = ("chat", "anthropic", "responses")
    ENDINGS = (b"\n", b"\r\n", b"\r", None)

    @staticmethod
    def _events(source):
        if source == "chat":
            return [
                {"choices": [{"delta": {"content": "合成🙂"}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7}},
            ]
        if source == "anthropic":
            return [
                {"type": "message_start", "message": {"usage": {}}},
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": "合成🙂"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                 "usage": {"input_tokens": 11, "output_tokens": 7}},
                {"type": "message_stop"},
            ]
        return [
            {"type": "response.created", "response": {"status": "in_progress"}},
            {"type": "response.output_text.delta", "delta": "合成🙂"},
            {"type": "response.completed", "response": {"status": "completed",
             "usage": {"input_tokens": 11, "output_tokens": 7}}},
        ]

    @staticmethod
    def _frame(event, ending=b"\n", *, explicit_type=False, multidata=False):
        data = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
        lines = []
        if explicit_type:
            lines.append(b"event: " + event["type"].encode())
        if multidata and b"," in data:
            head, tail = data.split(b",", 1)
            lines.extend([b"data: " + head + b",", b"data:" + tail])
        else:
            lines.append(b"data: " + data)
        lines.append(b"")
        # 最后完整空行使用 CR，即使 chunk 结束在 CR 也必须即时 dispatch。
        mixed = (b"\n", b"\r\n", b"\n")
        return b"".join(line + (ending if ending is not None else
                               (b"\r" if i == len(lines) - 1 else mixed[i % 3]))
                        for i, line in enumerate(lines))

    @classmethod
    def _payload(cls, source, ending=b"\n", *, multidata=False, explicit_type=None):
        if explicit_type is None:
            explicit_type = source == "anthropic"
        wire = b"".join(cls._frame(e, ending, explicit_type=explicit_type,
                                  multidata=multidata) for e in cls._events(source))
        if source == "chat":
            wire += b"data: [DONE]" + (ending or b"\r") * 2
        return wire

    @staticmethod
    def _wire_events(h):
        return [json.loads(line[6:]) for line in _decode_chunked(h.wfile.getvalue()).splitlines()
                if line.startswith(b"data: ")]

    def _run(self, source, chunks, *, check_first=False, disconnect=False):
        h = _Handler()
        if disconnect:
            h.wfile = SimpleNamespace(
                write=lambda _data: (_ for _ in ()).throw(BrokenPipeError()),
                flush=lambda: None)
        first_type = b"response.created" if source == "anthropic" else b"message_start"

        def check():
            self.assertIn(first_type, _decode_chunked(h.wfile.getvalue()))

        resp = _FramingUpstream([c for c in chunks if c != b""],
                                check if check_first else None)
        framer = pt.SSEFramer()
        with patch.object(pt, "gen_msg_id", return_value="msg_synthetic"), \
                patch.object(pt, "gen_response_id", return_value="resp_synthetic"), \
                patch.object(pt, "gen_conversation_id", return_value="conv_synthetic"), \
                patch.object(pt, "gen_message_id", return_value="item_synthetic"), \
                patch.object(pt.time, "time", return_value=1700000000):
            if source == "chat":
                adapter = pt.OpenAIToAnthropicStreamAdapter({}, "m")
                writer = h._write_translated_stream
            elif source == "anthropic":
                adapter = pt.AnthropicToResponsesStreamAdapter("m")
                writer = h._write_responses_stream
            else:
                adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
                writer = h._write_translated_stream_from_responses
            with patch("core.server.pt.SSEFramer", return_value=framer), \
                    patch.object(framer, "feed", wraps=framer.feed) as feed, \
                    patch.object(framer, "finish", wraps=framer.finish) as finish, \
                    patch.object(adapter, "finalize", wraps=adapter.finalize) as finalize:
                writer(resp, adapter)
        self.assertTrue(resp.closed)
        self.assertTrue(all(n == 4096 for n in resp.read_sizes))
        return SimpleNamespace(h=h, adapter=adapter, resp=resp, feed=feed,
                               finish=finish, finalize=finalize)

    def _assert_success(self, result):
        self.assertEqual(result.h._acc["stream_integrity"], "valid")
        self.assertEqual(result.h._acc["terminal_status"], "completed")
        self.assertEqual(result.h._acc["terminal_reason"], "end_turn")
        self.assertEqual(result.adapter.usage_tuple(), (11, 7, 0))
        self.assertIn(("status", 200), result.h.headers_sent)

    def test_three_handlers_all_endings_every_cut_and_bytewise(self):
        for source in self.PROTOCOLS:
            baseline = self._run(source, [self._payload(source)])
            self._assert_success(baseline)
            expected = _decode_chunked(baseline.h.wfile.getvalue())
            for ending in self.ENDINGS:
                payload = self._payload(source, ending)
                for cut in range(len(payload) + 1):
                    with self.subTest(source=source, ending=ending, cut=cut):
                        result = self._run(source, [payload[:cut], payload[cut:]])
                        self._assert_success(result)
                        self.assertEqual(_decode_chunked(result.h.wfile.getvalue()), expected)
                with self.subTest(source=source, ending=ending, bytewise=True):
                    result = self._run(source, [payload[i:i + 1]
                                               for i in range(len(payload))])
                    self._assert_success(result)
                    self.assertEqual(_decode_chunked(result.h.wfile.getvalue()), expected)

    def test_multidata_and_type_fallback_match_single_data_wire(self):
        for source in self.PROTOCOLS:
            expected = _decode_chunked(self._run(
                source, [self._payload(source)]).h.wfile.getvalue())
            for ending in self.ENDINGS:
                with self.subTest(source=source, ending=ending):
                    result = self._run(source, [self._payload(
                        source, ending, multidata=True, explicit_type=False)])
                    self._assert_success(result)
                    self.assertEqual(_decode_chunked(result.h.wfile.getvalue()), expected)

    def test_first_frame_written_before_next_read_even_cr_lf_split(self):
        for source in self.PROTOCOLS:
            for ending in self.ENDINGS:
                with self.subTest(source=source, ending=ending):
                    first = self._frame(self._events(source)[0], ending)
                    continuation = b""
                    if ending == b"\r\n":
                        first, continuation = first[:-1], b"\n"
                    rest = b"".join(self._frame(e, ending)
                                    for e in self._events(source)[1:])
                    if source == "chat":
                        rest += b"data: [DONE]" + (ending or b"\r") * 2
                    result = self._run(source, [first, continuation + rest], check_first=True)
                    self.assertTrue(result.resp.checked)
                    self._assert_success(result)

    def test_comment_id_only_and_blank_frames_do_not_reach_adapter(self):
        for source in self.PROTOCOLS:
            with self.subTest(source=source):
                plain = self._payload(source, b"\r")
                result = self._run(source, [b": ping\r\rid: x\r\r" + plain])
                expected = self._run(source, [plain])
                self._assert_success(result)
                self.assertEqual(_decode_chunked(result.h.wfile.getvalue()),
                                 _decode_chunked(expected.h.wfile.getvalue()))

    def test_bad_multidata_and_type_conflict_remain_framing_errors(self):
        for source in self.PROTOCOLS:
            for bad in (b'data: {"n":1}\rdata: {"n":2}\r\r',
                        b'event: a\rdata: {"type":"b"}\r\r'):
                with self.subTest(source=source, bad=bad):
                    result = self._run(source, [bad])
                    self.assertEqual(result.h._acc["terminal_reason"], "malformed_stream")
                    self.assertNotIn("message_stop", [e["type"] for e in self._wire_events(result.h)])
                    self.assertNotIn("response.completed", [e["type"] for e in self._wire_events(result.h)])
                    self.assertEqual(result.finish.call_count, 0)
                    self.assertEqual(result.finalize.call_count, 0)

    def test_nonchat_missing_type_still_malformed(self):
        for source in ("anthropic", "responses"):
            with self.subTest(source=source):
                result = self._run(source, [b'data: {"delta":"synthetic"}\r\r'])
                self.assertEqual(result.h._acc["terminal_reason"], "malformed_stream")

    def test_unknown_event_and_chat_finish_stay_adapter_errors(self):
        for source in self.PROTOCOLS:
            with self.subTest(source=source):
                event = ({"choices": [{"delta": {}, "finish_reason": "future"}]}
                         if source == "chat" else {"type": "future_ping"})
                result = self._run(source, [self._frame(event, b"\r")])
                self.assertIn("unknown", result.h._acc["terminal_reason"])
                self.assertEqual(result.finish.call_count, 0)
                self.assertEqual(result.finalize.call_count, 0)

    def test_eof_complete_tail_is_accepted_only_at_normal_eof(self):
        for source in self.PROTOCOLS:
            with self.subTest(source=source):
                payload = self._payload(source)
                if source == "chat":
                    payload = payload[:-len(b"data: [DONE]\n\n")]
                payload = payload.rstrip(b"\n")
                result = self._run(source, [payload])
                self._assert_success(result)
                self.assertEqual(result.finish.call_count, 1)
                self.assertEqual(result.finalize.call_count, 1)

    def test_missing_terminal_normal_eof_keeps_original_finalize_failure(self):
        for source in self.PROTOCOLS:
            with self.subTest(source=source):
                result = self._run(source, [self._frame(self._events(source)[0], b"\r")])
                events = self._wire_events(result.h)
                expected = "response.failed" if source == "anthropic" else "error"
                self.assertEqual([e["type"] for e in events].count(expected), 1)
                self.assertNotIn("message_stop", [e["type"] for e in events])
                self.assertNotIn("response.completed", [e["type"] for e in events])
                self.assertEqual(result.finish.call_count, 1)
                self.assertEqual(result.finalize.call_count, 1)

    def test_read_exception_never_finishes_or_finalizes_buffered_tail(self):
        for source in self.PROTOCOLS:
            with self.subTest(source=source):
                payload = self._payload(source)
                if source == "chat":
                    payload = payload[:-len(b"data: [DONE]\n\n")]
                payload = payload.rstrip(b"\n")
                result = self._run(source, [payload, OSError("synthetic read failure")])
                self.assertEqual(result.finish.call_count, 0)
                self.assertEqual(result.finalize.call_count, 0)
                self.assertEqual(result.h._acc["terminal_reason"], "internal_translation_error")
                events = self._wire_events(result.h)
                self.assertNotIn("message_stop", [e["type"] for e in events])
                self.assertNotIn("response.completed", [e["type"] for e in events])

    def test_client_disconnect_does_not_finish_or_finalize(self):
        for source in self.PROTOCOLS:
            with self.subTest(source=source):
                result = self._run(source, [self._payload(source)], disconnect=True)
                self.assertEqual(result.h._acc["stream_integrity"], "client_disconnect")
                self.assertEqual(result.finish.call_count, 0)
                self.assertEqual(result.finalize.call_count, 0)

    def test_chat_done_breaks_without_consuming_bad_tail_or_reading_again(self):
        for ending in self.ENDINGS:
            with self.subTest(ending=ending):
                bad = b"data: {bad}" + (ending or b"\r") * 2
                result = self._run("chat", [self._payload("chat", ending) + bad,
                                          AssertionError("must not read after DONE")])
                self._assert_success(result)
                self.assertEqual(len(result.resp.read_sizes), 1)
                self.assertEqual(result.finish.call_count, 0)
                self.assertEqual(result.finalize.call_count, 1)
                self.assertNotIn("error", [e["type"] for e in self._wire_events(result.h)])

    def test_nonchat_done_requires_confirmed_terminal_including_eof_tail(self):
        for source in ("anthropic", "responses"):
            for ending in self.ENDINGS:
                for suffix in (b"", (ending or b"\r") * 2):
                    with self.subTest(source=source, ending=ending, suffix=suffix):
                        done = b"data: [DONE]" + suffix
                        before = self._run(source, [done])
                        self.assertEqual(before.h._acc["terminal_reason"], "malformed_stream")
                        after = self._run(source, [self._payload(source, ending) + done])
                        self._assert_success(after)

    def test_confirmed_terminal_survives_same_chunk_bad_tail(self):
        for source in ("anthropic", "responses"):
            for ending in self.ENDINGS:
                with self.subTest(source=source, ending=ending):
                    result = self._run(source, [self._payload(source, ending) +
                                               b"data: {bad}" + (ending or b"\r") * 2])
                    types = [e["type"] for e in self._wire_events(result.h)]
                    terminal = "response.completed" if source == "anthropic" else "message_stop"
                    self.assertEqual(types.count(terminal), 1)
                    self.assertNotIn("error", types)
                    self.assertNotIn("response.failed", types)
                    self.assertEqual(result.h._acc["terminal_reason"], "malformed_stream")
                    self.assertEqual(result.finish.call_count, 0)
                    self.assertEqual(result.finalize.call_count, 0)
                    self.assertTrue(result.feed.call_args_list)
                    self.assertTrue(all(c.kwargs == {"max_events": 1}
                                        for c in result.feed.call_args_list))
                    self.assertTrue(any(c.args == (b"",) for c in result.feed.call_args_list))

    def test_post_terminal_unknown_keeps_existing_adapter_redundancy_policy(self):
        for source in ("anthropic", "responses"):
            with self.subTest(source=source):
                result = self._run(source, [self._payload(source, b"\r") +
                                           self._frame({"type": "future_ping"}, b"\r")])
                self._assert_success(result)

    def test_nonchat_done_after_failed_or_incomplete_is_redundant(self):
        for source in ("anthropic", "responses"):
            for status in ("failed", "incomplete"):
                for ending in self.ENDINGS:
                    with self.subTest(source=source, status=status, ending=ending):
                        if status == "failed":
                            event = {"type": "error", "error": {
                                "type": "server_error", "message": "synthetic failure"}}
                            events = [event] if source == "anthropic" else [
                                {"type": "response.failed", "response": {
                                    "status": "failed", "error": event["error"]}}]
                        elif source == "anthropic":
                            events = [{"type": "message_delta", "delta": {
                                "stop_reason": "max_tokens"}, "usage": {}},
                                {"type": "message_stop"}]
                        else:
                            events = [{"type": "response.incomplete", "response": {
                                "status": "incomplete", "incomplete_details": {
                                    "reason": "max_output_tokens"}}}]
                        payload = b"".join(self._frame(e, ending) for e in events)
                        result = self._run(source, [payload + b"data: [DONE]"])
                        self.assertEqual(result.h._acc["stream_integrity"], "valid")
                        self.assertEqual(result.h._acc["terminal_status"], status)
                        types = [e["type"] for e in self._wire_events(result.h)]
                        terminal = (("response.failed" if status == "failed" else
                                     "response.incomplete") if source == "anthropic" else
                                    ("error" if status == "failed" else "message_stop"))
                        self.assertEqual(types.count(terminal), 1)

    def test_nonchat_done_after_candidate_but_before_confirmation_is_malformed(self):
        for source in ("anthropic", "responses"):
            with self.subTest(source=source):
                candidate = ({"type": "message_delta", "delta": {
                    "stop_reason": "end_turn"}, "usage": {}} if source == "anthropic" else
                    {"type": "response.created", "response": {"status": "in_progress"}})
                result = self._run(source, [self._frame(candidate, b"\r") + b"data: [DONE]\r\r"])
                self.assertEqual(result.h._acc["terminal_reason"], "malformed_stream")

    def test_confirmed_terminal_survives_malformed_unframed_eof_tail(self):
        for source in ("anthropic", "responses"):
            with self.subTest(source=source):
                result = self._run(source, [self._payload(source, b"\r") + b"data: {bad}"])
                types = [e["type"] for e in self._wire_events(result.h)]
                terminal = "response.completed" if source == "anthropic" else "message_stop"
                self.assertEqual(types.count(terminal), 1)
                self.assertNotIn("error", types)
                self.assertNotIn("response.failed", types)
                self.assertEqual(result.h._acc["terminal_reason"], "malformed_stream")
                self.assertEqual(result.finish.call_count, 1)
                self.assertEqual(result.finalize.call_count, 0)

    def test_unknown_terminal_keeps_original_adapter_failure_policy(self):
        for source in ("anthropic", "responses"):
            with self.subTest(source=source):
                events = ([{"type": "message_delta", "delta": {
                    "stop_reason": "future"}, "usage": {}}, {"type": "message_stop"}]
                    if source == "anthropic" else [{"type": "response.completed",
                    "response": {"status": "future"}}])
                result = self._run(source, [b"".join(self._frame(e, b"\r") for e in events)])
                types = [e["type"] for e in self._wire_events(result.h)]
                self.assertNotIn("message_stop", types)
                self.assertNotIn("response.completed", types)
                self.assertEqual(types.count("response.failed" if source == "anthropic" else "error"), 1)
                self.assertEqual(result.h._acc["terminal_status"],
                                 "failed" if source == "anthropic" else "open")
