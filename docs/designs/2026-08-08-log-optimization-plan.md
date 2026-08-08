---
type: design-decision
status: confirmed
target: "[[tools/model_proxy/core/server.py]]"
tags: [architect, model_proxy, logging, optimization-plan]
---

# model_proxy 日志体系优化方案（逐问题核实 + 设计原因 + 分批落地）

> **2026-08-08 基线对齐版**：本文写于 08-08 15:32，当晚代码被另一 session 大改（server.py `+226/-23`、translate.py `+209/-3`，commit `0a4f83fd`），引入新功能 **budget_retry**（预算截断反应式放大重试）。独立审查（[[2026-08-08-log-optimization-review]]）发现阻断级问题 O-1：行号整体漂移 + budget_retry 未入清单。本版按**当前工作树代码**重新核实全部行号、把 budget_retry 的 4 个 warn 点 + 3 个 ACCESS 字段并入清单、并逐项评估 budget_retry 对各 P0/P1 项的影响。budget_retry 是既成事实、予以保留，本文不评估其该不该做，只做覆盖处理。

---

## 基线对齐（前置，budget_retry 后重核）

### 0.1 为何重核

优化方案与审查文档（[[2026-08-08-log-audit-target-design]]）写于 08-08 15:18/15:32，行号对**写作当时**的代码（08-07 提交 `895f556e`）逐条准确。但 15:44/15:49 代码大改后，行号系统性漂移（文件顶部 287 未漂，底部漂约 230 行），且出现两份文档均未清点的 budget_retry。照旧行号实施会改错行、漏改新记录点。本节给出当前正确基线。

### 0.2 当前行号对照表（grep + Read 核实，2026-08-08 工作树）

**server.py 记录点**（旧值 = 方案/08-07 引用值 → 当前值）：

| 记录点 | 旧值 | 当前值 | 性质 |
|---|---|---|---|
| `_trim_log` / root basicConfig / access logger | 52 / 65-69 / 75-80 | 52 / 65-69 / 75-80 | 未漂（文件顶部） |
| 账本 corrupt warn（`_load`） | 153 | 153 | 未漂 |
| `UsageTotalsStore.record` | 177-204 | 177-204 | 未漂 |
| reasoning_pref learn warn | 287 | 287 | 未漂 |
| `get_budget_retry`（新函数） | — | **353-363** | budget_retry 新增 |
| `maybe_reload` / `_reload_locked` / reload 失败 warn | 369 / 398-408 / 407 | 369 / 410-420 / **419** | 漂移 |
| `clear_all` | 439-442 | **451** | 漂移 |
| reasoning debug `log.debug`（env 门控） | 848 | **881** | 漂移 |
| `log_message` 屏蔽 | — | **942** | 漂移 |
| `_forward_logged` `_acc` 初始化 | ~947-955 | **974-984** | 漂移 + **新增 budget 三字段** |
| **ACCESS 行**（`access_log.info`） | 954-961 | **991-1000** | 漂移 + **17 字段（含 budget 三新字段）** |
| `usage_totals.record` 调用 / 失败 warn | 963 / 965 | **1002 / 1004** | 漂移 |
| 401 no strategy/route warn | 1047 | **1086** | 漂移 |
| 400 unknown model tier warn | 1056 | **1095** | 漂移 |
| **`budget_truncated` 非流式 warn**（在 `_maybe_budget_retry` 内） | — | **1179-1182** | **budget_retry 新增** |
| **`budget_retry` 放大 warn**（在 `_maybe_budget_retry` 内） | — | **1190-1191** | **budget_retry 新增** |
| route missing tier warn | 1098 | **1204** | 漂移 |
| route_failover trying next warn | 1100-1102 | **1206-1208** | 漂移 |
| all supplies failed/cooling warn | 1120-1121 | **1226** | 漂移 |
| **attempts += 1**（选中 supply 后） | 1126-1127 | **1233** | 漂移 |
| detect_target 失败 warn | 1131 | **1237** | 漂移 |
| 501 UNSUPPORTED（静默） | 1137-1142 | **1243-1248** | 漂移，仍静默 |
| 请求转换失败 warn ×3 | 1202/1220/1241 | **1316 / 1335 / 1363** | 漂移 |
| `_stamp_budget` 调用 ×4 | — | **1305 / 1324 / 1343 / 1370** | **budget_retry 新增** |
| cooldown+failover（HTTP）warn | 1301 | **1424** | 漂移 |
| 不 failover 上游错误（HTTP，静默） | 1307-1314 | **1430-1437** | 漂移，仍静默 |
| cooldown+failover(net) warn | 1317 | **1440** | 漂移 |
| 不 failover 上游错误（net→502，静默） | 1323-1325 | **1446-1448** | 漂移，仍静默 |
| cooldown+failover（成功信号码）warn | 1330 | **1453** | 漂移 |
| **`budget_truncated` 流式 PASSTHROUGH warn** | — | **1473-1475** | **budget_retry 新增** |
| **`budget_truncated` 流式 ANTHROPIC_TO_CHAT warn** | — | **1521-1522** | **budget_retry 新增** |
| 响应转换失败 warn ×3 | 1378/1408/1442 | **1538 / 1574 / 1615** | 漂移 |
| route_failover exhausted warn | 1461-1463 | **1635-1637** | 漂移 |
| 控制面 `_dispatch_control`：503 无 token / 401 未授权 / 404（均静默） | — | **1763-1789**（503 at 1769-1771、**401 at 1773-1775**、404 at 1789） | 漂移，仍静默 |
| `_handle_reload`（手动 reload+清冷却，静默） | 1653-1662 | **1827-1836**（clear_all at 1835） | 漂移，仍静默 |
| 流式中断 warn ×3 | 1808/1862/1911 | **1982 / 2036 / 2085** | 漂移 |
| main print ×2（锁冲突 / listening） | 2005/2031 | **2208 / 2234** | 漂移 |

