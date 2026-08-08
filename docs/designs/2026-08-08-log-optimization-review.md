---
type: review
status: draft
target: "[[2026-08-08-log-optimization-plan]]"
tags: [architect, model_proxy, logging, review]
---

# model_proxy 日志优化方案 独立审查（挑刺复核）

审查对象：[[2026-08-08-log-optimization-plan]]（下称"优化方案"）+ 其依据 [[2026-08-08-log-audit-target-design]]（下称"审查文档"）。本份为独立第二意见，只挑硬伤与遗漏，不重做设计。核实以 grep + Read 为准，行号为本审查重新核实后的**当前工作树**值。

## 结论速览

- **代码不属实（现已漂移，但非核实失实）**：两份文档的行号对**写作当时**的代码（08-07 提交 `895f556e`）**完全准确**，优化方案"全部核对通过"的声称在写作时成立。但文档写完后代码被大改（server.py `+226/-23`、translate.py `+209/-3`），引入了文档清单**未覆盖的新功能 budget_retry**。行号现已系统性漂移。
- **阻断级遗漏 1 项**：优化方案落地前必须按当前代码重新对齐基线（行号 + 新增 budget_retry 的 4 个 warn 点 + 3 个 ACCESS 新字段），否则按方案行号改会改错行、且漏改新记录点。
- **设计原文引用全部属实**：三处被引设计文档原文逐字核对，无断章取义。这是方案的扎实之处。
- 其余维度二/三的多项事实（线程模型、CLI 内部、流式旁路、账本迁移并发）因本次审查中断**未及核实**，已在对应条目标注"未核实"。

---

## 一、代码属实性核实

### 1.1 行号：写作时准确，现已漂移（本审查最关键发现）

**时间线（`stat` + `git log` 实证）**：

| 时刻 | 事件 |
|---|---|
| 08-08 15:18:19 | 审查文档写完 |
| 08-08 15:32:45 | 优化方案写完 |
| 08-08 15:44:08 | **translate.py 被修改（+209/-3）** |
| 08-08 15:49:02 | **server.py 被修改（+226/-23）** |
| 08-08 16:05:53 | 上述改动随 vault backup 提交（`0a4f83fd`） |

**归因（公平起见必须说清）**：用 `git show 895f556e:.../server.py`（08-07 提交，即文档写作时的代码状态）比对，文档行号**逐条吻合**——ACCESS 955、401 at 1047、三处 failover 1301/1317/1330、stream 1808/1862/1911 等全部命中。因此优化方案"核实通过、无漂移"**在写作时是真实的**，不是核实失实。漂移源于文档写完之后的代码大改。

**但结论仍成立**：以**当前工作树**为准，文档行号已不可用，且出现了文档未曾清点的内容。server.py 日志/print 调用点由 28 个增至 **32 个**。

**新增功能 budget_retry（两份文档均未覆盖）**：
- 新增函数：`_maybe_budget_retry`、`_stamp_budget`、`get_budget_retry`（server.py）；translate.py 侧新增 thinking-block 助手 `_content_block_delta_thinking`/`_content_block_start_thinking`/`_flush_thinking_block`/`_extract_reasoning_thinking_text`/`is_budget_truncated`（**translate.py 无新增日志调用，仅行号移位**）。
- server.py 新增 **4 个 warn 调用点**（当前行号）：
  - `1179-1182` `budget_truncated: supply=... budget=... retries=...`（非流式，到上限/无基线，如实返回截断响应）
  - `1190-1191` `budget_retry: supply=... budget old→new (n/max)`（触发预算放大重试）
  - `1473-1475` `budget_truncated(stream,不重试): ... stop=...`（PASSTHROUGH 流式收口）
  - `1521-1522` `budget_truncated(stream,不重试): ...`（ANTHROPIC_TO_CHAT 流式收口）
- ACCESS 行新增 **3 个字段**：`budget_retried` / `budget_truncated` / `stop_reason`（当前 991-1000）。审查文档 §1.2 的 ACCESS 字段清单（无这三个）与优化方案 OPT-04 的字段目标集都已落后于现状。

### 1.2 设计原因引用：全部属实（逐字核对通过）

