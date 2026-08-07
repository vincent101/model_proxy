---
type: design-decision
status: confirmed
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, supply, failover, nation-route]
---

# nation route supply 扩展与双 route 均衡

> [务实] 路径产出：用户已拍板扩展方向（加 supply、拆 nation 为 nation1/nation2、route_pool 双 route 均衡、清 session 覆盖），本文做可行性核实 + 配置方案 + 风险评估。
>
> 前置：[[2026-07-28-session-route-dispatch-design]]（route_pool / 一致性哈希 / failover 机制）、[[2026-08-04-in-band-route-command-design]]（$route 命令层，清理 session 覆盖会用到）

## 0. 用户已拍板的决策（不再评估"要不要做"）

1. 给现有 3339 appkey 的模型增加 5 个新 appkey 的 supply（同账号不同 key，限流隔离）
2. 现 nation route 改名 nation1，三档各补到 3 个 supply（现仅 1 个）
3. 新增 nation2 route，三档各配 3 个 supply
4. strategy=cc 的 route_pool 从 `[nation1]` 改为 `[nation1, nation2]`，权重均 1，做均衡
5. 清掉 sidecar 里全部 session 覆盖（现在都指向 nation，与新 nation1/nation2 语义不符，且 route_pool 已均衡，不需要 override）

## 1. 现状核实（已查清）

### 1.1 现 nation route 三档（只有 3 个 3339 supply）

```
nation.opus:   [kimi-k3-sankuai-3339]      → kimi-k3, appkey 3339
nation.sonnet: [glm-52-sankuai-3339]       → glm-5.2, appkey 3339
nation.haiku:  [ds-pro-sankuai-3339]       → deepseek-v4-pro, appkey 3339
```

**这就是 503 集中在 nation/opus 的根因**：每档只有 1 个 supply，该 supply 一旦 429/503 进入冷却，该档无可用候选 → 直接 503 给客户端（attempts=1 或 2 后全败）。failover 机制（`select_supply` 有序取第一个未冷却未试过的）本可支持多 supply 轮换，但配置上只给了 1 个，failover 无处可转。

### 1.2 7 个 3339 supply 中只有 3 个被 nation 用

| supply id | model | protocol | 用于 |
|---|---|---|---|
| kimi-k3-sankuai-3339 | kimi-k3 | anthropic | **nation.opus** |
| glm-52-sankuai-3339 | glm-5.2 | anthropic | **nation.sonnet** |
| ds-pro-sankuai-3339 | deepseek-v4-pro | anthropic | **nation.haiku**（也用于 claude.haiku） |
| kimi-k3-sankuai-openai-3339 | kimi-k3 | chat | 未被 route 用 |
| glm-52-sankuai-openai-3339 | glm-5.2 | responses | 未被 route 用 |
| glm-51-sankuai-3339 | glm-5.1 | anthropic | 未被 route 用 |
| ds-flash-sankuai-3339 | deepseek-v4-flash | anthropic | 未被 route 用 |

**决策点 A（需用户拍板）**：扩展范围只针对 nation 实际用的 3 个模型（kimi-k3 / glm-5.2 / deepseek-v4-pro，都是 anthropic 协议），还是把 4 个未用的也一并扩展？

- **推荐：只扩 3 个**。那 4 个未用的协议变体/低档模型当前无 route 引用，扩了也无 route 消费，徒增配置体积。若未来 nation2 想用 chat/responses 协议或 glm-5.1/ds-flash，再按需扩。
- 若用户想"一次性把 3339 全扩了备用"也可，但本文默认按推荐方案（只扩 3 个）设计，§5 给出扩展到全 7 个的差量。

### 1.3 failover 机制（已核实 `select_supply`，`server.py:725`）

```python
for sid in supplies:              # 有序遍历
    if sid in tried_set: continue # 跳过已试
    if sid not in supply_map: continue
    if cooldown.is_cooling(sid): continue  # 跳过冷却中
    return supply_map[sid]        # 取第一个可用的
```

**这是有序 failover，不是负载均衡**：第一个 supply 挂了（429/503）进冷却，下次请求自动用第二个，再挂用第三个。三个都冷却中 → 该档 503。

**对方案的含义**：
- "3 个 supply 轮换"在现有机制下天然成立，不需要改代码
- **顺序有意义**：列在第一位的会被优先用，前一个挂了才用后一个。nation1 和 nation2 的三档 supply 顺序会影响哪个 key 被优先打。建议把 3339（原 key）放第一位，新 key 按尾4 排序跟随，保持稳定可预期。

### 1.4 route_pool 一致性哈希 + 1:1 权重（已核实）

