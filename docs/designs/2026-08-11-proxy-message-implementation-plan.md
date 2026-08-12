---
type: implementation-plan
status: shelved
version: 1.1
design: "[[2026-08-10-proxy-message-inter-session-design]]"
branch: feat/proxy-message
tags: [architect, model_proxy, proxy-message, implementation-plan, superseded-by-cc-native]
---

> ⚠️ **搁置（2026-08-12）**：CC 2.1.224+ 官方 cross-session messaging 覆盖本方案核心
> 场景，改用官方功能。详见设计文档附录 A。本计划保留作为"若重启自建时的实施参考"，
> 不执行。

# `$message` 分步实施计划（文件级粒度，v1.1）

> 关联设计：[[2026-08-10-proxy-message-inter-session-design]]（v3.1，工作树已同步）。
> 工作树：`/Users/vincentwang/Documents/worktrees/notevault/feat-proxy-message`。
> 所有路径以工作树绝对路径为基准。难度排序按 v3.1 §B。
> v1.1 相对 v1.0：落回架构审核修正——listen 滑动窗口落地到拦截点、source 分发注入、
> hooker 输出方式钉死、PASSTHROUGH 流式幂等、ACCESS 投递元数据日志、注入失败回执
> 边界、responses 方向钉死、邮箱路径补 `messages/` 层、fork/前置约定等低优项。

## 章节结构

0. 前置约定（路径 / 常量 / 命名 / 消息 schema）
1. 收件箱基础（maildir 式 inbox/delivered）
2. "见过的 session" 表 + 短 id 前缀匹配
3. commands.py `$message` 解析层
4. `$message send` handler（非 splice 部分）
5. `$message check` handler（无信 / to-human 合成响应 + 刷新 listen 窗口）
6. listen 拦截点（proxy 拦截 + 滑动窗口，无显式 off）
7. 请求注入 `_maybe_inject_message`（按 source 分发，R3 先认领后注入）
8. splice 3b/4b 非流式（ANTHROPIC_TO_CHAT / RESPONSES_TO_ANTHROPIC 非流式）
9. splice 3a/4a 流式（adapter finalize 注入）
10. splice 1b PASSTHROUGH 非流式
11. splice 1a PASSTHROUGH 流式（最硬，放最后）
12. hooker/deliver_messages.sh（通道 a UserPromptSubmit + SessionStart）
13. _install_ops.py hook 管理泛化（第 2 条 hook）
14. ACCESS 日志扩展 + 投递元数据 + splice 失败降级（O3）
15. README `$message` 小节 + 安全提示
16. proxy skill（skill-creator 落地，最后一步）

---

## Step 0: 前置约定

**文件**：本文档定约，代码侧落点为 `core/commands.py` / `core/server.py` 顶部常量。

**约定**：
- **基准路径**：所有路径以工作树绝对路径
  `/Users/vincentwang/Documents/worktrees/notevault/feat-proxy-message/` 为根。
- **命令前缀**：`MESSAGE_CMD_PREFIX = "$message"`（与 `$route` 并列，不进 `$proxy`）。
- **listen 滑动窗口常量**：`LISTEN_WINDOW = 3600`（秒，≥ 60min，对齐 cron 连续 6 空自动停周期）。
- **消息结构 schema**（inbox/delivered 单条 JSONL 行）：
  ```json
  {
    "from": "<sender session_id>",
    "to": "<target session_id or 字面 prefix>",
    "msg_type": "to_agent | to_human",
    "kind": "query | guidance",
    "content": "<≤2000 字符正文>",
    "ts": "<iso8601>",
    "source": "anthropic | responses | chat",
    "status": "pending | delivered"
  }
  ```
- **source 取值口径**：`source` 由 proxy 在请求门控处判定，取当前请求上游协议——
  anthropic 入站（`/v1/messages`）记 `"anthropic"`；responses 入站（Codex
  `/v1/responses`）记 `"responses"`；chat 入站记 `"chat"`。`source` 字段跟随
  消息投递，注入时按 `source` 分发（Step 7）。

**依赖**：无。

**验证**：常量与 schema 在后续 Step 被引用时回归校对。

**风险**：无。

---

## Step 1: 收件箱基础（maildir 式 inbox/delivered）

**文件**：新增 `<worktree>/tools/model_proxy/core/inbox.py`

**改动**：
- `InboxStore` 类，持 `config_path.parent / "messages" / "inbox"` 与
  `config_path.parent / "messages" / "delivered"` 两目录（与 v3.1 §11 item G 一致，
  `config/messages/` 层）。
- `append(session_id, msg_dict)`：append 到 `messages/inbox/<sid>.jsonl`（每行一条 JSON）。
- `claim(session_id)`：maildir rename 原子认领——把 `messages/inbox/<sid>.jsonl` rename
  到 `messages/delivered/<sid>.<ts>.<pid>.jsonl`（tmp 中间名），返回消息列表。多调用方
  竞态只一方 rename 成功，另一方 `FileNotFoundError`。
- `peek_pending(session_id)` → `list[dict]`：只读不认领（check 路径用）。
- `peek_for_agent(session_id)` → `(to_agent_msgs, to_human_msgs)`：按 `msg_type` 字段分拣。
- `archive(session_id, msg)`：归档到 `messages/delivered/`。
- `list_sessions()` → `list[str]`：列出 inbox 有待投消息的 session id。
- 线程安全：`threading.Lock`，与 `SessionOverridesSidecar` 同款模式。

**依赖**：无（基础设施）。

**TDD**：先写 `tests/test_inbox.py`。

**验证**：
```bash
cd <worktree>/tools/model_proxy && python3 -m unittest tests.test_inbox -v
```
- `append` → `claim` → 消息不丢不重。
- 并发 `claim`（两个线程同时 rename）只一方成功。
- `peek` 不认领（多次调用结果一致）。
- 落盘路径含 `messages/` 层（`config/messages/inbox/...`、`config/messages/delivered/...`）。

