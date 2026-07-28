---
type: design-decision
status: draft
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, session-routing, load-balancing]
---

# model_proxy 方向二：strategy 下多 route 间的 session 级分配设计

> 用户已拍板走方向二（见前置可行性文档 `2026-07-28-session-load-balancing-feasibility.md`）。
> 本文是**具体设计方案**，不含实现代码。目标：同一 CC 会话全程稳定走同一 route，不同会话尽量均匀分布到多个 route，从而(a)让不同会话用到不同后端能力组合、(b)摊开各 appkey 配额。

## 背景与问题

CC 客户端只能配 opus/sonnet/haiku 三档，用户无法在 CC 内按会话手动选后端。诉求：代理层"看会话"分配 route——同一 session 全程固定一个 route（稳定），不同 session 打散到 strategy 下的多个 route（均衡）。

## 1. session 标识怎么稳定拿到（已核实，非推测）

**结论：可直接从请求 body 拿到稳定的 per-session 标识，无需 CC 额外配置、无需新增 header/env。**

- CC 发往 `/v1/messages` 的 body 含 `metadata.user_id`，实测格式为
  `user_<设备/账号hash>_account_<account_uuid>_session_<session_uuid>`
  （来源：多份 CC 抓包逆向分析，2026-03~06）。
- 其中 `session_<uuid>` 段 = CC 每次会话 `randomUUID()` 生成、存内存、**生命周期 = 单次会话**（来源：CC 指纹/封号机制逆向报告）。即**同一会话的所有轮次该段不变，不同会话不同**——正是所需的 session_key。
- 代理已在 `_forward` 里完整解析了 `body_json`（`core/server.py:867`），可直接取
  `body_json["metadata"]["user_id"]` 并正则提取 `_session_([0-9a-f-]+)` 段作为 `session_key`。PASSTHROUGH（anthropic→anthropic，占 90%+ 流量）当前不读它，但字段就在 body 里，取用零成本。

**排除的备选方案**：`ANTHROPIC_CUSTOM_HEADERS` 官方支持自定义请求头（CC env-vars 文档确认），但它是**进程级静态值**（`export` 一次、会话内固定），无法承载"每会话不同的 id"——除非每会话单起进程设不同值，而现有 SessionStart hook 是单进程共享模型，不满足。故不走此路，metadata.user_id 已足够。

**取不到 session_key 的兜底**（非 CC 客户端、字段缺失、格式变更）：退回 strategy 的默认 route（见 §4 schema 的 `default_route` / 列表首项），保证不 500。

### 待落地前实测确认（不阻塞设计，属验证动作）

`session_<uuid>` 段在以下场景是否保持不变，公开资料未 100% 覆盖，需开代理 DEBUG dump 实测：
- `/resume` 恢复会话（预期不变，同一会话）
- `/compact` 压缩上下文（预期不变，未新建会话）
- **子 agent（Task 工具派生的 subagent 请求）**：是否复用父会话 session_id，还是各自新 id？这直接影响"一个 CC 会话内派生的 subagent 会不会被分到不同 route"。**这一条最需要实测**。

## 2. 给定 session_key 如何在多 route 间稳定分配

**推荐：一致性哈希，纯函数、无状态、重启一致。**

- 机制：`idx = int(hashlib.md5(session_key.encode()).hexdigest, 16) % W`，`W` = 权重总和；按各 route 的累积权重区间落点，命中一个 route。无权重时退化为 `% N`。
- 性质：
  - **稳定**：session_key 全程不变 + 哈希是确定函数 ⇒ 同一会话每次都落同一 route。
  - **均衡**：不同 session_key 的 md5 近似均匀 ⇒ 大量会话按权重比例散开。
  - **无状态 / 无需落盘**：分配是 `session_key → route` 的纯函数，**进程重启后同 session_key 仍映射同 route**，不需要任何持久化。这是相对"记忆型分配表"的关键优势——省掉落盘、并发锁、状态一致性问题。

不推荐维护"session→route 映射表"落盘：既无必要（纯函数已稳定），又引入状态管理复杂度；唯一会需要它的场景是"记住某 session 曾 failover 到的 fallback route"，那属 §3 开放问题，先不引入。

