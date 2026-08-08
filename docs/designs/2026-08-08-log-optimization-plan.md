---
type: design-decision
status: pending
target: "[[tools/model_proxy/core/server.py]]"
tags: [architect, model_proxy, logging, optimization-plan]
---

# model_proxy 日志体系优化方案（逐问题核实 + 设计原因 + 分批落地）

## 背景与问题

在 [[2026-08-08-log-audit-target-design]]（下称"审查文档"）基础上做三件事：逐个核实其 38 处记录点的行号与描述、追每个问题的当时设计原因、把它的理想目标形态拆成可分批落地的优化项。本文为纯只读分析，核实以 grep + Read 为准，行号均为本文重新核实后的值。

**核实总述**：审查文档的行号与描述与代码现状**基本一致，无漂移**。server.py 27 个 log/print 调用点、translate.py 20 个、commands.py 1 个全部核对通过。日志文件实证（`.claude_model_proxy.log` 当前 5025 行窗口）：ACCESS 1706 / WARNING 2856 / DEBUG 131 / **ERROR 0 / INFO 0**。新发现 2 处审查文档漏列的静默点（见问题 1/3）。

---

## 问题 1：503/failover 链无法严格还原

### 核实结果

- ACCESS 行在 server.py:954-961（`_forward_logged` finally），字段与审查文档一致，**确认无 req_id/method/path**。
- 逐 attempt warn 三处：1301-1302（HTTP 状态码 failover）、1317-1318（网络错误 failover）、1330-1331（成功响应但冷却信号码），均只带 `supply/status/err/key_tail4`，**确认无 session/req_id**。
- attempts 计数在 1126-1127：选中 supply 后才 `+1`；冷却跳过发生在 `select_supply` 内部，不可见。实证：`status=503 supply= failover=0 attempts=0 route_failover=1`（日志 2026-08-08 13:54 两条）与 `attempts=1 failover=1` 的 503（同分钟一条）并存——attempts=0 可区分"一个都没发出去"，但无法区分"全冷却"与"route 缺 tier 配置"（1105-1108 与 1466-1469 两个 503 出口都可能是 attempts=0）。
- **审查文档漏列**：两个"不 failover 的上游错误"出口无任何 warn 行——1307-1314（HTTPError 回传上游状态码，upstream_msg 只回客户端）与 1323-1325（URLError 且 failover=off → 502）。只有 ACCESS 的 status 字段可见，无事件细节。

### 设计原因

- **无 req_id 是明确的务实取舍**，非遗漏。[[2026-07-22-access-log-and-latency]] 风险节原文："request_id 不引入：本可给每请求生成短 rid 串联 access 与 WARNING 行，但个人工具并发极低，靠时间戳 + token_tail 已能人工对齐两类日志；引入 rid 需改动 20 余处 WARNING 调用点……务实路径下收益 < 改动成本。若日后并发上升再补。"
- "ACCESS 记终态、warn 记逐 attempt 明细"的互补分工同为该文档的明确设计（"现有 WARNING 已逐次记了，两者天然互补，无需为明细再新增日志"）。
- attempts 只在选中后 +1：该文档实施节明写"每进一次循环体 attempts += 1"（在选中 supply 后），属设计如此；"全冷却 vs 缺 tier"的 503 歧义当时未识别。

### 优化方案

| 项 | 内容 | 独立性 | 依赖 | 风险 | 工作量 | 优先级 |
|---|---|---|---|---|---|---|
| OPT-01 | **req_id 全链关联**：`logging.Filter` 从 `threading.local` 读 req_id 注入 record，文件 handler 的 Formatter 加 `%(req_id)s`（默认 `-`）；do_* 入口生成 `uuid4().hex[:8]`（含 `_dispatch_control` 路径），finally 清除；ACCESS 行加 `req_id=` 字段。**不改任何 warn 调用点签名**，translate.py/commands.py 同线程 propagate 自动获益 | 独立 | 无 | 低：Filter 需覆盖两个 FileHandler；CLI awk 按键名解析（`^ms=` 逐字段 split），新增字段向后兼容 | M | **P0** |
| OPT-04 | **终态可区分 + final_error**：`_acc` 加 `final_error`，各错误出口（503×2、501、502、不 failover 上游错误、401/400）写短 reason；ACCESS 行加 `final_error=`（截断 80 字符，空格转下划线保 k=v 可解析）。可选扩展：`select_supply` 返回冷却跳过计数 → `cooling_skips=` 字段（+S） | 独立 | 无 | 低：纯增量字段 | S | **P0** |

