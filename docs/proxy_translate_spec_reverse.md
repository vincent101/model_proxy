# Proxy 协议转换器规格（反向）：OpenAI Responses API ↔ Anthropic

> 用途：本地 proxy 新增能力——把 codex-cli 发出的 OpenAI **Responses API** 请求（`POST /v1/responses`）转换成 Anthropic `/v1/messages` 格式，发给美团网关 Anthropic 端点（`aigc.sankuai.com/v1/anthropic/v1/messages`，Bearer appkey 鉴权），再把 Claude 的 Anthropic 响应（含流式 SSE、工具调用）转换回 Responses 格式返回给 codex。
>
> 本文档为**编码地基**，字段级精确，可直接照写代码。不含实现代码，逻辑用伪代码/状态转移描述。
>
> **与正向规格的关系**：Anthropic 侧字段定义（system / messages / content block / tools / thinking / stop_reason / usage / 流式事件序列）**不重抄**，一律引用 `《proxy_translate_spec.md》`（下称"正向规格"）对应章节。本文档只精写 Responses 侧结构与两侧映射。
>
> **来源标注约定**：
> - `[实测样本]`：从 `../samples/responses_api_samples.txt`（美团网关 `/v1/responses` 端点 2026-07-17 实测 4 个样本）逐字段查证
> - `[正向规格]`：Anthropic 侧字段定义引用正向规格 `proxy_translate_spec.md`
> - `[Responses]`：OpenAI Responses API 官方语义（codex-cli 依赖）
> - `[Anthropic]`：Anthropic Messages API 语义
> - `[设计推断]`：本文档基于上述事实做的工程设计决策，实现时可调整

---

## 0. 总体数据流与职责边界

```
codex-cli
  │  POST /v1/responses   (Responses body: instructions/input/tools/reasoning/stream ...)
  ▼
proxy  ── _forward() 中新增分支：命中 /v1/responses 且目标是 Anthropic 端点 ──▶
  │
  ├─[模块A'] req_responses_to_anthropic(responses_body) → (anthropic_body, ctx)
  │         ctx 含 request_model、stream 标志、call_id 映射等
  │
  │  POST /v1/anthropic/v1/messages   (Anthropic body, Bearer appkey)
  ▼
美团网关 Anthropic 端点 → Claude
  │
  ├─ 非流式：{type:message, role:assistant, content:[...], stop_reason, usage}
  │     └─[模块B'] resp_anthropic_to_responses(anthropic_resp, ctx) → responses_resp(dict)
  │
  └─ 流式：Anthropic SSE (event: message_start ... event: message_stop)
        └─[模块C'+D'] ResponsesStreamAdapter(anthropic_sse_events, ctx)
              产出 Responses SSE 事件序列 (data: {json}\n\n，每事件带 sequence_number)
  ▼
codex-cli
```

**关键约束**：
- 全程只用标准库（`json`、`hashlib`、`secrets`、`urllib`、`http.server`），与正向实现一致。
- 转换器输入输出一律是 `dict` / `bytes` / 字符串迭代器。
- 转换失败必须返回**结构合法的 Responses error**（见 §5），绝不让 codex 挂死。
- 流式路径**不字节透传**：逐事件读 Anthropic SSE → 状态机转换 → 逐事件写出 Responses SSE，且必须维护全局递增的 `sequence_number`（见 §3）。
- **反向特有负担**：输入端 Responses 的 `input` 是"扁平 items 数组"（message / function_call / function_call_output 混排），需还原成 Anthropic 的 messages + content block（tool_use / tool_result），比正向的 Anthropic→OpenAI 还繁琐；输出端 Claude 产 thinking，而 Responses 有 `reasoning` 位，需决策如何落位（见 §5）。

---

## 1. 请求转换：Responses → Anthropic（模块A'）

**签名（设计推断）**：
```
req_responses_to_anthropic(body: dict, model_is_reasoning: bool) -> tuple[dict, dict]
    返回 (anthropic_body, ctx)
    ctx = {
        "request_model": str,        # 回填响应 model 字段
        "stream": bool,
        "tool_call_ids": dict,       # 见 §1.3，call_id ↔ tool_use_id 对齐（透传即可）
    }
```

### 1.1 顶层字段映射表

Responses 请求字段以 `[Responses]` 语义为准（codex 发出）；Anthropic 目标字段定义见 [正向规格 §1]。

| Responses 字段 | Anthropic 字段 | 规则 | 来源 |
|---|---|---|---|
| `model` | `model` | 原样透传（proxy 上游已做 model_map） | [设计推断] |
| `instructions` | 顶层 `system`（字符串） | Responses 的系统指令是**纯字符串**；直接放 Anthropic `system` 字符串。见 §1.2 | [Responses]+[正向规格 §1.2] |
| `input` | `messages` | 核心还原逻辑，见 §1.3 | [Responses]+[Anthropic] |
| `max_completion_tokens` | `max_tokens` | 改名；Anthropic **必填**，缺省给默认 `4096` | [Anthropic]+[设计推断] |
| `max_output_tokens` | `max_tokens` | 同上（Responses 亦可能用此别名，二者取其一，都缺则 4096） | [设计推断] |
| `reasoning.effort` | `thinking` + `output_config.effort` | 见 §1.4 | [Responses]+[正向规格 §1.4] |
| `tools` | `tools` | Responses 扁平 function → Anthropic `input_schema`，见 §1.5 | [实测样本]+[Anthropic] |
| `tool_choice` | `tool_choice` | 见 §1.6 | [Responses]+[Anthropic] |
| `stream` | `stream` | 原样透传布尔 | [设计推断] |
| `temperature` | `temperature` | 原样透传 | [Responses] |
| `top_p` | `top_p` | 原样透传 | [Responses] |
| `parallel_tool_calls` | —（丢弃） | Anthropic 无对应顶层开关（默认允许并行），丢弃 | [设计推断] |
| `text.format` / `text.verbosity` | —（丢弃） | Anthropic 无对应；`text.format.type=text` 是默认，丢弃 | [实测样本]+[设计推断] |
| `store` / `background` / `service_tier` / `truncation` / `conversation` / `metadata` | —（丢弃） | Responses 平台侧字段，Anthropic 不认，丢弃 | [实测样本]+[设计推断] |
| `previous_response_id` | —（不支持） | 有状态会话续接，本 proxy 无状态，丢弃并记 log（见 §5） | [设计推断] |

**白名单策略**：只转换上表字段，其余 Responses 平台字段一律丢弃并记 log，避免把 Anthropic 端点不认识的字段透传导致 400 `[设计推断]`。

### 1.2 instructions → system [Responses]

Responses 的 `instructions` 语义上是**单个字符串**（codex 把系统提示整体放这里）。

