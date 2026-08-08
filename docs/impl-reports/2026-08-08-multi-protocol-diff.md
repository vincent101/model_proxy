# 多协议差异实测报告（anthropic vs responses）

- 日期：2026-08-08
- 执行：implementer-opus-xhigh
- 背景：① 层（effort 降级 + thinking 丢失）已修复，本次测**修复后的真实多协议差异**。预期两协议一致；如不一致即为新问题。
- 方法：经 model_proxy（127.0.0.1:18889）真实转发。客户端统一发 anthropic 协议到 `/v1/messages`（`model=claude-sonnet`，`thinking=adaptive` + `output_config.effort=max`，`max_tokens=8000`），上游按 route 分别命中 anthropic 或 responses supply。每个模型两协议用同一探针「用一句话自我介绍，并说明当前推理强度档位」。开 `MODEL_PROXY_REASONING_DEBUG=1` 抓 wire。每组合跑 3 轮（th_chars / reasoning_tokens 取 3 轮以观察方差）。

## 测试组合与路由

| route / token | 协议 | supply | target_model | supply effort_enum |
|---|---|---|---|---|
| glm-52-anthropic | anthropic | glm-52-sankuai-3339 | glm-5.2 | high,max |
| glm-52-responses | responses | glm-52-sankuai-openai-3339 | glm-5.2 | high,max |
| ds-flash-anthropic | anthropic | ds-flash-sankuai-3339 | deepseek-v4-flash | low,high,max |
| ds-flash-responses | responses | ds-flash-sankuai-openai-3339 | deepseek-v4-flash | low,high,max |
| ds-pro-anthropic | anthropic | ds-pro-sankuai-3339 | deepseek-v4-pro | high,max |
| ds-pro-responses | responses | ds-pro-sankuai-openai-3339 | deepseek-v4-pro | high,max |
| kimi-k3-anthropic | anthropic | kimi-k3-sankuai-3339 | kimi-k3 | low,high,max |
| kimi-k3-responses | responses | kimi-k3-sankuai-openai-3339 | kimi-k3 | low,high,max |

source capability 全部 `[low,medium,high,xhigh,max]`；strategy 用 route_pool 单值写法。8 组合全部 200、attempts=1、无 budget_retry。

## 主矩阵（wire / th_chars / 自述 / reasoning_tokens）

| 模型 | 协议 | wire 档名 | th_chars (r1/r2/r3) | reasoning_tokens (r1/r2/r3) | 自述档位(r1) |
|---|---|---|---|---|---|
| glm-5.2 | anthropic | **max** | 309 / 1194 / 1887 | 无字段 | 标准档位 |
| glm-5.2 | responses | **max** | 1381 / 339 / 1240 | 756 / 166 / 689 | 中等档位 |
| deepseek-v4-flash | anthropic | **max** | 30 / 313 / 419 | 无字段 | 中等（默认） |
| deepseek-v4-flash | responses | **max** | 1382 / 31 / 1077 | 762 / 17 / 556 | 标准均衡档 |
| deepseek-v4-pro | anthropic | **max** | 1641 / 661 / 2683 | 无字段 | 深度思考（Max） |
| deepseek-v4-pro | responses | **max** | 1065 / 2529 / 2532 | 557 / 1263 / 1191 | 标准模式 |
| kimi-k3 | anthropic | **max** | 261 / 462 / 879 | thinking_tokens：64 / 132 / 223 | max |
| kimi-k3 | responses | **max** | 380 / 294 / 1375 | 172 / 119 / 343 | max |

wire 结构（协议固有能力，非差异）：anthropic 直通 → `{'thinking':{'type':'adaptive'},'output_config':{'effort':'max'}}`（variant=anthropic_adaptive）；responses 翻译 → `{'reasoning':{'effort':'max'}}`（variant=resp_effort）。两者 effort 档名均为 max。

## 核心问题逐项判定

