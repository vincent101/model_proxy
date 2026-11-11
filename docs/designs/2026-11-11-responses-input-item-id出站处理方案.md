---
created: 2026-11-11
modified: 2026-11-11
type: design-decision
status: draft
target: /Users/vincentwang/Documents/NoteVault/tools/model_proxy
tags:
  - architect
  - model-proxy
  - responses
path: 务实
---

# Responses input item id 出站处理：从前缀改写收敛到「出站不携带"]

## 1. 背景与问题

`responses→responses` 是 PASSTHROUGH：body 结构不改，但已有少量出站规范化（model 改写、thinking 剥离、reasoning wire merge、预算 stamp）。2026-11-10 又加了一条 `item_ → fc_` 前缀改写（`core/item_id_normalize.py`）。上线后暴露出这条思路的地板：

1. **改名只治前缀校验，治不了存在性校验**：reasoning item 带 `rs_` id + `store=false` → `Item with id ... not found`（改前缀无效；实测 `store=true` 也照样 400）。
2. **改名覆盖面天然滞后于上游类型表**：message 的 `item_` 前缀同样 400（应 `msg_`），当前未覆盖；未来任何新 item type 都会再漏一次。
3. 这是同一机制第三次补丁（生成侧 `cd0cd49` → 出站改名 `2622fb2` → 现在 message/reasoning）。按项目 L3「三轮补丁即上移契约层」，本轮应上移一层给契约，而不是继续扩前缀表。

本记录只做只读核查与设计，不改代码、不提交、不部署；真实上游实验由用户执行（凭证在用户侧）。

## 2. 代码事实核查（只读，带锚点）

| 位置 | 事实 |
|---|---|
| `core/server.py:979-991` | `_TRANSLATOR_TABLE`：PASSTHROUGH 仅 `(anthropic,anthropic)` 与 `(responses,responses)`；`target=="responses"` 在 PASSTHROUGH 分支内等价于 source==target==responses |
| `core/server.py:2434-2483` | PASSTHROUGH 出站链：model 改写 → thinking 剥离（仅 anthropic）→ **item id 前缀改写（2453-2463）** → reasoning wire merge → 预算 stamp → system_inject。每次改写后 `send_body` 重新 dump，其余情况保持 `raw_body` |
| `core/server.py:2234-2256` | failover/重试循环在循环体内**原地复用同一个 body_json**；首轮所做的出站改写对后续 supply 的第二轮仍然有效（幂等前提下） |
| `core/item_id_normalize.py:19-52` | 现模块：`_REWRITABLE_TYPES={function_call,custom_tool_call}`，只改 `item_` 前缀、保留 hex、不动 call_id；返回改写数 |
| `core/translate.py:592-597` | `gen_item_id` 已产出 `fc_`（生成侧已修，保留） |
| `core/translate.py:1886-1948` | r2a 响应侧用 `gen_message_id/gen_item_id/gen_reasoning_id` **伪造** item id 回给客户端（`msg_`/`fc_`/`rs_` + 32 hex） |
| `core/translate.py:608-609` | `gen_reasoning_id` = `rs_` + `token_hex(16)`（32 hex）。证据包里报错的 `rs_a458158d6bbcd0ece4c275b72d27fc71` 恰为 32 hex，与伪造形态同构〔推算，待 E0 取证〕 |
| `core/translate.py:2436-2582` | **a2r 出站输入形态**：message / function_call / function_call_output 一律**不带 id**；reasoning 只带 `encrypted_content`（2553-2557）、**不带 id**。该形态 2026-11-06 已上游闭环实测（CHANGELOG:12-15） |
| `core/translate.py:2605-2695` | a2r 请求**不含 `store`**（白名单转换）→ 上游按缺省 `store=true`，仍能用无 id 输入正常工作 |
| `core/translate.py:1820-1834` | r2a 消费 responses 历史时：function_call 只读 `call_id`；reasoning 靠 `encrypted_content`，无加密块的 reasoning 直接丢弃——**全链路不存在「靠 item id 关联」的路径** |
| `core/server.py:1065-1089` | `previous_response_id` 仅作 session key 兜底读取，从不注入/改写；证据包记录线上从未出现该字段 |
| `tests/test_responses_guard_removal.py:92-104` | 该测试用「镜像实现」构造期望出站 body（现为前缀改写版），规则变更必须同步，否则成为双实现漂移点 |

## 3. 五个待决问题的直接回答

**Q1 移除 id 是否无损？** 分类型看：