---

## 问题 2：级别倒挂

### 核实结果

- 全代码 grep 确认 **0 个 `log.error`**；日志文件 5025 行窗口 **ERROR 0 行、INFO 0 行**，双重确认。
- 客户端可见失败全是 WARNING：请求转换失败 ×3（1202/1220/1241）、响应转换失败 ×3（1378/1408/1442）、流式中断 ×3（1808/1862/1911）、detect_target 500（1131）。
- `reasoning_pref learn`（287）正常学习事件用 WARNING，确认倒挂。
- translate.py 两条 INFO（538 empty content fallback、560 content_filter）propagate 到 root WARNING 被吞——**实证死日志**（全文件 0 INFO 行）。translate.py 两个 logger（55-56 行）无 handler，确认 propagate。
- root logger 配置在 65-69（basicConfig WARNING + FileHandler）；access logger 75-80（INFO、同文件、propagate=False）。

### 设计原因

- WARNING-only 是**明确取舍**：access-log 设计背景节"WARNING-only 作为「错误追踪」对个人工具是合理且够用的"；root 不开 INFO 也是明确的："不改 root logger 级别（避免误收 INFO 噪声）"。该设计做了"错误追踪够用"的判断，但**从未定义级别语义**（什么该 ERROR）——0 ERROR 是"级别语义未建"的结果，不是"有意全压 WARNING"。
- `reasoning_pref` 用 WARNING：**无明确设计依据**。2026-07-19 引入（d3ed64c0），当时 root=WARNING 是唯一配置，想可见只能 WARNING/ERROR，疑似随手提级求可见。
- 两条死 INFO：**设计文档原文即 `logger.info`，实施照抄**——[[2026-07-23-chat-reasoning-content-fallback]]:94 的设计代码片段就是 `logger.info("empty content fallback: ...")`，content_filter 处注释"丢弃 + 记 log（§2.4）"表明有意要记。两级都没察觉 root=WARNING 会吞掉，属**设计+实施双层遗漏**。

### 优化方案

| 项 | 内容 | 独立性 | 依赖 | 风险 | 工作量 | 优先级 |
|---|---|---|---|---|---|---|
| OPT-02 | **客户端可见失败提 ERROR**：1202/1220/1241/1378/1408/1442/1808/1862/1911/1131 共 10 处 WARNING→ERROR。纯级别调整，语义=客户端拿到失败响应或流被截断 | 独立 | 无 | 无 | S | **P0** |
| OPT-03 | **死日志修复**：translate.py 538/560 INFO→WARNING（语义=内容降级，与 267 等处同级）。注意与 OPT-08 关系：root 开 INFO 后它们自然复活，但提 WARNING 仍要做（级别语义正确性），两者独立不冲突 | 独立 | 无 | 无 | S | **P0** |
| OPT-09 | **reasoning_pref 降 INFO**：287 WARNING→INFO | 独立 | **OPT-08**（root 开 INFO），否则变死日志 | 无 | S | P1 |

---

## 问题 3：关键事件静默

### 核实结果

全部确认，行号：config reload 成功无日志（`_reload_locked` 398-408，仅失败 407 有 WARNING）；手动 reload + 清冷却无日志（`_handle_reload` 1653-1662、`clear_all` 439-442）；501 无 warn（1137-1142，唯一无事件的 5xx 出口，但 ACCESS 有 status=501）；控制面无日志（`_dispatch_control` 1589-1615：503 未配 token / 401 未授权 / 404 均无记录）；进程退出无日志（main 2034-2035 `except KeyboardInterrupt: pass`）；sidecar 写无事件日志（commands.py `apply_command` 295-374 写盘成功无记录；`touch` 286-289 纯内存）；`_config_ops.py` 40 处 print、0 处 logging，全部终端即逝。**新增发现**：问题 1 的两处不 failover 上游错误出口（1307-1314、1323-1325）也属静默，归 OPT-04 覆盖。

### 设计原因

- **控制面不记 ACCESS 是明确取舍**：access-log 设计 §3"不记 access 日志。它们是低频运维动作、无 supply/tier 语义，记了只是噪声"。但该决策只排除了 ACCESS 这一种形式，**"要不要事件级记录（含 401 审计）"从未被讨论**——401 无审计属遗漏，不是安全考量。
- reload 成功无日志：`ConfigStore` docstring 明写骨架"拷贝 proxy.py"（294-300 行注释），`_reload_locked` 沿袭旧实现，**无明确依据，疑似遗漏**。
- sidecar `touch` 无 IO 无日志：**明确设计**（[[2026-08-04-in-band-route-command-design]] §5.4/V13"热路径无写盘 IO"）。`apply_command` 写盘无事件日志：该设计 §304 以 ACCESS `builtin=route` 字段为记录手段（"ACCESS 日志要记，加可辨识字段"），**有意从简**，但缺 from_route→to_route 细节。
- 501 无 warn、进程退出无日志、`_config_ops` 无持久化：**均无明确设计依据，疑似遗漏**。

