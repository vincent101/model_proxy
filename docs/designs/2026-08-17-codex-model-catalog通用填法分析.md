---
type: analysis
status: draft
target: tools/model_proxy
tags: [architect, model_proxy, codex, catalog]
---

# codex model_catalog_json 通用填法分析

## 1. 背景与问题

codex 0.145.0 接入 model_proxy（HTTP 转发代理），config.toml 设 `model_provider="model_proxy"`, `model="claude-sonnet"`。codex 报 warning "Model metadata for claude-sonnet not found. Defaulting to fallback metadata"——因为 codex 内置 model catalog 不认识 `claude-sonnet`（它是 model_proxy 的档位标识，不是真实模型名）。需用 `model_catalog_json` 配一份自定义 catalog 消掉 warning 并让 codex 用准确参数。

核心矛盾：model_proxy 会在切换 route 时换上游真实模型——同一 `claude-sonnet` 档位，不同 route 背后是完全不同的真实模型（claude-sonnet-5 / gpt-5.6-terra / glm-5.2 / deepseek-v4-pro-tencent）。catalog 是 codex 侧的，它看不到 route 切换。catalog 该描述什么？有没有相对通用的填法？

## 2. 核心结论

**catalog 应描述契约层（codex↔model_proxy 接口），不描述具体上游模型。**

理由：catalog 的职责是告诉 codex 如何构造 wire 请求（发什么 model 值、要不要带 reasoning 字段、注册哪些工具、何时 auto-compact）。这些决策发生在 codex 侧，model_proxy 负责把 wire 请求翻译转发到真实上游。catalog 不应越俎代庖地描述上游模型能力——model_proxy 自身已有 effort 映射链路（decode→remap→encode→syntax_adapt，含 STRIP/DISABLED 兜底）处理 reasoning 能力差异，协议翻译层处理 wire 格式差异。

**关键洞察**：model_proxy 已经内建了能力适配安全网（effort 相对映射 + STRIP/DISABLED + 协议翻译）。catalog 应利用这些安全网，而非试图预判上游模型能力。唯一没有安全网的字段是 `context_window`。

## 3. 字段分类：契约层 vs 模型能力层

### 3.1 契约层字段（route 无关，可通用填）

| 字段 | 通用值 | 依据 |
|---|---|---|
| `slug` | `"claude-sonnet"` | 必须等于 codex 发的 model 值。model_proxy `_MODEL_TIER_MAP` 精确匹配此值映射到 sonnet 档 |
| `shell_type` | `"default"` | 控制 codex 注册哪个 exec 工具，是 codex 侧工具协商，与上游无关 |
| `apply_patch_tool_type` | `"freeform"` | 控制 codex 是否注册 apply_patch 工具。注册是 codex 侧行为；上游能否遵循是软伤（降级，不报错） |
| `supports_reasoning_summary_parameter` | `true` | true 时 codex 发 reasoning 字段。model_proxy 的 effort 映射链路会按 target 能力处理（STRIP/DISABLED/remap）。填 false 则对所有 route 都不发 reasoning，浪费强模型能力 |
| `default_reasoning_summary` | `"auto"` | codex 侧默认值，与上游无关 |
| `truncation_policy` | `{"mode":"bytes","limit":10000}` | 输出截断，codex 侧行为。与 fallback 默认值一致 |
| `input_modalities` | `["text"]` | 保守填 text-only。多数上游不支持图像，填 text+image 会触发 image 工具但上游拒绝。text-only 不影响文本能力 |
| `web_search_tool_type` | `"text"` | codex 侧工具注册，与上游无关 |
| `support_verbosity` | `false` | 无上游依赖的 wire 字段开关。false 不发 verbosity 字段，减少弱模型的指令负担 |
| `experimental_supported_tools` | `[]` | 空列表，不注册实验性工具 |
| UI/元数据字段 | 见下方 JSON 草稿 | display_name/description/visibility/priority 等纯 UI 字段，不影响 wire |

### 3.2 模型能力层字段（route 相关，需取舍）

