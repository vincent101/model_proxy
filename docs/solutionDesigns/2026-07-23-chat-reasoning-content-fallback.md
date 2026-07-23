---
type: design-decision
date: 2026-07-23
status: draft
target: "[[tools/model_proxy/core/translate.py]]"
tags: [architect, model_proxy, translate, reasoning]
---

# ANTHROPIC_TO_CHAT 空回答 reasoning_content 兜底 + reasoning_tokens 统计修复

路径标记：[务实]

## 背景与问题

`nation` route 的 `opus` tier 挂 chat 协议上游 `kimi-k3-sankuai-3339`，走 `ANTHROPIC_TO_CHAT`
转换。kimi-k3 强制思考、无关闭选项。当 `max_tokens` 太小（实测 16/64）时，上游把全部输出预算
耗在 `reasoning_content`（思考过程），正式回答 `message.content` 为 `""`，`finish_reason: "length"`。

`openai_to_anthropic_response`（`core/translate.py:484`）当前只读 `message.content`，非空才建 text
block，完全不识别 `reasoning_content`。结果 `content: []`（空 block 数组）。Obsidian AI 插件的
连通性测试拿到空内容判定为失败。

顺带发现两个既有统计漏洞（本次一并修）：
1. **非流式**：`openai_to_anthropic_response` 产出的 usage 只有 `input_tokens`/`output_tokens`，
   无 `output_tokens_details`。而 `server.py:1211-1212` 读的正是
   `usage.output_tokens_details.reasoning_tokens` → 永远取不到 → `usage_reasoning` 恒为 0。
   上游 chat 的 reasoning token 数其实在 `usage.completion_tokens_details.reasoning_tokens`，
   从未被映射。
2. **流式**：`OpenAIToAnthropicStreamAdapter._absorb_usage`（`translate.py:667`）只读
   `prompt_tokens`/`completion_tokens`，不读 reasoning；`usage_tuple()` 第三位硬编码返回 0
   （`translate.py:783`）。流式 reasoning token 同样丢失。

## 方案设计

### 一、触发条件的精确定义（关键判断，不照抄 max_tokens<128）

**不引入 `max_tokens < 128` 阈值。** 理由：

- 128 是复现测出来的经验边界，脆弱——不同模型、不同 prompt、不同思考长度下临界值不同；
  写死是把偶然测量值固化成逻辑。
- 更本质：问题不是"max_tokens 小"，而是"空手而归"这一**结果**。即便 `max_tokens=4096`，
  只要模型思考超长把预算耗尽，同样会 `content` 空。按结果判定才覆盖全部情形。
- 代理层不该反推请求参数去猜结果，直接看响应本身即可。

**最终判定条件（最少特例）：转换完成后 `content_blocks` 为空 且 `reasoning_content` 非空。**

- 不把 `finish_reason == "length"` 作为必要条件。若 content 空 + reasoning 非空但
  `finish_reason` 是 `stop`（模型主动结束却只给了思考），客户端拿到的一样是空，兜底同样合理。
- `finish_reason == "tool_calls"` 场景天然被排除：那时 `content_blocks` 因 tool_use block 非空，
  不满足"content_blocks 为空"，不触发兜底。**这正是把判定放在 content_blocks 组装完成之后
  （text + tool_use 都处理完）的原因**——一个条件同时正确处理了工具场景，无需为 tool_calls
  单列特例。

**不按 supply 区分"强制思考无 off"。** 配置里 `reasoning_capability` 只有 `effort_enum`，
没有可标记"强制思考"的字段（已核对 `config/model_proxy_config.json` 的两个 chat supply）。
且无需区分：判定基于响应结果，对所有 chat 协议 supply 一视同仁。不返回 `reasoning_content`
的模型天然不触发（字段缺失/空）。因此**不新增任何配置项**。

### 二、改 `openai_to_anthropic_response`（非流式，translate.py:484-546）

在函数顶部定义模块级常量（放文件常量区，`_FINISH_REASON_MAP` 附近）：

