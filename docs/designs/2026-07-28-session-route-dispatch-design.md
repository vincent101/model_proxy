---
type: design-decision
status: confirmed
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, session-routing, load-balancing]
---

# model_proxy 方向二：strategy 下多 route 间的 session 级分配设计

> 用户已拍板走方向二（见前置可行性文档 `2026-07-28-session-load-balancing-feasibility.md`）。
> 本文是**具体设计方案**，不含实现代码。目标：同一 CC 会话全程稳定走同一 route，不同会话尽量均匀分布到多个 route，从而(a)让不同会话用到不同后端能力组合、(b)摊开各 appkey 配额。

## 背景与问题

CC 客户端只能配 opus/sonnet/haiku 三档，用户无法在 CC 内按会话手动选后端。诉求：代理层"看会话"分配 route——同一 session 全程固定一个 route（稳定），不同 session 打散到 strategy 下的多个 route（均衡）。

## 1. session 标识怎么稳定拿到（已在沙箱用真实 CC 请求实测，2026-07-28）

**结论：可直接从请求 body 拿到稳定的 per-session 标识，无需 CC 额外配置、无需新增 header/env。但取值格式与本文档最初的推测不同，已按实测结果修正。**

> ⚠️ **格式假设已被实测纠正**：下方最初写的 `user_<hash>_account_<uuid>_session_<uuid>` 拼接字符串格式**不成立**，是未经验证的推测。真实格式见下。

- CC 发往 `/v1/messages` 的 body 含 `metadata.user_id`，**实测真实格式是一个 JSON 字符串**（沙箱环境用 `claude --setting-sources project,local` + 临时 `ANTHROPIC_BASE_URL` 覆盖打真实请求验证，非猜测）：
  ```json
  {"device_id":"<设备hash>","account_uuid":"","session_id":"<session_uuid>"}
  ```
  取值需要**二次 `json.loads`**：先取 `metadata.user_id`（一个字符串），再对这个字符串本身做 JSON 解析，取里面的 `session_id` 字段——不是原方案设想的正则抠子串。
- 该 `session_id` = CC 每次会话生成、**生命周期 = 单次会话**，且**实测确认**：
  - 同一会话跨轮次（`--session-id` 起始 + 后续请求）：session_id 不变。
  - `-r <session_id>` 显式 resume：session_id 与恢复前一致，不变。
  - `/compact` 触发的请求：session_id 不变。
  - **子 agent（Task 工具派生请求，用 Explore 类型子agent实测）：复用父会话的 session_id，不生成新 id。** 整个"父请求 + 子agent 发出的多次调用"窗口内 `session_id` 全部相同。这意味着一个 CC 会话内派生的所有子 agent 请求会被分到**同一个** route，不会被打散到不同 route——如果用户预期"子agent也算独立会话参与分摊"，现状不满足，但如果预期"整个会话（含子agent）走同一后端"，现状恰好满足。
- 代理已在 `_forward` 里完整解析了 `body_json`（`core/server.py:867` 附近），取用零成本，只需按上述二次解析写正确的 `extract_session_key`。PASSTHROUGH（anthropic→anthropic，占 90%+ 流量）当前不读它，但字段就在 body 里。

**排除的备选方案**：`ANTHROPIC_CUSTOM_HEADERS` 官方支持自定义请求头（CC env-vars 文档确认），但它是**进程级静态值**（`export` 一次、会话内固定），无法承载"每会话不同的 id"——除非每会话单起进程设不同值，而现有 SessionStart hook 是单进程共享模型，不满足。故不走此路，metadata.user_id 已足够。

**取不到 session_key 的兜底**（非 CC 客户端、字段缺失、格式变更、非标准 JSON）：退回 strategy 的默认 route（见 §4 schema 的 `default_route` / 列表首项），保证不 500。

### 实测方法记录（供复现/后续回归）

沙箱环境：`/tmp/model_proxy_sandbox`，端口 18899，与生产（`tools/model_proxy/`，端口 18889，pid 61666）完全隔离。用真实 `claude` CLI 触发请求指向沙箱的方法：

```bash
env ANTHROPIC_BASE_URL="http://127.0.0.1:18899/" ANTHROPIC_AUTH_TOKEN="cc" \
  claude --setting-sources project,local -p "<prompt>" --session-id "<uuid>"
```

关键点：**必须带 `--setting-sources project,local`**（排除 `user` 来源），否则 `~/.claude/settings.json` 里的全局 `env.ANTHROPIC_BASE_URL` 优先级更高，会覆盖掉 shell 临时 env 变量，导致请求仍打到生产 18889（此前一次尝试未加此参数，误打了几条真实请求到生产，生产进程本身无损，仅多耗费几次真实配额，已知悉）。resume 用 `-r <session_id>`（而非 `-c`，`-c` 是"续最近一个会话"，在多会话并行时会选错）。

### 已解决，无需再实测的项

原文档标注的三项待实测（`/resume`、`/compact`、子agent）**均已完成实测**，结论见上，不再是开放问题。

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

