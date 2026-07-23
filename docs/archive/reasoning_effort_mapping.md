---
created: 2026-07-23 21:23:29
---
# Claude Code 思考档位 → 上游真实模型 的映射事实核实

> 编制日期：2026-07-20
> 性质：事实调查 + 映射核实，非架构设计。所有取值只写能由代码 / 真实日志 / 官方文档确认的内容，不确定项显式标注。
> 证据来源标记：`[代码]`=model_proxy 源码逻辑推演；`[日志]`=`.claude_model_proxy.log` 真实运行记录；`[官方]`=code.claude.com 官方文档（2026-07-20 抓取）；`[网关实测]`=spec 记录的历史 curl 实测；`[未确认]`=当前证据不足。
>
> **⚠️ 已过时提示**：本文档记录的是重构前（`align()` 绝对钳位、无 source 侧能力建模）的现状。
> 本文档提出的"档位数量不对等已被正确处理，剩下的是可观测性缺口"这一结论，后续推动了完整的
> 架构重构——已改为 source/target 双侧能力建模 + 相对排名映射（`remap()`），不再是本文档描述的
> 单侧绝对钳位。最新设计见 `reasoning_relative_remap_redesign.md`，代码现状以该文档 +
> `core/reasoning/capability.py` 源码为准。本文档保留作历史调查记录，不代表当前实现。

---

## 0. 背景：为什么需要这张表

`model_proxy` 让 Claude Code（只认 Anthropic 协议）接入美团 aigc 网关背后任意厂商的真实模型（Claude / GPT / GLM / DeepSeek）。

Claude Code 客户端的"思考档位选择器"本是给 Anthropic 模型设计的一组离散选项（`low/medium/high/xhigh`，加会话级 `max`/`ultracode`）。但经 model_proxy 接入后，客户端选的 `claude-opus/sonnet/haiku` 可能被路由到 GLM、DeepSeek、GPT 等模型上——这些模型的思考强度表达方式（离散档名 or 连续 budget_tokens）、档位数量、语义都不一定与客户端 UI 呈现的选项匹配。

本文用三张表核实"客户端选档位 → 实际数据传递"的每一环，并回答核心问题：**这种"错位"到底错在哪，是转换有损，还是已被现有三层架构正确处理。**

model_proxy 的 reasoning 处理是三层解耦架构（`core/reasoning/`）：
1. **ladder.py** — `CanonicalEffort` 全序枚举（OFF<MINIMAL<LOW<MEDIUM<HIGH<XHIGH<MAX），跨协议统一强度刻度 + budget↔canonical 锚点。
2. **capability.py** — 每个 supply 的 `reasoning_capability`（真实支持哪几档）+ `align()` 唯一钳位点（单调不减就近钳位）。
3. **codecs.py** — 各协议编解码器，把 canonical 强度编码回目标协议的具体字段。

链路：`decode(source 协议) → align(目标 supply 的 capability) → select_variant(目标模型学到的语法偏好) → encode(目标协议)`。source 协议恒为 anthropic（Claude Code 发的）。`[代码 core/server.py:564-600]`

---

## 表1：Claude Code 选不同档位 → 实际发给 model_proxy 的 Anthropic body

### 客户端档位选择机制 `[官方]`

| 机制 | 说明 |
|---|---|
| 可选档位 | `low` / `medium` / `high` / `xhigh`（可持久化，写 `effortLevel`）；`max` / `ultracode`（仅会话级，不可写 `effortLevel`） |
| 选择方式 | `/effort` 命令、`/model` 里的 effort 滑块、`--effort` flag、`CLAUDE_CODE_EFFORT_LEVEL` 环境变量、skill/subagent frontmatter 的 `effort` 字段 |
| 优先级 | 环境变量 > 配置档位 > 模型默认档 |
| `ultracode` | 不是模型档位，是 Claude Code 设置：向模型发 `xhigh`，另外触发 workflow 编排 |
| `ultrathink` 关键词 | 不改 API effort，仅在 prompt 里加 in-context 指令；`think`/`think hard` 等短语当普通文本处理 |
| 关闭思考 | `MAX_THINKING_TOKENS=0`：Anthropic API 上关闭 thinking；**第三方 provider 上改为省略 `thinking` 参数**，自适应模型仍可能思考 |