- `function_call` / `custom_tool_call` / `function_call_output`：无损。配对只靠 `call_id`（证据 A/B/I；r2a 消费侧也只读 `call_id`，`translate.py:1808,1817`），且 a2r 一直以「无 id」形态生产运行。
- `message`：无损。无 id 形态（D）与伪造前缀 id（G）的差异只在前缀校验；a2r 的 message 从不带 id。
- `reasoning`：**移除 id 是唯一可行解，但「无损」只在 store=false / 伪造 id 两档成立**。若 id 确属当前 supply 且确实持久化，保留 id 可能让上游加载存储推理，移除后退化为 `summary`/`encrypted_content` 明文形态。这一档的收益既无实测支撑，也与本代理的多 supply 现实冲突（见 Q2），判为可接受损失（风险 R1，实验 E4）。

唯一真正「有损」的是引用形态（`item_reference` 或去掉 id 后无任何负载的裸引用）——所以不无条件剥离（见 §4.2）。

**Q2 是否按 store 分流？** 否决，三条独立理由：

1. **默认值语义**：`store` 缺省为 true。分流等于让「未显式写的客户端」走保留分支，而保留分支恰是 bug 面。
2. **supply 亲和不可保证**：proxy 的存在前提是同档多 supply + 冷却/failover。id 只在其产生的那一个 supply 的存储里有意义；同一 body 换 supply 后 id 必然悬空。store 分流会把「可 failover」直接变成「必然 400」，等于关掉 failover。
3. **store=true 也不保证 id 存在**：证据 E（伪造 rs_ + store=true → 400）已实证。store 不是「id 可解析」的判据。

**Q3 既有改名投资怎么处理？** 分两半：

- `gen_item_id` 的 `fc_`（`cd0cd49`）：**保留**。它管响应侧生成，本方案不碰，且是正确方向。
- `core/item_id_normalize.py` 的前缀改写（`2622fb2`）：**整体替换为剥离**，不保留双规则。前缀表是无限追更的机制（上游按类型校验，已有 fc/msg/rs 三套），留着它只会让未来读者以为「id 是应该保住的、只是格式要修正」——错误心智模型。

**Q4 门控粒度？** 保持 `target == "responses"`（PASSTHROUGH 内即 responses→responses），不引入 supply/store 维度。理由：这是**协议级不变量**（「不声明我们无法兑现的 id」），不是某个上游的怪癖；supply 级门控会让新接入的 responses 上游默认漏网。

**Q5 2026-11-04 撤除的理由是否适用？** 不适用，但有一条原则要继承——逐条见 §4.5。

## 4. 方案设计

### 4.1 候选对比

| 方案 | 规则 | 判定 |
|---|---|---|
| W 扩前缀表 | 按 type 维护 id 前缀映射，继续改写 | 否决：第三次补丁；前缀已知三类且还会增；reasoning 的存在性问题无解 |
| X 无条件剥离 | `input[]` 里所有 item 的 `id` 全删 | 否决：会破坏引用形态（`item_reference` / 裸引用），把「可解析引用」变成「空 item」 |
| Z 按 store 分流 | store=true 保留/规范化，store=false 剥离 | 否决：Q2 三条（缺省 true、failover 跨 supply、store=true 也不保证存在） |
| **Y 结构性剥离 + 引用豁免（推荐）** | 有负载的 item 删 `id`；引用形态保留并告警 | 采用：一条结构规则覆盖全部类型（含未来新增），与 a2r 已验证形态一致，跨 supply 安全 |

推荐 Y 的额外理由（不只「更简单」）：

- **与代理自身已验证的输入形态对齐**：a2r 出站一直是无 id 形态且上游闭环实测（`translate.py:2436-2582`、CHANGELOG:12-15）。Y 让两条通向 responses 上游的路走同一形态（不同场景走同一条路），而不是让 passthrough 独享一个「看起来合法」的伪造 id。
- **诚实**：改名是把不可兑现的 id 伪装成合规 id（上游若查存在性就 400，且错误点与真实原因脱节）；剥离是明确「本代理不声明该引用有效」，失败面收敛为 0。
- **开闭**：判据是结构（有无负载）而非类型白名单，新增 item type 无需改代码。
- **failover 正确性**：剥离后 body 自含，跨 supply 重放安全；这是 proxy 的核心职责。

### 4.2 方案契约

模块 `core/item_id_normalize.py`（保留路径，替换语义；文件名描述目标是「出站合规」，仍成立；避免为一个 1 天前的文件做重命名 churn）：

