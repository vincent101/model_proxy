"""model_proxy 多协议结构转换器。

职责边界：本文件只负责协议之间**结构字段**（messages/tools/tool_result/system/
content blocks/流式事件）的转换；reasoning 强度的解析与钳位由 `core.reasoning.*`
完成，本文件各 `*_request` 函数只接收调用方（server.py）已经算好的
`reasoning_fields` 片段，原地 merge 进目标 body，不自行判断/计算强度。

涵盖三组协议组合：

  §1 Anthropic ⇄ OpenAI Chat
      Anthropic /v1/messages 请求 → OpenAI Chat Completions；
      OpenAI Chat 响应（含流式）→ Anthropic。
        请求转换   anthropic_to_openai_request(body, reasoning_fields=None) -> (openai_body, ctx)
        非流式响应 openai_to_anthropic_response(resp, ctx=None) -> anthropic_dict
        流式       class OpenAIToAnthropicStreamAdapter: feed(chunk)->list, finalize()->list

  §2 Responses ⇄ Anthropic（客户端讲 Responses，后端为 Anthropic）
      OpenAI Responses 请求 → Anthropic /v1/messages；
      Anthropic 响应（含流式）→ Responses。
        请求转换   responses_to_anthropic_request(body, max_tokens_default=4096, reasoning_fields=None) -> anthropic_body
        非流式响应 anthropic_to_responses_response(resp, model, ...) -> responses_dict
        流式       class AnthropicToResponsesStreamAdapter: feed(type, data)->list, finalize()->list

  §3 Anthropic ⇄ Responses（客户端讲 Anthropic，后端为 Responses）
      Anthropic /v1/messages 请求 → OpenAI Responses；
      Responses 响应（含流式）→ Anthropic。
        请求转换   anthropic_to_responses_request(body, reasoning_fields=None) -> (responses_body, ctx)
        非流式响应 responses_to_anthropic_response(resp, ctx=None) -> anthropic_dict
        流式       class ResponsesToAnthropicStreamAdapter: feed(event)->list, finalize()->list

严格照 docs/designs/model_proxy_translate_spec.md 的规格实现，字段映射不得自创。
纯标准库（json / hashlib / secrets / time），无网络 IO，可脱离 HTTP 单测。

历史脚注：本文件由原 model_proxy_translate.py（§1/§2 正向）与
model_proxy_translate_reverse.py（§2 反向）合并而来，§3 为后续新增协议组合。

§2/§3 涉及 Anthropic SSE 解析的 3 处实测修正：
  修正1 wire format：Anthropic SSE 是 `event:xxx\\ndata:{json}`（冒号后无空格）。
      本转换器 feed(event_type, data) 接收的是已解析好的 (type, dict)，SSE 拆行由主文件负责；
      本文件的输出侧 responses_sse_bytes 产出 Responses 侧 `data: {json}\\n\\n`。
  修正2 thinking 块必现：每次响应首块永远是 thinking(index 0)，text/tool_use 从 index 1 起。
      本状态机将 thinking/redacted_thinking 块映射为 Responses reasoning item
      （cur_item_kind="reasoning"），占用一个 output_index，故 output_index 与
      Anthropic index 解耦但顺序一致（thinking 在前则 reasoning 占 index 0）。
  修正3 usage 落点：message_start.usage 为空 {}，完整 usage（input+output_tokens）在 message_delta。
      故 usage_in / usage_out 均从 message_delta 取，不依赖 message_start。
"""

import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)                       # 正向/反向侧日志


# ---------------------------------------------------------------------------
# OPT-07：translate 降级/缺 usage 限流 helper
# key=事件 kind，60s 窗口，首条全量 + 窗口末 suppressed=N 汇总一条。
# 进程退出丢 suppressed 计数（可接受，方案已认可）。
# budget_retry warn 不挂此限流器（保持 WARNING + 完整阶梯轨迹）。
# ---------------------------------------------------------------------------

class _RateLimitedLogger:
    """按 key 限流的日志辅助器。

    每个 key 独立 60s 窗口：
    - 窗口首条：全量输出（原 msg）
    - 窗口内后续：计数吞掉，不输出
    - 窗口过期后首次调用：先输出旧窗口 suppressed=N 汇总条，再输出新窗口首条全量

    线程安全：translate 调用均在请求线程内（ThreadingHTTPServer），各 key 的
    dict 操作用 dict.setdefault + 赋值，Python GIL 下原子性足够（最坏丢一条计数，
    不影响正确性）。
    """

    _WINDOW = 60.0  # 秒

    def __init__(self):
        self._state: dict[str, tuple[float, int]] = {}  # key -> (window_start, suppressed)

    def warning(self, key: str, log_obj, msg: str, *args) -> None:
        """限流 WARNING 调用。

        Args:
            key: 事件 kind（用于去重，同 key 在窗口内只出首条）
            log_obj: logger
            msg: 原始日志消息（含 %s 占位符）
            *args: msg 的格式化参数
        """
        now = time.time()
        entry = self._state.get(key)
        if entry is None:
            # 首次：全量输出，开窗口
            log_obj.warning(msg, *args)
            self._state[key] = (now, 0)
            return
        window_start, suppressed = entry
        if now - window_start >= self._WINDOW:
            # 窗口过期：先出旧窗口汇总，再开新窗口
            if suppressed > 0:
                log_obj.warning("%s suppressed=%d (window=%.0fs)",
                                key, suppressed, self._WINDOW)
            log_obj.warning(msg, *args)
            self._state[key] = (now, 0)
        else:
            # 窗口内：计数吞掉
            self._state[key] = (window_start, suppressed + 1)


_rl = _RateLimitedLogger()

# OpenAI 工具名上限（正向规格 §1.5.1）
OPENAI_MAX_TOOL_NAME_LENGTH = 64


class TerminalStatus(str, Enum):
    OPEN = "open"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    REFUSED = "refused"
    PAUSED = "paused"
    FAILED = "failed"


@dataclass(frozen=True)
class TerminalState:
    status: TerminalStatus
    reason: str


@dataclass(frozen=True)
class SSEEvent:
    raw: bytes
    event_type: str | None
    data: Any
    is_comment: bool = False
    is_done: bool = False


class SSEFramer:
    """增量 SSE framing；保留原始字节并严格解析业务事件。"""

    def __init__(self):
        self._buffer = bytearray()
        self.saw_business_event = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes, *, max_events: int | None = None) -> list[SSEEvent]:
        self._buffer.extend(chunk)
        events = []
        while max_events is None or len(events) < max_events:
            raw = bytes(self._buffer)
            lf = raw.find(b"\n\n")
            crlf = raw.find(b"\r\n\r\n")
            candidates = [(lf, 2), (crlf, 4)]
            candidates = [(p, n) for p, n in candidates if p >= 0]
            if not candidates:
                break
            pos, sep_len = min(candidates)
            block = bytes(self._buffer[:pos])
            framed = bytes(self._buffer[:pos + sep_len])
            del self._buffer[:pos + sep_len]
            event = self._parse(block, framed)
            if event is not None:
                if not event.is_comment:
                    self.saw_business_event = True
                events.append(event)
        return events

    def finish(self) -> list[SSEEvent]:
        if not self._buffer or not bytes(self._buffer).strip():
            self._buffer.clear()
            return []
        block = bytes(self._buffer)
        self._buffer.clear()
        # 完整末尾事件可无空行；只有 event/data 字段才视为完整。
        if not any(line.lstrip().startswith((b"event:", b"data:"))
                   for line in block.splitlines()):
            if all(not line.strip() or line.lstrip().startswith(b":")
                   for line in block.splitlines()):
                return [SSEEvent(block, None, None, is_comment=True)]
            raise TranslationError("malformed trailing SSE frame", reason="malformed_stream")
        return [self._parse(block, block)]

    @staticmethod
    def _parse(block: bytes, raw: bytes) -> SSEEvent | None:
        event_type = None
        data_lines = []
        has_field = False
        only_comments = True
        for line in block.splitlines():
            line = line.rstrip(b"\r")
            if not line:
                continue
            if line.startswith(b":"):
                continue
            only_comments = False
            if line.startswith(b"event:"):
                has_field = True
                event_type = line[6:].lstrip().decode("utf-8", "replace")
            elif line.startswith(b"data:"):
                has_field = True
                value = line[5:]
                if value.startswith(b" "):
                    value = value[1:]
                data_lines.append(value)
        if only_comments or not has_field:
            return SSEEvent(raw, None, None, is_comment=True)
        if not data_lines:
            raise TranslationError("SSE event has no data", reason="malformed_stream")
        payload = b"\n".join(data_lines)
        if payload == b"[DONE]":
            return SSEEvent(raw, event_type, None, is_done=True)
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            raise TranslationError("malformed SSE JSON", reason="malformed_stream") from exc
        data_type = data.get("type") if isinstance(data, dict) else None
        if event_type and data_type and event_type != data_type:
            raise TranslationError("SSE event/data type conflict", reason="malformed_stream")
        resolved = event_type or data_type
        # Chat Completions 的 data JSON 没有 type；由消费方按协议校验。
        return SSEEvent(raw, resolved, data)


