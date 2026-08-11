---
type: design-decision
status: draft
version: 2
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, in-band-command, inter-session-message, hooks, bidirectional, remote-control, listen]
---

# $message 跨 session 消息互通与远程遥控方案设计（v2）

> [务实] 路径。在 model_proxy 现有命令框架上新增 `$message` 命令族，实现跨 session
> 双向消息传递与远程遥控。核实基准：2026-08-10/11 master 工作区；claude-code 2.1.197
> 二进制（strings 实证）；官方 hooks 文档（code.claude.com/docs/en/hooks）；
> 本地 `~/.claude` 实际文件；隔离实测（`/tmp/ups-hook-test/`）。

## 前缀取舍：`$message` 而非 `$proxy message`

v2 采用 `$message` 前缀，与现有 `$route` 并列，**不进 `$proxy` 命名空间**。

- [[2026-08-09-proxy-command-namespace-design]] 那版"统一进 `$proxy` 命名空间"
  的方向（`$proxy message`、`$proxy route`…）仍是 draft 未落地，v2 显式推翻。
- 理由：`$proxy` 前缀对 message 场景冗余——message 是独立命令族（send/receive/
  listen/poll），与 route 平级，不需要多一层命名空间。`$proxy` 前缀只增加 token
  长度和解析复杂度，无收益。
- 解析层多认一个 `$message` 前缀，无成本（现有 parse 逻辑按首 token 分派即可）。
- 若日后 `$proxy` 命名空间落地，`$message` 与 `$route` 可一并作为平级命令保留，
  不强制归入。

## 1. 背景与问题

**要解决什么**：人在 session B 的机器上跑任务，要离开 B 的机器；离开后仍想持续
和 B 的 agent/模型沟通——发指令、问进度、收回复。

**场景链路**：

1. B 在跑任务，人要离开 B 的机器。
2. 离开前在 B 敲 `$message listen on` → B 进入"可远程联系"态。
3. 人到别处，经 IM 通道操作 session A。
4. 在 A 用 `$message send to-agent <b-id> <内容>` 发指令/问进度。
5. B 的 agent 收到后处理，可用 `$message send to-agent <a-id> <回复>` 反向回 A（**双向**）。
6. 人在 A 经 IM 看到 B 的回复，继续指导下一步。

**本质**：离开 B 后仍能持续和 B 的 agent/模型沟通。

## 2. 识别：proxy 如何知道请求来自哪个 session（已核实，保留 v1）

`core/server.py:658-681` `extract_session_key(body_json)`：请求体 `metadata.user_id`
是 JSON 字符串 `'{"device_id":"...","account_uuid":"","session_id":"<uuid>"}'`，
二次 `json.loads` 取 `session_id`。该函数自 2026-07-28 沙箱实测修正后即为生产口径，
session 哈希路由、`$route` override、ACCESS 日志 `session=` 字段全部依赖它（实证：
ACCESS 日志中 `session=2896beec…` 与 transcript 文件名一致）。

**含义**：
- proxy 在每个 anthropic 请求上天然拿到发送方 session_id → `$message` 的 `from`
  字段零成本可得。
- proxy 顺带可维护一份**进程内"见过的 session"表**（`{session_id: last_seen}`，
  纯内存，属代理自身运行时状态，符合命令层边界），用于投递时的"目标近期是否活跃"
  提示与短 id 前缀匹配。
- 对方 session 的 `transcript_path` 不在 proxy 可见面内，但 hook 侧输入自带
  `session_id`/`transcript_path`（官方文档实证），接收侧不需要 proxy 提供路径。

## 3. 四通道状态覆盖（v2 核心）

B 在不同状态下，消息投递的可用通道不同。四个通道**非冗余，各覆盖一种状态**：

