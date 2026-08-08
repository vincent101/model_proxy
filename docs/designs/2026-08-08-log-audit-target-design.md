---
type: design-decision
status: draft
target: "[[tools/model_proxy/core/server.py]]"
tags: [architect, model_proxy, logging, log-audit]
---

# model_proxy 日志体系审查与目标形态（理想路径）

## 背景与问题

proxy 现有三类记录输出（ACCESS 明细日志、warn 级事件日志、stats 独立账本）散落在同一/不同文件中，随功能迭代逐点添加，从未做过整体审查。任务：全量清点记录点与记录方式，评估"能否凭日志完整还原一次 503/failover"，并给出不计成本的目标设计。本份为纯只读分析，记录点以 grep + 通读代码为准。

## 一、现状：全部记录点清单

### 1.1 写出去向总览

| 去向 | 内容 | 文件/通道 |
|---|---|---|
| 主日志文件 | ACCESS 行 + WARNING 行 + 启动 print + 未捕获异常 traceback 混在同一文件 | `tools/model_proxy/.claude_model_proxy.log`（启动时 `_trim_log` 截断保留末 5000 行，server.py:52-64） |
| stats 账本 | 天桶 × supply×route×strategy 组合键累加，只增不截 | `.claude_model_proxy_totals.json`（server.py:96-231） |
| stdout（nohup 重定向进主日志） | 启动 banner / 锁冲突报错，无时间戳无级别 | server.py:2005, 2031 |
| CLI 终端 print | `_config_ops.py` 全部交互输出，无持久化 | 终端即逝 |

日志通道技术结构：root logger WARNING 级持一个 FileHandler；`model_proxy.access` 独立 logger INFO 级持第二个 FileHandler 写同一文件、propagate=False（server.py:65-80）。translate.py 两个 logger（`core.translate`、`model_proxy.translate_reverse`）无 handler，propagate 到 root。

### 1.2 server.py 记录点

| # | 位置 | 事件 | 级别 | 字段 |
|---|---|---|---|---|
| 1 | `_forward_logged` finally (954) | ACCESS 整请求一条 | INFO(access) | ms,status,source,route,tier,supply,failover(0/1),attempts,usage_in/out,token(tail4),session(全uuid),route_failover(0/1),builtin；**无 method/path/req_id** |
| 2 | 同处 finally (963) | 同步触发 `usage_totals.record` | — | 账本 |
| 3 | `UsageTotalsStore._load` (153) | 账本损坏重置 | WARNING | err |
| 4 | `_forward_logged` (965) | 账本写失败 | WARNING | exc_info |
| 5 | `SyntaxPreferenceStore.learn` (287) | reasoning 语法偏好学习 | **WARNING** | model, variant |
| 6 | `ConfigStore._reload_locked` (407) | 热重载失败保留旧配置 | WARNING | err；**成功无日志** |
| 7 | `extract_route_candidates` (645) | strategy 同时配 route_id+route_pool | WARNING | strategy token；**每请求重复刷** |
| 8 | 同处 (653) | route_pool 非法项跳过 | WARNING | strategy, route_id；**每请求重复刷**（实测日志连刷 4 条） |
| 9 | `_forward` (1047) | 401 no strategy/route matched | WARNING | token_tail4, source；**无 session/path** |
| 10 | (1056) | 400 unknown model tier | WARNING | model, pinned_route |
| 11 | (1098) | route 缺 tier 配置 | WARNING | route, tier |
| 12 | (1100) | route_failover（缺 tier） | WARNING | 与 #11 **双发** |
| 13 | (1120) | 全 supply 失败或冷却 | WARNING | route, tier |
| 14 | (1461) | route_failover（exhausted） | WARNING | 与 #13 **双发** |
| 15 | (1131) | detect_target 失败 500 | WARNING | supply, err |
| 16 | (1137) | 501 UNSUPPORTED 组合 | **无日志** | — |
| 17 | (1202/1220/1241) | 请求转换失败 400 ×3 | WARNING | err；无 supply/route 上下文字段 |
| 18 | (1301/1330) | cooldown+failover（HTTP 状态码） ×2 | WARNING | supply, status, key_tail4；**无 req_id/session** |
| 19 | (1317) | cooldown+failover(net) | WARNING | supply, err, key_tail4 |
| 20 | (1378/1408/1442) | 响应转换失败 500 ×3 | WARNING | err |
| 21 | (1808/1862/1911) | 流式中断 ×3 | WARNING | err |
| 22 | `_log_reasoning_debug` (848) | reasoning 映射细节 | DEBUG | 全字段，env `MODEL_PROXY_REASONING_DEBUG` 门控，调用点双重判级避免拼接——**全代码最规范的一处** |
| 23 | `main` (2005/2031) | 锁冲突 / listening | print | 无格式 |