```
# reasoning_content 空回答兜底：content 空但 reasoning_content 非空时，把思考内容填进
# text block，避免客户端收到空 content 数组。可整体关闭（改此常量）。
_ENABLE_REASONING_FALLBACK = True
_REASONING_FALLBACK_PREFIX = "[模型仅返回思考过程，未生成正式回答]\n\n"
```

逻辑改动（伪代码，保持现有 text/tool_calls 处理不动，在其后插入兜底）：

```
message = choice.get("message") or {}
content_blocks = []

# ── 原有：文本 ──
text = message.get("content")
if text:
    content_blocks.append({"type": "text", "text": text})

# ── 原有：工具调用（不变） ──
for tc in message.get("tool_calls") or []:
    ... 原逻辑 ...

# ── 新增：空回答兜底（必须在 text + tool_calls 都处理完之后） ──
if _ENABLE_REASONING_FALLBACK and not content_blocks:
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        content_blocks.append({
            "type": "text",
            "text": _REASONING_FALLBACK_PREFIX + reasoning,
        })
        logger.info("empty content fallback: filled reasoning_content into text block")

# stop_reason：保持原映射，不改（见下）
stop_reason = map_finish_reason(choice.get("finish_reason"))
```

**填充规则**：
- **整段填入，不截断**。reasoning_content 是模型真实产出，代理层截断会丢信息且引入魔法长度值；
  长度处理交给客户端。
- **加前缀标注**。语义上这是思考过程不是正式回答，前缀避免客户端/用户误判为正式回答。
  连通性测试只看"有无内容"，前缀不影响其通过。
- 仅当 `reasoning` 是非空字符串（strip 后）才填；否则保持空 `content_blocks`（老行为）。

**stop_reason 保持不变**：仍走 `map_finish_reason`（`length` → `max_tokens`）。兜底填了内容
不改变"确实被截断"的事实，客户端应当据此知道是长度截断；若改成 `end_turn` 会误导为正常完成。

### 三、usage reasoning_tokens 修复（非流式）

在 `openai_to_anthropic_response` 的 usage 映射处（当前 527-531 行），把上游
`completion_tokens_details.reasoning_tokens` 映射进 Anthropic usage，使 `server.py:1211`
能读到：

```
u = resp.get("usage") or {}
anthropic_usage = {
    "input_tokens": u.get("prompt_tokens", 0),
    "output_tokens": u.get("completion_tokens", 0),
}
_ctd = u.get("completion_tokens_details") or {}
_rt = _ctd.get("reasoning_tokens")
if _rt:                       # 有值才加字段，无值不臆造、不加空 details（保持旧结构）
    anthropic_usage["output_tokens_details"] = {"reasoning_tokens": _rt}
```

注意：不复用反向路径的 `_extract_reasoning_tokens`——那个函数读的是 Anthropic 侧
`output_tokens_details.thinking_tokens` / 顶层 `thinking_tokens`，字段路径与 chat 侧
`completion_tokens_details.reasoning_tokens` 不同，直接复用会取不到。此处单独读 chat 字段。

server.py 侧无需改动：`server.py:1211-1212` 已按 `output_tokens_details.reasoning_tokens` 读，
本改动补齐了该字段，漏洞即闭合。

### 四、流式路径 `OpenAIToAnthropicStreamAdapter`（translate.py:553-783）

流式存在**相同两个缺口**：(a) 空回答兜底缺失（feed 的 B 分支只看 `delta.content`，
`reasoning_content` 被忽略）；(b) reasoning_tokens 未统计。同步修，策略如下。

**兜底策略（方案B：finalize 时补，不实时透传思考）**：

正常情况（max_tokens 足够）流式形态为：先一串 `delta.reasoning_content` 分片（思考），
后一串 `delta.content` 分片（正式回答）。若实时把 reasoning_content 当 text 发出，会污染正常
输出。故只累积、不实时发；仅在流结束且"从未产出任何 text/tool block"时补一个 text block。
此判定与非流式"content_blocks 为空才兜底"语义一致。

