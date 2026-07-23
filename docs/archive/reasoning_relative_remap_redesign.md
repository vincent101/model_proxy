# core/reasoning 理想路径重构方案：相对映射 + OFF/MAX 统一 + encode 分层

> 状态：已实施并验证通过（2026-07；经 reviewer 复核 + 端到端验证）。本文为设计决策记录，当前实现与用法以 ../../README.md 为准。
> 决策人：Vincent。设计：architect（2026-07-20）。

## 0. 背景

现状 reasoning 链路 `decode → align → select_variant → encode`：

- 只有 target 侧能力建模（`ReasoningCapability.from_config(supply)`），没有 source 侧（表面模型 claude-opus/sonnet/haiku 各自能选哪些档）的概念。
- `align()` 是"绝对钳位"：客户端发的档名被钉死在全局 `CanonicalEffort` 绝对值上，只按 target 的 `cap.enum` 单侧裁剪。
- OFF（`explicit_off` 独立字段）和 MAX（`_canonical_to_chat_domain_name` 里的 if 分支）在多处被特殊处理，不是统一走查表/序数路径。
- `encode()` 混杂"canonical→协议档名转换"和"协议内 wire 语法选择（如 Anthropic 的 enabled/adaptive）"两件事。
- 真实 bug：当 `aligned.level=None` 时 `encode()` 简单 `return {}`，导致"客户端发了思考字段但该 supply 完全不支持思考"时，客户端原始 `thinking`/`output_config` 字段被原样透传到上游（未被清理）。已用 `claude-haiku-sankuai-0956` 配置 `effort_enum:[]` 真实复现。

用户拍板三个决策：① 补 source 侧建模，用**相对排名映射**（不是绝对锚定）；② OFF 和 MAX 都要作为思考程度序列的正式成员统一处理；③ encode 拆成"协议无关抽象编码"+"协议内 wire 语法适配"两层。

**本次会话对决策2的最终澄清（比初版方案更明确）**：OFF **允许**保留为特殊分支处理，但必须**统一收在一处**（不能像现状一样散落在 `codecs.py` 的多个 `encode()` 里、`ladder.py` 的 `ReasoningIntent.explicit_off` 字段里）。MAX **不允许**任何特殊分支，必须完全走统一查表/序数路径。这是本文档与 architect 初版分析的唯一差异点，以本文档为准。

---

## 1. 新数据流

```
decode(source_codec, body)
   ↓ RawIntent
resolve_source_capability(request_model)      → SourceCapability
resolve_target_capability(supply)              → TargetCapability
   ↓
remap(RawIntent, SourceCapability, TargetCapability)   → TargetEffort
   ↓
abstract_encode(TargetEffort)   → AbstractReasoning   # OFF→DISABLED 的唯一判断点
   ↓
syntax_adapt(target_codec, AbstractReasoning, variant) → wire dict
   ↓
apply_fields(body, wire dict)
```

对比现状：`align`→`remap`（签名从 `(intent,cap)` 变为 `(intent,src_cap,tgt_cap)`，语义从"单侧绝对钳位"变"双侧相对映射"）；新增 `resolve_source_capability`；`encode`拆成`abstract_encode`（协议无关）+ `syntax_adapt`（协议内 wire，含 enabled/adaptive 变体选择、`interpret_rejection`、`pref_store` 学习缓存）。

---

## 2. 数据结构（字段级）

