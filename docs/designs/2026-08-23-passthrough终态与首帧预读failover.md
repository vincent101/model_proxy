---
type: design-decision
status: draft
target: "[[tools/model_proxy]]"
tags:
  - architect
  - model-proxy
  - streaming
  - failover
  - observability
---

# 背景与问题

PASSTHROUGH 流当前把上游 EOF 直接当正常结束，未验证协议终态；200 空流因此被记成成功并让下游永久等待。本设计补齐 PASSTHROUGH 终态约束，并把“响应提交前可 failover”抽成四条流路径共用的首帧预读契约。

已核实现状：

- 主 attempt 循环在 `urlopen()` 返回 HTTP 响应后才分派写回；HTTPError/网络错误可在提交客户端响应前 cooldown+failover（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:1952-2017`）。
- PASSTHROUGH 流式分支直接进入 `_write_streaming_response()`，返回后即结束本请求，没有“writer 请求重试”的返回契约（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:2019-2035`）。
- `_write_streaming_response()` 在进入时立即提交状态码和 chunked 响应头；随后逐块原样转发，上游 EOF 无条件写 `0\r\n\r\n`（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:2437-2472`）。
- PASSTHROUGH 嗅探异常被吞掉，残余 buffer 只在 finally 再尝试一次；嗅探失败不改变写回结果（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:2463-2481`）。
- 现有嗅探只处理 usage、stop_reason 和正文标记，不维护统一终态；Anthropic 仅看 `message_delta`，Responses 仅看 completed/incomplete，未覆盖 `message_stop`、`response.failed` 和非法 EOF（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:2710-2762`）。
- 现有 SSE 块切分只认 `\n\n`，未正确覆盖标准 `\r\n\r\n`；解析器虽逐行兼容 `\r`，但前置 framing 不兼容（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:2465-2468,2605-2608,2665-2668,2765-2789`）。
- 三条转换 writer 同样进入即提交 200；异常只能在已提交后补协议错误事件，不能换 supply（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:2506-2519,2530-2588,2590-2647,2649-2708`）。
- 转换 adapter 已具备终态映射和异常 EOF fail-fast：Anthropic、Responses、Chat 共用 `TerminalState/TranslationError`（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/translate.py:121-205`）；各 adapter 的 `finalize()` 对无终态 EOF 产出失败（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/translate.py:1109-1140,1729-1738,2532-2541`）。
- ACCESS 的 `status` 目前由发送给客户端的 HTTP 状态写入，`UsageTotalsStore` 仅以 `status == 200` 判成功；流内失败会被算成功（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:331-357,1360-1405`）。
- 当前 upstream timeout 是整条上游连接统一值，默认 1800 秒，没有首事件 deadline（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py:423-425,517-519,1952-1954`）。
- status 的跨协议提醒只统计 `conversion_kind != passthrough`，PASSTHROUGH 链路异常不会出现（`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/_format_ops.py:253-323`）。

# 方案设计

## 1. 方案选择

### 方案 A：只改 PASSTHROUGH writer

在 `_write_streaming_response()` 内增加终态布尔值，EOF 未终态时补 error。

- 优点：改动最小，可最快止血。
- 缺点：响应头已提交，200 空流不能 failover；四条 writer 继续各自处理 framing/EOF，P1 仍需二次重构。

### 方案 B：PASSTHROUGH 独立预读

只给 PASSTHROUGH 在 `_forward()` 前预读首个完整事件，转换路径保持现状。

- 优点：直接覆盖本次事故，工作量中等。
- 缺点：同一种“首字节前失败”出现两套契约；转换路径的首帧坏流仍不可 failover，违背 v4 已立项的通用 P1。

### 方案 C：共享流门卫，协议解析/转换保持分层

新增固定内存的 SSE framer 与 `StreamProbeResult`；四路统一执行“预读→判定→提交→回放→续读”，PASSTHROUGH 仍回放原始字节，转换路径回放 adapter 已生成但尚未发送的目标事件。

- 优点：一次建立正确的提交边界；新增协议或方向只实现 probe/parser，不改 failover 编排；PASSTHROUGH 不做转换，性能优势保留。
- 缺点：需重构 `_forward()` 与四个 writer 的契约，测试面较大。

**推荐方案 C，分 P0′/P1′ 落地。** P0′先在现有结构上补 PASSTHROUGH 终态与显式中止，立即消除静默成功；P1′再引入共享门卫和四路预读。P0′的终态 tracker、framer、错误序列化直接成为 P1′组件，不做一次性补丁。

## 2. 公共状态与组件

### 2.1 复用终态体系

在 `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/translate.py` 增加纯状态组件，不新增平行枚举：

- `PassthroughTerminalTracker(source)`：内部终态仍为现有 `TerminalState`；协议错误使用现有 `TranslationError`。
- Anthropic：
  - `message_delta.delta.stop_reason` 用 `map_anthropic_terminal()` 得到候选终态并记录 usage。
  - 只有随后收到 `message_stop` 才确认 framing 终态；`message_stop` 前无合法 stop_reason 为 `TranslationError(reason="missing_stop_reason")`。
  - `event:error` 是显式失败终态，不再要求 `message_stop`。
- Responses：
  - `response.completed`、`response.incomplete` 用 `map_responses_terminal()`。
  - `response.failed`、`error` 通过 `classify_upstream_error()` 成为失败终态。
  - 未知 terminal status/event 默认 `TranslationError(reason="unknown:...")`。
- EOF 时：无确认终态统一抛 `TranslationError(reason="unexpected_eof", http_status=502, retry_class="configured")`；空流是其 `bytes_seen=0` 特例，观测 reason 为 `empty_stream`。
- 已有 usage/content 嗅探并入 tracker；删除 `_sniff_passthrough_usage()` 的独立概念，避免 usage 与终态解析两次 JSON。

这里“终态”区分两层：`TerminalState` 表示业务结果，`message_stop/response.*` 表示协议 framing 完成。Anthropic 仅见 `message_delta.stop_reason` 但缺 `message_stop` 仍是异常 EOF，不能视为成功。

### 2.2 共享 SSE framing

在 `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/translate.py` 增加 `SSEFramer`：

- 增量 `feed(bytes) -> list[raw_event_bytes]`，同时支持 `\n\n`、`\r\n\r\n`及边界跨 read。
- 保留事件原始字节；PASSTHROUGH 回放原字节，不重新序列化。
- 支持多行 `data:` 按 SSE 规则拼接；注释/keep-alive 单独标识并忽略语义，但原样回放。
- 内部 buffer 有硬上限；超限抛 `TranslationError(reason="frame_too_large")`，防止无分隔坏流无限占内存。
- `finish()` 只接纳完整的末尾事件；残缺 data/event 块判 `malformed_stream`，纯空白/注释不算业务首帧或终态。

固定内存上限使嗅探为 O(1) 空间；每字节至多进入、移出 buffer 一次。正常 PASSTHROUGH 提交后仍是原始 chunk 直写，仅在旁路 tracker 中解析少数候选事件。

## 3. P0′：PASSTHROUGH 终态嗅探与显式中止

改动目标：

- `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/translate.py`
- `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py`
- `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/tests/test_passthrough_sniff.py`
- `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/tests/test_protocol_terminal_handler.py`

流程：

1. `_write_streaming_response()` 保持当前提交时点，先发送上游 status/headers。
2. 每次 read 后先把原始 chunk 写给客户端，再把同一 chunk 喂给 `SSEFramer + PassthroughTerminalTracker`；tracker 的解析异常不能阻断“当前 chunk”转发，但必须被保存为首个 integrity error，不能吞掉。
3. 正常 EOF 调 `framer.finish()` 和 `tracker.finalize()`：
   - 已确认成功/不完整/拒绝终态：正常写 chunked 终止符。
   - 已收到上游协议失败终态：原样已转发，记失败；正常关闭 chunked，避免追加重复错误。
   - 无终态、坏帧、tracker 异常：补发 source 对应的 SSE error，再写 chunked 终止符。
4. 补发错误统一由 `stream_error_event_for_source(source, TranslationError)` 生成：错误分类复用 `TranslationError`；错误 payload 先由 `error_body_for_source(source, http_status, message)` 生成，再封装为 source 协议 SSE。Anthropic 输出 `event: error`；Responses 输出协议级 `error` 事件。不得补 `message_stop` 或 `response.completed` 伪装成功。
5. 客户端 `BrokenPipeError/ConnectionResetError` 仍只记 `client_disconnect`，不伪造上游异常、不 cooldown。
6. `finally` 只负责关闭上游；不得再执行可能改变终态的“残余嗅探”。

P0′不会 failover：进入 writer 前响应已经提交。它的价值是把永久挂起改为下游可见失败，并把 ACCESS 从静默成功改为链路失败。

## 4. P1′：首帧预读与首字节前 failover

### 4.1 契约重构

在 `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py` 把流处理拆为两阶段：

```text
probe_upstream_stream(resp, source, target, mode, adapter, limits)
  -> StreamProbeResult