新增实例状态（`__init__`）：
```
self.reasoning_buf = ""              # 累积 delta.reasoning_content
self.produced_content_block = False  # 是否产出过 text/tool block（不含 reasoning 累积）
self.reasoning_tokens = 0            # completion_tokens_details.reasoning_tokens
```

`feed` 改动：
- B 分支（`delta.get("content")` 建/续 text block 处）：进入时置
  `self.produced_content_block = True`。
- C 分支（`_handle_tool_calls_delta`，新工具建 tool block 处）：同样置
  `self.produced_content_block = True`。
- 新增分支（在 B/C 之后）：
  ```
  if delta.get("reasoning_content"):
      self.reasoning_buf += delta["reasoning_content"]   # 只累积，不发事件
  ```

`_absorb_usage` 改动：增加读 reasoning_tokens
```
ctd = usage.get("completion_tokens_details") or {}
if ctd.get("reasoning_tokens"):
    self.reasoning_tokens = ctd.get("reasoning_tokens")
```

`finalize` 改动：在收尾（现有 770-775 行 `content_block_stop` / `message_delta` / `message_stop`
之前）插入兜底补块——
```
if (not self.produced_content_block
        and _ENABLE_REASONING_FALLBACK
        and self.reasoning_buf.strip()):
    self.cur_index += 1
    events.append(self._content_block_start_text(self.cur_index))
    events.append(self._content_block_delta_text(
        self.cur_index, _REASONING_FALLBACK_PREFIX + self.reasoning_buf))
    events.append(self._content_block_stop(self.cur_index))
```
注意补块要在 `block_open` 收尾逻辑之前发，且补块自身 start→delta→stop 完整闭合，
不置 `block_open`（发完即闭）。若此前有未闭合 block（正常场景已在别处收），本兜底仅在
`not produced_content_block` 时进入，此时不会有已开的 text/tool block。

`usage_tuple` 改动（783 行）：第三位返回真实值
```
return (self.input_tokens, self.output_tokens, self.reasoning_tokens)
```

`_message_delta_event`（可选一致性增强）：无需改。流式 usage 经 `usage_tuple` 直接回传给
server.py（`server.py:1191-1192`），不经 Anthropic usage dict 的 `output_tokens_details`；
只要 `usage_tuple` 返回真实 reasoning 即闭环。

### 五、是否新增可关闭开关

**不新增运行时/配置开关。** 评估：

- 兜底是"有内容总比空数组好"的普适降级，且加了前缀标注，不会让客户端误判为正式回答，
  副作用可控。
- 该场景本身是边缘（小 max_tokens + 强制思考模型），为它加配置面/文档/校验不划算。
- 已提供模块级常量 `_ENABLE_REASONING_FALLBACK`（默认 True）——需整体关闭改一行即可，
  不侵入配置系统。若未来出现"某 supply 要关"的真实需求，再按 supply 加字段，不预先设计。

## 风险与权衡

- **前缀文案**：`[模型仅返回思考过程，未生成正式回答]` 会出现在返回文本里。连通性测试无影响；
  真实对话中用户会看到该前缀——这是有意的（诚实标注），但需用户确认文案措辞是否可接受，
  是否要中英双语/可配置。**需用户确认**。
- **流式补块位置**：补块在 finalize 中生成，依赖 `produced_content_block` 标记的准确维护
  （B/C 两个分支都要置位）。实现时若漏置某分支，会导致正常有回答也误补——测试须覆盖
  "先思考后正常回答"的流式序列，断言不补块。
- **reasoning_content 字段名假设**：基于 Kimi/DeepSeek 系 chat 协议惯例（非流式
  `message.reasoning_content`，流式 `delta.reasoning_content`，usage
  `completion_tokens_details.reasoning_tokens`）。任务已用真实上游复现非流式形态。流式字段名
  按同源惯例推定；实现后建议用真实小 `max_tokens` 流式请求验证一次字段名。**实现后需实测确认流式字段名**。
