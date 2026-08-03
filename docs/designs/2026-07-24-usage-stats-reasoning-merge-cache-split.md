---
created: 2026-07-24 18:40:00
type: design-decision
date: 2026-07-24
status: draft
target: "[[model_proxy_cli.sh]]"
tags: [architect, model_proxy, token-usage, reasoning, cache, stats, cli]
---

# model_proxy 统计口径二次收敛：reasoning 彻底合并 + 缓存命中拆分

## 背景与问题

一次改动的一体两面，同源于"统计维度该按计费差异取舍，而非按协议是否暴露明细取舍"：

- **Part 1（收敛）**：上一版方案（[[2026-07-24-usage-stats-reasoning-column-converge]]，status→本方案标记为 superseded）推荐"选项 A+：reasoning 存储层保留、展示层默认隐藏、`--verbose` 才显示"。**用户已明确否决 A+，要求"reasoning 不需要拆开，从表层到底层，因为没有必要"**——即比该文档"选项 B"更彻底：提取/累加/存储/展示全链路都不再把 reasoning 当独立统计维度。reasoning 已计入 output_tokens、无独立计费差异，作为**统计观测维度**无保留价值。
- **Part 2（新增）**：缓存命中（prompt/context caching）与 reasoning 相反——命中 vs 未命中 vs 写入存在**真实、可观的计费差异**（Anthropic 命中约 0.1x、写入 1.25x/2x；Responses/chat 命中约 0.25x-0.5x、新款写入 1.25x），值得作为独立统计维度拆分。当前统计层完全没有缓存维度。

两者共用同一套链路（`_extract_*` helper → `_acc` → `record`/`totals.json` → CLI `VAL_FIELDS`），故合并为一次改动设计。

### 现状核查结论（已逐处验证）

**协议与路由实况（config/model_proxy_config.json）**——纠正派单中一处前提：

| supply | protocol | 缓存 usage 格式 |
|---|---|---|
| claude-sonnet/opus/haiku | **anthropic** | Anthropic 三段：`input_tokens`(剩余) + `cache_creation_input_tokens`(写入) + `cache_read_input_tokens`(命中) |
| ds-pro/ds-flash/ds-v3friday | **anthropic** | **同 Anthropic 格式**（上游网关用 Anthropic 协议封装 DeepSeek，`target_model=deepseek-v4-*`）。**DeepSeek 原生的 `prompt_cache_hit_tokens`/`prompt_cache_miss_tokens` 在当前架构下不出现**——那是 DeepSeek 直连 chat API 的字段，此处网关已归一为 Anthropic 缓存字段。派单里对 DeepSeek 字段的调研在当前接入形态下不适用（下详）。 |
| glm-51/52 | **anthropic** | 同 Anthropic 格式 |
| openai-terra/sol/luna | **responses** | Responses：`input_tokens_details.cached_tokens`（+ 新款可能 `cache_write_tokens`） |
| kimi-k3 | **chat** | OpenAI chat：`prompt_tokens_details.cached_tokens`（+ 新款可能 `cache_write_tokens`） |

当前 strategy 仅 `cc`→claude route、`codex`→openai route（totals.json 里 `route=nation|strategy=obs-yolo` 是历史遗留，config 已无该 strategy）。据此，实际转发 mode（`_MODE_MAP`，server.py:467-471）：**绝大多数流量是 `PASSTHROUGH`（anthropic→anthropic，claude/ds/glm）**；codex 走 `PASSTHROUGH`（responses→responses）；`ANTHROPIC_TO_CHAT`（kimi）与两个转换 mode 是低频/历史/交叉客户端场景。

**统计链路（reasoning 现状，Part 1 要拆的对象）**：
1. `core/translate.py:1040` `_extract_reasoning_tokens(usage)`：三协议多路径提取 helper（07-23 implemented）。
2. `core/server.py`：`_acc` 初始化含 `usage_reasoning`（828）；四 mode + 两处流式旁路（1176、1189-1190、1209-1210、1221-1222、1242-1243、1257-1258、1279-1280、1630-1632、1642）写 `_acc["usage_reasoning"]`；ACCESS 日志打点（839-842）。
3. `UsageTotalsStore`：`_zero_combo`（116）、`record`（179-194）、`_archive_if_needed`（222）累加 `usage_reasoning`；落 `.claude_model_proxy_totals.json` `combos[key].usage_reasoning`（`version:2`）。
4. `model_proxy_cli.sh cmd_stats`（388-588）：`VAL_FIELDS` 六字段含 `usage_reasoning`（401），period 行（536-537、546-551）与 `print_groups`（562-563）打印 reasoning。