`extract_route_candidates`（`server.py:609`）对 `route_pool` 做 `md5(session_key) % 权重总和` 定位主选 route。两个 route 权重都 1：
- 约一半 session 主选 nation1、另一半主选 nation2
- 主选 route 全挂时，候选列表里的另一个 route 作跨 route 兜底（route_failover）

**对方案的含义**：
- route 层 1:1 均衡是 session 级的（同一 session 稳定走 nation1 或 nation2），不是请求级轮换
- 加上 nation1/nation2 内部各 3 个 supply，**单个 session 的请求会固定优先打某个 route 的某个 supply**，该 supply 挂了才在 route 内 failover，route 内 3 个都挂了才跨 route 兜底
- 6 个 key 真正分散：nation1 用 3 个、nation2 用另外 3 个，不重叠 → 6 个 key 都能用上，且任一 session 的 failover 链最多 6 步（3 route内 + 3 跨route）

### 1.5 session 覆盖现状（sidecar，6 条都指向 nation）

```
cc: {
  2896beec...: nation, 7b4cb865...: nation, 6ad2e1b5...: nation,
  4c3ba96f...: nation, c2e29916...: nation, cf9e4ee3...: nation
}
```

**问题**：`nation` 这个 route id 在新方案里改名为 `nation1`，这 6 条 override 指向的 `nation` 会失效（`routes_map` 里不再有 `nation`，只有 `nation1`/`nation2`）。`extract_route_candidates` 对 override 命中但 route_id 不存在于 routes_map 的情况会**忽略 override 落回哈希分配**（已核实 `server.py:675-680`：`override_rid and override_rid in routes_map` 才生效）。

**所以即使不清，这 6 条也会自动失效**。但用户明确要求清掉（语义已过时），方案里给出清理方式。

## 2. 方案设计

### 2.1 新增 supply（25 个，只扩 nation 用的 3 个模型）

每个模型扩 5 个新 appkey supply，命名规则沿用 `{model}-sankuai-{appkey尾4}`：

| 模型 | 现有(3339) | 新增 5 个 |
|---|---|---|
| kimi-k3 (anthropic) | kimi-k3-sankuai-3339 | kimi-k3-sankuai-8101, -9907, -2330, -4200, -6372 |
| glm-5.2 (anthropic) | glm-52-sankuai-3339 | glm-52-sankuai-8101, -9907, -2330, -4200, -6372 |
| deepseek-v4-pro (anthropic) | ds-pro-sankuai-3339 | ds-pro-sankuai-8101, -9907, -2330, -4200, -6372 |

每个新 supply 结构（以 kimi-k3-sankuai-8101 为例）：
```json
{
  "id": "kimi-k3-sankuai-8101",
  "url": "https://aigc.sankuai.com/v1/anthropic/v1/messages",
  "protocol": "anthropic",
  "appkey": "21896456862825218101",
  "target_model": "kimi-k3",
  "reasoning_capability": {"effort_enum": ["low","high","max"]}
}
```
`url`/`protocol`/`target_model`/`reasoning_capability` 沿用对应 3339 supply 的值，只改 `id` 和 `appkey`。

**appkey 尾4 与完整值对照**（配置时用完整值）：
- 8101 → 21896456862825218101
- 9907 → 22032067268943609907
- 2330 → 22032068324364472330
- 4200 → 22032068370569224200
- 6372 → 22032070810785263672

### 2.2 nation1（原 nation 改名，三档各 3 个 supply）

不重叠分配：nation1 用 3339 + 8101 + 9907（前 3 个 key）。

```json
{
  "id": "nation1",
  "tiers": {
    "opus":   ["kimi-k3-sankuai-3339", "kimi-k3-sankuai-8101", "kimi-k3-sankuai-9907"],
    "sonnet": ["glm-52-sankuai-3339",  "glm-52-sankuai-8101",  "glm-52-sankuai-9907"],
    "haiku":  ["ds-pro-sankuai-3339",  "ds-pro-sankuai-8101",  "ds-pro-sankuai-9907"]
  },
  "failover": "on"
}
```

### 2.3 nation2（新增，三档各 3 个 supply）

不重叠分配：nation2 用 2330 + 4200 + 6372（后 3 个 key）。

```json
{
  "id": "nation2",
  "tiers": {
    "opus":   ["kimi-k3-sankuai-2330", "kimi-k3-sankuai-4200", "kimi-k3-sankuai-6372"],
    "sonnet": ["glm-52-sankuai-2330",  "glm-52-sankuai-4200",  "glm-52-sankuai-6372"],
    "haiku":  ["ds-pro-sankuai-2330",  "ds-pro-sankuai-4200",  "ds-pro-sankuai-6372"]
  },
  "failover": "on"
}
```

### 2.4 strategy=cc 改 route_pool