| B 的状态 | 谁发请求 | a(UserPromptSubmit) | b(请求注入) | 轮询 | 用哪个 |
|---|---|---|---|---|---|
| 在跑（tool 循环） | agent 自动 | ✗ 不触发 | ✓ | ✗ REPL 忙 | **b** |
| 空闲、人人在 | 人敲 prompt | ✓ | — | — | a |
| 空闲、人离开 | 没人敲 | ✗ | — | ✓ | **轮询** |
| 关闭 | 无 | ✗ | ✗ | ✗ | SessionStart(重开) |

### 3a. 已验证事实：UserPromptSubmit 只在人提交 prompt 时触发

**实测口径**：2026-08-10/11，隔离目录 `/tmp/ups-hook-test/`，claude 2.1.197，
强制 Bash 工具调用 run 的 stream-json 显示 `num_turns:2`（人 prompt → tool 调用为
第 1 轮、tool_result → 最终回复为第 2 轮），但 UserPromptSubmit hooklog 恰好 1 行、
prompt == 人给的原始指令。

**结论**：UserPromptSubmit hook **只在人提交 prompt 时触发，tool_result 续轮不触发**。
依赖 UserPromptSubmit 覆盖 mid-run 不成立，b 通道不可省。此为已验证事实，不再是
"待实测项"。

### 3b. 通道 a：UserPromptSubmit hook 注入

B 空闲且人在时，人下一次提交 prompt → UserPromptSubmit hook 触发 → 查 B 收件箱 →
有信则注入 context。与 v1 §2b 机制一致（stdout 纯文本通道，transcript 可见可审计）。

### 3c. 通道 b：mid-run 请求注入（listen 开关）

B 正在跑 tool 循环时，proxy 在 B 的 to-agent 消息到达后，**改写 B 的下一个 anthropic
请求**——把消息注入到请求体中转发模型。这是"请求路径注入"，不依赖 hook。

**listen 开关控制**（推荐方案，非默认常开）：只有 B 处于 listen 态时，proxy 才对 B 的
to-agent 消息走 mid-run 请求注入。平时（人在机器旁正常用）不打扰。listen on 开、
listen off 关。

> 注：b 是请求路径注入、PostToolUse hook 是工具后注入，前者更对口径（在请求提交时
> 插入），后者可作未来评估，v2 不展开。

### 3d. 通道轮询：CronCreate `$message poll`

B 空闲但人离开时，没人敲 prompt → UserPromptSubmit 不触发、b 也没有请求可注入。
靠 `$message listen on` 时 agent 自建的 CronCreate 轮询覆盖此状态。

### 3e. 通道 SessionStart：重开投递

B 关闭后，消息留存。B 下次 resume/启动时 SessionStart hook 投递。与 v1 一致。

## 4. `$message listen` 机制

`$message listen on|off` **不由 proxy 拦截**，要让 **B 的 agent** 处理。

### 4.1 agent 侧动作（listen on）

1. 调 `CronCreate` 注册一个每 N 分钟的 `$message poll` 轮询：
   - `durable: true`（防 B 重启丢失）。
   - 间隔 2-5min。
   - 连续 N 次空轮询自动停（避免无限空转）。
2. 通知 proxy 对 B 开 b 注入通道（让 proxy 知道 B 处于 listen 态，可对 B 的 to-agent
   消息走 mid-run 请求注入）。

### 4.2 硬约束：proxy 推不进去

proxy **没法替存活 session 建轮询**：
- 进程内调度器无通道（proxy 是请求转发层，不能向 CC 进程注入定时任务）。
- 写 `.claude/scheduled_tasks.json` 运行中 session 不热加载（CC 启动时读一次，运行中
  不重读）。

因此 listen 必须由 B 的 agent 自建轮询，这是硬约束而非设计选择。

### 4.3 listen off

agent 调 CronRemove 停轮询 + 通知 proxy 关 b 注入通道。

## 5. 轮询机制详解

CronCreate 的 `$message poll` 在 REPL 空闲时触发 → 请求过 proxy → proxy 查 B 收件箱：

