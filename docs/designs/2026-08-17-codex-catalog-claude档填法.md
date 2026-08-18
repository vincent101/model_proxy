---
type: analysis
status: draft
target: tools/model_proxy
tags: [architect, model_proxy, codex, catalog, claude]
---

# codex catalog：claude-opus / claude-sonnet 档填法

## 1. 背景与问题

通用分析（[[2026-08-17-codex-model-catalog通用填法分析]]）已确定 catalog 字段分契约层（route 无关）与模型能力层（route 相关），并给出通用填法。本篇聚焦 route=claude（上游真 Claude）时 `claude-opus` 和 `claude-sonnet` 两条 ModelInfo 的具体填法——当上游有公开数据时，模型能力层字段可以从"保守取最小值"升级为"精确填"。

## 2. 关键事实（调研确认）

### 2.1 上游模型身份

model_proxy `config/model_proxy_config.json` L11-12 明确：

| supply id | target_model | protocol | effort_enum |
|---|---|---|---|
| `claude-opus-sankuai-2023` | `claude-opus-5` | anthropic | `[low, medium, high, xhigh, max]` |
| `claude-sonnet-sankuai-2023` | `claude-sonnet-5` | anthropic | `[low, medium, high, xhigh, max]` |

两个 supply 的 `effort_enum` 均为全 5 档，protocol 均为 anthropic。

### 2.2 context_window 官方数据

Anthropic 官方文档（platform.claude.com/docs/en/build-with-claude/context-windows）明确：

> "Claude Opus 5, Claude Opus 4.8, Claude Opus 4.7, Claude Opus 4.6, Claude Sonnet 5, and Claude Sonnet 4.6 have a 1M-token context window on the Claude API."

即 **claude-opus-5 和 claude-sonnet-5 的 context window 均为 1,000,000 tokens**。

### 2.3 model_proxy anthropic 上游的 reasoning 字段

`core/reasoning/codecs.py` 的 `AnthropicReasoningCodec` 确认：

- anthropic 协议用 **`thinking`** 字段（`type=enabled/adaptive/disabled`），不是 OpenAI 的 `reasoning` 字段
- 默认变体 `ANTHROPIC_ADAPTIVE`：`thinking.type=adaptive` + `output_config.effort=<level>`
- 备选变体 `ANTHROPIC_ENABLED`：`thinking.type=enabled` + `thinking.budget_tokens=<budget>`
- 有 400 自适应重试：若 adaptive 被上游拒绝 → 自动切 enabled，缓存 48 小时
- claude-opus-5 / claude-sonnet-5 已在 config 里声明全 5 档 effort_enum，model_proxy remap 链路完整

**codex 侧 wire 链路**：codex 发 OpenAI responses 协议（`reasoning.effort` 字段）→ model_proxy `ResponsesReasoningCodec.decode()` 解码 → `capability.remap()` 相对映射 → `AnthropicReasoningCodec.syntax_adapt()` 编码为 `thinking.type=adaptive` + `output_config.effort` → 发给 anthropic 上游。

catalog 的 `supports_reasoning_summary_parameter=true` 让 codex 在 wire 请求里带 reasoning 字段，model_proxy 完成协议翻译。链路完整，无断点。

### 2.4 base_instructions（prompt.md）内容

已抓取 `codex-rs/models-manager/prompt.md` 全文（约 3389 词）。内容是 codex CLI 的 agentic coding 系统提示词：

- 定义人格：concise, direct, friendly
- AGENTS.md spec（多级指令覆盖规则）
- Planning（update_plan 工具使用规范）
- Task execution（apply_patch 工具、代码规范、测试策略）
- Tool Guidelines（shell 搜索用 rg、update_plan 规范）
- 输出格式（section headers、bullets、monospace、file references）

**评估**：这是通用 agentic coding 指令，不绑定特定模型厂商。对 Claude opus-5/sonnet-5（强模型、指令遵循能力强）完全适用。发给弱模型（ds-pro/glm-5.2）可能不遵循，但 fallback 机制本就用这份 prompt.md，显式填不改变现状。

## 3. 方案设计

### 3.1 instructions_template（系统提示词注入路径）