冷却事件：触发有日志（#18/#19 调用点），过期为被动无事件，`clear_all`（手动 reload，1661）**无日志**。

### 1.3 translate.py 记录点（propagate 到 root WARNING）

| 事件 | 级别 | 说明 |
|---|---|---|
| unsupported block dropped / role dropped / image downgraded / tool_call arguments 非法 JSON 降级 ×10 处 | WARNING | 每请求可多条，**无去重无限流**（实测同一事件 10+ 连刷） |
| 上游响应缺 usage（正向 545 / 反向 1119） | WARNING | 同上刷屏（实测 15 连刷） |
| empty content fallback（538） | INFO | **死日志**：root=WARNING，永远不输出 |
| content_filter triggered（560） | INFO | 同上死日志 |

### 1.4 commands.py（sidecar）

- sidecar 文件损坏：WARNING（221）。
- sidecar 写入（$route set/reset/touch）：**无任何自身日志**，只能靠 ACCESS 的 `builtin=route` + route 字段间接可见；last_seen touch 按设计无盘 IO 也无日志。

### 1.5 控制面（`_dispatch_control`）

/status、/reload、401 未授权、404 **全部无日志**（不经 `_forward_logged`，无 `_acc`）。手动 reload 成功 + 清冷却无任何记录。未授权访问尝试无审计。

### 1.6 CLI 侧读取

- `logs [N]` = `grep ' ACCESS ' log | tail -N`（cli:350-353）——只看 ACCESS 一类。
- `stats` = 读账本 JSON 投影聚合；末尾 `max_ms` 从 ACCESS 日志窗口 awk 提取（cli:546-555）——**跨源口径混搭**，help 已自标注"非账本口径"。

## 二、合理性审查

### 2.1 核心场景检验：能否还原一次 503/failover 全链

目标链路"哪个 session → 哪个 route → 哪个 supply → 什么状态码 → failover 到哪 → 最终结果"：**只能近似还原，不能严格还原**。

- 有：ACCESS 行给终态（route/supply/status/failover/attempts/session）；warn 行给逐 attempt 明细（supply+status）。
- 断点：**两类记录无关联键**。warn 行不带 session/req_id，并发下只能靠时间戳 + supply 名猜测归属。
- 断点：实测 ACCESS 行 `status=503 supply= failover=0 attempts=0`——attempts 只在选中 supply 后才 +1，"全冷却一个请求都没发出去"与"发了全挂"在 ACCESS 上无法区分（前者 attempts=0，后者 attempts≥1 但中间 attempt 的细节只在无关联的 warn 行里）。
- 缺：method/path 不记（同一 session 的 messages 请求与其它请求无法区分）；终态错误摘要（upstream message 提取后只回给客户端，不落日志）。

### 2.2 级别问题

- **全代码 0 个 ERROR**。客户端可见失败（请求/响应转换失败→400/500、流式中断、502）全是 WARNING，偏低，应 ERROR。
- `reasoning_pref learn`（正常自适应学习事件）用 WARNING，**级别倒挂**：真错误与学习提示同级。
- translate.py 两条 INFO 是**死日志**（root WARNING 吞掉）——要么提到 WARNING，要么删除，现状是"以为有记录其实没有"。
- cooldown+failover、config reload 失败、sidecar/账本 corrupt 用 WARNING：恰当。

### 2.3 遗漏的关键事件