**协议转换保真链路（reasoning，Part 1 边界争点）**——与统计无关的另一层：
- `openai_to_anthropic_response`（549）、`_anthropic_usage_to_responses`（1076）、`AnthropicToResponses` skeleton（1237）、`responses_to_anthropic_response`（1768）、各 stream adapter `_absorb_usage`/`message_delta`（697、1331、1960）：把上游 reasoning 明细如实写进转换后响应的 `output_tokens_details.reasoning_tokens`，交给下游消费者（Claude Code / Codex）。

**缓存字段现状（Part 2 起点）**：统计层**完全没有**缓存维度。协议转换层仅 3 处做了缓存**只读透传**映射：`_anthropic_usage_to_responses`（1074，`cache_read_input_tokens`→`cached_tokens`）、`AnthropicToResponses` skeleton（1235，恒 0 占位）、`responses_to_anthropic_response`（1763-1765，`cached_tokens`→`cache_read_input_tokens`）。**`openai_to_anthropic_response`（chat 非流式，543）与 chat 流式 adapter 完全没读缓存字段**——即 kimi/chat 上游若有 `cached_tokens`，转换时被丢弃。所有映射都只映射了 `cache_read`，**无一处映射 `cache_creation`(写入)**，跨协议时写入信息本就在丢。

## 方案设计

### 需要先确认的两个决策点（不替用户隐性决定）

**决策点 A（Part 1 边界）——协议转换保真是否一并砍？** 用户"从表层到底层"字面上可有两种理解：
- **A1（推荐）**：只砍**统计观测**链路（`_acc`/`totals.json`/CLI），**保留**协议转换保真（转换后响应仍如实回显上游 `reasoning_tokens`）。
- **A2**：连协议转换保真也砍（转换后响应不再带 `reasoning_tokens` 明细）。

**我的判断与推荐：A1。** 理由：用户诉求是"我的代理统计不要再拆 reasoning"，针对的是运维观测；协议转换保真服务的是**下游消费者**（Claude Code/Codex 收到的响应里 `output_tokens_details.reasoning_tokens` 是上游真实回传值），砍掉等于代理擅自篡改/丢失上游返回的信息，属转换正确性倒退，且这是 07-23 刚修好的能力。A2 无收益、有风险。**若你认可 A1，Part 1 按下方清单执行；若你确实想连转换透传也去掉（A2），告诉我，我再补 A2 的额外删除点。** 下述 Part 1 清单默认按 A1。

**决策点 B（Part 2 口径）——是否顺带统一 usage_in 的跨供应商口径？** 这是务实/理想的实质分叉：

现有 `usage_in` 口径**本就不统一**（既存问题，非本次引入）：
- Anthropic 格式（claude/ds/glm，主流量）：`usage_in = input_tokens`，是**未命中的剩余量**，**不含** `cache_read`/`cache_creation`。
- Responses/chat 格式（openai/kimi）：`usage_in = input_tokens`/`prompt_tokens`，是**含命中的总量**（`cached_tokens` 是它的子集）。

即同一个 `usage_in` 列，claude 是"缓存外的剩余"，openai 是"含缓存的全量"，两者不可比，且 claude 的缓存输入量目前完全没被计入任何统计字段。

- **选项 B-务实（推荐主方案）**：`usage_in` 语义**原样不动**（承认口径遗留不一致），另加 `cache_read`/`cache_write` 两个**绝对量**累计字段。**不做命中率派生指标**（口径不统一时算不准，强行做会重蹈 reasoning 误导覆辙）。改动小、零历史数据语义漂移。
- **选项 B-理想**：把 `usage_in` 重新定义为统一的 `input_remain`（全价部分），令 `input_remain + cache_write + cache_read = 完整输入总量` 对所有协议成立（需在 Responses/chat 分支把 `usage_in` 从"总量"改减为"总量 − cached − cache_write"）。如此命中率 `cache_read/(cache_read+cache_write+usage_in)` 可跨供应商准确计算并默认展示。**代价**：改变历史同名字段 `usage_in` 含义（历史 openai/kimi 的 `usage_in` 是总量，新旧不可直接相加对比），且需改多个转换分支的取数口径。

