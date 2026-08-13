---
type: design-decision
status: confirmed
target: "[[tools/model_proxy/core/server.py]]"
tags: [architect, model_proxy, cooldown, failover, errorcode-grouping]
---

# 按 errorcode 分组的 cooldown 策略组方案（单层全局架构）

## 背景与问题

model_proxy 早期冷却机制有两个硬伤：
1. **402 不触发 failover**：硬编码 `_FAILOVER_STATUSES` 为 `{401,403,429} ∪ 5xx`，402（额度耗尽）不在白名单，命中 402 时原样透传给客户端，不冷却不换渠道，阻断任务。
2. **冷却时长单一**：只有全局 `cooldown_seconds` 秒数，不可按状态码区分。

需求：把冷却参数扩写为"按 errorcode 分组的策略组列表"，不同状态码走不同冷却时长；所有出现在策略组里的状态码都触发 failover。纯秒数表达，不落盘，不引入绝对时间 spec。

## 架构设计：单层全局

### 核心原则

**只有顶层 `cooldown_rules`，无 supply 级覆盖，无全局白名单缓存，无 default_cd 兜底，无老配置兼容。所有 supply 共用同一份策略组。**

### 1. 配置 schema

```json
{
  "cooldown_rules": [
    {"errorcode": [401, 403, 429, 500, 502, 503, 504], "cooldown_seconds": 60},
    {"errorcode": [402], "cooldown_seconds": 21600},
    {"errorcode": ["URLError"], "cooldown_seconds": 60}
  ]
}
```

**字段说明：**
- `cooldown_rules`：顶层策略组列表，有序。每条含 `errorcode`（int 或 `"URLError"` 字符串数组）和 `cooldown_seconds`（int 秒数，每条必须）。
- 无 supply 级 `cooldown_rules`，所有 supply 共用顶层策略组。
- 无 `default_cooldown_seconds` 字段参与决策。

**errorcode 元素类型：**
- int：HTTP 状态码（401/402/429/5xx 等）
- `"URLError"` 字符串：网络错误 sentinel（`urllib.error.URLError`/`OSError`，无 HTTP 状态码）

**多条策略组命中同一 code**：取首条（配置数组顺序优先），不报错。

**无 cooldown_rules 配置时**：返回 `[]` → 所有 code 未命中 → 全部透传 + 告警。不做老配置隐式兼容。

### 2. ConfigStore

`ConfigStore.get_cooldown_rules() -> list[dict]`：直接 `return self._config.get("cooldown_rules", [])`（浅拷贝）。无隐式默认策略组，无白名单缓存。

**校验**（`_validate_cooldown_rules`，在 `_validate_config` 中调用）：
- 每条 rule 必须有 `errorcode`（非空 list）和 `cooldown_seconds`（正 int）
- errorcode 元素必须是 int 或 `"URLError"` 字符串
- 非法 rule log warning + 跳过（不阻断加载），不进有效决策列表
- 校验只告警，不影响加载成功（`_reload_locked` 仍返回 True）

**已删除**（相比早期双层方案的遗留）：
- `_DEFAULT_FAILOVER_STATUSES` 常量
- `_DEFAULT_COOLDOWN_SECONDS` 常量
- `get_failover_statuses()` / `_compute_failover_statuses()` / `self._failover_statuses` 字段
- `get_default_cooldown()` 方法
- `_reload`/`_reload_locked` 里重算白名单的代码

### 3. resolve_cooldown_seconds 函数

```python
def resolve_cooldown_seconds(errorcode, cs: "ConfigStore") -> int | None:
    """按 errorcode 查顶层 cooldown_rules，首条命中返回 cooldown_seconds，未命中返回 None。
    errorcode: int(HTTP 状态码) 或 "URLError" 字符串。"""
    for rule in cs.get_cooldown_rules():
        if errorcode in rule.get("errorcode", []):
            return int(rule["cooldown_seconds"])
    return None
```

纯函数，无 IO，无副作用。未命中返回 `None`（无 `default_cd` 兜底）。

### 4. _forward 热路径