| 优化方案引用 | 原文出处（已核对） | 结论 |
|---|---|---|
| access-log "request_id 不引入…个人工具并发极低…改动 20 余处 WARNING 调用点…务实路径收益<成本" | `2026-07-22-access-log-and-latency.md:404-406` | 属实，无断章取义 |
| access-log "控制面不记 access…低频运维…记了只是噪声" | 同文 `:116` | 属实 |
| access-log "不改 root 级别（避免误收 INFO 噪声）" | 同文 `:34` | 属实 |
| usage-totals "max 不存，由日志补（§3）" | `2026-07-23-usage-totals-ledger.md:117` | 属实 |
| usage-totals "max ms 与账本口径不一致…已显式标注" | 同文 `:306` | 属实 |
| 死 INFO 出处即设计代码 `logger.info("empty content fallback...")` | `2026-07-23-chat-reasoning-content-fallback.md:94` | 属实（逐字一致，设计+实施双层遗漏的判断成立） |

### 1.3 日志实证：ERROR 0 / INFO 0 确认属实

当前 `.claude_model_proxy.log`（5125 行窗口）实测：`ACCESS 1833 / WARNING 2725 / DEBUG 131 / ERROR 0 / INFO 0`。优化方案"0 ERROR、0 INFO、死日志"的实证**属实**。

附带观察（非方案问题，属环境事实）：日志尾部（17:06）ACCESS 行仍是**旧格式**（无 budget 字段），说明当前运行进程跑的是改动前的旧代码、尚未重启——工作树代码已领先运行进程。这对 OPT-10（账本 schema 迁移）有操作层面的含义，见遗漏清单。

### 1.4 优化方案"2 处新发现静默出口"：实质正确

优化方案称审查文档漏列 1307-1314 / 1323-1325 两处不 failover 上游错误出口无 warn。**核实属实**（当前行号）：
- `1430-1437`：HTTPError 不 failover → `_extract_upstream_error_message` 提取后仅回客户端，`_write_buffered_response` 后 `return`，**全程无 log**。
- `1446-1448`：URLError 且 failover=off → 写 502 后 `return`，**全程无 log**。

两处确只有 ACCESS 的 status 字段可见、无事件细节，审查文档 §1.2 确未列出。优化方案的新发现**成立**（仅行号为 08-07 旧值）。

### 1.5 P0 涉及行号 逐条核对表（文档/08-07 值 → 当前工作树值）

| 记录点 | 文档值 | 当前值 | 事件是否仍在/性质 |
|---|---|---|---|
| ACCESS（`_forward_logged` finally） | 954-961 | **991-1000** | 在；新增 3 字段 |
| `usage_totals.record` 同步调用 | 963 | **1002** | 在 |
| record 失败 warn | 965 | **1004** | 在 |
| failover warn ×3 | 1301/1317/1330 | **1424/1440/1453** | 在 |
| 不 failover 上游错误（HTTP） | 1307-1314 | **1430-1437** | 在，静默 |
| 不 failover 上游错误（net→502) | 1323-1325 | **1446-1448** | 在，静默 |
| 死 INFO ×2（translate） | 538/560 | **616/638** | 在，仍死（INFO 0 实证） |
| 501 UNSUPPORTED 无 warn | 1137-1142 | **1243-1248** | 在，静默 |
| 401 no strategy/route | 1047 | **1086** | 在 |
| config reload 失败 warn | 407 | **419** | 在 |
| reasoning_pref learn | 287 | **287** | 在（文件顶部，未漂移） |
| stream interrupted ×3 | 1808/1862/1911 | **1982/2036/2085** | 在 |
| main print ×2 | 2005/2031 | **2208/2234** | 在 |
| attempts 选中后 +1 | 1126-1127 | **1233** | 在 |

**结论**：所有被抽查的 P0 记录点**事件本身均核实存在、性质与文档描述一致**；唯一问题是**行号已整体漂移**（漂移量随文件位置递增，顶部 287 未漂，底部漂 ~230 行）。

---

## 二、遗漏清单（按严重度）

### 阻断级

