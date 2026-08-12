---
type: design-decision
status: shelved
version: 3.1
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, in-band-command, inter-session-message, bidirectional, remote-control, listen, response-splice, superseded-by-cc-native]
---

# `$message` 跨 session 消息互通与远程遥控方案设计（v3.1）

> [务实] 路径。在 model_proxy 现有命令框架上新增 `$message` 命令族，实现跨 session
> 双向消息传递与远程遥控，所有投递经响应通道在 SDK 页面可见。核实基准：2026-08-11
> master 工作区；core/server.py 代码实证；claude 2.1.197；隔离实测（/tmp/ups-hook-test、
> /tmp/splice-test、/tmp/splice-test2）。
>
> v3.1 相对 v3：落回架构审核意见——listen 简化为滑动时间窗（无手动 off）、条件触发
> 前置统一判断 + 分层落地、splice 多场景实测结论写入、请求注入抽独立函数、ACCESS
> 日志 message 事件、splice 失败降级、b 通道先认领后注入、resume/fork 边角补全。

## 1. 背景与问题

**要解决什么**：人在 session B 的机器上跑任务，要离开 B 的机器；离开后仍想持续和 B
的 agent/模型沟通——发指令、问进度、收回复。

**场景链路（主场景：远程遥控 session-b 的 agent）**：

1. B 在跑任务，人要离开 B 的机器。
2. 离开前在 B 敲 `$message listen` → B 进入"可远程联系"态。
3. 人到别处，经 IM 通道操作 session A。
4. 在 A 用 `$message send to-agent <b-id> <内容>` 发指令/问进度。
5. B 的 agent 收到后处理，用 `$message send to-agent <a-id> <回复>` 反向回 A。
6. A 也 listen，回复经 A 的 check/IM 推到人。**双向、双端 listen。**

**本质**：离开 B 后仍能持续和 B 的 agent/模型沟通，且所有投递在两端 SDK 页面可见。

## 2. 前缀：`$message`，与 `$route` 并列

v3 采用 `$message` 前缀，与现有 `$route` 并列，**不进 `$proxy` 命名空间**。

- [[2026-08-09-proxy-command-namespace-design]] 那版"统一进 `$proxy` 命名空间"
  （`$proxy message`、`$proxy route`…）仍是 draft 未落地，v3 显式推翻。
- 理由：`$proxy` 前缀对 message 场景冗余——message 是独立命令族（send/check/
  listen），与 route 平级，不需要多一层命名空间。`$proxy` 前缀只增加 token 长度
  和解析复杂度，无收益。
- 解析层多认一个 `$message` 前缀，无成本。

## 3. session 识别（发送方 id 零成本）

`core/server.py:658` `extract_session_key(body_json)`：请求体 `metadata.user_id`
是 JSON 字符串 `'{"device_id":"...","account_uuid":"","session_id":"<uuid>"}'`，
二次 `json.loads` 取 `session_id`。该函数自 2026-07-28 沙箱实测修正后即为生产口径，
session 哈希路由、`$route` override、ACCESS 日志 `session=` 字段全部依赖它。

**含义**：
- proxy 在每个 anthropic 请求上天然拿到发送方 session_id → `$message` 的 `from`
  字段零成本可得。
- proxy 顺带可维护一份**进程内"见过的 session"表**（`{session_id: last_seen}`，
  纯内存，属代理自身运行时状态），用于短 id 前缀匹配。
- 对方 session 的 `transcript_path` 不在 proxy 可见面内，但 hook 侧输入自带
  `session_id`/`transcript_path`，接收侧不需要 proxy 提供路径。
- 支持短 id 前缀匹配：在"见过的 session"表中唯一命中则补全；零命中按字面存并在
  回执明示"本进程启动以来未见过该 session"；多命中报错列候选。体验对齐 status
  active-sessions 的前 8 位惯例。

## 4. 四通道状态覆盖

B 在不同状态下，消息投递的可用通道不同。四个通道**非冗余，各覆盖一种状态**：

| B 的状态 | 谁发请求 | a(UserPromptSubmit) | b(请求注入+响应splice) | 轮询 | 用哪个 |
|---|---|---|---|---|---|
| 在跑(tool循环) | agent 自动 | ✗不触发 | ✓ | ✗REPL忙 | **b** |
| 空闲、人人在 | 人敲 prompt | ✓ | — | — | a |
| 空闲、人离开 | 没人敲 | ✗ | — | ✓ | **轮询** |
| 关闭 | 无 | ✗ | ✗ | ✗ | SessionStart(重开) |

### 4a. 通道 a：UserPromptSubmit hook 注入

B 空闲且人在时，人下一次提交 prompt → UserPromptSubmit hook 触发 → 查 B 收件箱 →
有信则注入 context。

### 4b. 通道 b：mid-run 请求注入 + 响应 splice 回执

B 正在跑 tool 循环时，proxy 在 B 的 to-agent 消息到达后，**改写 B 的下一个 anthropic
请求**——把消息注入到请求体中转发模型。同时把回执 **splice 进响应流末尾**，使 SDK
页面可见（见 §6）。

