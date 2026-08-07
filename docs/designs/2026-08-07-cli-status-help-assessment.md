---
type: evaluation
status: draft
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, cli, status, help]
---

# CLI status / --help 展示评估（理想路径 · assessment）

> [理想] 路径：不计改动成本，评估信息展示的结构化、合理性、一致性、可用性，给目标形态。
> 评估基准：master 工作区运行态，2026-08-07 实测（代理在跑 18889；config = **25 supplies / 4 routes / 2 strategies**，cc/codex 全 route_pool 写法；`config/session_overrides.json` 不存在，override=0）。本次实测无 e2e 并发测试干扰，是三篇同题文档中最干净的基线。
> 前作关系：与同目录 [[2026-08-07-cli-status-help-evaluation]]、[[2026-08-07-cli-status-help-display-eval]] 同题。本文独立重核，结论与两者高度收敛（P1 pool 写路径落差、P3 sidecar count bug、列宽/长行、展示双实现等判断一致），并新增：nation1∩nation2 共享 3339 行、orphan supply 不可见、停机时 status 无降级、`supply add` 多参静默忽略等点。**三篇并存属重复，建议用户择一置 confirmed、其余置 superseded——本文不擅自改动前作。**

## 1. 现状核实

### 1.1 status 数据链路

`cmd_status`（`model_proxy_cli.sh:112-184`）→ lsof 判端口 → `GET /model_proxy/status` → `_handle_status`（`core/server.py:1621-1651`）→ CLI 内嵌 python（`:131-183`）格式化成五段文本。

server 返回 JSON：supplies（剥 appkey 留 `appkey_tail4`，`:1630-1634`）/ routes（原样）/ strategies（完整拷贝 + 补 `sidecar_overrides_count`，`:1636-1643`，源自 sidecar 单一存储）/ cooldown（`CooldownStore.snapshot()` 仅剩余秒，`:444-454`）/ default_cooldown_seconds。

### 1.2 status 实测输出（2026-08-07 完整结构，节选）

```
model_proxy: running on port 18889
supplies:
  claude-opus-sankuai-0956 protocol=anthropic  model=claude-opus-5        appkey=...0956
  openai-sol-sankuai-0956 protocol=responses  model=gpt-5.6-sol          appkey=...0956
  ...（共 25 条平铺）
  kimi-k3-sankuai-6372 protocol=anthropic  model=kimi-k3              appkey=...3672   ← id 尾 6372 ≠ key 尾 3672
routes (家族模板):
  claude       opus=[claude-opus-sankuai-0956] sonnet=[claude-sonnet-sankuai-0956] haiku=[ds-pro-sankuai-3339] failover=on
  openai       opus=[...] sonnet=[...] haiku=[...] failover=on
  nation1      opus=[kimi-k3-sankuai-3339,kimi-k3-sankuai-8101,kimi-k3-sankuai-9907] sonnet=[...] haiku=[...] failover=on   ← 约 230 字符
  nation2      opus=[kimi-k3-sankuai-3339,kimi-k3-sankuai-2330,kimi-k3-sankuai-4200] sonnet=[...] haiku=[...] failover=on
strategies (token 绑定):
  cc               -> pool[nation1:1,nation2:1]
  codex            -> pool[openai:1]
cooldown: (无)
default_cooldown_seconds: 60
```

config 实测结构：strategies 仅含 `client_token/route_pool/tiers_source_capability/note`（无 `dispatch`，override 已全部迁 sidecar）；routes 仅含 `id/tiers/failover`（failover 只是 on/off 开关，无链配置）；sidecar 文件当前不存在。

### 1.3 对照组：同源信息的另一处展示

`_config_ops.py` 交互菜单的 list 函数：
- `supply_list`（`:542-553`）：`sid:24`，多 `reasoning_capability=Y/-`、`cooldown=(默认)/Ns` 两列，appkey 走 `_mask_appkey`（`:538-539`，空值 `(空)` 兜底）；
- `route_list`（`:738-747`）：与 CLI status routes 段逐字相同（两份复制实现）；
- `strategy_list`（`:878-885`）：经 `_strategy_route_desc()`（`:861-875`）统一描述 route_id/pool，多 `note` 列。

### 1.4 --help 实测输出结构

`print_help`（`model_proxy_cli.sh:18-67`）：12 个子命令平铺无分组（status/reload/supply/route/strategy/switch/install/on/off/logs/stats/--help）；stats 示例详尽（模板级）；尾部交代"增删改只能经交互菜单、非 TTY 只打印 list"（`:63-65`）。

help 与 case 分支（`:602-646`）**逐一对齐，无漏列无 phantom**——已逐个核对。

