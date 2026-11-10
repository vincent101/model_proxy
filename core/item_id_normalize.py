"""Responses input 里 function_call item id 的前缀规范化（2026-11-10）。

背景：Responses 规范要求 function_call item 的 `id` 以 `fc_` 开头。本代理早期
`gen_item_id()` 产出的是 `item_` 前缀（已于 2026-11-06 commit cd0cd49 修正为 fc_），
但**存量客户端历史**里仍是旧的 `item_...` id；这些历史回放到 responses 上游时，
上游校验前缀直接 400（terra/sol 实测：
"Invalid 'input[46].id': 'item_...'. Expected an ID that begins with 'fc'"）。

定性：这不是上游怪癖（无需 learn / TTL），而是**本方历史数据的格式不合规**，
因此按无条件规范化处理——出站前就地重写，仅影响本次出站副本，不改客户端数据。

配对安全性（2026-11-10 上游实测）：function_call 与 function_call_output 的配对
靠 `call_id` 维系，`id` 前缀改写不影响模型读取工具结果
（重写 id + 原 call_id → 模型正确返回工具输出）。

`gen_item_id()` 已生成 fc_，本模块只兜历史；两条路径合起来保证「出站恒合规」。
"""

_LEGACY_PREFIX = "item_"
_CANONICAL_PREFIX = "fc_"

# 只处理这两类 item 的 id；message/rs_/fco_ 等前缀由各自生成点保证，
# 不在此扩大改写面（避免误伤其他类型的上游约定）。
_REWRITABLE_TYPES = frozenset({"function_call", "custom_tool_call"})


def normalize_responses_item_ids(body_json: dict) -> int:
    """把 input[] 中 function_call/custom_tool_call 的 `item_` id 前缀改写为 `fc_`。

    - 返回改写的 item 数量（0 = 无需改写）。
    - 只换前缀，保留原 hex 尾部（便于日志/取证溯源）。
    - 仅处理 type 命中且 id 为 「item_ 开头」的字符串；其他一律不动。
    - 幂等：连续调用第二次返回 0。
    - 原地修改传入的 body_json（调用方保证这是本次请求的私有副本）。
    """
    if not isinstance(body_json, dict):
        return 0
    items = body_json.get("input")
    if not isinstance(items, list):
        return 0
    rewritten = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in _REWRITABLE_TYPES:
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.startswith(_LEGACY_PREFIX):
            continue
        item["id"] = _CANONICAL_PREFIX + item_id[len(_LEGACY_PREFIX):]
        rewritten += 1
    return rewritten
