"""
tools/model_proxy/core/commands.py — 内建 in-band 指令层

设计文档：docs/designs/2026-08-04-in-band-route-command-design.md（已 confirmed）

职责边界（§7.3，必须严格遵守，这是命令层被批准的前提条件）：
    内建命令层只允许操作「代理自身的路由/观测状态」，且只允许纯本地操作。
    禁止：执行外部命令、读写代理配置/sidecar 以外的文件、代理请求转发、任何
    需要网络的动作。本模块内任何新增代码都不得违反这条边界。

本模块包含三部分：
    1. 指令匹配规则（§1.4/§2.2）：与 tests/test_command_match_rules.py 同一份实现
       （该测试文件 import 本模块，不再自持第二份逻辑）。
    2. sidecar 存储（§4.5/§5.4）：session_overrides.json 的加载、mtime 热重载、
       写入、7 天清理、last_seen 内存记账。sidecar 是唯一来源。
    3. 命令层骨架（§7.3）：命令名 → handler 注册表，目前只注册 `$route` 一个命令。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# §1.4 / §2.2 指令匹配规则（参考实现迁移自 tests/test_command_match_rules.py，
# 该测试文件现在 import 本模块，两处不再各存一份，避免漂移）。
# ---------------------------------------------------------------------------

CMD_PREFIX = "$route"

# 与 Claudian src/utils/context.ts 的 XML_CONTEXT_PATTERN 完全同源。
# 六个标签名必须完整覆盖，缺一个即在该场景失效。
# 锚定 "\n\n<tag" + [\s>] 结尾，用于区分 <current_note> 与 <current_note_foo>。
XML_CONTEXT_PATTERN = re.compile(
    r"\n\n<(?:current_note|editor_selection|editor_cursor"
    r"|context_files|canvas_selection|browser_selection)[\s>]")


def last_text_block(content):
    """级1：定位用户这一轮实际打的那句话。

    只取最后一个 type=="text" 块，**不拼接**。
    理由（实测）：CLI 会把 <system-reminder> 作为独立的前置 text block 注入；
    拼接会让首 token 变成 <system-reminder> 且引入换行，判定必然失败。

    注意：与 core/translate.py 内「拼接全部 text 块」的口径**故意不同**——
    那边要的是「这条消息的全部文本」（传给上游），这边要的是「用户这轮打的话」。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return texts[-1] if texts else None
    return None


def strip_trailing_context(text):
    """级2：剥离 Claudian 追加在用户输入之后的 XML 上下文标签。

    只截首个匹配之前的内容，**不做全局替换**——用户正文里若本就含这些标签字样，
    全局替换会改变正文语义、放大误判面。
    """
    m = XML_CONTEXT_PATTERN.search(text)
    return text[: m.start()].strip() if m else text.strip()


def parse_route_command(content):
    """返回 (是否为指令, 参数 or None)。

    参数语义：None=查询当前 route；"reset"=清除 override；其他=目标 route id。

    判定规则（§1.4）：整条消息单行 + 首 token 精确等于 $route + token 数 <= 2。
    任一不满足即 fail-open（照常转发，绝不吞用户消息）。
    """
    text = last_text_block(content)
    if text is None:
        return False, None
    text = strip_trailing_context(text)

    if "\n" in text:                      # 规则2：单行
        return False, None
    tokens = text.split()
    if not tokens:
        return False, None
    if tokens[0] != CMD_PREFIX:           # 规则3：首 token 精确匹配（大小写敏感）
        return False, None
    if len(tokens) > 2:                   # 规则4：token 数 <= 2
        return False, None
    return True, (tokens[1] if len(tokens) == 2 else None)


def extract_last_user_input_content(body_json: dict):
    """从 Responses input 数组取最后一条 user message 的 content。

    只支持 type=="message" 且 role=="user" 的 item；content 兼容三种形态：
    - 字符串：原样返回（官方 Responses API 允许）；
    - 数组：与 commands.last_text_block 同语义，取最后一个 input_text 块；
    - 其他：返回 None（fail-open，调用方照常转发）。

    同样只看最后一条 user message（与 Anthropic 版 extract_last_user_message_content
    对齐），避免历史指令被反复命中；末尾若先是 assistant/function_call item，则跳过
    它们继续向前找最近的 user message。
    """
    inputs = body_json.get("input") if isinstance(body_json, dict) else None
    if not isinstance(inputs, list):
        return None
    for item in reversed(inputs):
        if not (isinstance(item, dict) and item.get("type") == "message"
                and item.get("role") == "user"):
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "input_text"]
            if texts:
                return texts[-1]
        return None
    return None


def extract_last_user_message_content(body_json: dict):
    """从请求体 messages 数组取最后一条 role=="user" 元素的 content。

    只看最后一条 user 消息（§2.2）：全量扫描会导致历史里出现过的旧指令被
    反复命中，且用户切到别的 route 后历史里的旧指令会造成「切不动」。取不到
    返回 None（调用方按 fail-open 处理，不是这里报错）。
    """
    messages = body_json.get("messages")
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return msg.get("content")
    return None


