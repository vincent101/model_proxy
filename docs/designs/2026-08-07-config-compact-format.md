---
type: design-decision
status: confirmed
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, config, formatting]
---

# model_proxy 配置文件紧凑格式：effort_enum 与 tier 对象单行化

> [务实] 路径产出：用户已手动把 config 里 `effort_enum` 数组和 `tiers_source_capability` 下的 tier 对象改成单行紧凑格式，希望后续代码生成配置时也按此格式输出，避免"手改的紧凑格式被代码写配置覆盖回多行"的冲突。

## 1. 现状核实

### 1.1 用户改了什么（已核实原始文本）

**改动1：`effort_enum` 多元素数组 → 单行**
```json
// 改前（json.dumps(indent=2) 默认）
"effort_enum": [
  "low",
  "medium",
  "high",
  "xhigh",
  "max"
]
// 改后（用户手改）
"effort_enum": ["low","medium","high","xhigh","max"]
```

**改动2：`tiers_source_capability` 下的 tier 对象 → 单行**
```json
// 改前
"tiers_source_capability": {
  "opus": {
    "effort_enum": [
      "low","medium","high","xhigh","max"
    ]
  },
  ...
}
// 改后（tier 对象本身也压成单行）
"tiers_source_capability": {
  "opus": {"effort_enum": ["low","medium","high","xhigh","max"]},
  "sonnet": {"effort_enum": ["low","medium","high","xhigh","max"]},
  "haiku": {"effort_enum": ["low","medium","high","max"]}
}
```

### 1.2 用户**没改**什么（保持多行，不要纳入紧凑化）

已核实现网 config：以下多元素结构用户保持 `indent=2` 多行展开，方案**不动这些**：
- `supplies`（12 元素，每条是大对象）— 多行
- `routes`（5 元素，每条是大对象）— 多行
- `strategies`（2 元素）— 多行
- `route_pool`（现网单元素，但即便多元素也应多行，每条是 `{route_id, weight}` 小对象）— 多行
- `routes.tiers.<tier>`（单元素数组 `["claude-opus-sankuai-0956"]`，`json.dumps` 默认就是单行，无需处理）

**结论：紧凑化只针对两类**：
1. 任何 `effort_enum` 键下的字符串数组（不管在哪层、几个元素，都压单行）
2. `tiers_source_capability` 下的每个 tier 对象（`{"opus": {...}}` 这种"单键 dict 值是含 effort_enum 的 dict"，整体压单行）

### 1.3 代码怎么写配置（已核实）

5 处写出点，全部 `json.dump/dumps(indent=2, ensure_ascii=False)`：

| 文件:行 | 用途 | 是否需紧凑化 |
|---|---|---|
| `_config_ops.py:47` `atomic_write` | 主 config 原子写（strategy add/edit、switch） | **是** |
| `_install_ops.py:335/370/514` | install/配置同步脚本写 config | **是** |
| `core/server.py:108` `_atomic_write_json` | sidecar 写（session_overrides.json，无 effort_enum） | 否（但统一序列化器顺带覆盖无妨） |
| `core/server.py:780/1187/1193/1210` | **请求体**序列化（转发上游的 body） | **绝对不动**（格式由上游 API 决定） |

读取侧用 `json.load`，对格式无感知，**零改动**。

### 1.4 标准库限制（已实测）

`json.dumps(indent=2)` 是全局缩进：要么全多行、要么 `indent=None` 全紧凑。**无法"某数组单行、其余多行"**。Python 3.14 标准库亦无内置支持局部紧凑的参数。

## 2. 技术路径评估