**budget_retry 的 4 个非流式 `continue` 重进 while 点**：1479-1481（PASSTHROUGH）、1531-1532（ANTHROPIC_TO_CHAT）、1566-1567（ANTHROPIC_TO_RESPONSES）、1606-1607（RESPONSES_TO_ANTHROPIC）。

**计数**：server.py 常设输出点 **32 个**（29 WARNING + 1 ACCESS INFO + 2 print），其中 **4 个 WARNING 为 budget_retry 新增**；若计入 env 门控的 reasoning debug（881）则 33。与审查"28→32"一致（其口径不含该 DEBUG；旧 28 = 25 WARNING + 1 ACCESS + 2 print）。

**translate.py 记录点**（旧值 → 当前值）：降级类 WARNING 13 处（正向 345/357/401/425/429/467/592 + 反向 1695/1712/1745/1749/1899）；缺 usage WARNING 2 处（正向 **623**（logger）、反向 **1197**（`logger_reverse`，旧 545/1119））；死 INFO 2 处（**616 / 638**，旧 538/560，仍死）；DEBUG 3 处（465/1743/1834）。合计 **18 个 log 调用**（14 WARNING + 2 INFO + 3 DEBUG，2 getLogger 定义在 55/56 不计）。**translate.py 无 budget_retry 新增日志调用，仅行号移位**（新增的是 thinking-block 助手纯函数）。

**commands.py 记录点**：sidecar corrupt warn **221**（未漂）；`apply_command` 295（写盘 at 367，成功静默）、`touch` 286（纯内存静默）——仍无事件日志。

### 0.3 ACCESS 行字段目标集（当前已 17 字段）

`ms status source route tier supply failover attempts usage_in usage_out token session route_failover builtin budget_retried budget_truncated stop_reason`（991-1000 实证，日志 17:31 新格式行可见 `budget_retried= budget_truncated=0 stop_reason=tool_use`）。

**budget 三新字段语义**（impl batch4 + 设计 §5a）：`budget_retried`=放大轨迹（形如 `16000→32000[,32000→64000]`，逗号相接无空格，awk 单字段可解析）；`budget_truncated`=0/1（最终仍截断，含流式收口检测）；`stop_reason`=响应停止原因（anthropic 取 stop_reason、responses 取 `status[:reason]`、拿不到留空）。OPT-04 的字段目标集**必须把这三字段并入统一设计**，不是事后补丁。

### 0.4 未核实项的代码核实结论（O-2/O-3/O-4/O-6）

- **O-2 线程模型 → 已核实，threading.local 可行**：`ThreadingHTTPServer`（26 import、2228 实例化），单请求单线程。请求路径**无 `threading.Thread` 派生、无线程池**（仅各 Store 持 `threading.Lock`）。流式写回（`_write_streaming_response`/`_write_translated_stream`/`_sniff_passthrough_usage`）是请求线程内同步内联调用。4 个 budget warn 全在 `_forward` 请求线程内触发。**结论：所有 warn 与请求同线程，Filter+threading.local 适用前提成立；req_id 在 budget_retry 的 `continue` 重进 while 时不换线程、不丢不重。**
- **O-3 codex/responses 入口覆盖 → 已核实，全覆盖**：`do_GET`（949-953）/`do_POST`（955-959）按 `_CONTROL_PATH_PREFIX` 分流到 `_dispatch_control` 或 `_forward_logged`；`do_PUT/DELETE/PATCH`（961-963）恒走 `_forward_logged`。**responses/codex 协议请求与普通请求同走 do_* → `_forward_logged`**（source 协议在 `_forward` 内部检测，不设独立入口）。唯一绕过 `_forward_logged` 的是 `_dispatch_control`（控制面），由 OPT-08 单列。do_* 入口生成 req_id 即覆盖全部转发流量。
- **O-4 账本迁移并发 → 已核实机制，窗口窄但真实**：`UsageTotalsStore._load`（139-167）当前**无版本迁移逻辑**（仅有 corrupt 重置 + 备份），`record`（177-204）持进程内 `threading.Lock` + `_atomic_write_json`（mkstemp+os.replace）。`_load` 仅在 `__init__`（启动时、受理请求前）跑一次，故进程内无迁移并发。**真实风险在进程间交错窗口**：旧进程（跑旧代码写旧 schema）与新进程（启动时迁移）并存时，旧进程收尾的写会覆盖新进程已迁移的文件——单用户工具，重启窗口短，但 OPT-10 须写明"先停旧进程→迁移→起新进程"的顺序与并发互斥口径（实证：当前运行进程已重启跑新代码，日志尾部全新格式）。
- **O-6 CLI 兼容 → 已核实，无问题**：`logs`（349-352）= `grep ' ACCESS ' | tail -N` 整行透传，不解析字段；`stats` 的 max_ms awk（546-554）**按 `^ms=` 键名逐字段匹配**（`for i in 1..NF: if $i ~ /^ms/ split`），非按位置。ACCESS 尾部追加 budget 三字段、新旧格式混存，均不影响 ms 提取；`budget_retried` 值无空格（`,`/`→` 连接），仍是单个 awk 字段。**"新增字段向后兼容"断言在混存期成立。**

### 0.5 budget_retry 影响摘要（一句话）

budget_retry 引入 4 个 warn 点 + 3 个 ACCESS 字段 + 第三类重试（`continue` 重进 while、再次 `attempts+=1`），但**均为增量、不改写既有记录点的语义**：req_id 方案（OPT-01）对其零额外改动自动覆盖；attempts=0 判"全冷却"的不变式不受影响；唯一需实质补述的是 attempts 计数口径（现含 budget 重试派发）、OPT-10 账本对 budget 重试的归类（不记账）、以及 budget warn 的音量治理。逐项见下。

---

## 背景与问题

在 [[2026-08-08-log-audit-target-design]]（下称"审查文档"）基础上做三件事：逐个核实其 38 处记录点的行号与描述、追每个问题的当时设计原因、把它的理想目标形态拆成可分批落地的优化项。本文为纯只读分析，核实以 grep + Read 为准，行号均为本文重新核实后的值。