```
sys = body.get("instructions")
if sys:                                   # 非空字符串
    anthropic_body["system"] = sys        # 直接作为 Anthropic system 字符串
# 若 input items 里还混有历史 system 语义的 message（role 非标准），一般不会出现；忽略
```
> Anthropic `system` 既可是字符串也可是 text block 数组（见 [正向规格 §1.2]）；此处用**纯字符串**最简单稳妥。若 `instructions` 缺失则不设 `system`。

### 1.3 input → messages（反向核心，最繁琐）[Responses]+[Anthropic]

`input` 有两种形态：

**形态1：字符串**（简单单轮）
```
if isinstance(input, str):
    anthropic_body["messages"] = [{"role":"user", "content": input}]
```

**形态2：items 数组**（多轮 / 带工具）
`input` 是 item 列表，每个 item 有 `type` 字段，codex 会发这几类：
- `{"type":"message", "role":"user"|"assistant", "content":[...]}` — 普通对话消息
- `{"type":"function_call", "call_id":..., "name":..., "arguments":"<JSON字符串>"}` — 模型上一轮发起的工具调用（回放历史）
- `{"type":"function_call_output", "call_id":..., "output":"<字符串>"}` — 工具执行结果（回放历史）

> 注意：Responses 的 items 是**扁平序列**（function_call / function_call_output 与 message 平级混排），而 Anthropic 要求：assistant 消息的 `tool_use` 是 content block，工具结果的 `tool_result` 是**下一条 user 消息**的 content block。反向转换必须把扁平序列**重新分组**成 Anthropic 的 `{role, content:[blocks]}` 消息。

**分组算法（设计推断）**：
```
messages = []            # 输出 Anthropic messages
pending_user_blocks = [] # 累积待成为一条 user 消息的 block（含 tool_result）

def flush_user():
    if pending_user_blocks:
        messages.append({"role":"user", "content": pending_user_blocks[:]})
        pending_user_blocks.clear()

for item in input_items:
    t = item.get("type")

    if t == "message":
        role = item.get("role")
        blocks = _responses_content_to_anthropic_blocks(item.get("content"))  # §1.3.1
        if role == "user":
            pending_user_blocks.extend(blocks)      # 并入待 flush 的 user 块
        elif role == "assistant":
            flush_user()                            # 先收尾 user 消息
            messages.append({"role":"assistant", "content": blocks})

    elif t == "function_call":
        # 模型历史发起的工具调用 → 归到一条 assistant 消息的 tool_use block
        flush_user()
        tool_use = {
            "type": "tool_use",
            "id":   item.get("call_id") or gen_toolu_id(),   # call_id 直接当 tool_use.id 透传
            "name": item.get("name", ""),
            "input": _safe_json_loads(item.get("arguments", "{}")),  # Responses arguments 是字符串→dict
        }
        # 若上一条已是 assistant，可合并进其 content；否则新开一条 assistant 消息
        _append_tool_use(messages, tool_use)        # 见下

    elif t == "function_call_output":
        # 工具结果 → 下一条 user 消息的 tool_result block
        pending_user_blocks.append({
            "type": "tool_result",
            "tool_use_id": item.get("call_id"),      # 与 function_call 的 call_id 对齐
            "content": item.get("output", ""),       # Responses output 是字符串，直接放
        })
    # 其他 type（如 reasoning item 回放）忽略并记 log

flush_user()   # 收尾
anthropic_body["messages"] = messages
```

`_append_tool_use(messages, tool_use)`（设计推断）：
```
if messages 且 messages[-1]["role"] == "assistant" 且 content 是 list:
    messages[-1]["content"].append(tool_use)   # 连续多个 function_call 合并进同一 assistant
else:
    messages.append({"role":"assistant", "content":[tool_use]})
```

> **invariant**：一个 `function_call.call_id` 必须与对应 `function_call_output.call_id` 相等，转换后 Anthropic 侧 `tool_use.id == tool_result.tool_use_id`。call_id（形如 `call_...`）**原样透传**当作 `tool_use.id`，无需重编码 `[设计推断]`。

#### 1.3.1 message.content → Anthropic content block

Responses message 的 `content` 是数组，元素类型（[实测样本] 见响应侧 `output_text`，请求侧 codex 常发以下类型）：

| Responses content 元素 | Anthropic block | 规则 | 来源 |
|---|---|---|---|
| `{"type":"input_text", "text":...}` | `{"type":"text", "text":...}` | user 侧文本 | [Responses]+[Anthropic] |
| `{"type":"output_text", "text":...}` | `{"type":"text", "text":...}` | assistant 侧文本（历史回放） | [实测样本]+[Anthropic] |
| `{"type":"input_image", "image_url":...}` | `{"type":"image","source":{...}}` | 图片，见下 | [Responses]+[正向规格 §1.3] |
| content 为纯字符串 | `[{"type":"text","text":<字符串>}]` | 兼容简写 | [设计推断] |

```
def _responses_content_to_anthropic_blocks(content) -> list:
    if isinstance(content, str):
        return [{"type":"text", "text": content}]
    blocks = []
    for part in content or []:
        pt = part.get("type")
        if pt in ("input_text", "output_text", "text"):
            blocks.append({"type":"text", "text": part.get("text","")})
        elif pt in ("input_image",):
            src = _responses_image_to_anthropic_source(part)   # data url / http url → Anthropic source
            if src: blocks.append({"type":"image", "source": src})
        # 其他忽略并记 log
    return blocks
```
> 图片：Responses 的 `image_url` 若是 `data:<media>;base64,<data>` 则拆成 Anthropic `source:{type:"base64",media_type,data}`；若是 http(s) url 则 `source:{type:"url",url}`。Anthropic source 结构见 [正向规格 §1.3]（正向是反方向拼 data url，这里逆向拆解）`[设计推断]`。

### 1.4 reasoning.effort → thinking + output_config.effort [Responses]+[正向规格 §1.4]

Responses 请求携带 `reasoning:{effort: "low"|"medium"|"high"}`（[实测样本] 响应回显 `reasoning.effort` 为 `low`/`null`，请求侧 codex 传三档之一）。

Anthropic 侧接受形态见 [正向规格 §1.4]：`thinking:{type:"adaptive"}` + `output_config:{effort}`。

**映射规则（设计推断）**：

| Responses `reasoning.effort` | Anthropic | 说明 |
|---|---|---|
| `"low"` | `thinking:{type:"adaptive"}` + `output_config:{effort:"low"}` | 直传 |
| `"medium"` | 同上 `effort:"medium"` | 直传 |
| `"high"` | 同上 `effort:"high"` | 直传 |
| 缺失 / `null` | 不设 `thinking` / `output_config` | 非 reasoning 请求 |

```
def map_reasoning(body) -> dict:
    r = body.get("reasoning") or {}
    eff = r.get("effort")
    if eff in ("low","medium","high"):
        return {"thinking": {"type":"adaptive"}, "output_config": {"effort": eff}}
    return {}   # 不注入
```
> 只有目标 model 是 reasoning 模型（Claude 支持 thinking）时才注入 `thinking`；非 reasoning 模型带 `thinking` 可能 400，由 `model_is_reasoning` 门控 `[设计推断]`。Responses 无 `budget_tokens` 语义，故只走 effort 分档，不涉及正向的 budget→effort 换算。

