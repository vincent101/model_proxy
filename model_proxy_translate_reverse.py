"""model_proxy 反向协议转换器：OpenAI Responses API ↔ Anthropic Messages API。

对应 tools/model_proxy/docs/proxy_translate_spec_reverse.md 的模块 A' / B' / C'+D'：
- 模块 A' 请求转换：responses_to_anthropic_request  (Responses body -> Anthropic body)
- 模块 B' 非流式响应：anthropic_to_responses_response (Anthropic resp -> Responses resp)
- 模块 C'+D' 流式状态机：ResponsesStreamAdapter (Anthropic SSE 事件 -> Responses SSE 事件)

纯标准库，脱网络，不 import model_proxy.py 或正向转换器。

字段映射查反向规格；Responses 侧字段结构查 tools/model_proxy/samples/responses_api_samples.txt；
Anthropic 流式格式查 tools/model_proxy/samples/anthropic_stream_samples.txt（含 3 处对规格 §3 假设的实测修正）：
  修正1 wire format：Anthropic SSE 是 `event:xxx\\ndata:{json}`（冒号后无空格）。
      本转换器 feed(event_type, data) 接收的是已解析好的 (type, dict)，SSE 拆行由主文件负责；
      本文件的输出侧 responses_sse_bytes 产出 Responses 侧 `data: {json}\\n\\n`。
  修正2 thinking 块必现：每次响应首块永远是 thinking(index 0)，text/tool_use 从 index 1 起。
      本状态机对 thinking/redacted_thinking 块显式跳过（cur_item_kind="thinking_skip"），
      不产出 Responses 事件、不占 output_index，故 output_index 独立计数、与 Anthropic index 解耦。
  修正3 usage 落点：message_start.usage 为空 {}，完整 usage（input+output_tokens）在 message_delta。
      故 usage_in / usage_out 均从 message_delta 取，不依赖 message_start。
"""

import json
import logging
import secrets
import time

logger = logging.getLogger("model_proxy.translate_reverse")


# ============================================================================
# 辅助：id 生成（反向规格 §6.3）
# ============================================================================

def gen_response_id() -> str:
    return "resp_" + secrets.token_hex(16)      # 32 位 hex


def gen_message_id() -> str:
    return "msg_" + secrets.token_hex(16)       # message item id


def gen_item_id() -> str:
    return "item_" + secrets.token_hex(16)      # function_call item id


def gen_call_id() -> str:
    return "call_" + secrets.token_hex(12)      # call_id 兜底


def gen_conversation_id() -> str:
    return "conv_" + secrets.token_hex(16)


def gen_tooluse_id() -> str:
    return "toolu_" + secrets.token_hex(12)


# ============================================================================
# 辅助：Responses SSE 序列化（反向规格 §3.3）
# ============================================================================

def responses_sse_bytes(data: dict) -> bytes:
    """把一个 Responses 事件 dict 序列化为 SSE 字节。

    格式：`data: <紧凑JSON>\\n\\n`（无 event: 行、无 [DONE] 哨兵，中文不转义）。
    data 需已含 "type" 键与已填好的 "sequence_number"。
    """
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return ("data: " + payload + "\n\n").encode("utf-8")


# ============================================================================
# 辅助：请求侧纯函数
# ============================================================================

def _safe_json_loads(s):
    """把 Responses 的 arguments 字符串解析为 dict；失败则返回 {}。"""
    if isinstance(s, dict):
        return s
    if not s:
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def responses_image_to_anthropic_source(part: dict):
    """Responses input_image -> Anthropic image source（反向规格 §1.3.1）。

    data URL -> {type:base64, media_type, data}；http(s) URL -> {type:url, url}。
    """
    url = part.get("image_url")
    if isinstance(url, dict):                    # 兼容 {image_url:{url:...}}
        url = url.get("url")
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        # data:<media_type>;base64,<data>
        try:
            head, data = url.split(",", 1)
            media_type = head[len("data:"):].split(";", 1)[0]
            return {"type": "base64", "media_type": media_type, "data": data}
        except ValueError:
            return None
    if url.startswith("http://") or url.startswith("https://"):
        return {"type": "url", "url": url}
    return None


