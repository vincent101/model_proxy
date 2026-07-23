---
type: design-decision
date: 2026-07-22
status: draft
target: "tools/model_proxy/core/server.py"
tags: [architect, model-proxy, bugfix, auth]
---

# 入站 client_token 提取 bug 修复 + 入站/出站不对称排查

## 背景与问题

`core/server.py` 的 `_forward`（676-677 行）提取 client_token 只认 `Authorization: Bearer <token>` 一种 header，不认 Anthropic 官方同样合法的 `x-api-key: <token>`。已用 curl 复现：客户端只发 `x-api-key: cc` 时 token 解析为空串，strategy 匹配失败返回 401 "no strategy/route matched"。而出站转发（885-894 行）同时注入 `Authorization: Bearer {appkey}` 和 `x-api-key: {appkey}`——出站两种都发、入站只认一种，形成不对称。

本方案范围：修此 bug，并系统性排查 `_forward` 全链路是否还有同类"客户端多种合法写法只认其一"的遗漏。

## 方案设计

### 发现点 1（P0，必修）：client_token 只认 Authorization Bearer

**问题**：676-677 行只从 `Authorization: Bearer` 提取，不支持 `x-api-key`。已复现完全阻断。

**修复**：把内联提取逻辑抽成模块级纯函数 `extract_client_token(headers) -> str`（放在 `resolve_strategy` 附近），`_forward` 改为调用它。抽函数的理由见"测试建议"——当前内联在 HTTP handler 里无法单测。

优先级规则（推荐）：
1. `Authorization: Bearer <v>` 存在且非空 → 取其值。
2. 否则回退 `x-api-key: <v>`。
3. 两者都无 → 空串（维持现有 401 行为不变）。

**两者都提供的处理**：`Authorization: Bearer` 优先，忽略 `x-api-key`，**不报错**。理由：
- 本代理里 client_token 只是查 strategy 表的键，不是真实密钥校验，"值不同"没有安全语义，报错反而制造新失败面。
- 出站侧本就对同一 appkey 双发两个 header，入站"取其一"与之对称。
- Anthropic 官方 SDK 场景两者若同时出现值也一致；第三方客户端只发其一。优先 Authorization 是因为它是跨协议（OpenAI/Responses/Anthropic）更通用的写法，命中面最广。

值不同时不做一致性校验、不报 400——记录在方案里作为已知取舍，若后续要审计可在 debug 日志里补一条"两 header 值不一致"的 warning，但不改变取值行为。

**边界**：`Bearer ` 前缀判断建议大小写不敏感（`Authorization: bearer xxx` 少见但 RFC 6750 规定 scheme 大小写不敏感）；`x-api-key` 取值应 `.strip()` 去除首尾空白。header 名查找 `self.headers.get` 本身已大小写不敏感（`email.message.Message` 特性），无需额外处理。

### 发现点 2（P3，需人工核实后再定）：其他鉴权 header 变体

除 `x-api-key` / `Authorization: Bearer` 外，是否还要兼容第三种鉴权 header（如 Azure OpenAI 网关常用的 `api-key`，无 `x-` 前缀）。

**核实状态**：本次设计环境下 WebSearch/websearch MCP 均不可用、Anthropic 官方文档区域重定向抓取失败，**未能联网确证**。以下为待人工核实的判断，不作断言：
- Anthropic Messages API：官方主推 `x-api-key`；`Authorization: Bearer` 主要用于 OAuth/第三方网关。**（需人工核实是否官方两者皆收）**
- OpenAI Chat Completions / Responses API：官方标准是 `Authorization: Bearer`。**（需人工核实 Responses 是否有额外变体）**
- Azure OpenAI：使用非标准 `api-key` header（无 Bearer 前缀）。**（需人工核实，且取决于是否真有客户端经 Azure 网关接入本代理）**

**建议**：本次**不**盲目加 `api-key` 兼容。发现点 1 的 `extract_client_token` 函数留出扩展位（回退链末尾可追加），待确认确有 `api-key`-only 客户端接入需求时再加一条回退。避免为不存在的场景增加解析面。

### 发现点 3（P2，建议修）：detect_source 路径匹配大小写敏感

`detect_source`（325-341 行）用 `clean.endswith("/v1/messages")` 等，大小写敏感。客户端发 `/V1/Messages` 时端点尾缀不匹配，会落到 body 特征兜底：带 `max_tokens`/`system` 仍能救回 anthropic，带 `input` 救回 responses，否则 `unknown` → 501。

**现实影响**：主流 SDK 路径全小写，触发概率低；但一旦大写且 body 特征也不足即彻底失败，属实存在隐患。

**修复**：路径判断前对尾缀比较做小写归一，仅用于协议识别判断，不改变 `_sanitize_forward_query` 与出站 target_url（出站用 `supply.url` 完整端点，与客户端 path 无关，见 344-371 行注释，故归一 path 不影响转发正确性）。改法：`clean_lower = clean.lower()` 后用 `clean_lower.endswith(...)`。低成本善意兼容。

### 非问题（排查后确认无需改）

