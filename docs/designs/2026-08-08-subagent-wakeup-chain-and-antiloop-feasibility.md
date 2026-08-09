---
type: design-decision
status: shelved
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, anti-loop, wakeup-chain, kimi-k3, 调研]
---

# subagent 唤醒主会话链路还原 + modelproxy 拦截可行性评估

> **注**：链路分析留存有价值；拦截/防护方案随 id-guard 一并搁置（2026-08-08 用户拍板）。

> 调研/分析类文档。起因：2026-08-07 14:42–15:32 UTC 主会话（Claudian）故障——每次 subagent 跑完
> tool_result 唤醒主会话，kimi-k3 就幻觉"用户发了空消息 (no content)"并重复派同一个 Agent 调用，
> 循环 6 次直到用户手动拒绝。本文回答四个问题：链路还原、proxy 能感知什么、拦截可行性、根因定性。
> 证据主源：主会话 transcript（`~/.claude/projects/-Users-vincentwang-Documents-NoteVault/2896beec-d221-4013-a073-1ae74010a865.jsonl`）、
> 同目录 `subagents/*.meta.json`、Claudian 插件 main.js、Agent SDK 0.3.220（bun cache）、claude-code 2.1.197 二进制、
> modelproxy 代码与 ACCESS 日志。推断与实证已分别标注。

## 1. 背景与问题

主会话在 kimi-k3 服务时段出现"tool_result 唤醒 → 幻觉空消息 → 重复派单"自强化循环。要判断
modelproxy（无状态转发网关）能否在链路上做有效防护，需先搞清楚唤醒链路各环节归属、proxy 的
可见面、以及故障本质是模型问题还是协议缺陷。

## 2. 链路还原：subagent 跑完 → 唤醒主会话 → 下一轮 API 请求

**各环节归属（实证 + 标注推断）：**

| 环节 | 处理者 | 证据 |
|---|---|---|
| 主会话 agentic 循环（发请求→收流→执行工具→把 tool_result 追加为 user 消息→发下一请求） | **claude-code 运行时进程**（Agent SDK spawn 的 native binary，`pathToClaudeCodeExecutable` 可指定，默认解析已安装的 claude） | SDK sdk.mjs 中 spawn child_process + "Claude Code executable not found" 等错误文案（实证）；transcript 中 queue-operation / stop_hook_summary / attachment 条目是 claude-code 运行时格式（实证） |
| Agent 工具执行 = spawn 子 agent | claude-code 运行时（in-process 新上下文），子 agent transcript 落 `<session>.jsonl` 同目录 `subagents/agent-*.jsonl` + `*.meta.json` | meta.json 内容 `{"agentType":"architect-xhigh","toolUseId":"Agent_221","spawnDepth":1}`（实证），6 个故障窗口的 architect 子 agent toolUseId 全是 `Agent_221` |
| 子 agent 完成 → tool_result 回主会话 | 运行时把子 agent 最终文本包装为 tool_result block（user 角色消息）追加进主会话历史，**立即继续循环发下一请求**——无用户参与，无 Claudian 参与 | transcript：14:47:02 tool_result 落盘 → 14:47:19 下一条 assistant thinking（实证） |
| transcript 落盘 | claude-code 运行时写 `~/.claude/projects/<proj>/<session>.jsonl` | 文件格式与内容（实证） |
| Claudian（Obsidian 插件）角色 | **被动渲染层**：调 SDK `query()`、消费 stream 事件渲染气泡；不持有循环、不决定唤醒。代码中对 "(no content)" 只有 3 处**过滤**（`extractTextContent` 等），无注入 | main.js grep（实证） |
| API 请求出口 | 运行时按 ANTHROPIC_BASE_URL 发到 modelproxy → `translate.py` Anthropic→OpenAI 转换 → 上游 supply | proxy ACCESS 日志 session=2896beec… 记录（实证） |

