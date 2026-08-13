---
type: design-decision
status: confirmed
target: "[[tools/model_proxy/core/server.py]]"
tags: [architect, model_proxy, cooldown, failover, errorcode-grouping]
---

# 按 errorcode 分组的 cooldown 策略组方案

## 背景与问题

model_proxy 当前冷却机制有两个硬伤：
1. **402 不触发 failover**：`_FAILOVER_STATUSES`（`core/server.py:595`）硬编码为 `{401,403,429} ∪ 5xx`，402（额度耗尽）不在白名单。命中 402 时走行 1636-1644 原样透传给客户端，不冷却不换渠道，阻断任务。
2. **冷却时长单一**：只有 `cooldown_seconds`（行 1600），秒级，且整个 failover 白名单与冷却时长硬编码、不可按状态码区分。

需求：把冷却参数从单一秒数扩写为"按 errorcode 分组的策略组列表"，不同状态码走不同冷却时长；所有出现在策略组里的状态码都触发 failover。纯秒数表达，不落盘，不引入绝对时间 spec。

## 方案设计

### 1. 配置 schema

策略组列表放顶层 `cooldown_rules`，supply 级可选同名字段覆盖。

**完整 JSON 示例：**

```json
{
  "default_cooldown_seconds": 60,
  "cooldown_rules": [
    {"errorcode": [401, 403, 429, 500, 502, 503, 504], "cooldown_seconds": 60},
    {"errorcode": [402], "cooldown_seconds": 21600},
    {"errorcode": ["URLError"], "cooldown_seconds": 60}
  ],
  "supplies": [
    {
      "id": "kimi-k3-sankuai-3339",
      "url": "...",
      "appkey": "...",
      "cooldown_rules": [
        {"errorcode": [402], "cooldown_seconds": 3600}
      ]
    }
  ]
}
```

**字段说明：**
- `cooldown_rules`：策略组列表，有序。每条含 `errorcode`（int 或 `"URLError"` 字符串数组）、`cooldown_seconds`（int 秒数）。
- 顶层 `cooldown_rules`：全局默认策略组。
- supply 级 `cooldown_rules`：该 supply 专属策略组，覆盖顶层。

**errorcode 元素类型：**
- int：HTTP 状态码（401/402/429/5xx 等）
- `"URLError"` 字符串：网络错误 sentinel（`urllib.error.URLError`/`OSError`，无 HTTP 状态码）

**合并/覆盖规则（supply 级颗粒度）：**
- supply 有自己的 `cooldown_rules` 时，该 supply 的 errorcode 查找优先用 supply 级策略组；supply 级未命中的 code 回退顶层策略组。
- supply 无 `cooldown_rules` 时，全部用顶层策略组。

**多条策略组命中同一 code**：取首条（配置数组顺序优先），不报错。

**向后兼容（老配置无 `cooldown_rules`）：**
隐式生成等效策略组 `{errorcode: [401,403,429] ∪ 5xx, cooldown_seconds: default_cooldown_seconds}`，行为与现状完全一致。`default_cooldown_seconds` 保留，仅在"config 完全无 `cooldown_rules`"时作为隐式默认策略组的时长（向后兼容命脉），新配置不再参与决策。

### 2. failover 白名单动态化

删除行 595 硬编码的 `_FAILOVER_STATUSES`。在 `ConfigStore` 中新增动态聚合：

```python
_DEFAULT_FAILOVER_STATUSES = frozenset([401, 403, 429]) | frozenset(range(500, 600))  # 行595 改名，仅老配置回退用

# ConfigStore 新增
def get_failover_statuses(self) -> frozenset[int]:
    """返回当前配置聚合的 failover 状态码集合（数字，不含 URLError sentinel）。
    缓存值，reload 时重算，不每请求重算。"""
    with self._lock:
        return self._failover_statuses or _DEFAULT_FAILOVER_STATUSES

def _compute_failover_statuses(self) -> frozenset[int]:
    """聚合所有策略组（顶层 + 各 supply 级）的 errorcode 中的 int 部分。
    URLError sentinel 不进白名单集合（URLError 分支单独处理）。"""
    codes: set[int] = set()
    for rule in self._config.get("cooldown_rules", []):
        for c in rule.get("errorcode", []):
            if isinstance(c, int):
                codes.add(c)
    for supply in self._config.get("supplies", []):
        for rule in (supply.get("cooldown_rules") or []):
            for c in rule.get("errorcode", []):
                if isinstance(c, int):
                    codes.add(c)
    return frozenset(codes)
```

缓存时机：在 `_reload_locked()` 末尾、`self._config = new_config` 之后调用 `self._failover_statuses = self._compute_failover_statuses()`。初始化 `_reload()` 同样调用。

