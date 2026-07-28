---
type: design-decision
status: draft
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, load-balancing, feasibility]
---

# model_proxy 基于 session 的负载均衡：调研与可行性评估

> 本文只做"做不做、往哪个方向做"的判断，不含实施方案/代码。

## 背景与问题

`tools/model_proxy` 当前 supply 之间只有"异常轮转"（failover），无主动负载均衡。用户提出两个方向：
- **方向一**：同一 route 下多个 supply 间按 session 做 LB
- **方向二**：同一 strategy 下多个 route 间按 session 做 LB

需评估：技术可行性、改动量、兼容风险、真实收益，并对"session"如何定义给出明确结论或列为开放问题。

## 一、现状（已核实代码，非推测）

### 1.1 三层数据结构与选择逻辑

- 结构：`strategy(client_token) → route(route_id) → tier(opus/sonnet/haiku) → supplies[]`。
  - strategy 的 `route_id` 是**单值**（`resolve_route` 取 `s.get("route_id")`，`core/server.py:567`）。一个 strategy 只能绑一个 route。
  - route 的 `tiers` 是 `{tier: [supply_id,...]}`（`select_supply_list`, `server.py:601`）。
- 选择逻辑 `select_supply`（`server.py:606`）：在 supplies 列表里**有序取第一个"未冷却且未在本请求 tried_set 里"** 的 supply。**没有游标、没有轮转、没有权重、没有随机、没有 LB**。列表顺序 = 优先级。
- failover 主循环（`server.py:933-1153`）：请求内 `while True`，某 supply 返回 `{401,403,429}∪5xx` 或网络异常 → `cd.cooldown(supply_id)` + `tried_set.add(supply_id)` + `continue` 取下一个。`tried_set` 是**请求内局部集合**，不改全局。
- 冷却状态 `CooldownStore`（`server.py:406`）：`{supply_id: until_epoch}` 纯内存字典 + 线程锁，**不落盘**，进程重启清零。`failover` 开关挂在 route 上（`failover: on/off`）。

### 1.2 "session" 在代码里是否存在

- **不存在客户端来源的 session 标识。**
  - `conversation_id`（`translate.py:113 gen_conversation_id`）是**代理自己 uuid 生成**的，仅用于 responses 协议 adapter 内部构造事件（`translate.py:1182/1220`），不来自客户端、跨请求不保留。
  - `metadata.user_id`（`translate.py:470-473`）从入站 body 读取，但**仅在 anthropic→chat 转换时**映射到 OpenAI `user` 字段；PASSTHROUGH（anthropic→anthropic，占绝大多数流量）根本不解析它。
  - ACCESS 日志无任何会话/请求维度标识（`server.py` emit 字段：ms/status/source/route/tier/supply/failover/attempts/usage/token）。
- **进程模型**：单进程单例监听 18889。SessionStart hook（`hooker/ensure_model_proxy.sh`）只保证进程存活（幂等，已运行则退出），**不是每会话一进程**——所有 Claude Code 会话共享同一代理进程，进程内无"当前哪个会话"概念。
- **唯一潜在 session 线索**：Claude Code 的 Anthropic 请求 body 里 `metadata.user_id` 通常形如 `user_<hash>_account_<uuid>_session_<uuid>`，含 session 段；且代理透传除鉴权外的所有请求头（`_skip_req_headers` 仅 `{host,content-length,authorization,x-api-key}`，`server.py:1071`），故 `user-agent`/`x-stainless-*` 等也在但未被用于路由。**这些只是"可能可用"，其稳定性/语义未经验证，见开放问题。**

## 二、最近使用情况（真实数据，来源 .log + totals.json，2026-07-23~28，5天）

| 指标 | 数值 |
|---|---|
| 总请求 | 11943（ACCESS 行） |
| failover 触发 | **33 次 = 0.28%** |
| 实际发生轮转（attempts=2） | 13 次，**全部在 haiku tier（ds-pro 双 supply）** |
| route 分布 | claude 12179 / nation 50 / openai 11 |
| tier 分布 | sonnet 8907 / opus 2331 / haiku 998 |
| strategy(token) | cc 11878（≈99.5%），其余为 codex/临时 eval 少量 |
| 并发（同秒请求数） | 每秒 1 请求占 88%；峰值瞬时 10/秒仅 2 次，极罕见 |

多 supply 配置现状（config.json）：
- **claude route 的 opus / sonnet = 单 supply**（占 90%+ 流量，无 LB 施展空间）。
- 只有 claude/haiku（ds-pro ×2）、deepseek、nation 的部分 tier 配了 2 个 supply，且多是"同一 target_model 挂两个 appkey（3339/0956）"的冗余，目的在分摊单 appkey 限流。
- `supply=(none)` 大量 fail（totals 里 274+ 条）是 token 未匹配到 strategy 的错误请求，与 LB 无关。

