"""model_proxy 双向协议转换器（合并版）。

本文件由原 model_proxy_translate.py（正向）与 model_proxy_translate_reverse.py
（反向）合并而来，涵盖两个方向的协议转换职责：

  §1 Anthropic ⇄ OpenAI Chat（正向）
      Anthropic /v1/messages 请求 → OpenAI Chat Completions；
      OpenAI Chat 响应（含流式）→ Anthropic。
        A   请求转换   anthropic_to_openai_request(body, model_is_reasoning=True) -> (openai_body, ctx)
        B   非流式响应 openai_to_anthropic_response(resp, ctx=None) -> anthropic_dict
        C+D 流式       class OpenAIToAnthropicStreamAdapter: feed(chunk)->list, finalize()->list

  §2 Responses ⇄ Anthropic（反向）
      OpenAI Responses 请求 → Anthropic /v1/messages；
      Anthropic 响应（含流式）→ Responses。
        A'   请求转换   responses_to_anthropic_request(body, max_tokens_default=4096) -> anthropic_body
        B'   非流式响应 anthropic_to_responses_response(resp, model, ...) -> responses_dict
        C'+D' 流式      class AnthropicToResponsesStreamAdapter: feed(type, data)->list, finalize()->list

严格照 docs/model_proxy_translate_spec.md（合并后的双向规格）实现，字段映射不得自创。
纯标准库（json / hashlib / secrets / time），无网络 IO，可脱离 HTTP 单测。

反向侧 3 处对规格 §2（原反向 §3）假设的实测修正：
  修正1 wire format：Anthropic SSE 是 `event:xxx\\ndata:{json}`（冒号后无空格）。
      本转换器 feed(event_type, data) 接收的是已解析好的 (type, dict)，SSE 拆行由主文件负责；
      本文件的输出侧 responses_sse_bytes 产出 Responses 侧 `data: {json}\\n\\n`。
  修正2 thinking 块必现：每次响应首块永远是 thinking(index 0)，text/tool_use 从 index 1 起。
      本状态机对 thinking/redacted_thinking 块显式跳过（cur_item_kind="thinking_skip"），
      不产出 Responses 事件、不占 output_index，故 output_index 独立计数、与 Anthropic index 解耦。
  修正3 usage 落点：message_start.usage 为空 {}，完整 usage（input+output_tokens）在 message_delta。
      故 usage_in / usage_out 均从 message_delta 取，不依赖 message_start。
"""

import hashlib
import json
import logging
import secrets
import time

logger = logging.getLogger(__name__)                       # 正向侧日志
logger_reverse = logging.getLogger("model_proxy.translate_reverse")  # 反向侧日志

# OpenAI 工具名上限（正向规格 §1.5.1）
OPENAI_MAX_TOOL_NAME_LENGTH = 64

# stop_reason 映射表（正向规格 §2.2）
_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
}


# ############################################################################
# §0 公共辅助：id 生成 / SSE 序列化 / 通用映射
# ############################################################################

# ------------------------------------------------------------------
# id 生成（正向规格 §6.3）
# ------------------------------------------------------------------

def gen_msg_id() -> str:
    """message id：msg_ 前缀 + 唯一（正向侧，12 字节 hex）。"""
    return "msg_" + secrets.token_hex(12)


def gen_toolu_id() -> str:
    """tool_use id 兜底：toolu_ 前缀 + 唯一（正向侧）。"""
    return "toolu_" + secrets.token_hex(12)


# ------------------------------------------------------------------
# id 生成（反向规格 §6.3）
# ------------------------------------------------------------------

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


# ------------------------------------------------------------------
# SSE 序列化
# ------------------------------------------------------------------

def anthropic_sse_bytes(event: dict) -> bytes:
    """把一个 Anthropic 事件 dict 序列化为 SSE wire bytes（正向规格 §3.3）。

    格式：
        event: <type>\n
        data: <紧凑JSON>\n
        \n
    event 行的值与 data JSON 里的 type 一致；JSON 用紧凑无空格。
    """
    etype = event.get("type", "")
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"event: {etype}\ndata: {data}\n\n".encode("utf-8")