**"唤醒"的本质**：不是事件通知，而是运行时 agentic 循环的普通一次迭代——子 agent 结果作为
tool_result 进入 messages 后，循环自然地发起下一次 API 请求。对模型而言，这个回合的最后一个
user 消息**只含 tool_result block、无任何 text**——这是与"真实用户消息回合"唯一的结构性差异，
也是 kimi-k3 误读的起点。

**"(no content)" 来源考据**：claude-code 二进制中 `Xv="(no content)"` 是 **assistant 侧**占位符
（空 assistant 响应/API 错误时合成 text block，配套 `tengu_filtered_whitespace_only_assistant`
过滤逻辑）；transcript 故障窗口内**没有任何真实的 "(no content)" user 消息**（13 处出现全部是
kimi 自己的 thinking 或后续 grep 结果引用）。结论：kimi 幻觉出的该字符串大概率来自其训练语料中
的 Claude Code transcript 格式知识（推断），harness 未在 user 侧注入该占位（实证：transcript 无记录；
推断：Claudian 只过滤不注入，运行时注入路径均为 assistant 侧）。

## 3. 关键实证：tool_use id 的生成者是模型自己

按服务模型统计 transcript 中 tool_use id 格式（实证）：

| 服务模型 | id 格式 | 来源 |
|---|---|---|
| claude-opus-5 / sonnet-5 | `toolu_*` | 上游不给 id，proxy `gen_toolu_id()` 兜底（translate.py:518/827） |
| glm-5.2 | `call_*` | 上游网关生成 |
| kimi-k3 | `<ToolName>_<N>`（Bash_211、Agent_221…） | **kimi 模型输出的一部分**，proxy 原样透传 |

kimi-k3 的 id 序号行为（实证，故障窗口完整序列）：
- 序号 = "当前可见上下文里最大 N + 1" 的学习型 pattern，**非唯一性保证机制**：上午窗口 211→226 单调；下午 14:25 上下文重建后从 209 重启（旧 id 滚出窗口）；`/compact` 后从 **Bash_0** 重启。
- 故障核心：14:43:33 首发 Agent_221（正常，前文最大 220）；但 14:47:45 起连续 5 次重派**仍输出 Agent_221**——与模型幻觉的世界观自洽（它认为"之前没派出去"，于是重新生成"220 之后的第一个 Agent 调用"）。后续 Bash_222 重复 12 次同理。
- 重复 id 造成**协议层真实歧义**：历史中多个 tool_use 同名同 id，tool_result 与 tool_use 的配对不再唯一。这既是幻觉的症状，也反过来加剧后续回合的上下文追踪混乱（双向强化）。

## 4. modelproxy 能感知到什么

proxy 在每个请求上已有/可有的可见面（实证，server.py `_forward` 流程）：

1. **完整 messages 数组**（body_json 全量解析）——含全部历史 tool_use/tool_result block 及其 id。
2. **session_key**：`extract_session_key` 从 `metadata.user_id` 解出 session_id（已用于 session 哈希路由与 ACCESS 日志）。子 agent 请求走独立 session id，可与主会话区分。
3. **回合类型可判定**：取最后一条 role=user 消息的 content——纯 tool_result block 列表 = 工具唤醒回合；含 text/字符串 = 真实用户回合。`commands.py:extract_last_user_message_content` 已有取最后 user 消息的现成函数（内建命令层在用），加类型判断即可。
4. **重复 id 可检测**：单请求内扫描 messages 即可发现"同一 tool_use id 出现在多个 assistant 消息"或"响应侧新 tool_use id 与历史中已存在的 id 冲突"——**无需跨请求状态**（历史就在请求里）。
5. **响应侧内容可见**：流式路径经 `OpenAIToAnthropicStreamAdapter._handle_tool_calls_delta`（translate.py:810）逐事件转换，tool_use id/name/input 在 `_content_block_start_tool` 处成形；非流式在 translate.py:507-523。text/thinking 内容同样流经适配器（reasoning_content 镜像为 thinking block）。
6. **client 区分**：`token=cc/codex`、`detect_source`（anthropic/openai/responses）已有；可只对 source==anthropic 生效。
7. **现有跨请求状态先例**：CooldownStore（内存）、SyntaxPreferenceStore（内存、按模型）、SessionOverridesSidecar（文件 + 热重载）。维护"per-session 近期派单指纹"有现成模式可仿。

