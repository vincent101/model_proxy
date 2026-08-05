---
type: design-decision
status: draft
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, session-routing, in-band-command, topic-routing]
---

# model_proxy 会话内指令切换 route（in-band control command）设计

> [理想] 路径产出：先问「架构上正确的做法是什么」，再评估落地代价。
> 本文只做设计，不含实现代码，未改动任何实现文件。
>
> 前置：[[2026-08-04-topic-based-route-dispatch-feasibility]]（上一轮「按主题自动路由」调研，结论不建议做）、
> [[2026-07-28-session-route-dispatch-design]]（现状 route_pool / session_overrides / 一致性哈希机制）、
> [[2026-07-28-session-load-balancing-feasibility]]（更早的可行性调研，已 superseded）。

## 背景：本方案与上一轮调研的关系

上一轮结论是**不建议做「按主题自动路由」**，核心理由：主题识别是**归类判断**（有准确率、有漂移、需维护类目体系），而代理层拿到的信息比客户端 agent 体系更少，等于用信息更少的第二个分类器去覆盖信息更全的第一个。

本方案换了一条路：**不猜主题，让用户直接说。**

| | 上一轮：主题自动路由 | 本方案：显式指令 |
|---|---|---|
| 识别性质 | 语义归类，概率性 | 字符串精确匹配，确定性 |
| 准确率 | 有错判概念，需维护规则/模型 | 无错判概念（匹配或不匹配） |
| 类目体系 | 需定义并长期维护 | 不需要，用户自己说 route id |
| 落点数据结构 | 需新设计 | **已存在**（`dispatch.session_overrides`） |
| 与会话粘性冲突 | 会话级则「错一次错到底」，请求级则破坏 cache | 用户主动切，切换点由用户掌握 |

**关键判断：本方案不是新建机制，而是把用户当前的手工动作自动化。** 现网 `cc` 的 `session_overrides` 有 5 条手写条目指向 `nation`，用户现在的操作流程是「翻 ACCESS 日志抄 session_id → 粘进 config → 存盘」。本方案要做的就是把这三步变成在对话里打一行指令。这是本方案价值成立的根本依据，也界定了它的价值上限（见 §7）。

---

## 1. 语法选型：`!` 与 `#` 均被证伪，最终定 `$`

### 1.0 首要约束：必须在**所有在用客户端**上验证可达

本节结论经历两次推翻，根因是同一个方法错误：**只验证一个客户端就下结论**。用户实际同时使用两个客户端——**Claude Code CLI** 与 **Claudian**（Obsidian 插件）——各有独立的输入拦截层，吞掉的前缀不同。

> **通用约束（实现与日后改语法都须遵守）**：任何 in-band 指令前缀，必须在**全部在用客户端**上验证「字符能原样进入输入框、消息能到达 API」。任一客户端拦截即该前缀作废。客户端升级可能新增拦截，故此项属**持续性外部依赖**，需在 README 标注。

### 1.1 `!` 被证伪（两个客户端都拦）

- **CLI**：扫描 `~/.claude/projects/-Users-vincentwang-Documents-NoteVault/**/*.jsonl`，**6038 条真实 user 消息中 `!` 开头 0 条**。反编译 CC 二进制（`/opt/homebrew/Caskroom/claude-code/2.1.197/claude`）确认存在 `startsWith("!")?"bash":"prompt"` 判定：`!` 被截为 **bash 模式**当 shell 命令执行，**永不到达 API**。同次确认 CC 只有 `"bash"`/`"prompt"` 两种输入模式（无 memory 模式）。
- **Claudian**：`main.js` 内 `BangBashModeManager`，`handleTriggerKey` 为 `inputEl.value === "" && e.key === "!"` → `preventDefault()`。同样截为 bash 模式。

### 1.2 `#` 被证伪（Claudian 拦截，这是上一版的漏检）

上一版基于「CLI 实测 228 条 `#` 开头消息到达 API」推荐 `#route`。**该推荐错误**——漏检了 Claudian。

Claudian `main.js` 内 `InstructionModeManager`（**Instruction Mode**，用户可见占位提示 `"# Save in custom system prompt"`）：

```js
handleTriggerKey(e2) {
  if (!this.state.active && this.inputEl.value === "" && e2.key === "#") {
    if (this.enterMode()) { e2.preventDefault(); return true; }
  }
}
```

