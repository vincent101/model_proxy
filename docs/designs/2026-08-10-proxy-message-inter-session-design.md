---
type: design-decision
status: draft
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, in-band-command, inter-session-message, hooks]
---

# $proxy message 跨 session 消息互通可行性与方案设计

> [务实] 路径。任务：评估在 model_proxy 现有 `$proxy` 命令框架上新增
> `$proxy message <session-b-id> <内容>`，让 sessionA 的用户/agent/脚本向 sessionB 投递消息。
> 核实基准：2026-08-10 master 工作区；claude-code 2.1.197 二进制（strings 实证）；
> 官方 hooks 文档（code.claude.com/docs/en/hooks）；本地 `~/.claude` 实际文件。

## 结论速览

| 核心问题 | 结论 |
|---|---|
| 1. 识别 | **可行，已生产验证**：`extract_session_key` 从请求体 `metadata.user_id`（JSON 字符串）取 `session_id`，session 路由/ACCESS 日志均依赖它 |
| 2. 投递通道 | **hooks 是唯一可靠通道**（UserPromptSubmit + SessionStart 注入 context）；直写对方 jsonl 不可行；收件箱+轮询只能做兜底 |
| 3. 触发时机 | **非实时**。空闲 session 最早在其下一次用户输入时看到；已关闭 session 在下次 resume 时看到；无"回合中实时插入"通道 |
| 4. 方案 | maildir 式收件箱（proxy 写）+ hook 脚本（CC 读、注入、归档），命令层注册 `message` handler |
| 5. 主要风险 | prompt-injection 面（注入内容被目标 session 模型读到）；Claudian 侧 hook 触发需一次实测 |

---

## 1. 识别：proxy 如何知道请求来自哪个 session（已核实）

`core/server.py:658-681` `extract_session_key(body_json)`：请求体 `metadata.user_id` 是
JSON 字符串 `'{"device_id":"...","account_uuid":"","session_id":"<uuid>"}'`，二次
`json.loads` 取 `session_id`。该函数自 2026-07-28 沙箱实测修正后即为生产口径，session
哈希路由、`$route` override、ACCESS 日志 `session=` 字段全部依赖它（实证：ACCESS 日志中
`session=2896beec…` 与 transcript 文件名一致）。

**含义**：
- proxy 在每个 anthropic 请求上天然拿到发送方 session_id → `$proxy message` 的 `from` 字段零成本可得。
- proxy 顺带可维护一份**进程内"见过的 session"表**（`{session_id: last_seen}`，纯内存，属代理自身运行时状态，符合 §7.3 命令层边界），用于投递时的"目标近期是否活跃"提示与短 id 前缀匹配。
- 对方 session 的 `transcript_path` 不在 proxy 可见面内，但 hook 侧输入自带 `session_id`/`transcript_path`（官方文档实证），接收侧不需要 proxy 提供路径。

## 2. 投递通道逐一评估

### 2a. 直写 sessionB 的 jsonl —— 不可行（否决）

- jsonl 结构已核实：每行一个条目，`uuid`/`parentUuid` 构成链条，类型含
  `user/assistant/queue-operation/attachment/mode/system` 等。
- 运行中的 CC 进程**在内存持有对话状态**，jsonl 只是 append-only 落盘；启动/resume 时一次性加载。
  外部 append 的行不会进入运行中 session 的上下文（架构层面无中途重读路径；二进制 strings
  未见 transcript 文件 watcher；社区亦有"两进程同写一会话文件会损坏"的已知问题）。
- 即便目标 session 未运行，append 也脆弱：sessionB 下次启动后以**它内存中的尾 uuid** 为
  parent 继续写，我们插入的行 parent 指向旧尾 → 链条分叉；resume 从 leaf 回溯时我们的行成为
  孤儿分支，**不会出现在恢复的历史里**。即"对方只要又跑过一轮，投递即静默丢失"。
- 结论：无论目标运行与否，都不可作为可靠通道。否决。

### 2b. Hooks 注入 —— 可行，官方支持（采纳）