```python
# ladder.py —— CanonicalEffort 序列定义不变，7 档全序不改，只改处理它的代码路径
class CanonicalEffort(IntEnum):
    OFF = 0; MINIMAL = 1; LOW = 2; MEDIUM = 3; HIGH = 4; XHIGH = 5; MAX = 6

@dataclass(frozen=True)
class RawIntent:                      # 原 ReasoningIntent，改名
    level: CanonicalEffort | None     # 客户端表达的 canonical 档；None=未表达或无法识别
    source_budget: int | None         # Anthropic enabled 原始 budget_tokens，供无损回填
    present: bool                     # 客户端是否表达了 reasoning 意图
    # explicit_off 字段删除：thinking.type=disabled 直接 decode 成 level=OFF, present=True

@dataclass(frozen=True)
class ModelReasoningCapability:       # 原 ReasoningCapability，source/target 共用同一类型
    enum: tuple                       # 有序去重 CanonicalEffort 元组（低→高），含/不含 OFF
    off_alias: CanonicalEffort | None # "关闭"落点；缺省 = enum 含 OFF 则 OFF 否则 None
                                       # 新增校验（决策3拍板）：off_alias 不得高于 enum 中最低思考档，
                                       # 否则 from_config 视为无效配置回退 None（保证单调性，见§4）

@dataclass(frozen=True)
class TargetEffort:                   # 原 AlignedEffort，改名
    level: CanonicalEffort | None
    source_budget: int | None
    stripped: bool                    # present 但 target 完全不支持思考 → True，需主动清理原始字段

class AbstractKind(Enum):
    THINKING = "thinking"
    DISABLED = "disabled"             # OFF 的唯一落点，统一收在 abstract_encode 这一处
    STRIP = "strip"
    ABSENT = "absent"

@dataclass(frozen=True)
class AbstractReasoning:              # 协议无关中间物，新增
    kind: AbstractKind
    level: CanonicalEffort | None     # 仅 kind=THINKING 时有意义
    source_budget: int | None
```

---

## 3. 相对映射算法

### 3.1 思考子序列与 OFF 的分段处理（决策2的落地方式）

`think_seq(cap) = tuple(e for e in cap.enum if e != OFF)`——排名计算只在**思考子序列**上进行，OFF 不参与比例计算。

**这是本次唯一被允许的特殊分支，且必须统一收在一处**：只出现在 `remap()` 内部作为一条 clause，和 `abstract_encode()` 内部把 `level==OFF` 映射成 `DISABLED` 这一处。除此之外，`codecs.py` 的 `syntax_adapt()` 里不再有任何 `if level == OFF` 判断——它只根据 `AbstractKind` 分派到对应的 wire 结构模板，不需要重复判断 level 本身。

MAX 完全统一：作为思考子序列的最高值，走跟 LOW/MEDIUM/HIGH 完全一样的查表/排名路径，`codecs.py` 里不允许出现 `if level == MAX` 这种判断（现状 `_canonical_to_chat_domain_name` 的 MAX 分支要改成统一处理，见§6命名/改动清单）。

**Chat/Responses 协议域没有 MAX 字符串这一真实限制怎么处理**：这不是"MAX 需要特殊处理"，是"该协议的档名表本身比 canonical 全序窄"——这属于协议 wire 层的词表限制，应该在 `syntax_adapt` 阶段的协议域档名表里正常体现为"该域的 enum 本就不含 MAX"，而不是在转换函数里写 if 分支。具体做法：Chat/Responses 的 `ModelReasoningCapability`（无论是 source 还是 target）如果配置了含 MAX 的 `effort_enum`，`remap()` 阶段就已经把它钳位/映射到该域实际支持的最高档了（因为 `tgt_think` 本身应该反映"该协议域能表达什么"），`syntax_adapt` 拿到的 `AbstractReasoning.level` 应该已经是协议域内合法的值，不需要在 `syntax_adapt` 里再做一次"MAX 降级"。**换句话说：现状"encode 内部做 MAX/MINIMAL 降级"这个职责，本次重构后要上收到"该协议 target 的 `effort_enum` 该怎么配置"这个配置层面，而不是写在转换代码里。**

### 3.2 rank 映射算法

```
remap_rank(i, m, n):
    # i: source 思考子序列里的 rank（0..m-1）；m: source 长度；n: target 长度
    if n == 1: return 0                            # target 只有1档，全部塌缩
    if m == 1: return (n - 1) // 2                 # source 单思考档 → target 中位档（用户拍板：选"中位"）
    return floor( i / (m - 1) * (n - 1) + 0.5 )    # 四舍五入，.5 向上
```