**风险**：
- `rename` 跨文件系统不成（同目录内 rename 原子，inbox/delivered 须在同一卷——本工具
  `config_path.parent/messages/` 同目录，满足）。
- JSONL append 非原子（单行 `write + flush`，崩溃可能半行——缓释：append 前序列化为一行，
  `write(line + "\n")` 一次性；崩溃留半行，读时 `json.loads` 失败行跳过 + warning）。

---

## Step 2: "见过的 session" 表 + 短 id 前缀匹配

**文件**：新增 `<worktree>/tools/model_proxy/core/session_registry.py`（或并入 `inbox.py`
同模块，视实现偏好——推荐独立文件，职责单一）。

**改动**：
- `SeenSessions` 类：`{session_id: last_seen_epoch}` 纯内存 dict + `threading.Lock`。
- `touch(session_id)`：更新 last_seen。
- `resolve_short_id(prefix)` → `(status, full_id_or_candidates)`：
  - 唯一命中 → `("ok", full_id)`。
  - 零命中 → `("unknown", None)`。
  - 多命中 → `("ambiguous", [candidate_list])`。
- 前缀长度对齐 status active-sessions 前 8 位惯例。

**依赖**：无（基础设施）。

**TDD**：先写 `tests/test_session_registry.py`。

**验证**：
```bash
cd <worktree>/tools/model_proxy && python3 -m unittest tests.test_session_registry -v
```
- touch 后 resolve 命中。
- 前缀短至 1 字符，唯一命中补全。
- 多 session 共同前缀 → ambiguous。
- 零命中 → unknown。

**风险**：
- 进程重启后"见过的 session"清空——短 id 匹配失效。设计 §3 明示"本进程启动以来未见过
  该 session"，回执明示。属预期行为。

---

## Step 3: commands.py `$message` 解析层

**文件**：`<worktree>/tools/model_proxy/core/commands.py`

**改动**：
- 新增 `MESSAGE_CMD_PREFIX = "$message"`。
- 新增 `parse_message_command(content)` → `(is_cmd, sub_cmd, args)`：
  - 复用 `last_text_block` + `strip_trailing_context`。
  - 单行约束保留。
  - 首 token 精确等于 `$message`。
  - 内容豁免 token≤3 上限（消息内容含空格，`$message send to-agent <id> <内容...>` 的
    内容部分不截断）。
  - sub_cmd 取第二个 token（`send` / `check` / `listen` / 裸命令 None）。
  - args 取剩余部分：
    - send 时含 `<to-agent|to-human> <id> <内容>`。
    - **listen 无 args**（v1.1：listen 已是双重动作的"开"，无 on/off 参数，见 Step 6）。
- `extract_last_user_message_content` 复用（已存在）。
- 在 `_forward` 拦截点（`server.py:1252-1258`）扩展：`parse_route_command` 不命中 → 试
  `parse_message_command`；命中 → 走 `_handle_builtin_command` 的 message 分支。

**依赖**：无（纯解析，不碰收件箱）。

**TDD**：先写 `tests/test_message_parse.py`。

**验证**：
```bash
cd <worktree>/tools/model_proxy && python3 -m unittest tests.test_message_parse -v
```
- `$message send to-agent abc123 你好` → `(True, "send", ["to-agent", "abc123", "你好"])`。
- `$message check` → `(True, "check", [])`。
- `$message listen` → `(True, "listen", [])`（无 args，v1.1）。
- `$message` → `(True, None, [])`（裸命令）。
- `请你 $message send` → `(False, ...)`（句中提及不误命中）。
- 多行内容 → `(False, ...)`（单行约束）。
- 句中提及 `$route` 回归（已有测试 `test_command_match_rules.py` 不回归）。

```bash
cd <worktree>/tools/model_proxy && python3 -m unittest tests.test_command_match_rules tests.test_route_command -v
```

**风险**：
- 解析层 `$message` 与 `$route` 前缀分派在同一拦截点，需保证两个 parse 互斥（先试
  `$route`，不中再试 `$message`，或统一一个分派函数按首 token 路由）。推荐后者：
  新增 `dispatch_parse(content)` 统一入口，按首 token 查表。

---

## Step 4: `$message send` handler（非 splice 部分）

**文件**：`<worktree>/tools/model_proxy/core/commands.py`

**改动**：
- `CommandContext` 扩展 `__slots__`：加 `inbox: InboxStore`、`seen_sessions: SeenSessions`、
  `listen_registry: ListenRegistry`（Step 6 实现，此处先占位类型）。
- 新增 `handle_message_send(ctx) -> CommandResult`：
  - 解析 args：`<to-agent|to-human> <target_id> <content...>`。
  - from = `ctx.session_key`（发送方 session_id，零成本）。
  - 自环拒绝：`from == target_id` → 回执"不能给自己发消息"。
  - 短 id 前缀匹配：`ctx.seen_sessions.resolve_short_id(target_id)` → 唯一补全 /
    零命中按字面存 + 回执警示 / 多命中报错列候选。
  - 超长拒绝：content > 2000 字符 → 回执拒绝。
  - 写收件箱：`ctx.inbox.append(resolved_target_id, msg_dict)`，`msg_dict["source"]`
    取当前请求的 source 字段（由 proxy 在门控处记入 `ctx`）。
  - 回执：`已向 session <短 id> 投递消息（from=<自身短 id>）。对方 $message check 或
    下次 prompt 时投递。`。
  - `wrote=True`（发生了 inbox 写入，供 ACCESS 记录）。
