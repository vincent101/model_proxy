#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""keeper_common.py — CIBA/mcli 两条链路与 keeper 编排层共享的底座。

state.json 只由持有 keeper.lock 的 keeper 进程写入；state_mcli.json 由
keeper tick 和人工铸票 hook 写入，使用 state_mcli.lock 串行读改写。
迁移旧状态时先持有 keeper.lock，再持有 state_mcli.lock，先写 mcli 文件，
再移除旧文件中的 mcli 子对象。日志只存指纹，状态文件均为 0600。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from datetime import datetime

import file_lock
import mtsso_core as core

STATE_DIR = os.path.expanduser(core.STATE_DIR)
STATE_FILE = os.path.expanduser(core.STATE_FILE)
LOG_FILE = os.path.expanduser(core.LOG_FILE)
LOCK_FILE = os.path.expanduser(core.LOCK_FILE)
MCLI_STATE_FILE = os.path.join(STATE_DIR, "state_mcli.json")
MCLI_LOCK = os.path.join(STATE_DIR, "state_mcli.lock")
MCLI_LOCK_TIMEOUT_S = 2.0
MCLI_LOCK_POLL_S = 0.02

REFRESH_THRESHOLD_MS = 48 * 3600 * 1000   # 剩余 < 48h 才续（唯一续期判据）
ALERT_THRESHOLD_MS = 12 * 3600 * 1000     # 失败且剩余 < 12h → ALERT
HISTORY_LIMIT = 30                         # history 滚动截断
LOG_ROTATE_BYTES = 512 * 1024              # 日志超 512KB 截半
MCLI_FAIL_ALERT_LIMIT = 8                  # mcli 段连续失败 8 次 → ALERT（同 CIBA 节奏）


