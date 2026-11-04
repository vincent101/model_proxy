"""Responses 历史 function_call item ID 兼容层。

背景：Codex SDK 生成的 function_call item id 形如 ``item_...``，而当前
aigc / Azure Responses 网关要求该类 item id 必须以 ``fc_`` 开头。model_proxy
本身不改 id（PASSTHROUGH 原样转发），因此 Codex 会话一旦带历史工具调用 item，
后续每轮都稳定 400。这里采用“400 触发式重写”：首轮原样转发，只有上游明确报
``Expected an ID that begins with 'fc'`` 时才对指定 input item 做最小改写并
同 supply 重试一次。

设计边界：
- 不默认清洗请求；
- 只改 id 字段，不改业务内容；
- 同请求只重试一次；
- 不加入 tried_set、不触发 cooldown；
- 失败后回落既有 failover 逻辑。
"""

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class InvalidItemIdRejection:
    """上游对 Responses input item id 的拒绝信息。"""

    index: int
    expected_prefix: str


def _parse_error(resp_body: bytes) -> dict | None:
    """兼容多种 error envelope，返回内层 error dict；解析失败返回 None。"""
    try:
        parsed = json.loads(resp_body)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    err = parsed.get("error")
    if isinstance(err, dict):
        return err
    ext = parsed.get("ext")
    if isinstance(ext, dict) and isinstance(ext.get("error"), dict):
        return ext["error"]
    return None


def parse_invalid_item_id_rejection(resp_body: bytes) -> InvalidItemIdRejection | None:
    """识别 ``input[N].id`` 的前缀不匹配 400。

    匹配要点：
    - param 形如 ``input[4].id``；
    - message 同时包含 ``Expected an ID that begins with`` 与期望前缀。
      当前已知上游要求 ``fc``（wire 上通常表现为 ``fc_``）。
    """
    err = _parse_error(resp_body)
    if err is None:
        return None
    param = err.get("param")
    message = err.get("message")
    if not (isinstance(param, str) and isinstance(message, str)):
        return None
    if not param.startswith("input[") or not param.endswith("].id"):
        return None
    digits = param[6:-4]
    if not digits.isdigit():
        return None
    expected_marker = "expected an id that begins with '"
    lower = message.lower()
    marker_pos = lower.find(expected_marker)
    if marker_pos == -1:
        return None
    quote_pos = marker_pos + len(expected_marker)
    end_pos = message.find("'", quote_pos)
    if end_pos == -1:
        return None
    expected_prefix = message[quote_pos:end_pos]
    if not expected_prefix:
        return None
    return InvalidItemIdRejection(int(digits), expected_prefix)


def _wire_id_prefix(expected_prefix: str) -> str:
    """错误文案给的是不含下划线的前缀（``fc``），wire id 需要补下划线。"""
    return expected_prefix if expected_prefix.endswith("_") else expected_prefix + "_"


def rewrite_invalid_item_id(body_json: dict, rejection: InvalidItemIdRejection) -> bool:
    """按上游拒绝信息改写指定 input item 的 id，返回是否发生修改。

    只处理 dict item；不校验 item.type（上游错误体只报 id 字段，不报类型）。
    新 id 稳定：同一旧 id 总是映射到同一新 id，重试/回放可复现。
    """
    inputs = body_json.get("input") if isinstance(body_json, dict) else None
    if not isinstance(inputs, list):
        return False
    idx = rejection.index
    if idx < 0 or idx >= len(inputs):
        return False
    item = inputs[idx]
    if not isinstance(item, dict):
        return False
    old_id = item.get("id")
    if not (isinstance(old_id, str) and old_id):
        return False
    prefix = _wire_id_prefix(rejection.expected_prefix)
    if old_id.startswith(prefix):
        return False
    digest = hashlib.sha256(old_id.encode("utf-8")).hexdigest()
    # OpenAI wire id 通常带下划线（fc_<opaque>）；32 字符足够且可读。
    item["id"] = f"{prefix}{digest[:32]}"
    return True