# ---------------------------------------------------------------------------
# sidecar 存储（§4.5 / §5.4）
# ---------------------------------------------------------------------------

OVERRIDE_TTL_SECONDS = 7 * 24 * 3600  # 7 天（§5.4 阈值选定依据：48h 会误删活跃会话）


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> "float | None":
    """ISO8601（Z 结尾）字符串 → epoch 秒。解析失败返回 None（不参与清理判据）。"""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        s = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def _atomic_write_json(path: Path, obj: dict) -> None:
    """mkstemp + os.replace 原子写盘。与 core/server.py 内同名函数同一模式
    （mkstemp 同目录临时文件 + os.replace），并额外 chmod 0600（sidecar 含
    敏感 session id，见设计文档 §4.1 对 _config_ops.atomic_write 的既有口径）。
    """
    _dir = str(path.parent)
    try:
        fd, tmp = tempfile.mkstemp(dir=_dir, suffix=".tmp")
    except BaseException as exc:
        # An interrupted mkstemp may have allocated an unreturned descriptor.
        interrupted = not isinstance(exc, Exception)
        raise SidecarWriteError("UNKNOWN" if interrupted else "NOT_REPLACED",
                                release_unknown=interrupted, close_unknown=interrupted) from None
    replacing = False
    fd_owned = True
    close_unknown = False
    try:
        # On successful fdopen the file object owns the descriptor; never close
        # the raw fd again after transfer, even when its close result is unclear.
        f = os.fdopen(fd, "w", encoding="utf-8")
        fd_owned = False
        try:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        finally:
            try:
                f.close()
            except BaseException:
                close_unknown = True
                raise
        replacing = True
        os.replace(tmp, str(path))
        os.chmod(path, 0o600)
    except BaseException:
        if fd_owned:
            try:
                os.close(fd)
            except BaseException:
                close_unknown = True
        cleanup_unknown = False
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        except BaseException:
            cleanup_unknown = True
        raise SidecarWriteError(
            "UNKNOWN" if replacing or close_unknown or cleanup_unknown else "NOT_REPLACED",
            release_unknown=close_unknown, close_unknown=close_unknown,
            cleanup_unknown=cleanup_unknown) from None


@dataclass(frozen=True)
class SidecarSnapshot:
    """Adapter-verified stable bytes and opaque parent/target/protection evidence.

    ``contents=None`` means *proved absent*, not a failed read. ``identity`` must
    identify the protected parent and either the single-link target or its proved
    absence; equality must include all relevant protection evidence. The adapter
    owns verification and resource-release checks; this class cannot prove them.
    """
    contents: bytes | None = field(repr=False)
    identity: Any = field(repr=False)
    mtime: float = 0.0


class SidecarWriteError(RuntimeError):
    """Commit outcome; UNKNOWN may include the candidate's identity evidence."""

    def __init__(self, state: str, candidate: SidecarSnapshot | None = None, *,
                 previous_recovered: bool = False, legacy_unverifiable: bool = False,
                 release_unknown: bool = False, close_unknown: bool = False,
                 cleanup_unknown: bool = False):
        super().__init__(state)
        self.state = state
        self.candidate = candidate
        self.previous_recovered = previous_recovered
        self.legacy_unverifiable = legacy_unverifiable
        self.release_unknown = release_unknown
        self.close_unknown = close_unknown
        self.cleanup_unknown = cleanup_unknown


def _sidecar_error_flag(exc: BaseException, name: str) -> bool:
    """Preserve only boolean uncertainty, never exception text or arbitrary fields."""
    try:
        return getattr(exc, name, False) is True
    except BaseException:
        # An uninspectable resource state is not proof of a clean release.
        return True



@dataclass(frozen=True)
class _SidecarResourceUncertainty:
    """Sticky process-local evidence; target B/C never proves resource cleanup."""
    release_unknown: bool = False
    close_unknown: bool = False
    cleanup_unknown: bool = False

    def __bool__(self):
        return self.release_unknown or self.close_unknown or self.cleanup_unknown

    def merge(self, *, release_unknown=False, close_unknown=False, cleanup_unknown=False):
        return _SidecarResourceUncertainty(
            self.release_unknown or release_unknown,
            self.close_unknown or close_unknown,
            self.cleanup_unknown or cleanup_unknown)

def _sidecar_candidate(value, payload):
    """Accept only comparable evidence of exactly this candidate, without logging it."""
    try:
        if (isinstance(value, SidecarSnapshot) and value.identity is not None
                and value.contents == payload):
            return value
    except BaseException:
        pass
    return None