`preventDefault()` 使 **`#` 字符根本不进入输入框**；后续 Enter 走 `submit()` 进指令精炼流程，最终经 `appendMarkdownSnippet` 追加到 `plugin.settings.systemPrompt`（Notice 文案 `"Instruction added to custom system prompt"`），**不发普通对话请求**。

即在 Claudian 输入 `#route nation` 的实际结果是：`#` 被吞 → 进入 Instruction Mode → `route nation` 被当作「要写进 system prompt 的指令」。代理永远收不到。

### 1.3 客户端拦截矩阵（实测）

| 前缀 | Claude Code CLI | Claudian | 历史碰撞（6038 条） | 结论 |
|---|---|---|---|---|
| `!` | ✗ bash 模式 | ✗ `BangBashModeManager` | 0 | 双废 |
| `#` | ✓ 可达（228 条到达） | ✗ `InstructionModeManager` | 228（单行 1） | **双废** |
| `@` | ✗ 文件引用语义，客户端做路径补全 | — | 36（单行 20） | 语义冲突 |
| `/` | ✗ slash command | — | 1（恰是文件路径） | 语义冲突 |
| **`$`** | **✓ 无拦截** | **✓ 无拦截** | **0** | **✓ 采用** |

`$` 的验证明细（三项全过）：
1. **Claudian 无拦截**：`main.js` 内 `key === "$"` 出现 **0 次**；全文件仅 `BangBashModeManager`/`InstructionModeManager` 两个输入模式拦截器，分别只管 `!`/`#`。
2. **CLI 无拦截**：反编译二进制中无 `$` 的输入模式判定。唯一命中 `startsWith("$")) return "variable"` 属**已进入 bash 模式后**的补全类型判定（区分 `$VAR` 环境变量 / 路径 / 命令，由同段 `_yf`/`yyf` 函数上下文可证），不影响普通 prompt 输入。
3. **历史零碰撞**：6038 条真实 user 消息中 `$` 开头 **0 条**（对比 `#` 的 228 条）。

附带优势：`$` 在命令行语境有「提示符/执行」的语义联想，作指令前缀直观。

### 1.4 最终语法

```
$route <id>     切换当前 session 到 <id>，下一条消息起生效
$route          查询当前 session 生效的 route 及其来源
$route reset    清除 override，落回自动哈希分配
```

**匹配规则（严格定义，避免实现时放宽）**：
1. 取最后一条 user 消息的纯文本（见 §2.2），`strip()` 后
2. 必须为**单行**（`"\n" not in text`）
3. 用 `text.split()` 分词后，`tokens[0]` 必须**精确等于** `$route`（大小写敏感，不做 lower）
4. `len(tokens) <= 2`；`tokens[1]`（若有）为参数
5. 以上任一不满足 → **不是指令，照常转发上游**（fail-open，绝不因误判吞掉用户真实消息）

第 5 点是本方案最重要的安全属性：**判定偏保守，宁可漏识别（用户重打一次）也不能误识别（吞掉真实消息并返回假响应）。** 在 `$` 前缀 + 历史零碰撞的前提下，规则 2-4 的三重约束使误触发概率实质为零。

---

## 2. 指令识别：位置与边界

### 2.1 拦截点

`_forward` 内现成的执行顺序（`core/server.py`）：

```
980  body_json = json.loads(raw_body)
984  self._acc["session"] = extract_session_key(body_json) or ""
987  source = detect_source(self.path, body_json)
     ← ★ 拦截点插在这里
997  route_candidates = extract_route_candidates(strategy, session_key, routes_map)
```

**插入位置：987 与 997 之间。** 此时 `body_json` 已解析、`session_key` 已提取（`extract_session_key`，`server.py:497`，二次 `json.loads` 取 `metadata.user_id.session_id`）、`source` 已判定，而 route 选择尚未开始——正是需要的全部前提条件都已就绪、且还没产生任何副作用的时刻。

**门控条件（全部满足才进入识别）**：
- `source == "anthropic"`（排除 codex/responses 与 count_tokens，见 §2.3）
- `session_key` 非空（没有 session_key 就无处落 override，此时应 fail-open 转发）
- `body_json` 是 dict 且含 `messages` 列表

### 2.2 只取最后一条 user 消息