| 字段 | 问题 | 通用填法 | 代价 |
|---|---|---|---|
| `context_window` | 不同上游窗口不同（claude 200k? gpt-5.6 272k? ds-pro/glm-5.2 128k?）。填错是硬伤（过大→溢出请求被上游拒；过小→过早 compact 浪费窗口） | **取所有可能 route 上游的最小窗口**。需用户确认具体值 | 强模型浪费窗口（软伤），但不会溢出（避免硬伤） |
| `base_instructions` | 官方 prompt.md 是为 GPT 写的 agentic 提示词。发给 ds-pro/glm（指令遵循弱）可能无效或干扰 | **用官方 BASE_INSTRUCTIONS**（prompt.md 全文）。fallback 本就发这个，显式填只是消 warning | ds-pro/glm 可能不遵循这些指令（软伤，但 fallback 也一样发，不更差） |
| `supported_reasoning_levels` | 不同上游支持的 effort 档不同 | **填全 5 档** `[low, medium, high, xhigh, max]`。model_proxy 的 remap 会按 target 能力映射 | codex 发 max → model_proxy remap 到 target 最高档。弱模型只有 low/high → remap 降级，不报错 |
| `default_reasoning_level` | 默认 effort 档 | `"medium"` | 中性默认，model_proxy remap 到 target 中位档 |

### 3.3 base_instructions 深入分析

codex 源码 `model_info.rs` 的 `model_info_from_slug()` fallback 行为：
- `model_messages.instructions_template = Some(BASE_INSTRUCTIONS.to_string())`（即 prompt.md 全文，约 3400 词）
- `used_fallback_model_metadata = true`

这意味着**不配 catalog 时，codex 已经在向 model_proxy 发这份 prompt.md 作为 system prompt**。model_proxy 原样翻译转发给上游。配 catalog 显式填 BASE_INSTRUCTIONS，不会改变现状，只是消掉 warning 并让 `used_fallback_model_metadata = false`。

另一个选择是填空/最小指令。但 codex 的 `get_model_instructions()` 在 template 为 None 时打 warning 并返回空字符串——上游模型完全没有 agentic 指令引导，对强模型（claude/gpt）是损失。因此**用官方 BASE_INSTRUCTIONS 是更好的通用选择**。

### 3.4 context_window 深入分析

codex 的 `auto_compact_token_limit()` 方法：`resolved_context_window() * 0.9`（当 auto_compact_token_limit 未显式设时）。`resolved_context_window()` = `context_window.or(max_context_window)`。

后果链：
- **填过大**（如 272k）→ codex 认为有 272k 窗口 → auto-compact 阈值 244k → 当上游是 ds-pro（假设 128k）→ 请求体在 128k~244k 之间时上游拒绝（context overflow）→ **硬伤（请求失败）**
- **填过小**（如 128k）→ codex 认为只有 128k → auto-compact 阈值 115k → 当上游是 claude（200k）→ 在 115k 时就 compact → **软伤（过早压缩，浪费 72k 窗口，但不报错）**
- **填 0 或 None** → fallback 路径：codex 的 `resolved_context_window()` 返回 None → `auto_compact_token_limit()` 返回 None → codex 不触发 auto-compact → 长会话最终上游拒绝 → **硬伤**

通用填法结论：**取所有可能 route 上游的最小窗口值**。代价是强模型浪费窗口（软伤），但不会溢出（避免硬伤）。具体值需用户确认——这些模型名（claude-sonnet-5 / gpt-5.6-terra / deepseek-v4-pro-tencent / glm-5.2）是美团内部别名，公开数据查不到准确窗口。

## 4. 三档 catalog 设计

model_proxy 的 `_MODEL_TIER_MAP` 有三档：`claude-opus`→opus、`claude-sonnet`→sonnet、`claude-haiku`→haiku。用户可能在 codex config.toml 切换 `model` 值为三档之一。catalog 应同时含三条 ModelInfo，slug 分别 = `claude-opus`/`claude-sonnet`/`claude-haiku`，这样切档位时 catalog 仍可用。

### 三档字段差异

| 字段 | claude-opus | claude-sonnet | claude-haiku |
|---|---|---|---|
| slug | `"claude-opus"` | `"claude-sonnet"` | `"claude-haiku"` |
| display_name | `"Claude Opus (proxy)"` | `"Claude Sonnet (proxy)"` | `"Claude Haiku (proxy)"` |
| default_reasoning_level | `"high"` | `"medium"` | `"low"` |
| supported_reasoning_levels | 全 5 档 | 全 5 档 | `[low, medium, high]`（haiku 档通常对应弱模型，收敛档位减少 remap 偏差） |
| context_window | 同值（取最小） | 同值 | 同值 |
| base_instructions | 同（BASE_INSTRUCTIONS） | 同 | 同 |
| 其余字段 | 同 | 同 | 同 |

