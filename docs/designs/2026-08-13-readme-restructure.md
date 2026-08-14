---
created: 2026-08-13 22:30:00
type: design-decision
status: draft
target: "[[tools/model_proxy/README.md]]"
tags: [architect, model_proxy, documentation, refactor]
---

# model_proxy README 重构方案（修正重设计）

路径标记：[理想] — 不计迁移成本，追求 README 自包含上手 + 深入内容外移的目标架构。

## 背景与问题

现状 README 929 行，经 2026-07-23 首次重构后按"认知路径"组织，但随着 route_pool、`$route` 命令、budget_retry、nudge 改写等功能持续叠加，已膨胀回重型文档。

上一版方案推荐精简到 ~230 行（Quick Start 只保留 on/install/logs 三步、`$route` 移到 ARCHITECTURE.md、拆 4 份专题文档）。用户反馈指出三处必须修正：

1. **Quick Start 必须含配置教程**：新人看完能正确配出一份可用的 `model_proxy_config.json`，不能只有 on/install/logs。
2. **`$route` 命令层留 README**：用户实际会用的 in-band 命令，必须随手可查，不能移到 ARCHITECTURE.md。
3. **README 定位变更**：第一次看到项目的人，仅凭 README 就能正确配置和使用起来（不需要先翻其他文档）。

这三条改变了 README 的定位——从"高频入口 + 指针"变为"新人自包含上手 + 深入指针"。

---

## A. README 定位重新审视

### 新定位：新人能仅凭 README 配置 + 使用起来

README 必须自包含以下内容：
- 项目定位（是什么）
- Quick Start（从零配置到验证的完整路径，含配置教程）
- CLI 命令速查（日常 shell 命令）
- `$route` 会话内命令（用户在对话里打的 in-band 命令）
- 配置结构概述（三段式概念 + 最小示例，不展开字段详解）
- 请求处理流程精简版（帮新人理解"请求怎么被处理的"）
- SDK 接入说明（install 命令行为 + 四 SDK 写入目标）
- 已知限制
- 目录结构 + 文档导航

### "自包含上手"与"不冗长"的平衡

矛盾点：配置教程如果展开每个字段语义，README 会回到 900+ 行；如果只给三步命令，新人无法配置。

解法：**配置教程给最小可用路径（复制 example.json + 改 appkey），不展开字段语义。** example.json 本身就是一条完整可用配置（有 supplies/routes/strategies），新人只需改 appkey 占位符。配置三段式概念给关系图 + 每段一句话，字段完整明细指针到 CONFIG.md。

---

## B. Quick Start 重新设计

### 新人配置一份可用 config 的最小步骤

1. 复制 `config/model_proxy_config.example.json` 为 `config/model_proxy_config.json`，改其中的 `<APPKEY_PLACEHOLDER>` 为真实 appkey。
2. example.json 已含一条可用的 strategy（client_token=`cc`），无需额外配置。
3. 启动代理 `model_proxy_cli.sh on`。
4. 接入 Claude Code `model_proxy_cli.sh install`。
5. 重启 Claude Code，`model_proxy_cli.sh logs` 验证。

### Quick Start 新结构

| 步骤 | 做什么 | 预期结果 |
|---|---|---|
| ① 配置 | 复制 example.json → config.json，改 appkey | config.json 600 权限，含真实凭证 |
| ② 启动 | `model_proxy_cli.sh on` | `model_proxy started (pid <PID>), ready` |
| ③ 接入 | `model_proxy_cli.sh install`，选 claude SDK + client_token | 写入 `~/.claude/settings.json` 的 env |
| ④ 验证 | 重启 Claude Code，`model_proxy_cli.sh logs` | 出现 `ACCESS ... status=200` 行 |

### 配置教程在 README 放多深

推荐：**最小示例 + 指针**，不嵌完整配置说明。

- 最小路径（复制 example.json + 改 appkey）只需 5 行说明，够跑通。
- 配置三段式概念在 Quick Start 后的 §3 单独概述（关系图 + 每段一句话）。
- 字段完整语义、protocol 推断规则、route_pool 机制等指针到 CONFIG.md。

行数预估：Quick Start 含配置约 70 行（上一版三步约 40 行，增加配置步骤约 30 行）。

---

## C. `$route` 命令层放 README 的位置

### 放置位置

`$route` 是用户在 Claude Code 对话里打的 in-band 命令，与 CLI 命令（shell 里跑 `model_proxy_cli.sh`）使用场景完全不同。