```python
_NON_PAYLOAD_KEYS = frozenset({"id", "type", "status"})

def sanitize_responses_input_ids(body_json) -> tuple[int, int]:
    """返回 (stripped, kept_references)。仅作用于本次出站副本，幂等。"""
```

规则（按序）：

1. `body_json` 非 dict、`input` 非 list → `(0, 0)`；`input` 是字符串/其它 → 原样，`(0, 0)`。
2. 逐 item：非 dict / 无 `id` 键 → 跳过。
3. `type == "item_reference"`，或 **去掉 `{id,type,status}` 后无任何键**（裸引用形态）→ **保留 id**，`kept_references += 1`（键存在即算负载，即使值为空：`{"type":"reasoning","id":"x","summary":[]}` 属有负载，应剥离）。
4. 其余 → `del item["id"]`，`stripped += 1`。
5. **不看 id 内容、不判前缀、不碰 `call_id` / `arguments` / `output` / `encrypted_content` / `summary` / 其它任何字段**；不读 `store` / `previous_response_id`。

调用点 `core/server.py:2453-2463` 替换为：

```python
if target == "responses" and isinstance(body_json, dict):
    _nid, _nref = sanitize_responses_input_ids(body_json)
    if _nid:
        send_body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
        log.info("input_item_id_strip: supply=%s stripped %d item ids", supply_id, _nid)
    if _nref:
        # 引用形态不剥离（剥离会变成空 item），但不保证可解析：fail-fast 可观测，不猜
        log.warning("input_item_id_strip: supply=%s kept %d reference-shaped item ids",
                    supply_id, _nref)
```

- 仅 `_nid > 0` 才 re-dump（`_nref` 不改 body）。
- 不新增 ACCESS 字段（避免占位/参数错位）；沿用 `log.info` 观测模式。
- 不改 `raw_body`，不改客户端数据；不注入 `store`、不重写 `call_id`、不做任何 400 触发重试。

### 4.3 实施清单（文件 / 函数 / 行锚点）

| 顺序 | 文件（绝对路径） | 改动 |
|---|---|---|
| 1 | `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/item_id_normalize.py` | 整模块重写：删 `_LEGACY_PREFIX`/`_CANONICAL_PREFIX`/`_REWRITABLE_TYPES`（:19-24）与 `normalize_responses_item_ids`（:27-52），新增 `sanitize_responses_input_ids`。docstring 改为陈述不变量：「passthrough responses 出站不携带无法兑现的 input item id；引用形态豁免」 |
| 2 | `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/core/server.py` | `:38` import 改新函数名；`:2453-2463` 调用点按 §4.2 替换（注释重写：说明为何是剥离而非改名、为何不按 store/supply 分流） |
| 3 | `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/tests/test_item_id_normalize.py` | 整文件重写为剥离语义（见 §6.1） |
| 4 | `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/tests/test_responses_guard_removal.py` | `:3-7` 不变量描述改为「出站仅 target_model 替换 + input item id 剥离」；`:92-104` `_expect_forwarded` 同步（或改为显式断言「出站 input 无 id，其余深等」，避免镜像实现漂移）；`:129-169` 各 subTest 期望随之；新增「failover 第二轮 body 同样无 id」与「item_reference 保留」两例 |
| 5 | `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/CHANGELOG.md` | 新增一条（0.13 段内，日期 2026-11-11）：剥离策略取代前缀改写、理由、门控与回滚方式。**不改历史条目 :38-45**（那是当时的真实行为记录） |
| 6 | `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/docs/ARCHITECTURE.md:125-126` | 把「r2a item ID 前缀 fc_」条目扩为两条：①生成侧 `fc_` 保留；②passthrough responses 出站剥离 input item id（引用形态豁免） |
| 7 | `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/README.md:327` | 仅一句：passthrough 出站会剥离 input item id（引用形态除外）。若不改也无功能影响，但保持「已知限制」段与实现一致 |

不建议的改动（明确排除）：`core/translate.py` 不动（a2r 形态已是目标形态；r2a 消费不依赖 id）；不加配置开关；不给 `_acc` 加字段；不动 `previous_response_id`。

### 4.4 推演

**① 规则求值序 / 迁移对照（同一具象样本，旧→新）**

样本 S：`input = [message id=item_aaa(有 content), function_call id=item_bbb call_id=call_1, function_call_output id=fco_ccc call_id=call_1, reasoning id=rs_ddd summary=[…], item_reference id=msg_eee]`，`store=false`。