1. config 热重载**成功**无日志（什么时候换了配置、换成什么，无迹可查）。
2. 手动 reload + `clear_all` 清冷却无日志（冷却被谁、何时清的不可见）。
3. 501 UNSUPPORTED 无日志（唯一一个静默 5xx 路径）。
4. 控制面全体无日志，含 **401 未授权尝试**（安全审计盲区）。
5. 进程生命周期：启动 print 无格式无时间戳；优雅退出（KeyboardInterrupt）无任何记录。
6. sidecar 写操作（$route set/reset）无事件级日志。
7. `_config_ops.py` 全部运维操作 print 到终端即逝——谁改了 config 无任何持久审计。

### 2.4 冗余/重复

- #11+#12、#13+#14 同一事件双发两行 WARNING。
- #7/#8 配置类问题在**热路径每请求重复刷**（应在 reload 校验一次）。
- translate 降级/缺 usage 类无去重，一次请求刷 10+ 行（有实测证据）。

### 2.5 格式一致性

- 三类记录混一文件、三种格式：ACCESS（`asctime ACCESS k=v`，无级别位）、WARNING（`asctime LEVEL 自然语言`）、print/traceback（无格式）。`grep ' ACCESS '` 靠消息前缀约定，脆弱但可用。
- warn 行是 freestyle 自然语言 + 部分 k=v 混合，awk 提取字段困难（"cooldown+failover: supply=X status=Y" 前缀是自然语言）。

### 2.6 与 stats 账本的边界

划分原则本身清晰且被遵守（明细→日志可截断，累计→账本不截断），但有三处越界/缺口：

1. `max_ms` 依赖日志窗口，口径混搭（CLI 已自知）。
2. **账本只记请求级终态，中间 attempt 不计**：failover 场景被冷却 supply 的失败不体现在该 supply 的 fail 计数里，supply 真实失败率被系统性低估。
3. 异常逃逸（status=0）按 fail 入账；builtin 命令以 `supply=(builtin)` 入账——口径上可接受但需要明示。

### 2.7 写入时机

全同步热路径：每请求至少 1 次 ACCESS 行写 + 1 次账本整文件 JSON dump 原子写。本地单用户量级可接受，非问题；理想形态再议异步。

## 三、目标形态（理想设计，不计成本）

### 3.1 原则

1. **日志答"这一次发生了什么"（明细，可截断）；账本答"累计有多少"（聚合，不截断）**。凡聚合口径需要的字段必须进账本，日志只做佐证。
2. 全部记录结构化 k=v（或 JSON Lines），统一基底字段 `ts level event req_id`。
3. 一次请求一个 req_id，请求内所有事件行与最终 ACCESS 行都携带，任何 503/failover 链可凭 req_id 严格还原。

### 3.2 级别规范

| 级别 | 语义 | 现有记录点迁移 |
|---|---|---|
| ERROR | 客户端拿到失败响应或流被截断 | 请求/响应转换失败 ×6、流式中断 ×3、detect_target 500、未捕获异常、终态 5xx |
| WARNING | 可恢复降级、数据有损但流程继续 | cooldown+failover、route_failover、内容降级丢弃、上游缺 usage、reload 失败、sidecar/账本 corrupt |
| INFO | 生命周期与运维事件（默认级别从 WARNING 降到 INFO，root 开 INFO） | 启动/停止、reload 成功（含 supplies/routes/strategies 计数或 diff 摘要）、手动 reload 清冷却、cooldown.set/clear_all、reasoning_pref learn、$route 写、控制面调用与 401 |
| DEBUG | 排障细节 | reasoning_debug（保持现状 env 门控模式） |

### 3.3 事件清单（event= 命名规范，补齐遗漏）

- `process.start`（port, pid, config_path）/ `process.stop`（signal）——替代裸 print。
- `config.reload.ok`（mtime, 各段计数或 diff 摘要）/ `config.reload.fail`（err）——配置类校验（route_pool 非法项、route_id+route_pool 互斥）**挪到 reload 时一次性告警**，热路径不再每请求刷。
- `admin.reload`（清冷却动作显式记录）/ `admin.status` / `admin.auth_fail`（来源无关，仅记事件）。
- `cooldown.set`（req_id, supply, seconds, reason=status/err, key_tail4）——合并现 #18/#19；`cooldown.clear_all`；`cooldown.expire` 保持被动不记（可在 select_supply 跳过时 DEBUG）。
- `route.failover`（req_id, from_route, to_route, reason=missing_tier/exhausted）——合并现双发。
- `request.reject`（req_id, status, reason）——统一 401/400/501，消灭静默 501。
- `sidecar.write`（req_id, token_tail4, session, from_route→to_route / reset）。
- `translate.degrade`（req_id, kind, detail）——按 (kind,supply) 限流：每分钟首条全量 + 后续 suppressed=N 汇总。
- `stream.interrupted`（req_id, mode, err）ERROR。