### 1.5 tools 转换 [实测样本]+[Anthropic]

Responses tool（[实测样本] 样本3/4 逐字段照抄，**扁平结构**，无 `function` 包裹）：
```json
{"type":"function", "name":"get_weather", "description":"查天气",
 "parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]},
 "strict":true}
```
Anthropic tool：`{name, description, input_schema}`（见 [正向规格 §1.5]）。

```
def translate_tools(responses_tools) -> list:
    out = []
    for tool in responses_tools or []:
        if tool.get("type") != "function":
            # 非 function 类型（Responses 托管工具如 web_search/file_search）Anthropic 不认，跳过并记 log
            continue
        out.append({
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "input_schema": tool.get("parameters") or {"type":"object","properties":{}},
        })
    return out
```
> Responses 工具名无 64 字符上限约束（那是 OpenAI Chat Completions 的限制），此处**不需要正向的 truncate_tool_name 逻辑**；Anthropic 工具名限制更宽，直接透传 `[设计推断]`。`strict` 字段丢弃（Anthropic input_schema 无此开关）。

### 1.6 tool_choice 转换 [Responses]+[Anthropic]

Responses `tool_choice`：`"auto"` | `"none"` | `"required"` | `{"type":"function","name":X}`（[实测样本] 见 `"tool_choice":"auto"`）。
Anthropic `tool_choice`：`{type:"auto"|"any"|"tool"|"none", name?}`（见 [正向规格 §1.6]）。

| Responses | Anthropic |
|---|---|
| `"auto"` | `{"type":"auto"}` |
| `"none"` | `{"type":"none"}` |
| `"required"` | `{"type":"any"}` |
| `{"type":"function","name":X}` | `{"type":"tool","name":X}` |
| 缺失 | 不设（Anthropic 默认 auto） |

### 1.7 max_tokens 兜底 [Anthropic]+[设计推断]

Anthropic `max_tokens` **必填**。取值优先级：`max_completion_tokens` → `max_output_tokens` → 默认 `4096`。
```
anthropic_body["max_tokens"] = body.get("max_completion_tokens") or body.get("max_output_tokens") or 4096
```

---

## 2. 非流式响应转换：Anthropic → Responses（模块B'）

**输入** Anthropic 非流式响应（结构见 [正向规格 §2.5]）：
```json
{"id":"msg_...", "type":"message", "role":"assistant",
 "model":"...", "content":[ {type:text,text} | {type:tool_use,id,name,input} | {type:thinking,...} ],
 "stop_reason":"end_turn"|"max_tokens"|"tool_use"|...,
 "stop_sequence":null,
 "usage":{"input_tokens":N,"output_tokens":M, ...}}
```

**输出** Responses 响应结构以 [实测样本 样本1/样本3] 为准，**逐字段照抄**。

**签名（设计推断）**：`resp_anthropic_to_responses(anthropic_resp: dict, ctx: dict) -> dict`

### 2.1 content blocks → output items [实测样本]

Anthropic `content` 是 block 数组；Responses `output` 是 item 数组。逐块映射：

| Anthropic block | Responses output item | 来源 |
|---|---|---|
| `{type:"text", text}` | `{type:"message", role:"assistant", content:[{type:"output_text", text, annotations:[], logprobs:[]}]}` | [实测样本 样本1] |
| `{type:"tool_use", id, name, input}` | `{type:"function_call", call_id, name, arguments:"<JSON字符串>"}` | [实测样本 样本3] |
| `{type:"thinking", ...}` | 见 §5（reasoning 落位决策，首版丢弃或转 reasoning item） | [设计推断] |

```
output = []
for block in anthropic_resp.get("content", []):
    bt = block.get("type")

    if bt == "text":
        output.append({
            "type": "message",
            "id": "msg_" + token_hex(16),          # §6.3 id 格式
            "status": "completed",
            "role": "assistant",
            "content": [{
                "type": "output_text",
                "text": block.get("text", ""),
                "annotations": [],
                "logprobs": [],
            }],
        })

    elif bt == "tool_use":
        output.append({
            "type": "function_call",
            "id": "item_" + token_hex(16),          # item 自身 id（≠ call_id）
            "status": "completed",
            "call_id": block.get("id"),             # Anthropic tool_use.id 直接当 call_id 透传
            "name": block.get("name", ""),
            "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),  # dict → JSON 字符串
        })

    elif bt in ("thinking", "redacted_thinking"):
        # 见 §5：首版丢弃（或聚合到顶层 reasoning.summary）
        pass
```
> **关键点**：Anthropic `tool_use.input` 是 **dict**，Responses `function_call.arguments` 要求是 **JSON 字符串**，必须 `json.dumps`（与 [实测样本 样本3] `"arguments":"{\"city\":\"北京\"}"` 一致）`[实测样本]`。`call_id`（形如 `call_...`）优先透传 Anthropic 的 `tool_use.id`；codex 后续会用它回传 `function_call_output.call_id`，只要能对上即可 `[设计推断]`。

### 2.2 stop_reason → status [Anthropic]+[实测样本]

Responses 顶层 `status` 实测只见 `"completed"`（[实测样本 样本1/3]）。Responses 无与 Anthropic `stop_reason` 一一对应的枚举，`status` 表达的是**整个 response 的完成态**，不是停止原因。

| Anthropic `stop_reason` | Responses `status` | 说明 |
|---|---|---|
| `end_turn` | `"completed"` | 正常结束 |
| `tool_use` | `"completed"` | 有 function_call item，仍是 completed（codex 看 output 里有无 function_call 判断是否要执行工具） | 
| `max_tokens` | `"completed"`（或 `"incomplete"`） | 截断；实测未见 incomplete，保守用 completed，可在 log 标注 |
| `stop_sequence` | `"completed"` | |
| `refusal` / 其他 | `"completed"` | |

> `[设计推断]`：实测样本 status 恒为 `completed`。`stop_reason` 信息在 Responses 侧主要通过 `output` items 的存在与否体现（有 function_call → codex 执行工具后再请求）。首版**统一置 `completed`**；若需精确表达截断，可探测 codex 是否识别 `incomplete` + `incomplete_details:{reason:"max_output_tokens"}`，未实测前不启用。

### 2.3 usage 映射 [实测样本]