**核实总述（2026-08-08 基线对齐版）**：审查文档的行号与描述对**写作当时**的代码一致；经 budget_retry 大改后，行号已按当前工作树重核（见 §0.2 对照表）。server.py 常设输出点 32 个（含 budget_retry 新增 4 个 warn）、translate.py 18 个 log 调用、commands.py 1 个。日志实证（`.claude_model_proxy.log` 当前 5044 行窗口）：ACCESS 1894 / WARNING 2609 / DEBUG 189 / **ERROR 0 / INFO 0**；budget_retry 放大 warn 已 127 条、budget_truncated 23 条（刷屏实证）。新发现 2 处审查文档漏列的静默点（见问题 1/3）+ budget_retry 的 2 类设计留白静默路径（见问题 3）。

---

## 问题 1：503/failover 链无法严格还原

### 核实结果

- ACCESS 行在 server.py:991-1000（`_forward_logged` finally），字段与审查文档一致并**新增 budget 三字段**，**确认无 req_id/method/path**。
- 逐 attempt warn 三处：1424（HTTP 状态码 failover）、1440（网络错误 failover）、1453（成功响应但冷却信号码），均只带 `supply/status/err/key_tail4`，**确认无 session/req_id**。
- attempts 计数在 1233：选中 supply 后才 `+1`；冷却跳过发生在 `select_supply` 内部，不可见。实证：`status=503 supply= failover=0 attempts=0 route_failover=1` 与 `attempts=1 failover=1` 的 503 并存——attempts=0 可区分"一个都没发出去"，但无法区分"全冷却"与"route 缺 tier 配置"（1204-1214 与 1629-1641 两个 503 出口都可能是 attempts=0）。
- **审查文档漏列**：两个"不 failover 的上游错误"出口无任何 warn 行——1430-1437（HTTPError 回传上游状态码）与 1446-1448（URLError 且 failover=off → 502）。只有 ACCESS 的 status 字段可见，无事件细节。

### budget_retry 对本问题的影响（新增）

- **attempts 语义扩展**：attempts 现计 **4 类派发**——初次 + reasoning 语法重试（1419 `continue`，同 supply）+ failover 换 supply（1429/1445/1458 `continue`，tried_set.add）+ **budget 重试（1479/1531/1566/1607 `continue`，同 supply，不 cooldown 不进 tried_set）**。即 attempts = 1 + 语法重试 + budget 重试 + failover 换 supply 次数。
- **关键不变式成立**：attempts=0 ⟺ 一个都没派发（全冷却/缺 tier）。budget_retry 必须先选中 supply（attempts 已≥1）才触发，**不影响"全冷却"判定**——OPT-04 的 attempts=0 可区分性设计在 budget_retry 语义下依然成立。
- **budget_retried ↔ attempts 关系**：轨迹中 "→" 个数 = budget 重试次数 = 其对 attempts 的抬升量。无 failover/语法重试时 attempts = 1 + budget 重试次数。故 attempts≥1 不再单纯等同"失败次数"，须结合 budget_retried 轨迹解读（轨迹非空即部分 attempts 是预算放大重派，非失败）。
- **终态 budget 截断的表达**：到上限放弃时如实写回截断响应，status=200 + `budget_truncated=1` + `stop_reason=max_tokens`，**不走进 final_error 的失败出口**——与 final_error 正交，由 budget 三字段独立表达。

### 设计原因

- **无 req_id 是明确的务实取舍**，非遗漏。[[2026-07-22-access-log-and-latency]] 风险节原文："request_id 不引入：本可给每请求生成短 rid 串联 access 与 WARNING 行，但个人工具并发极低，靠时间戳 + token_tail 已能人工对齐两类日志；引入 rid 需改动 20 余处 WARNING 调用点……务实路径下收益 < 改动成本。若日后并发上升再补。"
- "ACCESS 记终态、warn 记逐 attempt 明细"的互补分工同为该文档的明确设计（"现有 WARNING 已逐次记了，两者天然互补，无需为明细再新增日志"）。
- attempts 只在选中后 +1：该文档实施节明写"每进一次循环体 attempts += 1"（在选中 supply 后），属设计如此；"全冷却 vs 缺 tier"的 503 歧义当时未识别。budget_retry 沿用同一 `continue` 重进循环机制（对齐 reasoning 语法重试），故自然被计入 attempts——**无专门设计依据，是机制复用的副产物**。

### 优化方案

| 项 | 内容 | 独立性 | 依赖 | 风险 | 工作量 | 优先级 |
|---|---|---|---|---|---|---|
| OPT-01 | **req_id 全链关联**：`logging.Filter` 从 `threading.local` 读 req_id 注入 record，文件 handler 的 Formatter 加 `%(req_id)s`（默认 `-`）；do_* 入口生成 `uuid4().hex[:8]`（含 `_dispatch_control` 路径），finally 清除；ACCESS 行加 `req_id=` 字段。**不改任何 warn 调用点签名**，translate.py/commands.py 同线程 propagate 自动获益。**budget_retry 覆盖：4 个 budget warn 全在 `_forward` 请求线程内（O-2 已核实），`continue` 不换线程，Filter 自动注入 req_id，零额外改动；落地后需验证单请求 5 级放大链同 req_id 可 grep** | 独立 | 无 | 低：Filter 需覆盖两个 FileHandler；CLI awk 按键名解析，新增字段向后兼容（O-6 已核实） | M | **P0** |
| OPT-04 | **终态可区分 + final_error**：`_acc` 加 `final_error`，各错误出口（503×2、501、502、不 failover 上游错误、401/400）写短 reason；ACCESS 行加 `final_error=`（截断 80 字符，空格转下划线保 k=v 可解析）。**budget_retry 覆盖：① 字段目标集并列保留既有 budget 三字段（budget_retried/budget_truncated/stop_reason），final_error 只在真失败出口写、budget 截断终态（status=200 + budget_truncated=1）不写 final_error，两者正交；② attempts=0 判"全冷却"不变式不受 budget_retry 影响（§问题1 影响节）**。可选扩展：`select_supply` 返回冷却跳过计数 → `cooling_skips=` 字段（+S） | 独立 | 无 | 低：纯增量字段 | S | **P0** |

