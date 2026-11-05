"""Offline synthetic regressions for the two Anthropic-output stream adapters."""
import json
import unittest
from unittest.mock import patch

from core import translate as pt


def chat(delta=None, finish=None, usage=None):
    value = {"choices": [{"delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        value["usage"] = usage
    return value


def call(index, fragment="", tool_id=None, name=None):
    value = {"index": index, "function": {"arguments": fragment}}
    if tool_id is not None:
        value["id"] = tool_id
    if name is not None:
        value["function"]["name"] = name
    return value


_ABSENT = object()


def raw_call(index, fragment="", tool_id=_ABSENT, name=_ABSENT):
    fn = {"arguments": fragment}
    if name is not _ABSENT:
        fn["name"] = name
    value = {"index": index, "function": fn}
    if tool_id is not _ABSENT:
        value["id"] = tool_id
    return value


def raw_added(item_id="ia", output_index=0, call_id=_ABSENT, name=_ABSENT):
    item = {"type": "function_call", "arguments": ""}
    if item_id is not _ABSENT:
        item["id"] = item_id
    if call_id is not _ABSENT:
        item["call_id"] = call_id
    if name is not _ABSENT:
        item["name"] = name
    value = {"item": item}
    if output_index is not None:
        value["output_index"] = output_index
    return "response.output_item.added", value


def added(item_id="ia", output_index=0, call_id="ca", name="a"):
    item = {"type": "function_call", "arguments": ""}
    if name is not None:
        item["name"] = name
    if item_id is not None:
        item["id"] = item_id
    if call_id is not None:
        item["call_id"] = call_id
    value = {"item": item}
    if output_index is not None:
        value["output_index"] = output_index
    return "response.output_item.added", value


def argument(fragment="", item_id="ia", output_index=0, done=False):
    value = {"arguments" if done else "delta": fragment}
    if item_id is not None:
        value["item_id"] = item_id
    if output_index is not None:
        value["output_index"] = output_index
    return "response.function_call_arguments." + ("done" if done else "delta"), value


def item_done(item_id="ia", output_index=0, snapshot="ignored"):
    item = {"type": "function_call", "arguments": snapshot}
    if item_id is not None:
        item["id"] = item_id
    value = {"item": item}
    if output_index is not None:
        value["output_index"] = output_index
    return "response.output_item.done", value


def completed(status="completed", usage=None):
    response = {"status": status, "usage": usage or {"input_tokens": 2, "output_tokens": 3}}
    if status == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    return "response." + status, {"response": response}


class BlockLifecycle:
    """Shared incremental validator: one open block, monotone starts, no late delta."""
    def __init__(self, test):
        self.test = test
        self.events = []
        self.blocks = []
        self.active = None
        self.terminal = None

    def consume(self, events):
        for event in events:
            t = event["type"]
            self.test.assertIsNone(self.terminal, "event after terminal output")
            if t == "content_block_start":
                self.test.assertIsNone(self.active, "overlapping output blocks")
                self.test.assertEqual(event["index"], len(self.blocks))
                self.active = event["index"]
                self.blocks.append({**event["content_block"], "fragments": [], "stops": 0})
            elif t == "content_block_delta":
                self.test.assertEqual(event["index"], self.active, "delta after stop / wrong block")
                delta = event["delta"]
                block = self.blocks[self.active]
                key, kind = {"tool_use": ("partial_json", "input_json_delta"),
                             "text": ("text", "text_delta"),
                             "thinking": ("thinking", "thinking_delta")}[block["type"]]
                self.test.assertEqual(delta["type"], kind)
                block["fragments"].append(delta[key])
            elif t == "content_block_stop":
                self.test.assertEqual(event["index"], self.active, "duplicate / wrong stop")
                self.blocks[self.active]["stops"] += 1
                self.active = None
            elif t == "message_stop":
                self.test.assertIsNone(self.active, "successful terminal with open block")
                self.test.assertTrue(all(b["stops"] == 1 for b in self.blocks))
                self.test.assertEqual(sum(e["type"] == "message_delta" for e in self.events), 1)
                self.terminal = t
            elif t == "error":
                self.terminal = t
            self.events.append(event)
        return events

    def tools(self):
        return [b for b in self.blocks if b["type"] == "tool_use"]

    def normalized(self):
        result = []
        for block in self.blocks:
            text = "".join(block["fragments"])
            if block["type"] == "tool_use":
                result.append({"type": "tool_use", "id": block["id"],
                               "name": block["name"], "input": json.loads(text)})
            else:
                key = "text" if block["type"] == "text" else "thinking"
                result.append({"type": block["type"], key: text})
        return result


class SyntheticCase(unittest.TestCase):
    def setUp(self):
        self.life = BlockLifecycle(self)
        self.socket_patch = patch("socket.socket", side_effect=AssertionError("synthetic test: no sockets"))
        self.http_patch = patch("urllib.request.urlopen", side_effect=AssertionError("synthetic test: no HTTP"))
        self.socket_patch.start()
        self.http_patch.start()
        self.addCleanup(self.socket_patch.stop)
        self.addCleanup(self.http_patch.stop)

    def feed_chat(self, adapter, *chunks):
        for chunk in chunks:
            self.life.consume(adapter.feed(chunk))

    def feed_responses(self, adapter, *events):
        for event in events:
            self.life.consume(adapter.feed(*event))

    def finish_chat(self, adapter, finish="tool_calls"):
        self.feed_chat(adapter, chat(finish=finish))
        self.life.consume(adapter.finalize())
        self.assertEqual(adapter.finalize(), [])

    def assert_failed(self, adapter):
        self.assertEqual(self.life.terminal, "error")
        self.assertEqual(adapter.terminal_state().status, pt.TerminalStatus.FAILED)
        self.assertEqual(sum(e["type"] == "error" for e in self.life.events), 1)
        self.assertFalse(any(e["type"] == "message_stop" for e in self.life.events))
        self.assertEqual(adapter.finalize(), [])
        self.assertEqual(adapter.finalize(), [])


class TestChatAssociation(SyntheticCase):
    def test_chat_interleaved_red(self):
        adapter = pt.OpenAIToAnthropicStreamAdapter()
        self.feed_chat(adapter, chat({"tool_calls": [call(0, '{"a":', "ca", "a"),
                                                      call(7, '{"b":', "cb", "b")]}))
        self.assertEqual(self.life.tools()[0]["fragments"], ['{"a":'])
        self.feed_chat(adapter, chat({"tool_calls": [call(7, '2}'), call(0, '1}')]}))
        self.finish_chat(adapter)
        self.assertEqual([b["fragments"] for b in self.life.tools()],
                         [['{"a":', '1}'], ['{"b":', '2}']])


class TestResponsesAssociation(SyntheticCase):
    def test_responses_interleaved_red(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('{"a":'), added("ib", 7, "cb", "b"))
        self.feed_responses(adapter, argument('1}'), argument('{"b":2}', "ib", 7),
                            argument('{"a":1}', done=True), item_done(), completed())
        self.assertEqual([b["fragments"] for b in self.life.tools()],
                         [['{"a":', '1}'], ['{"b":2}']])


class TestChatBoundaries(SyntheticCase):
    def test_three_nonconsecutive_indexes_same_frame_and_repeated_fragments(self):
        adapter = pt.OpenAIToAnthropicStreamAdapter()
        self.feed_chat(adapter, chat({"tool_calls": [call(0, '{"a":"', "ca", "a"),
                                                      call(7, '{"b":', "cb", "b"),
                                                      call(19, '{"c":', "cc", "c")]}))
        self.assertEqual([b["id"] for b in self.life.tools()], ["ca"])
        self.assertEqual(adapter.openai_index_to_anthropic_index, {0: 0})
        for values in ([call(19, '3}'), call(0, 'x'), call(7, '2}')],
                       [call(0, 'x')], [call(0, '"}')]):
            self.feed_chat(adapter, chat({"tool_calls": values}))
            self.assertEqual(len(self.life.tools()), 1)
            self.assertEqual(self.life.tools()[0]["stops"], 0)
        self.finish_chat(adapter)
        self.assertEqual([b["fragments"] for b in self.life.tools()],
                         [['{"a":"', 'x', 'x', '"}'], ['{"b":', '2}'], ['{"c":', '3}']])
        self.assertEqual(adapter.openai_index_to_anthropic_index, {0: 0, 7: 1, 19: 2})

    def test_first_seen_index_order_not_numeric_sort(self):
        adapter = pt.OpenAIToAnthropicStreamAdapter()
        self.feed_chat(adapter, chat({"tool_calls": [call(7, '{}', "cb", "b"), call(0, '{}', "ca", "a")]}))
        self.finish_chat(adapter)
        self.assertEqual([b["id"] for b in self.life.tools()], ["cb", "ca"])

    def test_tool_text_tool_queue_preserves_groups(self):
        adapter = pt.OpenAIToAnthropicStreamAdapter()
        self.feed_chat(adapter, chat({"content": "before"}),
                       chat({"tool_calls": [call(0, '{"a":', "ca", "a")]}),
                       chat({"content": "after"}), chat({"content": "!"}),
                       chat({"tool_calls": [call(7, '{}', "cb", "b"), call(0, '1}')]}))
        self.assertEqual([b["type"] for b in self.life.blocks], ["text", "tool_use"])
        self.assertEqual(self.life.tools()[0]["fragments"], ['{"a":', '1}'])
        self.finish_chat(adapter)
        self.assertEqual([b["type"] for b in self.life.blocks], ["text", "tool_use", "text", "tool_use"])
        self.assertEqual(self.life.blocks[2]["fragments"], ["after", "!"])

    def test_tool_then_thinking_then_text_does_not_stop_tool(self):
        adapter = pt.OpenAIToAnthropicStreamAdapter()
        self.feed_chat(adapter, chat({"tool_calls": [call(0, '{"a":', "ca", "a")]}),
                       chat({"reasoning_content": "思考"}), chat({"content": "answer"}),
                       chat({"tool_calls": [call(0, '1}')]}))
        self.assertEqual([b["type"] for b in self.life.blocks], ["tool_use"])
        self.finish_chat(adapter, "stop")
        self.assertEqual([b["type"] for b in self.life.blocks], ["tool_use", "thinking", "text"])
        self.assertEqual(self.life.blocks[1]["fragments"], ["思考"])
        self.assertEqual(self.life.events[-2]["delta"]["stop_reason"], "end_turn")

    def test_missing_id_generated_once_and_name_restored(self):
        with patch.object(pt, "gen_toolu_id", return_value="generated") as generate:
            adapter = pt.OpenAIToAnthropicStreamAdapter({"tool_name_mapping": {"short": "long-name"}})
            self.feed_chat(adapter, chat({"tool_calls": [call(0, '{"a":', name="short")]}),
                           chat({"tool_calls": [call(0, '1}')]}))
            self.finish_chat(adapter)
            self.assertEqual(generate.call_count, 1)
            self.assertEqual(self.life.tools()[0]["id"], "generated")
            self.assertEqual(self.life.tools()[0]["name"], "long-name")

    def test_duplicate_client_ids_are_distinct_calls(self):
        adapter = pt.OpenAIToAnthropicStreamAdapter()
        self.feed_chat(adapter, chat({"tool_calls": [call(0, '{}', "same", "a"), call(7, '[]', "same", "b")]}))
        self.finish_chat(adapter)
        self.assertEqual([b["id"] for b in self.life.tools()], ["same", "same"])
        self.assertEqual([b["fragments"] for b in self.life.tools()], [['{}'], ['[]']])

    def test_finish_frame_fragment_and_trailing_usage_no_early_close(self):
        adapter = pt.OpenAIToAnthropicStreamAdapter()
        self.feed_chat(adapter, chat({"tool_calls": [call(0, '{"a":', "ca", "a"), call(7, '{}', "cb", "b")]}),
                       chat({"tool_calls": [call(0, '1}')]}, "tool_calls"))
        # The finish reason has already been mapped into the Anthropic domain.
        self.assertEqual(adapter.terminal_state(),
                         pt.TerminalState(pt.TerminalStatus.COMPLETED, "tool_use"))
        self.assertEqual(len(self.life.tools()), 1)
        self.assertEqual(self.life.tools()[0]["stops"], 0)
        self.feed_chat(adapter, {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 17,
                                                         "completion_tokens_details": {"reasoning_tokens": 5}}})
        self.life.consume(adapter.finalize())
        self.assertEqual(adapter.usage_tuple(), (11, 17, 5))
        self.assertEqual(self.life.events[-2]["usage"], {"input_tokens": 11, "output_tokens": 17})
        self.assertEqual(adapter.finalize(), [])
        self.assertEqual(adapter.feed(chat({"tool_calls": [call(0, "late")]})), [])

    def test_missing_finish_discards_pending_and_emits_one_error(self):
        adapter = pt.OpenAIToAnthropicStreamAdapter()
        self.feed_chat(adapter, chat({"tool_calls": [call(0, '{', "ca", "a"), call(7, '{}', "cb", "b")]}))
        self.life.consume(adapter.finalize())
        self.assert_failed(adapter)
        self.assertEqual(len(self.life.tools()), 1)
        self.assertEqual(adapter._blocks.queue, [])
        self.assertEqual(adapter._tools[7]["fragments"], [])
        self.assertEqual(adapter.feed(chat(finish="tool_calls")), [])

    def test_upstream_error_discards_pending_after_finish(self):
        adapter = pt.OpenAIToAnthropicStreamAdapter()
        self.feed_chat(adapter, chat({"tool_calls": [call(0, '{}', "ca", "a"), call(7, '{}', "cb", "b")]}, "tool_calls"),
                       {"error": {"type": "server_error", "message": "synthetic failure"}})
        self.assert_failed(adapter)
        self.assertEqual(len(self.life.tools()), 1)
        self.assertEqual(adapter._tools[7]["fragments"], [])

    def test_missing_index_explicit_error_not_current_tool(self):
        adapter = pt.OpenAIToAnthropicStreamAdapter()
        self.feed_chat(adapter, chat({"tool_calls": [call(0, '{}', "ca", "a")]}),
                       chat({"tool_calls": [{"function": {"arguments": "bad"}}]}))
        self.assert_failed(adapter)
        self.assertEqual(self.life.tools()[0]["fragments"], ['{}'])

    def test_invalid_index_types_explicit_error(self):
        for index in (None, True, -1, "0", 1.5):
            with self.subTest(index=index):
                life = BlockLifecycle(self)
                adapter = pt.OpenAIToAnthropicStreamAdapter()
                life.consume(adapter.feed(chat({"tool_calls": [call(index, '{}', "ca", "a")]})))
                self.assertEqual(life.terminal, "error")
                self.assertEqual(adapter.finalize(), [])

    def test_conflicting_id_or_name_at_same_index_explicit_error(self):
        for extra in (call(0, 'bad', "other"), call(0, 'bad', name="other")):
            with self.subTest(extra=extra):
                adapter = pt.OpenAIToAnthropicStreamAdapter()
                life = BlockLifecycle(self)
                life.consume(adapter.feed(chat({"tool_calls": [call(0, '{}', "ca", "a"), extra]})))
                self.assertEqual(life.terminal, "error")
                self.assertEqual(adapter.finalize(), [])

    def test_special_and_invalid_arguments_are_raw_not_normalized(self):
        raw = json.dumps({"spaces": "a  b,c\\\"中文", "nested": [{}, []]}, ensure_ascii=False)
        for value in (raw, '{"broken":', '42', '[]'):
            with self.subTest(value=value):
                adapter = pt.OpenAIToAnthropicStreamAdapter()
                life = BlockLifecycle(self)
                fragments = [value[:2], "", value[2:]]
                for n, fragment in enumerate(fragments):
                    life.consume(adapter.feed(chat({"tool_calls": [call(0, fragment, "ca" if n == 0 else None,
                                                                          "a" if n == 0 else None)]})))
                life.consume(adapter.feed(chat(finish="tool_calls")))
                life.consume(adapter.finalize())
                self.assertEqual(life.tools()[0]["fragments"], [f for f in fragments if f])

    def test_serial_stream_nonstream_block_order_and_tools_match(self):
        ctx = {"tool_name_mapping": {"short": "long-name"}, "request_model": "synthetic"}
        adapter = pt.OpenAIToAnthropicStreamAdapter(ctx)
        args_a = '{"value":"a  b,c\\\\中文", "nested":[{},[]]}'
        args_b = '{"n":2}'
        self.feed_chat(adapter, chat({"reasoning_content": "reason"}), chat({"content": "hello"}),
                       chat({"tool_calls": [call(0, args_a[:8], "ca", "short"), call(7, args_b, "cb", "b")]}),
                       chat({"tool_calls": [call(0, args_a[8:])] }))
        self.finish_chat(adapter)
        expected = pt.openai_to_anthropic_response({"choices": [{"message": {
            "role": "assistant", "reasoning_content": "reason", "content": "hello", "tool_calls": [
                {"id": "ca", "function": {"name": "short", "arguments": args_a}},
                {"id": "cb", "function": {"name": "b", "arguments": args_b}}]},
            "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 2, "completion_tokens": 3}}, ctx)
        self.assertEqual(self.life.normalized(), expected["content"])
        self.assertEqual(self.life.events[-2]["delta"]["stop_reason"], expected["stop_reason"])

    def test_reasoning_after_first_mirror_is_not_remapped(self):
        adapter = pt.OpenAIToAnthropicStreamAdapter()
        self.feed_chat(adapter, chat({"reasoning_content": "first"}), chat({"content": "text"}),
                       chat({"reasoning_content": "late"}), chat({"tool_calls": [call(0, '{}', "ca", "a")]}))
        self.finish_chat(adapter)
        self.assertEqual([b["fragments"] for b in self.life.blocks], [["first"], ["text"], ['{}']])


class TestResponsesBoundaries(SyntheticCase):
    def test_two_preadded_calls_bind_in_reverse_order_without_crossing_gate(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, argument('{"a":'), argument('{}', "ib", 7),
                            added("ib", 7, "cb", "b"), item_done("ib", 7))
        self.assertEqual(self.life.blocks, [])
        self.feed_responses(adapter, added(), argument('1}'), item_done(), completed())
        self.assertEqual([b["id"] for b in self.life.tools()], ["ca", "cb"])
        self.assertEqual([b["fragments"] for b in self.life.tools()], [['{"a":', '1}'], ['{}']])

    def test_first_tool_keeps_streaming_while_unrelated_preadded_event_waits(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('{"a":'),
                            argument('{}', "ib", 7), argument('1}'))
        self.assertEqual(self.life.tools()[0]["fragments"], ['{"a":', '1}'])
        self.feed_responses(adapter, added("ib", 7, "cb", "b"), item_done(), completed())
        self.assertEqual([b["fragments"] for b in self.life.tools()], [['{"a":', '1}'], ['{}']])

    def test_done_events_use_item_only_index_only_and_consistent_dual_keys(self):
        for item_id, index in (("ia", None), (None, 0), ("ia", 0)):
            with self.subTest(item_id=item_id, index=index):
                adapter = pt.ResponsesToAnthropicStreamAdapter()
                life = BlockLifecycle(self)
                for event in (added(), added("ib", 7, "cb", "b"), argument('{}'),
                              argument('snapshot', item_id, index, done=True)):
                    life.consume(adapter.feed(*event))
                self.assertEqual(life.tools()[0]["stops"], 0)
                life.consume(adapter.feed(*item_done(item_id, index)))
                self.assertEqual([b["stops"] for b in life.tools()], [1, 0])
                life.consume(adapter.feed(*argument('{}', "ib", 7)))
                life.consume(adapter.feed(*completed()))
                self.assertEqual([b["fragments"] for b in life.tools()], [['{}'], ['{}']])

    def test_both_done_event_types_reject_crossed_keys(self):
        for event in (argument('snapshot', "ia", 7, done=True), item_done("ia", 7)):
            with self.subTest(event=event):
                adapter = pt.ResponsesToAnthropicStreamAdapter()
                life = BlockLifecycle(self)
                for incoming in (added(), added("ib", 7, "cb", "b"), event):
                    life.consume(adapter.feed(*incoming))
                self.assertEqual(life.terminal, "error")
                self.assertEqual(adapter.finalize(), [])

    def test_three_tools_and_repeated_fragments(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('{"a":"'), added("ib", 7, "cb", "b"),
                            added("ic", 19, "cc", "c"), argument('{}', "ic", 19),
                            argument('{"b":2}', "ib", 7), argument('x'), argument('x'), argument('"}'))
        self.assertEqual([b["id"] for b in self.life.tools()], ["ca"])
        self.feed_responses(adapter, completed())
        self.assertEqual([b["fragments"] for b in self.life.tools()],
                         [['{"a":"', 'x', 'x', '"}'], ['{"b":2}'], ['{}']])

    def test_item_only_index_only_and_consistent_dual_keys(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), added("ib", 7, "cb", "b"),
                            argument('{"a":', output_index=None), argument('1', item_id=None),
                            argument('}'), argument('{}', "ib", 7), completed())
        self.assertEqual([b["fragments"] for b in self.life.tools()], [['{"a":', '1', '}'], ['{}']])

    def test_crossed_known_keys_fail_immediately(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), added("ib", 7, "cb", "b"), argument('bad', "ia", 7))
        self.assert_failed(adapter)
        self.assertEqual(self.life.tools()[0]["fragments"], [])
        self.assertEqual(adapter._pending_arguments, [])
        self.assertEqual(adapter._blocks.queue, [])

    def test_added_conflicting_keys_id_name_and_index(self):
        for event in (added("ia", 7), added("ib", 0), added(call_id="different"), added(name="different")):
            with self.subTest(event=event):
                adapter = pt.ResponsesToAnthropicStreamAdapter()
                life = BlockLifecycle(self)
                life.consume(adapter.feed(*added()))
                life.consume(adapter.feed(*event))
                self.assertEqual(life.terminal, "error")
                self.assertEqual(adapter.finalize(), [])

    def test_duplicate_consistent_added_and_done_are_idempotent(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), added(), argument('{}'), argument('{}', done=True),
                            argument('{}', done=True), item_done(), item_done(), added(), completed())
        self.assertEqual(len(self.life.tools()), 1)
        self.assertEqual(self.life.tools()[0]["fragments"], ['{}'])
        self.assertEqual(self.life.tools()[0]["stops"], 1)

    def test_added_partial_key_can_fill_other_key_without_reopening(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(output_index=None), argument('{', output_index=None),
                            added(), argument('}', item_id=None), completed())
        self.assertEqual(len(self.life.tools()), 1)
        self.assertEqual(self.life.tools()[0]["fragments"], ['{', '}'])

    def test_late_added_binds_each_key_form_and_keeps_fragment_order(self):
        for item_id, index in (("ia", None), (None, 0), ("ia", 0)):
            with self.subTest(item_id=item_id, index=index):
                adapter = pt.ResponsesToAnthropicStreamAdapter()
                life = BlockLifecycle(self)
                life.consume(adapter.feed(*argument('{"a":', item_id, index)))
                self.assertEqual(life.blocks, [])
                life.consume(adapter.feed(*argument('1}', item_id, index)))
                life.consume(adapter.feed(*added()))
                self.assertEqual(life.tools()[0]["fragments"], ['{"a":', '1}'])
                life.consume(adapter.feed(*completed()))

    def test_late_added_preserves_first_logical_order_before_other_tool_and_text(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, argument('{"a":'), added("ib", 7, "cb", "b"),
                            argument('{}', "ib", 7), ("response.output_text.delta", {"delta": "after"}))
        self.assertEqual(self.life.blocks, [])
        self.feed_responses(adapter, added(), argument('1}'), item_done(), completed())
        self.assertEqual([b["type"] for b in self.life.blocks], ["tool_use", "tool_use", "text"])
        self.assertEqual([b["id"] for b in self.life.tools()], ["ca", "cb"])
        self.assertEqual(self.life.tools()[0]["fragments"], ['{"a":', '1}'])

    def test_pending_partially_known_keys_do_not_overtake_first_fragment(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(output_index=None), argument('{"a":'),
                            argument('1}', output_index=None))
        self.assertEqual(self.life.tools()[0]["fragments"], [])
        self.feed_responses(adapter, added(), completed())
        self.assertEqual(self.life.tools()[0]["fragments"], ['{"a":', '1}'])

    def test_two_partial_pending_key_forms_bind_same_added(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, argument('{"a":', output_index=None), argument('1}', item_id=None),
                            added(), completed())
        self.assertEqual(len(self.life.tools()), 1)
        self.assertEqual(self.life.tools()[0]["fragments"], ['{"a":', '1}'])

    def test_unknown_item_not_routed_to_current_tool_and_terminal_fails(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('{'), argument('bad', "unknown", None))
        self.assertEqual(self.life.tools()[0]["fragments"], ['{'])
        self.feed_responses(adapter, completed())
        self.assert_failed(adapter)
        self.assertEqual(self.life.tools()[0]["fragments"], ['{'])
        self.assertEqual(adapter._blocks.queue, [])
        self.assertEqual(adapter._pending_arguments, [])

    def test_unbound_preadded_event_terminal_is_error(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, argument('{}'), completed())
        self.assert_failed(adapter)
        self.assertEqual(self.life.blocks, [])

    def test_late_added_cross_binding_fails_not_falls_back(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('bad', "ia", 7), added("ib", 7, "cb", "b"))
        self.assert_failed(adapter)
        self.assertEqual(self.life.tools()[0]["fragments"], [])

    def test_no_identifiers_waits_for_terminal_and_blocks_later_same_call(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('{"a":', None, None), argument('1}'),
                            argument('{"a":1}', done=True), item_done())
        self.assertEqual(self.life.tools()[0]["fragments"], [])
        self.assertEqual(self.life.tools()[0]["stops"], 0)
        self.feed_responses(adapter, completed())
        self.assertEqual(self.life.tools()[0]["fragments"], ['{"a":', '1}'])

    def test_no_identifiers_with_no_candidate_is_error(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, argument('{}', None, None))
        self.assert_failed(adapter)

    def test_no_identifiers_after_multiple_tools_is_error(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), added("ib", 7, "cb", "b"), argument('{}', None, None))
        self.assert_failed(adapter)

    def test_second_tool_after_unidentified_fragment_is_error(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('unknown', None, None), added("ib", 7, "cb", "b"))
        self.assert_failed(adapter)
        self.assertEqual(self.life.tools()[0]["fragments"], [])

    def test_call_id_is_not_an_item_key(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('bad', "ca", None), completed())
        self.assert_failed(adapter)
        self.assertEqual(self.life.tools()[0]["fragments"], [])

    def test_arguments_done_only_seals_item_done_closes_corresponding_tool(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('{}'), added("ib", 7, "cb", "b"),
                            argument('{}', "ib", 7), argument('snapshot', done=True))
        self.assertEqual(self.life.tools()[0]["stops"], 0)
        self.assertEqual(len(self.life.tools()), 1)
        self.feed_responses(adapter, item_done())
        self.assertEqual(self.life.tools()[0]["stops"], 1)
        self.assertEqual([b["id"] for b in self.life.tools()], ["ca", "cb"])
        self.assertEqual(self.life.tools()[1]["stops"], 0)
        self.assertIsNone(self.life.terminal)
        self.feed_responses(adapter, item_done("ib", 7), completed())
        self.assertEqual([b["fragments"] for b in self.life.tools()], [['{}'], ['{}']])

    def test_later_tool_done_first_waits_then_drains_and_opens_unfinished(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), added("ib", 7, "cb", "b"), added("ic", 19, "cc", "c"),
                            argument('{}', "ib", 7), item_done("ib", 7), argument('{}'))
        self.assertEqual(len(self.life.tools()), 1)
        self.feed_responses(adapter, item_done())
        self.assertEqual([b["stops"] for b in self.life.tools()], [1, 1, 0])
        self.feed_responses(adapter, argument('{"c":3}', "ic", 19))
        self.assertEqual(self.life.tools()[2]["fragments"], ['{"c":3}'])
        self.feed_responses(adapter, completed())

    def test_late_nonempty_delta_after_argument_or_item_done_is_error(self):
        for done_event in (argument('{}', done=True), item_done()):
            with self.subTest(done=done_event):
                adapter = pt.ResponsesToAnthropicStreamAdapter()
                life = BlockLifecycle(self)
                for event in (added(), argument('{}'), added("ib", 7, "cb", "b"), done_event,
                              argument('late')):
                    life.consume(adapter.feed(*event))
                self.assertEqual(life.terminal, "error")
                self.assertEqual(life.tools()[0]["fragments"], ['{}'])
                self.assertEqual(adapter.finalize(), [])

    def test_empty_delta_after_done_is_not_nonempty_lifecycle_violation(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('{}'), item_done(), argument(''), completed())
        self.assertEqual(self.life.tools()[0]["fragments"], ['{}'])

    def test_no_done_snapshot_replay_even_without_delta(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('{"snapshot":1}', done=True),
                            item_done(snapshot='{"snapshot":1}'), completed())
        self.assertEqual(self.life.tools()[0]["fragments"], [])

    def test_preadded_done_binds_and_seals_without_snapshot(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, argument('snapshot', done=True), item_done(), added(), completed())
        self.assertEqual(self.life.tools()[0]["fragments"], [])
        self.assertEqual(self.life.tools()[0]["stops"], 1)

    def test_partial_incomplete_drains_pending_and_preserves_usage(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('{"a":'), added("ib", 7, "cb", "b"),
                            argument('{"b":', "ib", 7), completed("incomplete", {
                                "input_tokens": 11, "output_tokens": 17,
                                "output_tokens_details": {"reasoning_tokens": 5}}))
        self.assertEqual([b["fragments"] for b in self.life.tools()], [['{"a":'], ['{"b":']])
        self.assertEqual(self.life.events[-2]["delta"]["stop_reason"], "max_tokens")
        self.assertEqual(adapter.terminal_state().status, pt.TerminalStatus.INCOMPLETE)
        self.assertEqual(adapter.usage_tuple(), (11, 17, 5))

    def test_failed_upstream_and_eof_discard_queued_tools(self):
        for terminal in (None, ("response.failed", {"response": {"error": {
                "type": "server_error", "message": "synthetic failure"}}}),
                ("error", {"error": {"type": "server_error", "message": "synthetic failure"}})):
            with self.subTest(terminal=terminal):
                adapter = pt.ResponsesToAnthropicStreamAdapter()
                life = BlockLifecycle(self)
                for event in (added(), argument('{'), added("ib", 7, "cb", "b"), argument('{}', "ib", 7)):
                    life.consume(adapter.feed(*event))
                life.consume(adapter.finalize() if terminal is None else adapter.feed(*terminal))
                self.assertEqual(life.terminal, "error")
                self.assertEqual(len(life.tools()), 1)
                self.assertEqual(adapter._tools[1]["fragments"], [])
                self.assertEqual(adapter._pending_arguments, [])
                self.assertEqual(adapter.finalize(), [])
                self.assertEqual(adapter.feed(*completed()), [])

    def test_completed_redundant_tail_events_and_finalize_are_ignored(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('{}'), completed())
        for event in (argument('late'), argument('snapshot', done=True), item_done(), added(),
                      completed(), ("error", {"error": {"message": "late"}})):
            self.assertEqual(adapter.feed(*event), [])
        self.assertEqual(adapter.finalize(), [])
        self.assertEqual(adapter.finalize(), [])
        self.assertEqual(adapter.terminal_state().status, pt.TerminalStatus.COMPLETED)

    def test_missing_client_id_generated_once_duplicate_call_ids_not_merged(self):
        with patch.object(pt, "gen_toolu_id", return_value="generated") as generate:
            adapter = pt.ResponsesToAnthropicStreamAdapter({"tool_name_mapping": {"a": "restored"}})
            self.feed_responses(adapter, added(call_id=None), added(call_id=None), argument('{}'),
                                added("ib", 7, "same", "b"), added("ic", 19, "same", "c"),
                                argument('{}', "ib", 7), argument('{}', "ic", 19), completed())
            self.assertEqual(generate.call_count, 1)
            self.assertEqual([b["id"] for b in self.life.tools()], ["generated", "same", "same"])
            self.assertEqual(self.life.tools()[0]["name"], "restored")

    def test_tool_then_thinking_text_tool_queues_original_groups_and_stop_reason(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('{"a":'),
                            ("response.output_item.added", {"item": {"type": "reasoning", "id": "r"}}),
                            ("response.reasoning_summary_text.delta", {"delta": "think"}),
                            ("response.reasoning_summary_text.done", {}),
                            ("response.output_item.added", {"item": {"type": "message", "id": "m"}}),
                            ("response.output_text.delta", {"delta": "text"}),
                            ("response.output_text.delta", {"delta": "!"}),
                            added("ib", 7, "cb", "b"), argument('{}', "ib", 7), argument('1}'))
        self.assertEqual([b["type"] for b in self.life.blocks], ["tool_use"])
        self.feed_responses(adapter, item_done())
        self.assertEqual([b["type"] for b in self.life.blocks], ["tool_use", "thinking", "text", "tool_use"])
        self.assertEqual(self.life.blocks[1]["fragments"], ["think"])
        self.assertEqual(self.life.blocks[2]["fragments"], ["text", "!"])
        self.feed_responses(adapter, completed())
        self.assertEqual(self.life.events[-2]["delta"]["stop_reason"], "tool_use")

    def test_tool_then_text_preserves_response_tool_handoff(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('{}'), ("response.output_text.delta", {"delta": "tail"}))
        self.assertEqual(adapter.cur_type, "tool_use")
        self.feed_responses(adapter, completed())
        self.assertEqual([b["type"] for b in self.life.blocks], ["tool_use", "text"])
        self.assertEqual(self.life.events[-2]["delta"]["stop_reason"], "tool_use")

    def test_completed_tool_handoff_matches_nonstream_for_mixed_output(self):
        tool_a = {"type": "function_call", "call_id": "ca", "name": "a", "arguments": '{"x":1}'}
        tool_b = {"type": "function_call", "call_id": "cb", "name": "b", "arguments": '{"y":2}'}
        text = {"type": "message", "content": [{"type": "output_text", "text": "tail!"}]}
        thinking = {"type": "reasoning", "summary": [{"type": "summary_text", "text": "think"}]}
        tool_a_events = [added(), argument('{"x":1}'), argument('{"x":1}', done=True), item_done()]
        tool_b_events = [added("ib", 7, "cb", "b"), argument('{"y":2}', "ib", 7),
                         item_done("ib", 7)]
        text_events = [("response.output_item.added", {"item": {"type": "message"}}),
                       ("response.output_text.delta", {"delta": "tail"}),
                       ("response.output_text.delta", {"delta": "!"})]
        thinking_events = [("response.output_item.added", {"item": {"type": "reasoning"}}),
                           ("response.reasoning_summary_text.delta", {"delta": "think"}),
                           ("response.reasoning_summary_text.done", {})]
        cases = [
            ([tool_a, text], tool_a_events + text_events),
            ([tool_a, thinking], tool_a_events + thinking_events),
            ([text, tool_a, text], text_events + tool_a_events + text_events),
            ([tool_a, tool_b, text], tool_a_events + tool_b_events + text_events),
        ]
        ctx = {"tool_name_mapping": {"a": "restored"}, "request_model": "synthetic"}
        for output, incoming in cases:
            with self.subTest(output=output):
                adapter = pt.ResponsesToAnthropicStreamAdapter(ctx)
                life = BlockLifecycle(self)
                for event in incoming + [completed()]:
                    life.consume(adapter.feed(*event))
                expected = pt.responses_to_anthropic_response(
                    {"status": "completed", "output": output}, ctx)
                self.assertEqual(life.normalized(), expected["content"])
                self.assertEqual(life.events[-2]["delta"]["stop_reason"], "tool_use")
                self.assertEqual(life.events[-2]["delta"]["stop_reason"], expected["stop_reason"])
                self.assertEqual(adapter.feed(*completed()), [])
                self.assertEqual(adapter.finalize(), [])
                self.assertEqual(adapter.finalize(), [])

    def test_completed_without_tools_stays_end_turn(self):
        for event in (("response.output_text.delta", {"delta": "text"}),
                      ("response.reasoning_text.delta", {"delta": "thinking"})):
            with self.subTest(event=event):
                adapter = pt.ResponsesToAnthropicStreamAdapter()
                life = BlockLifecycle(self)
                for incoming in (event, completed()):
                    life.consume(adapter.feed(*incoming))
                self.assertEqual(life.events[-2]["delta"]["stop_reason"], "end_turn")
                self.assertEqual(life.terminal, "message_stop")

    def test_explicit_stop_reason_is_not_overridden_by_tool(self):
        # Exercise the existing priority branch; this does not add Responses refusal support.
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('{}'))
        adapter.final_stop_reason = "refusal"
        self.feed_responses(adapter, completed())
        self.assertEqual(self.life.events[-2]["delta"]["stop_reason"], "refusal")
        self.assertEqual(adapter.terminal_state().status, pt.TerminalStatus.REFUSED)

    def test_tool_does_not_override_incomplete_or_failure(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('{}'), completed("incomplete"))
        self.assertEqual(self.life.events[-2]["delta"]["stop_reason"], "max_tokens")
        self.assertEqual(adapter.terminal_state().status, pt.TerminalStatus.INCOMPLETE)
        for terminal in (("response.failed", {"response": {"error": {"message": "bad"}}}),
                         argument('{}', "unknown", 0)):
            with self.subTest(terminal=terminal):
                adapter = pt.ResponsesToAnthropicStreamAdapter()
                life = BlockLifecycle(self)
                for event in (added(), argument('{}'), terminal, completed()):
                    life.consume(adapter.feed(*event))
                self.assertEqual(life.terminal, "error")
                self.assertNotIn("message_stop", [e["type"] for e in life.events])
                self.assertEqual(adapter.finalize(), [])
        for terminal in (("response.completed", {"response": {"status": "future"}}),
                         ("response.refusal.delta", {"delta": "refused"})):
            with self.subTest(terminal=terminal):
                adapter = pt.ResponsesToAnthropicStreamAdapter()
                life = BlockLifecycle(self)
                for event in (added(), argument('{}')):
                    life.consume(adapter.feed(*event))
                with self.assertRaises(pt.TranslationError):
                    adapter.feed(*terminal)
                life.consume(adapter.finalize())
                self.assertEqual(life.terminal, "error")
                self.assertNotIn("message_stop", [e["type"] for e in life.events])

    def test_special_and_invalid_arguments_are_not_parsed_or_deduplicated(self):
        raw = json.dumps({"text": 'a  b,c\\"中文', "nested": [{}, []]}, ensure_ascii=False)
        for value in (raw, '42', '[]', '{"broken":'):
            with self.subTest(value=value):
                adapter = pt.ResponsesToAnthropicStreamAdapter()
                life = BlockLifecycle(self)
                fragments = [value[:2], '', value[2:]]
                life.consume(adapter.feed(*added()))
                for fragment in fragments:
                    life.consume(adapter.feed(*argument(fragment)))
                life.consume(adapter.feed(*argument(value, done=True)))
                life.consume(adapter.feed(*item_done(snapshot=value)))
                life.consume(adapter.feed(*completed()))
                self.assertEqual(life.tools()[0]["fragments"], [f for f in fragments if f])

    def test_serial_stream_nonstream_tools_reasoning_and_text_match(self):
        ctx = {"tool_name_mapping": {"a": "restored"}, "request_model": "synthetic"}
        adapter = pt.ResponsesToAnthropicStreamAdapter(ctx)
        raw = json.dumps({"text": 'a  b,c\\"中文', "nested": [{}, []]}, ensure_ascii=False)
        self.feed_responses(adapter, ("response.output_item.added", {"item": {"type": "reasoning"}}),
                            ("response.reasoning_text.delta", {"delta": "think", "obfuscation": "not-thinking"}),
                            ("response.reasoning_text.done", {}),
                            ("response.output_item.added", {"item": {"type": "message"}}),
                            ("response.output_text.delta", {"delta": "hello"}),
                            added(), argument(raw[:10]), added("ib", 7, "cb", "b"),
                            argument('{}', "ib", 7), argument(raw[10:]), completed())
        expected = pt.responses_to_anthropic_response({"status": "completed", "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "think"}]},
            {"type": "message", "content": [{"type": "output_text", "text": "hello"}]},
            {"type": "function_call", "call_id": "ca", "name": "a", "arguments": raw},
            {"type": "function_call", "call_id": "cb", "name": "b", "arguments": '{}'}],
            "usage": {"input_tokens": 2, "output_tokens": 3}}, ctx)
        self.assertEqual(self.life.normalized(), expected["content"])
        self.assertEqual(self.life.events[-2]["delta"]["stop_reason"], expected["stop_reason"])

    def test_nested_item_id_and_top_level_item_id_conflict(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), ("response.output_item.done", {
            "item_id": "other", "output_index": 0, "item": {"type": "function_call", "id": "ia"}}))
        self.assert_failed(adapter)

    def test_invalid_key_types_fail_closed(self):
        for event in (argument('{}', "ia", True), argument('{}', "ia", -1),
                      argument('{}', "ia", "0"), argument('{}', 123, 0)):
            with self.subTest(event=event):
                adapter = pt.ResponsesToAnthropicStreamAdapter()
                life = BlockLifecycle(self)
                life.consume(adapter.feed(*event))
                self.assertEqual(life.terminal, "error")
                self.assertEqual(adapter.finalize(), [])

    def test_done_pending_before_unidentified_late_delta_still_fails(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('{', None, None), argument('snapshot', done=True),
                            argument('late'))
        self.assert_failed(adapter)