Anthropic usage → Responses usage（[实测样本 样本1] 结构逐字段照抄）：
```
u = anthropic_resp.get("usage") or {}
responses_usage = {
    "input_tokens":  u.get("input_tokens", 0),
    "input_tokens_details":  {"cached_tokens": u.get("cache_read_input_tokens", 0)},
    "output_tokens": u.get("output_tokens", 0),
    "output_tokens_details": {"reasoning_tokens": 0},   # 见下
    "total_tokens":  u.get("input_tokens", 0) + u.get("output_tokens", 0),
}
```
> - `input_tokens_details.cached_tokens`：Anthropic 有 `cache_read_input_tokens` 时回填，否则 0 `[设计推断]`。
> - `output_tokens_details.reasoning_tokens`：Anthropic 顶层 usage **不单列** thinking token（已含在 output_tokens 内）。实测样本该值为 0。首版填 `0`；若二期解析 thinking 块可估算，非必需 `[设计推断]`。
> - `total_tokens`：Responses 要求该字段（[实测样本] 有），Anthropic 不返回，需**自行相加** `[设计推断]`。

### 2.4 补齐 Responses 必需字段 [实测样本 样本1]

严格照 [实测样本 样本1] 完整结构补齐顶层字段：
```
{
  "id":          "resp_" + token_hex(16),          # §6.3
  "object":      "response",
  "created_at":  int(time.time()),                  # Unix 秒
  "status":      <§2.2 映射结果，通常 "completed">,
  "background":  false,
  "completed_at": int(time.time()),                 # 完成时间戳
  "model":       ctx["request_model"],              # 回填请求 model
  "output":      output,                             # §2.1
  "parallel_tool_calls": true,
  "reasoning":   {"effort": <请求回显或 null>, "summary": null},
  "service_tier":"default",
  "store":       true,
  "text":        {"format": {"type":"text"}, "verbosity": "medium"},
  "tool_choice": "auto",
  "tools":       <请求 tools 回显，见下>,
  "truncation":  "disabled",
  "usage":       responses_usage,                    # §2.3
  "metadata":    {},
}
```
> - `tools`：[实测样本 样本3] 响应里**回显了请求的 tools**（扁平 function 结构）。codex 可能不强依赖，但为贴合样本，把请求侧 `tools` 原样回填（若无则 `[]`）`[设计推断]`。
> - `reasoning.effort`：回显请求的 `reasoning.effort`；请求未带则 `null`（[实测样本 样本1] effort=low 对应请求带了 low，样本3 effort=null）`[实测样本]`。
> - `conversation`：[实测样本] 有 `conversation:{id:conv_...}`。本 proxy 无状态，可省略该字段或生成一个 `conv_` 占位；codex 主要读 `output`，首版**省略**并观察 codex 是否报错，报错则补占位 `[设计推断]`。
> - 其余 `object/service_tier/store/text/truncation/parallel_tool_calls/metadata` 均为 [实测样本] 固定值，照抄常量即可。

---

## 3. 流式 SSE 转换：Anthropic → Responses（模块C'，核心）

**输入**：Anthropic 流式事件序列（`event: <type>\ndata: {json}\n\n`），事件类型与 JSON 结构见 [正向规格 §3.1 / §3.2]：`message_start → content_block_start → content_block_delta → content_block_stop → message_delta → message_stop`（可穿插 `ping`）。

**输出**：Responses 流式事件序列，结构以 [实测样本 样本2（文本）/ 样本4（工具）] 为准，逐字段照抄。

### 3.1 Responses 流式事件序列（文本，[实测样本 样本2]）

```
response.created           (response 骨架, status=in_progress, output=[])
response.in_progress       (同上骨架)
response.output_item.added (output_index=0, item={type:message,status:in_progress,role:assistant,content:[]})
response.content_part.added(item_id, output_index=0, content_index=0, part={type:output_text,text:"",...})
response.output_text.delta (item_id, output_index=0, content_index=0, delta:"Hi")   # 可多次
response.output_text.done  (item_id, output_index=0, content_index=0, text:"Hi! 👋")
response.content_part.done (item_id, output_index=0, content_index=0, part={...完整text})
response.output_item.done  (output_index=0, item={type:message,status:completed,content:[...]})
response.completed         (response 完整骨架, status=completed, output=[...], usage)
```

**每个事件都带 `sequence_number`，从 0 开始全局单调递增**（[实测样本 样本2] seq 0..10）`[实测样本]`。

### 3.2 各 Responses 事件精确 JSON 结构 [实测样本 样本2/4，逐字段照抄]

**response.created / response.in_progress**（骨架相同，只 `type` 不同）：
```json
{"type":"response.created","response":{
  "id":"resp_<hex>","object":"response","created_at":<ts>,"status":"in_progress",
  "background":false,"model":"<request_model>","output":[],"parallel_tool_calls":true,
  "conversation":{"id":"conv_<hex>"},"reasoning":{"effort":null,"summary":null},
  "service_tier":"auto","store":true,"text":{"format":{"type":"text"},"verbosity":"medium"},
  "tool_choice":"auto","tools":[<请求tools回显>],"truncation":"disabled","metadata":{}},
 "sequence_number":0}
```
> 骨架内 `status=in_progress`、`service_tier="auto"`（注意：`completed` 事件里变 `"default"`）、无 `usage`（`completed` 才有）`[实测样本]`。

**response.output_item.added**（开一个 output item）：
```json
{"type":"response.output_item.added","output_index":I,
 "item":{"id":"<msg_/item_ id>","type":"message"|"function_call","status":"in_progress",...},
 "sequence_number":S}
```
- text 消息 item：`{"id":"msg_...","type":"message","status":"in_progress","role":"assistant","content":[]}`
- 工具 item：`{"id":"item_...","type":"function_call","status":"in_progress","call_id":"call_...","name":"...","arguments":""}`（见 §4）

**response.content_part.added**（仅 message item 需要，工具 item 不发）：
```json
{"type":"response.content_part.added","item_id":"msg_...","output_index":I,"content_index":0,
 "part":{"type":"output_text","text":"","annotations":[],"logprobs":[]},"sequence_number":S}
```

**response.output_text.delta**（文本增量，可多次）：
```json
{"type":"response.output_text.delta","item_id":"msg_...","output_index":I,"content_index":0,
 "delta":"<片段>","logprobs":[],"obfuscation":"<可选>","sequence_number":S}
```
> `obfuscation` 字段实测存在（`obf_xxx`），是 OpenAI 侧混淆字段；proxy **可省略**该字段，codex 不依赖 `[设计推断]`。`logprobs:[]` 固定空数组。

**response.output_text.done**（文本块结束，给完整文本）：
```json
{"type":"response.output_text.done","item_id":"msg_...","output_index":I,"content_index":0,
 "text":"<完整累计文本>","logprobs":[],"sequence_number":S}
```

**response.content_part.done**：
```json
{"type":"response.content_part.done","item_id":"msg_...","output_index":I,"content_index":0,
 "part":{"type":"output_text","text":"<完整文本>","annotations":[],"logprobs":[]},"sequence_number":S}
```

**response.output_item.done**（item 收尾，给完整 item）：
```json
{"type":"response.output_item.done","output_index":I,
 "item":{"id":"msg_...","type":"message","status":"completed","role":"assistant",
         "content":[{"type":"output_text","text":"<完整文本>","annotations":[],"logprobs":[]}]},
 "sequence_number":S}
```