## 2. 合理性评估（逐项，附依据）

### 2.1 status

| # | 项 | 判定 | 依据 |
|---|---|---|---|
| S1 | 自底向上顺序（supplies→routes→strategies→cooldown） | **合理** | 与依赖关系同构，`:141-182` |
| S2 | session 覆盖数计数口径 | **合理**（已随 sidecar 单一存储同步修） | server 从 `sidecar.count_overrides_for(ct)` 取（`server.py:1642`），不再读已废弃的 `dispatch.session_overrides` |
| S3 | 覆盖数来源说明 | **缺失** | `+N个session覆盖`（`:171`）未注明来自 sidecar/`$route`；且仅 count>0 才拼（`:170`），当前 override=0 时 strategies 段完全无 override 痕迹，运维无迹可循 |
| S4 | 覆盖数单值 route_id 分支漏报 | **不合理（结构性 bug）** | count 只在 `elif route_pool:` 分支拼（`:162-171`）；单值写法 strategy 即使 sidecar 有覆盖也永不显示。当前 2/2 全 pool 未暴露，一旦出现单值写法即漏报 |
| S5 | nation1/nation2 supply 分配可辨性 | **不合理** | 两行各约 230 字符，80 列终端折行后三档结构不可读；且**nation1∩nation2 共享 3339 整行 3 个 supply**（实测，`kimi/glm-52/ds-pro-sankuai-3339` 同时在两个 route 首位）——此事实需人工对撞两个长行才能发现。共享 supply 的冷却全局生效（`CooldownStore` 按 supply id 键控，`:423`），双 route 隔离度被削弱：实际分散 key 是 5 个不是 6 个，且共享 key 居两 route 有序 failover 首位、被打最狠。与 [[2026-08-07-nation-route-supply-expansion]] §2.3「不重叠分配（nation2 用 2330/4200/6372）」的设计意图偏离，**是否系 6372 appkey 验证未过而临时改配，需用户确认** |
| S6 | orphan supply 可见性 | **缺失** | 实测 5/25 supply 不被任何 route 引用（6372 三个 + `ds-flash-sankuai-3339` + `glm-51-sankuai-3339`），status 无任何标注；其中 ds-flash/glm-51 是设计内备用件，6372 三个疑似配置残留 |
| S7 | 列宽 | **不合理** | `{sid:20}`（`:147`）对实际 id（22-24 字符）失效，protocol/model 列被顶歪（实测 `claude-sonnet-sankuai-0956` 行与短 id 行 protocol 列不起齐）；`{rid_desc:12}`（`:174`）对 `pool[nation1:1,nation2:1]`（25 字符）无意义 |
| S8 | 字段集与 list 函数漂移 | **不合理** | status supplies 无 reasoning_capability/cooldown 列（`supply_list` 有）；strategies 无 note（`strategy_list` 有）；脱敏两份实现（status 空 appkey 显示 `appkey=...` 无兜底 vs `_mask_appkey` 的 `(空)`） |
| S9 | failover 链长可见性 | **缺失** | route_pool 双 route + 档内 3 supply 后，单 session 最坏链长 6 步（`extract_route_candidates`，`server.py:605-688`：档内有序 failover + 跨 route 候选兜底）。status 展示 pool 组成但不可直接读出链长与共享 supply 对链的折叠效应 |
| S10 | 停机降级 | **缺失** | 代理不在跑时 status 只打一行 `NOT running` 即 return 1（`:113-118`），supplies/routes/strategies 这些 config 静态信息一并看不了——而这些恰是排查"为什么起不来/起来会怎样"时想看的 |
| S11 | cooldown 信息粒度 | **可接受，可增强** | 仅剩余秒；触发状态码/时刻无（`CooldownStore` 自述「不记账」，`:415-420`）。增强需松动其定位，属权衡项 |
| S12 | 概览元信息 | **缺失** | 无 pid/uptime/config mtime/版本号（server 无 `started_at`，全仓无版本常量，已 grep 确认） |
| S13 | appkey tail4 列 | **合理且有独立校验价值** | 实测抓到 6372 三个 supply 的 id 尾号 ≠ appkey 尾4（`...3672`），全仓仅此处能把两值并置对照。**不能当冗余删**；数据本身笔误归属需用户核对 |

### 2.2 --help