**HTTPError 分支**：
```python
secs = resolve_cooldown_seconds(resp_status, cs)
if failover == "on" and secs is not None:
    log.warning("cooldown+failover: supply=%s status=%s secs=%s", supply_id, resp_status, secs)
    self._acc["failover"] = 1
    self._acc["attempt_errors"].append((supply_id, f"http_{resp_status}"))
    cd.cooldown(supply_id, secs)
    tried_set.add(supply_id)
    continue
# 未命中策略 → 透传 + 告警 + 累计 unconfigured_hits
log.warning("unconfigured upstream status: supply=%s status=%s (not in cooldown_rules, passing through)", supply_id, resp_status)
_record_unconfigured(resp_status)
upstream_msg = _extract_upstream_error_message(resp_body)
self._write_buffered_response(resp_status, [], error_body_for_source(source, resp_status, f"upstream error {resp_status}: {upstream_msg}"))
self._acc["final_error"] = f"upstream_error {resp_status} {upstream_msg}"
return
```

**URLError 分支**：
```python
secs = resolve_cooldown_seconds("URLError", cs)
if failover == "on" and secs is not None:
    log.warning("cooldown+failover(net): supply=%s err=%s secs=%s", supply_id, e, secs)
    self._acc["failover"] = 1
    self._acc["attempt_errors"].append((supply_id, f"net_error:{e}"))
    cd.cooldown(supply_id, secs)
    tried_set.add(supply_id)
    continue
# 未配 URLError 策略 → 透传 502 + 告警
log.warning("unconfigured net error: supply=%s err=%s (URLError not in cooldown_rules, passing through)", supply_id, e)
_record_unconfigured("URLError")
self._write_buffered_response(502, [], error_body_for_source(source, 502, f"upstream error: {e}"))
self._acc["final_error"] = f"upstream net error: {e}"
return
```

**关键语义**：
- 命中策略（`secs is not None`）+ `failover == "on"` → cooldown + failover + continue
- 未命中策略 → 透传上游错误给客户端 + 告警 + 累计 unconfigured_hits + return（不冷却不 failover）
- `failover == "off"` + 命中策略 → 不冷却不 failover，透传（`secs is not None` 但条件不满足，落入未命中分支）

### 5. unconfigured_hits 全局计数 + status 暴露

模块级线程安全计数器，记录未命中 `cooldown_rules` 的 errorcode：

```python
_unconfigured_hits: dict[str, int] = {}
_unconfigured_lock = threading.Lock()

def _record_unconfigured(code) -> None:
    key = str(code)
    with _unconfigured_lock:
        _unconfigured_hits[key] = _unconfigured_hits.get(key, 0) + 1

def _snapshot_unconfigured_hits() -> dict[str, int]:
    with _unconfigured_lock:
        return dict(_unconfigured_hits)
```

`_handle_status` 在响应 JSON 里暴露：
```json
"unconfigured_codes": {"402": 3, "URLError": 1}
```

即 `{code_str: count}`，只含命中过未配置的 code。重启清零（纯内存）。用户可据此发现"哪些 code 撞了但没配策略"，补 `cooldown_rules`。

### 6. 503 响应体摘要

所有 supply 都冷却/失败后，503 响应体拼入 `attempt_errors` 摘要：
```python
errs = self._acc.get("attempt_errors") or []
err_summary = "; ".join(f"{sid}={reason}" for sid, reason in errs) if errs else "no attempts"
msg = f"all upstream supplies failed or cooling: {err_summary}"
```

`attempt_errors` 在 failover continue 前记录 `(supply_id, reason)` 元组。

### 7. CooldownStore

纯内存，不落盘，接口不变：
- `cooldown(supply_id, seconds)` — 置入冷却
- `is_cooling(supply_id)` — 是否冷却中
- `clear_all()` — 清空（仅手动 reload 调用，mtime 自动 reload 不调）
- `snapshot()` — 返回 `{supply_id: remaining_seconds}`

**不落盘的取舍**：长冷却（如 402 的 21600s=6h）进程重启后丢失，下次请求会再撞一次再冷却。用户已确认接受。

### 8. 改动影响面

全部在 `core/server.py` + `config/*.json` + `tests/test_cooldown_rules.py`：