**response.completed**（末事件，完整 response，含 usage）：
```json
{"type":"response.completed","response":{
  ...骨架同 created 但 status="completed", service_tier="default",
  "completed_at":<ts>, "output":[<所有完整 item>],
  "usage":{"input_tokens":N,"input_tokens_details":{"cached_tokens":0},
           "output_tokens":M,"output_tokens_details":{"reasoning_tokens":0},"total_tokens":N+M}},
 "sequence_number":S}
```

### 3.3 wire format [实测样本]

Responses 流式**不带 `event:` 行**，只有 `data:` 行（与 Anthropic 双行不同）：
```
data: <紧凑JSON>\n
\n
```
- 无 `event: <type>` 前缀行；事件类型在 JSON 的 `"type"` 字段里 `[实测样本]`。
- JSON 用 `json.dumps(obj, ensure_ascii=False, separators=(",",":"))`（紧凑，但实测样本对中文**未转义**，用 `ensure_ascii=False`）。
- 每事件后一个空行 `\n\n`。
- 响应头：`Content-Type: text/event-stream`、`Cache-Control: no-cache`；用 chunked 逐事件写。
- **无 `[DONE]` 哨兵**：实测样本流以 `response.completed` 结束，未见 `data: [DONE]`。codex 靠 `response.completed` 判定结束 `[实测样本]`。

### 3.4 Anthropic 事件 → Responses 事件 状态机 [设计推断]

**状态变量**：
```
seq                = 0        # sequence_number，每 emit 一个事件 +1（emit 后自增）
response_id        = "resp_" + token_hex(16)   # created 时生成，全程复用
sent_created       = False    # 是否已发 created/in_progress
output_index       = -1       # 当前 output item 的索引（首块 open 时 +1 → 0）
cur_item_kind      = None     # "message" | "function_call"
cur_item_id        = None     # 当前 item id（msg_.. 或 item_..）
cur_text_buf       = ""       # 当前 message item 累计文本（output_text.done 用）
content_part_open  = False    # message item 是否已发 content_part.added
final_stop_reason  = None     # 由 Anthropic message_delta.stop_reason 记录（本反向不直接用，见 §2.2）
usage_in           = 0        # input_tokens（message_start 首帧可拿）
usage_out          = 0        # output_tokens（message_delta 末尾给）
tools_echo         = ctx 里的请求 tools     # 骨架回填
model              = ctx["request_model"]
```

**输入事件 → 输出事件 映射规则**：

```
FOR each Anthropic 事件 (type, data):

  # (0) 首次任意事件前：发 created + in_progress
  if not sent_created:
      emit response.created(骨架, status=in_progress)          # seq 0
      emit response.in_progress(骨架, status=in_progress)      # seq 1
      sent_created = True

  # === message_start ===
  if type == "message_start":
      usage_in = data["message"]["usage"].get("input_tokens", 0)   # 回填 input（可选）
      # 不产出 Responses 事件（Responses 的 item 由 content_block_start 触发）

  # === content_block_start ===
  elif type == "content_block_start":
      cb = data["content_block"]
      if cb["type"] == "text":
          output_index += 1
          cur_item_kind = "message"
          cur_item_id   = "msg_" + token_hex(16)
          cur_text_buf  = ""
          emit response.output_item.added(output_index, item={message,in_progress,content:[]})
          emit response.content_part.added(cur_item_id, output_index, content_index=0, part={output_text,text:""})
          content_part_open = True
      elif cb["type"] == "tool_use":
          handle_tool_block_start(cb)     # §4
      elif cb["type"] in ("thinking","redacted_thinking"):
          # 见 §5：首版忽略（不为其开 Responses item）
          cur_item_kind = "thinking_skip"

  # === content_block_delta ===
  elif type == "content_block_delta":
      d = data["delta"]
      if d["type"] == "text_delta":
          txt = d["text"]
          cur_text_buf += txt
          emit response.output_text.delta(cur_item_id, output_index, content_index=0, delta=txt)
      elif d["type"] == "input_json_delta":
          handle_tool_args_delta(d["partial_json"])   # §4
      elif d["type"] == "thinking_delta":
          pass   # §5 首版忽略

  # === content_block_stop ===
  elif type == "content_block_stop":
      if cur_item_kind == "message":
          emit response.output_text.done(cur_item_id, output_index, content_index=0, text=cur_text_buf)
          emit response.content_part.done(cur_item_id, output_index, content_index=0, part={output_text,text:cur_text_buf})
          emit response.output_item.done(output_index, item={message,completed,content:[{output_text,cur_text_buf}]})
          content_part_open = False
      elif cur_item_kind == "function_call":
          handle_tool_block_stop()        # §4
      # thinking_skip: 不产出
      cur_item_kind = None

  # === message_delta ===
  elif type == "message_delta":
      usage_out = data.get("usage", {}).get("output_tokens", usage_out)
      final_stop_reason = data.get("delta", {}).get("stop_reason")
      # 不产出事件，记状态待 completed 用

  # === message_stop ===
  elif type == "message_stop":
      # 收尾：发 response.completed
      emit response.completed(完整骨架, status=completed, output=[所有已完成 item], usage={...})

  # === ping ===
  elif type == "ping":
      pass   # 丢弃，Responses 无 ping
```

### 3.5 item / index 管理规则 [实测样本]+[设计推断]

- Responses `output_index` 从 **0** 开始，每个 output item（message 或 function_call）占一个连续递增 index，与 Anthropic content block index **一一对应**（Anthropic index 0 的 text → Responses output_index 0）。
- 每个 Anthropic content block（text / tool_use）→ 一个 Responses output item，`output_index` 随之 +1。
- message item 内部 `content_index` 固定 `0`（单个 output_text part）`[实测样本]`。
- **item id 类型**：message item 用 `msg_` 前缀，function_call item 用 `item_` 前缀（[实测样本] 样本2 message 是 `msg_...`，样本4 function_call 是 `item_...`）`[实测样本]`。
- **thinking 块不占 output_index**（首版忽略，见 §5）；若二期支持，需为其单独开 reasoning item 并占一个 index，此时后续 index 需相应偏移 `[设计推断]`。

### 3.6 流式写回改造 [设计推断]

新写一个 `_write_responses_stream(status, headers, upstream_resp, ctx)`：
```
发送 200 + text/event-stream 头 + chunked
逐事件读取 upstream_resp（Anthropic SSE，按 "\n\n" 分块累积缓冲）:
    解析出 (event_type, data_json)   # Anthropic 双行 event:/data:
    for ev in adapter.feed(event_type, data_json):   # 状态机产出 0..N 个 Responses 事件 dict
        写出 chunked(responses_sse_bytes(ev))         # data: {json}\n\n（无 event: 行）
流结束（Anthropic message_stop 已触发 completed）→ 写 chunked 结束 0\r\n\r\n
异常 → BrokenPipe 静默；json 解析失败 → 记 log 跳过该事件
```
> Anthropic SSE 是 `event: <type>\ndata: {json}\n\n` 双行；需先解析出 event_type（也可只读 data JSON 里的 `type`，二者一致，见 [正向规格 §3.3]）。Responses 输出是**单 `data:` 行**（§3.3）`[设计推断]`。

