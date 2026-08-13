---
type: design-decision
status: draft
target: "[[tools/model_proxy/core/server.py]]"
tags: [architect, model_proxy, cooldown, failover, errorcode-grouping]
---

# 按 errorcode 分组的 cooldown 策略组方案

## 背景与问题

model_proxy 当前冷却机制有三个硬伤：
1. **402 不触发 failover**：`_FAILOVER_STATUSES`（行595）硬编码为 `{401,403,429} ∪ 5xx`，402（额度耗尽）不在白名单，命中 402 时原样透传给客户端，不冷却不换渠道。
2. **冷却时长单一**：只有 `cooldown_seconds`（行1600），秒级，无法表达"额度按天刷新、冷却到次日凌晨"这种绝对时间点语义。
3. **长冷却不落盘**：`CooldownStore`（行547-586）纯内存，进程重启丢失。若 402 冷却十几小时，重启后冷却丢失会立刻又撞 402。

需求：把冷却参数从单一秒数扩写为"按 errorcode 分组的策略组列表"，不同状态码走不同冷却时长；支持绝对时间点（如次日01:00）；长冷却落盘防重启丢失。

## 方案设计

### 1. 配置 schema

策略组列表放顶层 `cooldown_rules`，supply 级可选同名字段覆盖。

**完整 JSON 示例：**

```json
{
  "default_cooldown_seconds": 60,
  "cooldown_rules": [
    {"errorcode": [402], "cooldown": "T1-0100", "name": "quota_exhausted"},
    {"errorcode": [429], "cooldown": "60s"},
    {"errorcode": [401, 403], "cooldown": "60s"},
    {"errorcode": [500, 502, 503, 504], "cooldown": "30s"}
  ],
  "supplies": [
    {
      "id": "kimi-k3-sankuai-3339",
      "url": "...",
      "appkey": "...",
      "cooldown_rules": [
        {"errorcode": [402], "cooldown": "T0-2359", "name": "quota_exhausted_today"}
      ]
    }
  ]
}
```

**字段说明：**
- `cooldown_rules`：策略组列表，有序。每条含 `errorcode`（int 数组）、`cooldown`（spec 字符串或 int）、可选 `name`（人类可读标注，用于 status 展示）。
- 顶层 `cooldown_rules`：全局默认策略组。
- supply 级 `cooldown_rules`：该 supply 专属策略组，覆盖顶层。

**合并/覆盖规则：**
- supply 有自己的 `cooldown_rules` 时，该 supply 的 errorcode 查找优先用 supply 级策略组；supply 级未命中的 code 回退顶层策略组。
- supply 无 `cooldown_rules` 时，全部用顶层策略组。

**向后兼容（老配置无 `cooldown_rules`）：**
隐式生成一个等效策略组：`{errorcode: [401,403,429] ∪ 5xx, cooldown: default_cooldown_seconds}`，行为与现状完全一致。`default_cooldown_seconds` 字段保留，作为隐式策略组的 fallback 时长，也作为 URLError 等网络错误的默认冷却。

### 2. failover 白名单动态化

删除行595 硬编码的 `_FAILOVER_STATUSES` 常量。在 `ConfigStore` 中新增动态聚合：

```python
# ConfigStore 新增
def get_failover_statuses(self) -> frozenset[int]:
    """返回当前配置聚合的 failover 状态码集合（缓存，reload 时重算）。"""
    with self._lock:
        return self._failover_statuses

def _compute_failover_statuses(self) -> frozenset[int]:
    """聚合所有策略组（顶层 + 各 supply 级）的 errorcode。"""
    codes: set[int] = set()
    for rule in self._config.get("cooldown_rules", []):
        codes.update(rule.get("errorcode", []))
    for supply in self._config.get("supplies", []):
        for rule in (supply.get("cooldown_rules") or []):
            codes.update(rule.get("errorcode", []))
    return frozenset(codes)
```