**注意字段名**：ModelInfo **没有顶层 `base_instructions` 字段**（核对源码 `protocol/src/openai_models.rs` L385-475 确认）。系统提示词通过 `model_messages.instructions_template` 字段注入（codex `get_model_instructions()` 走此路径）。草稿 JSON 即用 `model_messages.instructions_template: "{{BASE_INSTRUCTIONS}}"`，这是正确路径。

**推荐：instructions_template 填 prompt.md 全文**。

理由：
- route=claude 时 Claude opus-5/sonnet-5 完全能遵循这些 agentic 指令，无风险
- 显式填消除 `used_fallback_model_metadata` warning，这是配 catalog 的初衷之一
- fallback 机制本就发这份 prompt.md（`model_info_from_slug` 用 BASE_INSTRUCTIONS 常量），显式填不改变实际发给上游的内容，只消 warning
- JSON 文件增大约 20KB，作为配置文件可接受

不推荐精简版：prompt.md 是精心设计的完整 agentic 指令，自行删减可能破坏指令完整性。

获取方式：`curl -sL https://raw.githubusercontent.com/openai/codex/main/codex-rs/models-manager/prompt.md`，将全文作为 JSON 字符串值填入 `model_messages.instructions_template`（注意转义换行符和引号）。

### 3.2 context_window

| 字段 | claude-opus | claude-sonnet | 依据 |
|---|---|---|---|
| `context_window` | `1000000` | `1000000` | Anthropic 官方文档：claude-opus-5/sonnet-5 均为 1M tokens |
| `max_context_window` | `1000000` | `1000000` | 同上 |
| `auto_compact_token_limit` | `null` | `null` | null → codex 用 `context_window * 0.9` = 900k 自动 compact |

**注意**：此值基于官方公开数据。上游 supply id 为 `claude-opus-sankuai-2023` / `claude-sonnet-sankuai-2023`（美团内部别名），target_model 写的是 `claude-opus-5` / `claude-sonnet-5`。通常美团 aigc.sankuai.com 转发的是官方模型（能力一致），但内部服务可能施加窗口限制（如限制到 200k）。**需用户确认内部别名是否有窗口限制**。

填错后果：
- 过大（填 1M 但实际限制 200k）→ auto-compact 阈值 900k → 请求在 200k~900k 之间时上游溢出 → 硬伤
- 过小（填 200k 但实际 1M）→ auto-compact 阈值 180k → 过早 compact → 软伤（浪费 820k 窗口）
- 精确（填 1M 且实际 1M）→ auto-compact 阈值 900k → 合理

**如果不确定内部别名是否有窗口限制，保守填 `200000`**（取通用分析的最小值策略，软伤但不溢出）。

### 3.3 reasoning 字段

| 字段 | claude-opus | claude-sonnet | 依据 |
|---|---|---|---|
| `supports_reasoning_summary_parameter` | `true` | `true` | 让 codex 发 reasoning 字段；model_proxy 翻译为 anthropic thinking 字段 |
| `default_reasoning_summary` | `"auto"` | `"auto"` | codex 侧默认值 |
| `supported_reasoning_levels` | 全 5 档 | 全 5 档 | config 确认两个 supply 的 effort_enum 均为 `[low, medium, high, xhigh, max]` |
| `default_reasoning_level` | `"high"` | `"medium"` | opus 档默认高强度，sonnet 档中性默认；model_proxy remap 到 target 对应排名 |

`supported_reasoning_levels` 数组格式：
```json
[
  {"effort": "low", "description": "Low reasoning effort"},
  {"effort": "medium", "description": "Medium reasoning effort"},
  {"effort": "high", "description": "High reasoning effort"},
  {"effort": "xhigh", "description": "Extra-high reasoning effort"},
  {"effort": "max", "description": "Maximum reasoning effort"}
]
```

**reasoning 协议翻译链路确认**：
1. codex 发 `reasoning.effort`（OpenAI responses 协议）
2. model_proxy `ResponsesReasoningCodec.decode()` 解码为 `RawIntent(level=canonical)`
3. `capability.remap(source_capability, target_capability)` 相对映射到 target 序列
4. `AnthropicReasoningCodec.syntax_adapt()` 编码为 `thinking.type=adaptive` + `output_config.effort=<remapped_level>`
5. 发给 anthropic 上游

