---
type: design-decision
status: confirmed
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, reasoning, max_tokens, truncation, protocol-consistency, effort-mapping]
updated: 2026-08-08 confirmed（全档 5×4×2=40 探针验证通过：wire 符合推演、两协议一致、th_chars>0；①+②/④b/⑤ 已落地过复核；③/⑥ 不做）。此前：第四轮修订（预算治理重构）、2026-08-07 三轮修订
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

**1a. 修复 Defect A——encode+decode 双侧去掉写死字典，信 supply 配置（根因修正后的方向）。**

encode（出站，target 侧）：
- `_canonical_to_openai_effort_name`（codecs.py:185-190）改为 `return abstract.level.name.lower()`（MAX→"max"，HIGH→"high"），不再查 `_CANONICAL_TO_CHAT_NAME` 写死字典。
- **安全性**：remap 已把 level 收窄到 target supply 的 `effort_enum` 内（supply 没配的档不会出现在 level），所以 `level.name.lower()` 一定是 supply 声明支持的 wire 档名字符串。supply 配 max 就发 max，配置是权威。
- 上游不认该档（400）= supply 配置与上游实际不符（配置错误），该修配置，不是代理偷偷降级。代理职责是"忠实地把配置的档位发给上游"。
- 实测验证：responses/chat 端点发 max 都 200（见 Defect A 实测），supply 配 max 是对的、网关接受，写死字典是多余且有害的一层。

decode（入站，source 侧，对称修复）：
- `ChatReasoningCodec.decode` / `ResponsesReasoningCodec.decode`（codecs.py:204/234 附近）把查 `_CHAT_NAME_TO_CANONICAL`（不含 max）改为查全表 `_NAME_TO_CANONICAL`（ladder.py:79-88，含 off/none/minimal/low/medium/high/xhigh/max）。
- **现状 bug**：responses/chat SDK 发 `effort=max` → decode 查窄字典不认 → present=False → max 意图被丢（推演组合 C/D 的 max，见验证）。
- **为什么用全表而非 source capability**：decode 职责是"识别客户端档名字符串→canonical"，这是固定映射（档名=canonical 枚举小写），协议无关；source capability（`tiers_source_capability`）约束（SDK 能发哪些档）在 remap 阶段 clamp，不在 decode。decode 用全表识别所有合法档名，remap 信 source/target capability 映射——职责分离。

anthropic 统一（终态，非可选——审核硬伤 1）：
- anthropic encode 的 `_CANONICAL_TO_ANTHROPIC_NAME.get(level, "medium")`（codecs.py:143）与 Defect A 同种（写死字典+静默 medium 兜底），今天不爆是因为字典恰好含全档，下次新增档（如 MAX+1）Defect A 在 anthropic 域原样复发。**anthropic encode 同改 `level.name.lower()`**（MINIMAL→"minimal"…MAX→"max"，全部命中现字典值域，行为零变化）。
- anthropic decode 的"未识别→静默 MEDIUM"（codecs.py:117-122）须区分 absent 与 unrecognized：effort_str 缺失（absent）→ 维持现状默认 MEDIUM；非空但未识别（unrecognized）→ `logger.warning` + 进 ⑤ 观测，不再静默降级为 MEDIUM。**unrecognized 的返回值定死为 `RawIntent(level=None, present=False)`**（与 chat/responses decode 未识别对齐，勿走 STRIP 静默清字段——decode 只负责"无法识别"的诚实标注，后续 STRIP/透传由 remap 决定）。**行为变化点明**：`effort="none"/"off"` 经全表 decode 将识别为 OFF（走 DISABLED），现状是静默 MEDIUM——这是有意变化，与 chat 域"none=关闭"语义对齐。
- 终态：四张域字典（`_ANTHROPIC_*` / `_CHAT_*` 双向共 4 张）整体删除，**codec 层零词表**，词表唯一权威在 ladder 的 `_NAME_TO_CANONICAL`。DISABLED 的 `"none"` 硬编码是协议域事实（openai 域关闭词），保留但注释显式声明理由。

**1b. 修复 Defect B——补齐 responses→anthropic 的 reasoning→thinking 回传，实现双向对称。**
- **前置（审核硬伤 3）：先抓网关真实 SSE 事件流定词表再实现**。方案按正向镜像（translate.py:1300-1310 的 summary 通道）对称，但真实 responses 上游还可能发 `response.reasoning_summary_part.added/done`（多段 summary）与新版 `response.reasoning_text.delta`（非 summary 的原始 reasoning）。**只按 `reasoning_summary_text.delta` 实现有"修完仍 th_chars=0"的风险**。落地前必须对 glm/kimi 的 responses 端点抓真实事件流，确认事件词表后再写状态机。**抓取方法**：直 curl 网关带 `stream: true` 发固定探针（如"写一段需要多步推理的短答"），保存原始 SSE 到 `tests/samples/`（复用既有样本机制），事件词表以样本为准。
- 流式 `ResponsesToAnthropicStreamAdapter.feed`：增加 reasoning item 分支——`response.output_item.added` 遇 `item.type=="reasoning"` 开 anthropic `thinking` block；summary/reasoning delta 事件 → `thinking_delta`；对应 `.done` → `content_block_stop`。产出与 anthropic 原生 thinking 块同构，使 anthropic 客户端经 responses 上游也能看到 thinking。
- 非流式 `responses_to_anthropic_response`：把 `it=="reasoning"` 的 `summary[].text` 拼成 `{"type":"thinking","thinking":...}` block 放进 content（而非 `pass`）；多 part 用 `\n\n` 连接（与正向单 part 缓冲一致）。
- **已知限制声明**：anthropic thinking block 的 `signature` 字段在转换侧无来源（正向丢 signature_delta，反向永远无 signature）。对只读评估无影响；对会把 thinking 回传的多轮客户端（Claude Code），须声明为已知限制。
- 对称性原则：anthropic→responses 已有 reasoning 回传（`_start_reasoning_item`），responses→anthropic 必须补齐镜像，消除"同模型不同协议接入 thinking 可见性不同"。

**1c. 抽象层补强——词表不变量单测（审核硬伤 2）**：`ladder.py` 的 canonical 全序已是跨协议统一表示；①a 终态后 codec 层零词表，"canonical 枚举名小写 == 协议 wire 档名字符串"成为全局唯一映射约定。加单测不变量固化：