### 档位 → wire body 形态 `[官方]` + `[代码]`

Claude Code 依据它对"当前 model 名 + `_SUPPORTED_CAPABILITIES`"的判断，用两种 wire 形态之一表达档位：

| 模型类别（客户端视角） | wire 形态 | 说明 |
|---|---|---|
| 采用自适应推理的模型（Fable 5 / Sonnet 5 / Opus 4.7+ 恒用） | 形态A：`thinking:{type:"adaptive"}` + `output_config:{effort: <档名>}` | `[官方]` 明确 Sonnet5/Opus4.7+ always use adaptive reasoning |
| 固定思考预算模型（Opus 4.6 / Sonnet 4.6，或设 `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1`） | 形态B：`thinking:{type:"enabled", budget_tokens: N}` | 由 `MAX_THINKING_TOKENS` 控制 N |
| 显式关闭 | 形态C：`thinking:{type:"disabled"}`（Anthropic API）；第三方 provider 上省略 `thinking` | |

**经 model_proxy 时**：客户端配置的模型名是 `claude-opus/sonnet/haiku`（映射到 opus-4.8 / sonnet-5 家族），匹配"采用自适应推理"的新模型 → 客户端应发**形态A（adaptive + output_config.effort）**。

**真实日志佐证** `[日志 line 285-859]`：运行中 `aws.claude-opus-4.8`、`claude-sonnet-5` 的 reasoning 语法偏好均学到 `anthropic_adaptive`（proxy 侧对上游语法的学习结果，间接印证 adaptive 语法是该链路的稳定形态）。

### 各档位 → decode 后的 canonical 强度

以形态A（adaptive + output_config.effort）为例，`AnthropicReasoningCodec.decode` 的产出 `[代码 codecs.py:80-103]`：

| 客户端档位 | output_config.effort | decode → CanonicalEffort | present | explicit_off |
|---|---|---|---|---|
| （不发/未启用思考） | 无 thinking.type | `None` | False | False |
| 显式关闭 | `thinking.type=disabled` | `None` | True | **True** |
| low | `low` | LOW(2) | True | False |
| medium | `medium` | MEDIUM(3) | True | False |
| high | `high` | HIGH(4) | True | False |
| xhigh | `xhigh` | XHIGH(5) | True | False |
| max | `max` | MAX(6) | True | False |
| adaptive 无有效 effort | （缺失/非标准词） | MEDIUM(3) 兜底 | True | False |

以形态B（enabled + budget_tokens）为例，budget→canonical 锚点 `[代码 ladder.py:43-98]`：

| budget_tokens N | decode → CanonicalEffort |
|---|---|
| N < 2000 | LOW(2) |
| 2000 ≤ N < 8000 | MEDIUM(3) |
| 8000 ≤ N < 32000 | HIGH(4) |
| 32000 ≤ N < 64000 | XHIGH(5) |
| N ≥ 64000 | MAX(6) |
| enabled 但无 budget_tokens | 默认 10000 → HIGH(4) |

**未确认项**：
- 客户端各档位（low/medium/high/xhigh/max）对应的**具体 output_config.effort 档名字符串**几乎可以肯定与档位同名（`[官方]` 档位命名与协议档名一致），但客户端真实发出的 HTTP body 未在本地抓包，属 `[未确认，需抓包/客户端日志]`；日志只记路由与 variant 偏好，不记请求 body。
- 若走形态B，客户端各档位对应的**具体 budget_tokens 数值**官方未公开（`[官方]` 明说各 effort 档的 token 预算由 Claude Code 内部管理，本页未列数值）→ `[未确认]`。
- 经代理时客户端实际走形态A 还是形态B，取决于客户端对虚拟模型名 `claude-*` 的 capability 判断。官方文档支持形态A（adaptive），日志间接印证，但无客户端 body 抓包直证 → 结论"形态A"为**高置信推断，非直接实测**。

---

## 表2：model_proxy 收到后转发给上游各真实模型的 body

