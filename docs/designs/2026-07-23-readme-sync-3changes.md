---
created: 2026-07-23 21:23:29
type: design-decision
date: 2026-07-23
status: draft
target: "[[tools/model_proxy/README.md]]"
tags: [architect, model_proxy, docs, readme]
---

# README 增量同步：3 项已落地改动的编辑指令

路径标记：[务实] — 信息同步任务，非结构重构。产出精确到章节/行号的编辑指令，implementer 逐条照做即可。

## 背景与问题

`tools/model_proxy/README.md`（重构后 643 行，8 正文节 + 2 附录）需增量同步 3 项已落地改动：
1. chat 协议空回答 reasoning_content 兜底填充（转换器出站行为）。
2. 全链路 reasoning_tokens 统计修复与提取收敛。
3. 新增独立累计用量账本 + 重写 stats 命令。

**通读现状 README 后的关键判断（决定了本指令的最终范围，请 implementer 先读这段再动手）：**

- **改动3的 stats 新用法已在 README 里。** 5.5 节 CLI 命令参考（`stats` 速查表 390-392 行 + 详解 417-422 行）现状**已经是新用法**：读独立账本 `.claude_model_proxy_totals.json`、supply/route/strategy 任意维度组合切片、today/month/YYYY-MM-DD/YYYY-MM/全历史、max ms 标注「近日志窗口内，非账本口径」。逐字比对任务给的新用法表，语义全部覆盖。**因此 stats 的 CLI 描述无需重写，只做一处微调（见 E）。** 不要因为任务说过"整个替换"就重写——那是无意义改动。
- **改动2在 README 层面几乎不需要改。** 5.2 节 352-358 行讲 token 用量统计时已声明"转换模式（Anthropic↔Chat/Responses，流式+非流式）...均会提取 usage_in/usage_out/usage_reasoning"——这是修复后的**正确**描述。改动2修复前虽有 4 条链路 usage_reasoning 恒为 0 的 bug，但 README 从未写过"某链路统计为 0/不准"这类缺陷描述（该记录只在 access-log 设计记录的 §6.2）。所以改动2修复后，README 这句名副其实，**无需改动**，也无旧描述可删。是否加"提取收敛到单一 helper"的实现细节：不加，README 5.2 是观测视角，helper 是实现细节，加了破坏 altitude。
- **"已知限制"节无过时项。** 通读第 8 节（554-574 行）：571 行"进程崩溃不会自动重启/自愈，需手动 `on`"是真实现状（3 项改动都没加自愈），**保留不动**；574 行 effort 探测不准也保留；全节没有 reasoning 统计缺陷条目，无需删改。
- **6. reasoning 强度映射（深入）节讲 effort 分类映射，与 token 计数无关，通读无交叉提及，无需改动。**

真正要改的只有 4 处新增/微调：A 第8节加改动1兜底特性、B 5.2 节加账本说明段、C 附录B判断（结论：不改，说明理由）、E 5.5 stats 一处微调。改动2 → 无操作（仅确认）。

## 方案设计

以下每处标注【章节标题】+【定位锚点原文】+【删除/替换/新增什么】。implementer 用锚点原文定位，不依赖行号（行号仅供参考，编辑后会漂移）。

---

### A. 第 8 节「当前状态 / 已知限制」——新增改动1的空回答兜底特性

**定位**：第 8 节 `## 8. 当前状态 / 已知限制` 下的无序列表。找到这一条（现状约 562-563 行）：

```
- thinking/effort 方言自适应：Anthropic 有 `enabled`/`adaptive` 双变体，默认 `adaptive`；识别
  网关对 reasoning 语法的 400 拒绝后切换到对方接受的格式重试并缓存 48 小时。重试只重跑协议内 wire
  语法适配，不重算强度映射结果。Chat/Responses 单变体，无此重试。
```

**操作**：在这条列表项**之后、紧接着**（即在它和下一条 `- reasoning 强度映射：...` 之间）**新增一条列表项**，原文如下：

```
- chat 协议空回答兜底：上游 chat 协议模型（如 kimi-k3）强制思考、无关闭档，当 `max_tokens` 太小导致
  输出预算全耗在 `reasoning_content`（思考过程）、正式回答 `content` 挤不出字时，`ANTHROPIC_TO_CHAT`
  转换（非流式 `openai_to_anthropic_response` / 流式 `OpenAIToAnthropicStreamAdapter`）在 content
  block 组装完成后判定「无任何 text/tool block 且 reasoning_content 非空」，把思考内容整段（加前缀
  `[模型仅返回思考过程，未生成正式回答]`）填入返回的 text block，避免客户端收到空 `content`；
  `stop_reason` 不变（仍反映真实截断原因）。可用 `core/translate.py` 模块级常量
  `_ENABLE_REASONING_FALLBACK`（默认 True）整体关闭。
```