缓存时机：在 `_reload_locked()`（行495）末尾、`self._config = new_config` 之后调用 `self._failover_statuses = self._compute_failover_statuses()`。初始化 `_reload()`（行486）同样调用。

**隐式回退**：如果配置无 `cooldown_rules`（老配置），`_compute_failover_statuses` 返回空集。此时 `get_failover_statuses()` 需返回默认白名单 `{401,403,429} ∪ 5xx`。实现：`return self._failover_statuses or _DEFAULT_FAILOVER_STATUSES`（保留原常量改名作 fallback）。

### 3. cooldown 时长决策

行1600 附近，原 `cd_seconds = int(supply.get("cooldown_seconds", default_cd))` 替换为按 `resp_status` 查策略组：

```python
# 新增模块级函数
def resolve_cooldown_spec(resp_status: int, supply: dict, cs: "ConfigStore") -> tuple[str, float, str] | None:
    """按 resp_status 查命中的策略组，返回 (kind, value, spec_str) 或 None。
    
    查找顺序：supply 级 cooldown_rules（正序）→ 顶层 cooldown_rules（正序）。
    首条命中即返回（配置顺序即优先级）。
    未命中任何策略组 → None（不在白名单，不 failover）。
    """
```

**多条策略组命中同一 code**：取首条（配置数组顺序优先）。用户通过配置顺序表达优先级，不报错。

行1627 failover 条件改为：
```python
# 旧：if failover == "on" and resp_status in _FAILOVER_STATUSES:
# 新：
if failover == "on" and resp_status in cs.get_failover_statuses():
    spec = resolve_cooldown_spec(resp_status, supply, cs)
    if spec is None:
        # 不在任何策略组（但可能在白名单——仅当老配置回退场景）→ 用 default_cd
        cd.cooldown(supply_id, default_cd, reason=f"http_{resp_status}")
    else:
        kind, value, spec_str = spec
        if kind == "seconds":
            cd.cooldown(supply_id, int(value), reason=f"http_{resp_status}→{spec_str}")
        else:  # until
            cd.cooldown_until(supply_id, value, reason=f"http_{resp_status}→{spec_str}")
    tried_set.add(supply_id)
    continue
```

**URLError 分支**（行1652）：网络错误无 HTTP 状态码，走 `default_cd`（`default_cooldown_seconds`），与现有行为一致。

### 4. 时长 spec 解析器

**语法定义：**

cooldown 字段支持两种格式：

| 格式 | 语法 | 示例 | 语义 |
|------|------|------|------|
| 相对秒 | `<int>` 或 `<int>s` | `60`、`60s` | now + 60s |
| 绝对时间点 | `T<offset>-<HHMM>` | `T1-0100` | 次日 01:00 CST(UTC+8) |

**`T<offset>-<HHMM>` 完整语法定义：**
- `T`：固定前缀，标记绝对时间点 spec。
- `<offset>`：非负整数，日期偏移天数。`0`=今天，`1`=次日，`2`=后天...
- `-`：分隔符。
- `<HHMM>`：4位数字，`0000`-`2359`，表示本地时区(CST UTC+8)的时分。`0100`=01:00，`2359`=23:59。
- 完整正则：`^T(\d+)-(\d{4})$`

**`T1-0100` = 次日 01:00 Asia/Shanghai(UTC+8)**。

**跨天边界算法（方案A：固定日期偏移）：**

`T<offset>-<HHMM>` 永远表示"当前日期 + offset 天 的 HH:MM CST"，无论当前时间几点。

- 当前 14:00 触发 `T1-0100` → 明天 01:00（约11小时后）
- 当前 00:30 触发 `T1-0100` → 明天 01:00（约24.5小时后）
- 当前 23:59 触发 `T1-0100` → 明天 01:00（约1小时后）