### 3.4 ACCESS 行字段目标集

在现有基础上增 `req_id`、`method`、`path`（端点尾缀）、`final_error`（终态错误摘要，截断）；`failover`/`route_failover` 从 0/1 改计数；区分 `attempts`（选中 supply 次数）与新 `cooling_skips`（因冷却跳过次数），消灭"全冷却"与"全失败"不可区分的盲区。

### 3.5 账本边界修正

- 桶内增 `max_ms`，CLI stats 的 max_ms 改从账本取，与 avg_ms 同口径，斩断对日志窗口的依赖（schema 升 v3，旧文件迁移）。
- 增 attempt 级累计（每 combo 增 `attempts`、`attempt_fail` 字段，failover 的中间失败计入对应 supply），使 supply 真实失败率可观测；请求级 requests/ok/fail 语义不变。
- status=0 与 builtin=(builtin) 入账语义写进 README/账本注释，明示口径。

### 3.6 去向划分

- 主日志：logging 统一格式；`RotatingFileHandler` 按大小轮转（如 5×1MB）替代启动时截断 5000 行——不再因重启丢历史。
- 账本：保持独立 JSON 文件只增不截。
- 运维审计：`_config_ops.py` 增写 `config_audit.log`（who/when/what：子命令、变更对象 id、reload 是否触发），CLI 操作不再即逝。
- 写盘异步化：`QueueHandler`+`QueueListener` 把 ACCESS/事件/账本写挪出请求线程（理想形态；账本写需保留"异常告警回热路径"通道）。

### 3.7 读取端适配

- `logs` 子命令支持按 req_id / event / level 过滤（如 `logs req=xxx`、`logs event=cooldown.set`）。
- `stats` 的 max_ms 与 attempt 级失败率从新账本字段出。

## 风险与权衡

- **迁移代价提示**（理想路径不计成本，仅知情）：涉及 server.py 全部 20+ 记录点改造、translate.py 级别修正与限流、账本 schema v2→v3 迁移、CLI 读取端重写、新增 audit 文件；面大但逐点独立，可分批。req_id 引入需在 `_forward_logged` 生成并透传到所有 warn 调用点（改动点多但机械）。
- 配置校验挪到 reload 需 ConfigStore 增校验回调，与"reload 失败保留旧配置"的容错语义要协调（校验告警 ≠ 拒绝加载）。
- 日志轮转改变文件假设，`logs`/`stats` 的 grep 目标需包含轮转历史（或 stats 全部去日志化后无此问题）。

## 验证方式

1. 复核清单：`grep -n "log\.\|logger\.\|print(" core/server.py core/translate.py core/commands.py` 对照本文 §1 表逐条核对。
2. 核心场景演练：构造 failover 链（坏 key supply + 好 supply），改造后凭 req_id 单条 grep 应输出完整链：cooldown.set →（可选 route.failover）→ ACCESS 终态。
3. 级别抽查：`grep -c ERROR log` 不再为 0；`reasoning_pref` 不出现在 WARNING。
4. 账本口径：`stats` 输出的 max_ms 与 attempt_fail 与日志窗口内手工 awk 结果一致（迁移窗口期交叉验证）。
5. 刷屏回归：同一缺 usage 上游连续 20 请求，日志中 translate.degrade 行数 ≤ 2（首条 + suppressed 汇总）。

## 关联

- [[2026-07-22-access-log-and-latency]]（ACCESS 日志与 k=v 风格的最初设计）
- [[2026-07-23-usage-totals-ledger]]（stats 账本设计）
- [[2026-07-23-model-proxy-full-audit]]（全量审查，本文是其日志维度的深化）
- [[tools/model_proxy/core/server.py]]、[[tools/model_proxy/model_proxy_cli.sh]]
