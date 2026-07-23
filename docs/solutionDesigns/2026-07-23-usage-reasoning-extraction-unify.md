---
created: 2026-07-23 21:23:29
type: design-decision
date: 2026-07-23
status: implemented
target: "[[tools/model_proxy/core/translate.py]]"
tags: [architect, model_proxy, translate, token-usage, reasoning]
---

# model_proxy 全链路 reasoning token 统计修复与提取收敛

路径标记：[理想] — 追求全链路正确性完备 + 提取逻辑收敛到单一权威 helper，不计迁移成本。

## 背景与问题

其他 session 报告：`ANTHROPIC_TO_CHAT` 非流式链路下 `usage_reasoning` 恒为 0。经实测复现与
全链路排查，确认这是**一类系统性问题**（同一约定散落多处、部分实现基于错误假设），共 **4 处真 bug**，
且既有 `[务实]` 草案 [[2026-07-23-chat-reasoning-content-fallback]] 只覆盖其中 2 处（chat 链路），
并做了**与本方案相反的收敛决策**（决定各处单写、不收敛）。本方案给出全链路修复 + 统一收敛的理想终态。

### 实测确认（真实上游，非推测）

对 `kimi-k3-sankuai-3339`（`https://aigc.sankuai.com/v1/openai/native/chat/completions`，
chat 协议，reasoning_effort=high）实发请求，确认 chat 上游 reasoning token 字段路径：

- **非流式**：`usage.completion_tokens_details.reasoning_tokens`（实测值 47）。
- **流式**（末帧 usage chunk）：`usage.completion_tokens_details.reasoning_tokens`（实测值 35）。
- **陷阱**：同一 usage 里并存 `output_tokens: 0`、`output_tokens_details: null`（chat 侧这两个字段
  是无效占位，**不是** Anthropic 的 `output_tokens_details`）。提取逻辑读 `output_tokens_details`
  时必须 `or {}` 防 `null`。

Responses 上游 reasoning 路径（`samples/responses_api_samples.txt` 已证）：
`usage.output_tokens_details.reasoning_tokens`。Anthropic 上游：`output_tokens_details.thinking_tokens`
/ `output_tokens_details.reasoning_tokens` / 顶层 `thinking_tokens`。

### 全链路排查结论（10 条链路，逐条）

`_TRANSLATOR_TABLE` 5 组合 × 流式/非流式 = 10 条。判据：usage_in/out 是否统计；usage_reasoning
是否统计；若为 0，是"上游本不提供（合理 0）"还是"响应体已有却没提取（真 bug）"。

| # | 链路（source→target，上游协议） | in/out | reasoning | 结论 |
|---|---|---|---|---|
| 1 | PASSTHROUGH anthropic 非流式（上游 anthropic） | ✅ | ✅ 读 `output_tokens_details.reasoning_tokens`（server.py:1176-1178，手写路径） | 正确，但手写未走 helper（一致性瑕疵） |
| 2 | PASSTHROUGH anthropic 流式 | ✅ | ✅ `_sniff_passthrough_usage` 用 `pt._extract_reasoning_tokens`（server.py:1632） | 正确 |
| 3 | PASSTHROUGH responses 非流式（上游 responses） | ✅ | ✅ 同 #1 同段代码，路径 `output_tokens_details.reasoning_tokens` 恰对 responses | 正确（但同瑕疵） |
| 4 | PASSTHROUGH responses 流式 | ✅ | ✅ `_sniff_passthrough_usage` 用 `pt._extract_reasoning_tokens`（server.py:1644） | 正确 |
| 5 | **ANTHROPIC_TO_CHAT 非流式（上游 chat）** | ✅ | ❌ **真 bug** | 见下 |
| 6 | **ANTHROPIC_TO_CHAT 流式** | ✅ | ❌ **真 bug** | 见下 |
| 7 | **ANTHROPIC_TO_RESPONSES 非流式（上游 responses）** | ✅ | ❌ **真 bug** | 见下 |
| 8 | **ANTHROPIC_TO_RESPONSES 流式** | ✅ | ❌ **真 bug** | 见下 |
| 9 | RESPONSES_TO_ANTHROPIC 非流式（上游 anthropic） | ✅ | ✅ `_anthropic_usage_to_responses` 用 `_extract_reasoning_tokens`（translate.py:1034） | 正确 |
| 10 | RESPONSES_TO_ANTHROPIC 流式 | ✅ | ✅ adapter `message_delta` 用 `_extract_reasoning_tokens`（translate.py:1289） | 正确 |