### 优化方案

| 项 | 内容 | 独立性 | 依赖 | 风险 | 工作量 | 优先级 |
|---|---|---|---|---|---|---|
| OPT-08 | **运维/生命周期事件 + root 开 INFO**：① root WARNING→INFO（已核实 stdlib 无 INFO 噪声：`log_message` 已屏蔽、urllib 不打 INFO）；② 启动/锁冲突/退出 print 改 `log.info`（port/pid/config_path；nohup 重定向同一文件，CLI start 只 `tail -5` 不解析，无兼容问题）；③ `_reload_locked` 成功 `config.reload.ok`（mtime + supplies/routes/strategies 计数）；④ `_handle_reload` `admin.reload`（cleared_cooldowns=N）；⑤ 控制面：`admin.status` INFO、**401 `admin.auth_fail` WARNING**（不记 token 值）、404 INFO；⑥ 501 出口 `request.reject` WARNING（source/target/mode） | 独立 | 无 | 低：点多但每点 1-3 行；root 开 INFO 后 translate 两条死日志自然复活（OPT-03 仍单独做） | M | **P1** |
| OPT-13 | **`_config_ops` 运维审计**：新增 `config_audit.log`（时间/子命令/变更对象 id/是否触发 reload），CLI 层写入 | 独立 | 无 | 低 | M | P2 |
| sidecar 写事件 | `apply_command` 成功后 `log.info("sidecar.write ...")`（token_tail4/session/action/target_route） | 并入 OPT-08 | — | — | （含在 OPT-08 内） | P1 |

---

## 问题 4：冗余刷屏

### 核实结果

- 双发两对确认：1098（route missing tier）+ 1100-1102（route_failover trying next）非末候选时必双发；1120-1121（all supplies failed or cooling）+ 1461-1463（route_failover exhausted trying next）同。
- 配置类 warn 每请求刷确认：645-646（route_id+route_pool 互斥）、653-654（route_pool 非法项）在 `extract_route_candidates` 内，每请求必经（调用点 1039）。
- translate 连刷确认并有实证：缺 usage 两条（545-548 正向、1118-1121 反向）同一毫秒连刷 5 条（日志 5021-5025 行）；降级类 12 处 WARNING 无去重无限流。

### 设计原因

- 双发是**增量演进产物**：1098/1120 是 proxy.py 沿袭的存量条件告警；1100/1461 是 [[2026-07-28-session-route-dispatch-design]] 引入跨 route failover（§3 选项B）时新增的动作告警（该设计要求"ACCESS 日志加跨 route failover 标记"）。两处语义不同（条件 vs 动作），但总是同时触发，等于双发——非复制粘贴，也非有意两行。
- 配置 warn 每请求刷：**兜底意图是明确的**（640-644 注释："配置文件可能被手工/外部改动绕过写入侧校验，运行时兜底……但要留日志可见性"），但只考虑了"可见"，没考虑"每请求重复"——部分有意、部分遗漏。
- translate 连刷：**无明确依据，疑似遗漏**（从未设计去重/限流）。

### 优化方案

| 项 | 内容 | 独立性 | 依赖 | 风险 | 工作量 | 优先级 |
|---|---|---|---|---|---|---|
| OPT-05 | **双发合并**：非末候选只打 route_failover 动作行（消息内含 missing tier / exhausted 原因，吞并条件行）；末候选只打条件行 | 独立 | 无 | 低：消息文案变化，无机器消费者 | S | P1 |
| OPT-06 | **配置校验挪 reload**：`ConfigStore` 增校验回调，启动 `_reload` 与 `_reload_locked` 成功后各跑一次（route_id+route_pool 互斥、route_pool 非法项各告警一次）；热路径 645/653 删 WARNING（或降 DEBUG 保底） | 独立 | 无 | 中：保持"校验告警 ≠ 拒绝加载"的容错语义；`maybe_reload` 只在 mtime 变时触发，告警频率=启动一次+每次变更一次 | M | P1 |
| OPT-07 | **translate 降级/缺 usage 限流**：module 级限流 helper（key=事件 kind，60s 窗口，首条全量 + 窗口末 `suppressed=N` 汇总一条），挂到 translate.py 全部 14 处 WARNING | 独立 | 无 | 低：汇总条在窗口末才出，进程退出丢 suppressed 计数（可接受） | M | P1 |