class PassthroughTerminalTracker:
    """PASSTHROUGH 协议终态与 usage 旁路跟踪器。"""

    def __init__(self, source: str, acc: dict | None = None):
        self.source = source
        self.acc = acc if acc is not None else {}
        self.terminal = TerminalState(TerminalStatus.OPEN, "")
        self.confirmed = False
        self.saw_business_event = False
        self._anthropic_candidate: TerminalState | None = None

    def feed(self, event: SSEEvent) -> None:
        if event.is_comment:
            return
        self.saw_business_event = True
        if event.is_done:
            if self.source != "chat":
                raise TranslationError("unexpected [DONE]", reason="malformed_stream")
            if self._anthropic_candidate is None:
                raise TranslationError("[DONE] before finish_reason", reason="missing_stop_reason")
            self.terminal = self._anthropic_candidate
            self.confirmed = True
            return
        data = event.data if isinstance(event.data, dict) else {}
        typ = event.event_type or ""
        if self.source in ("anthropic", "responses") and not typ:
            raise TranslationError("SSE event type missing", reason="malformed_stream")
        if self.source == "anthropic":
            if typ == "message_start":
                usage = ((data.get("message") or {}).get("usage") or {})
                self._usage(usage)
            elif typ == "message_delta":
                self._usage(data.get("usage") or {})
                reason = (data.get("delta") or {}).get("stop_reason")
                if reason is not None:
                    self._anthropic_candidate = map_anthropic_terminal(reason)
                    self.acc["stop_reason"] = reason
            elif typ == "message_stop":
                if self._anthropic_candidate is None:
                    raise TranslationError("message_stop before stop_reason", reason="missing_stop_reason")
                self.terminal = self._anthropic_candidate
                self.confirmed = True
            elif typ == "error":
                self.terminal = TerminalState(TerminalStatus.FAILED, "upstream_error")
                self.confirmed = True
            elif typ == "content_block_start":
                if (data.get("content_block") or {}).get("type") in ("text", "tool_use"):
                    self.acc["stream_content"] = 1
            # 未知非终态事件原样容忍。
        elif self.source == "responses":
            if typ in ("response.completed", "response.incomplete"):
                response = data.get("response") or {}
                status = response.get("status") or typ.split(".", 1)[1]
                details = response.get("incomplete_details") or {}
                self.terminal = map_responses_terminal(status, details.get("reason"))
                self.confirmed = True
                self._usage(response.get("usage") or {})
                self.acc["stop_reason"] = status
            elif typ in ("response.failed", "response.error", "error"):
                self.terminal = TerminalState(TerminalStatus.FAILED, "upstream_error")
                self.confirmed = True
            elif typ == "response.output_item.added":
                if (data.get("item") or {}).get("type") in ("message", "function_call"):
                    self.acc["stream_content"] = 1
        else:
            raise TranslationError(f"unsupported stream source: {self.source}", reason="malformed_stream")

    def _usage(self, usage: dict) -> None:
        if usage.get("input_tokens") is not None:
            self.acc["usage_in"] = usage.get("input_tokens") or 0
        if usage.get("output_tokens") is not None:
            self.acc["usage_out"] = usage.get("output_tokens") or 0
        details = usage.get("output_tokens_details") or {}
        if details.get("reasoning_tokens") is not None:
            self.acc["usage_reasoning"] = details.get("reasoning_tokens") or 0

    def finalize(self) -> TerminalState:
        if self.confirmed:
            return self.terminal
        reason = "unexpected_eof" if self.saw_business_event else "empty_stream"
        raise TranslationError(f"stream ended without terminal: {reason}", reason=reason)


class TranslationError(Exception):
    """协议转换失败；携带 HTTP 与 failover 决策所需的稳定分类。"""

    def __init__(self, message: str, *, phase: str = "response", path: str = "",
                 source_type: str = "", reason: str = "upstream_error",
                 error_type: str = "", error_code: Any = None,
                 http_status: int = 502, retry_class: str = "none"):
        super().__init__(message)
        self.phase = phase
        self.path = path
        self.source_type = source_type
        self.reason = reason
        self.error_type = error_type
        self.error_code = error_code
        self.http_status = http_status
        self.retry_class = retry_class


_ANTHROPIC_TERMINALS = {
    "end_turn": TerminalState(TerminalStatus.COMPLETED, "end_turn"),
    "stop_sequence": TerminalState(TerminalStatus.COMPLETED, "end_turn"),
    "tool_use": TerminalState(TerminalStatus.COMPLETED, "tool_use"),
    "max_tokens": TerminalState(TerminalStatus.INCOMPLETE, "max_tokens"),
    "refusal": TerminalState(TerminalStatus.REFUSED, "refusal"),
    "pause_turn": TerminalState(TerminalStatus.PAUSED, "pause_turn"),
    "model_context_window_exceeded": TerminalState(
        TerminalStatus.FAILED, "model_context_window_exceeded"),
}
_RESPONSES_TERMINALS = {
    "completed": TerminalState(TerminalStatus.COMPLETED, "end_turn"),
    "failed": TerminalState(TerminalStatus.FAILED, "upstream_error"),
    "cancelled": TerminalState(TerminalStatus.FAILED, "upstream_error"),
}
_FINISH_REASON_MAP = {
    "stop": TerminalState(TerminalStatus.COMPLETED, "end_turn"),
    "length": TerminalState(TerminalStatus.INCOMPLETE, "max_tokens"),
    "tool_calls": TerminalState(TerminalStatus.COMPLETED, "tool_use"),
    "content_filter": TerminalState(TerminalStatus.REFUSED, "refusal"),
}


def map_anthropic_terminal(stop_reason) -> TerminalState:
    state = _ANTHROPIC_TERMINALS.get(stop_reason)
    if state is None:
        raise TranslationError(
            f"unknown Anthropic stop_reason: {stop_reason!r}", path="stop_reason",
            source_type="anthropic", reason=f"unknown:{stop_reason}")
    return state


def map_responses_terminal(status, incomplete_reason=None) -> TerminalState:
    if status == "incomplete":
        if incomplete_reason == "max_output_tokens":
            return TerminalState(TerminalStatus.INCOMPLETE, "max_tokens")
        return TerminalState(TerminalStatus.FAILED, f"unknown:{incomplete_reason}")
    state = _RESPONSES_TERMINALS.get(status)
    if state is None:
        raise TranslationError(
            f"unknown Responses status: {status!r}", path="status",
            source_type="responses", reason=f"unknown:{status}")
    return state