```python
for e in CanonicalEffort:
    assert name_to_canonical(e.name.lower()) == e  # OFF 双拼 off/none 都断言映射 OFF
```

未来新增枚举值时该单测强制同步词表，杜绝"新增档名 → 某域字典漏加 → 静默降级"的 Defect A 复发路径。

**1d. 文档与注释同步（复核新增）**：①a 终态后 codec 零词表，配套文档/注释必须同步，否则文档与现实矛盾：
- README line 143-144"Chat/Responses 协议域词表本身不含 max/minimal"的说明，与①a 论据及 live config 现实均已矛盾，改写为"档名词表唯一权威在 ladder._NAME_TO_CANONICAL，codec 零词表；supply `effort_enum` 声明的档名即 wire 档名"；README §6 档名表同步。
- README §8 已知限制加"反向（responses→anthropic）thinking block 无 signature 字段"条目。
- codecs.py 模块头注释与域字典注释随四表删除重写。

### ② 入站参数处理（server.py 对 max_tokens 的解析、effort 映射）

> 2026-08-08 调整：原 ②a（出站前按 ③ 表预防性放大）随 ③ 一并**撤销**。proxy 不再主动改客户端已给的 max_tokens——客户端预算是权威；代理只在两处介入：客户端**缺省时给合理默认**（本条）、**截断后反应式重试**（④b）。

**2a（撤销）**：出站前预防性放大删除，不存在 `budget_raised` 行为。

**2b. 反向 4096 兜底改造（不依赖 ③，保留并修正）**：responses→anthropic 方向客户端不传 max_tokens 时的 `max_tokens_default=4096`（server.py:1238）对 reasoning 上游是陷阱默认值（glm-5.2 首轮 4096 截断的直接来源）。改为按 remap 结果区分默认：
- remap 结果为 THINKING（本请求将产生 thinking）→ 用全局 `_THINKING_MAX_TOKENS_DEFAULT`（建议 16384，全局 config 可覆盖）；
- 非 thinking 请求 → 维持 4096 不变。
- 即便 16384 仍不够，④b 反应式爬升兜底——默认值只决定"从哪开始爬"，不追求一次给准。

**2c（原 2b，保留不动）. source capability 配置规范**：source 能力（`tiers_source_capability`）应能声明完整 7 档含 MAX，eval strategy 应显式配置，避免依赖 clamp 的偶然对齐。属配置规范而非代码改动，README/SOP 写清"eval strategy 的 source capability 必须覆盖到被测 effort 档"。

### ③ 运维默认值（per-supply output_budget）——已撤销（2026-08-08 用户决策）

整条撤销，不引入 `output_budget` 配置字段。原因（用户决策依据）：手动维护"每模型 × 每 effort 档"预算表成本高，且标定永远滞后于上游模型行为变化；反应式 ④b 用有界重试替代了先验标定，运维零表格。原 3b（model_eval SOP 按表取预算）同步失效，eval SOP 改为"给一个合理起步 max_tokens，截断由代理自动放大重试；关注 budget_retried 日志"。本节编号保留以示追溯。

### ④ 重试/兜底策略（检测到 stop=max_tokens 且正文缺失时自动放大重试）——唯一预算机制（2026-08-08 强化）

③ 撤销后，④ 成为 model_proxy 唯一的预算治理机制：**不做先验标定，只在截断真实发生后反应式放大重试**。

**4a. 响应侧截断检测——在原始上游响应上判定（关键，不能被转换层兜底掩盖）**。非流式各分支在 `resp.read()` 之后、响应转换/写回之前检测：

| mode | 判定对象（原始响应 JSON） | 截断且正文缺失的条件 |
|---|---|---|
| PASSTHROUGH（anth→anth） | 透传 body | `stop_reason=="max_tokens"` 且 content 无 text/tool_use block（只有 thinking 或全空） |
| ANTHROPIC_TO_CHAT | chat 原始响应 | `choices[0].finish_reason=="length"` 且 `message.content` 为空且无 `tool_calls` |
| ANTHROPIC_TO_RESPONSES | responses 原始响应 | `status=="incomplete"` 且 `incomplete_details.reason=="max_output_tokens"` 且 output 无 message 项（或 output_text 全空） |
| RESPONSES_TO_ANTHROPIC | anthropic 原始响应 | `stop_reason=="max_tokens"` 且 content 无 text/tool_use block |
| PASSTHROUGH（responses→responses） | 透传 body | 同 ANTHROPIC_TO_RESPONSES 的 responses 判定 |

**必须在原始响应上判，不能在转换后的 anthropic 响应上判**——`_ENABLE_REASONING_FALLBACK`（translate.py:532-538）会把 reasoning_content 填成 text block，转换后再判"无 text"永远为假，检测被兜底掩盖。"正文缺失"定义：无 text 且无 tool_use（只有 thinking 或全空）。

**4b. ×2 阶梯自动放大重试（反应式，唯一预算机制）**。命中 4a 即触发：

- **起点**：客户端有效 max_tokens（客户端给了就用客户端值；反向缺省用 ②b 新默认）。proxy 不改动客户端给定值的首次发送。
- **阶梯**：每次重试 ×2，即 `next = min(current × 2, CEILING)`；`next == current`（已达封顶）则停止。
- **次数上限**：`_BUDGET_RETRY_MAX = 5` 次（有限次，防无限重试）。
- **封顶**：`_BUDGET_CEILING = 131072`（全局上限，不依赖任何 per-supply 配置）。
- **示例**：4096→8192→16384→32768→65536→131072（5 次）；16000→32000→64000→128000→131072（4 次，末次钳到封顶）。
- **配置**：全局 config 增加可选顶层块 `budget_retry: {"enabled": true, "max_retries": 5, "ceiling": 131072}`，ConfigStore 照 `get_default_cooldown` 模式加 `get_budget_retry()`；缺省全开。无 per-supply 维度。
- **机制（对齐现有 reasoning 语法自适应重试，server.py:1290-1298）**：`resp.close()` 后 `continue` 重进 while 循环——同一 supply 重发（不进 tried_set、不 cooldown、不计 failover、不 rotate）。独立计数 `_budget_retries`（请求周期作用域，与 `_reasoning_retried` 并列声明于 1066 附近），两类重试互不干扰、可先后发生（先 400 语法重试、后 200 截断预算重试）。remap 缓存（`_reasoning_cache_supply_id`）照常复用，重试只改预算不改档。
- **出站覆写点**：四个转发分支构建完 outgoing body 后统一 stamp 当前预算值——PASSTHROUGH 写 `body_json["max_tokens"]`；ANTHROPIC_TO_CHAT 写 `openai_body["max_completion_tokens"]`；ANTHROPIC_TO_RESPONSES 写 `responses_body["max_output_tokens"]`；RESPONSES_TO_ANTHROPIC 写 `anthropic_body["max_tokens"]`。首轮 stamp 原值（无行为变化），重试轮 stamp 放大值。
- **流式边界（显式）**：流式响应字节即时下发客户端，发出后无法回追重试——**④b 仅非流式生效**；流式仅在收口处（adapter finalize，`produced_content_block` 状态允许处）检测记 `budget_truncated` 日志，不重试、不改流式行为。
- **终态**：爬升途中 end_turn → 正常写回（ACCESS 记 `budget_retried=<old>→<new>` 轨迹）；到上限仍截断 → 如实写回截断响应（stop=max_tokens、thinking 原样保留、chat 场景 fallback 照常兜底），ACCESS 记 `budget_truncated=1`。

