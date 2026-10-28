---
type: design-decision
status: draft
target: tools/model_proxy
tags: [architect, model_proxy, reasoning, failover]
---

# thinking 块空签名剥离 + supply 级自适应学习（务实）

## 背景与问题

混合路由池下 mcli（不验签、产出空签名 thinking 块）与 aigc（对历史 assistant thinking 块验签）同池服务 sonnet tier：mcli 服务过的轮次在客户端历史留下空签名 thinking 块，之后每条请求原样重发打 aigc 必 400（`Invalid \`signature\` in \`thinking\` block`）→ cooldown+failover 白耗一次尝试。已实证：只剥空签名块后打 aigc 200；aigc 自签块不受连带影响；多轮累积下持续有效；删签名键/伪造签名均不可行，必须整块删除。

需求：① anthropic→anthropic PASSTHROUGH 出站前剥离空签名 thinking 块；② 不依赖手工 per-supply 配置，复用 `SyntaxPreferenceStore` + `interpret_rejection` 自适应先例，从 400 文本自动学习；③ fail-open。

## 方案设计

### 0. 总体形态

三层，全部复用先例模式：

1. **纯函数层**（新模块 `core/thinking_guard.py`）：剥离函数 + 400 文本分类器 + 学习存储，零 server 依赖，便于单测。
2. **接线层**（`core/server.py`）：PASSTHROUGH 出站前剥离 + HTTPError 400 重试触发 + run() 装配 + ACCESS 观测（count_tokens 原防御性同款已删，见 F 点订正）。
3. **测试层**：纯函数单测 + 假上游驱动的重试链路集成测。

### 1. 新模块 `core/thinking_guard.py`（新建，约 90 行含注释）

```python
def strip_empty_signature_thinking(body_json: dict) -> int:
    """删除 assistant 历史消息中空签名/缺失签名的 thinking 块（整块），返回删除块数。

    判据：type == "thinking" 且 not block.get("signature")（None 或 ""）。
    仅命中 mcli 产块形态；aigc 自签块 signature 非空不命中。
    redacted_thinking 不处理（见风险R4）。幂等：重复调用删除 0 块。
    """
    msgs = body_json.get("messages") if isinstance(body_json, dict) else None
    if not isinstance(msgs, list):
        return 0
    removed = 0
    for msg in msgs:
        if not (isinstance(msg, dict) and msg.get("role") == "assistant"):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        kept = [b for b in content
                if not (isinstance(b, dict) and b.get("type") == "thinking"
                        and not b.get("signature"))]
        removed += len(content) - len(kept)
        if len(kept) != len(content):
            msg["content"] = kept
    return removed


def is_invalid_signature_rejection(resp_body: bytes) -> bool:
    """识别 aigc 的 thinking 签名 400。匹配策略：lower + 去反引号后
    contains "invalid signature in thinking block"（覆盖带/不带反引号两种报文形态）。
    刻意精确：schema 层 Field required: signature 类报文不命中（fail-open，走现状路径）。
    """
    try:
        t = resp_body.decode("utf-8", errors="replace").lower().replace("`", "")
    except Exception:
        return False
    return "invalid signature in thinking block" in t


class ThinkingStripStore:
    """supply_id → {"at": float}，TTL 48h（对齐 SyntaxPreferenceStore 先例）。
    学到的值恒为"需要剥离"，无需 variant 字段；snapshot() 返回 bool。
    线程安全（Lock），内存态，重启丢失可接受（先例如此，重启后首请求吃一次 400
    再learn，自愈）。"""
