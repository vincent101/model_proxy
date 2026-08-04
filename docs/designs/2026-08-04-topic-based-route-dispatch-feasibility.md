---
type: design-decision
status: draft
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, session-routing, topic-routing, feasibility]
---

# model_proxy 按「问题主题」分配 route 的可行性与目标架构

> [理想] 路径产出：不计迁移成本，先问「架构上正确的做法是什么」，再看它能不能落。
> 前置基线：[[2026-07-28-session-route-dispatch-design]]（现状 route_pool 机制）、
> [[2026-07-28-session-load-balancing-feasibility]]（前置调研，已 superseded 但数据可参考）。
> 本文只做调研与设计，不含实现代码，未改动任何实现文件。

## 背景与问题

现状 route_pool 是「看会话身份」分配：`md5(session_key) % 权重总和` 把会话钉到一个 route，
求跨会话均匀（`core/server.py:600-681` `extract_route_candidates`）。与请求内容无关。

用户诉求：能不能改成/叠加「看会话正在处理的问题主题」分配——写代码走 claude、调研走
kimi/glm、简单任务走 deepseek 省成本。

**本文的核心结论先行**：这个诉求在本架构里**已经有一条实现路径在跑，而且跑在比代理层更合适的位置**
（客户端 agent 体系 → tier → 各 route 的 tier→supply 表）。代理层再做一次主题识别，是拿信息更少的
第二个分类器去覆盖信息更全的第一个分类器。详见 §3。以下逐维度给依据。

---

## 1. 「主题」在代理层能不能识别、怎么识别、代价多大

### 1.1 先厘清一个被混淆的前提：可读 ≠ 可判定

`_forward` 已在 `core/server.py:978-983` 完整 `json.loads` 了 body，`messages`/`system`/`tools`
全部零成本可读。但这只解决「拿到字节」，不解决「判定主题」：

- `session_id` 是**确定性提取**：`metadata.user_id` 二次 `json.loads` 取字段（`server.py:497-520`），
  输入→输出是函数关系，无歧义、无错判概念。
- `topic` 是**归类判断**：需要先定义类目体系，再把自然语言映射进去，天然有准确率、有漂移、有维护成本。

两者不是同一类工程问题。现有 `extract_session_key` 的低成本不能类推到 topic。

### 1.2 硬约束（先摆出来，它直接淘汰一半选项）

**本项目零第三方依赖，纯标准库。** 已核实：`core/server.py`、`core/translate.py`、`_config_ops.py`、
`core/reasoning/*` 的全部 import 只有 stdlib + 项目内模块；仓库无 `requirements.txt`、无 `pyproject.toml`、
无 `setup.py`。

推论：任何需要 `transformers` / `sentence-transformers` / `onnxruntime` / 甚至 `numpy` 的本地分类器
（BERT、embedding 相似度、ModernBERT）**都不是"加个依赖"，而是给一个刻意保持零依赖的单文件代理
引入模型权重 + 推理运行时**。这不是成本高低问题，是设计取向的整体反转。

### 1.3 延迟不是主要矛盾（实测数据推翻了直觉）

用户对延迟敏感是合理担忧，但本代理的真实延迟量级让"轻量分类"的开销可以忽略：

| 指标 | 实测值（4866 条 ACCESS，2026-07-29 14:47 → 2026-08-04 11:48） |
|---|---|
| p10 | 4568 ms |
| median | 9652 ms |
| p90 | 39397 ms |
| 最快一条成功请求 | 1548 ms |
| < 3000 ms 的占比 | 3.1% |

对照业界公开口径（RouteLLM 相关综述）：规则路由 <1ms，embedding/ML 路由 5-50ms。
即 5-50ms 落在本代理 median 9652ms 的 **0.05%-0.5%**，用户不可感知。

所以**结论要修正**：反对本地分类器的理由是 §1.2 的依赖约束，**不是延迟**。
真正被延迟否决的只有下面的 (b)：额外发一次 LLM 调用做分类，按本代理最快成功请求 1548ms 估，
至少 +1.5s，占 median 的 15%+，且要多烧一次配额、还要解决"分类调用打到哪个后端"的鸡生蛋
（分类器自己也要选 supply、也要 failover、也可能 429）。**(b) 明确否决**。

### 1.4 四条技术路径逐条评估

