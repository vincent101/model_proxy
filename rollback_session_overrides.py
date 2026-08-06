#!/usr/bin/env python3
"""回滚 session_overrides 迁移：还原主 config 备份 + 删 sidecar。

用法：
    python3 rollback_session_overrides.py [config_path] [sidecar_path]

场景：迁移脚本（migrate_session_overrides.py）跑完后发现问题，要还原到迁移前。
      找最新的 model_proxy_config.json.bak.<时间戳> 备份还原主 config，
      删除迁移产生的 sidecar 文件。

注意：
    - 新代码兼容「主 config 有 session_overrides 字段」的读取路径
      （effective_overrides 合并逻辑仍在），所以回滚后即使不重启代理，
      mtime 热重载也会感知到 config 变化并恢复旧路由。重启只为状态干净。
    - sidecar 里若有 $route 命令后续写入的新记录（非迁移来的），也会被一并删除。
      若想保留这些新记录、只还原主 config，手动操作即可，别用本脚本。
"""

import glob
import shutil
import sys
from pathlib import Path


def find_latest_backup(config_path: Path) -> Path | None:
    """找最新的 model_proxy_config.json.bak.<时间戳> 备份。"""
    pattern = str(config_path) + ".bak.*"
    backups = sorted(glob.glob(pattern))
    return Path(backups[-1]) if backups else None


def rollback(config_path: Path, sidecar_path: Path) -> bool:
    config_path = Path(config_path)
    sidecar_path = Path(sidecar_path)

    # 1. 找最新备份
    bak = find_latest_backup(config_path)
    if not bak:
        print(f"错误：找不到 {config_path}.bak.* 备份，无法回滚。")
        print("      迁移脚本跑过才会产生备份；若没跑过迁移，无需回滚。")
        return False

    print(f"最新备份: {bak.name}")

    # 2. 还原主 config
    shutil.copy2(bak, config_path)
    print(f"已还原主 config: {bak.name} -> {config_path.name}")

    # 3. 删 sidecar（迁移前不存在，迁移产生，直接删）
    if sidecar_path.exists():
        sidecar_path.unlink()
        print(f"已删除 sidecar: {sidecar_path.name}")
    else:
        print(f"sidecar 不存在，跳过: {sidecar_path.name}")

    # 4. 提示
    print()
    print("回滚完成。代理 mtime 热重载会自动感知 config 变化，恢复迁移前路由。")
    print("若想状态干净，可重启代理：")
    print("  bash model_proxy_cli.sh off && bash model_proxy_cli.sh on")
    return True


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config/model_proxy_config.json"
    sidecar_path = sys.argv[2] if len(sys.argv) > 2 else "config/session_overrides.json"
    ok = rollback(config_path, sidecar_path)
    sys.exit(0 if ok else 1)
