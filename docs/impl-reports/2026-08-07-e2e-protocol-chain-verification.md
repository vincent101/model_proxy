---
type: impl-report
status: done
target: "[[tools/model_proxy]]"
tags: [model_proxy, e2e, reasoning, effort-mapping, protocol-consistency, verification]
updated: 2026-08-07
related: "[[2026-08-07-reasoning-thinking-truncation-and-protocol-consistency]]"
---

# E2E 协议链路验证：①a(codec 零词表)+①b(responses→anthropic thinking 回传)

对 ①a/①b 修复做端到端实证：经 model_proxy(127.0.0.1:18889)真实转发到上游网关，逐链路采集
**wire 档名 / thinking 可见性(th_chars)/ 模型自述 / usage.reasoning_tokens(rt)**，对照
`/tmp/trace_combos.py` 的推演预期判定符合/不符。不凭空说符合，全部走真实请求。

## 验证方法（实际执行）

1. **临时 config**：基于用户当前 config 加 2 个 supply + 4 条 eval route + 4 条 eval strategy。
   两个 openai/chat supply 在当前 config 不存在，定义取自 `config/model_proxy_config.json.bak.20260807162028`：
   - `glm-52-sankuai-openai-3339`(responses, glm-5.2, `[high,max]`)
   - `kimi-k3-sankuai-openai-3339`(chat, kimi-k3, `[low,high,max]`)
   - 复用已有 `glm-52-sankuai-6372`(anthropic, glm-5.2, `[high,max]`)、`ds-flash-sankuai-3339`(anthropic, deepseek-v4-flash, `[low,high,max]`)
   - **4 个 eval supply 全部选用"不被 cc/codex 池引用"的 id**（实测期间用户有并发真实流量，池内 id 会污染 wire 日志归属；`glm-52-sankuai-3339` 在 nation1/2 sonnet 池，故 eval-anth 改指 6372)。每条 strategy 显式 `tiers_source_capability.sonnet=[low,medium,high,xhigh,max]`（与推演 SRC 一致）。
2. **开 reasoning debug**：`MODEL_PROXY_REASONING_DEBUG=1` 重启进程（该 env 仅 import 时读取，config reload 不生效，必须重启）。debug 旁路日志记录 `intent→target→wire` 实际档名。
3. **发请求**：每个 target 发 anthropic-adaptive / anthropic-enabled(budget=128000) / responses 三种格式 × 多档 effort,model=claude-sonnet 映射 sonnet tier。非流式。
4. **还原**：测完删临时项，从字节备份还原 config,reload 校验 md5；重启进程去掉 debug env 复原。

## 结论速览

| 验证点 | 结果 |
|---|---|
| **Defect A encode**:responses/chat target 收 max 不降 medium | **符合**,A/C/E/K-max wire 全发 `max` |
| **Defect A decode**:responses sdk 发 max 认意图(present=True) | **符合**,C/D/H-max `intent=MAX(pTrue)` |
| **Defect B**:responses→anthropic thinking 回传可见 | **符合**,A/E(anthropic 客户端→responses target)th_chars 全 >0 |
| 老 budget 链路(enabled budget=128000→MAX→wire max) | **符合**,E/F/I-max wire 全发 `max` |
| ds-flash low 不升档(对照 glm low→high 升档) | **符合**,G-low wire `low` |
| anthropic target 全档 wire+thinking(对照组) | **符合**,B/D/F/G/H/I 全 OK |
| **chat target(kimi)thinking 可见性** | **不符合预期**:wire 档名正确(max 已发),但 `th_chars=0` —— 详见"不符合项" |

## 验证矩阵（20 用例，全经真实转发)

> wire档名列只写最终发给上游的 effort 值（anthropic=`output_config.effort`、responses=`reasoning.effort`、chat=`reasoning_effort`)。rt=None/0 含义见"附带观察"。自述为模型自报档位（软信号，见"附带观察")。

### target=responses(glm-52-sankuai-openai-3339,`[high,max]`)— Defect A encode/decode + Defect B 主战场

| 链路 | sdk格式 | effort | wire档名 | intent→target | th_chars | rt | 自述 | 符合 |
|---|---|---|---|---|---|---|---|---|
| A | anth-adaptive | low | **high** | LOW→HIGH | 1452 | 905 | low | ✓ |
| A | anth-adaptive | medium | **high** | MEDIUM→HIGH | 1090 | 677 | medium | ✓ |
| A | anth-adaptive | high | **max** | HIGH→MAX | 1443 | 883 | low | ✓ |
| A | anth-adaptive | max | **max** | MAX→MAX | 1793 | 1103 | low | ✓ |
| C | responses | low | **high** | LOW→HIGH | 495 | 311 | high | ✓ |
| C | responses | max | **max** | MAX→MAX(pTrue) | 1505 | 917 | minimal | ✓ |
| E | anth-enabled budget=128000 | (max) | **max** | MAX→MAX | 2619 | 759 | low | ✓ |