**与现有 chat `_ENABLE_REASONING_FALLBACK` 的关系（正交，保留）**：④b 处理请求侧预算不足（根因）——预算够了正文自然出来；fallback 处理"已有 reasoning 但无 text"（症状）——chat 协议的最后兜底。④b 检测在原始响应上，不被 fallback 掩盖；④b 最终放弃时 fallback 照常生效，两者不冲突不重复。

### ⑤ 监控告警（thinking/output 占比、effort 生效性）

**5a. 预算治理事件指标**：ACCESS 日志与 totals 账本目前只记 `usage_in`/`usage_out`（server.py:1348-1354，reasoning token 已被 2026-07-24 有意移除，见其设计记录）。本方案不违背该"不单独统计 reasoning"决策，**新增两个派生观测维度**（不记账、只进 ACCESS 瞬时值）：
  - `budget_retried=<old>→<new>`（④b 每次放大重试时记，轨迹可 grep）、`budget_truncated=1`（④b 爬升到上限仍截断、或流式收口检测到截断时记）。`budget_raised` 随 ②a 撤销**不再存在**。
  - 可选：`stop_reason` 字段（end_turn/max_tokens/...）进 ACCESS，占比统计 max_tokens 出现频率。
  - **④b 触发频率的信号语义（2026-08-08 调整）**：③ 撤销后不再是"标定失准"信号，而是**"调用侧预算普遍偏小 / 该模型 thinking 量大"的运营信号**——某 supply 的 budget_retried 高频出现时，提示调高调用侧（eval SOP/客户端）的起步 max_tokens，减少重试浪费。

**5b. effort 生效性指标**：Defect A 这类"wire 层降级"要有可见性。①a 终态后 wire 档名恒等于配置档名（不再有降级兜底），观测点是"intent → wire 档名"保真度：在 reasoning debug 旁路日志（server.py:809-856）的 wire dump 里**固定记录最终发给上游的档名字符串**，并使 `MODEL_PROXY_REASONING_DEBUG=1` 时能一眼看到"intent=max → wire effort 实际值"。更进一步：smoke test / 评估 SOP 增加一条"读模型自述档位 ≠ 请求档位则告警"的校验（本轮正是靠模型自述"中等"发现的）。

### ⑥ 协议一致性（确保同模型不同协议接入行为一致）

**6a. 对称性作为强约束**：确立"同一 (target_model, canonical effort) 经任一协议转换后，到达上游的有效思考强度、以及回到客户端的 thinking 可见性，应行为一致"为 model_proxy 的不变量。①a/①b 是把当前违反该不变量的两处补齐。

**6b. 一致性自检能力**：新增一个轻量自检路径——对同一 supply 的 anthropic / responses 两种协议入口，发同一探针（固定 prompt + 显式 canonical effort），比对两侧的 (a) 上游实际收到的 effort 档名 (b) 客户端是否收到 thinking 内容。可作为 `supply test`（CLI 既有 [t]est，README §5.5）的扩展子项"effort 一致性探测"，也可由评估体系在接入新模型时调用。本轮 glm 双协议对比就是人工版的这个探测，应工具化、自动化。

---

## 风险与权衡

- **迁移/落地代价提示（[理想] 路径不计成本，仅供知情）**：
  - ①b（responses→anthropic 补 reasoning 回传）是真实功能开发，要在流式状态机里正确管理 thinking block 的开/合/index 时序，需补脱网络单测覆盖 reasoning item 的开合、thinking_delta 增量、与 text/tool_use 交错；评估面改动中等。
  - ②b/④b（反向默认值区分 + 截断反应式重试）改动 server.py 出站热路径与响应收口，但**不引入"代理主动改客户端已给 max_tokens"的行为**——客户端给定值首轮原样发出，代理只在客户端缺省时给默认、在截断真实发生后放大重试。原 ②a/③ 的"误伤客户端刻意小预算"风险随撤销消失。
  - ④b 自动放大重试会让"原本一次失败的请求"变成"最多 5 次放大重试"：成本上 input+thinking 每轮重付（最坏 4096 起步爬 5 轮），延迟上叠加（ds-flash Q5 单轮已 499s）。`budget_retry.enabled=false` 可全局关闭。**流式请求不受益**（字节已下发无法回追），流式场景仍靠调用侧给足预算。
- **不影响既有正确性**：所有修复都应是"增量补齐"（补回传、去写死字典、补反应式重试），不改变 remap 主算法的相对映射语义（①a 只动档名映射函数/字典引用，不动 remap 本体；MAX 在主路径仍走正常映射路径，无特殊分支，符合 codecs/capability 的决策2约束）。④b 不动 remap、不动 supply 选择，只在响应收口处判定后重进循环。
- **需用户确认**：
  1. ①a 方向已从原"钳到 xhigh"修正为"去掉写死字典、信 supply 配置直接发档名"（依据：实测 responses/chat 网关都接受 max，supply 配置是上游真实能力权威）。是否认可？上游不认该档则 400 暴露配置错误，代理不偷偷降级。
  2. decode 改用全表 `_NAME_TO_CANONICAL`（含 max）替代窄字典——是否认可？（source capability 约束仍在 remap。）
  3. ~~②a 允许 proxy 主动放大 max_tokens~~ → **2026-08-08 用户决策：不允许，②a 撤销**。
  4. ~~新增 per-supply `output_budget` 配置字段~~ → **2026-08-08 用户决策：不要，③ 整条撤销**。
  5. ④b 三个数值待确认：封顶 131072、重试上限 5 次、反向 thinking 默认 16384（均为全局常量/config 可覆盖，非 per-supply）。