---

## 问题 2：级别倒挂

### 核实结果

- 全代码 grep 确认 **0 个 `log.error`**；日志文件 5044 行窗口 **ERROR 0 行、INFO 0 行**，双重确认。
- 客户端可见失败全是 WARNING：请求转换失败 ×3（1316/1335/1363）、响应转换失败 ×3（1538/1574/1615）、流式中断 ×3（1982/2036/2085）、detect_target 500（1237）。
- `reasoning_pref learn`（287）正常学习事件用 WARNING，确认倒挂。
- translate.py 两条 INFO（616 empty content fallback、638 content_filter）propagate 到 root WARNING 被吞——**实证死日志**（全文件 0 INFO 行）。translate.py 两个 logger（55-56 行）无 handler，确认 propagate。
- root logger 配置在 65-69（basicConfig WARNING + FileHandler）；access logger 75-80（INFO、同文件、propagate=False）。

### budget_retry 对本问题的影响（新增）

budget_retry 新增 4 个 warn，其级别定级需纳入级别规范统一考量：**budget_retry 放大（1190）与 budget_truncated 截断（1179/1473/1521）语义=可恢复降级/数据有损但流程继续**（截断后放大重试、或如实返回截断响应），归 WARNING 恰当，**不属于**"客户端拿到失败响应"的 ERROR 范畴（截断响应 status=200 如实回客户端，非失败响应）。故 OPT-02 的 WARNING→ERROR 清单**不含** budget_retry 4 点。但 budget_retry 音量治理涉及级别微调（见问题 4 与 §budget_retry 覆盖处理）。

### 设计原因

- WARNING-only 是**明确取舍**：access-log 设计背景节"WARNING-only 作为「错误追踪」对个人工具是合理且够用的"；root 不开 INFO 也是明确的："不改 root logger 级别（避免误收 INFO 噪声）"。该设计做了"错误追踪够用"的判断，但**从未定义级别语义**（什么该 ERROR）——0 ERROR 是"级别语义未建"的结果，不是"有意全压 WARNING"。
- `reasoning_pref` 用 WARNING：**无明确设计依据**。2026-07-19 引入（d3ed64c0），当时 root=WARNING 是唯一配置，想可见只能 WARNING/ERROR，疑似随手提级求可见。
- 两条死 INFO：**设计文档原文即 `logger.info`，实施照抄**——[[2026-07-23-chat-reasoning-content-fallback]]:94 的设计代码片段就是 `logger.info("empty content fallback: ...")`，content_filter 处注释"丢弃 + 记 log（§2.4）"表明有意要记。两级都没察觉 root=WARNING 会吞掉，属**设计+实施双层遗漏**。

### 优化方案

| 项 | 内容 | 独立性 | 依赖 | 风险 | 工作量 | 优先级 |
|---|---|---|---|---|---|---|
| OPT-02 | **客户端可见失败提 ERROR**：1316/1335/1363/1538/1574/1615/1982/2036/2085/1237 共 10 处 WARNING→ERROR。纯级别调整，语义=客户端拿到失败响应或流被截断。**budget_retry 4 warn 不在此列（截断非失败，见上）** | 独立 | 无 | 无 | S | **P0** |
| OPT-03 | **死日志修复**：translate.py 616/638 INFO→WARNING（语义=内容降级，与 467 等处同级）。注意与 OPT-08 关系：root 开 INFO 后它们自然复活，但提 WARNING 仍要做（级别语义正确性），两者独立不冲突 | 独立 | 无 | 无 | S | **P0** |
| OPT-09 | **reasoning_pref 降 INFO**：287 WARNING→INFO | 独立 | **OPT-08**（root 开 INFO），否则变死日志 | 无 | S | P1 |

---

## 问题 3：关键事件静默

### 核实结果

全部确认，行号：config reload 成功无日志（`_reload_locked` 410-420，仅失败 419 有 WARNING）；手动 reload + 清冷却无日志（`_handle_reload` 1827-1836、`clear_all` 451）；501 无 warn（1243-1248，唯一无事件的 5xx 出口，但 ACCESS 有 status=501）；控制面无日志（`_dispatch_control` 1763-1789：503 未配 token at 1769-1771 / **401 未授权 at 1773-1775** / 404 at 1789 均无记录）；进程退出无日志（main `except KeyboardInterrupt: pass`）；sidecar 写无事件日志（commands.py `apply_command` 295-367 写盘成功无记录；`touch` 286 纯内存）；`_config_ops.py` 40 处 print、0 处 logging，全部终端即逝。**新增发现**：问题 1 的两处不 failover 上游错误出口（1430-1437、1446-1448）也属静默，归 OPT-04 覆盖。

### budget_retry 对本问题的影响（新增）

budget_retry 自身**不引入"该记未记"的静默**（命中即 warn：放大 1190、截断 1179/1473/1521），但有 **2 类设计留白静默路径**：
- **流式 ANTHROPIC_TO_RESPONSES / RESPONSES_TO_ANTHROPIC 截断不检测不记**：adapter 未持有 incomplete/正文状态（impl batch4 §9，README 已声明为已知限制）。要补需扩展 adapter 状态，成本单列，**不进 OPT-08 的静默补齐清单**（非"该记未记"，是检测能力未覆盖）。
- **响应解析失败（垃圾输入）→ 不重试不记**：`is_budget_truncated` 返回 False（判不出不重试，安全方向，设计有意）。`enabled=false` 时全链路透传不记（特性关闭，有意）。

这两类应在 README/方案显式标注边界（归 OPT-12 口径明示），不作为静默缺陷修复。

### 设计原因