---

## 4. 流式工具调用转换（模块D'）[实测样本 样本4]+[Anthropic]

### 4.1 Anthropic tool_use 流式形态 [正向规格 §3/§4]

Anthropic 工具块的事件序列（见 [正向规格 §3.2]）：
```
content_block_start   content_block:{type:"tool_use","id":"toolu_..","name":"get_weather","input":{}}
content_block_delta   delta:{type:"input_json_delta","partial_json":"{\"ci"}   # 可多次，片段拼接
content_block_delta   delta:{type:"input_json_delta","partial_json":"ty\":\"北京\"}"}
content_block_stop
```
- `id`、`name` 在 `content_block_start` 一次性给全（不像 OpenAI 分片给 name）。
- `partial_json` 是 arguments 的**字符串片段**，需顺序拼接成完整 arguments。

### 4.2 转换为 Responses function_call 事件序列 [实测样本 样本4，逐字段照抄]

Responses 侧工具序列（[实测样本 样本4]）：
```
response.output_item.added   item:{type:function_call,status:in_progress,call_id,name,arguments:""}
response.function_call_arguments.delta   item_id, output_index, delta:"{\""       # 可多次
response.function_call_arguments.delta   item_id, output_index, delta:"city"
...
response.function_call_arguments.done    item_id, name, output_index, arguments:"{\"city\":\"北京\"}"   # 完整
response.output_item.done    item:{type:function_call,status:completed,call_id,name,arguments:"{...}"}
```
> **关键差异**：工具 item **不发** `content_part.added` / `content_part.done`（那是 message item 专属）；工具参数增量用 `response.function_call_arguments.delta`（**不是** `output_text.delta`）`[实测样本]`。

**状态扩展（在 §3.4 基础上）**：
```
cur_call_id    = None    # 当前工具的 call_id
cur_tool_name  = None    # 当前工具名
cur_args_buf   = ""      # 累计 arguments 字符串（done 事件用）
```

**`handle_tool_block_start(cb)`**（Anthropic content_block_start 且 type=tool_use）：
```
output_index += 1
cur_item_kind = "function_call"
cur_item_id   = "item_" + token_hex(16)         # function_call item 用 item_ 前缀
cur_call_id   = cb.get("id")                     # Anthropic tool_use.id → Responses call_id 透传
cur_tool_name = cb.get("name", "")
cur_args_buf  = ""
emit response.output_item.added(output_index, item={
    "id": cur_item_id, "type":"function_call", "status":"in_progress",
    "call_id": cur_call_id, "name": cur_tool_name, "arguments": ""})
# 注意：Anthropic tool_use 的初始 input 通常为 {}，不据此发 delta；等 input_json_delta
```

**`handle_tool_args_delta(partial_json)`**（Anthropic input_json_delta）：
```
if partial_json:
    cur_args_buf += partial_json
    emit response.function_call_arguments.delta(
        item_id=cur_item_id, output_index=output_index, delta=partial_json)
```
> `partial_json` **原样透传**为 Responses 的 `delta`，不解析、不重拼；codex 负责把所有 delta 拼起来 parse。与正向的 input_json_delta 透传镜像 `[实测样本]+[设计推断]`。

**`handle_tool_block_stop()`**（Anthropic content_block_stop 且当前是 tool_use）：
```
emit response.function_call_arguments.done(
    item_id=cur_item_id, name=cur_tool_name, output_index=output_index, arguments=cur_args_buf)
emit response.output_item.done(output_index, item={
    "id": cur_item_id, "type":"function_call", "status":"completed",
    "call_id": cur_call_id, "name": cur_tool_name, "arguments": cur_args_buf})
```

### 4.3 多工具并发 [实测样本]+[设计推断]

Anthropic 一次响应可含多个 tool_use 块（index 递增）；每个块独立走 start→delta→stop，各自映射到独立的 Responses `output_index`：
```
text(idx0) → tool_use(idx1) → tool_use(idx2)
  ↓            ↓                ↓
output_index 0  output_index 1   output_index 2
(message)     (function_call)   (function_call)
```
- 每个 Anthropic content_block_start 触发 `output_index += 1` 并开新 item。
- Anthropic 的 content block **串行**（一个 stop 后才下一个 start），不会像 OpenAI 那样交错分片，故**无需 index 映射表**（比正向 §4 简单）`[设计推断]`。

### 4.4 关键边界处理

| 情形 | 处理 | 来源 |
|---|---|---|
| `input_json_delta.partial_json` 跨多帧断裂 | 每帧原样透传一个 `function_call_arguments.delta`，末尾用累计 buf 发 `.done` | [实测样本] |
| 工具无参数（input 为空 `{}`） | 不发 delta；`.done` 的 arguments 用 `"{}"` 或空串（实测样本4 有参数，无参数场景 [设计推断] 用 `"{}"`） | [设计推断] |
| tool_use.id 缺失 | 自生成 `call_<hex>` 兜底当 call_id | [设计推断] |
| 文本块 + 工具块混合 | text 块走 §3.4 message 分支，tool 块走本节；各占独立 output_index | [实测样本] |
| Anthropic `message_delta.stop_reason=tool_use` | 不改 Responses status（仍 completed）；codex 见 output 有 function_call 自会执行工具（见 §2.2） | [设计推断] |

---

## 5. 边界与降级

### 5.1 转换失败返回合法 Responses error [设计推断]

任何转换阶段抛异常，都要返回**结构合法的 Responses 错误响应**，让 codex 正常显示而非挂死。

**非流式**（HTTP 4xx/5xx body）——OpenAI/Responses error 结构：
```json
{"error":{"message":"proxy translate failed: <原因>","type":"invalid_request_error","code":null,"param":null}}
```
> 请求转换失败用 `invalid_request_error`（400）；上游/响应转换失败用 `server_error`（500）。Responses error 结构与 Chat Completions 一致（`error.{message,type,code,param}`）`[设计推断]`。

**流式**（已开始发 SSE 后出错）——Responses 错误事件：
```
data: {"type":"response.error","error":{"message":"...","type":"server_error"},"sequence_number":S}
```
或用 `response.failed`（Responses 有 `status:"failed"` 态）：
```
data: {"type":"response.failed","response":{...骨架, "status":"failed", "error":{...}},"sequence_number":S}
```
> `[设计推断]`：实测样本未覆盖错误事件；优先用 `response.failed` 携带完整骨架，codex 更可能识别。若 `response.created` 还没发就出错，直接返回非流式 error body（HTTP 500）。

### 5.2 上游 Anthropic HTTP 错误透传 [设计推断]

