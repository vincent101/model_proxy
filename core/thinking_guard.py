"""thinking 块空签名剥离 + 400 分类器 + supply 级学习存储（零 server 依赖，纯函数层）。

设计记录：docs/designs/2026-10-28-thinking空签名剥离与supply级自适应.md
背景：混合路由池下 mcli（不验签、产出空签名 thinking 块）与 aigc（对历史
assistant thinking 块验签）同池；mcli 服务过的轮次在客户端历史留下空签名
thinking 块，之后原样重发打 aigc 必 400。已实证：整块删除（删签名键/伪造
签名均不可行）后 200。

自适应先例：core/server.py::SyntaxPreferenceStore + core/reasoning/codecs.py::
interpret_rejection（48h TTL、learn-from-400、不进 tried_set 不 cooldown 的
同 supply 重试三要素照抄）。
"""

import logging
import threading
import time

log = logging.getLogger(__name__)


def strip_empty_signature_thinking(body_json: dict) -> int:
    """删除 assistant 历史消息中空签名/缺失签名的 thinking 块（整块），返回删除块数。

    判据：type == "thinking" 且 not block.get("signature")（None 或 ""）。
    仅命中 mcli 产块形态；aigc 自签块 signature 非空不命中。
    redacted_thinking 不处理（见设计风险 R4）。幂等：重复调用删除 0 块。
    """
    msgs = body_json.get("messages") if isinstance(body_json, dict) else None
    if not isinstance(msgs, list):
        return 0
    removed = 0
    for msg in msgs:
        if not (isinstance(msg, dict) and msg.get("role") == "assistant"):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        kept = [b for b in content
                if not (isinstance(b, dict) and b.get("type") == "thinking"
                        and (not b.get("signature")
                             or str(b.get("signature", "")).startswith("resp:")))]
        removed += len(content) - len(kept)
        if len(kept) != len(content):
            msg["content"] = kept
    return removed


def is_invalid_signature_rejection(resp_body: bytes) -> bool:
    """识别 aigc 的 thinking 签名 400。匹配策略：lower + 去反引号后
    contains "invalid signature in thinking block"（覆盖带/不带反引号两种报文形态）。
    刻意精确：schema 层 Field required: signature 类报文不命中（fail-open，走现状路径）。
    """
    try:
        t = resp_body.decode("utf-8", errors="replace").lower().replace("`", "")
    except Exception:
        return False
    return "invalid signature in thinking block" in t


class ThinkingStripStore:
    """supply_id → {"at": float}，TTL 48h（对齐 SyntaxPreferenceStore 先例）。
    学到的值恒为"需要剥离"，无需 variant 字段；snapshot() 返回 bool。
    线程安全（Lock），内存态，重启丢失可接受（先例如此，重启后首请求吃一次 400
    再 learn，自愈）。"""

    _TTL = 48 * 3600

    def __init__(self):
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    def snapshot(self, supply_id: str) -> bool:
        """该 supply 当前是否学到了"需要剥离空签名 thinking 块"；过期或未学到返回 False。"""
        with self._lock:
            entry = self._cache.get(supply_id)
            return bool(entry and time.time() - entry["at"] < self._TTL)

    def learn(self, supply_id: str) -> None:
        with self._lock:
            self._cache[supply_id] = {"at": time.time()}
        log.info("thinking_strip.learn: supply=%r (cached 48h)", supply_id)


class ToolStrictStore:
    """supply_id → {"strict": bool, "at": float}，TTL 48h（对齐 SyntaxPreferenceStore）。

    语义：该 Responses 上游对未显式声明 strict 的工具应采用什么策略。
    - 缺省（未 learn）：False —— 出站写 strict:false，阻止上游对 optional
      schema 的静默严格化（2026-11-06 sol 实测会把 required 扩成全字段）。
    - learn(supply_id, True)：上游显式拒绝 strict:false（400 提及 strict）后
      学到，此后该 supply 出站写 strict:true（schema 不兼容时上游诚实 400）。

    线程安全（Lock），内存态，重启丢失可接受（重启后首请求吃一次 400 再
    learn，自愈——与 ThinkingStripStore 相同取舍）。
    """

    _TTL = 48 * 3600

    def __init__(self):
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    def snapshot(self, supply_id: str) -> bool:
        """该 supply 当前是否学到了需要 strict:true；过期或未学到返回 False。"""
        with self._lock:
            entry = self._cache.get(supply_id)
            return bool(entry and time.time() - entry["at"] < self._TTL
                        and entry["strict"])

    def learn(self, supply_id: str, strict: bool) -> None:
        with self._lock:
            self._cache[supply_id] = {"strict": strict, "at": time.time()}
        log.info("tool_strict.learn: supply=%r → strict=%r (cached 48h)",
                 supply_id, strict)


def is_strict_mode_rejection(resp_body: bytes) -> bool:
    """识别 Responses 上游对 strict:false 的拒绝（400 提及 strict/schema 约束）。

    匹配策略：lower 后 contains "strict"，或同时含 "additionalproperties" 与
    "required"。刻意保守：只捕获明确指向 strict 模式冲突的报文，普通 schema
    错误（如缺 type 字段）不命中，避免误 learn。
    """
    try:
        t = resp_body.decode("utf-8", errors="ignore").lower()
    except Exception:
        return False
    if "strict" in t:
        return True
    return "additionalproperties" in t and "required" in t


class CountTokensUnsupportedStore:
    """supply_id → {"at": float}，TTL 48h（对齐 ThinkingStripStore/ToolStrictStore）。

    语义：该 anthropic 上游不支持 `/v1/messages/count_tokens` 端点（实测 mcli 返 404）。
    - 未 learn：正常转发到上游（其他网关可能支持，不能预设不支持）。
    - learn 后：该 supply 的 count_tokens 请求直接本地返 404，不打上游
      （省一次无用往返 + 日志噪音；客户端行为不变——CC 拿不到就本地估算）。

    线程安全（Lock），内存态，重启丢失可接受（重启后首次请求吃一次 404 再 learn，
    自愈——与 ThinkingStripStore 相同取舍）。
    """

    _TTL = 48 * 3600

    def __init__(self):
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    def snapshot(self, supply_id: str) -> bool:
        """该 supply 是否已学到不支持 count_tokens；过期或未学到返回 False。"""
        with self._lock:
            entry = self._cache.get(supply_id)
            return bool(entry and time.time() - entry["at"] < self._TTL)

    def learn(self, supply_id: str) -> None:
        with self._lock:
            self._cache[supply_id] = {"at": time.time()}
        log.info("count_tokens_unsupported.learn: supply=%r (cached 48h)", supply_id)