- **与 2026-07-24 reasoning 统计移除决策的边界**：⑤a 不重启 reasoning token 记账，只新增截断/放大/重试的事件标记与可选 stop_reason，与"不再单独统计 reasoning token"的原决策不冲突。

## 验证方式

- **Defect A 修复验证（encode）**：构造 anthropic 请求 `output_config.effort=max` → responses/chat 上游，断言发出的 wire 为 `reasoning.effort=="max"`（修复前为 `'medium'`）。单测覆盖全档 low/medium/high/xhigh/max，断言均发 `level.name.lower()`。
- **Defect A 修复验证（decode）**：构造 responses/chat 入站请求 `reasoning.effort=max`，断言 decode 产出 `RawIntent(level=MAX, present=True)`（修复前 `present=False`）；anthropic 入站 decode 断言未识别档名 → warning + 不静默 MEDIUM，absent（effort_str 缺失）→ 维持 MEDIUM 默认。
- **Defect A 实测证据（网关真实接受 max，绕开 model_proxy 直 curl 上游）**：
  - responses 端点（glm-52-sankuai-openai-3339）：`reasoning.effort=max` → HTTP 200 + 模型自述"最高档" + reasoning_tokens=578
  - chat 端点（kimi-k3-sankuai-openai-3339）：`reasoning_effort=max` → HTTP 200 + 模型自述"max(最高档)" + reasoning_content 756 字符
  - 证明 supply 配 max 正确、网关接受，写死字典是多余且有害的一层。
- **全档推演验证（6+3 组合 × low/medium/high/max）**：source=`[low,medium,high,xhigh,max]`，覆盖 sdk=anthropic(adaptive/enabled budget)/responses × target=anthropic(`[high,max]` 与 `[low,high,max]`)/responses(`[high,max]`)。脚本 `/tmp/trace_combos.py`（含 ds-flash target=`[low,high,max]` 组合 G/H/I）。关键断言：
  - 修复前：target=responses/chat 且 remap=MAX（high/xhigh/max）时 encode 发 medium（bug）；修复后发 max。
  - 修复前：responses/chat sdk 发 max 时 decode present=0（bug）；修复后 present=1。
  - low/medium 全协议无 bug（remap→HIGH，字典有 high）；anthropic target 全档无 bug（字典含 max）。
  - ds-flash（anthropic target `[low,high,max]`）：encode 本就正常，low→low 不升档（对比 glm-openai `[high,max]` 的 low→high 升档），证明 target cap 档位越多 remap 越保真。
- **Defect B 修复验证**：非流式 + 流式各构造含 reasoning item 的 responses 响应/事件流，断言 anthropic 侧产出 `thinking` block 且文本完整；回归 glm-52-sankuai-openai-3339 跑 Q10，th_chars 应从 0 变为 >0。
- **预算治理验证（2026-08-08 调整）**：用 max_tokens=16000 对 ds-flash@max 发 Q6，验证 ④b 自动 ×2 爬升（16000→32000）后正常 end_turn、ACCESS 可见 `budget_retried` 轨迹；构造"恒截断"stub 上游验证爬升到封顶后如实返回 stop=max_tokens 且记 `budget_truncated=1`；responses→anthropic 且客户端不传 max_tokens 时，thinking 请求默认 16384（非 4096）、非 thinking 请求仍 4096；流式请求验证不重试、仅收口记日志；`budget_retry.enabled=false` 时全链路退回原透传行为。
- **④b 新增单测**：三协议原始响应的截断判定（含"fallback 填 reasoning 成 text 后检测仍触发"的 chat 用例）；阶梯计算（×2、封顶钳位、次数上限、next==current 停止）；与语法重试共存（同请求先 400 语法重试后 200 预算重试，互不消耗对方次数）；重试不 cooldown/不进 tried_set。
- **一致性验证**：对 glm-5.2 同一 canonical effort，分别走 anthropic / responses 入口发探针，断言两侧 th_chars 均 >0 且模型自述档位一致。
- **既有单测改动清单（复核新增，回归必撞红）**：①a/①b 落地后以下断言反转，必须同步修改，否则回归爆红：
  - `tests/test_reasoning.py:768-772` chat MAX 断言 `"medium"` → 改 `"max"`（测试名 `falls_back_default` 语义失效，改名）
  - `tests/test_reasoning.py:805-808` responses MAX 断言 `"medium"` → 改 `"max"`
  - `tests/test_translate.py:1657-1662` `test_ar_reasoning_item_dropped` 断言 reasoning item 被丢弃 → ①b 后反转为产出 thinking block
- **回归**：跑通 model_proxy 既有 tests/ 全部脱网络单测；确认 remap 主算法单测不受影响（①a 不改 remap）。

## 关联

- [[tools/model_proxy/README.md]]（§6 reasoning 强度映射、§8 已知限制——chat 空回答兜底）
- [[tools/model_eval/reports/glm-52-sankuai-openai-3339-max-20260807.md]]（现象 2 证据：responses 版 thinking 全 0 + 自述中等）
- [[tools/model_eval/reports/glm-52-sankuai-3339-max-20260807.md]]（对照：anthropic 版 thinking 正常）
- [[tools/model_eval/reports/ds-flash-sankuai-3339-max-20260807.md]]（现象 1 证据：max_tokens=16000 截断）
- [[tools/model_eval/calibration.md]]（2026-07-27 补测：glm 4096 截断记录）
- [[tools/model_proxy/docs/designs/2026-07-24-model-proxy-reasoning统计移除安全上线.md]]（⑤a 不重启 reasoning 记账的边界）

---

## 审核意见（architect-max 独立复核，[理想] 路径，2026-08-07）