推荐：放在 CLI 命令速查之后，作为**独立子节**"会话内命令：`$route`"。理由：
- 两者使用场景不同（shell vs 对话），分设比混在一起更清晰。
- `$route` 是新人在 route_pool 配置下排查"为什么消息打到了某个后端"的主要手段，需要随手可查。

### `$route` 在 README 放多详细

推荐：**语法 + 3 个示例 + 生效语义关键点**，不展开匹配规则细节。

保留内容：
- 语法（`$route <id>` / `$route` / `$route reset`）
- 3 个示例及预期回执
- 生效语义关键 3 条：下一条消息起生效、只对 anthropic 协议生效、旧式 route_id 写法不支持 set/reset

不展开（指针到设计文档 `docs/designs/2026-08-04-in-band-route-command-design`）：
- 匹配规则细节（单行 + 首 token 精确匹配 + token 数 ≤ 2）
- 7 天静默清理机制
- sidecar 文件格式
- 扩展性注册表

行数预估：约 30 行。

---

## D. 拆解方案调整

### 上一版拆 4 份 vs 新版评估

上一版拆 CONFIG / ARCHITECTURE / OPERATIONS / REASONING 四份。新定位下 README 自包含上手信息后，重新评估：

| 文档 | 是否拆 | 理由 |
|---|---|---|
| **CONFIG.md** | 拆 | README 给最小配置示例 + 三段式概述，字段完整明细、protocol 推断、route_pool 机制、session override 存储机制需要独立文档承载。 |
| **ARCHITECTURE.md** | 拆 | README 留精简版链路总览图，入站鉴权、协议识别、三阶段匹配完整说明、effort 映射链路、出站转换机制需要独立文档。`$route` 命令留 README。 |
| **OPERATIONS.md** | **不拆** | CLI 速查表留 README 够用；运维深入细节（日志字段完整说明、降级限流、账本 schema、supply test 归因分类、stats 多维过滤）散落在现有设计文档中已有覆盖，用指针引即可，不必再建一份聚合文档增加碎片。 |
| **REASONING.md** | 拆 | effort_enum 语义、STRIP/DISABLED、off_alias 约束、档名词表、映射算法公式与边界条件——纯进阶内容，新人不需要懂。 |

结论：**拆 3 份**（CONFIG.md、ARCHITECTURE.md、REASONING.md），不拆 OPERATIONS.md。

### §4 请求处理流程

README 保留**精简版链路总览图**（5 步框图，每步一行注释 + 指针到 ARCHITECTURE.md 对应子节）。理由：帮新人理解"请求怎么被处理的"，没有这张图新人读完配置后不知道"代理到底干了什么"。完整细节（入站鉴权识别、协议识别、三阶段匹配、effort 映射链路）移 ARCHITECTURE.md。

### §6 reasoning 深入

新人不需要懂。移到 REASONING.md，README 只在"已知限制"和"配置结构概述"各留一句话指针。

---

## E. 新方案

### README 新大纲

| 章节 | 预计行数 | 核心内容 |
|---|---|---|
| `## 1. 这是什么` | ~8 | 定位 + 跨协议举例 + 端口默认值 |
| `## 2. Quick Start` | ~70 | ① 配置（复制 example.json + 改 appkey）② 启动 on ③ 接入 install ④ 验证 logs + 日常挂载提示 |
| `## 3. 配置结构概述` | ~40 | 三段式关系图 + supplies/routes/strategies 各一句话 + 指针到 CONFIG.md |
| `## 4. 请求处理流程` | ~30 | 精简版链路总览图（5 步框图）+ 关键点 + 指针到 ARCHITECTURE.md |
| `## 5. CLI 命令速查` | ~40 | 命令表（status/reload/supply/route/strategy/switch/install/on/off/logs/stats）+ 关键说明 |
| `## 6. 会话内命令：$route` | ~30 | 语法 + 3 示例 + 生效语义关键点 + 指针到设计文档 |
| `## 7. SDK 接入（install）` | ~25 | install 行为 + 四 SDK 写入目标 + base_url 表 + detect 口径 |
| `## 8. 已知限制` | ~30 | 五种协议组合 + failover + thinking 方言 + budget_retry + chat 兜底 + nudge 改写 + codex 未核对 + 不自愈 + 无自动化测试 + effort 探测不准（各一条精简） |
| `## 附录 A：目录结构` | ~15 | 目录树（含 designs/） |
| `## 附录 B：文档导航` | ~15 | 指向 CONFIG.md / ARCHITECTURE.md / REASONING.md / 设计记录索引 |
| **合计** | **~303** | |