**隐式回退**：配置无 `cooldown_rules` 时 `_compute_failover_statuses` 返回空集，`get_failover_statuses()` 回退 `_DEFAULT_FAILOVER_STATUSES`。

### 3. cooldown 时长决策

行 1600 原 `cd_seconds = int(supply.get("cooldown_seconds", default_cd))` 替换为按状态码/sentinel 查策略组：

```python
# 新增模块级函数
def resolve_cooldown_seconds(errorcode, supply: dict, cs: "ConfigStore") -> int | None:
    """按 errorcode（int 状态码 或 "URLError" sentinel）查命中的策略组，返回 cooldown_seconds。
    
    查找顺序：supply 级 cooldown_rules（正序）→ 顶层 cooldown_rules（正序）。
    首条命中即返回（配置顺序即优先级）。
    未命中任何策略组 → None。
    """
    supply_rules = (supply.get("cooldown_rules") or [])
    top_rules = cs.get_cooldown_rules()
    for rules in (supply_rules, top_rules):
        for rule in rules:
            if errorcode in rule.get("errorcode", []):
                return int(rule.get("cooldown_seconds", 60))
    return None
```

ConfigStore 新增 `get_cooldown_rules()`：`return self._config.get("cooldown_rules", [])`。

### 4. _forward 热路径改造

**行 1600**：删除 `cd_seconds = int(supply.get("cooldown_seconds", default_cd))`（改在 failover 分支内按需 resolve）。

**行 1627-1635 HTTPError failover 分支**：
```python
# 旧：if failover == "on" and resp_status in _FAILOVER_STATUSES:
# 新：
if failover == "on" and resp_status in cs.get_failover_statuses():
    secs = resolve_cooldown_seconds(resp_status, supply, cs)
    if secs is None:
        secs = default_cd   # 仅老配置回退路径会走到
    log.warning("cooldown+failover: supply=%s status=%s key_tail4=%s secs=%s",
                supply_id, resp_status, appkey[-4:] if appkey else "", secs)
    self._acc["failover"] = 1
    self._acc["attempt_errors"].append((supply_id, f"http_{resp_status}"))
    cd.cooldown(supply_id, secs)
    tried_set.add(supply_id)
    continue
```

**行 1645-1658 URLError 分支**：
```python
except (urllib.error.URLError, OSError) as e:
    if failover == "on":
        secs = resolve_cooldown_seconds("URLError", supply, cs)
        if secs is None:
            secs = default_cd   # 未配 URLError 策略 → 老行为
        log.warning("cooldown+failover(net): supply=%s err=%s key_tail4=%s secs=%s",
                    supply_id, e, appkey[-4:] if appkey else "", secs)
        self._acc["failover"] = 1
        self._acc["attempt_errors"].append((supply_id, f"net_error:{e}"))
        cd.cooldown(supply_id, secs)
        tried_set.add(supply_id)
        continue
    self._write_buffered_response(
        502, [], error_body_for_source(source, 502, f"upstream error: {e}"))
    self._acc["final_error"] = f"upstream net error: {e}"
    return
```

URLError 始终走 failover（与现有行为一致），是否冷却取决于策略组是否含 `"URLError"` sentinel。

### 5. 503 响应体带 attempt_errors 摘要

行 1844-1848 当前：
```python
self._write_buffered_response(
    503, [], error_body_for_source(
        source, 503, "all upstream supplies failed or cooling"))
self._acc["final_error"] = "all supplies failed or cooling"
```

改造为拼入 attempt_errors 摘要：
```python
errs = self._acc.get("attempt_errors") or []
err_summary = "; ".join(f"{sid}={reason}" for sid, reason in errs) if errs else "no attempts"
msg = f"all upstream supplies failed or cooling: {err_summary}"
self._write_buffered_response(
    503, [], error_body_for_source(source, 503, msg))
self._acc["final_error"] = msg
```

`attempt_errors` 已在行 1631/1650 记录 `(supply_id, reason)` 元组，直接复用。最小实现，不引入新类/函数。

### 6. CooldownStore 不改

`CooldownStore`（行 547-586）保持原样：
- 纯内存，不落盘
- `cooldown(supply_id, seconds)` 接口不变
- `is_cooling` / `clear_all` / `snapshot` 不变
- `clear_all()` 仅手动 reload 调用，mtime 自动 reload 不调（与现有注释行 572 一致）

**不落盘的取舍**：长冷却（如 402 的 21600s=6h）进程重启后丢失，下次请求会再撞一次 402 再冷却。用户已确认接受（撞一次冷却 6h，其他渠道兜底；最坏情况每 6h 撞一次，所有渠道都 402 时每 6h 一波 503）。

### 7. 改动影响面

全部在 `core/server.py`：