官方文档核实（code.claude.com/docs/en/hooks，claude-code 2.1.197 行为一致）：

| Hook | 输入（stdin JSON） | 注入方式 | 触发时机 |
|---|---|---|---|
| `UserPromptSubmit` | `session_id`、`transcript_path`、`cwd`、`prompt` | ① stdout 纯文本进 context（**transcript 可见**，用户可审计）；② JSON `hookSpecificOutput.additionalContext` 注入为 system-reminder（模型可见、UI 不可见） | sessionB 用户**下一次提交 prompt 时** |
| `SessionStart` | 同上 + `source`（`startup/resume/clear/compact/fork`） | 同上，additionalContext 已文档化 | 会话启动/恢复/清空/压缩 |
| `Stop` | 同上 + `stop_hook_active` | `decision:"block"+reason` 强制继续回合（reason 给模型），**连续 block 上限 8 次** | agent 回合结束 |

要点：
- hooks 是**项目级配置**（`.claude/settings.json`），对本 project 全部 session 生效；hook 脚本按
  输入的 `session_id` 查收件箱，天然实现"只给有信的 session 注入"。
- 已有先例与基础设施：SessionStart 已有 3 条 hook（`ensure_model_proxy.sh` 等），
  `_install_ops.py::ensure_session_hook` 已解决"hook 条目的检测/注入/幂等修复"。
- `Stop` hook 虽然能在"agent 工作中回合结束"时近实时投递，但 `decision:block` 会强制 agent
  继续回合（打扰 + 有 8 次上限），**首版不启用**，仅作可选增强记录。
- **待实测项**：Claudian（Agent SDK，`settingSources: [user,project,local]`）会话是否触发
  UserPromptSubmit/SessionStart hook。SDK 会加载项目 settings，高置信可用，但必须做一次端到端
  实测（与 2026-08-04 文档 V1b 同类验证）。

### 2c. 文件收件箱 + 轮询/skill —— 可行但只是兜底

CLAUDE.md 约定"每轮先查收件箱"或用户手动触发 skill 读取，纯拉模式。依赖 agent 自觉、无
强制时机，不可靠。不采纳为通道，但收件箱文件格式与它天然兼容（人/脚本可直接读）。

### 2d. CC 内建 SendMessage/TeammateMailbox —— 调研到的其他机制，不采纳

2.1.197 二进制 strings 实证存在 `SendMessage` 工具与 `TeammateMailbox`（文件收件箱：
`[TeammateMailbox] Wrote message to <agent>'s inbox`、`getInboxPath: agent=… team=…`），
且文案显示 `SendMessage can reach a peer session on another machine via Remote Control`。
但这是 **agent-teams 功能的内部机制**：寻址是 team member name / agentId，teammate 由主会话
spawn、生命周期归 CC 管；两个独立用户 session 不在同一 team 体系内，无法经 model_proxy 借用。
且属 CC 内部 API，版本间变动风险高。**不采纳**；但若用户真实诉求演化为"agent 间协作"，
CC 原生 teammate/SendMessage 是更对口的工具，值得知晓。

## 3. 触发时机（诚实结论）

不存在"向对方运行中回合实时插入"的通道。消息可见的最早时机：
- sessionB 空闲等输入 → **其用户下一次提交 prompt 时**（UserPromptSubmit）。
- sessionB 已关闭 → **下次 resume 时**（SessionStart，消息留存不丢）。
- sessionB agent 正在跑长任务 → 首版同样要等回合结束后的下一次用户输入；
  若启用可选 Stop hook，可在回合边界近实时投递（代价见 §2b）。

## 4. 推荐方案

### 4.1 命令语法与解析（commands.py）

```
$proxy message <session-b-id> <消息内容>
```

- 现有 parse 规则"单行 + 首 token `$proxy` + token ≤ 3"需为 message **豁免 token 上限**：
  `tokens[1]=="message"` 时，`tokens[2]` 为目标 id，`tokens[3:]` 以空格 join 为内容
  （内容允许空格）。**单行约束保留**（防误吞的安全属性不放宽），超长内容（建议上限 2000
  字符）回执拒绝并提示。