| 路径 | 可行性 | 延迟 | 准确率/可维护性 | 与 PASSTHROUGH 哲学冲突 |
|---|---|---|---|---|
| **(a) 关键词/启发式**（system/首条 message 出现"写代码""debug"等） | 可行，纯 stdlib `re` | <1ms，可忽略 | **差**。CC 的 system prompt 是工具说明 + 环境注入 + agent 定义拼装，用户自然语言只占很小一段且位置不固定；关键词表要人肉长期维护，CC 版本升级即漂移 | 中。要读 `messages`/`system` 语义内容，越过了"只做协议透传"的边界 |
| **(b) 额外调 小模型/分类器 API** | 可行但代价大 | **+1.5s 起（median 的 15%+）**，另加一次配额 + 鸡生蛋（分类调用自身的选路/failover） | 准确率最高，但引入"分类失败/超时/429"整套新故障面 | **高**。代理从"转发器"变成"会自己发起推理请求的 agent" |
| **(c) 结构化弱信号**（tools schema 指纹、system 里的 agent/skill 声明、客户端类型） | 可行，纯 stdlib，**且是确定性匹配而非 NLP** | <1ms | **相对最好**——但它识别的其实不是"主题"，是"**调用者角色**"（见 §1.5） | 低-中。读的是结构字段而非语义文本 |
| **(d) 业界做法** | 见下 | — | — | — |

**(d) 业界实践核实**（已抓原文，非印象）：

- **RouteLLM**（ICLR 2025，UC Berkeley/Anyscale/Canva）：训练出的**二分类器**，只在 strong/weak
  两个模型间选，明确列为局限"binary routing only, not multi-model"。训练数据 Chatbot Arena 80k
  人类偏好对战 + GPT-4-as-judge 标注。四种内部实现（Matrix Factorization 默认、BERT、
  Causal LLM w/ Llama3-8B、Similarity-weighted Ranking）。关键：**"decides from the query alone"，
  不看对话历史，每轮独立路由。**
- **vLLM Semantic Router**：fine-tuned ModernBERT 做多任务意图分类，以 Envoy ExtProc（gRPC）形态
  旁挂部署，六类信号进布尔表达式树。论文未单列分类器延迟、也未单列分类准确率。
  **roadmap 里"stateful multi-turn routing"仍是待做项**，即当前版本按单请求无状态路由。

**两者的共同前提与本项目的结构性错配**：它们服务的是**无状态单轮 API 流量**（每个请求是一个独立
用户问题，路由完即结束）。本代理承接的是 **agentic 多轮会话**——带 prefix cache、带工具循环、
带子 agent 派生。"每轮独立分类路由"这个前提在这里不成立（§2 展开）。

### 1.5 (c) 的真实语义：它识别的是「角色」不是「主题」，而角色已经在客户端被显式声明了

`tools` 列表与 agent system prompt 是确定性指纹。核实 `.claude/agents/*.md`：

| agent | model | effort | tools 特征 |
|---|---|---|---|
| architect / -xhigh / -max | opus | high / xhigh / max | 含 WebSearch/WebFetch/Skill/context7/friday-websearch |
| implementer / -max | sonnet | high / max | 仅 Read/Write/Edit/Bash/thinking |
| implementer-opus-xhigh | opus | xhigh | 同上 |
| reviewer | sonnet | high | 仅 Read/Bash/thinking |
| runner | haiku | — | 含 WebSearch/open-websearch/friday-websearch |

即 tools 指纹能区分角色。**但这条信息在客户端本就是显式已知的，并且已经通过 `model` 字段发出来了**
（agent frontmatter 的 `model: opus/sonnet/haiku` → CC 发出对应 model 标签 → 代理 `_MODEL_TIER_MAP`
精确查表成 tier，`server.py:579-583/684-688`）。

代理从 tools 指纹反推角色，是**把客户端已经明确告诉你的事，再猜一遍**。信息量只减不增。

---

## 2. 与会话粘性的冲突：必须先回答"会话级还是请求级"，而数据已经把会话级否决了

### 2.1 一个决定性的实测事实：这里的 session 不是"一个话题"，是"一条持续数天的工作线"

对 30 个 session 逐个统计（口径：ACCESS 行按 `session=` 分组，相邻请求间隔 >30min 视为切分一个活动块）：

| session（前12位） | 请求数 | 时间跨度 | 活动块数 | tier 组成 | route |
|---|---|---|---|---|---|
| 56d5aed9-6df | 1875 | **139.3 h** | **11** | sonnet 918 / opus 907 / haiku 50 | claude ×1875 |
| c2e29916-326 | 1263 | **140.1 h** | **20** | sonnet 1060 / opus 191 / haiku 12 | nation 1179 / claude 84 |
| 28e6491c-59c | 382 | 102.9 h | 5 | sonnet 257 / opus 125 | claude ×382 |
| 51e79863-be1 | 277 | 101.0 h | 6 | sonnet 199 / haiku 61 / opus 17 | claude ×277 |
| 8075d979-87a | 277 | — | — | sonnet 203 / haiku 53 / opus 21 | claude ×277 |