def map_chat_terminal(finish_reason) -> TerminalState:
    state = _FINISH_REASON_MAP.get(finish_reason)
    if state is None:
        raise TranslationError(
            f"unknown Chat finish_reason: {finish_reason!r}", path="choices[0].finish_reason",
            source_type="chat", reason=f"unknown:{finish_reason}")
    return state


def classify_upstream_error(error: dict | None) -> TranslationError:
    err = error if isinstance(error, dict) else {}
    error_type = str(err.get("type") or "")
    code = err.get("code")
    message = str(err.get("message") or "upstream request failed")
    key = f"{error_type} {code or ''}".lower()
    if any(x in key for x in ("invalid", "validation", "bad_request")):
        status, retry = 400, "none"
    elif any(x in key for x in ("auth", "unauthorized")):
        status, retry = 401, "configured"
    elif any(x in key for x in ("permission", "forbidden")):
        status, retry = 403, "configured"
    elif any(x in key for x in ("rate", "quota")):
        status, retry = 429, "configured"
    elif any(x in key for x in ("overload", "unavailable")):
        status, retry = 503, "configured"
    elif any(x in key for x in ("internal", "server_error")):
        status, retry = 502, "configured"
    elif any(x in key for x in ("capability", "unsupported")):
        status, retry = 422, "capability_mismatch"
    else:
        status, retry = 502, "none"
    return TranslationError(message, source_type="upstream", error_type=error_type,
                            error_code=code, http_status=status, retry_class=retry)

# reasoning_content 空回答兜底：content 空但 reasoning_content 非空时，把思考内容填进
# text block，避免客户端收到空 content 数组。可整体关闭（改此常量）。
_ENABLE_REASONING_FALLBACK = True
_REASONING_FALLBACK_PREFIX = "[模型仅返回思考过程，未生成正式回答]\n\n"


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


def gen_reasoning_id() -> str:
    return "rs_" + secrets.token_hex(16)      # reasoning item id


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
# reasoning 强度处理已迁移到 core.reasoning.*（ladder/capability/codecs/registry）。
# 本模块的 *_request 函数不再自己算强度/选语法，改为接收调用方（server.py）已算好的
# reasoning_fields dict，只负责 merge 进 body（详见各 *_request 函数签名变更）。
# ============================================================


# ============================================================
# 辅助：finish_reason 映射（正向规格 §2.2）
# ============================================================

def map_finish_reason(finish) -> str:
    """Chat finish_reason → Anthropic stop_reason；null/未知值显式失败。"""
    return map_chat_terminal(finish).reason


# ============================================================
# 辅助：预算截断判定（④b 反应式预算重试的检测谓词，架构审查 R1）
# ============================================================

def is_budget_truncated(target_protocol: str, raw_resp) -> bool:
    """在**原始上游响应**上判定「达到输出预算上限且正文缺失」（④b 重试触发条件）。

    target_protocol：响应所在协议（anthropic / chat / responses，即上游 supply 侧协议）。
    raw_resp：原始响应体（bytes/str/dict 均可）；无法解析一律返回 False（判不出就不重试，
    由调用方按正常路径处理/报错）。

    判定条件（三协议本质相同：思考与正文共享同一输出预算，预算耗尽即硬截断；
    「正文缺失」= 无 text 且无 tool_use/function_call，只有 thinking 或全空）：
      - anthropic：stop_reason=="max_tokens" 且 content 无 text/tool_use block；
      - chat：choices[0].finish_reason=="length"（与本模块 _FINISH_REASON_MAP 的
        "length"→"max_tokens" 映射同源）且 message.content 为空且无 tool_calls；
      - responses：status=="incomplete" 且 incomplete_details.reason==
        "max_output_tokens" 且 output 无文本项且无 function_call。

    必须在原始响应上判、不能在转换后的 anthropic 响应上判：chat 的空回答兜底
    （_ENABLE_REASONING_FALLBACK）会把 reasoning_content 填成 text block，转换后再判
    「无 text」恒为假，检测会被兜底掩盖。纯函数，无副作用；重试编排留在 server.py。
    """
    if isinstance(raw_resp, (bytes, bytearray, str)):
        try:
            raw_resp = json.loads(raw_resp)
        except (ValueError, TypeError):
            return False
    if not isinstance(raw_resp, dict):
        return False

    if target_protocol == "anthropic":
        if raw_resp.get("stop_reason") != "max_tokens":
            return False
        for block in raw_resp.get("content") or []:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "tool_use":
                return False
            if bt == "text" and (block.get("text") or "").strip():
                return False
        return True

    if target_protocol == "chat":
        choices = raw_resp.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        if choice.get("finish_reason") != "length":
            return False
        message = choice.get("message") or {}
        if (message.get("content") or "").strip():
            return False
        if message.get("tool_calls"):
            return False
        return True

    if target_protocol == "responses":
        if raw_resp.get("status") != "incomplete":
            return False
        if (raw_resp.get("incomplete_details") or {}).get("reason") != "max_output_tokens":
            return False
        for item in raw_resp.get("output") or []:
            if not isinstance(item, dict):
                continue
            it = item.get("type")
            if it == "function_call":
                return False
            if it == "message":
                for part in item.get("content") or []:
                    if (isinstance(part, dict)
                            and part.get("type") in ("output_text", "text")
                            and (part.get("text") or "").strip()):
                        return False
        return True

    return False


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
            _rl.warning("tool_result_image_downgrade", logger,
                        "tool_result image content downgraded to placeholder")
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
            _rl.warning("tool_result_multiblock_serialize", logger,
                        "tool_result multi-block content serialized to string")
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
                _rl.warning("unknown_role_dropped", logger,
                            "unknown message role dropped: %r", role)
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
                _rl.warning("unsupported_image_source", logger,
                            "unsupported image source dropped")
        elif btype == "tool_result":
            tool_result_msgs.append(_tool_result_to_openai(block))
        else:
            _rl.warning("unsupported_user_block", logger,
                        "unsupported user content block dropped: %r", btype)

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
                _rl.warning("unsupported_assistant_block", logger,
                            "unsupported assistant content block dropped: %r", btype)
    msg = {"role": "assistant"}
    msg["content"] = "\n".join(text_parts) if text_parts else None
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


# ============================================================
# 模块 A：请求转换 Anthropic → OpenAI（正向规格 §1）
# ============================================================

def _merge_reasoning_fields(target: dict, fields: "dict | None") -> None:
    """把 core.reasoning codec.encode() 产出的字段片段 merge 进 target（原地修改）。

    约定：value 为 None → 删除该 key；其余 → 设置该 key；fields 为空/None → 不动。
    与 core.reasoning.registry.apply_fields 语义一致（此处独立实现，保持
    translate.py 不反向依赖 core.reasoning，维持依赖单向性）。
    """
    for key, value in (fields or {}).items():
        if value is None:
            target.pop(key, None)
        else:
            target[key] = value