**共性规律**：4 处 bug 全部落在"上游 chat/responses → 回程转成 anthropic 给客户端"这一方向。
这个方向的 anthropic-usage 构造函数/adapter 是早期先写的（§1 正向 + §3.1），构造 usage 时只映射
in/out；反方向（anthropic 上游 → responses 客户端）后写、当时已用了 `_extract_reasoning_tokens`，
所以对。access-log 文档 §6.2（165-167 行）当时明确写下"ANTHROPIC_TO_CHAT usage_reasoning 记 0，
因 openai_to_anthropic_response 无 reasoning 明细产出"——**这是基于"chat 无 reasoning 明细"错误
假设的将错就错**，实测证明该假设不成立。

四处 bug 细节：

- **#5** `openai_to_anthropic_response`（translate.py:522-546）：`anthropic_usage` 只有
  `input_tokens`/`output_tokens`，无 `output_tokens_details`。server.py:1211-1212 按
  `output_tokens_details.reasoning_tokens` 读 → 恒 0。上游有 `completion_tokens_details.reasoning_tokens`。
- **#6** `OpenAIToAnthropicStreamAdapter`：`_absorb_usage`（667-673）只累加 prompt/completion；
  `usage_tuple`（783）第三位硬编码 `0`。上游末帧 usage chunk 有 `completion_tokens_details.reasoning_tokens`。
- **#7** `responses_to_anthropic_response`（translate.py:1716-1723）：`anthropic_usage` 只有
  in/out（+cache_read），无 `output_tokens_details`。server.py:1244-1245 按
  `output_tokens_details.reasoning_tokens` 读 → 恒 0。上游 responses 有 `output_tokens_details.reasoning_tokens`。
- **#8** `ResponsesToAnthropicStreamAdapter`：`response.completed` 分支（1907-1913）只取
  in/out，无 `usage_reasoning` 属性；`usage_tuple`（1951）第三位硬编码 `0`。上游
  `response.completed.usage` 有 `output_tokens_details.reasoning_tokens`。

### 为什么现有 `_extract_reasoning_tokens` 没被 #5/#6 复用

不是遗漏——是该 helper 的**设计目的原本只覆盖 Anthropic 侧 usage 结构**（docstring 明写"从
Anthropic usage dict"），候选路径只有 `output_tokens_details.thinking_tokens/reasoning_tokens`
和顶层 `thinking_tokens`，**不含 chat 侧的 `completion_tokens_details.reasoning_tokens`**。
`openai_to_anthropic_response` 的入参是 chat usage（`completion_tokens_details`），直接调它读不到。
既有 `[务实]` 草案据此判断"不能复用，各处单写 chat 字段"——在只修 chat 一条链路时这个判断没错，
但放到全链路看，它会让"reasoning 提取"这一约定继续散落（chat 单写一处、responses 又要单写一处、
将来新增协议再单写），正是本项目 url/protocol 重构踩过的"同一约定多处实现"的老坑。

## 方案设计

**核心决策：扩展 `_extract_reasoning_tokens` 为覆盖全协议的单一权威提取器，所有提取点统一调它。**

三协议的 reasoning 路径互不冲突（一个 usage dict 只会命中其中一类），合并进一个函数不会误读。
扩展后它同时覆盖：chat（`completion_tokens_details.reasoning_tokens`）、responses/anthropic
（`output_tokens_details.reasoning_tokens/thinking_tokens`）、anthropic 别名（顶层 `thinking_tokens`）。

### 一、扩展 helper（translate.py:1002-1018）

改函数体（保持函数名与返回类型不变，改 docstring + 多加一条 chat 路径）：

```
def _extract_reasoning_tokens(usage: dict) -> int:
    """从任意上游协议 usage dict 防御性多路径读取 reasoning/thinking token 数。

    覆盖 chat / responses / anthropic 三协议已知路径（互不冲突，一个 usage 只命中一类）：
        usage.output_tokens_details.thinking_tokens        # anthropic
        usage.output_tokens_details.reasoning_tokens       # responses / anthropic
        usage.completion_tokens_details.reasoning_tokens   # chat（OpenAI 风格）
        usage.thinking_tokens                              # anthropic 顶层别名
    全部缺失/为 0 则返回 0（不臆造）。各 details 用 `or {}` 防 null（chat 上游实测
    output_tokens_details 为 null）。
    """
    u = usage or {}
    otd = u.get("output_tokens_details") or {}
    ctd = u.get("completion_tokens_details") or {}
    return (
        otd.get("thinking_tokens")
        or otd.get("reasoning_tokens")
        or ctd.get("reasoning_tokens")
        or u.get("thinking_tokens")
        or 0
    )
```

