---
type: design-decision
status: confirmed
target: tools/model_proxy
tags: [architect, model_proxy, cooldown, cli]
---

# CooldownStore 扩展冷却原因（errorcode）展示

## 背景与问题

`model_proxy_cli.sh status` 的 cooldown 段只显示 supply 剩余冷却秒数，看不到该 supply 因什么 errorcode（如 `http_429`、`net_error:...`）进入冷却。上游限流时用户无从判断每个 supply 具体坏在哪个错误码，排查效率低。

根因：CooldownStore 只存 `supply_id → cooldown_until(epoch)`，不存触发原因。冷却写入点（server.py L1737/L1760）调用 `cd.cooldown(supply_id, secs)` 时已有 errorcode 字符串（在 attempt_errors 里），但没传进 CooldownStore。

## 方案设计

### 核心思路

CooldownStore 加 `_reason` 字典（`supply_id → errorcode`），`cooldown()` 方法加 `reason` 参数，`snapshot()` 返回结构从 `dict[str, float]` 升级为 `dict[str, dict]`，CLI 渲染时展示。

### 1. CooldownStore 数据结构扩展

**文件**：`core/server.py` L625-664

```python
class CooldownStore:
    def __init__(self):
        self._until: dict[str, float] = {}    # supply_id -> cooldown_until(epoch)
        self._reason: dict[str, str] = {}     # supply_id -> errorcode（当前冷却周期的触发原因）
        self._lock = threading.Lock()

    def cooldown(self, supply_id: str, seconds: int, reason: str = "") -> None:
        """将 supply 置入冷却。reason 为触发此冷却的 errorcode（如 "http_429"、"net_error:..."）。"""
        until = time.time() + seconds
        with self._lock:
            self._until[supply_id] = until
            if reason:
                self._reason[supply_id] = reason

    def clear_all(self) -> None:
        with self._lock:
            self._until.clear()
            self._reason.clear()

    def snapshot(self) -> dict[str, dict[str, float | str]]:
        """返回 supply_id -> {"remain": 剩余秒, "reason": errorcode}（仅含仍在冷却中的 supply）。"""
        now = time.time()
        with self._lock:
            items = list(self._until.items())
            reasons = dict(self._reason)
        result: dict[str, dict] = {}
        for supply_id, until in items:
            remaining = until - now
            if remaining > 0:
                result[supply_id] = {
                    "remain": round(remaining, 1),
                    "reason": reasons.get(supply_id, ""),
                }
        return result
```

**设计决策**：

- **reason 覆盖语义**：同一 supply 再次被 `cooldown()` 调用时，`_reason` 随 `_until` 一起覆盖。语义为"当前冷却周期的触发原因"，不累积历史。
- **不累积多错误码**：一个 supply 同时只可能因一个 errorcode 处于冷却（failover 逻辑保证：一个 supply 失败后进 tried_set，同一请求不会再次选中它）。
- **冷却到期后 reason 不展示**：snapshot() 只返回 `remaining > 0` 的条目，过期的 reason 残留在 `_reason` 字典中但不展示。下次同 supply 再次冷却时覆盖。`clear_all()` 同步清空。
- **不复用 attempt_errors**：attempt_errors 是请求级临时列表（每次请求 `_acc` 新建），CooldownStore 是跨请求的持久状态。两者职责不同，CooldownStore 自持 reason 更简单可靠。
- **reason 默认空串**：`cooldown()` 的 `reason` 参数有默认值，保证调用方不传时不报错（向后兼容边界）。

### 2. 冷却写入点改动

**文件**：`core/server.py`

两处冷却触发点，在调用 `cd.cooldown()` 时传入已构造的 errorcode：

**L1730-1737（HTTPError 路径）**：
```python
# 改前
cd.cooldown(supply_id, secs)
# 改后
cd.cooldown(supply_id, secs, f"http_{resp_status}")
```

**L1752-1760（URLError 路径）**：
```python
# 改前
cd.cooldown(supply_id, secs)
# 改后
cd.cooldown(supply_id, secs, f"net_error:{e}")
```

注意：这两处已经在 `attempt_errors.append` 里构造了同样的 errorcode 字符串，现在只是多传一份给 CooldownStore。不引入新信息。

### 3. status API 返回

**文件**：`core/server.py` L2141-2148

```python
self._send_json(200, {
    "supplies": safe_supplies,
    "routes": cs.get_routes(),
    "strategies": strategies_out,
    "cooldown": cd.snapshot(),  # 现在返回 dict[str, dict]，结构变了
    "unconfigured_codes": _snapshot_unconfigured_hits(),
    "version": _VERSION,
})
```

无需改代码——`cd.snapshot()` 的返回值直接序列化为 JSON。结构从 `{"s1": 45.0}` 变为 `{"s1": {"remain": 45.0, "reason": "http_429"}}`。

### 4. CLI 渲染改动

**文件**：`_format_ops.py` L659-664

```python
# 改前
if cooldown and not cooldown_unknown:
    lines.append("")
    lines.append("cooldown (剩余秒):")
    for sid, remain in sorted(cooldown.items()):
        ref = ",".join(refs.get(sid, [])) or "未被引用"
        lines.append(f"  {_pad(sid, 24)} {int(remain)}s  ← {ref}")

# 改后
if cooldown and not cooldown_unknown:
    lines.append("")
    lines.append("cooldown (剩余秒):")
    for sid, info in sorted(cooldown.items()):
        # 兼容旧格式 {sid: float} 和新格式 {sid: {"remain": float, "reason": str}}
        if isinstance(info, dict):
            remain = info.get("remain", 0)
            reason = info.get("reason", "")
        else:
            remain = info
            reason = ""
        ref = ",".join(refs.get(sid, [])) or "未被引用"
        suffix = f" ({reason})" if reason else ""
        lines.append(f"  {_pad(sid, 24)} {int(remain)}s  ← {ref}{suffix}")
```