复核范围：通读 codecs.py / capability.py / ladder.py 全文，translate.py 正反向流式状态机与非流式转换（1255-1520、1700-1998），server.py reasoning 链路与 max_tokens 调用点（699-717、1068-1298），推演脚本 /tmp/trace_combos.py。以下论断均经代码实证。

### 总体判定

方案抓住了根本（supply 配置是上游能力权威 + 协议双向对称不变量），方向是体系化的，不是打补丁。但按理想路径标准，**①a 只走了一半**：同物种的"写死字典 + 静默 medium 兜底"在 anthropic 域和 decode 侧还有三处残留，方案把 anthropic 统一标为"可选"是降级思维残留。推到终态（三域零词表字典）才是体系化完成态；停在现状等于"把补丁打在了对的层"。

### 已验证成立的关键论断（不需要改）

1. **①a 的 `level.name.lower()` 安全性成立**。通读 remap() 全部返回路径：THINKING 产出的 level 只有三个来源——`tgt_think[j]`（enum 元素）、`clamp_absolute`（返回 enum 元素）、OFF clause 的 `off_alias`（from_config 保证 ∈ enum，且为 OFF 时经 abstract_encode 转 DISABLED 不进 THINKING 分支）。即 kind=THINKING 时 level 恒 ∈ tgt_cap.enum。syntax_adapt 全代码库唯一调用点在 server.py:1172，只消费 remap 链产物（缓存复用限于同 supply 重试）。codecs.py 头注释（line 16-17）本就声明了该不变量。clamp 逻辑覆盖所有 path，无遗漏。
2. **decode 用全表 `_NAME_TO_CANONICAL` 而非 source capability，选择正确**，且理由可比原方案更强：若 decode 按 source capability 钳位，"max 意图"会在观测层被改写成 xhigh，⑤b 的 effort 生效性指标（intent vs wire 比对）直接失效；decode 保真识别 + remap 的 rank_of 内部 clamp，功能等价且观测保真。职责分离论证成立。
3. **Defect B 定位准确**。正向镜像蓝本在 translate.py:1300-1310（thinking_delta→reasoning_summary_text.delta），反向 1752 的 `pass` 与 1906-1926 的缺失分支确认无误。
4. **②-⑥ 与 ① 体系一致、无重复**：⑥a 是不变量陈述，①a/①b 是恢复不变量的两处修复，⑥b 是回归检测工具，层次清楚。①c 与 ⑥ 的轻微重叠可接受（一个是代码内注释+单测，一个是运行时自检）。

### 硬伤与必须修正（按理想路径）

1. **①a 不彻底：三处同物种写死残留必须一并清除**。
   - **anthropic 统一不是"可选"**。`_CANONICAL_TO_ANTHROPIC_NAME.get(level, "medium")`（codecs.py:143）与 Defect A 是同一物种：写死字典 + 静默 medium 兜底。今天不爆只因字典恰好含全档；未来新增档（MAX+1）时 Defect A 在 anthropic 域原样复发。anthropic encode 同改 `level.name.lower()`（MINIMAL→"minimal"…MAX→"max" 全部命中现字典值域，行为零变化），随后 `_ANTHROPIC_NAME_TO_CANONICAL` / `_CANONICAL_TO_ANTHROPIC_NAME` / `_CHAT_NAME_TO_CANONICAL` / `_CANONICAL_TO_CHAT_NAME` 四表整体删除，codec 层零词表——词表唯一权威收在 ladder。
   - **anthropic decode 的"未识别→静默 MEDIUM"（codecs.py:117-122）是 decode 侧降级残留**。方案只修了 chat/responses decode 的"不认 max"，没修 anthropic decode 的"不认则静默 medium"。须区分：absent（effort 缺失 → 维持现状 MEDIUM 默认）与 unrecognized（非空但未识别 → warning + present=False 或进观测），不能继续静默改写客户端意图。
   - **DISABLED 硬编码 "none"（codecs.py:215/246）是唯一可保留的协议域常量**（OpenAI 域关闭词确为 "none"，是域事实不是配置事实），但须在 codec 注释显式声明"唯一保留常量及其理由"。要消除的是"静默"，不是这个常量本身。
2. **隐含约定必须显式化、可执行化**。①a 之后，"canonical 枚举名小写 == wire 档名字符串"成为全局唯一映射规则，其成立依赖 `_NAME_TO_CANONICAL` 键 ⊇ 枚举名小写且自映射（off/none 双拼是唯一例外，只影响 OFF）。应在 ladder 加单测不变量：对 CanonicalEffort 每个成员断言 `name_to_canonical(e.name.lower()) == e`（OFF 断言 off/none 双键均映射 OFF），未来新增枚举值时强制同步词表。①c 的"codec 词表注释"在域字典全删后应改写为"codec 不持有词表，唯一权威在 ladder"。
3. **①b 流式镜像的事件词表需实测补全，不能只按 summary 单通道实现**。方案只提 `reasoning_summary_text.delta`——与正向对称没错，但真实 responses 上游还可能发 `response.reasoning_summary_part.added/done`（多段 summary）及新版 `response.reasoning_text.delta`（原始 reasoning 通道）。落地前必须先抓 glm/kimi 网关真实 SSE 事件流定词表，否则存在"修完仍 th_chars=0"的风险。非流式多 part 拼接的连接符需定死（建议 "\n\n"）。另须声明已知限制：anthropic thinking block 的 signature 在转换侧无来源（正向 1311 行丢 signature_delta，反向永远无 signature）——对只读评估无影响，对会把 thinking 回传上游的多轮客户端（Claude Code）是限制。
4. **②a 与 ④b 职责边界需在文中点明**（两者都改 max_tokens）：②a = 发送前预防性地板（读 ③ 表）；④b = 截断后反应式阶梯，从 ②a 放大后的有效值起算、封顶 supply 上限，时序串联不重叠。且理想路径下 ④b 的定位是**补偿控制**，不是根因治理——③ 标定准确时 ④b 应近似零触发，其触发频率本身应进 ⑤ 作为"标定失准"信号。方案已隐含此意但未点破。
5. **设计记录两处 stale 引用（订正级，不影响方向）**：⑤b"①a 的 overflow warning 之外"与风险节"①a 只动 overflow 兜底，不动 remap 本体"是修订前"钳到 xhigh"方案的残留，新 ①a 已无 overflow 概念，需订正。

### 理想路径下的兜底思维裁决