**触发条件**：B 处于 listen 态 + 有 to-agent 待投 + B 发任意 API 请求（tool 循环）。
listen 开关控制，非常开。

### 4c. 通道轮询：CronCreate `$message check`

B 空闲但人离开时，没人敲 prompt → UserPromptSubmit 不触发、b 也没有请求可注入。
靠 `$message listen` 时 agent 自建的 CronCreate 轮询覆盖此状态。

### 4d. 通道 SessionStart：重开投递

B 关闭后，消息留存。B 下次 resume/启动时 SessionStart hook 投递。

## 5. listen 机制（v3.1：滑动时间窗，无手动 off）

命令只留 `$message listen`（= listen on），**去掉 `listen off`**——无手动关闭场景，
off 全自动。listen 状态信号 + 自动关闭用**滑动时间窗**：

- `$message listen` → proxy 拦截记 `B.listen_until = now + 窗口`（窗口时长 ≥ 60min，
  对齐 cron 自动停周期）；同时 agent 侧执行 CronCreate（双重动作）。
- 每次 `$message check` 轮询 → proxy 刷新 `B.listen_until = now + 窗口`。
- b 注入只在 `now < B.listen_until` 时触发。
- cron 自动停（连续 6 空 = 60min 无轮询）→ 不再刷新 → 窗口超时 → b 自动关。
  **按时间计算，无需显式 off 信号。**

listen 命令由 proxy 拦截记录 `listen_until` + agent 侧执行 CronCreate——这解决原
H1"proxy 不知道 listen 态"：proxy 在拦截 listen 时即写入窗口，b 注入判断纯看
`listen_until`，不依赖任何额外 off 信号。

### 5.1 双重动作

`$message listen` 是双重动作：
1. **proxy 拦截**：记 `B.listen_until = now + 窗口`，合成确认响应返回（不转发上游）。
2. **agent 侧**（经 skill）：调 `CronCreate` 注册每 10min 的 `$message check` 轮询
   （`durable: true` 防 B 重启丢失；连续 6 次空轮询自动停 = 60min 无活动）。

### 5.2 硬约束：proxy 推不进去

proxy **没法替存活 session 建轮询**：
- 进程内调度器无通道（proxy 是请求转发层，不能向 CC 进程注入定时任务）。
- 写 `.claude/scheduled_tasks.json` 运行中 session 不热加载（CC 启动时读一次，
  运行中不重读）。

因此 listen 必须由 B 的 agent 自建轮询，这是**硬约束而非设计选择**。

### 5.3 自动关闭（无手动 off）

listen 无手动关闭路径：cron 6 空自动停 → 不再 check → `listen_until` 不再刷新 →
窗口超时 → b 注入自动失效。proxy 侧无需监听任何 off 信号，纯时间计算。

## 6. 投递机制（v3 核心：响应通道回执）

`$message check`（手动或 cron）→ 请求过 proxy → proxy 查 B 收件箱：

| 收件箱状态 | proxy 动作 | 经模型？ | 可见性 |
|---|---|---|---|
| **无信** | 合成"无"完整响应返回 | ✗ | ✓ 合成响应（复用 `$route` 机制） |
| **to-human** | 合成完整响应、内容即消息原文返回 | ✗ | ✓ 同上 |
| **to-agent** | 消息注入请求转发模型 + 回执 splice 进响应流末尾 | ✓ | ✓ splice 回执 |
| **b(mid-run)** | 同 to-agent：注入请求 + splice 回执进响应 | ✓ | ✓ splice 回执 |

### 6.1 synthetic 完整响应（无信 / to-human）

复用 `$route` 的 `_write_builtin_stream_response`（`core/server.py:1914`），手工合成
完整 anthropic SSE 事件序列：
`message_start → ping → content_block_start_text(0) → content_block_delta_text(0, receipt)
→ content_block_stop(0) → message_delta → message_stop`。

CC 客户端正常解析、记录进 transcript、显示在 SDK 页面——已验证。无需新机制。

### 6.2 splice 回执进响应流（to-agent / b 通道）

to-agent 消息注入请求转发模型后，proxy 在响应流**末尾追加一个 text content block**
（`index = 上游已用最大 index + 1`，在 `message_delta` 事件前注入），内容形如
`[收到 A 留言: <内容摘要>]`，使 SDK 页面可见。

**实测已验证可行**（见 §9.2）：splice 组（tool_use block + 末尾 text 回执 block）
transcript 记录回执、stream-json 可见、续轮请求原样保留 splice text+tool_use、
num_turns:2 success；对照组唯一差异是无回执。

**条件触发（v3.1 前置统一判断）**：在 `server.py:1660` 附近前置统一判断一次
（设 `self._splice_receipt`），各分支用各自机制注入。按难度分层落地：

