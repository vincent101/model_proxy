---
type: evaluation
status: draft
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, cli, status, help]
---

# CLI status / --help 展示评估（理想路径）

> [理想] 路径产出：不计改动成本，只评估信息展示的结构化、合理性、一致性、可用性，给出目标形态。
> 评估基准：master 工作区运行态（2026-08-07，代理在跑，config 含 cc/codex + 评估期间并发 e2e 测试新增的 eval-* 临时 route/strategy）。
> 相关：[[2026-07-28-session-route-dispatch-design]]（route_pool/override 机制）、[[2026-08-06-session-overrides-single-storage]]（sidecar 单一存储）、[[2026-08-07-nation-route-supply-expansion]]（nation1/nation2 拆分）

## 1. 现状核实

### 1.1 status 数据链路

CLI `cmd_status`（`model_proxy_cli.sh:112-184`）：
1. lsof 判端口监听，打印 `running / NOT running on port`；
2. GET `/model_proxy/status` → server `_handle_status`（`core/server.py:1621-1651`）返回 JSON：supplies（appkey 脱敏为 tail4）/ routes / strategies（每条补 `sidecar_overrides_count`）/ cooldown（`CooldownStore.snapshot()`，仅剩余秒）/ default_cooldown_seconds；
3. CLI 内嵌 python 把 JSON 格式化成五段文本。

### 1.2 实测输出（2026-08-07，摘关键段）

```
model_proxy: running on port 18889
supplies:
  claude-opus-sankuai-0956 protocol=anthropic  model=claude-opus-5        appkey=...0956
  ...（共 25 条）
routes (家族模板):
  nation1      opus=[kimi-k3-sankuai-3339,kimi-k3-sankuai-8101,kimi-k3-sankuai-9907] sonnet=[...] haiku=[...] failover=on
  ...
strategies (token 绑定):
  cc               -> pool[nation1:1,nation2:1]
  codex            -> pool[openai:1]
  eval-resp        -> pool[eval-resp:1]   （评估期间 e2e 测试并发加入）
  ...
cooldown: (无)
default_cooldown_seconds: 60
```

### 1.3 对照组：同源信息的另一处展示

`_config_ops.py` 的 list 函数（`supply`/`route`/`strategy` 交互菜单入口打印）：

- `supply_list`（`_config_ops.py:542-553`）：`sid:24`，字段含 `reasoning_capability=Y/-`、`cooldown=(默认)/Ns`，appkey 用 `_mask_appkey`（空值有 `(空)` 兜底）；
- `route_list`（`_config_ops.py:738-747`）：格式与 CLI status 的 routes 段完全相同（复制粘贴的两份实现）；
- `strategy_list`（`_config_ops.py:878-885`）：经 `_strategy_route_desc()` 统一描述 route_id/pool，带 `note` 列。

### 1.4 help 现状

`print_help`（`model_proxy_cli.sh:18-67`）：12 个子命令平铺无分组；stats 用法示例详尽；尾部交代了"增删改只能经交互菜单、非 TTY 只打印 list"的降级行为。

## 2. 理想路径评估：结构性问题

按严重度排序。每条均附核实依据。

### P1 — help 与 CLI 写路径落后于 route_pool 数据模型（准确性硬伤）

route_pool 是当前 strategy 的主写法（cc/codex/eval-* 全部 6 条都是 pool），但：

- `switch` 对 route_pool 写法的 strategy **直接报错拒绝**（`_config_ops.py:1007-1017`：「switch 只支持单值 route_id 的旧写法」）。即 **switch 对当前全部 strategy 不可用**，help（`model_proxy_cli.sh:44`）无任何提示，仍描述为常规操作「切换某 token 绑定的 route 家族」；
- `strategy add` 交互只能录入单值 `route_id`（`_config_ops.py:908-941`），pool 只能手改 config，help（`model_proxy_cli.sh:39`）描述「client_token -> route_id」未交代此限制。

运维照 help 操作会踩空。这不是文案问题，是 CLI 写路径与 config 数据模型的版本落差在 help 上的投影。

### P2 — 同一实体两套展示实现，字段集已漂移