commit_and_write_stream(result, resp, headers, writer_context)
  -> StreamWriteResult
```

`StreamProbeResult` 至少包含：

- `ok`、`error: TranslationError | None`
- `raw_prefix`：从上游读取的全部原始字节，供 PASSTHROUGH 原样回放
- `parsed_events`：已解析的源事件，供 tracker/adapter 状态延续
- `encoded_prefix`：转换路径由 adapter 生成、尚未提交的目标协议字节
- `terminal_state`、`terminal_confirmed`
- `bytes_read`、`first_event_ms`

writer 不再决定是否可 failover，只消费已通过 probe 的结果；`_begin_sse_chunked()` 仅在 `result.ok` 后调用。

### 4.2 “首帧”定义

不是一次 `read()`，而是**第一个完整、非注释、协议可解析的 SSE 事件**：

- 允许 TCP/read 任意切片和前置 heartbeat。
- 空 EOF：`empty_stream`。
- EOF 只有注释/空白：`empty_stream`。
- 首个业务块 JSON 非法、事件类型缺失或协议不允许：`malformed_stream`。
- 首个事件已是显式 error/failed/incomplete：按 `TerminalState/TranslationError` 分类，不提交成功响应。
- 首个事件是合法成功终态（例如合法零输出 completed）：probe 成功，可提交并立即收尾；不能用 usage=0 判断失败。

### 4.3 四路统一范围

本期 P1′四路统一，不只修 PASSTHROUGH：

1. PASSTHROUGH：probe 保存原始 prefix；提交后原字节回放，再继续原始 chunk 转发。
2. Chat→Anthropic：probe 解析首个 chat data 事件并喂新建 adapter；暂存其 Anthropic 输出。
3. Responses→Anthropic：probe 解析首个 Responses 事件并喂新建 adapter；暂存 Anthropic 输出。
4. Anthropic→Responses：probe 解析首个 Anthropic 事件并喂新建 adapter；暂存 Responses 输出。

统一的是提交/failover 契约，不把四种协议转换合并成一个大函数。每个 attempt 新建 adapter；失败 attempt 的 adapter、buffer、prefix 全部丢弃，绝不带到下一 supply。

理由：若只做 PASSTHROUGH，P1 遗留仍存在且 `_forward()` 会形成协议特判；四路统一才能保证“客户端首字节前可换 supply，首字节后绝不拼接”是一条系统不变量。

### 4.4 failover、cooldown、attempts 顺序

保持当前“兼容判定在记账前”的原则，但**实际发出上游请求即算 attempt**：

1. supply 存在、未冷却、协议兼容。
2. 写入 `supply/target_protocol/conversion_kind`，`attempts += 1`。
3. `urlopen()`；HTTP 状态错误沿用现有 cooldown/failover。
4. HTTP 2xx 后执行 probe，客户端尚未收到任何响应头或 body。
5. probe 失败：
   - `attempt_errors.append((supply_id, "stream_<reason>"))`。
   - `failover=on`：置 `failover=1`，按独立的 `stream_failure_cooldown_seconds` 冷却该 supply，加入 `tried_set`，关闭响应，继续同 route；同 route 耗尽后沿用既有 route failover。
   - `failover=off`：关闭响应，向客户端发送 source 协议的 buffered 502/504 error。
6. probe 成功：提交响应并回放 prefix；从此设置 `response_committed=1`，禁止任何 supply/route failover。后续异常按 P0′补 SSE error 后断流。

不得把首帧协议错误伪装成 HTTP 429，也不得复用 `resolve_cooldown_seconds(200)`。新增明确的 stream failure cooldown 配置，默认值需用户确认；若不配置，则建议“允许本请求 failover但不跨请求 cooldown”，避免一次偶发坏流长期摘除 supply。

### 4.5 超时与缓冲

建议配置：

```json
{
  "stream_probe": {
    "first_event_timeout_seconds": 30,
    "max_prefix_bytes": 262144,
    "stream_failure_cooldown_seconds": 0
  }
}
```

- `first_event_timeout_seconds` 是从 `urlopen()` 返回到首个完整业务事件的独立 deadline，不替代现有 1800 秒全请求超时。
- 超时为 `TranslationError(reason="first_event_timeout", http_status=504, retry_class="configured")`。
- `max_prefix_bytes` 同时约束前置 heartbeat、残缺事件和转换后的暂存输出；超限为 `frame_too_large`。
- probe 完成后恢复原上游 read timeout；实现必须封装 timeout 切换，禁止业务代码直接穿透访问 urllib/socket 私有成员。若现有 urllib 响应无法稳定提供分阶段 deadline，实施前应将上游 transport 封装为最小 `read/close/set_read_timeout` 接口，而不是散落 socket hack。

30 秒与 256 KiB 是推荐初值，不是协议事实：30 秒避免正常模型首 token 较慢时过度切换，256 KiB 足以容纳首个元数据/工具事件且严格限内存。

## 5. 提交后规则

一旦 `response_committed=1`：

- 禁止切换 supply 或 route，避免把两个模型响应拼成一个 SSE 流。
- 后续读异常、坏帧、无终态 EOF：补 source 协议 error，随后 chunked 正常结束。
- 已收到协议原生失败终态：不追加第二个错误事件，仅记账并结束。
- 客户端断连：只关闭上游，记 `client_disconnect`；不 cooldown、不计上游失败。
- 正常终态之后的 socket/read 异常不得覆盖成功终态，沿用 P0 已验证的“终态幂等”原则。

## 6. 观测与告警

### 6.1 ACCESS 字段

在 `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py` 的 `_acc` 与 ACCESS 增加：

- `response_committed=0|1`
- `stream_outcome=success|incomplete|refused|upstream_failed|empty_stream|unexpected_eof|malformed_stream|first_event_timeout|frame_too_large|client_disconnect`
- `terminal_status=open|completed|incomplete|refused|paused|failed`
- `terminal_reason=<TerminalState.reason 或 TranslationError.reason>`
- `first_event_ms=<整数或空>`

语义：

- `status` 继续表示实际发给客户端的 HTTP 状态，兼容现有日志消费者；已提交后失败仍可能是 200。
- 成功不再由 `status=200` 单独定义。`UsageTotalsStore.record()` 改为：流式请求仅 `stream_outcome in {success,incomplete,refused}` 进入既有 ok 口径；完整性失败进入 fail。是否将 incomplete/refused 从 ok 再拆出属于后续指标升级，本期至少不能计为链路成功。
- 空流样例应记录：`status=200 response_committed=1 stream_outcome=empty_stream terminal_status=failed terminal_reason=empty_stream final_error=empty_stream usage_in=0 usage_out=0`（P0′）。P1′发生 failover 后，最终 ACCESS 记录最后结果，同时 `attempt_errors` 保留坏 supply 的 `stream_empty_stream`；全部耗尽时客户端 HTTP 为 502、`response_committed=0`。
- `stop_reason` 保留现有客户端语义字段；`terminal_*` 是协议完整性字段，二者不互相覆盖。

### 6.2 status 展示

不修改现有 `protocol conversions` 段：PASSTHROUGH 空流不是协议转换风险。

在 `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/_format_ops.py` 新增独立 `stream integrity` 段，聚合最近 7 天 `stream_outcome` 非成功项，键为 `(token, source, route, tier, supply, stream_outcome)`，展示 committed 与 pre-commit/failover 次数。示例：

```text
stream integrity (last 7d):
  ⚠ cc[anthropic] → nation1.sonnet → glm-x: empty_stream 1 (committed 1, failover 0)
  hint: upstream stream ended without a valid protocol terminal