| # | 位置 | 改动类型 | 内容 |
|---|------|----------|------|
| 1 | 行 595 `_FAILOVER_STATUSES` | 改现有常量 | 改名 `_DEFAULT_FAILOVER_STATUSES`，仅老配置回退用 |
| 2 | 行 374-513 `ConfigStore` | 新增方法 | `get_cooldown_rules()`、`get_failover_statuses()`、`_compute_failover_statuses()`；`_reload_locked`/`_reload` 末尾重算白名单缓存；`_validate_config` 增加策略组格式校验（容错：非法策略组跳过 + log warning，不阻断加载） |
| 3 | 行 595 附近 | 新增模块级函数 | `resolve_cooldown_seconds(errorcode, supply, cs)` |
| 4 | 行 1600 | 删除 | 删除 `cd_seconds = int(supply.get("cooldown_seconds", default_cd))` 行 |
| 5 | 行 1627 | 改现有逻辑 | `_FAILOVER_STATUSES` → `cs.get_failover_statuses()`；行 1633 `cd.cooldown` 用 `resolve_cooldown_seconds` 返回值 |
| 6 | 行 1645-1658 URLError 分支 | 改现有逻辑 | `cd.cooldown` 用 `resolve_cooldown_seconds("URLError", supply, cs)`，未命中回退 `default_cd` |
| 7 | 行 1844-1848 | 改现有逻辑 | 503 响应体拼入 attempt_errors 摘要 |

行 1301 `default_cd = cs.get_default_cooldown()` 保留（兜底路径用）。行 429-431 `get_default_cooldown` 保留。行 2003-2033 `_handle_status` 不改（snapshot 结构不变）。

**风险评估：**
- 核心风险在行 1627 failover 条件——热路径，每请求必走。`get_failover_statuses()` 返回缓存值（reload 时重算），`resolve_cooldown_seconds` 是纯函数无 IO，性能无虑。
- `default_cd` 兜底路径：仅当老配置（无 `cooldown_rules`）或策略组漏配某 code 但该 code 又在白名单时走到。新配置下白名单完全由策略组生成，不会"在白名单但不在策略组"，故新配置不会走兜底。
- snapshot 结构不变：CLI 或其他消费方无需适配。

### 8. 向后兼容测试点

| 场景 | 配置 | 预期行为 |
|------|------|----------|
| 老配置 | 无 `cooldown_rules`，只有 `default_cooldown_seconds: 60` | 401/403/429/5xx 触发 failover，冷却 60s；402 不 failover（透传）；URLError 走 `default_cd` failover；行为与改前完全一致 |
| 新配置 | 有 `cooldown_rules` 含 402→21600s | 402 触发 failover + 冷却 6h；429 触发 failover + 冷却 60s |
| URLError 策略 | 配 `{"errorcode": ["URLError"], "cooldown_seconds": 60}` | 网络错误触发 failover + 冷却 60s；未配则走 `default_cd` |
| 混合配置 | supply 级 `cooldown_rules` 覆盖顶层 | 该 supply 的 402 走 supply 级（如 3600s）；该 supply 的 429 回退顶层（60s） |
| 503 摘要 | 所有 supply 都 402 冷却后发请求 | 503 响应体含 `attempt_errors` 摘要（如 `kimi-k3-sankuai-3339=http_402`） |
| 手动 reload | `POST /model_proxy/reload` | 内存清空（`clear_all`），行为不变 |
| mtime reload | 改配置文件触发自动 reload | 不动冷却状态（内存不变，与现有一致） |

## 验证方式

1. **老配置回归**：不改配置文件（无 `cooldown_rules`），用坏 appkey 触发 401，验证 cooldown 60s + failover 行为不变。
2. **新配置 402 冷却**：配置 `cooldown_rules: [{"errorcode": [402], "cooldown_seconds": 21600}]`，mock 上游返回 402，验证：
   - failover 触发（换下一个 supply）
   - cooldown 21600s（`GET /model_proxy/status` 验证 remaining ≈ 21600）
3. **URLError 策略**：配 `{"errorcode": ["URLError"], "cooldown_seconds": 60}`，mock 网络错误，验证 failover + 冷却 60s。
4. **503 摘要**：所有 supply 都 402 冷却后发请求，验证 503 响应体含 attempt_errors 摘要。
5. **混合配置**：supply 级设 402→3600s，顶层设 402→21600s，验证该 supply 的 402 走 3600s。
6. **default_cd 兜底**：老配置（无 `cooldown_rules`）触发 401，验证走 `default_cooldown_seconds`。
7. **mtime reload 不动冷却**：触发 402 冷却后 `touch config`，发请求触发 maybe_reload，验证冷却状态不变。

## 关联

- [[2026-08-07-reasoning-thinking-truncation-and-protocol-consistency]] — failover 循环结构与 budget_retry 互斥关系
- [[2026-08-04-in-band-route-command-design]] — ConfigStore 热重载机制