一个 session_id 存活 **4-6 天、横跨 5-20 个互不相干的活动块**（最大空档 63-70 小时）。
`/resume`、`/compact`、子 agent 全部复用同一 id（前置文档 §1 已实测）。

**同会话内 tier 切换率（相邻请求 tier 不同的比例）**：
56d5aed9 4.7%（88 次切换）、c2e29916 2.8%（35 次）、28e6491c 4.2%、51e79863 7.2%、8075d979 5.8%。
即单个 session 内部**几十次**在 opus/sonnet/haiku 之间来回——这正是主会话按 CLAUDE.md 决策树
派不同 agent 的痕迹。

### 2.2 由此得到两个子方案的判定

**「会话级主题路由」（首条定调、全程固定）—— 语义上不成立，直接否决。**
它要求 session 是一个主题连贯单位。实测是：拿一条 **139 小时、1875 请求、11 个活动块、
内部切换 tier 88 次**的工作线，用它的**第一条消息**定调，然后钉住 6 天。这不是"稳定"，是"错一次错到底"。
用户问的"会话开始时判断一次主题"在别的产品里合理，在这里因为 session_id 的粒度（=CC 会话生命周期，
含 resume/compact/子agent）而不成立。

**「请求级主题路由」（每条消息可换 route）—— 技术上可做，但要付三笔代价：**

1. **打破 prefix cache。** 观测到强证据（`usage_in` 分布，仅 status=200）：

   | route/tier | n | usage_in 中位数 | p90 | usage_in ≤5 的占比 |
   |---|---|---|---|---|
   | claude/sonnet | 1900 | **2** | 2 | **97%** |
   | claude/opus | 1157 | **2** | 305 | **86%** |
   | nation/sonnet | 1060 | **1224** | 26227 | 1% |
   | claude/haiku | 166 | 20156 | 57906 | 1% |
   | nation/opus | 116 | 38128 | 94005 | 0% |

   claude route（anthropic passthrough）绝大多数请求上报 `input_tokens≈2`，而 nation route 上报
   上千到数万。CC 单请求实际前缀远大于 2 token，所以合理解释是：claude 链路命中了上游 prompt cache、
   只计非缓存增量；nation 链路每次全量重读。
   **标注**：这是推断，不是直接证据——ACCESS 日志只取 `input_tokens/prompt_tokens`
   （`server.py:1303-1306`），**未记录 `cache_read_input_tokens`**，无法直接算命中率。若要确证，
   需先把 cache 字段落进日志（见 §5 验证方式）。但方向明确：**逐请求换 route = 换上游 = 前缀重算**，
   而前缀就是 CC 最大的成本项。
2. **打破工具循环的连续性。** 一次子 agent 的 Task 调用是"多轮 tool_use ↔ tool_result"闭环。
   循环中途换家族，不只是风格跳变——还会跨越 PASSTHROUGH 与 ANTHROPIC_TO_CHAT/RESPONSES 两套
   转换路径（`pick_translator`，`server.py:743-750`），tool schema/tool_call id 语义在两侧由
   `translate.py` 转换，中途切换等于在同一个工具循环里换协议栈。风险不在"输出风格"，在正确性。
3. **需要引入抗抖动状态。** 用户提到的"连续 N 条指向新主题才切换"确实必要，但它把当前
   **纯函数、无状态、重启一致**的分配（前置文档 §2 的核心优势）变成需要 per-session 滑动窗口 +
   TTL 清理 + 并发保护的有状态组件。这是本方案最大的隐性成本。

---

## 3. 业务价值是否成立：用现状数据与配置结构说话

### 3.1 决定性发现一：跨家族按主题分流，**在 tier→supply 层已经实现了**

`routes` 的结构不是"一个 route 绑一个家族"，而是"每个 tier 各自挂一张 supply 表"，可自由混家族。
核实当前生产配置 `config/model_proxy_config.json`：

| route | opus | sonnet | haiku |
|---|---|---|---|
| **claude** | claude-opus-5 | claude-sonnet-5 | **deepseek-v4-pro-tencent ×2** |
| openai | gpt-5.6-sol | gpt-5.6-terra | gpt-5.6-luna |
| deepseek | ds-v4-pro ×2 | ds-v4-pro ×2 | ds-v4-flash ×2 |
| nation | **kimi-k3 ×2** | **glm-5.2 ×2** | **deepseek-v4-pro ×2** |

即用户实际在跑的 `claude` route，其 haiku 档**已经不是 claude 家族**，而是 deepseek。
"简单任务走 deepseek 省成本"这条诉求**已经在生效**，且是用改一行 tier→supply 映射实现的，
没有动一行代码。同理 nation route 的三档本身就是 kimi/glm/deepseek 三家混装。