**看不到的**： Claudian  UI 层事件、运行时内部状态（hook、权限拒绝原因细节——拒绝只表现为 tool_result 错误文本）、未经过 proxy 的任何东西。

## 5. 拦截可行性评估（分档）

### 档位 A：观测/告警（低成本、零误判风险、建议先做）

- **A1 重复 tool_use id 检测（请求侧）**：扫描请求 messages，发现同一 id 被多个 assistant tool_use 使用（或 tool_result 配对歧义）→ ACCESS 日志加字段 + warn 日志。纯观测，不改流量。实现：~50 行，挂在 `_forward` body 解析后（与内建命令层同位置）。误判率≈0（协议上本来就不该重复）。
- **A2 重复派单循环检测（请求侧）**：同一 session 历史中，最近 N 个回合内出现 ≥2 次 Agent/Task tool_use 且 input.prompt 高度雷同（如归一化后前缀/编辑距离阈值），且各自已有完成态 tool_result → 告警。仅在 source==anthropic 生效。误判风险低（正常重复派单的 prompt 通常有差异；同名 agent 不同 prompt 不命中）。实现：~100 行 + 相似度函数。

### 档位 B：协议修复（中成本、针对真实缺陷、建议做）

- **B1 响应侧 tool_use id 唯一性改写**：上游返回的新 tool_call id 若与本请求 messages 历史中已存在的 id 冲突（或同一条 assistant 消息内重复），改写为 `toolu_<hex>` 再发给客户端。**自洽性**：客户端记录的是改写后的 id，下一请求里历史与 tool_result 引用自动一致，proxy 无需跨请求状态。挂钩点：流式 `_handle_tool_calls_delta` 的 `_content_block_start_tool` 前 + 非流式 507-523 段，历史 id 集合从 fwd_ctx 传入。实现：~80 行 + ctx 传递。风险：改写改变了"模型自己起的 id"，对依赖 id pattern 的模型（kimi 计数器）会打破其"最大 N+1"递推——这既是修复也是行为扰动，需观察 kimi 在 id 被改写后的续推行为（预期无妨，因为 `toolu_` 不混入 `_N` 序列）。
- **B2 唤醒回合标注（可选，谨慎）**：判定为"纯 tool_result 唤醒回合"时，在翻译后的 OpenAI 消息流里给 role:tool 内容前加一行系统式标注（如 `[tool result for call X, not a user message]`）。对弱模型可能显著降低误读，但这是**内容改写**，影响所有经该路由的会话，误判即污染。建议仅在按 supply 粒度（kimi-k3）开启 + 可配置开关。

### 档位 C：主动拦截（高成本、有误判代价、暂不建议默认开）

- **C1 重复派单熔断**：检测到 A2 模式且计数 ≥3 时，不转发，向客户端返回合成 assistant 响应（文本告知"检测到重复派单循环，已熔断，请人工确认"）。技术上 proxy 已有合成响应能力（`_write_builtin_stream_response`，内建命令层在用）。风险：熔断即干预会话——若误判（用户确实要批量派相似任务），用户看到莫名拒绝。必须先跑 A2 纯告警 1-2 周收集命中率，再考虑升级。
- **C2 "(no content)" 幻觉陈述检测**：响应 text/thinking 中出现"用户发了空消息/(no content)"类陈述且当前回合实为 tool_result 唤醒 → 告警/改写。**不建议**：自然语言模式匹配脆弱（中英文变体多、思考流里正常的元讨论会误命中），收益被 A2/B1 覆盖。

### 档位 D：路由层规避（治"供应"之本，与检测正交）