**为何放第8节而非 4.1 链路图 ⑤ 或 4.5**：4.1 是宏观端到端链路图、4.5 只讲 effort 强度映射，塞入这种"某协议某边界响应形态"的转换细节会破坏其 altitude。第 8 节本就是转换特性清单（已列 thinking 方言自适应、错误路径加固、cross-supply failover 等同粒度特性），兜底行为归此处最自洽。**不动 4.1 链路图、不动 4.5。**

---

### B. 5.2 节「日志与观测」——新增「累计用量账本」说明段

**定位**：5.2 节 `### 5.2 日志与观测`，找到该节最后一段（现状 352-358 行，讲 token 用量统计的那段，以"不做 token 成本折算，只统计数量。"结尾）：

```
token 用量统计：转换模式（Anthropic↔Chat/Responses，流式+非流式）、PASSTHROUGH 非流式、
以及 PASSTHROUGH 流式（anthropic→anthropic、responses→responses 的流式请求）均会提取
`usage_in`/`usage_out`/`usage_reasoning` 填入 access 行。PASSTHROUGH 流式采用「转发在前、
旁路嗅探在后」策略（`_write_streaming_response` 转发 chunk 后累积进本地 buffer，按 `\n\n`
切出完整 SSE 事件块，从 anthropic 的 `message_delta` 或 responses 的 `response.completed`
事件里覆盖式提取 usage），不改变、不阻塞原有转发时序，异常整体隔离不影响透传正确性。
不做 token 成本折算，只统计数量。
```

**操作**：在这段**之后**、5.2 节结尾（即 5.3 节标题 `### 5.3 配置热重载` 之前）**新增一个完整段落**，原文如下：

```
累计用量账本：ACCESS 日志会在进程启动时被 `_trim_log` 截断到最后 5000 行，早期行永久丢失，无法
回答「本月/某天累计用了多少 token」这类长期问题。为此另建一个独立账本文件
`.claude_model_proxy_totals.json`（与日志文件同目录），每请求在 `_forward_logged` 收口处同步累加、
原子写盘：按天分桶，桶内以 `supply×route×strategy` 组合键（形如
`supply=<s>|route=<r>|strategy=<t>`，strategy 段是 client_token 明文如 `cc`/`codex`）累加
`requests`/`ok`/`fail`/`usage_in`/`usage_out`/`usage_reasoning`，另存 `total` 全历史汇总。账本
**只增不截**，不受进程重启与日志截断影响。天分桶只保留最近 `KEEP_DAYS=400` 天，超窗旧天桶汇总进
`months_archive` 月归档节点（永久保留）。天/月边界固定按 UTC+8 划分（`timezone(timedelta(hours=8))`，
不依赖系统时区）。账本供 `stats` 命令查询（见「CLI 命令参考」），与 ACCESS 日志完全独立。账本结构
细节见设计记录 `docs/designs/2026-07-23-usage-totals-ledger.md`。
```

**与 5.5 stats 描述的关系**：5.5 是 CLI 命令视角（stats 怎么用），5.2 新增段是数据/观测视角（账本是什么、机制如何）。两者视角不同、不重复，各留一句指针互指即可（本段末已指向设计记录，5.5 无需再改指向）。

---

### C. 附录 B「目录结构」——判断：不新增账本文件条目（保持一致性）

**判断结论：不改附录 B。** 理由：附录 B 现状（620-643 行）**只列纳入结构的源码/配置文件**，并未列任何运行时产物——`.claude_model_proxy.log`（日志）、`/tmp/*.lock`/`*.pid`（锁/PID）这些运行时文件都不在附录 B，而是在 5.1/5.2 正文交代。账本 `.claude_model_proxy_totals.json` 同属运行时产物，按此既定口径**不应单列进附录 B**，否则与 `.log` 不列的现状不自洽。账本文件路径已在 B 段（5.2 新增段）正文点明，足够排障查阅。

**给 implementer 的明确指令：附录 B 不做任何改动。** 若 implementer 倾向列出，须同时补 `.claude_model_proxy.log`（保持一致），但本方案不建议——运行时产物统一留在正文，附录 B 保持"源码/配置结构"单一职责。

---

### D. 改动2（reasoning_tokens 统计修复）——无操作，仅确认