### 3.2 决定性发现二：客户端已有一个信息更全的主题分类器在跑

完整链路（已核实各环节）：

```
用户提出问题
  │
  ▼  ① 主会话按 CLAUDE.md「派单决策树」判断任务性质 ← 这一步就是主题分类
  │     判断/方案→architect；只读查找→runner；有耦合的落地→implementer；复核→reviewer
  ▼  ② agent frontmatter 声明 model + effort（.claude/agents/*.md 已核实，见 §1.5 表）
  ▼  ③ CC 发出 model=claude-opus/-sonnet/-haiku（ANTHROPIC_DEFAULT_*_MODEL，~/.claude/settings.json）
  ▼  ④ 代理 _MODEL_TIER_MAP 精确查表 → tier（server.py:579-583, 684-688）
  ▼  ⑤ route 的 tiers[tier] → supplies → 真实家族（§3.1 的表，可任意混家族）
```

①的分类质量为什么代理层追不上：主会话持有完整对话上下文、用户的历史偏好、项目约定，
且是**用户可直接编辑的显式规则**（改 CLAUDE.md 决策树 / 改 agent frontmatter 即生效）。
代理层只能看到单个 HTTP body，看不到"用户上一句说了什么、这个项目的惯例是什么"。

**所以"topic → 用什么模型"这件事没有缺口，缺口只在"同一 tier 内想按主题去不同家族"。**
而那个缺口的代价是 §2.2 的三笔（前缀缓存 + 工具循环 + 有状态抗抖动）。

### 3.3 现有 `session_overrides` 已覆盖到什么程度（实测）

统计窗口内 nation route 全部 1188 条请求的来源：

| session | nation 请求数 | 来源 |
|---|---|---|
| c2e29916-326e… | 1179 | `session_overrides` 手动指定 |
| 7b4cb865-c308… | 9 | `session_overrides` 手动指定 |
| **合计** | **1188 = 100%** | **全部来自手动指定，哈希分配贡献 0** |

哈希贡献 0 的原因：当前 `route_pool` 只有一项（`claude`, weight 1），哈希无处可分。
即**现网跑的其实是"单 route + 手动 override"，自动分配机制虽已落地但未启用**。

另注一处配置卫生问题（非本方案范畴，供顺手处理）：`session_overrides` 现有 5 条，其中
`6ad2e1b5`（22 请求）、`4c3ba96f`（29 请求）、`cf9e4ee3`（2 请求）在窗口内 **nation 命中 0 次**
——它们是前置文档 §4b 预期的"僵尸条目"（会话已结束或条目添加晚于会话活跃期，配置 mtime 08-04 11:37，
其中 `cf9e4ee3` 末次请求 11:22、`4c3ba96f` 末次 11:35，均早于或紧邻改配置时刻）。行为无害，符合设计预期。

**手动 vs 自动的增量收益评估**：用户已在用手动机制，且用得重（1179 条请求钉在 nation）。
自动化能省掉的只是"从日志抄一个 session_id 粘进 config"这一次动作——而代价是 §2.2 的三笔 +
一套需要长期维护的分类体系。**这是本方案投入产出比最不成立的一点。**

### 3.4 真实痛点核对：数据显示的痛点不是"选错模型"，是"配额打满"

| 指标 | 本窗口（5.9 天，4866 条） | 前置文档窗口（5 天，11943 条） |
|---|---|---|
| failover 触发 | **87 = 1.8%** | 33 = 0.28% |
| route_failover（跨 route 兜底） | **87 = 1.8%** | 机制尚未存在 |
| attempts ≥2 | 38 | 13 |
| status 503 | **273 = 5.6%** | — |

87 次 route_failover **全部**是 `nation/opus`（kimi-k3 429）→ 跌落 `claude/opus`；其中 58 次救回 200、
**29 次仍以 503 收尾**。冷却触发明细：kimi-k3-3339 ×32(429)、claude-opus-0956 ×32(429)+×10(500)、
kimi-k3-0956 ×25(429)、ds-pro ×6(429)、glm-52/claude-sonnet ×3(502)。

**failover 率从 0.28% 升到 1.8%（6.4 倍），503 占 5.6%。这是本次数据里唯一显著恶化的指标，
而它是配额/可用性问题，与"主题"无关。** 主题路由不能解决它；能解决它的是把 route_pool 填实、
让哈希与 route_failover 有地方可去（现在 pool 只有一项，哈希形同虚设）。

---

## 4. 目标架构（若用户仍决定要做，这是理想形态；含被约束否决的更优形态）