**m=1 取整语义已按用户拍板确定为"中位"**：`(n-1)//2`（整数除法向下取整）。例如 target 有 2 档（n=2）时中位是 `(2-1)//2=0`，即最低档；target 有 3 档（n=3）时中位是 `(3-1)//2=1`，即中间档。

### 3.3 remap 完整流程

```python
def remap(intent: RawIntent, src_cap: ModelReasoningCapability, tgt_cap: ModelReasoningCapability) -> TargetEffort:
    if not intent.present:
        return TargetEffort(level=None, source_budget=None, stripped=False)          # ABSENT
    if not tgt_cap.enum:
        return TargetEffort(level=None, source_budget=intent.source_budget, stripped=True)   # STRIP
    if intent.level is None:
        return TargetEffort(level=None, source_budget=intent.source_budget, stripped=True)   # 发了但无法识别档 → 仍需清理

    tgt_think = think_seq(tgt_cap)   # 提前计算：OFF 吸收态 clause 也需要判断 tgt_think 是否为空

    # —— OFF 吸收态 clause（决策2：允许特殊分支，但统一收在这一处）——
    if intent.level == CanonicalEffort.OFF:
        if not tgt_think:            # target 无真实思考档（[] 或 ["off"]）→ 关闭意图统一 STRIP，不看 off_alias
            return TargetEffort(level=None, source_budget=intent.source_budget, stripped=True)
        return TargetEffort(level=tgt_cap.off_alias, source_budget=intent.source_budget,
                             stripped=(tgt_cap.off_alias is None))

    # —— 思考档相对映射 ——
    src_think = think_seq(src_cap)
    if not tgt_think:                              # target 无真实思考档 → STRIP（不消费 off_alias）
        return TargetEffort(level=None, source_budget=intent.source_budget, stripped=True)
    if not src_think:                               # source 未建模思考子序列 → 回退绝对钳位（兼容兜底）
        clamped = clamp_absolute(intent.level, tgt_think)
        return TargetEffort(level=clamped, source_budget=intent.source_budget, stripped=False)

    i = rank_of(intent.level, src_think)             # 若 intent.level 不在 src_think 中，先绝对钳到最近档再取 rank
    j = remap_rank(i, len(src_think), len(tgt_think))
    return TargetEffort(level=tgt_think[j], source_budget=intent.source_budget, stripped=False)
```

**决策B（本文档定稿后的追加拍板）**：`[]` 与 `["off"]` 两种 `effort_enum` 配置强制等价——
只要 `think_seq(tgt_cap)` 为空（即"去掉 OFF 后没有真实思考档"），任何思考意图或关闭意图都
统一走 STRIP，`off_alias` 不被消费，即便配置了 `off_alias` 也不生效。这是对上面 §3.3 初版
（`tgt_think` 为空时思考意图 STRIP、但 OFF 意图仍走 `off_alias`）的进一步统一，消除了"同样
'没有真实思考能力'的两种配置写法（`[]` vs `["off"]`）在关闭意图上表现不一致"的不一致点。

### 3.4 举例验证

source haiku think_seq=(LOW,MEDIUM,HIGH) m=3；target think_seq=(HIGH,MAX) n=2：
- LOW: i=0 → j=floor(0/2·1+0.5)=0 → **HIGH**
- MEDIUM: i=1 → j=floor(1/2·1+0.5)=floor(1.0)=1 → **MAX**
- HIGH: i=2 → j=floor(2/2·1+0.5)=1 → **MAX**

### 3.5 单调性证明

**命题**：固定 src_cap、tgt_cap，对任意 `intent1.level ≤ intent2.level`（皆为思考档，present=True），有 `remap(intent1).level ≤ remap(intent2).level`。

