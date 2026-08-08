---
type: design-decision
status: draft
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, anti-loop, id-guard, 务实, 实施方案]
---

# A+B 档实施方案：tool_use id 守卫（检测 + 出站修复）

> 用户已拍板做 A（观测告警）+ B（协议修复），[务实] 路径：最小改动、可灰度、可回滚。
> 前置分析：[[2026-08-08-subagent-wakeup-chain-and-antiloop-feasibility]]（故障链路还原 + 分档建议）。
> 本文只出方案，不含代码实现。

## 0. 关键事实更正（影响落点，必须先讲）

前序文档推断"kimi 流量经 translate.py 的 ANTHROPIC_TO_CHAT 转换"，**实证不成立**：
`config/model_proxy_config.json` 中全部 kimi-k3 / glm supply 均为 `protocol: anthropic`
（网关统一暴露 `/v1/anthropic/v1/messages`），source==anthropic 时 `pick_translator` 命中
**PASSTHROUGH**（server.py:751 决策表，`server.py:1339-1362` 字节级透传，不解析响应）。
含义：

1. **响应侧没有现成的转换挂钩点**——passthrough 流式路径是字节拷贝（`_write_streaming_response`），
   在其上做 id 改写需要新增 SSE 解析层，热路径成本高、风险大。
2. **请求侧 body_json 是全量解析的**（`_forward` 第 3 步，server.py:987-991），且 passthrough 分支
   已有"改 body_json 后重新序列化"的成熟先例（model 改写、reasoning 字段 merge，server.py:1184-1195）。
3. 因此本方案把 **A 检测与 B 修复都落在请求侧**：扫描/改写的是**出站请求副本**中的历史消息，
   不动客户端持久化历史，不需要解析任何响应。响应侧改写降级为可选后续项（§6 论证为什么可以先不做）。

## 1. 设计论证（三个关键决策）

### 1.1 唯一性要保证到什么程度：跨全量历史，不是单响应内

kimi 的 id 重复是**跨回合**的：第 N 回合的 `Agent_221` 与第 N+1 回合重派的 `Agent_221` 在客户端
拼装的 messages 里同时出现，tool_use↔tool_result 配对产生歧义。所以"单次响应内唯一"不够，
判定基准必须是"**新 id 不得与当前请求 messages 历史中任何已存在的 id 冲突**"。
好消息：无状态转发模型下，每个请求自带全量历史——**历史 id 集合从请求体现取，proxy 无需跨请求状态**。

### 1.2 为什么修历史（请求侧）而不是只修新响应（响应侧）

前序文档的响应侧改写思路（改新发响应里的重复 id，靠客户端回带实现自洽）在 passthrough 下
成本高（要解析 SSE）。重新审视后发现请求侧修复**更优且更省**：

- **客户端历史里的重复配对，位置是可解的**：协议结构保证 tool_result 紧跟触发它的 assistant
  消息。对重复 id 的第 2..n 次出现做**位置序 FIFO 配对**（改写该次 tool_use 的 id + 其后首个
  未消费的同 id tool_result），出站副本即恢复无歧义。这正是弱模型（kimi）实际看到的上下文——
  **修复的是模型的输入，直击"歧义加剧幻觉"的放大器**。
- 响应侧改写修的是**客户端持久化历史**的卫生（事后取证美观），对当下循环的打断没有直接作用；
  且客户端历史里的存量污染无法由响应侧改写消除。
- 两者兼容，可后续叠加（§6），但务实路径先做请求侧。

### 1.3 幂等性：确定性派生 id，纯函数、可重放

修复是请求体的**纯函数**：重复 id 第 k 次出现 → `toolu_rep_<md5(session_key|id|k)[:16]>`。
- 同一请求体重放（failover 换 supply 重发、reasoning 重试）产出完全一致，无二次改写问题。
- 跨请求稳定：客户端从不回带修复后的 id（它没见过），所以每个请求对存量重复做同样的修复，
  结果逐位相同——**天然幂等，且对上游 prompt 前缀缓存友好**（id 不随请求漂移）。