- **Content-Type 变体**：入站 `_forward` 完全不读 Content-Type 判断，body 解析靠 `json.loads(raw_body)` 的 try/except（681-686 行），带不带 `charset=utf-8` 都不影响。出站 PASSTHROUGH 原样透传客户端 Content-Type（`_skip_req_headers` 不含 content-type），非 PASSTHROUGH 强制 `application/json`（896-898 行）。无遗漏。
- **anthropic-version / anthropic-beta header**：入站 fwd_headers 遍历 `self.headers`，`_skip_req_headers` 只跳 host/content-length/authorization/x-api-key（887 行），故 `anthropic-version`/`anthropic-beta` 原样透传上游，正确。query 里的 `beta` 参数由 `_sanitize_forward_query` 剔除（这是 query 层，与 header 层无关，二者不冲突）。无"只认一种写法"问题。

### 发现点 4（评估结论：不修，属设计契约）：model 字段大小写/别名

`_MODEL_TIER_MAP`（374-378 行）精确匹配 `claude-opus`/`claude-sonnet`/`claude-haiku` 三个字面值，`resolve_tier` 明确注释"不做子串猜测"。发 `Claude-Sonnet`（大写）或 `claude-3-5-sonnet`（真实模型名）会 miss → 400。

**判断**：这三个 tier 名是**代理内部约定的请求别名**（客户端 token→route→tier 三段路由的中间键），不是真实模型名——真实模型名在 supply.target_model 侧。因此：
- **真实模型名别名（`claude-3-5-sonnet` 等）**：明确不该由代理猜测映射。代理的设计前提就是客户端按约定发这三个 tier 字面值；放宽会让"请求别名"与"真实模型名"语义混淆，且哪个 3.5-sonnet 归 sonnet tier 是运营决策不是代理能猜的。**保持严格匹配，不修。**
- **大小写变体（`Claude-Sonnet`）**：可商榷。若要善意兼容，成本极低（查表前 `model.lower()`）。但既然这是代理自定义契约、且文档应已要求客户端配全小写，**倾向不修**，保持契约清晰；除非实际出现大小写导致的接入失败工单，再作为 P3 追加。

与发现点 3 的区别：path 大小写是"同一个标准 HTTP 端点的书写变体"（HTTP 路径本就大小写不敏感的惯例），值得归一；model tier 是"代理私有约定值"，严格匹配是设计意图。

## 修复优先级排序

| 优先级 | 发现点 | 理由 |
|---|---|---|
| P0 必修 | 1. client_token 支持 x-api-key | 已 curl 复现，完全阻断真实第三方客户端接入 |
| P2 建议本次一起修 | 3. detect_source 大小写归一 | 低成本，消除潜在完全失败路径 |
| P3 记录暂不改 | 2. 其他鉴权 header（api-key 等） | 需先人工核实规范 + 确认有真实接入需求 |
| 不修 | 4. model tier 别名/大小写 | 属设计契约，严格匹配是意图（大小写可选，倾向不放宽）|

本次落地建议：**P0 + P2 一起改**，P3/P4 在方案里记录结论。

## 验证方式

### 新增测试（`tests/test_route.py`）

前提：发现点 1 抽出 `extract_client_token(headers)` 纯函数并 export，使其可脱 HTTP 单测。测试用 `email.message.Message` 或简单 dict-like 构造 headers（需大小写不敏感，建议用 `http.client.HTTPMessage` 或复用一个 case-insensitive dict fake）。

新增 `TestExtractClientToken`：
1. 仅 `Authorization: Bearer cc` → `"cc"`
2. 仅 `x-api-key: cc` → `"cc"`（**本次核心复现场景**）
3. 两者都有且值相同 → `"cc"`
4. 两者都有值不同（`Authorization: Bearer a` + `x-api-key: b`）→ `"a"`（验证 Authorization 优先规则）
5. 都无 → `""`
6. `Authorization: Bearer ` 空值 + `x-api-key: cc` → 回退到 `"cc"`（验证"存在但空则回退"）
7. 大小写：`authorization: bearer cc` → `"cc"`（scheme 大小写不敏感，若采纳该边界处理）
8. `x-api-key: " cc "`（带空白）→ `"cc"`（strip）

新增 `TestDetectSourceCaseInsensitive`（发现点 3）：
1. `/V1/Messages` + anthropic body → `"anthropic"`
2. `/V1/Responses` → `"responses"`
3. `/Chat/Completions` → `"chat"`
4. 回归：现有全小写用例仍通过

### 端到端人工核对（curl）

1. `curl -H "x-api-key: cc" .../v1/messages -d '{...}'` → 不再 401，正常路由（复现修复）。
2. `curl -H "Authorization: Bearer cc" ...` → 保持原行为，回归不破。
3. 两 header 都带、值不同 → 按 Authorization 的 token 路由（人工核对 access 日志 `token=` 尾4位）。

### 回归

`cd tools/model_proxy && python3 -m unittest tests.test_route tests.test_translate` 全绿。重点确认 `_sanitize_forward_query` 相关用例不受 detect_source 大小写归一影响。

## 关联

- [[2026-07-22-access-log-and-latency]]
- 目标文件：`tools/model_proxy/core/server.py`（`_forward` 676-677、885-894；`detect_source` 319-341；`_MODEL_TIER_MAP`/`resolve_tier` 374-399）
- 测试：`tools/model_proxy/tests/test_route.py`