**判断结论：README 无需为改动2做任何编辑。** 复述理由供 implementer 核对而非动手：
- 5.2 节 352-358 行已声明转换模式流式+非流式均提取 `usage_reasoning`——正是改动2修复后的正确态，无需改。
- README 全文（含第 8 节已知限制）从未写过"某链路 usage_reasoning 恒为 0/统计不准"的缺陷描述，故无旧描述可删。
- 提取收敛到单一 helper `_extract_reasoning_tokens` 是实现细节，不进 README 观测视角。

implementer 对改动2**跳过，不编辑任何位置**。

---

### E. 5.5 节 stats 描述——一处可选微调（补多维过滤取交集的例子）

**判断结论：stats 描述基本已准确，仅一处可选微调，非必须。**

**定位**：5.5 节末尾讲 stats 的那条列表项（现状 417-422 行），其中举例这句：

```
（`stats` / `stats today` / `stats month` / `stats 2026-07-23` / `stats 2026-07` /
`stats today supply` / `stats today route=claude supply` 等）。
```

**操作（可选）**：把示例串补上一个"多维过滤取交集"的例子，改为：

```
（`stats` / `stats today` / `stats month` / `stats 2026-07-23` / `stats 2026-07` /
`stats today supply` / `stats today supply=<X>` / `stats today supply=<X> route=<Y>` /
`stats today route=claude supply` 等）。
```

此微调仅为让示例更完整地覆盖"过滤（`字段=值`）"和"多维过滤取交集"两种形态；语义此前已被"投影和/或过滤"这句话涵盖。**若 implementer 认为现状示例已够，可跳过 E。** 其余 stats 文字（速查表 390-392、详解 417-421 主体）**一律不动**。

## 风险与权衡

- **最大风险是过度编辑。** 本任务真实需改动的只有 A（新增1条）+ B（新增1段）两处，C/D 是"确认不改"、E 是可选微调。implementer 切忌把 stats 描述当成"待重写"而重写，也切忌往已知限制节塞"reasoning 已修复"之类的正向条目（已知限制节记缺陷，不记修复）。
- **A 条兜底特性的措辞**含前缀文案 `[模型仅返回思考过程，未生成正式回答]`——与改动1设计记录一致。该前缀是用户可见文案，设计记录里标注"需用户确认措辞"，但当前已落地即用此文案，README 如实记录即可，无需在此为文案再设开关说明。
- **B 段 UTC+8 与 KEEP_DAYS=400 是实现常量**，写进 README 会形成"文档-代码"耦合：若日后改常量需同步改 README。权衡后仍写入——这两个值对"账本能回溯多久""按什么时区分天"是用户排障时的关键信息，值得写；若 implementer 担心耦合，可把"400 天""UTC+8"这两个具体值降级为"最近若干天（`KEEP_DAYS`）""固定 UTC+8"的表述，弱化硬编码。建议保留具体值（个人工具，改常量频率极低）。
- **不涉及结构改动**：3 项改动全部装进现有 8 节 + 2 附录框架（A 进第8节、B 进5.2、改动3已在5.5），无需新增小节。符合任务"不重新设计结构"要求。

## 验证方式

编辑后人工核对以下检查点（无自动化测试覆盖 README）：

1. **A 已加**：第 8 节列表在"thinking/effort 方言自适应"和"reasoning 强度映射"两条之间，多出一条"chat 协议空回答兜底"，含 `_ENABLE_REASONING_FALLBACK` 常量名与前缀文案。
2. **B 已加**：5.2 节末尾（5.3 标题前）多出"累计用量账本"段，含文件名 `.claude_model_proxy_totals.json`、按天分桶、`supply×route×strategy` 组合键、`KEEP_DAYS=400`、`months_archive`、UTC+8、指向设计记录。
3. **C 未改**：附录 B 与编辑前逐字一致。
4. **D 未改**：5.2 节 token 用量统计段、第 8 节已知限制各条与编辑前一致（除 A 新增外）。
5. **E**：若做了，示例串含 `supply=<X> route=<Y>`；若跳过，5.5 stats 文字不变。
6. **未误伤**：6. reasoning 强度映射（深入）节、4.1 链路图、4.5 effort 映射节、Quick Start、附录 A 全部未改动。
7. `grep -n '_ENABLE_REASONING_FALLBACK\|claude_model_proxy_totals\|months_archive\|KEEP_DAYS' tools/model_proxy/README.md` 应命中 A、B 两处新增内容且仅命中它们。

## 关联

- 目标文件：[[tools/model_proxy/README.md]]
- 改动1设计记录：[[2026-07-23-chat-reasoning-content-fallback]]
- 改动2设计记录：[[2026-07-23-usage-reasoning-extraction-unify]]
- 改动3设计记录：[[2026-07-23-usage-totals-ledger]]
- 前序 access 日志设计（含旧 stats）：[[2026-07-22-access-log-and-latency]]