**必须只看 `messages` 数组中最后一个 `role == "user"` 的元素。** 理由：CC 每轮请求携带完整历史，若全量扫描，会话里一旦出现过指令，后续每轮都会重复命中——更糟的是，用户之后若切到别的 route，历史里的旧指令会反复覆盖新决定，造成「切不动」的诡异现象。

`content` 有两种形态，需都正确处理：
- `str`：直接用
- `list`（content blocks）：只取 `type == "text"` 的块的 `text` 字段拼接；**忽略 `tool_result`、`image` 等其他块类型**

> `core/translate.py` 内已有 content blocks 取文本的既有处理逻辑，实现时应复用同一套取文本口径，不要另写一份（避免两处口径漂移）。

### 2.3 必须排除的误触发面

| 面 | 处理 | 依据 |
|---|---|---|
| 指令出现在 assistant / system / tool_result 里 | 只看最后一条 **role=user**，天然排除 | §2.2 定位规则 |
| **count_tokens 请求** | `source != "anthropic"` 门控天然排除 | 见下方专项 |
| 子 agent 派生请求 | 子 agent 复用父会话 session_id（已实测），其 messages 可能含父会话内容；但「只看最后一条 user」使父会话历史里的旧指令不会被重复命中 | §2.2 + 前置文档 §1 实测 |
| 用户在代码块/引用里写指令 | 「单行」约束基本规避（代码块至少含围栏行，多行）；残余风险见下 |  |

**count_tokens 专项（本轮新发现，影响匹配规则）**：

CC 会发 `/v1/messages/count_tokens` 请求，**携带完整对话历史**。该路径尾缀不匹配 `detect_source`（`server.py:523`）对 `/v1/messages` 的精确判定，body 又无 `max_tokens`/`system` 特征，故落入 `source == "chat"` → `pick_translator` 返回 UNSUPPORTED → **501**。

日志实证（`.claude_model_proxy.log`）：`source=chat` 共 100 条 = **92 条 501 + 8 条 503**；`source=anthropic` 2987 条。

设计含义有两层：
1. 这类请求带全量历史但不是真实用户轮次，**若做全量扫描会被它重复触发**；`source == "anthropic"` 门控已排除它。
2. 顺带暴露一个既有问题：**这 92 次 501 是 CC 的正常 token 计数行为被代理拒绝**。与本方案无关，但值得单独修（让 count_tokens 走通或显式静默），已列入 §9 顺带发现。

**残余风险（诚实标注）**：用户若真的单独发一行 `$route nation` 只是想「讨论这个语法」而非执行，会被执行。缓解：切换本身可逆（`$route reset`）、有明确回执、且这是用户主动打出的确定字符串——相比自然语言匹配的误切风险，这已是可接受的最小面。**本文档讨论中反复出现的该字样均为多行上下文内的一部分，不会触发。**

> ⚠️ **未验证项**：CC 可能把 `<system-reminder>` 块追加进 user 消息内容。若追加发生在**同一个 text 块内**且带换行，「单行」约束会使指令**永不匹配**（方案失效，非误触发）；若追加为**独立 block**，则 §2.2 的「只取 text 块拼接」需改为「只取第一个 text 块」或做 system-reminder 剥离。**这是必须实测的项，见 §8 V2。**

---

## 3. 拦截后合成响应

### 3.1 可直接复用既有构件（无需手写 SSE）

`core/translate.py` 已有完整的 anthropic SSE 构件，**自造响应应直接复用，不要另写事件字典**：

| 构件 | 位置 | 用途 |
|---|---|---|
| `anthropic_sse_bytes(event)` | `translate.py:129`，返回 `f"event: {etype}\ndata: {data}\n\n".encode()` | 事件 → SSE 字节，唯一序列化口径 |
| `gen_msg_id()` | `translate.py:83` | 生成 `message.id` |
| `AnthropicStreamAdapter._message_start_event()` 等 helper | `translate.py:609-684` | 各事件构造，字段已对齐真实上游 |

`samples/anthropic_stream_samples.txt` 有真实事件样本可作回归比对基准。

### 3.2 流式（stream=true，CC 默认）事件序列

按 `AnthropicStreamAdapter` 既有产出顺序（`translate.py:705-812`）：