def responses_sse_bytes(data: dict) -> bytes:
    """把一个 Responses 事件 dict 序列化为 SSE 字节（反向规格 §3.3）。

    格式：`data: <紧凑JSON>\\n\\n`（无 event: 行、无 [DONE] 哨兵，中文不转义）。
    data 需已含 "type" 键与已填好的 "sequence_number"。
    """
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return ("data: " + payload + "\n\n").encode("utf-8")


# ############################################################################
# §1 Anthropic ⇄ OpenAI Chat（正向）
# ############################################################################

# ============================================================
# 辅助：工具名截断（正向规格 §1.5.1）
# ============================================================

def truncate_tool_name(name: str) -> str:
    """OpenAI 工具名上限 64，超长则 name[:55] + '_' + sha256前8位。"""
    if len(name) <= OPENAI_MAX_TOOL_NAME_LENGTH:
        return name
    h = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{name[:55]}_{h}"  # 55 + 1 + 8 = 64


# ============================================================
# 辅助：reasoning_effort 映射（正向规格 §1.4）
# ============================================================

def map_reasoning_effort(body: dict):
    """thinking + output_config.effort → reasoning_effort（low/medium/high 或 None）。"""
    oc = body.get("output_config") or {}
    effort = oc.get("effort")
    if effort in ("low", "medium", "high"):
        return effort
    if effort in ("max", "xhigh"):
        return "high"  # 网关无 max/xhigh，降级为 high
    thinking = body.get("thinking") or {}
    ttype = thinking.get("type")
    if ttype == "enabled":
        b = thinking.get("budget_tokens", 10000)
        if b < 2000:
            return "low"
        if b >= 32000:
            return "high"
        return "medium"
    if ttype == "adaptive" and not effort:
        return "medium"  # adaptive 无 effort 时给中档
    return None


# ============================================================
# 辅助：finish_reason 映射（正向规格 §2.2）
# ============================================================

def map_finish_reason(finish) -> str:
    """OpenAI finish_reason → Anthropic stop_reason。null/其他 → end_turn。"""
    if finish is None:
        return "end_turn"
    return _FINISH_REASON_MAP.get(finish, "end_turn")


# ============================================================
# 辅助：图片转 data url（正向规格 §1.3.1）
# ============================================================

def anthropic_image_to_data_url(source: dict):
    """Anthropic image source → OpenAI image_url 的 url 字符串；无法转换返回 None。"""
    if not isinstance(source, dict):
        return None
    stype = source.get("type")
    if stype == "base64":
        media_type = source.get("media_type", "image/jpeg")
        return f'data:{media_type};base64,{source.get("data", "")}'
    if stype == "url":
        return source.get("url", "")
    return None


# ============================================================
# 辅助：tools 转换（正向规格 §1.5）
# ============================================================

def _translate_tools(anthropic_tools):
    """返回 (openai_tools, tool_name_mapping)。"""
    openai_tools = []
    tool_name_mapping = {}  # truncated → original
    for idx, tool in enumerate(anthropic_tools or []):
        original = tool.get("name") or f"litellm_unnamed_tool_{idx}"
        truncated = truncate_tool_name(original)
        if truncated != original:
            tool_name_mapping[truncated] = original
        func = {
            "name": truncated,
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        }
        openai_tools.append({"type": "function", "function": func})
    return openai_tools, tool_name_mapping


# ============================================================
# 辅助：tool_choice 三态转换（正向规格 §1.6）
# ============================================================

def _translate_tool_choice(tc):
    """Anthropic tool_choice → OpenAI tool_choice。未知形态保守设 'auto'。"""
    if not isinstance(tc, dict):
        return "auto"
    t = tc.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "none":
        return "none"
    if t == "tool":
        return {
            "type": "function",
            "function": {"name": truncate_tool_name(tc.get("name", ""))},
        }
    return "auto"  # 其他：保守降级


# ============================================================
# 辅助：tool_result block → role:tool 消息（正向规格 §1.3.3）
# ============================================================