全链路无断点。catalog 填 `supports_reasoning_summary_parameter=true` 是正确选择——填 false 会导致 codex 不发 reasoning 字段，浪费 Claude opus-5/sonnet-5 的 thinking 能力。

### 3.4 其他契约层字段

| 字段 | 值 | 依据 |
|---|---|---|
| `shell_type` | `"default"` | codex 侧工具注册，与上游无关 |
| `apply_patch_tool_type` | `"freeform"` | Claude 指令遵循强，可用 freeform |
| `input_modalities` | `["text", "image"]` | route=claude 时真 Claude us-5/sonnet-5 支持视觉输入。**若 catalog 需跨 route 通用则保守填 `["text"]`** |
| `truncation_policy` | `{"mode": "bytes", "limit": 10000}` | codex 侧输出截断，与 fallback 默认一致 |
| `web_search_tool_type` | `"text"` | codex 侧工具注册 |
| `support_verbosity` | `false` | 减少弱模型指令负担（route=claude 时可填 true，但 false 保守通用） |
| `default_verbosity` | `null` | 不设默认 |
| `experimental_supported_tools` | `[]` | 不注册实验性工具 |
| `visibility` | `"list"` | 在 model picker 中可见 |
| `supported_in_api` | `true` | API 可用 |
| `priority` | `50` | 排序权重（与通用分析一致） |
| `availability_nux` | `null` | 无新手引导 |
| `upgrade` | `null` | 无升级提示 |
| `include_skills_usage_instructions` | `false` | 不含 skills 指令 |
| `include_plugin_usage_instructions` | `false` | 不含 plugin 指令 |
| `include_apps_usage_instructions` | `false` | 不含 apps 指令 |
| `supports_image_detail_original` | `false` | 不支持 image_detail=original |
| `supports_search_tool` | `false` | 不注册 search 工具 |
| `use_responses_lite` | `false` | 不用 lite 模式 |
| `effective_context_window_percent` | `95` | 有效窗口 95% |

### 3.5 claude-opus vs claude-sonnet 差异

| 字段 | claude-opus | claude-sonnet |
|---|---|---|
| `slug` | `"claude-opus"` | `"claude-sonnet"` |
| `display_name` | `"Claude Opus (via model_proxy)"` | `"Claude Sonnet (via model_proxy)"` |
| `description` | `"model_proxy opus tier (claude route)"` | `"model_proxy sonnet tier (claude route)"` |
| `default_reasoning_level` | `"high"` | `"medium"` |
| `context_window` | `1000000` | `1000000` |
| `supported_reasoning_levels` | 全 5 档（同） | 全 5 档（同） |
| `base_instructions` | prompt.md 全文（同） | prompt.md 全文（同） |
| 其余字段 | 相同 | 相同 |

差异点仅 3 个：slug、display_name/description、default_reasoning_level。base_instructions 不区分（共用一份通用 agentic 指令）。context_window 相同（两个模型都是 1M）。

### 3.6 catalog JSON 草稿

```json
[
  {
    "slug": "claude-opus",
    "display_name": "Claude Opus (via model_proxy)",
    "description": "model_proxy opus tier (claude route)",
    "default_reasoning_level": "high",
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
      "instructions_template": "{{BASE_INSTRUCTIONS}}",
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
    "context_window": 1000000,
    "max_context_window": 1000000,
    "auto_compact_token_limit": null,
    "effective_context_window_percent": 95,
    "experimental_supported_tools": [],
    "input_modalities": ["text", "image"],
    "supports_search_tool": false,
    "use_responses_lite": false,
    "node_repl_auto_review_required": false,
    "node_repl_disabled": false,
    "auto_review_model_override": null,
    "model_specialty": null,
    "tool_mode": null,
    "multi_agent_version": null
  },
  {
    "slug": "claude-sonnet",
    "display_name": "Claude Sonnet (via model_proxy)",
    "description": "model_proxy sonnet tier (claude route)",
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
      "instructions_template": "{{BASE_INSTRUCTIONS}}",
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
    "context_window": 1000000,
    "max_context_window": 1000000,
    "auto_compact_token_limit": null,
    "effective_context_window_percent": 95,
    "experimental_supported_tools": [],
    "input_modalities": ["text", "image"],
    "supports_search_tool": false,
    "use_responses_lite": false,
    "node_repl_auto_review_required": false,
    "node_repl_disabled": false,
    "auto_review_model_override": null,
    "model_specialty": null,
    "tool_mode": null,
    "multi_agent_version": null
  }
]
```