```json
{
  "client_token": "cc",
  "route_pool": [
    {"route_id": "nation1", "weight": 1},
    {"route_id": "nation2", "weight": 1}
  ]
}
```
原 `nation` route 从 `routes` 数组里删除（或保留但不被任何 route_pool 引用——建议删除避免混淆）。

### 2.5 清 session 覆盖

两种方式：
- **方式 A（推荐）**：直接删 sidecar 文件 `config/session_overrides.json`。代理 mtime 热重载会感知，`_reload_locked` 文件缺失视为 `{}`。零代码、零风险。
- **方式 B**：用 `$route reset`（每个 session 逐条清），但要发 6 次，且当前 session 的 reset 会把自己清回哈希分配——可行但麻烦。

**用方式 A**：`rm config/session_overrides.json`（或备份后删）。

## 3. 风险与注意事项

### 3.1 新 appkey 需在线验证（实施前必做）
5 个新 appkey 虽是同账号 key，但**是否都已开通 kimi/glm/ds 模型权限需实测**。未开通的 key 配上去会 401，failover 能兜但会浪费一次 attempts。
**验证方法**：配置前用 `model_proxy_cli.sh` 的 supply 连通性测试（或直接 curl）逐个 key 测三个模型。设计文档 §5 给验证脚本骨架。

### 3.2 nation route 改名的连带影响
- `routes` 数组里 `nation` → `nation1`（改名）
- `cc.strategy.route_pool` 里 `nation` → `nation1`/`nation2`（已在 §2.4）
- **sidecar 6 条 override 指向 nation**：自动失效（§1.5 已分析），且本方案要清掉，无影响
- **代码里有没有硬编码 "nation"？** 已 grep：`core/server.py`/`commands.py` 无硬编码 route id，route 名都是配置驱动。安全。

### 3.3 failover 链变长
原来：1 个 supply 挂 → 503。现在：nation1 内 3 个都挂 → 跨 route 到 nation2 → nation2 内 3 个 → 全挂才 503。**最坏 6 步 attempts**，单请求延迟可能上升（每个挂的 supply 要等 429/503 返回）。但 503 概率大幅下降，总体可用性提升。

### 3.4 冷却时长
`default_cooldown_seconds: 60`（现网）。一个 supply 429 后冷却 60 秒，期间 failover 到同档下一个。60 秒后原 supply 解冷却重新可用。多 key 轮换下，60 秒通常够上游恢复。**不改冷却时长**。

### 3.5 一致性哈希的 session 分布
两个 route 权重 1:1，`md5(session_key) % 2` 决定主选。**现有 6 个 session 的主选会重新分配**（因为 route_pool 从 1 个变 2 个，哈希结果变了）——但反正要清 session 覆盖，且新分配是均衡的，可接受。

## 4. 实施步骤（逐个确认，不自动执行）

1. **验证 5 个新 appkey 对 kimi/glm/ds 的权限**（§3.1，必做）
2. **编辑 `config/model_proxy_config.json`**：
   - 加 15 个新 supply（3 模型 × 5 key）
   - `routes` 里 `nation` 改名 `nation1`、三档补到 3 个 supply
   - 新增 `nation2` route
   - `strategies[cc].route_pool` 改为 `[nation1, nation2]`
3. **删 sidecar**：`rm config/session_overrides.json`（或备份后删）
4. **重启代理**：`model_proxy_cli.sh off && on`，让新 config 生效（config mtime 热重载其实不用重启，但改名+大改建议重启确保干净）
5. **验证**：
   - `model_proxy_cli.sh status` 确认 nation1/nation2 三档各 3 个 supply、cc route_pool 两个
   - 发一个请求看走哪个 route/supply（看 ACCESS 日志 `route=nation1 supply=...`）
   - 观察 failover 是否在 3 个 supply 间轮换（grep `failover=1`）

## 5. 决策点 A 的差量：若要扩全部 7 个 3339 模型

如果用户决定把 4 个未用的协议变体/低档模型也扩了（kimi-k3 chat、glm-5.2 responses、glm-5.1、ds-flash），则：
- 新增 supply 数量：7 模型 × 5 key = 35 个（多 20 个）
- nation1/nation2 仍只用 anthropic 协议的 3 个模型，其余 supply 配了但无 route 引用
- 价值：未来若想给 nation2 配 chat/responses 协议档位，supply 已就位
- 代价：配置体积膨胀、20 个 supply 闲置

**本文默认不扩这 4 个**，除非用户明确要。

## 6. 配置生成格式

本方案新增的 supply/route 配置，若用代码（`_config_ops.py` 的 `strategy add`/CLI）生成，会自动走 `compact_config_json`（见 [[2026-08-07-config-compact-format]]），`effort_enum` 数组单行。若手编 config 文件，按现有文件的紧凑格式手写即可。