**证明**：
1. `rank_of` 对 canonical 值单调不减（绝对钳到 src_think 后取下标，下标随 canonical 值单调不减）。故 `level1≤level2 ⇒ i1≤i2`。
2. `remap_rank(i,m,n)` 在固定 m,n 下对 i 单调不减：n=1/m=1 分支为常函数，平凡满足；主分支是 i 的严格增线性函数复合 `floor`，单调不减。∴ `i1≤i2 ⇒ j1≤j2`。
3. `tgt_think` 按 canonical 升序，`j1≤j2 ⇒ tgt_think[j1] ≤ tgt_think[j2]`。
∴ 命题成立。∎

**OFF 参与的单调性约束（用户已拍板接受）**：`remap(OFF)=off_alias` 必须 ≤ 任意 `tgt_think` 档，否则"关闭思考"会被映射得比"低强度思考"更强，破坏单调性。**约束**：`ModelReasoningCapability.from_config` 必须校验 `off_alias` 不高于 `enum` 中最低思考档，违反则该 off_alias 配置视为无效、回退 `None`。这是本方案对配置施加的新不变式，需要在 `from_config` 里实现校验逻辑，并在 CLI/文档里说明。

**跨 supply（failover）不在单调承诺内**：同一 intent 换 target 得不同结果，属"目标能力差异"，非"意图强弱"，与现状承诺一致。

---

## 4. abstract_encode + syntax_adapt

### 4.1 abstract_encode（协议无关，OFF→DISABLED 的唯一判断点）

```python
def abstract_encode(te: TargetEffort) -> AbstractReasoning:
    if te.stripped:
        return AbstractReasoning(kind=AbstractKind.STRIP, level=None, source_budget=None)
    if te.level is None:
        return AbstractReasoning(kind=AbstractKind.ABSENT, level=None, source_budget=None)
    if te.level == CanonicalEffort.OFF:
        return AbstractReasoning(kind=AbstractKind.DISABLED, level=None, source_budget=None)
    return AbstractReasoning(kind=AbstractKind.THINKING, level=te.level, source_budget=te.source_budget)
```

这是**全局唯一** `if level==OFF` 出现处（连同 §3.3 remap 里的 OFF clause，全代码库总共只有这两处，且都在"OFF 统一收在一处"这个约束下——remap 负责"OFF 该落到 target 的哪个值"，abstract_encode 负责"OFF 这个值该转成什么抽象类型"，两处职责不同、不重复判断同一件事）。

### 4.2 syntax_adapt（协议内 wire 语法，含变体选择）

Anthropic：
```python
def syntax_adapt(abstract: AbstractReasoning, variant: str) -> dict:
    if abstract.kind == AbstractKind.ABSENT:
        return {}
    if abstract.kind == AbstractKind.STRIP:
        return {"thinking": None, "output_config": None}
    if abstract.kind == AbstractKind.DISABLED:
        return {"thinking": {"type": "disabled"}, "output_config": None}
    # THINKING
    if variant == ANTHROPIC_ENABLED:
        budget = abstract.source_budget or canonical_to_budget(abstract.level)
        return {"thinking": {"type": "enabled", "budget_tokens": budget}, "output_config": None}
    # ANTHROPIC_ADAPTIVE
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": _anthropic_name(abstract.level)}}
```

Chat / Responses（单变体）：ABSENT→`{}`；STRIP→`{"reasoning_effort":None}` / `{"reasoning":None}`；DISABLED→`{"reasoning_effort":"none"}` / `{"reasoning":{"effort":"none"}}`；THINKING→查表取档名字符串（MAX 走正常查表，不特殊判断，见§3.1）。

### 4.3 变体选择 / interpret_rejection / pref_store 归属

- `select_variant(pref)`：留在 codec，归属 syntax_adapt 阶段，作为其入参。
- `interpret_rejection(error_body, used_variant)`：留在 codec，归属 syntax_adapt 阶段的反馈回路。
- `pref_store`（`SyntaxPreferenceStore`）：留在 `server.py`，运行时状态不变。
- **400 重试优化**：重试时只需重跑 `variant=select_variant(...)` → `syntax_adapt(...)`，`remap`/`abstract_encode` 结果可复用（因为重试不改变 intent/cap，只改变 variant）。这比现状"整个 encode 重跑"更干净，`server.py` 的重试循环需要按此调整——把 `remap`+`abstract_encode` 挪到重试循环外算一次，`syntax_adapt` 留在循环内按 variant 重算。

