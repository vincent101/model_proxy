"""标准 HTTP 出站路径的共用纯逻辑。

设计记录：docs/designs/2026-11-21-message_start用量形态与三类provider配置设计.md §4.1

职责：把 legacy 与 catpaw-cli 共用的三件事抽成纯函数——
    1. 鉴权值解析（appkey 直取 / appkey_file 每请求读取）
    2. system 块注入（幂等）
    3. 出站 header 构造

**行为必须与抽取前逐字一致**。`core/server.py` 保留同名兼容导出，覆盖既有
测试与 `core/cli/_config_ops.py` 的导入。

边界：本模块不感知 provider 种类、不做选路、不发请求。
"""
from __future__ import annotations

import os

# extra_headers 禁键（小写比较）：鉴权归 appkey/appkey_file 管，避免双来源冲突；
# content-length 由 body 长度决定，不可覆盖。
FORBIDDEN_EXTRA_HEADERS = {"authorization", "x-api-key", "content-length"}

# 透传时从客户端 header 中剔除的键：鉴权与长度由本代理重新生成。
SKIP_CLIENT_HEADERS = {"host", "content-length", "authorization", "x-api-key"}


def resolve_appkey(supply: dict) -> str:
    """出站鉴权值：appkey 直取，或 appkey_file 每请求读取解析。

    - appkey_file 支持 ~ 展开（os.path.expanduser）；逐行 strip 后精确匹配
      "AUTHORIZATION:" 前缀取值（目标文件非标准 YAML，勿用 yaml 库；
      与 CatpawCLI token store 同款策略，无缓存）。
    - 失败（文件不存在 / 无 AUTHORIZATION 行 / 值为空）抛 ValueError（消息含
      supply id 与原因），调用方出站前 500 返回、不进 cooldown（配置问题
      非上游问题，冷却会误伤 failover）。
    - appkey 与 appkey_file 均缺 → 返回 ""（维持历史空串照发行为；必填由
      validate_supply_ext 在加载/写盘层把关，热重载降级路径不额外拦截）。
    """
    appkey = supply.get("appkey")
    if appkey:
        return appkey
    appkey_file = supply.get("appkey_file")
    if not appkey_file:
        return ""
    sid = supply.get("id", "")
    path = os.path.expanduser(appkey_file)
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        raise ValueError(f"supply {sid} appkey_file 读取失败: {e}") from e
    for line in lines:
        line = line.strip()
        if line.startswith("AUTHORIZATION:"):
            token = line[len("AUTHORIZATION:"):].strip()
            if token:
                return token
            raise ValueError(f"supply {sid} appkey_file 读取失败: AUTHORIZATION 值为空")
    raise ValueError(f"supply {sid} appkey_file 读取失败: 无 AUTHORIZATION 行")


def inject_system_block(body_json: dict, text: str) -> bool:
    """system_inject：向 body["system"] 前置合并注入块（幂等，原地修改）。

    合并矩阵（设计记录 §2）：
    - system 缺省 / 空数组 → [inject_block]
    - 字符串 → [inject_block, {"type":"text","text":原值}]
    - 非空数组 → 首块 text == 注入文本 → 跳过（幂等硬约束：budget_retry /
      failover 重试复用同一 body_json，重复注入会叠加计费标识块）；
      否则前置插入
    - 首块畸形（非 dict）→ 视为不匹配 → 前置插入
    返回是否发生修改（False = 幂等跳过；未知形态不动）。
    """
    inject_block = {"type": "text", "text": text}
    system = body_json.get("system")
    if system is None or (isinstance(system, list) and not system):
        body_json["system"] = [inject_block]
        return True
    if isinstance(system, str):
        body_json["system"] = [inject_block, {"type": "text", "text": system}]
        return True
    if isinstance(system, list):
        first = system[0]
        if isinstance(first, dict) and first.get("text") == text:
            return False
        body_json["system"] = [inject_block] + system
        return True
    return False


def build_standard_headers(client_headers, appkey: str, body_len: int,
                           *, converted: bool, extra_headers) -> dict:
    """构造标准 HTTP 出站 header（纯函数，不读全局状态）。

    顺序与抽取前完全一致：
      1. 复制客户端 header，剔除 host/content-length/authorization/x-api-key
      2. 写入 Bearer + x-api-key 双认证
      3. 写入 Content-Length（由 body 长度决定）
      4. converted=True（即非 PASSTHROUGH）时设 Content-Type: application/json
      5. extra_headers 最后覆盖（最高优先），跳过 content-length

    Args:
        client_headers: 入站 header 映射（大小写不敏感比较键名）
        appkey: 已解析的鉴权值
        body_len: 出站 body 字节数
        converted: 是否经过协议转换（决定 Content-Type 是否强制 JSON）
        extra_headers: supply 声明的额外 header，可为 None
    """
    out: dict[str, str] = {}
    for key, val in client_headers.items():
        if key.lower() in SKIP_CLIENT_HEADERS:
            continue
        out[key] = val
    out["Authorization"] = f"Bearer {appkey}"
    out["x-api-key"] = appkey
    out["Content-Length"] = str(body_len)
    if converted:
        out["Content-Type"] = "application/json"
    for k, v in (extra_headers or {}).items():
        if k.lower() == "content-length":
            continue
        out[k] = v
    return out