**`{{BASE_INSTRUCTIONS}}` 占位符说明**：实际使用时替换为 `codex-rs/models-manager/prompt.md` 全文（约 3389 词）。获取方式：

```bash
curl -sL https://raw.githubusercontent.com/openai/codex/main/codex-rs/models-manager/prompt.md
```

填入 JSON 时需将 prompt.md 内容作为 JSON 字符串值（注意转义换行符和引号）。

## 4. 风险与权衡

### 4.1 context_window 内部别名不确定性

官方公开数据：claude-opus-5 / claude-sonnet-5 = 1M tokens。但上游 supply 是 `claude-opus-sankuai-2023` / `claude-sonnet-sankuai-2023`（美团内部别名），可能施加窗口限制。

- **若内部无限制**：填 1M 精确，auto-compact 阈值 900k，合理
- **若内部限制为 200k**：填 1M → auto-compact 阈值 900k → 请求在 200k~900k 之间溢出 → 硬伤
- **保守做法**：填 200000，auto-compact 阈值 180k，软伤（浪费窗口但不溢出）

**需用户确认**：美团内部 `claude-opus-sankuai-2023` / `claude-sonnet-sankuai-2023` 的实际 context window 限制。若无法确认，建议保守填 `200000`。

### 4.2 input_modalities 跨 route 风险

route=claude 时填 `["text", "image"]` 精确（真 Claude 支持视觉）。但若 catalog 需跨 route 通用（切到 ds-pro/glm-5.2 时），这些上游不支持图像 → codex 注册 image 工具但上游拒绝。通用分析建议保守 `["text"]`。

**如果 catalog 仅用于 route=claude**：填 `["text", "image"]`。
**如果 catalog 需跨 route 通用**：填 `["text"]`。

### 4.3 base_instructions 对弱模型

prompt.md 是英文 agentic 指令。route=claude 时 Claude 完全能遵循。若 catalog 跨 route 用于 ds-pro/glm-5.2，弱模型可能不遵循——但 fallback 也发同样内容，不更差。

### 4.4 与通用分析的关系

本方案是通用分析在 route=claude 场景下的**精确化**：
- context_window 从"取最小值"升级为"填 1M"（有公开数据）
- input_modalities 从"保守 text"升级为"text+image"（真 Claude 支持视觉）
- base_instructions 从"填 prompt.md 或留空"收敛为"填 prompt.md 全文"（route=claude 无风险）

如果 catalog 需跨 route 通用，仍应回退到通用分析的保守填法。

## 5. 验证方式

1. **消 warning**：配好 catalog 后启动 codex（`model="claude-opus"` 或 `"claude-sonnet"`），观察启动日志是否还有 "Model metadata not found" warning
2. **wire 请求验证**：用 codex 发请求，在 model_proxy ACCESS 日志检查：
   - model 字段 = "claude-opus" / "claude-sonnet"（slug 正确）
   - reasoning.effort 字段存在（supports_reasoning_summary_parameter=true 生效）
   - instructions 字段存在且为 prompt.md 内容（base_instructions 生效）
   - model_proxy 日志里 thinking.type=adaptive + output_config.effort 存在（协议翻译成功）
3. **auto-compact 行为**：长会话中观察 codex 是否在 ~900k tokens 触发 compact（验证 context_window=1M 填值）
4. **reasoning remap 验证**：codex 发 `reasoning.effort=high` → model_proxy 日志确认 remap 到 target 序列的对应排名（claude supply 全 5 档，high 排名 2/4 → 映射 high）
5. **image 输入测试**：若填了 `["text","image"]`，用 codex 发一个含图像的请求，验证上游正常响应

## 6. 关联