- 新增 `handle_message_status(ctx)`（裸 `$message` 命令）：
  - 展示本 session 的投递状态 + inbox 待收条数 + help 简述。
  - `wrote=False`。
- 注册到 `COMMAND_HANDLERS`：`MESSAGE_CMD_PREFIX` → 分派到 send/check/status/listen 子
  handler（或统一 `handle_message_command` 内部按 sub_cmd 分派）。

**依赖**：Step 1（收件箱）、Step 2（session 表 + 短 id）、Step 3（解析）。

**TDD**：先写 `tests/test_message_command.py`。

**验证**：
```bash
cd <worktree>/tools/model_proxy && python3 -m unittest tests.test_message_command -v
```
- send to-agent 正常写 inbox。
- send to-human 正常写 inbox。
- 自环拒绝。
- 超长拒绝。
- 短 id 唯一补全、零命中警示、多命中报错。
- 裸 `$message` 展示状态。

**风险**：
- `CommandContext` 扩展 `__slots__` 会影响现有 `$route` handler 的构造调用
  （`server.py:1880-1888`）——需同步补传新参数，或用关键字参数默认值占位。推荐关键字
  默认 `None`，`$route` handler 不碰新字段即可。

---

## Step 5: `$message check` handler（无信 / to-human 合成响应 + 刷新 listen 窗口）

**文件**：`<worktree>/tools/model_proxy/core/commands.py` + `core/server.py`

**改动**：
- 新增 `handle_message_check(ctx) -> CommandResult`：
  - 查本 session 收件箱：`ctx.inbox.peek_for_agent(ctx.session_key)` →
    `(to_agent, to_human)`。
  - **无信**：回执"无新消息"，`wrote=False`。走合成完整响应路径（复用
    `_write_builtin_stream_response`）。
  - **to-human**：回执即消息原文（可能多条，拼接），`wrote=False`。同上合成完整响应。
  - **to-agent 且 splice 未实现**（本步占位）：回执"有 N 条 to-agent 消息，注入通道尚未
    启用（splice 未实现），请 `$message listen` 后由 b 通道注入"。`wrote=False`。
    注：占位回执用 "listen"，非 "listen on"（v1.1：命令已无 on/off）。
  - **to-agent 且 splice 已实现**（Step 7-11 完成后切换）：走注入 + splice 路径，
    `wrote=True`。
  - **刷新 listen 窗口**（v1.1 补，落 v3.1 §5 "每次 check 刷新 listen_until"）：
    `handle_message_check` 处理后（无论是否无信），执行
    `ctx.listen_registry.refresh(ctx.session_key)` →
    `listen_until[session_key] = now + LISTEN_WINDOW`。窗口刷新与 check 处理独立，
    不依赖 check 命中 to-agent。
- `server.py` `_handle_builtin_command` 扩展：按命令前缀分派到 route / message handler；
  message handler 内部按 sub_cmd 分派。

**依赖**：Step 1（收件箱 peek）、Step 4（CommandContext 句柄）、Step 6（ListenRegistry.refresh）。

**TDD**：先写 `tests/test_message_check.py`（无信、to-human 路径完整；to-agent 占位回执；
窗口刷新断言）。

**验证**：
```bash
cd <worktree>/tools/model_proxy && python3 -m unittest tests.test_message_check -v
```
- 无信 → 合成"无新消息"响应。
- to-human → 合成响应内容即消息原文。
- to-agent → 占位回执（splice 实现后改测）。
- check 后 `listen_until[session_key]` 被刷新到 `now + LISTEN_WINDOW`（v1.1 补断言）。
- ACCESS 日志断言不在本步——移到 Step 14 验证（v1.1：删本步的
  `builtin=message_check` ACCESS 字段断言）。

**风险**：
- to-agent 路径在 splice 实现后行为变化——测试需同步更新。缓释：占位回执测试标
  `@unittest.expectedFailure` 或独立 test case，splice 实现后替换。
- check 刷新窗口使"listen 后只要持续 check 就一直 b 可注入"——属预期（v3.1 §5 滑窗语义），
  cron 6 空自动停后不再刷新，窗口自然超时。

---

## Step 6: listen 拦截点（proxy 拦截 + 滑动窗口，无显式 off）

**文件**：`<worktree>/tools/model_proxy/core/commands.py`（ListenRegistry）+
`core/server.py`（拦截点）

**改动**：
- 新增 `ListenRegistry` 类（可并入 `session_registry.py`）：
  - `listen_until: dict[str, float]`（session_id → epoch 截止时间，v1.1 改：不再是
    `set[str]`，改为带截止的滑动窗口）。
  - 常量 `LISTEN_WINDOW = 3600`（秒，与 Step 0 一致，≥ 60min 对齐 cron 6 空自动停）。
  - `set_listen(session_id)`：`listen_until[session_id] = now + LISTEN_WINDOW`（开窗）。
  - `refresh(session_id)`：同 `set_listen`，语义别名（check 路径用，Step 5 调）。
  - `is_listening(session_id) -> bool`：`now < listen_until.get(session_id, 0)`，窗口超时
    自动 False，无显式 off（v3.1 §5.3）。
  - 线程安全。
- **`$message listen` 即 proxy 拦截点**（v1.1 改，落 v3.1 §5.1 双重动作第 1 条）：
  - handler 记 `listen_until[session_key] = now + LISTEN_WINDOW`，合成确认响应、不转发
    上游。
  - **删 v1.0 的 `_listen_notify` 命令、删 `handle_message_listen_notify`、删相关验证**
    （v1.1：listen 拦截直接落在 `$message listen` 本身，不再要 agent 回发内部命令通知）。
- agent 侧 CronCreate 部分仍由 skill 教 agent 自己完成（双重动作第 2 条，不在本步代码内）。

**依赖**：Step 2（session 表）、Step 3（解析）、Step 4（CommandContext）。

