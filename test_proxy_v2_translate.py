"""proxy_v2_translate 正向转换器单测（脱网络，纯标准库 unittest）。

照正向规格 tools/model_proxy/docs/proxy_translate_spec.md §6.4 的用例：
    - 模块A 请求转换：string/array system、tool_use、tool_result、各 effort、工具名>64 截断
    - 模块B 非流式响应：纯文本 / 带 tool_calls / 各 finish_reason / arguments 非法 JSON
    - 模块C+D 流式：1 纯文本 / 2 单工具 / 3 双工具并发 / 4 arguments 断裂 / 5 缺 usage
      额外：text+tool 混合、SSE wire format、辅助函数

运行：python3 tools/test_proxy_v2_translate.py
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxy_v2_translate import (  # noqa: E402
    AnthropicStreamAdapter,
    anthropic_image_to_data_url,
    anthropic_sse_bytes,
    anthropic_to_openai_request,
    map_finish_reason,
    map_reasoning_effort,
    openai_to_anthropic_response,
    truncate_tool_name,
)


def collect(adapter, chunks, with_done=True):
    """喂一串 chunk，收集所有事件；with_done 时末尾调 finalize。"""
    events = []
    for c in chunks:
        events.extend(adapter.feed(c))
    if with_done:
        events.extend(adapter.finalize())
    return events


def types_of(events):
    return [e["type"] for e in events]


# ============================================================
# 辅助函数
# ============================================================

class TestHelpers(unittest.TestCase):

    def test_truncate_tool_name_short(self):
        self.assertEqual(truncate_tool_name("get_weather"), "get_weather")
        self.assertEqual(truncate_tool_name("a" * 64), "a" * 64)

    def test_truncate_tool_name_long(self):
        name = "x" * 100
        out = truncate_tool_name(name)
        self.assertEqual(len(out), 64)              # 55 + 1 + 8
        self.assertEqual(out[:55], "x" * 55)
        self.assertEqual(out[55], "_")
        # 确定性：同名同结果
        self.assertEqual(out, truncate_tool_name(name))

    def test_map_finish_reason(self):
        self.assertEqual(map_finish_reason("stop"), "end_turn")
        self.assertEqual(map_finish_reason("length"), "max_tokens")
        self.assertEqual(map_finish_reason("tool_calls"), "tool_use")
        self.assertEqual(map_finish_reason("content_filter"), "end_turn")
        self.assertEqual(map_finish_reason(None), "end_turn")
        self.assertEqual(map_finish_reason("weird"), "end_turn")

    def test_map_reasoning_effort_effort_field(self):
        self.assertEqual(map_reasoning_effort({"output_config": {"effort": "low"}}), "low")
        self.assertEqual(map_reasoning_effort({"output_config": {"effort": "medium"}}), "medium")
        self.assertEqual(map_reasoning_effort({"output_config": {"effort": "high"}}), "high")
        # max / xhigh 降级为 high
        self.assertEqual(map_reasoning_effort({"output_config": {"effort": "max"}}), "high")
        self.assertEqual(map_reasoning_effort({"output_config": {"effort": "xhigh"}}), "high")

    def test_map_reasoning_effort_budget(self):
        self.assertEqual(
            map_reasoning_effort({"thinking": {"type": "enabled", "budget_tokens": 1000}}), "low")
        self.assertEqual(
            map_reasoning_effort({"thinking": {"type": "enabled", "budget_tokens": 5000}}), "medium")
        self.assertEqual(
            map_reasoning_effort({"thinking": {"type": "enabled", "budget_tokens": 40000}}), "high")

    def test_map_reasoning_effort_adaptive_and_none(self):
        self.assertEqual(map_reasoning_effort({"thinking": {"type": "adaptive"}}), "medium")
        self.assertIsNone(map_reasoning_effort({}))
        self.assertIsNone(map_reasoning_effort({"thinking": None}))

    def test_image_to_data_url(self):
        self.assertEqual(
            anthropic_image_to_data_url({"type": "base64", "media_type": "image/png", "data": "AAA"}),
            "data:image/png;base64,AAA")
        self.assertEqual(
            anthropic_image_to_data_url({"type": "url", "url": "http://x/y.png"}),
            "http://x/y.png")
        self.assertIsNone(anthropic_image_to_data_url({"type": "other"}))

    def test_sse_bytes_format(self):
        ev = {"type": "ping"}
        out = anthropic_sse_bytes(ev)
        self.assertEqual(out, b'event: ping\ndata: {"type":"ping"}\n\n')
        # 紧凑无空格 + ensure_ascii=False
        ev2 = {"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "你好"}}
        out2 = anthropic_sse_bytes(ev2).decode("utf-8")
        self.assertTrue(out2.startswith("event: content_block_delta\ndata: "))
        self.assertIn("你好", out2)          # 非 ASCII 不转义
        self.assertNotIn(", ", out2)          # 紧凑分隔符
        self.assertTrue(out2.endswith("\n\n"))


# ============================================================
# 模块 A：请求转换
# ============================================================

class TestRequestTranslate(unittest.TestCase):

    def test_basic_string_system_and_message(self):
        body = {
            "model": "gpt-4o",
            "max_tokens": 128,
            "system": "you are helpful",
            "messages": [{"role": "user", "content": "hi"}],
        }
        out, ctx = anthropic_to_openai_request(body, model_is_reasoning=False)
        self.assertEqual(out["model"], "gpt-4o")
        self.assertEqual(out["max_completion_tokens"], 128)          # 改名
        self.assertNotIn("max_tokens", out)
        self.assertEqual(out["messages"][0], {"role": "system", "content": "you are helpful"})
        self.assertEqual(out["messages"][1], {"role": "user", "content": "hi"})
        self.assertEqual(ctx["request_model"], "gpt-4o")
        self.assertFalse(ctx["stream"])
        # 非 reasoning：不发 reasoning_effort
        self.assertNotIn("reasoning_effort", out)

    def test_array_system(self):
        body = {
            "system": [
                {"type": "text", "text": "line1"},
                {"type": "text", "text": "line2"},
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
        out, _ = anthropic_to_openai_request(body)
        self.assertEqual(out["messages"][0], {"role": "system", "content": "line1\nline2"})

    def test_no_system(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        out, _ = anthropic_to_openai_request(body)
        self.assertEqual(out["messages"][0]["role"], "user")

    def test_user_with_image(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}},
        ]}]}
        out, _ = anthropic_to_openai_request(body)
        msg = out["messages"][0]
        self.assertEqual(msg["role"], "user")
        self.assertIsInstance(msg["content"], list)
        self.assertEqual(msg["content"][0], {"type": "text", "text": "看图"})
        self.assertEqual(msg["content"][1],
                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}})

    def test_assistant_tool_use(self):
        body = {"messages": [{"role": "assistant", "content": [
            {"type": "text", "text": "let me check"},
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
             "input": {"location": "SF"}},
        ]}]}
        out, _ = anthropic_to_openai_request(body)
        msg = out["messages"][0]
        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(msg["content"], "let me check")
        tc = msg["tool_calls"][0]
        self.assertEqual(tc["id"], "toolu_1")
        self.assertEqual(tc["type"], "function")
        self.assertEqual(tc["function"]["name"], "get_weather")
        # input dict → arguments JSON 字符串
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"location": "SF"})

    def test_assistant_thinking_dropped(self):
        body = {"messages": [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "answer"},
        ]}]}
        out, _ = anthropic_to_openai_request(body)
        self.assertEqual(out["messages"][0]["content"], "answer")
        self.assertNotIn("tool_calls", out["messages"][0])

    def test_tool_result_string(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "72F sunny"},
            {"type": "text", "text": "谢谢"},
        ]}]}
        out, _ = anthropic_to_openai_request(body)
        # tool_result 先，normal 后（§1.3.1）
        self.assertEqual(out["messages"][0],
                         {"role": "tool", "tool_call_id": "toolu_1", "content": "72F sunny"})
        self.assertEqual(out["messages"][1], {"role": "user", "content": "谢谢"})

    def test_tool_result_single_text_block(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "result text"}]},
        ]}]}
        out, _ = anthropic_to_openai_request(body)
        self.assertEqual(out["messages"][0],
                         {"role": "tool", "tool_call_id": "t1", "content": "result text"})

    def test_tools_translate_and_truncate(self):
        long_name = "n" * 100
        body = {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"name": "get_weather", "description": "d",
                 "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}}},
                {"name": long_name, "input_schema": {"type": "object", "properties": {}}},
            ],
        }
        out, ctx = anthropic_to_openai_request(body)
        t0 = out["tools"][0]
        self.assertEqual(t0["type"], "function")
        self.assertEqual(t0["function"]["name"], "get_weather")
        self.assertEqual(t0["function"]["parameters"]["properties"]["x"]["type"], "string")
        # 截断工具进映射
        truncated = out["tools"][1]["function"]["name"]
        self.assertEqual(len(truncated), 64)
        self.assertEqual(ctx["tool_name_mapping"][truncated], long_name)
        # 未截断的不进映射
        self.assertNotIn("get_weather", ctx["tool_name_mapping"])

    def test_tool_choice_three_states(self):
        base = {"messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(
            anthropic_to_openai_request({**base, "tool_choice": {"type": "auto"}})[0]["tool_choice"],
            "auto")
        self.assertEqual(
            anthropic_to_openai_request({**base, "tool_choice": {"type": "any"}})[0]["tool_choice"],
            "required")
        self.assertEqual(
            anthropic_to_openai_request({**base, "tool_choice": {"type": "none"}})[0]["tool_choice"],
            "none")
        out = anthropic_to_openai_request(
            {**base, "tool_choice": {"type": "tool", "name": "get_weather"}})[0]
        self.assertEqual(out["tool_choice"],
                         {"type": "function", "function": {"name": "get_weather"}})

    def test_tool_choice_tool_name_truncated(self):
        long_name = "z" * 100
        out = anthropic_to_openai_request(
            {"messages": [], "tool_choice": {"type": "tool", "name": long_name}})[0]
        self.assertEqual(len(out["tool_choice"]["function"]["name"]), 64)

    def test_stop_temp_topp_stream(self):
        body = {
            "messages": [{"role": "user", "content": "hi"}],
            "stop_sequences": ["STOP", "END"],
            "temperature": 0.7,
            "top_p": 0.9,
            "stream": True,
        }
        out, ctx = anthropic_to_openai_request(body)
        self.assertEqual(out["stop"], ["STOP", "END"])
        self.assertEqual(out["temperature"], 0.7)
        self.assertEqual(out["top_p"], 0.9)
        self.assertTrue(out["stream"])
        self.assertTrue(ctx["stream"])
        self.assertEqual(out["stream_options"], {"include_usage": True})

    def test_reasoning_effort_emitted_when_reasoning(self):
        body = {"messages": [], "output_config": {"effort": "high"}}
        out, _ = anthropic_to_openai_request(body, model_is_reasoning=True)
        self.assertEqual(out["reasoning_effort"], "high")

    def test_metadata_user_id(self):
        body = {"messages": [], "metadata": {"user_id": "u123"}}
        out, _ = anthropic_to_openai_request(body)
        self.assertEqual(out["user"], "u123")

    def test_whitelist_drops_unknown(self):
        body = {"messages": [], "container": "x", "mcp_servers": [1]}
        out, _ = anthropic_to_openai_request(body)
        self.assertNotIn("container", out)
        self.assertNotIn("mcp_servers", out)


# ============================================================
# 模块 B：非流式响应转换
# ============================================================

class TestResponseTranslate(unittest.TestCase):

    def test_plain_text(self):
        resp = {
            "id": "chatcmpl-1",
            "choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        out = openai_to_anthropic_response(resp, {"request_model": "gpt-4o"})
        self.assertEqual(out["id"], "chatcmpl-1")
        self.assertEqual(out["type"], "message")
        self.assertEqual(out["role"], "assistant")
        self.assertEqual(out["model"], "gpt-4o")
        self.assertEqual(out["content"], [{"type": "text", "text": "Hello"}])
        self.assertEqual(out["stop_reason"], "end_turn")
        self.assertIsNone(out["stop_sequence"])
        self.assertEqual(out["usage"], {"input_tokens": 10, "output_tokens": 5})

    def test_tool_calls(self):
        resp = {
            "choices": [{
                "message": {"content": None, "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"location":"SF"}'},
                }]},
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 8, "completion_tokens": 12},
        }
        out = openai_to_anthropic_response(resp, {})
        self.assertEqual(out["stop_reason"], "tool_use")
        block = out["content"][0]
        self.assertEqual(block["type"], "tool_use")
        self.assertEqual(block["id"], "call_1")
        self.assertEqual(block["name"], "get_weather")
        self.assertEqual(block["input"], {"location": "SF"})

    def test_tool_calls_name_restore(self):
        long_name = "m" * 100
        truncated = truncate_tool_name(long_name)
        resp = {"choices": [{"message": {"tool_calls": [{
            "id": "c1", "function": {"name": truncated, "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}]}
        out = openai_to_anthropic_response(resp, {"tool_name_mapping": {truncated: long_name}})
        self.assertEqual(out["content"][0]["name"], long_name)

    def test_text_and_tool_combined(self):
        resp = {"choices": [{"message": {
            "content": "let me check",
            "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}],
        }, "finish_reason": "tool_calls"}]}
        out = openai_to_anthropic_response(resp, {})
        self.assertEqual(out["content"][0], {"type": "text", "text": "let me check"})
        self.assertEqual(out["content"][1]["type"], "tool_use")

    def test_finish_reason_length(self):
        resp = {"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]}
        self.assertEqual(openai_to_anthropic_response(resp, {})["stop_reason"], "max_tokens")

    def test_finish_reason_content_filter(self):
        resp = {"choices": [{"message": {"content": "x"}, "finish_reason": "content_filter"}],
                "content_filter_results": {"hate": {"filtered": True}}}
        self.assertEqual(openai_to_anthropic_response(resp, {})["stop_reason"], "end_turn")

    def test_invalid_json_arguments_downgrade(self):
        resp = {"choices": [{"message": {"tool_calls": [{
            "id": "c1", "function": {"name": "f", "arguments": "{not json"}}]},
            "finish_reason": "tool_calls"}]}
        out = openai_to_anthropic_response(resp, {})
        self.assertEqual(out["content"][0]["input"], {})   # 降级空对象

    def test_missing_id_and_usage(self):
        resp = {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}
        out = openai_to_anthropic_response(resp, {})
        self.assertTrue(out["id"].startswith("msg_"))       # 自生成
        self.assertEqual(out["usage"], {"input_tokens": 0, "output_tokens": 0})

    def test_empty_content_no_text_block(self):
        resp = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
        out = openai_to_anthropic_response(resp, {})
        self.assertEqual(out["content"], [])                # 空文本不产 block

    def test_tool_call_missing_id_gets_generated(self):
        resp = {"choices": [{"message": {"tool_calls": [{
            "function": {"name": "f", "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}]}
        out = openai_to_anthropic_response(resp, {})
        self.assertTrue(out["content"][0]["id"].startswith("toolu_"))


# ============================================================
# 模块 C+D：流式状态机
# ============================================================

class TestStreamText(unittest.TestCase):
    """用例1：纯文本流。"""

    def test_pure_text(self):
        chunks = [
            {"choices": [{"delta": {"content": "Hel"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "lo"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"completion_tokens": 5}},
        ]
        ad = AnthropicStreamAdapter({}, "gpt-4o")
        events = collect(ad, chunks)
        self.assertEqual(types_of(events), [
            "message_start", "ping",
            "content_block_start", "content_block_delta",
            "content_block_delta",
            "content_block_stop",
            "message_delta", "message_stop",
        ])
        # message_start 骨架
        ms = events[0]["message"]
        self.assertEqual(ms["model"], "gpt-4o")
        self.assertEqual(ms["content"], [])
        self.assertEqual(ms["usage"]["output_tokens"], 0)
        # 文本块 index=0
        self.assertEqual(events[2]["index"], 0)
        self.assertEqual(events[2]["content_block"], {"type": "text", "text": ""})
        self.assertEqual(events[3]["delta"], {"type": "text_delta", "text": "Hel"})
        self.assertEqual(events[4]["delta"], {"type": "text_delta", "text": "lo"})
        self.assertEqual(events[5], {"type": "content_block_stop", "index": 0})
        # message_delta：stop_reason + output_tokens
        md = events[6]
        self.assertEqual(md["delta"], {"stop_reason": "end_turn", "stop_sequence": None})
        self.assertEqual(md["usage"], {"output_tokens": 5})


class TestStreamSingleTool(unittest.TestCase):
    """用例2：单工具流（含 §4 分片重组）。"""

    def test_single_tool(self):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "call_a", "type": "function",
                "function": {"name": "get_weather", "arguments": ""}}]},
                "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "function": {"arguments": '{"loc'}}]}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "function": {"arguments": 'ation":"SF"}'}}]}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"completion_tokens": 20}},
        ]
        ad = AnthropicStreamAdapter({}, "gpt-4o")
        events = collect(ad, chunks)
        self.assertEqual(types_of(events), [
            "message_start", "ping",
            "content_block_start",       # tool_use，index 0
            "content_block_delta",       # partial_json '{"loc'
            "content_block_delta",       # partial_json 'ation":"SF"}'
            "content_block_stop",
            "message_delta", "message_stop",
        ])
        start = events[2]
        self.assertEqual(start["index"], 0)
        self.assertEqual(start["content_block"],
                         {"type": "tool_use", "id": "call_a", "name": "get_weather", "input": {}})
        # partial_json 原样透传，不拼接
        self.assertEqual(events[3]["delta"],
                         {"type": "input_json_delta", "partial_json": '{"loc'})
        self.assertEqual(events[4]["delta"],
                         {"type": "input_json_delta", "partial_json": 'ation":"SF"}'})
        self.assertEqual(events[6]["delta"]["stop_reason"], "tool_use")

    def test_tool_name_restored_in_stream(self):
        long_name = "k" * 100
        truncated = truncate_tool_name(long_name)
        chunks = [{"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "c1", "function": {"name": truncated, "arguments": ""}}]},
            "finish_reason": "tool_calls"}]}]
        ad = AnthropicStreamAdapter({"tool_name_mapping": {truncated: long_name}}, "m")
        events = collect(ad, chunks)
        start = next(e for e in events if e["type"] == "content_block_start")
        self.assertEqual(start["content_block"]["name"], long_name)

    def test_tool_id_missing_generated(self):
        chunks = [{"choices": [{"delta": {"tool_calls": [{
            "index": 0, "function": {"name": "f", "arguments": ""}}]},
            "finish_reason": "tool_calls"}]}]
        ad = AnthropicStreamAdapter({}, "m")
        events = collect(ad, chunks)
        start = next(e for e in events if e["type"] == "content_block_start")
        self.assertTrue(start["content_block"]["id"].startswith("toolu_"))


class TestStreamDualTool(unittest.TestCase):
    """用例3：双工具并发（index 0 与 1 各自成块）。"""

    def test_dual_tool(self):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "c0", "function": {"name": "f0", "arguments": '{"a":1}'}}]},
                "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 1, "id": "c1", "function": {"name": "f1", "arguments": '{"b":2}'}}]},
                "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        ad = AnthropicStreamAdapter({}, "m")
        events = collect(ad, chunks)
        self.assertEqual(types_of(events), [
            "message_start", "ping",
            "content_block_start",       # tool0 index 0
            "content_block_delta",       # tool0 args
            "content_block_stop",        # stop tool0 before start tool1
            "content_block_start",       # tool1 index 1
            "content_block_delta",       # tool1 args
            "content_block_stop",        # final stop tool1
            "message_delta", "message_stop",
        ])
        # 两个块索引单调递增 0,1
        starts = [e for e in events if e["type"] == "content_block_start"]
        self.assertEqual(starts[0]["index"], 0)
        self.assertEqual(starts[0]["content_block"]["name"], "f0")
        self.assertEqual(starts[1]["index"], 1)
        self.assertEqual(starts[1]["content_block"]["name"], "f1")
        # stop 用旧 index，start 用新 index
        stops = [e for e in events if e["type"] == "content_block_stop"]
        self.assertEqual(stops[0]["index"], 0)
        self.assertEqual(stops[1]["index"], 1)


class TestStreamArgumentsFragmented(unittest.TestCase):
    """用例4：arguments 跨多 chunk 断裂，partial_json 逐片透传。"""

    def test_fragmented(self):
        frags = ["{", '"x"', ":", "1", "}"]
        chunks = [{"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "c0", "function": {"name": "f", "arguments": ""}}]},
            "finish_reason": None}]}]
        for f in frags:
            chunks.append({"choices": [{"delta": {"tool_calls": [{
                "index": 0, "function": {"arguments": f}}]}, "finish_reason": None}]})
        chunks.append({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
        ad = AnthropicStreamAdapter({}, "m")
        events = collect(ad, chunks)
        deltas = [e["delta"]["partial_json"] for e in events
                  if e["type"] == "content_block_delta"]
        self.assertEqual(deltas, frags)   # 逐片原样，不拼接
        # 只有一个 content_block_start（首片）
        self.assertEqual(sum(1 for e in events if e["type"] == "content_block_start"), 1)


class TestStreamMissingUsage(unittest.TestCase):
    """用例5：缺 usage chunk，output_tokens=0 不报错。"""

    def test_missing_usage(self):
        chunks = [
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            # 无末尾 usage chunk
        ]
        ad = AnthropicStreamAdapter({}, "m")
        events = collect(ad, chunks)
        md = next(e for e in events if e["type"] == "message_delta")
        self.assertEqual(md["usage"], {"output_tokens": 0})
        self.assertEqual(md["delta"]["stop_reason"], "end_turn")


class TestStreamMixed(unittest.TestCase):
    """额外：文本块后接工具块（index 递增，先 stop text 再 start tool）。"""

    def test_text_then_tool(self):
        chunks = [
            {"choices": [{"delta": {"content": "let me check "}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "c0", "function": {"name": "f", "arguments": "{}"}}]},
                "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        ad = AnthropicStreamAdapter({}, "m")
        events = collect(ad, chunks)
        self.assertEqual(types_of(events), [
            "message_start", "ping",
            "content_block_start",       # text index 0
            "content_block_delta",       # text delta
            "content_block_stop",        # stop text 0
            "content_block_start",       # tool index 1
            "content_block_delta",       # tool args
            "content_block_stop",        # stop tool 1
            "message_delta", "message_stop",
        ])
        text_start = events[2]
        self.assertEqual(text_start["index"], 0)
        self.assertEqual(text_start["content_block"]["type"], "text")
        tool_start = events[5]
        self.assertEqual(tool_start["index"], 1)
        self.assertEqual(tool_start["content_block"]["type"], "tool_use")

    def test_finalize_idempotent(self):
        ad = AnthropicStreamAdapter({}, "m")
        ad.feed({"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]})
        first = ad.finalize()
        second = ad.finalize()
        self.assertTrue(len(first) > 0)
        self.assertEqual(second, [])          # 重复 finalize 不重发

    def test_empty_stream_still_valid(self):
        """一个 chunk 都没喂就 finalize，仍产出合法序列。"""
        ad = AnthropicStreamAdapter({}, "m")
        events = ad.finalize()
        self.assertEqual(types_of(events),
                         ["message_start", "ping", "message_delta", "message_stop"])

    def test_input_tokens_backfill_from_first_chunk(self):
        chunks = [
            {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 42}},
        ]
        ad = AnthropicStreamAdapter({}, "m")
        events = collect(ad, chunks)
        self.assertEqual(events[0]["message"]["usage"]["input_tokens"], 42)


if __name__ == "__main__":
    unittest.main(verbosity=2)