- ②a"只升不降"**不是兜底**，是"客户端预算权威性"不变量，保留。
- "无配置退保守全局值"可接受，但须响亮（log + metric），不静默。
- ④b 重试保留，定位为补偿控制 + 标定失准信号源，可整体关闭。
- 必须清除的三处兜底残留：anthropic encode 的 `.get(...,"medium")`、anthropic decode 的未识别静默 MEDIUM、以及把 anthropic 统一标为"可选"这一决策本身。

### 结论

**体系化方向确认，方案可执行，但需按上述硬伤 1/2/3 把 ① 推到终态后再实施**：三域零写死字典（codec 零词表）、词表不变量单测固化、①b 事件词表先实测。做到这三点，本方案从"修对了地方"升级为"同类问题在架构上不可能复发"；不做，则 anthropic 域与 decode 侧仍各埋着一颗与 Defect A 同种的雷。

---

## 第二轮复核（architect-max 独立新实例，[理想] 路径，2026-08-07）

复核范围：逐条核对第一轮 5 条硬伤的修订落实；整体一致性；二轮修订是否引入新问题；实施可行性终审。复核中实证了：live config（glm-52-sankuai-openai-3339 `effort_enum=[high,max]`、kimi-k3-sankuai-openai-3339 `[low,high,max]`，openai 域已配 max，①a 落地后 encode 立即正确、无需改配置）、translate.py 正向镜像蓝本（1300-1310 thinking_delta→reasoning_summary_text.delta、1311 signature_delta 跳过）、反向缺口（1752 `pass`、1906-1926 无 reasoning 分支）、server.py 各引用行号（809-856 debug 旁路、1238 兜底 4096、1290-1298 语法重试）、既有单测全文。

### 5 条硬伤逐条核对结论

1. **硬伤 1（anthropic 强制化 + decode absent/unrecognized 区分）：已解决**。①a 段落（"anthropic 统一（终态，非可选）"）三项全部落实：encode 改 `level.name.lower()`、decode 区分 absent（维持 MEDIUM）/unrecognized（warning + 进观测）、四表整体删除 + DISABLED `"none"` 保留并声明理由。残留两个实施级待定项（见新增问题 4），不影响本条关闭。
2. **硬伤 2（词表不变量单测）：已解决**。①c 单测不变量 `name_to_canonical(e.name.lower()) == e`（OFF 双拼断言）与 ladder.py:79-88 实际键集核对无误，7 个枚举成员 `name.lower()` 全部命中 `_NAME_TO_CANONICAL`。
3. **硬伤 3（①b 事件词表实测前置 + signature 已知限制）：已解决**。前置实测要求与 signature 无来源声明均已写入 ①b；正向蓝本行号经核实属实。
4. **硬伤 4（②a/④b 职责边界显式化）：已解决**。④b 段落已点明"预防地板 vs 反应阶梯、时序串联不重叠、补偿控制定位、触发频率进 ⑤ 作标定失准信号"，与 ⑤a 三事件字段（budget_raised/budget_truncated/budget_retried）呼应自洽。
5. **硬伤 5（stale 引用订正）：已解决**。⑤b 与风险节已无 "overflow" 残留，全文 grep 确认。

### 整体一致性

①a 终态"codec 零词表"与 DISABLED `"none"` 硬编码保留、与 ①c 单测不变量互相自洽；②a/④b 边界与 ③ 表、⑤ 监控呼应；⑤b "wire 档名恒等于配置档名"经 remap 全路径核实成立（THINKING 的 level 恒 ∈ tgt_cap.enum）。无方向级矛盾。

### 新增问题（均为实施级遗漏/表述错误，非方向级）

1. **【必须在实施清单点名】①a/①b 会反转 3 处既有单测断言，文档未点名**，而验证方式节自己要求"回归：跑通既有 tests/ 全部脱网络单测"——implementer 跑回归必撞红：
   - `tests/test_reasoning.py:768-772` `test_syntax_adapt_max_no_special_branch_falls_back_default`：断言 chat MAX→`"medium"`，①a 后应为 `"max"`；测试名中 `falls_back_default` 语义失效，需改名。
   - `tests/test_reasoning.py:805-808` `test_syntax_adapt_max_no_special_branch`（responses）：断言 MAX→`{"effort":"medium"}`，①a 后应为 `"max"`。
   - `tests/test_translate.py:1657-1662` `test_ar_reasoning_item_dropped`：断言非流式 reasoning item 被丢弃，①b 后应产出 thinking block，断言需反转。
2. **README/注释同步缺口**：README line 143-144"Chat/Responses 协议域……其 effort_enum 词表本身不含 max/minimal"与 ①a 论据（网关真实接受 max、词表以 supply 配置为权威）及 live config 现实均矛盾，落地后必须改；README §8 已知限制需新增 ①b signature 无来源条目；codecs.py 模块头注释与域字典注释（line 72-73、172-174）随四表删除需重写。方案未列此同步任务，建议在 ⑥ 或验证方式补一条"文档/注释同步清单"。
3. **表述错误（订正级 typo）**：风险节"符合 codes/capability 的决策2约束"——`codes` 应为 `codecs`。
4. **①a decode 两个实施语义未定死**：
   - anthropic decode unrecognized 的返回值未写死（"warning + 不静默 MEDIUM"之后返回什么）。建议明确为 `present=False`（原字段透传，与 chat/responses decode 的 unrecognized 行为对齐；上游 400 由既有 interpret_rejection 自适应兜底），而非 `level=None,present=True`（走 STRIP 会静默清掉客户端字段）。
   - 四表删除后 anthropic decode 必然改查全表 `_NAME_TO_CANONICAL`，则 `output_config.effort="none"/"off"` 从现状"未识别→MEDIUM"变为"识别为 OFF → remap OFF 吸收态 → DISABLED/STRIP"。语义更正确，但属行为变化，文档应点一句。
5. **小模糊**：④b"封顶 supply 配置上限"在 ③ 的 `output_budget` schema 中无对应字段（只有 default/by_effort/min_for_thinking），需定死封顶读哪个键（建议新增 `max` 键或约定取 by_effort 最大值）。①b 的 SSE 抓取操作建议补一句（直 curl 网关 responses 端点 `stream:true` 存原始 SSE，Defect A 实测已证明直 curl 可行；抓到的真实事件流可存成 `tests/samples/` 样本文件做脱网络回归，复用既有样本机制）。