```python
_ABSOLUTE_SPEC_RE = re.compile(r'^T(\d+)-(\d{4})$')

def _parse_absolute_spec(spec: str) -> float:
    """T1-0100 → 次日01:00 CST 的 epoch。"""
    m = _ABSOLUTE_SPEC_RE.match(spec)
    if not m:
        raise ValueError(f"invalid absolute cooldown spec: {spec}")
    offset_days = int(m.group(1))
    hhmm = m.group(2)
    hh, mm = int(hhmm[:2]), int(hhmm[2:])
    if hh > 23 or mm > 59:
        raise ValueError(f"invalid time in spec: {spec}")
    now_cst = _cst_now()
    target_date = (now_cst + timedelta(days=offset_days)).date()
    target_dt = datetime(target_date.year, target_date.month, target_date.day,
                         hh, mm, 0, tzinfo=_CST)
    return target_dt.timestamp()

def parse_cooldown_spec(spec) -> tuple[str, float]:
    """解析 cooldown spec，返回 (kind, value)。
    
    kind="seconds" → value=秒数(float)
    kind="until"   → value=epoch(float)
    
    支持格式：
      60 / "60" / "60s" → ("seconds", 60.0)
      "T1-0100"         → ("until", <epoch>)
    """
    if isinstance(spec, (int, float)):
        return ("seconds", float(spec))
    s = str(spec).strip()
    if re.match(r'^\d+s?$', s):
        return ("seconds", float(s.rstrip('s')))
    if _ABSOLUTE_SPEC_RE.match(s):
        return ("until", _parse_absolute_spec(s))
    raise ValueError(f"invalid cooldown spec: {spec}")
```

**时区处理**：复用现有 `_CST = timezone(timedelta(hours=8))`（行126）和 `_cst_now()`（行129），固定 UTC+8 偏移，不依赖 `ZoneInfo`/`tzdata`。理由：代理面向中国时区用户，固定偏移足够；精简环境可能缺 tzdata，`ZoneInfo("Asia/Shanghai")` 会抛 `ZoneInfoNotFoundError`，固定偏移彻底消除该依赖。

### 5. 长冷却落盘

**文件位置**：`tools/model_proxy/.claude_model_proxy_cooldown.json`（与现有 `.claude_model_proxy_totals.json` 同级，参照行134 `TOTALS_FILE` 模式）。

**文件格式**：
```json
{
  "kimi-k3-sankuai-3339": 1723425600.0,
  "glm-52-sankuai-8101": 1723425600.0
}
```
只存 `{supply_id: until_epoch}`。不持久化 reason（重启后 status 展示丢 reason，但 cooldown 状态不丢，功能不受影响）。

**CooldownStore 改造**（行547-586）：

```python
class CooldownStore:
    def __init__(self, path: Path | None = None):
        self._until: dict[str, float] = {}
        self._reason: dict[str, str] = {}   # 内存 only
        self._lock = threading.Lock()
        self._path = path
        if path and path.exists():
            self._load_from_disk()

    def cooldown_until(self, supply_id: str, until_epoch: float, reason: str = "") -> None:
        """将 supply 置入冷却至绝对时间点。长冷却专用（T1-0100 等）。"""
        with self._lock:
            self._until[supply_id] = until_epoch
            if reason:
                self._reason[supply_id] = reason
        if self._path:
            self._persist()

    def cooldown(self, supply_id: str, seconds: int, reason: str = "") -> None:
        """将 supply 置入冷却：until = now + seconds。"""
        self.cooldown_until(supply_id, time.time() + seconds, reason)

    def clear_all(self) -> None:
        """清空所有冷却（手动 reload 调用）。同步删盘文件。"""
        with self._lock:
            self._until.clear()
            self._reason.clear()
        if self._path and self._path.exists():
            try:
                self._path.unlink()
            except OSError:
                pass

    def snapshot(self) -> dict[str, dict]:
        """返回 {supply_id: {remaining, until, reason}}（仅含仍在冷却中的）。"""
        now = time.time()
        with self._lock:
            items = list(self._until.items())
        result = {}
        for sid, until in items:
            remaining = until - now
            if remaining > 0:
                result[sid] = {
                    "remaining": round(remaining, 1),
                    "until": until,
                    "reason": self._reason.get(sid, ""),
                }
        return result

    def _persist(self) -> None:
        """持锁拷贝 _until，锁外原子写盘。"""
        with self._lock:
            data = dict(self._until)
        try:
            _atomic_write_json(self._path, data)
        except Exception:
            log.warning("cooldown persist failed", exc_info=True)

    def _load_from_disk(self) -> None:
        """启动时从盘加载冷却状态。容错：损坏则忽略。"""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            now = time.time()
            with self._lock:
                self._until = {k: float(v) for k, v in data.items() if float(v) > now}
            log.info("cooldown.load_from_disk count=%d", len(self._until))
        except (json.JSONDecodeError, OSError, ValueError) as e:
            log.warning("cooldown file corrupt, ignoring: %s", e)
```