**前提**：假设客户端选 `high` 档（decode → HIGH），走 `decode→align→encode`。variant 取默认或日志已学到的偏好。`canonical_to_budget` 反算值 `[代码 ladder.py:61-69]`：LOW→1500 / MEDIUM→5000 / HIGH→16000 / XHIGH→48000 / MAX→128000（仅当无 source_budget 回填时兜底；形态A 无 source_budget，故走反算）。

| # | 真实模型 (target_model) | supply.protocol | reasoning_capability（config 实配） | 解析后 capability enum | align(HIGH)→ | encode 变体 | **最终发给上游的 reasoning body** |
|---|---|---|---|---|---|---|---|
| 1 | claude-sonnet-5 | anthropic | `["low","medium","high","xhigh","max"]` | LOW,MED,HIGH,XHIGH,MAX | HIGH | adaptive `[日志学到]` | `thinking:{type:"adaptive"}` + `output_config:{effort:"high"}` |
| 2 | aws.claude-opus-4.8 | anthropic | `["low","medium","high","xhigh","max"]` | LOW,MED,HIGH,XHIGH,MAX | HIGH | adaptive `[日志学到]` | `thinking:{type:"adaptive"}` + `output_config:{effort:"high"}` |
| 3 | aws.claude-haiku-4.5 | anthropic | **未配** → 默认 5 档 | OFF,LOW,MED,HIGH,XHIGH | HIGH | 默认 enabled | `thinking:{type:"enabled", budget_tokens:16000}` + 清除 `output_config` |
| 4 | gpt-5.6-terra | responses | `["none","low","medium","high","xhigh"]` | OFF,LOW,MED,HIGH,XHIGH | HIGH | resp（单变体） | `reasoning:{effort:"high"}` |
| 5 | gpt-5.6-sol | responses | `["none","low","medium","high","xhigh"]` | OFF,LOW,MED,HIGH,XHIGH | HIGH | resp | `reasoning:{effort:"high"}` |
| 6 | gpt-5.6-luna | responses | `["none","low","medium","high","xhigh"]` | OFF,LOW,MED,HIGH,XHIGH | HIGH | resp | `reasoning:{effort:"high"}` |
| 7 | glm-5.2 | anthropic | **未配** → 默认 5 档 | OFF,LOW,MED,HIGH,XHIGH | HIGH | 默认 enabled | `thinking:{type:"enabled", budget_tokens:16000}` + 清除 `output_config` |
| 8 | glm-5.1 | anthropic | **未配** → 默认 5 档 | OFF,LOW,MED,HIGH,XHIGH | HIGH | 默认 enabled | `thinking:{type:"enabled", budget_tokens:16000}` + 清除 `output_config` |
| 9 | deepseek-v4-pro-tencent | anthropic | **未配** → 默认 5 档 | OFF,LOW,MED,HIGH,XHIGH | HIGH | 默认 enabled | `thinking:{type:"enabled", budget_tokens:16000}` + 清除 `output_config` |
| 10 | deepseek-v4-flash-tencent | anthropic | **未配** → 默认 5 档 | OFF,LOW,MED,HIGH,XHIGH | HIGH | 默认 enabled | `thinking:{type:"enabled", budget_tokens:16000}` + 清除 `output_config` |
| 附 | deepseek-v3-friday | anthropic | `["effort_enum": []]` **空档** | ()（空） | **None** | — | **不塞任何 reasoning 字段**（该 supply 声明不支持思考） |

> 注：用户列的 10 个模型中，haiku tier 实际路由到 `ds-flash`（见 `routes.claude.haiku`），`aws.claude-haiku-4.5` supply 虽在 config 中但当前无 route 引用。两者均已列出。`deepseek-v3-friday` 不在用户 10 个之列，作为"空档 capability"的边界示例附上。

### 边界推演：客户端选 max（decode → MAX）时的钳位差异（错位最直观处）

| 真实模型 | capability 是否含 MAX | align(MAX)→ | 最终 body |
|---|---|---|---|
| claude-sonnet-5 / opus-4.8 | **含 MAX** | MAX | adaptive + `output_config:{effort:"max"}`（无损透传最强档） |
| haiku-4.5 / glm-5.2 / glm-5.1 / ds-pro / ds-flash | 最高 XHIGH | **XHIGH（钳降）** | enabled + `budget_tokens:48000` |
| gpt-terra / sol / luna | 最高 XHIGH | **XHIGH（钳降）** | `reasoning:{effort:"xhigh"}` |

