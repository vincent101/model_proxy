# Proxy 协议转换器规格：Anthropic ↔ OpenAI

> 用途：本地 proxy（`tools/proxy.py`，纯 Python 标准库）新增能力——把 Claude Code 发出的 Anthropic `/v1/messages` 请求转换成 OpenAI Chat Completions 格式，发给美团网关 native 端点 `aigc.sankuai.com/v1/openai/native/chat/completions`，再把 OpenAI 响应（含流式 SSE、工具调用）转换回 Anthropic 格式返回。
>
> 本文档为**编码地基**，字段级精确，可直接照写代码。不含实现代码，逻辑用伪代码/状态转移描述。
>
> **来源标注约定**：
> - `[LiteLLM]`：从 LiteLLM `experimental_pass_through/adapters/{transformation.py, streaming_iterator.py}` 源码查证
> - `[Anthropic]`：从 Anthropic Messages API 流式规范查证
> - `[网关实测]`：任务给定的美团 native 端点已实测事实
> - `[设计推断]`：本文档基于上述事实做的工程设计决策，实现时可调整

---

## 0. 总体数据流与职责边界

```
Claude Code
  │  POST /v1/messages   (Anthropic body, header anthropic-version/anthropic-beta)
  ▼
proxy.py  ── _forward() 中新增分支：命中 /v1/messages 且目标是 native 端点 ──▶
  │
  ├─[模块A] req_anthropic_to_openai(anthropic_body) → (openai_body, ctx)
  │         ctx 含 tool_name_mapping、stream 标志
  │
  │  POST native/chat/completions   (OpenAI body, Bearer appkey)
  ▼
美团网关 native 端点
  │
  ├─ 非流式：{object:"chat.completion", choices, usage, content_filter_results}
  │     └─[模块B] resp_openai_to_anthropic(openai_resp, ctx) → anthropic_resp(dict)
  │
  └─ 流式：OpenAI SSE (data: {chunk} ... data: [DONE])
        └─[模块C+D] AnthropicStreamAdapter(openai_sse_lines, ctx)
              产出 Anthropic SSE 事件序列 (event: xxx\ndata:{json}\n\n)
  ▼
Claude Code
```

**关键约束**：
- 全程只用标准库（`json`、`hashlib`、`urllib`、`http.server`）。
- 转换器输入输出一律是 `dict` / `bytes` / 字符串迭代器，不引入类型库。
- 转换失败必须返回**结构合法的 Anthropic error**，绝不让客户端挂死（见 §5）。
- 现有 proxy 的 `_write_streaming_response` 是逐 8192 字节透传的 chunked 转发；本能力需要**改写流式路径**：不再字节透传，而是逐行读 OpenAI SSE、经状态机转换、再逐事件写出 Anthropic SSE（见 §3.6）。

---

## 1. 请求转换：Anthropic → OpenAI（模块A）

**签名（设计推断）**：
```
req_anthropic_to_openai(body: dict, model_is_reasoning: bool) -> tuple[dict, dict]
    返回 (openai_body, ctx)
    ctx = {
        "tool_name_mapping": dict[str,str],   # truncated_name → original_name，用于响应还原
        "stream": bool,
        "request_model": str,                 # 回填响应 model 字段用
    }
```

### 1.1 顶层字段映射表

| Anthropic 字段 | OpenAI 字段 | 规则 | 来源 |
|---|---|---|---|
| `model` | `model` | 原样透传（proxy 上游已做 model_map 映射，见 proxy.py:491） | [网关实测] |
| `max_tokens` | `max_completion_tokens` | 直接改名。native 端点是 OpenAI native，用 `max_completion_tokens`（reasoning 模型场景 `max_tokens` 已被 OpenAI 废弃） | [设计推断] |
| `system` | `messages[0]` (role=system) | 见 §1.2 | [LiteLLM] |
| `messages` | `messages` | 见 §1.3，system 消息 insert 到 index 0 之后 | [LiteLLM] |
| `thinking` + `output_config.effort` | `reasoning_effort` | 见 §1.4 | [网关实测]+[设计推断] |
| `tools` | `tools` | 见 §1.5 | [LiteLLM] |
| `tool_choice` | `tool_choice` | 见 §1.6 | [LiteLLM] |
| `stream` | `stream` | 原样透传；若 `stream=true` 追加 `stream_options.include_usage=true`（见 §1.7） | [设计推断] |
| `stop_sequences` | `stop` | 直接改名（都是字符串数组） | [LiteLLM] |
| `temperature` | `temperature` | 原样透传 | [LiteLLM] |
| `top_p` | `top_p` | 原样透传 | [LiteLLM] |
| `metadata.user_id` | `user` | 若存在则映射（LiteLLM 做法；可选） | [LiteLLM] |
| `anthropic-version` / `anthropic-beta`（header） | — | 丢弃，不转发给 OpenAI 端点 | [设计推断] |

**未列出的顶层参数**：LiteLLM 做法是维护一个 `translatable_anthropic_params()` 集合，不在集合内的键原样拷贝过去 `[LiteLLM]`。**本实现建议采用白名单**：只转换上表字段，其余（如 Anthropic 特有的 `container`、`mcp_servers` 等）丢弃并记 log，避免把 OpenAI 端点不认识的字段透传导致 400（见 §5）`[设计推断]`。