**我的推荐：B-务实。** 用户诉求是"看到缓存命中/写入多少、计费差异"，核心是绝对量可见，不是重构输入口径。命中率虽有决策价值，但务实方案下算不准；把命中率与 B-理想绑定。**若你希望缓存命中率准确可比、愿意接受 usage_in 口径变更与历史不可比（走 B-理想），请明示；否则默认 B-务实。** 下述 Part 2 清单默认按 B-务实，末尾附 B-理想的增量差异。

---

### Part 1：reasoning 彻底合并（按 A1）

废弃上一版 A+。改动清单（`_extract_reasoning_tokens` 与各 adapter 内部 reasoning 属性/协议透传映射**全部保留**，只删统计观测）：

1. **`core/server.py`**
   - `_acc` 初始化（~828）去掉 `"usage_reasoning": 0`。
   - ACCESS 日志格式串与参数（~839-842）去掉 `usage_reasoning=%s` 及对应实参。
   - 四 mode 响应处理里对 `_acc["usage_reasoning"]` 的赋值全部删除：
     - PASSTHROUGH 非流式（1176）删 `self._acc["usage_reasoning"] = pt._extract_reasoning_tokens(_pu)`。
     - 三处 `adapter.usage_tuple()` 三元解包（1189-1190、1221-1222、1257-1258）：改为丢弃第三位，即 `(a["usage_in"], a["usage_out"], _) = adapter.usage_tuple()`（`usage_tuple` 签名保持三元组不变，见下"次要点"）。
     - 三处从转换后 usage 读 `output_tokens_details.reasoning_tokens`（1209-1210、1242-1243、1279-1280）删除。
     - 两处流式旁路（1630-1632、1642）删 `usage_reasoning` 赋值。
   - `_zero_combo`（116）去掉 `"usage_reasoning": 0`。
   - `record`（179-194）去掉 `usage_reasoning` 读取与两桶累加。
   - `_archive_if_needed`（222）去掉 `usage_reasoning` 累加。
   - **次要点（可选，倾向不做）**：三个 stream adapter 的 `usage_tuple()` 目前返回三元组，第三位是 reasoning。删统计后该位无人消费但仍正确。**推荐保留三元组签名、调用端丢弃第三位**（改动最小、零耦合）；若追求彻底可降为二元组，但需同步改 3 个 adapter 签名 + 全部解包点（有一致性耦合，漏一处解包报错），个人工具不值得，不推荐。
2. **`model_proxy_cli.sh cmd_stats`**
   - `VAL_FIELDS`（401）收敛为五字段：`("requests", "ok", "fail", "usage_in", "usage_out")`。
   - period 行解包与打印（536-537、546-551）去掉 `usage_reasoning`。
   - `print_groups`（562-563）去掉 `reasoning=` 列。
   - `print_help`（无 reasoning 文案则无需动；若曾加过则清除）。
3. **保留不动**：`_extract_reasoning_tokens`（1040）；各 adapter `self.reasoning_tokens`/`self.usage_reasoning` 属性与其写入 skeleton/response `output_tokens_details.reasoning_tokens` 的逻辑（549、1076、1237、1331、1768、1960 等）——这些是 A1 保真链路。
4. **历史数据**：`.claude_model_proxy_totals.json` 里已有的 `usage_reasoning` 键成为**孤儿字段**，天然向后兼容——新 `VAL_FIELDS` 遍历时用 `v.get(f, 0)` 不会读它、也不报错；随后续归档/重写逐步消失。**不强制迁移脚本**（个人工具不值得）。如需彻底清，可写一次性脚本遍历 `total`/`days`/`months_archive` 各 combos 删 `usage_reasoning` 键后原子回写——列为可选，不推荐。**版本号不升**（减字段向后兼容；07-23 加 `usage_reasoning` 时 version 亦保持 2，与本次减字段对称）。