**这是"错位"最直观的表现**：客户端能选 `max`，但只有真 Claude（sonnet/opus）接得住 `max`；GLM/DS/GPT 全部按 `align()` 单调就近钳到各自最高档 `xhigh`。`_clamp_to_enum` 保证：并列取更高档、超上限钳最高档、单调不减 `[代码 capability.py:67-92]`。

### 语法自适应重试（enabled↔adaptive）`[代码 codecs.py:121-140 + server.py:728-735]`

anthropic 协议的 supply 首发默认 `enabled`；若上游 400 报错，`interpret_rejection` 从错误体识别应换的变体，学到后缓存 48h：
- `thinking.type.enabled not supported` / `budget_tokens not supported` → 换 `adaptive`
- `thinking.type.adaptive not supported` / `output_config not permitted` → 换 `enabled`
- GLM 泛化中文错误"参数有误" / "invalid parameter" → 换 `enabled`

日志证明 sonnet-5 / opus-4.8 已学到 `adaptive`（即上游 Claude 家族拒绝 enabled+budget、要 adaptive+effort）。GLM/DS 若首发 enabled 被接受则维持 enabled；若报错则可能回退——本地日志未见 GLM/DS 的 variant 学习记录 → 它们当前实际发 `enabled` 属推断，未见反证。

**未确认项**：
- GLM / DeepSeek 上游对 `thinking.type=enabled + budget_tokens` 的真实接受度（是否 400 回退 adaptive）未在日志中出现 → `[未确认，需实测或等日志累积]`。
- 网关对 `reasoning_effort`/`reasoning.effort` 五档（none/low/medium/high/xhigh）的支持已由 spec `[网关实测 2026-07-18]` 确认，`minimal` 不支持（400）。

---

## 表3（对照组）：不经代理，Claude Code 直连该真实模型时会发什么

**核心结论（机制层，`[官方]` 支撑）**：Claude Code 客户端发出的 body 格式 / 档位取值，**只取决于客户端自身对"当前配置 model 名 + `_SUPPORTED_CAPABILITIES` + 内建 model pattern"的判断，与请求最终被谁应答无关**。官方原文："`ANTHROPIC_BASE_URL` changes where requests are sent, not which model answers them."

| 维度 | 经 model_proxy | 直连真实模型（自定义 base_url） |
|---|---|---|
| 客户端看到的 model 名 | `claude-opus/sonnet/haiku`（虚拟名，匹配 Claude 新模型 pattern） | 真实模型名（如 glm-5.2 / deepseek-v4-pro），**大概率不匹配** Claude pattern |
| effort 特性是否启用 | 启用（匹配 Claude pattern，走 adaptive） | 取决于是否手动设 `_SUPPORTED_CAPABILITIES`；不设则特性可能全部禁用（官方："leaving supported features disabled"） |
| 可选档位集合 | 固定 low/medium/high/xhigh(+max/ultracode)，客户端 UI 决定 | 同样由客户端 UI + `_SUPPORTED_CAPABILITIES`（`xhigh_effort`/`max_effort`）决定，与背后模型无关 |
| wire 形态 | 形态A（adaptive + output_config.effort） | 若模型名不匹配 Claude pattern 且未配 capabilities → 可能**不发 thinking/output_config**，或按 fixed budget 发 |
| `MAX_THINKING_TOKENS=0` 行为 | — | 第三方 provider 上**省略 thinking 参数**（非 disabled），自适应模型仍可能思考 `[官方]` |

**这一条能否完全确认**：
- **能确认的**：客户端档位选项集合、wire 形态的"决定因素"（模型名 pattern + `_SUPPORTED_CAPABILITIES` + 客户端版本），以及"是否经代理"本身不改变客户端行为——这些 `[官方]` 明确。
- **不能确认的**：直连 GLM/DeepSeek 时客户端实际发出的**精确 body**（是否发 output_config.effort、budget_tokens 数值、pattern 是否命中）。原因：① Claude Code 是闭源客户端，无法读源码；② 本地无直连这些厂商的抓包样本；③ GLM/DeepSeek 是否提供 Anthropic 兼容端点、其 model ID 是否命中 Claude Code 内建 pattern 均未知。**要补全需要：对直连场景做真实抓包，或获取客户端 `_SUPPORTED_CAPABILITIES` 匹配逻辑的官方说明。** → `[未确认，需抓包]`

