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
       与主 config 合并（sidecar 优先）、写入、7 天清理、last_seen 内存记账。
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
    fd, tmp = tempfile.mkstemp(dir=_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def normalize_override_entry(entry) -> "str | None":
    """把一条 override 条目（旧式纯字符串 或 新式 {route_id,last_seen,created}）
    规范化为 route_id 字符串。无法识别返回 None。

    兼容性要求（§5.4 正确性耦合点）：现网 5 条是旧式纯字符串 value；sidecar
    写入的是新式 dict。读取侧必须同时支持两种形态。
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
      按 client_token 分组是为了能与主 config 内每条 strategy 各自的
      dispatch.session_overrides 对应合并（§4.5 合并语义）。
    - last_seen 内存记账（§5.4 代价一节）：命中 override 的普通请求只更新
      `self._mem_last_seen`（不写盘）；只有 `$route` 写操作（`apply_command`）
      才会把内存中已记录的时间戳刷入对应 sidecar 条目，随后一次性原子写盘。
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._mtime: float = 0.0
        self._data: dict[str, dict[str, Any]] = {}
        self._mem_last_seen: dict[tuple[str, str], float] = {}
        self._reload_locked()

    # ------------------------------------------------------------------
    # 读取 + 热重载
    # ------------------------------------------------------------------

    def _reload_locked(self) -> None:
        """持锁加载文件；缺失视为 {}；非法 JSON 保留旧值 + warning。调用方需持锁。"""
        if not self._path.exists():
            self._data = {}
            self._mtime = 0.0
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("sidecar root is not a dict")
            self._data = loaded
            self._mtime = self._path.stat().st_mtime
        except (json.JSONDecodeError, ValueError, OSError) as e:
            log.warning("session_overrides sidecar corrupt, keeping last known value: %s", e)

    def _maybe_reload_locked(self) -> None:
        """`maybe_reload` 的持锁版本：假设调用方已持有 `self._lock`，只做一次
        mtime 判断 + 必要时 reload，不再自行加锁（`threading.Lock` 不可重入，
        `apply_command` 等已持锁的写路径必须调用这个版本，不能调用 `maybe_reload`，
        否则会对同一把锁二次获取导致死锁）。
        """
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime <= self._mtime:
            return
        self._reload_locked()

    def maybe_reload(self) -> None:
        """mtime 比对 + 双重检查，与 ConfigStore.maybe_reload 同一模式。

        对外入口：先做一次无锁的快速 mtime 判断（绝大多数调用文件未变，省掉抢锁），
        确认可能需要 reload 才获取锁，在锁内交给 `_maybe_reload_locked` 二次确认后
        执行。已持锁的调用方（如 `apply_command`）不得调用本方法，应直接调用
        `_maybe_reload_locked`。
        """
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime <= self._mtime:
            return
        with self._lock:
            self._maybe_reload_locked()

    def get_overrides_for(self, client_token: str) -> dict[str, str]:
        """返回该 client_token 下 sidecar 的 {session_id: route_id} 映射
        （已规范化为纯字符串，供与主 config 合并、sidecar 优先覆盖）。
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
            self._maybe_reload_locked()
            data = copy.deepcopy(self._data)

            # 1. 把内存记账里已存在于 sidecar 的条目的 last_seen 刷新（用较新值），
            #    避免「写入时刻」之后持续活跃却因为没有再次 $route 而被误判过期。
            for (ct, sid), ts in self._mem_last_seen.items():
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
                self._mem_last_seen[(client_token, session_id)] = time.time()
            else:  # reset
                token_map.pop(session_id, None)
                self._mem_last_seen.pop((client_token, session_id), None)

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
                        self._mem_last_seen.pop((ct, sid), None)
                if not sessions:
                    data.pop(ct, None)

            # 4. 一次性原子写
            _atomic_write_json(self._path, data)
            self._data = data
            try:
                self._mtime = self._path.stat().st_mtime
            except OSError:
                pass

            return {"cleaned": cleaned}


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


def effective_overrides(strategy: dict, sidecar: SessionOverridesSidecar) -> dict[str, str]:
    """合并主 config 内该 strategy 的 session_overrides（基线）与 sidecar（优先）。
    同 key 冲突时 sidecar 覆盖（§4.5 合并语义）。

    server.py 侧在把 strategy 传给 `extract_route_candidates` 前，用本函数算出的
    结果构造一份 strategy 的浅拷贝（只替换 dispatch.session_overrides），确保普通
    请求命中 override 时能读到 sidecar 里刚写入的最新值，同时不污染 ConfigStore
    内部对象（不 deepcopy 整个 strategy，只浅拷贝顶层两层，见 §4.2）。
    """
    client_token = strategy.get("client_token", "")
    dispatch = strategy.get("dispatch") or {}
    base = dispatch.get("session_overrides") or {}
    merged: dict[str, str] = {}
    for sid, entry in base.items():
        rid = normalize_override_entry(entry)
        if rid:
            merged[sid] = rid
    merged.update(sidecar.get_overrides_for(client_token))  # sidecar 优先
    return merged


def build_merged_strategy(strategy: dict, sidecar: SessionOverridesSidecar) -> dict:
    """构造一份仅用于本次调用的 strategy 视图：dispatch.session_overrides 替换为
    `effective_overrides` 的合并结果，其余字段原样引用。

    只浅拷贝 strategy 与 dispatch 两层（不 deepcopy 整个 strategy），返回的新 dict
    与 ConfigStore 内部对象无任何共享的可变引用会被写入——调用方后续也不得对
    返回值做任何就地修改（§4.2 别名污染要求：写路径必须 deepcopy 后改，这里的
    "浅拷贝" 只用于读路径的一次性合并视图，从不写回）。
    """
    merged = dict(strategy)
    dispatch = dict(strategy.get("dispatch") or {})
    dispatch["session_overrides"] = effective_overrides(strategy, sidecar)
    merged["dispatch"] = dispatch
    return merged


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
        result = ctx.sidecar.apply_command(client_token, ctx.session_key, "reset")
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

    result = ctx.sidecar.apply_command(client_token, ctx.session_key, "set", target_route_id=target_id)
    cleaned = result["cleaned"]
    text = (
        f"已把 session {_short_session(ctx.session_key)} 切到 route \"{target_id}\"，"
        f"下一条消息起生效。撤销请发 $route reset。\n{_format_cleaned(cleaned)}"
    )
    return CommandResult(text, wrote=True)


def _handle_query(ctx: CommandContext) -> CommandResult:
    client_token = ctx.client_token
    session_key = ctx.session_key
    strategy = ctx.strategy
    routes_map = ctx.routes_map

    sidecar_map = ctx.sidecar.get_overrides_for(client_token)
    base_dispatch = strategy.get("dispatch") or {}
    base_overrides = base_dispatch.get("session_overrides") or {}
    # 去重后计数：同一 session_id 若同时存在于主 config 与 sidecar（被 $route
    # 覆盖过的旧手工条目），只算一条，避免虚高（reviewer 指出的疑点，已确认为真）。
    total_overrides = len(set(base_overrides.keys()) | set(sidecar_map.keys()))
    available = ", ".join(sorted(routes_map.keys())) or "(无可用 route)"

    if not strategy.get("route_pool"):
        # 与写路径（handle_route_command 里对 route_id/无 route_pool 的防御）对称：
        # extract_route_candidates 对旧式单值 route_id strategy 完全不读取
        # session_overrides，此时即便 effective_overrides 命中该 session_id，也
        # 不代表实际生效——不能展示一个看似生效实则不生效的 route（假成功回执）。
        fixed_route = strategy.get("route_id")
        lines = [
            f"当前 session {_short_session(session_key)}："
            f"该 strategy 未使用 route_pool 机制，session override 不生效，"
            f"实际路由固定为 \"{fixed_route}\"（来自 strategy.route_id）。",
            f"可用 route id: {available}",
            f"该 strategy override 总条数: {total_overrides}"
            f"（主 config {len(base_overrides)} / sidecar {len(sidecar_map)}，"
            f"以上条目对当前 strategy 均不生效）",
        ]
        return CommandResult("\n".join(lines), wrote=False)

    merged = effective_overrides(strategy, ctx.sidecar)
    override_rid = merged.get(session_key)
    if override_rid and override_rid in routes_map:
        source = "sidecar（本次会话最近一次 $route 指令）" if session_key in sidecar_map else "主 config 手工 override"
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
        f"（主 config {len(base_overrides)} / sidecar {len(sidecar_map)}，"
        f"同一 session 同时存在两处时已去重只计一条）",
    ]
    return CommandResult("\n".join(lines), wrote=False)


# 命令名 → handler 注册表。首版只有 $route；扩展新命令只需在此注册一个 handler，
# 不需要改动分发/响应合成/ACCESS 记录逻辑（这正是做命令层的收益，见设计文档 §7.3）。
COMMAND_HANDLERS: dict[str, Callable[[CommandContext], CommandResult]] = {
    CMD_PREFIX: handle_route_command,
}