```
message_start          ← usage.output_tokens=0，input/cache 各字段按 3.3
ping                   ← 适配器在 message_start 后紧跟一个 ping，保持一致
content_block_start    ← {"type":"text","text":""}，index=0
content_block_delta    ← {"type":"text_delta","text":"<回执文案>"}
content_block_stop     ← index=0
message_delta          ← delta.stop_reason="end_turn"，usage.output_tokens=<回执token>
message_stop
```

响应头按 `_send_stream_headers`（`server.py:1567` 附近）既有口径：`200` + `Content-Type: text/event-stream` + chunked。

### 3.3 usage 与计量（需明确决策）

代理有 `.claude_model_proxy_totals.json` 账本。自造响应**没有真实上游消耗**，若按普通请求计入会污染用量统计（虚增请求数、且 supply 维度无处归属——本请求根本没选 supply）。

**建议**：
- `usage.input_tokens` / `output_tokens` 填 **0**，`cache_*` 填 0。理由：如实反映「零上游消耗」，且 CC 客户端侧只用它显示用量，填 0 不影响功能。
- **不计入 totals 账本**，或计入一个独立的 `builtin_command` 维度与真实流量隔离。倾向后者：可观测「这功能被用了多少次」，又不污染成本统计。
- ACCESS 日志**要记**，加可辨识字段。与现有 `failover=` / `route_failover=` 同风格，建议 `builtin=route`（值为命令名）；`supply=` 留空或记 `(builtin)`，`route=` 记切换的目标 route 以便核对。

> `usage` 填 0 是否会让 CC 客户端的 context 用量显示异常（如进度条跳变），**未验证**，需实测观察，见 §8 V3。

### 3.4 非流式（stream=false）

返回标准 anthropic messages 响应体：`{"id","type":"message","role":"assistant","model","content":[{"type":"text","text":"<回执>"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{...}}`。`model` 回填客户端请求的 model 字面值。

### 3.5 回执文案（用户可见，需信息充分）

| 场景 | 文案要素 |
|---|---|
| 切换成功 | 目标 route、**下一条消息起生效**、当前 session 短标识、如何撤销（`$route reset`） |
| 查询（`$route`） | 当前生效 route、来源（手动 override / 自动哈希 / 默认）、可用 route id 列表 |
| route 不存在 | 明确报错 + **列出全部可用 route id**（避免用户猜） |
| 写盘失败 | 明确「切换未生效」+ 原因摘要（见 §4.4） |

---

## 4. 写回配置：并发与安全（正确性关键）

**这是本方案唯一的架构性变化：代理进程首次获得配置写权限。** 现状 `core/server.py` 无任何写盘调用，写只发生在 `_config_ops.py` / `model_proxy_cli.sh`。

### 4.1 已核实的现状约束

- `_config_ops.py:42` 有 `atomic_write(path, cfg)`：`tempfile.mkstemp` 同目录建临时文件 → `json.dump(indent=2, ensure_ascii=False)` → `os.replace(tmp, path)` → `chmod 0o600`。**原子替换已具备。**
- **全项目 grep 无 `flock` / `fcntl` / 任何文件锁。** CLI 与手工编辑之间本就没有互斥，只是单人低频使用没撞上。
- 项目**零第三方依赖、纯 stdlib**（无 requirements.txt / pyproject.toml）——只能用 `fcntl`（stdlib，macOS/Linux 可用）。

### 4.2 别名风险（必须正面处理）

`ConfigStore.get_strategies()`（`server.py:332`）实现是：

```python
with self._lock:
    return list(self._config.get("strategies", []))
```

**这是浅拷贝**：外层 list 是新的，但**里面每个 strategy dict 仍是 ConfigStore 内部对象的引用**。

因此 `strategy["dispatch"]["session_overrides"][sid] = rid` 这种就地写会**直接改到 ConfigStore 的内存配置**，产生三个问题：
1. 绕过热重载语义——内存状态与磁盘不一致，且下次 `maybe_reload` 才会被磁盘覆盖，中间窗口行为不可预测
2. 无锁并发写 dict（HTTP server 多线程），有竞态
3. 若后续写盘失败，内存已被改脏，无法回滚

**处置：写路径必须 `copy.deepcopy` 后再改，绝不就地修改 getter 返回的对象。** 这一点必须在实现时显式验证（见 §8 V5）。

### 4.3 与自身热重载的交互（是否成环）

代理写盘 → 改 mtime → 自己的 `maybe_reload`（`server.py:349`，mtime 比对 + 双重检查 + 失败保留旧配置）会检测到变化并重载。