### 拆解清单

**CONFIG.md**（新建，约 200 行）：
- 配置文件路径与权限说明
- supplies 字段完整明细表
- routes 字段完整明细表
- strategies 字段完整明细表（含 route_pool、dispatch 预留字段）
- 顶层字段表（admin_token / default_cooldown_seconds / upstream_timeout_seconds / budget_retry）
- protocol 推断规则完整说明
- route_pool 一致性哈希分配机制详解
- session override sidecar 文件机制详解
- 7 天静默清理机制

**ARCHITECTURE.md**（新建，约 150 行）：
- 入站鉴权识别（client_token 提取完整说明）
- 入站协议识别（detect_source 完整说明）
- 三阶段匹配完整说明（含 route_pool 候选列表、route 全挂跨 route 兜底）
- effort 映射链路（一句话链路 + 指针到 REASONING.md）
- 出站转换 / 转发机制（PASSTHROUGH vs 转换、出站 URL 拼接、双发头、failover 冷却）

**REASONING.md**（新建，约 120 行）：
- reasoning_capability 字段语义（effort_enum 四写法行为表、STRIP/DISABLED）
- off_alias 语义与约束
- 档名词表
- source 能力为何挂 strategy、为何人工填
- effort 跨模型映射算法（公式、边界条件、单调性、off 吸收态）
- budget 分档断点说明
- 指针到 `docs/archive/reasoning_relative_remap_redesign.md`

### 迁移清单

| 现状 README 内容 | 去向 | 处理方式 |
|---|---|---|
| §1 这是什么 (L9-L14) | README §1 | 精简保留 |
| §2 Quick Start (L15-L60) | README §2 | 重写（加配置教程步骤） |
| §3 核心概念：三段式配置 (L62-L288) | README §3 概述 + CONFIG.md 完整 | 拆分：关系图+概述留 README，字段明细/protocol 推断/route_pool/session override 移 CONFIG.md |
| §3.4 `route_pool` 详解 (L238-L288) | CONFIG.md | 整段移走 |
| §4 请求处理流程 (L290-L493) | README §4 精简 + ARCHITECTURE.md 完整 | 拆分：精简链路图留 README，入站鉴权/协议识别/三阶段匹配/effort 映射/出站转换移 ARCHITECTURE.md |
| §4.6 `$route` 命令 (L396-L489) | README §6 | 精简后留 README（语法+示例+关键语义），匹配规则/清理/sidecar/扩展性指针到设计文档 |
| §5 运维与控制 (L491-L671) | README §5 CLI 速查 + 散落指针 | 拆分：CLI 命令表留 README，启动/日志/热重载/reasoning debug 精简后散入 README 各处或指针到设计文档；详细说明（status 输出格式、supply test 归因、stats 多维过滤等）指针到现有设计文档 |
| §6 reasoning 深入 (L672-L760) | REASONING.md | 整段移走 |
| §7 接入各 SDK (L762-L803) | README §7 | 精简保留 |
| §8 已知限制 (L805-L854) | README §8 | 精简保留 |
| 附录 A 配置字段速查表 (L856-L902) | CONFIG.md | 整段移走 |
| 附录 B 目录结构 (L904-L929) | README 附录 A | 保留，修正 designs/ 描述 |
| —（无原文） | README 附录 B 文档导航 | 新增 |

---

## 与上一版方案的差异

| 维度 | 上一版方案 | 新方案 | 改动原因 |
|---|---|---|---|
| Quick Start | 三步 on/install/logs | 四步 配置/on/install/logs | 用户反馈：必须含配置教程 |
| `$route` 位置 | 移到 ARCHITECTURE.md | 留 README 独立节 | 用户反馈：用户实际命令必须随手可查 |
| README 定位 | 高频入口 + 指针 | 新人自包含上手 + 深入指针 | 用户反馈：仅凭 README 要能配置+使用 |
| README 行数 | ~230 行 | ~310 行 | 增加配置教程 + $route + 精简链路图 + 三段式概述 |
| 拆解份数 | 4 份（CONFIG/ARCHITECTURE/OPERATIONS/REASONING） | 3 份（CONFIG/ARCHITECTURE/REASONING） | OPERATIONS 不拆：CLI 速查留 README，运维细节散落指针到现有设计文档覆盖 |
| 配置结构概述 | 移到 CONFIG.md | README 留精简版（关系图+一句话）+ 指针 | 新人需要理解三段式才能配置，不能完全外移 |
| 请求处理流程 | 全移 ARCHITECTURE.md | README 留精简链路图 + ARCHITECTURE.md 完整 | 新人需要理解代理干了什么，精简图帮助建立心智模型 |