**启动加载**：`__init__` 中 `_load_from_disk()` 过滤已过期的条目（`until > now`），只恢复仍在冷却中的 supply。

### 6. clear_all 与落盘一致性

两条路径明确区分（与现有注释行572一致）：

| 路径 | 触发 | 内存 | 落盘 |
|------|------|------|------|
| 手动 reload | `POST /model_proxy/reload` → `_handle_reload`（行2035）→ `cd.clear_all()` | 清空 | **删盘文件** |
| mtime 自动 reload | `maybe_reload()`（行453）每请求检查 | **不动** | **不动** |

`clear_all()`（行571）改造后同时清内存 + 删盘文件。mtime reload 不调 `clear_all()`，不影响冷却状态（内存和盘都不动）。

### 7. CooldownStore 接口推荐

**推荐新增 `cooldown_until(sid, until_epoch, reason)` 绝对时间 API**，而非复用 `cooldown(sid, seconds)` 传换算后的秒数。

理由：
1. 语义清晰：`T1-0100` 解析出 epoch 后直接传入，无需"次日凌晨距现在多少秒"的中间换算。
2. 避免精度损失：长冷却（十几小时）用秒数换算在极端边界可能有浮点误差。
3. 落盘语义一致：`_until` 本就存 epoch，`cooldown_until` 直接写入，`cooldown` 内部转调它。
4. `cooldown(sid, seconds)` 改为 `cooldown_until` 的语法糖，向后兼容现有调用方（行1633/1652 改为传 reason 即可）。

### 8. status 展示

`_handle_status`（行2003）中 `cd.snapshot()` 返回值从 `{sid: remaining_seconds}` 变为 `{sid: {remaining, until, reason}}`。

展示示例：
```json
"cooldown": {
  "kimi-k3-sankuai-3339": {
    "remaining": 43200.5,
    "until": 1723425600.0,
    "reason": "http_402→T1-0100"
  }
}
```

`until` 是 epoch，CLI 可自行格式化为 ISO 时间。reason 字段标注命中的策略组（状态码 + spec 字面量）。

需要 CooldownStore 额外存 reason 元数据：是，存内存 `_reason` dict（不落盘，重启后丢失 reason 但 cooldown 仍生效）。

### 9. 503 响应体带 attempt_errors 摘要

行1844-1846 当前：
```python
self._write_buffered_response(
    503, [], error_body_for_source(
        source, 503, "all upstream supplies failed or cooling"))
```

改造为拼入 attempt_errors 摘要：
```python
# 末候选 route 全挂时
errs = self._acc.get("attempt_errors") or []
err_summary = "; ".join(f"{sid}={reason}" for sid, reason in errs) if errs else "no attempts"
cooling_info = cd.snapshot()
cooling_summary = ""
if cooling_info:
    cooling_summary = "; ".join(
        f"{sid} cooling {info['remaining']:.0f}s ({info['reason']})"
        for sid, info in cooling_info.items() if sid in {e[0] for e in errs})
msg = f"all upstream supplies failed or cooling: {err_summary}"
if cooling_summary:
    msg += f" | cooling: {cooling_summary}"
self._write_buffered_response(
    503, [], error_body_for_source(source, 503, msg))
```