- **无信** → proxy 拦截返回合成"无"，**不调模型**（但留 2 行 transcript，无法避免，
  见 §9）。agent 收到空回复，不产生后续动作。
- **有信** → proxy 把 poll 请求**改写**为"你收到来自 A(id=…) 的留言：…，请处理，
  可用 `$message send to-agent <a-id>` 回复"转发模型 → agent 处理。

## 6. 双向回复

注入消息的包装格式带 `from=<发送方短id>` 全称 id，并附"可用
`$message send to-agent <from-id>` 回复"。B 的 agent 处理后回 A，A 经自身通道
（a/轮询/SessionStart）收到。对称使用，无新机制。

## 7. 消息类型（降 mid-run 跑偏风险）

注入包装支持标类型：

- **query 类**：标"简答后继续当前任务"——告诉 B 的 agent 这是个问题，简短回答后
  继续它正在做的事。
- **guidance 类**：标"按此调整"——告诉 B 的 agent 这是指导，按此调整当前行为。

消息类型在 `$message send` 时可选指定，默认 query。

## 8. 两态邮箱（option C）

```
inbox/<session-id>.jsonl          # proxy append；消息入口
agent-seen/<session-id>.jsonl      # b 已投一次、停止重投
delivered/<session-id>.<ts>.jsonl  # hook/轮询落 transcript 后归档
```

- **b 负责及时**：消息到 inbox 后，b 注入一次，inbox → agent-seen（停止 b 重投）。
- **hook/轮询负责留 transcript 痕迹**：hook 或轮询把消息落进 B 的 transcript 后，
  agent-seen → delivered 归档。
- maildir rename 原子认领，不丢不重。各司其职。

## 9. 空轮询 transcript 卫生

痕迹在 `~/.claude/projects/<slug>/<session-id>.jsonl`，每轮询 2 行。

- **运行中不能安全清**：CC 独占追加，外部改损坏 uuid 链 + 运行中不重读。
- **关闭后离线删 poll-pattern 行**：脆弱不推荐（jsonl 行级删除易破坏链条）。
- **最稳的"清"是 `/compact`**：CC 原生压缩摘要掉 clutter。

**策略**：源头限制为主（低频 + 极简 prompt + 连续 N 空停），必要时 `/compact`，
不碰 jsonl。

## 10. 命令族（v2 定稿）

| 命令 | 执行者 | 用途 |
|---|---|---|
| `$message send to-agent <id> <内容>` | proxy 拦截 | 向目标 session 的 agent 投递消息 |
| `$message send to-human <id> <内容>` | proxy 拦截 | 向目标 session 的人投递消息（注入 context 标注） |
| `$message receive` | proxy 拦截 | 人拉取自己的消息，直接返回不经模型（同 `$route` 手感） |
| `$message poll` | proxy 拦截/改写 | 轮询触发用，proxy 查收件箱决定拦截或改写转发 |
| `$message listen on\|off` | agent 执行 | CronCreate + 通知 proxy 开/关 b 注入 |
| `$message` | proxy 拦截 | 裸命令：我发出的投递状态 + 我 inbox 待收 + help |

- 目标 id：支持**短 id 前缀匹配**——在 proxy 进程内"见过的 session"表（§2）中唯一
  命中则补全；零命中按字面存并在回执明示"本进程启动以来未见过该 session"；多命中
  报错列候选。短 id 体验对齐 status active-sessions 的前 8 位惯例。
- from 为发送方 session_id，与目标相同（自环）→ 拒绝。
- send 内容上限 2000 字符，超长回执拒绝并提示。单行约束保留（防误吞）。

## 11. 已否决的替代方案（保留 v1）

### 11a. 直写 sessionB 的 jsonl —— 不可行（否决）

- jsonl 结构已核实：每行一个条目，`uuid`/`parentUuid` 构成链条，类型含
  `user/assistant/queue-operation/attachment/mode/system` 等。