### Part 2：缓存命中/写入统计（按 B-务实）

#### 1. 统一提取 helper `_extract_cache_tokens`（`core/translate.py`，紧邻 `_extract_reasoning_tokens`）

与 reasoning helper 同构，覆盖三协议 usage 格式，防御性 `or {}`：

```python
def _extract_cache_tokens(usage: dict) -> dict:
    """从任意上游协议 usage 防御性提取缓存 token 三态。

    返回 {"cache_read": int|None, "cache_write": int|None}。
    约定：某槽位为 None 表示"该协议此响应未暴露此字段"（不适用/未知），
          为 0 表示"支持且本次确为 0"。累加层把 None 折 0，靠 cache_probed 计数区分。
    协议路径（互不冲突，一个 usage 只命中一类）：
      anthropic : cache_read_input_tokens / cache_creation_input_tokens
      responses : input_tokens_details.cached_tokens / input_tokens_details.cache_write_tokens
      chat      : prompt_tokens_details.cached_tokens / prompt_tokens_details.cache_write_tokens
    """
    u = usage or {}
    itd = u.get("input_tokens_details") or {}
    ptd = u.get("prompt_tokens_details") or {}
    # cache_read：按键存在性判定"支持",不存在返回 None
    read = None
    if "cache_read_input_tokens" in u:
        read = u.get("cache_read_input_tokens") or 0
    elif "cached_tokens" in itd:
        read = itd.get("cached_tokens") or 0
    elif "cached_tokens" in ptd:
        read = ptd.get("cached_tokens") or 0
    # cache_write：Anthropic cache_creation / Responses|chat 新款 cache_write_tokens
    write = None
    if "cache_creation_input_tokens" in u:
        write = u.get("cache_creation_input_tokens") or 0
    elif "cache_write_tokens" in itd:
        write = itd.get("cache_write_tokens") or 0
    elif "cache_write_tokens" in ptd:
        write = ptd.get("cache_write_tokens") or 0
    return {"cache_read": read, "cache_write": write}
```

**"不支持 vs 支持但为 0"**：helper 层用 `None`/`0` 区分（按键存在性，不按值）。但累加进 totals.json 的是整数，None 无法累加——因此**该区分不靠单次值，而靠一个 `cache_probed` 计数字段**（见 §3）：某次响应 `cache_read` 或 `cache_write` 任一非 None（即该协议暴露了缓存字段）则 `cache_probed += 1`。展示层据此判 N-A（见 §4）。这比"值恒为 0 即判 N-A"严谨——能区分"claude 命中恒 0（真没命中）"与"kimi 根本不暴露缓存字段（不适用）"。

#### 2. `_acc` 与累加链路（`core/server.py`）

- `_acc` 初始化（~828）新增 `"cache_read": 0, "cache_write": 0, "cache_probed": 0`。
- ACCESS 日志（839-842）可选追加 `cache_read=%s cache_write=%s`（供 logs 观测；不追加也行，账本为准）。
- 每个 mode 分支，用**能拿到的最完整上游 usage** 调 `_extract_cache_tokens`，写 `_acc`：

  | mode | 取数位置 | 说明 |
  |---|---|---|
  | PASSTHROUGH 非流式（1169 `_pu`） | 对 `_pu`（原始上游 usage）调 helper | anthropic/responses 原始格式，直接可用 ✓ |
  | PASSTHROUGH 流式旁路（1625 anthropic / 1639 responses） | 对旁路嗅探出的 `u` 调 helper | 主流量路径 ✓ |
  | ANTHROPIC_TO_CHAT 非流式（1206） | **改从 raw `openai_resp` 取**，或在 `openai_to_anthropic_response` 补映射后从 anthropic usage 取 | 现从转换后 anthropic usage 取；而 chat 缓存字段转换器未映射→会丢。见下"顺带修的转换遗漏" |
  | ANTHROPIC_TO_CHAT 流式（1189 adapter） | adapter 需新增缓存属性 + `usage_tuple` 扩展，或旁路 | chat 流式 adapter 目前不读缓存 |
  | ANTHROPIC_TO_RESPONSES（1239 转换后 / 1221 adapter） | 转换后 anthropic usage（`responses_to_anthropic_response` 已映射 `cached_tokens`→`cache_read_input_tokens`✓，但未映射 write） | 补 write 映射后可用 |
  | RESPONSES_TO_ANTHROPIC（1276 转换后 / 1257 adapter） | 转换后 responses usage（`_anthropic_usage_to_responses` 已映射 read✓，未映射 write） | 补 write 映射后可用 |

  **累加约定**：`cache_read`/`cache_write` 取 helper 返回值、None 折 0 累加；`cache_probed` 当次 `cache_read is not None or cache_write is not None` 时 +1。