最小实现点：在 503 响应分支（行1844）就地拼摘要，不引入新类/函数。`attempt_errors` 已在行1631/1650 记录 `(supply_id, reason)` 元组，直接复用。

### 10. 改动影响面

全部在 `core/server.py`，约7处改动：

| # | 位置 | 改动类型 | 内容 |
|---|------|----------|------|
| 1 | 行134 附近 | 新增常量 | `COOLDOWN_FILE` 路径常量 |
| 2 | 行547-586 `CooldownStore` | 改现有类 | `__init__` 加 path 参数 + `_load_from_disk`；新增 `cooldown_until`；`cooldown` 加 reason 参数 + 转调 `cooldown_until`；`clear_all` 加删盘；`snapshot` 扩展返回结构；新增 `_persist`/`_load_from_disk` |
| 3 | 行595 `_FAILOVER_STATUSES` | 改现有常量 | 改名 `_DEFAULT_FAILOVER_STATUSES`，仅作老配置回退用 |
| 4 | 行374-513 `ConfigStore` | 新增方法 | `get_cooldown_rules()`、`get_failover_statuses()`、`_compute_failover_statuses()`；`_reload_locked`/`_reload` 末尾重算白名单；`_validate_config` 增加策略组校验 |
| 5 | 行595 附近 | 新增模块级函数 | `parse_cooldown_spec(spec)`、`_parse_absolute_spec(spec)`、`resolve_cooldown_spec(resp_status, supply, cs)` |
| 6 | 行1600 附近 `_forward` | 改现有逻辑 | 删除 `cd_seconds` 行；行1627 `_FAILOVER_STATUSES` → `cs.get_failover_statuses()`；行1633/1652 `cd.cooldown` 调用改为按 spec kind 分支 |
| 7 | 行1844-1846 | 改现有逻辑 | 503 响应体拼入 attempt_errors + cooling 摘要 |
| 8 | 行2003-2033 `_handle_status` | 改现有逻辑 | snapshot 展示适配新结构 |
| 9 | 行2434 启动初始化 | 改现有逻辑 | `CooldownStore()` → `CooldownStore(COOLDOWN_FILE)` |

**风险评估：**
- 核心风险在行1627 failover 条件与行1633 cooldown 调用——这是热路径，每请求必走。需确保 `get_failover_statuses()` 返回缓存值（不每请求重算），`resolve_cooldown_spec` 是纯函数无 IO。
- `clear_all` 加删盘：需确保 `_path` 为 None 时不操作（防御性）。
- snapshot 返回结构变化：CLI 或其他消费方需适配。当前唯一消费点是 `_handle_status`（行2031），同文件内同步改。

### 11. 向后兼容测试点

| 场景 | 配置 | 预期行为 |
|------|------|----------|
| 老配置 | 无 `cooldown_rules`，只有 `default_cooldown_seconds: 60` | 401/403/429/5xx 触发 failover，冷却 60s；402 不 failover（透传）；行为与改前完全一致 |
| 新配置 | 有 `cooldown_rules` 含 402→T1-0100 | 402 触发 failover + 冷却到次日01:00；429 触发 failover + 冷却60s |
| 混合配置 | supply 级 `cooldown_rules` 覆盖顶层 | 该 supply 的 402 走 supply 级策略组（如 T0-2359）；该 supply 的 429 回退顶层（60s） |
| 长冷却落盘 | 触发 402→T1-0100 后重启进程 | 冷却状态从盘恢复，重启后 supply 仍冷却中 |
| 手动 reload | `POST /model_proxy/reload` | 内存清空 + 盘文件删除 |
| mtime reload | 改配置文件触发自动 reload | 不动冷却状态（内存+盘都不动） |