**兼容性**：现有测试 `test_B_reasoning_tokens_multi_path`（test_translate.py:885）断言的两条
anthropic 路径顺序不变、结果不变；新增的 chat 路径优先级排在 anthropic 之后，不影响已有断言。

### 二、修 #5 `openai_to_anthropic_response`（translate.py:527-531）

usage 映射处改为通过 helper 回填 `output_tokens_details.reasoning_tokens`：

```
u = resp.get("usage") or {}
anthropic_usage = {
    "input_tokens": u.get("prompt_tokens", 0),
    "output_tokens": u.get("completion_tokens", 0),
}
_rt = _extract_reasoning_tokens(u)      # 统一 helper，读 completion_tokens_details.reasoning_tokens
if _rt:                                 # 有值才加 details，无值保持旧结构（不加空字段）
    anthropic_usage["output_tokens_details"] = {"reasoning_tokens": _rt}
```

server.py:1208-1212 无需改（已按 `output_tokens_details.reasoning_tokens` 读，本改动补齐该字段）。

### 三、修 #6 `OpenAIToAnthropicStreamAdapter`

- `__init__`（约 580）新增 `self.reasoning_tokens = 0`。
- `_absorb_usage`（667-673）末尾加：
  ```
  _rt = _extract_reasoning_tokens(usage)
  if _rt:
      self.reasoning_tokens = _rt
  ```
- `usage_tuple`（783）第三位返回真实值：`return (self.input_tokens, self.output_tokens, self.reasoning_tokens)`。

### 四、修 #7 `responses_to_anthropic_response`（translate.py:1716-1723）

usage 映射处补 `output_tokens_details`：

```
u = resp.get("usage") or {}
anthropic_usage = {
    "input_tokens": u.get("input_tokens", 0) or 0,
    "output_tokens": u.get("output_tokens", 0) or 0,
}
cached = (u.get("input_tokens_details") or {}).get("cached_tokens")
if cached:
    anthropic_usage["cache_read_input_tokens"] = cached
_rt = _extract_reasoning_tokens(u)      # 读 output_tokens_details.reasoning_tokens
if _rt:
    anthropic_usage["output_tokens_details"] = {"reasoning_tokens": _rt}
```

server.py:1241-1245 无需改。

### 五、修 #8 `ResponsesToAnthropicStreamAdapter`

- `__init__`（1755-1769）新增 `self.usage_reasoning = 0`。
- `feed` 的 `response.completed` 分支（1907-1913）加：
  ```
  self.usage_reasoning = _extract_reasoning_tokens(u) or self.usage_reasoning
  ```
- `usage_tuple`（1951）第三位返回真实值：`return (self.input_tokens, self.output_tokens, self.usage_reasoning)`。

### 六、收敛 server.py 手写路径（一致性，非 bug）

PASSTHROUGH 非流式（server.py:1176-1178）当前手写 `output_tokens_details.reasoning_tokens`。
理想终态下改为统一走 helper：

```
self._acc["usage_reasoning"] = pt._extract_reasoning_tokens(_pu)
```

收敛后：**全项目所有 reasoning 提取点无一例外调用 `pt._extract_reasoning_tokens`**，
新增协议/新增字段路径时只改这一个函数，杜绝再漏改。

### 与既有 `[务实]` 草案的关系

[[2026-07-23-chat-reasoning-content-fallback]] 覆盖 #5/#6 的 reasoning 修复 + 空回答
reasoning_content 兜底。本方案与它的差异：
1. **补齐它漏掉的 #7/#8**（ANTHROPIC_TO_RESPONSES 两条链路）——它的"影响面收敛"一节明确
   声明不碰该链路，但该链路同样漏统计。
2. **收敛决策相反**：它决定 chat 字段单写、不复用 helper；本方案扩展 helper 使全链路统一。
3. **reasoning_content 空回答兜底**（它的第二/四节）与本方案正交，不冲突，可独立保留——
   本方案只动 usage 提取，不动 content block 组装。

落地时若两方案都执行：本方案的 helper 扩展 + #5/#6 usage 修复应**取代**务实草案第三/四节里
"单写 chat 字段"的 usage 部分（改为统一 helper），务实草案的 content 兜底部分照旧保留。

## 风险与权衡

- **helper 合并的正确性依赖"路径互不冲突"假设**：若某上游同时返回 chat 和 responses 两套 details
  且值不同，`or` 短路会取第一个非 0 的（anthropic > responses > chat 优先级）。实测三协议各自
  只出一套，假设成立；但这是隐式约定，需在 docstring 写清优先级（已写）。若将来出现同 usage 混
  两套的上游，需重审。