def normalize_override_entry(entry) -> "str | None":
    """把一条 override 条目（旧式纯字符串 或 新式 {route_id,last_seen,created}）
    规范化为 route_id 字符串。无法识别返回 None。

    兼容性要求（§5.4 正确性耦合点）：人工手改 sidecar 时可能写成纯字符串；
    apply_command 写入的是新式 dict。读取侧必须同时支持两种形态。
    """
    if isinstance(entry, str):
        return entry or None
    if isinstance(entry, dict):
        rid = entry.get("route_id")
        return rid if isinstance(rid, str) and rid else None
    return None


class SessionOverridesSidecar:
    """`config/session_overrides.json`：代理独占写的 override 运行时状态存储。

    - 文件缺失视为 `{}`（首次运行的正常状态，不报错）。
    - 文件非法 JSON：保留上一次成功加载的内存值 + warning，不中断请求。
    - 结构：{"<client_token>": {"<session_id>": {"route_id","last_seen","created"}}}。
      按 client_token 分组，与每条 strategy 各自对应。
    - last_seen 内存记账（§5.4 代价一节）：命中 override 的普通请求只更新
      `self._mem_last_seen`（不写盘）；只有 `$route` 写操作（`apply_command`）
      才会把内存中已记录的时间戳刷入对应 sidecar 条目，随后一次性原子写盘。
    """

    def __init__(self, path: Path, *, secure_io=None):
        """secure_io.capture(path) -> SidecarSnapshot;
        secure_io.commit(path, baseline, bytes) -> verified SidecarSnapshot.

        commit must revalidate the baseline immediately before atomic submission,
        verify the result and report SidecarWriteError(NOT_REPLACED|UNKNOWN).
        UNKNOWN may carry candidate identity evidence. Unclassified exceptions
        after entering commit are UNKNOWN. Unknown resource release is UNKNOWN
        even if the target was previously verified, and the adapter must not
        turn that observation alone into candidate identity evidence. The adapter
        also owns runtime staging, cross-volume policy, role collisions and
        platform-specific protection. Resource uncertainty is latched for this
        store's lifetime: B/C target evidence cannot prove cleanup or release.
        No resource-resolution API or persistent restart marker exists here.

        The no-argument compatibility path is the historical writer, NOT the
        new platform security protocol. It cannot verify B/C after UNKNOWN and
        must remain blocked. Production must inject the real adapter before use.
        """
        self._path = Path(path)
        self._secure_io = secure_io
        self._lock = threading.Lock()
        self._mtime: float = 0.0
        self._data: dict[str, dict[str, Any]] = {}
        self._mem_last_seen: dict[tuple[str, str], float] = {}
        self._baseline: SidecarSnapshot | None = None
        self._unknown = None
        self._resource_unknown = _SidecarResourceUncertainty()
        self._unavailable = False
        if secure_io is None:
            log.warning("sidecar: legacy compatibility writer; secure adapter required for production")
            self._reload_locked()
        else:
            self._refresh_secure_locked()

    @staticmethod
    def _decode_snapshot(snapshot: SidecarSnapshot) -> dict:
        if not isinstance(snapshot, SidecarSnapshot) or snapshot.identity is None:
            raise ValueError("sidecar evidence unavailable")
        if snapshot.contents is None:
            return {}
        if not isinstance(snapshot.contents, bytes):
            raise ValueError("sidecar contents unavailable")
        loaded = json.loads(snapshot.contents.decode("utf-8"))
        if not isinstance(loaded, dict) or any(not isinstance(v, dict) for v in loaded.values()):
            raise ValueError("invalid sidecar structure")
        return loaded

    @staticmethod
    def _same_snapshot(a, b) -> bool:
        return (isinstance(a, SidecarSnapshot) and isinstance(b, SidecarSnapshot)
                and a.identity is not None and b.identity is not None
                and a.identity == b.identity and a.contents == b.contents)

    def _capture_secure_locked(self):
        snapshot = self._secure_io.capture(self._path)
        return snapshot, self._decode_snapshot(snapshot)

    def _remember_resource_unknown(self, exc: BaseException) -> _SidecarResourceUncertainty:
        # Some underlying platform errors expose only a fixed code. Inspect no
        # message or arbitrary payload; uninspectable evidence is UNKNOWN.
        try:
            code = getattr(exc, "code", None)
        except BaseException:
            code = "release_unknown"
        if type(code) is not str:
            code = None
        self._resource_unknown = self._resource_unknown.merge(
            release_unknown=(_sidecar_error_flag(exc, "release_unknown") or code in (
                "release_unknown", "close_failed", "handle_close_failed", "resource_release_failed")),
            close_unknown=(_sidecar_error_flag(exc, "close_unknown") or code in (
                "fd_close_unknown", "parent_close_unknown", "parent_close_unknown_after_verified",
                "parent_close_and_cleanup_unknown", "staging_close_unknown", "staging_close_and_cleanup_unknown")),
            cleanup_unknown=(_sidecar_error_flag(exc, "cleanup_unknown") or code in (
                "staging_cleanup_failed", "staging_cleanup_unknown",
                "parent_close_and_cleanup_unknown", "staging_close_and_cleanup_unknown")))
        return self._resource_unknown

    def _resource_error(self) -> SidecarWriteError:
        flags = self._resource_unknown
        return SidecarWriteError("UNKNOWN", legacy_unverifiable=self._secure_io is None,
                                 release_unknown=flags.release_unknown,
                                 close_unknown=flags.close_unknown,
                                 cleanup_unknown=flags.cleanup_unknown)

    def _refresh_secure_locked(self) -> bool:
        # A successful target snapshot cannot prove a previously uncertain FD,
        # handle or temporary artifact has been released/cleaned.
        if self._resource_unknown:
            self._unavailable = True
            return False
        try:
            snapshot, loaded = self._capture_secure_locked()
        except BaseException as exc:
            flags = self._remember_resource_unknown(exc)
            self._unavailable = True
            if not flags and not isinstance(exc, Exception):
                raise
            if flags:
                log.warning("sidecar: resource state unverified; writes remain blocked")
            else:
                log.warning("sidecar: read or verification unavailable; retaining verified overrides")
            return False
        self._data = loaded
        self._baseline = snapshot
        self._mtime = snapshot.mtime
        self._unavailable = False
        return True

    def _recover_locked(self) -> bool:
        if self._resource_unknown:
            log.warning("sidecar: resource state unverified; B/C cannot resolve it")
            return False
        if self._unknown is None:
            if self._secure_io is None:
                return not self._unavailable
            return self._refresh_secure_locked()
        if self._secure_io is None:
            log.warning("sidecar: legacy UNKNOWN cannot be verified; writes blocked; "
                        "production requires secure adapter")
            return False
        pending = self._unknown
        baseline, candidate, candidate_data, candidate_mem, removed = pending
        old_data, old_mem, old_baseline, old_mtime = (
            self._data, self._mem_last_seen, self._baseline, self._mtime)
        old_unavailable = self._unavailable
        publishing = False
        try:
            observed, loaded = self._capture_secure_locked()
            if self._same_snapshot(observed, baseline):
                recovered_data = loaded
                recovered_mem = old_mem
            elif self._same_snapshot(observed, candidate):
                recovered_data = candidate_data
                # Build the complete memory publication independently: touches
                # made during isolation survive except for keys removed by C.
                recovered_mem = old_mem.copy()
                for key in removed:
                    recovered_mem.pop(key, None)
                for key, value in candidate_mem.items():
                    recovered_mem[key] = max(value, recovered_mem.get(key, value))
            else:
                log.warning("sidecar: UNKNOWN; target does not match verified B or C")
                return False
            publishing = True
            self._data = recovered_data
            self._mem_last_seen = recovered_mem
            self._baseline = observed
            self._mtime = observed.mtime
            self._unavailable = False
            self._unknown = None
            log.warning("sidecar: UNKNOWN resolved against verified snapshot")
            return True
        except BaseException as exc:
            # Re-arm before restoring every field if publication was interrupted.
            self._unknown = pending
            self._data, self._mem_last_seen = old_data, old_mem
            self._baseline, self._mtime = old_baseline, old_mtime
            self._unavailable = old_unavailable
            flags = self._remember_resource_unknown(exc)
            close_unknown = flags.close_unknown
            cleanup_unknown = flags.cleanup_unknown
            release_unknown = flags.release_unknown
            if (not publishing and isinstance(exc, Exception)
                    and not (close_unknown or cleanup_unknown or release_unknown)):
                log.warning("sidecar: UNKNOWN; verification unavailable")
                return False
            raise SidecarWriteError("UNKNOWN", close_unknown=close_unknown,
                                    cleanup_unknown=cleanup_unknown,
                                    release_unknown=release_unknown) from None

    def recover(self) -> bool:
        """Explicitly verify and recover; never infer a previous process outcome."""
        with self._lock:
            return self._recover_locked()

    # ------------------------------------------------------------------
    # 读取 + 热重载
    # ------------------------------------------------------------------

    def _reload_locked(self) -> None:
        """持锁加载文件；缺失视为 {}；非法 JSON 保留旧值 + warning。调用方需持锁。"""
        try:
            f = open(self._path, "r", encoding="utf-8")
        except FileNotFoundError:
            self._data = {}
            self._mtime = 0.0
            self._unavailable = False
            return
        except OSError:
            self._unavailable = True
            log.warning("sidecar: read unavailable; retaining last known overrides")
            return
        try:
            with f:
                loaded = json.load(f)
            if not isinstance(loaded, dict) or any(not isinstance(v, dict) for v in loaded.values()):
                raise ValueError("sidecar root is not a dict")
            mtime = self._path.stat().st_mtime
            self._data = loaded
            self._mtime = mtime
            self._unavailable = False
        except (ValueError, OSError, UnicodeError):
            self._unavailable = True
            log.warning("sidecar: read unavailable; retaining last known overrides")

    def _maybe_reload_locked(self) -> None:
        """`maybe_reload` 的持锁版本：假设调用方已持有 `self._lock`，只做一次
        mtime 判断 + 必要时 reload，不再自行加锁（`threading.Lock` 不可重入，
        `apply_command` 等已持锁的写路径必须调用这个版本，不能调用 `maybe_reload`，
        否则会对同一把锁二次获取导致死锁）。
        """
        if self._unknown is not None or self._resource_unknown:
            return
        if self._unavailable:
            if self._secure_io is not None:
                self._refresh_secure_locked()
            else:
                self._reload_locked()
            return
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            if self._secure_io is not None:
                self._refresh_secure_locked()
                return
            # 文件被删除：视为空（与 __init__/_reload_locked 的文件缺失语义一致），
            # 必须清空内存，否则已删除的 sidecar 数据会残留在内存里（曾导致 status 误报 +6）
            if self._data or self._mtime:
                self._data = {}
                self._mtime = 0.0
            return
        except OSError:
            self._unavailable = True
            log.warning("sidecar: stat unavailable; retaining last known overrides")
            return
        if mtime <= self._mtime:
            return
        if self._secure_io is None:
            self._reload_locked()
        else:
            self._refresh_secure_locked()

    def maybe_reload(self) -> None:
        """mtime 比对 + 双重检查，与 ConfigStore.maybe_reload 同一模式。

        对外入口：先做一次无锁的快速 mtime 判断（绝大多数调用文件未变，省掉抢锁），
        确认可能需要 reload 才获取锁，在锁内交给 `_maybe_reload_locked` 二次确认后
        执行。已持锁的调用方（如 `apply_command`）不得调用本方法，应直接调用
        `_maybe_reload_locked`。

        文件不存在时也必须进锁调 `_maybe_reload_locked`（由其负责清空内存），
        不能在无锁快速路径直接 return——否则已删除的 sidecar 数据会残留在内存里
        （曾导致 status 误报 +6）。
        """
        if self._unknown is not None or self._resource_unknown or self._unavailable:
            with self._lock:
                self._maybe_reload_locked()
            return
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            with self._lock:
                self._maybe_reload_locked()
            return
        except OSError:
            with self._lock:
                self._maybe_reload_locked()
            return
        if mtime <= self._mtime:
            return
        with self._lock:
            self._maybe_reload_locked()

    def get_overrides_for(self, client_token: str) -> dict[str, str]:
        """返回该 client_token 下 sidecar 的 {session_id: route_id} 映射
        （已规范化为纯字符串，供 extract_route_candidates 读取）。
        """
        with self._lock:
            token_map = self._data.get(client_token) or {}
            result: dict[str, str] = {}
            for sid, entry in token_map.items():
                rid = normalize_override_entry(entry)
                if rid:
                    result[sid] = rid
            return result

    def count_overrides_for(self, client_token: str) -> int:
        with self._lock:
            return len(self._data.get(client_token) or {})

    # ------------------------------------------------------------------
    # 热路径：内存记账，不写盘（§5.4 代价一节 / V13）
    # ------------------------------------------------------------------

    def touch(self, client_token: str, session_id: str) -> None:
        """命中 override 的普通请求调用：只更新内存中的 last_seen，绝不写盘。"""
        with self._lock:
            self._mem_last_seen[(client_token, session_id)] = time.time()

    # ------------------------------------------------------------------
    # 写路径：$route 切换/清除（唯一的写盘入口，一次原子写，见 V12）
    # ------------------------------------------------------------------

    def apply_command(self, client_token: str, session_id: str,
                       action: str, target_route_id: "str | None" = None) -> dict:
        """应用一次 $route 写操作（action: "set" | "reset"），随之做 7 天清理，
        全部改动落在同一次 `_atomic_write_json` 里（V12：一次 os.replace，无中间态）。

        返回 {"cleaned": [(client_token, session_id), ...]}，供回执列出被清理的条目。

        正确性要点（§4.2 别名污染 / V5）：全程在 deepcopy 出的字典上操作，
        只在最后整体替换 self._data，绝不就地改动可能被其他线程持有的旧引用。
        """
        if action not in ("set", "reset"):
            raise ValueError(f"unknown action: {action!r}")

        with self._lock:
            if self._resource_unknown:
                raise self._resource_error() from None
            previous_recovered = False
            if self._unknown is not None:
                previous_baseline, previous_candidate = self._unknown[:2]
                if not self._recover_locked():
                    raise self._resource_error() from None
                previous_recovered = (
                    not self._same_snapshot(previous_baseline, previous_candidate)
                    and self._same_snapshot(self._baseline, previous_candidate))
            elif self._secure_io is not None:
                # A fresh protected baseline is mandatory for every command.
                if not self._refresh_secure_locked():
                    if self._resource_unknown:
                        raise self._resource_error() from None
                    raise SidecarWriteError("NOT_REPLACED")
            else:
                self._maybe_reload_locked()
                if self._unavailable:
                    raise SidecarWriteError("NOT_REPLACED")
            data = copy.deepcopy(self._data)
            candidate_mem = self._mem_last_seen.copy()
            removed = set()

            # 1. 把内存记账里已存在于 sidecar 的条目的 last_seen 刷新（用较新值），
            #    避免「写入时刻」之后持续活跃却因为没有再次 $route 而被误判过期。
            for (ct, sid), ts in candidate_mem.items():
                token_map = data.get(ct)
                if not token_map or sid not in token_map:
                    continue
                entry = token_map[sid]
                if not isinstance(entry, dict):
                    continue
                existing_epoch = _parse_iso(entry.get("last_seen")) or 0.0
                if ts > existing_epoch:
                    entry["last_seen"] = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ")

            # 2. 应用本次命令
            now_iso = _utc_now_iso()
            token_map = data.setdefault(client_token, {})
            if action == "set":
                existing = token_map.get(session_id)
                created = existing.get("created") if isinstance(existing, dict) else None
                token_map[session_id] = {
                    "route_id": target_route_id,
                    "last_seen": now_iso,
                    "created": created or now_iso,
                }
                candidate_mem[(client_token, session_id)] = time.time()
            else:  # reset
                token_map.pop(session_id, None)
                candidate_mem.pop((client_token, session_id), None)
                removed.add((client_token, session_id))

            # 3. 7 天清理（只清 sidecar；无 last_seen 的条目不参与；当前 session 永不清理）
            cleaned: list[tuple[str, str]] = []
            cutoff = time.time() - OVERRIDE_TTL_SECONDS
            for ct in list(data.keys()):
                sessions = data[ct]
                if not isinstance(sessions, dict):
                    continue
                for sid in list(sessions.keys()):
                    if ct == client_token and sid == session_id:
                        continue  # 当前 session 永不被清理
                    entry = sessions[sid]
                    if not isinstance(entry, dict):
                        continue  # 无 last_seen（含手工塞入的旧式字符串）不参与清理
                    ls = entry.get("last_seen")
                    ts = _parse_iso(ls) if ls else None
                    if ts is None:
                        continue
                    if ts < cutoff:
                        sessions.pop(sid)
                        cleaned.append((ct, sid))
                        candidate_mem.pop((ct, sid), None)
                        removed.add((ct, sid))
                if not sessions:
                    data.pop(ct, None)

            # 4. 保留 B，先建立隔离态，再进入可能提交的区间。这样即使
            # 中断发生在 adapter 返回、验证或分步发布期间，也不会触发热加载。
            baseline = self._baseline
            old_data, old_mem, old_mtime = self._data, self._mem_last_seen, self._mtime
            old_unavailable = self._unavailable
            payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
            committed = None
            commit_returned = False
            self._unknown = (baseline, None, data, candidate_mem, removed)
            try:
                if self._secure_io is None:
                    _atomic_write_json(self._path, data)
                    commit_returned = True
                    legacy_mtime = self._path.stat().st_mtime
                else:
                    committed = self._secure_io.commit(self._path, baseline, payload)
                    commit_returned = True
                    if (self._decode_snapshot(committed) != data
                            or committed.contents != payload):
                        raise SidecarWriteError("UNKNOWN", committed)
                self._data = data
                self._mem_last_seen = candidate_mem
                if self._secure_io is not None:
                    self._baseline = committed
                    self._mtime = committed.mtime
                else:
                    self._mtime = legacy_mtime
                self._unknown = None
                log.info("sidecar.write action=%s", action)
                return {"cleaned": cleaned}
            except BaseException as exc:
                # Only a classified, unambiguously pre-return result can prove
                # NOT_REPLACED. An interrupt/unclassified error, including one
                # after any partial memory publication, must quarantine.
                classified = isinstance(exc, SidecarWriteError)
                flags = self._remember_resource_unknown(exc)
                close_unknown = flags.close_unknown
                cleanup_unknown = flags.cleanup_unknown
                release_unknown = flags.release_unknown
                try:
                    not_replaced = (classified and exc.state == "NOT_REPLACED"
                                    and not commit_returned
                                    and not (close_unknown or cleanup_unknown or release_unknown))
                except BaseException:
                    not_replaced = False
                    release_unknown = True
                    self._resource_unknown = self._resource_unknown.merge(release_unknown=True)
                candidate = _sidecar_candidate(committed, payload)
                if candidate is None and classified:
                    try:
                        candidate = _sidecar_candidate(exc.candidate, payload)
                    except BaseException:
                        candidate = None
                # Re-arm first, then undo any partially published C. No other
                # thread can observe the intermediate state while this lock is held.
                self._unknown = (baseline, candidate, data, candidate_mem, removed)
                self._data, self._mem_last_seen = old_data, old_mem
                self._baseline, self._mtime = baseline, old_mtime
                self._unavailable = old_unavailable
                if not_replaced:
                    self._unknown = None
                    log.warning("sidecar: commit not performed")
                    raise SidecarWriteError(
                        "NOT_REPLACED", previous_recovered=previous_recovered) from None
                if self._secure_io is None:
                    log.warning("sidecar: legacy commit UNKNOWN; verification unavailable; "
                                "writes blocked; production requires secure adapter")
                else:
                    log.warning("sidecar: commit UNKNOWN; retaining last known overrides")
                raise SidecarWriteError(
                    "UNKNOWN", legacy_unverifiable=self._secure_io is None,
                    release_unknown=release_unknown, close_unknown=close_unknown,
                    cleanup_unknown=cleanup_unknown) from None



