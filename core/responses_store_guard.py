"""Responses store=false 引用持久化 item 的兼容层。

背景：Codex SDK 请求顶层常带 ``store=false``，但历史 input 会引用服务端
reasoning item（形如 ``rs_...``）。上游 Responses 网关因此返回 400：

    Item with id 'rs_...' not found.
    Items are not persisted when `store` is set to false.
    Try again with `store` set to true, or remove this item from your input.

按上游建议采用方案 A：遇到该特定错误时，将本次出站请求的 ``store`` 改为
``true`` 并同 supply 重试一次。这样保留完整 reasoning 历史，不删除任何
上下文 item。

设计边界：
- 不默认改请求；
- 只在该特定 400 出现后触发；
- 只改顶层布尔字段 ``store``；
- 同请求只重试一次；
- 不加入 tried_set、不触发 cooldown；
- 失败后回落既有 failover 逻辑。
"""

import json


# 上游错误文案中稳定可识别的子串。保持小写匹配，避免大小写差异。
_STORE_MARKER = "items are not persisted when `store` is set to false"


def parse_store_disabled_rejection(resp_body: bytes) -> bool:
    """识别 ``store=false`` 导致历史 item 引用失败的 400。

    判据刻意保守：必须同时出现持久化错误文案。不解析具体 item id——本方案
    不删除 item，因此无需关心缺失的是 rs_/msg_/fc_ 哪一种。
    """
    try:
        text = resp_body.decode("utf-8", errors="replace").lower()
    except Exception:
        return False
    return _STORE_MARKER in text


def enable_store_for_retry(body_json: dict) -> bool:
    """把顶层 ``store`` 置为 true，返回是否发生修改。

    仅当当前值为布尔 ``False`` 时修改；缺省（None/不存在）不处理——缺省的
    行为依上游实现而定，盲目写入可能改变默认语义。
    """
    if not isinstance(body_json, dict):
        return False
    if body_json.get("store") is False:
        body_json["store"] = True
        return True
    return False