| item | 旧规则结果 | 新规则结果 | 上游判据（证据） |
|---|---|---|---|
| message `item_aaa` | 保留 → 400 Expected 'msg' | 删除 → 无校验 | G / D |
| function_call `item_bbb` | 改 `fc_bbb` → 200 | 删除 → 200 | A/B/I |
| function_call_output `fco_ccc` | 保留 → 未测 | 删除 → 200 | 待 E7 补证 |
| reasoning `rs_ddd` | 保留 → 400 not found | 删除 → 200 | C1 / C2 |
| item_reference `msg_eee` | 保留 → 200 | 保留 → 200 | F + 豁免分支 |

旧规则 2 类必错；新规则 0 类。红判据：任一有负载 item 出站仍含 id ⇒ 违反不变量；绿判据：`stripped==4, kept_references==1`。

**② 跨步骤 id 生命周期（同一对象在两端各记成什么）**

- 步骤 1（上一轮响应）：proxy（r2a）或 responses 上游产出 item id → 客户端存进本地历史（`msg_`/`fc_`/`rs_`）。
- 步骤 2（本轮出站）：passthrough 出站剥离 → 上游只看到自含 items 与 `call_id`。
- 步骤 3（本轮响应）：上游重新生成它自己的 id → 客户端再存 → 下一轮再剥。
- 稳态：id 只存在于客户端侧与单次响应里，**永不跨请求出站**。与 a2r 的形态一致（那里根本不生成输入侧 id）。
- 红：若某轮因为门控/幂等缺陷漏剥，则上游按其存储态随机 400——正是当前现象的复现路径。

**③ 时序 / failover 重入（边界：多 supply 重试）**

attempt1（supply A）：剥离（stripped=4）→ re-dump → 400（任意原因）→ 冷却 A、换 B。attempt2（supply B）：循环重入 PASSTHROUGH 分支，剥第二次 → `(0,0)`（幂等），model/stamp 按既有逻辑覆盖 → 发出的仍是剥离后 body。红：B 收到带 A 时代 id 的 body ⇒ 跨 supply 悬空 400 级联；绿：两轮同形、B 自含可解。这正是否决 Q2「按 supply 门控」的场景化理由。

**④ 论证链与反例（针对「store=true 应保留 id」）**

- 前提 P1「store=true ⇒ 上游持久化 ⇒ id 可解析」。反例1：证据 E（伪造 `rs_` + store=true → 400）。反例2：failover 到 supply B（A 的存储 id 在 B 不存在 → 400）。⇒ P1 不成立。
- 前提 P2「剥离会让 store=true 用户丢失引用已存 item 的能力」。反例边界：引用形态（`item_reference`）被豁免保留 ⇒ 该能力不失；以完整 item 重放时 id 无额外作用（A/B/I 无 id 正确关联）⇒ 不成立。
- 前提 P3「剥离会破坏 pairing」。反例：r2a 消费侧只读 `call_id`（`translate.py:1808,1817`），a2r 也从不发输入侧 id（`translate.py:2540-2545, 2509-2513`）⇒ 不成立。

**⑤ 框架结构不变量**

- 正交：模块只碰 `input[].id`；model/thinking/reasoning/budget 各自独立函数，无交叉改写。
- 数据流无环：入站 dict → 出站副本，单向；不落盘、不学习状态、不回写客户端。
- 扩展点在扩展方向：新 item type 走一般规则，不改代码（对比 W 的类型表）。
- 退化=一般情况特例：引用形态是「剥离会改变语义」的退化分支，判据取结构而非类型。
- 入口覆盖：唯一入口 = `(responses,responses)` PASSTHROUGH；另四组合不经过 `input[]` 结构（a2r 自建、r2a 只读 `call_id`/`encrypted_content`）。无第二入口需接线。

### 4.5 与 2026-11-04 guard 撤除记录逐条比对（最关键复核点）

`docs/designs/2026-11-04-tool-config-fix-and-guard-revert.md` §2.4 撤的是两个 guard：`responses_id_guard`（400 后把 function_call id 批量改成稳定 hash **且成对重写 call_id**，同 supply 重试）与 `responses_store_guard`（400 后注入 `store=true`，同 supply 重试）。撤除理由逐条映射：