关键点：A/E 是 anthropic 客户端→responses target，响应经 **①b 反向**(responses→anthropic）回传，th_chars 全 >0 → **Defect B 修复生效**;wire 全发 `max` 而非 medium → **Defect A encode 生效**。C 是 responses→responses 直透（passthrough 也走 remap,server.py:1191),`intent=MAX(pTrue)` → **Defect A decode 生效**。

### target=anthropic(glm-52-sankuai-6372,`[high,max]`)— 对照组

| 链路 | sdk格式 | effort | wire档名 | intent→target | th_chars | rt | 自述 | 符合 |
|---|---|---|---|---|---|---|---|---|
| B | anth-adaptive | low | high | LOW→HIGH | 1594 | None | minimal | ✓ |
| B | anth-adaptive | max | max | MAX→MAX | 1893 | None | low | ✓ |
| D | responses | max | max | MAX→MAX(pTrue) | 1101 | 0 | low | ✓ |
| F | anth-enabled budget=128000 | (max) | max | MAX→MAX | 2451 | None | 未知 | ✓ |

对照组本就正常，全档 wire 正确 + thinking 可见。D 是 responses 客户端→anthropic target，响应用 anthropic→responses **正向**回传（非①b),th>0 符合。

### target=chat(kimi-k3-sankuai-openai-3339,`[low,high,max]`)— Defect A encode(chat) ✓ / thinking 可见 ✗

| 链路 | sdk格式 | effort | wire档名 | intent→target | th_chars | rt | 自述 | 符合 |
|---|---|---|---|---|---|---|---|---|
| K | anth-adaptive | low | **low** | LOW→LOW | **0** | 133 | low | wire✓/thinking✗ |
| K | anth-adaptive | medium | **high** | MEDIUM→HIGH | **0** | 166 | high | wire✓/thinking✗ |
| K | anth-adaptive | max | **max** | MAX→MAX | **0** | 175 | max | wire✓/thinking✗ |

wire 档名全对（max 发 `reasoning_effort=max` 不降 medium,Defect A encode 对 chat 同样生效；low 不升档因 kimi 有 low)。**但 th_chars 全 0**。kimi 自述 low/high/max 与 wire 完全对齐（见下）。

### target=anthropic(ds-flash-sankuai-3339,`[low,high,max]`)— 对照 + low 不升档

| 链路 | sdk格式 | effort | wire档名 | intent→target | th_chars | rt | 自述 | 符合 |
|---|---|---|---|---|---|---|---|---|
| G | anth-adaptive | low | **low** | LOW→LOW | 660 | None | low | ✓ |
| G | anth-adaptive | medium | high | MEDIUM→HIGH | 853 | None | minimal | ✓ |
| G | anth-adaptive | high | high | HIGH→HIGH | 945 | None | low | ✓ |
| G | anth-adaptive | max | max | MAX→MAX | 1631 | None | low | ✓ |
| H | responses | max | max | MAX→MAX(pTrue) | 2044 | 0 | low | ✓ |
| I | anth-enabled budget=128000 | (max) | max | MAX→MAX | 3692 | None | low | ✓ |

G-low wire `low`(**不升档**)，对照 A-low(glm `[high,max]` 缺 low → 升 high)，证明 target cap 档位越多 remap 越保真。

## 不符合项详述（唯一):chat target thinking 不可见(th_chars=0)

**现象**：三条 K 链路（anthropic 客户端→kimi chat target)wire 档名全对（low/high/max 按 remap 发出，rt>0 证明上游确实产出了 reasoning)，但 anthropic 客户端收到的响应里**没有任何 thinking block**,th_chars=0。

**上游佐证（绕开 proxy 直 curl kimi chat 端点，task 明确允许)**：
`reasoning_effort=max` 直发 `https://aigc.sankuai.com/v1/openai/native/chat/completions` 返回
`reasoning_content` 长 1059 字符 + 正常 `content` 长 588 + `completion_tokens_details.reasoning_tokens=339` + `finish_reason=stop`。
即**上游确实回了 reasoning_content，但经 proxy 后被丢弃**。