- supplies：status（`sid:20`，无 reasoning_capability/cooldown 列）vs `supply_list`（`sid:24`，有这两列）。运维在 status 看不到每个 supply 的冷却时长配置和能力标记，要再进一次 `supply` 菜单；
- strategies：status 内联实现 pool 描述，`_strategy_route_desc()` 是第二份实现；status 缺 `note` 列；
- routes：两份完全相同的实现，目前同步，纯漂移风险。

单源化是理想形态的必选项（见 §3）。

### P3 — strategy 段 sidecar count 消费不完整（结构性 bug）

CLI 内嵌 python（`model_proxy_cli.sh:162-173`）只在 `route_pool` 分支拼 `+N个session覆盖`；单值 `route_id` 的 strategy 即使 server 返回了 `sidecar_overrides_count > 0` 也永不显示。sidecar override 按 client_token 存储，与 strategy 用哪种 route 写法无关。当前 config 全是 pool 写法未暴露，一旦存在单值写法的 strategy 即漏报。

### P4 — 可用性：列宽溢出与超长行

- supplies 段 `sid:20` 对实际 id（22-25 字符，如 `claude-sonnet-sankuai-0956`）失效，protocol/model 列随 id 长度右移，行间不对齐（实测输出前 5 行与后续行 protocol 列起始位置肉眼可见不同）；
- routes 段 nation1/nation2 每行 200+ 字符，终端折行后三档结构不可读。三档各 3 supply 是 nation 拆分后的常态，不是边缘 case。

### P5 — 运维四问，两问答不出

对照运维看 status 想快速回答的问题：

| 问题 | 现状 | 判断 |
|---|---|---|
| 代理在跑吗 | 第一行即有 | 够 |
| 哪些 supply 在冷却 | 有，但仅剩余秒 | **缺触发原因（401/429/5xx 哪个码）、触发时刻**。`CooldownStore`（`core/server.py:415-454`）只存 until 时间戳 |
| 哪个 session 走哪个 route | 仅 `+N个session覆盖` 计数 | **无明细**。sidecar 内有完整 {session: route_id, last_seen, created}，status 不展示，只能去翻 `config/session_overrides.json` |
| 503 多不多 | 无 | 该由 stats 回答（`stats today supply` 有 fail 维度），**不该塞进 status**，边界正确 |

另缺：pid/启动时刻/uptime（server 无 started_at 记录）、config 路径与 mtime、版本号（代码无版本概念）。这些是"概览"的正当成分。

### P6 — 细粒度一致性

- appkey 为空时 status 显示 `appkey=...`（无兜底），`_mask_appkey` 有 `(空)`——两处脱敏实现也是两份；
- `rid_desc:12` 宽度对 `pool[nation1:1,nation2:1]`（24 字符）无意义；
- cooldown 空值显式打印 `(无)`——好实践，应推广到 overrides（无覆盖时显式打印而非静默省略）。

### 数据疑问（超出 CLI 评估范围，提请核对）

实测发现 `kimi-k3-sankuai-6372`、`glm-52-sankuai-6372`、`ds-pro-sankuai-6372` 三条 supply 的 **id 后缀 6372 ≠ appkey 尾4 3672**（appkey `22032070810785263672`）。其余 22 条 id 后缀均等于 appkey 尾4。要么 id 笔误要么 appkey 笔误。这同时证明 status 的 appkey 尾4 列有独立校验价值，不能当冗余删掉。

### 现状中合理的部分（不硬挑问题）

- supplies→routes→strategies→cooldown 的自底向上顺序与依赖关系同构，是对的；
- routes 段家族模板展示（三档并列 + failover）与 config 结构同构，单 supply 档可读性好；
- status（瞬时态+配置态）/ stats（累计账本）/ logs（请求明细）三者边界清晰，503 频率归 stats 是正确分工；
- reload 清空 cooldown 的语义在 help 与输出中均有交代；非 TTY 降级行为有说明；stats 的 help 示例是模板级的。

## 3. 改进建议（目标形态）

### 3.1 status：单命令分层重组，不拆多子命令

拆分方案（status-supplies/status-routes/...）会丧失 status 的核心价值——一屏概览。推荐**单 status + 可选 section 参数**：

```
status                 # 概览（默认）
status overrides       # 展开 session override 明细
status --json          # 直通 server 原始 JSON（机器消费）
```

概览目标结构：