## 3. 与现有 supply 级异常轮转（failover）的共存

**两层正交，是本方案最干净的部分。**

- 现有 failover 是 **route 内 supply 级**（`select_supply` + `CooldownStore` + 请求内 `tried_set`，`server.py:606/933`）。
- 方向二只新增**上层"选哪个 route"**一步；route 选定后，`tier → supplies → select_supply → 冷却/轮转`全链路**完全不动**。
- 因此：
  - pin 住的 route 内**某个 supply** 故障 → 现有 supply 级 failover 在该 route 内照常轮转，方向二无感知、无影响。
  - pin 住的 route 内**整个 tier 的 supply 全部**不可用（全 cooling/全失败）→ 现状返回 503。**要不要跨 route 跌落**是真正的开放问题：

### 开放问题（route 全挂时的行为，请用户选，不替选）

| 选项 | 行为 | 代价 |
|---|---|---|
| **A. 绝对粘性** | pin route 全挂即返回 503，不跨 route | 语义最简单、可预测、无状态；但单 route 挂会让该 session 完全不可用，可用性差 |
| **B. 粘性 + 兜底跨 route failover** | pin route 全挂时，按同一哈希顺序（或列表顺序）尝试 strategy 下其他 route，成功则用之 | 可用性最好；但同一会话可能中途换到能力不同的 route（模型家族变了，输出风格/能力跳变），且需在 ACCESS 日志加"跨 route failover"标记以可观测 |
| **C. 粘性 + 兜底 + 会话内记忆跌落目标** | 同 B，但跌落后把该 session 临时 pin 到 fallback route（避免反复试挂掉的主 route） | 可用性与稳定性最佳；但**打破无状态**，需引入 session→fallback 的内存映射（重启丢失、需 TTL 清理），复杂度上升 |

我的中性提示（不代表替选）：若配额分摊是主诉求、且各 route 是"可接受互替的能力池"，B 性价比高；若各 route 能力差异大、不希望会话中途换模型，A 最稳；C 仅在 B 的"反复重试主 route"成为实际痛点时才值得。

## 4. 配置 schema 改动草案（贴近现有风格，不含实现）

现状：`strategy.route_id` 单值。改为可表达"N 个 route + 分配规则"，**保持向后兼容**（单值 route_id 继续有效 = 单 route、不分配）。

草案（新增 `route_pool` + `dispatch`，与 `route_id` 二选一）：

```jsonc
{
  "client_token": "cc",
  // 二选一：保留旧字段（单 route，行为不变）
  // "route_id": "claude",

  // 或新写法：多 route 池 + 分配策略
  "route_pool": [
    { "route_id": "claude",   "weight": 2 },
    { "route_id": "deepseek", "weight": 1 },
    { "route_id": "nation",   "weight": 1 }
  ],
  "dispatch": {
    "type": "session_hash",          // 首版唯一实现；见下方待选
    "session_key_source": "metadata.user_id",  // 从该字段提取 _session_ 段
    "fallback": "on_missing_first"   // 取不到 session_key → 用 route_pool 首项
  },

  "tiers_source_capability": { /* 不变，见下 */ }
}
```

- `tiers_source_capability` 仍挂 strategy、结构不变：它建模的是**客户端侧能力**（strategy=客户端身份），与"选哪个 route"无关，故多 route 不影响它。
- 校验规则：`route_id` 与 `route_pool` 互斥；`route_pool` 每项 route_id 必须在 `routes` 中存在；weight 为正整数（缺省 1）。

### 待用户选的路径分歧（务实 vs 理想，不替选）

两条路径 schema 与改动范围**实质不同**，请用户拍板：

- **[务实] 只做够用的 session_hash**：`dispatch.type` 固定 `session_hash`，一致性哈希 + 权重写死一种分配算法。schema 里 `dispatch` 可以更瘦（甚至省掉 `type`）。改动最小、够满足当前诉求。
- **[理想] 可配置分配策略框架**：`dispatch.type` 作为策略选择器，预留 `session_hash` / `explicit_map`（显式 session 前缀→route 映射）/ `weighted_random` / `round_robin` 等，代码侧抽象一个策略注册点便于后续扩展。schema 更完整，但要多写抽象层与多策略实现。

