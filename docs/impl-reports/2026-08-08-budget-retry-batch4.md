---
type: impl-report
batch: 4
target: "[[tools/model_proxy]]"
design: "[[2026-08-07-reasoning-thinking-truncation-and-protocol-consistency]]"
updated: 2026-08-08
---

# 第四批交付：④b 自动放大重试 + ② 反向默认 + ⑤ 监控字段 + 4 条架构 refinement

## 改动文件清单

### `core/translate.py`

1. 新增 R1 纯谓词 `is_budget_truncated(target_protocol, raw_resp)`（map_finish_reason 之后）：
   三协议「达到输出预算上限且正文缺失」判定——anthropic `stop_reason=="max_tokens"` 且
   content 无 text/tool_use；chat `finish_reason=="length"` 且 content 空且无 tool_calls；
   responses `status=="incomplete"` 且 `incomplete_details.reason=="max_output_tokens"` 且
   output 无文本无 function_call。接受 bytes/str/dict，解析失败返回 False（判不出不重试）。
   只加此函数，转换/兜底逻辑零改动。

### `core/server.py`

2. 常量区（`_TRANSLATOR_TABLE` 后）：`_BUDGET_CEILING=131072`、`_BUDGET_RETRY_MAX=5`、
   `_THINKING_MAX_TOKENS_DEFAULT=16384`、`_NON_THINKING_MAX_TOKENS_DEFAULT=4096`、
   `_BUDGET_FIELD_BY_PROTOCOL`（R2：anthropic=max_tokens / chat=max_completion_tokens /
   responses=max_output_tokens）。
3. `ConfigStore.get_budget_retry()`（照 get_default_cooldown 模式）：读可选顶层
   `budget_retry` 块，缺省全开 `{enabled: True, max_retries: 5, ceiling: 131072}`。
4. `_forward_logged` 的 `_acc` 初始化与 ACCESS 格式串：新增 `budget_retried`（放大轨迹，
   形如 `16000→32000`，多次逗号相接）、`budget_truncated`（0/1）、`stop_reason`。
5. `_forward` 重试链路（`_reasoning_cache_supply_id` 声明块后）：新增 ④b 请求周期作用域
   状态 `_budget_retries`（int 计数器，R3 非布尔位）/ `_budget_current`，及两个闭包——
   `_stamp_budget`（首轮读回客户端有效预算作起点、写回原值无行为变化；重试轮覆写放大值）
   与 `_maybe_budget_retry`（原始响应上判 → ×2 封顶 → 计数+轨迹 → True 由调用方
   `resp.close()`+`continue`；到顶/无基线 → 记 `budget_truncated=1` 返回 False 如实写回）。
   R4 注释声明：状态跨 failover 继承（有意行为，预算不足是模型属性）。
6. 四转发分支 stamp 点（构建 outgoing body 后、json.dumps 前）：PASSTHROUGH（按 target
   子协议分字段，含 responses→responses 的 max_output_tokens；值变化才 re-dump）、
   ANTHROPIC_TO_CHAT、ANTHROPIC_TO_RESPONSES、RESPONSES_TO_ANTHROPIC 各一行调用。
7. ② 反向默认：RESPONSES_TO_ANTHROPIC 分支 `max_tokens_default=4096` 改为按
   `abstract.kind == AbstractKind.THINKING` 分档 16384/4096（import 增 AbstractKind）。
8. 非流式四处收口（各分支 `resp.read()` 后、转换/写回前）接入 `_maybe_budget_retry`；
   命中 → close+continue（同 supply 重选，不 cooldown、不进 tried_set、不计 failover，
   remap 缓存复用）。非流式各分支补 `stop_reason` 进 _acc（透传 anthropic 取 stop_reason、
   responses 取 status[:reason]，转换分支取转换后/原始 anthropic 值）。