### 1.2 system 转换 [LiteLLM]

`system` 可为**字符串**或 **content block 数组**。

```
if body 无 "system" 或为空:
    不产生 system 消息
elif isinstance(system, str):
    openai_messages 头部 insert {"role":"system", "content": system}
elif isinstance(system, list):   # content block 数组
    text_parts = []
    for block in system:
        if block.get("type") == "text":
            text_parts.append(block["text"])
        # 忽略非 text block（system 里一般只有 text）
    # 设计推断：native 端点 system content 用纯字符串更稳妥，拼接为一个字符串
    openai_messages 头部 insert {"role":"system", "content": "\n".join(text_parts)}
```
> 注：LiteLLM 对 Claude 上游会保留 `[{type:text,...}]` 列表形式以带 `cache_control`；但 native 端点是 OpenAI 格式，`cache_control` 不被支持，**统一降级为纯字符串** `[设计推断]`。

### 1.3 messages[].content 转换 [LiteLLM]

`messages` 是数组，每条 `{role, content}`。`content` 可为**字符串**或 **block 数组**。

#### 1.3.1 role == "user"

```
if isinstance(content, str):
    输出一条 {"role":"user", "content": content}
elif isinstance(content, list):
    分两类累积：
      normal_parts = []      # 文本/图片 → 归到一条 user 消息
      tool_result_msgs = []  # 每个 tool_result → 一条独立的 role:tool 消息

    for block in content:
        type = block["type"]
        if type == "text":
            normal_parts.append({"type":"text", "text": block["text"]})
        elif type == "image":
            url = _anthropic_image_to_data_url(block["source"])   # 见下
            normal_parts.append({"type":"image_url", "image_url":{"url": url}})
        elif type == "tool_result":
            tool_result_msgs.append(_tool_result_to_openai(block))  # 见 §1.3.3
        # 其他 type 忽略并记 log

    # 输出顺序（设计推断）：Anthropic 里 tool_result 通常在 user turn 开头，
    # OpenAI 要求 role:tool 消息紧跟在触发它的 assistant tool_calls 之后。
    # 简化规则：先输出所有 tool_result_msgs，再输出 normal_parts（若非空）
    先 extend(tool_result_msgs)
    if normal_parts:
        若只有一个 text → content 用字符串；否则用 parts 数组
        输出 {"role":"user", "content": <str or parts>}
```

**图片转换 `_anthropic_image_to_data_url(source)`** [LiteLLM]：
```
if source["type"] == "base64":
    media_type = source.get("media_type", "image/jpeg")
    return f'data:{media_type};base64,{source["data"]}'
elif source["type"] == "url":
    return source.get("url", "")
else:
    return None   # 调用方跳过该 block
```

#### 1.3.2 role == "assistant"

```
if isinstance(content, str):
    输出 {"role":"assistant", "content": content}
elif isinstance(content, list):
    text_parts = []
    tool_calls = []
    for block in content:
        type = block["type"]
        if type == "text":
            text_parts.append(block["text"])
        elif type == "tool_use":
            tool_calls.append({
                "id": block["id"],
                "type": "function",
                "function": {
                    "name": truncate_tool_name(block["name"]),   # §1.5.1
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                }
            })
        elif type == "thinking" / "redacted_thinking":
            # native 端点不吃 thinking block，丢弃（reasoning 由 reasoning_effort 控制）
            忽略并记 log
    msg = {"role":"assistant"}
    msg["content"] = "\n".join(text_parts) if text_parts else None   # 可为 None/空
    if tool_calls: msg["tool_calls"] = tool_calls
    输出 msg
```
> 注意：assistant 消息里 `tool_use.input` 是 **dict**，OpenAI 的 `function.arguments` 要求是 **JSON 字符串**，必须 `json.dumps` `[LiteLLM]`。

#### 1.3.3 tool_result block → role:tool 消息 [LiteLLM]

`_tool_result_to_openai(block)`：
```
tool_call_id = block["tool_use_id"]
inner = block.get("content")   # 可为 字符串 / block 数组
if isinstance(inner, str):
    content = inner
elif isinstance(inner, list):
    if 只有一个 text block:      content = 该 text
    elif 只有一个 image block:   content = data_url（部分 OpenAI 端点 role:tool 不支持图片，见下）
    else:                        content = [ {type:text,...} / {type:image_url,...} ... ]
else:
    content = ""
返回 {"role":"tool", "tool_call_id": tool_call_id, "content": content}
```
> **边界（设计推断）**：OpenAI 经典 Chat Completions 的 `role:tool` 消息 `content` 只接受字符串。若 tool_result 含图片，native 端点是否支持数组形式未实测。**保守策略**：tool_result 含非文本内容时，把图片降级为占位文本 `[image omitted]` 或把数组 JSON 序列化为字符串，避免 400。首版可先只支持文本 tool_result，图片场景记 log 降级。`invariant`：一个 tool_result → 恰好一条 role:tool 消息，一个 tool_call_id。

### 1.4 thinking + output_config.effort → reasoning_effort