### 终审结论

方向级无硬伤，第一轮 5 条硬伤全部妥善解决，方案整体自洽、前提经实证成立。但新增问题 1（3 处既有单测断言反转）是回归阶段必然爆红的实质遗漏，新增问题 2/4 属落地正确性所需。**判定：需再修订（小订正级，非方向修订）**——把上述 5 点并入文档（验证方式节加"既有单测改动清单"、①a decode 段落定死 unrecognized 返回值与 none/off 行为变化、方案设计补"README/注释同步"条目、③ schema 补封顶键约定、①b 补抓取方法一句话）后，即可交 implementer 落地。

---

## ①b-chat 扩展（第三批落地记录，2026-08-07）

**背景**：端到端验证发现 chat target（kimi-k3-sankuai-openai-3339）经 anthropic 客户端请求时 wire 档名正确但 th_chars=0——①b 只补了 responses→anthropic 的 reasoning→thinking 回传，chat→anthropic 反向是同种缺陷的另一处（原仅在"空回答兜底"时把 reasoning_content 填成 text，从不映射 thinking block）。按 ⑥a 对称不变量补齐。

**SSE 样本词表（以真实样本为准）**：openai chat 流式中 reasoning 经 `choices[].delta.reasoning_content` 增量下发（非流式在 `choices[0].message.reasoning_content`），无独立"开块"事件；kimi 实测序列为"全部 reasoning_content 分片 → 一个空 reasoning_content → 全部 content 分片"。样本落盘 `tests/samples/kimi_chat_reasoning_high.sse`（流式）/ `kimi_chat_reasoning_high_nonstream.json`（非流式）。

**落地实现**（`core/translate.py`，不动 responses→anthropic / codecs / server / config）：

- **非流式 `openai_to_anthropic_response`**：`message.reasoning_content` 非空且已有正文/工具块时，`insert(0, {"type":"thinking","thinking":...})` 置前于 text/tool_use；空 reasoning_content 不产 block；signature 无来源不产出（与 ①b 一致）。
- **流式 `OpenAIToAnthropicStreamAdapter`**：新增 `_content_block_start_thinking` / `_content_block_delta_thinking` helper 与 `_flush_thinking_block`——`delta.reasoning_content` 仍先累积进 `reasoning_buf`，在**首个 content/tool 增量处**把累积思考一次性镜像为 thinking block（开 index 在 text/tool 前、thinking_delta、合块），`thinking_emitted` 标记防重。
- **与现有兜底的关系（关键边界，互斥不双写）**：
  - content 非空（有 text 或 tool_calls）→ 走镜像：reasoning_content 变 thinking block + content 是 text/tool block；`produced_content_block=True` 使 finalize 兜底自然不触发，且 flush 清空 reasoning_buf 双保险。
  - content 空 → 走既有兜底：finalize 把 reasoning_buf 填成 text block（不产 thinking block）。`test_reasoning_only_finalize_adds_text_block` 等空回答兜底单测保持绿。
- **为何流式用 buffer-flush 而非逐 delta 实时开块**：chat 流无独立 reasoning 开块事件，收到 reasoning_content 时无法预知 content 是否为空；而 ⑥a 边界要求"content 空时仍走兜底填 text（产 text block、不产 thinking）"。逐 delta 实时开 thinking 块会让空回答场景产出 thinking block、与兜底断言冲突或双写。buffer-flush 在 content 首次到达时才镜像，既保证 thinking index 在 text 前，又保证空回答仍走兜底。已知限制：正文块产出后再到的 reasoning_content 分片（非标准交错）不镜像，留在 buf 不双写。

**既有单测改动清单（本批翻转，对齐 ①b "dropped→backfilled" 反转先例）**：
- `test_reasoning_fallback_not_triggered_with_real_content` → `test_reasoning_mirror_with_real_content`（断言由"reasoning 被忽略"反转为 `[thinking, text]`）。
- `test_reasoning_fallback_not_triggered_with_tool_calls` → `test_reasoning_mirror_with_tool_calls`（反转为 `[thinking, tool_use]`）。
- 流式 `test_reasoning_then_real_content_no_extra_block` → `test_reasoning_then_real_content_mirror_thinking`（反转为 thinking 在 text 前、finalize 不兜底）。
- 空回答兜底单测（`test_reasoning_fallback_length` / `_finish_reason_stop` / `_no_reasoning_keeps_old_behavior` / 流式 `test_reasoning_only_finalize_adds_text_block` / `test_reasoning_empty_no_fallback_block`）**未改、保持绿**。

**新增单测**：`TestChatReasoningMirror` 4 个——非流式 kimi 样本（thinking 在 text 前、无 signature）、流式 kimi SSE 样本（thinking/text index 0/1、thinking_delta 拼接==样本 reasoning_content 全文、text_delta 拼接==样本 content 全文、start/stop 配对）、两条 content 空走兜底边界。

**验证结果**：`python3 -m unittest discover -s tests -q` 482 全绿（478+新增4）；样本验证 th_chars 非流式 0→96、流式 0→92。

---

## 预算治理重构（第四轮调整记录，2026-08-08，[理想] 路径）

**用户决策**：
1. 要 ④b 自动放大重试（反应式，×2 阶梯）——遇 stop=max_tokens 且正文缺失，自动放大 max_tokens（每次 ×2）重试，直到 end_turn 或到上限。
2. 不要 ③ per-supply output_budget（预防式）——手动维护每模型每档预算表成本太高，放弃。
3. 倾向 ×2 阶梯（保守，多次重试），而非直接跳大值。

**落地结论**：
- ③ 整条撤销（无 output_budget 配置字段）；②a 预防性放大同步撤销（proxy 不再主动改客户端已给的 max_tokens）。
- ④b 强化为**唯一预算机制**：反应式 ×2 阶梯（起点=客户端有效值，封顶全局 131072，上限 5 次），机制对齐现有 reasoning 语法自适应重试（continue 重进循环、同 supply、不 cooldown、不计 failover）；检测在原始上游响应上判定（防 chat fallback 掩盖）；仅非流式生效（流式字节已下发无法回追，仅收口记日志）。
- ②b 反向 4096 兜底改为按 remap 结果区分（THINKING→16384 / 非 thinking→4096，全局常量可覆盖），不依赖 ③ 表。
- ⑤ 去掉 budget_raised，保留 budget_retried/budget_truncated + 可选 stop_reason；④b 触发频率语义从"③ 标定失准"改为"调用侧预算偏小/模型 thinking 量大"的运营信号。
- ⑥ 不动。
- 正文 ②-⑥、风险与权衡、验证方式已按本轮决策就地重写；两轮复核章节（针对 ① 及原 ②-⑥）保留原样作历史记录，其中涉 ②a/③/④b 边界的论断以本轮重写为准。

