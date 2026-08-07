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

## 任务2:扩展 `compact_config_json` 覆盖 `routes.tiers.<tier>` 数组

### 现状(已确认)

用户手编的 `routes.tiers.<tier>` 数组是单行紧凑:
```json
"tiers": {
  "opus": ["kimi-k3-sankuai-3339","kimi-k3-sankuai-8101","kimi-k3-sankuai-9907"],
  ...
}
```

但 `compact_config_json`(`_config_ops.py`)只压 `effort_enum` 数组和 `tiers_source_capability` 下的 tier 对象,**不压 `routes.tiers` 数组**。代码生成(如 `strategy add`)会输出多行:
```json
"tiers": {
  "opus": [
    "kimi-k3-sankuai-3339",
    "kimi-k3-sankuai-8101",
    "kimi-k3-sankuai-9907"
  ],
  ...
}
```

**导致**:手编格式与代码生成格式不一致,下次代码写 config 会把单行展开成多行(git diff 噪音 + 用户手改被覆盖)。

之前的 compact 方案(`2026-08-07-config-compact-format.md`)§1.2 明确说"routes.tiers 单元素数组 json.dumps 默认就是单行,无需处理"——**当时现网只有单元素,没考虑多元素**。现在 nation1/nation2 三档都是 3 元素,需要纳入。

### 修法

`compact_config_json` 加第三个正则,压 `routes` 下 `tiers.<tier>` 的数组(含多元素)。目标输出:三个 tier 的数组都单行。

**正则设计**:
```python
# 正则3:把 routes.tiers 下的 "tier": [ 多行 ] 压成单行
# tier 名限定 opus|sonnet|haiku(与现有 _TIER_OBJECT 一致)
# 注意:这个正则匹配的是"tier 键后跟数组",和 _TIER_OBJECT(匹配 tier 键后跟对象)不同
_ROUTES_TIERS_ARRAY = re.compile(
    r'("(?:opus|sonnet|haiku)":\s*)\[\s*\n([^\]]*?)\n\s*\]',
    re.DOTALL
)
```

**但这里有个重叠风险**:`_TIER_OBJECT` 匹配 `"opus": {\n "effort_enum": ...\n}`,`_ROUTES_TIERS_ARRAY` 匹配 `"opus": [\n ...\n]`。一个匹配对象、一个匹配数组,模式不冲突( `{` vs `[`),可以共存。

**顺序**:加在现有两个 sub 之后(先 effort_enum 数组、再 tier 对象、最后 routes.tiers 数组)。三个正则的匹配域不重叠:
- 正则1: `"effort_enum": [ ... ]` — 只匹配 effort_enum 键的数组
- 正则2: `"opus": { "effort_enum": [...] }` — 匹配 tiers_source_capability 下的单键对象
- 正则3: `"opus": [ ... ]` — 匹配 routes.tiers 下的数组

### 测试

`tests/test_config_compact_format.py` 新增:
1. `routes.tiers.<tier>` 多元素数组压单行(nation1 的 3 元素)
2. 单元素数组也单行(不崩,claude route 的单元素)
3. 数据无损:`json.loads(compact_config_json(cfg)) == cfg`(含 routes)
4. 与 effort_enum 混合:一个 config 同时含 routes.tiers 和 effort_enum,两者都正确压行

### 文档更新

`docs/designs/2026-08-07-config-compact-format.md`:
- §1.2:"routes.tiers 单元素无需处理" → "routes.tiers.<tier> 数组也纳入紧凑化(含多元素)"
- "紧凑化只针对两类" → "三类"(加 routes.tiers 数组)
- §3.1:加正则3 的代码骨架和顺序说明

---

## 实施范围

| 文件 | 改动 |
|---|---|
| `core/commands.py` | 任务1:修 `_maybe_reload_locked` |
| `tests/test_route_command.py` | 任务1:加 sidecar 删除清空测试 |
| `_config_ops.py` | 任务2:加第三个正则到 `compact_config_json` |
| `tests/test_config_compact_format.py` | 任务2:加 routes.tiers 测试 |
| `docs/designs/2026-08-07-config-compact-format.md` | 任务2:更新 §1.2/§3.1 |

**不动**:请求体序列化、读取侧、sidecar 写、`_reload_locked` 的非法 JSON 兜底逻辑。

## 风险

- 任务1:修法明确,守卫避免无意义重复清空,无正确性风险
- 任务2:三个正则匹配域不冲突(已分析),数据无损有测试强保证;未来新增 tier 名需更新正则(已在 docstring 标注)

## 请确认后派 implementer 执行
