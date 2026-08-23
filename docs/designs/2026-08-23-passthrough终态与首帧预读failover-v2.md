---
type: design-decision
status: confirmed
target: "[[tools/model_proxy]]"
tags:
  - architect
  - model-proxy
  - streaming
  - failover
  - observability
---

# 背景与问题

PASSTHROUGH 流当前把上游 EOF 直接当正常结束，未验证协议终态；200 空流因此被记成成功并让下游永久等待。本设计替代 [[tools/model_proxy/docs/designs/2026-08-23-passthrough终态与首帧预读failover]]：补齐 PASSTHROUGH 终态约束，并把“响应提交前可 failover”抽成四条流路径共用的首帧预读契约。

已核实现状：

- 主 attempt 循环在 `urlopen()` 返回 HTTP 响应后才分派写回；HTTPError/网络错误可在提交客户端响应前 cooldown+failover（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:1952-2017`）。
- PASSTHROUGH 流式分支直接进入 `_write_streaming_response()`，返回后即结束本请求，没有“writer 请求重试”的返回契约（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:2019-2035`）。
- `_write_streaming_response()` 进入即提交状态码和 chunked 响应头；上游 EOF 无条件写 chunked 终止符（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:2437-2472`）。
- PASSTHROUGH 嗅探异常被吞掉，残余 buffer 只在 finally 再尝试一次，嗅探失败不改变写回结果（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:2463-2481`）。
- 现有嗅探只处理 usage、stop_reason 和正文标记，不维护统一终态；Anthropic 未识别 `message_stop`，Responses 未识别 `response.failed`（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:2710-2762`）。
- SSE 块切分只认 `\n\n`，未覆盖标准 `\r\n\r\n`；解析器虽兼容行尾 `\r`，但 framing 不兼容（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:2465-2468,2605-2608,2665-2668,2765-2789`）。
- Responses→Anthropic 当前明确兼容只有 `data:`、没有 `event:` 的流，并以 `data.type` 兜底事件类型（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:2649-2655,2787-2789`）。
- 三条转换 writer 同样进入即提交 200；异常只能在已提交后补协议错误事件，不能换 supply（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:2506-2519,2530-2708`）。
- 转换 adapter 已具备终态映射和异常 EOF fail-fast，三协议共用 `TerminalState/TranslationError`（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/translate.py:121-205`）；各 adapter 的 `finalize()` 对无终态 EOF 产出失败（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/translate.py:1109-1140,1729-1738,2532-2541`）。
- `tried_set` 在每个 route 内重建；同一 supply 被多个 route 引用时，route failover 会再次选择它（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:1742-1775`）。
- ACCESS 的 `status` 表示客户端 HTTP 状态；`UsageTotalsStore` 仅以 `status == 200` 判成功，流内失败会被算成功（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:331-357,1360-1405`）。
- active sessions 解析目前没有流完整性字段，旧健康口径主要依赖 HTTP status（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/_format_ops.py:200-250,338-420`）。
- status 的跨协议提醒只统计 `conversion_kind != passthrough`；PASSTHROUGH 链路异常不会出现（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/_format_ops.py:253-323`）。
- upstream timeout 是整条上游连接统一值，默认 1800 秒，没有首事件 deadline（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:423-425,517-519,1952-1954`）。

# 方案设计

## 1. 目标架构与分期

采用“共享流门卫、协议解析分层”方案：新增固定内存 SSE framer、PASSTHROUGH tracker 与 `StreamProbeResult`；四路统一执行“预读首个业务事件→判定→提交→回放→续读”。PASSTHROUGH 始终回放原始字节，不做响应转换。

分两期：

- **P0′立即止血**：PASSTHROUGH 终态跟踪、无终态显式 error、ACCESS/账本口径修正。不改变提交时点，因此不能 failover。
- **P1′完成终态化**：四路统一首帧预读、提交前 failover、30 秒 deadline、每 attempt 256 KiB 总预算、请求级坏 supply 排除。

P0′组件必须直接成为 P1′组件，不写一次性布尔补丁。

已确定参数：

```json
{
  "stream_probe": {
    "first_event_timeout_seconds": 30,
    "max_buffer_bytes": 262144,
    "stream_failure_cooldown_seconds": 0
  }
}
```

`cooldown=0` 的准确语义是：不写跨请求 cooldown store，但在**本请求全部 route** 中排除已发生 probe 失败的 supply。

## 2. 公共终态与 SSE framing

### 2.1 终态只复用现有体系

在 `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/translate.py` 增加 `PassthroughTerminalTracker`，内部继续使用现有 `TerminalState/TerminalStatus/TranslationError`。

两维状态严格分离：

- `stream_integrity = valid | invalid | client_disconnect`
- `terminal_status = open | completed | incomplete | refused | paused | failed`

`stream_integrity`回答“是否收到协议合法、完整的终态”；`terminal_status`回答“业务为何终止”。合法的 `completed/incomplete/refused/paused/failed` 都是 `stream_integrity=valid`。无终态 EOF、坏帧、未知终止原因是 `stream_integrity=invalid`。

Anthropic tracker：

1. `message_start.message.usage.input_tokens` 补采输入 token；现有只看 `message_delta` 会使 `usage_in` 常为 0。
2. `message_delta.delta.stop_reason` 用 `map_anthropic_terminal()` 映射候选业务终态；同步增量覆盖 output/cache/reasoning usage 字段。
3. 只有随后收到 `message_stop` 才确认协议终态。`message_stop` 前没有合法 stop_reason 为 `missing_stop_reason`。
4. `event:error` 是合法失败终态：`stream_integrity=valid, terminal_status=failed`，不要求 `message_stop`。
5. `pause_turn` 映射为 `terminal_status=paused`，属于合法完整终态。
6. 未知**非终态** Anthropic 事件：PASSTHROUGH 原样容忍并继续，保证前向兼容；转换 adapter 保持现有 allowlist，可按转换能力拒绝。
7. 未知 stop_reason 仍由 `map_anthropic_terminal()` fail-fast。

Responses tracker：

1. `response.completed/incomplete` 用 `map_responses_terminal()`；`response.failed/error` 用 `classify_upstream_error()`。
2. `completed/incomplete/failed/error` 均可确认协议终态；其中 failed/error 的 `terminal_status=failed`。
3. 未知非终态事件在 PASSTHROUGH 原样容忍；未知终态 status 仍 fail-fast。

EOF：

- 已确认终态：保持现有终态，后续 read 异常不得覆盖。
- 无确认终态：空流为 `empty_stream`，其余为 `unexpected_eof`。
- 只有注释/空白后 EOF 仍是 `empty_stream`。

### 2.2 SSE 事件类型规则

事件类型解析顺序固定：

1. 优先读取 SSE `event:`。
2. 缺 `event:` 时，允许合法 JSON 的 `data.type` 兜底；这是 Responses 现有兼容行为，必须保留。
3. `event:` 与 `data.type` 同时存在但不一致，判 `malformed_stream`。
4. Chat 的 `data: [DONE]` 是合法终止哨兵，不做 JSON 解析；其业务终态仍要求 adapter 此前收到合法 `finish_reason`。
5. 有 `data:` 但 JSON 非法，判 `malformed_stream`；纯注释/heartbeat 不是业务事件。

### 2.3 共享 `SSEFramer`

在 `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/translate.py` 增加增量 framer：

- 同时支持 `\n\n`、`\r\n\r\n`、分隔符跨 read、多事件同 read。
- 保留事件原始字节；PASSTHROUGH 不重新序列化。
- 支持多行 `data:` 按 SSE 规则拼接。
- 注释/heartbeat 原样保留但不算业务首帧。
- `finish()` 只接受完整末尾事件；残缺 data/event 块判 `malformed_stream`，纯空白忽略。
- 空间上限由 attempt 级预算统一约束，framer 不另占一份无界 buffer。

## 3. P0′：PASSTHROUGH 显式终态

涉及文件：

- `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/translate.py`
- `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py`
- `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/tests/test_passthrough_sniff.py`
- `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/tests/test_protocol_terminal_handler.py`

流程：

1. `_write_streaming_response()` 暂时保持进入即提交响应头。
2. 每次 read 的原始 chunk 先写客户端，再喂 `SSEFramer + PassthroughTerminalTracker`。
3. tracker/framer 异常不能撤回已发送 chunk，但必须保存首个 integrity error，不能吞掉。
4. EOF 调 `framer.finish()` 与 `tracker.finalize()`：
   - 合法业务终态：正常 chunked 结束。
   - 上游原生 failed/error：已原样转发，不补第二个错误；正常结束，记 `valid/failed`。
   - 空流、无终态、坏帧：补 source 对应 SSE error，再结束 chunked；记 `invalid`。
5. 错误事件由统一 `stream_error_event_for_source(source, TranslationError)` 生成，错误分类复用 `TranslationError` 与 `error_body_for_source()`；不得伪造 `message_stop/response.completed`。
6. 客户端断连只记 `stream_integrity=client_disconnect`；不 cooldown、不改成上游失败。
7. `finally` 只关闭上游，不再做可能改变终态的残余嗅探。

P0′空流 ACCESS 应为：

```text
status=200 response_committed=1 stream_integrity=invalid
terminal_status=open terminal_reason=empty_stream final_error=empty_stream
usage_in=0 usage_out=0
```

## 4. P1′：四路首帧预读

### 4.1 首帧定义与停止点

首帧是**第一个完整、非注释、协议可解析的业务事件**，不是一次 `read()`。

- 允许 TCP 任意切片、前置 heartbeat。
- 空 EOF/只有 heartbeat：`empty_stream`。
- 首业务事件坏 JSON、类型冲突、协议不允许：`malformed_stream`。
- 首业务事件是显式 failed/error：probe 失败，不提交成功响应。
- 首业务事件是合法终态，包括合法零输出 completed/incomplete/refused/paused：probe 成功。
- **首个合法业务事件一成立，立即停止继续读取上游，返回 probe 结果并提交客户端响应；不得为了提前验证 EOF/终态继续预读。** 首帧之后的完整性由提交后 tracker/adapter 负责。

### 4.2 唯一所有权的 continuation

在 `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py` 建立：

```text
probe_upstream_stream(...) -> StreamProbeResult
commit_and_write_stream(result, resp, headers, ...) -> StreamWriteResult
```

`StreamProbeResult` 不同时暴露 raw/parsed/encoded 三份可重复消费数据，而是按模式持有唯一 continuation：

- PASSTHROUGH：`raw_prefix + framer + tracker`。tracker 已消费 prefix；writer 只原样回放 `raw_prefix`，**不得再 feed**，随后新 read 才继续 feed。
- 转换路径：`encoded_prefix + framer + adapter`。adapter 已消费源 prefix；writer 只回放 `encoded_prefix`，**不得再 feed**，随后新 read 才继续解析/feed。

公共元数据：`ok/error/bytes_read/first_event_ms/terminal_state`。

每个 attempt 新建 continuation。失败 attempt 关闭并整体丢弃；不得把 framer、tracker、adapter 或 prefix 带给下一 supply。

### 4.3 每 attempt 256 KiB 总预算

`max_buffer_bytes=262144` 是**每个 attempt 的总暂存预算**，覆盖：

- 已读取但尚未提交的 raw prefix；
- framer 未完成事件（它是 raw prefix 的视图/切片，不得复制计双份）；
- 转换后尚未提交的 encoded prefix。

实现可用共享 bytearray + offset/memoryview，或显式 `buffered_bytes` 统一计数；禁止 raw 256 KiB + encoded 256 KiB 各自放宽。超过总预算抛 `frame_too_large`。

### 4.4 四路统一但不合并转换逻辑

1. PASSTHROUGH：保存原始 prefix，提交后逐字节回放。
2. Chat→Anthropic：解析 Chat data 事件；`[DONE]` 合法；新 adapter 暂存 Anthropic 输出。
3. Responses→Anthropic：允许无 `event:`、以 `data.type` 兜底；新 adapter 暂存 Anthropic 输出。
4. Anthropic→Responses：解析 Anthropic 事件；新 adapter 暂存 Responses 输出。

统一的是提交/failover 契约；各协议 parser/adapter 仍独立。

### 4.5 请求级坏 supply 排除

在主请求作用域、route 循环外新增：

```text
stream_failed_supply_ids: set[str] = set()
```

只记录 HTTP 2xx 后 probe 失败的 supply。`select_supply()` 增加额外排除集合，候选条件同时满足：

- 不在当前 route 的 `tried_set`；
- 不在请求级 `stream_failed_supply_ids`；
- 不在 cooldown；
- supply 存在。

probe 失败时，无论是否进入下一 route，先加入 `stream_failed_supply_ids`。因此同一 supply 被多个 route 引用时，本请求不会再次尝试。

HTTP 429/5xx/URLError 继续沿用现有 route 内 `tried_set + CooldownStore` 语义，不写入 `stream_failed_supply_ids`，避免改变既有跨 route 行为。两类失败集合不得混用。

### 4.6 attempt、failover 与 cooldown 顺序

1. supply 存在、协议兼容、未被任一集合排除。
2. 写 `supply/target_protocol/conversion_kind`，`attempts += 1`。
3. `urlopen()`；HTTP/网络错误保持现有规则。
4. HTTP 2xx 后 probe；此时尚未提交客户端响应。
5. probe 失败：
   - 写 `attempt_errors += (supply_id, "stream_<reason>")`；
   - 写请求级 `stream_failed_supply_ids`；
   - `failover=on`：置 `failover=1`，关闭响应，继续当前/后续 route；cooldown 配置为 0，不写全局 cooldown store；
   - `failover=off`：关闭响应，直接向客户端写 buffered 502/504 error。
6. probe 成功：提交响应，设置 `response_committed=1`，回放 prefix；从此禁止任何 supply/route failover。

### 4.7 全耗尽最终 HTTP 状态

新增结构化 `last_retryable_error`，至少含 `{kind, reason, http_status, supply_id}`；不得从 `attempt_errors` 文本反推。

最终状态优先级：

1. 本请求任一未提交 probe 失败为 `first_event_timeout`：504。
2. 否则本请求发生过未提交流完整性失败：502。
3. 否则仅因 supply 冷却/不存在而未实际发请求：503。
4. HTTP 失败保持现有 cooldown/failover 与最终错误策略，不被 stream 规则改写；若 stream 与 HTTP 失败混合，最后一个结构化 retryable error 决定，timeout 仍按第 1 条优先。

全部耗尽时 `response_committed=0`，客户端收到 source 协议 buffered error。

### 4.8 首事件 deadline 与预读期客户端断连

- 30 秒从 `urlopen()` 返回开始计，到首个合法业务事件成立为止；不替代现有 1800 秒上游总超时。
- 超时为 `TranslationError(reason="first_event_timeout", http_status=504, retry_class="configured")`。
- probe 完成后恢复原 read timeout。若 urllib 无稳定分阶段 timeout 接口，先封装最小 transport `read/close/set_read_timeout`，禁止散落 socket 私有成员访问。
- 预读期间尚未向客户端写数据，设计**不承诺即时感知客户端断连**；最长滞留由 30 秒 deadline 限制。
- 只有实际写客户端时出现 BrokenPipe/Reset 才确认 `client_disconnect`。预读期不能凭推测记录 client_disconnect。
- transport 层未来若提供 cancel signal，可提前取消 probe，属于增强，不是本期正确性依赖。

## 5. 提交后不变量

`response_committed=1` 后：

- 禁止切 supply/route，避免拼接两个模型响应。
- 后续无终态 EOF、坏帧、read 异常：补 source 协议 error，随后结束 chunked，记 `stream_integrity=invalid`。
- 已收到协议原生 failed/error：不追加第二错误，记 `valid/failed`。
- 正常终态后的尾部读异常不得覆盖终态。
- 客户端断连只关闭上游，记 `client_disconnect`；不 cooldown、不记上游失败。

## 6. 观测、账本与展示

### 6.1 ACCESS 字段

在 `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py` 增加：

- `response_committed=0|1`
- `stream_integrity=valid|invalid|client_disconnect|空`；非流请求留空
- `terminal_status=open|completed|incomplete|refused|paused|failed|空`
- `terminal_reason=<TerminalState.reason 或 TranslationError.reason>`
- `first_event_ms=<整数或空>`

保留 `status` 表示真实 HTTP 状态，保留 `stop_reason` 表示既有客户端业务语义；两者不得代替流完整性字段。

### 6.2 账本口径

`UsageTotalsStore.record()`：

- 流式新日志：`stream_integrity=valid` 才进入链路 ok；`invalid` 进入 fail；`client_disconnect` 单列，不伪装 upstream fail，也不计有效完成。
- 合法 `terminal_status in {completed,incomplete,refused,paused,failed}` 均表示协议完整收到终态。业务结果按 terminal_status 另行累计分布；`failed` 可以同时是 integrity valid、业务 failed。
- HTTP 非 200 仍按现有失败口径。
- 旧记录缺 `stream_integrity` 时回退 status，保持历史可读，不回写历史。

这解决“协议完整性成功”与“业务结果成功”混为一谈的问题。

### 6.3 active sessions

扩展 `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/_format_ops.py::parse_access_line()` 返回新增字段。

`load_active_sessions` 对新日志优先使用 `stream_integrity`：

- `invalid` 显示失败；
- `valid` 显示链路完成，并附 terminal_status；
- `client_disconnect` 显示客户端断开，不归因上游；
- 缺字段的旧日志才回退 `status == 200`。

因此 200+empty_stream 不再显示 ok。

### 6.4 status 独立展示

不修改 `protocol conversions` 段；PASSTHROUGH 空流不是协议转换风险。

新增最近 7 天 `stream integrity` 段，按 `(token, source, route, tier, supply, terminal_reason)` 聚合 invalid/client_disconnect，并分 pre-commit、committed、failover 数：

```text
stream integrity (last 7d):
  ⚠ cc[anthropic] → nation1.sonnet → glm-x: empty_stream 1 (committed 1, failover 0)
  hint: upstream stream ended without a valid protocol terminal
