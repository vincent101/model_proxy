---
type: design-decision
status: draft
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, reasoning, max_tokens, truncation, protocol-consistency, effort-mapping]
---

# reasoning 模型经 model_proxy 的 thinking 截断与协议不一致：根因与理想治理方案

## 背景与问题

model_eval 评估体系（`tools/model_eval/`）经 model_proxy（`tools/model_proxy/`，127.0.0.1:18889）向多个 reasoning 上游跑 12 道固定考题，反复出现两个相关问题：

- **现象 1（stop=max_tokens 截断，thinking 占满正文）**：ds-flash@max 以 max_tokens=16000 发起，Q4/Q5/Q6/Q10 首轮 `stop=max_tokens`，thinking 占满整个预算（Q4 thinking 66170 字符、Q6 79287、Q1 51871），text block 完全缺失；提到 32000 后才正常 end_turn。glm-5.2@max 首轮 max_tokens=4096 时 output_tokens 被 thinking 完全占满（4097/4096），正文为空。
- **现象 2（responses 协议下 thinking 完全缺失 + effort 疑似未生效）**：同模型 glm-5.2 同 effort=max，anthropic 协议（`glm-52-sankuai-3339`）有 thinking（Q10 th_chars=14070）、模型自述"1.0 最高级深度思考"；responses 协议（`glm-52-sankuai-openai-3339`）12 题 th_chars 全 0、模型自述"中等水平"。

本记录逐一根因定位（全部经代码/数据实证，不凭空假设），并给出 [理想] 路径的系统性治理方案。

---

## 根因分析（四问逐一）

### Q1：stop=max_tokens 是什么？max_tokens 限制的是 thinking+text 合计还是仅 text？

**`stop_reason=max_tokens` 的确切语义**：API 在生成达到 `max_tokens` 上限时强制截断，返回已生成内容（可能只有 thinking、正文为空）。与 `end_turn`（模型自然结束）相对，`max_tokens` 是**外部配额耗尽的硬截断**，不代表模型说完了。

**关键事实：anthropic 协议下 `max_tokens` 限制的是 output_tokens 总量 = thinking + text 合计，thinking 与正文共享同一预算。** 证据：

- ds-flash@max max_tokens=16000 时 Q4/Q5/Q6/Q10 text 缺失——thinking 独占 16000，正文挤出不来；提到 32000 才 end_turn（报告 `ds-flash-sankuai-3339-max-20260807.md` §五）。
- glm-5.2@max max_tokens=4096 时 output_tokens=4097/4096 全部耗在 thinking，正文空字符（`calibration.md` 2026-07-27 补测记录第 2 条）；"不是模型能力问题，是测试脚本预算设置过小"。
- anthropic 协议里 thinking 块（`thinking.type=enabled/adaptive`）产生的 token 计入 `output_tokens`，**没有独立的 thinking 预算字段**从客户端侧把两者隔开（`budget_tokens` 是 thinking 的内部引导上限，不是与 max_tokens 并列的独立配额；且 adaptive 语法根本不暴露 budget_tokens）。客户端只给一个 `max_tokens`，上游把 thinking+text 一起往里塞，塞满即截。

**三协议在此问题上的差异**：

| 协议 | 输出预算字段 | thinking 是否独占预算 | 截断信号 |
|---|---|---|---|
| anthropic `/v1/messages` | `max_tokens` | thinking+text **共享同一 max_tokens** | `stop_reason=max_tokens` |
| openai-chat | `max_completion_tokens` | reasoning_content+content 共享同一上限 | `finish_reason=length`（→ 代理映射为 max_tokens，translate.py:64） |
| openai-responses | `max_output_tokens` | reasoning+output 共享同一上限 | `status=incomplete` / `incomplete_details.reason=max_output_tokens` |

三协议在这一点上**本质相同**：都是"思考与正文共享同一个总额上限"，没有任何一家把 thinking 单独配额化。差异只在字段名与截断信号的形态。model_proxy 的转换层（translate.py:437-438、1675-1676）对这三者都是**原样改数值不改语义**。

### Q2：为什么 thinking 会占满？根因在模型侧 / 协议侧 / proxy 侧？

**三方叠加，但可治理的杠杆在 proxy 侧**。逐层拆：