| 路径 | 思路 | 复杂度 | 健壮性 | 覆盖面 | 采信 |
|---|---|---|---|---|---|
| **A 自定义 Encoder** | 继承 `json.JSONEncoder`，重写 dict/list 的输出逻辑 | 高（要重写缩进/换行控制流） | 中（嵌套层级一变可能漏） | 全局（所有 dump 点） | ✗ 过度工程 |
| **B 后处理文本** | `json.dumps(indent=2)` 生成多行 → 正则把 `effort_enum` 数组 + tier 对象压单行 | 低（两个正则替换） | 中（正则有脆性，需测试覆盖） | 全局（任何经此函数的文本） | **✓ 推荐** |
| **C 分片拼接** | 手动构造 config 文本段，绕开 json.dump | 高 | 低（结构变化即断） | 仅主 config | ✗ 最差 |
| **D 不改** | 接受代码写配置时覆盖回多行 | 0 | — | — | ✗ 违背用户意图 |

### 2.1 推荐：路径 B（后处理文本）

**理由**：
- 改动最小：写一个 `_compact_config_json(obj) -> str` 函数，5 处写出点把 `json.dumps(cfg, indent=2, ensure_ascii=False)` 换成 `_compact_config_json(cfg)` 即可
- 健壮性可证：两个正则都有明确的"匹配 `effort_enum` 数组"和"匹配 tier 对象"的模式，且**解析回 dict 验证数据无损**是强保证（见 §3 测试）
- 与用户手改一致：代码生成的格式 = 用户手改的格式，冲突消失

**路径 B 的脆性风险与对冲**：
- 正则可能误伤"note 字段里恰好包含 effort_enum 字样"的文本 → 实测不会（正则锚定 `"effort_enum":\s*\[`，note 里的文字不会带冒号+方括号语法）
- 配置结构扩展（未来 effort_enum 改名或换位置）→ 正则会静默不匹配（不报错、回退到多行），不会出错；测试用例覆盖"存在 effort_enum 时紧凑、不存在时正常多行"两种情况

## 3. 推荐方案设计

### 3.1 新增函数（放 `_config_ops.py`，供所有写出点复用）

```python
import json, re

# 正则1：把 "effort_enum": [ 多行 ] 压成 "effort_enum": ["a","b","c"]
# 匹配 effort_enum 键后、跨行的数组，捕获数组内容
_EFFORT_ENUM_ARRAY = re.compile(
    r'("effort_enum":\s*)\[\s*\n([^\]]*?)\n\s*\]',
    re.DOTALL
)

# 正则2：把 tiers_source_capability 下的 "tier": {\n ... \n} 压成 "tier": {...}
# 仅匹配紧邻 effort_enum 的单键 dict（tier 对象只有 effort_enum 一个键）
_TIER_OBJECT = re.compile(
    r'("(?:opus|sonnet|haiku)":\s*)\{\s*\n\s*("effort_enum":\s*\[[^\]]*\])\s*\n\s*\}',
    re.DOTALL
)

def compact_config_json(obj) -> str:
    """生成 config 文本：indent=2 多行，但 effort_enum 数组与
    tiers_source_capability 下的 tier 对象压成单行（与用户手改格式一致）。

    读取侧用 json.load 无感知，本函数只影响文本外观。
    请求体序列化（转发上游）不得用本函数。
    """
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    # 先压 effort_enum 数组
    def _compact_array(m):
        vals = [s.strip().strip('"') for s in m.group(2).split(',') if s.strip()]
        return m.group(1) + '[' + ','.join('"'+v+'"' for v in vals) + ']'
    text = _EFFORT_ENUM_ARRAY.sub(_compact_array, text)
    # 再压 tier 对象（此时其内 effort_enum 已是单行）
    text = _TIER_OBJECT.sub(lambda m: m.group(1) + '{' + m.group(2) + '}', text)
    return text
```

### 3.2 改动点（5 处，全部替换 json.dumps 调用）

| 文件:行 | 改动 |
|---|---|
| `_config_ops.py:47` | `json.dump(cfg, f, indent=2, ensure_ascii=False)` → `f.write(compact_config_json(cfg) + "\n")` |
| `_install_ops.py:335` | `json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"` → `compact_config_json(cfg) + "\n"` |
| `_install_ops.py:370` | 同上 |
| `_install_ops.py:514` | 同上 |
| `core/server.py:108` `_atomic_write_json` | sidecar 写。sidecar 无 effort_enum/tiers_source_capability，正则不匹配、回退多行，**功能正确但无收益**。为保持一致性可改（统一走 `compact_config_json`），也可不改（sidecar 结构简单、多行可读性好）。**建议不改**，避免无谓改动引入风险。 |