- 不需要"识别已改写 id"的机制：修复只作用于"历史中重复 id 的第 2..n 次出现"，谓词本身
  与是否曾改写无关。`toolu_rep_` 前缀只是可观测性约定（日志里一眼认出是修复产物）。

## 2. 方案总览

新增一个纯函数模块 + server.py 两处粘合 + config 一个开关节。**不碰** commands.py、translate.py、
响应路径、ACCESS 日志格式、usage_totals。

| 组件 | 文件 | 改动 |
|---|---|---|
| 守卫逻辑（扫描/告警判定/修复） | `core/id_guard.py`（新建，~200 行，纯标准库） | 三个纯函数：`scan_tool_use_ids()` / `detect_agent_redispatch()` / `repair_duplicate_ids()` |
| 请求侧粘合 | `core/server.py::_forward` | 插入点 1：内建命令层之后（~line 1027）调守卫；插入点 2：passthrough 分支（~line 1184）消费 `body_repaired` 标志强制重序列化 |
| 配置 | `config/model_proxy_config.json` + `ConfigStore` | 新增顶层 `guards` 节 + `get_guards()` getter（热重载现有机制自动生效） |
| 测试 | `tests/test_id_guard.py`（新建） | 见 §5 |

config 压缩函数 `compact_config_json` 是对全量 `json.dumps` 文本做正则美化，未知顶层 key 原样
保留（_config_ops.py:80-99 注释自证），新增 `guards` 节无兼容问题。

### 2.1 配置 Schema 与灰度默认值

```json
"guards": {
  "dup_tool_use_id": {
    "detect": true,            // A1 告警，默认开（纯日志，零流量影响）
    "repair": "off",           // B1 修复：off | shadow | on，默认 off
    "repair_supplies": []      // 生效 supply id 列表（精确匹配）；repair=on 时仅这些 supply 的请求被改
  },
  "agent_redispatch": {
    "detect": true,            // A2 告警，默认开
    "similarity": 0.75,        // 归一化 prompt 的 difflib ratio 阈值
    "min_completed": 2         // 历史中相似且已完成派单 ≥ 此数才告警
  }
}
```

灰度节奏：`detect=true + repair=off`（全量观测）→ 观察日志确认命中率/误判 →
`repair=shadow`（计算修复并日志比对，不改 body）→ `repair=on + repair_supplies=[kimi 6 个]` →
视效果推广。**任一档回滚 = 改 config 一个字段，ConfigStore 热重载即时生效，无需重启。**

## 3. 详细设计

### 3.1 `core/id_guard.py`

**`scan_tool_use_ids(body_json) -> dict`**（A1 + B1 共用的单次扫描）
- 遍历 `messages`，收集：每个 tool_use block 的 (id, msg_index, block_index)；每个 tool_result 的
  (tool_use_id, msg_index)；assistant 消息里 name 属 Agent/Task 类的 tool_use 的 (id, prompt, 
  其 tool_result 是否已存在于后续 user 消息)。
- 产出：`{id: [出现位置列表]}`、agent_dispatches 列表。一次遍历，O(blocks)。
- 守卫门控（任一不满足直接返回空，fail-open）：`source=="anthropic"`、`body_json` 含 messages 列表。

**`detect_agent_redispatch(scan, cfg) -> alert | None`**（A2）
- 取 scan 中 Agent/Task 类派单：若最新一次派单的 prompt 与此前 **≥min_completed 次**已完成派单
  相似（`difflib.SequenceMatcher.ratio` ≥ similarity，prompt 先做空白/大小写归一），产出告警。
- **不需要跨请求状态**：历史在请求体内，"前序派单是否完成"从 tool_result 存在性现判。
  这是相对派单时共识的简化——原假设 A2 要维护 per-session 状态，实测不需要。
- 唯一的跨请求设施是**告警去重**（防日志刷屏）：`{(session_key, 最新派单id): 已告警}` 内存 dict，
  参照 CooldownStore 模式（server.py:415），进程内 dict + 惰性 TTL（如 24h）。proxy 本就是
  长驻单进程内存态（CooldownStore/SyntaxPreferenceStore 同此语义），可接受。