def responses_content_to_anthropic_blocks(content) -> list:
    """Responses message.content -> Anthropic content block 列表（反向规格 §1.3.1）。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        pt = part.get("type")
        if pt in ("input_text", "output_text", "text"):
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif pt == "input_image":
            src = responses_image_to_anthropic_source(part)
            if src:
                blocks.append({"type": "image", "source": src})
        # 其他类型忽略
    return blocks


def map_reasoning(body: dict) -> dict:
    """Responses reasoning.effort -> Anthropic thinking + output_config（反向规格 §1.4）。

    low/medium/high 直传；缺失/null 不注入。
    """
    r = body.get("reasoning") or {}
    eff = r.get("effort")
    if eff in ("low", "medium", "high"):
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": eff}}
    return {}


def map_tool_choice(tc):
    """Responses tool_choice -> Anthropic tool_choice（反向规格 §1.6）。"""
    if tc == "auto":
        return {"type": "auto"}
    if tc == "none":
        return {"type": "none"}
    if tc == "required":
        return {"type": "any"}
    if isinstance(tc, dict) and tc.get("type") == "function":
        return {"type": "tool", "name": tc.get("name", "")}
    return None                                  # 缺失/未知 -> 不设


def translate_tools(responses_tools) -> list:
    """Responses 扁平 function tools -> Anthropic tools（反向规格 §1.5）。"""
    out = []
    for tool in responses_tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue                             # 非 function（托管工具）跳过
        out.append({
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "input_schema": tool.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


# ============================================================================
# 模块 A'：请求转换 Responses -> Anthropic（反向规格 §1）
# ============================================================================

def responses_to_anthropic_request(body: dict, max_tokens_default: int = 4096) -> dict:
    """把 codex 的 Responses 请求体转换成 Anthropic /v1/messages 请求体。

    白名单策略：只转换反向规格 §1.1 表中字段，其余平台字段丢弃。
    注：thinking 注入未按 model_is_reasoning 门控（任务签名未含该参数）；
    非 reasoning 模型的门控由主文件外层负责。
    """
    ab: dict = {}

    # model 原样透传
    if "model" in body:
        ab["model"] = body.get("model")

    # instructions -> system（纯字符串，§1.2）
    sys = body.get("instructions")
    if sys:
        ab["system"] = sys

    # input -> messages（§1.3）
    ab["messages"] = _input_to_messages(body.get("input"))

    # max_completion_tokens / max_output_tokens -> max_tokens（§1.7，必填兜底）
    ab["max_tokens"] = (
        body.get("max_completion_tokens")
        or body.get("max_output_tokens")
        or max_tokens_default
    )

    # reasoning.effort -> thinking + output_config（§1.4）
    ab.update(map_reasoning(body))

    # tools -> input_schema（§1.5）
    if body.get("tools"):
        tools = translate_tools(body.get("tools"))
        if tools:
            ab["tools"] = tools

    # tool_choice（§1.6）
    if "tool_choice" in body:
        tc = map_tool_choice(body.get("tool_choice"))
        if tc is not None:
            ab["tool_choice"] = tc

    # stream / temperature / top_p 透传
    if "stream" in body:
        ab["stream"] = body.get("stream")
    if "temperature" in body:
        ab["temperature"] = body.get("temperature")
    if "top_p" in body:
        ab["top_p"] = body.get("top_p")

    return ab


def _append_tool_use(messages: list, tool_use: dict) -> None:
    """连续 function_call 合并进同一 assistant 消息（反向规格 §1.3）。"""
    if messages and messages[-1]["role"] == "assistant" and isinstance(messages[-1]["content"], list):
        messages[-1]["content"].append(tool_use)
    else:
        messages.append({"role": "assistant", "content": [tool_use]})


def _input_to_messages(input_items) -> list:
    """Responses input（字符串 或 items 数组）-> Anthropic messages（反向规格 §1.3）。"""
    # 形态1：字符串
    if isinstance(input_items, str):
        return [{"role": "user", "content": input_items}]
    if input_items is None:
        return []

    # 形态2：items 数组，重新分组
    messages: list = []
    pending_user_blocks: list = []

    def flush_user():
        if pending_user_blocks:
            messages.append({"role": "user", "content": pending_user_blocks[:]})
            pending_user_blocks.clear()

    for item in input_items:
        if not isinstance(item, dict):
            continue
        t = item.get("type")

        if t == "message":
            role = item.get("role")
            blocks = responses_content_to_anthropic_blocks(item.get("content"))
            if role == "user":
                pending_user_blocks.extend(blocks)
            elif role == "assistant":
                flush_user()
                messages.append({"role": "assistant", "content": blocks})
            # 其他 role 忽略

        elif t == "function_call":
            flush_user()
            tool_use = {
                "type": "tool_use",
                "id": item.get("call_id") or gen_tooluse_id(),
                "name": item.get("name", ""),
                "input": _safe_json_loads(item.get("arguments", "{}")),
            }
            _append_tool_use(messages, tool_use)

        elif t == "function_call_output":
            pending_user_blocks.append({
                "type": "tool_result",
                "tool_use_id": item.get("call_id"),
                "content": item.get("output", ""),
            })
        # 其他 type（如 reasoning item 回放）忽略

    flush_user()
    return messages


# ============================================================================
# 模块 B'：非流式响应转换 Anthropic -> Responses（反向规格 §2）
# ============================================================================

def _anthropic_usage_to_responses(usage: dict) -> dict:
    """Anthropic usage -> Responses usage（反向规格 §2.3）。"""
    if not usage:
        logger.warning(
            "anthropic response missing usage field, responses usage will be all-zero"
        )
    u = usage or {}
    in_tok = u.get("input_tokens", 0) or 0
    out_tok = u.get("output_tokens", 0) or 0
    return {
        "input_tokens": in_tok,
        "input_tokens_details": {"cached_tokens": u.get("cache_read_input_tokens", 0) or 0},
        "output_tokens": out_tok,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": in_tok + out_tok,
    }


def anthropic_to_responses_response(resp: dict, model: str,
                                    reasoning_effort=None, tools_echo=None) -> dict:
    """把 Anthropic 非流式响应转换成 Responses 响应（反向规格 §2）。

    reasoning_effort / tools_echo 为请求侧回显值（缺省 None / []）。
    """
    output = []
    for block in resp.get("content", []) or []:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")

        if bt == "text":
            output.append({
                "type": "message",
                "id": gen_message_id(),
                "status": "completed",
                "role": "assistant",
                "content": [{
                    "type": "output_text",
                    "text": block.get("text", ""),
                    "annotations": [],
                    "logprobs": [],
                }],
            })
        elif bt == "tool_use":
            output.append({
                "type": "function_call",
                "id": gen_item_id(),
                "status": "completed",
                "call_id": block.get("id") or gen_call_id(),
                "name": block.get("name", ""),
                # Anthropic tool_use.input 是 dict -> Responses arguments 要 JSON 字符串
                "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
            })
        elif bt in ("thinking", "redacted_thinking"):
            # 反向规格 §5.3 策略 A：首版丢弃
            pass
        # 其他 block 忽略

    now = int(time.time())
    return {
        "id": gen_response_id(),
        "object": "response",
        "created_at": now,
        "status": "completed",                   # 反向规格 §2.2：首版统一 completed
        "background": False,
        "completed_at": now,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "reasoning": {"effort": reasoning_effort, "summary": None},
        "service_tier": "default",
        "store": True,
        "text": {"format": {"type": "text"}, "verbosity": "medium"},
        "tool_choice": "auto",
        "tools": tools_echo or [],
        "truncation": "disabled",
        "usage": _anthropic_usage_to_responses(resp.get("usage")),
        "metadata": {},
    }


# ============================================================================
# 模块 C'+D'：流式状态机 Anthropic 事件 -> Responses 事件（反向规格 §3+§4）
# ============================================================================

class ResponsesStreamAdapter:
    """喂 Anthropic 流式事件，产出 Responses 流式事件序列。

    用法：
        adapter = ResponsesStreamAdapter(model="gpt-5.6-sol", ctx={"tools":[...], "reasoning_effort":"low"})
        for anthropic_event in ...:
            for responses_event in adapter.feed(event_type, data):
                write(responses_sse_bytes(responses_event))
        for ev in adapter.finalize():           # 流意外结束时补收尾
            write(responses_sse_bytes(ev))

    每个产出的事件已带全局单调递增的 sequence_number（从 0 开始）。
    """

    def __init__(self, model: str = "", ctx: dict = None):
        ctx = ctx or {}
        self.model = model
        self.tools_echo = ctx.get("tools") or []
        self.reasoning_effort = ctx.get("reasoning_effort")

        self.seq = 0
        self.response_id = gen_response_id()
        self.conversation_id = gen_conversation_id()
        self.sent_created = False
        self.completed = False

        self.output_index = -1
        self.cur_item_kind = None                # "message" | "function_call" | "thinking_skip"
        self.cur_item_id = None
        self.cur_text_buf = ""

        self.cur_call_id = None
        self.cur_tool_name = None
        self.cur_args_buf = ""

        self.usage_in = 0
        self.usage_out = 0
        self.final_stop_reason = None
        self.completed_items = []                # 已完成的 output item，供 response.completed

    # ---- 内部工具 ----

    def _emit(self, obj: dict) -> dict:
        obj["sequence_number"] = self.seq
        self.seq += 1
        return obj

    def _skeleton(self, status: str, service_tier: str,
                  with_completed_at: bool = False, with_usage: bool = False) -> dict:
        skel = {
            "id": self.response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "background": False,
            "model": self.model,
            "output": list(self.completed_items) if with_usage else [],
            "parallel_tool_calls": True,
            "conversation": {"id": self.conversation_id},
            "reasoning": {"effort": self.reasoning_effort, "summary": None},
            "service_tier": service_tier,
            "store": True,
            "text": {"format": {"type": "text"}, "verbosity": "medium"},
            "tool_choice": "auto",
            "tools": self.tools_echo,
            "truncation": "disabled",
            "metadata": {},
        }
        if with_completed_at:
            skel["completed_at"] = int(time.time())
        if with_usage:
            skel["usage"] = {
                "input_tokens": self.usage_in,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": self.usage_out,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": self.usage_in + self.usage_out,
            }
        return skel

    def _ensure_created(self, events: list) -> None:
        if self.sent_created:
            return
        events.append(self._emit({
            "type": "response.created",
            "response": self._skeleton("in_progress", "auto"),
        }))
        events.append(self._emit({
            "type": "response.in_progress",
            "response": self._skeleton("in_progress", "auto"),
        }))
        self.sent_created = True

    # ---- 主入口 ----

    def feed(self, event_type: str, data: dict) -> list:
        events: list = []
        data = data or {}
        self._ensure_created(events)

        if event_type == "message_start":
            # 修正3：message_start.usage 为空 {}，不在此取 usage。
            pass

        elif event_type == "content_block_start":
            cb = data.get("content_block") or {}
            cbt = cb.get("type")
            if cbt == "text":
                self._start_message_item(events)
            elif cbt == "tool_use":
                self._start_tool_item(events, cb)
            elif cbt in ("thinking", "redacted_thinking"):
                # 修正2：thinking 块显式跳过，不产事件、不占 output_index。
                self.cur_item_kind = "thinking_skip"

        elif event_type == "content_block_delta":
            d = data.get("delta") or {}
            dt = d.get("type")
            if dt == "text_delta" and self.cur_item_kind == "message":
                txt = d.get("text", "")
                self.cur_text_buf += txt
                events.append(self._emit({
                    "type": "response.output_text.delta",
                    "item_id": self.cur_item_id,
                    "output_index": self.output_index,
                    "content_index": 0,
                    "delta": txt,
                    "logprobs": [],
                }))
            elif dt == "input_json_delta" and self.cur_item_kind == "function_call":
                partial = d.get("partial_json", "")
                if partial:
                    self.cur_args_buf += partial
                    events.append(self._emit({
                        "type": "response.function_call_arguments.delta",
                        "item_id": self.cur_item_id,
                        "output_index": self.output_index,
                        "delta": partial,
                    }))
            # thinking_delta / signature_delta：跳过

        elif event_type == "content_block_stop":
            if self.cur_item_kind == "message":
                self._stop_message_item(events)
            elif self.cur_item_kind == "function_call":
                self._stop_tool_item(events)
            # thinking_skip：不产事件
            self.cur_item_kind = None

        elif event_type == "message_delta":
            # 修正3：完整 usage 在 message_delta。
            usage = data.get("usage") or {}
            if "output_tokens" in usage and usage.get("output_tokens") is not None:
                self.usage_out = usage.get("output_tokens")
            if "input_tokens" in usage and usage.get("input_tokens") is not None:
                self.usage_in = usage.get("input_tokens")
            self.final_stop_reason = (data.get("delta") or {}).get("stop_reason")

        elif event_type == "message_stop":
            self._emit_completed(events)

        elif event_type == "ping":
            pass                                 # Responses 无 ping，丢弃

        return events

    def finalize(self) -> list:
        """流意外结束时补收尾。若尚未发过 completed 则补一个 response.completed。"""
        events: list = []
        if not self.sent_created:
            return events                        # 连 created 都没发，无从收尾
        if not self.completed:
            self._emit_completed(events)
        return events

    # ---- message item ----

    def _start_message_item(self, events: list) -> None:
        self.output_index += 1
        self.cur_item_kind = "message"
        self.cur_item_id = gen_message_id()
        self.cur_text_buf = ""
        events.append(self._emit({
            "type": "response.output_item.added",
            "output_index": self.output_index,
            "item": {
                "id": self.cur_item_id,
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            },
        }))
        events.append(self._emit({
            "type": "response.content_part.added",
            "item_id": self.cur_item_id,
            "output_index": self.output_index,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": [], "logprobs": []},
        }))

    def _stop_message_item(self, events: list) -> None:
        text = self.cur_text_buf
        events.append(self._emit({
            "type": "response.output_text.done",
            "item_id": self.cur_item_id,
            "output_index": self.output_index,
            "content_index": 0,
            "text": text,
            "logprobs": [],
        }))
        part = {"type": "output_text", "text": text, "annotations": [], "logprobs": []}
        events.append(self._emit({
            "type": "response.content_part.done",
            "item_id": self.cur_item_id,
            "output_index": self.output_index,
            "content_index": 0,
            "part": part,
        }))
        item = {
            "id": self.cur_item_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [part],
        }
        events.append(self._emit({
            "type": "response.output_item.done",
            "output_index": self.output_index,
            "item": item,
        }))
        self.completed_items.append(item)

    # ---- function_call item ----

    def _start_tool_item(self, events: list, cb: dict) -> None:
        self.output_index += 1
        self.cur_item_kind = "function_call"
        self.cur_item_id = gen_item_id()
        self.cur_call_id = cb.get("id") or gen_call_id()
        self.cur_tool_name = cb.get("name", "")
        self.cur_args_buf = ""
        events.append(self._emit({
            "type": "response.output_item.added",
            "output_index": self.output_index,
            "item": {
                "id": self.cur_item_id,
                "type": "function_call",
                "status": "in_progress",
                "call_id": self.cur_call_id,
                "name": self.cur_tool_name,
                "arguments": "",
            },
        }))

    def _stop_tool_item(self, events: list) -> None:
        args = self.cur_args_buf if self.cur_args_buf else "{}"
        events.append(self._emit({
            "type": "response.function_call_arguments.done",
            "item_id": self.cur_item_id,
            "name": self.cur_tool_name,
            "output_index": self.output_index,
            "arguments": args,
        }))
        item = {
            "id": self.cur_item_id,
            "type": "function_call",
            "status": "completed",
            "call_id": self.cur_call_id,
            "name": self.cur_tool_name,
            "arguments": args,
        }
        events.append(self._emit({
            "type": "response.output_item.done",
            "output_index": self.output_index,
            "item": item,
        }))
        self.completed_items.append(item)

    # ---- 收尾 ----

    def _emit_completed(self, events: list) -> None:
        if self.completed:
            return
        events.append(self._emit({
            "type": "response.completed",
            "response": self._skeleton("completed", "default",
                                       with_completed_at=True, with_usage=True),
        }))
        self.completed = True