class TestChatLateMetadata(SyntheticCase):
    def test_missing_id_then_repeated_provided_keeps_fallback_and_arguments(self):
        with patch.object(pt, "gen_toolu_id", return_value="fallback"):
            adapter = pt.OpenAIToAnthropicStreamAdapter()
            self.feed_chat(adapter, chat({"tool_calls": [call(0, '{"a":', name="a")]}),
                           chat({"tool_calls": [call(0, '1}', "provided")]}),
                           chat({"tool_calls": [call(0, '"}', "provided")]}))
            self.finish_chat(adapter)
        self.assertEqual(self.life.tools()[0]["id"], "fallback")
        self.assertEqual(self.life.tools()[0]["fragments"], ['{"a":', '1}', '"}'])
        self.assertEqual(self.life.tools()[0]["stops"], 1)

    def test_missing_id_then_two_different_provided_values_conflicts(self):
        with patch.object(pt, "gen_toolu_id", return_value="fallback"):
            adapter = pt.OpenAIToAnthropicStreamAdapter()
            self.feed_chat(adapter, chat({"tool_calls": [call(0, '{"a":', name="a")]}),
                           chat({"tool_calls": [call(0, '1}', "provided-a")]}),
                           chat({"tool_calls": [call(0, '2}', "provided-b")]}))
        self.assert_failed(adapter)
        self.assertEqual(self.life.tools()[0]["fragments"], ['{"a":', '1}'])

    def test_queued_missing_id_uses_provided_id_before_start(self):
        adapter = pt.OpenAIToAnthropicStreamAdapter()
        self.feed_chat(adapter, chat({"tool_calls": [call(0, '{}', "ca", "a"),
                                                      call(7, '{"b":', None, "b")]}))
        self.assertEqual([b["id"] for b in self.life.tools()], ["ca"])
        self.feed_chat(adapter, chat({"tool_calls": [call(7, '}', "provided")]}))
        self.finish_chat(adapter)
        self.assertEqual([b["id"] for b in self.life.tools()], ["ca", "provided"])

    def test_missing_name_then_repeated_provided_keeps_published_name(self):
        adapter = pt.OpenAIToAnthropicStreamAdapter()
        self.feed_chat(adapter, chat({"tool_calls": [call(0, '{"a":', "ca")]}),
                       chat({"tool_calls": [call(0, '1}', "ca", "provided-name")]}),
                       chat({"tool_calls": [call(0, '"}', "ca", "provided-name")]}))
        self.finish_chat(adapter)
        self.assertEqual(self.life.tools()[0]["name"], "")
        self.assertEqual(self.life.tools()[0]["fragments"], ['{"a":', '1}', '"}'])

    def test_missing_name_then_two_different_provided_values_conflicts(self):
        adapter = pt.OpenAIToAnthropicStreamAdapter()
        self.feed_chat(adapter, chat({"tool_calls": [call(0, '{"a":', "ca")]}),
                       chat({"tool_calls": [call(0, '1}', "ca", "provided-a")]}),
                       chat({"tool_calls": [call(0, '2}', "ca", "provided-b")]}))
        self.assert_failed(adapter)
        self.assertEqual(self.life.tools()[0]["name"], "")