- **模型侧（诱因，不可控）**：reasoning 模型 max 档 thinking 量天然巨大且不收敛可预期。ds-flash@max Q6 thinking 79287 字符（约 20k+ tokens）、Q4 66170——单次思考的 token 消耗能轻松超过一个"对普通对话够用"的 max_tokens。这是 reasoning 模型的固有行为，无法也不应靠 proxy"压缩"。
- **协议侧（放大器）**：如上，anthropic 把 thinking 计入 output_tokens 且不给独立预算，于是 thinking 与正文抢同一池子，thinking 先到时正文颗粒无收。这是协议设计，proxy 改不了上游。
- **proxy 侧（本可兜底、当前缺位，是真正可治理的根因）**：**model_proxy 对 max_tokens 完全透传，无任何钳位、无 thinking 独立预算、无"截断且正文缺失"的检测重试**。实证：
  - `core/server.py` 全文 `grep max_tokens` 只有协议识别注释（line 550-551）和 responses→anthropic 的兜底默认值（line 1238 `max_tokens_default=4096`），**转发主链路对客户端给的 max_tokens 一字不改直接发出**。
  - 三处转换只改字段名（translate.py:437-438 `max_tokens→max_completion_tokens`、1675-1676 `max_tokens→max_output_tokens`），数值原样。
  - 唯一的"处理"是反向（responses→anthropic）客户端不传时兜底 4096——**这个 4096 正是 glm-5.2 首轮 4096 截断的直接来源之一**（若调用方不显式给 max_tokens，proxy 静默塞一个对 max 档远远不够的小预算）。
  - 已有的 chat 空回答兜底（translate.py:69-72 `_ENABLE_REASONING_FALLBACK`，把 reasoning_content 填进 text 防客户端收空 content）只覆盖 **chat 协议**且只在"已有 reasoning_content 但无 text"时触发；对 anthropic/responses 协议、以及"thinking 占满导致的 max_tokens 截断"这种**请求侧预算不足**的根因，完全不触及——它是症状缓解，不是预算治理。

**结论**：thinking 占满的根因是"reasoning 模型 thinking 量大（模型侧）× thinking 与正文共享 max_tokens（协议侧）× proxy 不透传外的任何预算治理（proxy 侧缺位）"。前两者不可改，proxy 侧的预算治理缺位是理想方案的主攻点。

### Q3：responses 协议 thinking 缺失 + effort 疑似未生效——转换发生了什么？

经实测复算（非脑推），这是**两个独立的 defect 叠加**，都在 model_proxy 转换层，与"上游不回传"无关：

**Defect A：effort=max 在 chat/responses 域被静默降级为 "medium"（实锤，根因已修正）**

链路：入站 anthropic `output_config.effort=max` → `AnthropicReasoningCodec.decode` → level=MAX；`remap`（source `tiers_source_capability=[low,medium,high,xhigh,max]` 与 target glm `effort_enum=[high,max]`）正确落到 target MAX（level=6）。问题出在 `ResponsesReasoningCodec.syntax_adapt` 的最后一公里：

- `_canonical_to_openai_effort_name(MAX)` = `_CANONICAL_TO_CHAT_NAME.get(MAX, "medium")`（codecs.py:185-190）
- `_CANONICAL_TO_CHAT_NAME` 由 `_CHAT_NAME_TO_CANONICAL={none,low,medium,high,xhigh}` 反转（codecs.py:175-182），**写死、不含 max**（反映 openai 原生 chat/responses 协议规范）
- MAX 查表落空，兜底返回 `"medium"`

实测复算确认：`responses wire: {'reasoning':{'effort':'medium'}}`，对照 anthropic 协议版 `{'output_config':{'effort':'max'}}`。

**根因修正（经实测+全档推演，推翻原"兜底策略选错/应钳 xhigh"的判断）**：

- **写死字典 `_CHAT_NAME_TO_CANONICAL` 这一层本身就不该存在**。它反映 openai 原生 chat/responses 协议规范（无 max），但 aigc.sankuai.com 的 chat/responses 端点是网关给各家模型（glm/kimi/ds）用的，支持的档位由模型 supply 决定，不是 openai 原生规范。
- **supply 的 `effort_enum`（如 `[high,max]`）就是上游真实支持的 wire 档名字符串**——这是权威来源。代理该信配置发档名，不该用一张写死的、比 supply 配置窄的字典二次过滤。
- 实测证据（直接 curl 上游网关，绕开 model_proxy）：
  - responses 端点（glm-52-sankuai-openai-3339）：`reasoning.effort=max` → HTTP 200 + 模型自述"最高档" + rt=578 ✓
  - chat 端点（kimi-k3-sankuai-openai-3339）：`reasoning_effort=max` → HTTP 200 + 模型自述"max(最高档)" + reasoning_content 756字符 ✓
  - 两个网关端点都真实接受 max，写死字典挡了一个真实存在的档。
