---
type: design-decision
status: pending
target: "[[tools/model_proxy]]"
tags: [model_proxy, sidecar, bugfix, config-format]
---

# sidecar 文件删除 bug 修复 + compact 格式扩展 routes.tiers

> 两个独立小改动,合并一份方案。均为 [务实] 路径。

## 任务1:修 `_maybe_reload_locked` 文件删除 bug

### bug 现状(已确认)

`core/commands.py:223` `_maybe_reload_locked`:
```python
def _maybe_reload_locked(self) -> None:
    try:
        mtime = self._path.stat().st_mtime
    except FileNotFoundError:
        return          # ← bug:文件没了直接 return,内存保留旧数据
    if mtime <= self._mtime:
        return
    self._reload_locked()
```

设计要求(类 docstring):"文件缺失视为 `{}`"。`_reload_locked` 本身正确处理了文件缺失(置空),但 `_maybe_reload_locked` 的 mtime 守卫在文件不存在时短路,进不到 `_reload_locked`。

**实测后果**:删 sidecar 文件后,进程内存里仍保留旧 override 数据,status 端点误报 "+6个session覆盖"。重启进程才清掉(因为 `__init__` 调 `_reload_locked` 正确置空)。

日志还显示 15:38-15:39 有 "sidecar corrupt, keeping last known value" 警告——当时 sidecar 被写坏过(根因待查,可能并发写),`_reload_locked` 的"非法 JSON 保留旧值"兜底生效,内存保留了 6 条。后续删除文件也没清掉。

### 修法

`FileNotFoundError` 时清空内存,加守卫避免无意义重复清空:
```python
def _maybe_reload_locked(self) -> None:
    try:
        mtime = self._path.stat().st_mtime
    except FileNotFoundError:
        # 文件被删除:视为空(与 __init__/_reload_locked 的文件缺失语义一致),
        # 必须清空内存,否则已删除的 sidecar 数据残留在内存里(曾导致 status 误报 +6)
        if self._data or self._mtime:
            self._data = {}
            self._mtime = 0.0
        return
    if mtime <= self._mtime:
        return
    self._reload_locked()
```

`if self._data or self._mtime` 守卫:文件持续不存在时,每请求都调 `maybe_reload` 会反复进这里,没守卫会每请求重置一次 `self._data={}`(虽无害但无意义)。加了守卫只在"内存非空且文件消失"时清一次。

### 测试

`tests/test_route_command.py` 新增:
1. 用 `apply_command` 种一条记录(sidecar 文件产生)
2. 删 sidecar 文件
3. 调 `sc.maybe_reload()`(对外入口,不是 `_maybe_reload_locked`)
4. 断言 `sc.get_overrides_for(client_token) == {}`
5. 补:文件持续不存在时,连续调 `maybe_reload` 不会反复清空(可选,测守卫)

### 不修的

- **sidecar 被写坏的根因**(15:38 的 corrupt):`_reload_locked` 的"非法 JSON 保留旧值"是设计既定行为(sidecar 允许人工手改,手滑写坏不该丢数据)。本次只修"文件删除不清空"这个明确 bug,不改"非法 JSON 保留旧值"。

---

## 任务2:统一 config 紧凑格式（supplies 对象 + routes.tiers 数组 + tier 对象）

### 现状（已确认）

用户手编的 config 里，三类结构都用了单行紧凑格式：
- `supplies` 数组里每条 supply 对象（整个 `{"id":...,"reasoning_capability":{...}}` 单行）——**新增 15 条是单行，原有 12 条是多行，格式不统一**
- `routes.tiers.<tier>` 的 supply id 数组（多元素，如 `["kimi-k3-sankuai-3339","kimi-k3-sankuai-8101","kimi-k3-sankuai-9907"]` 单行）
- `tiers_source_capability` 下的 tier 对象（`"opus": {"effort_enum": [...]}` 单行）

但 `compact_config_json`（`_config_ops.py`）只压 `effort_enum` 数组和 tier 对象，**不压 supplies 对象、不压 routes.tiers 数组**。代码生成（如 `strategy add`、CLI）会把用户手编的紧凑格式展开成多行——格式不一致 + 手改被覆盖。

**用户拍板：方向 A，统一单行紧凑**。即：把 12 个旧 supply 也改成单行，同时扩展 `compact_config_json` 让代码生成也压 supply 对象 + routes.tiers 数组。

### 改动范围

`compact_config_json` 现在要压三类结构（在原有两类基础上扩展）：

1. **`supplies` 数组里的每条 supply 对象**（新增）：整个对象压单行，包括嵌套的 `reasoning_capability:{"effort_enum":[...]}`。
   目标：`{"id":"kimi-k3-sankuai-8101","url":"...","protocol":"anthropic","appkey":"...","target_model":"kimi-k3","reasoning_capability":{"effort_enum":["low","high","max"]}}` 单行。
2. **`routes.tiers.<tier>` 的 supply id 数组**（新增）：多元素也压单行（原只压单元素，因单元素 `json.dumps` 默认就是单行）。
3. **`effort_enum` 数组**（已有）：保持。
4. **`tiers_source_capability` 下的 tier 对象**（已有）：保持。