def anthropic_to_openai_request(body: dict, reasoning_fields: "dict | None" = None):
    """Anthropic /v1/messages body → (openai_body, ctx)。

    ctx = {
        "tool_name_mapping": dict[str,str],  # truncated → original，响应侧还原用
        "stream": bool,
        "request_model": str,                # 回填响应 model 字段
    }
    采用白名单：只转换规格 §1.1 列出字段，其余（container/mcp_servers 等）丢弃。
    reasoning_fields：调用方（server.py）用 core.reasoning 已经 decode→align→encode 好的
    reasoning 字段片段（ChatReasoningCodec.encode 产出），本函数只负责 merge，不再自己算强度。
    非 reasoning 模型场景由调用方传 None/{} 实现"不发 reasoning_effort"。
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

    # reasoning 字段（已由调用方算好，本函数只 merge）
    _merge_reasoning_fields(openai_body, reasoning_fields)

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
    """Chat 非流式响应 → Anthropic；严格校验 envelope 与终态。"""
    if not isinstance(resp, dict):
        raise TranslationError("Chat response must be an object", source_type="chat",
                               reason="malformed_response")
    if isinstance(resp.get("error"), dict):
        raise classify_upstream_error(resp.get("error"))
    if not isinstance(resp.get("choices"), list) or not resp["choices"]:
        raise TranslationError("Chat response.choices must be a non-empty array", path="choices",
                               source_type="chat", reason="malformed_response")
    if not isinstance(resp["choices"][0], dict) or not isinstance(resp["choices"][0].get("message"), dict):
        raise TranslationError("Chat choice.message must be an object", path="choices[0].message",
                               source_type="chat", reason="malformed_response")
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
            _rl.warning("tool_call_bad_json", logger,
                        "tool_call arguments not valid JSON, downgraded to {}")
        name = tool_name_mapping.get(fn.get("name", ""), fn.get("name", ""))  # 还原原名
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id") or gen_toolu_id(),
            "name": name,
            "input": parsed,
        })

    # ①b-chat 镜像：reasoning_content → thinking block（置前，对齐 anthropic 原生
    # "thinking 在前"约定）。仅在已有正文/工具块（content 非空场景）时镜像；
    # content 空场景由下方兜底把 reasoning_content 填成 text——两条路径互斥不双写。
    # signature 无来源（与 ①b 一致），不产出 signature 字段。
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip() and content_blocks:
        content_blocks.insert(0, {"type": "thinking", "thinking": reasoning})

    # 空回答兜底：content 与 tool_calls 都为空，但 reasoning_content 非空
    if _ENABLE_REASONING_FALLBACK and not content_blocks:
        if isinstance(reasoning, str) and reasoning.strip():
            content_blocks.append({
                "type": "text",
                "text": _REASONING_FALLBACK_PREFIX + reasoning,
            })
            _rl.warning("empty_content_fallback", logger,
                        "empty content fallback: filled reasoning_content into text block")

    # stop_reason 映射
    stop_reason = map_finish_reason(choice.get("finish_reason"))

    # usage 映射
    if not resp.get("usage"):
        _rl.warning("missing_usage_chat", logger,
                    "upstream chat completion response missing usage field, "
                    "anthropic usage will be all-zero")
    u = resp.get("usage") or {}
    anthropic_usage = {
        "input_tokens": u.get("prompt_tokens", 0),
        "output_tokens": u.get("completion_tokens", 0),
    }
    _rt = _extract_reasoning_tokens(u)      # 统一 helper，读 completion_tokens_details.reasoning_tokens
    if _rt:                                 # 有值才加 details，无值保持旧结构（不加空字段）
        anthropic_usage["output_tokens_details"] = {"reasoning_tokens": _rt}

    # content_filter_results：丢弃 + 记 log（§2.4）
    if resp.get("content_filter_results") or (choice.get("finish_reason") == "content_filter"):
        _rl.warning("content_filter", logger,
                    "content_filter triggered (dropped from anthropic response)")

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
        self._failed = False
        self.reasoning_buf = ""              # 累积 delta.reasoning_content
        self.produced_content_block = False  # 是否产出过 text/tool block（不含 reasoning 累积）
        self.thinking_emitted = False        # ①b-chat：reasoning_buf 是否已镜像为 thinking block
        self.reasoning_tokens = 0

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
    def _content_block_start_thinking(index: int) -> dict:
        # ①b-chat：reasoning_content→thinking 回传。signature 无来源（与 ①b 一致），
        # 不产出 signature 字段——已知限制。
        return {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "thinking", "thinking": ""},
        }

    @staticmethod
    def _content_block_delta_thinking(index: int, thinking: str) -> dict:
        return {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "thinking_delta", "thinking": thinking},
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
        _rt = _extract_reasoning_tokens(usage)
        if _rt:
            self.reasoning_tokens = _rt

    # ---- 核心：feed（§3.4） ----

    def feed(self, openai_chunk: dict) -> list:
        """喂一个 Chat chunk，返回 0..N 个 Anthropic 事件 dict。"""
        events = []
        if self._finalized or self._failed:
            return events
        if not isinstance(openai_chunk, dict):
            raise TranslationError("Chat SSE payload must be an object", source_type="chat",
                                   reason="malformed_stream")
        if isinstance(openai_chunk.get("error"), dict):
            self._failed = True
            err = classify_upstream_error(openai_chunk["error"])
            return [{"type": "error", "error": {
                "type": "invalid_request_error" if err.http_status < 500 else "api_error",
                "message": str(err)}}]

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
            self.produced_content_block = True
            self._flush_thinking_block(events)   # ①b-chat：thinking 置前于 text
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
            self.produced_content_block = True
            self._flush_thinking_block(events)   # ①b-chat：thinking 置前于 tool_use
            events.extend(self._handle_tool_calls_delta(delta["tool_calls"]))

        # (C.5) 思考增量：仅累积，不实时透传（finalize 时若从未产出内容才补块）
        if delta.get("reasoning_content"):
            self.reasoning_buf += delta["reasoning_content"]

        # (D) finish_reason 到达：仅记状态，不立即收尾
        if finish is not None:
            self.final_stop_reason = map_finish_reason(finish)

        # (E) 本 chunk 内带 usage
        self._absorb_usage(openai_chunk.get("usage"))

        return events

    def _flush_thinking_block(self, events: list) -> None:
        """①b-chat 镜像：首个正文/工具块之前，把累积的 reasoning_buf 以 thinking block 产出
        （index 在 text/tool 之前，对齐 anthropic 原生"thinking 在前"约定）。

        只在有正文/工具块（content 非空场景）时被调用；content 空场景由 finalize 兜底把
        reasoning_buf 填成 text，两条路径互斥不双写。流内 reasoning_content 分片先于
        content 到达（kimi/glm 实测序列），故在首个 content/tool 增量处一次性镜像；
        已产出正文块后再到的 reasoning_content 分片不再镜像（非标准交错，留在 buf 不双写）。
        """
        if self.thinking_emitted or not self.reasoning_buf.strip():
            return
        if self.block_open:
            events.append(self._content_block_stop(self.cur_index))
            self.block_open = False
        self.cur_index += 1
        self.cur_type = "thinking"
        self.block_open = True
        events.append(self._content_block_start_thinking(self.cur_index))
        events.append(self._content_block_delta_thinking(self.cur_index, self.reasoning_buf))
        events.append(self._content_block_stop(self.cur_index))
        self.block_open = False
        self.cur_type = None
        self.thinking_emitted = True
        self.reasoning_buf = ""   # 已镜像为 thinking，清空避免 finalize 兜底重复

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
        if self._failed:
            return []
        if self.final_stop_reason is None:
            self._failed = True
            return [{"type": "error", "error": {"type": "api_error",
                    "message": "Chat stream ended before finish_reason"}}]
        events = []
        # 若流内一个 chunk 都没喂过，也要保证 message_start 已发
        if not self.sent_message_start:
            events.append(self._message_start_event())
            self.sent_message_start = True
            events.append(self._ping_event())
        if self.block_open:
            events.append(self._content_block_stop(self.cur_index))
            self.block_open = False
        # 空回答兜底：从未产出任何 text/tool block，但累积了 reasoning_content
        if (not self.produced_content_block
                and _ENABLE_REASONING_FALLBACK
                and self.reasoning_buf.strip()):
            self.cur_index += 1
            events.append(self._content_block_start_text(self.cur_index))
            events.append(self._content_block_delta_text(
                self.cur_index, _REASONING_FALLBACK_PREFIX + self.reasoning_buf))
            events.append(self._content_block_stop(self.cur_index))
        events.append(self._message_delta_event())
        events.append(self._message_stop_event())
        return events

    def usage_tuple(self) -> tuple[int, int, int]:
        """统一 usage 读取接口：返回 (usage_in, usage_out, usage_reasoning)。

        供 server.py access 日志统一调用，避免按 adapter 类型分别取不同命名的属性。
        """
        return (self.input_tokens, self.output_tokens, self.reasoning_tokens)


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

def responses_to_anthropic_request(body: dict, max_tokens_default: int = 4096,
                                   reasoning_fields: "dict | None" = None) -> dict:
    """把 codex 的 Responses 请求体转换成 Anthropic /v1/messages 请求体。

    白名单策略：只转换反向规格 §1.1 表中字段，其余平台字段丢弃。
    reasoning_fields：调用方（server.py）用 core.reasoning 已经 decode→align→encode 好的
    reasoning 字段片段（AnthropicReasoningCodec.encode 产出，即 thinking/output_config），
    本函数只负责 merge，不再自己算强度/选语法。
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

    # reasoning 字段（已由调用方算好，本函数只 merge）
    _merge_reasoning_fields(ab, reasoning_fields)

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
                "id": item.get("call_id") or gen_toolu_id(),
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

