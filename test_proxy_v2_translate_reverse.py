"""proxy_v2_translate_reverse 单测（脱网络，纯标准库）。

覆盖反向规格 §6.4：
  模块 A' 请求转换：input 字符串 / items 混排 / effort 三档 / tools 扁平->input_schema /
                    tool_choice 四态 / max_tokens 兜底。
  模块 B' 非流式响应：纯 text / tool_use(arguments 是 JSON 字符串) / stop_reason / thinking 丢弃 /
                    usage total_tokens 相加 / 完整结构。
  模块 C'+D' 流式：1 纯文本 2 单工具 3 文本+工具混合 4 多工具并发 5 thinking 跳过
                  6 arguments 跨帧断裂 7 usage total_tokens；sequence_number 0..N 连续、
                  以 response.completed 收尾。
  辅助：responses_sse_bytes（data: 单行、无 event:、无 [DONE]）；id 格式。
"""

import json
import sys

import proxy_v2_translate_reverse as R


# ---------------------------------------------------------------------------
# 迷你测试框架
# ---------------------------------------------------------------------------

_PASS = 0
_FAIL = 0


def check(cond, msg):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print("  FAIL: " + msg)


def eq(a, b, msg):
    check(a == b, "%s (got %r, want %r)" % (msg, a, b))


# ---------------------------------------------------------------------------
# 模块 A'：请求转换
# ---------------------------------------------------------------------------

def test_A_input_string():
    ab = R.responses_to_anthropic_request({"model": "m", "input": "hello"})
    eq(ab["messages"], [{"role": "user", "content": "hello"}], "A input 字符串 -> 单条 user")
    eq(ab["model"], "m", "A model 透传")
    eq(ab["max_tokens"], 4096, "A max_tokens 默认 4096")


def test_A_instructions_to_system():
    ab = R.responses_to_anthropic_request({"instructions": "你是助手", "input": "hi"})
    eq(ab["system"], "你是助手", "A instructions -> system 字符串")
    # 缺 instructions 则不设 system
    ab2 = R.responses_to_anthropic_request({"input": "hi"})
    check("system" not in ab2, "A 缺 instructions 不设 system")


def test_A_max_tokens_priority():
    eq(R.responses_to_anthropic_request({"input": "x", "max_completion_tokens": 100})["max_tokens"],
       100, "A max_completion_tokens 优先")
    eq(R.responses_to_anthropic_request({"input": "x", "max_output_tokens": 200})["max_tokens"],
       200, "A max_output_tokens 次选")
    eq(R.responses_to_anthropic_request({"input": "x"}, max_tokens_default=512)["max_tokens"],
       512, "A max_tokens 兜底可配")


def test_A_reasoning_effort():
    for eff in ("low", "medium", "high"):
        ab = R.responses_to_anthropic_request({"input": "x", "reasoning": {"effort": eff}})
        eq(ab.get("thinking"), {"type": "adaptive"}, "A effort=%s -> thinking adaptive" % eff)
        eq(ab.get("output_config"), {"effort": eff}, "A effort=%s -> output_config" % eff)
    # 缺失/null 不注入
    ab = R.responses_to_anthropic_request({"input": "x"})
    check("thinking" not in ab and "output_config" not in ab, "A 无 reasoning 不注入 thinking")
    ab = R.responses_to_anthropic_request({"input": "x", "reasoning": {"effort": None}})
    check("thinking" not in ab, "A effort=null 不注入 thinking")