# ---------------------------------------------------------------------------
# 命令层骨架（§7.3）：命令名 → handler 注册表。首版只注册 $route 一个命令。
# 边界约束（必须遵守，§7.3）：handler 只允许操作代理自身路由/观测状态、只允许
# 纯本地操作；禁止执行外部命令、读写 sidecar/主 config 以外的文件、转发请求、
# 任何网络动作。新增命令前必须重新确认这条边界。
# ---------------------------------------------------------------------------

# handler 统一签名：CommandContext -> CommandResult
class CommandContext:
    """命令 handler 拿到的上下文（只读句柄，handler 不直接碰 ConfigStore 内部对象）。"""

    __slots__ = ("arg", "client_token", "session_key", "strategy", "routes_map", "sidecar",
                 "resolved_route_id")

    def __init__(self, arg, client_token, session_key, strategy, routes_map, sidecar,
                 resolved_route_id=None):
        self.arg = arg                      # None=查询 / "reset" / 其他=目标 route id
        self.client_token = client_token
        self.session_key = session_key
        self.strategy = strategy            # 命中的 strategy dict（只读，不得就地改）
        self.routes_map = routes_map        # {route_id: route dict}
        self.sidecar: SessionOverridesSidecar = sidecar
        # 调用方（server.py）用合并后 override 跑一次 extract_route_candidates 算出的
        # candidates[0].id：即"此刻若照常转发会打到哪个 route"，供查询命令展示，
        # 避免本模块重复实现一致性哈希算法（circular import 也不允许 import server）。
        self.resolved_route_id = resolved_route_id