## 4b. 手动指定 session → route（显式覆盖机制）

### 先回答直接问题：现状不保存、不能手动改

**§2 的一致性哈希方案分配结果是运行时纯计算得出的，不落盘、不写任何 session→route 映射文件。** 这是它"无状态、重启一致"的代价——**现状下用户无法手动指定某个 session 走某个 route**。`hash(session_key) % 权重` 每次现算，改不了、也看不到。要支持手动指定，必须**额外**引入一个显式映射（本节补充），它是加在哈希之上的一层，不是原方案已有的能力。

### 补充设计：`session_overrides` 显式映射，优先于哈希

**放在哪 / 什么结构**：放在 strategy 配置里新增 `session_overrides` 字段（一个 `session_key → route_id` 的对象），**不建议独立文件**。理由：现有 config 是单文件 `model_proxy_config.json`（strategy/route/supply 全在内），且已有 mtime 热重载（`ConfigStore.maybe_reload`，`server.py:348`）——放进同一文件可直接复用热重载，改完存盘即生效、无需重启，也不新增文件管理成本。

草案（挂在 strategy 的 `dispatch` 下，紧邻分配规则）：

```jsonc
{
  "client_token": "cc",
  "route_pool": [
    { "route_id": "claude",   "weight": 2 },
    { "route_id": "deepseek", "weight": 1 },
    { "route_id": "nation",   "weight": 1 }
  ],
  "dispatch": {
    "type": "session_hash",
    "session_key_source": "metadata.user_id",
    "fallback": "on_missing_first",
    "session_overrides": {
      "3f2a9c1e-....-uuid": "deepseek",   // 手动把这个会话钉到 deepseek
      "8b7d....-uuid":     "nation"
    }
  }
}
```

### 运行时优先级（清晰的两级）

选 route 时：
1. **先查 `session_overrides[session_key]`** —— 命中且该 route_id 存在于 `routes` 中 → 直接用它，**跳过哈希**。
2. **查不到** → 落回 §2 的 `hash(session_key) % 权重` 自动分配。
3. `session_key` 取不到（非 CC / 字段缺失）→ 走 §4 的 `fallback`（route_pool 首项）。

即 `select_route(strategy, session_key)` 的逻辑变为：`overrides 查表 → 命中即返回；否则哈希`。改动仍集中在这一个函数内，主流程不额外变。

### 可观测性：用户怎么拿到要填的 session_id（必须一并解决）

**这是前置阻塞点**：目前 ACCESS 日志不记录 session_id（见 §1，代理内部解析但从不落日志），用户**根本不知道要往 overrides 里填哪个 id**。所以手动覆盖机制必须**配套在 ACCESS 日志加 session 标识字段**，否则不可用。

方案（够用即可）：
- 在 ACCESS 日志每行加 `session=<session_key>` 字段（与现有 `token=cc` 同风格）。用户在 CC 里跑一轮 → `tail` 日志即可看到当前会话的 session_key → 复制去填 `session_overrides`。
- **建议记录完整 session 段**（不截断），让 overrides 用精确全等匹配，最简单无歧义。若担心日志体积/隐私想记尾若干位，则 overrides 需相应支持"后缀匹配"——**不推荐后缀匹配**（引入歧义与误钉风险），故倾向记全量 + 全等匹配。

（可选增强，非必需）加一个只读查询端点 `GET /model_proxy/sessions`，列出近期见过的 `session_key → 当前被分到的 route`，省去翻日志。属锦上添花，最小方案用日志字段即可。

### 临时/过期？—— 不做，给"手动增删"即够用

- **不引入 TTL / 过期 / 临时标记**。overrides 是用户手写进 config 的静态覆盖，语义就是"我要这个会话固定走这个 route"；会话结束后该条目变成无害的僵尸条目（对应 session_key 再不出现，永不命中），不影响任何请求。用户想清理就手动删——与"手动加"对称，认知成本最低。
- 加 TTL 需要落盘时间戳 + 定期清理 + 时钟管理，是过度设计，不做。
- 若日后僵尸条目积累碍眼，靠上面可选的 `/model_proxy/sessions` 端点辅助识别哪些还活跃即可，仍不需要自动过期。

### 与原方案是否冲突（逐项确认无冲突）

- **与哈希自动分配**：不冲突，纯"查表命中优先、否则哈希"两级串联；`session_overrides` 缺省为空 `{}` 时行为 = 原纯哈希方案，完全向后兼容。
- **overrides 指向的 route 要不要在 route_pool 里重复定义？**——**不需要，且刻意允许它超出 route_pool**。overrides 的 route_id 只要求存在于顶层 `routes`（全局 route 定义），**不要求也在本 strategy 的 route_pool 权重列表内**。这是有意的：手动覆盖本就是"例外指定"，用户可能想把某会话钉到一个不在自动分摊池里的 route（如临时全 deepseek 跑某个重活）。校验规则：overrides 的 value 必须是合法 `routes` id；不校验其是否在 route_pool 内。哈希分配仍只在 route_pool 内进行，两者互不干扰。
- **与开放问题 §3（route 全挂 A/B/C）**：不冲突。overrides 只决定"初始选哪个 route"，选定后该 route 全挂时的行为仍由 §3 选定的选项接管（若选 B/C 跨 route 兜底，被 override 钉住的会话在其 pin route 全挂时同样按该选项跌落）。
- **与 schema 务实/理想/折中（§4）**：正交。`session_overrides` 作为 `dispatch` 下一个可选字段，三种 schema 取向都能容纳它。