- 性能保护：相似度只在"历史中 Agent 派单 ≥2 次"时计算，且最多与最近 10 次比较。

**`repair_duplicate_ids(body_json, scan, session_key) -> (repaired: bool, report: dict)`**（B1）
- 对 scan 发现 count>1 的每个 id：第 1 次出现保留原 id（保护原始配对）；第 2..n 次出现，
  按消息序走 FIFO——改写该次 tool_use 的 `id`，并在其后的 user 消息中找到**首个尚未消费的、
  tool_use_id 等于原 id** 的 tool_result 一并改写为新派生 id（§1.3 确定性派生）。
- FIFO 错配风险：极端乱序（如批量并发工具结果跨消息）下可能配错对——但错配不比原始歧义更糟，
  且 report 记录每条修复，shadow 阶段可审计。
- 只改 `body_json` 这个内存副本；`raw_body` 不动；客户端无感知。
- 若配对找不到对应 tool_result（如工具被拒/中断后结果缺失），只改 tool_use 并在 report 标记
  `orphan=true`（这类消息上游本来就可能 400，修复不引入新风险——被拒的 tool_use 本来就有
  错误态 tool_result 紧跟，属正常配对分支）。

### 3.2 server.py 粘合（两处，~25 行）

**插入点 1**（内建命令层 `if` 块结束之后、route candidates 计算之前，~line 1027）：

```
guards = cs.get_guards()
if guards 启用 and source=="anthropic" and isinstance(body_json, dict) and messages 是 list:
    scan = id_guard.scan_tool_use_ids(body_json)
    # A1
    if dup = scan 中存在 count>1 的 id:
        log.warning("ALERT dup_tool_use_id: session=%s id=%s count=%d model=%s",
                    session尾8位, id, count, request_model)   # 按 (session,id) 去重
    # A2
    if alert = id_guard.detect_agent_redispatch(scan, cfg):
        log.warning("ALERT agent_redispatch_loop: session=%s tool=%s similar_completed=%d ratio=%.2f", ...)
    # B1（按 repair 档位与 supply 门控；supply 列表在此刻尚未选定——见下"supply 门控时机"）
    body_repaired, repair_report = 按档位执行 off/shadow/on
```

**supply 门控时机问题**：`repair_supplies` 按 supply 生效，但插入点 1 时 supply 尚未选定（在后面的
循环里逐候选尝试）。务实处理：repair 判定放在插入点 1 只做 detect/shadow（与 supply 无关），
**on 档的实际改写在 supply 循环内、send_body 计算前**按当前 supply id 门控——与 model 改写/
reasoning merge 同位置（passthrough 分支 server.py:1184 起），改写后必须走
`send_body = json.dumps(body_json)` 重序列化路径（现状：若 target_model 与 reasoning_wire 都为空
则 `send_body = raw_body`，修复会被静默丢弃——所以 passthrough 分支要加
`or body_repaired_pending` 条件强制重序列化；kimi supply 实际都有 target_model，现状已重序列化，
此分支是兜底正确性）。注意循环内每个 supply 尝试都用同一份 body_json：**on 档改写必须在
body_json 的逐 supply 副本上做，或保证改写只发生一次**（推荐：进入循环前若已知 route 候选的
全部 supply 都在 repair_supplies 内，则提前一次性改写；否则在循环内命中首个门控 supply 时改写
并置标志，后续 supply 复用——同一请求内 id 派生是确定性的，重复调用也幂等）。

**插入点 2**（passthrough 分支）：`send_body` 赋值条件加 `body_repaired` 标志，如上。

### 3.3 告警日志格式（接入现有体系）

复用 `_forward` 已有的 `log`（logging.getLogger），与现有 `log.warning("cooldown+failover: ...")`
同风格，key=value 裸文本，**不加 ACCESS 字段**（避免动 UsageTotalsStore._combo_key 的维度）：