class CommandResult:
    """handler 返回值：回执文本 + 是否发生了写操作（供上层记 ACCESS/日志用）。"""

    __slots__ = ("receipt_text", "wrote")

    def __init__(self, receipt_text: str, wrote: bool):
        self.receipt_text = receipt_text
        self.wrote = wrote


def _short_session(session_key: str) -> str:
    return session_key[:8] if session_key else ""


def _format_cleaned(cleaned: list[tuple[str, str]]) -> str:
    if not cleaned:
        return "本次未清理任何僵尸条目"
    items = "、".join(f"{_short_session(sid)}" for _ct, sid in cleaned)
    return f"顺带清理了 {len(cleaned)} 条静默超 7 天的僵尸条目（不静默删除，如需恢复请重新 $route）：{items}"


def handle_route_command(ctx: CommandContext) -> CommandResult:
    """`$route` 命令 handler（§5.2 命令集）。

    - arg is None：查询，纯读零副作用，不触发清理。
    - arg == "reset"：清除 override，落回自动哈希分配，随写操作触发清理。
    - 其他：切换到 <id>，随写操作触发清理。
    """
    client_token = ctx.client_token
    routes_map = ctx.routes_map
    strategy = ctx.strategy

    if ctx.arg is None:
        return _handle_query(ctx)

    # 防御性校验（设计文档未直接讨论的组合，实现时按文档 §4.4"宁可当场失败也不能
    # 制造假成功"的既定原则处理）：extract_route_candidates 对旧式单值 route_id
    # strategy（无 route_pool）完全不读取 dispatch.session_overrides（server.py 内
    # 该函数对旧写法直接返回单一固定 route，无 override 概念）。若在这种 strategy
    # 上执行 $route 写操作，写入会"成功"但对实际路由永远无效，构成假成功回执。
    # 现网唯一实际使用 $route 的 strategy（cc）已是 route_pool 写法，此分支只是
    # 兜底防护，不改变任何已拍板决策。
    if not strategy.get("route_pool"):
        return CommandResult(
            "当前 client_token 使用旧式单值 route_id 配置（无 route_pool），"
            "不支持 $route 切换/reset：该配置只有一个固定 route，写入 override 不会"
            "产生任何效果。请先把该 strategy 迁移到 route_pool 写法。",
            wrote=False,
        )

    if ctx.arg == "reset":
        try:
            result = ctx.sidecar.apply_command(client_token, ctx.session_key, "reset")
        except SidecarWriteError as exc:
            return _sidecar_failure_receipt(exc)
        cleaned = result["cleaned"]
        text = (
            f"已清除 session {_short_session(ctx.session_key)} 的 route override，"
            f"下一条消息起落回自动哈希分配。\n{_format_cleaned(cleaned)}"
        )
        return CommandResult(text, wrote=True)

    target_id = ctx.arg
    if target_id not in routes_map:
        available = ", ".join(sorted(routes_map.keys())) or "(无可用 route)"
        text = f"route \"{target_id}\" 不存在。可用 route id: {available}"
        return CommandResult(text, wrote=False)

    try:
        result = ctx.sidecar.apply_command(client_token, ctx.session_key, "set", target_route_id=target_id)
    except SidecarWriteError as exc:
        return _sidecar_failure_receipt(exc)
    cleaned = result["cleaned"]
    text = (
        f"已把 session {_short_session(ctx.session_key)} 切到 route \"{target_id}\"，"
        f"下一条消息起生效。撤销请发 $route reset。\n{_format_cleaned(cleaned)}"
    )
    return CommandResult(text, wrote=True)