- `record`（179-194）：读 `acc` 的三个新字段，两桶（day/total）combo 累加。
- `_zero_combo`（116）新增三字段。
- `_archive_if_needed`（222）新增三字段累加。

**顺带修的转换遗漏（协议保真，与统计正确性同源，建议一并做）**：
- `openai_to_anthropic_response`（543）：补 `cached_tokens`（chat `prompt_tokens_details`）→ `cache_read_input_tokens`、`cache_write_tokens`→`cache_creation_input_tokens` 映射（当前完全没映射）。
- 三处已映射 read 的转换点（1074、1237、1763）补 `cache_write`（`cache_creation_input_tokens`↔`cache_write_tokens`）映射（当前只映射了 read，write 一直在丢）。
- 若采纳"转换 mode 从转换后 usage 取缓存统计"，则必须先补齐上述转换映射，否则统计取不到；若"转换 mode 从 raw 上游 usage 取"，可绕开转换器、统计不受转换遗漏影响，但转换器遗漏仍在（下游保真仍缺）。**推荐补齐转换映射 + 统计从转换后取**：一处修复同时惠及保真与统计，链路统一。

#### 3. `.claude_model_proxy_totals.json` schema

- `combos[key]` 新增 `cache_read` / `cache_write` / `cache_probed` 三整数字段（`_zero_combo` 反映）。
- **版本号不升**（纯加法；旧数据缺这些字段，读取 `v.get(f, 0)` 缺省 0 兼容）。
- **无需迁移脚本**：旧 combo 读出三字段皆 0、`cache_probed=0`→展示层判 N-A（"该组合历史无缓存数据"），语义正确。
- **DeepSeek 假设备注**：因 ds-pro 走 anthropic 协议网关，其缓存（若上游填充）以 Anthropic 三段字段呈现，`cache_read+cache_write+usage_in` 相加是否等于完整输入取决于上游网关是否按 Anthropic 规范填。**建议实施前用一次真实 ds-pro 请求核实**：上游是否真回 `cache_read_input_tokens`/`cache_creation_input_tokens`。若网关根本不填 → ds-pro 的 `cache_probed` 恒 0 → 展示 N-A，符合预期，无需特殊处理。派单里"DeepSeek 原生 hit/miss 字段"在当前接入形态不适用，helper 不为其单独加分支（除非将来有 supply 直连 DeepSeek chat API）。

#### 4. `VAL_FIELDS` 与 CLI 展示（缓存默认可见 + N-A 区分）

- `VAL_FIELDS` 增至八字段：`("requests","ok","fail","usage_in","usage_out","cache_read","cache_write","cache_probed")`（`usage_reasoning` 已按 Part 1 移除）。`merge_bucket_into`/`aggregate`/`zero_group` 自动随 `VAL_FIELDS` 累加，无需逐个手改（现有代码是遍历 `VAL_FIELDS`）。
- **展示（默认视图，缓存可见）**：
  - period 行追加 `cache_r=<k> cache_w=<k>`（`cache_probed` 不直接印，用于 N-A 判定）。
  - `print_groups` 各行：若该组 `cache_probed == 0` → 印 `cache=n/a`（该供应商/组合未暴露缓存字段，如 kimi 普通转发、或历史无数据）；否则印 `cache_r=<k> cache_w=<k>`。
  - 这样 kimi（走独立 Context Caching API、当前普通转发无缓存字段）显示 `cache=n/a` 而非误导性的 `cache_r=0`；claude 真未命中则显示 `cache_r=0`（`cache_probed>0`，是真 0 不是不适用）。**这是本方案对"reasoning 当年 claude 恒 0 误导"教训的正面回应**。