**验证**：
```bash
cd <worktree>/tools/model_proxy && python3 -m unittest tests.test_message_listen -v
```
- `$message listen` → `is_listening(sid)` 返回 True。
- 模拟时间推进超 `LISTEN_WINDOW` → `is_listening(sid)` 返回 False（v1.1 改：窗口超时自动
  失效，无显式 off）。
- `handle_message_check` 后窗口被刷新（与 Step 5 联动，跨 step 测试或集成测试覆盖）。
- listen 状态在进程内持久（不落盘——设计 §5.2 硬约束，proxy 推不进存活 session，重启后
  agent 重新 `$message listen`）。

**风险**：
- proxy 重启后 `listen_until` 清空——属预期（设计 §5.2），agent 侧 `durable:true`
  CronCreate 会重新触发 check → 但 check 刷新窗口要求 `listen_until` 已存在基线。
  缓释：Step 5 的 check refresh 对未存在基线的 session 也会写入新窗口（refresh 即
  set_listen 语义），因此 cron 触发的首次 check 即恢复 listen 态。需 README/skill 说明
  "重启后首次 check 自动恢复 listen 窗口"。

---

## Step 7: 请求注入 `_maybe_inject_message`（按 source 分发，R3 先认领后注入）

**文件**：`<worktree>/tools/model_proxy/core/server.py`

**改动**：
- 在 `_forward` 内、source 门控后、route 选择前（参照 `_rewrite_known_injected_texts`
  位置 ~1237-1241），新增注入逻辑。
- 新增 `_maybe_inject_message(body_json, session_key, listen_state, inbox, source) -> bool`
  （v1.1：签名加 `source` 参数，按 source 分发）：
  - 条件：`listen_state.is_listening(session_key)` + inbox 有 to-agent 待投消息。
  - 动作（R3 先认领后注入）：
    1. `inbox.claim(session_key)` 原子认领（maildir rename，防 b/check/hook 竞态）。
    2. 取 to-agent 消息，按 `source` 分发（v1.1）：
       - `source == "anthropic"` → 调 `_inject_anthropic`（现有逻辑：追加 text block 到
         最后一条 user 消息 content）。
       - `source == "responses"` → 调 `_inject_responses`（首版 **TODO + log + 不注入跳过、
         不报错**，与 v3.1 §6.2 "responses 入站首版不做" 一致）。
       - `source == "chat"` → 不支持（chat 入站 501 在前，不会走到这里）。
    3. 注入包装格式（**anthropic source 专属**，v1.1 标注）：
       ```
       [proxy 投递的跨 session 消息 — 非本会话用户指令]
       from: <发送方短 id>（完整 id: <full>）
       类型: query|guidance
       内容: <消息正文>
       回复请用: $message send to-agent <from-id> <回复内容>
       ```
    4. 追加后重序列化 `raw_body`（与 nudge 改写同款）。
  - **"找最后 user 消息"helper 抽出共用**（v1.1）：anthropic / responses 两侧都需找最后
    user 消息，抽 `_last_user_message(body_json, source)` 共用 helper；形态处理
    （anthropic content string→list vs responses 结构）**不合并**，各 source 自行处理
    content 形态。
  - 多条消息拼接为多个 text block（便于 agent 区分）。
  - 返回是否注入（供 `_acc` 记 `message_injected=1`）。
- `body_json` content 形态处理：string 形态转 list 形态后追加（与 nudge 改写一致）。
- `_acc` 加 `message_injected` 字段。

**依赖**：Step 1（claim）、Step 6（listen 滑动窗口）。

**TDD**：先写 `tests/test_message_inject.py`。

**验证**：
```bash
cd <worktree>/tools/model_proxy && python3 -m unittest tests.test_message_inject -v
```
- listen 窗口内 + 有 to-agent 消息 + source=anthropic → 注入后 body 最后 user 消息 content
  含追加 text block。
- listen 窗口超时 → 不注入。
- 无消息 → 不注入。
- 注入不插新 user message（messages 长度不变）。
- claim 后消息从 inbox 移除（防重复注入）。
- 多条消息全部注入。
- **source=responses + 有待投 → 不注入 + log warning + 不阻断转发**（v1.1 补：responses
  入站首版不做注入，转发原样继续）。

**风险**：
- 注入包装被模型误解为本会话用户指令——缓释：包装格式显式标注"非本会话用户指令"，
  设计 §15 prompt-injection 面。
- **注入失败 vs splice 失败边界**（v1.1 钉死，落 M3）：
  - **注入失败**（请求侧，本步）：claim 后 body 改写异常（如 body 不可变、json 改写抛
    异常）。回执路径：合成"投递失败"回执、不阻断转发（原 body 照发）；消息已在
    `delivered/` 归档不丢。log warning + `_acc["message_injected"]=0`。
  - **splice 失败**（响应侧，Step 8-11 / O3 / item F）：消息已注入请求、模型已看到，
    splice 回执注入响应流时异常。降级：放弃回执，不重投。**不是**注入失败。
  - 两者边界：注入失败=模型没看到消息；splice 失败=模型已看到、回执没出来。Step 7
    风险栏只管注入失败；Step 14 风险栏管 splice 失败。
- 续轮请求（tool_result 后的下一轮）也会触发注入——只要 B 还在 listen 窗口内 + 有新消息，
  每次请求都注入。属预期（b 通道持续投递）。

---

## Step 8: splice 3b/4b 非流式（ANTHROPIC_TO_CHAT / RESPONSES_TO_ANTHROPIC 非流式）

> v1.1 方向澄清（M4）：本步的 4b = anthropic 入站 + RESPONSES_TO_ANTHROPIC 转换（出站
> anthropic 格式），属 v3.1 §6.2 "先行/次之"层，**首版做**。responses **入站**
> （Codex `/v1/responses`）的 splice 标"首版不做，留 TODO"，与 Step 7 source 分发一致。

