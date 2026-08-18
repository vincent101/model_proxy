---
type: diagnosis
status: draft
target: tools/model_proxy
tags: [architect, diagnosis, streaming, http, codex]
---

# codex 0.145.0 responses 流式 SSE 断连根因诊断

## 背景与问题

codex 0.145.0 配置 `wire_api="responses"` 经 model_proxy 跨协议转换（responses→anthropic 上游），非流式探针正常返回，流式探针（curl）SSE 事件序列看似合规，但 `codex exec` 每次报 `stream disconnected before completion` 并重连 5 次后失败。model_proxy 日志显示 `status=200, usage_in=0, usage_out=0, stop_reason=, final_error=`——代理认为请求成功，但 usage 全零、无错误。

## 方案设计（根因诊断 + 修复方向）

### 根因定位

**根因：`BaseHTTPRequestHandler.protocol_version` 未设置（默认 `"HTTP/1.0"`），流式响应以 HTTP/1.0 + `Transfer-Encoding: chunked` 发出，属非标准组合。codex 的 HTTP 客户端（Rust reqwest→hyper）可能不正确处理 HTTP/1.0 的 chunked 分帧，导致 chunked 帧字节（hex size + CRLF）混入 SSE 数据流，SSE 解析器无法识别事件，codex 判定流断连。**

### 代码证据

1. **`protocol_version` 未设置**：`ModelProxyHandler`（`core/server.py:1197`）继承 `BaseHTTPRequestHandler`，全仓库无任何 `protocol_version` 赋值（grep 确认）。Python 默认 `protocol_version = "HTTP/1.0"`（`python3 -c "from http.server import BaseHTTPRequestHandler; print(repr(BaseHTTPRequestHandler.protocol_version))"` → `'HTTP/1.0'`）。

2. **流式响应使用 chunked 编码**：`_begin_sse_chunked()`（`core/server.py:2246-2259`）发 `Transfer-Encoding: chunked` 头 + 手动 chunked 分帧（`_write_sse_chunk` L2261-2268），但 HTTP 状态行是 `HTTP/1.0 200 OK`。

3. **raw TCP 捕获证实**：模拟 model_proxy 的 `_begin_sse_chunked` 行为，用 raw socket 发送 `POST /v1/responses HTTP/1.1` 请求，捕获到的响应首行为 `HTTP/1.0 200 OK`，且带 `Transfer-Encoding: chunked`——客户端发 HTTP/1.1，服务端回 HTTP/1.0。

4. **日志统计**：164 条 `source=responses` 请求中，仅 4 条 `usage_out≠0`（均为非流式路径——其中 1 条有 `budget_retried=32→64`，证明走的是非流式分支的 `_maybe_budget_retry`），其余 160 条流式请求全部 `usage_in=0, usage_out=0, stop_reason=, final_error=`。

5. **codex 二进制内含 `h2` crate**（HTTP/2 库），确认其 HTTP 栈为 hyper/reqwest。Web 搜索证实："HTTP/1.0 responses with chunked encoding may not be parsed correctly by hyper's strict HTTP/1.1 implementation"。

6. **curl 可用但 codex 不可用的原因**：curl 对 HTTP/1.0 + chunked 采取宽松策略（自动 de-chunk），hyper 遵循 RFC 严格处理（chunked 仅在 HTTP/1.1 定义），可能导致不 de-chunk 或解析异常。

### usage_in=0 / usage_out=0 的产生机制

当 codex 因 SSE 数据被 chunked 帧污染而断连时：
1. `_write_sse_chunk` 中的 `self.wfile.write()` 抛 `BrokenPipeError`
2. 异常被 `except (BrokenPipeError, ConnectionResetError): pass`（L2363）静默吞掉
3. `finalize()` 在 try 块内、异常点之后，**不会被执行**——`response.completed` 事件不发、`adapter.usage_tuple()` 返回初始值 (0,0,0)
4. 回到 `_forward` L1916：`(self._acc["usage_in"], self._acc["usage_out"], _) = adapter.usage_tuple()` → 写入 0/0
5. 无 `final_error`（BrokenPipeError 被 pass，不记日志）