---

## 风险与权衡

1. **README 行数回升**：从上一版的 ~230 回到 ~310，增加约 80 行。代价是换来了"新人自包含上手"能力。值不值取决于使用场景——用户明确要求 README 要自包含，这是用户定位而非设计妥协。

2. **OPERATIONS.md 不拆的取舍**：运维深入细节（日志字段完整说明、降级限流机制、账本 schema、supply test 归因分类、stats 多维过滤）散落在现有设计文档中。好处是不增加文档碎片；代价是运维人员需要知道去哪份设计文档查。权衡后选择不拆——这些设计文档已有清晰命名（如 `2026-08-08-log-optimization-plan.md`、`2026-07-23-usage-totals-ledger.md`），README 附录 B 文档导航可引。

3. **ASCII 图渲染**：三段式关系图和链路总览图在等宽字体下对齐，Obsidian 阅读视图可能因比例字体错位。用代码块包裹保证等宽。落地后需目视确认。

4. **章节编号交叉引用**：新结构用"§ X"式编号交叉引用。若后续增删章节导致编号漂移，所有引用需同步改。建议落地时可用"章节名"引用代替编号引用（不带 §），降低编号漂移维护成本。

5. **迁移工作量**：等于重写 README 骨架（929 行全量重排 + 3 份新文档创建 + 交叉引用改写）。工作量约半天。建议落地后再派 reviewer 核对"信息未丢失、交叉引用无断链、字段无遗漏"。

---

## 需用户拍板的开放问题

1. **OPERATIONS.md 是否真的不拆？** CLI 命令的详细说明（status 输出格式、supply test 归因分类、stats 多维过滤用法）目前散落在现有设计文档。是否需要集中到一份 OPERATIONS.md？还是散落指针即可？
2. **README §3 配置结构概述和 §4 链路图：是否保留 ASCII 图？** 图在 Obsidian 阅读视图可能错位，但对新人建立心智模型帮助很大。可选：保留图（代码块包裹）或精简为纯文字描述。
3. **专题文档放置位置：** CONFIG.md / ARCHITECTURE.md / REASONING.md 放在 `docs/` 目录下还是 README 同级？（放 docs/ 下与设计记录混在一起但路径统一；放同级可见性更高）
4. **README §5 CLI 命令速查的详细程度：** 上一版 README 只留命令表；现状 README §5.5 有大量交互菜单说明、supply test 归因分类、strategy add/edit 录入说明等。这些是否全部移走只留速查表？还是保留关键说明（如 switch 仅支持单值写法、非 TTY 自动退出）在 README 注释里？

---

## 验证方式

implementer 落地后，按以下清单人工核对（或派 reviewer）：

1. **新人自包含上手验证**：找一个未接触过 model_proxy 的人，仅给 README，能否在 30 分钟内配出可用 config 并跑通 Claude Code 接入。这是核心验收标准。
2. **信息无丢失**：现状 929 行每个内容块都能在新结构（README + CONFIG.md + ARCHITECTURE.md + REASONING.md）找到落点。逐段对照迁移清单勾。
3. **交叉引用无断链**：README 所有"见 § X"/"见 CONFIG.md"/"见设计文档"指针都能找到目标。
4. **`$route` 可查**：README §6 含语法 + 3 示例 + 生效语义关键点，用户不需翻其他文档即可使用。
5. **reasoning 不在 README 展开**：全文搜 "STRIP"/"DISABLED"/"off_alias"/"相对排名映射"，确认深讲只在 REASONING.md，README 处只有一句话指针。
6. **图对齐**：Obsidian 阅读视图打开，§3 关系图、§4 链路图无错行。
7. **与代码一致性抽查**：Quick Start 命令实跑一遍（配置 → on → install → logs）确认输出与描述一致；base_url 表对照 `_install_ops.py` 实际写入值。

---

## 关联

- [[tools/model_proxy/README.md]]（重构目标文件）
- [[2026-07-23-readme-ideal-restructure-execution]]（首次重构方案，已被本方案修正）
- [[2026-07-23-readme-sync-3changes]]（增量同步方案）
- [[2026-08-04-in-band-route-command-design]]（`$route` 命令设计文档）
- `tools/model_proxy/config/model_proxy_config.example.json`（Quick Start 配置教程基准）