---

## 架构合理性审查（architect-xhigh，④b 落地前，2026-08-08）

复核范围：server.py 请求处理+重试链路（1040-1470）、响应收口（1339-1453）、流式写回 helper（1763-1820）、ConfigStore（294-351）、_acc 初始化与 ACCESS（941-965）；translate.py 转换+兜底边界（415-482、489-548、1695-1734、971-999、map_finish_reason 180-184、_FINISH_REASON_MAP 64）；reasoning 三层模块头职责声明（ladder/capability/codecs）。结论均经代码实证。

**总体判定：合理（④b 放 server.py 正确，无架构级硬伤），附 4 条 refinement 级调整（非 blocker）。**

### 1. 改动文件：server.py 正确

④b 本质是 HTTP 请求/响应编排——读原始上游响应、判重试、改 outgoing body 预算、在同 supply 内重发。这些全是 server.py 既有职责。reasoning 三层是显式纯模块（codecs 头"不依赖 registry/server/translate"、capability 头"不依赖 codecs/registry/server/translate"、ladder 头"零依赖"），不能承载 HTTP 重试编排；translate.py 是无状态转换（每次调用独立、无循环、无 supply/cooldown 态、无 HTTP），放重试会破坏其纯度。**server.py 是唯一正确落点。**

### 2. 架构符合性：符合"重试/failover 编排"职责，不越界

server.py 已承载全部重试编排：reasoning 语法重试（1290-1298 `continue`）、failover 冷却重试（1300-1306/1316-1322/1328-1335 `continue`）。④b 只是新增一个重试触发器，落在同一 `while True`（1117）内，复用同一 `continue` 幂等。响应收口处读协议特定字段判截断，与该层既有行为一致（usage 双字段读取 1351-1354 已同时认 anthropic `input_tokens` 与 chat `prompt_tokens`）。不越界。

### 3. 截断检测位置：原始响应上判（转换前）——正确且被代码强制

`openai_to_anthropic_response` 的 `_ENABLE_REASONING_FALLBACK`（translate.py:532-538）在 content 空时把 reasoning_content 填成 text block。转换后再判"无 text"恒为假，检测被兜底掩盖。故 chat 方向必须在原始 chat 响应上判；为统一，全 mode 都在原始响应上判。检测（server，编排决策）与兜底（translate，转换产出）是时序先后、不重叠的两件事：先判（不重试才）→ 转换+兜底。无职责重叠。

### 4. 与语法重试的关系：复用机制 + 独立状态——正确

两者共享的是**机制**（`continue` 重进 while、同 supply 重选、不 cooldown、不进 tried_set、复用 `_reasoning_cache_supply_id` remap 缓存），不是同一份状态。触发正交（400 语法拒绝 vs 200 截断）、可同请求先后发生（先 400 语法重试、后 200 预算重试），故状态必须独立——共享单一布尔会互相阻塞。耦合风险低：唯一共享可变项是循环本身与 remap 缓存，而 remap 输入（intent/两侧 cap）不被任一重试改变。有界性：语法重试布尔、预算重试计数（≤5）、failover tried_set 耗尽，while 循环必终止。同 supply 重选的确定性假设（select_supply 不依赖被重试改变的 cd/tried_set）与语法重试 1296-1298 注释所依赖的完全相同，④b 继承即正确。

### 5. stamp 预算值：不破坏转换层 body 构建

stamp 在 pt.* 请求转换器返回后、`json.dumps(send_body)` 前，覆写单一标量字段。转换器保持纯函数、零改动。这与既有"转换后覆写 model 字段"（1207-1208 / 1225-1226 / 1246-1247）是同一模式，属已确立惯例，不破坏分层。

### 6. 总体判断：合理，无更优独立模块

不抽独立 budget 治理模块：重试必须内联在 while 循环（循环抽不走）；预算起点依赖 remap 结果（②b 反向默认按 THINKING→16384/否则→4096，remap 结果只有 server 侧有，translate.py:1238 目前只收固定 `max_tokens_default=4096`），天然 server 侧；可抽的纯部分（截断判定、阶梯计算）体量小，单独立模块是过度设计，且违背"所有重试编排在 server.py"的既有先例。

### 附 4 条 refinement（落地时并入，非方向修订）

- **R1 截断判定建议抽成纯函数、落 translate.py**：判定是"协议响应形状"知识，而 translate.py 已持有截断信号词表（`map_finish_reason` 180-184、`_FINISH_REASON_MAP` 64 `"length"→"max_tokens"`），且判定消费的正是转换器同款原始响应。建议 `translate.py` 加纯谓词 `is_budget_truncated(target_protocol, raw_resp) -> bool`，server.py 收口处调用——避免 server.py 内联堆积 5 段协议解析。retries/阶梯/continue 编排仍留 server.py。**（server.py 内联亦可接受，此为清洁度优化，非硬伤。）**
- **R2 PASSTHROUGH stamp 字段名需分协议**：落地清单"PASSTHROUGH 写 `body_json["max_tokens"]`"只覆盖 anth→anth 透传；responses→responses 透传的字段是 `max_output_tokens`。④a 检测表已含 responses→responses 透传，stamp 点必须同样按透传子协议分字段，否则该方向重试不生效。
- **R3 `_budget_retries` 是计数器（int，≤5）不是布尔位**：与落地清单"计数"一致；避免按语法重试的布尔 `_reasoning_retried` 误实现成一次性。
- **R4 明确 budget 状态跨 failover 的语义**：`_budget_retries`/`_budget_current` 声明于 1066（请求周期作用域），若爬升途中该 supply 返回 failover 状态码（1328-1335 触发 cooldown+换 supply），放大后的预算会被下一 supply 继承。这是合理语义（预算不足是模型属性，同 tier 换 supply 不必从起点重爬），但设计未点明，建议显式声明为有意行为。