- **控制面不记 ACCESS 是明确取舍**：access-log 设计 §3"不记 access 日志。它们是低频运维动作、无 supply/tier 语义，记了只是噪声"。但该决策只排除了 ACCESS 这一种形式，**"要不要事件级记录（含 401 审计）"从未被讨论**——401 无审计属遗漏，不是安全考量。
- reload 成功无日志：`ConfigStore` docstring 明写骨架"拷贝 proxy.py"（294-300 行注释），`_reload_locked` 沿袭旧实现，**无明确依据，疑似遗漏**。
- sidecar `touch` 无 IO 无日志：**明确设计**（[[2026-08-04-in-band-route-command-design]] §5.4/V13"热路径无写盘 IO"）。`apply_command` 写盘无事件日志：该设计 §304 以 ACCESS `builtin=route` 字段为记录手段（"ACCESS 日志要记，加可辨识字段"），**有意从简**，但缺 from_route→to_route 细节。
- 501 无 warn、进程退出无日志、`_config_ops` 无持久化：**均无明确设计依据，疑似遗漏**。

### 优化方案

| 项 | 内容 | 独立性 | 依赖 | 风险 | 工作量 | 优先级 |
|---|---|---|---|---|---|---|
| OPT-08a | **401 未授权审计（S-4 上调）**：`_dispatch_control` 的 401 出口（1773-1775）加 `admin.auth_fail` WARNING（不记 token 值）。安全审计盲区，实现极简（控制面一处 WARNING），与 OPT-08 其余"root 开 INFO"等较大改动无依赖，拆出单独做 | 独立 | 无 | 无 | S | **P0（批次一末尾）** |
| OPT-08 | **运维/生命周期事件 + root 开 INFO**：① root WARNING→INFO（已核实 stdlib 无 INFO 噪声：`log_message` 已屏蔽 at 942、urllib 不打 INFO）；② 启动/锁冲突/退出 print 改 `log.info`（port/pid/config_path；nohup 重定向同一文件，CLI start 只 `tail -5` 不解析，无兼容问题）；③ `_reload_locked` 成功 `config.reload.ok`（mtime + supplies/routes/strategies 计数）；④ `_handle_reload` `admin.reload`（cleared_cooldowns=N）；⑤ 控制面：`admin.status` INFO、404 INFO（401 已拆 OPT-08a）；⑥ 501 出口 `request.reject` WARNING（source/target/mode） | 独立 | 无 | 低：点多但每点 1-3 行；root 开 INFO 后 translate 两条死日志自然复活（OPT-03 仍单独做） | M | **P1** |
| OPT-13 | **`_config_ops` 运维审计**：新增 `config_audit.log`（时间/子命令/变更对象 id/是否触发 reload），CLI 层写入 | 独立 | 无 | 低 | M | P2 |
| sidecar 写事件 | `apply_command` 成功后 `log.info("sidecar.write ...")`（token_tail4/session/action/target_route） | 并入 OPT-08 | — | — | （含在 OPT-08 内） | P1 |

---

## 问题 4：冗余刷屏

### 核实结果

- 双发两对确认：1204（route missing tier）+ 1206-1208（route_failover trying next）非末候选时必双发；1226（all supplies failed or cooling）+ 1635-1637（route_failover exhausted trying next）同。
- 配置类 warn 每请求刷确认：678（route_id+route_pool 互斥）、686（route_pool 非法项）在 `extract_route_candidates` 内，每请求必经。
- translate 连刷确认并有实证：缺 usage 两条（623 正向、1197 反向）同一毫秒连刷多条；降级类 13 处 WARNING 无去重无限流。

### budget_retry 对本问题的影响（新增，刷屏实证）

- **实证已有音量**：当前 5044 行窗口 `budget_retry` 放大 warn **127 条**、`budget_truncated` **23 条**，占 WARNING 总量（2609）约 5.7%。单请求爬满 5 级 = 5 条放大 warn（实证轨迹 `16384→32768→65536→131072` 对应 (3/5)(4/5)(5/5)）+ 1 条截断 warn。
- **与 ACCESS 冗余但有意可见**：逐步放大 warn（1190）的完整轨迹已并入 ACCESS 的 `budget_retried` 字段（`16000→32000,32000→64000`），形式上冗余；但逐步 warn 提供长耗时重试的**实时进度**（单轮 ds-flash 可达数百秒，等不到请求末的 ACCESS），且设计 §5a 把 budget_retried 高频定位为**"调用侧预算偏小/模型 thinking 量大"的运营信号，有意要可见**。
- **治理建议（2026-08-08 用户拍板：保持 WARNING）**：**不做 OPT-07 式时间窗限流**（会丢阶梯 progression，且违背设计意图）。budget 放大 warn 保持 WARNING（接受音量作为运营信号，不降 INFO）；截断 warn 同保 WARNING。若未来嫌噪再议。

### 设计原因

- 双发是**增量演进产物**：1204/1226 是 proxy.py 沿袭的存量条件告警；1206/1635 是 [[2026-07-28-session-route-dispatch-design]] 引入跨 route failover（§3 选项B）时新增的动作告警（该设计要求"ACCESS 日志加跨 route failover 标记"）。两处语义不同（条件 vs 动作），但总是同时触发，等于双发——非复制粘贴，也非有意两行。
- 配置 warn 每请求刷：**兜底意图是明确的**（注释："配置文件可能被手工/外部改动绕过写入侧校验，运行时兜底……但要留日志可见性"），但只考虑了"可见"，没考虑"每请求重复"——部分有意、部分遗漏。
- translate 连刷：**无明确依据，疑似遗漏**（从未设计去重/限流）。
- budget_retry 逐步 warn：设计 §5a 有意要轨迹可 grep 作运营信号，**未设计去重**（与 ACCESS 轨迹的冗余是机制副产物）。

### 优化方案