**网关事实** `[网关实测]`：`reasoning_effort` 仅 `low`/`medium`/`high` 三档，**无 `max`/`xhigh`**。

Anthropic 侧 Claude Code 发出的两种形态：
- 形态1：`thinking:{type:"adaptive"}` + `output_config:{effort: "low"|"medium"|"high"|"max"|"xhigh"}`
- 形态2：`thinking:{type:"enabled", budget_tokens: N}`

**映射规则（设计推断，参考 LiteLLM 的 budget→effort 分档）**：

| Anthropic 输入 | reasoning_effort | 说明 |
|---|---|---|
| `output_config.effort = "low"` | `low` | |
| `output_config.effort = "medium"` | `medium` | |
| `output_config.effort = "high"` | `high` | |
| `output_config.effort = "max"` | `high` | **降级**：网关无 max，取最高档 high |
| `output_config.effort = "xhigh"` | `high` | **降级**：同上 |
| `thinking.type = "enabled"`, `budget_tokens < 2000` | `low` | LiteLLM 分档：`<2000→low` |
| `thinking.type = "enabled"`, `2000 ≤ budget < 32000` | `medium` | `[LiteLLM]` 分档 |
| `thinking.type = "enabled"`, `budget ≥ 32000` | `high` | `[LiteLLM]` 分档 |
| `thinking` 缺失/为 None 且无 `output_config.effort` | 不设 `reasoning_effort` | 非 reasoning 请求 |

```
def map_reasoning_effort(body) -> str | None:
    oc = body.get("output_config") or {}
    effort = oc.get("effort")
    if effort in ("low","medium","high"):  return effort
    if effort in ("max","xhigh"):          return "high"   # 降级
    thinking = body.get("thinking") or {}
    if thinking.get("type") == "enabled":
        b = thinking.get("budget_tokens", 10000)
        return "low" if b < 2000 else ("high" if b >= 32000 else "medium")
    if thinking.get("type") == "adaptive" and not effort:
        return "medium"   # 设计推断：adaptive 无 effort 时给中档
    return None
```
> 只有目标 model 是 reasoning 模型时才发 `reasoning_effort`（非 reasoning 模型带此参数可能 400）。是否 reasoning 由 proxy 配置/model 名判断 `[设计推断]`。

### 1.5 tools 转换 [LiteLLM]

Anthropic tool：`{name, description, input_schema:{type:"object", properties, required}}`
OpenAI tool：`{type:"function", function:{name, description, parameters}}`

```
def translate_tools(anthropic_tools) -> tuple[list, dict]:
    openai_tools = []
    tool_name_mapping = {}          # truncated → original
    mapped = {"name","input_schema","description","cache_control","type"}
    for idx, tool in enumerate(anthropic_tools):
        # Anthropic 托管工具（type 以 web_search/computer 等前缀）——native 端点不认，跳过或降级
        original = tool.get("name") or f"litellm_unnamed_tool_{idx}"
        truncated = truncate_tool_name(original)
        if truncated != original:
            tool_name_mapping[truncated] = original
        func = {
            "name": truncated,
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type":"object","properties":{}}),
        }
        # LiteLLM：input_schema 之外的额外键（computer-use 等）并入 parameters；
        # 本实现可忽略这些非标准键 [设计推断]
        openai_tools.append({"type":"function", "function": func})
    return openai_tools, tool_name_mapping
```

#### 1.5.1 工具名 >64 字符截断 [LiteLLM]

OpenAI 工具名上限 64（`OPENAI_MAX_TOOL_NAME_LENGTH`）。
```
def truncate_tool_name(name: str) -> str:
    if len(name) <= 64:
        return name
    h = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"{name[:55]}_{h}"          # 55 + 1('_') + 8 = 64
```
- 只在真的截断时写入 `tool_name_mapping[truncated] = original`。
- **还原**发生在响应侧（§2.2、§4）：`tool_name_mapping.get(name, name)`。

### 1.6 tool_choice 三态转换 [LiteLLM]

Anthropic `tool_choice`：`{type: "auto"|"any"|"tool"|"none", name?}`

| Anthropic | OpenAI `tool_choice` |
|---|---|
| `{type:"auto"}` | `"auto"` |
| `{type:"any"}` | `"required"` |
| `{type:"tool", name:X}` | `{"type":"function", "function":{"name": truncate_tool_name(X)}}` |
| `{type:"none"}` | `"none"` |
| 其他 | 抛错 → 走 §5 降级；或保守设为 `"auto"` |

> `tool` 态的 name 必须走 `truncate_tool_name` 保持与 §1.5 一致 `[LiteLLM]`。

### 1.7 stream 与 usage

```
openai_body["stream"] = bool(body.get("stream"))
if openai_body["stream"]:
    openai_body["stream_options"] = {"include_usage": True}   # 让末尾 chunk 带 usage
```
> `[设计推断]`：网关实测流式返回是标准 OpenAI SSE。`include_usage=true` 是 OpenAI 让最后带一个只含 `usage` 的 chunk 的标准做法，用于回填 Anthropic `message_delta.usage.output_tokens`。若网关不支持该选项也不影响（缺 usage 时 output_tokens 回填 0，见 §3.4）。