- 运行中的 CC 进程**在内存持有对话状态**，jsonl 只是 append-only 落盘；启动/resume 时
  一次性加载。外部 append 的行不会进入运行中 session 的上下文（架构层面无中途重读
  路径；二进制 strings 未见 transcript 文件 watcher；社区亦有"两进程同写一会话文件
  会损坏"的已知问题）。
- 即便目标 session 未运行，append 也脆弱：sessionB 下次启动后以**它内存中的尾 uuid**
  为 parent 继续写，插入的行 parent 指向旧尾 → 链条分叉；resume 从 leaf 回溯时插入的
  行成为孤儿分支，**不会出现在恢复的历史里**。
- 结论：无论目标运行与否，都不可作为可靠通道。否决。

### 11b. CC 内建 SendMessage/TeammateMailbox —— 不采纳

2.1.197 二进制 strings 实证存在 `SendMessage` 工具与 `TeammateMailbox`（文件收件箱：
`[TeammateMailbox] Wrote message to <agent>'s inbox`、`getInboxPath: agent=… team=…`），
且文案显示 `SendMessage can reach a peer session on another machine via Remote Control`。
但这是 **agent-teams 功能的内部机制**：寻址是 team member name / agentId，teammate 由
主会话 spawn、生命周期归 CC 管；两个独立用户 session 不在同一 team 体系内，无法经
model_proxy 借用。且属 CC 内部 API，版本间变动风险高。**不采纳**；但若用户真实诉求
演化为"agent 间协作"，CC 原生 teammate/SendMessage 是更对口的工具，值得知晓。

## 12. 风险与权衡

| # | 风险 | 说明与缓释 |
|---|---|---|
| 1 | A、B 须共享收件箱 | 同机器最简；跨机器要 proxy 网络可达 + 鉴权，v2 不展开，标 out-of-scope |
| 2 | B 的机器不能睡、CC 进程不能死 | 否则 b 和轮询全停；listen 不改变这个前提 |
| 3 | listen 靠 agent 自建轮询 | proxy 推不进去（§4.2 硬约束），只能 agent 自己 CronCreate |
| 4 | 空轮询留 transcript 痕迹 | 无法避免，靠源头限制 + `/compact`（§9） |
| 5 | 轮询 7 天自动过期 | CronCreate 限制，离开超 7 天要续 |
| 6 | mid-run 注入有跑偏风险 | 靠消息类型标注缓释（§7） |
| 7 | B 重启丢 session-only 轮询 | `durable:true` 缓解，但仍需注意 |

### prompt-injection 面

任何持有 client token 的客户端可向任意 session 投递，内容会被目标 session 的模型读到。
与 `$route` 同级信任假设（能发请求即有 token），但后果从"改自己路由"扩大为"影响他人
上下文"。缓释：包装格式显式标注"非本会话用户指令"；stdout 通道 transcript 可见可审计；
delivered 归档留痕。README 必须写明。

## 13. proxy-message skill

v2 明确需要 `proxy-message` skill（A、B 两边通用，agent 按意图选动作）。

内容至少：
- 命令族（§10）。
- listen 流程（B 侧）：`$message listen on` → CronCreate 轮询 + 通知 proxy 开 b。
- 收信处理（B 侧）：识别注入包装当远程指令，用 from-id 回复。
- 发信（A 侧）：`$message send to-agent <b-id> <内容>`。
- 取回执（A 侧）：`$message` 裸命令看投递状态 + `$message receive` 拉取。
- 卫生：短 id 匹配、消息类型、轮询自动停。

落地走 skill-creator，设计定稿后做。

## 14. 影响面

- `core/commands.py`：parse 函数新增 `$message` 前缀分派；`handle_message_send`、
  `handle_message_receive`、`handle_message_poll`、`handle_message_status`（裸命令）
  handler；help 文案。listen on/off 不在 proxy 侧（agent 执行）。