# ── 日志（单行结构化 + 自转轮）───────────────────────────────────────
def _rotate_log_if_needed(path: str) -> None:
    """超 512KB 截半（照 km_download_v2.py 的 _rotate_log 行截断做法）。"""
    try:
        if not os.path.exists(path) or os.path.getsize(path) <= LOG_ROTATE_BYTES:
            return
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[len(lines) // 2:])
    except OSError:
        pass


_LOG_MODE_CHECKED = False


def _ensure_log_private() -> None:
    """日志文件必须是 0600（历史遗留是 0644：同机其他用户可读）。

    只在进程内首次写日志时检查一次：存在但权限过宽就收紧，不存在则交给
    下面的 O_CREAT 以 0600 创建。typo/权限异常一律忽略——日志不该阻断 tick。
    """
    global _LOG_MODE_CHECKED
    if _LOG_MODE_CHECKED:
        return
    _LOG_MODE_CHECKED = True
    try:
        if os.path.exists(LOG_FILE):
            if os.stat(LOG_FILE).st_mode & 0o077:
                os.chmod(LOG_FILE, 0o600)
    except OSError:
        pass


def log(parts: str) -> None:
    """写一行结构化日志：`<iso8601> <parts>`。token 一律先过 core.fingerprint。

    文件以 0600 创建（`os.open` 显式 mode）：`open(..., "a")` 会按 umask 落成
    0644，等于把日志暴露给同机其他用户。日志本身只存指纹，但不该依赖这个前提。
    """
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    _rotate_log_if_needed(LOG_FILE)
    _ensure_log_private()
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    line = f"{ts} {parts}\n".encode("utf-8")
    try:
        fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass


def _fmt_h(ms: float) -> str:
    return f"{ms / 3600000:.1f}h"


# ── state ────────────────────────────────────────────────────────────
def _read_json_dict(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError, UnicodeError):
        st = {}
    return st if isinstance(st, dict) else {}


class McliBusyError(RuntimeError):
    """mcli 状态锁等待超时，不得无锁写入。"""


@contextlib.contextmanager
def _mcli_locked():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    deadline = time.monotonic() + MCLI_LOCK_TIMEOUT_S
    handle = file_lock.try_acquire(MCLI_LOCK)
    while handle is None:
        if time.monotonic() >= deadline:
            raise McliBusyError(f"state_mcli.lock 被占用超过 {MCLI_LOCK_TIMEOUT_S:.1f}s")
        time.sleep(MCLI_LOCK_POLL_S)
        handle = file_lock.try_acquire(MCLI_LOCK)
    try:
        yield
    finally:
        file_lock.release(handle)


def migrate_split(*, keeper_locked: bool = False) -> None:
    """先迁移 mcli，再移除旧键；调用者未持主锁时先取得主锁。"""
    if not keeper_locked:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        handle = file_lock.try_acquire(LOCK_FILE)
        if handle is None:
            raise McliBusyError("keeper.lock 被占用，无法迁移状态")
        try:
            migrate_split(keeper_locked=True)
        finally:
            file_lock.release(handle)
        return
    with _mcli_locked():
        st = _read_json_dict(STATE_FILE)
        if "mcli" not in st:
            return
        if not os.path.exists(MCLI_STATE_FILE):
            _write_mcli_locked(_load_mcli_for_write())
        st.pop("mcli")
        core.atomic_write_json(STATE_FILE, st)


def load_state(*, keeper_locked: bool = False) -> dict:
    """读 CIBA 状态；纯读，不做迁移（迁移由入口显式调 `migrate_split`）。"""
    st = _read_json_dict(STATE_FILE)
    st.pop("mcli", None)  # 旧布局遗留键：CIBA 视图永远看不到 mcli
    return st


def load_mcli_state() -> dict:
    """读 mcli 状态；仅当独立文件**不存在**时回退读旧布局的 `mcli` 子对象（只读）。

    文件存在但内容为空/不可解析时以它为准（空即无 mcli 事实），不回退——否则
    迁移刚写成 `{}` 的窗口里会读出已被接管、即将被删除的旧子对象。
    """
    if os.path.exists(MCLI_STATE_FILE):
        return _read_json_dict(MCLI_STATE_FILE)
    legacy = _read_json_dict(STATE_FILE).get("mcli")
    return legacy if isinstance(legacy, dict) else {}


def _load_mcli_for_write() -> dict:
    """锁内读取写盘基线：等同 `load_mcli_state`（旧布局时以遗留子对象为初值）。"""
    return load_mcli_state()


def save_ciba(st: dict) -> None:
    """由 keeper 单写者原子写入 CIBA 状态。

    迁移未完成（独立文件还不存在）时把磁盘上的遗留 `mcli` 子对象原样带回：
    `load_state` 已把它从视图里摘掉，若此处直接丢弃，一次"迁移因锁忙跳过"
    就会永久删掉尚未搬家的 mcli 事实。
    """
    st.pop("mcli", None)
    if not os.path.exists(MCLI_STATE_FILE):
        legacy = _read_json_dict(STATE_FILE).get("mcli")
        if isinstance(legacy, dict) and legacy:
            st["mcli"] = legacy
    st["schema"] = 1
    st["history"] = st.get("history", [])[-HISTORY_LIMIT:]
    core.atomic_write_json(STATE_FILE, st)


def _write_mcli_locked(m: dict) -> None:
    """mcli 状态落盘的唯一写点；调用者必须已持 `state_mcli.lock`。"""
    core.atomic_write_json(MCLI_STATE_FILE, m)


def update_mcli(mutator, *, guard=None) -> bool:
    """持锁读取 mcli 状态、检查 guard、增量修改并原子写入。"""
    with _mcli_locked():
        m = _load_mcli_for_write()
        if guard is not None and not guard(m):
            return False
        mutator(m)
        _write_mcli_locked(m)
    return True


def apply_mcli_minted(new_token: str, old_token_code=None) -> bool:
    """铸票成功后更新 keeper state 的 mcli 段（mcli 归属写者，走 update_mcli）。

    只记指纹与身份哈希，均不可逆；不记明文。写失败不阻断成功结论（warn 到
    stderr，返回 False；下 tick 探测自愈身份）；锁争用同样只降级为 stderr。
    """
    now = int(time.time() * 1000)

    def _apply(m: dict) -> None:
        m["token_fp"] = core.fingerprint(new_token)
        m["token_id"] = core.token_id(new_token)
        m["last_ok"] = now
        m["last_probe"] = now
        m["minted_at"] = now
        m["fail_count"] = 0
        m["alert"] = False        # 铸票成功即解除告警：挡住 tick 陈旧结论残留的 ALERT
        m.pop("stop_retry", None)
        if old_token_code is not None:
            m.setdefault("observed", {})["old_token_code_after_mint"] = old_token_code

    try:
        return update_mcli(_apply)
    except Exception as e:  # noqa: BLE001 —— state 记录失败不阻断铸票成功
        print(f"[warn] keeper state 更新失败（不阻断结果，下 tick 自愈）: {e}",
              file=sys.stderr)
        return False
