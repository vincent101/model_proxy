---
created: 2026-07-18 18:09:30
---
# model_proxy 双向协议转换规格（合并版）

> 本文档由原两份规格合并而来，涵盖 model_proxy 的两个方向协议转换：
> - **Part 1 正向**：Anthropic `/v1/messages` ⇄ OpenAI Chat Completions（原 `proxy_translate_spec.md`）
> - **Part 2 反向**：OpenAI Responses API ⇄ Anthropic Messages API（原 `proxy_translate_spec_reverse.md`）
>
> 两部分各自保留原有的 `§0`~`§6` 章节编号，内部自引用（如"见 §1.2"）指本 Part 内章节。
> Part 2 中对 Anthropic 侧字段定义的引用一律指向 **Part 1** 对应章节（原"正向规格"引用已改写为"Part 1 §X"）。
> 两部分共享的实现约定：纯 Python 标准库、脱网络可单测、转换失败返回结构合法的 error、流式逐事件转换不字节透传。

---

> 实现落地说明：本规格成文时挂载点设想为 tools/proxy.py，实际实现已重构为独立包 core/translate.py（转换器）+ core/reasoning/（reasoning 强度处理）。下文 §0/§3.6/§6.5 中所有 "proxy.py"、"_forward 新增分支" 等挂载点描述均指此重构前设想，字段映射规格本身不受影响、仍为当前有效参照。

# Part 1 正向：Anthropic ⇄ OpenAI Chat

> 原标题：# Proxy 协议转换器规格：Anthropic ↔ OpenAI

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

**网关事实** `[网关实测 2026-07-18]`：`reasoning_effort` 实际支持 `none`/`low`/`medium`/`high`/`xhigh` **五档**，`minimal` 不支持（返回 400）。证据来源：curl 实测网关报错消息直接列出支持列表 `none / low / medium / high / xhigh`，且 `xhigh`、`none` 均实测 200 正常。另实测 `none` 与不发字段有实质差异——不发字段 `reasoning_tokens:21`（网关默认档仍思考），发 `none` 则 `reasoning_tokens:0`（彻底关闭）。

Anthropic 侧 Claude Code 发出的形态：
- 形态1：`thinking:{type:"adaptive"}` + `output_config:{effort: "low"|"medium"|"high"|"xhigh"|"max"}`
- 形态2：`thinking:{type:"enabled", budget_tokens: N}`
- 形态3：`thinking:{type:"disabled"}`（显式关闭思考）

**触发条件**：只有 `thinking.type ∈ {enabled, adaptive, disabled}` 才产出非 None 值。裸 `output_config.effort`（无 `thinking.type`）视为未生效意图，返回 None（不塞字段）。

**映射规则**：

| Anthropic 输入 | reasoning_effort | 说明 |
|---|---|---|
| `thinking.type = "disabled"` | `none` | 显式关闭思考，塞 `none` 才能让网关 `reasoning_tokens` 清零 |
| `adaptive` + `effort ∈ {low,medium,high,xhigh}` | 同值 | 直传（`xhigh` 不再降级） |
| `adaptive` + `effort = "max"` | `xhigh` | **降级**：Anthropic 最强档 max 映射到网关最强档 xhigh |
| `adaptive` 无有效 effort | `medium` | 默认中档 |
| `thinking.type = "enabled"`, `budget < 2000` | `low` | 4档3断点 |
| `thinking.type = "enabled"`, `2000 ≤ budget < 8000` | `medium` | |
| `thinking.type = "enabled"`, `8000 ≤ budget < 32000` | `high` | |
| `thinking.type = "enabled"`, `budget ≥ 32000` | `xhigh` | |
| 裸 `output_config.effort`（无 thinking.type）/ thinking 缺失 | 不设 `reasoning_effort` | 未生效意图 |