def _extract_reasoning_tokens(usage: dict) -> int:
    """从任意上游协议 usage dict 防御性多路径读取 reasoning/thinking token 数。

    覆盖 chat / responses / anthropic 三协议已知路径（互不冲突，一个 usage 只命中一类）：
        usage.output_tokens_details.thinking_tokens        # anthropic
        usage.output_tokens_details.reasoning_tokens       # responses / anthropic
        usage.completion_tokens_details.reasoning_tokens   # chat（OpenAI 风格）
        usage.thinking_tokens                              # anthropic 顶层别名
    全部缺失/为 0 则返回 0（不臆造）。各 details 用 `or {}` 防 null（chat 上游实测
    output_tokens_details 为 null）。
    """
    u = usage or {}
    otd = u.get("output_tokens_details") or {}
    ctd = u.get("completion_tokens_details") or {}
    return (
        otd.get("thinking_tokens")
        or otd.get("reasoning_tokens")
        or ctd.get("reasoning_tokens")
        or u.get("thinking_tokens")
        or 0
    )


def _anthropic_usage_to_responses(usage: dict) -> dict:
    """Anthropic usage -> Responses usage（反向规格 §2.3）。"""
    if not usage:
        _rl.warning("missing_usage_responses", logger,
                    "anthropic response missing usage field, responses usage will be all-zero")
    u = usage or {}
    in_tok = u.get("input_tokens", 0) or 0
    out_tok = u.get("output_tokens", 0) or 0
    return {
        "input_tokens": in_tok,
        "input_tokens_details": {"cached_tokens": u.get("cache_read_input_tokens", 0) or 0},
        "output_tokens": out_tok,
        "output_tokens_details": {"reasoning_tokens": _extract_reasoning_tokens(u)},
        "total_tokens": in_tok + out_tok,
    }