| 当年撤除理由 | 是否适用于本方案 |
|---|---|
| 用户判断「看起来不是这个问题」（诊断未被证实） | **部分适用 → 已补证**。本次有直测矩阵（A/B/I/C1/C2/D/F/G/E）+ 代码事实（a2r 无 id 形态生产可用、`rs_` 32hex 与 `gen_reasoning_id` 同构）。「移除 id 后上游一定接受」仍留 E1-E7 待补，实验项已列；不存在「先落地再看」 |
| guard 是**错误触发**的**同 supply 重试** + 语义注入 | **不适用**。本方案无条件、确定性、一次性出站变换，不识别 400、不重试、不注入 `store`、不改 `call_id`；与同分支既有的 thinking 剥离/预算 stamp 同类 |
| guard **伪造** id（hash 改写后看似合法实则不存在），把可诊断错误变成存储态相关的 400 | **反向适用**。本方案正是消灭伪造：删除而非改写，不声明 id 有效性 |
| guard 新增 ACCESS 键/告警面，需同步占位与参数 | **不适用**。只加一条 info + 一条罕见 warning，不动 ACCESS 格式 |
| 记录结论「撤除后两类 400 交回既有错误策略，不借机改通用 retry / raw_body」 | **继承**。本方案不改 retry/raw_body；失败仍走既有 HTTP 错误/冷却/failover |

唯一需要显式承认的重叠风险：本方案确实**删除客户端数据字段**（虽为可选字段）。缓解：引用形态豁免 + 告警计数 + 出站副本隔离（客户端历史不动）+ 单 commit 可回滚。若复核者要求「不做有损变换」，则只剩 W（扩前缀表），而 W 对 reasoning 无解、对 message 需再加一条、对未知类型未来必漏——那是把已知必错换成未知偶错。

## 5. 风险与权衡

- **R1（主要）**：`store=true` 且 id 真属当前 supply 时，保留 id 理论上可让上游加载存储推理；剥离后退化为 `summary`/`encrypted_content`。收益未实测，且「真属当前 supply」无法保证。实验 E4 定量；若 E4 显示明显差异，再议（届时也是加「客户端显式声明」而非按 store 猜）。
- **R2**：引用形态保留 ⇒ 该形态在 store=false / 跨 supply 下仍会 400。这是诚实的 fail-fast（不猜、不丢 item），并以 warning 计数暴露；当前 codex 形态不产生引用 item（E6 复核）。
- **R3**：观测性下降——出站不再有 id 前缀可供溯源。用 `stripped` 计数 + supply 补偿；需要按类型分布时再扩（不预先加）。
- **R4**：`function_call_output` / 未知新类型的「id 可删」依据弱于已被实测的类型（证据覆盖 message/function_call/reasoning）。风险低（id 在该类型上无关联用途），以 E7 补证；若未来某种类型要求 id 必填，单点回滚。
- **R5**：测试镜像实现漂移（`test_responses_guard_removal._expect_forwarded` 复制了规则）。建议改为明细断言（见 §4.3-4）。
- **R6**：文档一致性——"字节透传"表述已在既有条目中修正为先例（thinking 剥离、预算 stamp），本方案再加一条同性质说明，避免读者误以为 body 全等。
- **回滚**：单 commit revert 即可；无配置、无持久状态、无数据迁移。回滚后等价于回到 11-10 的改名行为（保留 `item_` 前缀 400 已修、message/reasoning 两类错仍在）。

## 6. 验证方式

### 6.1 模块单测（`tests/test_item_id_normalize.py` 重写）

1. message/function_call/function_call_output/reasoning 各带 id（含 `item_`、`fc_`、`msg_`、`rs_` 四种前缀）→ 全部剥离，`stripped==4`，`kept_references==0`。
2. 前缀无关性：合法前缀同样剥离（证明规则不是前缀表）。
3. 无 id → `(0,0)`；`call_id`/`arguments`/`output`/`encrypted_content`/`summary` 逐字段深等不变。
4. 未知类型（`local_shell_call`/`web_search_call`/`mcp_call`）带 id + 负载 → 剥离。
5. `item_reference` → 保留，计入 references。
6. 裸引用 `{"id": "msg_x"}`（无 type）→ 保留，计入 references。
7. `{"type":"reasoning","id":"rs_x"}`（无任何负载键）→ 保留，计入 references。
8. `{"type":"reasoning","id":"rs_x","summary":[]}`（键存在但值为空）→ **剥离**（判据是键集合，不是值非空）。
9. 幂等：连续两次第二次 `(0,0)`。
10. 边界：`None`、`{}`、`{"input":"str"}`、`{"input":[None,42,"x"]}`、`input` 为字符串 → `(0,0)` 且不改体。