```

旧日志不猜测、不纳入。转换提醒与流完整性不得混算。

## 7. 实施顺序

### P0′

1. `SSEFramer` 与类型解析规则。
2. `PassthroughTerminalTracker`，补 Anthropic message_start usage、pause_turn、Responses 终态。
3. PASSTHROUGH 提交后无终态显错。
4. ACCESS 双维字段。
5. 账本与 active sessions 新日志口径。

### P1′

1. `StreamProbeResult` 唯一 continuation。
2. 每 attempt 256 KiB 总预算与 30 秒 deadline。
3. 四路 writer 延迟提交、prefix 单次回放。
4. 请求级 `stream_failed_supply_ids` 跨 route 排除。
5. `last_retryable_error` 与最终 502/503/504。
6. stream integrity status 聚合。

# 风险与权衡

1. 首个 read 不等于首帧，必须按完整 SSE 业务事件判断。
2. 首帧合法不保证整流合法；只有提交前能 failover，提交后只能显错。
3. 客户端首字节延迟增加到首个完整业务事件到达，这是 failover 的必要成本。
4. PASSTHROUGH 提交后仍原始字节直传；framer/tracker 是固定空间旁路，不取消性能定位。
5. 30 秒可能误杀极慢首帧，需上线后观察 `first_event_ms` 分布；不得边实现边擅自修改已确定值。
6. 256 KiB 是 raw+framer+encoded 总预算，实现若复制多份会突破约束，必须在测试中测总计数。
7. `stream_integrity=valid, terminal_status=failed` 不是矛盾：前者是协议完整，后者是业务失败。
8. Responses error 事件的客户端兼容需真实验证；若目标客户端只认 `response.failed`，须基于已知 response 元数据生成，不能凭空造成功终态。
9. 账本口径从单一 HTTP status 升为双维，会形成上线前后分段；旧数据保持回退规则。

# 验证方式

## 必补硬用例

1. **跨 route 共用 supply**：route A probe 空流后进入 route B，同一 supply 不再尝试；其他 supply 可继续。
2. **Responses 无 event 行**：仅 `data:{"type":"response.created",...}` 首帧合法，不能触发 malformed/failover。
3. **未知 Anthropic 非终态事件**：PASSTHROUGH 原样转发并继续，随后合法终态成功；转换路径可按 adapter 能力拒绝。
4. **pause_turn**：Anthropic `message_delta.stop_reason=pause_turn + message_stop` 得 `stream_integrity=valid, terminal_status=paused`，账本计协议完整并单列 paused。

## 场景矩阵

1. Anthropic PASSTHROUGH 正常：message_start 采 usage_in，message_delta+message_stop 得 valid/completed。
2. Responses PASSTHROUGH 正常：无 event 行，以 data.type 推导，completed 得 valid/completed。
3. Chat→Anthropic 正常：首 chat JSON 事件成立立即提交；`[DONE]` 合法，finish_reason 决定终态。
4. Anthropic→Responses、Responses→Anthropic：首事件暂存输出只回放一次，adapter 不双 feed。
5. P0′200 空流：已提交后补 error，invalid/empty_stream，不再静默成功。
6. P1′200 空流：未提交，加入请求级坏 supply 集合；跨 route 仍排除；后续 supply 正常可恢复。
7. 首帧 heartbeat 后合法事件：heartbeat 不算首帧，合法事件一成立立即停止预读并提交。
8. 首帧完整合法后上游迟迟不 EOF：不得继续预读等待 EOF，客户端及时收到首帧。
9. 首帧后断流：禁止 failover，补 error，invalid/unexpected_eof。
10. 合法零输出 completed/incomplete/refused/paused：不能按 usage=0 判坏，均为 integrity valid。
11. 首事件 failed/error：未提交 probe 失败，可 failover；原生业务 failed 在已提交后不重复补错。
12. timeout 与总预算：30 秒→504；raw+encoded 合计超过 256 KiB→502。
13. 仅冷却/不存在、无 attempt：503。
14. 混合 HTTP 与 stream 失败：由结构化 `last_retryable_error` 决定，timeout 优先 504，不解析字符串。
15. 预读期客户端离开：不虚构 client_disconnect；30 秒内结束或写时确认断连。
16. 正常终态后尾部 read error：不覆盖 valid 终态。

## 回归测试

- `SSEFramer`：LF/CRLF、跨 chunk、多行 data、注释、残块、事件类型冲突、`[DONE]`。
- PASSTHROUGH 原始字节与改造前完全一致。
- 每 attempt continuation 唯一消费；writer 不重复 feed。
- 失败 attempt 的 adapter/tracker/framer 不泄漏到下一 supply。
- active sessions：新 200+invalid 显示失败，旧日志仍按 status。
- 账本：valid+failed 与 invalid 分离，paused/refused/incomplete 单列。
- protocol conversions 段输出不变。

命令：

```bash
cd /Users/vincentwang/Documents/NoteVault/tools/model_proxy
python3 -m unittest tests.test_passthrough_sniff -v
python3 -m unittest tests.test_protocol_terminal_handler -v
python3 -m unittest tests.test_cooldown_rules -v
python3 -m unittest tests.test_format_ops -v
python3 -m unittest discover -s tests -v
```

验收：现有 735 tests 全绿，新增矩阵全绿。

## 本地与真实链路

1. 本地代理 `127.0.0.1:18889`，Bearer `cc`、模型 `claude-sonnet`。
2. mock 顺序：429、429、429、200 空流、200 合法流。P0′应显错不挂；P1′应排除空流 supply 并恢复。
3. mock 两个 route 共用空流 supply，验证请求级排除。
4. mock 合法首帧后阻塞/断流，验证立即提交且之后不 failover。
5. curl 直连 aigc appkey `2088140743252222023` 与经代理对照正常 SSE 字节顺序、usage、终态；真实 429 不作为稳定验收前提。
6. 人工核对 `.model_proxy.log`：新流请求必须有完整性字段；空流不再被 active sessions/账本显示为成功。

# 关联

- 替代：[[tools/model_proxy/docs/designs/2026-08-23-passthrough终态与首帧预读failover]]
- [[tools/model_proxy/docs/designs/2026-08-22-跨协议静默丢弃审计与修复方案-v4]]
- [[tools/model_proxy/docs/designs/model_proxy_translate_spec]]
