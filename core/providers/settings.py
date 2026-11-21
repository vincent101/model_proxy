"""provider 级本机设置：凭据路径、桥库路径等"机器相关"的配置项。

设计边界（2026-11-21 provider 设置收敛）：

- **模块内置默认值**保证开箱可用：不在 supply 上声明任何路径也能跑。
- **本机配置文件** `config/provider_settings.json` 用于换机器/换平台时改路径，
  不需要动 supply、不需要动主配置。文件缺失或某键缺失即回落默认值。
- **supply 上的同名字段仍是最高优先级覆盖**：同一路径若要按 supply 分叉
  （例如 IDE 与另一个账号并存），写在那一条 supply 上即可。

为什么不放 supply：6 条 IDE supply 的 `credential_file`/`bridge_lib` 值完全
相同，属于纯重复；且桥库路径按平台不同（`.dylib`/`.dll`/`.so`），写在配置里
等于把 mac 路径固化成配置事实。这与 catpaw-cli 的收敛方向一致。

读取策略：进程内按 (mtime, size) 缓存，文件改动后下一次调用自动生效
（与主配置的 reload 解耦：路径变更不需要重启服务）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# config/provider_settings.json：与 model_proxy_config.json 同目录
SETTINGS_BASENAME = "provider_settings.json"

_cache: dict[str, object] = {"key": None, "data": {}}


def settings_path() -> str:
    """本机设置文件路径。

    可用 MODEL_PROXY_PROVIDER_SETTINGS 覆盖（测试与多实例隔离用）。
    """
    override = os.environ.get("MODEL_PROXY_PROVIDER_SETTINGS")
    if override:
        return os.path.expanduser(override)
    return os.fspath(Path(__file__).resolve().parent.parent.parent
                     / "config" / SETTINGS_BASENAME)


def load() -> dict:
    """读取并缓存整份设置；文件缺失/损坏一律按空设置处理（fail-open 到默认值）。

    损坏时不抛异常：这是"可选覆盖层"，坏文件不应该让服务起不来；
    调用方拿到的仍是可用默认值。
    """
    path = settings_path()
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        _cache["key"], _cache["data"] = (path, None, None), {}
        return {}
    if _cache.get("key") == key:
        return _cache["data"]  # type: ignore[return-value]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    _cache["key"], _cache["data"] = key, data
    return data


def section(provider: str) -> dict:
    """取某个 provider 的设置段（非 dict 或缺失返回空 dict）。"""
    raw = load().get(provider)
    return raw if isinstance(raw, dict) else {}


def get(provider: str, key: str, default=None):
    val = section(provider).get(key)
    return default if val is None else val


def reset_cache() -> None:
    """测试用：清空缓存。"""
    _cache["key"], _cache["data"] = None, {}


# 平台相关的桥库默认目录与文件名后缀
_BRIDGE_SUFFIX = {"darwin": ".dylib", "win32": ".dll", "linux": ".so"}
_BRIDGE_DIR = {"darwin": "~/.local/lib", "win32": "~/.local/lib",
               "linux": "~/.local/lib"}


def default_bridge_lib() -> str:
    """按当前平台给出桥库默认路径（未配置任何东西时用它）。"""
    platform = sys.platform
    suffix = _BRIDGE_SUFFIX.get(platform, ".so")
    folder = _BRIDGE_DIR.get(platform, "~/.local/lib")
    return f"{folder}/libcatpaw_bridge{suffix}"