def _sidecar_failure_receipt(exc: SidecarWriteError) -> CommandResult:
    if exc.state == "UNKNOWN":
        if exc.release_unknown or exc.close_unknown or exc.cleanup_unknown:
            return CommandResult("route 写入或资源状态未知；暂用最后已知的 override，"
                                 "资源释放和清理未确认，单独核验 B/C 不能解除写入暂停。",
                                 wrote=False)
        if exc.legacy_unverifiable:
            return CommandResult("route 写入结果未知，当前实例无法安全核验；已暂停 route 写入，"
                                 "生产环境必须接入真实安全适配器。", wrote=False)
        return CommandResult("route 写入结果未知，暂用最后已知的 override；"
                             "重新验证前不会覆盖 sidecar。", wrote=False)
    if exc.previous_recovered:
        return CommandResult("先前未知结果已验证恢复；本次 route 写入未提交，"
                             "当前 override 保持恢复后的状态。", wrote=False)
    return CommandResult("route 写入未提交或无法安全验证；当前 override 未变。", wrote=False)


def _handle_query(ctx: CommandContext) -> CommandResult:
    client_token = ctx.client_token
    session_key = ctx.session_key
    strategy = ctx.strategy
    routes_map = ctx.routes_map

    sidecar_map = ctx.sidecar.get_overrides_for(client_token)
    # sidecar 是唯一来源，总条数即 sidecar 条数。
    total_overrides = len(sidecar_map)
    available = ", ".join(sorted(routes_map.keys())) or "(无可用 route)"

    if not strategy.get("route_pool"):
        # 与写路径（handle_route_command 里对 route_id/无 route_pool 的防御）对称：
        # extract_route_candidates 对旧式单值 route_id strategy 完全不读取
        # session_overrides，此时即便 sidecar 命中该 session_id，也
        # 不代表实际生效——不能展示一个看似生效实则不生效的 route（假成功回执）。
        fixed_route = strategy.get("route_id")
        lines = [
            f"当前 session {_short_session(session_key)}："
            f"该 strategy 未使用 route_pool 机制，session override 不生效，"
            f"实际路由固定为 \"{fixed_route}\"（来自 strategy.route_id）。",
            f"可用 route id: {available}",
            f"该 strategy override 总条数: {total_overrides}"
            f"（sidecar {len(sidecar_map)}，以上条目对当前 strategy 均不生效）",
        ]
        return CommandResult("\n".join(lines), wrote=False)

    override_rid = sidecar_map.get(session_key)
    if override_rid and override_rid in routes_map:
        source = "sidecar"
        current_route = override_rid
    else:
        # 未命中 override：自动哈希分配。具体候选顺序由 server.py 调用
        # extract_route_candidates 算好后通过 ctx.resolved_route_id 传入
        # （本模块不 import server，避免循环依赖，也不重复实现一致性哈希算法）。
        source = "自动哈希分配"
        current_route = ctx.resolved_route_id

    route_line = (
        f"当前 session {_short_session(session_key)} 生效 route: {current_route}（来源: {source}）"
        if current_route else
        f"当前 session {_short_session(session_key)} 未能确定生效 route（{source}）"
    )
    lines = [
        route_line,
        f"可用 route id: {available}",
        f"该 strategy override 总条数: {total_overrides}"
        f"（sidecar {len(sidecar_map)}）",
    ]
    return CommandResult("\n".join(lines), wrote=False)


# 命令名 → handler 注册表。首版只有 $route；扩展新命令只需在此注册一个 handler，
# 不需要改动分发/响应合成/ACCESS 记录逻辑（这正是做命令层的收益，见设计文档 §7.3）。
COMMAND_HANDLERS: dict[str, Callable[[CommandContext], CommandResult]] = {
    CMD_PREFIX: handle_route_command,
}