```

该段覆盖 PASSTHROUGH 与转换路径；旧日志缺字段不猜测、不纳入。A 功能的转换提醒保持原样，两个维度不可混算。

### 6.3 日志

- P0′提交后失败：ERROR，包含 req_id/supply/source/reason/bytes_read/terminal_seen。
- P1′提交前 probe 失败并切换：WARNING，格式与现有 cooldown+failover 对齐，但 reason 明确为 `stream_*`。
- 全部耗尽：最终 ERROR/ACCESS，不再留下 `status=200 usage=0/0 stop_reason=` 的成功假象。

## 7. 实施分期

### P0′：立即止血

范围：共享 `SSEFramer`、`PassthroughTerminalTracker`、PASSTHROUGH EOF 校验、提交后 source 协议 error、ACCESS 完整性字段与最小统计修正。

理由：不改变 supply 选择和响应提交时序，回归面可控；先保证任何无终态流都显式失败，消除 SDK 永久等待。

### P1′：首字节前 failover 终态

范围：四路统一 probe/result 契约、首事件 timeout/上限、writer 延迟提交、probe 失败 cooldown/failover、独立 status 展示。

理由：failover 必须发生在 `_forward()` attempt 循环，不能塞进 writer；四路一起改才能形成单一提交边界并关闭 v4 P1 遗留。

建议 P0′合入并真实验证后立即实施 P1′，不长期停在“只能显错、不能自愈”的中间态。

# 风险与权衡

1. **“首个 read”不等于首帧。** 必须按完整 SSE 事件预读；否则 chunk 切割会误判正常流。
2. **首帧合法不代表整流合法。** P1′只能在首字节前 failover；提交后的中途断流只能显式失败，这是不可绕过的 HTTP 边界。
3. **延迟增加。** 客户端首字节延迟增加到“上游首个完整业务事件”到达；这是获得安全 failover 的必要成本。PASSTHROUGH 提交后仍是字节直传。
4. **超时误杀。** 部分模型首事件可能很慢；30 秒需用生产 `first_event_ms` 分布校准。上线首周建议同时记录分位数，再决定是否调整。
5. **错误事件兼容。** Anthropic error SSE 形态明确；Responses 协议级 error 事件需实施前用现有客户端/真实 endpoint 做契约测试。若某客户端只识别 `response.failed`，需在 probe 中缓存 response id/model 后生成该事件，不能凭空伪造字段。
6. **cooldown 策略。** 坏流可能是 supply 故障，也可能是瞬时链路抖动；推荐默认仅本请求 failover、不跨请求 cooldown。是否配置短 cooldown 需用户拍板。
7. **账本兼容。** 改 `UsageTotalsStore` 成功判定会使新旧统计口径分段；不回写历史，版本升级并在 status 注明起始时间。

需用户拍板：

1. 首事件 timeout 是否采用推荐 30 秒。
2. prefix/frame 上限是否采用推荐 256 KiB。
3. stream probe 失败默认是否跨请求 cooldown；推荐默认 0，仅本请求排除。
4. P1′是否按推荐四路统一实施；不建议只做 PASSTHROUGH。

# 验证方式

## 单元测试

1. `SSEFramer`：`\n\n`、`\r\n\r\n`、分隔符跨 chunk、多事件同 chunk、多行 data、注释、末尾残块、超上限。
2. Anthropic tracker：正常 end_turn/tool_use/max_tokens/refusal/pause_turn；message_stop 缺 stop_reason；stop_reason 后缺 message_stop；error；未知 reason；空 EOF。
3. Responses tracker：completed/incomplete/failed/error；未知 status；空 EOF；终态后尾部读异常不覆盖。
4. P0′ writer：正常流字节完全一致；空流/无终态/坏帧补 error 且无成功终态；原生失败不重复补错；客户端断连不标上游失败。
5. P1′ probe：首事件跨多 read、heartbeat 后首事件、首事件超时、空 EOF、frame 超限、首事件 error；失败不提交 headers。
6. failover：前三个 429 后第四个空流、下一 supply 正常；attempts/attempt_errors/cooldown/route_failover 顺序正确；首字节提交后中断绝不换 supply。
7. 四方向：每路 probe 暂存并只回放一次；失败 attempt 的 adapter 状态不泄漏；正常输出与改造前逐字节或逐事件等价。
8. ACCESS/账本/status：200+无终态计失败；P1′全耗尽为 502/504；旧日志不进入 stream integrity；protocol conversions 段不受影响。

建议命令：

```bash
cd /Users/vincentwang/Documents/NoteVault/tools/model_proxy
python3 -m unittest tests.test_passthrough_sniff -v
python3 -m unittest tests.test_protocol_terminal_handler -v
python3 -m unittest tests.test_cooldown_rules -v
python3 -m unittest tests.test_format_ops -v
python3 -m unittest discover -s tests -v
```

基线验收：现有 735 tests 全绿，新增用例全绿。

## 本地与真实链路

1. 本地 mock supply 依次返回 429、429、429、200 空流、200 合法流，代理监听 `127.0.0.1:18889`；Bearer `cc`、模型 `claude-sonnet`。P0′预期收到协议 error 且不挂；P1′预期空流 supply 未提交并切到合法 supply。
2. mock“合法首帧后断流”：预期不 failover，收到显式 error，ACCESS `response_committed=1 stream_outcome=unexpected_eof`。
3. curl 直连 aigc appkey `2088140743252222023` 与经代理请求对照，确认正常 PASSTHROUGH 原始 SSE 字节顺序、终态、usage 一致；真实 429 不作为稳定验收前提。
4. 人工核对 `.model_proxy.log`：不得再出现无 `stream_outcome` 的新格式流请求；空流不得被账本算为成功；status 的 stream integrity 段能定位 token/route/tier/supply。

# 关联

- [[tools/model_proxy/docs/designs/2026-08-22-跨协议静默丢弃审计与修复方案-v4]]
- [[tools/model_proxy/docs/designs/model_proxy_translate_spec]]