```

**粒度决策：supply_id 级，不用 target_model 级。** 理由：验签行为是上游 endpoint 部署属性而非模型属性；supply_id 是循环内现成变量、语义上是"该部署需要什么出站整形"的正确锚点。config 事实（复核订正）：仅 claude 系 supply↔target_model 一一对应；glm aigc 簇六 supply（glm-53-sankuai-{2330,3339,3672,4200,8101,9907}）共用同 URL 同模型 glm-5.3，ds-flash aigc 簇七 supply 两模型——多 supply 簇上 supply 粒度冷启动代价按 supply 数放大（glm 簇若需剥离则 6 次 400+重试才全 learn；ds-flash 簇 7 次）。target_model/URL 粒度可摊薄此代价，但引入"key 是 model 还是 supply"的归属歧义与 learn 时的第二套查表，务实档不为此加抽象；多 supply 簇当前均为非 claude 模型（glm/deepseek），是否触发签名 400 未实证，该代价目前是假设值。

### 2. server.py 接线（行级定位，锚点为当前文件行号）

| # | 位置 | 改动 |
|---|---|---|
| A | ~line 49（`from . import translate as pt` 后） | `from .thinking_guard import ThinkingStripStore, is_invalid_signature_rejection, strip_empty_signature_thinking` |
| B | ~line 1988（`pref_store: ... = self.server.pref_store` 后） | `thinking_strip_store: ThinkingStripStore = self.server.thinking_strip_store` |
| C | ~line 2102（`_reasoning_retried = False` 旁） | `_thinking_strip_retried = False`（请求周期作用域，与语法重试 flag 独立互不消耗） |
| D | ~line 2298（PASSTHROUGH 分支，model 改写块之后、reasoning merge 之前） | 见下方代码形态 |
| E | ~line 2440（HTTPError 内，**置于既有 reasoning 重试 if 块之前**） | 签名 400 检测 + learn + continue 重试（见下方） |
| F | （已删）~line 1890 `_forward_count_tokens` 原防御性剥离 | 复核实证：count_tokens 带空签名块打 aigc 已 200（不验签）→ 该点成纯死重，删除。count_tokens 保持现状零改动。 |
| G | ~line 3395 / 3407（run() 实例化与 server 装配处） | `thinking_strip_store = ThinkingStripStore()` + `server.thinking_strip_store = thinking_strip_store`（照抄 pref_store 装配模式） |

**D 点关键代码形态**（注意最后一行强制 re-dump——见推演·具象3）：

```python
# thinking 剥离：aigc 验签 supply（learn 到的）出站前删空签名历史 thinking 块。
# fail-open：未 learn 的 supply 首轮请求不剥离（mcli 不验签，零行为变化；
# 剥离后 failover 到未 learn supply 的场景见风险 R7，后果良性）。
if (target == "anthropic" and isinstance(body_json, dict)
        and thinking_strip_store.snapshot(supply_id)):
    _n = strip_empty_signature_thinking(body_json)
    if _n:
        log.warning("thinking_strip: supply=%s removed %d empty-signature thinking blocks",
                    supply_id, _n)
        send_body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
```

**E 点关键代码形态**：

```python
# thinking 签名 400 自适应剥离重试（仅一次，不 rotate/cooldown/tried_set，
# 与 reasoning 语法重试同模式；分类器文本与 interpret_rejection 各 pattern 互斥，
# 单个 400 至多命中其一。置于语法重试之前：签名错误更特定且不依赖 reasoning_wire）。
if (resp_status == 400 and not _thinking_strip_retried
        and mode == PASSTHROUGH and target == "anthropic"
        and is_invalid_signature_rejection(resp_body)):
    thinking_strip_store.learn(supply_id)
    _thinking_strip_retried = True
    self._acc["thinking_strip"] = 1
    log.warning("thinking_strip_retry: supply=%s learned, stripping and retrying once",
                supply_id)
    continue  # 重进 while：select_supply 复选同 supply，D 点命中刚 learn 的偏好自动剥离重发
```

**`_acc["thinking_strip"]` 观测接线（复核补充，两处配套，缺一即成死字段）**：
1. 默认 `_acc` 表（server.py ~1761-1786）加 `"thinking_strip": 0,`；
2. ACCESS 格串（server.py ~1817-1834，按位置枚举传参）在 `nudge_rewritten=%s` 后追加 `thinking_strip=%s` 并在参数表对应位置补 `a["thinking_strip"]`。
置而不发会被格串静默吞掉成死观测点；补格串而非删字段——生产上"剥离/重试发生了几次"是低频高价值观测（对齐 body_error/nudge_rewritten 先例）。注意追加位置必须在格式串与参数表**同一位序**，否则整行错位（位置枚举格串的固有风险）。既有测试 harness 自建 `_acc` dict 的（test_body_error 等）不经过 ACCESS 写行则可不补该 key——`a["thinking_strip"]` 直接索引仅发生在写行路径；稳妥起见驱动 `_forward` 全链路的 harness 同步补 `"thinking_strip": 0`。

continue 后重试轮的剥离**不需要额外代码**：store 已 learn，D 点在重试轮自然命中并剥离重发（照抄语法重试"learn 后 select_variant 命中刚学偏好"的结构）。remap 缓存（`_reasoning_cache_supply_id`）不受影响：同 supply、未进 tried_set，缓存继续复用，剥离不触碰 reasoning 字段。

### 3. 自适应学习状态机

```
[冷启动] store 空
  → 请求打 aigc，历史含空签名 thinking 块
  → aigc 400，文本含 "Invalid `signature` in `thinking` block"
  → E 点命中：learn(supply_id)（内存缓存，TTL 48h）+ _thinking_strip_retried=True + continue
  → 重试轮 D 点剥离空签名块 → 重发 → 200