def _tool_result_to_openai(block: dict) -> dict:
    """一个 tool_result → 恰好一条 role:tool 消息（一个 tool_call_id）。"""
    tool_call_id = block.get("tool_use_id", "")
    inner = block.get("content")
    if isinstance(inner, str):
        content = inner
    elif isinstance(inner, list):
        text_blocks = [b for b in inner if isinstance(b, dict) and b.get("type") == "text"]
        image_blocks = [b for b in inner if isinstance(b, dict) and b.get("type") == "image"]
        if len(inner) == 1 and text_blocks:
            content = text_blocks[0].get("text", "")
        elif len(inner) == 1 and image_blocks:
            # 保守策略（§1.3.3 边界）：role:tool 图片支持未实测，降级为占位文本
            content = "[image omitted]"
            logger.warning("tool_result image content downgraded to placeholder")
        else:
            # 多块：文本原样、图片降级为占位文本，序列化为字符串数组
            parts = []
            for b in inner:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    parts.append({"type": "text", "text": b.get("text", "")})
                elif b.get("type") == "image":
                    parts.append({"type": "text", "text": "[image omitted]"})
            content = json.dumps(parts, ensure_ascii=False)
            logger.warning("tool_result multi-block content serialized to string")
    else:
        content = ""
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


# ============================================================
# 辅助：system 转换（正向规格 §1.2）
# ============================================================

def _system_to_openai_message(system):
    """system（字符串或 content block 数组）→ role:system 消息 dict，或 None。"""
    if not system:
        return None
    if isinstance(system, str):
        return {"role": "system", "content": system}
    if isinstance(system, list):
        text_parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        return {"role": "system", "content": "\n".join(text_parts)}
    return None


# ============================================================
# 辅助：messages 转换（正向规格 §1.3）
# ============================================================

def _translate_messages(messages):
    """Anthropic messages → OpenAI messages（不含 system，system 由调用方 insert 到 index 0）。"""
    out = []
    for msg in messages or []:
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            out.extend(_translate_user_message(content))
        elif role == "assistant":
            out.append(_translate_assistant_message(content))
        else:
            # 未知 role：原样保留 role + 尽量取字符串 content
            if isinstance(content, str):
                out.append({"role": role, "content": content})
            else:
                logger.warning("unknown message role dropped: %r", role)
    return out


def _translate_user_message(content):
    """role==user 的 content → 0..N 条 OpenAI 消息（§1.3.1）。"""
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    if not isinstance(content, list):
        return []

    normal_parts = []      # 文本/图片 → 归到一条 user 消息
    tool_result_msgs = []  # 每个 tool_result → 一条独立 role:tool 消息
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            normal_parts.append({"type": "text", "text": block.get("text", "")})
        elif btype == "image":
            url = anthropic_image_to_data_url(block.get("source"))
            if url is not None:
                normal_parts.append({"type": "image_url", "image_url": {"url": url}})
            else:
                logger.warning("unsupported image source dropped")
        elif btype == "tool_result":
            tool_result_msgs.append(_tool_result_to_openai(block))
        else:
            logger.warning("unsupported user content block dropped: %r", btype)

    result = list(tool_result_msgs)  # 先 tool_result，再 normal（§1.3.1）
    if normal_parts:
        if len(normal_parts) == 1 and normal_parts[0].get("type") == "text":
            result.append({"role": "user", "content": normal_parts[0]["text"]})
        else:
            result.append({"role": "user", "content": normal_parts})
    return result


def _translate_assistant_message(content):
    """role==assistant 的 content → 一条 OpenAI 消息（§1.3.2）。"""
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    text_parts = []
    tool_calls = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": truncate_tool_name(block.get("name", "")),
                        # tool_use.input 是 dict，OpenAI arguments 要求 JSON 字符串
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                })
            elif btype in ("thinking", "redacted_thinking"):
                # native 端点不吃 thinking block，丢弃（§1.3.2）
                logger.debug("thinking block dropped in assistant message")
            else:
                logger.warning("unsupported assistant content block dropped: %r", btype)
    msg = {"role": "assistant"}
    msg["content"] = "\n".join(text_parts) if text_parts else None
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