- **命中率派生指标**：**B-务实下不做**（见决策点 B）。仅当选 B-理想（usage_in 统一为 remain）才加 `hit%=cache_read/(cache_read+cache_write+usage_in)`。
- `print_help` stats 段补一句缓存列说明 + N-A 语义：`cache_r/cache_w 为缓存命中读/写入 token；某组合无缓存字段暴露时显示 n/a（如 kimi 普通转发、独立 Context Caching API 未接入）`。
- README stats 段同步。

#### 5. 供应商缓存统计可行性表

| 供应商(supply) | 协议 | 缓存字段来源 | 可行性 | 展示 |
|---|---|---|---|---|
| claude-sonnet/opus/haiku | anthropic | `cache_read_input_tokens`/`cache_creation_input_tokens` | 完整支持读+写 | 真实数值；未命中显 `cache_r=0`（真 0） |
| ds-pro/ds-flash/ds-v3friday | anthropic（网关封装 DS） | 同上（取决于上游网关是否填，**待实测**） | 支持则完整；网关不填则 N-A | 实测决定；不填→`cache=n/a` |
| glm-51/52 | anthropic（网关封装 GLM） | 同上（待实测） | 同 DeepSeek | 同上 |
| openai-terra/sol/luna | responses | `input_tokens_details.cached_tokens`(+新款 `cache_write_tokens`) | 支持读；写视模型代际 | 真实数值 |
| kimi-k3 | chat | `prompt_tokens_details.cached_tokens`(+新款 `cache_write_tokens`) | **走独立 Context Caching API，当前普通 chat 转发大概率无字段** | **`cache=n/a`**（不适用，非 0） |

Kimi 的 Context Caching 需开发者主动创建/维持/复用 Cache 对象；当前代理仅普通 chat 转发未使用该 API，故 usage 无缓存字段 → `cache_probed=0` → 展示 `n/a`。这与"支持但恰为 0"用 `cache_probed` 计数严格区分，避免造出新的误导列。

## 风险与权衡

- **决策点 A/B 未定则不落地**：这两点会导致实质不同的改动范围与历史数据兼容性，已在方案里显式反问、给出推荐（A1、B-务实），不替用户隐性选。用户拍板后本方案 status→confirmed 再派实施。
- **B-务实的遗留**：`usage_in` 跨供应商口径仍不统一（anthropic=剩余 / responses·chat=含命中总量），且 claude 的缓存输入量不进 `usage_in`。这是**既存问题**，本方案不新引入、也不隐藏——`cache_read`/`cache_write` 独立成列后，用户看 claude 组能自行理解"实际输入 = usage_in + cache_read + cache_write"。若将来要精确成本核算/跨供应商可比，再走 B-理想（届时新开一版方案，因涉历史数据语义变更）。
- **多点一致性耦合（实施风险）**：Part 1 删 `usage_reasoning` 与 Part 2 加三字段都需在 `_zero_combo`/`record`/`_archive_if_needed`/`_acc`/CLI `VAL_FIELDS` **同步生效**，漏一处→字段不累加或读取异常。`VAL_FIELDS` 驱动的 CLI 聚合是遍历实现（改一处 tuple 即全生效），但 server.py 三处（zero_combo/record/archive）是手写逐字段，属**有耦合的多点修改**——实施建议派 implementer 而非 runner，交付后派 reviewer 或 `/code-review` 专项核对"新增/删除字段五处（server 三 + acc + cli）一致"。
- **转换保真映射补齐的连带正确性**：补 `cache_write`/chat `cached_tokens` 映射会改变转换后响应的 usage 结构（多出字段），需确认下游 Claude Code/Codex 对多出的 `cache_creation_input_tokens`/`cache_write_tokens` 容忍（应容忍，属标准字段）。这是"顺带修"的附带面，若担心可先只做统计取数从 raw 取、暂不改转换器（但保真遗漏仍在）。
- **DeepSeek/GLM 缓存字段待实测**：anthropic 网关是否透传缓存字段决定 ds/glm 是显数值还是 N-A。实测前方案不假设，`cache_probed` 机制天然兜住（不填即 N-A）。
- **totals.json 实时写入**：账本随代理运行持续累加（核查期间 requests 2192→2366）。Part 1 减字段 / Part 2 加字段都是内存 dict 结构变更 + 原子回写，重启进程即生效；旧进程用旧 `_zero_combo` 写入、新进程读到缺字段用 `.get(,0)` 兼容，无需停服，但**改动上线需重启 model_proxy 进程**（`model_proxy_cli.sh off && on`）才让新 `_zero_combo`/`record` 生效。
- **上一版 A+ 为何不采纳**：非 A+ 本身有错，而是**用户改变了决策**（从"存储保留+按需展示"改为"彻底不作为统计维度"）。A+ 的分层理念（存储记客观事实、展示按可比性裁剪）对 reasoning 已不再需要——因 reasoning 无计费差异，连"单供应商成本占比"都无分析价值；而缓存有计费差异，恰恰适用该分层理念（Part 2 即其体现）。

