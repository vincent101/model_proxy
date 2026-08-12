---
type: design-decision
status: shelved
target: "[[2026-08-10-proxy-message-inter-session-design]]"
tags: [architect, model_proxy, splice, sse, feasibility, inter-session-message, superseded-by-cc-native]
---

# `$message` splice 回执与命令收敛可行性调研

> ⚠️ **搁置（2026-08-12）**：CC 2.1.224+ 官方 cross-session messaging 覆盖本方案核心
> 场景，自建方案搁置。详见 [[2026-08-10-proxy-message-inter-session-design]] 附录 A。
> 本文档的 splice 实测证据保留，作为"若重启自建时的技术验证参考"。

> [务实] 路径。在 v2 设计（[[2026-08-10-proxy-message-inter-session-design]]）基础上
> 调研两个细化方向的可行性，产出结论与 v2 更新建议清单（不重写 v2，只列建议）。
> 核实基准：2026-08-11 master 工作区；core/server.py + core/translate.py 代码实证；
> claude-api skill 的 anthropic SSE 格式参考（curl/examples.md）；CC hooks changelog
> （~/.claude/cache/changelog.md）。

## 1. 背景与问题

v2 的 b（mid-run 请求注入）/poll（cron 轮询改写请求）都是**请求侧注入**——消息本体不进
transcript，B 的 SDK 页面看不到"agent 收到了什么"。用户提出两个细化：

- **细化1（命令收敛）**：`poll`（cron 定时）和 `receive`（手动拉取）收敛为一个
  `$message check`，手动或定时都调它，按消息类型（to-agent/to-human）走对应链路。
- **细化2（响应通道回执）**：借鉴 `$route` 直接返回合成响应——B 无论手动还是定时 check、
  无论 to-agent 还是 to-human，**先把消息内容经响应通道返回到 B 的 SDK 页面**（形成
  可见记录）。依据：check 时 B 正向 proxy 发请求，proxy 可以经响应通道把内容发回 B，
  而响应会被 CC 记录进 transcript、显示在 SDK 页面。

目标：所有投递都在 SDK 页面可见，消除对 model echo（靠模型自觉复述）的依赖。

## 2. 可行性结论

### 2.1 synthetic 完整响应（to-human / 无信）——已验证可行

- v2 的 `$route` 已实现 `_write_builtin_stream_response` / `_write_builtin_buffered_response`
  （`core/server.py:1914-1962`），手工合成完整 anthropic SSE 事件序列：
  `message_start → ping → content_block_start_text(0) → content_block_delta_text(0, receipt)
  → content_block_stop(0) → message_delta → message_stop`（流式）或等价非流式 JSON body。
- CC 客户端在此场景下正常解析、记录进 transcript、显示在 SDK 页面——已验证。
- `$message check` 的 to-human / 无信两条路径直接复用该机制即可，无需重测。
- **结论：可行，无需新机制。**

### 2.2 splice 回执进响应流（to-agent / b 通道）——协议合法、工程可行，但 CC 客户端容忍度是黑盒，必须实测

这是决定性未知点。

**协议层面合法（anthropic SSE 格式，claude-api skill 的 curl/examples.md 实证）：**

anthropic 流式响应是 SSE 事件序列：`message_start` → N×(`content_block_start` +
`content_block_delta`×k + `content_block_stop`) → `message_delta` → `message_stop`。
每个 `content_block_*` 事件带 `index` 字段标识它属于第几个 block（从 0 起）。

两条 splice 注入路径：

| 路径 | 做法 | 复杂度 | 风险 |
|---|---|---|---|
| **开头注入** | message_start 之后、上游第一个 content_block_start 之前，插入 `start_text(0) → delta → stop(0)`，然后把上游所有后续 content_block 的 `index` 全部 +1 | 高——需逐事件解析、index 重排，PASSTHROUGH 从字节透传升级为事件级 | index 漂移易错；与 thinking block（若有）交错复杂 |
| **末尾追加** | 让上游流正常透传，检测到 `message_delta` 事件时，在其之前注入一个完整 text block（`start(N) → delta(N) → stop(N)`，N = 上游已用最大 index + 1），再转发 message_delta/message_stop | 中——需按 SSE 事件边界（`\n\n`）切块转发而非 8K chunk，跟踪 index 上限 | 需处理 tool_use 结尾消息的结构合法性 |

**推荐末尾追加**——实现更简、不碰 index 重排。

**工程层面可行（代码证据）：**

- `OpenAIToAnthropicStreamAdapter`（`core/translate.py:716+`）已是完整状态机，管理
  `cur_index`（从 -1 起，单调递增）、`block_open`/`cur_type`、content_block_start/stop
  配对。所有事件构造 helper（`_content_block_start_text` / `_content_block_delta_text` /
  `_content_block_stop` / `_message_delta_event` / `_message_stop_event`）都已存在。