**`server.py:780/1187/1193/1210`（请求体序列化）绝对不动**——那是发给上游 API 的 body，紧凑/多行对上游无影响，但不应为本需求改动转发路径。

### 3.3 实现要点

1. **两个正则的顺序**：先压 `effort_enum` 数组（正则1），再压 tier 对象（正则2）。因为正则2匹配的 tier 对象内部含 effort_enum，必须等 effort_enum 先变成单行后，正则2才能匹配到"`"effort_enum": [...]` 紧邻 `}`"的完整模式。
2. **正则2的 tier 名限定**：`opus|sonnet|haiku`，避免误匹配其他单键 dict。如果未来新增 tier 名，正则会不匹配该 tier（回退多行，不报错），需同步更新正则——这个约束写进函数 docstring。
3. **解析回 dict 验证**：`json.loads(compact_config_json(obj)) == obj` 必须成立，这是数据无损的强保证。

## 4. 风险与测试

### 4.1 风险

| 风险 | 评估 | 对冲 |
|---|---|---|
| 正则误伤其他字段 | 低（已实测 note 字段含 "effort_enum" 文字不被匹配，因正则锚定 `":\s*\[`） | 测试覆盖"字段值含 effort_enum 字样" |
| 配置结构变化导致正则失配 | 中（未来 effort_enum 改名/移位） | 失配时回退多行，不报错不丢数据；docstring 标注"新增 tier 名需更新正则" |
| 请求体被误用本函数 | 低（调用点明确） | 函数名 `compact_config_json` + docstring 标注"请求体不得用" |
| sidecar 写出不一致 | 无（sidecar 无 effort_enum，正则不匹配，回退多行 = 原行为） | 不改 sidecar 写出点 |

### 4.2 测试方案

新增 `tests/test_config_compact_format.py`：

1. **格式断言**：生成含 effort_enum 的 config 文本，断言：
   - `"effort_enum": ["low","medium",...]` 单行出现（无跨行数组）
   - `"opus": {"effort_enum": [...]}` 单行出现（tier 对象不跨行）
   - 其他多元素数组（`supplies`/`routes`/`strategies`）仍多行
2. **数据无损**：`json.loads(compact_config_json(cfg)) == cfg`（深相等）
3. **无 effort_enum 时正常多行**：构造不含 effort_enum 的 config，断言输出 == `json.dumps(indent=2)`
4. **note 字段含 effort_enum 文字不误伤**：构造 `note` 值含 "effort_enum" 字样的 config，断言 note 值不变
5. **多 tier 名覆盖**：opus/sonnet/haiku 三个都压成单行
6. **空数组边界**：`effort_enum: []` 不崩（生成 `[]`）

## 5. 实施建议

- **派 implementer**：改动明确（1 个新函数 + 4 处调用替换 + 1 个测试文件），无正确性耦合（解析侧不动、请求体不动），适合 implementer 落地后 reviewer 轻量复核。
- **不需要改读取侧、不需要改请求体序列化、不需要改 sidecar 写出**。
- 实施后，用户手改的紧凑格式与代码生成的格式一致，`strategy add/edit` 等操作不再覆盖回多行。

## 6. 一个诚实的反思：这个改动的价值上限

紧凑格式纯属**文本美观**，不带来任何功能或性能收益。它解决的是"用户手改的格式和代码生成的格式不一致导致的 git diff 噪音 + 心理不适"。这个痛点真实但不重大——如果用户从不手改 config（全靠 CLI），这个改动就没价值。鉴于用户明确表达了偏好并已手改，做是值得的，但不应过度投入（路径 B 的简单正则已经是最省力方案，不要再追求"通用 JSON 紧凑化框架"）。