**文件**：`<worktree>/tools/model_proxy/core/translate.py`（`openai_to_anthropic_response`
~625、`responses_to_anthropic_response` ~1939）+ `core/server.py`

**改动**：
- `server.py` PASSTHROUGH 非流式分支（~1677-1703）之后的 `ANTHROPIC_TO_CHAT` /
  `RESPONSES_TO_ANTHROPIC` 非流式分支：转换后 json，若 `_acc["message_injected"]`，
  在 `content` 数组末尾追加 text block。
- splice 回执 block：
  ```json
  {"type": "text", "text": "[proxy 投递回执: 已向 session <sid> 注入 N 条消息]", "index": <max_index+1>}
  ```
  —— 或更简洁：`[收到 <from-短-id> 留言: <内容摘要>]`（设计 §6.2）。
- `max_index` 从转换后 json 的 content 数组里找 `max(item["index"])` +1。
- `stop_reason` 不变（非流式转换后已确定）。
- `translate.py` 两个转换函数本身不改——splice 在 server.py 转换后追加，保持转换函数纯净。
- `_acc` 加 `splice_done` 标志。

**依赖**：Step 7（`_acc["message_injected"]` 标志）。

**验证**：
```bash
cd <worktree>/tools/model_proxy && python3 -m unittest tests.test_splice_nonstream -v
```
- `ANTHROPIC_TO_CHAT` 非流式 + injected → 转换后 content 末尾有 splice block，index 单调。
- `RESPONSES_TO_ANTHROPIC` 非流式 + injected → 同上。
- 未注入 → 无 splice block（回归）。
- index 连续性校验（splice block index = 原 max +1）。

**风险**：
- 转换后 json 的 content 可能无 index 字段（responses 协议）——需补 index 字段或按数组
  长度推断。

---

## Step 9: splice 3a/4a 流式（adapter finalize 注入）

> v1.1 方向澄清（M4）：本步的 4a = anthropic 入站 + RESPONSES 转换流式（出站
> anthropic 格式），属 v3.1 §6.2 "次之"层，**首版做**。responses 入站流式 splice
> 首版不做。

**文件**：`<worktree>/tools/model_proxy/core/translate.py`
（`OpenAIToAnthropicStreamAdapter.finalize` ~980、`ResponsesToAnthropicStreamAdapter.finalize`
~2253）+ `core/server.py`

**改动**：
- adapter 构造时或 feed 过程中接收 `splice_pending: bool` + `splice_text: str` 标志
  （由 server.py 传入，来源 `_acc["message_injected"]`）。
- `finalize()` 末尾，若 `splice_pending`：
  1. 取 `self.cur_index`（adapter 已跟踪的最大 index）+1。
  2. 追加事件：`content_block_start(text, new_index)` →
     `content_block_delta_text(new_index, splice_text)` → `content_block_stop(new_index)`。
  3. 在 `message_delta` 事件**前**注入（设计 §6.2：在 message_delta 前注入回执 block）。
  4. `message_delta` + `message_stop` 正常发出。
- server.py `_write_translated_stream` / `_write_translated_stream_from_responses`：
  构造 adapter 时传入 `splice_pending`。

**依赖**：Step 7（injected 标志）、Step 8（index 跟踪经验）。

**验证**：
```bash
cd <worktree>/tools/model_proxy && python3 -m unittest tests.test_splice_stream -v
```
- `OpenAIToAnthropicStreamAdapter` finalize + splice_pending → 末尾有 splice block 事件，
  index 单调。
- `ResponsesToAnthropicStreamAdapter` finalize + splice_pending → 同上。
- 无 splice_pending → finalize 原行为（回归，`test_translate.py` 全绿）。
- splice block 在 message_delta 前注入。
- index 单调性校验。

```bash
cd <worktree>/tools/model_proxy && python3 -m unittest tests.test_translate -v
```

**风险**：
- `cur_index` 跟踪：adapter 在 thinking 交错、多 tool_use 场景下 index 跳跃，需确保 finalize
  时 `cur_index` 是真实最大值。已有 `produced_content_block` 标记，需确认 `cur_index` 同步更新。
- thinking block 的 index 也要计入（thinking block 也是 content block）。
- finalize 被多次调用（流意外结束补收尾）——splice 要幂等（`splice_done` 标志防重复）。

---

## Step 10: splice 1b PASSTHROUGH 非流式

> v1.1 方向澄清（M4）：responses→responses 是否支持首版不做，留 TODO。

**文件**：`<worktree>/tools/model_proxy/core/server.py`

**改动**：
- PASSTHROUGH 非流式分支（~1677-1703）：`resp_body = resp.read()` 后，若
  `_acc["message_injected"]`：
  1. `json.loads(resp_body)` → 找 content 数组 max index → 追加 splice text block →
     `json.dumps` → `_write_buffered_response`。
  2. usage 嗅探逻辑不变（splice 在 usage 嗅探后）。
- anthropic→anthropic 与 responses→responses 两 source 的 content 结构可能不同——
  anthropic content 是 `[{type, text, index}]`，responses 是不同结构。需按 source 分支处理。
  - responses→responses PASSTHROUGH 非流式：responses 协议的 content 结构追加 splice——
    可能不兼容（responses 的 output item 结构）。**v1.1 钉死：首版不做，标 TODO + log
    warning + 不追加 splice**（与 Step 7 source 分发一致：responses 入站首版不做）。

**依赖**：Step 7（injected 标志）。