| 层 | 分支 | 机制 | 难度 |
|---|---|---|---|
| 先行 | 3b/4b 转换非流式 | dict `content.append(text block)` | 低 |
| 次之 | 3a/4a 转换流式（ANTHROPIC_TO_CHAT/RESPONSES） | adapter 已事件级，finalize 前 `message_delta` 之前注入 text block（index=`cur_index+1`） | 低 |
| 再次 | 1b PASSTHROUGH 非流式 | json.loads 后 content append 再 dumps | 低 |
| 最后 | 1a PASSTHROUGH 流式 | 字节透传升级为事件级，复用 `_parse_anthropic_sse_block`（`server.py:2363+`），message_delta 前注入 | 高 |
| 不做 | 5a/5b RESPONSES_TO_ANTHROPIC | responses 格式，B 的 CC 走 anthropic 不涉及 | 首版不支持，文档标注 |

关键更正：转换分支（3a/4a）本就事件级，splice 不需要"字节透传升级"，只有 1a 是
硬骨头。`OpenAIToAnthropicStreamAdapter`（`core/translate.py:716+`）的事件构造 helper
（`_content_block_start_text` / `_content_block_delta_text` / `_content_block_stop` /
`_message_delta_event` / `_message_stop_event`）可复用。实现量中等，纯标准库，
不引入新依赖。

**失败降级（O3）**：splice 注入时上游断 / 写入一半 CC 断 → 放弃回执即可（消息已注入
请求转发、模型已看到，回执是锦上添花），不重投。

## 7. 请求注入约束（v3.1：抽独立函数）

请求注入抽独立函数 `_maybe_inject_message(body_json, session_key, listen_state)`，
在命令拦截后、route 选择前调用。

**与 `_rewrite_known_injected_texts` 的关系**：两者都是条件触发，但**不合并**——
- 条件类型不同：`_rewrite_known_injected_texts` 是内容精确匹配（命中已知注入文本），
  `_maybe_inject_message` 是状态判断（listen 态 + 有待投消息）。
- 操作不同：前者原地改写既有 text block（`core/server.py:684-725`，在 `_forward`
  内、source 门控后、转发前），后者追加新 text block。

**注入约束**：参照 nudge 改写先例，**追加 text block 到最后一条 user 消息，不插新
user message**。理由：插新 user message 破坏 role 交替（400）或改历史结构。

## 8. 双向回复

注入包装带 `from=<发送方全 id>` + "可用 `$message send to-agent <from-id>` 回复"。
B 的 agent 处理后回 A，A 经自身 check/listen 收到。**对称使用，无新机制。**

## 9. 已验证事实

### 9.1 UserPromptSubmit 只在人提交 prompt 时触发

**实测口径**：2026-08-10/11，隔离目录 `/tmp/ups-hook-test/`，claude 2.1.197，强制
Bash 工具调用 run 的 stream-json 显示 `num_turns:2`（人 prompt → tool 调用为第 1 轮、
tool_result → 最终回复为第 2 轮），但 UserPromptSubmit hooklog 恰好 1 行、prompt ==
人给原始指令。

**结论**：UserPromptSubmit hook **只在人提交 prompt 时触发，tool_result 续轮不触发**。
依赖 UserPromptSubmit 覆盖 mid-run 不成立，b 通道不可省。此为已验证事实。

### 9.2 splice 末尾追加 text block 可行（多场景实测）

**实测口径**：2026-08-11，隔离目录 `/tmp/splice-test/`（基础场景）与
`/tmp/splice-test2/`（扩展多场景），独立最小中间层（不碰 model_proxy 代码），
每场景 splice 组 + 对照组。

**基础场景**（tool_use 结尾 + 末尾 text 回执）：
- splice 组合成 anthropic SSE 流 = `message_start` →
  `content_block_start(tool_use, index=0)` → `content_block_delta(input_json_delta, 0)`
  → `content_block_stop(0)` → **（splice 注入）**
  `content_block_start(text, index=1)` → `content_block_delta(text_delta, 1,
  "[收到 A 留言: hi]")` → `content_block_stop(1)` → `message_delta(stop_reason=tool_use)`
  → `message_stop`。
- 结果：splice 组 transcript line 8 记录回执、stream-json 可见、续轮请求原样保留
  splice text+tool_use、num_turns:2 success；对照组唯一差异是无回执。

**扩展实测（/tmp/splice-test2，5 场景全过，无硬障碍）**：

| 场景 | 结果 |
|---|---|
| thinking+text+tool_use 交错 | 可行 |
| 多 tool_use（3 个） | 可行 |
| 大 delta 分片（48 个 1 字符 delta） | 可行，CC 正确拼接 |
| index 跳跃（0→2→5→6） | 可行，CC 完全容忍非连续 |
| 上游中途断流 | 不构成障碍——CC 优雅处理（不报错不卡死），未完成回执被丢弃但 tool_use 不受影响 |

**实现层约束**：回执 block 的 `start→delta→stop` 必须完整发出后再发
`message_delta`+`message_stop`。

**附带发现**：CC 把同一响应的多个 content block 拆成多条独立 assistant transcript
条目——回执是独立条目，可见性更好。

**结论**：响应通道回执可落地。splice 回执由 proxy 确定性注入，**消除对 model echo
（靠模型自觉复述）的依赖**。合成实测已覆盖主要流式形态，但真实上游（非合成）端到端
仍建议落地时回归（V7）。