### 非流式为何正常

非流式路径走 `_write_buffered_response`（L2223-2237），发送 `Content-Length` 头（HTTP/1.0 合法），不使用 chunked 编码，codex/reqwest 可正常解析。

### 流式探针（curl）为何看似正常

curl 对 HTTP/1.0 + `Transfer-Encoding: chunked` 做了兼容处理（自动 de-chunk），SSE 数据被正确还原，事件序列完整。但这不能证明 codex 也能正确处理——两者的 HTTP 客户端实现不同。

### 次要问题：BrokenPipeError 吞噬导致 finalize 丢失

`_write_responses_stream`（L2329-2378）的 `finalize()` 和 chunked 终止符 `0\r\n\r\n` 都在 try 块内。当 codex 断连触发 BrokenPipeError 时，这些代码被跳过，导致：
- `response.completed` 不发出（codex 更确信流未完成）
- usage 全零
- chunked 流未正常终止

这是根因的**放大器**——即使 codex 因其他原因断连，这个设计缺陷也会导致日志无法反映真实状况。

## 风险与权衡

1. **修复 HTTP/1.0→1.1 的风险**：设置 `protocol_version = "HTTP/1.1"` 后，BaseHTTPRequestHandler 默认 `Connection: keep-alive`，需要确保 ThreadingHTTPServer 在 keep-alive 模式下正确处理多请求复用连接。Python 标准库在 HTTP/1.1 模式下自动处理 keep-alive，风险低，但需验证。

2. **finalize 移到 finally 的风险**：如果 BrokenPipeError 后仍尝试写 `finalize()` 事件，会再次抛 BrokenPipeError。需在 finally 内部 try-except 包裹写操作。

3. **对照实验需求**：修复前应先做对照实验确认根因（见验证方式），避免基于推测改代码。

## 验证方式

### 对照实验 A（区分"跨协议转换问题"vs"HTTP 传输层问题"）

将 `~/.codex/config.toml` 的 route 改为 `openai`（responses→responses PASSTHROUGH，不经跨协议转换），跑 `codex exec`。如果同样报 `stream disconnected`，则根因在 HTTP 传输层（HTTP/1.0），而非适配器。

### 对照实验 B（验证 HTTP/1.0 假说）

在 `ModelProxyHandler` 类上加一行 `protocol_version = "HTTP/1.1"`，重启 model_proxy，跑 `codex exec`。如果断连消失，确认根因。

### 对照实验 C（raw 字节捕获）

在 `_write_responses_stream` 入口处加临时日志：`log.warning("RAW_UPSTREAM_FIRST_BYTES: %r", upstream_resp.read(200))`（注意会消费前 200 字节，需在 feed 前拼接回去）。检查上游是否返回标准 SSE 格式。

### 修复后验证

1. 设置 `protocol_version = "HTTP/1.1"` 后，用 raw TCP 捕获响应首行，确认 `HTTP/1.1 200 OK`
2. 跑 `codex exec`，确认不再报 `stream disconnected`
3. 检查 model_proxy 日志，确认 `usage_in≠0, usage_out≠0, stop_reason=end_turn`

## 修复方向要点

1. **主修复**：`ModelProxyHandler` 类加 `protocol_version = "HTTP/1.1"`（`core/server.py:1197` 附近，一行代码）
2. **辅助修复**：将 `_write_responses_stream`/`_write_translated_stream`/`_write_translated_stream_from_responses` 中的 `finalize()` + `0\r\n\r\n` 移入 `finally` 块，在 finally 内部 try-except 包裹，确保即使客户端断连也尝试发收尾事件 + 终止符
3. **诊断增强（可选）**：在 `_write_responses_stream` 首次循环加 DEBUG 日志记录上游前 N 字节，便于未来排障

## 关联

- 适配器实现：[[model_proxy_translate_spec|translate spec]] §3+§4
- 日志架构：[[2026-07-22-access-log-and-latency]]
- 预算治理：[[2026-08-07-reasoning-thinking-truncation-and-protocol-consistency]]