### 4.1 理想终态其实不是"代理猜主题"，而是"客户端显式声明意图"

架构第一性原理：**意图的持有者应该直接声明意图，而不是让下游从副产品里反推。**
客户端（主会话）100% 确定当前任务性质；代理推断必然信息有损。所以理想终态是一条
**声明式路由标签通道**：客户端带一个显式 label，代理只做 `label → route` 的查表映射。
零推断、零延迟、100% 准确、规则完全由用户掌握、与 PASSTHROUGH 哲学不冲突（读一个字段，不读语义）。

**但这条理想路径被客户端能力硬约束卡住**（已核实）：

| 候选通道 | 能否承载 per-request 标签 | 依据 |
|---|---|---|
| `ANTHROPIC_CUSTOM_HEADERS` | **否**，进程级静态值 | 前置文档 §1 已排除；SessionStart hook 是单进程共享模型 |
| `metadata.user_id` | **否**，只含 `device_id`/`account_uuid`/`session_id` | `server.py:497-520` + 前置文档 §1 实测格式 |
| `model` 字段 | **仅 3 个值**，且由 env 进程级固定 | `_MODEL_TIER_MAP` 精确 3 值（`server.py:579-583`）；`ANTHROPIC_DEFAULT_*_MODEL` 在 `~/.claude/settings.json` |
| agent system prompt 内嵌标记 | **可能可以（未验证）** | 见 4.2 |

即：**在当前 CC 能力下，per-request 显式意图通道只有"3 个 tier 标签"这一条，而它已被 tier 语义占满。**
理想架构因客户端限制不可得，这是本方案最根本的天花板——不是我们设计得不够好。

### 4.2 次优但仍属声明式：agent system prompt 内嵌路由标记（**关键前提未验证**）

用户自己写 `.claude/agents/*.md`，可在 body 里放一行固定标记（如 `ROUTE-HINT: research`）。
代理对 `system` 字段做**精确子串匹配**提取标记 → 查表定 route。这仍是"客户端声明"，不是 NLP：
确定性、<1ms、纯 stdlib、规则由用户维护、错判概念不存在。

**必须先验证的阻塞前提**（我没有验证，不能当结论用）：
子 agent 的 system prompt 是否**原样出现在** CC 发往 `/v1/messages` 的 `system` 字段中、
在多轮与 compact 后是否稳定。验证方法沿用前置文档 §1 的沙箱套路（`/tmp/model_proxy_sandbox` 端口
18899 + `claude --setting-sources project,local`，务必带 `--setting-sources` 否则会误打生产 18889）。
**若该前提不成立，4.2 整条方案作废。**

已知副作用（可接受）：标记进入被缓存的前缀 → 每个 agent 首次调用重建一次 cache，之后稳定；
且它是 in-band signaling（把路由控制信息塞进 prompt），架构上不优雅，属"客户端无带外通道"的妥协。

### 4.3 组件与 schema：topic 应当是"候选排序策略"，不是新的优先级层

这是用户第 4 问的直接回答。**必须选后者（策略替换），不能选前者（新增一层）**，理由是现有代码形状：
`extract_route_candidates` 返回的是一个**有序候选列表**，`_forward` 的外层 for 循环
（`server.py:1044`）依赖这个顺序做 route_failover。任何新机制若只产出"一个 route"而不产出"完整有序
候选列表"，就会打断 route_failover。

```jsonc
{
  "client_token": "cc",
  "route_pool": [
    { "route_id": "claude", "weight": 2 },
    { "route_id": "nation", "weight": 1 },
    { "route_id": "deepseek", "weight": 1 }
  ],
  "dispatch": {
    "type": "topic_hint",              // 与 "session_hash" 二选一，同一个槽位
                                        // （注：dispatch.type 当前是预留字段，代码不读取，见 README 3.4）
    "session_overrides": { "<uuid>": "nation" },   // 不变，优先级仍最高

    // type=topic_hint 时生效
    "hint_source": "system_marker",    // 可插拔来源：未来可加 "tools_fingerprint" 等
    "hint_pattern": "ROUTE-HINT:\\s*(\\w+)",
    "topic_routes": {
      "research": "nation",
      "code":     "claude",
      "bulk":     "deepseek"
    },
    "on_no_hint": "session_hash"       // 无标记/未命中 → 退回哈希（不是硬失败）
  }
}
```

优先级链（三层，与现状同构）：

```
1. session_overrides[session_key] 命中 → 该 route 置首（现状，server.py:671-677）
2. dispatch.type 选定的排序策略：session_hash | topic_hint
     · session_hash：md5 旋转（现状 _hash_rotate，server.py:655-666）
     · topic_hint  ：命中的 topic route 置首，其余按 §2 哈希顺序跟随作兜底
3. 都不可用（session_key 缺失 / 无标记）→ route_pool 首项 + 原顺序（现状 server.py:679-681）
```