### 9.3 nudge 改写先例

`core/server.py:684-725` `_rewrite_known_injected_texts`，请求侧改写既有 user 消息
text，在 `_forward` 转发前。→ v3 请求注入复用此模式（但追加 block 到最后 user 消息，
非原地改写）。

## 10. 消息类型（降 mid-run 跑偏）

注入包装支持类型标注：

- **query 类**：标"简答后继续当前任务"——告诉 B 的 agent 这是个问题，简短回答后
  继续它正在做的事。
- **guidance 类**：标"按此调整"——告诉 B 的 agent 这是指导，按此调整当前行为。

消息类型在 `$message send` 时可选指定，默认 query。

## 11. 邮箱（v3.1：目录口径 + 认领时机 + ACCESS 日志）

**单 inbox + status 字段（pending/delivered）**，不设 agent-seen 中间态。

```
config/messages/inbox/<session-id>.jsonl          # proxy append；消息入口
config/messages/delivered/<session-id>.<ts>.jsonl # hook/check/splice 落 transcript 后归档
```

- **目录口径**：与 `session_overrides.json` 同目录口径一致（都在 config/ 下）。
  README 注明"config/ 混有配置与运行时状态两类文件"。
- **简化理由**：b 一次性注入靠模型响应带下去，接受极少数"模型没接住"边角；若实测
  咬人再加 agent-seen 中间态。
- **b 通道认领时机（先于注入）**：b 判断有 to-agent 待投后，**先 rename 认领
  （mv 到 delivered/），再注入请求 + splice 回执**。否则 b 注入后 check 又认领
  同一封信 → 重复投递。其他通道看到信已不在 inbox 自然跳过。
- **ACCESS 日志 message 事件（O2）**：只记元数据不记内容（隐私 + 体积）：
  - `message_sent`（from / to / type / len）
  - `message_delivered`（channel = a / b / poll / sessionstart）
  - `message_spliced`（index）
  - `listen`（session, until）
- ground-truth 审计靠 `delivered/` 归档 + ACCESS 日志。

## 12. 空轮询 transcript 卫生

空轮询留 2 行（`$message check` + 合成"无"），无法避免。

**策略**：
- **源头限制为主**：10min 间隔 + 连续 6 次空自动停（=60min 无活动）。
- **必要时 `/compact`**：CC 原生压缩摘要掉 clutter。
- **不碰 jsonl**：运行中改 = uuid 链损坏 + 不重读，离线删脆弱。

## 13. 可见性（v3 关键特性）

所有投递在 B 的 SDK 页面可见，经响应通道：

- **to-human / 无信** = 合成完整响应（复用 `$route`，§6.1）
- **to-agent / b** = 响应末尾 splice 回执（§6.2）

**消除 model echo（靠模型自觉复述）的依赖**，回执由 proxy 确定性 splice。

## 14. 命令族

| 命令 | 执行者 | 用途 |
|---|---|---|
| `$message send to-agent <id> <内容>` | proxy 拦截 | 向目标 session 的 agent 投递消息 |
| `$message send to-human <id> <内容>` | proxy 拦截 | 向目标 session 的人投递消息 |
| `$message check` | proxy 拦截 | 统一手动/定时拉取，按类型路由（§6） |
| `$message listen` | proxy 拦截 + agent 执行 | proxy 记 `listen_until`（滑动窗口）；agent 侧 CronCreate 轮询（双重动作） |
| `$message` | proxy 拦截 | 裸命令：发出消息投递状态 + inbox 待收 + help |

### 解析层约束

当前工作树命令层仍是 `$route` 单命令形态（`commands.py:40`），`$message` 命名空间需
同批实施：

- 解析层认 `$message` 前缀。
- 内容豁免 token≤3 上限（消息含空格）。
- 单行约束保留。
- 短 id 前缀匹配（§3）。
- 目标不存在不硬拒，回执警示"本进程启动以来未见过该 session"。
- from 为发送方 session_id，与目标相同（自环）→ 拒绝。
- send 内容上限 2000 字符，超长回执拒绝并提示。

## 15. 风险与权衡

| # | 风险 | 说明与缓释 |
|---|---|---|
| 1 | A/B 须共享收件箱 | 同机器最简；跨机器要 proxy 网络可达 + 鉴权，out-of-scope 不展开 |
| 2 | B 的机器不能睡、CC 进程不能死 | 否则 b 和轮询全停 |
| 3 | listen 靠 agent 自建轮询 | proxy 推不进存活 session 进程内调度器（§5.2 硬约束），只能 agent 自建 |
| 4 | 空轮询留 transcript 痕迹 | 无法避免，靠源头限制（10min + 6 空自动停）+ `/compact`（§12） |
| 5 | CronCreate 7 天自动过期 | 离开超 7 天要续 |
| 6 | mid-run 注入有跑偏风险 | 靠消息类型标注缓释（§10） |
| 7 | B 重启丢 session-only 轮询 | `durable:true` 缓解 |
| 8 | splice 实测为合成响应 | 合成实测已覆盖主要流式形态（§9.2，5 场景全过），真实上游（非合成）端到端仍建议落地时回归（V7） |
| 9 | resume 是否触发 SessionStart 未知 | V4 端到端须明确覆盖 `--resume` 场景确认 SessionStart 是否触发；若不触发，补 fallback（resume 后首次 UserPromptSubmit 顺带查 inbox） |
| 10 | fork 后 listen 不继承 | fork 产生新 session_id，不继承原 session 的 listen 窗口和 cron。skill 文档提醒 agent fork 后需重新 `$message listen` |