- 故障只在 kimi-k3 供应下出现（opus/sonnet/glm 服务时段无此问题）。可在策略层面对"主会话 agentic 负载"（特征：tools 列表含 Agent/Task、messages 含 tool_result 历史）做供应能力门槛，或在 proxy 统计侧给 supply 打"agentic 适配度"标签（SyntaxPreferenceStore 的按模型学习机制是先例）。这超出本次拦截主题，单列备查。

## 6. 根因定性

**主因是模型能力问题（kimi-k3），但存在一个协议层真实缺陷可被 proxy 修复。**

1. **主因（模型）**：(a) 把"纯 tool_result 唤醒回合"误读为"用户发了空消息"；(b) 编造不存在的对话事件并计数（"连续 N 条空消息"）；(c) 错误复盘自身行为（明明派了 6 次却说"一直没派出去"）；(d) tool_call id 用"可见上下文最大 N+1"的学习 pattern 生成，无唯一性意识。这些是上下文追踪 + 协议理解的模型短板。
2. **协议层缺陷（可修）**：重复 tool_use id 造成历史中 tool_use↔tool_result 配对歧义。健康模型能容忍，弱模型被它进一步带偏（症状反过来加重病因）。B1 的 id 唯一性改写修的是这个真实缺陷，成本可控。
3. **proxy 防护的整体定位**：A/B 档修协议缺陷 + 提供可观测性，属于**对症且有独立价值**（id 歧义对任何模型都是脏上下文）；C 档才是治标（防模型犯错的后果），且代价最高。治理的根本手段仍是供应选择（D 档）——不要在主会话 agentic 负载上用上下文追踪不可靠的模型。

## 7. 风险与权衡

- **proxy 多 client 服务**：所有检测必须按 session_key 维度做，且仅对 source==anthropic 开启；codex/openai 协议路径不动。无 session_key 的请求 fail-open（不检测），与内建命令层门控风格一致。
- **无状态边界**：A1/A2/B1 设计上**不需要跨请求状态**（历史在请求体内），符合 proxy 现有架构；若未来要"近 5 分钟窗口"类时间维度判定，需引入 per-session 内存状态（有 CooldownStore 先例，但要处理 TTL 与多进程）。
- **性能**：A1/A2 扫描 messages 是 O(历史消息数)，大会话（数千 block）每请求一次全扫有 CPU 开销，但相比上游 RTT（秒级）可忽略；可做大小上限保护。
- **B1 行为扰动**：改写 kimi 自生成 id 会打破其序号递推 pattern，需灰度观察（先仅日志"会改写"跑一段时间，再开改写）。
- **需用户确认的决策点**：(1) 是否接受 B2 这类内容改写进请求流；(2) C1 熔断的开启阈值与灰度节奏；(3) D 档是否立项（涉及路由策略语义变化）。

## 8. 验证方式

- **A1/A2 回归**：用故障窗口 transcript 重放构造请求体（含 6 次 Agent_221 重复段），喂给检测函数单测——应命中；用健康时段（glm/opus 服务）transcript 构造——应不命中。
- **B1 单测**：构造历史已含 `Agent_221` 的请求 + 上游响应再返回 `Agent_221` → 断言发出客户端的 id 已改写且历史不变；响应内同消息双 tool_call 同 id 场景同理。跑通现有 `tests/` 全量（当前 478 个）。
- **灰度观测**：A 档上线后看 ACCESS/warn 日志一周，核对命中是否全是 kimi-k3 会话、有无误报。
- **端到端复现**（可选）：用 Claudian + kimi 供应重跑同类派单任务，确认 A 档告警触发时机早于人工发现。

## 关联

- [[2026-08-04-in-band-route-command-design]]（内建命令层拦截点先例）
- [[2026-07-28-session-route-dispatch-design]]（session_key 提取与哈希路由）
- [[2026-08-07-reasoning-thinking-truncation-and-protocol-consistency]]（协议一致性相关）
- 故障 transcript：`~/.claude/projects/-Users-vincentwang-Documents-NoteVault/2896beec-d221-4013-a073-1ae74010a865.jsonl`
