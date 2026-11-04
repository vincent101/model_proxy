"""Responses 历史 function_call item ID 兼容层。

背景：Codex SDK 生成的 function_call item id 形如 ``item_...``，而当前
aigc / Azure Responses 网关要求该类 item id 必须以 ``fc_`` 开头。model_proxy
本身不改 id（PASSTHROUGH 原样转发），因此 Codex 会话一旦带历史工具调用 item，
后续每轮都稳定 400。

本模块采用“400 触发式批量重写”：首轮原样转发；只有上游明确报
``Expected an ID that begins with 'fc'`` 时，才把 input 中所有同类不合规 id
统一稳定重写，并同 supply 重试一次。

为什么不只重写错误体指出的 index：线上已观察到同一请求中往往存在多个历史
function_call item；上游每次只返回第一个错误位置，逐个修会形成“每次重试都
暴露下一个坏 id”的循环。批量重写保证一次重试内把可静态识别的坏 id 全部修复。

设计边界：
- 不默认清洗请求；
- 只改 id / call_id 字段，不改业务内容；
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


@dataclass(frozen=True)
class RewriteResult:
    """批量重写结果。

    fields: 实际发生修改的 (index, field) 数量；
    id_count / call_id_count: 分别对应 ``id`` 与 ``call_id`` 的修改数。
    """

    fields: int
    id_count: int
    call_id_count: int


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


def _wire_call_prefix(expected_prefix: str) -> str:
    """OpenAI call_id 的 wire 前缀：``fc`` → ``call_``。

    历史样本显示 ``call_id`` 形如 ``call_TVkb...``。当前上游错误文案只报
    ``fc`` / function_call id；未观察到 call_id 报错时保持原值，不做臆造。
    """
    return "call_"


def _stable_id(old_value: str, prefix: str) -> str:
    digest = hashlib.sha256(old_value.encode("utf-8")).hexdigest()
    # OpenAI wire id 通常带下划线（fc_<opaque>）；32 字符足够且可读。
    return f"{prefix}{digest[:32]}"


def _candidate_types_for_id(expected_prefix: str) -> tuple[str, ...]:
    """哪些 input item type 需要批量重写 ``id``。

    当前证据（上游 400 + codex 样本）都指向 function_call：
    - 上游要求 ``fc_`` 前缀；
    - codex 历史 function_call item 是 ``item_...``。
    不扩到 message/reasoning：它们天然就是 ``msg_`` / ``rs_`` 前缀，上游也
    接受这些形态；盲目重写会破坏语义且缺乏证据。
    """
    if expected_prefix == "fc":
        return ("function_call", "custom_tool_call")
    return ("function_call",)


def rewrite_invalid_item_ids(
    body_json: dict,
    rejection: InvalidItemIdRejection,
) -> RewriteResult:
    """按上游拒绝信息批量重写 input 中同类 item 的不合规 id / call_id。

    规则：
    - ``id``：错误体指出 index；若该 item 的 type 属于本错误前缀的候选类型，
      批量处理整个 input 中同类型、且前缀不匹配的 ``id``。
    - ``call_id``：function_call_output 与对应 function_call 的关联键。
      若上游错误指向 function_call 且关联 call_id 不符合 wire 前缀，则成对
      重写：function_call.call_id 与 function_call_output.call_id 使用同一
      新值，保持引用一致。
    - 新值稳定：同一旧值始终映射到同一新值，重试/回放可复现。
    """
    inputs = body_json.get("input") if isinstance(body_json, dict) else None
    if not isinstance(inputs, list):
        return RewriteResult(0, 0, 0)

    idx = rejection.index
    if idx < 0 or idx >= len(inputs) or not isinstance(inputs[idx], dict):
        return RewriteResult(0, 0, 0)

    id_prefix = _wire_id_prefix(rejection.expected_prefix)
    id_types = _candidate_types_for_id(rejection.expected_prefix)
    seed_type = inputs[idx].get("type")

    id_count = 0
    if seed_type in id_types:
        for item in inputs:
            if not isinstance(item, dict) or item.get("type") not in id_types:
                continue
            old_id = item.get("id")
            if isinstance(old_id, str) and old_id and not old_id.startswith(id_prefix):
                item["id"] = _stable_id(old_id, id_prefix)
                id_count += 1

    # call_id 成对重写：只处理与 seed_type 相关的 item，保持最小影响面。
    call_id_count = 0
    if seed_type == "function_call":
        call_prefix = _wire_call_prefix(rejection.expected_prefix)
        new_call_ids: dict[str, str] = {}
        # 第一遍：收集 function_call 的 call_id 映射
        for item in inputs:
            if not (isinstance(item, dict) and item.get("type") == "function_call"):
                continue
            old_call = item.get("call_id")
            if (isinstance(old_call, str) and old_call
                    and not old_call.startswith(call_prefix)):
                new_call_ids[old_call] = _stable_id(old_call, call_prefix)
        # 第二遍：更新 function_call / function_call_output，两处保持一致
        if new_call_ids:
            for item in inputs:
                if not isinstance(item, dict):
                    continue
                if item.get("type") not in ("function_call", "function_call_output"):
                    continue
                old_call = item.get("call_id")
                if isinstance(old_call, str) and old_call in new_call_ids:
                    item["call_id"] = new_call_ids[old_call]
                    call_id_count += 1

    return RewriteResult(id_count + call_id_count, id_count, call_id_count)