网关 Anthropic 端点返回 4xx/5xx（Anthropic error 格式 `{type:"error",error:{type,message}}`，见 [正向规格 §5.1]）时，**转成 Responses error 结构**再返回，不要原样透传（codex 解析字段不同）。映射：Anthropic `error.type` → Responses `error.type`（`invalid_request_error`/`authentication_error`→ 保留同名；`api_error`/`overloaded_error` → `server_error`）。

### 5.3 reasoning 透传决策（反向特有负担）[设计推断]

Claude 会产 `thinking` 块（非流式 content 里的 `thinking` block；流式的 `thinking_delta` / `signature_delta`）。Responses 侧有 `reasoning` 位（顶层 `reasoning:{effort,summary}`，以及可能的 `reasoning` 类型 output item）。三种策略：

| 策略 | 做法 | 取舍 |
|---|---|---|
| **A. 首版：丢弃**（推荐） | thinking 块/delta 全部忽略，不进 output；顶层 `reasoning.summary=null`、`reasoning.effort` 回显请求值 | 最简单、最稳；codex 主要消费 output_text/function_call，thinking 非必需。**首版采用** |
| B. 聚合到 summary | 非流式时把 thinking 文本拼接进顶层 `reasoning.summary` | 需确认 codex 是否渲染 summary；实测样本 summary 恒为 null，未验证 |
| C. 转 reasoning output item | 为 thinking 开独立 `{type:"reasoning",...}` output item + 对应流式事件 | Responses reasoning item 结构未实测，且要占 output_index、加流式事件类型，复杂度高。**不做，列为三期** |

> `[设计推断]`：**首版走 A（丢弃）**。§2.1 / §3.4 中 thinking 分支均为 `pass`。`output_tokens_details.reasoning_tokens` 保持 0（§2.3）。这是反向转换相对正向多出的负担——正向是"OpenAI 无 thinking，Anthropic 有位子选择不填"，反向是"Claude 主动产 thinking，需主动丢弃"。

### 5.4 已知不处理/降级项（明确列出）

| 项 | 处理 | 原因 |
|---|---|---|
| Claude `thinking` / `redacted_thinking` 块 | 丢弃（§5.3 策略 A） | Responses reasoning item 未实测，首版不透传 |
| Responses `previous_response_id`（有状态会话） | 丢弃 + 记 log | 本 proxy 无状态，无法续接会话 |
| Responses `store` / `background` / `conversation` | 丢弃 | 平台侧字段，Anthropic 不认 |
| Responses `text.format`（非 text，如 json_schema 结构化输出） | 首版丢弃；如 codex 用到需二期支持 | Anthropic 结构化输出走 tool，未实测映射 |
| Responses 托管工具（web_search/file_search/computer_use） | 跳过（§1.5） | Anthropic 端点不支持这些 Responses 内置工具 |
| Anthropic `stop_reason` 精确态（max_tokens 截断） | Responses status 统一 completed（§2.2） | 实测未见 incomplete，保守处理 |
| `obfuscation` 字段（流式 delta） | 省略不发 | codex 不依赖 |
| `conversation.id` | 首版省略，报错则补 `conv_` 占位 | 无状态 proxy 无真实会话 id |
| `total_tokens` | 自行相加 input+output | Anthropic 不返回该字段 |
| `sequence_number` | 状态机全局单调递增维护 | Responses 强制要求，Anthropic 无此概念 |

---

## 6. 模块化 + 四组合统一分发

### 6.1 反向模块函数签名（标准库）[设计推断]

| 模块 | 函数 | 职责 |
|---|---|---|
| A' 请求转换 | `req_responses_to_anthropic(body, model_is_reasoning) -> (anthropic_body, ctx)` | §1 全部；纯 dict→dict，无 IO |
| B' 非流式响应 | `resp_anthropic_to_responses(anthropic_resp, ctx) -> responses_dict` | §2 全部；纯 dict→dict，无 IO |
| C'+D' 流式状态机 | `class ResponsesStreamAdapter` | §3+§4；`feed(event_type, data) -> list[event_dict]`，`finalize() -> list[event_dict]` |
| 辅助 | `map_reasoning`, `map_tool_choice`, `responses_content_to_anthropic_blocks`, `responses_image_to_anthropic_source`, `responses_sse_bytes(data) -> bytes` | 无状态纯函数 |

```python
# 模块 A'
def req_responses_to_anthropic(body: dict, model_is_reasoning: bool) -> tuple[dict, dict]: ...
    # in: Responses body dict；out: (Anthropic body dict, ctx dict)

# 模块 B'
def resp_anthropic_to_responses(anthropic_resp: dict, ctx: dict) -> dict: ...
    # in: Anthropic 非流式响应 dict；out: Responses 响应 dict（照 §2.4 完整结构）

# 模块 C'+D'（有状态）
class ResponsesStreamAdapter:
    def __init__(self, ctx: dict, model: str): ...
    def feed(self, event_type: str, data: dict) -> list[dict]: ...   # 喂一个 Anthropic 事件，返回 0..N 个 Responses 事件 dict（已带 sequence_number）
    def finalize(self) -> list[dict]: ...                             # 流意外结束时补 response.completed/failed

# 辅助
def responses_sse_bytes(data: dict) -> bytes: ...
    # data 含 "type" 键与已填好的 "sequence_number"；产出 b"data: <json>\n\n"（无 event: 行，见 §3.3）
```
> 状态机产出**事件 dict 列表**（`sequence_number` 由状态机内部计数器填好），外层写回函数用 `responses_sse_bytes` 序列化 + chunked 写出，可脱离 HTTP 单测 `[设计推断]`。

### 6.2 四组合统一分发逻辑（核心工程决策）[设计推断]

proxy 需支持 4 种「source 协议 × target 上游」组合，其中 2 种透传、2 种转换：

| # | source（客户端发来） | target（配置指向的上游） | 处理 | 转换器 |
|---|---|---|---|---|
| 1 | claudecode → Anthropic `/v1/messages` | 网关 Anthropic 端点 | **透传** | 无（原样转发） |
| 2 | codex → Responses `/v1/responses` | 网关 GPT/Responses 端点 | **透传** | 无（原样转发） |
| 3 | claudecode → Anthropic `/v1/messages` | 网关 GPT 端点（Chat Completions） | **正向转换** | 正向规格 模块 A/B/C/D |
| 4 | codex → Responses `/v1/responses` | 网关 Anthropic 端点 | **反向转换**（本文档） | 模块 A'/B'/C'+D' |

**识别 source 协议**（看请求 path + body 特征）：
```
def detect_source(path: str, body: dict) -> str:
    if path.endswith("/v1/responses"):        return "responses"    # codex
    if path.endswith("/v1/messages"):          return "anthropic"    # claudecode
    if path.endswith("/chat/completions"):     return "chat"         # 兜底：老式 chat（若有）
    # body 特征兜底：有 "input"/"instructions" → responses；有 "system"/"messages"+"max_tokens" → anthropic
    if "input" in body or "instructions" in body:  return "responses"
    if "messages" in body:                          return "anthropic"
    return "unknown"
```