## 5. 改动量与耦合面

| 文件/模块 | 改动 |
|---|---|
| `core/server.py` | 新增 `extract_session_key(body_json)`（解析 metadata.user_id 的 session 段）+ `select_route(strategy, session_key, routes_map)`（**先查 session_overrides，未命中再哈希** + fallback）；`_forward` 在"resolve_strategy 之后、resolve_tier 之前"插入 route 选择（替换现在 `route = routes_map.get(strategy.get("route_id"))` 一处，`server.py:881`）；**ACCESS 日志加 `session` 字段**（供用户查 session_key，见 §4b）；若选 §3 的 B/C 选项，failover 循环末尾加"pin route 全挂后跨 route"分支 + 日志标记 |
| `_config_ops.py` | strategy 的 CRUD 与 schema 校验支持 `route_pool` / `dispatch`（含 `session_overrides`）；`route_id` 与 `route_pool` 互斥校验；`session_overrides` 的 value 必须是合法 `routes` id（不校验是否在 route_pool 内，见 §4b）；向后兼容旧单值 |
| `config/model_proxy_config.example.json` | 加一个多 route 池的示例 strategy |
| `README.md` | 补 session 分配机制、schema 说明 |
| ACCESS 日志（**必需**，非可选） | 加 `session=<session_key>` 字段——既用于验证"同会话稳定/跨会话均匀"，也是 §4b 手动覆盖的前提（用户靠它拿到要填的 session_id）。若日后要 `/model_proxy/sessions` 只读端点则另加，属可选增强 |

**量级判断**：属**有正确性耦合**的改动——改 schema 要连带改校验、主流程 route 选择、并严格保证旧单值 `route_id` 配置（现网 cc/codex）不被破坏；`select_route` 与 failover 的边界要精确。核心逻辑集中（不算大 diff），但正确性敏感。**建议派 implementer 落地 + reviewer 复核**，不适合 runner 铺。

## 风险与权衡

- **session_key 稳定性已实测（§1）**：resume/compact/子agent 三项均确认 session_id 不变，子agent 复用父会话 id。风险已从"未验证"转为"已知行为"——一个 CC 会话（含其派生的所有子agent请求）会被视为同一个分配单位，整体落到同一个 route，不会被打散分摊。若用户诉求是"子agent 也算独立单位参与配额分摊"，现状不满足此诉求，需另行讨论（例如改用其他 session_key 来源），但不阻塞当前方案落地。
- **归因指纹与 prefix cache**（旁支提示，非本方案引入）：CC 请求前缀含动态指纹块，第三方链路不剥离会破坏上游 prompt cache。这与 session 分配无关，但既然要读 body，提示一句：session_key 只从 `metadata.user_id` 稳定段取，勿用会变字段。
- **跨 route = 跨模型家族**：route 语义是模型家族，多 route 分配意味着不同会话实际用不同模型能力。用户诉求(a)明确接受这点，但需确保 route_pool 里各 route 的三档 tier 都配了可用 supply，否则某会话被分到"缺该 tier 的 route"会 503。

## 验证方式

1. **session_key 稳定性**：已在沙箱用真实 CC 请求实测完成（见 §1），同一会话多轮/`/resume`/`/compact`/子agent 均确认恒定，不再需要重复此步。
2. **分配稳定性**：同一 session_key 多次请求 → ACCESS 日志中 route 恒定。
3. **分配均衡性**：跑 N 个不同会话，统计各 route 命中比例是否≈权重比例。
4. **failover 共存**：手动把 pin route 的某 supply 置坏 key → 确认 route 内 supply 级 failover 仍生效、不跨 route（选项 A）或按选定选项跨 route（B/C）。
5. **向后兼容**：现网 cc/codex 的单值 `route_id` 配置不改动即行为完全不变；`session_overrides` 缺省为空时行为 = 纯哈希方案。
6. **手动覆盖（§4b）**：从 ACCESS 日志取某活跃会话的 `session` 值 → 写入 `session_overrides` 指到某 route → 热重载后确认该会话后续请求全落到指定 route；删除条目后确认落回哈希分配。

## 关联

- 前置可行性判断：[[2026-07-28-session-load-balancing-feasibility]]（结论"当前不建议做/优先方向一"已被用户决策覆盖为"做方向二"）
- [[2026-07-23-model-proxy-full-audit]]
- 核心逻辑：`tools/model_proxy/core/server.py`（`_forward` L881 route 解析处 / `select_supply` / `CooldownStore`）
- schema/校验：`tools/model_proxy/_config_ops.py`
