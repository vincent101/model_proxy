"""model_proxy 双向协议转换器合并单测（脱网络，纯标准库 unittest）。

合并自 test_model_proxy_translate.py（正向，46 个 unittest 方法）与
test_model_proxy_translate_reverse.py（反向，24 个函数 / 120 条断言）。
两个方向的转换目标合并到 core/translate.py，本文件统一以 `from core import translate as pt` 引用。

  §1 正向 Anthropic → OpenAI Chat：TestHelpers / TestRequestTranslate /
     TestResponseTranslate / TestStream*（原正向 TestCase 原样迁入）。
  §2 反向 Responses → Anthropic：原迷你框架的 test_* 函数等价改写为
     TestReverseTranslate 的方法（check/eq 改为抛 AssertionError，语义与断言一一对应，
     一条不丢），保证可被 `python3 -m unittest` 收集。

运行：cd tools/model_proxy && python3 -m unittest tests.test_translate -v
"""

import json
import os
import sys
import unittest

# tests/ 与 core/ 同级，sys.path 指向 tools/model_proxy/ 以便 from core import translate
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import translate as pt  # noqa: E402


# ############################################################################
# §1 正向 Anthropic → OpenAI Chat
# ############################################################################


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
        self.assertEqual(pt.truncate_tool_name("get_weather"), "get_weather")
        self.assertEqual(pt.truncate_tool_name("a" * 64), "a" * 64)

    def test_truncate_tool_name_long(self):
        name = "x" * 100
        out = pt.truncate_tool_name(name)
        self.assertEqual(len(out), 64)              # 55 + 1 + 8
        self.assertEqual(out[:55], "x" * 55)
        self.assertEqual(out[55], "_")
        # 确定性：同名同结果
        self.assertEqual(out, pt.truncate_tool_name(name))

    def test_map_finish_reason(self):
        self.assertEqual(pt.map_finish_reason("stop"), "end_turn")
        self.assertEqual(pt.map_finish_reason("length"), "max_tokens")
        self.assertEqual(pt.map_finish_reason("tool_calls"), "tool_use")
        self.assertEqual(pt.map_finish_reason("content_filter"), "end_turn")
        self.assertEqual(pt.map_finish_reason(None), "end_turn")
        self.assertEqual(pt.map_finish_reason("weird"), "end_turn")

    def test_image_to_data_url(self):
        self.assertEqual(
            pt.anthropic_image_to_data_url({"type": "base64", "media_type": "image/png", "data": "AAA"}),
            "data:image/png;base64,AAA")
        self.assertEqual(
            pt.anthropic_image_to_data_url({"type": "url", "url": "http://x/y.png"}),
            "http://x/y.png")
        self.assertIsNone(pt.anthropic_image_to_data_url({"type": "other"}))

    def test_sse_bytes_format(self):
        ev = {"type": "ping"}
        out = pt.anthropic_sse_bytes(ev)
        self.assertEqual(out, b'event: ping\ndata: {"type":"ping"}\n\n')
        # 紧凑无空格 + ensure_ascii=False
        ev2 = {"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "你好"}}
        out2 = pt.anthropic_sse_bytes(ev2).decode("utf-8")
        self.assertTrue(out2.startswith("event: content_block_delta\ndata: "))
        self.assertIn("你好", out2)          # 非 ASCII 不转义
        self.assertNotIn(", ", out2)          # 紧凑分隔符
        self.assertTrue(out2.endswith("\n\n"))


# ============================================================
# _extract_reasoning_tokens：全协议统一提取 helper
# ============================================================

class TestExtractReasoningTokens(unittest.TestCase):

    def test_chat_path(self):
        self.assertEqual(
            pt._extract_reasoning_tokens({"completion_tokens_details": {"reasoning_tokens": 47}}),
            47)

    def test_responses_path(self):
        self.assertEqual(
            pt._extract_reasoning_tokens({"output_tokens_details": {"reasoning_tokens": 9}}),
            9)

    def test_anthropic_path_regression(self):
        self.assertEqual(
            pt._extract_reasoning_tokens({"output_tokens_details": {"thinking_tokens": 7}}),
            7)

    def test_null_details_defense(self):
        usage = {"output_tokens_details": None, "completion_tokens_details": {"reasoning_tokens": 5}}
        self.assertEqual(pt._extract_reasoning_tokens(usage), 5)

    def test_all_missing_returns_zero(self):
        self.assertEqual(pt._extract_reasoning_tokens({}), 0)
        self.assertEqual(pt._extract_reasoning_tokens(None), 0)


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
        out, ctx = pt.anthropic_to_openai_request(body, reasoning_fields=None)
        self.assertEqual(out["model"], "gpt-4o")
        self.assertEqual(out["max_completion_tokens"], 128)          # 改名
        self.assertNotIn("max_tokens", out)
        self.assertEqual(out["messages"][0], {"role": "system", "content": "you are helpful"})
        self.assertEqual(out["messages"][1], {"role": "user", "content": "hi"})
        self.assertEqual(ctx["request_model"], "gpt-4o")
        self.assertFalse(ctx["stream"])
        # reasoning_fields=None（调用方模拟非 reasoning 模型场景）：不发 reasoning_effort
        self.assertNotIn("reasoning_effort", out)

    def test_array_system(self):
        body = {
            "system": [
                {"type": "text", "text": "line1"},
                {"type": "text", "text": "line2"},
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
        out, _ = pt.anthropic_to_openai_request(body)
        self.assertEqual(out["messages"][0], {"role": "system", "content": "line1\nline2"})

    def test_no_system(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        out, _ = pt.anthropic_to_openai_request(body)
        self.assertEqual(out["messages"][0]["role"], "user")

    def test_user_with_image(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}},
        ]}]}
        out, _ = pt.anthropic_to_openai_request(body)
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
        out, _ = pt.anthropic_to_openai_request(body)
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
        out, _ = pt.anthropic_to_openai_request(body)
        self.assertEqual(out["messages"][0]["content"], "answer")
        self.assertNotIn("tool_calls", out["messages"][0])

    def test_tool_result_string(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "72F sunny"},
            {"type": "text", "text": "谢谢"},
        ]}]}
        out, _ = pt.anthropic_to_openai_request(body)
        # tool_result 先，normal 后（§1.3.1）
        self.assertEqual(out["messages"][0],
                         {"role": "tool", "tool_call_id": "toolu_1", "content": "72F sunny"})
        self.assertEqual(out["messages"][1], {"role": "user", "content": "谢谢"})

    def test_tool_result_single_text_block(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "result text"}]},
        ]}]}
        out, _ = pt.anthropic_to_openai_request(body)
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
        out, ctx = pt.anthropic_to_openai_request(body)
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
            pt.anthropic_to_openai_request({**base, "tool_choice": {"type": "auto"}})[0]["tool_choice"],
            "auto")
        self.assertEqual(
            pt.anthropic_to_openai_request({**base, "tool_choice": {"type": "any"}})[0]["tool_choice"],
            "required")
        self.assertEqual(
            pt.anthropic_to_openai_request({**base, "tool_choice": {"type": "none"}})[0]["tool_choice"],
            "none")
        out = pt.anthropic_to_openai_request(
            {**base, "tool_choice": {"type": "tool", "name": "get_weather"}})[0]
        self.assertEqual(out["tool_choice"],
                         {"type": "function", "function": {"name": "get_weather"}})

    def test_tool_choice_tool_name_truncated(self):
        long_name = "z" * 100
        out = pt.anthropic_to_openai_request(
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
        out, ctx = pt.anthropic_to_openai_request(body)
        self.assertEqual(out["stop"], ["STOP", "END"])
        self.assertEqual(out["temperature"], 0.7)
        self.assertEqual(out["top_p"], 0.9)
        self.assertTrue(out["stream"])
        self.assertTrue(ctx["stream"])
        self.assertEqual(out["stream_options"], {"include_usage": True})

    def test_reasoning_effort_emitted_when_reasoning(self):
        # 带 thinking.type=adaptive 才触发；裸 output_config 不再触发
        # reasoning_fields 由调用方（server.py）用 core.reasoning 链路算好后传入，
        # 这里模拟该链路：AnthropicReasoningCodec.decode → remap(源=target 同一 cap，
        # 等效原单侧钳位语义) → abstract_encode → ChatReasoningCodec.syntax_adapt
        from core.reasoning.capability import ModelReasoningCapability, abstract_encode, remap
        from core.reasoning.registry import get_codec
        body = {"messages": [], "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}
        intent = get_codec("anthropic").decode(body)
        cap = ModelReasoningCapability.from_config(None)
        target_effort = remap(intent, cap, cap)
        abstract = abstract_encode(target_effort)
        fields = get_codec("chat").syntax_adapt(abstract, "chat_effort")
        out, _ = pt.anthropic_to_openai_request(body, reasoning_fields=fields)
        self.assertEqual(out["reasoning_effort"], "high")

    def test_reasoning_effort_not_emitted_for_bare_output_config(self):
        # 裸 output_config.effort（无 thinking）→ decode 不产出意图 → 不塞 reasoning_effort
        from core.reasoning.capability import ModelReasoningCapability, abstract_encode, remap
        from core.reasoning.registry import get_codec
        body = {"messages": [], "output_config": {"effort": "high"}}
        intent = get_codec("anthropic").decode(body)
        cap = ModelReasoningCapability.from_config(None)
        target_effort = remap(intent, cap, cap)
        abstract = abstract_encode(target_effort)
        fields = get_codec("chat").syntax_adapt(abstract, "chat_effort")
        out, _ = pt.anthropic_to_openai_request(body, reasoning_fields=fields)
        self.assertNotIn("reasoning_effort", out)

    def test_metadata_user_id(self):
        body = {"messages": [], "metadata": {"user_id": "u123"}}
        out, _ = pt.anthropic_to_openai_request(body)
        self.assertEqual(out["user"], "u123")

    def test_whitelist_drops_unknown(self):
        body = {"messages": [], "container": "x", "mcp_servers": [1]}
        out, _ = pt.anthropic_to_openai_request(body)
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
        out = pt.openai_to_anthropic_response(resp, {"request_model": "gpt-4o"})
        self.assertEqual(out["id"], "chatcmpl-1")
        self.assertEqual(out["type"], "message")
        self.assertEqual(out["role"], "assistant")
        self.assertEqual(out["model"], "gpt-4o")
        self.assertEqual(out["content"], [{"type": "text", "text": "Hello"}])
        self.assertEqual(out["stop_reason"], "end_turn")
        self.assertIsNone(out["stop_sequence"])
        self.assertEqual(out["usage"], {"input_tokens": 10, "output_tokens": 5})

    def test_usage_reasoning_tokens_from_chat(self):
        """#5：usage 带 completion_tokens_details.reasoning_tokens=47 -> output_tokens_details 回填。"""
        resp = {
            "choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "completion_tokens_details": {"reasoning_tokens": 47}},
        }
        out = pt.openai_to_anthropic_response(resp, {})
        self.assertEqual(out["usage"]["output_tokens_details"]["reasoning_tokens"], 47)

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
        out = pt.openai_to_anthropic_response(resp, {})
        self.assertEqual(out["stop_reason"], "tool_use")
        block = out["content"][0]
        self.assertEqual(block["type"], "tool_use")
        self.assertEqual(block["id"], "call_1")
        self.assertEqual(block["name"], "get_weather")
        self.assertEqual(block["input"], {"location": "SF"})

    def test_tool_calls_name_restore(self):
        long_name = "m" * 100
        truncated = pt.truncate_tool_name(long_name)
        resp = {"choices": [{"message": {"tool_calls": [{
            "id": "c1", "function": {"name": truncated, "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}]}
        out = pt.openai_to_anthropic_response(resp, {"tool_name_mapping": {truncated: long_name}})
        self.assertEqual(out["content"][0]["name"], long_name)

    def test_text_and_tool_combined(self):
        resp = {"choices": [{"message": {
            "content": "let me check",
            "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}],
        }, "finish_reason": "tool_calls"}]}
        out = pt.openai_to_anthropic_response(resp, {})
        self.assertEqual(out["content"][0], {"type": "text", "text": "let me check"})
        self.assertEqual(out["content"][1]["type"], "tool_use")

    def test_finish_reason_length(self):
        resp = {"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]}
        self.assertEqual(pt.openai_to_anthropic_response(resp, {})["stop_reason"], "max_tokens")

    def test_finish_reason_content_filter(self):
        resp = {"choices": [{"message": {"content": "x"}, "finish_reason": "content_filter"}],
                "content_filter_results": {"hate": {"filtered": True}}}
        self.assertEqual(pt.openai_to_anthropic_response(resp, {})["stop_reason"], "end_turn")

    def test_invalid_json_arguments_downgrade(self):
        resp = {"choices": [{"message": {"tool_calls": [{
            "id": "c1", "function": {"name": "f", "arguments": "{not json"}}]},
            "finish_reason": "tool_calls"}]}
        out = pt.openai_to_anthropic_response(resp, {})
        self.assertEqual(out["content"][0]["input"], {})   # 降级空对象

    def test_missing_id_and_usage(self):
        resp = {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}
        out = pt.openai_to_anthropic_response(resp, {})
        self.assertTrue(out["id"].startswith("msg_"))       # 自生成
        self.assertEqual(out["usage"], {"input_tokens": 0, "output_tokens": 0})

    def test_empty_content_no_text_block(self):
        resp = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
        out = pt.openai_to_anthropic_response(resp, {})
        self.assertEqual(out["content"], [])                # 空文本不产 block

    def test_tool_call_missing_id_gets_generated(self):
        resp = {"choices": [{"message": {"tool_calls": [{
            "function": {"name": "f", "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}]}
        out = pt.openai_to_anthropic_response(resp, {})
        self.assertTrue(out["content"][0]["id"].startswith("toolu_"))

    def test_reasoning_fallback_length(self):
        """content 空、reasoning_content 非空、finish_reason=length -> 兜底为 text block。"""
        resp = {"choices": [{
            "message": {"content": "", "reasoning_content": "思考…"},
            "finish_reason": "length",
        }]}
        out = pt.openai_to_anthropic_response(resp, {})
        self.assertEqual(len(out["content"]), 1)
        block = out["content"][0]
        self.assertEqual(block["type"], "text")
        self.assertTrue(block["text"].startswith(pt._REASONING_FALLBACK_PREFIX))
        self.assertIn("思考…", block["text"])
        self.assertEqual(out["stop_reason"], "max_tokens")  # 保持不变

    def test_reasoning_fallback_finish_reason_stop(self):
        """finish_reason 非 length 也兜底。"""
        resp = {"choices": [{
            "message": {"content": "", "reasoning_content": "思考中"},
            "finish_reason": "stop",
        }]}
        out = pt.openai_to_anthropic_response(resp, {})
        self.assertEqual(len(out["content"]), 1)
        self.assertTrue(out["content"][0]["text"].startswith(pt._REASONING_FALLBACK_PREFIX))

    def test_reasoning_fallback_not_triggered_with_real_content(self):
        """有正式回答时不兜底，reasoning_content 被忽略。"""
        resp = {"choices": [{
            "message": {"content": "正式答案", "reasoning_content": "思考"},
            "finish_reason": "stop",
        }]}
        out = pt.openai_to_anthropic_response(resp, {})
        self.assertEqual(out["content"], [{"type": "text", "text": "正式答案"}])

    def test_reasoning_fallback_not_triggered_with_tool_calls(self):
        """有 tool_calls 时不兜底，即便 content 空、reasoning_content 非空。"""
        resp = {"choices": [{
            "message": {
                "content": "",
                "reasoning_content": "思考",
                "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}],
            },
            "finish_reason": "tool_calls",
        }]}
        out = pt.openai_to_anthropic_response(resp, {})
        self.assertEqual(len(out["content"]), 1)
        self.assertEqual(out["content"][0]["type"], "tool_use")

    def test_reasoning_fallback_no_reasoning_keeps_old_behavior(self):
        """content 空且无 reasoning_content -> 保持老行为，content 为空数组。"""
        resp = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
        out = pt.openai_to_anthropic_response(resp, {})
        self.assertEqual(out["content"], [])


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
        ad = pt.OpenAIToAnthropicStreamAdapter({}, "gpt-4o")
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
        ad = pt.OpenAIToAnthropicStreamAdapter({}, "gpt-4o")
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
        truncated = pt.truncate_tool_name(long_name)
        chunks = [{"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "c1", "function": {"name": truncated, "arguments": ""}}]},
            "finish_reason": "tool_calls"}]}]
        ad = pt.OpenAIToAnthropicStreamAdapter({"tool_name_mapping": {truncated: long_name}}, "m")
        events = collect(ad, chunks)
        start = next(e for e in events if e["type"] == "content_block_start")
        self.assertEqual(start["content_block"]["name"], long_name)

    def test_tool_id_missing_generated(self):
        chunks = [{"choices": [{"delta": {"tool_calls": [{
            "index": 0, "function": {"name": "f", "arguments": ""}}]},
            "finish_reason": "tool_calls"}]}]
        ad = pt.OpenAIToAnthropicStreamAdapter({}, "m")
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
        ad = pt.OpenAIToAnthropicStreamAdapter({}, "m")
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
        ad = pt.OpenAIToAnthropicStreamAdapter({}, "m")
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
        ad = pt.OpenAIToAnthropicStreamAdapter({}, "m")
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
        ad = pt.OpenAIToAnthropicStreamAdapter({}, "m")
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
        ad = pt.OpenAIToAnthropicStreamAdapter({}, "m")
        ad.feed({"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]})
        first = ad.finalize()
        second = ad.finalize()
        self.assertTrue(len(first) > 0)
        self.assertEqual(second, [])          # 重复 finalize 不重发

    def test_empty_stream_still_valid(self):
        """一个 chunk 都没喂就 finalize，仍产出合法序列。"""
        ad = pt.OpenAIToAnthropicStreamAdapter({}, "m")
        events = ad.finalize()
        self.assertEqual(types_of(events),
                         ["message_start", "ping", "message_delta", "message_stop"])

    def test_input_tokens_backfill_from_first_chunk(self):
        chunks = [
            {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 42}},
        ]
        ad = pt.OpenAIToAnthropicStreamAdapter({}, "m")
        events = collect(ad, chunks)
        self.assertEqual(events[0]["message"]["usage"]["input_tokens"], 42)


class TestStreamUsageFix(unittest.TestCase):
    """usage 吸收统一逻辑修复回归测试。"""

    def test_tail_chunk_backfills_input_tokens(self):
        chunks = [
            {"choices": [{"delta": {"content": "Hi"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 30}},
        ]
        ad = pt.OpenAIToAnthropicStreamAdapter({}, "gpt-4o")
        events = collect(ad, chunks)
        # 首帧还未读到 usage，抢发时未知，这是预期行为不是 bug
        self.assertEqual(events[0]["message"]["usage"]["input_tokens"], 0)
        md = next(e for e in events if e["type"] == "message_delta")
        self.assertEqual(md["usage"], {"output_tokens": 30, "input_tokens": 100})

    def test_finish_chunk_with_usage_same_frame(self):
        chunks = [
            {"choices": [{"delta": {"content": "Hi"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 50, "completion_tokens": 10}},
        ]
        ad = pt.OpenAIToAnthropicStreamAdapter({}, "gpt-4o")
        events = collect(ad, chunks)
        md = next(e for e in events if e["type"] == "message_delta")
        self.assertEqual(md["usage"]["input_tokens"], 50)
        self.assertEqual(md["usage"]["output_tokens"], 10)

    def test_usage_tuple(self):
        """usage_tuple() 统一接口：无 reasoning 时第三位为 0。"""
        chunks = [
            {"choices": [{"delta": {"content": "Hi"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 50, "completion_tokens": 10}},
        ]
        ad = pt.OpenAIToAnthropicStreamAdapter({}, "gpt-4o")
        collect(ad, chunks)
        self.assertEqual(ad.usage_tuple(), (50, 10, 0))

    def test_usage_tuple_reasoning_nonzero(self):
        """末帧 usage chunk 带 completion_tokens_details.reasoning_tokens=35 -> usage_tuple()[2]==35（#6）。"""
        chunks = [
            {"choices": [{"delta": {"content": "Hi"}, "finish_reason": "stop"}]},
            {"choices": [],
             "usage": {"prompt_tokens": 50, "completion_tokens": 10,
                       "completion_tokens_details": {"reasoning_tokens": 35}}},
        ]
        ad = pt.OpenAIToAnthropicStreamAdapter({}, "gpt-4o")
        collect(ad, chunks)
        self.assertEqual(ad.usage_tuple(), (50, 10, 35))


class TestStreamReasoningFallback(unittest.TestCase):
    """空回答 reasoning_content 兜底（流式，finalize 时补块）。"""

    def test_reasoning_only_finalize_adds_text_block(self):
        """全程只有 delta.reasoning_content 分片、无 content/tool_calls -> finalize 补一个 text block。"""
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "思"}, "finish_reason": None}]},
            {"choices": [{"delta": {"reasoning_content": "考中"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
        ]
        ad = pt.OpenAIToAnthropicStreamAdapter({}, "m")
        events = collect(ad, chunks)
        self.assertEqual(types_of(events), [
            "message_start", "ping",
            "content_block_start", "content_block_delta", "content_block_stop",
            "message_delta", "message_stop",
        ])
        start = events[2]
        self.assertEqual(start["index"], 0)
        self.assertEqual(start["content_block"], {"type": "text", "text": ""})
        delta_ev = events[3]
        self.assertEqual(delta_ev["delta"]["type"], "text_delta")
        self.assertTrue(delta_ev["delta"]["text"].startswith(pt._REASONING_FALLBACK_PREFIX))
        self.assertIn("思考中", delta_ev["delta"]["text"])
        self.assertEqual(events[4], {"type": "content_block_stop", "index": 0})
        md = events[5]
        self.assertEqual(md["delta"]["stop_reason"], "max_tokens")

    def test_reasoning_then_real_content_no_extra_block(self):
        """先思考分片再正式回答分片 -> 只产出正式回答 text block，finalize 不补额外块。"""
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "先想想"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "答案"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        ad = pt.OpenAIToAnthropicStreamAdapter({}, "m")
        events = collect(ad, chunks)
        self.assertEqual(types_of(events), [
            "message_start", "ping",
            "content_block_start", "content_block_delta", "content_block_stop",
            "message_delta", "message_stop",
        ])
        # 只有一个 text block，内容是正式回答，不含前缀/思考
        self.assertEqual(sum(1 for e in events if e["type"] == "content_block_start"), 1)
        delta_ev = events[3]
        self.assertEqual(delta_ev["delta"]["text"], "答案")

    def test_reasoning_empty_no_fallback_block(self):
        """无 reasoning_content 分片时，finalize 不产出多余块（保持老行为）。"""
        chunks = [
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        ad = pt.OpenAIToAnthropicStreamAdapter({}, "m")
        events = collect(ad, chunks)
        self.assertEqual(types_of(events), [
            "message_start", "ping",
            "message_delta", "message_stop",
        ])

# ############################################################################
# §2 反向 Responses → Anthropic
# ############################################################################

# 原反向单测的迷你框架 check/eq 改为抛 AssertionError 版本，
# 使每条断言在 unittest 下失败即报错（语义等价，断言逐条保留）。

def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def eq(a, b, msg):
    if a != b:
        raise AssertionError("%s (got %r, want %r)" % (msg, a, b))




# ---------------------------------------------------------------------------
# 模块 A'：请求转换
# ---------------------------------------------------------------------------

def test_A_input_string():
    ab = pt.responses_to_anthropic_request({"model": "m", "input": "hello"})
    eq(ab["messages"], [{"role": "user", "content": "hello"}], "A input 字符串 -> 单条 user")
    eq(ab["model"], "m", "A model 透传")
    eq(ab["max_tokens"], 4096, "A max_tokens 默认 4096")


def test_A_instructions_to_system():
    ab = pt.responses_to_anthropic_request({"instructions": "你是助手", "input": "hi"})
    eq(ab["system"], "你是助手", "A instructions -> system 字符串")
    # 缺 instructions 则不设 system
    ab2 = pt.responses_to_anthropic_request({"input": "hi"})
    check("system" not in ab2, "A 缺 instructions 不设 system")


def test_A_max_tokens_priority():
    eq(pt.responses_to_anthropic_request({"input": "x", "max_completion_tokens": 100})["max_tokens"],
       100, "A max_completion_tokens 优先")
    eq(pt.responses_to_anthropic_request({"input": "x", "max_output_tokens": 200})["max_tokens"],
       200, "A max_output_tokens 次选")
    eq(pt.responses_to_anthropic_request({"input": "x"}, max_tokens_default=512)["max_tokens"],
       512, "A max_tokens 兜底可配")


def _responses_body_to_reasoning_fields(body, variant="anthropic_adaptive"):
    """测试辅助：模拟 server.py 的 decode→remap→abstract_encode→syntax_adapt 链路
    （responses→anthropic）。"""
    from core.reasoning.capability import ModelReasoningCapability, abstract_encode, remap
    from core.reasoning.registry import get_codec
    cap = ModelReasoningCapability.from_config(None)
    intent = get_codec("responses").decode(body)
    target_effort = remap(intent, cap, cap)
    abstract = abstract_encode(target_effort)
    return get_codec("anthropic").syntax_adapt(abstract, variant)


def test_A_reasoning_effort():
    for eff in ("low", "medium", "high"):
        body = {"input": "x", "reasoning": {"effort": eff}}
        fields = _responses_body_to_reasoning_fields(body)
        ab = pt.responses_to_anthropic_request(body, reasoning_fields=fields)
        eq(ab.get("thinking"), {"type": "adaptive"}, "A effort=%s -> thinking adaptive" % eff)
        eq(ab.get("output_config"), {"effort": eff}, "A effort=%s -> output_config" % eff)
    # 缺失/null 不注入
    body_missing = {"input": "x"}
    ab = pt.responses_to_anthropic_request(
        body_missing, reasoning_fields=_responses_body_to_reasoning_fields(body_missing))
    check("thinking" not in ab and "output_config" not in ab, "A 无 reasoning 不注入 thinking")
    body_null = {"input": "x", "reasoning": {"effort": None}}
    ab = pt.responses_to_anthropic_request(
        body_null, reasoning_fields=_responses_body_to_reasoning_fields(body_null))
    check("thinking" not in ab, "A effort=null 不注入 thinking")


def test_A_tools():
    tools = [{"type": "function", "name": "get_weather", "description": "查天气",
              "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                             "required": ["city"]}, "strict": True}]
    ab = pt.responses_to_anthropic_request({"input": "x", "tools": tools})
    eq(ab["tools"], [{"name": "get_weather", "description": "查天气",
                      "input_schema": {"type": "object", "properties": {"city": {"type": "string"}},
                                       "required": ["city"]}}],
       "A tools 扁平 function -> input_schema（丢 strict）")
    # 非 function 类型跳过
    ab2 = pt.responses_to_anthropic_request({"input": "x", "tools": [{"type": "web_search"}]})
    check("tools" not in ab2, "A 托管工具（非 function）跳过后无 tools")


def test_A_tool_choice():
    eq(pt.responses_to_anthropic_request({"input": "x", "tool_choice": "auto"})["tool_choice"],
       {"type": "auto"}, "A tool_choice auto")
    eq(pt.responses_to_anthropic_request({"input": "x", "tool_choice": "none"})["tool_choice"],
       {"type": "none"}, "A tool_choice none")
    eq(pt.responses_to_anthropic_request({"input": "x", "tool_choice": "required"})["tool_choice"],
       {"type": "any"}, "A tool_choice required -> any")
    eq(pt.responses_to_anthropic_request(
        {"input": "x", "tool_choice": {"type": "function", "name": "f"}})["tool_choice"],
       {"type": "tool", "name": "f"}, "A tool_choice 指定 function -> tool")
    check("tool_choice" not in pt.responses_to_anthropic_request({"input": "x"}),
          "A 缺 tool_choice 不设")


def test_A_input_items_grouping():
    """items 混排：message(user) + function_call + function_call_output + message(assistant)
    应重分组为 Anthropic messages，tool_use 归 assistant、tool_result 归下一条 user、call_id 对齐。"""
    body = {
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "北京天气？"}]},
            {"type": "function_call", "call_id": "call_1", "name": "get_weather",
             "arguments": "{\"city\":\"北京\"}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "晴 30 度"},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "北京今天晴，30 度。"}]},
        ]
    }
    msgs = pt.responses_to_anthropic_request(body)["messages"]
    # 期望：user(text) -> assistant(tool_use) -> user(tool_result) -> assistant(text)
    eq(len(msgs), 4, "A 混排分组出 4 条消息")
    eq(msgs[0], {"role": "user", "content": [{"type": "text", "text": "北京天气？"}]},
       "A 消息0 user text")
    eq(msgs[1], {"role": "assistant", "content": [
        {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "北京"}}]},
       "A 消息1 assistant tool_use（call_id 透传当 id、arguments 解析成 dict）")
    eq(msgs[2], {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "晴 30 度"}]},
       "A 消息2 user tool_result（tool_use_id 与 call_id 对齐）")
    eq(msgs[3], {"role": "assistant", "content": [{"type": "text", "text": "北京今天晴，30 度。"}]},
       "A 消息3 assistant text")


def test_A_consecutive_function_calls_merge():
    """连续多个 function_call 合并进同一 assistant 消息。"""
    body = {"input": [
        {"type": "function_call", "call_id": "c1", "name": "f1", "arguments": "{}"},
        {"type": "function_call", "call_id": "c2", "name": "f2", "arguments": "{}"},
    ]}
    msgs = pt.responses_to_anthropic_request(body)["messages"]
    eq(len(msgs), 1, "A 连续 function_call 合并为 1 条 assistant")
    eq(len(msgs[0]["content"]), 2, "A 该 assistant 含 2 个 tool_use")


# ---------------------------------------------------------------------------
# 模块 B'：非流式响应转换
# ---------------------------------------------------------------------------

def test_B_text_response():
    resp = {"id": "msg_x", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "1+1等于2。"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 11}}
    out = pt.anthropic_to_responses_response(resp, model="gpt-5.6-sol", reasoning_effort="low")
    eq(out["object"], "response", "B object=response")
    eq(out["status"], "completed", "B status=completed")
    eq(out["model"], "gpt-5.6-sol", "B model 回填")
    check(out["id"].startswith("resp_"), "B id resp_ 前缀")
    eq(len(out["output"]), 1, "B 单 message item")
    item = out["output"][0]
    eq(item["type"], "message", "B item type=message")
    check(item["id"].startswith("msg_"), "B message item id msg_ 前缀")
    eq(item["content"], [{"type": "output_text", "text": "1+1等于2。",
                          "annotations": [], "logprobs": []}], "B output_text 结构")
    eq(out["reasoning"], {"effort": "low", "summary": None}, "B reasoning.effort 回显")
    # usage 逐字段
    eq(out["usage"], {"input_tokens": 12, "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens": 11, "output_tokens_details": {"reasoning_tokens": 0},
                      "total_tokens": 23}, "B usage total_tokens=input+output")
    # 顶层常量
    eq(out["service_tier"], "default", "B service_tier=default")
    eq(out["text"], {"format": {"type": "text"}, "verbosity": "medium"}, "B text 常量")
    eq(out["truncation"], "disabled", "B truncation=disabled")
    check(out["parallel_tool_calls"] is True, "B parallel_tool_calls=true")


def test_B_tool_use_response():
    resp = {"content": [{"type": "tool_use", "id": "call_TVk", "name": "get_weather",
                         "input": {"city": "北京"}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 124, "output_tokens": 18}}
    out = pt.anthropic_to_responses_response(resp, model="gpt-5.6-sol")
    eq(out["status"], "completed", "B tool_use 仍 status=completed")
    item = out["output"][0]
    eq(item["type"], "function_call", "B item type=function_call")
    check(item["id"].startswith("item_"), "B function_call item id item_ 前缀")
    eq(item["call_id"], "call_TVk", "B call_id 透传 tool_use.id")
    eq(item["name"], "get_weather", "B name 透传")
    # arguments 必须是 JSON 字符串（不是 dict）
    check(isinstance(item["arguments"], str), "B arguments 是字符串")
    eq(json.loads(item["arguments"]), {"city": "北京"}, "B arguments JSON 解析回 dict")
    eq(out["reasoning"], {"effort": None, "summary": None}, "B 无 effort 回显 null")


def test_B_thinking_to_reasoning_item():
    resp = {"content": [{"type": "thinking", "thinking": "让我想想"},
                        {"type": "text", "text": "答案"}],
            "usage": {"input_tokens": 5, "output_tokens": 3}}
    out = pt.anthropic_to_responses_response(resp, model="m")
    eq(len(out["output"]), 2, "B thinking 转 reasoning item，output 含 2 项")
    reasoning_item, message_item = out["output"]
    eq(reasoning_item["type"], "reasoning", "B 第1项是 reasoning")
    check(reasoning_item["id"].startswith("rs_"), "B reasoning item id rs_ 前缀")
    eq(reasoning_item["status"], "completed", "B reasoning item status=completed")
    eq(reasoning_item["summary"], [{"type": "summary_text", "text": "让我想想"}],
       "B 非空 thinking 正文 summary 有值")
    eq(message_item["type"], "message", "B 第2项是 message")
    eq(out["usage"]["output_tokens_details"]["reasoning_tokens"], 0,
       "B usage 无 thinking 明细字段时 reasoning_tokens 仍为 0")


def test_B_thinking_empty_text_summary_empty():
    resp = {"content": [{"type": "thinking", "thinking": ""},
                        {"type": "text", "text": "答案"}],
            "usage": {"input_tokens": 5, "output_tokens": 3}}
    out = pt.anthropic_to_responses_response(resp, model="m")
    eq(out["output"][0]["summary"], [], "B thinking 正文空串 summary=[]（不伪造占位文字）")


def test_B_redacted_thinking_summary_empty():
    resp = {"content": [{"type": "redacted_thinking", "data": "encrypted-blob"},
                        {"type": "text", "text": "答案"}],
            "usage": {"input_tokens": 5, "output_tokens": 3}}
    out = pt.anthropic_to_responses_response(resp, model="m")
    reasoning_item = out["output"][0]
    eq(reasoning_item["type"], "reasoning", "B redacted_thinking 也转 reasoning item")
    eq(reasoning_item["summary"], [], "B redacted_thinking summary=[]")
    check("encrypted_content" not in reasoning_item, "B redacted_thinking 不映射 encrypted_content")


def test_B_reasoning_tokens_multi_path():
    # 路径1：output_tokens_details.thinking_tokens
    out1 = pt.anthropic_to_responses_response(
        {"content": [], "usage": {"input_tokens": 1, "output_tokens": 2,
                                   "output_tokens_details": {"thinking_tokens": 7}}},
        model="m")
    eq(out1["usage"]["output_tokens_details"]["reasoning_tokens"], 7,
       "B reasoning_tokens 读取 output_tokens_details.thinking_tokens")
    # 路径2：output_tokens_details.reasoning_tokens
    out2 = pt.anthropic_to_responses_response(
        {"content": [], "usage": {"input_tokens": 1, "output_tokens": 2,
                                   "output_tokens_details": {"reasoning_tokens": 9}}},
        model="m")
    eq(out2["usage"]["output_tokens_details"]["reasoning_tokens"], 9,
       "B reasoning_tokens 读取 output_tokens_details.reasoning_tokens")
    # 路径3：顶层 thinking_tokens
    out3 = pt.anthropic_to_responses_response(
        {"content": [], "usage": {"input_tokens": 1, "output_tokens": 2, "thinking_tokens": 5}},
        model="m")
    eq(out3["usage"]["output_tokens_details"]["reasoning_tokens"], 5,
       "B reasoning_tokens 读取顶层 thinking_tokens")
    # total_tokens 不重复累加 reasoning
    eq(out1["usage"]["total_tokens"], 3, "B total_tokens=input+output，不重复加 reasoning")


def test_B_tools_echo():
    tools = [{"type": "function", "name": "f", "description": "d", "parameters": {}}]
    out = pt.anthropic_to_responses_response({"content": [], "usage": {}}, model="m",
                                            tools_echo=tools)
    eq(out["tools"], tools, "B tools 回显请求")
    out2 = pt.anthropic_to_responses_response({"content": [], "usage": {}}, model="m")
    eq(out2["tools"], [], "B 无 tools 回显为 []")


# ---------------------------------------------------------------------------
# 模块 C'+D'：流式状态机
# ---------------------------------------------------------------------------

def _types(events):
    return [e["type"] for e in events]


def _seqs(events):
    return [e["sequence_number"] for e in events]


def _run(adapter, anthropic_events):
    """喂一串 (event_type, data)，返回所有产出的 Responses 事件。"""
    out = []
    for et, data in anthropic_events:
        out.extend(adapter.feed(et, data))
    out.extend(adapter.finalize())
    return out


def _assert_seq_contiguous(events, label):
    seqs = _seqs(events)
    eq(seqs, list(range(len(events))), "%s sequence_number 0..N 连续" % label)


# 实测样本A（纯文本，含首块 thinking）改写的 Anthropic 事件序列
def _sample_text_events():
    return [
        ("message_start", {"type": "message_start",
                           "message": {"id": "msg_x", "type": "message", "role": "assistant",
                                       "model": "claude-sonnet-5", "content": [],
                                       "usage": {}}}),
        # 修正2：首块永远是 thinking(index 0)
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "thinking", "thinking": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "signature_delta", "signature": "abc"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        # text 从 index 1 起
        ("content_block_start", {"type": "content_block_start", "index": 1,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                 "delta": {"type": "text_delta", "text": "Hi! "}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                 "delta": {"type": "text_delta", "text": "有什么我可以帮你的吗？😊"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        # 修正3：usage 在 message_delta
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"input_tokens": 9, "output_tokens": 22}}),
        ("message_stop", {"type": "message_stop"}),
    ]


def test_C_text_stream():
    adapter = pt.AnthropicToResponsesStreamAdapter(model="gpt-5.6-sol")
    events = _run(adapter, _sample_text_events())
    # 期望事件序列：thinking(空正文，只有 signature) → reasoning item added/done（无 delta，无 summary_part.done）
    # 顺延到 text item 在 output_index 1
    eq(_types(events), [
        "response.created",
        "response.in_progress",
        "response.output_item.added",       # reasoning item, output_index 0
        "response.output_item.done",        # reasoning item done（正文空，无 summary_part.done）
        "response.output_item.added",       # message item, output_index 1
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ], "C 纯文本事件序列（thinking 转 reasoning item，text 顺延到 index 1）")
    _assert_seq_contiguous(events, "C 纯文本")
    added_items = [e for e in events if e["type"] == "response.output_item.added"]
    reasoning_added, message_added = added_items
    eq(reasoning_added["output_index"], 0, "C reasoning item 占 output_index 0")
    eq(reasoning_added["item"]["type"], "reasoning", "C 首个 item 是 reasoning")
    check(reasoning_added["item"]["id"].startswith("rs_"), "C reasoning item id rs_ 前缀")
    eq(message_added["output_index"], 1, "C message 顺延到 output_index 1")
    check(message_added["item"]["id"].startswith("msg_"), "C message item id msg_ 前缀")
    # reasoning item done：正文空 summary=[]
    done_items = [e for e in events if e["type"] == "response.output_item.done"]
    reasoning_done = done_items[0]
    eq(reasoning_done["item"]["summary"], [], "C reasoning 正文空 summary=[]")
    eq(reasoning_done["item"]["status"], "completed", "C reasoning item 收尾 completed")
    # 完整文本
    text_done = [e for e in events if e["type"] == "response.output_text.done"][0]
    eq(text_done["text"], "Hi! 有什么我可以帮你的吗？😊", "C output_text.done 完整文本")
    # completed 携带 usage 与 output（reasoning + message 共 2 项）
    completed = events[-1]
    eq(completed["response"]["status"], "completed", "C completed status")
    eq(completed["response"]["service_tier"], "default", "C completed service_tier=default")
    eq(completed["response"]["usage"]["input_tokens"], 9, "C completed usage input")
    eq(completed["response"]["usage"]["output_tokens"], 22, "C completed usage output")
    eq(completed["response"]["usage"]["total_tokens"], 31, "C completed total_tokens=input+output")
    eq(len(completed["response"]["output"]), 2, "C completed output 含 2 item（reasoning+message）")
    # created/in_progress 骨架
    created = events[0]
    eq(created["response"]["status"], "in_progress", "C created status=in_progress")
    eq(created["response"]["service_tier"], "auto", "C created service_tier=auto")
    check("usage" not in created["response"], "C created 无 usage")


def test_C_thinking_to_reasoning_item_full_lifecycle():
    """thinking 正文非空：产出 output_item.added→summary.delta→summary_part.done→output_item.done
    完整生命周期，占用 output_index 0；signature_delta 仍跳过；text item 顺延到 output_index 1。"""
    adapter = pt.AnthropicToResponsesStreamAdapter(model="m")
    evs = _run(adapter, [
        ("message_start", {"message": {"usage": {}}}),
        ("content_block_start", {"content_block": {"type": "thinking", "thinking": ""}}),
        ("content_block_delta", {"delta": {"type": "thinking_delta", "thinking": "想"}}),
        ("content_block_delta", {"delta": {"type": "signature_delta", "signature": "s"}}),
        ("content_block_stop", {}),
        ("content_block_start", {"content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"delta": {"type": "text_delta", "text": "OK"}}),
        ("content_block_stop", {}),
        ("message_delta", {"delta": {"stop_reason": "end_turn"},
                           "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ("message_stop", {}),
    ])
    # reasoning_summary_text.delta 应产出（正文非空），signature_delta 不产出任何事件
    reasoning_deltas = [e for e in evs if e["type"] == "response.reasoning_summary_text.delta"]
    eq(len(reasoning_deltas), 1, "C thinking 正文非空产出 1 条 summary.delta")
    eq(reasoning_deltas[0]["delta"], "想", "C summary.delta 内容透传")
    # reasoning item 占 output_index 0，text item 顺延到 1
    added = [e for e in evs if e["type"] == "response.output_item.added"]
    eq(len(added), 2, "C reasoning item + text item 共 2 个 output item")
    eq(added[0]["output_index"], 0, "C reasoning item output_index=0")
    eq(added[0]["item"]["type"], "reasoning", "C item0 类型 reasoning")
    eq(added[1]["output_index"], 1, "C text item output_index=1（顺延）")
    # reasoning item done：summary 非空 + summary_part.done 先发
    part_done = [e for e in evs if e["type"] == "response.reasoning_summary_part.done"]
    eq(len(part_done), 1, "C 正文非空发 1 条 summary_part.done")
    reasoning_item_done = [e for e in evs if e["type"] == "response.output_item.done"][0]
    eq(reasoning_item_done["item"]["summary"], [{"type": "summary_text", "text": "想"}],
       "C reasoning item done summary 含正文")
    _assert_seq_contiguous(evs, "C thinking 完整生命周期")


# 实测样本B（工具，含首块 thinking）改写
def _sample_tool_events():
    return [
        ("message_start", {"message": {"usage": {}}}),
        ("content_block_start", {"content_block": {"type": "thinking", "thinking": ""}}),
        ("content_block_delta", {"delta": {"type": "signature_delta", "signature": "sig"}}),
        ("content_block_stop", {}),
        ("content_block_start", {"content_block": {"type": "tool_use",
                                                   "id": "toolu_01", "name": "get_weather",
                                                   "input": {}}}),
        ("content_block_delta", {"delta": {"type": "input_json_delta", "partial_json": "{\"city\""}}),
        ("content_block_delta", {"delta": {"type": "input_json_delta", "partial_json": ": \""}}),
        ("content_block_delta", {"delta": {"type": "input_json_delta", "partial_json": "北京\"}"}}),
        ("content_block_stop", {}),
        ("message_delta", {"delta": {"stop_reason": "tool_use"},
                           "usage": {"input_tokens": 429, "output_tokens": 64}}),
        ("message_stop", {}),
    ]


def test_C_tool_stream():
    adapter = pt.AnthropicToResponsesStreamAdapter(model="gpt-5.6-sol")
    events = _run(adapter, _sample_tool_events())
    eq(_types(events), [
        "response.created",
        "response.in_progress",
        "response.output_item.added",       # reasoning item, output_index 0
        "response.output_item.done",        # reasoning item done（正文空）
        "response.output_item.added",       # function_call item, output_index 1
        "response.function_call_arguments.delta",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
        "response.completed",
    ], "C 工具事件序列（thinking 转 reasoning item 顺延，无 content_part 事件）")
    _assert_seq_contiguous(events, "C 工具")
    added_items = [e for e in events if e["type"] == "response.output_item.added"]
    eq(added_items[0]["output_index"], 0, "C reasoning item output_index 0")
    added = added_items[1]
    eq(added["output_index"], 1, "C 工具 output_index 顺延到 1")
    check(added["item"]["id"].startswith("item_"), "C function_call item id item_ 前缀")
    eq(added["item"]["call_id"], "toolu_01", "C call_id 透传 tool_use.id")
    eq(added["item"]["name"], "get_weather", "C name 透传")
    eq(added["item"]["arguments"], "", "C added 初始 arguments 空串")
    # 参数跨帧透传，done 给完整
    done = [e for e in events if e["type"] == "response.function_call_arguments.done"][0]
    eq(done["arguments"], "{\"city\": \"北京\"}", "C arguments.done 拼接完整（原样透传拼接）")
    eq(done["name"], "get_weather", "C arguments.done 带 name")
    item_done = [e for e in events if e["type"] == "response.output_item.done"][1]
    eq(item_done["item"]["status"], "completed", "C function_call item 收尾 completed")
    eq(item_done["item"]["arguments"], "{\"city\": \"北京\"}", "C item.done arguments 完整")


def test_C_text_and_tool_mixed():
    """thinking(idx0) + 文本(idx1) + 工具(idx2)：thinking 转 reasoning item 占 output_index 0，
    message 顺延到 1，function_call 顺延到 2。"""
    adapter = pt.AnthropicToResponsesStreamAdapter(model="m")
    evs = _run(adapter, [
        ("message_start", {"message": {"usage": {}}}),
        ("content_block_start", {"content_block": {"type": "thinking", "thinking": ""}}),
        ("content_block_stop", {}),
        ("content_block_start", {"content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"delta": {"type": "text_delta", "text": "查一下"}}),
        ("content_block_stop", {}),
        ("content_block_start", {"content_block": {"type": "tool_use", "id": "t1",
                                                   "name": "f", "input": {}}}),
        ("content_block_delta", {"delta": {"type": "input_json_delta", "partial_json": "{}"}}),
        ("content_block_stop", {}),
        ("message_delta", {"delta": {"stop_reason": "tool_use"},
                           "usage": {"input_tokens": 10, "output_tokens": 5}}),
        ("message_stop", {}),
    ])
    added = [e for e in evs if e["type"] == "response.output_item.added"]
    eq(len(added), 3, "C 混合出 3 个 output item（reasoning+message+function_call）")
    eq(added[0]["output_index"], 0, "C reasoning output_index 0")
    eq(added[0]["item"]["type"], "reasoning", "C item0 reasoning")
    eq(added[1]["output_index"], 1, "C message output_index 1（顺延）")
    eq(added[1]["item"]["type"], "message", "C item1 message")
    eq(added[2]["output_index"], 2, "C function_call output_index 2（顺延）")
    eq(added[2]["item"]["type"], "function_call", "C item2 function_call")
    _assert_seq_contiguous(evs, "C 文本+工具混合")
    eq(len(evs[-1]["response"]["output"]), 3, "C completed output 含 3 item")


def test_C_multi_tool():
    """多工具并发：thinking idx0 转 reasoning item；两 tool_use 顺延占 output_index 1、2。"""
    adapter = pt.AnthropicToResponsesStreamAdapter(model="m")
    evs = _run(adapter, [
        ("message_start", {"message": {"usage": {}}}),
        ("content_block_start", {"content_block": {"type": "thinking", "thinking": ""}}),
        ("content_block_stop", {}),
        ("content_block_start", {"content_block": {"type": "tool_use", "id": "a", "name": "f1", "input": {}}}),
        ("content_block_delta", {"delta": {"type": "input_json_delta", "partial_json": "{\"x\":1}"}}),
        ("content_block_stop", {}),
        ("content_block_start", {"content_block": {"type": "tool_use", "id": "b", "name": "f2", "input": {}}}),
        ("content_block_delta", {"delta": {"type": "input_json_delta", "partial_json": "{\"y\":2}"}}),
        ("content_block_stop", {}),
        ("message_delta", {"delta": {"stop_reason": "tool_use"},
                           "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ("message_stop", {}),
    ])
    added = [e for e in evs if e["type"] == "response.output_item.added"]
    eq(len(added), 3, "C 多工具出 3 个 item（reasoning+工具1+工具2）")
    eq(added[0]["output_index"], 0, "C reasoning output_index 0")
    eq(added[0]["item"]["type"], "reasoning", "C item0 reasoning")
    eq(added[1]["output_index"], 1, "C 工具1 output_index 1（顺延）")
    eq(added[2]["output_index"], 2, "C 工具2 output_index 2（顺延）")
    eq(added[1]["item"]["call_id"], "a", "C 工具1 call_id")
    eq(added[2]["item"]["call_id"], "b", "C 工具2 call_id")
    _assert_seq_contiguous(evs, "C 多工具")


def test_C_args_split_frames():
    """arguments 跨多帧断裂：每帧原样透传一个 function_call_arguments.delta，末尾 buf 完整。"""
    adapter = pt.AnthropicToResponsesStreamAdapter(model="m")
    frames = ["{\"", "ci", "ty\":\"", "北", "京\"}"]
    evs = _run(adapter, [
        ("message_start", {"message": {"usage": {}}}),
        ("content_block_start", {"content_block": {"type": "thinking", "thinking": ""}}),
        ("content_block_stop", {}),
        ("content_block_start", {"content_block": {"type": "tool_use", "id": "t", "name": "f", "input": {}}}),
    ] + [("content_block_delta", {"delta": {"type": "input_json_delta", "partial_json": p}}) for p in frames]
      + [
        ("content_block_stop", {}),
        ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ("message_stop", {}),
    ])
    deltas = [e for e in evs if e["type"] == "response.function_call_arguments.delta"]
    eq([d["delta"] for d in deltas], frames, "C 每帧原样透传为 delta（不重拼）")
    done = [e for e in evs if e["type"] == "response.function_call_arguments.done"][0]
    eq(done["arguments"], "".join(frames), "C done arguments 为各帧拼接")
    eq(json.loads(done["arguments"]), {"city": "北京"}, "C 拼接后可 JSON 解析")


def test_C_empty_tool_args():
    """工具无参数（无 input_json_delta）：done arguments 用 '{}'。"""
    adapter = pt.AnthropicToResponsesStreamAdapter(model="m")
    evs = _run(adapter, [
        ("message_start", {"message": {"usage": {}}}),
        ("content_block_start", {"content_block": {"type": "thinking", "thinking": ""}}),
        ("content_block_stop", {}),
        ("content_block_start", {"content_block": {"type": "tool_use", "id": "t", "name": "f", "input": {}}}),
        ("content_block_stop", {}),
        ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ("message_stop", {}),
    ])
    done = [e for e in evs if e["type"] == "response.function_call_arguments.done"][0]
    eq(done["arguments"], "{}", "C 无参数工具 arguments 用 '{}'")


def test_C_tools_and_effort_echo():
    """created/completed 骨架回显 ctx 的 tools 和 reasoning_effort。"""
    tools = [{"type": "function", "name": "f", "description": "d", "parameters": {}}]
    adapter = pt.AnthropicToResponsesStreamAdapter(model="m", ctx={"tools": tools, "reasoning_effort": "high"})
    evs = _run(adapter, _sample_text_events())
    created = evs[0]
    eq(created["response"]["tools"], tools, "C created 回显 tools")
    eq(created["response"]["reasoning"], {"effort": "high", "summary": None},
       "C created 回显 reasoning.effort")


def test_C_finalize_incomplete():
    """流意外结束（无 message_stop）：finalize 补 response.completed 收尾。"""
    adapter = pt.AnthropicToResponsesStreamAdapter(model="m")
    evs = _run(adapter, [
        ("message_start", {"message": {"usage": {}}}),
        ("content_block_start", {"content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"delta": {"type": "text_delta", "text": "半句"}}),
        # 无 content_block_stop / message_delta / message_stop
    ])
    eq(evs[-1]["type"], "response.completed", "C finalize 补 response.completed 收尾")
    _assert_seq_contiguous(evs, "C finalize 收尾")


def test_C_finalize_no_double_completed():
    """正常收尾后 finalize 不应重复发 completed。"""
    adapter = pt.AnthropicToResponsesStreamAdapter(model="m")
    evs = _run(adapter, _sample_text_events())   # _run 已调 finalize
    completed = [e for e in evs if e["type"] == "response.completed"]
    eq(len(completed), 1, "C 正常流仅 1 个 response.completed")


def test_C_usage_tuple():
    """usage_tuple() 统一接口：三元组读到累加后的 usage_in/out/reasoning。"""
    adapter = pt.AnthropicToResponsesStreamAdapter(model="m")
    _run(adapter, [
        ("message_start", {"message": {"usage": {}}}),
        ("content_block_start", {"content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"delta": {"type": "text_delta", "text": "hi"}}),
        ("content_block_stop", {}),
        ("message_delta", {"delta": {"stop_reason": "end_turn"},
                            "usage": {"input_tokens": 5, "output_tokens": 3}}),
        ("message_stop", {}),
    ])
    eq(adapter.usage_tuple(), (5, 3, 0), "C usage_tuple 无 reasoning 累加器场景返回 0")


# ---------------------------------------------------------------------------
# 辅助：SSE 序列化 + id 格式
# ---------------------------------------------------------------------------

def test_sse_bytes():
    b = pt.responses_sse_bytes({"type": "response.completed", "sequence_number": 3,
                               "response": {"text": "北京"}})
    s = b.decode("utf-8")
    check(s.startswith("data: "), "SSE 以 'data: ' 开头")
    check(s.endswith("\n\n"), "SSE 以 \\n\\n 结尾")
    check("event:" not in s, "SSE 无 event: 行")
    check("[DONE]" not in s, "SSE 无 [DONE] 哨兵")
    check("北京" in s, "SSE 中文不转义（ensure_ascii=False）")
    # 单行 data（去掉尾部空行后应无内部换行）
    eq(s.rstrip("\n").count("\n"), 0, "SSE data 单行")
    # 可解析回原 dict
    payload = json.loads(s[len("data: "):].strip())
    eq(payload["type"], "response.completed", "SSE payload type 保真")
    eq(payload["sequence_number"], 3, "SSE payload sequence_number 保真")


def test_id_formats():
    check(pt.gen_response_id().startswith("resp_") and len(pt.gen_response_id()) == 5 + 32,
          "id resp_ + 32 hex")
    check(pt.gen_message_id().startswith("msg_") and len(pt.gen_message_id()) == 4 + 32,
          "id msg_ + 32 hex")
    check(pt.gen_item_id().startswith("item_") and len(pt.gen_item_id()) == 5 + 32,
          "id item_ + 32 hex")
    check(pt.gen_call_id().startswith("call_") and len(pt.gen_call_id()) == 5 + 24,
          "id call_ + 24 hex（兜底）")


class TestReverseTranslate(unittest.TestCase):
    """反向 test_* 函数的 unittest 包装（每个函数一个 test 方法）。"""


def _bind_reverse_tests():
    g = globals()
    for name in sorted(g):
        if name.startswith("test_") and callable(g[name]):
            fn = g[name]

            def _method(self, _fn=fn):
                _fn()

            _method.__name__ = name
            _method.__doc__ = fn.__doc__
            setattr(TestReverseTranslate, name, _method)


_bind_reverse_tests()


# ============================================================================
# §3 Anthropic → Responses（新组合，标准 unittest）
# 前缀 test_ar_ 区分现有 test_A/B/C（反向）与正向 TestXxx。
# 用独立 TestCase 类（非 module-level test_* 函数），不会被 _bind_reverse_tests 抓取。
# ============================================================================


def _run_ar_stream(adapter, events):
    """喂 (event_type, data) 序列给 ResponsesToAnthropicStreamAdapter，收集所有产出。"""
    out = []
    for et, d in events:
        out.extend(adapter.feed(et, d))
    out.extend(adapter.finalize())
    return out


class TestARRequest(unittest.TestCase):
    """§3.1 请求转换 anthropic_to_responses_request。"""

    def test_ar_system_string(self):
        body = {"system": "你是助手", "messages": [{"role": "user", "content": "hi"}]}
        rb, ctx = pt.anthropic_to_responses_request(body)
        self.assertEqual(rb["instructions"], "你是助手")

    def test_ar_system_block_array(self):
        body = {"system": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}],
                "messages": []}
        rb, _ = pt.anthropic_to_responses_request(body)
        self.assertEqual(rb["instructions"], "A\nB")

    def test_ar_user_text_to_input_message(self):
        body = {"messages": [{"role": "user", "content": "hello"}]}
        rb, _ = pt.anthropic_to_responses_request(body)
        self.assertEqual(rb["input"], [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hello"}]}])

    def test_ar_user_image_data_url(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": "AAAA"}}]}]}
        rb, _ = pt.anthropic_to_responses_request(body)
        item = rb["input"][0]
        self.assertEqual(item["content"][0]["type"], "input_image")
        self.assertEqual(item["content"][0]["image_url"], "data:image/png;base64,AAAA")

    def test_ar_assistant_tool_use_to_function_call(self):
        body = {"messages": [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
             "input": {"city": "北京"}}]}]}
        rb, _ = pt.anthropic_to_responses_request(body)
        fc = rb["input"][0]
        self.assertEqual(fc["type"], "function_call")
        self.assertEqual(fc["call_id"], "toolu_1")
        self.assertEqual(fc["name"], "get_weather")
        self.assertEqual(json.loads(fc["arguments"]), {"city": "北京"})

    def test_ar_user_tool_result_to_output(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "晴"}]}]}
        rb, _ = pt.anthropic_to_responses_request(body)
        out = rb["input"][0]
        self.assertEqual(out["type"], "function_call_output")
        self.assertEqual(out["call_id"], "toolu_1")
        self.assertEqual(out["output"], "晴")

    def test_ar_consecutive_tool_use_multiple_items(self):
        body = {"messages": [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "f1", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "f2", "input": {}}]}]}
        rb, _ = pt.anthropic_to_responses_request(body)
        fcs = [i for i in rb["input"] if i["type"] == "function_call"]
        self.assertEqual(len(fcs), 2)
        self.assertEqual([f["name"] for f in fcs], ["f1", "f2"])

    def test_ar_max_tokens(self):
        body = {"max_tokens": 128, "messages": []}
        rb, _ = pt.anthropic_to_responses_request(body)
        self.assertEqual(rb["max_output_tokens"], 128)

    def _anthropic_body_to_responses_reasoning_fields(self, body):
        """测试辅助：模拟 server.py 的 decode→remap→abstract_encode→syntax_adapt 链路
        （anthropic→responses）。"""
        from core.reasoning.capability import ModelReasoningCapability, abstract_encode, remap
        from core.reasoning.registry import get_codec
        cap = ModelReasoningCapability.from_config(None)
        intent = get_codec("anthropic").decode(body)
        target_effort = remap(intent, cap, cap)
        abstract = abstract_encode(target_effort)
        return get_codec("responses").syntax_adapt(abstract, "resp_effort")

    def test_ar_reasoning_effort_tiers(self):
        # thinking enabled budget 分档：<2000→low, <8000→medium, <32000→high, <64000→xhigh
        # （budget=40000 落在 [32000,64000) → XHIGH，与旧行为 >=32000→xhigh 一致）
        for budget, expect in [(1000, "low"), (5000, "medium"), (10000, "high"), (40000, "xhigh")]:
            body = {"messages": [], "thinking": {"type": "enabled", "budget_tokens": budget}}
            fields = self._anthropic_body_to_responses_reasoning_fields(body)
            rb, _ = pt.anthropic_to_responses_request(body, reasoning_fields=fields)
            self.assertEqual(rb["reasoning"], {"effort": expect}, f"budget={budget}")

    def test_ar_reasoning_not_injected_when_non_reasoning(self):
        # 非 reasoning 模型场景：调用方直接传 None（不走 core.reasoning 链路）
        body = {"messages": [], "thinking": {"type": "enabled", "budget_tokens": 40000}}
        rb, _ = pt.anthropic_to_responses_request(body, reasoning_fields=None)
        self.assertNotIn("reasoning", rb)

    def test_ar_tools_flat_and_name_truncation(self):
        long_name = "x" * 70
        body = {"messages": [], "tools": [
            {"name": "short", "description": "d", "input_schema": {"type": "object"}},
            {"name": long_name, "input_schema": {"type": "object"}}]}
        rb, ctx = pt.anthropic_to_responses_request(body)
        self.assertEqual(rb["tools"][0]["type"], "function")
        self.assertNotIn("function", rb["tools"][0])  # 扁平，非两层嵌套
        self.assertEqual(rb["tools"][0]["name"], "short")
        truncated = rb["tools"][1]["name"]
        self.assertLessEqual(len(truncated), 64)
        self.assertIn(truncated, ctx["tool_name_mapping"])
        self.assertEqual(ctx["tool_name_mapping"][truncated], long_name)

    def test_ar_tool_choice_four_states(self):
        cases = [
            ({"type": "auto"}, "auto"),
            ({"type": "any"}, "required"),
            ({"type": "none"}, "none"),
            ({"type": "tool", "name": "f"}, {"type": "function", "name": "f"}),
        ]
        for tc_in, expect in cases:
            rb, _ = pt.anthropic_to_responses_request({"messages": [], "tool_choice": tc_in})
            self.assertEqual(rb["tool_choice"], expect, f"tc={tc_in}")

    def test_ar_stop_sequences_dropped(self):
        body = {"messages": [], "stop_sequences": ["STOP"]}
        rb, _ = pt.anthropic_to_responses_request(body)
        self.assertNotIn("stop_sequences", rb)
        self.assertNotIn("stop", rb)


class TestARResponse(unittest.TestCase):
    """§3.1 非流式响应 responses_to_anthropic_response。"""

    def test_ar_text_response(self):
        resp = {"id": "resp_x", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "答案"}]}],
            "usage": {"input_tokens": 5, "output_tokens": 3}}
        ar = pt.responses_to_anthropic_response(resp, {"request_model": "m"})
        self.assertEqual(ar["content"], [{"type": "text", "text": "答案"}])
        self.assertEqual(ar["stop_reason"], "end_turn")
        self.assertEqual(ar["model"], "m")
        self.assertEqual(ar["usage"]["input_tokens"], 5)
        self.assertEqual(ar["usage"]["output_tokens"], 3)

    def test_ar_usage_reasoning_tokens(self):
        """#7：usage 带 output_tokens_details.reasoning_tokens=12 -> 原样回填。"""
        resp = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "答案"}]}],
                "usage": {"input_tokens": 5, "output_tokens": 3,
                          "output_tokens_details": {"reasoning_tokens": 12}}}
        ar = pt.responses_to_anthropic_response(resp)
        self.assertEqual(ar["usage"]["output_tokens_details"]["reasoning_tokens"], 12)

    def test_ar_function_call_response(self):
        resp = {"id": "r", "output": [
            {"type": "function_call", "call_id": "call_1", "name": "get_weather",
             "arguments": "{\"city\":\"北京\"}"}]}
        ar = pt.responses_to_anthropic_response(resp)
        blk = ar["content"][0]
        self.assertEqual(blk["type"], "tool_use")
        self.assertEqual(blk["id"], "call_1")
        self.assertEqual(blk["name"], "get_weather")
        self.assertEqual(blk["input"], {"city": "北京"})
        self.assertEqual(ar["stop_reason"], "tool_use")

    def test_ar_text_and_tool_mixed(self):
        resp = {"output": [
            {"type": "message", "content": [{"type": "output_text", "text": "t"}]},
            {"type": "function_call", "call_id": "c", "name": "f", "arguments": "{}"}]}
        ar = pt.responses_to_anthropic_response(resp)
        self.assertEqual([b["type"] for b in ar["content"]], ["text", "tool_use"])
        self.assertEqual(ar["stop_reason"], "tool_use")

    def test_ar_reasoning_item_dropped(self):
        resp = {"output": [
            {"type": "reasoning", "summary": "x"},
            {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}
        ar = pt.responses_to_anthropic_response(resp)
        self.assertEqual(ar["content"], [{"type": "text", "text": "ok"}])

    def test_ar_tool_name_restored(self):
        long_name = "y" * 70
        truncated = pt.truncate_tool_name(long_name)
        resp = {"output": [
            {"type": "function_call", "call_id": "c", "name": truncated, "arguments": "{}"}]}
        ar = pt.responses_to_anthropic_response(resp, {"tool_name_mapping": {truncated: long_name}})
        self.assertEqual(ar["content"][0]["name"], long_name)

    def test_ar_bad_arguments_downgrade(self):
        resp = {"output": [
            {"type": "function_call", "call_id": "c", "name": "f", "arguments": "not-json"}]}
        ar = pt.responses_to_anthropic_response(resp)
        self.assertEqual(ar["content"][0]["input"], {})

    def test_ar_missing_usage_and_id_fallback(self):
        resp = {"output": [
            {"type": "message", "content": [{"type": "output_text", "text": "x"}]}]}
        ar = pt.responses_to_anthropic_response(resp)
        self.assertEqual(ar["usage"], {"input_tokens": 0, "output_tokens": 0})
        self.assertTrue(ar["id"].startswith("msg_"))

    def test_ar_cached_tokens_mapped(self):
        resp = {"output": [], "usage": {"input_tokens": 10, "output_tokens": 2,
                                        "input_tokens_details": {"cached_tokens": 4}}}
        ar = pt.responses_to_anthropic_response(resp)
        self.assertEqual(ar["usage"]["cache_read_input_tokens"], 4)

    def test_ar_empty_output_end_turn(self):
        ar = pt.responses_to_anthropic_response({"output": []})
        self.assertEqual(ar["content"], [])
        self.assertEqual(ar["stop_reason"], "end_turn")


def _ar_text_stream_events():
    """基于 samples/responses_api_samples.txt 样本2构造的文本流事件序列。"""
    return [
        ("response.created", {"response": {"id": "resp_c", "model": "gpt-5.6-sol"}}),
        ("response.in_progress", {"response": {"id": "resp_c"}}),
        ("response.output_item.added", {"output_index": 0, "item": {
            "id": "msg_1", "type": "message", "status": "in_progress",
            "role": "assistant", "content": []}}),
        ("response.content_part.added", {"item_id": "msg_1", "output_index": 0,
            "content_index": 0, "part": {"type": "output_text", "text": ""}}),
        ("response.output_text.delta", {"item_id": "msg_1", "delta": "Hi"}),
        ("response.output_text.delta", {"item_id": "msg_1", "delta": "!"}),
        ("response.output_text.delta", {"item_id": "msg_1", "delta": " 👋"}),
        ("response.output_text.done", {"item_id": "msg_1", "text": "Hi! 👋"}),
        ("response.content_part.done", {"item_id": "msg_1", "part": {
            "type": "output_text", "text": "Hi! 👋"}}),
        ("response.output_item.done", {"output_index": 0, "item": {
            "id": "msg_1", "type": "message", "status": "completed",
            "role": "assistant", "content": [{"type": "output_text", "text": "Hi! 👋"}]}}),
        ("response.completed", {"response": {
            "id": "resp_c", "status": "completed",
            "usage": {"input_tokens": 8, "output_tokens": 8}}}),
    ]


def _ar_tool_stream_events():
    """基于样本4构造的工具调用流事件序列。"""
    return [
        ("response.created", {"response": {"id": "resp_t", "model": "gpt-5.6-sol"}}),
        ("response.in_progress", {"response": {"id": "resp_t"}}),
        ("response.output_item.added", {"output_index": 0, "item": {
            "id": "item_1", "type": "function_call", "status": "in_progress",
            "call_id": "call_a", "name": "get_weather", "arguments": ""}}),
        ("response.function_call_arguments.delta", {"item_id": "item_1", "delta": "{\""}),
        ("response.function_call_arguments.delta", {"item_id": "item_1", "delta": "city"}),
        ("response.function_call_arguments.delta", {"item_id": "item_1", "delta": "\":\""}),
        ("response.function_call_arguments.delta", {"item_id": "item_1", "delta": "北京"}),
        ("response.function_call_arguments.delta", {"item_id": "item_1", "delta": "\"}"}),
        ("response.function_call_arguments.done", {"item_id": "item_1",
            "name": "get_weather", "arguments": "{\"city\":\"北京\"}"}),
        ("response.output_item.done", {"output_index": 0, "item": {
            "id": "item_1", "type": "function_call", "status": "completed",
            "call_id": "call_a", "name": "get_weather", "arguments": "{\"city\":\"北京\"}"}}),
        ("response.completed", {"response": {
            "id": "resp_t", "status": "completed",
            "usage": {"input_tokens": 123, "output_tokens": 18}}}),
    ]


class TestARStream(unittest.TestCase):
    """§3.1 流式 ResponsesToAnthropicStreamAdapter。"""

    def test_ar_text_stream_sequence(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        evs = _run_ar_stream(adapter, _ar_text_stream_events())
        types = [e["type"] for e in evs]
        self.assertEqual(types[0], "message_start")
        self.assertEqual(types[1], "ping")
        self.assertIn("content_block_start", types)
        self.assertIn("content_block_delta", types)
        self.assertEqual(types[-1], "message_stop")
        self.assertEqual(types[-2], "message_delta")
        # message_stop 前一个是 content_block_stop
        self.assertEqual(types[types.index("message_delta") - 1], "content_block_stop")
        # 文本 delta 拼接
        text = "".join(e["delta"]["text"] for e in evs
                        if e["type"] == "content_block_delta"
                        and e["delta"]["type"] == "text_delta")
        self.assertEqual(text, "Hi! 👋")

    def test_ar_text_stream_usage_to_message_delta(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        evs = _run_ar_stream(adapter, _ar_text_stream_events())
        md = [e for e in evs if e["type"] == "message_delta"][0]
        self.assertEqual(md["usage"]["output_tokens"], 8)
        self.assertEqual(md["usage"]["input_tokens"], 8)

    def test_ar_usage_tuple(self):
        """usage_tuple() 统一接口：无 reasoning 时第三位为 0。"""
        adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        _run_ar_stream(adapter, _ar_text_stream_events())
        self.assertEqual(adapter.usage_tuple(), (8, 8, 0))

    def test_ar_usage_tuple_reasoning_nonzero(self):
        """#8：response.completed.usage.output_tokens_details.reasoning_tokens=8 -> usage_tuple()[2]==8。"""
        adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        events = _ar_text_stream_events()[:-1] + [
            ("response.completed", {"response": {
                "id": "resp_c", "status": "completed",
                "usage": {"input_tokens": 8, "output_tokens": 8,
                          "output_tokens_details": {"reasoning_tokens": 8}}}}),
        ]
        _run_ar_stream(adapter, events)
        self.assertEqual(adapter.usage_tuple(), (8, 8, 8))

    def test_ar_tool_stream_sequence(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        evs = _run_ar_stream(adapter, _ar_tool_stream_events())
        starts = [e for e in evs if e["type"] == "content_block_start"]
        self.assertEqual(starts[0]["content_block"]["type"], "tool_use")
        self.assertEqual(starts[0]["content_block"]["name"], "get_weather")
        self.assertEqual(starts[0]["content_block"]["id"], "call_a")
        # input_json_delta 拼接为完整 arguments
        args = "".join(e["delta"]["partial_json"] for e in evs
                       if e["type"] == "content_block_delta"
                       and e["delta"]["type"] == "input_json_delta")
        self.assertEqual(json.loads(args), {"city": "北京"})
        stop_reason = [e for e in evs if e["type"] == "message_delta"][0]["delta"]["stop_reason"]
        self.assertEqual(stop_reason, "tool_use")

    def test_ar_tool_name_restored_in_stream(self):
        long_name = "z" * 70
        truncated = pt.truncate_tool_name(long_name)
        adapter = pt.ResponsesToAnthropicStreamAdapter(
            {"tool_name_mapping": {truncated: long_name}}, "m")
        evs = _run_ar_stream(adapter, [
            ("response.created", {"response": {"id": "r"}}),
            ("response.output_item.added", {"item": {
                "type": "function_call", "call_id": "c", "name": truncated, "arguments": ""}}),
            ("response.completed", {"response": {"usage": {"input_tokens": 1, "output_tokens": 1}}}),
        ])
        start = [e for e in evs if e["type"] == "content_block_start"][0]
        self.assertEqual(start["content_block"]["name"], long_name)

    def test_ar_text_and_tool_mixed_stream(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        evs = _run_ar_stream(adapter, [
            ("response.created", {"response": {"id": "r"}}),
            ("response.output_item.added", {"item": {"type": "message", "content": []}}),
            ("response.output_text.delta", {"delta": "hello"}),
            ("response.output_item.done", {"item": {"type": "message"}}),
            ("response.output_item.added", {"item": {
                "type": "function_call", "call_id": "c", "name": "f", "arguments": ""}}),
            ("response.function_call_arguments.delta", {"delta": "{}"}),
            ("response.completed", {"response": {"usage": {"input_tokens": 1, "output_tokens": 1}}}),
        ])
        block_types = [e["content_block"]["type"] for e in evs
                       if e["type"] == "content_block_start"]
        self.assertEqual(block_types, ["text", "tool_use"])
        # 两个 block 各配一个 stop
        self.assertEqual(len([e for e in evs if e["type"] == "content_block_stop"]), 2)

    def test_ar_finalize_idempotent(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        evs = _run_ar_stream(adapter, _ar_text_stream_events())  # 已含 completed + finalize
        self.assertEqual(len([e for e in evs if e["type"] == "message_stop"]), 1)
        # 再次 finalize 不产事件
        self.assertEqual(adapter.finalize(), [])

    def test_ar_empty_stream_valid_finish(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        # 只有 created，无内容，无 completed → finalize 补收尾
        evs = _run_ar_stream(adapter, [
            ("response.created", {"response": {"id": "r"}}),
        ])
        types = [e["type"] for e in evs]
        self.assertIn("message_start", types)
        self.assertEqual(types[-1], "message_stop")
        self.assertIn("message_delta", types)

    def test_ar_incomplete_stream_finalize(self):
        adapter = pt.ResponsesToAnthropicStreamAdapter({}, "m")
        # 文本进行到一半，无 completed
        evs = _run_ar_stream(adapter, [
            ("response.created", {"response": {"id": "r"}}),
            ("response.output_item.added", {"item": {"type": "message", "content": []}}),
            ("response.output_text.delta", {"delta": "半句"}),
        ])
        types = [e["type"] for e in evs]
        self.assertEqual(types[-1], "message_stop")
        # 开着的 text block 被 finalize 收掉
        self.assertIn("content_block_stop", types)


if __name__ == "__main__":
    unittest.main(verbosity=2)