- 当前 PASSTHROUGH 流式走 `_write_streaming_response`（`server.py:2054+`），逐 chunk
  `read(8192)` 转发 + 旁路 `_sniff_passthrough_usage` 嗅探 usage。要把 splice 做进
  PASSTHROUGH 流式，需把该路径从"字节透传 + 旁路嗅探"升级为"事件级透传 + 注入"：
  按 `\n\n` 切 SSE 事件块 → 解析 event_type → 累积 max index → 在 message_delta 前注入
  回执 block。已有 `_parse_anthropic_sse_block`（`server.py:2363+`）做事件解析。
- 实现量：中等。不引入新依赖（纯标准库）。

**mid-tool-loop 的关键风险点（真正需要实测的）：**

mid-run 注入时，模型响应以 `tool_use` block 结尾，`stop_reason=tool_use`。若在末尾追加
一个 text block，assistant 消息的 content 变成 `[..., tool_use, text]`。

- **API 层面合法**：anthropic content 是有序 block 数组，text-after-tool_use 合法。
- **CC 客户端层面未知**（黑盒）：
  - CC 的 agent loop 逻辑：解析 `stop_reason=tool_use` → 提取所有 tool_use block → 执行 →
    构造 tool_result 续轮。追加的 text block 是否影响 tool 提取？（按规范不应影响，CC 应
    只过滤 type==tool_use 的 block。）
  - 追加的 text block 是否进入 transcript？（按规范应进，SDK 页面应显示——这正是要的效果。）
  - 续轮时这条 assistant 消息（含尾部 text）作为历史回传上游，API 是否接受？（按规范接受。）
- **无法从代码/文档 100% 确定，必须实测。**

**实测方案（决定性实测，留 implementer 执行）：**

做一个独立最小 splice 中间层脚本（**不碰 model_proxy 代码**），验证 CC 客户端对
"tool_use 结尾的 assistant 消息末尾追加 text block"的容忍度：

1. 写一个最小 Python HTTP server（监听如 127.0.0.1:18901），行为：
   - 接收 `claude -p` 的 anthropic 请求。
   - 不转发上游；直接合成一个固定的 anthropic SSE 流作为响应，事件序列：
     `message_start` → `content_block_start(tool_use, index=0)` →
     `content_block_delta(input_json_delta, index=0)` → `content_block_stop(0)` →
     **（splice 注入）** `content_block_start(text, index=1)` →
     `content_block_delta(text_delta, index=1, text="[收到 A 留言: hi]")` →
     `content_block_stop(1)` → `message_delta(stop_reason=tool_use)` → `message_stop`。
   - 即：一个合法的 tool_use 回复，末尾追加一个 text 回执 block。
2. 沙箱起 `claude -p --session-id test-splice --setting-sources project,local`，配一个
   会触发 tool_use 的 prompt（如让 CC 调 Bash 跑 `echo hi`），`ANTHROPIC_BASE_URL`
   指向 splice 中间层。
3. 观察：
   - transcript（`~/.claude/projects/<slug>/test-splice.jsonl`）是否记录注入的 text 回执。
   - CC 是否正常执行 tool_use、正常续轮（tool_result → 下一轮请求）。
   - SDK 页面（若用 Claudian）是否显示回执。
   - turn 是否正常完成（不卡死、不报错）。
4. 对照组：去掉 splice（不追加 text block），重复实验，确认行为差异只在"回执是否可见"。

若实测通过：splice 可行，v2 可加入 splice 回执机制，消除 model echo。
若实测不通过（CC 报错/卡死/不记录回执）：
  - 尝试"开头注入"路径（index 重排）——但若末尾追加都不行，开头注入更可能出问题。
  - 退备选：b 通道可见性退回 model echo（注入请求时附带指令"收到 X 留言后请先复述再继续
    当前任务"）——不可靠但可用；或 b 通道不留可见回执，只在 hook/轮询其他通道留痕。

### 2.3 命令收敛（poll/receive → `$message check`）——可行

- 手动 check 与 cron check 都是同一个 prompt `$message check`，proxy 无法也无需区分来源。
- 按消息类型路由：
  - 无信 → proxy 合成"无"返回（不经模型，同 §2.1）。
  - to-human → proxy 合成完整响应直接返回内容（不经模型，同 §2.1）。
  - to-agent → proxy 把消息注入请求转发模型（模型处理），**同时 splice 回执进响应流**（§2.2）。
- 一致性：手动 check 时人在 SDK 页面看结果；cron check 时没人看，但回执仍 splice 进响应/
  合成返回，留下 transcript 痕迹。两者一致，都可见。
- 唯一语义注意点：手动 check 有 to-agent 信会触发模型处理（消耗 turn、可能跑偏）。如果人
  只想看 to-human 的信、不想让模型动，可用子参数（如 `$message check human` 只拉 to-human）
  缓解；或不缓解，文档写明"check 会处理 to-agent 信"。**不阻塞收敛。**

### 2.4 是否消除 model echo、是否整体简化 v2