| # | 项 | 判定 | 依据 |
|---|---|---|---|
| H1 | 子命令覆盖 | **合理** | 12 个 help 条目与 case 分支完全对齐，无实现未列、无列已废弃 |
| H2 | switch 文案 | **过时/误导（硬伤）** | help `:44`「切换某 token 绑定的 route 家族」无任何限制提示；实际 `switch()` 对 route_pool 写法直接报错拒绝（`_config_ops.py:1016-1021`），而当前 2/2 strategy 全 pool——**switch 对现网全部 strategy 不可用** |
| H3 | strategy 文案 | **不准确** | add 只说「client_token -> route_id」（`:39`），未交代不支持录入 route_pool/dispatch（`strategy_add` 硬编码单值，`:932`）；edit 未交代 pool 写法只部分可编（note/source 可、route 拒绝，`:957-960`） |
| H4 | status 描述 | **不准确** | `:22`「supplies/routes/cooldown 概览」漏 strategies 段 |
| H5 | `$route` in-band 指令 | **缺失** | help 只字未提。`$route`（`core/commands.py:40`）已上线，是 status `+N个session覆盖` 的唯一生产者、override 除手改 sidecar 外的唯一运维入口。它不该列为 CLI 子命令，但 help 应设指引段，否则运维看到覆盖计数无迹可循 |
| H6 | 分组 | **可增强** | 12 子命令平铺，查询/配置/进程/安装混杂；与 stats 的模板级示例比详略失衡 |
| H7 | 多参数静默忽略 | **可接受，可更明确** | `supply add` 等多余参数被 case 静默吞掉（`supply)` 分支不调参，`:612-613`；实测打印 list 后按非 TTY 退出，无任何"add 已忽略"提示）。help `:63` 已声明"不再支持直达"，行为与文档不矛盾，但静默忽略不如打一行警告 |
| H8 | install 描述 | **合理** | 「四个 SDK」与 `_install_ops.py:5,39`（claude/codex/hermes/openclaw）一致 |
| H9 | reload 清 cooldown 语义、非 TTY 降级、stats 示例 | **合理** | `:23`、`:63-65`、`:50-60` |

### 2.3 两者一致性

- help 承诺 supply test 可写 `reasoning_capability`（`:29`），但该能力值在 status 完全不可见、`supply_list` 仅有 Y/-——**半呼应**：运维测完想确认写了什么，两处都看不到具体枚举值。
- help 的 switch 描述与 status 展示的现实（全 pool）互相矛盾：照 help 操作必踩空（同 H2）。
- status 的 `+N个session覆盖` 在 help 里找不到任何对应能力条目（$route 未提，同 H5）——status 展示了 help 世界不存在的东西。

## 3. 改进方案（理想形态）

### 3.1 status：单命令 + section 参数，不拆多子命令

保留一屏概览的核心价值，加两个可选参数：

```
status                 # 概览（默认）
status overrides       # 展开 session override 明细（sidecar 全量）
status --json          # 直通 server 原始 JSON（机器消费）
```

概览目标输出样例（基于当前真实 config 渲染）：

```
model_proxy: running on port 18889 (pid 69224, up 4d13h, config mtime 08-07 22:32)
health: cooldown 0/25 · overrides 0 · orphan supplies 5 · shared-across-routes 3

supplies (25):
  claude-opus-5 (anthropic)
    claude-opus-sankuai-0956    key=...0956  effort=l/m/h/xh/max  cd=(默认)  ← claude.opus
  claude-sonnet-5 (anthropic)
    claude-sonnet-sankuai-0956  key=...0956  effort=l/m/h/xh/max  cd=(默认)  ← claude.sonnet
  gpt-5.6-sol/-terra/-luna (responses)
    openai-sol-sankuai-0956     key=...0956  effort=l/m/h/xh/max  cd=(默认)  ← openai.opus
    ...
  kimi-k3 (anthropic)
    kimi-k3-sankuai-3339        key=...3339  effort=l/h/max  cd=(默认)  ← nation1.opus#1, nation2.opus#1 ⚠共享
    kimi-k3-sankuai-8101        key=...8101  effort=l/h/max  cd=(默认)  ← nation1.opus#2
    ...
    kimi-k3-sankuai-6372        key=...3672  effort=l/h/max  cd=(默认)  ← (未被引用) ⚠id尾号≠key尾号
  ...
  glm-5.1 (anthropic)
    glm-51-sankuai-3339         key=...3339  effort=l/h/max  cd=(默认)  ← (未被引用)

routes (家族模板, failover=on):
  claude
    opus:   claude-opus-sankuai-0956
    sonnet: claude-sonnet-sankuai-0956
    haiku:  ds-pro-sankuai-3339
  openai
    opus:   openai-sol-sankuai-0956
    sonnet: openai-terra-sankuai-0956
    haiku:  openai-luna-sankuai-0956
  nation1
    opus:   kimi-k3-sankuai-{3339,8101,9907}
    sonnet: glm-52-sankuai-{3339,8101,9907}
    haiku:  ds-pro-sankuai-{3339,8101,9907}
  nation2
    opus:   kimi-k3-sankuai-{3339,2330,4200}
    sonnet: glm-52-sankuai-{3339,2330,4200}
    haiku:  ds-pro-sankuai-{3339,2330,4200}
  ⚠ nation1∩nation2 共享 3339 行 3 个 supply（冷却全局生效，隔离度 6→5 key）

strategies (token 绑定):
  cc     -> pool[nation1:1, nation2:1]   一致性哈希 by session；档内 3 + 跨 route 3，链长≤6
            overrides: (无)   note: 默认 Claude 家族（Claude Code SDK）
  codex  -> pool[openai:1]               单 route 池，无哈希分散；档内 failover 链长=1
            overrides: (无)   note: codex-cli SDK，走 Responses 协议

cooldown: (无)
default_cooldown_seconds: 60
```