- **`output_tokens_details: null` 陷阱**：chat 上游实测该字段为 `null`。helper 与所有调用点凡读
  `output_tokens_details`/`completion_tokens_details` 必须 `or {}`，否则 `None.get` 抛异常。
  已在方案中全部覆盖。
- **既有 status 冲突**：务实草案 status=draft、未 implemented。若先执行务实草案再执行本方案，会有
  一次"单写→改回 helper"的返工。建议：**二者合并为一次实施**，usage 部分直接按本方案（helper），
  content 兜底按务实草案，避免返工。需用户裁决实施顺序。
- **PASSTHROUGH 非流式的 helper 收敛（第六节）是纯一致性改动**，无正确性收益（现有手写路径对
  anthropic/responses 都正确）。若想最小化 diff 可只做 #5-#8 四处 bug 修复 + helper 扩展，第六节
  可选。理想路径推荐做（彻底收敛）。

### 迁移/落地代价提示

改动集中在 `core/translate.py`（1 个 helper + 2 个非流式函数 + 2 个 adapter）与
`core/server.py`（1 处可选收敛）。无数据结构变更、无配置变更、无接口签名变更（`usage_tuple`
返回类型不变，只是第三位从常量 0 变真实值）。代价主要是回归测试补齐（见下）。

## 验证方式

单测：`cd tools/model_proxy && python3 -m unittest tests.test_translate tests.test_passthrough_sniff -v`
全量：`cd tools/model_proxy && python3 -m unittest discover tests -v`

**必须用非零 reasoning 场景反推**（避免"测了但没覆盖到字段"的假阳性，用实测值 47/35 或任意非 0 值）：

helper（`_extract_reasoning_tokens`）：
1. chat 路径：`{"completion_tokens_details": {"reasoning_tokens": 47}}` → 47。
2. responses 路径：`{"output_tokens_details": {"reasoning_tokens": 9}}` → 9。
3. anthropic 路径（回归）：`{"output_tokens_details": {"thinking_tokens": 7}}` → 7（不变）。
4. null 防御：`{"output_tokens_details": null, "completion_tokens_details": {"reasoning_tokens": 5}}` → 5，不抛异常。
5. 全缺失 → 0。

#5 非流式 chat→anthropic：
6. usage 带 `completion_tokens_details.reasoning_tokens=47` →
   `out["usage"]["output_tokens_details"]["reasoning_tokens"] == 47`。
7. 无该字段 → out usage 不含 `output_tokens_details` 键（保持旧结构，兼容现有断言）。

#6 流式 chat→anthropic：
8. 末帧 usage chunk 带 `completion_tokens_details.reasoning_tokens=35` →
   `adapter.usage_tuple()[2] == 35`；且 `test_usage_tuple`（现断言 0）需改为构造带 reasoning 的用例
   或另立用例（不要留一个恒 0 的断言给人"已测"的假象）。

#7 非流式 responses→anthropic：
9. usage 带 `output_tokens_details.reasoning_tokens=12` →
   `responses_to_anthropic_response` 输出 usage 含 `output_tokens_details.reasoning_tokens == 12`。

#8 流式 responses→anthropic：
10. `response.completed.usage.output_tokens_details.reasoning_tokens=8` →
    `adapter.usage_tuple()[2] == 8`。

人工核对（可选，端到端）：本地代理起 kimi-k3 路由，走 ANTHROPIC_TO_CHAT 发一次 reasoning_effort=high
的非流式 + 一次流式请求，`grep ACCESS .claude_model_proxy.log` 确认 `usage_reasoning>0`；
`model_proxy_cli.sh stats` 确认账本 combo 的 `usage_reasoning` 累加非 0。

## 关联

- 转换器：[[tools/model_proxy/core/translate.py]]（`_extract_reasoning_tokens` / `openai_to_anthropic_response` / `responses_to_anthropic_response` / 两个流式 adapter）
- 记账点：[[tools/model_proxy/core/server.py]]（非流式 1201-1245、流式 1189-1224、PASSTHROUGH 1169-1178）
- 前序 access 日志设计（含 §6.2 曾将 chat usage_reasoning 记 0 的错误假设）：[[2026-07-22-access-log-and-latency]]
- 累计账本设计：[[2026-07-23-usage-totals-ledger]]
- 交叠的务实草案（chat 空回答兜底 + chat usage 单写）：[[2026-07-23-chat-reasoning-content-fallback]]
- 规格：`tools/model_proxy/docs/model_proxy_translate_spec.md`
- 测试：`tools/model_proxy/tests/test_translate.py`、`tests/test_passthrough_sniff.py`