---

## 问题 5：stats 越界

### 核实结果

- `UsageTotalsStore.record`（177-204）：每请求 finally 调用一次（963），只记终态 `_acc`；`ok/fail` 按 `status==200` 分（186-187）。**中间 attempt 确认不入账**——failover 被冷却 supply 的失败不体现在该 supply 的 fail 计数。
- CLI `stats` 的 max_ms 确认从日志窗口 awk 提取（model_proxy_cli.sh:546-555），输出自标注"(近日志窗口内，非账本口径)"。
- builtin 入账确认：`_acc["builtin"]="route"`（1488），ACCESS 记 `supply=(builtin)`，账本 combo key 实证存在 `supply=(builtin)|route=nation2|strategy=cc` 等条目。status=0（异常逃逸）按 fail 入账确认（186-187 逻辑必然）。
- 账本 bucket 结构确认无 max_ms（118-125）。

### 设计原因

- **max_ms 依赖日志窗口是明确取舍**：[[2026-07-23-usage-totals-ledger]] §1"**max 不存**，由日志补（§3）"，风险节"max ms 与账本口径不一致……输出已显式标注"。当时是知情妥协。
- **中间 attempt 不入账**：账本的设计目标是"token 用量长期累计"（failover 中间 attempt 不产生 usage），record 挂 finally 每请求一次是结构决定；supply 失败率观测**不在该文档的设计目标内，无"有意不记"的依据**——属设计目标未覆盖，审查文档"系统性低估"的判断成立。
- **builtin 入账是明确设计**：in-band 设计 §303"倾向后者：可观测「这功能被用了多少次」，又不污染成本统计"。但未写进 README/账本注释，口径不明示。
- **status=0 入账**：无明确依据，口径未明示。

### 优化方案

| 项 | 内容 | 独立性 | 依赖 | 风险 | 工作量 | 优先级 |
|---|---|---|---|---|---|---|
| OPT-10 | **账本 schema v3（一次迁移做两件事）**：① bucket 加 `max_ms`（record 时 max 比较），CLI stats 的 max_ms 改从账本取、删日志 awk 段；② combo 加 `attempts`/`attempt_fail`：`_acc` 增 `attempt_errors: list[(supply_id, reason)]`，三处 failover continue 前 append，record 遍历入账到对应 combo。`_load` 迁移 v2→v3：旧桶补 `max_ms=0`、combo 补 `attempts=requests, attempt_fail=fail`（近似值，README 注明口径） | 独立 | 无 | 中：迁移近似值口径需明示；CLI stats 段落重写；建议补迁移单测 | M-L | **P1** |
| OPT-12 | **口径明示**：status=0 按 fail、builtin 以 `supply=(builtin)` 入账写进 README | 独立 | 无 | 无 | S | P2 |

---

## 分批落地路线图

```
批次一（P0，硬伤修复，合计 ≈ S×3 + M ≈ 0.5-1 人日）
  OPT-01 req_id 全链关联        ← 核心断点修复
  OPT-02 10 处提 ERROR          ← 级别倒挂修复
  OPT-03 死日志提 WARNING
  OPT-04 final_error + 503 可区分（+2 处新发现静默出口）
  验证：构造 failover 链，单条 grep req_id 还原全链；grep -c ERROR ≠ 0

批次二（P1，事件补齐 + 去刷屏 + 账本口径，合计 ≈ 2 人日）
  OPT-08 运维/生命周期事件 + root 开 INFO（sidecar 写事件含在内）
  OPT-09 reasoning_pref 降 INFO（依赖 OPT-08）
  OPT-05 双发合并
  OPT-06 配置校验挪 reload
  OPT-07 translate 限流
  OPT-10 账本 schema v3（max_ms + attempt 级，一次迁移）
  OPT-11 logs 子命令过滤（依赖 OPT-01/08 的 req_id 与级别，S-M）
  内部顺序：OPT-08 先于 OPT-09/11；OPT-10 独立随时可做
  验证：缺 usage 上游连发 20 请求 → 日志 ≤ 2 条；stats 的 max_ms 与手工 awk 交叉一致

批次三（P2，锦上添花，按需独立选做）
  OPT-12 口径写 README（S）
  OPT-13 _config_ops 审计日志（M）
  OPT-14 RotatingFileHandler 替代 _trim_log（S-M；原设计明确不引入、理由是重启频繁，
         该理由仍成立——只有接受"重启不再丢历史"的价值才做）
  OPT-15 写盘异步化 QueueHandler（M；QPS≪1 无实际收益，仅对齐理想形态，可不做）
  OPT-16 event= 命名规范统一存量 warn 格式（M-L；依赖 OPT-01/08 落地，无机器消费者、风险低）
```