- 对比 anthropic 域字典 `_ANTHROPIC_NAME_TO_CANONICAL`（codecs.py:74-81）**含 max**——所以 anthropic 协议 target 正常发 max，chat/responses target 被降 medium。**两个域字典覆盖范围不同是直接原因**。

**结论**：Defect A 不是"兜底值选错"（原方案"钳到 xhigh"是治标），是**写死字典这一层不该存在**——supply 配置才是上游真实能力的权威，encode 该信 supply 直接发档名。

**附带发现（decode 对称 bug）**：decode（入站）也查同一写死字典。responses/chat SDK 发 `effort=max` → `Chat/ResponsesReasoningCodec.decode` 查 `_CHAT_NAME_TO_CANONICAL` 不认 max → present=False → max 意图被丢（推演组合 C/D 的 max，见验证）。anthropic SDK（adaptive/enabled）decode 用 `_ANTHROPIC_NAME_TO_CANONICAL`（含 max）+ budget 锚点（含 max），不受影响。**source 侧也被同一写死字典挡了 max，与 target 侧同源**。

**Defect B：responses→anthropic 方向根本没实现 reasoning→thinking 的回传（实锤）**

即使上游真回了 reasoning 内容，anthropic 客户端也看不到：

- 非流式 `responses_to_anthropic_response`（translate.py:1752-1753）：`elif it == "reasoning": pass  # 丢弃，对称反向丢 thinking`。
- 流式 `ResponsesToAnthropicStreamAdapter.feed`（translate.py:1906-1926）：`response.output_item.added` 只处理 `message`/`function_call` 两种 item，**无 `reasoning` 分支**；也无 `response.reasoning_summary_text.delta` 事件处理。grep 该区间 reasoning/thinking 全部命中（1752、1766-1768、1960）都只是读 `usage.output_tokens_details.reasoning_tokens` 做 token 统计，**thinking 内容块本身被丢弃**。
- 对比：anthropic→responses 方向（`AnthropicToResponsesStreamAdapter`，translate.py:1418-1456）**有**完整的 `_start_reasoning_item`/`_stop_reasoning_item`/`thinking_delta→reasoning_summary_text.delta`。**两个方向不对称**——这就是 12 题 th_chars 全 0 的机制。

**两个 defect 的叠加效应**：Defect A 让 responses 上游收到的 effort 是 medium（思考强度被调低），Defect B 让 anthropic 客户端即便上游有 reasoning 输出也收不到（thinking 内容块被丢）。报告观察到的"thinking 完全缺失 + 自述中等"两个症状分别由 B 和 A 解释。两者都是 **model_proxy 转换层缺陷**，不是上游网关不回传。

**维度画像为何仍一致**：因为 glm-5.2 基础能力足够，即使 effort 被降到 medium、thinking 不可见，仅凭 text 仍能在 12 题拿高分——这恰恰说明**当前评估在 responses 协议下测的不是真 max 档能力**（报告 verdict 已如实标注这一局限）。

### Q4：如何系统性解决（理想路径）？

见下方"方案设计"。

---

## 方案设计（理想路径，按 6 维组织）

总原则：**把"thinking 预算治理"和"协议一致性"从隐式散布状态，上收为 model_proxy 的显式、可配置、可观测的一等职责**。不计迁移成本，追求架构合理与长期可扩展。

### ① 协议转换层（translate.py / reasoning/codecs.py / reasoning/ladder.py）

