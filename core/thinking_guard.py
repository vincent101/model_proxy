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