### 正则设计（四个正则，顺序敏感）

```python
# 正则1（已有）：压 effort_enum 数组
_EFFORT_ENUM_ARRAY = re.compile(
    r'("effort_enum":\s*)\[\s*\n([^\]]*?)\n\s*\]',
    re.DOTALL
)

# 正则2（已有）：压 tiers_source_capability 下的 tier 对象
_TIER_OBJECT = re.compile(
    r'("(?:opus|sonnet|haiku)":\s*)\{\s*\n\s*("effort_enum":\s*\[[^\]]*\])\s*\n\s*\}',
    re.DOTALL
)

# 正则3（新增）：压 routes.tiers 下的 supply id 数组
_ROUTES_TIERS_ARRAY = re.compile(
    r'("(?:opus|sonnet|haiku)":\s*)\[\s*\n([^\]]*?)\n\s*\]',
    re.DOTALL
)

# 正则4（新增）：压 supplies 数组里的每条 supply 对象（含嵌套 reasoning_capability）
# 注意：必须在正则1之后执行——先压 effort_enum 数组，才能匹配到整个 supply 对象单行
_SUPPLY_OBJECT = re.compile(
    r'\{\s*\n\s*"id":\s*"[^"]+",\s*\n\s*"url":\s*"[^"]+",\s*\n\s*"protocol":\s*"[^"]+",\s*\n\s*"appkey":\s*"[^"]+",\s*\n\s*"target_model":\s*"[^"]+",\s*\n\s*"reasoning_capability":\s*\{[^}]*\}\s*\n\s*\}',
    re.DOTALL
)
```

**顺序**：正则1 → 正则2 → 正则3 → 正则4。
- 正则1 先压 effort_enum 数组，这样正则4 匹配 supply 对象时其内 `effort_enum` 已是单行，能匹配到完整对象
- 正则2/3 互不冲突（一个匹配对象、一个匹配数组）
- 正则4 最后，等 effort_enum 已单行后再压整个 supply 对象

**正则4 的风险**：supply 对象含嵌套 `reasoning_capability`，正则要在 `reasoning_capability` 已压行后匹配完整对象。如果 supply 结构未来加字段（如 `priority`、`weight`），正则4 会失配、回退多行（不报错、不丢数据）。这是可接受的脆性——格式不一致只会导致该条多行展开，功能正常。

### 同时改 config 文件本身

把 12 个旧 supply 的多行格式改成单行紧凑（与新加的 15 条统一）。这是一次性手动编辑，用 Edit 工具改 `config/model_proxy_config.json`。

### 测试

`tests/test_config_compact_format.py` 新增/扩展：
1. supplies 数组每条对象压单行（含嵌套 reasoning_capability）
2. routes.tiers.<tier> 多元素数组压单行（nation1 的 3 元素）
3. routes.tiers.<tier> 单元素数组也单行（不崩，claude route 的单元素）
4. 数据无损：`json.loads(compact_config_json(cfg)) == cfg`（含 supplies + routes + strategies 全结构）
5. 混合场景：一个 config 同时含 supplies 多行/单行、routes.tiers 多元素/单元素、effort_enum，全部正确压行
6. 未来 supply 结构扩展（加 priority 字段）时正则4 失配回退多行，不报错不丢数据

### 文档更新

`docs/designs/2026-08-07-config-compact-format.md`：
- §1.2："routes.tiers 单元素无需处理" → "routes.tiers.<tier> 数组纳入紧凑化（含多元素）"
- "紧凑化只针对两类" → "三类"（加 routes.tiers 数组）
- §3.1：加正则3/正则4 的代码骨架和顺序说明
- 补一句：supplies 对象也纳入紧凑化（含嵌套 reasoning_capability），见正则4

---

## 实施范围

| 文件 | 改动 |
|---|---|
| `core/commands.py` | 任务1：修 `_maybe_reload_locked` |
| `tests/test_route_command.py` | 任务1：加 sidecar 删除清空测试 |
| `_config_ops.py` | 任务2：加正则3/正则4 到 `compact_config_json` |
| `tests/test_config_compact_format.py` | 任务2：加 supplies/routes.tiers 测试 |
| `config/model_proxy_config.json` | 任务2：12 个旧 supply 改成单行（手动编辑，不进 git） |
| `docs/designs/2026-08-07-config-compact-format.md` | 任务2：更新 §1.2/§3.1 |

**不动**：请求体序列化、读取侧、sidecar 写、`_reload_locked` 的非法 JSON 兜底逻辑。

## 风险

- 任务1：修法明确，守卫避免无意义重复清空，无正确性风险
- 任务2：
  - 四个正则匹配域互不冲突（已分析），数据无损有测试强保证
  - 正则4 对 supply 结构扩展有脆性（加字段即失配回退多行），但失配不报错不丢数据，可接受
  - 顺序敏感（正则1 必须先于正则4），docstring 和测试要覆盖顺序错误的情况

## 请确认后派 implementer 执行