**O-1 优化方案须按当前代码重新对齐基线后才可落地。**
行号整体漂移 + budget_retry 新功能未入清单，两者叠加意味着：照方案当前行号实施会改错行；且方案对"38 处记录点 / server.py 27 调用点"的清点已少计 4 个 warn 点与 3 个 ACCESS 字段。具体影响：
- OPT-01 的 ACCESS 改造、OPT-02 的 10 处提 ERROR、OPT-04 的 final_error，全部以旧行号定位，需重定位。
- **OPT-04 的字段目标集未含 budget 三字段**——而这三个字段（budget_retried 轨迹、budget_truncated 截断标记、stop_reason）恰是"还原一次请求发生了什么"的新增关键证据，目标字段集应把它们纳入统一设计，而不是事后补丁。
- budget_retry 引入了**第三类重试机制**（既非 reasoning-variant 重试，也非 failover）：`_maybe_budget_retry` 命中后 `continue` 重进 `while`（当前 1479-1481、1531-1532），同一 supply 重选会在 `1233` 再次 `attempts += 1`。**优化方案对"attempts 只在选中后 +1"的语义分析（问题 1）未覆盖这一新路径**——budget 重试会抬高 attempts，与"全冷却 attempts=0""全失败 attempts≥1"的判别逻辑交织，需重新确认 OPT-04 的可区分性设计在新语义下是否仍成立。

### 建议级

**O-2 req_id 的 `threading.local` 隔离正确性 —— 未核实（OPT-01 的前置依赖）。**
任务关切的"多线程 HTTP server 下 threading.local 是否隔离、warn 是否都在请求线程触发、流式/子线程能否取到 req_id"，本次审查**未及用代码核实**（需确认是否 `ThreadingHTTPServer`、`_write_streaming_response`/`_write_translated_stream` 是否同步在请求线程内、有无子线程回调）。从已读代码看，流式写回（如 1466 `_write_streaming_response`）是**同步内联调用**、未见显式线程派生，倾向"同线程、threading.local 可行"，但**未验证**无线程边界。落地 OPT-01 前必须核实；若存在子线程写日志路径，Filter 在 emit 时从 threading.local 取到的会是默认值 `-`，req_id 链在该处断裂。

**O-3 codex client / responses 协议的 req_id 覆盖 —— 未核实。**
优化方案称"do_* 入口生成 req_id（含 `_dispatch_control` 路径）"，可覆盖全部入口。但 responses 协议（codex）请求是否同样收敛到 do_* 入口、有无绕过 `_forward_logged` 的分支，本次未逐一核到。需确认无"不产生 req_id 的请求路径"。

**O-4 账本 v2→v3 迁移的并发/进程安全 —— 未核实，且与运行现状耦合。**
优化方案 OPT-10 只说"`_load` 迁移"，未讨论：(a) 迁移时进程在跑、有请求并发写入的互斥；(b) 迁移失败回滚；(c) 旧桶检测的判定依据。结合 1.3 的观察（运行进程跑旧代码、写旧 schema），**若先升级代码再起新进程，旧进程残留/新进程迁移之间存在 schema 版本交错窗口**。这是可操作的真实风险，方案应补"迁移与进程重启的先后顺序 + 并发写互斥"说明。具体迁移代码本次未读，标注未核实。

**O-5 跨请求 session 关联：方案基本够用，但 warn 行仍无 session。**
ACCESS 行已含 `session=`（全 uuid，日志实证可见），同一 session 的多请求**终态**可凭 session 关联，方案"req_id 每请求新生成 + ACCESS 已有 session"的组合对终态序列是够的。但 **warn 行仍只带 req_id 不带 session**——凭 req_id 能串单请求内的事件，却不能直接从 warn 行反查它属于哪个 session（需先 req_id→ACCESS→session 两跳）。是否要在 warn 行也补 session，方案未讨论，属可改进点而非缺陷。

### 可选

**O-6 CLI `grep ' ACCESS '` 兼容性 —— 未核实，且出现新的复杂因素。**
优化方案 §3.7/风险节称"CLI awk 按键名解析、新增字段在尾部向后兼容"。但两点未落实：(a) 当前 `logs`/`stats` 的实际实现行（文档指 cli:350-353、546-555，为 08-07 值）本次未核到现状；(b) 1.1 已揭示 ACCESS **已经**加过三个字段（budget 系列），且**运行进程仍在写旧格式**——日志文件当前是新/旧两种 ACCESS 格式混存。方案"新增字段向后兼容"的断言需在这种**格式过渡期混存**的现实下重新验证 `stats` 的 max_ms awk 段是否会被旧格式行干扰。

**O-7 落地后该派 reviewer 的耦合点（回应任务 reviewer 视角）。**
- OPT-10（账本 schema 迁移 + CLI stats 重写 + record 入账逻辑）：**有正确性耦合**（迁移近似值口径、max_ms 双源切换、attempt 级计数三处需互洽），应派 reviewer。
- OPT-01（Filter 跨两个 FileHandler + do_* 入口 + finally 清除）：req_id 生命周期跨多函数，漏清/漏注入即污染或断链，属耦合，建议 reviewer。
- OPT-02/03/05/09（纯级别调整）：无耦合，runner 即可，不必 reviewer。