| 项 | 内容 | 独立性 | 依赖 | 风险 | 工作量 | 优先级 |
|---|---|---|---|---|---|---|
| OPT-05 | **双发合并**：非末候选只打 route_failover 动作行（消息内含 missing tier / exhausted 原因，吞并条件行）；末候选只打条件行 | 独立 | 无 | 低：消息文案变化，无机器消费者 | S | P1 |
| OPT-06 | **配置校验挪 reload**：`ConfigStore` 增校验回调，启动 `_reload` 与 `_reload_locked` 成功后各跑一次（route_id+route_pool 互斥、route_pool 非法项各告警一次）；热路径 678/686 删 WARNING（或降 DEBUG 保底） | 独立 | 无 | 中：保持"校验告警 ≠ 拒绝加载"的容错语义；`maybe_reload` 只在 mtime 变时触发，告警频率=启动一次+每次变更一次 | M | P1 |
| OPT-07 | **translate 降级/缺 usage 限流**：module 级限流 helper（key=事件 kind，60s 窗口，首条全量 + 窗口末 `suppressed=N` 汇总一条），挂到 translate.py 全部 14 处 WARNING。**budget_retry 覆盖：budget warn 不挂此限流器（见 §budget_retry 覆盖处理第 6 条），音量治理走"放大降 INFO/截断保 WARNING"的级别路径，另行决策** | 独立 | 无 | 低：汇总条在窗口末才出，进程退出丢 suppressed 计数（可接受） | M | P1 |

---

## 问题 5：stats 越界

### 核实结果

- `UsageTotalsStore.record`（177-204）：每请求 finally 调用一次（1002），只记终态 `_acc`；`ok/fail` 按 `status==200` 分（186-187）。**中间 attempt 确认不入账**——failover 被冷却 supply 的失败不体现在该 supply 的 fail 计数。
- CLI `stats` 的 max_ms 确认从日志窗口 awk 提取（model_proxy_cli.sh:546-554，按 `^ms=` 键名解析），输出自标注"(近日志窗口内，非账本口径)"。
- builtin 入账确认：`_acc["builtin"]="route"`（1662），ACCESS 记 `supply=(builtin)`，账本 combo key 实证存在 `supply=(builtin)|route=...` 等条目。status=0（异常逃逸）按 fail 入账确认（186-187 逻辑必然）。
- 账本 bucket 结构确认无 max_ms（118-125）；`_load`（139-167）当前无版本迁移逻辑（仅 corrupt 重置）。

### budget_retry 对本问题的影响（新增）

- **budget 重试不是 supply 失败**：budget_retry 重试是"同 supply 同请求内重试"，supply 正常回了 200（只是截断），**不属于 failover 的失败范畴**。
- **按 §5a 决策不记账**：设计文档明确"budget 指标不记账、只进 ACCESS 瞬时值"。故 OPT-10 的 `attempt_errors`/`attempt_fail` **只覆盖 3 处 failover continue（1429/1445/1458），不含 budget 重试**；`budget_retried`/`budget_truncated` 维持只进 ACCESS、不入账。
- **attempts 口径分列提示**：ACCESS 的 `attempts` 含 budget 重试派发（§问题1 影响节）；若账本 `attempts` 想与 ACCESS `attempts` 对齐，会含 budget 非失败派发、稀释失败率分母。**建议账本 attempt 级只记 failover 口径（attempt_fail / failover 触发次数），与 ACCESS attempts 语义分列，README 注明**，不与 ACCESS attempts 强行对齐。

### 设计原因

- **max_ms 依赖日志窗口是明确取舍**：[[2026-07-23-usage-totals-ledger]] §1"**max 不存**，由日志补（§3）"，风险节"max ms 与账本口径不一致……输出已显式标注"。当时是知情妥协。
- **中间 attempt 不入账**：账本的设计目标是"token 用量长期累计"（failover 中间 attempt 不产生 usage），record 挂 finally 每请求一次是结构决定；supply 失败率观测**不在该文档的设计目标内，无"有意不记"的依据**——属设计目标未覆盖，审查文档"系统性低估"的判断成立。
- **builtin 入账是明确设计**：in-band 设计 §303"倾向后者：可观测「这功能被用了多少次」，又不污染成本统计"。但未写进 README/账本注释，口径不明示。
- **status=0 入账**：无明确依据，口径未明示。
- **budget 不记账**：[[2026-08-07-reasoning-thinking-truncation-and-protocol-consistency]] §5a 明确"不记账、只进 ACCESS 瞬时值"，且不违背 2026-07-24 "不单独统计 reasoning token"决策——budget 三字段是该边界下的派生观测维度。

### 优化方案

| 项 | 内容 | 独立性 | 依赖 | 风险 | 工作量 | 优先级 |
|---|---|---|---|---|---|---|
| OPT-10 | **账本 schema v3（一次迁移做两件事）**：① bucket 加 `max_ms`（record 时 max 比较），CLI stats 的 max_ms 改从账本取、删日志 awk 段；② combo 加 `attempts`/`attempt_fail`：`_acc` 增 `attempt_errors: list[(supply_id, reason)]`，**3 处 failover continue（1429/1445/1458）前 append**（budget 重试的 4 处 continue 不 append，§问题5 影响节），record 遍历入账到对应 combo。**迁移与重启顺序（O-4 已核实机制）：先停旧进程→迁移→起新进程，避免旧进程写旧 schema 覆盖；`_load` 当前无迁移逻辑需新增 v2→v3 检测**。**旧桶迁移决策（2026-08-08 用户拍板：补 0）**：旧桶 combo 无 attempt 字段，迁移时补 `attempts=0`/`attempt_fail=0`，历史 supply 失败率自迁移日起算（断档但保真，不虚高）。README/账本注释注明口径切换日 | 独立 | 无 | 中：迁移口径需明示 + 进程重启顺序；CLI stats 段落重写；建议补迁移单测 | M-L | **P1** |
| OPT-12 | **口径明示**：status=0 按 fail、builtin 以 `supply=(builtin)` 入账、budget 三字段只进 ACCESS 不入账（§5a）、budget_retry 流式 A2R/R2A 不检测的边界，一并写进 README | 独立 | 无 | 无 | S | P2 |

