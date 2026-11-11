"""Responses 出站 input item id 剥离（2026-11-11）。

## 不变量

`passthrough responses → responses` 出站时，input[] 里不携带**无法兑现**的 item id。

## 为什么是剥离而不是改前缀

上游对 item id 有两条独立校验，改前缀只能过第一条：

1. **前缀校验**：`function_call` 须 `fc_`、`message` 须 `msg_`、`reasoning` 须 `rs_`。
   旧 `item_` 前缀在此 400（2026-11-06 修生成侧 `gen_item_id`，2026-11-10 补出站改名）。
2. **存在性 / 绑定校验**：上游要求 id 真实存在，且 `encrypted_content` 内部绑定的
   item_id 必须与 item 声明的 id 一致。实测（2026-11-11，terra）：
   - reasoning 带伪造 `rs_` id + 真实 encrypted_content → 400
     `Encrypted content item_id did not match the target item id`
   - 同一份 encrypted_content **不带 id** → 200

代理用 `gen_reasoning_id()` 生成的是随机 id，与密文内部 id 永不可能匹配；而真实 id
只存在于产出该响应的那个上游的存储里，proxy 无状态、failover 还可能换上游，拿不到。
因此**改名是把不合规伪装的更隐蔽，剥离才是诚实解**。

## 为什么不是无条件删所有 id

`item_reference` 形态本身只有 id，剥离后成空 item（实测 `Missing required parameter:
'input[0].id'`）。故引用形态豁免（保留 id，fail-fast 由上游判定），只剥离**有负载**的 item。

## 与 a2r 的一致性

a2r 出站输入一直是「无 id」形态（`core/translate.py` 的 `_messages_to_input` 不产出 id；
reasoning 只带 `encrypted_content`），该形态 2026-11-06 已上游闭环实测。本模块让 passthrough
向同一已验证形态对齐，而非引入新行为。

## 边界

- 只作用于本次出站副本；不改客户端数据、不改 `call_id` / `encrypted_content` / `summary`。
- 不读 `store` / `previous_response_id`：store 缺省 true，且 failover 换 supply 后 id 必悬空，
  按 store 分流会让 failover 路径失效（实测 store=true 下伪造 id 同样 400）。
- 不看前缀、不做类型白名单：按「有负载就剥离」的结构判据，未来新 item type 自动覆盖。
"""

_NON_PAYLOAD_KEYS = frozenset({"id", "type", "status"})


def sanitize_responses_input_ids(body_json: dict) -> tuple[int, int]:
    """剥离 Responses input[] 中无法兑现的 item id。

    返回 ``(stripped, kept_references)``：
    - ``stripped``：剥离 id 的 item 数（0 = 无需改写）。
    - ``kept_references``：豁免的引用形态 item 数（保留 id，由上游 fail-fast）。

    规则（按序，命中即止）：
    1. body 非 dict / input 非 list → ``(0, 0)``。
    2. 逐 item：非 dict 或没有 ``id`` 键 → 跳过。
    3. ``type == "item_reference"``，或去掉 ``{id, type, status}`` 后无任何键
       （裸引用形态）→ 保留 id，计入 kept_references。
    4. 其余 → 删除 ``id``，计入 stripped。

    幂等：连续调用第二次返回 ``(0, 0)``。原地修改传入的 body_json（调用方保证
    这是本次请求的私有副本）。
    """
    if not isinstance(body_json, dict):
        return 0, 0
    items = body_json.get("input")
    if not isinstance(items, list):
        return 0, 0

    stripped = 0
    kept_references = 0
    for item in items:
        if not isinstance(item, dict) or "id" not in item:
            continue
        payload_keys = set(item) - _NON_PAYLOAD_KEYS
        if item.get("type") == "item_reference" or not payload_keys:
            # 引用形态：id 是唯一语义载体，剥离会变成空 item（上游 missing_required_parameter）
            kept_references += 1
            continue
        del item["id"]
        stripped += 1
    return stripped, kept_references