---

## 2. 非流式响应转换：OpenAI → Anthropic（模块B）

**输入** `[网关实测]`：
```json
{"object":"chat.completion",
 "choices":[{"message":{"content": "...", "tool_calls":[...]?}, "finish_reason":"stop"}],
 "usage":{"prompt_tokens":N, "completion_tokens":M,
          "completion_tokens_details":{"reasoning_tokens":K}},
 "content_filter_results": {...}}
```

**签名（设计推断）**：`resp_openai_to_anthropic(openai_resp: dict, ctx: dict) -> dict`

### 2.1 content 组装

```
choice = openai_resp["choices"][0]
message = choice["message"]
content_blocks = []

# 文本
text = message.get("content")
if text:   # 非空字符串
    content_blocks.append({"type":"text", "text": text})

# 工具调用
for tc in message.get("tool_calls") or []:
    fn = tc["function"]
    raw_args = fn.get("arguments", "") or "{}"
    try:    parsed = json.loads(raw_args)
    except: parsed = {}          # 参数非法 JSON 时降级为空对象并记 log
    name = ctx["tool_name_mapping"].get(fn["name"], fn["name"])   # 还原原名
    content_blocks.append({
        "type":"tool_use",
        "id": tc.get("id") or gen_toolu_id(),
        "name": name,
        "input": parsed,
    })
```

### 2.2 stop_reason 映射 [LiteLLM]

| OpenAI `finish_reason` | Anthropic `stop_reason` |
|---|---|
| `"stop"` | `"end_turn"` |
| `"length"` | `"max_tokens"` |
| `"tool_calls"` | `"tool_use"` |
| `"content_filter"` | `"end_turn"`（映射到 end_turn，同时把 content_filter_results 记 log；Anthropic 无对应枚举，见下） |
| `null` / 其他 | `"end_turn"`（默认） |

> `content_filter` 归入 `end_turn` 是 `[设计推断]`：Anthropic `stop_reason` 合法枚举为 `end_turn/max_tokens/stop_sequence/tool_use/pause_turn/refusal`，无 content_filter；映射 end_turn 最安全，避免客户端解析异常。

### 2.3 usage 映射 [LiteLLM]

```
u = openai_resp.get("usage") or {}
anthropic_usage = {
    "input_tokens":  u.get("prompt_tokens", 0),
    "output_tokens": u.get("completion_tokens", 0),
}
# reasoning_tokens：Anthropic 顶层 usage 无该字段。completion_tokens 已包含 reasoning，
# 无需额外加。可选：不做特殊处理，直接丢弃 reasoning_tokens。[设计推断]
```
> LiteLLM 还会拆 `cache_read_input_tokens` / `cache_creation_input_tokens`；native 端点未返回缓存字段，本实现忽略缓存 `[设计推断]`。

### 2.4 content_filter_results 处理

`[设计推断]`：**丢弃**，不放进 Anthropic 响应（Anthropic 无对应字段，放进去客户端会忽略或报错）。可在 log 记录一行 `content_filter triggered` 便于排查。

### 2.5 补齐 Anthropic 必需字段 [LiteLLM]

```
{
  "id": openai_resp.get("id") or gen_msg_id(),   # 见 §6.3 id 格式
  "type": "message",
  "role": "assistant",
  "model": ctx["request_model"],                 # 回填请求的 model 名（客户端期望）
  "content": content_blocks,                       # §2.1
  "stop_reason": <映射结果>,                        # §2.2
  "stop_sequence": null,                           # native 端点不返回具体命中的 stop 串
  "usage": anthropic_usage,                         # §2.3
}
```
> `stop_sequence` 固定 `null`（除非能从 finish_reason=stop 时精确判断命中的序列，网关未返回，故 null）`[设计推断]`。

---

## 3. 流式 SSE 转换：OpenAI → Anthropic（模块C，核心）

这是最难部分。给出完整**事件状态机**。

### 3.1 Anthropic 流式事件序列（正常一次带工具的完整序列）[Anthropic]

```
event: message_start
data: {message 骨架，usage 初值}

event: content_block_start        # 第一个块（如 text）
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: ping
data: {"type":"ping"}             # 可选心跳，可穿插任意位置

event: content_block_delta        # 文本增量，可多次
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hel"}}
...
event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: content_block_start        # 工具块
data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_...","name":"get_weather","input":{}}}

event: content_block_delta        # 工具参数增量，可多次
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"loc"}}
...
event: content_block_stop
data: {"type":"content_block_stop","index":1}

event: message_delta              # 收尾：stop_reason + 累计 usage
data: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":57}}

event: message_stop
data: {"type":"message_stop"}
```

### 3.2 各事件精确 JSON 结构 [Anthropic] + [LiteLLM]

**message_start**（`[LiteLLM]` 产出的骨架）：
```json
{"type":"message_start",
 "message":{
   "id":"msg_<uuid>", "type":"message", "role":"assistant",
   "content":[], "model":"<request_model>",
   "stop_reason":null, "stop_sequence":null,
   "usage":{"input_tokens":0,"output_tokens":0,
            "cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}
```
> 初始 usage 全 0 是 LiteLLM 有意为之（给 Claude Code 信号：支持 prompt caching）；真实 token 在末尾 `message_delta` 给。若能从上游首帧拿到 prompt_tokens 可回填 input_tokens，否则填 0 `[设计推断]`。

