---
type: impl-report
status: done
target: "[[tools/model_proxy]]"
tags: [implementer, model_proxy, reasoning, thinking-backfill, batch2]
updated: 2026-08-07
---

# ①b 落地交付：responses→anthropic 的 reasoning→thinking 回传

设计依据：[[2026-08-07-reasoning-thinking-truncation-and-protocol-consistency]] ①b 节 + 两轮复核相关条款。事件词表以真实样本为准（glm 走 `response.reasoning_text.delta` 通道，非 openai 官方 summary 通道），实现双通道兼容。

## 改动文件清单

### `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/translate.py`

1. 新增 `_extract_reasoning_thinking_text(item)`（模块 B'' 前）：从 reasoning item 双通道提取 thinking 文本——glm `content[].reasoning_text` + openai 官方 `summary[].summary_text`，多 part 用 `\n\n` 连接；注释声明 signature 无来源的已知限制。
2. `responses_to_anthropic_response`：`elif it == "reasoning": pass` 改为提取 thinking 并产出 `{"type":"thinking","thinking":...}` block（空内容不产 block）。
3. `ResponsesToAnthropicStreamAdapter` 新增 `_content_block_start_thinking` / `_content_block_delta_thinking` 两个事件 helper（signature 不产出，注释声明）。
4. `ResponsesToAnthropicStreamAdapter.feed`：`output_item.added` 加 reasoning 分支开 thinking block；新增 `reasoning_text.delta`（glm）/`reasoning_summary_text.delta`（openai）→ `thinking_delta`（含防御性自动开块，obfuscation 字段忽略）；新增两通道 `.done` → `content_block_stop`。thinking 与 text/tool_use 交错由"开新块前先关旧块"统一处理，与既有 text 分支同款防御模式。
5. `cur_type` 注释更新为 `"text" | "tool_use" | "thinking"`。

### `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/tests/test_translate.py`

6. `test_ar_reasoning_item_dropped` 反转为 `test_ar_reasoning_item_backfilled_as_thinking`（断言产出 thinking block 且在 text 之前、无 signature）；新增 `test_ar_reasoning_summary_channel_multi_part`（openai 通道 + `\n\n` 拼接）、`test_ar_reasoning_empty_produces_no_block`（空 reasoning 不产空 block）。
7. 新增样本加载 helper `_load_sse_sample_events` / `_load_json_sample`。
8. TestARResponse 新增 `test_ar_reasoning_nonstream_from_glm_sample`：真实非流式样本驱动，断言 thinking 文本与样本 `content[0].reasoning_text` 完全一致。
9. TestARStream 新增三个：`test_ar_reasoning_stream_from_glm_sample`（真实 SSE 样本驱动，thinking_delta 拼接 == response.completed 权威文本，usage_tuple==(38,625,513)）、`test_ar_reasoning_summary_stream_openai_channel`（openai 官方通道合成流）、`test_ar_reasoning_delta_without_item_added_defensive`（跳过 item.added 直接发 delta 的防御路径）。

## 验证结果

- 回归：`python3 -m unittest discover -s tests -q` → **Ran 474 tests, OK**（468 → 474，净 +6：新增 7、改名 1）。
- 真实样本驱动 th_chars（0 → >0）：
  - 流式 `glm52_openai_resp_reasoning_max.sse`：th_chars=**814**，text_chars=175，usage=(38, 625, 513)，thinking/text 块 index=0/1 配对开合。
  - 非流式 `glm52_openai_resp_reasoning_high_nonstream.json`：th_chars=**606**，content blocks=[thinking, text]。
- 未跑 live eval（glm-52-sankuai-openai-3339 真实网关重测 th_chars）——属评估侧动作，建议由主会话/eval 流程触发复测确认。

## 边界自查

未动 codecs.py（第一批已零词表）、server.py、config、正向 anthropic→responses 转换；仅回传方向 + tests/。

## 风险自评

低。改动为纯增量补齐，既有 474 单测全绿；thinking block 的 signature 字段无来源为设计已声明的已知限制（对只读评估无影响，对回传 thinking 的多轮客户端是限制）。