要点：
- supplies 按 model 分组、动态列宽，补 effort 枚举、cd 配置、**引用标注**（哪个 route 哪档第几位；未引用/共享/id-key 不一致三类异常内联标记）；
- routes 三档竖排，多 supply 档用公共前缀压缩 `{a,b,c}`（保证可复制还原、不丢字符）；段尾对跨 route 共享 supply 给一行告警；
- strategies 补 note、override 无条件展示（无覆盖显式 `(无)`，格式 `(无)` 与 cooldown 段一致）、pool 语义与链长估算（静态可推导，无需 server 新数据）；
- header 行补 pid/uptime/config mtime（需 server 增补 `started_at`，见 §3.3）；health 行给四个计数一眼总览；
- 停机降级：代理不在跑时仍打印 config 静态段（supplies/routes/strategies），仅 cooldown/uptime 标 `(代理未运行)`。

`status overrides` 目标样例：

```
session overrides (sidecar: config/session_overrides.json):
  cc:
    2896beec-c308-...  -> nation1   last_seen=2026-08-07T10:20Z  created=2026-08-06T22:01Z
  codex: (无)
```

### 3.2 展示逻辑单源化

把 supplies/routes/strategies 三段格式化收进 `_config_ops.py`（或新 `_format_ops.py`），CLI status 内嵌 python 与交互菜单 list 调同一组函数；status 与 list 的字段集差异（status 多运行态列）用参数控制。脱敏统一走 `_mask_appkey`。消除 S4/S8 全部漂移点。

### 3.3 server 端增补（只加不改，前向兼容）

`_handle_status` 返回体增补：`started_at`（模块级记录启动时刻）、`pid`、`config_mtime`；override 明细（`status overrides` 用，`sidecar.get_overrides_for` 已有）。cooldown 触发码/时刻需 `CooldownStore.cooldown()` 签名补 `last_trigger`（内存态 O(supply 数)、不写盘）——**可选**，松动「不记账」定位，见 §4 权衡。

### 3.4 help：分组 + 修正落差 + $route 指引

目标输出样例（骨架）：

```
用法: model_proxy_cli.sh <子命令> [参数]

查询观察:
  status [overrides|--json]   运行状态 + supplies/routes/strategies/cooldown 概览；
                              overrides 展开 session 覆盖明细；--json 输出原始 JSON
  stats [时间] [维度/过滤...]  读独立账本，按 supply/route/strategy 组合切片（示例见文末）
  logs [N]                    最近 N 条 ACCESS 日志（默认 30）

配置管理（增删改进入交互菜单执行，非 TTY 只打印 list 后退出）:
  supply                      supply 管理：add/edit/del/test（连通性 + effort 探测写 reasoning_capability）
  route                       route 家族模板管理：add/edit/del
  strategy                    strategy 绑定管理：add 仅录单值 route_id；
                              route_pool/dispatch 写法请直接编辑 config 后 reload
  switch <token> <route_id>   改 strategy.route_id。仅支持单值写法；
                              route_pool 写法的 strategy 会被拒绝，请手改 config
  reload                      热重载配置（无条件清空所有 cooldown）

进程控制:
  on                          启动（已在监听则跳过）
  off                         停止（严格匹配本目录 model_proxy.py，不影响 v1 18888）

安装:
  install                     四个 SDK（claude/codex/hermes/openclaw）检测与安装

会话内指令（非 CLI 子命令，在对话里直接发，仅 source=anthropic）:
  $route                      查询当前 session 的生效 route 与 override
  $route <route_id>           把当前 session 固定到指定 route（写 config/session_overrides.json）
  $route reset                清除当前 session 的 override
  ※ status strategies 段的 "+N个session覆盖" 即由 $route 产生

stats 用法示例:（保留现有 :51-60 全部示例）
```