**content_block_start（text）**：
```json
{"type":"content_block_start","index":I,"content_block":{"type":"text","text":""}}
```
**content_block_start（tool_use）**：
```json
{"type":"content_block_start","index":I,"content_block":{"type":"tool_use","id":"toolu_<id>","name":"<name>","input":{}}}
```
**content_block_delta（text_delta）**：
```json
{"type":"content_block_delta","index":I,"delta":{"type":"text_delta","text":"<片段>"}}
```
**content_block_delta（input_json_delta）**：
```json
{"type":"content_block_delta","index":I,"delta":{"type":"input_json_delta","partial_json":"<JSON片段字符串>"}}
```
**content_block_stop**：
```json
{"type":"content_block_stop","index":I}
```
**message_delta**：
```json
{"type":"message_delta","delta":{"stop_reason":"<映射值>","stop_sequence":null},"usage":{"output_tokens":M}}
```
> `message_delta` 的 `delta` 只含 `stop_reason`、`stop_sequence`；`usage` 只放 `output_tokens`（累计输出）。`[Anthropic]`

**message_stop**：
```json
{"type":"message_stop"}
```
**ping**：
```json
{"type":"ping"}
```

### 3.3 wire format [Anthropic]

每个事件两行 + 空行：
```
event: <type>\n
data: <紧凑JSON，无多余空格>\n
\n
```
- `event:` 行的值与 data JSON 里的 `type` 一致。
- JSON 用 `json.dumps(obj, ensure_ascii=False, separators=(",",":"))`（紧凑）。
- 结尾必须有一个空行 `\n\n` 分隔事件。
- 响应头：`Content-Type: text/event-stream`，`Cache-Control: no-cache`；用 chunked（现有 `_write_streaming_response` 的 chunked 写法可复用，但改成逐事件写，见 §3.6）。

### 3.4 OpenAI chunk → Anthropic 事件 状态机 [LiteLLM 逻辑改写]

**状态变量**（`[LiteLLM]` 的 flag 对齐）：
```
sent_message_start        = False    # 是否已发 message_start
block_open                = False    # 当前是否有 content block 处于 open 状态
cur_index                 = -1       # 当前 content block 的 Anthropic 索引（首块从 0 开始）
cur_type                  = None     # "text" | "tool_use"
cur_tool_openai_index     = None     # 当前 tool 块对应的 OpenAI tool_calls[].index
final_stop_reason         = None     # 由 finish_reason 映射，末尾用
output_tokens             = 0        # 从末尾 usage chunk 取
message_id                = "msg_" + uuid   # message_start 时生成，全程复用
```

**逐 chunk 处理规则（收到什么 → 发什么）**：

```
FOR each OpenAI chunk（已 json.loads）:

  # (0) 首次：发 message_start
  if not sent_message_start:
      emit message_start(message_id, model)
      sent_message_start = True
      # 可选：紧跟发一个 ping
      emit ping()

  choice = chunk["choices"][0]  if choices 非空 else None

  # (A) 末尾只含 usage 的 chunk（include_usage 场景，choices 为空）
  if choice is None:
      if chunk.get("usage"):
          output_tokens = chunk["usage"].get("completion_tokens", output_tokens)
      continue      # 不产出事件，等 [DONE] 收尾

  delta = choice.get("delta") or {}
  finish = choice.get("finish_reason")

  # (B) 文本增量
  if delta.get("content"):
      if cur_type != "text":
          # 需要切到 text 块
          if block_open:
              emit content_block_stop(cur_index)
          cur_index += 1
          cur_type = "text"
          block_open = True
          emit content_block_start_text(cur_index)
      emit content_block_delta_text(cur_index, delta["content"])

  # (C) 工具增量（见 §4 详解重组）
  if delta.get("tool_calls"):
      handle_tool_calls_delta(delta["tool_calls"])   # §4

  # (D) finish_reason 到达
  if finish is not None:
      final_stop_reason = map_finish_reason(finish)   # §2.2 同表
      # 不立即收尾：可能后面还有 usage chunk；只记状态

  # (E) 本 chunk 内若带 usage（部分实现 finish chunk 同时带 usage）
  if chunk.get("usage"):
      output_tokens = chunk["usage"].get("completion_tokens", output_tokens)

END FOR

# (F) 收到 data: [DONE] 或流结束 → 收尾
if block_open:
    emit content_block_stop(cur_index)
    block_open = False
emit message_delta(final_stop_reason or "end_turn", output_tokens)
emit message_stop()
```

### 3.5 块索引管理规则 [LiteLLM]

- 首个块 `cur_index` 从 **0** 开始（初值 -1，第一次 open 时 +1 → 0）。
- 每次**切换块类型**或**新工具**（见 §4）：先对旧块发 `content_block_stop(cur_index)`，再 `cur_index += 1`，再对新块发 `content_block_start(cur_index)`。
- 文本块与工具块共存：它们各占一个连续递增的 index。典型序列 `text(0) → tool_use(1) → tool_use(2)`。
- `content_block_stop` 用**旧** index，`content_block_start` 用**新**（已 +1）index。