**验证**：
```bash
cd <worktree>/tools/model_proxy && python3 -m unittest tests.test_splice_passthrough_nonstream -v
```
- anthropic→anthropic PASSTHROUGH 非流式 + injected → content 末尾有 splice block。
- 未注入 → 原样透传（回归，`test_passthrough_sniff.py` 全绿）。
- Content-Length 头同步更新（splice 后 body 变长）。
- responses→responses PASSTHROUGH 非流式 + injected → 不追加 splice + log warning + 转发
  不中断（v1.1 补）。

```bash
cd <worktree>/tools/model_proxy && python3 -m unittest tests.test_passthrough_sniff -v
```

**风险**：
- Content-Length 头需重算（splice 改变了 body 长度）——`_write_buffered_response` 用
  `len(body)` 设 Content-Length，splice 后 body 变长，需确保传给 `_write_buffered_response`
  的是 splice 后的 body。
- responses→responses 的 content 结构差异——v1.1 钉死首版不做。

---

## Step 11: splice 1a PASSTHROUGH 流式（最硬，放最后）

> v1.1 方向澄清（M4）：responses→responses 流式首版不做，留 TODO。

**文件**：`<worktree>/tools/model_proxy/core/server.py`（`_write_streaming_response` ~2054）

**改动**：
- `_write_streaming_response` 从"字节透传 + 旁路嗅探"升级为"事件级透传 + 注入"：
  1. 按 `\n\n` 切 SSE 事件块（已有 `sniff_buf` 逻辑 ~2069-2087，复用切分）。
  2. 每个块 `_parse_anthropic_sse_block` 解析 → `event_type` + `data`。
  3. 跟踪 `max_index`：`content_block_start` 事件的 `index` 字段 →
     `max_index = max(max_index, index)`。
  4. 在 `message_delta` 事件**前**，若 `_acc["message_injected"]`：
     - 注入 splice 事件序列：`content_block_start(text, max_index+1)` →
       `content_block_delta_text(max_index+1, splice_text)` →
       `content_block_stop(max_index+1)`。
  5. 透传字节本身不变（每个块原样写出），splice 事件在 message_delta 块写出前追加写出。
- 旁路嗅探逻辑（`_sniff_passthrough_usage`）保留——它已按块解析，与事件级透传共存。
- responses→responses PASSTHROUGH 流式：v1.1 钉死首版不做，标 TODO + log warning。
- **M1 幂等**（v1.1 补）：`_splice_injected` 一次性闸门——PASSTHROUGH 流式分支在
  `message_delta` 前注入一次后设 `splice_done=True`，后续（含 finalize 收尾、message_stop
  前）不再注入。与 Step 9 的 `splice_done` 标志对齐。

**依赖**：Step 7（injected 标志）、Step 8-10（index 跟踪经验）。

**TDD**：先写 `tests/test_splice_passthrough_stream.py`（合成上游 SSE 流，含多 content_block、
thinking 交错、tool_use）。

**验证**：
```bash
cd <worktree>/tools/model_proxy && python3 -m unittest tests.test_splice_passthrough_stream -v
```
- 合成上游流（`message_start` → 多个 `content_block_start/delta/stop` → `message_delta` →
  `message_stop`）+ injected → splice block 在 message_delta 前，index 单调。
- thinking 交错场景（`thinking` block + `text` block 混合）→ splice index 正确。
- 多 tool_use 场景 → splice index 正确。
- 未注入 → 字节透传不变（回归）。
- 透传字节完整性（`_decode_chunked` 还原后与上游原始 body 一致，splice 部分除外）。
- **幂等**（v1.1 补）：finalize 被多次调用（流意外结束补收尾）→ splice 只注入一次
  （`splice_done` 闸门），不重复。

**V7 真实上游流式回归**（设计验证方式 V7）：
```bash
# 沙箱：真实上游流式 + injected，核对 splice block 可见性
# 具体脚本在 Step 11 实现时编写，参考 /tmp/splice-test 的独立中间层模式
```

**风险**：
- **动核心管道**：`_write_streaming_response` 是 PASSTHROUGH 流式主路径，改动影响面大。
  缓释：先写单测覆盖回归，再改。
- 字节边界：上游 chunk 可能切在 SSE 事件块中间（`sniff_buf` 已处理跨 chunk 边界，事件级
  透传复用同一 buffer 逻辑）。
- index 跟踪：`content_block_start` 的 `index` 字段必须解析到——若上游不发 `index`
  （某些实现省略），按数组顺序推断。
- `message_delta` 可能在多个 chunk 之间（跨 chunk 边界）——splice 注入时机需在
  "完整 message_delta 块写出前"。
- responses→responses 流式：responses 协议无 `content_block` 概念，splice 机制不同——
  v1.1 钉死首版不做。

---

## Step 12: hooker/deliver_messages.sh（通道 a UserPromptSubmit + SessionStart）

> v1.1 改（已知点 3）：输出方式钉死纯 stdout 首选；resume 已验证触发 SessionStart，
> 删 R1 fallback 措辞；补 stdout 进 system prompt reminder 区；补 resume 同 cwd 约束；
> 补可执行单测。

**文件**：新增 `<worktree>/tools/model_proxy/hooker/deliver_messages.sh`

**改动**：
- **UserPromptSubmit 分支**（通道 a，§4a）：
  - hook 输入含 `session_id` / `transcript_path`（设计 §3）。
  - 查收件箱：`messages/inbox/<sid>.jsonl` 是否有待投消息。
  - 有 → `claim` 认领 → 输出注入包装到 **stdout**（CC hook 机制：UserPromptSubmit 的
    stdout 进 **system prompt reminder 区**，不进 user message.content——v1.1 补）。
  - 无 → 静默退出。