# ============================================================
# 模块 A：请求转换 Anthropic → OpenAI（正向规格 §1）
# ============================================================

def anthropic_to_openai_request(body: dict, model_is_reasoning: bool = True):
    """Anthropic /v1/messages body → (openai_body, ctx)。

    ctx = {
        "tool_name_mapping": dict[str,str],  # truncated → original，响应侧还原用
        "stream": bool,
        "request_model": str,                # 回填响应 model 字段
    }
    采用白名单：只转换规格 §1.1 列出字段，其余（container/mcp_servers 等）丢弃。
    model_is_reasoning=False 时不发 reasoning_effort（非 reasoning 模型带此参数可能 400）。
    """
    openai_body = {}
    ctx = {"tool_name_mapping": {}, "stream": False, "request_model": ""}

    # model：原样透传（上游 model_map 已处理）
    if "model" in body:
        openai_body["model"] = body["model"]
        ctx["request_model"] = body["model"]

    # max_tokens → max_completion_tokens
    if "max_tokens" in body:
        openai_body["max_completion_tokens"] = body["max_tokens"]

    # messages（含 system insert 到 index 0）
    messages = _translate_messages(body.get("messages"))
    sys_msg = _system_to_openai_message(body.get("system"))
    if sys_msg is not None:
        messages.insert(0, sys_msg)
    openai_body["messages"] = messages

    # reasoning_effort（仅 reasoning 模型）
    if model_is_reasoning:
        effort = map_reasoning_effort(body)
        if effort is not None:
            openai_body["reasoning_effort"] = effort

    # tools
    if body.get("tools"):
        openai_tools, tool_name_mapping = _translate_tools(body["tools"])
        openai_body["tools"] = openai_tools
        ctx["tool_name_mapping"] = tool_name_mapping

    # tool_choice
    if "tool_choice" in body:
        openai_body["tool_choice"] = _translate_tool_choice(body["tool_choice"])

    # stop_sequences → stop
    if "stop_sequences" in body:
        openai_body["stop"] = body["stop_sequences"]

    # temperature / top_p 原样透传
    if "temperature" in body:
        openai_body["temperature"] = body["temperature"]
    if "top_p" in body:
        openai_body["top_p"] = body["top_p"]

    # metadata.user_id → user（可选）
    metadata = body.get("metadata") or {}
    if metadata.get("user_id"):
        openai_body["user"] = metadata["user_id"]

    # stream 与 usage（§1.7）
    stream = bool(body.get("stream"))
    openai_body["stream"] = stream
    ctx["stream"] = stream
    if stream:
        openai_body["stream_options"] = {"include_usage": True}

    return openai_body, ctx


# ============================================================
# 模块 B：非流式响应转换 OpenAI → Anthropic（正向规格 §2）
# ============================================================

def openai_to_anthropic_response(resp: dict, ctx: dict = None) -> dict:
    """OpenAI 非流式响应 → Anthropic 响应 dict（补齐必需字段）。"""
    ctx = ctx or {}
    tool_name_mapping = ctx.get("tool_name_mapping", {})
    request_model = ctx.get("request_model", "")

    choices = resp.get("choices") or []
    choice = choices[0] if choices else {}       # n>1 只取 choices[0]（§5.3）
    message = choice.get("message") or {}

    content_blocks = []

    # 文本
    text = message.get("content")
    if text:  # 非空字符串
        content_blocks.append({"type": "text", "text": text})

    # 工具调用
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments", "") or "{}"
        try:
            parsed = json.loads(raw_args)
        except (ValueError, TypeError):
            parsed = {}  # 参数非法 JSON 降级为空对象
            logger.warning("tool_call arguments not valid JSON, downgraded to {}")
        name = tool_name_mapping.get(fn.get("name", ""), fn.get("name", ""))  # 还原原名
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id") or gen_toolu_id(),
            "name": name,
            "input": parsed,
        })

    # stop_reason 映射
    stop_reason = map_finish_reason(choice.get("finish_reason"))

    # usage 映射
    if not resp.get("usage"):
        logger.warning(
            "upstream chat completion response missing usage field, "
            "anthropic usage will be all-zero"
        )
    u = resp.get("usage") or {}
    anthropic_usage = {
        "input_tokens": u.get("prompt_tokens", 0),
        "output_tokens": u.get("completion_tokens", 0),
    }

    # content_filter_results：丢弃 + 记 log（§2.4）
    if resp.get("content_filter_results") or (choice.get("finish_reason") == "content_filter"):
        logger.info("content_filter triggered (dropped from anthropic response)")

    return {
        "id": resp.get("id") or gen_msg_id(),
        "type": "message",
        "role": "assistant",
        "model": request_model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,  # 网关未返回命中序列，固定 null
        "usage": anthropic_usage,
    }