### 3.6 流式写回改造（对接 proxy.py）[设计推断]

现有 `_write_streaming_response`（proxy.py:774）是逐 8192 字节透传。native 端点流式路径需要**新写一个函数**：
```
_write_translated_stream(status, headers, upstream_resp, ctx):
    发送 200 + text/event-stream 头 + chunked
    逐行读取 upstream_resp（OpenAI SSE）:
        line = readline()
        if line.startswith(b"data: "):
            payload = line[6:].strip()
            if payload == b"[DONE]":
                触发状态机收尾 (F)，写出 message_delta/message_stop
                break
            chunk = json.loads(payload)
            for ev in state_machine.feed(chunk):   # 状态机产出 0..N 个 Anthropic 事件
                写出 chunked(ev.wire_bytes())
    写 chunked 结束 0\r\n\r\n
    异常 → BrokenPipe 静默；json 解析失败 → 记 log 跳过该行
```
> OpenAI SSE 行以 `data: {json}\n\n` 分隔，可能有空行/注释行（`:`开头），需过滤。用逐行读或按 `\n\n` 切分累积缓冲。

---

## 4. 流式工具调用重组（模块D，最繁琐）[LiteLLM 逻辑改写]

### 4.1 OpenAI 流式 tool_calls 的分片形态 [网关实测/OpenAI 通用]

OpenAI 流式里 `delta.tool_calls` 是数组，每项带 `index`（该 assistant 消息内工具的序号）：
```
# 首片（含 id、name，arguments 起始片段或空）
delta.tool_calls = [{"index":0,"id":"call_abc","type":"function",
                     "function":{"name":"get_weather","arguments":""}}]
# 后续片（仅 arguments 片段，无 id/name）
delta.tool_calls = [{"index":0,"function":{"arguments":"{\"loc"}}]
delta.tool_calls = [{"index":0,"function":{"arguments":"ation\":\"SF\"}"}}]
# 并发第二个工具
delta.tool_calls = [{"index":1,"id":"call_def","type":"function",
                     "function":{"name":"get_time","arguments":""}}]
```

**规则**：
- `index` 唯一标识一个工具调用；同一 index 的多片 `arguments` 要**顺序拼接**。
- `id`、`name` **只在该 index 首次出现的片里给**，后续片没有。
- 多工具并发：不同 index 交错出现（通常不交错，但不能假设）。

### 4.2 重组为 Anthropic 事件

Anthropic 侧每个工具 = `content_block_start(tool_use)` + 连续 `input_json_delta(partial_json)` + `content_block_stop`。**关键**：Anthropic 的 `input_json_delta.partial_json` 就是**原样透传 OpenAI 的 arguments 片段字符串**，proxy **不需要**自己拼完整 JSON、不需要解析——客户端（Claude Code）负责把所有 partial_json 拼起来再 parse `[LiteLLM]`。

**状态（在 §3.4 状态机基础上扩展）**：
```
openai_index_to_anthropic_index = {}   # OpenAI tool_calls[].index → Anthropic content block index
```

**`handle_tool_calls_delta(tool_calls)`**：
```
for tc in tool_calls:
    oai_idx = tc["index"]

    # (1) 新工具：该 oai_idx 首次出现（首片必带 name）
    is_new = (oai_idx not in openai_index_to_anthropic_index)
    if is_new:
        # 收尾上一个 open 块（text 或上一个 tool）
        if block_open:
            emit content_block_stop(cur_index)
        cur_index += 1
        cur_type = "tool_use"
        block_open = True
        openai_index_to_anthropic_index[oai_idx] = cur_index
        fn = tc.get("function") or {}
        original_name = ctx["tool_name_mapping"].get(fn.get("name",""), fn.get("name",""))  # 还原
        tool_id = tc.get("id") or ("toolu_" + gen_id())
        emit content_block_start_tool(cur_index, tool_id, original_name)

    # (2) arguments 片段 → input_json_delta（原样透传字符串）
    fn = tc.get("function") or {}
    frag = fn.get("arguments")
    if frag:      # 可能为空字符串，空则不发
        a_idx = openai_index_to_anthropic_index[oai_idx]
        emit content_block_delta_input_json(a_idx, frag)
```

### 4.3 关键边界处理

| 情形 | 处理 | 来源 |
|---|---|---|
| `arguments` 跨多 chunk 断裂 | 每片单独发一个 `input_json_delta`，`partial_json` = 该片原文，不拼接、不校验 JSON 合法性 | [LiteLLM] |
| `id`/`name` 只在首片 | 用 `oai_idx not in map` 判断首片；只有首片发 `content_block_start`，后续片只发 delta | [LiteLLM] |
| 多工具并发（多 index） | 每个 `oai_idx` 映射到独立的 Anthropic index；新 index 出现时先 stop 前一个 open 块再 start 新块 | [LiteLLM] |
| 首片 arguments 非空 | 先发 `content_block_start`，紧接着发该片的 `input_json_delta` | [LiteLLM] |
| 文本块后接工具块 | 文本块 `content_block_stop` 后，index+1，再开工具块 | [LiteLLM] §3.5 |
| 工具 `id` 缺失 | 自生成 `toolu_<uuid>` 兜底 | [设计推断] |
| `input` 无参数（arguments 全空） | 不发任何 input_json_delta；客户端把空 partial_json 序列视为 `{}` | [设计推断] |