```
model_proxy: running on port 18889 (pid 12345, up 3d4h, config mtime 08-07 22:49)
health: cooldown 0/25 supplies, overrides 0 session
supplies:   （字段对齐 supply_list：+ reasoning_capability、+ cooldown 列，动态列宽）
routes:     （家族模板不变；任一档 supply 数 ≥2 时该档竖排缩进，或公共前缀压缩
             kimi-k3-sankuai-{3339,8101,9907}）
strategies: （route 描述 + note + sidecar count 无条件拼；无覆盖显式打印 "(无覆盖)"）
cooldown:   （剩余秒 + 触发状态码 + 触发时刻）
default_cooldown_seconds: 60
```

配套 server 端改动（`_handle_status` 返回增补）：`started_at`（模块级记录启动时刻）、`config_mtime`、`pid`；`CooldownStore.cooldown()` 签名补触发状态码，随 until 一起记 `last_trigger`（内存态，O(supply 数），不违背"不写盘"定位，仅松动"不记账"——代价一节见 §4）。

### 3.2 展示逻辑单源化

把 supplies/routes/strategies 三段的格式化函数收进 `_config_ops.py`（或新 `_format_ops.py`），CLI status 的内嵌 python 与交互菜单的 list 函数**调同一组函数**。消除 P2/P3/P6 的全部漂移点。status 与 list 视图字段集差异（如 status 多 cooldown 运行态列）通过参数控制，而非两份实现。

### 3.3 help：分组 + 修正落差

```
查询观察:  status / stats / logs
配置管理:  supply / route / strategy / switch / reload
进程控制:  on / off
安装:      install
```

文案修正：
- status 描述补 strategies（现漏）；
- switch 描述补「仅支持单值 route_id 写法；route_pool 写法请手改 config」——**更根本的是补齐 switch/strategy add 的 pool 支持**（P1 的功能落差，超出展示评估范围，建议单独立项）；
- strategy add 描述补「仅录入单值 route_id」；
- 头部加版本号（需先在代码引入版本常量）。

### 3.4 明确不做的

- 不把请求量/失败率塞进 status（stats 的职责）；
- 不在概览默认展开 override 明细（session id 长，污染一屏；用 `status overrides`）；
- 不为 status 引入配置文件（展示结构固化在代码即可，这是内部运维工具）。

## 4. 风险与权衡（迁移/落地代价提示，不改设计）

- **CooldownStore 记账松动**：`last_trigger` 违背其 docstring「不记账」的自我定位，但只是内存态 O(n) 字段，不写盘、不影响热路径锁竞争；若坚持不松动，替代方案是运维用 `logs`  grep 503 自行关联（体验差但零改动）。
- **单源化牵动面**：`_config_ops.py` 的 list 函数被交互菜单依赖，抽公共 format 函数需同步改菜单打印路径；CLI 内嵌 python 调 `_config_ops` 增加一次 import，启动开销可忽略。
- **server 端字段增补**只加不改，旧 CLI 消费新 JSON 前向兼容（多字段忽略）。
- **竖排/前缀压缩**改变 routes 段既有视觉格式，习惯旧格式的用户需要适应；压缩格式 `{a,b,c}` 需保证可复制粘贴还原（不丢字符）。
- P1 的 pool 写路径补齐是独立工作量，不在本评估的展示重组范围内，但 help 文案修正可以先于功能补齐落地（先诚实标注限制）。

## 5. 验证方式

- 重组后 `status` 在 80 列终端不折行（nation1/nation2 行是验收重点）；
- 构造单值 route_id strategy + sidecar override，确认 `+N个session覆盖` 显示（P3 回归点）；
- `status` 与 `supply`/`strategy` 菜单 list 同窗对比，同实体字段口径一致；
- `switch cc nation1` 的 help 描述与实际行为一致（报错文案或功能补齐后二选一）；
- `status --json` 输出与 `curl /model_proxy/status` 原始响应一致；
- 人工核对：id 后缀 6372 vs appkey 尾4 3672 的笔误归属（§2 数据疑问）。

## 关联

- [[2026-07-28-session-route-dispatch-design]]（route_pool / session override 数据模型）
- [[2026-08-06-session-overrides-single-storage]]（sidecar 单一存储，sidecar_overrides_count 字段来源）
- [[2026-08-07-nation-route-supply-expansion]]（nation1/nation2 三档 3 supply，routes 段超长行的成因）
- [[2026-08-04-in-band-route-command-design]]（$route 写 sidecar，override 明细的数据生产者）
