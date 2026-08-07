---
type: evaluation
status: draft
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, cli, status, help]
---

# CLI status / --help 展示评估（理想路径 · display-eval）

> [理想] 路径：不计改动成本，只评估信息展示的结构化、合理性、一致性、可用性，给目标形态。
> 评估基准：master 工作区运行态（2026-08-07 实测，代理在跑，config = 27 supplies / 8 routes / 6 strategies，6 条 strategy 全 route_pool 写法）。
> 前作关系：与同目录 [[2026-08-07-cli-status-help-evaluation]] 同题。本文是独立重核后的二次评估（25→27 supplies、补充 help↔case 对齐核验、override 管理缺口、help 漏 strategies 等），结论与其高度收敛、互为印证。两篇并存属重复，建议用户择一为 canonical、另一篇置 superseded——本文不擅自改动前作。

## 1. 现状梳理

### 1.1 status 数据链路与展示

链路：`cmd_status`（`model_proxy_cli.sh:112-184`）→ lsof 判端口 → `GET /model_proxy/status` → `_handle_status`（`core/server.py:1621-1651`）→ CLI 内嵌 python 格式化成五段文本。

server 返回 JSON（`core/server.py:1645-1651`）：
- `supplies`：appkey 脱敏为 `appkey_tail4`（`:1630-1634`）；
- `routes`：家族模板原样；
- `strategies`：每条补 `sidecar_overrides_count`（`:1636-1643`，源自 sidecar 单一存储）；
- `cooldown`：`CooldownStore.snapshot()`，仅 supply→剩余秒（`core/server.py:445-454`）；
- `default_cooldown_seconds`。

CLI 渲染五段：supplies 平铺列表 / routes 三档模板 / strategies 绑定 / cooldown 剩余秒 / default_cooldown_seconds。实测输出（2026-08-07，节选）：

```
model_proxy: running on port 18889
supplies:
  claude-opus-sankuai-0956 protocol=anthropic  model=claude-opus-5        appkey=...0956
  openai-sol-sankuai-0956 protocol=responses  model=gpt-5.6-sol          appkey=...0956   <- 列错位
  ...（共 27 条）
routes (家族模板):
  nation1      opus=[kimi-k3-sankuai-3339,kimi-k3-sankuai-8101,...] sonnet=[...] haiku=[...] failover=on   <- 233 字符
strategies (token 绑定):
  cc               -> pool[nation1:1,nation2:1]
  ...（6 条全 pool，无一条显示 +N个session覆盖，因当前 override=0）
cooldown: (无)
default_cooldown_seconds: 60
```

### 1.2 同源信息的另一处展示（对照组）

`_config_ops.py` 的 list 函数（supply/route/strategy 交互菜单入口打印）：
- `supply_list`（`_config_ops.py:542-553`）：`sid:24`，多 `reasoning_capability=Y/-`、`cooldown=(默认)/Ns` 两列，appkey 走 `_mask_appkey`（`:538-539`，空值 `(空)` 兜底）；
- `route_list`（`:738-748`）：与 CLI status 的 routes 段逐字相同（复制粘贴的两份实现）；
- `strategy_list`（`:878-885`）：经 `_strategy_route_desc()`（`:861-875`）统一描述 route_id/pool，多 `note` 列。

### 1.3 --help 现状

`print_help`（`model_proxy_cli.sh:18-67`）：11 个子命令平铺无分组；stats 示例详尽（模板级）；尾部交代"增删改只能经交互菜单、非 TTY 只打印 list"的降级行为。

## 2. 问题清单（按严重度）

### P1 — help 写路径描述落后于 route_pool 数据模型（准确性硬伤）

route_pool 是 strategy 当前唯一在用的写法（6/6），但 CLI 写路径不支持，help 无一句提示：