> **注意串接顺序**：一旦切到 tool_use 块并开始发 input_json_delta，就不应再回到同一个更小 index 的块（Anthropic 要求 index 单调）。若上游在工具片之间又插入 text delta（极少见），需再 stop 工具块、开新 text 块（index 继续 +1）——本实现按 §3.4 的 (B)(C) 分支自然处理。

---

## 5. 边界与降级

### 5.1 转换失败返回合法 Anthropic error [设计推断]

任何转换阶段抛异常，都要返回**结构合法的 Anthropic 错误响应**，让 Claude Code 正常显示而非挂死。

**非流式**（HTTP 4xx/5xx body）：
```json
{"type":"error",
 "error":{"type":"api_error","message":"proxy translate failed: <原因>"}}
```
Anthropic `error.type` 合法枚举：`invalid_request_error`、`authentication_error`、`permission_error`、`not_found_error`、`request_too_large`、`rate_limit_error`、`api_error`、`overloaded_error`。请求转换失败用 `invalid_request_error`（400），上游/响应转换失败用 `api_error`（500）。

**流式**（已经开始发 SSE 后出错）：Anthropic 流式错误事件：
```
event: error
data: {"type":"error","error":{"type":"api_error","message":"..."}}
```
> 若 message_start 还没发就出错，直接返回非流式 error body（HTTP 500）。若已进流式中途出错，发 `event: error` 后关闭连接。

### 5.2 上游 HTTP 错误透传 [设计推断]

native 端点返回 4xx/5xx（OpenAI error 格式 `{error:{message,type,code}}`）时，转成 Anthropic error 结构再返回，或直接把上游 status + 一个包裹的 Anthropic error body 回写。**不要**把 OpenAI error 结构原样透传（客户端解析字段不同）。

### 5.3 已知不处理/降级项（明确列出）

| 项 | 处理 | 原因 |
|---|---|---|
| Anthropic `cache_control` | 丢弃 | native 端点是 OpenAI 格式，无缓存字段 |
| `thinking`/`redacted_thinking` block（历史消息里） | 丢弃 | reasoning 由 `reasoning_effort` 控制，网关不吃思考块回填 |
| reasoning 流式增量（`reasoning_content`/思考过程） | 首版不透传为 Anthropic thinking 块 | Anthropic thinking 流式需 `thinking_delta`+`signature_delta`，复杂；首版忽略，只透传 text/tool。**可作为二期** |
| Anthropic 托管工具（web_search/computer 等） | 跳过或原样丢弃 | native 端点不支持 |
| tool_result 含图片 | 降级为占位文本或数组序列化 | role:tool 图片支持未实测 |
| `content_filter_results` | 丢弃 + 记 log | Anthropic 无对应字段 |
| `max`/`xhigh` effort | 降级为 `high` | 网关只有 low/medium/high |
| `stop_sequence`（响应里命中哪个序列） | 固定 null | 网关未返回 |
| 多个 `choices`（n>1） | 只取 `choices[0]` | Anthropic 单响应模型 |

---

## 6. 模块化建议

### 6.1 四个独立模块 [设计推断]

| 模块 | 函数 | 职责 |
|---|---|---|
| A 请求转换 | `req_anthropic_to_openai(body, model_is_reasoning) -> (openai_body, ctx)` | §1 全部；纯 dict→dict，无 IO |
| B 非流式响应 | `resp_openai_to_anthropic(openai_resp, ctx) -> anthropic_dict` | §2 全部；纯 dict→dict，无 IO |
| C+D 流式状态机 | `class AnthropicStreamAdapter` | §3+§4；`feed(openai_chunk: dict) -> list[event_dict]`，`finalize() -> list[event_dict]` |
| 辅助 | `truncate_tool_name`, `map_reasoning_effort`, `map_finish_reason`, `anthropic_image_to_data_url`, `sse_event_bytes(type, data) -> bytes` | 无状态纯函数 |

### 6.2 各函数输入输出签名（标准库）

```python
# 模块A
def req_anthropic_to_openai(body: dict, model_is_reasoning: bool) -> tuple[dict, dict]: ...
    # in: Anthropic body dict；out: (OpenAI body dict, ctx dict)

# 模块B
def resp_openai_to_anthropic(openai_resp: dict, ctx: dict) -> dict: ...
    # in: OpenAI 非流式响应 dict；out: Anthropic 响应 dict

# 模块C+D（状态机，有状态，用 class）
class AnthropicStreamAdapter:
    def __init__(self, ctx: dict, model: str): ...
    def feed(self, openai_chunk: dict) -> list[dict]: ...   # 喂一个 chunk，返回 0..N 个 Anthropic 事件 dict
    def finalize(self) -> list[dict]: ...                    # [DONE]/流结束时调用，返回收尾事件

# 辅助
def truncate_tool_name(name: str) -> str: ...
def map_reasoning_effort(body: dict) -> str | None: ...
def map_finish_reason(finish: str | None) -> str: ...
def sse_event_bytes(data: dict) -> bytes: ...
    # data 含 "type" 键；产出 b"event: <type>\ndata: <json>\n\n"
```
> 状态机产出的是**事件 dict 列表**（不是字节），由外层写回函数用 `sse_event_bytes` 序列化 + chunked 写出。这样状态机可脱离 HTTP 单测。