## 验证方式

Part 1：
1. `bash model_proxy_cli.sh stats`（及 today/month/维度投影/过滤各组合）→ 输出**无任何 `reasoning`/`usage_reasoning` 字样**。
2. 发一条经 openai(responses)/kimi(chat) 的真实请求 → 下游收到的转换后响应**仍带** `output_tokens_details.reasoning_tokens`（证明 A1 保真未回退）；同时 totals.json 该 combo **无 `usage_reasoning` 累加**（新写入的 combo 无此键）。
3. `git diff`：`_extract_reasoning_tokens` 及各 adapter reasoning 属性/透传逻辑未改。

Part 2：
1. 发经 claude(anthropic) 的真实请求（构造 >1024 token 且重复前缀以触发缓存写入+命中）→ totals.json 对应 combo 出现 `cache_read`/`cache_write`/`cache_probed` 非 0；`stats` 默认视图该组显示 `cache_r=<k> cache_w=<k>`。
2. `stats` 里 kimi 组（若有历史或新发一条）显示 `cache=n/a`（`cache_probed=0`），**非** `cache_r=0`。
3. claude 未命中缓存的请求 → 该组 `cache_probed>0` 但 `cache_read=0`，显示 `cache_r=0`（与 n/a 区分正确）。
4. ds-pro 实测：确认上游是否回 `cache_read_input_tokens`/`cache_creation_input_tokens`；据此该组显数值或 n/a，与预期一致。
5. 转换保真：经 RESPONSES_TO_ANTHROPIC / ANTHROPIC_TO_RESPONSES 的请求，转换后 usage 含 `cache_write`（补映射后），下游正常解析不报错。
6. 历史兼容：改动后读旧 totals.json（含 `usage_reasoning` 孤儿、无 cache 字段）→ `stats` 不报错，旧 combo cache 列显 n/a。
7. 一致性回归：`_zero_combo`/`record`/`_archive_if_needed`/`_acc`/CLI `VAL_FIELDS` 五处字段集一致（无 KeyError、无漏累加）。

## 关联

- 本方案替代：[[2026-07-24-usage-stats-reasoning-column-converge]]（选项 A+ 因用户改变决策而废弃，该文档 status→superseded）
- 前序（07-23，status=implemented，Part 1 A1 保留其保真成果）：[[2026-07-23-usage-reasoning-extraction-unify]]、账本 schema [[2026-07-23-usage-totals-ledger]]
- 统计链路：[[core/server.py]] `_acc`（~828）、`UsageTotalsStore`（`_zero_combo` 116 / `record` 179-194 / `_archive_if_needed` 199-222）、四 mode 响应处理（1161-1284）、流式旁路（1615-1642）
- 提取 helper：[[tools/model_proxy/core/translate.py]] `_extract_reasoning_tokens`（1040，保留）、拟新增 `_extract_cache_tokens`；缓存透传映射点（1074、1235、1763-1765，补 write）、`openai_to_anthropic_response`（543，补 read+write）
- 展示：[[model_proxy_cli.sh]] `cmd_stats`（388-588，`VAL_FIELDS` 401、period 行 536-551、`print_groups` 562-563）、`print_help`（50-59）
- 配置实况：`tools/model_proxy/config/model_proxy_config.json`（supply protocol / route / strategy）
- 账本文件：`tools/model_proxy/.claude_model_proxy_totals.json`（version:2）
- README：`tools/model_proxy/README.md`（stats 段）
