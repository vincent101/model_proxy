#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""keeper_common.py — CIBA/mcli 两条链路实施模块与 keeper 编排层共享的底座。

内容为原 keeper.py 的日志 / state 原语与阈值常量，逐字搬移；物理位置
从编排层下沉到此模块，避免 keeper ↔ 链路模块循环 import。
`apply_mcli_minted` 是 state.json 的 mcli 段唯一写者（tick 与人工 CLI 共用）。

日志只存指纹；state 明文只在 state.json（0600）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

import mtsso_core as core

STATE_DIR = os.path.expanduser(core.STATE_DIR)
STATE_FILE = os.path.expanduser(core.STATE_FILE)
LOG_FILE = os.path.expanduser(core.LOG_FILE)
LOCK_FILE = os.path.expanduser(core.LOCK_FILE)

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
def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    st["schema"] = 1
    st["history"] = st.get("history", [])[-HISTORY_LIMIT:]
    core.atomic_write_json(STATE_FILE, st)  # 0600 / 目录 0700 / 原子写


def apply_mcli_minted(new_token: str, old_token_code=None) -> bool:
    """铸票成功后更新 keeper state 的 mcli 段（mcli state 的唯一写者）。

    只记指纹不记明文（方案 §4.5）。写失败不阻断成功结论（warn 到 stderr，
    返回 False；下 tick 探测自愈 token_fp）。并发说明：人工 --force 与
    launchd tick 撞在毫秒级写窗口的概率 ≈ 0，且后果可恢复
    （bootstrap/refresh-once 重建），不加锁。
    """
    try:
        st = {}
        try:
            with open(os.path.expanduser(core.STATE_FILE), encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {}
        if not isinstance(st, dict):
            st = {}
        m = st.get("mcli") if isinstance(st.get("mcli"), dict) else {}
        now = int(time.time() * 1000)
        m["token_fp"] = core.fingerprint(new_token)
        m["last_ok"] = now
        m["last_probe"] = now
        m["minted_at"] = now
        m["fail_count"] = 0
        m.pop("stop_retry", None)
        if old_token_code is not None:
            m.setdefault("observed", {})["old_token_code_after_mint"] = old_token_code
        st["mcli"] = m
        core.atomic_write_json(core.STATE_FILE, st)
        return True
    except Exception as e:  # noqa: BLE001 —— state 记录失败不阻断铸票成功
        print(f"[warn] keeper state 更新失败（不阻断结果，下 tick 自愈）: {e}",
              file=sys.stderr)
        return False