关键设计约束：**topic 命中只改变"谁是第一候选"，不改变"其余候选仍完整跟随"**。这样
route_failover（`server.py:1411-1421`）、supply 级 failover、CooldownStore 全部不动，
与前置文档 §3"两层正交"的性质保持一致。

**分类结果缓存**：若采用 4.2 的确定性标记，**不需要缓存**——每请求现算 <1ms，且保持纯函数、
无状态、重启一致（前置文档 §2 的核心优势得以保留）。只有走 §1.4 的 (a)/(b) 才需要 per-session
缓存 + 抗抖动窗口，那会引入有状态组件 —— 这是又一条"选确定性标记、不选语义分类"的理由。

### 4.4 改动量与耦合面（比照前置文档 §5 口径）

以 4.2+4.3（确定性标记 + 策略槽位）为准。若改走 §1.4 (a)/(b)，工作量与风险显著高于此表。

| 文件/模块 | 改动 |
|---|---|
| `core/server.py` | 新增 `extract_topic_hint(body_json, pattern)`（对 `system` 做正则精确匹配，需处理 `system` 可能是 str 或 content-block 数组两种形态）；`extract_route_candidates` 内按 `dispatch.type` 分派排序策略（**签名要加 body 或 hint 参数——这是本次唯一的接口面变更**，现签名只收 `strategy/session_key/routes_map`，`server.py:600`）；`_forward` 调用点相应传参（`server.py:997`）；ACCESS 日志加 `topic=<hint或空>` 字段（`server.py:933-952`，观测必需，否则无法验证命中率） |
| `_config_ops.py` | `dispatch` 校验扩展：`type` 枚举、`topic_routes` 的 value 必须是合法 `routes` id（沿用 `session_overrides` 的口径：不要求在 route_pool 内）、`hint_pattern` 必须能编译、`on_no_hint` 枚举。注意现有 CLI 已明示不支持编辑 route_pool/dispatch（`_config_ops.py:855-857`），本次要么沿用"只支持手改文件"，要么补 CLI |
| `tests/test_session_route_dispatch.py` | 现有 24 个 case 是 `extract_route_candidates` 的行为契约，**改签名会全部受影响**，需同步调整 + 新增 topic 分支 case |
| `config/model_proxy_config.example.json` | 加 `type: topic_hint` 示例 |
| `README.md` | 3.4 节补 topic 分派；4.4 三阶段匹配补一步；附录 A strategies 字段表 |
| `.claude/agents/*.md`（**vault 侧，非 model_proxy**） | 若走 4.2，需给每个 agent 的 body 加 `ROUTE-HINT` 行——这是**跨项目改动**，且属"漏改一个 agent 就静默退回哈希"的耦合 |

**耦合点（会漏改即出错的地方）**：
1. `extract_route_candidates` 签名变更 → `_forward` 调用点 + 24 个既有测试，漏一处即断。
2. `system` 字段两种形态（字符串 / content-block 数组，`translate.py:289 _system_to_openai_message`
   已在处理这个差异）——只处理一种形态会导致标记静默不命中、无声退回哈希，**是最容易漏且最难发现的坑**。
3. topic 命中后**必须仍返回完整候选列表**，否则静默破坏 route_failover（该路径本窗口触发 87 次、
   救回 58 次，回归了不会立刻暴露）。
4. agent 定义与 `topic_routes` 是两处独立维护的映射，不一致时静默降级，无强校验手段。

**量级判断**：有正确性耦合、改签名带动既有测试、跨 model_proxy 与 vault 两个范围。
按 CLAUDE.md 决策树 → **implementer + reviewer**，不适合 runner。

---

## 5. 风险与权衡

### 5.1 主要风险

1. **两个分类器竞争同一决策，且下游那个信息更少。** 客户端决策树（信息全、用户可控）与代理
   topic 表（信息少、独立维护）会给出不一致结论，且不一致时无告警、静默生效。这是架构层面
   最实质的风险，不是实现细节。
2. **前缀缓存损失可能远超模型选择收益，且当前无法量化。** §2.2 的 `usage_in` 分布强烈提示
   claude 链路在吃 cache，但 ACCESS 未记 `cache_read_input_tokens`（`server.py:1303-1306`），
   **上线前无法测出"换家族省的钱 vs 丢的 cache"净值是正还是负**。这是"不建议先做"的关键理由之一：
   收益方向都还没测出来。