三档 base_instructions 不区分：prompt.md 内容是通用 agentic 指令，不按档位区分。reasoning levels 差异仅在于默认档和 haiku 收窄档位范围（减少 codex 向弱模型发 xhigh/max 后 remap 到 target 不存在档位的风险——虽然 model_proxy remap 会兜底，但收窄 source 序列让映射更精准）。

## 5. 通用填法 JSON 草稿

以下为 `claude-sonnet` 条目的 ModelInfo JSON 草稿。`base_instructions` 和 `context_window` 的具体值用 `<<需用户确认>>` 标注。

```json
{
  "slug": "claude-sonnet",
  "display_name": "Claude Sonnet (proxy)",
  "description": "model_proxy sonnet tier (route-agnostic catalog entry)",
  "default_reasoning_level": "medium",
  "supported_reasoning_levels": [
    {"effort": "low", "description": "Low reasoning effort"},
    {"effort": "medium", "description": "Medium reasoning effort"},
    {"effort": "high", "description": "High reasoning effort"},
    {"effort": "xhigh", "description": "Extra-high reasoning effort"},
    {"effort": "max", "description": "Maximum reasoning effort"}
  ],
  "shell_type": "default",
  "visibility": "list",
  "supported_in_api": true,
  "priority": 50,
  "availability_nux": null,
  "upgrade": null,
  "model_messages": {
    "instructions_template": "<<需用户确认：填 codex 官方 prompt.md 全文，或留 null 让 fallback 机制发 BASE_INSTRUCTIONS>>",
    "instructions_variables": null,
    "approvals": null,
    "collaboration_modes": null,
    "auto_review": null,
    "permissions": null,
    "multi_agent": null,
    "token_budget": null
  },
  "include_skills_usage_instructions": false,
  "include_plugin_usage_instructions": false,
  "include_apps_usage_instructions": false,
  "supports_reasoning_summary_parameter": true,
  "default_reasoning_summary": "auto",
  "support_verbosity": false,
  "default_verbosity": null,
  "apply_patch_tool_type": "freeform",
  "web_search_tool_type": "text",
  "truncation_policy": {"mode": "bytes", "limit": 10000},
  "supports_image_detail_original": false,
  "context_window": <<需用户确认：取所有 route 上游的最小窗口，如 128000>>,
  "max_context_window": <<需用户确认：同 context_window 或填最大可能值>>,
  "auto_compact_token_limit": null,
  "effective_context_window_percent": 95,
  "experimental_supported_tools": [],
  "input_modalities": ["text"],
  "supports_search_tool": false,
  "use_responses_lite": false,
  "node_repl_auto_review_required": false,
  "node_repl_disabled": false,
  "auto_review_model_override": null,
  "model_specialty": null,
  "tool_mode": null,
  "multi_agent_version": null
}
```

`claude-opus` 和 `claude-haiku` 条目结构相同，差异见第 4 节表格。

**关于 base_instructions 的两种做法**：
1. 显式填 prompt.md 全文：消 warning、`used_fallback_model_metadata=false`。但 JSON 文件会很大（prompt.md 约 3400 词）。
2. 不配 model_catalog_json，接受 warning：codex fallback 自动用 BASE_INSTRUCTIONS + context_window=272000。warning 不影响功能，只是日志噪音。

## 6. 风险与权衡

### 6.1 通用填法的代价

| 代价类型 | 具体表现 | 严重程度 |
|---|---|---|
| context_window 填保守值 | 强模型（claude 200k/gpt 272k）只能用到 ~115k 就 auto-compact | 软伤（性能降级，不报错） |
| base_instructions 对弱模型无效 | ds-pro/glm-5.2 可能不遵循 prompt.md 里的 agentic 指令 | 软伤（fallback 也发同样内容，不更差） |
| apply_patch 对弱模型不可靠 | ds-pro 可能不遵循 freeform 工具格式 | 软伤（工具调用失败 → codex 重试或改用 shell） |
| reasoning remap 降级 | codex 发 max → model_proxy remap 到 ds-pro 的 high（最高档） | 软伤（档位降级，不报错） |

### 6.2 硬伤 vs 软伤