### 6.3 id 生成 [设计推断]

- message id：`"msg_" + secrets.token_hex(12)` 或复用上游 `id`（若上游返回）。Anthropic 官方格式类似 `msg_01XxXx...`，只要以 `msg_` 前缀、唯一即可。
- tool_use id：优先用 OpenAI `tool_calls[].id`（`call_...`）；缺失时 `"toolu_" + token_hex(12)`。客户端只要求 tool_use.id 与后续 tool_result.tool_use_id 能对上，透传上游 id 最稳。

### 6.4 单测建议

| 模块 | 独立可测 | 测法 |
|---|---|---|
| A | ✅ 纯函数 | 喂各种 Anthropic body（string/array system、tool_use、tool_result、各 effort 形态），断言输出 OpenAI dict 字段。工具名 >64 截断+映射单独测。 |
| B | ✅ 纯函数 | 喂 OpenAI 响应（纯文本 / 带 tool_calls / 各 finish_reason / arguments 非法 JSON），断言 Anthropic dict。 |
| C+D | ✅ 状态机可脱离网络 | 准备录制好的 OpenAI chunk 序列（纯文本流、单工具流、多工具并发流、text+tool 混合流、arguments 跨 chunk 断裂、末尾 usage chunk），逐个 `feed`，收集所有事件，断言事件序列与每个事件 JSON。重点用例：<br>1. 纯文本：message_start→cbs(text,0)→cbd×N→cbstop(0)→message_delta→message_stop<br>2. 单工具：…→cbstop(text)→cbs(tool,1)→input_json_delta×N→cbstop(1)→…<br>3. 双工具并发：index 0 与 1 各自成块<br>4. arguments 断裂：partial_json 逐片透传<br>5. 缺 usage chunk：output_tokens=0 不报错 |
| 端到端 | 集成测 | mock native 端点（本地起个返回录制 SSE 的 HTTP server），走完整 proxy 路径，断言 Claude Code 侧收到合法 Anthropic SSE。 |

### 6.5 接入 proxy.py 的挂载点 [设计推断]

- 在 `_forward()`（proxy.py:439）中，判断「目标 URL 命中 native 端点路径 + 请求 path 是 `/v1/messages`」时，走新分支：
  1. 解析 `raw_body` 为 Anthropic dict。
  2. 调模块A → OpenAI body，替换 `raw_body`，并把 `target_url` 改写为 native chat/completions 路径。
  3. 拿到上游响应后：
     - 非流式 → 读全 body，调模块B，`_write_buffered_response` 写 Anthropic JSON。
     - 流式 → `_write_translated_stream`（§3.6），内部用 `AnthropicStreamAdapter`。
- 其余（failover 轮转、appkey rotate、thinking 缓存重试）逻辑复用现有；注意 thinking 格式重试（proxy.py:566）是针对 Anthropic 上游的，native 端点走 reasoning_effort，**此分支不触发 thinking 重试**。
- 判定「是否 reasoning 模型」用 proxy 配置/model 名，传给模块A 的 `model_is_reasoning`。

---

## 附录：查证来源清单

- `[LiteLLM]` transformation.py：`translate_anthropic_to_openai`、`translate_anthropic_messages_to_openai`、`translate_anthropic_tools_to_openai`、`truncate_tool_name`（SHA256 前8位、`name[:55]+"_"+hash`）、`translate_anthropic_tool_choice_to_openai`（any→required）、`translate_openai_response_to_anthropic`、`_translate_openai_finish_reason_to_anthropic`、`_translate_openai_usage_to_anthropic_usage`、`_add_system_message_to_messages`、`_translate_anthropic_image_to_openai`。
- `[LiteLLM]` streaming_iterator.py：`AnthropicStreamWrapper` 状态机（`sent_first_chunk`/`sent_content_block_start`/`sent_content_block_finish`/`current_content_block_index`/`current_content_block_type`/`holding_chunk`/`holding_stop_reason_chunk` 等 flag）、message_start 骨架与初始 usage 全 0、tool_use 块切换与 input_json_delta 原样透传、并发工具靠新 name 触发新块、`message_delta` 合并 usure、`message_stop` 收尾。
- `[Anthropic]` Messages API 流式规范：事件序列 message_start→content_block_start→content_block_delta→content_block_stop→message_delta→message_stop（可穿插 ping）、各事件 JSON keys、wire format `event:\ndata:\n\n`。
- `[网关实测]`（任务给定，直接采信）：native 非流式/流式返回结构、reasoning_effort 三档、Bearer appkey、content_filter_results。
- 现有 `tools/proxy.py`：`_forward`、`_write_streaming_response`（chunked）、`_apply_thinking_fmt`、model_map，作为接入点与代码风格参考。