**1a. 修复 Defect A——canonical→chat/responses 档名映射不得静默降级。**
- 新增"档名映射溢出"的显式处理策略，替代当前 `.get(level, "medium")` 的静默兜底。三选一并可配置，默认推荐"向上钳到该域最高可用思考档"：MAX→`"xhigh"`（而非 medium）。理由：客户端表达了"要最强思考"，降级到 medium 是反向违背意图，向上钳到域内最高（xhigh）最贴近原意图；XML/配置错误应暴露而非吞噬。
- 在 `codecs.py` 给 `_canonical_to_openai_effort_name` 增加 overflow 分支：命中不了时按 `_CANONICAL_TO_CHAT_NAME` 已注册键里 ≤ level 的最高者取值；level 高于所有键则取最高键（xhigh）。同时在该处记 `logger.warning`（"effort X 超出 responses 域词表，已钳到 xhigh"），让降级可见。
- **配套**：`registry.py` 增一个"supply capability 与协议域词表一致性"启动期校验——若 supply 是 responses/chat 协议但其 `effort_enum` 含 `max`/`minimal`（域外档名），启动/重载时 warning 提示该档在 wire 层会被钳位，从配置源头杜绝"配置写了 max 但域里不存在"的隐性漂移。

**1b. 修复 Defect B——补齐 responses→anthropic 的 reasoning→thinking 回传，实现双向对称。**
- `ResponsesToAnthropicStreamAdapter.feed` 增加 reasoning item 分支：`response.output_item.added` 遇 `item.type=="reasoning"` 开一个 anthropic `thinking` block；`response.reasoning_summary_text.delta` → `thinking_delta`；对应 `.done` → `content_block_stop`。产出与 anthropic 原生 thinking 块同构，使 anthropic 客户端经 responses 上游也能看到 thinking。
- 非流式 `responses_to_anthropic_response`：把 `it=="reasoning"` 的 `summary[].text` 拼成 `{"type":"thinking","thinking":...}` block 放进 content（而非 `pass`）。
- 对称性原则：anthropic→responses 已有 reasoning 回传（`_start_reasoning_item`），responses→anthropic 必须补齐镜像，消除"同模型不同协议接入 thinking 可见性不同"。

**1c. 抽象层补强**：`ladder.py` 的 canonical 全序已是跨协议统一表示，但 chat/responses 域词表是其真子集。在各 codec 顶部用注释 + 单测固化"该 codec 域词表 = canonical 的哪个子集、溢出如何处理"，避免后续新增档（如未来 MAX+1）再次落到静默兜底。

### ② 入站参数处理（server.py 对 max_tokens 的解析与钳位、effort 映射）

**2a. max_tokens 不再无条件透传，引入"理解 thinking 的出站预算治理"。**
- server.py 在出站前对 max_tokens 做一次**显式解析与条件性放大**：当判定本次请求将产生 thinking（target_cap 有真实思考档且 remap 结果为 THINKING，非 DISABLED/STRIP）时，若客户端 max_tokens 低于该 supply 的"thinking 安全下限"（见 ③），自动放大到 `max(客户端值, supply 该 effort 档的建议下限)`，并在 ACCESS 日志记 `budget_raised=<old>→<new>`。客户端显式给的更大值优先（不向下钳）。
- 反向（responses→anthropic）的 `max_tokens_default=4096` 兜底：对 max/high 档 reasoning 上游，4096 是陷阱默认值。改为按 supply 能力读"该 effort 档默认预算"（见 ③），无配置时才退回一个保守全局值，并对 reasoning 请求与非 reasoning 请求用不同默认。

**2b. effort 映射现状已带来的另一个隐患**：source 默认 5 档（`_DEFAULT_ENUM`）不含 MAX/MINIMAL，导致 target 声明 [high,max] 时，source 侧"max 意图"需先 clamp 到 XHIGH 再 remap（Q3 中 i=3=clamp 后的 XHIGH rank）。本例结果巧合正确（仍映射到 target MAX），但语义脆弱。**理想做法**：source 能力（`tiers_source_capability`）应能声明完整 7 档含 MAX，临时 eval strategy 也应显式配置，避免依赖 clamp 的偶然对齐。这属于配置规范而非代码改动，但要在 README/SOP 写清"eval strategy 的 source capability 必须覆盖到被测 effort 档"。

### ③ 运维默认值（不同模型/effort 档的 max_tokens 合理默认）

**3a. 在 supply 配置引入结构化预算档**：每条 reasoning supply 增加可选字段，按 effort 档声明建议的最小/默认 max_tokens：
```jsonc
"output_budget": {
  "default": 16000,          // 该 supply 非 thinking 或低档的默认
  "by_effort": { "high": 32000, "max": 48000 },  // 按档覆盖
  "min_for_thinking": 32000  // 凡产生 thinking 时的安全下限
}
```
- 来源：用真实 thinking 量分布标定。ds-flash@max thinking 峰值约 79k 字符（~20k+ tokens）+ 正文，建议 max 档 min_for_thinking ≥ 48000（留正文余量）；glm-5.2@max thinking 峰值 14k 字符（~4k tokens），min_for_thinking ≥ 12000。**按 supply 单独标定，不搞全局一刀切**。
- server.py ②a 的放大逻辑、反向兜底的 ②a 默认值，都读这张表。