### 6.2 集成（`tests/test_responses_guard_removal.py`，假上游，无网络）

- store 四态（`false`/`true`/`null`/缺省）下成功转发：出站 `input` 全无 id，其余字段与原文深等（除 model）。
- 400 + `failover=off`：一次调用、无同 supply 重试、无 store 注入（保持既有断言）。
- 400 + `failover=on`：第二 supply 收到的 body 同样无 id（跨 supply 安全）。
- 构造 `item_reference`：出站保留 id，且出现 warning（断言 warning 文本含计数）。
- 既有正向转换用例（`test_protocol_conversion_preserves_tool_use_and_result_pair`）不改。

### 6.3 离线命令（实施者执行，不含真实上游）

```bash
cd /Users/vincentwang/Documents/NoteVault/tools/model_proxy
python3 -m pytest tests -q                     # 基线 1218 passed，期望 ±新增用例数
python3 -m pytest tests/test_item_id_normalize.py tests/test_responses_guard_removal.py -q
grep -rn "normalize_responses_item_ids\|_LEGACY_PREFIX\|_CANONICAL_PREFIX" core tests   # 期望无命中
git diff --check
```

另需人工核对：`core/server.py` diff 仅 import + 单个 if 块；`core/translate.py` 零 diff；ACCESS 占位与参数未动。

### 6.4 需要用户补测的真实上游实验（每条单发、加可过滤标记，勿走生产 proxy 路径以免污染日志）

| # | 场景 | 期望 | 决策影响 |
|---|---|---|---|
| E0 | 日志取证（无网络）：出错请求的 `rs_a458…` 是否代理伪造 —— 该 session 上一轮是否路由到 anthropic supply（r2a 生成 `rs_`）；同时全量 grep `previous_response_id` 出现次数 | 次数 0；id 来自 r2a | 确认「伪造 id」口径；若为上游真实 id，仍不改变结论（store=false 必 400），但影响措辞 |
| E1 | reasoning `{id: rs_伪造, encrypted_content: <真实密文>}`，store=false；再发一次「同密文、无 id」 | 前者 400 或 200？后者 200 且能续推理 | 决定 R1 是否成立（加密载体是否依赖 id） |
| E2 | message 带**前缀正确但伪造**的 `msg_` id，store=false | 200（预期，同 F） | 证明 message 只校验前缀 → 剥离同样安全 |
| E3 | function_call 带伪造 `fc_` id，store=false | 200（预期） | 同上；也说明旧改名方案对 fc 有效但不足 |
| E4 | ① 同一 supply 用 store=true 拿到真实 `rs_` id，下一轮原样重放 → 200？② 把同 id 发另一 supply（store=true） | ①200 ②400 | 定量 R1 的「保留收益」；②直接实证否决 Q2 的 store 分流 |
| E5 | store=false + `previous_response_id` | 400 | 文档化「codex 无状态」假设，关闭该项疑问 |
| E6 | `input=[{"type":"item_reference","id":<上轮 store=true 真实 msg_ id>}]`：store=true / store=false 各一次 | 200 / 400 | 确认豁免分支的必要性与失败边界 |
| E7 | 同 payload（function_call_output 带 id / 不带 id）各一次，比较 `usage.input_tokens`、`cached_tokens` | 无差异 | 补证 R4，并排除 id 影响缓存/计费 |

不建议的实验：批量重放真实 codex 历史（成本高、噪声大）；在代理路径上加临时开关（会引入我们正要消除的分支）。

## 7. 交付物规范

无新增产物目录：设计记录落既有 `docs/designs/`（本文件）；代码与测试改动均在既有文件路径内，不新建模块/目录/配置项，不新增运行时产物。命名沿用 `YYYY-MM-DD-<主题>.md` 与 `core/*.py`、`tests/test_*.py` 约定，均为既有 README 索引范围内的路径。

## 8. 关联

- `docs/designs/2026-11-04-tool-config-fix-and-guard-revert.md` §2.4（ID/store guard 撤除边界，本次复核基准）
- `docs/designs/2026-08-08-tool-use-id-guard-implementation.md`（ID guard 最早形态，展示「伪造 id」路线的起源）
- `docs/designs/model_proxy_translate_spec.md`（Responses input item 字段映射契约）
- `docs/ARCHITECTURE.md` §5 跨协议语义保真（待补第 7 条）
- 证据包 `/tmp/item_id_evidence.md`（A/B/I/C1/C2/D/F/G/E 直测矩阵）