- **SessionStart 分支**（通道 4d，§4d）：
  - hook 输入含 `session_id`。
  - 查收件箱：有 to-agent / to-human 待投 → `claim` 认领 → 输出注入包装到 stdout
    （SessionStart 的 stdout 作为 session 初始 context，同样进 reminder 区）。
  - **resume 场景**（v1.1 改）：`--resume` 触发 SessionStart（source="resume"）**已验证
    有效**（R1），**不需要 fallback**。删 v1.0 的 "R1 resume fallback" 措辞。Step 12
    标题改 "通道 a UserPromptSubmit + SessionStart"（删 "+ R1 resume fallback"）。
  - **resume 须同 cwd 约束**（v1.1 补）：resume 时 CC 须以原 session 的 cwd 启动，
    hook 才能复用同一 `config/messages/` 路径推断。skill/README 提醒。
- 脚本读 model_proxy 的 config 路径（复用 `ensure_model_proxy.sh` 的路径推断模式）。
- 纯 bash + python3 内联（或调 model_proxy 的 inbox 模块）。
- additionalContext 备选（v1.1 标）：若 stdout 路径在某个 CC 版本失效，备选改用
  `additionalContext` 字段（需构造合法 JSON），但首版用 stdout。

**依赖**：Step 1（收件箱 claim）、Step 7（注入包装格式对称）。

**验证**：
```bash
# V1 端到端（通道 a）
# 沙箱起两个 claude --session-id，A 发 $message send to-agent <B> hi
# 断言：A 收回执；B 下一条 prompt 后 UserPromptSubmit hook 注入消息
# （transcript 可见包装文本）；inbox → delivered 归档

# V4 端到端（SessionStart）
# B 退出后 A 发消息 → B --resume → SessionStart 投递（已验证触发，不需 fallback）
```
- **可执行单测**（v1.1 补，不只注释式 V1/V4）：
  - hooker 的 claim 认领：python 驱动测试 bash 脚本，造 inbox 文件 → 跑脚本 → 断言
    stdout 含注入包装 + inbox 已归档到 delivered。
  - stdout 输出格式：断言输出为纯文本注入包装（无多余行、无 JSON 包裹），符合 system
    prompt reminder 区格式。
  - 空收件箱：断言 stdout 为空、退出码 0。

**风险**：
- UserPromptSubmit / SessionStart 的 stdout 注入机制已验证（v1.1 删 "需先测" 措辞）。
- resume 同 cwd 约束未满足时 hook 路径推断失败 → log warning + 静默退出（不阻断 CC
  启动）。

---

## Step 13: _install_ops.py hook 管理泛化（第 2 条 hook）

**文件**：`<worktree>/tools/model_proxy/_install_ops.py`

**改动**：
- `_is_model_proxy_hook` / `_normalize_session_start` 泛化到本工具第 2 条 hook
  （`deliver_messages.sh`）。
- 新增 `_is_deliver_messages_hook(entry)` 判定函数（command 含 `deliver_messages.sh`）。
- `ensure_session_hook` 扩展：同时确保 `ensure_model_proxy.sh` + `deliver_messages.sh`
  两条 hook 存在且正确。
- UserPromptSubmit 和 SessionStart 两个 hook 事件类型都要管理（`deliver_messages.sh`
  注册到这两个事件）。
- `_normalize_session_start` 改名或扩展为 `_normalize_hooks`，处理多个 hook 事件类型。

**依赖**：Step 12（deliver_messages.sh 存在）。

**验证**：
```bash
cd <worktree>/tools/model_proxy && python3 -m unittest tests.test_install_ops -v
```
- 两条 hook 都存在且正确。
- stale 路径清理。
- 重复 hook 去重。
- 回归现有 `ensure_model_proxy.sh` hook 管理不破坏。

**风险**：
- 现有 `_normalize_session_start` 逻辑只管 SessionStart 事件——UserPromptSubmit 事件是
  新增管理面，需新建 `_normalize_user_prompt_submit` 或扩展通用函数。
- 测试 `test_install_ops.py` 已有 26490 字节，扩展时注意回归。

---

## Step 14: ACCESS 日志扩展 + 投递元数据 + splice 失败降级（O3）

**文件**：`<worktree>/tools/model_proxy/core/server.py`

**改动**：
- `_acc` 新增字段：`message_cmd`（send/check/listen/裸命令）、`message_injected`（0/1）、
  `splice_done`（0/1）、`splice_failed`（0/1）——v1.0 已有，v1.1 在此之上补投递元数据
  日志（M2，落 v3.1 §11 item E）。
- ACCESS 日志格式扩展（~1174-1185）：加 `message_cmd=%s message_injected=%s
  splice_done=%s splice_failed=%s`（处理流程标志，v1.0 已有）。
- **M2 投递元数据日志**（v1.1 补，与处理流程标志并行，不替换）：
  - send 时记 `message_sent`(from / to / type / len)。
  - 注入 / splice 成功记 `message_delivered`(channel = a / b / poll / sessionstart)。
  - splice 时记 `message_spliced`(index)。
  - listen 时记 `listen`(session, until)。
  - 与 `message_cmd` / `message_injected` / `splice_done` / `splice_failed` 并行
    （前者是处理流程标志，后者是投递元数据事件，两类不互斥）。
- **splice 失败降级（O3，item F）**：
  - splice 任何环节异常（index 跟踪失败、json 解析失败、adapter finalize 异常）→
    log warning + `splice_failed=1` + **不阻断转发**。
  - 降级策略：splice 失败时按"未注入"处理，上游响应原样透传/转换，回执不追加。
  - **注意边界**（v1.1 钉死，落 M3）：item F 覆盖的是 **splice 失败**（响应侧，消息已
    注入模型已看到），**不是**注入失败（请求侧，Step 7 风险栏）。本步只管 splice 失败
    降级；注入失败回执在 Step 7。
- 所有 splice 入口（Step 8-11）加 try/except 兜底。

