"""model_proxy 正向协议转换器：Anthropic /v1/messages → OpenAI Chat Completions。

严格照 tools/model_proxy/docs/proxy_translate_spec.md（正向规格）实现，字段映射不得自创。
纯标准库（json / hashlib / secrets），无网络 IO，可脱离 HTTP 单测。

模块划分（正向规格 §6.1）：
    A 请求转换   anthropic_to_openai_request(body, model_is_reasoning=True) -> (openai_body, ctx)
    B 非流式响应 openai_to_anthropic_response(resp, ctx=None) -> anthropic_dict
    C+D 流式     class AnthropicStreamAdapter: feed(chunk)->list[dict], finalize()->list[dict]
    辅助         truncate_tool_name / map_reasoning_effort / map_finish_reason /
                anthropic_image_to_data_url / anthropic_sse_bytes / gen_msg_id / gen_toolu_id
"""

import hashlib
import json
import logging
import secrets

logger = logging.getLogger(__name__)

# OpenAI 工具名上限（正向规格 §1.5.1）
OPENAI_MAX_TOOL_NAME_LENGTH = 64

# stop_reason 映射表（正向规格 §2.2）
_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
}


# ============================================================
# 辅助：id 生成（正向规格 §6.3）
# ============================================================

def gen_msg_id() -> str:
    """message id：msg_ 前缀 + 唯一。"""
    return "msg_" + secrets.token_hex(12)


def gen_toolu_id() -> str:
    """tool_use id 兜底：toolu_ 前缀 + 唯一。"""
    return "toolu_" + secrets.token_hex(12)


# ============================================================
# 辅助：SSE 事件序列化（正向规格 §3.3）
# ============================================================

def anthropic_sse_bytes(event: dict) -> bytes:
    """把一个 Anthropic 事件 dict 序列化为 SSE wire bytes。

    格式（§3.3）：
        event: <type>\n
        data: <紧凑JSON>\n
        \n
    event 行的值与 data JSON 里的 type 一致；JSON 用紧凑无空格。
    """
    etype = event.get("type", "")
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"event: {etype}\ndata: {data}\n\n".encode("utf-8")


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

class AnthropicStreamAdapter:
    """把 OpenAI SSE chunk 序列转换为 Anthropic 流式事件序列。

    用法：
        adapter = AnthropicStreamAdapter(ctx, model)
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