- 通用分析：[[2026-08-17-codex-model-catalog通用填法分析]]
- model_proxy 档位映射：`core/server.py` L898-901 `_MODEL_TIER_MAP`
- model_proxy effort 映射：[[docs/REASONING.md]] §1-§6
- model_proxy anthropic codec：`core/reasoning/codecs.py` `AnthropicReasoningCodec`
- model_proxy 配置：`config/model_proxy_config.json` L11-12（claude supply 定义）
- codex ModelInfo 源码：`codex-rs/protocol/src/openai_models.rs`（struct 定义）、`codex-rs/models-manager/src/model_info.rs`（fallback 逻辑）、`codex-rs/models-manager/prompt.md`（BASE_INSTRUCTIONS，约 3389 词）

---

## 7. 复核结论（独立第二意见，针对 §1-§6 的 claude-opus/sonnet 方案）

### 7.1 无硬伤

对照源码事实逐字段核验，claude-opus/sonnet 档方案**无硬伤**：

- **字段名清单**：JSON 草稿用到的全部字段名与 ModelInfo struct 一致（slug/display_name/description/default_reasoning_level/supported_reasoning_levels/shell_type/visibility/supported_in_api/priority/availability_nux/upgrade/model_messages/include_*/supports_reasoning_summary_parameter/default_reasoning_summary/support_verbosity/default_verbosity/apply_patch_tool_type/web_search_tool_type/truncation_policy/supports_image_detail_original/context_window/max_context_window/auto_compact_token_limit/effective_context_window_percent/experimental_supported_tools/input_modalities/supports_search_tool/use_responses_lite/node_repl_*/auto_review_model_override/model_specialty/tool_mode/multi_agent_version）。无幻觉字段名。
- **model_messages 子字段**：草稿显式列出全部 8 个子字段（instructions_template/instructions_variables/approvals/collaboration_modes/auto_review/permissions/multi_agent/token_budget）。若 ModelMessages struct 这些字段是 Option<T> 且无 `#[serde(default)]`，则 key 必须存在（可 null）——草稿满足此约束。
- **context_window=1M**：Anthropic 官方文档确认 claude-opus-5/sonnet-5 均为 1M tokens。内部别名窗口限制风险已在 §4.1 标注。
- **supported_reasoning_levels 全 5 档**：config L11-12 确认两个 supply 的 effort_enum 均为 `["low","medium","high","xhigh","max"]`，与 catalog 一致。strategy（L49-50）的 source_capability opus/sonnet 也是全 5 档，三方一致。
- **default_reasoning_level**：opus=high、sonnet=medium，合理（档位语义匹配）。
- **apply_patch_tool_type=freeform**：Claude 指令遵循强，合理。
- **input_modalities=["text","image"]**：route=claude 真 Claude 支持视觉，正确。跨 route 风险已在 §4.2 标注。

### 7.2 唯一需用户确认的事项

context_window 内部别名（claude-opus-sankuai-2023 / claude-sonnet-sankuai-2023）是否有窗口限制。若无法确认，保守填 200000（§4.1 已给出降级方案）。用户已确认填 1M，接受此风险。

### 7.3 JSON 结构合法性

草稿 JSON 结构合法，应能通过 serde 反序列化（无 deny_unknown_fields，未知字段静默忽略）。必填字段（非 Option 且无 `#[serde(default)]`）均已赋值。可 null 的必填 key（availability_nux/upgrade/default_verbosity/model_messages 子字段等）均显式列出了 null 值。

---

## 8. haiku 档填法（按 ds-flash，核心新增）

### 8.1 关键事实修正：ds-flash 支持 reasoning

任务描述称"ds-flash effort_enum=None（不支持 reasoning）"，但经核验 config `model_proxy_config.json` L27-32，**ds-flash supply 的 effort_enum 实际为 `["high"]`（一档），不是 None/空**。同时 `core/reasoning/codecs.py` L99-100 注释明确记载：

> "deepseek-v4-flash-tencent 三个模型的 adaptive 语法亦经真实探测确认可用（全部 200 且真实产生 thinking 内容）"

即 ds-flash **支持 reasoning**（thinking.type=adaptive + output_config.effort=high），经真实探测确认。任务描述的前提有误。

