"""R2A SSE usage regressions with synthetic, offline fixtures."""
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import server, translate as pt
from tests.test_budget_retry import _FakeResp, _make_handler, _make_server, _resp_client_body, _supply
from tests.test_protocol_terminal_handler import _decode_chunked
from tests.test_usage_totals import _business_store


def _event(kind, data):
    return f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode()


def _complete(adapter, start, deltas):
    events = adapter.feed("message_start", start)
    for usage in deltas:
        events += adapter.feed("message_delta", {"usage": usage})
    events += adapter.feed("message_delta", {"delta": {"stop_reason": "end_turn"}})
    events += adapter.feed("message_stop", {})
    return next(e["response"]["usage"] for e in events if e["type"] == "response.completed")


class TestR2AAdapterUsage(unittest.TestCase):
    def test_start_and_delta_field_semantics(self):
        cases = [
            ({"input_tokens": 11, "output_tokens": 0}, [{"output_tokens": 13}], (11, 13)),
            ({"input_tokens": 11, "output_tokens": 2}, [], (11, 2)),
            ({"input_tokens": 11, "output_tokens": 2}, [None], (11, 2)),
            ({"input_tokens": 11, "output_tokens": 2}, [{"input_tokens": 8}, {"output_tokens": 13}], (8, 13)),
            ({"input_tokens": 11, "output_tokens": 2}, [{"input_tokens": 0, "output_tokens": 0}], (0, 0)),
            ({"input_tokens": 11, "output_tokens": 2}, [{"input_tokens": None, "output_tokens": None}], (11, 2)),
            ({"input_tokens": 0, "output_tokens": 0}, [{"input_tokens": 0, "output_tokens": 0}], (0, 0)),
            ({}, [{"input_tokens": 11, "output_tokens": 13}], (11, 13)),
            (None, [{"input_tokens": 11, "output_tokens": 13}], (11, 13)),
            ({"input_tokens": None, "output_tokens": None}, [{}], (0, 0)),
            ({"input_tokens": 11, "output_tokens": 2}, [{"input_tokens": 11}, {"input_tokens": 11, "output_tokens": 13}], (11, 13)),
        ]
        for start, deltas, expected in cases:
            with self.subTest(start=start, deltas=deltas):
                a = pt.AnthropicToResponsesStreamAdapter("m")
                usage = _complete(a, {"message": {"usage": start}}, deltas)
                self.assertEqual(a.usage_tuple(), (*expected, 0))
                self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]), (*expected, sum(expected)))

    def test_repeated_and_late_start_do_not_overwrite_delta(self):
        a = pt.AnthropicToResponsesStreamAdapter("m")
        a.feed("message_start", {"message": {"usage": {"input_tokens": 11}}})
        a.feed("message_start", {"message": {"usage": {"input_tokens": 99}}})
        self.assertEqual(a.usage_tuple(), (11, 0, 0))
        a.feed("message_delta", {"usage": {"input_tokens": 17, "output_tokens": 13}})
        a.feed("message_start", {"message": {"usage": {"input_tokens": 11, "output_tokens": 0}}})
        self.assertEqual(a.usage_tuple(), (17, 13, 0))
        b = pt.AnthropicToResponsesStreamAdapter("m")
        b.feed("message_delta", {"usage": {"input_tokens": 17, "output_tokens": 13}})
        b.feed("message_start", {"message": {"usage": {"input_tokens": 11, "output_tokens": 0}}})
        self.assertEqual(b.usage_tuple(), (17, 13, 0))
        b.feed("message_stop", {})
        self.assertEqual(b.feed("message_start", {"message": {"usage": {"input_tokens": 1}}}), [])
        self.assertEqual(b.usage_tuple(), (17, 13, 0))

    def test_invalid_start_and_delta_counts_keep_last_trusted_values(self):
        invalid_values = [-7, True, False, 1.5, "13", [], {}, None]
        for value in invalid_values:
            with self.subTest(value=value):
                a = pt.AnthropicToResponsesStreamAdapter("m")
                usage = _complete(a, {"message": {"usage": {"input_tokens": value,
                                  "output_tokens": value}}},
                                  [{"input_tokens": 11, "output_tokens": 13},
                                   {"input_tokens": value, "output_tokens": value}])
                self.assertEqual(a.usage_tuple(), (11, 13, 0))
                self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]),
                                 (11, 13, 24))
        # Each field is validated independently; valid zero must still override.
        a = pt.AnthropicToResponsesStreamAdapter("m")
        usage = _complete(a, {"message": {"usage": {"input_tokens": 11, "output_tokens": 13}}},
                          [{"input_tokens": -1, "output_tokens": 0},
                           {"input_tokens": 0, "output_tokens": "bad"}])
        self.assertEqual(a.usage_tuple(), (0, 0, 0))
        self.assertEqual(usage["total_tokens"], 0)

    def test_non_dict_delta_usage_is_ignored(self):
        for value in [None, [], ["output_tokens"], "output_tokens", -7, 3.5, True, False]:
            with self.subTest(value=value):
                a = pt.AnthropicToResponsesStreamAdapter("m")
                usage = _complete(a, {"message": {"usage": {"input_tokens": 11, "output_tokens": 13}}},
                                  [value])
                self.assertEqual(a.usage_tuple(), (11, 13, 0))
                self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]),
                                 (11, 13, 24))

    def test_only_delta_and_reasoning_existing_fallback(self):
        a = pt.AnthropicToResponsesStreamAdapter("m")
        a.feed("message_delta", {"usage": {"input_tokens": 11, "output_tokens": 13,
                                           "output_tokens_details": {"thinking_tokens": 5}}})
        a.feed("message_delta", {"usage": {"output_tokens_details": {"thinking_tokens": 0}}})
        self.assertEqual(a.usage_tuple(), (11, 13, 5))  # existing zero fallback
        b = pt.AnthropicToResponsesStreamAdapter("m")
        usage = _complete(b, {"message": {"usage": {"input_tokens": 11,
                            "output_tokens_details": {"thinking_tokens": 3}}}},
                          [{"output_tokens": 13, "thinking_tokens": 0}])
        self.assertEqual(b.usage_tuple(), (11, 13, 0))
        self.assertEqual(usage["output_tokens_details"]["reasoning_tokens"], 0)
        c = pt.AnthropicToResponsesStreamAdapter("m")
        usage = _complete(c, {"message": {"usage": {"input_tokens": 11,
                            "output_tokens_details": {"thinking_tokens": 3}}}},
                          [{"output_tokens": 13}, {"thinking_tokens": 5}])
        self.assertEqual(c.usage_tuple(), (11, 13, 5))
        self.assertEqual(usage["output_tokens_details"]["reasoning_tokens"], 5)

    def test_malformed_start_remains_tolerated(self):
        cases = ({}, {"message": None}, {"message": []}, {"message": "bad"},
                 {"message": {"usage": []}}, {"message": {"usage": "bad"}},
                 {"message": {"usage": {"input_tokens": "bad", "output_tokens": -1}}},
                 {"message": {"usage": {"input_tokens": True, "output_tokens": 1.5}}},
                 {"message": {"usage": {"output_tokens_details": "bad"}}})
        for start in cases:
            with self.subTest(start=start):
                a = pt.AnthropicToResponsesStreamAdapter("m")
                usage = _complete(a, start, [{"output_tokens": 13}])
                self.assertEqual(a.usage_tuple(), (0, 13, 0))
                self.assertEqual(usage["total_tokens"], 13)
        a = pt.AnthropicToResponsesStreamAdapter("m")
        with self.assertRaises(pt.TranslationError):
            a.feed("message_start", None)
        with self.assertRaises(pt.TranslationError):
            a.feed("unknown", {})