9. 流式不重试只记日志：PASSTHROUGH 流式在 `_write_streaming_response` 后按
   `_acc["stop_reason"] in ("max_tokens","incomplete:max_output_tokens")` 且无
   `stream_content` 标记 → `budget_truncated=1`+warning；ANTHROPIC_TO_CHAT 流式用
   adapter 状态（`final_stop_reason=="max_tokens"` 且 `not produced_content_block`）。
   ANTHROPIC_TO_RESPONSES / RESPONSES_TO_ANTHROPIC 流式 adapter 未持有 incomplete/正文
   状态，不检测（README 已声明）。
10. `_sniff_passthrough_usage` 扩展（纯旁路，转发字节不变）：anthropic 增嗅
    message_delta.stop_reason 与 content_block_start(text/tool_use)→stream_content；
    responses 增嗅 response.incomplete（原只认 completed）status[:reason] 与
    output_item.added(message/function_call)→stream_content。

### `config/model_proxy_config.json` / `config/model_proxy_config.example.json`

11. 顶层各加可选块 `budget_retry: {"enabled": true, "max_retries": 5, "ceiling": 131072}`，
    现有内容不动。注意：live config（gitignored）重写时 JSON round-trip 把单行紧凑格式
    展开为 indent=2，键/值/顺序逐一保留、仅空白变化；example config 为一行最小 diff。

### `README.md`（model_proxy）

12. §5.2 ACCESS 字段串加三个新字段及语义（含 budget_retried 高频=「调用侧预算偏小/
    模型 thinking 量大」运营信号）；§8 已知限制加 ④b 条目（机制、R2 字段名、R4 继承、
    **仅非流式生效**、流式检测覆盖范围）；附录 A 顶层字段表加 `budget_retry` 行。

### `../model_eval/README.md`

13. SOP 第 2 步加起步预算指引：给合理起步 max_tokens（reasoning 高档 ≥16000），截断由
    代理自动放大，关注 `budget_retried`/`budget_truncated`；流式需一次给足。

### `tests/test_translate.py`

14. 新增 `TestIsBudgetTruncated` 18 个：三协议正反用例 + **fallback 不掩盖**用例
    （chat 原始响应仅 reasoning_content，转换后兜底会填 text，原始响应上判仍 True）+
    bytes/垃圾输入/未知协议。

### `tests/test_budget_retry.py`（新增）

15. 18 个 _forward 全链路集成测试（patch `core.server.urllib.request.urlopen`）：
    首轮原值/重试 ×2、封顶钳位（16000→…→131072 共 5 发）、次数上限（4096 起步 5 次共
    6 发）、到顶如实返回+budget_truncated=1、config 关闭退回透传、无预算基线只记不重试、
    有正文不重试；chat/responses/反向三转换分支字段名 + responses 透传 R2 字段名；
    ② 反向默认 16384(THINKING)/4096(普通)/从 16384 起爬；语法重试+预算重试共存
    （400→200截断→200好，独立状态互不阻塞）；R4 failover 继承放大预算；不 cooldown
    不进 tried_set（单 supply 重选成功即证）；流式三例（透传截断记标记不重试、有 text
    不记、chat 流 length 映射后记标记）。

## 验证结果

- `python3 -m unittest discover -s tests -q`：**544 全绿**（既有 508 + 新增 36），
  既有语法重试/failover/sniff/转换单测零改动通过。
- `ConfigStore('config/model_proxy_config.json').get_budget_retry()` 实读：
  `{'enabled': True, 'max_retries': 5, 'ceiling': 131072}`；无该块时缺省全开一致。

## 风险自评

改动落在出站热路径与重试状态机，但形态是「新增独立触发器 + 旁路观测」：首轮 stamp
写回原值（有单测钉死客户端给定值一字不改发出），重试复用既有 continue 幂等，与语法
重试/failover 的共存语义有专项单测。主要残余风险：真实上游的截断响应形状若与
`is_budget_truncated` 三协议判定式不符（如网关缺 incomplete_details）会判 False 退化为
不重试（安全方向，不误判）；live config 格式被 round-trip 展开（内容不变，功能无影响）。
建议复核 server.py ④b 闭包段与四分支 stamp/检测接入点。