- 目标 id：支持**短 id 前缀匹配**——在 proxy 进程内"见过的 session"表（§1）中唯一命中则
  补全；零命中按字面存并在回执明示"本进程启动以来未见过该 session"；多命中报错列候选。
  短 id 体验对齐 status active-sessions 的前 8 位惯例。
- from 为发送方 session_id，与目标相同（自环）→ 拒绝。

### 4.2 存储：maildir 式收件箱（proxy 独占写，符合 §7.3 边界）

```
tools/model_proxy/config/messages/
  inbox/<session-id>.jsonl       # proxy append；一行一条：
                                 # {"id","from","ts","content"}
  delivered/<session-id>.<ts>.jsonl   # hook 投递后 rename 归档（可审计）
```

- proxy 侧 append（O_APPEND，单行写入，POSIX 下对小写入原子），无需锁。
- hook 侧投递 = **读出内容 → stdout 输出 → `mv inbox/<id>.jsonl delivered/<id>.<ts>.jsonl`**。
  rename 原子；proxy 在 mv 之后的新 append 会重建 inbox 文件、下轮再投，**不丢消息**（经典
  maildir 语义，proxy 与 hook 无锁并发安全）。
- 文件留在 proxy 自己的 `config/` 下 → 不违反命令层"只操作代理自身状态"的边界；
  读该文件的是 hook 脚本（shell），与 proxy 进程无关。

### 4.3 接收侧：hook 脚本（hooker/deliver_messages.sh）

- 读 stdin JSON → `session_id` → 查 `config/messages/inbox/<session_id>.jsonl`。
- 有信：stdout 输出包装后的消息（**用 stdout 纯文本通道，transcript 可见、用户可审计**），
  然后 mv 归档；无信：静默 exit 0（一次文件存在性检查，ms 级开销）。
- 包装格式（注入目标 session 的 context，同时声明来源、降低误执行）：
  ```
  [proxy-message from=<发送方短id> at=<ts> — 来自另一会话的留言，非本会话用户指令]
  <内容>
  [/proxy-message]
  ```
- settings.json 注册两处（走 `_install_ops.py::ensure_session_hook` 同款幂等注入，泛化
  为可管多条本工具 hook）：
  - `UserPromptSubmit`（无 matcher）
  - `SessionStart`（matcher `startup|resume|clear`；`compact` 也建议带上——压缩后重建上下文时投递）

### 4.4 回执（sessionA 立即可见）

```
已投递 → session a1b2c3d4（全称 a1b2c3d4-…）收件箱（第 2 条未读）。
对方将在下次提交消息/恢复会话时收到。该 session 最近活动：12 分钟前。
```
（未见过的目标：`注意：本进程启动以来未见过该 session，消息将留存至其下次出现。`）

### 4.5 边界情况

| 情况 | 行为 |
|---|---|
| 目标 session 不存在/拼错 | 不硬拒（可能是新 session）；按字面存 + 回执警示；短 id 多命中时报错列候选 |
| 目标已关闭 | 消息留存，resume 时 SessionStart 投递 ✓ |
| 并发消息 | jsonl 排队，hook 一次全投；maildir rename 保证不丢不重 |
| 目标从未启用 hooks 的客户端（codex 等） | 消息留存不投；README 注明接收侧仅 CC/Claudian |
| proxy 重启 | "见过的 session"表清空，短 id 匹配与活跃度提示降级为"未见过"，消息文件不受影响 |

### 4.6 影响面

- `core/commands.py`：parse 函数 message 分支（豁免 token 上限）、`handle_message_command`
  （~80 行）、help 文案。
- `core/server.py`：`CommandContext` 注入"见过的 session"表句柄；`_forward` 在无命令时也
  顺带记录 session last_seen（一行字典写）。
- 新增 `hooker/deliver_messages.sh`（~40 行 bash/python）。
- `_install_ops.py`：hook 管理泛化到本工具第 2 条 hook（UserPromptSubmit/SessionStart）。
- `tests/`：parse 豁免规则、handler（写收件箱/短 id 匹配/自环/超长）、maildir 投递与
  rename 不丢消息的并发用例。