---

## 三、优化建议（按优先级）

**S-1（最高，前置）先补一节"基线对齐"再谈落地。** 优化方案应新增一个前置步骤：按当前工作树重核全部行号、把 budget_retry 的 4 warn 点 + 3 ACCESS 字段并入记录点清单与 OPT-04 字段目标集、并重新确认 attempts 语义在 budget 重试下的判别有效性。否则 P0 三项的实施坐标全部失效。**这是把"文档写作时正确"与"落地时可用"接起来的唯一动作。**

**S-2 req_id 选型：Filter+threading.local 方向对，但需补一句失败模式说明。** 原设计否决 req_id 的理由是"改 20 余处调用点"（已核实属实，见 1.2）。Filter 方案恰好消除该成本，选型合理。但方案应显式写出 threading.local 的适用前提（所有 warn 与请求同线程）与失败模式（子线程 emit 时 req_id=`-`），并把 O-2 的核实列为 OPT-01 的实施前置。相较"逐个传参"，Filter 确实更省改动，但"更绕"的代价是隐式依赖线程模型——这一点方案目前只在风险节一句话带过，不够。

**S-3 账本 v3 体积/写入开销：方案未评估，建议补一句量级估算。** attempt 级计数为每 combo 增 `attempts`/`attempt_fail` 两字段，combo 数 = 天桶 × supply×route×strategy。本地单用户量级下增量可忽略，但方案应给出"账本行数 × 每 combo 增 2 字段"的粗算，并确认 record 每请求整文件 dump 的写入开销不因此失控（当前账本 `.claude_model_proxy_totals.json` 已 39KB）。非阻断，补上更稳。

**S-4 P0/P1/P2 分批大体合理，一处建议上调。** 401 未授权审计（`admin.auth_fail`）现列在 P1（OPT-08 内）。它是**安全审计盲区**且实现极简（控制面一处 WARNING），与 OPT-08 其余"root 开 INFO"等较大改动无依赖，建议拆出并上调 P0 或批次一末尾——安全类盲区的修复优先级应高于"事件补齐"类的平均位置。其余分批（P0 硬伤、P1 事件+去刷屏+账本、P2 锦上添花）合理，不调整。

**S-5 OPT-10 旧桶迁移存在第三选项，建议并列呈现。** 方案给了"补近似值（attempt_fail=fail）"与"补 0"两个选项让用户定。**第三选项：不迁移旧桶、新旧口径并存**——旧桶保持无 attempt 字段，`stats` 输出时对缺字段桶显式标注"attempt 口径自 <迁移日> 起"。代价是历史 supply 失败率长期断档，但避免了近似值污染历史数据的真实性。对"严谨不捏造数据"的偏好而言，这个选项值得并列，而非二选一。

---

## 验证方式（本审查结论的可复核点）

1. 漂移与归因：`git show 895f556e:tools/model_proxy/core/server.py | grep -nE 'ACCESS ms=|cooldown\\+failover'` 应命中文档旧行号（955/1301/1317/1330）；`git diff --stat 895f556e -- core/server.py core/translate.py` 应显示 +226/+209。
2. 新增功能：`grep -nE 'budget_retry|budget_truncated|_maybe_budget_retry' core/server.py` 应命中 1179/1190/1473/1521 及 ACCESS 新字段（991-1000）。
3. 静默出口：Read 当前 1430-1437、1446-1448，确认无 log 调用。
4. 设计原文：三处引用按 1.2 表格的行号核原文。
5. 未核实项（O-2/O-3/O-4/O-6）需后续补：线程模型、do_* 入口全覆盖、账本迁移并发、CLI 现状行号。

## 关联

- 审查对象：[[2026-08-08-log-optimization-plan]]、[[2026-08-08-log-audit-target-design]]
- 设计原因出处：[[2026-07-22-access-log-and-latency]]、[[2026-07-23-usage-totals-ledger]]、[[2026-07-23-chat-reasoning-content-fallback]]
- 代码：[[tools/model_proxy/core/server.py]]、[[tools/model_proxy/core/translate.py]]、[[tools/model_proxy/core/commands.py]]、[[tools/model_proxy/model_proxy_cli.sh]]