### prompt-injection 面

任何持有 client token 的客户端可向任意 session 投递，内容会被目标 session 的模型读到。
与 `$route` 同级信任假设（能发请求即有 token），但后果从"改自己路由"扩大为"影响他人
上下文"。缓释：包装格式显式标注"非本会话用户指令"；响应通道回执 transcript 可见
可审计；delivered 归档留痕。README 必须写明。

## 16. 不采纳的替代方案

- **直写 session jsonl**：否决——uuid 链分叉、运行中不感知。
- **CC 内建 SendMessage/TeammateMailbox**：agent-teams 内部机制，独立 session
  借用不了。
- **PostToolUse 作 mid-run 注入备选**：b（请求路径注入）更对口径，PostToolUse 可作
  未来评估。
- **model echo**：被 splice 回执取代——echo 不可靠（模型可能不复述、改述、遗漏），
  回执由 proxy 确定性 splice。
- **两态邮箱 agent-seen**：简化为单 inbox + status 字段（§11）。

## 17. proxy skill

需一个名为 **`proxy`** 的 skill（A/B 通用，agent 按意图选动作）。覆盖 proxy 层的完整
in-band 命令机制——`$route` 与 `$message` 两族命令、listen 流程、收/发/取回执处理。

> **理想路径口径**：不计成本，追求结构化、长期可扩展。标注"理想项"的部分为超出最小
> SKILL.md 的增量基建，务实口径可裁。

### 17.1 skill 定位与边界

**定位**：教 agent 理解 model_proxy 这一层是什么、in-band 命令怎么工作、如何使用
`$route` 和 `$message` 两族命令完成跨 session 通信与远程遥控。

**边界**：
- skill **只通过 in-band 命令**与 proxy 交互（`$route` / `$message`），不直接碰
  proxy 内部（sidecar / inbox 文件 / server.py 对象 / 日志）。
- listen 由 proxy 拦截（记 `listen_until`）+ agent 侧执行（CronCreate 轮询）双重
  动作——skill 教 agent 自己完成 CronCreate 部分。
- skill 是纯知识层，不持有 proxy 状态句柄；所有状态查询经 `$route` / `$message` 命令。

### 17.2 SKILL.md frontmatter description

```
description: 理解并使用 model_proxy 的 in-band 命令层。覆盖 $route 路由切换/查询
  与 $message 跨 session 消息互通两族命令。当用户需要切换模型路由、查看 proxy 状态、
  远程向另一 session 发消息、跨 session 通信、远程遥控另一 session、或收到注入的
  proxy 消息包装时激活。触发词：$route、$message、listen、proxy 路由、远程控制、
  跨 session 消息、收件箱。
```

### 17.3 正文结构

```
SKILL.md
├── 概念层
│   ├── proxy 是什么（请求转发层 + 控制面）
│   ├── 为什么有 in-band 命令（消息级拦截、不转发上游、合成响应回执）
│   └── 响应通道回执机制（合成完整响应 vs splice 回执）
├── 命令族
│   ├── $route 速查（切换 / 查询 / reset）
│   └── $message 速查（send to-agent / send to-human / check / listen / 裸命令）
├── A 侧流程（发信方）
│   ├── 发信：$message send to-agent <b-id> <内容>
│   ├── 取回执：$message check 或 $message 裸命令
│   └── listen 收 B 回复（对称，A 也开 listen）
├── B 侧流程（收信方）
│   ├── listen：$message listen（proxy 记 listen_until）+ CronCreate 轮询
│   ├── 收信处理：识别注入包装 → 当远程指令处理
│   └── 回复：$message send to-agent <from-id> <内容>
├── listen 机制
│   ├── 硬约束：proxy 推不进存活 session（§5.2），必须 agent 自建轮询
│   ├── listen 动作：$message listen（proxy 记 listen_until）+ CronCreate(10min, durable:true)
│   ├── 滑动窗口：每次 check 刷新 listen_until；无手动 off
│   └── 自动关：连续 6 次空轮询（60min）→ cron 停 → 窗口超时 → b 自动关
├── 收信/发信/取回执处理
│   ├── 收信（无信 / to-human）：proxy 合成完整响应，SDK 页面可见
│   ├── 收信（to-agent / b）：注入请求 + splice 回执进响应流末尾
│   ├── 发信回执：proxy 拦截后合成响应返回
│   └── 取回执：$message check 按类型路由
├── 卫生
│   ├── 短 id 匹配：前缀唯一命中自动补全，多命中报错列候选
│   ├── 消息类型：query（简答后继续）/ guidance（按此调整）
│   ├── 轮询自动停：连续 6 次空轮询（60min 无活动）自动停
│   ├── fork 后 listen 不继承：fork 产生新 session_id，需重新 $message listen
│   └── 自环拒绝：from == to 拒绝
└── 示例
    ├── A 侧：发信 → 取回执 → listen 收回复
    ├── B 侧：listen on → 收信 → 处理 → 回复
    └── 双端对称：双向 listen 完整链路
```