**不成环**：重载是「读磁盘覆盖内存」，是幂等收敛动作，不会再触发写。写盘只由用户指令触发，不由重载触发。

但有一个真实副作用：**重载会丢弃内存中未落盘的状态。** 当前 `CooldownStore`（纯内存、不落盘）**不在 config 内**，所以不受重载影响——已核实其为独立对象。故本方案的写盘不会冲掉冷却状态。**但这构成一条约束：日后若有其他内存状态被并入 config 结构，本方案的写盘会成为它的丢失来源。** 应在代码注释中标注此约束。

### 4.4 写盘失败的降级

失败来源：磁盘满、权限、**config 被外部改成非法 JSON**（读改写的「读」阶段就失败）。

**处置：切换失败 + 明确告知，不做「降级为仅内存生效」。** 理由：内存生效但磁盘没有，会造成「重启后静默失效」——用户以为切了、实际没切，是最坏的一类不一致。宁可当场失败让用户知道。

回执文案需含失败原因摘要（不泄露完整路径/敏感内容）。

### 4.5 主 config vs 独立 sidecar：**建议 sidecar**

用户决策是「状态持久化到配置文件的 `session_overrides` 语义」。基于 §4.1-4.4 的分析，**我建议在同一决策下采用 sidecar 实现形态**，并说明这不构成推翻用户决策：

| 维度 | 写主 config | **写 sidecar（建议）** |
|---|---|---|
| 与 CLI/手工编辑并发写 | **真实冲突**：CLI 读改写窗口内代理写盘 → 互相覆盖丢失。现状无锁，需新引入 `fcntl.flock` 并**同时改造 CLI 侧**才安全 | **消除**：代理独占写 sidecar，主 config 保持 CLI/人工领域，两者无交集 |
| 写失败影响面 | 主 config 是全部路由配置的唯一来源，写坏影响整个代理 | 只影响 override，主 config 完好，最坏退化为「切换失效」 |
| 热重载 | 复用现有 | 需给 sidecar 加一份 mtime 监听（小量新增） |
| 用户可见可手改 | ✓ | ✓ 同样是 JSON 文件，可读可手改 |
| 与既有 schema 关系 | 原地扩展 | 需定义「sidecar 的 override 与 config 内 override 的合并优先级」 |

**核心论据：并发写主 config 需要改造 CLI 侧才能安全，而 sidecar 只需代理侧自洽。** 用户决策的实质要求是「持久化、可见、可手改」，sidecar 全部满足。

**若采用 sidecar，需定义合并语义**（建议）：读取时 `config 内 session_overrides`（人工/CLI 领域，视为「基线」）与 `sidecar`（代理自动写入）**合并，sidecar 优先**——因为 sidecar 代表用户最近一次的显式指令。同 key 冲突时 sidecar 覆盖，并在 ACCESS 日志可辨识来源。

**最终由用户拍板**，列入 §10 开放问题。

---

## 5. 语义与生命周期

### 5.1 生效时机

当前请求已被拦截、不打上游，故**「下一条消息起生效」是唯一可能语义**。回执必须写明这点，否则用户会误以为当前这轮就已经在新 route 上（当前这轮根本没打上游，无所谓在哪个 route）。

### 5.2 命令集

| 命令 | 语义 | 读写 |
|---|---|---|
| `$route <id>` | 把当前 session 钉到 `<id>` | 写 |
| `$route` | 查询当前 session 生效的 route 及其来源 | **纯读** |
| `$route reset` | 删除当前 session 的 override，落回自动哈希分配 | 写 |

**`$route`（查询）强烈建议纳入**：纯读、零风险、实现成本最低，而可用性收益很大——用户**当前完全看不到自己被分到哪个 route**（这也是上一轮调研发现「nation 流量 100% 来自手工 override、哈希实际未产生分流」时用户难以自查的原因）。

### 5.3 合法性校验

- `<id>` 必须存在于**顶层 `routes`**；不存在则报错并列出全部可用 id。
- **保持「允许切到 route_pool 之外」的既有语义**（前置设计 §4b 明确这是有意的「例外指定」）：现网 `cc` 的 route_pool 只有 `claude` 一项，而 5 条手工 override 全部指向 `nation`——**若强制要求 target 在 route_pool 内，现网配置立刻失效**。这条兼容性不可破。