**3b. 评估侧默认值同步**：model_eval 的 SOP（README §SOP）应改为"按 supply output_budget 取该 effort 档预算"，替代当前手工试 16000→32000 的试错。

### ④ 重试/兜底策略（检测到 stop=max_tokens 且正文缺失时自动重试）

**4a. 响应侧截断检测**：非流式 anthropic 响应转换/透传收口处，检测 `stop_reason=="max_tokens"` 且 content 中无 text block（只有 thinking 或全空）→ 判定"预算被 thinking 耗尽、正文缺失"。流式在 message_delta 的 stop_reason=max_tokens 且全程未产出 text block 时同理。

**4b. 自动放大重试（有限次、不rotate supply、不计 failover）**：命中 4a 且 thinking 仍在持续产出（说明正文本可有、只是预算不够）时，自动把 max_tokens 按阶梯放大（如 ×2，封顶 supply 配置上限）重发一次。语义对齐现有 reasoning 语法自适应重试（server.py:1290-1298，单次、不 cooldown、不进 tried_set），新增一条独立的 `_budget_retried` 位，与语法重试互不干扰。重试仍失败才返回截断响应（如实 stop_reason=max_tokens），并在 ACCESS 记 `budget_truncated=1`。
- 与现有 chat `_ENABLE_REASONING_FALLBACK` 的关系：④处理的是"请求侧预算不足"（根因），fallback 处理的是"已有 reasoning 但无 text"（症状）。两者正交，保留 fallback 作为 chat 协议的最后兜底，但有了 ③④后 chat 场景的 4096 级截断应先被预算放大防住。

### ⑤ 监控告警（thinking/output 占比、effort 生效性）

**5a. thinking/output 占比指标**：ACCESS 日志与 totals 账本目前只记 `usage_in`/`usage_out`（server.py:1348-1354，reasoning token 已被 2026-07-24 有意移除，见其设计记录）。理想方案不违背该"不单独统计 reasoning"决策，但**新增两个派生观测维度**（不记账、只进 ACCESS 瞬时值）：
  - `budget_raised`（②a 触发时）、`budget_truncated`（④b 失败时）、`budget_retried`（④b 重试时）三个事件字段，能让"thinking 占满"从"事后看原始回答才发现"变成"日志可直接 grep"。
  - 可选：`stop_reason` 字段（end_turn/max_tokens/...）进 ACCESS，占比统计 max_tokens 出现频率作为"预算是否普遍偏小"的信号。

**5b. effort 生效性指标**：Defect A 这类"wire 层降级"要有可见性。①a 的 overflow warning 之外，可在 reasoning debug 旁路日志（server.py:809-856）的 wire dump 里**固定记录最终发给上游的档名字符串**，并使 `MODEL_PROXY_REASONING_DEBUG=1` 时能一眼看到"intent=max → wire effort 实际值"。更进一步：smoke test / 评估 SOP 增加一条"读模型自述档位 ≠ 请求档位则告警"的校验（本轮正是靠模型自述"中等"发现的）。

### ⑥ 协议一致性（确保同模型不同协议接入行为一致）

**6a. 对称性作为强约束**：确立"同一 (target_model, canonical effort) 经任一协议转换后，到达上游的有效思考强度、以及回到客户端的 thinking 可见性，应行为一致"为 model_proxy 的不变量。①a/①b 是把当前违反该不变量的两处补齐。

**6b. 一致性自检能力**：新增一个轻量自检路径——对同一 supply 的 anthropic / responses 两种协议入口，发同一探针（固定 prompt + 显式 canonical effort），比对两侧的 (a) 上游实际收到的 effort 档名 (b) 客户端是否收到 thinking 内容。可作为 `supply test`（CLI 既有 [t]est，README §5.5）的扩展子项"effort 一致性探测"，也可由评估体系在接入新模型时调用。本轮 glm 双协议对比就是人工版的这个探测，应工具化、自动化。