**兼容逻辑说明**：`isinstance(info, dict)` 判断兼容旧格式 `{sid: float}`，防止外部消费者（或旧 mock 数据）传旧格式时崩溃。真实运行路径走新格式。

## CLI 展示效果

### 改前

```
health: cooldown 2/6 · degraded 0 · overrides 0

cooldown (剩余秒):
  kimi-k3-sankuai-3339      29s  ← r1.opus(tok)
  claude-opus-sankuai-1     6s   ← r1.opus(tok)
```

### 改后

```
health: cooldown 2/6 · degraded 0 · overrides 0

cooldown (剩余秒):
  kimi-k3-sankuai-3339      29s  ← r1.opus(tok) (http_429)
  claude-opus-sankuai-1     6s   ← r1.opus(tok) (net_error:timeout)
```

## 向后兼容与边界

### snapshot() 结构变化的影响

`snapshot()` 返回从 `dict[str, float]` 变为 `dict[str, dict[str, float|str]]`。消费者盘点：

| 消费者 | 位置 | 影响 |
|--------|------|------|
| status API JSON | server.py L2145 | 无——直接序列化，结构变化透传给 CLI |
| reload cleared count | server.py L2158 `len(cd.snapshot())` | 无——`len()` 不受 value 类型影响 |
| _format_ops 渲染 | _format_ops.py L608/662 | 需改——已在上文设计 |
| test_format_ops mock | tests/test_format_ops.py 多处 | 需改——见测试清单 |

### 多错误码

一个 supply 在一次请求中只会因一个 errorcode 进入冷却（failover 逻辑保证）。如果在不同请求中先因 `http_429` 冷却、冷却期间又被 `net_error` 冷却（理论上面板调度会跳过冷却中的 supply，但防御性考虑），`_reason` 随 `_until` 一起覆盖为最新 errorcode。语义为"当前冷却周期的触发原因"。

### 冷却过期

`snapshot()` 只返回 `remaining > 0` 的条目。过期的 `_until`/`_reason` 残留在字典中但不在 CLI/status 展示。下次同 supply 再次冷却时覆盖。`clear_all()`（手动 reload）同步清空两个字典。

### 离线 status 路径

`cooldown_unknown=True` 时（离线路径，server 进程不在线），`_format_ops.py` L632-633 显示 `cooldown (未知)/N`，明细段跳过。不涉及 reason 展示，无需改动。

## 测试改动清单

### 1. tests/test_cooldown_rules.py

`_FakeCooldown`（L102-119）签名变更：

```python
class _FakeCooldown:
    def __init__(self):
        self.cooled = []
        self.cooled_secs = []
        self.cooled_reasons = []    # 新增

    def cooldown(self, sid, secs, reason=""):   # 加 reason 参数
        self.cooled.append(sid)
        self.cooled_secs.append(secs)
        self.cooled_reasons.append(reason)      # 新增

    def snapshot(self):
        return {}   # 测试不需要改（这些测试不验证 snapshot 返回值）
```

现有测试断言不受影响（`cd.cooled`、`cd.cooled_secs` 断言不变）。可选新增断言验证 reason 传入：

- `Test402CooldownFailover`: `self.assertEqual(cd.cooled_reasons, ["http_402"])`
- `TestURLErrorCooldownFailover`: `self.assertEqual(cd.cooled_reasons[0], "net_error:...")`  （URLError 的 reason 是 `f"net_error:{e}"`，e 是 mock 的 URLError 对象，断言前缀即可）

### 2. tests/test_format_ops.py

**test_cooldown_listed**（L296-314）：mock 数据和断言更新：

```python
# 改前
"cooldown": {"s1": 45}
# 改后
"cooldown": {"s1": {"remain": 45, "reason": "http_429"}}
```

断言加：
```python
self.assertIn("http_429", joined)
```

**其他测试用例**（`"cooldown": {}`）：空 dict 不受影响，无需改。但建议至少一处加上带 reason 的 cooldown mock 以覆盖渲染路径。

### 3. 新增测试（可选但建议）

- CooldownStore 单测：验证 `cooldown(sid, secs, reason)` 后 `snapshot()` 返回的 reason 字段正确。
- 验证过期后 snapshot 不含 reason（需 mock time 或等 1 秒）。
- 验证 `clear_all()` 清空 reason。

## 落地步骤清单

1. **改 CooldownStore**（core/server.py L625-664）：加 `_reason` 字典、`cooldown()` 加 `reason` 参数、`snapshot()` 返回 `dict[str, dict]`、`clear_all()` 清 `_reason`。
2. **改冷却写入点**（core/server.py L1737/L1760）：传入 errorcode 字符串。
3. **改 CLI 渲染**（_format_ops.py L659-664）：解析新格式、加 reason 展示、加兼容逻辑。
4. **改测试**（tests/test_cooldown_rules.py）：`_FakeCooldown.cooldown()` 签名加 `reason`。
5. **改测试**（tests/test_format_ops.py）：`test_cooldown_listed` mock 数据和断言更新。
6. **跑测试**：`cd tools/model_proxy && python3 -m unittest tests.test_cooldown_rules tests.test_format_ops -v`。
7. **手动验证**：启动 model_proxy，触发一个上游错误（如改错 appkey），`model_proxy_cli.sh status` 确认 cooldown 行展示 errorcode。

## 关联

- [[docs/designs/2026-07-23-model-proxy-full-audit|model_proxy 全面审查]]