- `switch` 对 route_pool strategy 直接报错拒绝（`_config_ops.py:1016-1020`：「switch 只支持单值 route_id 的旧写法」）。即 **switch 对当前全部 6 条 strategy 不可用**，help（`model_proxy_cli.sh:44`）仍描述为常规操作「切换某 token 绑定的 route 家族」；
- `strategy add` 交互只录单值 `route_id`（`_config_ops.py:932` 硬编码 `entry={...,"route_id":srid}`），无 pool 路径，help（`:39`）「client_token -> route_id」未交代；
- `strategy edit` 对 pool 写法拒绝编辑（`_config_ops.py:957-959`），help（`:40`）未交代。

运维照 help 操作会踩空。这不是文案瑕疵，是 CLI 写路径与 config 数据模型的版本落差在 help 上的投影。

### P2 — 同一实体两套展示实现，字段集已漂移

- supplies：status（`sid:20`，无 reasoning_capability/cooldown 列）vs `supply_list`（`sid:24`，有这两列）。运维在 status 看不到每个 supply 的能力标记与冷却时长配置，要再进一次 `supply` 菜单；
- routes：status（`model_proxy_cli.sh:149-156`）与 `route_list`（`_config_ops.py:740-747`）逐字相同，目前同步、纯漂移风险；
- strategies：status 内联 pool 描述（`:162-173`），`_strategy_route_desc()` 是第二份实现；status 缺 `note` 列。

### P3 — strategies 段 sidecar count 消费不完整（结构性 bug）

CLI 内嵌 python（`model_proxy_cli.sh:162-173`）只在 `route_pool` 分支拼 `+N个session覆盖`；单值 `route_id` 分支（`:162-163`）即使 server 返回了 `sidecar_overrides_count > 0` 也永不显示。sidecar override 按 client_token 存储，与 strategy 用哪种 route 写法无关。当前全 pool 写法未暴露，一旦出现单值写法 strategy 即漏报。

### P4 — 可用性：列宽溢出与超长行

- supplies 段 `sid:20` 对实际 id（19-27 字符）失效：`openai-sol-sankuai-0956`(23)、`glm-52-sankuai-openai-3339`(26) 等把 protocol/model 列顶得右移，行间不对齐（实测输出肉眼可见）；
- routes 段 nation1/nation2 每行 233 字符，80 列终端折行后三档结构不可读。三档各 3 supply 是 nation 拆分后的常态，非边缘 case。

### P5 — 运维四问两问答不出 + override 全生命周期对 CLI 不可见

| 运维看 status 想回答 | 现状 | 判断 |
|---|---|---|
| 代理在跑吗 | 第一行即有 | 够 |
| 哪些 supply 在冷却 | 仅剩余秒 | **缺触发状态码（401/429/5xx）、触发时刻**。`CooldownStore._until` 只存截止戳（`core/server.py:421-454`，docstring 自述「不记账」） |
| 哪个 session 走哪个 route | 仅 `+N个session覆盖` 计数 | **无明细**，且 **CLI/_config_ops 无任何查看/清除 override 的入口**（grep 全仓仅 status 一处引用 count），只能翻 `config/session_overrides.json` 或用 in-band `$route` |
| 503 多不多 | 无 | 该由 stats 回答，**不该塞进 status**，边界正确 |

override 的可见性/管理缺口同时是 help 该交代而没交代的：`$route` in-band 指令不是 CLI 子命令，不该列为子命令，但它是 status 里 `+N个session覆盖` 的唯一生产者，help 对其存在与如何管理只字未提，运维看到计数会无迹可循。

另缺概览正当成分：pid / 启动时刻 / uptime（server 无 `started_at`，`os.getpid()` 仅写 lock，`:2008`）、config 路径与 mtime、版本号（代码无版本常量）。

### P6 — help 自身一致性与细粒度