### 17.4 子文件评估（理想项）

| 文件 | 内容 | 理想/务实 |
|---|---|---|
| `references/listen-cron-template.md` | CronCreate 注册轮询的完整模板（durable:true、10min 间隔、6 次空自动停逻辑、通知 proxy 开关 b 的命令） | 理想项。务实口径可将模板内联在 SKILL.md 正文 |
| `references/message-wrapper-format.md` | 注入包装格式定义（from-id、消息类型标注、"非本会话用户指令"提示、回复指令）+ B 侧解析指引 | 理想项。务实口径可将格式样例内联在 SKILL.md 正文 |

### 17.5 与 model_proxy 代码的边界

- skill 是纯知识层，教 agent **怎么发命令**，不含 proxy 状态读写逻辑。
- proxy 状态查询一律经 `$route` / `$message` 命令（in-band），skill 不直接读
  sidecar / inbox 文件。
- listen 的 agent 侧动作（CronCreate / CronDelete）是 Claude Code 原生能力，
  skill 只教使用方式，不封装额外逻辑。

### 17.6 理想 vs 务实差异点

| 维度 | 务实口径 | 理想口径（本文） |
|---|---|---|
| 文件数 | 单 SKILL.md | SKILL.md + 2 个 references 子文件 |
| listen 模板 | 内联在正文 | 独立 references 文件，可复用 |
| 包装格式 | 正文给样例 | 独立 references 文件，完整格式定义 |
| 触发面 | 只覆盖 $message 命令 | 同时覆盖 $route 命令 + 注入包装识别 |

### 17.7 实施时机

**skill 创建是实施的最后一步**，排在所有功能代码之后：

1. commands.py parse 分派 + handler（send / check / listen / 裸命令）
2. server.py：`_maybe_inject_message` 独立函数 + splice 分层落地（条件触发
   前置统一判断 `_splice_receipt`）+ check 路由 + listen 滑动窗口（`listen_until`
   状态表）+ ACCESS 日志 message 事件
3. hooker/deliver_messages.sh（UserPromptSubmit + SessionStart）
4. _install_ops.py hook 管理泛化
5. tests/ 单测
6. README $message 小节 + 安全提示 + config/ 混合配置/运行时状态说明
7. **proxy skill 创建**（skill-creator 落地）

## 18. 影响面

- `core/commands.py`：parse 函数新增 `$message` 前缀分派；`handle_message_send`、
  `handle_message_check`、`handle_message_listen`（记 `listen_until` + 合成确认）、
  `handle_message_status`（裸命令）handler；help 文案。
- `core/server.py`：
  - `CommandContext` 注入"见过的 session"表句柄 + `listen_until` 状态表；`_forward`
    在无命令时也顺带记录 session last_seen。
  - `_maybe_inject_message(body_json, session_key, listen_state)` 独立函数（§7）：
    命令拦截后、route 选择前调用，与 `_rewrite_known_injected_texts` 不合并。
  - 条件触发前置统一判断 `_splice_receipt`（`server.py:1660` 附近），分层落地
    （§6.2 表）：3b/4b dict append / 3a/4a adapter 事件级 / 1b json 改写 / 1a
    PASSTHROUGH 流式字节透传升级事件级（复用 `_parse_anthropic_sse_block`）；
    5a/5b 首版不支持。
  - splice 失败降级（O3）：上游断 / 写一半 CC 断 → 放弃回执，不重投（§6.2）。
  - `$message check` 拦截/按类型路由逻辑。
  - ACCESS 日志 message 事件（§11）：`message_sent` / `message_delivered` /
    `message_spliced` / `listen`，只记元数据。
- 新增 `hooker/deliver_messages.sh`——UserPromptSubmit + SessionStart hook 脚本。
- `_install_ops.py`：hook 管理泛化到本工具第 2 条 hook。
- 新增 `proxy` skill（§17）——**实施的最后一步**，排在以上全部功能代码（commands.py
  handler、server.py 注入/splice/check/listen、hooker、_install_ops.py、单测、README）
  之后，功能代码全部落地且单测通过后才用 skill-creator 创建。
- `tests/`：parse `$message` 分派、send handler（写收件箱/短 id 匹配/自环/超长）、
  check 按类型路由（无信合成/to-human 合成/to-agent 注入+splice）、maildir rename
  不丢消息的并发用例、b 通道先认领后注入不重复投递（§11）、listen_until 窗口
  （listen 记窗 / check 刷新 / 超时自动失效）对 b 注入的控制、splice index 单调性。
- README 加 `$message` 小节 + 安全提示 + config/ 混合配置/运行时状态说明。

## 验证方式