```
ALERT dup_tool_use_id: session=1ae74010a865 id=Agent_221 count=3 model=claude-opus[1m]
ALERT agent_redispatch_loop: session=1ae74010a865 tool=Agent similar_completed=3 ratio=0.87 latest_id=Agent_221
REPAIR dup_tool_use_id: session=... id=Agent_221 occurrences=3 shadow=1   # shadow 档
REPAIR dup_tool_use_id: session=... id=Agent_221 occurrences=3 orphan=0    # on 档
```

## 4. 性能评估

- 扫描：O(messages blocks)，仅 dict/str 操作。故障会话 3.4MB transcript ≈ 1400 条消息、
  数千 block，单次扫描实测量级 <5ms；请求体本来就全量 `json.loads`（server.py:989），
  无新增解析。相对上游 RTT（秒级，ACCESS 日志 ms 列普遍 10^4 量级）可忽略。
- A2 相似度：仅当历史 Agent 派单 ≥2 才触发，difflib ratio 比较上限 10 对、单对 KB 级文本，ms 级。
- B1 改写：仅当发现重复才执行；正常流量（无重复）零额外成本（扫描结果 count==1 直接跳过）。
- 内存：告警去重 dict 每 session 每条目 <100B，TTL 清理；量级与 CooldownStore 相当。
- 重序列化：on 档命中时多一次 `json.dumps(body_json)`——passthrough 现状在多数路径本就重序列化
  （model 改写/reasoning merge），增量是零到一次 dumps，MB 级 body 约 10ms 级，可接受。

## 5. 测试方案（`tests/test_id_guard.py`，标准库 unittest，对齐现有风格）

运行：`cd tools/model_proxy && python3 -m unittest tests.test_id_guard -v`，并跑全量回归（现 478 个）。

1. **A1 命中/不命中**：历史含 `Agent_221`×2（各带 tool_result）→ 告警；唯一 id → 无告警。
2. **A1 去重**：同 (session,id) 第二次扫描 → 不重复告警；不同 id → 各自告警。
3. **A2 命中**：两次已完成相似派单（用故障 transcript 的真实 prompt 变体："评估CLI status与help
   展示[理想]" / "评估CLI status和--help展示" 系列）+ 最新一次相似 → 告警；ratio 低于阈值 / 
   完成数不足 / 最新派单不相似 → 无告警。
4. **A2 门控**：source!=anthropic、messages 缺失/非 list → 静默跳过（fail-open）。
5. **B1 基本修复**：重复 id 第 1 次保留、第 2/3 次改写为 `toolu_rep_` 前缀且互不相同；
   配对 tool_result 同步改写；修复后全 body id 唯一。
6. **B1 确定性/幂等**：同一 body 修两次 → 逐位相同输出；对已唯一 body → `repaired=False`，body 不变。
7. **B1 配对边界**：并行 tool_use（一条 assistant 多块）+ 紧跟一条 user 多 tool_result；
   孤儿 tool_use（无 tool_result）→ 只改 tool_use 且 report orphan=1；乱序 user 消息 → FIFO 行为符合预期。
8. **B1 不污染无关内容**：text/thinking block、其他 id、`metadata`、`cache_control` 字段原样。
9. **shadow 档**：body 不被改 + report 内容正确。
10. **server 粘合**（轻量）：构造 `_forward` 级用例成本高，改为直接测"passthrough 分支重序列化
    条件"提炼出的小helper（如 `_compute_send_body(..., body_repaired)`）——若实现时不提炼 helper，
    则至少用 mock 覆盖"body_repaired=True 时 send_body != raw_body"。

## 6. 响应侧改写（B2）评估与搁置理由

派单要求评估"响应侧改写 + 客户端回带自洽"。结论：**机制成立但务实路径暂不做**。

- 成立性：响应里新 tool_use id 若与请求历史 id 冲突，改写为 `toolu_<hex>` 后发给客户端；
  客户端下一请求会把改写后的 id 连同 tool_result 一起回带，配对自洽，proxy 无需识别/二次改写
  （改写谓词"新响应 id ∈ 历史 id 集"天然只作用于新块）。