- **硬伤**（请求失败/挂起）：只有 `context_window` 填过大时会触发——codex 不 compact → 上游溢出 → 请求被拒。通用填法用最小值规避此风险。
- **软伤**（性能降级但能用）：context_window 填保守值（过早 compact）、base_instructions 对弱模型无效、apply_patch 不可靠。这些都是可接受的降级。

### 6.3 通用填法 vs 每 route 一份 catalog

| 维度 | 通用填法 | 每 route 一份 catalog |
|---|---|---|
| 维护成本 | 低（一份 catalog，切 route 不改） | 高（每 route 一份，切 route 时同步切 catalog） |
| context_window 精度 | 低（取最小，强模型浪费窗口） | 高（每 route 填真实值） |
| base_instructions 精度 | 低（统一用 prompt.md） | 高（可按模型定制） |
| 切 route 时忘记改 catalog | 不存在此风险 | 存在（catalog 与 route 不匹配） |
| 消 warning | 能 | 能 |

codex 有 profile 机制（config.toml 的 profile 切换），理论上可以每 route 一个 profile、各带不同 model_catalog_json。但这增加了切换复杂度和"忘记切"的风险。

**推荐通用填法**。理由：唯一精度损失是 context_window（软伤），维护成本远低于每 route 一份。如果用户对强模型的窗口利用率有强烈需求，可以后续在 model_proxy 侧加一个"响应里带上游 context_window 提示"的机制让 codex 动态感知——但这是 model_proxy 的功能扩展，不在 catalog 填法范围内。

### 6.4 是否值得做

消 warning 本身不是刚需（warning 不影响功能）。但如果要做，通用填法是成本最低、风险最小的做法。主要价值不在消 warning，而在于：
- `context_window` 不再用 fallback 的 272k（避免对小窗口上游溢出）
- `input_modalities` 收窄为 text-only（避免向不支持图像的上游发图像工具调用）
- `apply_patch_tool_type` 显式设为 freeform（fallback 是 None，不注册 apply_patch）

## 7. 需用户确认的取值项

1. **context_window**：需确认所有可能 route 上游模型的最小 context window。当前 codex strategy 绑 route_pool=nation2（sonnet 档 = ds-pro），但也可能切到 claude/openai/nation1。需确认 ds-pro / glm-5.2 / claude-sonnet-5 / gpt-5.6-terra 的窗口值，取最小。
2. **base_instructions**：是填 prompt.md 全文（JSON 会很大），还是接受 fallback 机制（不配 catalog 或配 catalog 但 model_messages.instructions_template 填 null 接受 warning）？
3. **haiku 档 supported_reasoning_levels**：haiku 档在 model_proxy 里对应 ds-flash（effort_enum 只有 `["high"]`）。catalog 里 haiku 的 supported_reasoning_levels 是否应收窄为 `[low, medium, high]`？还是也填全 5 档让 model_proxy remap 兜底？

## 8. 验证方式

1. **消 warning**：配好 catalog 后启动 codex，观察启动日志是否还有 "Model metadata for claude-sonnet not found" warning。
2. **wire 请求验证**：用 codex 发一个请求，在 model_proxy 的 ACCESS 日志里检查：
   - model 字段 = "claude-sonnet"（slug 正确）
   - reasoning 字段存在（supports_reasoning_summary_parameter=true 生效）
   - instructions 字段存在（base_instructions 生效）
3. **auto-compact 行为**：在长会话中观察 codex 是否在预期 token 数触发 compact（验证 context_window 填值是否合理）。
4. **切 route 测试**：切 route 后（不改 catalog），发请求验证：
   - 请求不报错（catalog 通用性）
   - reasoning remap 正常（model_proxy 日志里看 decode→remap→encode 链路）
   - apply_patch 工具是否可用（弱模型可能不遵循，但不影响文本对话）

## 附录 A：catalog 字段与 Claude Code 接入对照

本节回答"catalog 要填的东西在 Claude Code 侧类似用途下填什么"，厘清 codex 与 Claude Code 的架构差异，解释为何 codex 比 Claude Code 多一份 catalog。

### 架构差异