### 8.2 supports_reasoning_summary_parameter 填 false 能否"避开问题"？

假设 ds-flash 真的 effort_enum=[]（空列表），分析两条路径：

| catalog 设置 | codex 行为 | model_proxy 链路 | 是否报错 |
|---|---|---|---|
| `false` | 不发 reasoning 字段 | decode → present=False → remap → ABSENT → syntax_adapt 返回 {} → 不增删字段 | 不报错 |
| `true` | 发 reasoning.effort | decode → present=True → remap → target 空 → **STRIP** → 删除 thinking/output_config | 不报错 |

**结论**：即使 target effort_enum 为空，model_proxy 的 STRIP 机制（`capability.py` L196-197）会安全清理 reasoning 字段，**不会报错**。catalog 填 false 和填 true 都不报错——但 false 会浪费 reasoning 能力，true 会触发 STRIP（多余但无害）。

**实际场景**（ds-flash effort_enum=["high"]）：

| catalog 设置 | codex 行为 | model_proxy 链路 | 结果 |
|---|---|---|---|
| `false` | 不发 reasoning | ABSENT → {} | ds-flash 不思考，**浪费 reasoning 能力** |
| `true` | 发 reasoning.effort | remap 到 "high"（n=1 塌缩）→ THINKING → thinking.type=adaptive+effort=high | ds-flash 正常思考，**合理** |

**推荐 `true`**：ds-flash 支持 reasoning，应让 codex 发 reasoning 字段，model_proxy remap 到 "high"。

### 8.3 haiku 档填法

| 字段 | 值 | 依据 |
|---|---|---|
| `slug` | `"claude-haiku"` | 匹配 `_MODEL_TIER_MAP` L901 |
| `display_name` | `"Claude Haiku (via model_proxy)"` | UI 标识 |
| `description` | `"model_proxy haiku tier (ds-flash, all routes)"` | haiku 档所有 route 均为 ds-flash |
| `supports_reasoning_summary_parameter` | `true` | ds-flash effort_enum=["high"]，支持 reasoning（§8.1） |
| `default_reasoning_summary` | `"auto"` | codex 侧默认值 |
| `supported_reasoning_levels` | `["low","medium","high","max"]` | 匹配 strategy L50 `tiers_source_capability.haiku`。model_proxy remap 到 ds-flash 的 "high"（n=1 塌缩，全部映射到 high） |
| `default_reasoning_level` | `"medium"` | haiku 档默认中等 reasoning；model_proxy remap medium→high（ds-flash effort_enum=["high"]，n=1 塌缩到 high） |
| `context_window` | `200000` | 用户指定 200k（保守值，软伤不溢出）。DeepSeek V4 虽原生 1M，但 haiku 档按保守填 |
| `max_context_window` | `200000` | 同上 |
| `auto_compact_token_limit` | `null` | null → codex 用 context_window * 0.9 = 180k 自动 compact |
| `apply_patch_tool_type` | `"freeform"` | 注册 freeform apply_patch（用户指定）。ds-flash 指令遵循弱（D3=0 评级），实际遵循度待实测，但先启用 |
| `input_modalities` | `["text"]` | ds-flash 图像支持不明，保守 text-only |
| `instructions_template` | `"{{BASE_INSTRUCTIONS}}"` | prompt.md 全文。ds-flash 指令遵循弱，但 fallback 机制本就发这份 prompt，显式填不更差，消 warning |
| `shell_type` | `"default"` | 契约层，与上游无关 |
| `truncation_policy` | `{"mode":"bytes","limit":10000}` | codex 侧行为 |
| `web_search_tool_type` | `"text"` | codex 侧工具注册 |
| `support_verbosity` | `false` | 减少弱模型指令负担 |
| `default_verbosity` | `null` | 不设默认 |
| `visibility` | `"list"` | picker 可见 |
| `supported_in_api` | `true` | API 可用 |
| `priority` | `50` | 排序权重 |
| `availability_nux` | `null` | 无新手引导 |
| `upgrade` | `null` | 无升级提示 |
| `experimental_supported_tools` | `[]` | 不注册实验工具 |
| `supports_image_detail_original` | `false` | 不支持 |
| `supports_search_tool` | `false` | 不注册 |
| `use_responses_lite` | `false` | 不用 lite |
| `effective_context_window_percent` | `95` | 有效窗口 95% |
| 其余字段 | 同 opus/sonnet 档 | node_repl_*/auto_review_model_override/model_specialty/tool_mode/multi_agent_version 等 |