1. **wire 档名一致？** 一致。8 组合 debug 行全部 `intent=MAX(6) -> target=MAX(6) [unchanged]`，wire effort 全 `max`，**无降级**。① 层 effort 降级问题已修复，两协议表现一致。
2. **th_chars 都 >0？** 是。8 组合 × 3 轮全部 >0，**thinking 在两协议下均可见**。① 层 thinking 丢失问题已修复。
3. **有没有差异？为什么？** 见下「唯一稳定差异」与「每模型分析」。

## 唯一稳定差异：usage 上报字段（非推理能力差异）

3 轮数据中唯一**稳定**的协议差异是 usage 里思考 token 的上报方式：

- **anthropic 直通**：glm-5.2 / ds-flash / ds-pro 的 usage **不含** reasoning/thinking token 字段（思考 token 折入 `output_tokens`，不单独上报）；kimi-k3 用 anthropic 风格字段名 `output_tokens_details.thinking_tokens`。
- **responses 翻译**：4 模型统一映射出 `output_tokens_details.reasoning_tokens`（`core/translate.py:_extract_reasoning_tokens`）。

这是**观测口径/字段命名**差异：responses 协议把思考 token 单独上报，anthropic 协议多数模型不上报（或换字段名）。**不代表推理强弱不同**。对调用方的影响：走 responses 时能读到 `reasoning_tokens`，走 anthropic 时多数模型读不到该项。

## 每模型差异分析

- **glm-5.2**：wire 全 max 一致；thinking 两协议均可见。th_chars 三轮 anthropic 309/1194/1887、responses 1381/339/1240，区间高度重叠，无稳定协议差。差异仅在 usage 字段（responses 出 reasoning_tokens=756/166/689，anthropic 无）。自述档位（标准/中等）与 wire=max 不符，自述不可靠。
- **deepseek-v4-flash**：wire 全 max 一致。th_chars anthropic 30/313/419、responses 1382/31/1077——r1 表观差异大（30 vs 1382）但 r2 反转（313 vs 31），纯运行方差。差异仅在 usage 字段。自述「中等/标准均衡」与 wire=max 不符。
- **deepseek-v4-pro**：wire 全 max 一致。th_chars anthropic 1641/661/2683、responses 1065/2529/2532，重叠。差异仅在 usage 字段。anthropic r1 自述「深度思考(Max)」偶然命中，responses 自述「标准模式」不命中——自述随输出波动，不可作判据。
- **kimi-k3**：wire 全 max 一致，自述两协议均「max」（唯一自述与 wire 一致的模型）。th_chars anthropic 261/462/879、responses 380/294/1375，重叠。usage 差异：anthropic 用 `thinking_tokens`（64/132/223），responses 用 `reasoning_tokens`（172/119/343），字段名不同但都有上报。

## 横向对比：哪个模型多协议差异最大？

**没有哪个模型存在稳定的多协议差异；4 模型修复后多协议表现一致。**

- 在 ① 层关注的核心指标（wire 档名、thinking 可见性）上，4 模型两协议**全无差异**。
- 唯一稳定差异（usage 思考 token 上报字段）对 4 模型**同向且程度相当**（anthropic 缺/异名，responses 统一 reasoning_tokens），不存在某模型特别突出的情况。
- 若按单次 th_chars 表观差异排名会被运行方差误导：r1 看 ds-flash 差异最大（30 vs 1382），但 r2 ds-flash 即反转（anthropic 313 vs responses 31）。th_chars 数值大小在协议间**无稳定差异**，被运行方差主导，不能作为协议差异判据。

结论：本次实测符合「修复后两协议一致」的预期，未发现新的多协议问题。唯一可记的非预期点是 usage 字段口径差异（anthropic 多数模型不报 reasoning_tokens），属既有协议翻译行为，非回归。

## config 还原校验

- 改前 md5（备份 `/tmp/model_proxy_config.pre_eval.json`）：`0e9fb3c61f08f29f7adc62b666cb6307`
- 还原后 md5：`0e9fb3c61f08f29f7adc62b666cb6307` —— **一致**
- 还原后条目：supplies 25 / routes 4 / strategies 2（4 个 responses supply、8 route、8 strategy 已全部移除）
- reload：已触发成功；随后关闭 `MODEL_PROXY_REASONING_DEBUG` 重启（新进程无该 env），代理恢复正常运行。