- **影响面收敛**：改动只涉及 `ANTHROPIC_TO_CHAT` 的非流式函数与流式 adapter。其他 chat supply
  不返回 `reasoning_content` → 字段缺失 → 不触发兜底 → 走老逻辑（空 block / 现有 usage）。
  其他协议路径（PASSTHROUGH / ANTHROPIC_TO_RESPONSES / RESPONSES_TO_ANTHROPIC）不碰
  `reasoning_content`，完全不受影响。

## 验证方式

单测文件：`tools/model_proxy/tests/test_translate.py`（现有覆盖
`openai_to_anthropic_response` 于第 300-388 行 `TestResponseTranslate`；流式在其后
`TestStreamText` 等类）。

运行：`cd tools/model_proxy && python3 -m unittest tests.test_translate -v`
全量：`cd tools/model_proxy && python3 -m unittest discover tests -v`

新增用例（非流式，`TestResponseTranslate` 内）：
1. **触发兜底**：`content=""`、`reasoning_content="思考…"`、`finish_reason="length"`
   → `content[0]` 为 text，`text` 以 `_REASONING_FALLBACK_PREFIX` 开头且含思考原文；
   `stop_reason == "max_tokens"`（保持不变）。
2. **finish_reason 非 length 也兜底**：同上但 `finish_reason="stop"` → 仍补 text block。
3. **有正式回答不兜底**：`content="正式答案"`、`reasoning_content="思考"` →
   `content == [{"type":"text","text":"正式答案"}]`，不含前缀、不含思考。
4. **有 tool_calls 不兜底**：`content=""`、`reasoning_content="思考"`、有 tool_calls、
   `finish_reason="tool_calls"` → `content_blocks` 只含 tool_use，无 reasoning text block。
5. **reasoning 也空则保持老行为**：`content=""`、无 `reasoning_content`（或空串）→
   `content == []`（对齐现有 `test_empty_content_no_text_block`，行为不变）。
6. **reasoning_tokens 映射**：usage 带 `completion_tokens_details.reasoning_tokens=42`
   → 输出 `usage.output_tokens_details.reasoning_tokens == 42`。
7. **无 reasoning_tokens 不加字段**：usage 无 `completion_tokens_details` →
   输出 usage 不含 `output_tokens_details` 键（保持旧结构）。

新增用例（流式，`OpenAIToAnthropicStreamAdapter`）：
8. **流式空回答兜底**：喂若干 `delta.reasoning_content` 分片、无 `delta.content`、
   finish `length` → finalize 后事件序列含一个补出的 text block（start+delta+stop），
   delta 文本带前缀 + 拼接后的 reasoning。
9. **流式正常回答不补块**：先喂 `reasoning_content` 分片再喂 `content` 分片 →
   只产出 content 的 text block，finalize 不补额外 block。
10. **流式 reasoning_tokens**：末帧 usage 带 `completion_tokens_details.reasoning_tokens`
    → `adapter.usage_tuple()[2]` 等于该值（非 0）。

人工核对：`_ENABLE_REASONING_FALLBACK = False` 时用例 1/2/8 应回到空数组/不补块（可选一条
覆盖开关关闭路径）。实现后用真实小 `max_tokens` 对 kimi-k3 发一次非流式 + 一次流式请求，
确认字段名与兜底生效。

## 关联

- 转换器：`tools/model_proxy/core/translate.py`
- 调用点与 usage 记账：`tools/model_proxy/core/server.py`（非流式 1193-1216、流式 1188-1192）
- 配置：`tools/model_proxy/config/model_proxy_config.json`（chat supply `kimi-k3-sankuai-3339` / `-0956`）
- 规格：`tools/model_proxy/docs/model_proxy_translate_spec.md`（§2 非流式响应、§3/§4 流式）
- 测试：`tools/model_proxy/tests/test_translate.py`