折中选项：**schema 先按理想预留 `dispatch.type` 字段，但首版只实现 `session_hash`**——结构可扩展、工作量接近务实。若用户认可这个折中，可直接采纳，无需在两极间选。

## 5. 改动量与耦合面

| 文件/模块 | 改动 |
|---|---|
| `core/server.py` | 新增 `extract_session_key(body_json)`（解析 metadata.user_id 的 session 段）+ `select_route(strategy, session_key, routes_map)`（哈希分配 + fallback）；`_forward` 在"resolve_strategy 之后、resolve_tier 之前"插入 route 选择（替换现在 `route = routes_map.get(strategy.get("route_id"))` 一处，`server.py:881`）；若选 §3 的 B/C 选项，failover 循环末尾加"pin route 全挂后跨 route"分支 + ACCESS 日志加字段 |
| `_config_ops.py` | strategy 的 CRUD 与 schema 校验支持 `route_pool` / `dispatch`；`route_id` 与 `route_pool` 互斥校验；向后兼容旧单值 |
| `config/model_proxy_config.example.json` | 加一个多 route 池的示例 strategy |
| `README.md` | 补 session 分配机制、schema 说明 |
| （可选）ACCESS 日志 | 加 `session4`（session_key 尾4位）或 route 分配来源，便于验证"同会话是否稳定、跨会话是否均匀" |

**量级判断**：属**有正确性耦合**的改动——改 schema 要连带改校验、主流程 route 选择、并严格保证旧单值 `route_id` 配置（现网 cc/codex）不被破坏；`select_route` 与 failover 的边界要精确。核心逻辑集中（不算大 diff），但正确性敏感。**建议派 implementer 落地 + reviewer 复核**，不适合 runner 铺。

## 风险与权衡

- **session_key 稳定性未 100% 实测**（§1 待确认三项，尤其 subagent）：若 subagent 请求带不同 session_id，则一个 CC 会话内的 subagent 会被分到别的 route——可能正是想要的（分摊），也可能不符合"整个会话一个 route"预期，取决于用户意图。**落地第一步应先 DEBUG dump 实测这三项，再定分配粒度。**
- **归因指纹与 prefix cache**（旁支提示，非本方案引入）：CC 请求前缀含动态指纹块，第三方链路不剥离会破坏上游 prompt cache。这与 session 分配无关，但既然要读 body，提示一句：session_key 只从 `metadata.user_id` 稳定段取，勿用会变字段。
- **跨 route = 跨模型家族**：route 语义是模型家族，多 route 分配意味着不同会话实际用不同模型能力。用户诉求(a)明确接受这点，但需确保 route_pool 里各 route 的三档 tier 都配了可用 supply，否则某会话被分到"缺该 tier 的 route"会 503。

## 验证方式

1. **session_key 稳定性**：临时开代理 DEBUG，dump 入站 `metadata.user_id`；在同一会话多轮、`/resume`、`/compact`、触发 Task subagent 各观察 session 段是否恒定。
2. **分配稳定性**：同一 session_key 多次请求 → ACCESS 日志中 route 恒定。
3. **分配均衡性**：跑 N 个不同会话，统计各 route 命中比例是否≈权重比例。
4. **failover 共存**：手动把 pin route 的某 supply 置坏 key → 确认 route 内 supply 级 failover 仍生效、不跨 route（选项 A）或按选定选项跨 route（B/C）。
5. **向后兼容**：现网 cc/codex 的单值 `route_id` 配置不改动即行为完全不变。

## 关联

- 前置可行性判断：[[2026-07-28-session-load-balancing-feasibility]]（结论"当前不建议做/优先方向一"已被用户决策覆盖为"做方向二"）
- [[2026-07-23-model-proxy-full-audit]]
- 核心逻辑：`tools/model_proxy/core/server.py`（`_forward` L881 route 解析处 / `select_supply` / `CooldownStore`）
- schema/校验：`tools/model_proxy/_config_ops.py`