class TestResponsesLateMetadata(SyntheticCase):
    def test_missing_call_id_then_repeated_provided_keeps_fallback_and_arguments(self):
        with patch.object(pt, "gen_toolu_id", return_value="fallback"):
            adapter = pt.ResponsesToAnthropicStreamAdapter()
            self.feed_responses(adapter, added(call_id=None), argument('{"a":'),
                                added(call_id="provided"), argument('1}'),
                                added(call_id="provided"), argument('"}'),
                                completed())
        self.assertEqual(len(self.life.tools()), 1)
        self.assertEqual(self.life.tools()[0]["id"], "fallback")
        self.assertEqual(self.life.tools()[0]["fragments"], ['{"a":', '1}', '"}'])
        self.assertEqual(self.life.tools()[0]["stops"], 1)
        self.assertEqual(sum(e["type"] == "content_block_start" for e in self.life.events), 1)
        self.assertEqual(sum(e["type"] == "content_block_stop" for e in self.life.events), 1)

    def test_missing_call_id_then_two_different_provided_values_conflicts(self):
        with patch.object(pt, "gen_toolu_id", return_value="fallback"):
            adapter = pt.ResponsesToAnthropicStreamAdapter()
            self.feed_responses(adapter, added(call_id=None), argument('{"a":'),
                                added(call_id="provided-a"), added(call_id="provided-b"))
        self.assert_failed(adapter)
        self.assertEqual(self.life.tools()[0]["fragments"], ['{"a":'])

    def test_queued_missing_call_id_uses_provided_id_before_start(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(), argument('{}'), added("ib", 7, None, "b"))
        self.assertEqual([b["id"] for b in self.life.tools()], ["ca"])
        self.feed_responses(adapter, added("ib", 7, "provided", "b"), argument('{}', "ib", 7),
                            item_done(), completed())
        self.assertEqual([b["id"] for b in self.life.tools()], ["ca", "provided"])

    def test_missing_name_then_repeated_provided_keeps_published_name(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(name=None), argument('{"a":'),
                            added(name="provided-name"), argument('1}'),
                            added(name="provided-name"), argument('"}'), completed())
        self.assertEqual(len(self.life.tools()), 1)
        self.assertEqual(self.life.tools()[0]["name"], "")
        self.assertEqual(self.life.tools()[0]["fragments"], ['{"a":', '1}', '"}'])
        self.assertEqual(sum(e["type"] == "content_block_start" for e in self.life.events), 1)
        self.assertEqual(sum(e["type"] == "content_block_stop" for e in self.life.events), 1)

    def test_missing_name_then_two_different_provided_values_conflicts(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(adapter, added(name=None), argument('{"a":'),
                            added(name="provided-a"), added(name="provided-b"))
        self.assert_failed(adapter)
        self.assertEqual(self.life.tools()[0]["name"], "")



class TestToolMetadataMissingEquivalence(SyntheticCase):
    def test_chat_published_missing_forms_then_nonempty_are_equivalent(self):
        for missing in (_ABSENT, None, ""):
            with self.subTest(missing=missing):
                self.life = BlockLifecycle(self)
                with patch.object(pt, "gen_toolu_id", return_value="fallback"):
                    adapter = pt.OpenAIToAnthropicStreamAdapter()
                    self.feed_chat(
                        adapter,
                        chat({"tool_calls": [raw_call(0, '{"a":', missing, missing)]}),
                        chat({"tool_calls": [raw_call(0, '1}', "provided", "provided-name")]}),
                        chat({"tool_calls": [raw_call(0, '"}')]}),
                        chat({"tool_calls": [raw_call(0, 'x', "provided", "provided-name")]}),
                    )
                    self.finish_chat(adapter)
                self.assertEqual(self.life.tools()[0]["id"], "fallback")
                self.assertIn(self.life.tools()[0]["name"], ("", None))
                self.assertEqual(self.life.tools()[0]["fragments"], ['{"a":', '1}', '"}', 'x'])

    def test_chat_published_nonempty_then_missing_then_same_nonempty(self):
        adapter = pt.OpenAIToAnthropicStreamAdapter()
        self.feed_chat(
            adapter,
            chat({"tool_calls": [raw_call(0, '{"a":', "provided", "provided-name")]}),
            chat({"tool_calls": [raw_call(0, '1}')]}),
            chat({"tool_calls": [raw_call(0, '"}', "provided", "provided-name")]}),
        )
        self.finish_chat(adapter)
        self.assertEqual(self.life.tools()[0]["id"], "provided")
        self.assertEqual(self.life.tools()[0]["name"], "provided-name")
        self.assertEqual(self.life.tools()[0]["fragments"], ['{"a":', '1}', '"}'])

    def test_chat_queued_empty_then_nonempty_then_missing_then_same_uses_source_id(self):
        with patch.object(pt, "gen_toolu_id", return_value="fallback"):
            adapter = pt.OpenAIToAnthropicStreamAdapter()
            self.feed_chat(
                adapter,
                chat({"tool_calls": [raw_call(0, '{}', "ca", "a"),
                                     raw_call(7, '{"b":', "", "")]}),
                chat({"tool_calls": [raw_call(7, '1}', "provided", "provided-name")]}),
                chat({"tool_calls": [raw_call(7, '2}')]}),
                chat({"tool_calls": [raw_call(7, '3}', "provided", "provided-name")]}),
            )
            self.finish_chat(adapter)
        self.assertEqual([b["id"] for b in self.life.tools()], ["ca", "provided"])
        self.assertEqual([b["name"] for b in self.life.tools()], ["a", "provided-name"])
        self.assertEqual(self.life.tools()[1]["fragments"], ['{"b":', '1}', '2}', '3}'])

    def test_chat_queued_nonempty_then_missing_then_same_nonempty(self):
        adapter = pt.OpenAIToAnthropicStreamAdapter()
        self.feed_chat(
            adapter,
            chat({"tool_calls": [raw_call(0, '{}', "ca", "a"),
                                 raw_call(7, '{"b":', "provided", "provided-name")]}),
            chat({"tool_calls": [raw_call(7, '1}')]}),
            chat({"tool_calls": [raw_call(7, '2}', "provided", "provided-name")]}),
        )
        self.finish_chat(adapter)
        self.assertEqual([b["id"] for b in self.life.tools()], ["ca", "provided"])
        self.assertEqual([b["name"] for b in self.life.tools()], ["a", "provided-name"])
        self.assertEqual(self.life.tools()[1]["fragments"], ['{"b":', '1}', '2}'])

    def test_chat_nonempty_a_then_b_still_conflicts(self):
        for field in ("id", "name"):
            with self.subTest(field=field):
                self.life = BlockLifecycle(self)
                adapter = pt.OpenAIToAnthropicStreamAdapter()
                first = raw_call(0, '{"a":', "provided-a", "name-a")
                second = raw_call(0, '1}', "provided-b", "name-b")
                if field == "id":
                    second["function"]["name"] = "name-a"
                else:
                    second["id"] = "provided-a"
                self.feed_chat(adapter, chat({"tool_calls": [first]}),
                               chat({"tool_calls": [second]}))
                self.assert_failed(adapter)

    def test_responses_published_missing_forms_then_nonempty_are_equivalent(self):
        for missing in (_ABSENT, None, ""):
            with self.subTest(missing=missing):
                self.life = BlockLifecycle(self)
                with patch.object(pt, "gen_toolu_id", return_value="fallback"):
                    adapter = pt.ResponsesToAnthropicStreamAdapter()
                    self.feed_responses(
                        adapter,
                        raw_added(call_id=missing, name=missing),
                        argument('{"a":'),
                        raw_added(call_id="provided", name="provided-name"),
                        argument('1}'),
                        raw_added(),
                        argument('"}'),
                        raw_added(call_id="provided", name="provided-name"),
                        argument('x'),
                        completed(),
                    )
                self.assertEqual(len(self.life.tools()), 1)
                self.assertEqual(self.life.tools()[0]["id"], "fallback")
                self.assertIn(self.life.tools()[0]["name"], ("", None))
                self.assertEqual(self.life.tools()[0]["fragments"], ['{"a":', '1}', '"}', 'x'])
                self.assertEqual(sum(e["type"] == "content_block_start" for e in self.life.events), 1)
                self.assertEqual(sum(e["type"] == "content_block_stop" for e in self.life.events), 1)

    def test_responses_published_nonempty_then_missing_then_same_nonempty(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(
            adapter,
            raw_added(call_id="provided", name="provided-name"),
            argument('{"a":'),
            raw_added(),
            argument('1}'),
            raw_added(call_id="provided", name="provided-name"),
            argument('"}'),
            completed(),
        )
        self.assertEqual(self.life.tools()[0]["id"], "provided")
        self.assertEqual(self.life.tools()[0]["name"], "provided-name")
        self.assertEqual(self.life.tools()[0]["fragments"], ['{"a":', '1}', '"}'])

    def test_responses_queued_empty_then_nonempty_then_missing_then_same_uses_source_id(self):
        with patch.object(pt, "gen_toolu_id", return_value="fallback"):
            adapter = pt.ResponsesToAnthropicStreamAdapter()
            self.feed_responses(
                adapter,
                raw_added(call_id="ca", name="a"),
                argument('{}'),
                raw_added("ib", 7, "", ""),
                argument('{"b":', "ib", 7),
                raw_added("ib", 7, "provided", "provided-name"),
                argument('1}', "ib", 7),
                raw_added("ib", 7),
                argument('2}', "ib", 7),
                raw_added("ib", 7, "provided", "provided-name"),
                argument('3}', "ib", 7),
                item_done(),
                item_done("ib", 7),
                completed(),
            )
        self.assertEqual([b["id"] for b in self.life.tools()], ["ca", "provided"])
        self.assertEqual([b["name"] for b in self.life.tools()], ["a", "provided-name"])
        self.assertEqual(self.life.tools()[1]["fragments"], ['{"b":', '1}', '2}', '3}'])
        self.assertEqual(sum(e["type"] == "content_block_start" for e in self.life.events), 2)
        self.assertEqual(sum(e["type"] == "content_block_stop" for e in self.life.events), 2)

    def test_responses_queued_nonempty_then_missing_then_same_nonempty(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(
            adapter,
            raw_added(call_id="ca", name="a"),
            argument('{}'),
            raw_added("ib", 7, "provided", "provided-name"),
            argument('{"b":', "ib", 7),
            raw_added("ib", 7),
            argument('1}', "ib", 7),
            raw_added("ib", 7, "provided", "provided-name"),
            argument('2}', "ib", 7),
            item_done(),
            item_done("ib", 7),
            completed(),
        )
        self.assertEqual([b["id"] for b in self.life.tools()], ["ca", "provided"])
        self.assertEqual([b["name"] for b in self.life.tools()], ["a", "provided-name"])
        self.assertEqual(self.life.tools()[1]["fragments"], ['{"b":', '1}', '2}'])

    def test_responses_nonempty_a_then_b_still_conflicts(self):
        for field in ("call_id", "name"):
            with self.subTest(field=field):
                self.life = BlockLifecycle(self)
                adapter = pt.ResponsesToAnthropicStreamAdapter()
                first = raw_added(call_id="provided-a", name="name-a")
                second = raw_added(call_id="provided-b", name="name-b")
                if field == "call_id":
                    second[1]["item"]["name"] = "name-a"
                else:
                    second[1]["item"]["call_id"] = "provided-a"
                self.feed_responses(adapter, first, argument('{}'), second)
                self.assert_failed(adapter)


class TestResponsesDoneMetadata(SyntheticCase):
    @staticmethod
    def done(item_id="ia", output_index=0, call_id=_ABSENT, name=_ABSENT):
        event = item_done(item_id, output_index, snapshot='MUST NOT REPLAY')
        if call_id is not _ABSENT:
            event[1]["item"]["call_id"] = call_id
        if name is not _ABSENT:
            event[1]["item"]["name"] = name
        return event

    def test_queued_tool_receives_done_metadata_before_publish(self):
        a = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(a, added(), argument('{"a":'), raw_added("ib", 7),
                            argument('{"b":2}', "ib", 7),
                            self.done("ib", 7, "callB", "nameB"))
        self.assertEqual(len(self.life.tools()), 1)
        self.feed_responses(a, argument('1}'), item_done(), completed())
        self.assertEqual([(b["id"], b["name"], b["fragments"]) for b in self.life.tools()],
                         [("ca", "a", ['{"a":', '1}']), ("callB", "nameB", ['{"b":2}'])])

    def test_done_before_added_binds_metadata_and_gate_order(self):
        a = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(a, argument('{"b":2}', "ib", 7),
                            self.done("ib", 7, "callB", "nameB"), added(),
                            argument('{}'), item_done())
        self.assertEqual(self.life.blocks, [])
        self.feed_responses(a, raw_added("ib", 7), completed())
        self.assertEqual([(b["id"], b["name"], b["fragments"]) for b in self.life.tools()],
                         [("callB", "nameB", ['{"b":2}']), ("ca", "a", ['{}'])])

    def test_published_metadata_stable_but_later_conflicts_are_checked(self):
        for field in ("call_id", "name"):
            with self.subTest(field=field):
                self.life = BlockLifecycle(self)
                a = pt.ResponsesToAnthropicStreamAdapter()
                self.feed_responses(a, raw_added(), argument('{}'))
                initial = (self.life.tools()[0]["id"], self.life.tools()[0]["name"])
                self.feed_responses(a, self.done(call_id="source-id", name="source-name"))
                self.assertEqual((self.life.tools()[0]["id"], self.life.tools()[0]["name"]), initial)
                kwargs = {"call_id": "source-id", "name": "source-name"}
                kwargs[field] = "different"
                self.feed_responses(a, self.done(**kwargs))
                self.assert_failed(a)

    def test_duplicate_done_same_metadata_does_not_duplicate_output(self):
        a = pt.ResponsesToAnthropicStreamAdapter()
        d = self.done("ib", 7, "callB", "nameB")
        self.feed_responses(a, added(), argument('{}'), raw_added("ib", 7),
                            argument('{"b":2}', "ib", 7), d, d, item_done(), completed())
        self.assertEqual([(b["id"], b["name"], b["fragments"], b["stops"]) for b in self.life.tools()],
                         [("ca", "a", ['{}'], 1), ("callB", "nameB", ['{"b":2}'], 1)])

    def test_added_done_explicit_metadata_conflicts(self):
        for field in ("call_id", "name"):
            with self.subTest(field=field):
                self.life = BlockLifecycle(self)
                a = pt.ResponsesToAnthropicStreamAdapter()
                kwargs = {"call_id": "ca", "name": "a"}; kwargs[field] = "different"
                self.feed_responses(a, added(), self.done(**kwargs))
                self.assert_failed(a)

    def test_missing_none_empty_do_not_conflict_or_erase_index_zero(self):
        for missing in (_ABSENT, None, ""):
            with self.subTest(missing=missing):
                self.life = BlockLifecycle(self)
                a = pt.ResponsesToAnthropicStreamAdapter()
                self.feed_responses(a, raw_added(call_id=missing, name=missing), argument('{}'),
                                    self.done(call_id="source-id", name="source-name"),
                                    self.done(call_id=missing, name=missing), completed())
                self.assertEqual(a._tools[0]["source_id"], "source-id")
                self.assertEqual(a._tools[0]["source_name"], "source-name")
                self.assertEqual(self.life.terminal, "message_stop")
                self.assertEqual(self.life.tools()[0]["fragments"], ['{}'])

    def test_pending_multiple_calls_cannot_exchange_metadata(self):
        a = pt.ResponsesToAnthropicStreamAdapter()
        self.feed_responses(a, argument('{"b":2}', "ib", 7), self.done("ib", 7, "cb", "b"),
                            argument('{"c":3}', "ic", 19), self.done("ic", 19, "cc", "c"),
                            raw_added("ic", 19), raw_added("ib", 7), completed())
        self.assertEqual([(b["id"], b["name"], b["fragments"]) for b in self.life.tools()],
                         [("cb", "b", ['{"b":2}']), ("cc", "c", ['{"c":3}'])])


    def test_pending_done_conflicts_with_later_added(self):
        for field in ("call_id", "name"):
            with self.subTest(field=field):
                self.life = BlockLifecycle(self)
                a = pt.ResponsesToAnthropicStreamAdapter()
                kw = {"call_id": "callB", "name": "nameB"}; kw[field] = "different"
                self.feed_responses(a, argument('{}', "ib", 7),
                                    self.done("ib", 7, "callB", "nameB"),
                                    raw_added("ib", 7, **kw))
                self.assert_failed(a)
                self.assertEqual(self.life.tools(), [])

    def test_pending_done_index_zero_and_mapped_name(self):
        a = pt.ResponsesToAnthropicStreamAdapter({"tool_name_mapping": {"wire_name": "client_name"}})
        self.feed_responses(a, argument('{"x":1}', None, 0),
                            self.done(None, 0, "call-zero", "wire_name"),
                            raw_added(_ABSENT, 0), completed())
        self.assertEqual([(b["id"], b["name"], b["fragments"]) for b in self.life.tools()],
                         [("call-zero", "client_name", ['{"x":1}'])])
        self.assertEqual(a._tools[0]["source_name"], "wire_name")


if __name__ == "__main__":
    unittest.main()