class TestR2AHandlerLedger(unittest.TestCase):
    def _check_sse_to_response_access_and_fake_ledger(self, start_usage, delta_usages, expected,
                                                     expected_reasoning=0):
        upstream = _FakeResp(b"".join([
            _event("message_start", {"type": "message_start", "message": {"usage": start_usage}}),
            _event("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            _event("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "synthetic reply"}}),
            _event("content_block_stop", {"type": "content_block_stop", "index": 0}),
            *[_event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                      "usage": usage}) for usage in delta_usages],
            _event("message_stop", {"type": "message_stop"}),
        ]))
        ns, _, _ = _make_server([_supply("s1", "anthropic")], ["s1"])
        h = _make_handler(ns, _resp_client_body(stream=True), path="/v1/responses")
        adapters = []
        adapter_type = pt.AnthropicToResponsesStreamAdapter

        def make_adapter(*args, **kwargs):
            adapter = adapter_type(*args, **kwargs)
            adapters.append(adapter)
            return adapter

        with tempfile.TemporaryDirectory(prefix="r2a-usage-test-") as tmp:
            ledger_path = Path(tmp) / "fake-totals.json"
            store = _business_store(ledger_path, mode="snapshot")
            with patch.object(server, "usage_totals", store), \
                 patch.object(server.urllib.request, "urlopen", return_value=upstream) as urlopen, \
                 patch.object(pt, "AnthropicToResponsesStreamAdapter", side_effect=make_adapter), \
                 patch.object(socket, "socket", side_effect=AssertionError("network forbidden")), \
                 self.assertLogs("model_proxy.access", level="INFO") as access:
                h._forward_logged("POST")
            urlopen.assert_called_once()
            self.assertTrue(upstream.closed)
            self.assertEqual(h._acc["conversion_kind"], "r2a")
            self.assertEqual(h._acc["stream_integrity"], "valid")
            self.assertEqual((h._acc["usage_in"], h._acc["usage_out"]), expected)
            self.assertEqual(len(adapters), 1)
            self.assertEqual(adapters[0].usage_tuple(), (*expected, expected_reasoning))
            self.assertEqual(len(access.records), 1)
            self.assertIn(f"usage_in={expected[0]} usage_out={expected[1]}", access.records[0].getMessage())
            events = [json.loads(line[6:]) for line in _decode_chunked(h.wfile.getvalue()).decode().splitlines() if line.startswith("data: ")]
            self.assertEqual([e["delta"] for e in events if e["type"] == "response.output_text.delta"],
                             ["synthetic reply"])
            self.assertEqual(sum(e["type"] == "response.completed" for e in events), 1)
            completed = next(e["response"] for e in events if e["type"] == "response.completed")
            usage = completed["usage"]
            self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]),
                             (*expected, sum(expected)))
            self.assertEqual(usage["output_tokens_details"]["reasoning_tokens"], expected_reasoning)
            self.assertEqual(completed["output"][0]["content"][0]["text"], "synthetic reply")
            ledger = json.loads(ledger_path.read_text())
            self.assertEqual(len(ledger["days"]), 1)
            for bucket in [ledger["total"], *ledger["days"].values()]:
                self.assertEqual(bucket["requests"], 1)
                combo = next(iter(bucket["combos"].values()))
                self.assertEqual((combo["usage_in"], combo["usage_out"]), expected)
                self.assertNotIn("usage_reasoning", combo)
                self.assertEqual(combo["usage_in"] + combo["usage_out"], sum(expected))

    def test_sse_to_response_access_and_fake_ledger(self):
        self._check_sse_to_response_access_and_fake_ledger(
            {"input_tokens": 11, "output_tokens": 0}, [{"output_tokens": 13}], (11, 13))

    def test_delta_negative_reasoning_is_not_recorded_in_ledger(self):
        # The existing R2A reasoning value is passed through to the response,
        # but the handler deliberately discards it before accounting.
        self._check_sse_to_response_access_and_fake_ledger(
            {"input_tokens": 11}, [{"output_tokens": 13, "thinking_tokens": -3}],
            (11, 13), expected_reasoning=-3)

    def test_invalid_delta_counts_and_usage_cannot_pollute_fake_ledger(self):
        cases = [
            ({"input_tokens": 11, "output_tokens": 0},
             [{"output_tokens": 13}, {"input_tokens": -1, "output_tokens": -7}], (11, 13)),
            ({"input_tokens": 11, "output_tokens": 13},
             ["output_tokens", {"input_tokens": True, "output_tokens": "bad"}, ["input_tokens"]], (11, 13)),
            ({"input_tokens": True, "output_tokens": -7},
             [{"input_tokens": -7, "output_tokens": False}, 42], (0, 0)),
        ]
        for start, deltas, expected in cases:
            with self.subTest(start=start, deltas=deltas):
                self._check_sse_to_response_access_and_fake_ledger(start, deltas, expected)


if __name__ == "__main__":
    unittest.main()