- splice 回执（§2.2）若可行，消除了对 model echo 的依赖：回执由 proxy 直接 splice 进
  响应流，不依赖模型自觉复述。这是关键改进——echo 不可靠（模型可能不复述、改述、遗漏）。
- to-human 走 synthetic 完整响应（不经模型），天然可见，无需 echo。
- to-agent 走"注入请求 + splice 回执进响应"，可见性由 splice 保证。
- 整体简化：消除 model echo 这个不可靠环节；命令族从 send/receive/poll/listen 收敛为
  send/check/listen（receive 并入 check）；回执机制统一（所有投递经响应通道可见）。

## 3. 风险与权衡

| # | 风险 | 说明与缓释 |
|---|---|---|
| 1 | **splice 的 CC 客户端容忍度未知** | 决定性未知点。必须做 §2.2 的决定性实测。实测不了就不敢上。 |
| 2 | PASSTHROUGH 流式升级为事件级透传 | 当前是字节透传 + 旁路嗅探；splice 需按 `\n\n` 切事件块、解析 event_type、跟踪 index。实现量中等，不引入新依赖。注意保持"异常不影响转发"原则（嗅探/注入的失败不阻断流）。 |
| 3 | 末尾追加遇 `stop_reason != tool_use`（纯 text 回复） | 同样合法：text 后再追加 text 是多个 text block，API 接受。但此时 proxy 已能直接合成（无需 splice）——若整条响应都是 text，走 synthetic 更简单。splice 只在"上游有 content_block 需透传"时才用。 |
| 4 | `$message check` 手动触发 to-agent 会跑模型 | 人手动 check 时 to-agent 信会触发模型处理，消耗 turn。可加子参数（`check human`）或文档写明。不阻塞收敛。 |
| 5 | 响应已发出后无法回追 | 流式 splice 一旦发出无法撤回；若 splice 逻辑出错（如 index 算错），CC 可能报错。需在注入前校验 index 单调性。 |
| 6 | prompt-injection 面（继承 v2） | splice 回执不改 prompt-injection 面——注入请求侧才是 injection 面。回执只是可见性。 |

## 4. 对 v2 的更新建议清单（不重写，只列建议）

以下为基于可行性结论对 v2 文档的更新建议，供用户确认后由 architect/implementer 落地。

### 4.1 命令族收敛

- 将 §10 命令族的 `$message receive` 与 `$message poll` 合并为 `$message check`。
- `$message check` 行为：proxy 查 B 收件箱，按消息类型路由：
  - 无信 → 合成"无"返回（不经模型）。
  - to-human → 合成完整响应直接返回内容（不经模型，同 `$route` 手感）。
  - to-agent → 把消息注入请求转发模型（模型处理），同时 splice 回执进响应流。
- §4.1 listen on 时 agent 自建 CronCreate 的轮询命令从 `$message poll` 改为
  `$message check`。
- §13 proxy-message skill 的"取回执（A 侧）"从 `$message receive` 改为 `$message check`。

### 4.2 回执机制（细化2）

- 新增 §X（splice 回执机制）：to-agent 消息注入请求转发模型后，proxy 在响应流末尾
  （message_delta 前）splice 一个 text content block 回执，内容形如
  `[收到 A(id=…) 留言: <内容摘要>]`，使 SDK 页面可见。
- 标注**决定性实测前置**：splice 回执的 CC 客户端容忍度必须先做 §2.2 的实测验证，
  通过后才落地此机制。
- 若实测不通过，b 通道可见性退回 model echo（注入请求时附带复述指令），并标注
  "不可靠，依赖模型自觉"。

### 4.3 消除 model echo

- §6（双向回复）/ §7（消息类型）中涉及"靠模型自觉复述/echo"的措辞，改为"回执由
  proxy splice 保证可见性，不依赖模型 echo"。
- §12 风险表新增一行：splice 回执的 CC 容忍度风险（若实测未通过则改列 echo 退路）。

### 4.4 影响面补充

- §14 影响面补：PASSTHROUGH 流式路径（`_write_streaming_response`）从字节透传升级为
  事件级透传 + 注入；新增 SSE 事件边界解析与 index 跟踪逻辑（复用
  `_parse_anthropic_sse_block`）。
- §14 补：`$message check` handler 合并 receive + poll 逻辑，按消息类型路由。

### 4.5 验证方式补充

- §验证方式 新增 V8（splice 决定性实测）：独立最小 splice 中间层脚本 + `claude -p`
  经它 + 检查 transcript 记录回执 / turn 正常完成 / SDK 显示 / 对照组。
- V2/V3 端到端改用 `$message check` 命令。

## 5. 关联

- [[2026-08-10-proxy-message-inter-session-design]]（v2 主设计，本调研为其补充）
- [[2026-08-04-in-band-route-command-design]]（`$route` synthetic 响应先例，splice 回执
  的机制基础）
- [[2026-08-09-cli-thinking-only-nudge文案proxy改写]]（PASSTHROUGH body 改写先例，
  事件级处理的参考）