| | codex | Claude Code |
|---|---|---|
| 架构定位 | **重客户端**：本地 catalog（StaticModelsManager）查表决定请求参数 | **轻客户端**：不维护 model 元数据表 |
| 接入第三方所需配置 | provider 段（base_url/wire_api/token）+ **catalog JSON** + onboarding（hasCompletedOnboarding） | env 段（ANTHROPIC_BASE_URL/AUTH_TOKEN/DEFAULT_*_MODEL）+ onboarding |
| model 能力决策位置 | 客户端查 catalog（context_window/reasoning/tools 全预存于客户端） | 推迟到请求时（thinking 参数）或依赖协议层协商 |
| 不认识 model 时行为 | warning "metadata not found" + fallback 到内置 prompt.md | **无此 warning**——不查表，直接发请求 |

结论：Claude Code 接入 model_proxy"开箱即用"（只配 env + onboarding）；codex 要多一份 catalog——这是架构代价：codex 把模型能力知识放在客户端侧，Claude Code 放在协议层/请求时。

### 字段对照表

catalog 必填字段在 Claude Code 侧的等价物（或"不需配"的理由）：

| catalog 字段 | 用途 | Claude Code 侧等价 |
|---|---|---|
| slug | model 标识，必须 = codex 发的 model 值；wire 的 model 字段 | `ANTHROPIC_DEFAULT_SONNET_MODEL="claude-sonnet"`（env 段，把档位固定到 model_proxy 认识的标识） |
| base_instructions | codex 发给上游的系统提示词（wire 的 instructions） | Claude Code 系统提示词**内置**、不绑 model；用户层用 AGENTS.md/CLAUDE.md 覆盖。**不配** |
| supports_reasoning_summaries | bool，门控是否发 reasoning 字段（false→完全不发） | Claude Code 用请求时的 `thinking` 参数，不预存 model 能力。**不配** |
| supported_reasoning_levels | reasoning effort 档位数组 | 同上，Claude Code 不预存。**不配** |
| context_window | auto-compact 阈值、UI | Claude Code **不显式配**，靠协议层 usage 或内置默认。**不配** |
| supports_parallel_tool_calls | wire 的 parallel_tool_calls | Claude Code 内部决定。**不配** |
| support_verbosity / default_verbosity | 门控 text.verbosity | Claude Code 不暴露此旋钮。**不配** |
| apply_patch_tool_type | "freeform" 注册 apply_patch 工具 | Claude Code 工具注册内部处理。**不配** |
| shell_type | 决定注册哪个 exec 工具 | Claude Code 内部。**不配** |
| truncation_policy | {mode,limit} 输出截断 | Claude Code 用 tool_output_token_limit 等，不绑 model。**不配** |
| input_modalities | ["text","image"] 门控图像 | Claude Code 不配 |
| display_name/description/visibility/supported_in_api/priority/availability_nux/upgrade/experimental_supported_tools | UI/picker/排序字段 | Claude Code 无 picker。**不需配** |

可选字段（default_reasoning_level/max_context_window/auto_compact_token_limit/web_search_tool_type/supports_search_tool 等）在 Claude Code 侧同样无对应配置。

### 关键观察

catalog 字段里**只有 slug 在 Claude Code 有直接等价**（`ANTHROPIC_DEFAULT_*_MODEL` 把档位固定到 claude-haiku/sonnet/opus 三个 model_proxy 认识的标识）。其余字段 Claude Code 要么内置处理（base_instructions/shell_type/tools）、要么推迟到请求时（reasoning）、要么靠协议层（context_window）——**都不需要配置层填**。

即：Claude Code 侧填的是"指向哪个端点 + 用哪个档位标识"（env 段约 5 个变量 + onboarding）；codex 侧除这些外，还得额外填"这个档位标识代表的能力画像"（catalog 整份 JSON）。这是 codex 接入比 Claude Code 多一步 catalog 的根因。

## 9. 关联

- model_proxy 档位映射：`core/server.py` L898-901 `_MODEL_TIER_MAP`
- model_proxy effort 映射链路：[[docs/REASONING.md]] §6
- model_proxy 架构：[[docs/ARCHITECTURE.md]] §3-§5
- codex ModelInfo 源码：`codex-rs/protocol/src/openai_models.rs`（struct 定义）、`codex-rs/models-manager/src/model_info.rs`（fallback 逻辑）、`codex-rs/models-manager/prompt.md`（BASE_INSTRUCTIONS）
- model_proxy 配置：`config/model_proxy_config.json`（supplies/routes/strategies）