- help（`:22`）status 描述「supplies/routes/cooldown 概览」**漏 strategies**，实际 status 有 strategies 段——help 与 status 不对齐；
- 11 子命令平铺无分组（查询/配置/进程/安装混杂），扫读性差（对比同 help 内 stats 的模板级示例，详略失衡）；
- appkey 空值：status 显示 `appkey=...`（tail4 空串直拼，无兜底），`_mask_appkey` 有 `(空)`——两处脱敏也是两份实现；
- `rid_desc:12` 宽度对 `pool[nation1:1,nation2:1]`(24 字符）无意义；
- 无版本号。

### 数据疑问（超出 CLI 评估范围，提请核对）

实测 `kimi-k3-sankuai-6372`、`glm-52-sankuai-6372`、`ds-pro-sankuai-6372` 三条 supply 的 **id 后缀 6372 ≠ appkey 尾4 3672**（appkey `22032070810785263672`）。其余 24 条 id 后缀均等于 appkey 尾4。要么 id 笔误要么 appkey 笔误。这同时证明 status 的 appkey 尾4 列有独立校验价值，**不能当冗余删**。

### 现状中合理的部分（不硬挑问题）

- help 子命令列表与 case 分支（`model_proxy_cli.sh:602-646`）**完全对齐**：status/reload/supply/route/strategy/switch/install/on/off/logs/stats + help，无漏列、无 phantom；
- supplies→routes→strategies→cooldown 的自底向上顺序与依赖关系同构；
- routes 家族模板展示（三档并列+failover）与 config 结构同构，单 supply 档可读性好；
- status（瞬时+配置态）/stats（累计账本）/logs（请求明细）边界清晰，503 频率归 stats 是正确分工；
- cooldown 空值显式打印 `(无)`（好实践，应推广到 overrides）；reload 清 cooldown 语义在 help 与输出均有交代；非 TTY 降级有说明。

## 3. 改进建议（理想形态）

### 3.1 status：单命令分层重组，不拆多子命令

拆成 status-supplies/status-routes/... 会丧失一屏概览的核心价值。推荐**单 status + 可选 section 参数**：

```
status                 # 概览（默认）
status overrides       # 展开 session override 明细
status --json          # 直通 server 原始 JSON（机器消费）
```

概览目标结构：

```
model_proxy: running on port 18889 (pid 12345, up 3d4h, config mtime 08-07 22:49)
health: cooldown 0/27 supplies, overrides 0 session
supplies:   （对齐 supply_list 字段：+ reasoning_capability、+ cooldown 列，动态列宽）
routes:     （家族模板不变；任一档 supply 数 ≥2 时该档竖排缩进，或公共前缀压缩
             kimi-k3-sankuai-{3339,8101,9907}）
strategies: （route 描述 + note + sidecar count 无条件拼；无覆盖显式打印 "(无覆盖)"）
cooldown:   （剩余秒 + 触发状态码 + 触发时刻）
default_cooldown_seconds: 60
```

配套 server 端增补（`_handle_status` 只加不改）：`started_at`（模块级记录启动时刻）、`pid`、`config_mtime`；`CooldownStore.cooldown()` 签名补触发状态码，随 until 记 `last_trigger`（内存态 O(supply 数），不写盘，代价见 §4）。

### 3.2 展示逻辑单源化

把 supplies/routes/strategies 三段格式化收进 `_config_ops.py`（或新 `_format_ops.py`），CLI status 的内嵌 python 与交互菜单 list **调同一组函数**，消除 P2/P3/P6 全部漂移点。status 与 list 的字段集差异（status 多 cooldown 运行态列）用参数控制，而非两份实现。appkey 脱敏统一走 `_mask_appkey`。

### 3.3 help：分组 + 修正落差 + 补 override 可见性

```
查询观察:  status / stats / logs
配置管理:  supply / route / strategy / switch / reload
进程控制:  on / off
安装:      install
```

文案修正：
- status 描述补 strategies（现漏）；
- switch 描述补「仅支持单值 route_id 写法；route_pool 请手改 config」；strategy add/edit 描述补「仅单值 route_id」——**更根本是补齐 switch/strategy 的 pool 写路径**（P1 功能落差，超出展示评估范围，建议单独立项），文案诚实标注可先于功能补齐落地；
- 增一节说明 session override：由 in-band `$route` 产生、存于 `config/session_overrides.json`，用 `status overrides` 查看、如何清除；
- 头部加版本号（需先引入版本常量）。

### 3.4 override 管理补口

CLI 增加 override 的查看（`status overrides`）与清除入口（如 `strategy` 菜单加 `[o]verrides`，或独立子命令），闭合「$route 产生 → status 计数 → 无处查看/清除」的断链。

### 3.5 明确不做的

- 不把请求量/失败率塞进 status（stats 职责）；
- 概览默认不展开 override 明细（session id 长，污染一屏）；
- 不为 status 引入配置文件/主题（内部运维工具，结构固化在代码即可）。

## 4. 值得改 vs 不值得改

**值得改（高回报/硬伤）**：
- P1 help 与写路径的 pool 落差——运维踩空，必须改（文案先行，功能补齐另立项）；
- P2 展示单源化——一次投入消除一类漂移；
- P3 sidecar count 单值分支漏报——bug，必须修；
- P4 列宽（supplies 动态列宽 + routes 长档处理）——日常可用性；
- help 分组 + status 描述补 strategies——低成本高回报；
- override 可见性/管理补口（P5/§3.4）——闭合断链。

**不值得改 / 可缓（避免过度设计）**：
- 请求量/失败率进 status——越界，stats 已覆盖；
- 概览默认展开 override 明细——污染一屏，用 section 参数即可；
- routes 公共前缀压缩 `{a,b,c}`——视觉改动需适应，且要保证可复制还原，可选而非必需（竖排缩进更稳）；
- pid/uptime/config mtime——需 server 加 `started_at`，锦上添花非必需；
- cooldown 触发码/时刻——需松动 `CooldownStore`「不记账」定位（见 §4 风险），体验增益与定位松动需权衡，可缓；
- status 配置文件/主题——过度设计。

## 5. 风险与权衡（迁移/落地代价提示，不改设计）

- **CooldownStore 记账松动**：`last_trigger` 违背其 docstring「不记账」定位，但仅内存态 O(n)、不写盘、不影响热路径锁竞争；若坚持不松动，替代是运维 `logs` grep 503 自行关联（体验差但零改动）；
- **单源化牵动面**：`_config_ops.py` 的 list 函数被交互菜单依赖，抽公共 format 需同步改菜单打印路径；CLI 内嵌 python 调 `_config_ops` 增一次 import，启动开销可忽略；
- **server 字段增补**只加不改，旧 CLI 消费新 JSON 前向兼容（多字段忽略）；
- **竖排/前缀压缩**改变 routes 段既有视觉格式，老用户需适应；压缩格式需保证可复制粘贴还原（不丢字符）；
- **P1 pool 写路径补齐**是独立工作量，不在本展示重组范围内；help 文案修正可先落地（先诚实标注限制）。

## 6. 验证方式

- 重组后 `status` 在 80 列终端不折行（nation1/nation2 行是验收重点）；
- 构造单值 route_id strategy + sidecar override，确认 `+N个session覆盖` 显示（P3 回归点）；
- `status` 与 `supply`/`strategy` 菜单 list 同窗对比，同实体字段口径一致；
- `switch cc nation1` 的 help 描述与实际行为一致（报错文案或功能补齐后二选一）；
- `status --json` 输出与 `curl /model_proxy/status` 原始响应一致；
- 人工核对 id 后缀 6372 vs appkey 尾4 3672 的笔误归属（§2 数据疑问）。

## 关联

- [[2026-08-07-cli-status-help-evaluation]]（同题前作，结论收敛，建议择一 canonical）
- [[2026-07-28-session-route-dispatch-design]]（route_pool / session override 数据模型）
- [[2026-08-06-session-overrides-single-storage]]（sidecar 单一存储，sidecar_overrides_count 来源）
- [[2026-08-07-nation-route-supply-expansion]]（nation1/nation2 三档 3 supply，routes 超长行成因）
- [[2026-08-04-in-band-route-command-design]]（$route 写 sidecar，override 明细生产者）