def test_A_tools():
    tools = [{"type": "function", "name": "get_weather", "description": "查天气",
              "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                             "required": ["city"]}, "strict": True}]
    ab = R.responses_to_anthropic_request({"input": "x", "tools": tools})
    eq(ab["tools"], [{"name": "get_weather", "description": "查天气",
                      "input_schema": {"type": "object", "properties": {"city": {"type": "string"}},
                                       "required": ["city"]}}],
       "A tools 扁平 function -> input_schema（丢 strict）")
    # 非 function 类型跳过
    ab2 = R.responses_to_anthropic_request({"input": "x", "tools": [{"type": "web_search"}]})
    check("tools" not in ab2, "A 托管工具（非 function）跳过后无 tools")


def test_A_tool_choice():
    eq(R.responses_to_anthropic_request({"input": "x", "tool_choice": "auto"})["tool_choice"],
       {"type": "auto"}, "A tool_choice auto")
    eq(R.responses_to_anthropic_request({"input": "x", "tool_choice": "none"})["tool_choice"],
       {"type": "none"}, "A tool_choice none")
    eq(R.responses_to_anthropic_request({"input": "x", "tool_choice": "required"})["tool_choice"],
       {"type": "any"}, "A tool_choice required -> any")
    eq(R.responses_to_anthropic_request(
        {"input": "x", "tool_choice": {"type": "function", "name": "f"}})["tool_choice"],
       {"type": "tool", "name": "f"}, "A tool_choice 指定 function -> tool")
    check("tool_choice" not in R.responses_to_anthropic_request({"input": "x"}),
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
    msgs = R.responses_to_anthropic_request(body)["messages"]
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
    msgs = R.responses_to_anthropic_request(body)["messages"]
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
    out = R.anthropic_to_responses_response(resp, model="gpt-5.6-sol", reasoning_effort="low")
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
    out = R.anthropic_to_responses_response(resp, model="gpt-5.6-sol")
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


def test_B_thinking_dropped():
    resp = {"content": [{"type": "thinking", "thinking": "让我想想"},
                        {"type": "text", "text": "答案"}],
            "usage": {"input_tokens": 5, "output_tokens": 3}}
    out = R.anthropic_to_responses_response(resp, model="m")
    eq(len(out["output"]), 1, "B thinking 块被丢弃，只剩 text item")
    eq(out["output"][0]["type"], "message", "B 剩下的是 message")
    eq(out["usage"]["output_tokens_details"]["reasoning_tokens"], 0, "B reasoning_tokens=0")


def test_B_tools_echo():
    tools = [{"type": "function", "name": "f", "description": "d", "parameters": {}}]
    out = R.anthropic_to_responses_response({"content": [], "usage": {}}, model="m",
                                            tools_echo=tools)
    eq(out["tools"], tools, "B tools 回显请求")
    out2 = R.anthropic_to_responses_response({"content": [], "usage": {}}, model="m")
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
    adapter = R.ResponsesStreamAdapter(model="gpt-5.6-sol")
    events = _run(adapter, _sample_text_events())
    # 期望事件序列（thinking 块被完全跳过）
    eq(_types(events), [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ], "C 纯文本事件序列（thinking 跳过，无多余事件）")
    _assert_seq_contiguous(events, "C 纯文本")
    # thinking 不占 output_index：message item 应在 output_index 0
    added = [e for e in events if e["type"] == "response.output_item.added"][0]
    eq(added["output_index"], 0, "C thinking 跳过后 message 在 output_index 0")
    check(added["item"]["id"].startswith("msg_"), "C message item id msg_ 前缀")
    # 完整文本
    done = [e for e in events if e["type"] == "response.output_text.done"][0]
    eq(done["text"], "Hi! 有什么我可以帮你的吗？😊", "C output_text.done 完整文本")
    # completed 携带 usage 与 output
    completed = events[-1]
    eq(completed["response"]["status"], "completed", "C completed status")
    eq(completed["response"]["service_tier"], "default", "C completed service_tier=default")
    eq(completed["response"]["usage"]["input_tokens"], 9, "C completed usage input")
    eq(completed["response"]["usage"]["output_tokens"], 22, "C completed usage output")
    eq(completed["response"]["usage"]["total_tokens"], 31, "C completed total_tokens=input+output")
    eq(len(completed["response"]["output"]), 1, "C completed output 含 1 item")
    # created/in_progress 骨架
    created = events[0]
    eq(created["response"]["status"], "in_progress", "C created status=in_progress")
    eq(created["response"]["service_tier"], "auto", "C created service_tier=auto")
    check("usage" not in created["response"], "C created 无 usage")


def test_C_thinking_skipped_no_events():
    """thinking_delta/signature_delta 不产出事件、不占 index（专测修正2）。"""
    adapter = R.ResponsesStreamAdapter(model="m")
    # 只喂 thinking 块 + text 块
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
    # thinking 相关不应产生任何 output_text.delta 或 item 事件
    thinking_leak = [e for e in evs if e["type"] in (
        "response.reasoning_summary_text.delta",)]
    eq(thinking_leak, [], "C thinking 不产 reasoning 事件")
    # 只有 1 个 output item（text），output_index 0
    added = [e for e in evs if e["type"] == "response.output_item.added"]
    eq(len(added), 1, "C thinking 跳过后仅 1 个 output item")
    eq(added[0]["output_index"], 0, "C text item output_index=0（thinking 未占用）")
    _assert_seq_contiguous(evs, "C thinking 跳过")


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
    adapter = R.ResponsesStreamAdapter(model="gpt-5.6-sol")
    events = _run(adapter, _sample_tool_events())
    eq(_types(events), [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
        "response.completed",
    ], "C 工具事件序列（无 content_part 事件）")
    _assert_seq_contiguous(events, "C 工具")
    added = [e for e in events if e["type"] == "response.output_item.added"][0]
    eq(added["output_index"], 0, "C 工具 output_index 0（thinking 跳过）")
    check(added["item"]["id"].startswith("item_"), "C function_call item id item_ 前缀")
    eq(added["item"]["call_id"], "toolu_01", "C call_id 透传 tool_use.id")
    eq(added["item"]["name"], "get_weather", "C name 透传")
    eq(added["item"]["arguments"], "", "C added 初始 arguments 空串")
    # 参数跨帧透传，done 给完整
    done = [e for e in events if e["type"] == "response.function_call_arguments.done"][0]
    eq(done["arguments"], "{\"city\": \"北京\"}", "C arguments.done 拼接完整（原样透传拼接）")
    eq(done["name"], "get_weather", "C arguments.done 带 name")
    item_done = [e for e in events if e["type"] == "response.output_item.done"][0]
    eq(item_done["item"]["status"], "completed", "C function_call item 收尾 completed")
    eq(item_done["item"]["arguments"], "{\"city\": \"北京\"}", "C item.done arguments 完整")


def test_C_text_and_tool_mixed():
    """文本(idx1) + 工具(idx2)：thinking idx0 跳过；Responses output_index 0(message)、1(function_call)。"""
    adapter = R.ResponsesStreamAdapter(model="m")
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
    eq(len(added), 2, "C 混合出 2 个 output item")
    eq(added[0]["output_index"], 0, "C message output_index 0")
    eq(added[0]["item"]["type"], "message", "C item0 message")
    eq(added[1]["output_index"], 1, "C function_call output_index 1")
    eq(added[1]["item"]["type"], "function_call", "C item1 function_call")
    _assert_seq_contiguous(evs, "C 文本+工具混合")
    eq(len(evs[-1]["response"]["output"]), 2, "C completed output 含 2 item")


def test_C_multi_tool():
    """多工具并发：thinking idx0 跳过；两 tool_use 各占 output_index 0、1。"""
    adapter = R.ResponsesStreamAdapter(model="m")
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
    eq(len(added), 2, "C 多工具出 2 个 item")
    eq(added[0]["output_index"], 0, "C 工具1 output_index 0")
    eq(added[1]["output_index"], 1, "C 工具2 output_index 1")
    eq(added[0]["item"]["call_id"], "a", "C 工具1 call_id")
    eq(added[1]["item"]["call_id"], "b", "C 工具2 call_id")
    _assert_seq_contiguous(evs, "C 多工具")


def test_C_args_split_frames():
    """arguments 跨多帧断裂：每帧原样透传一个 function_call_arguments.delta，末尾 buf 完整。"""
    adapter = R.ResponsesStreamAdapter(model="m")
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
    adapter = R.ResponsesStreamAdapter(model="m")
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
    adapter = R.ResponsesStreamAdapter(model="m", ctx={"tools": tools, "reasoning_effort": "high"})
    evs = _run(adapter, _sample_text_events())
    created = evs[0]
    eq(created["response"]["tools"], tools, "C created 回显 tools")
    eq(created["response"]["reasoning"], {"effort": "high", "summary": None},
       "C created 回显 reasoning.effort")


def test_C_finalize_incomplete():
    """流意外结束（无 message_stop）：finalize 补 response.completed 收尾。"""
    adapter = R.ResponsesStreamAdapter(model="m")
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
    adapter = R.ResponsesStreamAdapter(model="m")
    evs = _run(adapter, _sample_text_events())   # _run 已调 finalize
    completed = [e for e in evs if e["type"] == "response.completed"]
    eq(len(completed), 1, "C 正常流仅 1 个 response.completed")


# ---------------------------------------------------------------------------
# 辅助：SSE 序列化 + id 格式
# ---------------------------------------------------------------------------

def test_sse_bytes():
    b = R.responses_sse_bytes({"type": "response.completed", "sequence_number": 3,
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
    check(R.gen_response_id().startswith("resp_") and len(R.gen_response_id()) == 5 + 32,
          "id resp_ + 32 hex")
    check(R.gen_message_id().startswith("msg_") and len(R.gen_message_id()) == 4 + 32,
          "id msg_ + 32 hex")
    check(R.gen_item_id().startswith("item_") and len(R.gen_item_id()) == 5 + 32,
          "id item_ + 32 hex")
    check(R.gen_call_id().startswith("call_") and len(R.gen_call_id()) == 5 + 24,
          "id call_ + 24 hex（兜底）")


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        print("• " + t.__name__)
        t()
    print("\n%d passed, %d failed" % (_PASS, _FAIL))
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