沙箱口径复用 2026-08-04 文档：`ANTHROPIC_BASE_URL=http://127.0.0.1:18899/
--setting-sources project,local`。

- **单测**：parse `$message` 分派/回归（缺参、多行内容、句中提及不误命中）；send
  handler 写收件箱、短 id 唯一/多命中/零命中、自环拒绝、超长拒绝；check 按类型
  路由（无信合成"无"不调模型、to-human 合成返回、to-agent 注入+splice）；maildir
  并发用例（append 与 hook mv 交叉，断言不丢不重）；b 通道先认领后注入不重复投递；
  listen_until 窗口（listen 记窗 / check 刷新 / 超时自动失效）对 b 注入通道控制；
  splice index 单调性校验。
- **V1 端到端（CLI×CLI，通道 a）**：沙箱起两个 `claude --session-id`，A 发
  `$message send to-agent <B> hi`，断言：A 收回执；B 下一条 prompt 后 UserPromptSubmit
  hook 注入消息（B 的 transcript 可见包装文本）；inbox → delivered 归档。
- **V2 mid-run 注入（通道 b）**：B `$message listen` → B 跑长任务（tool 循环中）→
  A 发消息 → 断言 B 的下一个请求被 proxy 追加 text block 注入 → B 的响应末尾 splice
  回执可见 → B 的 agent 收到并处理。
- **V3 轮询（通道 check）**：B `$message listen` → B 空闲无人操作 → A 发消息 →
  断言 CronCreate 触发 `$message check` → proxy 按类型路由 → B 的 agent 处理 →
  B 用 `$message send to-agent <A>` 回复 → A 经 check/listen 收到。
- **V4 离线投递（SessionStart）**：B 退出后 A 发消息 → B `--resume` → **明确确认
  SessionStart 是否触发**；若不触发，补 fallback（resume 后首次 UserPromptSubmit
  顺带查 inbox）。
- **V5 边界**：目标不存在（回执警示 + 消息留存）；短 id 前缀投递；自环拒绝；
  超长拒绝。
- **V6 空轮询卫生**：连续 6 次空轮询后自动停 → `listen_until` 超时 → b 自动失效；
  检查 transcript poll-pattern 行数；`/compact` 后 poll 痕迹被摘要掉。
- **V7 splice 真实上游回归**：合成实测已覆盖主要流式形态（§9.2，5 场景全过），
  V7 聚焦真实上游（非合成）端到端回归确认无差异。
- **V8 Claudian 端到端**：临时改 user 层 settings 指向沙箱，Claudian tab 收/发
  各验一次，立即还原。
- **回归**：`python3 -m unittest discover tests` 全绿；不含命令的消息照常转发
  （fail-open）。

## 关联

- [[2026-08-11-proxy-message-splice-feasibility]]——splice 回执可行性调研，§9.2
  实测证据的来源文档（v3.1 §9.2 扩展多场景结论基于 /tmp/splice-test2 扩展实测）。
- [[2026-08-09-proxy-command-namespace-design]]（`$proxy` 命名空间方案）——v3 显式
  取代该方向的"统一进 `$proxy` 命名空间"设计，`$message` 与 `$route` 并列为平级命令。
  该文档 status 仍为 draft 未落地，v3 不依赖它。
- [[2026-08-04-in-band-route-command-design]]（命令层骨架、§7.3 边界约束、fail-open
  原则、沙箱验证方法）
- [[2026-08-08-status-active-sessions-design]]（活跃 session 口径、短 id 惯例）
- [[2026-07-22-install-manage-sessionstart-hook]]（hook 条目幂等管理先例）
- [[2026-08-06-session-overrides-single-storage]]（sidecar 存储模式）

---

## 附录 A：CC 官方 cross-session messaging 调研与搁置决策（2026-08-12）

### A.1 触发

CC 2.1.224（2026-08-07）正式引入官方 cross-session messaging。本方案 v3.1 设计的
核心场景（跨 session 双向消息、mid-run 投递、远程遥控）与官方功能高度重叠。经调研
官方文档与系统 prompt 工具描述，确认官方功能覆盖面足够，故**搁置本自建方案**，待
brew 版本更新到 ≥2.1.224 且解除禁用 env 后使用官方功能。

### A.2 CC 官方能力全貌（2.1.224+）

- **工具**：`ListAgents`（发现可达 session）+ `SendMessage`（按名字投递纯文本）。
  Claude 自己调，用户自然语言触发（"告诉 payments 那个 session 我们改了什么"）。
  slash 命令：`/list-agents`（`/peers`）、`/status`、`/rename`。
- **寻址**：会话名（`/rename`、`--name`，否则按工作目录推导）；重名用 `[ref]` 短标识
  消歧。`ListAgents` 自动发现同机 session。
- **投递时机**：mid-turn 在两次工具调用间隙注入（不打断单个运行中工具）；空闲时
  开新 turn。消息入队，接收方下一轮工具间隙排空。
- **可见性**：以发送者身份进 conversation，transcript 可见（2.1.227+ 内联显示，
  可折叠）。
- **回复**：入站消息带 `from`，回复 = 把 `from` 复制为 `to`。包装为
  `<cross-session-message from="...">`。