| # | 位置 | 改动类型 | 内容 |
|---|------|----------|------|
| 1 | `ConfigStore` | 新增方法 | `get_cooldown_rules()` 直接返回顶层策略组列表 |
| 2 | `ConfigStore._validate_config` | 强化校验 | `_validate_cooldown_rules` 校验 errorcode/cooldown_seconds 格式，非法 rule log warning + 跳过 |
| 3 | 模块级 | 新增函数 | `resolve_cooldown_seconds(errorcode, cs)` 纯函数查策略组 |
| 4 | 模块级 | 新增计数器 | `_unconfigured_hits` + `_record_unconfigured` + `_snapshot_unconfigured_hits` |
| 5 | `_forward` HTTPError 分支 | 重写 | `resolve_cooldown_seconds` + 命中则 cooldown+failover，未命中透传+告警+计数 |
| 6 | `_forward` URLError 分支 | 重写 | 同上，sentinel 为 `"URLError"` |
| 7 | `_forward` 503 终态 | 改造 | 拼入 `attempt_errors` 摘要 |
| 8 | `_handle_status` | 新增字段 | `unconfigured_codes` 暴露未配置 code 计数 |
| 9 | config 文件 | 新增字段 | 顶层 `cooldown_rules` 三条策略组 |
| 10 | 测试 | 专项测试 | `test_cooldown_rules.py` 覆盖 9 类场景 |

**已删除（相比早期双层方案的遗留，当前代码已无这些内容）**：
- `_DEFAULT_FAILOVER_STATUSES` / `_DEFAULT_COOLDOWN_SECONDS` 常量
- `get_failover_statuses()` / `_compute_failover_statuses()` / `self._failover_statuses`
- `get_default_cooldown()` 方法
- `_reload`/`_reload_locked` 里重算白名单的代码
- `default_cd = cs.get_default_cooldown()` 行
- HTTPError/URLError 分支里 `if secs is None: secs = default_cd` 兜底
- `_handle_status` 里 `default_cooldown_seconds` 字段
- supply 级 `cooldown_rules` 覆盖逻辑
- 老配置隐式兼容（无 `cooldown_rules` 时不再生成等效策略组）

**风险评估：**
- 核心风险在 `_forward` HTTPError/URLError 分支——热路径，每请求必走。`resolve_cooldown_seconds` 是纯函数无 IO，`get_cooldown_rules()` 返回浅拷贝列表（锁内取引用），性能无虑。
- 未命中策略时透传上游错误给客户端：这是显式设计选择（不做隐式兜底），用户通过 `unconfigured_codes` 观测补策略。
- snapshot 结构仅新增 `unconfigured_codes` 字段，不影响既有字段。

### 9. 测试点

| 场景 | 配置 | 预期行为 |
|------|------|----------|
| 402 命中策略 | `cooldown_rules` 含 402→21600s | failover + cooldown 21600s |
| URLError 命中策略 | `cooldown_rules` 含 "URLError"→60s | failover + cooldown 60s |
| 未命中 code（418） | `cooldown_rules` 不含 418 | 透传 418 + log warning + 不冷却不 failover |
| URLError 未配策略 | `cooldown_rules` 不含 "URLError" | 透传 502 + 告警 |
| unconfigured_hits 计数 | 空 rules，撞 402 三次 | status 显示 `{"402":3}` |
| 多条策略命中同 code | 两条都含 402，不同秒数 | 首条（配置顺序优先） |
| 无 cooldown_rules | 空 rules | 所有 code 透传+告警（不做老配置兼容） |
| 校验器 | rule 缺 cooldown_seconds | log warning + 跳过 |
| resolve 纯函数 | int code / "URLError" / 未命中 | 返回秒数 / None |

## 验证方式

1. **402 冷却**：配置 `cooldown_rules` 含 402→21600s，mock 上游返回 402，验证 failover 触发 + cooldown 21600s。
2. **URLError 策略**：配 `"URLError"`→60s，mock 网络错误，验证 failover + 冷却 60s。
3. **未命中透传**：mock 上游返回 418（不在策略组），验证透传 418 + 不冷却不 failover。
4. **unconfigured_codes**：空 rules 撞 402 三次，`GET /model_proxy/status` 验证 `unconfigured_codes` 含 `{"402":3}`。
5. **503 摘要**：所有 supply 都 402 冷却后发请求，验证 503 响应体含 attempt_errors 摘要。
6. **首条优先**：两条策略都含 402，验证首条 cooldown_seconds 生效。
7. **无规则透传**：空 rules，mock 500，验证透传 500 + 不冷却不 failover。

## 关联

- [[2026-08-07-reasoning-thinking-truncation-and-protocol-consistency]] — failover 循环结构与 budget_retry 互斥关系
- [[2026-08-04-in-band-route-command-design]] — ConfigStore 热重载机制