### 5.4 僵尸条目

现网已有命中 0 次的僵尸 override（上一轮调研发现 3 条）。自动写入会加速积累。

原设计**明确拒绝 TTL/自动过期**（前置文档 §4b：「加 TTL 需要落盘时间戳 + 定期清理 + 时钟管理，是过度设计」）。**尊重该取向**，建议替代做法：
- 写入时**记录时间戳与最后命中时间**（仅记录，不据此自动删除）——这是为人工识别提供依据，不是 TTL
- 提供 CLI 清理命令（`prune-overrides`，列出久未命中条目供人工确认删除）
- sidecar 方案下清理更安全（不碰主 config）

---

## 6. 与现有机制的关系（逐项确认）

| 机制 | 关系 | 冲突 |
|---|---|---|
| 手工 `session_overrides` | 同一张表/同一语义的自动写入，优先级不变（override > 哈希） | 无 |
| 一致性哈希 `session_hash` | override 优先级仍最高，哈希作未命中兜底，算法不动 | 无 |
| **`route_failover`（跨 route 兜底）** | **本方案只影响「首选谁」，不改变候选列表结构** | 见下方专项 |
| `select_supply` / `CooldownStore`（route 内 supply 级 failover） | 完全不接触，route 选定后全链路不动 | 无 |
| codex strategy（responses 协议） | 建议只对 `source == "anthropic"` 生效 | 见下方专项 |

**`route_failover` 专项（防止重犯已知坑）**：`extract_route_candidates`（`server.py:600`）返回的是**有序候选列表**，`_forward` 外层循环依赖它做跨 route 兜底。上一轮文档已明确指出「只产出单个 route 会静默打断 route_failover」这个陷阱。

本方案的写入落在 `session_overrides`，而 `extract_route_candidates` 对 override 的既有处理是：**命中的 route 放第一位，其余候选按哈希顺序跟在后面作兜底**（前置设计 §4b + `server.py` 内 override 分支实现）。**因此本方案天然沿用既有结构，不新增「只返回单个 route」的代码路径。** 实现时不得为了「切换要绝对生效」而改成只返回目标 route。

**codex 侧**：语法、响应格式（responses 协议 SSE）、拦截点全部不同，成本约翻倍，而 codex 流量占比极低。**建议首版只支持 anthropic**；codex 客户端发 `$route` 会被当普通文本转发上游（fail-open，无害）。列入开放问题。

---

## 7. 与 PASSTHROUGH 哲学的关系：是否该做成通用内建命令层

这是代理**首次「截流自答」**，偏离了「只做协议转换与转发」的纯代理定位。[理想] 路径下值得认真展开。

### 7.1 定位变化是实质性的

现状 PASSTHROUGH（anthropic→anthropic，占 2987/3087 ≈ 97% 流量）几乎不解析语义，只做协议透传。本方案要求：读 body → 匹配用户文本 → 可能不转发 → 自己生成响应。这引入了一个**新的职责类别**：代理成为一个可被对话直接寻址的控制面。

### 7.2 两种做法

| | 硬编码单指令 | **通用 in-band command 层** |
|---|---|---|
| 首版成本 | 更低 | 略高（需定义注册点、分发、统一回执构造） |
| 第二个命令的边际成本 | 每个都要重新接一遍拦截/响应合成 | 只需注册一个 handler |
| 架构清晰度 | 控制逻辑散在 `_forward` 里 | 控制面与转发面**显式分层**，`_forward` 只做一次「是否内建命令」判定后即分流 |
| 风险 | 口子小 | **口子更大**：鼓励后续往代理里塞更多命令，可能长成一个四不像的 mini-shell |

### 7.3 判断

**架构上，通用命令层更正确**，理由：一旦承认「代理有控制面」这件事（本方案已经承认了），把它**显式收敛成一层**比散落在转发路径里更可控——后者会让 `_forward` 这个已经很长的函数继续膨胀，且每个新命令都可能各自发明一套响应合成。

**但必须配一条明确的边界约束，否则口子会失控**。建议写进设计并落到代码注释：

> 内建命令层**只允许**操作「代理自身的路由/观测状态」，且**只允许**纯本地操作。禁止：执行外部命令、读写代理配置以外的文件、代理请求转发、任何需要网络的动作。