---

## 5. 两个真实缺陷的消除

**缺陷1（原始字段透传）**：由 `RawIntent.present` + `TargetEffort.stripped` + `AbstractKind` 四态消除。ABSENT（present=False）→`{}`，无残留。STRIP（present=True 但 target 无思考能力）→ syntax_adapt 产 `{"thinking":None,...}`，`apply_fields` 主动删除客户端原始字段。**该 bug 不会因为改了架构自动消失，`stripped` 是专门为此新增的显式信号，必须被正确实现和测试覆盖。**

**缺陷2（命名混乱）**：见下方命名对照表。

---

## 6. 命名对照表

| 旧名 | 新名 | 理由 |
|---|---|---|
| `ReasoningIntent` | `RawIntent` | 强调"未经跨模型换算的原始意图" |
| `ReasoningIntent.explicit_off` | 删除 | 关闭=`level=OFF` 一个值即可表达 |
| `ReasoningCapability` | `ModelReasoningCapability` | source/target 共用同一类型 |
| `align()` | `remap()` | 签名从 `(intent,cap)` 变 `(intent,src_cap,tgt_cap)`，语义从单侧钳位变跨模型相对映射 |
| `AlignedEffort` | `TargetEffort` | 产出是"落到 target 上的档"，新增 stripped 位 |
| `_clamp_to_enum` | `clamp_absolute` | 明确是"绝对序数钳位"，区别于新的相对 `remap`；仅作 source 未建模/`rank_of` 兜底用 |
| `ReasoningCodec.encode()` | 拆为模块级 `abstract_encode()`（协议无关）+ `codec.syntax_adapt()`（协议内） | 职责分离 |
| `_canonical_to_chat_domain_name` | `_canonical_to_openai_effort_name` | "chat_domain"实际同时服务chat+responses两协议，命名不准；且改造后不再含 MAX/MINIMAL 特殊 if 分支，只是普通查表 |
| server 局部变量 `aligned_effort` | `target_effort` | 跟随类型改名 |
| server 局部变量 `reasoning_fields` | `reasoning_wire`（可选，implementer可自行判断是否改） | 强调是 wire 层产物 |

---

## 7. config schema 改动

### 7.1 source 侧新增（挂在 strategy 下的 `tiers_source_capability`）

**归属修正（决策A，落地后取代本节初版设计）**：source 侧能力声明不放顶层独立表，而是挂在
每条 `strategy` 记录下的 `tiers_source_capability` 字段，因为真正代表"哪个客户端接入"的
身份标识是 `client_token`（一个 token 对应一条 strategy），而不是表面模型名——表面模型名
（`claude-opus`/`claude-sonnet`/`claude-haiku`）会被多个 SDK 共享（例如 codex-cli 也固定发
`model="claude-sonnet"`），不能代表客户端身份。

```jsonc
{
  "client_token": "cc",
  "route_id": "claude",
  "tiers_source_capability": {
    "opus":   {"effort_enum": ["low","medium","high","xhigh","max"]},
    "sonnet": {"effort_enum": ["low","medium","high","max"]},
    "haiku":  {"effort_enum": ["low","medium","high","max"]}
  },
  "note": "默认 Claude 家族（Claude Code SDK）"
}
```

