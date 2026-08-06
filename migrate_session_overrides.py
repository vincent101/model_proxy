"""一次性迁移脚本：把主 config 的 dispatch.session_overrides 并入 sidecar 单一存储。

设计文档：docs/designs/2026-08-06-session-overrides-single-storage.md（§2.3）

用法：
    python3 migrate_session_overrides.py [config_path] [sidecar_path]

    不传参数则用默认路径：
        config_path = config/model_proxy_config.json
        sidecar_path = config/session_overrides.json

行为（§2.3 伪代码）：
    1. 读主 config 的 strategies，对每条 strategy 取 dispatch.session_overrides
    2. 转成新式 dict（route_id / last_seen=now / created=now）写入 sidecar
       —— sidecar 已有该 session_id 的记录则跳过（不覆盖新数据）
    3. 从主 config 删除 dispatch.session_overrides；dispatch 变空字典则整体移除
    4. 顺序：先写 sidecar，再写主 config（幂等可重跑）

迁移前自动备份主 config 到 .bak.<时间戳>（复用 _install_ops.py 的命名约定）。
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import Any

# 复用产品代码里的原子写函数
from core.commands import _atomic_write_json as _sidecar_atomic_write
from _config_ops import atomic_write as _config_atomic_write


def _backup_config(config_path: Path, ts: str) -> Path:
    """备份主 config 到 .bak.<时间戳>，复用 _install_ops.py:172 的命名约定。"""
    bak_path = config_path.with_name(config_path.name + f".bak.{ts}")
    bak_path.write_bytes(config_path.read_bytes())
    return bak_path


def migrate(config_path: Path, sidecar_path: Path, now_iso: str | None = None) -> dict[str, Any]:
    """执行迁移，返回摘要 dict。

    参数：
        config_path: 主 config 文件路径
        sidecar_path: sidecar 文件路径
        now_iso: ISO8601 时间戳字符串（如 "2026-08-06T12:00:00Z"），
                 不传则用当前 UTC 时间。参数化便于单测。

    返回：
        {
            "migrated": int,          # 新写入 sidecar 的条目数
            "skipped": int,           # sidecar 已有、被跳过的条目数
            "strategies_touched": int, # 有迁移动作的 strategy 数
            "backup_path": str | None, # 主 config 备份路径（无迁移则为 None）
            "sidecar_path": str,       # sidecar 文件路径
        }
    """
    if now_iso is None:
        now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    strategies = cfg.get("strategies") or []

    # 读现有 sidecar（缺失视为空）
    if sidecar_path.exists():
        sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if not isinstance(sidecar_data, dict):
            sidecar_data = {}
    else:
        sidecar_data = {}

    migrated = 0
    skipped = 0
    strategies_touched = 0
    any_migration = False

    for strategy in strategies:
        if not isinstance(strategy, dict):
            continue
        dispatch = strategy.get("dispatch")
        if not isinstance(dispatch, dict):
            continue
        legacy = dispatch.get("session_overrides")
        if not legacy:
            continue

        client_token = strategy.get("client_token", "")
        bucket = sidecar_data.setdefault(client_token, {})
        strategies_touched += 1
        any_migration = True

        for sid, route_id in legacy.items():
            if sid in bucket:
                skipped += 1
                continue
            bucket[sid] = {
                "route_id": route_id,
                "last_seen": now_iso,
                "created": now_iso,
            }
            migrated += 1

        # 从主 config 删除该字段
        dispatch.pop("session_overrides", None)
        if not dispatch:
            strategy.pop("dispatch", None)

    if not any_migration:
        return {
            "migrated": 0,
            "skipped": skipped,
            "strategies_touched": 0,
            "backup_path": None,
            "sidecar_path": str(sidecar_path),
        }

    # 备份主 config（先备份，再写）
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    backup_path = _backup_config(config_path, ts)

    # 先写 sidecar（原子写 + chmod 0600）
    _sidecar_atomic_write(sidecar_path, sidecar_data)

    # 后写主 config（原子写 + chmod 0600）
    _config_atomic_write(str(config_path), cfg)

    return {
        "migrated": migrated,
        "skipped": skipped,
        "strategies_touched": strategies_touched,
        "backup_path": str(backup_path),
        "sidecar_path": str(sidecar_path),
    }


def _main():
    base = Path(__file__).resolve().parent
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else base / "config" / "model_proxy_config.json"
    sidecar_path = Path(sys.argv[2]) if len(sys.argv) > 2 else base / "config" / "session_overrides.json"

    if not config_path.exists():
        print(f"Error: config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    summary = migrate(config_path, sidecar_path)
    print(f"迁移完成：")
    print(f"  新写入 sidecar: {summary['migrated']} 条")
    print(f"  跳过（sidecar 已有）: {summary['skipped']} 条")
    print(f"  涉及 strategy: {summary['strategies_touched']} 条")
    print(f"  主 config 备份: {summary['backup_path'] or '(无迁移，未备份)'}")
    print(f"  sidecar 文件: {summary['sidecar_path']}")


if __name__ == "__main__":
    _main()