在此约束下，可预见的合理命令集是收敛的（`$route` 切换/查询、`$status` 看当前 supply/冷却状态、`$tier` 看档位映射），不会长成 shell。

`$status` 类命令的可用性收益其实不小——**现状用户要了解代理状态只能翻日志**。

**推荐：做成命令层，但首版只实现 `$route`（含查询）**，其余命令等有实际需求再加。这与前置设计文档在 schema 上的既有取向一致（「先按理想预留结构、首版只实现一种」）。列入开放问题由用户拍板。

---

## 8. 前置验证项（V1 是地基，不通则方案作废）

沙箱方法（复用前置文档已记录、已验证的口径）：

```bash
env ANTHROPIC_BASE_URL="http://127.0.0.1:18899/" ANTHROPIC_AUTH_TOKEN="cc" \
  claude --setting-sources project,local -p "<prompt>" --session-id "<uuid>"
```

> **必须带 `--setting-sources project,local`**（排除 `user` 来源），否则 `~/.claude/settings.json` 的全局 `env.ANTHROPIC_BASE_URL` 优先级更高，会覆盖 shell 临时变量导致**误打生产**（前置文档记录过一次真实误打）。沙箱 `/tmp/model_proxy_sandbox` 端口 18899，生产 18889，完全隔离。

**已完成的静态验证（§1.3，无需重跑）**：`$` 在 Claudian（`main.js` 无 `key === "$"` 拦截）与 CLI（反编译无 `$` 输入模式判定）双侧均无拦截；6038 条历史消息 `$` 开头 0 条。以下 V1 是对该静态结论的**端到端确认**，仍需实跑。

| # | 验证项 | 方法 | 不通的后果 |
|---|---|---|---|
| **V1** | **`$route nation` 在两个客户端都能原样到达 API** | 沙箱起只 dump body 的 stub；**CLI 与 Claudian 各发一次**，检查 `messages[-1]` 文本是否精确等于输入 | **方案作废**（通道不存在），需另找可达前缀。**必须两个客户端都测**——这正是 `#` 方案翻车的原因（见 §1.0/§1.2） |
| **V2** | user 消息是否被追加 `<system-reminder>` | 同 V1，检查 content 形态：是否多 block、text 内是否含换行 | 「单行」约束需调整（否则指令永不匹配） |
| V3 | usage 填 0 是否让客户端显示异常 | 自造响应后观察客户端用量显示 | 改为填合理估算值 |
| V4 | 自造 SSE 序列客户端能否正常消费 | 对比 `samples/anthropic_stream_samples.txt`，观察客户端是否卡住/报错；**Claudian 与 CLI 各验一次**（两者 SSE 消费实现不同） | 补齐缺失事件 |
| V5 | 写路径无别名污染 | 单测：写入后断言 `ConfigStore._config` 未被就地改动（deepcopy 生效） | 修实现（§4.2） |
| V6 | 并发写不丢失 | 并行跑「代理写 override」+「CLI 改配置」，校验两者都不丢 | 落 sidecar 或加锁（§4.5） |
| V7 | 兼容性回归 | 现网 `cc`/`codex` 配置不变时行为完全一致；不含指令的消息一律照常转发 | 阻断上线 |
| V8 | 生效语义 | 切换后下一条请求的 ACCESS `route=` 变为目标 route；`$route reset` 后落回哈希 | — |
| V9 | fail-open | 构造各种「像指令但不完全匹配」的消息（多行、大小写不同、三个 token、`$route` 出现在句中而非行首），确认全部正常转发不被吞 | 收紧匹配（§1.4 规则 5） |

---

## 9. 改动量与耦合面

| 文件/模块 | 改动 |
|---|---|
| `core/server.py` | 新增指令解析（取最后一条 user 文本 + 严格匹配）、命令 handler（切换/查询/reset）、自造响应写回（流式复用 `anthropic_sse_bytes` + `AnthropicStreamAdapter` helper；非流式构造 JSON）、override 写入（**deepcopy 后改，见 §4.2**）；`_forward` 在 987↔997 之间插入一次分流判定；ACCESS 加 `builtin=` 字段 |
| `_config_ops.py` | 若走 sidecar：新增 sidecar 读写与合并；若走主 config：`atomic_write` 加 `fcntl.flock` 并**同步改造 CLI 侧**取锁 |
| `ConfigStore`（`core/server.py`） | 若走 sidecar：加一份 mtime 监听与合并逻辑 |
| `model_proxy_cli.sh` | 主 config 方案下需加锁；建议补 `prune-overrides` 清理命令 |
| `README.md` | 补内建命令章节、语法、生效语义、可用 route 查询方式 |
| `tests/` | 匹配规则单测（含 §8 V9 的 fail-open 反例集）、别名污染单测（V5）、SSE 序列断言（V4） |