- README §4.6 加 message 小节 + 安全提示。
- **依赖项**：当前工作树命令层仍是 `$route` 单命令形态（`CMD_PREFIX="$route"`，
  commands.py:40），`$proxy` 命名空间（[[2026-08-09-proxy-command-namespace-design]]）
  尚未在本树落地。本方案按 `$proxy message` 设计；若落地时命名空间仍未就绪，
  需与其实现同批进行（或先落命名空间）。实施前确认该依赖状态。

## 5. 风险与权衡

1. **prompt-injection 面（最重要）**：任何持有 client token 的客户端可向任意 session 投递，
   内容会被目标 session 的模型读到。与 `$proxy route` 同级信任假设（能发请求即有 token），
   但后果从"改自己路由"扩大为"影响他人上下文"。缓释：包装格式显式标注"非本会话用户指令"；
   stdout 通道 transcript 可见可审计；delivered 归档留痕。README 必须写明。
2. **非实时**：无法插入对方进行中的回合（§3）。这是架构硬约束，不是实现缺陷；若日后需要
   近实时，再评估 Stop hook 增强（有强制继续回合的打扰代价 + 8 次 block 上限）。
3. **CC 版本行为差异**：hook stdin/JSON 输出格式在 2.1.x 文档化且与 2.1.197 二进制一致；
   `additionalContext` 必须嵌在 `hookSpecificOutput` 内（顶层会被静默忽略——文档明示的坑）。
   CC 升级属持续性外部依赖，与 `$` 前缀可达性同类，README 标注。
4. **Claudian 侧未端到端验证**：SDK 加载项目 hooks 高置信，但需一次实测（见验证方式 V4）。
   若不触发，Claudian 会话只能收、发两侧都降级为 CLI 可用。
5. **jsonl 直写被否决**，故无写入冲突风险；收件箱 maildir 语义无锁安全。
6. **消息持久化增长**：delivered 归档只增不减，量级极小（同 totals 账本口径），暂不做清理，
   README 注明可手删。

## 验证方式

沙箱口径复用 2026-08-04 文档：`ANTHROPIC_BASE_URL=http://127.0.0.1:18899/ --setting-sources project,local`。

- **单测**：parse message 豁免/回归（`$proxy message` 缺参、多行内容、句中提及不误命中）；
  handler 写收件箱、短 id 唯一/多命中/零命中、自环拒绝、超长拒绝；maildir 并发用例
  （append 与 hook mv 交叉，断言不丢不重）。
- **V1 端到端（CLI×CLI）**：沙箱起两个 `claude --session-id`，A 发 `$proxy message <B> hi`，
  断言：A 收回执；B 下一条 prompt 后 hook 注入消息（B 的 transcript 可见包装文本）；
  inbox 清空、delivered 归档生成。
- **V2 离线投递**：B 退出后 A 发消息 → B `--resume` → SessionStart 投递。
- **V3 边界**：目标不存在（回执警示 + 消息留存）；短 id 前缀投递。
- **V4 Claudian 端到端**：临时改 user 层 settings 指向沙箱（2026-08-04 文档记录的唯一路径，
  约 1 分钟全局窗口），Claudian tab 收/发各验一次 hook 触发，立即还原。
- **回归**：`python3 -m unittest discover tests` 全绿；不含命令的消息照常转发（fail-open）。

## 关联

- [[2026-08-09-proxy-command-namespace-design]]（`$proxy` 命名空间，本方案注册点与依赖项）
- [[2026-08-04-in-band-route-command-design]]（命令层骨架、§7.3 边界约束、fail-open 原则、沙箱验证方法）
- [[2026-08-08-status-active-sessions-design]]（活跃 session 口径、短 id 惯例）
- [[2026-07-22-install-manage-sessionstart-hook]]（hook 条目幂等管理先例）
- [[2026-08-06-session-overrides-single-storage]]（sidecar 存储模式）