**依赖汇总**：OPT-09 → OPT-08；OPT-11 → OPT-01 + OPT-08；OPT-16 → OPT-01 + OPT-08；其余全部独立。OPT-10 与任何项无依赖，但两件事必须同批做（省一次 schema 迁移）。

## 风险与权衡

- **OPT-01 的 Filter 方案是唯一架构性选择**：用 threading.local + Filter 注入而非改 20+ 调用点签名，是因为原设计否决 req_id 的理由就是"改 20 余处 WARNING 调用点成本"——Filter 方案恰好拆掉这个成本，使当年的务实取舍在 2026 年低成本可逆。风险点：`_dispatch_control` 不经 `_forward_logged`，req_id 生成须放 do_* 入口；非请求线程的 record 由 Filter 给默认值 `-`。
- **root 开 INFO（OPT-08）推翻的是原设计的明确决策**（"避免误收 INFO 噪声"）。复核后认为原顾虑不成立：进程内 INFO 级调用方只有 translate.py 两条（且本就该可见），stdlib 无噪声。若实施后真有噪声，把对应 logger 单独压回 WARNING 即可，可逆。
- **OPT-10 迁移近似值**（旧桶 attempt_fail=fail）会让历史数据的 attempt 口径略虚高，README 注明"迁移前数据 attempt 口径为近似"即可；不接受则可旧桶补 0，代价是历史 supply 失败率断档。需用户决策。
- **OPT-07 限流的 suppressed 汇总**在窗口末才输出，实时排查时首个事件可见、后续被计数吞掉——首条全量已保证"知道发生了什么"，suppressed=N 保证"知道发生了多少次"，语义不丢。
- **迁移代价提示**（理想分析、分批落地）：批次一即可解决审查文档的两个硬伤（503 不可还原、级别倒挂），改动集中在 server.py 日志基础设施层；批次二面最广（8 个文件触点）但每项独立可拆；批次三全可选。不建议一次全做——P0 落地后先跑一段时间验证 req_id 排查体验，再决定 P1/P2 节奏。

## 验证方式

1. 批次一：构造 failover 链（坏 key supply + 好 supply）→ `grep <req_id> .claude_model_proxy.log` 应输出完整链（cooldown.set warn + ACCESS 终态，同一 req_id）；`grep -c ERROR` ≥ 1（原 0）；`status=503` 的 ACCESS 行 `final_error` 区分"all supplies cooling"与"route missing tier"。
2. 批次二：改 config 触发 mtime reload → 日志出现 `config.reload.ok`；手动 reload → `admin.reload cleared_cooldowns=N`；错误 admin token → `admin.auth_fail` WARNING；缺 usage 上游连发 20 请求 → translate 类日志 ≤ 2 条（首条 + suppressed 汇总）；`stats` 的 max_ms 与日志窗口手工 awk 交叉一致（迁移窗口期）。
3. 回归：tests/ 全绿；`logs`/`stats` 命令输出格式向后兼容（新增字段在尾部，awk 按键名解析不受影响）。
4. 单测补充：Filter req_id 注入（有/无请求上下文）、账本 v2→v3 迁移、限流 helper 窗口行为。

## 关联

- 现状依据：[[2026-08-08-log-audit-target-design]]（38 处记录点清点与理想目标形态，本文为其落地拆解）
- 设计原因出处：[[2026-07-22-access-log-and-latency]]（req_id 不引入/WARNING-only/控制面不记 ACCESS 的原文取舍）、[[2026-07-23-usage-totals-ledger]]（max 不存由日志补/账本只记终态）、[[2026-07-23-chat-reasoning-content-fallback]]（死 INFO 出处）、[[2026-07-28-session-route-dispatch-design]]（route_failover warn 来源）、[[2026-08-04-in-band-route-command-design]]（sidecar 无事件日志/builtin 入账）
- 代码：[[tools/model_proxy/core/server.py]]、[[tools/model_proxy/core/translate.py]]、[[tools/model_proxy/core/commands.py]]、[[tools/model_proxy/model_proxy_cli.sh]]