## 风险与权衡

1. **`T1-0100` 跨天边界语义**：推荐方案A（固定日期偏移，T1=明天），凌晨 00:30 触发时冷却到次日01:00（约24.5h），虽然额度可能已在00:00刷新但多等无害。替代方案B/C（就近未来时间点）语义更省时但与"T1=次日"字面冲突。**需用户确认。**

2. **多条策略组命中同一 code**：取首条（配置顺序优先），不报错。简单但需用户知晓——配置顺序即优先级。**需用户确认。**

3. **supply 级与顶层合并规则**：supply 级覆盖顶层（supply 级有的 code 以 supply 级为准，未覆盖的回退顶层）。**需用户确认。**

4. **URLError 分支**：网络错误无 HTTP 状态码，走 `default_cooldown_seconds`（与现有行为一致），不走策略组。**需用户确认。**

5. **落盘不持久化 reason**：重启后 status 展示丢 reason 标注，但 cooldown 状态不丢。若需持久化 reason 需改落盘格式为 `{sid: {until, reason}}`，增加复杂度。**需用户确认是否接受。**

6. **snapshot 返回结构 breaking change**：`{sid: remaining_seconds}` → `{sid: {remaining, until, reason}}`。当前唯一消费点是同文件 `_handle_status`，同步改即可。若有外部脚本消费 status 需通知。

7. **配置校验**：`_validate_config` 需增加策略组格式校验（errorcode 是 int 数组、cooldown spec 格式合法）。校验失败时现有逻辑是 log warning + 保留加载（行509-510），不阻断。建议策略组校验也走容错：格式非法的策略组跳过 + log warning，不阻断整个配置加载。

8. **并发写盘**：`_persist` 持锁拷贝 dict 后锁外写，复用 `_atomic_write_json`（mkstemp+os.replace），多线程并发安全。

## 验证方式

1. **老配置回归**：不改配置文件（无 `cooldown_rules`），用坏 appkey 触发 401，验证 cooldown 60s + failover 行为不变。
2. **新配置 402 冷却**：配置 `cooldown_rules: [{errorcode: [402], cooldown: "T1-0100"}]`，mock 上游返回 402，验证：
   - failover 触发（换下一个 supply）
   - cooldown 直到次日01:00 CST（用 `date -r <until>` 验证 epoch 换算）
   - `.claude_model_proxy_cooldown.json` 文件出现且内容正确
3. **重启恢复**：触发 402 长冷却后 kill 进程重启，`GET /model_proxy/status` 验证该 supply 仍 cooling，remaining 秒数合理。
4. **手动 reload 清盘**：`POST /model_proxy/reload`，验证盘文件被删 + status 中 cooldown 清空。
5. **mtime reload 不动盘**：`touch config/model_proxy_config.json`（或微改后保存），发一个请求触发 maybe_reload，验证 cooldown 状态不变（内存+盘都不动）。
6. **503 摘要**：所有 supply 都 402 冷却后发请求，验证 503 响应体含 attempt_errors 摘要（如 `kimi-k3-sankuai-3339=http_402`）和 cooling 摘要。
7. **混合配置**：supply 级设 402→T0-2359，顶层设 402→T1-0100，验证该 supply 的 402 走 T0-2359 而非 T1-0100。
8. **parse_cooldown_spec 单测**：`60`→("seconds",60.0)、`"60s"`→("seconds",60.0)、`"T1-0100"`→("until",epoch)、非法 spec 抛 ValueError。

## 关联

- [[2026-08-07-reasoning-thinking-truncation-and-protocol-consistency]] — failover 循环结构与 budget_retry 互斥关系
- [[2026-07-23-usage-totals-ledger]] — `_atomic_write_json` 原子写盘参照
- [[2026-08-04-in-band-route-command-design]] — ConfigStore 热重载机制