---

## 结论：这个"错位"到底错在哪

用户的原始判断："客户端可选思考程度只有 claude-opus/sonnet/haiku 的几个离散选项，这些离散选项与真实模型的离散/连续思考强度不一定匹配。"

**基于三张表的判断：这不是"转换有损的错位"，而是"档位基数不对等"这个客观事实——它已被现有三层架构（ladder 全序 + align 单调就近钳位）正确处理，属于"正确处理了不匹配"，不是"处理错了"。**

分两层说清：

**1. 转换本身是否有损/错误？——否，转换正确。**
- `ladder` 提供跨协议统一全序，`align()` 是唯一钳位点，数学上单调不减（intent 越强 → 输出不减）`[代码 capability.py:95-110]`。
- 客户端选 `max`、上游只到 `xhigh` 时，钳到 `xhigh` 是"就近保留最高可用思考质量"，不是信息错误——这正是不对等场景下的正确行为。没有出现"强度被错误拉高/拉低"的 bug。
- budget↔canonical 往返一致性已在代码注释中记录并修复过漂移 bug `[代码 ladder.py:50-60]`。
- 唯一的"信息丢失"是钳位本身固有的（5 档客户端强度映到少档上游必然多对一），这是任何正确的不对等映射都无法避免的，不属于实现缺陷。

**2. "离散档位数量不对等"本身算不算问题？——不算逻辑错误，但存在"可观测性缺口"。**
- 客户端呈现 5 档（+max），上游真实档数各异（Claude 6 档含 max、GPT/默认 anthropic 5 档、v3-friday 0 档）。只要 `align()` 按选定强度单调就近钳位，这就是"正确处理了不匹配"。
- 真正的问题是：**用户在客户端选 `max` 时，无法直观知道当前路由的上游其实只能到 `xhigh`、请求被钳降了**。客户端 UI 显示的档位是"以为在跟 Claude 说话"的档位，与背后真实模型的实际生效档位之间没有反馈通道。
- 因此，"可观测性缺口"这一判断在有了三张表之后**依然成立且更精确**：缺的不是"对齐逻辑"（逻辑已正确），而是"让用户看见映射结果"的手段——即当前请求的 `客户端档位 → 上游实际生效档位/字段` 这条链路对用户不可见。

**一句话**：档位对齐作为"转换逻辑"已被现有架构正确解决；剩下的不是逻辑问题，是"用户看不见钳位发生了什么"的可观测性问题。

---

## 未确认项汇总（补全所需信息）

| 未确认项 | 补全所需 |
|---|---|
| 经代理时客户端各档位发出的精确 HTTP body（形态A 各字段实值、budget_tokens 数值） | 对 model_proxy 入站请求做 body 级抓包/日志（当前日志不记 body） |
| 客户端各 effort 档对应的具体 budget_tokens 数值（若走形态B） | 官方未公开；需抓包客户端在 fixed-budget 模型下的请求 |
| 直连 GLM/DeepSeek 时客户端的精确 body 与 pattern 命中结果（表3） | 直连场景抓包 + `_SUPPORTED_CAPABILITIES` 官方匹配逻辑 |
| GLM/DeepSeek 上游对 enabled+budget_tokens 的真实接受度 | 实测发请求或等运行日志累积 variant 学习记录 |

---

## 附：如果要改进，建议方向（非本次任务主体）

事实调查结论指向"可观测性缺口"，若要弥补，方向（不含实施）：
1. **入站 body 级 debug 日志**（可开关）：记录客户端发来的 `thinking`/`output_config` 原值，以及 `decode→align→encode` 三段结果，让"档位被钳到哪"可复盘——同时也能一次性补全表1/表2 的全部未确认项。
2. **响应侧回显生效档位**：在返回给客户端的 metadata 里带上"上游实际生效的 effort/budget"，让用户在会话中直接看到钳降（若客户端能显示）。
3. 二者都只是"让映射可见"，不改 `align` 逻辑——因为逻辑本身已正确。