def anthropic_to_responses_response(resp: dict, model: str,
                                    reasoning_effort=None, tools_echo=None) -> dict:
    """把 Anthropic 非流式响应转换成 Responses 响应。"""
    if not isinstance(resp, dict):
        raise TranslationError("Anthropic response must be an object", source_type="anthropic",
                               reason="malformed_response")
    if resp.get("type") == "error" or isinstance(resp.get("error"), dict):
        err = classify_upstream_error(resp.get("error"))
        return _responses_terminal_response(model, TerminalStatus.FAILED, err,
                                            reasoning_effort, tools_echo)
    if not isinstance(resp.get("content"), list):
        raise TranslationError("Anthropic response.content must be an array", path="content",
                               source_type="anthropic", reason="malformed_response")
    state = map_anthropic_terminal(resp.get("stop_reason"))
    if state.status in (TerminalStatus.REFUSED, TerminalStatus.PAUSED, TerminalStatus.FAILED):
        err = TranslationError(f"Anthropic response ended: {state.reason}",
                               source_type="anthropic", reason=state.reason)
        return _responses_terminal_response(model, TerminalStatus.FAILED, err,
                                            reasoning_effort, tools_echo)
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
        elif bt == "thinking":
            thinking_text = block.get("thinking", "") or ""
            output.append({
                "type": "reasoning",
                "id": gen_reasoning_id(),
                "summary": [{"type": "summary_text", "text": thinking_text}] if thinking_text else [],
                "status": "completed",
            })
        elif bt == "redacted_thinking":
            output.append({
                "type": "reasoning",
                "id": gen_reasoning_id(),
                "summary": [],
                "status": "completed",
            })
        # 其他 block 忽略

    now = int(time.time())
    response_status = "incomplete" if state.status == TerminalStatus.INCOMPLETE else "completed"
    result = {
        "id": gen_response_id(),
        "object": "response",
        "created_at": now,
        "status": response_status,
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
    if response_status == "incomplete":
        result["incomplete_details"] = {"reason": "max_output_tokens"}
        result["completed_at"] = None
    return result


def _responses_terminal_response(model: str, status: TerminalStatus, error: TranslationError,
                                 reasoning_effort=None, tools_echo=None) -> dict:
    now = int(time.time())
    return {
        "id": gen_response_id(), "object": "response", "created_at": now,
        "status": "failed", "background": False, "completed_at": None, "model": model,
        "output": [], "parallel_tool_calls": True,
        "reasoning": {"effort": reasoning_effort, "summary": None},
        "service_tier": "default", "store": True,
        "text": {"format": {"type": "text"}, "verbosity": "medium"},
        "tool_choice": "auto", "tools": tools_echo or [], "truncation": "disabled",
        "usage": _anthropic_usage_to_responses({}), "metadata": {},
        "error": {"type": error.error_type or "server_error", "code": error.error_code,
                  "message": str(error)},
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
        self.failed = False

        self.output_index = -1
        self.cur_item_kind = None                # "message" | "function_call" | "reasoning"
        self.cur_item_id = None
        self.cur_text_buf = ""

        self.cur_call_id = None
        self.cur_tool_name = None
        self.cur_args_buf = ""

        self.usage_in = 0
        self.usage_out = 0
        self.usage_reasoning = 0
        self.final_stop_reason = None
        self.completed_items = []                # 已完成的 output item，供 response.completed
        self.cur_reasoning_summary_buf = ""       # reasoning item 正文累积（thinking_delta）

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
                "output_tokens_details": {"reasoning_tokens": self.usage_reasoning},
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
        if self.completed or self.failed:
            return events
        if not isinstance(data, dict):
            raise TranslationError("Anthropic SSE payload must be an object", source_type="anthropic",
                                   reason="malformed_stream")
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
                self._start_reasoning_item(events)

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
            elif dt == "thinking_delta" and self.cur_item_kind == "reasoning":
                txt = d.get("thinking", "")
                if txt:
                    self.cur_reasoning_summary_buf += txt
                    events.append(self._emit({
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": self.cur_item_id,
                        "output_index": self.output_index,
                        "summary_index": 0,
                        "delta": txt,
                    }))
            # signature_delta：跳过（Responses 协议无对应字段）

        elif event_type == "content_block_stop":
            if self.cur_item_kind == "message":
                self._stop_message_item(events)
            elif self.cur_item_kind == "function_call":
                self._stop_tool_item(events)
            elif self.cur_item_kind == "reasoning":
                self._stop_reasoning_item(events)
            self.cur_item_kind = None

        elif event_type == "message_delta":
            # 修正3：完整 usage 在 message_delta。
            usage = data.get("usage") or {}
            if "output_tokens" in usage and usage.get("output_tokens") is not None:
                self.usage_out = usage.get("output_tokens")
            if "input_tokens" in usage and usage.get("input_tokens") is not None:
                self.usage_in = usage.get("input_tokens")
            # 流式场景 message_delta.usage 目前观察到的结构不含 thinking 明细，
            # 防御性读取：真实有值时自然带出，无值维持 0（不臆造）。
            self.usage_reasoning = _extract_reasoning_tokens(usage) or self.usage_reasoning
            stop_reason = (data.get("delta") or {}).get("stop_reason")
            if stop_reason is not None:
                self.final_stop_reason = stop_reason

        elif event_type == "message_stop":
            try:
                state = map_anthropic_terminal(self.final_stop_reason)
            except TranslationError as exc:
                self._emit_failed(events, exc)
            else:
                if state.status == TerminalStatus.COMPLETED:
                    self._emit_completed(events)
                elif state.status == TerminalStatus.INCOMPLETE:
                    self._emit_incomplete(events)
                else:
                    self._emit_failed(events, TranslationError(
                        f"Anthropic stream ended: {state.reason}", source_type="anthropic",
                        reason=state.reason))

        elif event_type == "error":
            self._emit_failed(events, classify_upstream_error(data.get("error")))

        elif event_type == "ping":
            pass                                 # Responses 无 ping，显式 allowlist

        else:
            raise TranslationError(f"unknown Anthropic stream event: {event_type!r}",
                                   source_type="anthropic", reason=f"unknown:{event_type}")

        return events

    def finalize(self) -> list:
        """无 message_stop 的 EOF 必须 response.failed。"""
        events: list = []
        if self.completed or self.failed:
            return events
        self._ensure_created(events)
        self._emit_failed(events, TranslationError(
            "Anthropic stream ended before message_stop", source_type="anthropic",
            reason="unexpected_eof"))
        return events

    def usage_tuple(self) -> tuple[int, int, int]:
        """统一 usage 读取接口：返回 (usage_in, usage_out, usage_reasoning)。

        供 server.py access 日志统一调用，避免按 adapter 类型分别取不同命名的属性。
        """
        return (self.usage_in, self.usage_out, self.usage_reasoning)

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

    # ---- reasoning item ----

    def _start_reasoning_item(self, events: list) -> None:
        self.output_index += 1
        self.cur_item_kind = "reasoning"
        self.cur_item_id = gen_reasoning_id()
        self.cur_reasoning_summary_buf = ""
        events.append(self._emit({
            "type": "response.output_item.added",
            "output_index": self.output_index,
            "item": {
                "id": self.cur_item_id,
                "type": "reasoning",
                "status": "in_progress",
                "summary": [],
            },
        }))

    def _stop_reasoning_item(self, events: list) -> None:
        buf = self.cur_reasoning_summary_buf
        if buf:
            events.append(self._emit({
                "type": "response.reasoning_summary_part.done",
                "item_id": self.cur_item_id,
                "output_index": self.output_index,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": buf},
            }))
        summary = [{"type": "summary_text", "text": buf}] if buf else []
        item = {
            "id": self.cur_item_id,
            "type": "reasoning",
            "status": "completed",
            "summary": summary,
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
        if self.completed or self.failed:
            return
        events.append(self._emit({
            "type": "response.completed",
            "response": self._skeleton("completed", "default",
                                       with_completed_at=True, with_usage=True),
        }))
        self.completed = True

    def _emit_incomplete(self, events: list) -> None:
        if self.completed or self.failed:
            return
        response = self._skeleton("incomplete", "default", with_usage=True)
        response["incomplete_details"] = {"reason": "max_output_tokens"}
        events.append(self._emit({"type": "response.incomplete", "response": response}))
        self.completed = True

    def _emit_failed(self, events: list, error: TranslationError) -> None:
        if self.completed or self.failed:
            return
        response = self._skeleton("failed", "default", with_usage=True)
        response["error"] = {"type": error.error_type or "server_error",
                             "code": error.error_code, "message": str(error)}
        events.append(self._emit({"type": "response.failed", "response": response}))
        self.failed = True


# ############################################################################
# §3 Anthropic → Responses（新组合，anthropic 请求 → responses 上游）
#
# 复用 §1 的辅助（truncate_tool_name /
# anthropic_image_to_data_url），产出 Responses 扁平结构。响应侧把 Responses
# 响应/事件还原为 Anthropic 格式，工具名经 ctx["tool_name_mapping"] 还原。
# ############################################################################

# ============================================================================
# 辅助：messages → Responses input items（§3，方向与 _input_to_messages 相反）
# ============================================================================

def _messages_to_input(messages, tool_name_mapping: dict) -> list:
    """Anthropic messages → Responses input items 数组。

    - user 文本/图片 → message(role=user, content=[input_text|input_image])
    - assistant 文本 → message(role=assistant, content=[output_text])
    - assistant tool_use → function_call item（name 经 tool_name_mapping 截断映射）
    - user tool_result → function_call_output item
    """
    items: list = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")

        if role == "user":
            if isinstance(content, str):
                if content:
                    items.append({
                        "type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": content}],
                    })
                continue
            normal_parts = []          # 文本/图片 → 一条 user message
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    normal_parts.append({"type": "input_text", "text": block.get("text", "")})
                elif bt == "image":
                    url = anthropic_image_to_data_url(block.get("source"))
                    if url is not None:
                        normal_parts.append({"type": "input_image", "image_url": url})
                    else:
                        _rl.warning("unsupported_image_source_a2r", logger,
                                    "unsupported image source dropped (a2r)")
                elif bt == "tool_result":
                    inner = block.get("content")
                    if isinstance(inner, str):
                        out = inner
                    elif isinstance(inner, list):
                        texts = [b.get("text", "") for b in inner
                                 if isinstance(b, dict) and b.get("type") == "text"]
                        out = "\n".join(texts) if texts else json.dumps(inner, ensure_ascii=False)
                    else:
                        out = "" if inner is None else json.dumps(inner, ensure_ascii=False)
                    items.append({
                        "type": "function_call_output",
                        "call_id": block.get("tool_use_id", ""),
                        "output": out,
                    })
                else:
                    _rl.warning("unsupported_user_block_a2r", logger,
                                "unsupported user content block dropped (a2r): %r", bt)
            if normal_parts:
                items.append({"type": "message", "role": "user", "content": normal_parts})

        elif role == "assistant":
            if isinstance(content, str):
                if content:
                    items.append({
                        "type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    })
                continue
            text_parts = []
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    text_parts.append({"type": "output_text", "text": block.get("text", "")})
                elif bt == "tool_use":
                    original = block.get("name", "")
                    truncated = truncate_tool_name(original)
                    if truncated != original:
                        tool_name_mapping[truncated] = original
                    items.append({
                        "type": "function_call",
                        "call_id": block.get("id", ""),
                        "name": truncated,
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    })
                elif bt in ("thinking", "redacted_thinking"):
                    logger.debug("thinking block dropped in assistant message (a2r)")
                else:
                    _rl.warning("unsupported_assistant_block_a2r", logger,
                                "unsupported assistant content block dropped (a2r): %r", bt)
            if text_parts:
                items.append({"type": "message", "role": "assistant", "content": text_parts})
        else:
            _rl.warning("unknown_role_dropped_a2r", logger,
                        "unknown message role dropped (a2r): %r", role)
    return items


def _a2r_translate_tool_choice(tc):
    """Anthropic tool_choice → Responses tool_choice。未知形态保守 'auto'。"""
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
        return {"type": "function", "name": truncate_tool_name(tc.get("name", ""))}
    return "auto"


# ============================================================================
# 模块 A''：请求转换 Anthropic → Responses（§3.1）
# ============================================================================

def anthropic_to_responses_request(body: dict, reasoning_fields: "dict | None" = None):
    """Anthropic /v1/messages body → (responses_body, ctx)。

    ctx = {"tool_name_mapping": dict[str,str], "request_model": str}
    白名单：只转换列出字段，其余丢弃。
    reasoning_fields：调用方（server.py）用 core.reasoning 已经 decode→align→encode 好的
    reasoning 字段片段（ResponsesReasoningCodec.encode 产出，即 reasoning.effort），
    本函数只负责 merge，不再自己算强度。非 reasoning 模型场景由调用方传 None/{} 实现。
    """
    responses_body: dict = {}
    ctx = {"tool_name_mapping": {}, "request_model": ""}

    # model 透传（会被主流程覆盖成 target_model）
    if "model" in body:
        responses_body["model"] = body["model"]
        ctx["request_model"] = body["model"]

    # system（字符串或 block 数组）→ instructions（纯字符串）
    system = body.get("system")
    if isinstance(system, str):
        if system:
            responses_body["instructions"] = system
    elif isinstance(system, list):
        text_parts = [b.get("text", "") for b in system
                      if isinstance(b, dict) and b.get("type") == "text"]
        if text_parts:
            responses_body["instructions"] = "\n".join(text_parts)

    # messages → input items（工具名映射在此收集）
    tool_name_mapping: dict = {}
    responses_body["input"] = _messages_to_input(body.get("messages"), tool_name_mapping)

    # max_tokens → max_output_tokens
    if "max_tokens" in body:
        responses_body["max_output_tokens"] = body["max_tokens"]

    # reasoning 字段（已由调用方算好，本函数只 merge）
    _merge_reasoning_fields(responses_body, reasoning_fields)

    # tools → Responses 扁平 function（复用 §1 _translate_tools 拿 mapping，再摊平）
    if body.get("tools"):
        openai_tools, tmap = _translate_tools(body["tools"])
        flat = []
        for t in openai_tools:
            fn = t.get("function") or {}
            flat.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        responses_body["tools"] = flat
        # 合并 tools 侧的截断映射（tool_use item 侧的已在 _messages_to_input 收集）
        tool_name_mapping.update(tmap)

    # tool_choice 四态
    if "tool_choice" in body:
        responses_body["tool_choice"] = _a2r_translate_tool_choice(body["tool_choice"])

    # stop_sequences 丢弃（Responses 无对应字段）
    if "stop_sequences" in body:
        logger.debug("stop_sequences dropped (a2r): Responses 无对应字段")

    # temperature / top_p / stream 同名透传
    if "temperature" in body:
        responses_body["temperature"] = body["temperature"]
    if "top_p" in body:
        responses_body["top_p"] = body["top_p"]
    if "stream" in body:
        responses_body["stream"] = body["stream"]

    ctx["tool_name_mapping"] = tool_name_mapping
    return responses_body, ctx


# ============================================================================
# 模块 B''：非流式响应转换 Responses → Anthropic（§3.1）
# ============================================================================

def _extract_reasoning_thinking_text(item: dict) -> str:
    """从 Responses reasoning item 提取 thinking 文本（①b reasoning→thinking 回传）。

    双通道兼容（以 glm 真实样本为准 + openai 官方结构）：
    - glm 通道：item.content[] 中 type=="reasoning_text" 的 text
    - openai 官方通道：item.summary[] 中 type=="summary_text" 的 text
    多 part 用 "\\n\\n" 连接。
    已知限制：anthropic thinking block 的 signature 在转换侧无来源
    （正向丢 signature_delta），故不产出 signature 字段。
    """
    parts: list = []
    for part in item.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "reasoning_text":
            t = part.get("text") or ""
            if t:
                parts.append(t)
    for part in item.get("summary") or []:
        if isinstance(part, dict) and part.get("type") == "summary_text":
            t = part.get("text") or ""
            if t:
                parts.append(t)
    return "\n\n".join(parts)


def responses_to_anthropic_response(resp: dict, ctx: dict = None) -> dict:
    """Responses 非流式响应 → Anthropic 响应；失败终态绝不伪装成功。"""
    if not isinstance(resp, dict):
        raise TranslationError("Responses response must be an object", path="$",
                               source_type="responses", reason="malformed_response")
    # 部分兼容网关省略 status；具备合法 output 时视为 completed。
    status = resp.get("status", "completed")
    state = map_responses_terminal(
        status, (resp.get("incomplete_details") or {}).get("reason"))
    if state.status == TerminalStatus.FAILED:
        err = resp.get("error") or {}
        classified = classify_upstream_error(err)
        classified.reason = state.reason
        if status == "incomplete" and not err:
            raise TranslationError(
                f"Responses response incomplete: {state.reason}", source_type="responses",
                reason=state.reason, http_status=classified.http_status,
                retry_class=classified.retry_class)
        raise classified
    output_value = resp.get("output")
    if not isinstance(output_value, list):
        raise TranslationError("Responses response.output must be an array", path="output",
                               source_type="responses", reason="malformed_response")
    ctx = ctx or {}
    tool_name_mapping = ctx.get("tool_name_mapping", {})
    request_model = ctx.get("request_model", "")

    content_blocks = []
    has_tool_use = False
    for item in resp.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        it = item.get("type")
        if it == "message":
            for part in item.get("content", []) or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    content_blocks.append({"type": "text", "text": part.get("text", "")})
        elif it == "function_call":
            has_tool_use = True
            raw_args = item.get("arguments", "") or "{}"
            try:
                parsed = json.loads(raw_args)
            except (ValueError, TypeError):
                parsed = {}
                _rl.warning("function_call_bad_json_r2a", logger,
                            "function_call arguments not valid JSON, downgraded to {} (r2a)")
            name = tool_name_mapping.get(item.get("name", ""), item.get("name", ""))  # 还原
            content_blocks.append({
                "type": "tool_use",
                "id": item.get("call_id") or gen_toolu_id(),
                "name": name,
                "input": parsed if isinstance(parsed, dict) else {},
            })
        elif it == "reasoning":
            # ①b：reasoning→thinking 回传（glm content 通道 + openai summary 通道）
            thinking = _extract_reasoning_thinking_text(item)
            if thinking:
                content_blocks.append({"type": "thinking", "thinking": thinking})
        else:
            raise TranslationError(f"unknown Responses output item type: {it!r}", path="output[].type",
                                   source_type="responses", reason=f"unknown:{it}")

    if state.status == TerminalStatus.INCOMPLETE:
        stop_reason = "max_tokens"
    else:
        stop_reason = "tool_use" if has_tool_use else "end_turn"

    u = resp.get("usage") or {}
    anthropic_usage = {
        "input_tokens": u.get("input_tokens", 0) or 0,
        "output_tokens": u.get("output_tokens", 0) or 0,
    }
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens")
    if cached:
        anthropic_usage["cache_read_input_tokens"] = cached
    _rt = _extract_reasoning_tokens(u)      # 读 output_tokens_details.reasoning_tokens
    if _rt:
        anthropic_usage["output_tokens_details"] = {"reasoning_tokens": _rt}

    return {
        "id": resp.get("id") or gen_msg_id(),
        "type": "message",
        "role": "assistant",
        "model": request_model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": anthropic_usage,
    }


# ============================================================================
# 模块 C''+D''：流式状态机 Responses 事件 → Anthropic 事件（§3.1）
# ============================================================================

class ResponsesToAnthropicStreamAdapter:
    """喂 Responses SSE 事件，产出 Anthropic 流式事件序列。

    用法：
        adapter = ResponsesToAnthropicStreamAdapter(ctx, model)
        for ev in adapter.feed(event_type, data):   # data 已 json.loads
            write(anthropic_sse_bytes(ev))
        for ev in adapter.finalize():                # 流意外结束补收尾
            write(anthropic_sse_bytes(ev))

    块索引管理（对齐 OpenAIToAnthropicStreamAdapter）：cur_index 从 -1 起，
    每开一个 block +1，单调递增；content_block_start/stop 配对。
    """

    def __init__(self, ctx: dict = None, model: str = ""):
        self.ctx = ctx or {}
        self.tool_name_mapping = self.ctx.get("tool_name_mapping", {})
        self.model = model or self.ctx.get("request_model", "")

        self.sent_message_start = False
        self.block_open = False
        self.cur_index = -1
        self.cur_type = None                          # "text" | "tool_use" | "thinking"
        self.final_stop_reason = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.message_id = gen_msg_id()
        self._finalized = False
        self._completed = False
        self._failed = False
        self.usage_reasoning = 0

    # ---- 事件构造 helper（复用 Anthropic 事件形态） ----

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
                "usage": {},          # message_start.usage 为空占位，completed 时经 message_delta 更新
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
    def _content_block_start_thinking(index: int) -> dict:
        # ①b：reasoning→thinking 回传。signature 无来源（正向丢 signature_delta），
        # 不产出 signature 字段——已知限制。
        return {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "thinking", "thinking": ""},
        }

    @staticmethod
    def _content_block_delta_thinking(index: int, thinking: str) -> dict:
        return {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "thinking_delta", "thinking": thinking},
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

    def _ensure_message_start(self, events: list) -> None:
        if not self.sent_message_start:
            events.append(self._message_start_event())
            self.sent_message_start = True
            events.append(self._ping_event())

    # ---- 核心：feed ----

    def feed(self, event_type: str, data: dict) -> list:
        events: list = []
        if self._completed or self._failed:
            return events
        if not isinstance(data, dict):
            raise TranslationError("Responses SSE payload must be an object", source_type="responses",
                                   reason="malformed_stream")
        data = data or {}

        if event_type in ("response.created", "response.in_progress"):
            self._ensure_message_start(events)

        elif event_type == "response.output_item.added":
            self._ensure_message_start(events)
            item = data.get("item") or {}
            it = item.get("type")
            if it == "message":
                # 开一个 text block（时机对齐正向：text 首个 delta 前开亦可，这里 added 时开）
                if self.block_open:
                    events.append(self._content_block_stop(self.cur_index))
                self.cur_index += 1
                self.cur_type = "text"
                self.block_open = True
                events.append(self._content_block_start_text(self.cur_index))
            elif it == "function_call":
                if self.block_open:
                    events.append(self._content_block_stop(self.cur_index))
                self.cur_index += 1
                self.cur_type = "tool_use"
                self.block_open = True
                original = self.tool_name_mapping.get(item.get("name", ""), item.get("name", ""))
                tool_id = item.get("call_id") or gen_toolu_id()
                events.append(self._content_block_start_tool(self.cur_index, tool_id, original))
            elif it == "reasoning":
                # ①b：开 thinking block（reasoning item 在 output 中位于 message 之前，
                # 对齐 anthropic 原生"thinking 在前"约定）
                if self.block_open:
                    events.append(self._content_block_stop(self.cur_index))
                self.cur_index += 1
                self.cur_type = "thinking"
                self.block_open = True
                events.append(self._content_block_start_thinking(self.cur_index))

        elif event_type in ("response.reasoning_text.delta",           # glm 通道（实测词表）
                            "response.reasoning_summary_text.delta"):  # openai 官方通道
            self._ensure_message_start(events)
            if self.cur_type != "thinking" or not self.block_open:
                if self.block_open:
                    events.append(self._content_block_stop(self.cur_index))
                self.cur_index += 1
                self.cur_type = "thinking"
                self.block_open = True
                events.append(self._content_block_start_thinking(self.cur_index))
            txt = data.get("delta", "")
            if txt:
                # obfuscation 字段（glm 特有混淆标记）忽略，不写入 thinking block
                events.append(self._content_block_delta_thinking(self.cur_index, txt))

        elif event_type in ("response.reasoning_text.done",
                            "response.reasoning_summary_text.done"):
            if self.cur_type == "thinking" and self.block_open:
                events.append(self._content_block_stop(self.cur_index))
                self.block_open = False

        elif event_type == "response.output_text.delta":
            self._ensure_message_start(events)
            if self.cur_type != "text" or not self.block_open:
                if self.block_open:
                    events.append(self._content_block_stop(self.cur_index))
                self.cur_index += 1
                self.cur_type = "text"
                self.block_open = True
                events.append(self._content_block_start_text(self.cur_index))
            txt = data.get("delta", "")
            if txt:
                events.append(self._content_block_delta_text(self.cur_index, txt))

        elif event_type == "response.function_call_arguments.delta":
            frag = data.get("delta", "")
            if frag and self.cur_type == "tool_use" and self.block_open:
                events.append(self._content_block_delta_input_json(self.cur_index, frag))

        elif event_type in ("response.output_text.done",
                             "response.content_part.done",
                             "response.output_item.done",
                             "response.function_call_arguments.done",
                             "response.content_part.added"):
            pass                                      # delta 已累积内容，忽略

        elif event_type in ("response.completed", "response.incomplete"):
            resp = data.get("response") or {}
            status = resp.get("status") or event_type.removeprefix("response.")
            state = map_responses_terminal(
                status, (resp.get("incomplete_details") or {}).get("reason"))
            if state.status == TerminalStatus.FAILED:
                events.extend(self._emit_error(classify_upstream_error(resp.get("error") or {
                    "type": "server_error", "message": f"Responses ended: {state.reason}"})))
                return events
            u = resp.get("usage") or {}
            if u.get("input_tokens") is not None:
                self.input_tokens = u.get("input_tokens") or 0
            if u.get("output_tokens") is not None:
                self.output_tokens = u.get("output_tokens") or 0
            self.usage_reasoning = _extract_reasoning_tokens(u) or self.usage_reasoning
            if state.status == TerminalStatus.INCOMPLETE:
                self.final_stop_reason = "max_tokens"
            elif self.final_stop_reason is None:
                self.final_stop_reason = "tool_use" if self.cur_type == "tool_use" else "end_turn"
            events.extend(self._emit_finish())

        elif event_type in ("response.failed", "error"):
            container = data.get("response") or data
            events.extend(self._emit_error(classify_upstream_error(container.get("error") or container)))

        elif event_type not in {
                "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
                "response.function_call_arguments.done", "response.content_part.added",
                "response.content_part.done", "response.output_item.done",
                "response.output_text.done", "response.reasoning_text.done",
                "response.reasoning_summary_text.done"}:
            raise TranslationError(f"unknown Responses stream event: {event_type!r}",
                                   source_type="responses", reason=f"unknown:{event_type}")

        return events

    def _emit_error(self, error: TranslationError) -> list:
        if self._completed or self._failed:
            return []
        self._failed = True
        return [{
            "type": "error",
            "error": {
                "type": "invalid_request_error" if error.http_status < 500 else "api_error",
                "message": str(error),
            },
        }]

    def _emit_finish(self) -> list:
        events: list = []
        if self._completed or self._failed:
            return events
        self._ensure_message_start(events)
        if self.block_open:
            events.append(self._content_block_stop(self.cur_index))
            self.block_open = False
        events.append(self._message_delta_event())
        events.append(self._message_stop_event())
        self._completed = True
        return events

    def finalize(self) -> list:
        """流意外结束补收尾。幂等。"""
        if self._finalized:
            return []
        self._finalized = True
        if self._completed or self._failed:
            return []
        return self._emit_error(TranslationError(
            "Responses stream ended before a terminal event", source_type="responses",
            reason="unexpected_eof"))

    def usage_tuple(self) -> tuple[int, int, int]:
        """统一 usage 读取接口：返回 (usage_in, usage_out, usage_reasoning)。

        供 server.py access 日志统一调用，避免按 adapter 类型分别取不同命名的属性。
        """
        return (self.input_tokens, self.output_tokens, self.usage_reasoning)