```
def map_reasoning_effort(body) -> str | None:
    thinking = body.get("thinking") or {}
    ttype = thinking.get("type")
    oc = body.get("output_config") or {}
    effort = oc.get("effort")
    if ttype == "disabled":                 return "none"
    if ttype not in ("enabled", "adaptive"): return None
    if ttype == "adaptive":
        if effort in ("low","medium","high","xhigh"): return effort
        if effort == "max":                            return "xhigh"  # 降级
        return "medium"
    b = thinking.get("budget_tokens", 10000)   # ttype == "enabled"
    if b < 2000:   return "low"
    if b < 8000:   return "medium"
    if b < 32000:  return "high"
    return "xhigh"
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
| `max` effort | 降级为 `xhigh` | 网关支持 none/low/medium/high/xhigh 五档；`xhigh` 直传不降级，仅 Anthropic 独有的 `max` 降到 `xhigh` |
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
- `[网关实测]`（任务给定，直接采信）：native 非流式/流式返回结构、Bearer appkey、content_filter_results。reasoning_effort 五档（none/low/medium/high/xhigh，minimal 400）见 §1.4（2026-07-18 curl 实测报错消息列出支持列表）。
- 现有 `tools/proxy.py`：`_forward`、`_write_streaming_response`（chunked）、`_apply_thinking_fmt`、model_map，作为接入点与代码风格参考。

---

# Part 2 反向：OpenAI Responses ⇄ Anthropic

> 原标题：# Proxy 协议转换器规格（反向）：OpenAI Responses API ↔ Anthropic

> 用途：本地 proxy 新增能力——把 codex-cli 发出的 OpenAI **Responses API** 请求（`POST /v1/responses`）转换成 Anthropic `/v1/messages` 格式，发给美团网关 Anthropic 端点（`aigc.sankuai.com/v1/anthropic/v1/messages`，Bearer appkey 鉴权），再把 Claude 的 Anthropic 响应（含流式 SSE、工具调用）转换回 Responses 格式返回给 codex。
>
> 本文档为**编码地基**，字段级精确，可直接照写代码。不含实现代码，逻辑用伪代码/状态转移描述。
>
> **与正向规格的关系**：Anthropic 侧字段定义（system / messages / content block / tools / thinking / stop_reason / usage / 流式事件序列）**不重抄**，一律引用 `本文档 Part 1（正向规格）`（下称"正向规格"）对应章节。本文档只精写 Responses 侧结构与两侧映射。
>
> **来源标注约定**：
> - `[实测样本]`：从 `../samples/responses_api_samples.txt`（美团网关 `/v1/responses` 端点 2026-07-17 实测 4 个样本）逐字段查证
> - `[Part 1]`：Anthropic 侧字段定义引用本文档 Part 1（正向规格）
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

Responses 请求字段以 `[Responses]` 语义为准（codex 发出）；Anthropic 目标字段定义见 [Part 1 §1]。

| Responses 字段 | Anthropic 字段 | 规则 | 来源 |
|---|---|---|---|
| `model` | `model` | 原样透传（proxy 上游已做 model_map） | [设计推断] |
| `instructions` | 顶层 `system`（字符串） | Responses 的系统指令是**纯字符串**；直接放 Anthropic `system` 字符串。见 §1.2 | [Responses]+[Part 1 §1.2] |
| `input` | `messages` | 核心还原逻辑，见 §1.3 | [Responses]+[Anthropic] |
| `max_completion_tokens` | `max_tokens` | 改名；Anthropic **必填**，缺省给默认 `4096` | [Anthropic]+[设计推断] |
| `max_output_tokens` | `max_tokens` | 同上（Responses 亦可能用此别名，二者取其一，都缺则 4096） | [设计推断] |
| `reasoning.effort` | `thinking` + `output_config.effort` | 见 §1.4 | [Responses]+[Part 1 §1.4] |
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
> Anthropic `system` 既可是字符串也可是 text block 数组（见 [Part 1 §1.2]）；此处用**纯字符串**最简单稳妥。若 `instructions` 缺失则不设 `system`。

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
| `{"type":"input_image", "image_url":...}` | `{"type":"image","source":{...}}` | 图片，见下 | [Responses]+[Part 1 §1.3] |
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
> 图片：Responses 的 `image_url` 若是 `data:<media>;base64,<data>` 则拆成 Anthropic `source:{type:"base64",media_type,data}`；若是 http(s) url 则 `source:{type:"url",url}`。Anthropic source 结构见 [Part 1 §1.3]（正向是反方向拼 data url，这里逆向拆解）`[设计推断]`。

### 1.4 reasoning.effort → thinking + output_config.effort [Responses]+[Part 1 §1.4]

Responses 请求携带 `reasoning:{effort: "low"|"medium"|"high"}`（[实测样本] 响应回显 `reasoning.effort` 为 `low`/`null`，请求侧 codex 传三档之一）。

Anthropic 侧接受形态见 [Part 1 §1.4]：`thinking:{type:"adaptive"}` + `output_config:{effort}`。

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
Anthropic tool：`{name, description, input_schema}`（见 [Part 1 §1.5]）。

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
Anthropic `tool_choice`：`{type:"auto"|"any"|"tool"|"none", name?}`（见 [Part 1 §1.6]）。

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

**输入** Anthropic 非流式响应（结构见 [Part 1 §2.5]）：
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

**输入**：Anthropic 流式事件序列（`event: <type>\ndata: {json}\n\n`），事件类型与 JSON 结构见 [Part 1 §3.1 / §3.2]：`message_start → content_block_start → content_block_delta → content_block_stop → message_delta → message_stop`（可穿插 `ping`）。

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
> Anthropic SSE 是 `event: <type>\ndata: {json}\n\n` 双行；需先解析出 event_type（也可只读 data JSON 里的 `type`，二者一致，见 [Part 1 §3.3]）。Responses 输出是**单 `data:` 行**（§3.3）`[设计推断]`。

---

## 4. 流式工具调用转换（模块D'）[实测样本 样本4]+[Anthropic]

### 4.1 Anthropic tool_use 流式形态 [Part 1 §3/§4]

Anthropic 工具块的事件序列（见 [Part 1 §3.2]）：
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

网关 Anthropic 端点返回 4xx/5xx（Anthropic error 格式 `{type:"error",error:{type,message}}`，见 [Part 1 §5.1]）时，**转成 Responses error 结构**再返回，不要原样透传（codex 解析字段不同）。映射：Anthropic `error.type` → Responses `error.type`（`invalid_request_error`/`authentication_error`→ 保留同名；`api_error`/`overloaded_error` → `server_error`）。

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
- `[Part 1]`：`tools/model_proxy/本文档 Part 1`——Anthropic 侧字段定义（§1.2 system、§1.3 messages/content block、§1.4 thinking/effort、§1.5 tools/input_schema、§1.6 tool_choice、§2.5 非流式响应结构、§3.1/§3.2 流式事件序列与各事件 JSON、§3.3 wire format、§5.1 Anthropic error 枚举）。
- `[Responses]`：OpenAI Responses API 语义（codex-cli 依赖）——`instructions`/`input`(items: message/function_call/function_call_output)/`tools`(扁平 function)/`reasoning.effort`/`tool_choice`/`max_completion_tokens`。
- `[Anthropic]`：Anthropic Messages API 语义——messages 分组、tool_use/tool_result block 对齐、max_tokens 必填。
- `[设计推断]`：本文档工程决策——分组算法、reasoning 丢弃策略、四组合分发、id 生成、错误按 source 协议返回、无状态降级项。