**依赖**：Step 7-11（splice 实现）。

**验证**：
```bash
cd <worktree>/tools/model_proxy && python3 -m unittest tests.test_message_access_log tests.test_splice_degradation -v
```
- ACCESS 日志含 message 处理流程标志（`message_cmd` 等）。
- ACCESS 日志含投递元数据事件（`message_sent` / `message_delivered` / `message_spliced` /
  `listen`，v1.1 补断言）。
- splice 异常注入 → `splice_failed=1` + 转发不中断。
- `builtin=message_check` ACCESS 字段断言（v1.1：从 Step 5 移到本步验证）。

```bash
# grep 自检
grep -n "message_cmd\|message_injected\|splice_done\|splice_failed\|message_sent\|message_delivered\|message_spliced\|listen=" <worktree>/tools/model_proxy/core/server.py
```

**风险**：
- splice 失败降级后用户无感知（SDK 页面无回执）——缓释：log warning + ACCESS 可审计；
  若实测咬人再加 agent-seen 中间态（设计 §11 简化理由）。

---

## Step 15: README `$message` 小节 + 安全提示

**文件**：`<worktree>/tools/model_proxy/README.md`（或对应文档）

**改动**：
- 新增 `$message` 命令族小节：send / check / listen / 裸命令用法。
- 安全提示（设计 §15 prompt-injection 面）：
  - 任何持有 client token 的客户端可向任意 session 投递，内容会被目标 session 的模型读到。
  - 与 `$route` 同级信任假设（能发请求即有 token），但后果从"改自己路由"扩大为"影响他人
    上下文"。
  - 缓释：包装格式显式标注"非本会话用户指令"；响应通道回执 transcript 可见可审计；
    delivered 归档留痕。
- listen 机制说明（agent 自建轮询，proxy 推不进存活 session）。
- 短 id 匹配、自环拒绝、超长限制、消息类型（query/guidance）说明。
- config/ 混合配置/运行时状态说明（`config/messages/` 是运行时状态）。
- **fork 后 listen 不继承**（v1.1 补，L2）：fork 产生新 session_id，不继承原 session 的
  listen 窗口和 cron。skill 文档提醒 agent fork 后需重新 `$message listen`。

**依赖**：所有功能代码（Step 1-14）。

**验证**：人工核对文档完整性。

**风险**：无。

---

## Step 16: proxy skill（skill-creator 落地，最后一步）

**文件**：新增 `<worktree>/.claude/skills/proxy/SKILL.md` + 可选 `references/` 子文件
（设计 §17.4 理想项）

**改动**：
- 用 skill-creator 创建 `proxy` skill。
- 覆盖设计 §17.3 正文结构：概念层 / 命令族 / A 侧流程 / B 侧流程 / listen 机制 /
  收信发信处理 / 卫生 / 示例。
- frontmatter description 见设计 §17.2。
- 可选子文件（理想项）：`references/listen-cron-template.md`、
  `references/message-wrapper-format.md`。
- **skill 卫生含 fork 后 listen 不继承**（v1.1 补，L2）：在 "卫生" 小节写明 fork 产生新
  session_id，需重新 `$message listen`。

**依赖**：所有功能代码（Step 1-15）。

**验证**：
```bash
# skill 触发测试：用户说"远程向另一 session 发消息" → proxy skill 激活
# skill-creator 落地后的验证流程
```

**风险**：无（纯知识层，不含 proxy 状态读写逻辑）。

---

## 依赖关系图

```
Step 1 (inbox)        ─┐
Step 2 (seen_sessions)─┼─→ Step 4 (send handler) ─→ Step 5 (check handler + 刷新窗口)
Step 3 (parse)        ─┘         │
                                 ├─→ Step 6 (listen 拦截 + 滑窗) ─→ Step 7 (inject by source) ─┬─→ Step 8  (splice 3b/4b 非流式)
                                 │                                                            ├─→ Step 9  (splice 3a/4a 流式)
                                 │                                                            ├─→ Step 10 (splice 1b PASSTHROUGH 非流式)
                                 │                                                            └─→ Step 11 (splice 1a PASSTHROUGH 流式, 幂等) ← 最重
                                 │
Step 1 (inbox)        ──────────────→ Step 12 (hooker) ─→ Step 13 (_install_ops)
Step 7 (inject)      ──────────────→ Step 12 (hooker)

Step 7-11 (splice)   ──────────────→ Step 14 (ACCESS 投递元数据 + 降级)
所有功能代码          ──────────────→ Step 15 (README) ─→ Step 16 (skill)
```

---

## 落地后回归

- **V7 真实上游流式 splice 回归**：当前 §9.2 实测为合成固定响应；Step 11 实现后需补测
  真实上游流式（index 跟踪、thinking 交错、多 tool_use 场景）。
- **V4 resume SessionStart**：Step 12 实现后验证 `--resume` 触发 SessionStart 投递
  （已验证触发，不需 fallback）。
- **端到端 A↔B 双 session 互发**：A `$message listen` → B 发消息 → A check/listen 收到 →
  A 回复 → B check/listen 收到。双向 listen 完整链路。
- **全量单测回归**：`cd <worktree>/tools/model_proxy && python3 -m unittest discover tests -v`
  全绿。
- **fail-open 回归**：不含命令的消息照常转发（`$route` 已有测试不回归，`$message` 新增
  测试不误命中普通消息）。

---

## 关联

- [[2026-08-10-proxy-message-inter-session-design]]——v3.1 设计文档，本计划的实施依据
  （工作树已同步到 v3.1）。
- [[2026-08-11-proxy-message-splice-feasibility]]——splice 回执可行性调研，§9.2 实测证据。
- [[2026-08-04-in-band-route-command-design]]——命令层骨架、§7.3 边界约束、fail-open 原则。