**根因（代码实证，非脑推）**:chat→anthropic 反向转换 `openai_to_anthropic_response`(translate.py:523-531）对 `reasoning_content` 的唯一处理是 `_ENABLE_REASONING_FALLBACK` **空回答兜底**——仅当 text 与 tool_calls 全空时，才把 reasoning_content 以前缀填进一个 **text** block；从不映射成 **thinking** block。本次探针模型正常作答（content 非空），兜底未触发，reasoning_content 被静默丢弃。流式路径（translate.py:743、806-810）同样只做 text 兜底，无 thinking 映射。

**定性**：这**不是 ①a/①b 引入的回归**，而是 chat→anthropic 方向的**既有行为**。①b 的作用域严格是 responses→anthropic(Defect B)，未覆盖 chat→anthropic。任务预期"kimi chat 回传 reasoning_content→thinking、thinking 可见"在当前实现下**不成立**——chat 协议下 reasoning 对 anthropic 客户端不可见（除非触发空回答兜底，且兜底出来的是 text 而非 thinking)。是否给 chat→anthropic 也补 reasoning_content→thinking 映射（①b 的 chat 镜像），超出本次 ①a/①b 范围，需 architect/主会话决策，本报告只如实记录现象、不临场改设计。

## 附带观察（不构成不符合项）

1. **rt(reasoning_tokens)拆分因协议而异**:responses/chat target 的响应 usage 带 reasoning 明细（rt 有值）;anthropic target 经 anthropic 客户端（B/F/G/I）响应用量不带 reasoning 拆分（rt=None)，经 responses 客户端（D/H）正转换后 reasoning_tokens 未从 thinking 回填（rt=0)。这是 usage 映射细节，不影响 thinking 可见性判定（这些链路 th_chars 全 >0)。
2. **模型自述档位是不可靠软信号**:glm 各档自述在 low/minimal/medium 间乱跳、与实际 wire 无关；kimi 自述 low/high/max 恰好与 wire 对齐。权威证据是 **wire 档名（证明发了什么）+ th_chars（证明 thinking 是否回传）+ rt 相对量（同 target 内 max>high 佐证 effort 生效）**，自述仅旁证。本轮 max 档 rt 普遍高于同 target 的 low/medium（如 glm-responses max rt=1103 vs low 905 vs medium 677)，与 effort 生效一致；但因探针是简单题，绝对值都不大。
3. **并发流量会污染 wire 日志归属**:reasoning_debug 行不含请求标识，cc/codex 池内 supply 的 debug 行与 eval 请求混在一起。本次通过"eval supply 全部选池外 id + 按 supply id 过滤日志"规避，结果可信。

## 与推演预期的一致性

`/tmp/trace_combos.py` 的"修复后"列（encode 发 `level.name.lower()`、decode 全表认 max）与本次 live wire **逐档一致**:
- responses/chat target:low/medium→high(glm `[high,max]` 升档）、high→max、max→**max**（修复前为 medium,已消除）;
- responses sdk max:decode present=True（修复前 present=False,已消除）;
- ds-flash `[low,high,max]`:low→low（不升档）、medium/high→high、max→max;
- chat kimi `[low,high,max]`:low→low、medium/high→high、max→max（补充推演，脚本未含 chat 组合，已单独用真实 codec/remap 复算确认）。

## config 还原校验

- 改前 md5 `0b5c9344ad5d32b893357bb0ef9a08d6` → 测中改 → 从字节备份 `/tmp/e2e_config_orig.json` 还原。
- **还原后 md5 = `0b5c9344ad5d32b893357bb0ef9a08d6`，与改前一致**;`git status` 对 config 无改动。
- 运行进程已 reload 回原 config(routes=[claude,openai,nation1,nation2]、strategies=[cc,codex]、supplies=25)，并**重启去掉 debug env** 复原到用户原有进程状态（仅 `MODEL_PROXY_PORT=18889`)，健康检查通过。

## 范围与限制

- 本环境无 pytest(python3.14 无 pytest 模块、无 venv),**形式化单测回归未重跑**；代码层正确性由 `/tmp/trace_combos.py`（驱动真实 codec/capability/ladder 模块）+ 本次 live e2e 双重覆盖。①a/①b 的单测断言反转（test_reasoning.py:768/805、test_translate.py:1657）在落地/复核阶段处理，不在本次 e2e 范围。
- 全部用例为非流式。①b 的流式状态机（`ResponsesToAnthropicStreamAdapter`）与非流式共用同一 `_extract_reasoning_thinking_text` 词表提取逻辑，非流式 th>0 已证明词表识别正确；流式逐事件时序未单独跑。
- 探针为单道简单推理题，rt 绝对值偏小；max 档 thinking 截断（max_tokens 治理，方案②③④）不在本次 ①a/①b 验证范围。