---

## budget_retry 覆盖处理（专节汇总）

| # | 影响面 | 结论 | 归批 |
|---|---|---|---|
| 1 | **req_id 覆盖（OPT-01）** | 4 个 budget warn 全在 `_forward` 请求线程内，`continue` 不换线程，Filter+threading.local 自动注入 req_id，**零额外改动**；落地后验证单请求 5 级放大链同 req_id | 批次一（OPT-01 内） |
| 2 | **attempts 语义（OPT-04）** | attempts 现计 4 类派发（含 budget 重试同 supply 重派）；**attempts=0 判"全冷却"不变式不受影响**；budget_retried 轨迹 "→" 数 = budget 重试对 attempts 的抬升量；方案补述此口径 | 批次一（OPT-04 内） |
| 3 | **final_error（OPT-04）** | budget 截断终态 status=200 + budget_truncated=1，**不写 final_error**；两者正交，字段目标集并列保留 budget 三字段 | 批次一（OPT-04 内） |
| 4 | **事件归类（事件清单/OPT-16）** | 4 个 budget warn 归两类新事件：**`budget.retry`**（放大，1190）、**`budget.truncated`**（截断，1179/1473/1521）；**不并入** cooldown.set/route.failover（budget 是预算治理、非 supply 健康）。加入审查文档 §3.3 事件清单 | 批次三（OPT-16）；req_id 已在批次一覆盖 |
| 5 | **stats 账本（OPT-10）** | budget 重试非 supply 失败，按 §5a **不记账**；`attempt_errors` 只盖 3 处 failover continue；账本 attempt 级与 ACCESS attempts 语义分列 | 批次二（OPT-10 内） |
| 6 | **去重/限流（OPT-07 外）** | budget warn **不做时间窗限流**（丢阶梯 progression、违背设计意图）；音量治理走级别路径：放大（1190）可降 INFO（依赖 OPT-08）、截断保持 WARNING；列 P1 决策 | 批次二（随 OPT-07 讨论，结论另行落地） |
| 7 | **静默路径** | 流式 A2R/R2A 截断不检测（README 已声明）、解析失败不重试——属设计留白非缺陷，README 标注边界（归 OPT-12），不进 OPT-08 静默补齐 | 批次三（OPT-12 内） |

---

## 分批落地路线图

```
批次一（P0，硬伤修复 + 401 安全审计，合计 ≈ S×4 + M ≈ 0.5-1 人日）
  OPT-01 req_id 全链关联        ← 核心断点修复（自动覆盖 4 个 budget warn，验证 5 级放大链）
  OPT-02 10 处提 ERROR          ← 级别倒挂修复（budget 4 warn 不在此列）
  OPT-03 死日志提 WARNING
  OPT-04 final_error + 503 可区分（+2 处新发现静默出口；字段集并列 budget 三字段、
         attempts 口径补述 budget 重试）
  OPT-08a 401 admin.auth_fail   ← S-4 上调：安全审计盲区，控制面一处 WARNING，独立无依赖
  验证：构造 failover 链，单条 grep req_id 还原全链；构造 budget 截断链（max_tokens 偏小）
       → grep req_id 应见 budget.retry×N + budget.truncated + ACCESS 同 req_id；
       grep -c ERROR ≠ 0；错误 admin token → admin.auth_fail WARNING

批次二（P1，事件补齐 + 去刷屏 + 账本口径，合计 ≈ 2 人日）
  OPT-08 运维/生命周期事件 + root 开 INFO（sidecar 写事件含在内；401 已拆 OPT-08a）
  OPT-09 reasoning_pref 降 INFO（依赖 OPT-08）
  OPT-05 双发合并
  OPT-06 配置校验挪 reload
  OPT-07 translate 限流（budget warn 不挂此限流器）
  budget 音量治理决策落地（放大降 INFO / 截断保 WARNING；依赖 OPT-08 的 root 开 INFO）
  OPT-10 账本 schema v3（max_ms + attempt 级 failover 口径，一次迁移；含重启顺序与三选项决策）
  OPT-11 logs 子命令过滤（依赖 OPT-01/08 的 req_id 与级别，S-M）
  内部顺序：OPT-08 先于 OPT-09/11 与 budget 音量治理；OPT-10 独立随时可做
  验证：缺 usage 上游连发 20 请求 → 日志 ≤ 2 条；stats 的 max_ms 与手工 awk 交叉一致；
       budget 截断链的账本 combo 不含 budget 重试的 attempt_fail

批次三（P2，锦上添花，按需独立选做）
  OPT-12 口径写 README（含 budget 不记账 §5a、流式 A2R/R2A 不检测边界，S）
  OPT-13 _config_ops 审计日志（M）
  OPT-14 RotatingFileHandler 替代 _trim_log（S-M；原设计明确不引入、理由是重启频繁，
         该理由仍成立——只有接受"重启不再丢历史"的价值才做）
  OPT-15 写盘异步化 QueueHandler（M；QPS≪1 无实际收益，仅对齐理想形态，可不做）
  OPT-16 event= 命名规范统一存量 warn 格式（M-L；含 budget.retry/budget.truncated 新事件
         命名落地，依赖 OPT-01/08，无机器消费者、风险低）
```

**依赖汇总**：OPT-09 → OPT-08；OPT-11 → OPT-01 + OPT-08；OPT-16 → OPT-01 + OPT-08；budget 音量治理（降 INFO）→ OPT-08；其余全部独立。OPT-10 与任何项无依赖，但两件事必须同批做（省一次 schema 迁移）。**budget_retry 相关项无新增跨批依赖**——req_id/attempts/final_error 的 budget 覆盖并入既有 P0 项（不新增批次），事件命名归 OPT-16，账本口径归 OPT-10，音量治理归批次二，静默边界归 OPT-12。

## 风险与权衡