- **信任**：`crossSessionInbound` accept/hold/refuse；按权限模式分类决定；held
  默认 5min 过期；消息不能批权限、不能改配置、命令不执行。
- **传输**：同机 Unix socket（`CLAUDE_CODE_MESSAGING_SOCKET`，owner-only）；跨机
  Remote Control 经 Anthropic 服务器。
- **限制**：纯文本；50 条未读/会话；限流；仅 macOS/Linux；Bedrock/Cloud 不可用；
  四个隐私 env（`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`、`DISABLE_TELEMETRY`、
  `DO_NOT_TRACK`、`DISABLE_GROWTHBOOK`）任一设置即静默禁用。
- **来源**：`code.claude.com/docs/en/cross-session-messaging`、
  `code.claude.com/docs/en/agent-teams`、`code.claude.com/docs/en/settings`、
  GitHub `anthropics/claude-code` CHANGELOG v2.1.224/2.1.225。

### A.3 与本方案 v3.1 的对比

| 维度 | CC native（2.1.224+） | v3.1（本方案） |
|---|---|---|
| 架构层 | CC 客户端原生（socket） | model_proxy 代理层 |
| mid-run 投递 | ✅ 工具调用间隙注入（不打断工具） | ✅ channel b 请求注入（下一请求） |
| 空闲投递 | ✅ 开新 turn | ✅ channel a / poll |
| 可见性 | transcript 内联可见 | response-splice 回执 + delivered 归档 + ACCESS 日志 |
| 信任模型 | 成熟：accept/hold/refuse + 权限模式分类 + 不能批权限/改配置/执行命令 | 简单：包装标注 + 可见 + 归档 |
| 会话发现 | ListAgents 自动发现同机 | session_registry 短 id 匹配 |
| 跨机 | Remote Control（Anthropic 服务器） | model_proxy + IM 桥（自托管） |
| 跨上游 | ❌ CC 内部，与上游无关 | ✅ 走 proxy，跨 glm/kimi/openai |
| 版本依赖 | 需 2.1.224+ + 解除 4 个 env | 2.1.197 即可 |
| 消息类型 / to-human | ❌ 纯文本，总给 agent | ✅ query/guidance + to-human 直达 |
| 限流 | 50 条/会话、限流 | 无硬上限（2000 字符） |
| 维护 | 官方 | 自维护 |

### A.4 决策与理由

官方功能覆盖了 v3.1 的核心场景，且 mid-run 投递更优雅（客户端工具间隙注入，不动
请求体，无 index 跟踪/splice 正确性风险），信任模型更成熟。**搁置 v3.1 自建**，
改用官方功能。

搁置而非删除：本方案的设计推演、实测证据（UserPromptSubmit 只触发人 prompt、
splice 5 场景、hooker stdout + resume 触发 SessionStart）、架构审核、plan v1.1
仍有参考价值——若未来官方功能在以下窄场景不足，可重启自建：

1. **跨上游**：官方 native 只在 CC 内部、与上游无关；若需跨 glm/kimi/openai 等
   非 CC 客户端互通，v3.1 的 proxy 层方案不可替代。
2. **自托管跨机**：官方跨机走 Anthropic 服务器（Remote Control）；若需完全自托管
   不经 Anthropic 中转，v3.1 + IM 桥是替代。
3. **proxy 侧审计**：官方只有 transcript 可见；v3.1 有 ACCESS 日志 + delivered
   归档，审计更全。
4. **消息类型 / to-human**：官方纯文本总给 agent；v3.1 区分 query/guidance、
   to-human 直达不进模型。

### A.5 启用官方功能的前置条件（待 brew 更新后执行）

1. **升级 CC**：`brew upgrade claude-code` 当前只到 2.1.220（不足）；待 brew
   `claude-code` cask 更新到 ≥2.1.224，或改用 `brew install --cask
   claude-code@latest`（当前 2.1.226）/ native installer
   （`curl -fsSL https://claude.ai/install.sh | bash`，自动后台更新到最新）。
2. **解除禁用 env**：`~/.claude/settings.json` 第 5 行
   `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1"` 会静默禁用 messaging——需删除
   或置 0（注意：此举同时放开 telemetry/growthbook，权衡隐私）。
3. **验证**：`/list-agents` 能识别即功能启用；`/status` 的 Peer address 显示
   `uds:` 前缀即 socket 已绑。

### A.6 相关物处置

- **实施计划** [[2026-08-11-proxy-message-implementation-plan]]（plan v1.1）：同步
  搁置，status 标 `shelved`。
- **splice 可行性** [[2026-08-11-proxy-message-splice-feasibility]]：实测证据保留，
  作为"若重启自建时的技术验证参考"。
- **worktree `feat-proxy-message`**：可清理（`qw` 删除或 `git worktree remove`），
  未合并的分支保留待重启。
- **proxy skill**：v3.1 §17 设想的 `proxy` skill 暂不建；若启用官方功能后需要，可
  建一个教 agent 用 `ListAgents`/`SendMessage` 的轻量 skill（非 v3.1 的自建命令族）。