**确定 target 协议**（从 proxy 配置/目标 URL 得知）：
```
def detect_target(target_url: str) -> str:
    if "/v1/anthropic/" in target_url:  return "anthropic"
    if "/v1/responses" in target_url:   return "responses"
    if "/chat/completions" in target_url: return "chat"       # OpenAI native chat（正向的 target）
    return "unknown"
```

**分发决策表**：
```
def pick_translator(source, target):
    if source == target:                                    return PASSTHROUGH        # 组合1/2
    if source == "anthropic" and target == "chat":          return FORWARD_TRANSLATOR # 组合3（正向规格）
    if source == "responses" and target == "anthropic":     return REVERSE_TRANSLATOR # 组合4（本文档）
    # 其余组合（如 responses→chat、anthropic→responses）未实现
    return UNSUPPORTED  # → 返回对应 source 协议的合法 error（§5.1）
```

**接入 `_forward()`**：
```
source = detect_source(req_path, parsed_body)
target = detect_target(target_url)
mode   = pick_translator(source, target)

if mode == PASSTHROUGH:
    走现有字节透传逻辑（含流式 8192 字节透传）
elif mode == FORWARD_TRANSLATOR:
    走正向规格：req_anthropic_to_openai → 发 chat 端点 → resp/stream 转回 Anthropic
elif mode == REVERSE_TRANSLATOR:
    body2, ctx = req_responses_to_anthropic(parsed_body, model_is_reasoning)
    target_url 改写为 Anthropic messages 路径；raw_body = body2
    发上游后：
        非流式 → resp_anthropic_to_responses(resp, ctx) → 写 Responses JSON
        流式   → _write_responses_stream(..., ResponsesStreamAdapter(ctx, model))
elif mode == UNSUPPORTED:
    返回 source 协议对应的合法 error
```
> **错误响应也要按 source 协议返回**：source=responses 出错返回 Responses error（§5.1），source=anthropic 出错返回 Anthropic error（正向规格 §5.1）。分发层需记住 source 以选对错误格式 `[设计推断]`。
> **鉴权**：4 种组合发往网关都用 `Authorization: Bearer <appkey>`，复用现有 appkey rotate；反向分支不触发正向的 thinking 格式重试（那是 Anthropic 上游专属）`[设计推断]`。

### 6.3 id 生成 [实测样本]+[设计推断]

- response id：`"resp_" + secrets.token_hex(16)`（[实测样本] 形如 `resp_1621465b45f7433c8b774b8a8230a215`，32 位 hex）。
- message item id：`"msg_" + token_hex(16)`（[实测样本] `msg_bd9fd384b3284232af347eca909c2eea`）。
- function_call item id：`"item_" + token_hex(16)`（[实测样本] `item_48b4803b0d584e12bf5ccd53fb79ad19`）。
- call_id：**优先透传** Anthropic `tool_use.id`（形如 `toolu_...` 或网关回的 `call_...`）；codex 只要求 function_call.call_id 与后续 function_call_output.call_id 能对上（§1.3 invariant）。缺失时 `"call_" + token_hex(12)` 兜底。
- conversation id（若需要）：`"conv_" + token_hex(16)`。

### 6.4 单测建议

| 模块 | 独立可测 | 测法 |
|---|---|---|
| A' | ✅ 纯函数 | 喂各种 Responses body：`input` 为字符串 / items 数组（message+function_call+function_call_output 混排）；断言输出 Anthropic messages 分组正确（tool_use 归 assistant、tool_result 归下一条 user、call_id 对齐）。effort 三档、tools 扁平→input_schema、tool_choice 四态、max_tokens 兜底各单测。 |
| B' | ✅ 纯函数 | 喂 Anthropic 响应：纯 text / 带 tool_use（断言 arguments 是 JSON 字符串）/ 各 stop_reason / 含 thinking（断言被丢弃）；断言 Responses 完整结构（照样本1/3 逐字段：id/object/status/output/usage/total_tokens 相加）。 |
| C'+D' | ✅ 状态机脱网络 | 准备录制的 Anthropic 事件序列（引用正向规格 §3 的事件），逐个 feed，收集事件断言序列与每事件 JSON。重点用例：<br>1. 纯文本：created→in_progress→output_item.added(msg)→content_part.added→output_text.delta×N→output_text.done→content_part.done→output_item.done→completed，且 sequence_number 0..N 连续<br>2. 单工具：…→output_item.added(function_call)→function_call_arguments.delta×N→.done→output_item.done→completed（无 content_part 事件）<br>3. 文本+工具混合：output_index 0(message)、1(function_call)<br>4. 多工具并发：output_index 递增各成 item<br>5. thinking 块：thinking_delta 被忽略，不产出事件、不占 index<br>6. arguments 跨帧断裂：partial_json 逐片透传为 function_call_arguments.delta<br>7. usage：completed 事件 total_tokens = input+output |
| 分发层 | ✅ 纯函数 | `detect_source` / `detect_target` / `pick_translator` 对 4 组合 + unsupported 组合断言正确路由。 |
| 端到端 | 集成测 | mock 网关 Anthropic 端点（本地起返回录制 Anthropic SSE 的 HTTP server），codex 侧发 Responses 请求走完整 proxy，断言收到合法 Responses SSE（以 `response.completed` 收尾、sequence_number 连续）。 |

---

## 附录：来源清单

- `[实测样本]`：`tools/model_proxy/samples/responses_api_samples.txt`（美团网关 `/v1/responses` 端点 2026-07-17 实测）——样本1（非流式基础）、样本3（工具调用非流式）、样本2（流式文本 seq 0..10）、样本4（流式工具 seq 0..10）。Responses 侧所有字段结构、id 格式、事件序列、sequence_number 递增、wire format（单 data 行、无 [DONE]）均以此为准。
- `[正向规格]`：`tools/model_proxy/proxy_translate_spec.md`——Anthropic 侧字段定义（§1.2 system、§1.3 messages/content block、§1.4 thinking/effort、§1.5 tools/input_schema、§1.6 tool_choice、§2.5 非流式响应结构、§3.1/§3.2 流式事件序列与各事件 JSON、§3.3 wire format、§5.1 Anthropic error 枚举）。
- `[Responses]`：OpenAI Responses API 语义（codex-cli 依赖）——`instructions`/`input`(items: message/function_call/function_call_output)/`tools`(扁平 function)/`reasoning.effort`/`tool_choice`/`max_completion_tokens`。
- `[Anthropic]`：Anthropic Messages API 语义——messages 分组、tool_use/tool_result block 对齐、max_tokens 必填。
- `[设计推断]`：本文档工程决策——分组算法、reasoning 丢弃策略、四组合分发、id 生成、错误按 source 协议返回、无状态降级项。