3. **未验证前提**：4.2 依赖"子 agent system prompt 原样出现在 `system` 字段且稳定"，**未验证**。
4. **分类体系的长期维护成本无衰减。** CC 升级、agent 增删、skill 变化都会让规则漂移，而漂移是
   静默的（静默退回哈希，不报错）。
5. **一致性哈希的无状态优势可能被牺牲**（仅当走语义分类 + 抗抖动窗口时）。

### 5.2 迁移/落地代价提示（理想路径必附，不因代价改设计）

- **纯 model_proxy 侧**：约 6 个文件，核心改动集中在 `extract_route_candidates` 一个函数 +
  一处调用点，diff 不大但要连带调 24 个既有测试。属"小 diff、高正确性敏感"。
- **跨项目侧（4.2 特有）**：要动 vault 的 9 个 agent 定义文件，且 model_proxy 与 vault 的 agent
  体系从此产生**耦合**——改 agent 定义要记得同步 `topic_routes`，反之亦然。原本两者是解耦的
  （agent 只声明 model，代理只认 3 个 tier），这层耦合是新增的长期负债。
- **可观测性前置**：验证收益需要先补 `cache_read_input_tokens` 到 ACCESS 日志（独立小改动，
  且**无论做不做主题路由都值得做**，见 §6 建议 3）。

### 5.3 权衡结论：三个子方案对比

| 方案 | 可行性 | 收益 | 代价 | 评价 |
|---|---|---|---|---|
| **会话级 + 任意分类方式** | 语义不成立 | 负（错一次钉 6 天） | — | **否决**。session_id 粒度 = 4-6 天工作线（§2.1 实测） |
| **请求级 + 语义分类（(a)/(b)）** | (a) 脆弱 / (b) +1.5s + 鸡生蛋 + 违背零依赖 | 未证实 | 最高（有状态、前缀缓存、协议栈中途切换） | **不建议** |
| **请求级 + 确定性标记（(c)/4.2）** | 可行（依赖一项未验证前提） | 有限（tier 层已覆盖大部分，§3.1/3.2） | 中（跨项目耦合 + 前缀缓存未量化） | **唯一值得考虑的形态，但仍建议缓做** |

---

## 6. 最终结论性建议

### 结论：不建议现在做主题路由。理由不是"太难"，是"要解决的问题已被解决在更合适的层，而剩下那点缺口的代价大于收益"。

四条独立依据，任一条成立都足以缓做：

1. **跨家族分流已在跑。** `claude` route 的 haiku 档实际是 `deepseek-v4-pro`，nation 三档是
   kimi/glm/deepseek 混装（§3.1 实测配置）。"按任务去不同家族"是**改一行 tier→supply 映射**的事，
   已经在用，不需要分类器。
2. **主题分类器已存在且更强。** CLAUDE.md 派单决策树 + agent frontmatter 的 `model` 声明，
   持有完整上下文、用户可直接编辑（§3.2）。代理层从 tools/system 反推角色是把已知信息猜一遍，
   信息量只减不增。
3. **session 粒度否决会话级方案，prefix cache + 工具循环 + 抗抖动状态三重代价压制请求级方案**
   （§2.1 实测 139h/1875 请求/11 活动块/tier 切换 88 次；§2.2）。
4. **数据显示的真实痛点是配额，不是选路。** failover 从 0.28% 升到 1.8%（6.4 倍）、503 占 5.6%、
   87 次 route_failover 全部是 `nation/opus` kimi-k3 429 打满、29 次仍 503（§3.4）。
   主题路由对此**完全无效**。

### 若用户仍要做，推荐路径（明确单一推荐，不摊选项）

**请求级 + 确定性标记（§4.2 + §4.3），且必须先做完两件前置事，任一不通就停：**
1. 沙箱验证"子 agent system prompt 稳定出现在 `system` 字段"（§4.2）——不通则方案作废。
2. 先把 `cache_read_input_tokens` 落进 ACCESS 日志，跑几天，测出"换家族的 cache 损失"到底多大
   （§5.1 风险 2）——净值为负则方案作废。

明确**不推荐**：任何形式的语义分类（关键词表脆弱且需长期维护；额外 LLM 调用 +1.5s、烧配额、
鸡生蛋、且违背本项目零依赖取向）。也**不推荐**会话级（§2.1 已否决）。

### 同等或更高优先级的替代投入（按性价比排序）

1. **把 `route_pool` 填实。** 现在只有一项（`claude`, weight 1），哈希分配贡献 0（§3.3 实测），
   route_failover 也只有一个兜底方向。这是**已落地机制处于未启用状态**——零代码改动，改配置即生效，
   直接缓解 §3.4 的配额痛点。这是当前最高性价比动作。