- `core/server.py`：`CommandContext` 注入"见过的 session"表句柄；`_forward` 在无命令
  时也顺带记录 session last_seen；mid-run 请求注入逻辑（B listen 态时改写请求体）；
  `$message poll` 拦截/改写逻辑。
- 新增 `hooker/deliver_messages.sh`（~40 行 bash/python）——UserPromptSubmit +
  SessionStart hook 脚本。
- `_install_ops.py`：hook 管理泛化到本工具第 2 条 hook（UserPromptSubmit/SessionStart）。
- 新增 `proxy-message` skill（§13）。
- `tests/`：parse `$message` 分派、send handler（写收件箱/短 id 匹配/自环/超长）、
  poll 拦截（无信不调模型/有信改写）、maildir 投递与 rename 不丢消息的并发用例、
  listen 开关对 b 注入的控制。
- README 加 `$message` 小节 + 安全提示。

## 验证方式

沙箱口径复用 2026-08-04 文档：`ANTHROPIC_BASE_URL=http://127.0.0.1:18899/
--setting-sources project,local`。

- **单测**：parse `$message` 分派/回归（缺参、多行内容、句中提及不误命中）；
  send handler 写收件箱、短 id 唯一/多命中/零命中、自环拒绝、超长拒绝；poll 拦截
  （无信返回合成"无"不调模型、有信改写转发）；maildir 并发用例（append 与 hook mv
  交叉，断言不丢不重）；listen on/off 对 b 注入通道的控制。
- **V1 端到端（CLI×CLI，通道 a）**：沙箱起两个 `claude --session-id`，A 发
  `$message send to-agent <B> hi`，断言：A 收回执；B 下一条 prompt 后 UserPromptSubmit
  hook 注入消息（B 的 transcript 可见包装文本）；inbox → agent-seen → delivered 归档。
- **V2 mid-run 注入（通道 b）**：B `$message listen on` → B 跑长任务（tool 循环中）→
  A 发消息 → 断言 B 的下一个请求被 proxy 改写注入 → B 的 agent 收到并处理。
- **V3 轮询（通道 poll）**：B `$message listen on` → B 空闲无人操作 → A 发消息 →
  断言 CronCreate 触发 `$message poll` → proxy 改写转发 → B 的 agent 处理 →
  B 用 `$message send to-agent <A>` 回复 → A 经通道 a/轮询收到。
- **V4 离线投递（SessionStart）**：B 退出后 A 发消息 → B `--resume` → SessionStart
  投递。
- **V5 边界**：目标不存在（回执警示 + 消息留存）；短 id 前缀投递；自环拒绝；超长拒绝。
- **V6 空轮询卫生**：连续 N 次空轮询后自动停；检查 transcript poll-pattern 行数；
  `/compact` 后 poll 痕迹被摘要掉。
- **V7 Claudian 端到端**：临时改 user 层 settings 指向沙箱（2026-08-04 文档记录的唯一
  路径，约 1 分钟全局窗口），Claudian tab 收/发各验一次 hook 触发，立即还原。
- **回归**：`python3 -m unittest discover tests` 全绿；不含命令的消息照常转发（fail-open）。

## 关联

- [[2026-08-09-proxy-command-namespace-design]]（`$proxy` 命名空间方案）——v2 显式
  取代该方向的"统一进 `$proxy` 命名空间"设计，`$message` 与 `$route` 并列为平级命令，
  不进 `$proxy` 命名空间。该文档 status 仍为 draft 未落地，v2 不依赖它。
- [[2026-08-04-in-band-route-command-design]]（命令层骨架、§7.3 边界约束、fail-open
  原则、沙箱验证方法）
- [[2026-08-08-status-active-sessions-design]]（活跃 session 口径、短 id 惯例）
- [[2026-07-22-install-manage-sessionstart-hook]]（hook 条目幂等管理先例）
- [[2026-08-06-session-overrides-single-storage]]（sidecar 存储模式）