- 挂在 strategy 下，key=tier 名（opus/sonnet/haiku，与 route.tiers 的 tier 命名一致），结构与 target 的 `reasoning_capability` 同构，由同一个 `ModelReasoningCapability.from_config` 解析。
- 新增 `resolve_strategy(strategies, client_token) -> dict | None`：client_token → 匹配的 strategy 记录本身。
- `resolve_source_capability(strategy, tier) -> ModelReasoningCapability`：从 `strategy.tiers_source_capability[tier]` 取值；strategy 为 None、无该字段、或该 tier 未声明 → 回退默认全档序列。
- `_MODEL_TIER_MAP`/`resolve_tier`/`resolve_route`/`select_supply` **不改**，source 建模与 tier 路由正交并存。`resolve_route` 内部改为复用 `resolve_strategy`，签名和返回值向后兼容。
- CLI 支持：`strategy add`（新增时逐 tier 交互式询问）/ `strategy edit <token>`（确认后可重新录入/保留/移除该字段），详见 `_config_ops.py::prompt_source_capability`。

### 7.2 target 侧 `reasoning_capability` —— 结构不变，语义重释

结构不变（仍 `effort_enum` + 可选 `off_alias`），rank 由 `think_seq` 实时算下标，不新增排名字段。但语义从"绝对锚定的允许档位表"变成"该 target 的思考子序列，决定 rank 空间大小和每个 rank 落到哪个 canonical 值"。

### 7.3 现有生产 config 兼容性（14+ supply）— 需要逐个复核，不是纯机械迁移

现有窄档数据是按"绝对锚定"思路填的，换成相对映射后语义会变，需要逐 supply 复核：

- `ds-pro=["high","max"]`、`glm-52=["none","high","max"]`：think_seq分别是`(HIGH,MAX)`，结构不变仍可用，但**客户端medium/low的落点会变**（原来在绝对锚定下一律钳到high；相对映射下取决于source侧的think_seq长度和排名，可能落到max）——这是预期的架构行为变化，不是回归，但需要在实施后重新验证一遍这几个supply的钳位结果是否符合预期。
- `claude-haiku-sankuai-0956`/`glm-51-*`（当前未配置，走默认5档）：需要重新验证默认序列在新架构下的行为。
- `ds-v3friday-sankuai-3339`（`effort_enum:[]`）：think_seq为空 → 所有思考意图走STRIP，关闭意图走`off_alias(None)`→STRIP。这是行为改进（现状会透传，新架构会清理）。

---

## 8. 迁移影响面（文件清单）

| 文件 | 改动 |
|---|---|
| `core/reasoning/ladder.py` | `ReasoningIntent`→`RawIntent`，删`explicit_off`；`CanonicalEffort`/budget表不变 |
| `core/reasoning/capability.py` | `ReasoningCapability`→`ModelReasoningCapability`（补off_alias单调性校验）；`AlignedEffort`→`TargetEffort`+`stripped`；`align`→`remap`（三参数）；新增`think_seq`/`rank_of`/`remap_rank`/`clamp_absolute`；新增`abstract_encode`+`AbstractReasoning`/`AbstractKind`（放本模块，不新建文件，除非implementer判断放独立文件更清晰） |
| `core/reasoning/codecs.py` | 各codec `encode`→`syntax_adapt`（吃`AbstractReasoning`）；OFF/MAX特殊if全部删除（OFF已上收到remap+abstract_encode两处，MAX走统一查表）；`_canonical_to_chat_domain_name`→`_canonical_to_openai_effort_name`；补STRIP结构模板 |
| `core/reasoning/registry.py` | `apply_fields`不变；`get_codec`不变 |
| `core/server.py` | `_forward`reasoning段重排：`resolve_source_capability`→`remap`→`select_variant`→`abstract_encode`→`syntax_adapt`；400重试段调整为只重跑`select_variant`+`syntax_adapt`；debug日志函数签名跟随改名 |
| `model_proxy_config.json` | 各条`strategy`下新增`tiers_source_capability`（见§7.1）；逐supply复核`reasoning_capability`语义（§7.3） |
| `tests/test_reasoning.py` | 大范围重写，见下方验证方式 |

---

## 9. 验证方式（implementer必须逐项覆盖）