---

## 风险与权衡

- **迁移/落地代价提示（[理想] 路径不计成本，仅供知情）**：
  - ①b（responses→anthropic 补 reasoning 回传）是真实功能开发，要在流式状态机里正确管理 thinking block 的开/合/index 时序，需补脱网络单测覆盖 reasoning item 的开合、thinking_delta 增量、与 text/tool_use 交错；评估面改动中等。
  - ②/③（出站预算治理 + per-supply 预算档）改动 server.py 出站热路径与 config schema，引入"代理主动改客户端 max_tokens"这一此前没有的行为，需谨慎设计为**只向上放大、绝不向下钳、且可整体关闭**，避免误伤客户端刻意给的小预算（如省成本场景）。建议配 `output_budget.enforce: on/off` 开关，默认对"会产生 thinking 且预算低于安全下限"才介入。
  - ④b 自动放大重试会让"原本一次失败的请求"变成"放大后多花 token 的重试"，对有成本敏感的上游要可关；且重试放大可能加剧延迟（ds-flash Q5 已 499s）。
- **不影响既有正确性**：所有修复都应是"增量补齐"（补回传、补 overflow 钳位、补预算治理），不改变 remap 主算法的相对映射语义（①a 只动 overflow 兜底，不动 remap 本体；MAX 在主路径仍走查表，无特殊分支，符合 codes/capability 的决策2约束）。
- **需用户确认**：
  1. ①a 的 overflow 默认策略取"向上钳到域内最高档 xhigh"是否认可（另一选项是"配置错误即 400 拒绝"，更严格但会让当前 glm responses 接入直接不可用）。
  2. ②a 允许 proxy 主动放大 max_tokens 是否可接受（涉及"代理改动客户端显式参数"的边界）；若不接受，退化为只告警不放大。
  3. 是否接受新增 per-supply `output_budget` 配置字段（config schema 扩展）。
- **与 2026-07-24 reasoning 统计移除决策的边界**：⑤a 不重启 reasoning token 记账，只新增截断/放大/重试的事件标记与可选 stop_reason，与"不再单独统计 reasoning token"的原决策不冲突。

## 验证方式

- **Defect A 修复验证**：构造 anthropic 请求 `output_config.effort=max` → responses 上游，断言发出的 wire 为 `reasoning.effort=="xhigh"`（而非 medium）；单测覆盖 MAX/MINIMAL 溢出、域内正常档。对照：修复前实测为 `'medium'`。
- **Defect B 修复验证**：非流式 + 流式各构造含 reasoning item 的 responses 响应/事件流，断言 anthropic 侧产出 `thinking` block 且文本完整；回归 glm-52-sankuai-openai-3339 跑 Q10，th_chars 应从 0 变为 >0。
- **预算治理验证**：
  - 用 max_tokens=16000 对 ds-flash@max 发 Q6，验证 ②a 自动放大 + ④b 重试后正常 end_turn（不再 stop=max_tokens 且 text 缺失），ACCESS 出现 `budget_raised/budget_retried`。
  - 用 responses→anthropic 且客户端不传 max_tokens，验证不再默认 4096，而按 supply output_budget 取值。
- **一致性验证**：对 glm-5.2 同一 canonical effort，分别走 anthropic / responses 入口发探针，断言两侧 th_chars 均 >0 且模型自述档位一致。
- **回归**：跑通 model_proxy 既有 tests/ 全部脱网络单测；确认 remap 主算法单测不受影响（①a 不改 remap）。

## 关联

- [[tools/model_proxy/README.md]]（§6 reasoning 强度映射、§8 已知限制——chat 空回答兜底）
- [[tools/model_eval/reports/glm-52-sankuai-openai-3339-max-20260807.md]]（现象 2 证据：responses 版 thinking 全 0 + 自述中等）
- [[tools/model_eval/reports/glm-52-sankuai-3339-max-20260807.md]]（对照：anthropic 版 thinking 正常）
- [[tools/model_eval/reports/ds-flash-sankuai-3339-max-20260807.md]]（现象 1 证据：max_tokens=16000 截断）
- [[tools/model_eval/calibration.md]]（2026-07-27 补测：glm 4096 截断记录）
- [[tools/model_proxy/docs/designs/2026-07-24-model-proxy-reasoning统计移除安全上线.md]]（⑤a 不重启 reasoning 记账的边界）