- **OPT-01 的 Filter 方案是唯一架构性选择**：用 threading.local + Filter 注入而非改 20+ 调用点签名，是因为原设计否决 req_id 的理由就是"改 20 余处 WARNING 调用点成本"——Filter 方案恰好拆掉这个成本，使当年的务实取舍在 2026 年低成本可逆。**适用前提与失败模式（S-2）**：所有 warn 与请求同线程（O-2 已核实 ThreadingHTTPServer、流式同步内联、无子线程派生，前提成立）；若未来出现子线程写日志路径，Filter 在 emit 时从 threading.local 取到默认值 `-`，req_id 链在该处断裂——落地时补一句注释声明此前提。风险点：`_dispatch_control` 不经 `_forward_logged`，req_id 生成须放 do_* 入口（O-3 已核实 do_* 全覆盖转发流量、responses/codex 同走）；非请求线程的 record 由 Filter 给默认值 `-`。
- **root 开 INFO（OPT-08）推翻的是原设计的明确决策**（"避免误收 INFO 噪声"）。复核后认为原顾虑不成立：进程内 INFO 级调用方只有 translate.py 两条（且本就该可见），stdlib 无噪声。若实施后真有噪声，把对应 logger 单独压回 WARNING 即可，可逆。
- **OPT-10 迁移与重启顺序（O-4）**：单用户工具、重启窗口短，但须写明"先停旧进程→迁移→起新进程"，避免旧进程写旧 schema 覆盖新进程已迁移的文件。**旧桶迁移三选项（S-5）需用户决策**：补近似值（口径虚高）/ 补 0（断档）/ 新旧并存不迁移（断档但保真）。
- **OPT-10 账本体积/写入开销（S-3）**：attempt 级计数为每 combo 增 `attempts`/`attempt_fail` 两字段，combo 数 = 天桶 × supply×route×strategy。本地单用户量级下增量可忽略（当前账本 `.claude_model_proxy_totals.json` 已 43KB，每 combo +2 整数字段对 JSON 体积影响小）；record 每请求整文件 dump 的原子写开销不因 +2 字段而质变。非阻断。
- **OPT-07 限流的 suppressed 汇总**在窗口末才输出，实时排查时首个事件可见、后续被计数吞掉——首条全量已保证"知道发生了什么"，suppressed=N 保证"知道发生了多少次"，语义不丢。**budget warn 不走此限流**（丢阶梯 progression），音量治理走级别路径。
- **budget_retry warn 音量**：实证已占 WARNING 约 5.7%，且随调用侧预算偏小程度伸缩。这是设计 §5a 有意的运营信号，不是缺陷；但若嫌噪，"放大降 INFO、截断保 WARNING"可在批次二一并落地。
- **迁移代价提示**（理想分析、分批落地）：批次一即可解决审查文档的两个硬伤（503 不可还原、级别倒挂）+ 401 安全审计，改动集中在 server.py 日志基础设施层；批次二面最广但每项独立可拆；批次三全可选。budget_retry 的覆盖处理全部并入既有批次，不新增批次。不建议一次全做——P0 落地后先跑一段时间验证 req_id 排查体验（含 budget 截断链的 req_id 串联），再决定 P1/P2 节奏。

## 验证方式

1. 批次一：构造 failover 链（坏 key supply + 好 supply）→ `grep <req_id> .claude_model_proxy.log` 应输出完整链（cooldown.set warn + ACCESS 终态，同一 req_id）；**构造 budget 截断链（max_tokens 偏小的 reasoning 请求）→ 同 req_id 应见 budget.retry×N + budget.truncated + ACCESS（含 budget_retried 轨迹）**；`grep -c ERROR` ≥ 1（原 0）；`status=503` 的 ACCESS 行 `final_error` 区分"all supplies cooling"与"route missing tier"；错误 admin token → `admin.auth_fail` WARNING。
2. 批次二：改 config 触发 mtime reload → 日志出现 `config.reload.ok`；手动 reload → `admin.reload cleared_cooldowns=N`；缺 usage 上游连发 20 请求 → translate 类日志 ≤ 2 条（首条 + suppressed 汇总）；`stats` 的 max_ms 与日志窗口手工 awk 交叉一致（迁移窗口期）；**budget 截断链对应 combo 的 attempt_fail 不含 budget 重试次数**。
3. 回归：tests/ 全绿（含既有 budget_retry 单测）；`logs`/`stats` 命令输出格式向后兼容（新增字段在尾部，awk 按 `^ms=` 键名解析不受影响，O-6 已核实）。
4. 单测补充：Filter req_id 注入（有/无请求上下文 + budget_retry continue 后 req_id 仍在）、账本 v2→v3 迁移（含三选项各路径）、限流 helper 窗口行为。

## 关联

- 现状依据：[[2026-08-08-log-audit-target-design]]（38 处记录点清点与理想目标形态，本文为其落地拆解；行号经本文 §0.2 基线对齐更新）
- 基线对齐依据：[[2026-08-08-log-optimization-review]]（O-1 阻断级 + S-1/S-4/S-5 优化建议，本版已采纳）、[[2026-08-08-budget-retry-batch4]]（budget_retry 实现清单）
- budget_retry 设计依据：[[2026-08-07-reasoning-thinking-truncation-and-protocol-consistency]]（§④b 反应式放大重试、§5a budget 指标不记账只进 ACCESS、流式检测覆盖边界）
- 设计原因出处：[[2026-07-22-access-log-and-latency]]（req_id 不引入/WARNING-only/控制面不记 ACCESS 的原文取舍）、[[2026-07-23-usage-totals-ledger]]（max 不存由日志补/账本只记终态）、[[2026-07-23-chat-reasoning-content-fallback]]（死 INFO 出处）、[[2026-07-28-session-route-dispatch-design]]（route_failover warn 来源）、[[2026-08-04-in-band-route-command-design]]（sidecar 无事件日志/builtin 入账）
- 代码：[[tools/model_proxy/core/server.py]]、[[tools/model_proxy/core/translate.py]]、[[tools/model_proxy/core/commands.py]]、[[tools/model_proxy/model_proxy_cli.sh]]