2. **补齐 `nation/opus` 的供给。** 87 次 route_failover 全部源于此（kimi-k3 双 appkey 均 429），
   29 次最终 503。加 supply 或调 cooldown，直接消掉 5.6% 的 503。
3. **ACCESS 日志补 `cache_read_input_tokens`。** 独立小改动，无论做不做主题路由都值得——它是
   目前唯一无法量化的成本项，补上才能谈"换家族到底省不省钱"。
4. **清理 `session_overrides` 僵尸条目**（3 条命中 0 次，§3.3）。行为无害，纯卫生。
5. **要按主题分家族，先直接改 tier→supply 映射试。** 零代码、热重载生效、可随时回滚。
   若这条路上不去（比如发现"同一 tier 内部还需要按主题分家族"是真需求），再回来看 §4.2 —— 那时
   需求已被实践证实，不是推测。

---

## 验证方式

1. **本文数据可复现**（口径明示，可复核）：
   - 窗口 = `session=` 字段上线后，`2026-07-29 14:47:37 → 2026-08-04 11:48:37`。
   - 总量：`grep -c ACCESS .claude_model_proxy.log` → 4866。
   - route/status/failover 分布：`grep ACCESS | grep -oE 'route=[a-z]*'`（等价字段同法）分组计数。
   - per-session 表（§2.1）、tier 切换率、`usage_in` 分位、延迟分位：按 ACCESS 行正则解析后
     按 `session=` / `(route,tier)` 分组统计，仅 `status=200` 计入 usage/延迟口径。
   - route_failover 归因：`grep 'route_failover=1'` 交叉 `grep 'route_failover: route='` warning 行。
2. **§4.2 前提验证（阻塞项，做之前必须先跑）**：沙箱 `/tmp/model_proxy_sandbox` 端口 18899，
   `env ANTHROPIC_BASE_URL=http://127.0.0.1:18899/ ANTHROPIC_AUTH_TOKEN=cc claude
   --setting-sources project,local -p "<派一个子agent的任务>"`。**必须带 `--setting-sources
   project,local`**，否则 `~/.claude/settings.json` 的全局 `ANTHROPIC_BASE_URL` 优先级更高、
   会误打生产 18889（前置文档 §1 有踩坑记录）。DEBUG 打印入站 `system` 字段，确认子 agent 的
   system prompt 是否原样出现、多轮/compact 后是否稳定、是 str 还是 content-block 数组。
3. **收益前置量化**：ACCESS 加 `cache_read` 字段后跑 ≥3 天，对比 claude 链路与 nation 链路的
   真实缓存命中，算出"换家族的前缀重算成本"。这一步没数前，任何"省成本"结论都是推测。
4. **若实施，回归必查**：(a) `dispatch.type` 缺省/写 `session_hash` 时行为与改动前逐位一致
   （24 个既有测试全绿）；(b) topic 命中时**其余候选仍完整跟随**，人为把首选 route 打挂，确认
   route_failover 仍触发、ACCESS 记 `route_failover=1`；(c) `system` 为 content-block 数组形态时
   标记仍能提取；(d) 无标记请求正确退回哈希且 `topic=` 字段为空。

## 关联

- 现状机制设计：[[2026-07-28-session-route-dispatch-design]]（route_pool / 一致性哈希 / session_overrides / route_failover 选项B）
- 前置调研（已 superseded，数据可参考）：[[2026-07-28-session-load-balancing-feasibility]]
- [[2026-07-23-model-proxy-full-audit]]
- 核心代码：`tools/model_proxy/core/server.py`
  （`extract_session_key` L497 / `extract_route_candidates` L600 / `_MODEL_TIER_MAP` L579 /
  `_forward` L962、body 解析 L978、候选解析 L997、route 候选外层循环 L1044、
  route_failover 收尾 L1411 / ACCESS emit L933）
- schema 校验：`tools/model_proxy/_config_ops.py`（`_validate_strategy_route_fields` L793、
  CLI 不支持编辑 route_pool/dispatch L855）
- 行为契约测试：`tools/model_proxy/tests/test_session_route_dispatch.py`（24 个 case）
- 用户文档：`tools/model_proxy/README.md` §3.4「按 session 分配到多个 route（route_pool）」、§4.4
- 客户端侧既有主题分类器：`/Users/vincentwang/Documents/NoteVault/CLAUDE.md`「派单决策树」+
  `/Users/vincentwang/Documents/NoteVault/.claude/agents/*.md`
- 业界参考：RouteLLM（ICLR 2025，二分类 strong/weak，"decides from the query alone"）、
  vLLM Semantic Router（ModernBERT 多任务意图分类 + Envoy ExtProc，multi-turn 有状态路由仍在 roadmap）