### 8.4 haiku 档 JSON 草稿

```json
{
  "slug": "claude-haiku",
  "display_name": "Claude Haiku (via model_proxy)",
  "description": "model_proxy haiku tier (ds-flash, all routes)",
  "default_reasoning_level": "medium",
  "supported_reasoning_levels": [
    {"effort": "low", "description": "Low reasoning effort"},
    {"effort": "medium", "description": "Medium reasoning effort"},
    {"effort": "high", "description": "High reasoning effort"},
    {"effort": "max", "description": "Maximum reasoning effort"}
  ],
  "shell_type": "default",
  "visibility": "list",
  "supported_in_api": true,
  "priority": 50,
  "availability_nux": null,
  "upgrade": null,
  "model_messages": {
    "instructions_template": "{{BASE_INSTRUCTIONS}}",
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
  "context_window": 200000,
  "max_context_window": 200000,
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

### 8.5 三档差异对比

| 字段 | claude-opus | claude-sonnet | claude-haiku |
|---|---|---|---|
| slug | `claude-opus` | `claude-sonnet` | `claude-haiku` |
| 上游模型 | claude-opus-5 | claude-sonnet-5 | deepseek-v4-flash |
| 随 route 变 | 是（切 route 换模型） | 是 | **否**（所有 route 都是 ds-flash） |
| context_window | 1000000 | 1000000 | 200000（用户指定保守值） |
| supported_reasoning_levels | 全 5 档 | 全 5 档 | 4 档 [low,medium,high,max] |
| default_reasoning_level | high | medium | medium（remap 塌缩到 high） |
| apply_patch_tool_type | freeform | freeform | freeform（用户指定启用；ds-flash 遵循度待实测） |
| input_modalities | [text,image] | [text,image] | **[text]**（保守） |
| supports_reasoning_summary_parameter | true | true | true |
| instructions_template | prompt.md | prompt.md | prompt.md |
| 其余字段 | 相同 | 相同 | 相同 |

### 8.6 context_window 查证结果

百度搜索（2026-08-13/17 多源）确认：**DeepSeek V4 全系列（Pro 和 Flash）均原生支持 1,000,000 Token（1M）上下文窗口**。V3.x/V3.2 为 128K，V4 升级到 1M。

- DeepSeek V4 Pro 正式版（0813）：1M 上下文，384K 最大输出
- DeepSeek V4 Flash：1M 上下文（同系列标配），并发限制 2500（vs Pro 的 500）

**不确定性**：`deepseek-v4-flash` 是美团内部别名（target_model），公开数据查的是 DeepSeek 官方模型。美团内部服务 `aigc.sankuai.com` 转发的通常是与官方能力一致的模型，但可能施加限制。同 §4.1 的内部别名风险。

**若保守填**：可填 128000（V3.x 的窗口值，即 V4 的下限已知值），auto-compact 阈值 115k，软伤（浪费 872k 窗口但不溢出）。但考虑到 V4 全系列标配 1M，填 1M 的风险低于 claude 档（claude 的内部别名更可能有窗口限制）。

### 8.7 instructions_template 对 ds-flash 的评估

prompt.md 是为强模型写的 agentic coding 指令。ds-flash 指令遵循能力弱于 Claude/GPT，可能不完全遵循。但：

1. fallback 机制（不配 catalog 时）本就发这份 prompt.md
2. 显式填只是消 warning，不改变实际发给上游的内容
3. 不填（留 null）会让 codex 打 warning 且返回空指令，上游完全没有 agentic 引导

**结论**：填 prompt.md 全文。不比 fallback 更差，消 warning。

---

## 9. 三档 catalog 整体一致性

### 9.1 slug 匹配

三条 slug（claude-opus / claude-sonnet / claude-haiku）均匹配 `_MODEL_TIER_MAP`（L898-902）。codex config.toml 切 `model="claude-haiku"` 时，catalog 查表命中 haiku 条目，model_proxy `_MODEL_TIER_MAP` 映射到 haiku tier，选 ds-flash supply。链路完整。

### 9.2 三档差异合理性

| 维度 | opus/sonnet | haiku |
|---|---|---|
| 上游 | 真 Claude（随 route 变） | ds-flash（不随 route 变） |
| reasoning | 全 5 档，effort_enum 全 5 档 | 4 档，effort_enum=["high"]（remap 塌缩到 high） |
| context_window | 1M（官方确认） | 200k（用户指定保守值；V4 实际 1M 但保守填） |
| apply_patch | freeform（强模型） | freeform（用户指定启用；遵循度待实测） |
| image | 支持（真 Claude） | 不支持（保守） |

差异：haiku 档是"小快省"定位（ds-flash 并发 2500 vs Pro 500），reasoning 档位收窄、image 不支持；apply_patch 与 opus/sonnet 同启 freeform（用户指定），但 ds-flash 弱模型的实际遵循度待实测。

### 9.3 切档位时 catalog 可用性

codex config.toml 切 `model` 值为三档之一，catalog 都能命中对应条目。model_proxy 侧 `_MODEL_TIER_MAP` 三档均有 supply 定义（haiku → ds-flash）。切档不切 route，切 route 不切档（haiku 档跨 route 不变）。无断裂。

### 9.4 reasoning remap 链路验证（haiku 档）

codex 发 `reasoning.effort=medium`（default_reasoning_level）→ ResponsesReasoningCodec.decode() → RawIntent(level=MEDIUM, present=True) → remap(source_cap=["low","medium","high","max"], target_cap=["high"]) → src_think 4 档、tgt_think 1 档 → n=1 塌缩 → remap_rank=0 → tgt_think[0]=HIGH → TargetEffort(level=HIGH) → abstract_encode → THINKING → AnthropicReasoningCodec.syntax_adapt(adaptive) → `{"thinking":{"type":"adaptive"},"output_config":{"effort":"high"}}` → 发给 ds-flash 上游 → 200 + thinking 内容（已探测确认）。

全链路无断点。

---

## 10. haiku 档验证方式

1. **消 warning**：codex `model="claude-haiku"` 启动，确认无 "Model metadata for claude-haiku not found" warning
2. **reasoning wire 验证**：codex 发请求，model_proxy ACCESS 日志检查 reasoning.effort 存在（supports_reasoning_summary_parameter=true 生效）
3. **remap 验证**：codex 发 `reasoning.effort=medium`（default_reasoning_level）→ model_proxy 日志确认 remap 到 `high`（4→1 塌缩）
4. **thinking 验证**：ds-flash 上游响应含 thinking 内容（codecs.py L99-100 已确认 adaptive 语法可用）
5. **apply_patch 验证**：codex 编辑代码时使用 freeform apply_patch（apply_patch_tool_type=freeform 注册）→ 验证 ds-flash 能否遵循该工具调用（遵循度待实测）
6. **auto-compact**：长会话观察 ~180k 触发 compact（context_window=200k）
7. **切 route**：切 route 后 codex `model="claude-haiku"` 发请求 → 仍是 ds-flash（haiku 不随 route 变）→ 正常响应

## 11. 关联（更新）

- 通用分析：[[2026-08-17-codex-model-catalog通用填法分析]]
- model_proxy 档位映射：`core/server.py` L898-901 `_MODEL_TIER_MAP`
- model_proxy effort 映射：[[docs/REASONING.md]] §1（effort_enum 四种写法/STRIP/DISABLED）、§6（映射算法）
- model_proxy reasoning 链路：`core/reasoning/capability.py` L178-220（remap）、L196-197（target 空 → STRIP）、`core/reasoning/codecs.py` L99-100（ds-flash adaptive 确认可用）
- model_proxy 配置：`config/model_proxy_config.json` L27-32（ds-flash supply 定义，effort_enum=["high"]）、L50（codex strategy haiku source_capability）、L43-46（routes haiku 均为 ds-flash）
- DeepSeek V4 上下文：百度搜索 2026-08-13/17，V4 全系列（Pro+Flash）1M 上下文