**remap相对映射核心**
- haiku(LOW,MED,HIGH)→target(HIGH,MAX)：断言LOW→HIGH、MED→MAX、HIGH→MAX（§3.4）。
- 等档数source(LOW,MED,HIGH)→target(LOW,MED,HIGH)：rank恒等，验证相对映射在同构range上退化为恒等。
- source(LOW,HIGH) 2档→target(MINIMAL,LOW,MED,HIGH,XHIGH) 5档：验证向上扩展。

**边界**
- n=1：target单思考档，所有source思考意图→该唯一档。
- m=1：source单思考档→target**中位**档（用户拍板，不是最强）。用具体例子验证：target 2档→中位是最低档（index 0）；target 3档→中位是中间档（index 1）。
- src_think空（source未建模）：回退`clamp_absolute`，等价现状行为（回归保护）。
- tgt_think空但含OFF：思考意图→off_alias，验证不会rank越界。
- tgt_cap.enum全空（ds-v3friday场景）：思考意图→STRIP，验证syntax_adapt产清理dict。

**OFF/DISABLED路径**
- 客户端disabled→decode level=OFF present=True→remap off_alias→abstract DISABLED→Anthropic syntax_adapt产`{thinking:{type:disabled}}`。
- 客户端OFF但target off_alias=None→STRIP，清理字段。
- **反例守卫测试**：off_alias配成高于最低思考档→`from_config`应拒绝，回退None（单调性守卫）。

**单调性属性测试（穷举）**
- 对多组(src_cap, tgt_cap)笛卡尔组合，穷举所有source思考档对(l1≤l2)，断言`remap(l1).level ≤ remap(l2).level`。
- 新增维度：固定tgt_cap，遍历不同src_cap，确认单调性对source range变化仍成立。

**缺陷1回归**
- present=True + target空enum：断言wire dict含`thinking:None`（会被apply_fields删除），而非现状的`{}`（透传残留）。
- present=False：断言wire`{}`，body无残留。

**failover跨supply不永久钳低**
- 循环外decode一次RawIntent，两轮不同tgt_cap各自remap，断言换到宽cap时强度恢复（现状`TestForwardLoopReasoningIntentNotPolluted`的等价断言，改用remap三参数签名）。

**跨协议一致性**
- 同RawIntent+同src_cap+同tgt_cap，经anthropic/chat/responses三codec的remap结果（canonical level）相同——remap只依赖intent+两cap，不依赖协议，断言不变。

**MAX无特殊分支验证**
- 专门写一条测试，读`codecs.py`源码或者通过mock/spy的方式，确认代码里不存在任何`if level == CanonicalEffort.MAX`的判断（可以用简单的grep断言，或者构造一个"MAX作为普通序列项"的查表测试用例，验证它跟LOW/MEDIUM/HIGH走同一条代码路径，不是单独if）。

---

## 10. implementer执行纪律

1. 这是大范围重构，涉及多文件正确性耦合，必须先通读全部方案再动手，不要边改边设计。
2. 严格按本文档的3个已拍板决策执行：OFF允许特殊分支但统一收在remap+abstract_encode两处；MAX完全不允许特殊分支；m=1时取中位不取最强；off_alias超过最低思考档时from_config要拒绝校验。
3. `source_models`的具体数值（claude-opus/sonnet/haiku各自的effort_enum）如果无法确认真实数据，先用默认全档序列占位并在汇报里明确标注"待确认"，不要编造具体档位数据。
4. 现有生产config的14+个supply，改完代码后需要重新过一遍debug日志验证实际钳位结果，如果发现某个supply的行为跟预期有出入（尤其是§7.3提到的"medium/low落点会变"这类预期内变化 vs 意外回归的区分），如实汇报，不要为了让测试通过而悄悄调整算法。
5. 单测覆盖第9节列的全部场景，不能选择性覆盖。
6. 涉及生产config改动前先备份，验证完成后再决定是否保留改动（这次不是"测试完还原"，是要真正落地的最终状态，但仍建议先在能推送真实请求验证的环境里跑通再合并）。
7. 完成后建议派reviewer独立复核（这次改动范围大、涉及核心转发路径，按项目规则默认应该复核一次）。