- 搁置理由：(1) kimi 流量是 passthrough，响应侧无转换层，需新增 anthropic SSE 事件解析器
  （~100+ 行热路径代码，改坏即断流）；(2) B1 已消除模型可见上下文里的歧义（治本的一半），
  客户端历史残留重复只是取证噪声，且 B1 的确定性修复使每个请求的出站上下文始终干净；
  (3) 若未来要在 translate 模式（ANTHROPIC_TO_CHAT/RESPONSES）供应上启用，挂钩点现成
  （`translate.py:518` 非流式 / `translate.py:827` 流式 `_handle_tool_calls_delta`，历史 id 集经
  fwd_ctx 传入即可），届时单独立项。
- responses 协议（codex 客户端）：call_id 同理可重复，但本次故障未涉及，且 codex 侧
  会话语义不同，A/B 均先只覆盖 source==anthropic；responses 扩展留作 follow-up。

## 7. 风险与权衡

| 风险 | 评估 | 缓解 |
|---|---|---|
| 出站 body 被改导致上游 400（kimi 网关校验 id 格式/配对） | 低：配对一致性在修复后更强；`toolu_rep_` 前缀与 proxy 既有 `gen_toolu_id()` 同族，网关上 claude 供应的 `toolu_` id 日常回流无恙 | shadow 档先跑，比对"会改什么"；on 档先限 kimi supply |
| B1 FIFO 错配（乱序历史） | 错配结果不劣于原始歧义；发生面极小（claude-code 产出严格交替） | report 全量记录，shadow 期审计 |
| A2 误报（用户故意重派相似任务） | 纯告警无副作用；阈值 min_completed=2 + ratio 0.75 偏保守 | 阈值 config 可调；观测期校准 |
| 修复破坏 prompt 缓存前缀 | 确定性派生保证跨请求稳定，缓存友好；反倒是**不修**时 kimi 重复 id 不变更利于缓存——但缓存收益不抵故障 | 无需额外动作 |
| 多 client 串扰 | 全部逻辑按 session_key + source 门控；codex/responses 不受影响 | 门控单测覆盖 |
| supply 门控时机（§3.2）导致同请求重复改写 | 确定性派生幂等，重复调用同结果 | 实现时优先"循环前一次性判定"，单测覆盖 |

**回滚**：`guards.dup_tool_use_id.repair="off"` / `detect=false`，热重载即时生效；
`core/id_guard.py` 不被调用即零影响。server.py 两处粘合在 flag 关闭时是无操作分支。

## 8. 验证方式

1. 单测全绿（§5 新增 + 现有 478 回归）。
2. **重放验证**：用故障 transcript 提取 14:47–15:23 时段的请求体（含 6× Agent_221），
   构造 HTTP 请求打到开了 shadow 档的本地 proxy → 日志应出现 1 条 A1 + 1 条 A2 + 每次
   REPAIR shadow 记录，且上游收到的 body（可在 supply url 指向本地 echo 服务验证）id 全部唯一。
3. **灰度观测**：detect 全开跑 3-7 天，核对：命中是否全部来自 kimi 供应会话；A2 有无误报
   （人工抽查告警 session 的 transcript）。
4. **端到端**：on 档限 kimi 后，用 Claudian 重复同类派单任务，确认 (a) 会话不再出现跨回合
   重复 id 进入上游（proxy 日志审计），(b) 客户端会话无异常（tool_result 正常渲染、无 400）。

## 关联

- [[2026-08-08-subagent-wakeup-chain-and-antiloop-feasibility]]（前置分析，本文更正其响应侧落点推断）
- [[2026-08-04-in-band-route-command-design]]（请求侧拦截点与 fail-open 门控先例）
- [[2026-07-28-session-route-dispatch-design]]（session_key 提取）
- 故障 transcript：`~/.claude/projects/-Users-vincentwang-Documents-NoteVault/2896beec-d221-4013-a073-1ae74010a865.jsonl`