修正点对应：H2（switch 限制诚实标注）、H3（strategy pool 限制）、H4（status 补 strategies/overrides 参数）、H5（$route 指引段）、H6（四组分组）。**更根本的是补齐 switch/strategy add 的 pool 写路径**（功能落差，超出展示评估范围，建议单独立项），文案诚实标注可先于功能落地。

### 3.5 明确不做的

- 请求量/失败率不进 status（stats 的职责，边界现状正确）；
- 概览默认不展开 override 明细（session id 长，污染一屏，用 section 参数）；
- 不为展示引入配置文件/主题（内部运维工具，结构固化在代码）。

## 4. 实施建议（性价比排序）

**第一梯队（低成本修硬伤，建议立即做）**：
1. help 文案修正（H2/H3/H4/H5，纯文本零风险）——switch/strategy 限制诚实标注 + status 补 strategies + $route 指引段；
2. S4 sidecar count 单值分支 bug（`:162-171` 把 count 拼接移出 pool 分支，几行）；
3. S7 动态列宽 + routes 长档竖排/前缀压缩（日常可用性，纯 CLI 侧）。

**第二梯队（一次投入消一类问题）**：
4. §3.2 展示单源化（消 S8 漂移，牵动交互菜单打印路径，需同步改）;
5. S10 停机降级展示 config 静态段（status 从"全或无"变"降级可用"）；
6. S3 override 无条件展示 + 来源标注。

**第三梯队（需 server 配合，锦上添花）**：
7. S12 started_at/pid/config_mtime 增补（只加不改）；
8. `status overrides` 明细视图 + override 清除入口（闭合「$route 产生 → status 计数 → 无处查看/清除」断链，需 server 返回明细）。

**可缓/另立项**：
- S11 cooldown 触发码/时刻——松动 `CooldownStore`「不记账」定位换体验，内存态 O(n) 不写盘、不影响热路径锁，可接受但非必需；坚持不松动的替代是运维 `logs` grep 503 自行关联；
- S5/S6 暴露的配置问题本身（nation 共享 3339、6372 残留、id/key 尾号不一致）——**这是 config 数据问题不是展示问题**，展示层只负责让它可见；修正归属需用户拍板；
- switch/strategy add 的 pool 写路径补齐——独立工作量，单独立项。

**迁移/落地代价提示**（理想路径不因代价改设计，仅供知情）：单源化牵动 `_config_ops.py` 三个 list 函数的菜单调用方；server 增补字段旧 CLI 前向兼容；routes 竖排/压缩改变既有视觉格式需适应；§3.1 全量落地约等于重写 `cmd_status` 内嵌 python + `_config_ops` 抽层 + server 十余行增补。

## 5. 验证方式

- 重组后 `bash model_proxy_cli.sh status` 在 80 列终端不折行（nation1/nation2 段为验收重点）；
- 构造单值 route_id strategy + sidecar override，确认 `+N个session覆盖` 显示（S4 回归点）；
- 手工置一条 override（`$route nation1` 发一条），`status` 与 `status overrides` 计数/明细一致；reset 后回到 `(无)`；
- 停代理后 `status` 仍能打印 config 静态段（S10 回归点）；
- `status` 与 `supply`/`strategy` 菜单 list 同窗对比，同实体字段口径一致；
- `switch cc nation1` 的 help 描述与实际行为一致（报错文案或功能补齐后二选一）；
- `status --json` 与 `curl -H "X-Proxy-Admin-Token: ..." http://127.0.0.1:18889/model_proxy/status` 原始响应一致；
- 人工核对 config 数据疑问（需用户确认，非本次改动范围）：nation2 未用 6372 三个 supply 的原因、6372 三条 supply id 尾号 ≠ appkey 尾4 的笔误归属。

## 关联

- [[2026-08-07-cli-status-help-evaluation]]（同题前作一，结论收敛）
- [[2026-08-07-cli-status-help-display-eval]]（同题前作二，结论收敛；三篇建议择一 canonical）
- [[2026-07-28-session-route-dispatch-design]]（route_pool / 一致性哈希 / 跨 route 兜底机制）
- [[2026-08-06-session-overrides-single-storage]]（sidecar 单一存储，sidecar_overrides_count 来源）
- [[2026-08-07-nation-route-supply-expansion]]（nation1/nation2 拆分与不重叠分配设计意图）
- [[2026-08-04-in-band-route-command-design]]（$route 命令，override 明细生产者）