**耦合性质**：**有正确性耦合**。三处敏感点：
1. 匹配规则放宽会**吞掉用户真实消息并返回假响应**（最坏后果，需 fail-open 反例集覆盖）
2. 写路径的**别名污染**会绕过热重载语义、且失败无法回滚（§4.2）
3. 不得为「切换绝对生效」而破坏 `extract_route_candidates` 的有序候选列表结构，否则**静默打断 route_failover**（§6）

**建议：派 implementer 落地 + reviewer 复核。** 不适合 runner。

### 顺带发现（与本方案独立，建议单独处理）

**92 次 count_tokens 被 501 拒绝**（§2.3）：CC 的正常 token 计数请求打到 `/v1/messages/count_tokens`，因 `detect_source` 路径尾缀精确匹配 + body 无 anthropic 特征而落入 `source=chat` → UNSUPPORTED → 501。这是既有缺陷，与本方案无关，但既然查明了应记录：要么让该端点走通（转发或本地估算），要么显式静默处理，不要继续以 501 噪音形式存在。

---

## 10. 开放问题（需用户拍板，不替选）

> 语法选型**已关闭**：用户拍板 `$`，且已通过双客户端拦截检查与历史碰撞检查（§1.3）。剩余开放项如下。

1. **主 config vs sidecar**（§4.5）：我**建议 sidecar**（代理独占写，消除与 CLI 的并发写冲突，且不必改造 CLI 侧）。仍满足「持久化、可见、可手改」。请确认。
2. **是否做成通用内建命令层**（§7）：我**建议做层、首版只实现 `$route`**，并接受「只操作代理自身路由/观测状态、纯本地」的边界约束。
3. **是否支持 codex 侧**：建议首版不支持（成本翻倍、流量极低，且 fail-open 无害）。
4. **僵尸条目治理力度**（§5.4）：只记时间戳供人工识别 / 另加 CLI 清理命令 / 都不做。原设计拒绝 TTL 的取向保持不变。

---

## 11. 结论

**方案成立，且明显优于上一轮的「主题自动路由」**——把概率性的语义分类换成确定性的字符串匹配，落点复用已存在的 `session_overrides` 数据结构，本质是把用户当前的手工三步操作（翻日志抄 id、粘配置、存盘）自动化。

**语法已定为 `$route`，地基已验**：前两个候选均被证伪——`!` 在 CLI 与 Claudian 都被截为 bash 模式；`#` 虽在 CLI 可达（228 条实测），但被 Claudian 的 Instruction Mode 吞掉。`$` 通过三项检查：Claudian 无拦截、CLI 无拦截、6038 条历史消息零碰撞（§1.3）。仍需 V1 做端到端确认。

**这一过程留下的最重要教训已写进 §1.0**：in-band 语法的可达性必须在**全部在用客户端**上验证。只验证一个客户端就下结论，是 `#` 方案翻车的直接原因。

**主要风险三条**：
1. **前缀可达性依赖客户端行为**，属外部依赖，可能随任一客户端升级新增拦截而失效（V1/V2 + §1.0 的持续性约束）
2. **误识别会吞掉用户真实消息**并返回假响应——靠「单行 + 严格 token 匹配 + fail-open」三重约束压制，需反例集覆盖（V9）；`$` 的历史零碰撞使该风险实质为零
3. **代理首次获得配置写权限**是架构性变化，现状无任何文件锁；建议用 sidecar 规避（§4.5）

**价值上限要诚实**：它自动化的是「抄 id 粘配置」这个动作，而上一轮调研已指出这是投入产出比最不成立的一点。**真正值得同时做的是 `$route`（查询）**——用户当前完全看不到自己被分到哪个 route，这个纯读、零风险的命令可用性收益可能比切换本身更高。另外，上一轮指出的更高性价比投入（把 `route_pool` 填实、补 `nation/opus` 供给消掉 503、ACCESS 补 cache 字段）依然成立，本方案不替代它们。