**数据结论**：这是单人、单策略（cc）、近乎串行的使用模式。LB 的经典收益（削峰、分摊并发）在此几乎不成立；限流已由 failover 覆盖，且触发率极低（0.28%）。

## 三、两方向可行性评估

### 方向一：route 下多 supply 间基于 session 的 LB

- **技术可行性**：中。需在 `select_supply` 引入分发策略（按 session key 取模/一致性哈希到 supplies 列表），并与冷却/tried_set 共存——被选中 supply 若在冷却则回退到现有有序 failover。session key 来源需先定（见开放问题）。状态可仍纯内存。
- **改动量/耦合**：较小且局部。核心只改 `select_supply` 一处 + 一个 session-key 提取函数，failover 主循环基本不动。
- **兼容风险**：低。单 supply 的 tier（opus/sonnet）行为完全不变；只有多 supply tier 的选中顺序改变。需保证"session 粘性 + 冷却回退"不破坏 failover。
- **收益**：**有限但方向正**。仅在多 appkey 冗余 tier 上有"主动把请求散开、降低单 key 打满概率"的价值。**但前提是先把 opus/sonnet 也配成多 supply**，否则 90% 流量无从 LB。当前 haiku 双 supply + 0.28% failover 的现实下，收益接近可忽略。

### 方向二：strategy 下多 route 间基于 session 的 LB

- **技术可行性**：低-中。**先要改 schema**：strategy 从 `route_id`（单值）→ `route_ids`（列表）或引入 route 组概念，牵动 `resolve_route`、`_config_ops.py` 校验、status 展示等所有读取点。
- **改动量/耦合**：大。schema + 校验 + 选择逻辑 + tiers_source_capability（挂在 strategy 上，多 route 时能力建模如何归属需重新设计）+ 文档。
- **兼容风险**：中-高。route 语义是"**模型家族**"（claude/openai/deepseek/nation）。跨 route LB = 同一会话可能一会儿 claude、一会儿 deepseek，**这是模型切换而非负载均衡**，除非用户明确认为这些 route 是"等价可互换模型池"，否则语义错误、输出质量不可控。
- **收益**：**弱**。它解决的不是并发压力，而是"跨模型分摊配额"，但当前流量集中在单 route(claude)、无跨 route 分摊需求。改造大、语义存疑、收益低。

## 四、结论性建议

1. **当前不建议做任何一个方向**。真实数据（近串行、failover 0.28%、90% 流量单 supply）表明 LB 解决的问题在当前使用模式下基本不存在；failover 已足够兜底限流。做了是过度设计。
2. **若未来确有需求（并发上升 / 单 appkey 频繁 429），优先方向一**：改动小、语义正、与现有 failover 天然兼容。但**必做前置**：先把 opus/sonnet 配成多 supply（多 appkey），否则主流量无 LB 空间。
3. **方向二暂缓/不做**：schema 改造大、跨模型语义不成立，除非用户重新定义为"等价模型池"并接受输出模型不确定性。

## 五、开放问题（需用户拍板，代码无线索、不自行假设）

1. **"session"到底指什么？** 代码里无客户端来源的 session 标识。候选：
   - (a) 一次 Claude Code 会话生命周期 —— 需依赖 `metadata.user_id` 里的 `session_<uuid>` 段。**但其稳定性未验证**：`/compact`、`/resume`、新开会话时该段是否变化？跨请求是否恒定？请用户确认或允许抓包验证。
   - (b) 按 conversation 内容/首条 system 指纹哈希（无需客户端配合，但边界模糊）。
   - (c) 按某个稳定请求头（如 `x-stainless-*`，同样需验证稳定性）。
   - 选哪个直接决定方向一是否可落地。**这是做与不做的前置阻塞项。**
2. LB 想解决的**真实痛点**是什么？是"降低单 appkey 429 概率"（→ 方向一 + 补多 supply）、还是"跨模型分摊配额"（→ 方向二，需接受模型切换）、还是别的？痛点不清则方向无法定。
3. 是否接受"同一 session 粘在同一 supply，但该 supply 冷却时回退到 failover 顺序"这一妥协语义（session 粘性非绝对）？

## 验证方式

- 现状核实：以上代码行号可直接 `Read` 复核；数据可用仓库内 `.claude_model_proxy.log` / `.claude_model_proxy_totals.json` 重跑聚合（本文 grep 命令口径：`grep -c ACCESS`、`grep failover=1`、`grep -oE "supply=..."` 分组计数）。
- 开放问题 1 验证：临时在 `_forward` 里 DEBUG 打印入站 body 的 `metadata.user_id` 与关键请求头，跨"新会话/compact/resume"各发一次，观察 session 段是否变化（属排查动作，非本文实施范围）。

## 关联

- [[2026-07-23-model-proxy-full-audit]]
- [[2026-07-22-install-manage-sessionstart-hook]]
- config: `tools/model_proxy/config/model_proxy_config.json`
- 核心逻辑: `tools/model_proxy/core/server.py`（`select_supply` / `_forward` failover 循环 / `CooldownStore`）