[稳态] 后续每请求 D 点前置剥离，零额外尝试
[过期] 48h 后 entry 失效 → 等价冷启动（首请求吃一次 400 → re-learn → 自愈）
[重启] 内存态丢失 → 同冷启动（先例 SyntaxPreferenceStore 同款可接受性）
[误学习防护] 分类器精确短语匹配；schema 类 signature 报错不命中 → 不 learn 不剥离
[坏块剥离不掉] 剥离判据未命中实际坏块（如非空伪造签名）→ 重试仍 400
  → _thinking_strip_retried 已置位 → 落回既有 cooldown+failover 路径（行为=改造前）
```

### 4. 与既有重试机制的交互矩阵

| # | 同请求事件序 | 行为 | 终态 |
|---|---|---|---|
| 1 | 签名 400（无语法问题） | 剥离 learn + 同 supply 重发 | 200 |
| 2 | 语法 400（无签名问题） | 现状不变（reasoning learn + 重发） | 200 |
| 3 | 语法 400 → 签名 400 | 两 flag 独立各一次：先语法重试，再剥离重试；剥离历史块与语法改写请求级字段互不冲突可叠加 | 200 |
| 4 | 签名 400 → 语法 400 | 先剥离重试（E 在前），再语法重试 | 200 |
| 5 | 剥离重试后仍签名 400 | flag 已置位，落 cooldown+failover（现状路径） | failover |
| 6 | 签名 400 但 mode≠PASSTHROUGH 或 target≠anthropic | E 点 guard 不触发 learn（不污染），走现状路径 | 现状 |
| 7 | 预算截断（200 后） | 无交互：剥离在出站前、预算判定在响应后；剥离重试不消耗 `_budget_retries`（对齐"语法/预算重试互不消耗"既有声明） | 200→预算爬升 |
| 8 | count_tokens | 已实证 aigc 不验签（带空签名块 200）→ 零改动零交互，现状透传 | 现状 |

注：单个 400 报文至多命中一个分类器——`is_invalid_signature_rejection` 的精确短语与 `interpret_rejection` 各 pattern（"thinking.type.enabled…not supported"/"budget_tokens…not supported"/"参数有误"/"invalid parameter"）文本互斥，已逐条核对无重叠。

### 5. 边界

- **流式**：剥离发生在出站 body 构建期，与响应流式无关；400 以 HTTPError 完整到达（既有语法重试依赖同一事实）。
- **count_tokens**：复核实证 aigc 对 count_tokens 不验签（带空签名 thinking 块 200）→ 原 F 点防御性剥离为纯死重，已删，count_tokens 零改动。
- **重试幂等**：剥离幂等（二次调用删 0 块）；`continue` 重试复用 body_json（原地改写持续生效）；与 `inject_system_block` 幂等先例同构。
- **body_json 为 None / messages 缺失 / content 为字符串**：剥离函数全部安全跳过返回 0。
- **现有测试 harness 兼容**：`self.server.thinking_strip_store` 为直接属性访问（对齐 pref_store 先例，fail-fast 不静默）→ 7 个驱动 `_forward` 的既有测试文件 SimpleNamespace/装配处各补一行 `thinking_strip_store=ThinkingStripStore()`（机械改动）：test_body_error / test_access_token / test_budget_retry / test_cooldown_rules / test_nudge_rewrite / test_supply_ext / test_route_command（test_count_tokens 走 `_forward_count_tokens` 不触该属性，F 点已删，无需改）。

### 推演小节（交付前自检，命中：规则求值序/具象走查/幂等衔接）

**具象1·剥离判据**（推演，非实测）：
- `{"type":"thinking","thinking":"…","signature":""}` → 命中删除（mcli 产块形态）
- `{"type":"thinking","thinking":"…"}`（缺 signature 键）→ 命中删除
- `{"type":"thinking","thinking":"…","signature":"EqQBC…"}` → 保留（aigc 自签块）
- `{"type":"redacted_thinking","data":"…"}` → 保留（不命中 type 判据）
- `{"type":"text","text":"…"}` / `{"type":"tool_use",…}` → 保留

**具象2·分类器**：`{"error":{"type":"invalid_request_error","message":"Invalid \`signature\` in \`thinking\` block: …"}}` → lower+去反引号 → 命中；`{"error":{"message":"messages.0.content.1: Field required: signature"}}` → 无完整短语 → 不命中（不 learn，落现状路径）；空 body / 乱码 → 不命中。〔判据形态来自派单转述的实测报文，短语匹配逻辑为推演，落测试实证〕

**具象3·raw_body 逃逸 bug（设计修正点）**：走查 PASSTHROUGH 分支发现——若 supply 无 target_model、无 reasoning_wire、无 system_inject、预算字段无变化，现有代码 `send_body = raw_body` 保持原始字节；若剥离只改 body_json 不强制 re-dump，剥离将被静默丢弃、重试仍发未剥离 body → 二次 400。D 点代码形态中 `if _n: send_body = json.dumps(...)` 即为此修正，为 load-bearing 行。

**规则求值序**：交互矩阵 #3/#4 用"语法400→签名400→200"与"签名400→语法400→200"两条具象事件序走查，两 flag 独立、互不阻塞，均达 200。

**幂等衔接**：剥离重试轮对已剥离 body 再跑 D 点 → 删 0 块 → 不 re-dump → send_body 不变（与 reasoning merge/system_inject 的 re-dump 共存无冲突，多次 dumps 结果一致）。

## 风险与权衡

- **R1 分类器文本漂移**：aigc 未来改报文措辞 → 不命中 → 不 learn → 行为退回改造前（cooldown+failover），不产生新错误。fail-open 成立。放宽匹配（只查 "signature"+"thinking"）会误吞 schema 类报文，刻意不做。
- **R2 aigc 放宽验签**：entry 48h 过期后自然停止剥离；存续期内对已放宽的 supply 继续剥空签名块——被剥的块本就是官方协议非法形态，无正确性损失，仅丢上下文（thinking 历史块对模型续写无语义贡献）。
- **R3 supply 粒度的重复学习**：同 endpoint 多 supply 各 learn 一次。config 现状（复核订正）：glm aigc 簇六 supply 同 URL 同模型、ds-flash aigc 簇七 supply两模型——若这些簇未来触发签名 400，冷启动代价按 supply 数放大（glm 簇 6 次 400+重试）。当前两簇均为非 claude 模型、是否验签未实证，代价为假设值；即便坐实，一次性有界且 48h 内不再重复。不为此上移到 model/URL 级（抽象收益 < 归属歧义成本）。
- **R4 redacted_thinking 未覆盖**：mcli 是否产空签名 redacted_thinking 未实证。若出现，走"剥离不掉→failover"现状路径不恶化；届时再扩展判据（一行）。
- **R5 剥离后 assistant content 变空列表**：官方协议 thinking 块不能是 assistant 消息末块，Claude Code 历史中 thinking 必伴随 text/tool_use，此形态实际不出现；即便出现，留空列表由上游 400 → failover 兜底。不为此加"整消息删除"逻辑（会破坏 tool_use/tool_result 配对，风险更大）。
- **R6 内存态重启丢失**：每 supply 重启后吃一次 400 即自愈，先例（SyntaxPreferenceStore）同款可接受性；多 supply 簇重启后按簇大小重复该代价（见 R3）。
- **R7 剥离后 failover 的 body 状态泄漏（复核知情项）**：剥离在 body_json 上原地发生，D 点 guard 是 supply 级——剥离重试后若仍失败落 cooldown+failover 换到**未 learn** 的 supply，出站的已是剥离后的 body_json，方案表述"未 learn 的 supply 永不剥离"字面被破（准确表述：未 learn 的 supply 的**首轮**请求不剥离）。后果良性：被剥的空签名块本就是官方协议非法形态，aigc 系不验签则不受影响，mcli 不验签不受影响；不为此做 body_json 深拷贝回滚（每请求拷贝全量消息历史的成本 > 良性后果）。

## 验证方式

**单测**（新增，`cd tools/model_proxy && python3 -m pytest tests/test_thinking_guard.py tests/test_thinking_strip_retry.py -v`）：

test_thinking_guard.py（纯函数，脱网络）：
1. 剥离判据四态：空签名删/缺键删/非空签名留/redacted_thinking 留（推演·具象1 逐条断言）。
2. 幂等：同 body 连续两次，第二次返回 0 且 body 不变。
3. 非法输入：body_json None、messages 缺失、content 字符串、messages 非列表 → 0 且不抛。
4. 分类器：反引号体命中/无反引号命中/Field-required 不命中/空 bytes 不命中（推演·具象2 逐条断言）。
5. ThinkingStripStore：learn→snapshot True；TTL 过期（patch time.time +48h+1s）→ False；未 learn → False。

test_thinking_strip_retry.py（假上游驱动，照抄 test_body_error.py 的 urlopen 队列 patch + SimpleNamespace server 模式）：
6. **核心链路**：aigc 单 supply，队列 [400 签名错误体, 200] → 断言：出站 2 次；第 2 次出站 body 无空签名 thinking 块、aigc 自签块仍在；cooldown 未被调用；最终 200；store 已 learn；`_acc["thinking_strip"]==1`。
7. **重试幂等**：队列 [400, 400] → 第 2 次 400 后不无限循环（flag 置位）→ 落 cooldown+failover（断言 cd.cooldown 被调用）。
8. **语法+签名叠加**（矩阵 #3）：队列 [语法400, 签名400, 200] → 3 次出站，第 2 次出站 thinking 语法已切换、第 3 次出站已剥离，终态 200。
9. **fail-open**：store 空 + 队列 [400 签名错误体] + failover 路由 → 行为与改造前一致（cooldown + 换源）。
10. **raw_body 逃逸回归**（推演·具象3 的实证靶）：构造无 target_model、无 thinking 请求级字段、无 system_inject 的 supply + 已 learn 的 store → 断言出站 body 确实已剥离（防 send_body 停留在 raw_body）。
11. **回归**：`python3 -m pytest tests/ -q` 全量通过（含 7 个 harness 补行的既有文件）。
12. **ACCESS 行观测**：集成测断言写回的 ACCESS 日志行含 `thinking_strip=1`（触发重试场景）；未触发场景为 `thinking_strip=0`。

**实测**（部署后人工核对，需内网）：
1. 重启代理 → 用带 mcli 空签名 thinking 历史的请求打 claude-sonnet-sankuai-2023（可经 `$route` in-band 指令 pin）→ 日志出现 `thinking_strip_retry: supply=claude-sonnet-sankuai-2023 learned` 随后 200；同会话后续请求日志出现 `thinking_strip: … removed N`。
2. 对照：同 body 不剥离直发 aigc 应 400（已有实测结论复现基线）。
3. 打 mcli（store 未 learn）→ 零剥离日志、行为不变。

## 交付物规范

- **目录树**（新增部分，锚 `tools/model_proxy/`）：
  ```
  tools/model_proxy/
  ├── core/thinking_guard.py                 # 新增
  ├── core/server.py                          # 修改（A-E/G 六处接线 + ACCESS 观测两处 + 默认 _acc 表）
  └── tests/
      ├── test_thinking_guard.py              # 新增
      └── test_thinking_strip_retry.py        # 新增
  ```
- **命名规则**：模块 `thinking_guard.py`（动词域命名，对齐 `protocol_hints.py`/`session_identity.py` 先例）；测试 `test_thinking_*.py`（对齐既有 `test_<topic>.py`）；设计记录 `docs/designs/2026-10-28-thinking空签名剥离与supply级自适应.md`（对齐该目录日期-slug 既有惯例）。该仓库 docs/designs/ 无产物索引文件，无索引可登记（无偏离）。
- **产物清单**：
  | 路径 | 用途 | 消费者 |
  |---|---|---|
  | `core/thinking_guard.py` | 剥离纯函数/400 分类器/学习存储 | server.py 三处接线、单测 |
  | `core/server.py`（改） | 接线：出站剥离/400 重试触发/装配 + ACCESS 观测（默认 _acc 表 ~1761、格串 ~1817） | 全部转发路径 |
  | `tests/test_thinking_guard.py` | 纯函数单测 | CI/本地 pytest |
  | `tests/test_thinking_strip_retry.py` | 假上游集成测（重试链路） | CI/本地 pytest |
  | 7 个既有测试文件（各 1 行） | harness 补 `thinking_strip_store` 属性 | 既有测试 |
- **归属引用**：本仓库无领域 CLAUDE.md/methodology 约定文件；自适应先例引用 `core/reasoning/codecs.py::interpret_rejection` 与 `core/server.py::SyntaxPreferenceStore`（48h TTL、learn-from-400、不进 tried_set 不 cooldown 的同 supply 重试三要素照抄）。

## 关联

- 2026-10-28 独立复核（architect-xhigh）四条知情级备注订正：config supply↔model 对应关系事实、剥离后 failover body 状态（R7）、`_acc["thinking_strip"]` ACCESS 格串配套观测、count_tokens 已实证不验签（F 点删除）。结论主体结构未动。

- 先例：[[tools/model_proxy/core/reasoning/codecs.py]] interpret_rejection、[[tools/model_proxy/core/server.py]] SyntaxPreferenceStore
- 相关设计：[[2026-08-07-reasoning-thinking-truncation-and-protocol-consistency]]、[[2026-10-15-supply-test正文摘要与body-error联动]]、[[2026-10-10-正文错误识别与failover衔接方案]]
- 相关测试范式：tests/test_body_error.py（假上游 urlopen 队列驱动）