# ============================================================
# 模块 C+D：流式状态机（正向规格 §3 + §4）
# ============================================================

class OpenAIToAnthropicStreamAdapter:
    """把 OpenAI SSE chunk 序列转换为 Anthropic 流式事件序列。

    用法：
        adapter = OpenAIToAnthropicStreamAdapter(ctx, model)
        for chunk in openai_chunks:      # 已 json.loads 的 dict
            for ev in adapter.feed(chunk):
                write(anthropic_sse_bytes(ev))
        for ev in adapter.finalize():     # [DONE] 或流结束时
            write(anthropic_sse_bytes(ev))

    块索引管理（§3.5）：cur_index 从 -1 起，首块 open 时 +1 → 0；单调递增；
    切块先对旧块 content_block_stop（用旧 index），再 +1，再 content_block_start（新 index）。
    工具分片重组（§4）：openai_index → anthropic_index 映射；id/name 只在首片出现；
    arguments/partial_json 分片原样透传不拼接、不校验 JSON。
    """

    def __init__(self, ctx: dict = None, model: str = ""):
        self.ctx = ctx or {}
        self.tool_name_mapping = self.ctx.get("tool_name_mapping", {})
        self.model = model or self.ctx.get("request_model", "")

        self.sent_message_start = False
        self.block_open = False
        self.cur_index = -1
        self.cur_type = None  # "text" | "tool_use"
        self.final_stop_reason = None
        self.output_tokens = 0
        self.input_tokens = 0
        self.message_id = gen_msg_id()
        self.openai_index_to_anthropic_index = {}
        self._finalized = False

    # ---- 事件构造 helper（§3.2） ----

    def _message_start_event(self) -> dict:
        return {
            "type": "message_start",
            "message": {
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": self.model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": self.input_tokens,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }

    @staticmethod
    def _ping_event() -> dict:
        return {"type": "ping"}

    @staticmethod
    def _content_block_start_text(index: int) -> dict:
        return {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text", "text": ""},
        }

    @staticmethod
    def _content_block_start_tool(index: int, tool_id: str, name: str) -> dict:
        return {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}},
        }

    @staticmethod
    def _content_block_delta_text(index: int, text: str) -> dict:
        return {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        }

    @staticmethod
    def _content_block_delta_input_json(index: int, partial_json: str) -> dict:
        return {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": partial_json},
        }

    @staticmethod
    def _content_block_stop(index: int) -> dict:
        return {"type": "content_block_stop", "index": index}

    def _message_delta_event(self) -> dict:
        usage = {"output_tokens": self.output_tokens}
        if self.input_tokens > 0:
            usage["input_tokens"] = self.input_tokens
        return {
            "type": "message_delta",
            "delta": {
                "stop_reason": self.final_stop_reason or "end_turn",
                "stop_sequence": None,
            },
            "usage": usage,
        }

    @staticmethod
    def _message_stop_event() -> dict:
        return {"type": "message_stop"}

    # ---- usage 吸收（统一入口） ----

    def _absorb_usage(self, usage: dict) -> None:
        if not usage:
            return
        if usage.get("prompt_tokens"):
            self.input_tokens = usage.get("prompt_tokens", 0)
        if usage.get("completion_tokens") is not None:
            self.output_tokens = usage.get("completion_tokens", self.output_tokens)

    # ---- 核心：feed（§3.4） ----

    def feed(self, openai_chunk: dict) -> list:
        """喂一个 OpenAI chunk，返回 0..N 个 Anthropic 事件 dict。"""
        events = []

        # (0) 首次：发 message_start（+ 紧跟一个 ping）
        if not self.sent_message_start:
            # 若首帧带 usage.prompt_tokens 可回填 input_tokens
            usage0 = openai_chunk.get("usage") or {}
            self._absorb_usage(usage0)
            events.append(self._message_start_event())
            self.sent_message_start = True
            events.append(self._ping_event())

        choices = openai_chunk.get("choices") or []
        choice = choices[0] if choices else None

        # (A) 末尾只含 usage 的 chunk（choices 为空）
        if choice is None:
            self._absorb_usage(openai_chunk.get("usage"))
            return events

        delta = choice.get("delta") or {}
        finish = choice.get("finish_reason")

        # (B) 文本增量
        if delta.get("content"):
            if self.cur_type != "text":
                if self.block_open:
                    events.append(self._content_block_stop(self.cur_index))
                self.cur_index += 1
                self.cur_type = "text"
                self.block_open = True
                events.append(self._content_block_start_text(self.cur_index))
            events.append(self._content_block_delta_text(self.cur_index, delta["content"]))

        # (C) 工具增量（§4）
        if delta.get("tool_calls"):
            events.extend(self._handle_tool_calls_delta(delta["tool_calls"]))

        # (D) finish_reason 到达：仅记状态，不立即收尾
        if finish is not None:
            self.final_stop_reason = map_finish_reason(finish)

        # (E) 本 chunk 内带 usage
        self._absorb_usage(openai_chunk.get("usage"))

        return events

    # ---- 工具分片重组（§4.2） ----

    def _handle_tool_calls_delta(self, tool_calls) -> list:
        events = []
        for tc in tool_calls or []:
            oai_idx = tc.get("index")

            # (1) 新工具：该 oai_idx 首次出现
            if oai_idx not in self.openai_index_to_anthropic_index:
                if self.block_open:
                    events.append(self._content_block_stop(self.cur_index))
                self.cur_index += 1
                self.cur_type = "tool_use"
                self.block_open = True
                self.openai_index_to_anthropic_index[oai_idx] = self.cur_index
                fn = tc.get("function") or {}
                original_name = self.tool_name_mapping.get(
                    fn.get("name", ""), fn.get("name", "")
                )
                tool_id = tc.get("id") or gen_toolu_id()
                events.append(
                    self._content_block_start_tool(self.cur_index, tool_id, original_name)
                )

            # (2) arguments 片段 → input_json_delta（原样透传，不拼接不校验）
            fn = tc.get("function") or {}
            frag = fn.get("arguments")
            if frag:  # 空字符串不发
                a_idx = self.openai_index_to_anthropic_index[oai_idx]
                events.append(self._content_block_delta_input_json(a_idx, frag))
        return events

    # ---- 收尾：finalize（§3.4 F） ----

    def finalize(self) -> list:
        """[DONE]/流结束时调用，返回收尾事件。幂等（重复调用不重发）。"""
        if self._finalized:
            return []
        self._finalized = True
        events = []
        # 若流内一个 chunk 都没喂过，也要保证 message_start 已发
        if not self.sent_message_start:
            events.append(self._message_start_event())
            self.sent_message_start = True
            events.append(self._ping_event())
        if self.block_open:
            events.append(self._content_block_stop(self.cur_index))
            self.block_open = False
        events.append(self._message_delta_event())
        events.append(self._message_stop_event())
        return events


# ############################################################################
# §2 Responses ⇄ Anthropic（反向）
# ############################################################################

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
        logger_reverse.warning(
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

class AnthropicToResponsesStreamAdapter:
    """喂 Anthropic 流式事件，产出 Responses 流式事件序列。

    用法：
        adapter = AnthropicToResponsesStreamAdapter(model="gpt-5.6-sol", ctx={"tools":[...], "reasoning_effort":"low"})
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
