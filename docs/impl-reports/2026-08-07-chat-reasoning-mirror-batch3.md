---
type: impl-report
batch: 3
target: "[[tools/model_proxy]]"
design: "[[2026-08-07-reasoning-thinking-truncation-and-protocol-consistency]]"
updated: 2026-08-07
---

# 第三批交付：chat→anthropic 的 reasoning_content→thinking 镜像（①b-chat 扩展）

## 改动文件清单

### `core/translate.py`（仅 chat→anthropic 反向，不动 responses→anthropic / codecs / server / config）

1. **非流式 `openai_to_anthropic_response`**：`message.reasoning_content` 非空且已有正文/工具块时，`content_blocks.insert(0, {"type":"thinking","thinking":...})`，置前于 text/tool_use；空 reasoning 不产 block；signature 无来源不产出（注释声明，与 ①b 一致）。
2. **流式 `OpenAIToAnthropicStreamAdapter`**：
   - 新增 `_content_block_start_thinking` / `_content_block_delta_thinking` 两个事件 helper；
   - 新增 `_flush_thinking_block(events)`：首个 content/tool 增量处把累积的 `reasoning_buf` 一次性镜像为 thinking block（开 index 在 text/tool 前 → thinking_delta → 合块），`thinking_emitted` 标记防重，flush 后清空 `reasoning_buf`；
   - 在 feed 的 (B) 文本分支与 (C) 工具分支开头接入 `_flush_thinking_block`；
   - `__init__` 新增 `self.thinking_emitted = False` 状态位。

### `tests/test_translate.py`

3. 新增 `_load_chat_sse_chunks` helper（chat SSE 无 `type` 字段，区别于 responses 的 `_load_sse_sample_events`）。
4. 新增 `TestChatReasoningMirror` 4 个单测：非流式 kimi 样本（thinking 在 text 前、无 signature）、流式 kimi SSE 样本（thinking/text index 0/1、thinking_delta 拼接==样本 reasoning_content 全文、text_delta 拼接==样本 content 全文、start/stop 各一对）、content 空走兜底边界（流式+非流式各一）。
5. 翻转 3 个"reasoning 被丢弃"既有断言为镜像断言（对齐 ①b "dropped→backfilled" 反转先例）：
   - `test_reasoning_fallback_not_triggered_with_real_content` → `test_reasoning_mirror_with_real_content`（断言 `[thinking, text]`）
   - `test_reasoning_fallback_not_triggered_with_tool_calls` → `test_reasoning_mirror_with_tool_calls`（断言 `[thinking, tool_use]`）
   - 流式 `test_reasoning_then_real_content_no_extra_block` → `test_reasoning_then_real_content_mirror_thinking`（断言 thinking 在 text 前、finalize 不兜底）

### `docs/designs/2026-08-07-reasoning-thinking-truncation-and-protocol-consistency.md`

6. 文末追加"①b-chat 扩展（第三批落地记录）"小节：SSE 样本词表（`delta.reasoning_content`）、实现要点、与兜底边界、buffer-flush 取舍理由、翻转清单、验证结果。

## 与现有兜底的关系（边界，互斥不双写）

- **content 非空**（有 text 或 tool_calls）→ 走镜像：reasoning_content → thinking block（置前），content → text/tool block；`produced_content_block=True` 使 finalize 兜底不触发 + flush 清空 buf 双保险。
- **content 空** → 走既有兜底：finalize 把 reasoning_buf 填成 text block（不产 thinking）。空回答兜底单测全部保持绿，未改。

## 关键设计取舍：流式用 buffer-flush 而非逐 delta 实时开块

chat 流无独立 reasoning 开块事件，收到 reasoning_content 时无法预知 content 是否为空；而边界要求"content 空时仍走兜底填 text（产 text、不产 thinking）"。逐 delta 实时开 thinking 块会让空回答场景产出 thinking、与兜底断言冲突或双写。buffer-flush 在 content 首次到达时才镜像，兼顾"thinking index 在 text 前"与"空回答仍走兜底"。已知限制：正文块产出后再到的 reasoning_content 分片（非标准交错）不镜像，留在 buf 不双写。

## 验证结果

- **回归**：`python3 -m unittest discover -s tests -q` → **482 全绿**（478 + 新增 4）。
- **样本验证（th_chars 0→>0）**：
  - 非流式 `kimi_chat_reasoning_high_nonstream.json` → block 序 `[thinking, text]`，th_chars **96**。
  - 流式 `kimi_chat_reasoning_high.sse` → block starts `[(0,thinking),(1,text)]`，th_chars **92**。

## 风险自评

低。改动收敛在 chat→anthropic 反向 + 测试，未触 responses→anthropic / codecs / server / config；空回答兜底路径行为不变（相关单测全绿）；翻转的 3 个断言均为本次缺陷修复的预期行为反转。建议复核 buffer-flush 的"正文后再到 reasoning 不镜像"这一已知限制是否符合预期。
