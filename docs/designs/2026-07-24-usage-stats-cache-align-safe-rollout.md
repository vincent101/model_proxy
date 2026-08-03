---
created: 2026-07-24 20:30:00
type: design-decision
date: 2026-07-24
status: draft
target: "[[model_proxy_cli.sh]]"
tags: [architect, model_proxy, token-usage, cache, stats, migration, rollout, cli]
---

# model_proxy 缓存统计口径统一（usage_in 对齐 claude）+ 线上安全落地

## 背景与问题

model_proxy 是**线上运行中**的代理（Claude Code/Codex 正通过它转发全部请求），本次迭代不得影响正常使用。承接前序两版方案的两个已拍板决策：

- **决策点 A（reasoning 彻底合并）→ A1**：只砍统计观测链路（`_acc`/`totals.json`/CLI 的 `usage_reasoning`），**保留**协议转换保真（转发给下游的响应仍如实回显上游 `reasoning_tokens`）。**沿用 [[2026-07-24-usage-stats-reasoning-merge-cache-split]] 的 Part 1 A1 清单，本方案不重新设计，仅在"上线组织"节交代它与决策 B 如何一起上线。**
- **决策点 B（缓存口径统一）→ 用户明确要 B-理想式统一，但表述更精确**：原话"openai/kimi，把重复计算拆开，对齐claude，这样数据口径才一致"。即把 openai/kimi 的 `usage_in` 里"已含的 cache_read（及 cache_write）部分"拆出去，使 `usage_in` 变为"剩余量"语义，与 claude 对齐——所有供应商统一为**三段互斥**模型：`usage_in`(剩余，全价) + `cache_read`(命中读取，折扣) + `cache_write`(写入，加价) 三者相加 = 完整输入总量，且 `usage_in` 跨供应商可比。

本方案重新设计决策 B（前序方案 Part 2 推荐的是 B-务实"不动 usage_in"，与用户新要求相反，故本方案取代其 Part 2），并新增"线上安全落地"完整机制。

### 现状核查结论（本次重新核实，行号以当前代码为准）

**协议/路由实况**（`config/model_proxy_config.json`）：
- 活跃 strategy 仅 `cc`→claude route、`codex`→openai route。`route=nation|strategy=obs-yolo`（kimi/openai 历史 6+7 条）是历史遗留，config 已无此 strategy → **kimi 当前无活跃流量**。
- claude route 的 haiku tier 实际指向 `ds-pro-*`（DeepSeek），所以 totals 里 `ds-pro|route=claude|strategy=cc` 是 CC haiku 流量走 DeepSeek。
- supply→protocol：claude/ds/glm = **anthropic**；openai-terra/sol/luna = **responses**；kimi-k3 = **chat**。
- 转发 mode（`_TRANSLATOR_TABLE`，server.py:466-472）：claude 走 `(anthropic,anthropic)=PASSTHROUGH`（主流量）；codex/openai 走 `(responses,responses)=PASSTHROUGH`；kimi 走 `(anthropic,chat)=ANTHROPIC_TO_CHAT`。**两大活跃流量都是 PASSTHROUGH。**

**totals.json 现状**（`version:2`，本次实读）：**完全没有任何 cache 字段**，combo 只有 `usage_in/usage_out/usage_reasoning`。openai combo `usage_in=151`、kimi combo `usage_in=574`（旧语义=含命中总量），claude/ds combo `usage_in` 达 89k~4.5M（一直是剩余量语义）。→ **历史无任何 cache_read 数据可供减法回溯，openai/kimi 历史 usage_in 无法精确重算，只能时间分界处理**。

**缓存字段提取/减法的确切代码点**（本次逐处确认）：

| 场景 | mode | 取数位置（server.py） | 上游 usage 格式 | 是否需减法 |
|---|---|---|---|---|
| claude 主流量 非流式 | PASSTHROUGH | 1169 `_pu`（raw anthropic usage） | `input_tokens`(剩余)+`cache_read_input_tokens`+`cache_creation_input_tokens` | **否**（本就剩余） |
| claude 主流量 流式 | PASSTHROUGH 旁路 | 1619-1632（anthropic 分支，raw `u`） | 同上 | **否** |
| codex/openai 非流式 | PASSTHROUGH | 1169 `_pu`（raw responses usage） | `input_tokens`(含命中)+`input_tokens_details.cached_tokens` | **是** |
| codex/openai 流式 | PASSTHROUGH 旁路 | 1633-1642（responses 分支，raw `u`） | 同上 | **是** |
| kimi 非流式 | ANTHROPIC_TO_CHAT | 1206-1210（转换后 anthropic usage） | 上游 chat `prompt_tokens`(含命中)+`prompt_tokens_details.cached_tokens` | **是** |
| kimi 流式 | ANTHROPIC_TO_CHAT | 1189（`adapter.usage_tuple()`） | 同上 | **是** |
| 转换 mode（低频/历史） | ANTHROPIC_TO_RESPONSES / RESPONSES_TO_ANTHROPIC | 1221/1240、1257/1277 | 转换后 usage | 视协议 |

**关键洞察**：两大活跃流量（claude、codex）都是 PASSTHROUGH，统计取的是 **raw 上游 usage**（不经转换器）。所以缓存采集 + usage_in 减法直接吃 raw usage 即可，**不依赖转换器映射，主流量路径最简、风险最低**。转换器的缓存映射遗漏（前序 §2 提到的 1074/1235/1763 只映射 read 未映射 write、`openai_to_anthropic_response` 完全没映射 cache）只影响**转换 mode 的下游保真**，与主流量统计无关，本方案列为独立低优先项（见 Part B §5）。

**减法只对 responses/chat 两协议做**（它们 `usage_in` 含命中总量）；**anthropic 协议（claude/ds/glm）不减**（本就是剩余量，三段互斥）。这是本方案的核心区分。

## 方案设计

### 需要用户确认的一个边界（其余按派单判断，不再反问）

**转发给下游的响应是否也做减法？→ 我判断：不做，减法只在统计层。** 理由：
- 下游消费者（Claude Code/Codex）预期收到"上游原始协议语义"的 usage。Responses/chat 规范里 `input_tokens`/`prompt_tokens` 本就是**含命中的总量**，下游计费显示按此语义算。代理擅自减掉 cached，会让下游少算 token/少显示成本，属篡改上游事实——与决策 A1"保真链路服务下游、统计链路服务观测"同一逻辑。
- **PASSTHROUGH 天然不碰转发**：claude/codex 主流量是原始字节透传（server.py:1163-1180 非流式 read 后原样 `_write_buffered_response`；流式 chunked 直透），减法只发生在写入 `_acc` 的数值上，**根本碰不到转发字节**。故对主流量，"统计减法不影响转发"是代码结构天然保证的，无需额外防护。
- 用户语境紧接"数据口径"，指向统计层，派单亦给出此判断方向。**此点我按"减法只在统计层"设计，若你要连转发也统一（下游 usage 也变剩余量），告诉我，那是另一套改动（要改转换器输出 + 承担下游计费显示变化风险），我再补。**

---

### Part A：reasoning 彻底合并（A1，沿用前序清单，此处仅复述要点）

按 [[2026-07-24-usage-stats-reasoning-merge-cache-split]] Part 1 A1 执行，改动点（当前行号）：
- `core/server.py`：`_acc` 初始化（825-828）去 `"usage_reasoning": 0`；ACCESS 日志（839-842）去 `usage_reasoning`；四 mode 的 `_acc["usage_reasoning"]` 赋值（1176、1189-1190、1209-1210、1221-1222、1242-1243、1257-1258、1279-1280）删除（`adapter.usage_tuple()` 三元解包改丢弃第三位）；流式旁路（1630-1632、1642）删；`_zero_combo`（116）、`record`（181、194）、`_archive_if_needed`（222）去 `usage_reasoning`。
- `model_proxy_cli.sh cmd_stats`：`VAL_FIELDS`（401）去 `usage_reasoning`；period 行（536-537、546-551）、`print_groups`（562-563）去 reasoning。
- **保留**：`_extract_reasoning_tokens`（translate.py:1040）及各 adapter reasoning 属性/透传（547-549、695-697、1076、1237、1331、1768、1960）——A1 保真链路。
- 历史 `usage_reasoning` 键成孤儿字段，`v.get(f,0)` 天然兼容，不强制清理。

A1 与 Part B 改的是同一批文件的同几处（`_acc`/`record`/`_zero_combo`/`_archive_if_needed`/`VAL_FIELDS`），**一起改一次上线更省操作**（见"上线组织"）。

### Part B：缓存统计 + usage_in 口径统一（openai/kimi 减法对齐 claude）

#### B.1 统一提取 helper `_extract_cache_tokens`（`core/translate.py`，紧邻 1040 的 reasoning helper）

沿用前序方案 §1 的 helper 设计（三协议、`None` 表"不暴露"、`0` 表"支持且为 0"、防御 `or {}`）。关键：**helper 只提取，不做减法**；减法在 server.py 采集点做（因减法需要同时拿到 `usage_in`）。helper 返回 `{"cache_read": int|None, "cache_write": int|None}`，路径：
- anthropic：`cache_read_input_tokens` / `cache_creation_input_tokens`
- responses：`input_tokens_details.cached_tokens` / `input_tokens_details.cache_write_tokens`
- chat：`prompt_tokens_details.cached_tokens` / `prompt_tokens_details.cache_write_tokens`

#### B.2 usage_in 减法：新增统一收敛函数 `_reconcile_input`（server.py 采集点共用）

在 server.py 加一个小 helper，把"提取 cache + 按协议归一 usage_in"收敛到一处，避免四个采集点各写一遍减法逻辑（一致性风险）：

```python
def _reconcile_input(usage_in_raw, cache, protocol):
    """把上游 usage_in 归一为"剩余量"语义（三段互斥），返回 (usage_in_remain, cache_read, cache_write)。
    - anthropic: usage_in_raw 本就是剩余量,不减。
    - responses/chat: usage_in_raw 含命中,需减去 cache_read(+cache_write)。
    cache 来自 _extract_cache_tokens; None 折 0。max(0,) 兜住上游异常数据。
    """
    cr = cache.get("cache_read") or 0
    cw = cache.get("cache_write") or 0
    if protocol in ("responses", "chat"):
        usage_in = max(0, (usage_in_raw or 0) - cr - cw)
    else:  # anthropic
        usage_in = usage_in_raw or 0
    return usage_in, cr, cw
```

**protocol 来源**：采集点已知当前 supply 的 protocol（`source` 变量区分 anthropic/responses；PASSTHROUGH 的 chat 走 ANTHROPIC_TO_CHAT mode 天然可判）。若采集点手头没有直接的 protocol 变量，用 `source`（"anthropic"/"responses"）+ mode 推断，或从 `_acc` 里已存的 supply 反查 config protocol——实施时按最近可得变量取，**这是需 implementer 落实的一处细节，不影响方案成立**。

**cache_write 归属备注**：OpenAI 自动缓存通常只暴露 `cached_tokens`(read)、写入不单列（Anthropic 才有显式 `cache_creation`）。responses/chat 的 `cache_write_tokens` 是否含于 `input_tokens` 需实测；本方案保守按"含则减"处理，`max(0,)` 兜底防负数。若实测确认 responses/chat 无 cache_write 字段，则减法实际只减 cached_tokens。

#### B.3 各采集点改动（server.py）

| 采集点 | 改动 |
|---|---|
| PASSTHROUGH 非流式（1169-1176） | `_pu` 调 `_extract_cache_tokens` → `_reconcile_input(_pu_input, cache, protocol)` → 写 `usage_in/cache_read/cache_write` |
| PASSTHROUGH 流式 anthropic 旁路（1619-1632） | raw `u` 提 cache（anthropic 不减）→ 写三字段 |
| PASSTHROUGH 流式 responses 旁路（1633-1642） | raw `u` 提 cache → `_reconcile_input`(减)→ 写三字段 |
| ANTHROPIC_TO_CHAT（kimi）非流式（1206-1210） | **改从 raw `openai_resp`（1198）取 cache**（转换后 anthropic usage 已丢 chat 缓存字段）→ 减法 → 写三字段 |
| ANTHROPIC_TO_CHAT 流式（1189 adapter） | adapter 需新增 cache 属性 + `usage_tuple` 扩展，或改旁路取 raw；kimi 无活跃流量，可先记 None（n/a），不阻塞上线 |
| 转换 mode（1221/1240、1257/1277） | 低频；先从转换后 usage 取（配合 §5 补映射），或记 None。不阻塞上线 |

**累加约定**：`cache_read/cache_write` None 折 0 累加；`cache_probed` 当次 `cache_read is not None or cache_write is not None` 时 +1（区分"真 0 命中"与"不暴露缓存字段"）。

#### B.4 `_acc` / 账本 / CLI（与前序 §2-§4 一致）

- `_acc` 初始化（825-828）新增 `"cache_read":0,"cache_write":0,"cache_probed":0`。
- `_zero_combo`（116）、`record`（179-194）、`_archive_if_needed`（199-222）三处**同步**新增三字段累加（**多点一致性耦合，实施重点**）。
- `VAL_FIELDS`（cli 401）增至：`("requests","ok","fail","usage_in","usage_out","cache_read","cache_write","cache_probed")`（`usage_reasoning` 已按 A1 移除）。CLI 的 `merge_bucket_into`/`aggregate`/`zero_group` 遍历 `VAL_FIELDS`，自动生效。
- **展示**：period 行与 `print_groups` 追加 `cache_r=<k> cache_w=<k>`；`cache_probed==0` 的组显 `cache=n/a`（如 kimi、历史无数据）。

#### B.5 命中率派生指标：**默认展示（口径统一后可比）**

usage_in 统一为剩余量后，三段互斥对所有协议成立，`hit% = cache_read / (cache_read + cache_write + usage_in)` 跨供应商同口径可比。**建议默认展示，用 `cache_probed` 守卫**：`cache_probed>0` 才算并显示 `hit%=xx.x%`，`cache_probed==0` 显 `n/a`。理由:口径已统一,命中率不再误导,且它是判断"缓存是否值得用/生效"的核心指标,有决策价值。（这与前序 B-务实"不做命中率"相反——前序因 usage_in 口径不统一才不做,现已统一。）

#### B.6 转换器缓存映射补齐（独立低优先项，不阻塞主流量上线）

仅影响**转换 mode 的下游保真**（与主流量 PASSTHROUGH 统计无关）：
- `openai_to_anthropic_response`（543）补 chat `cached_tokens`/`cache_write_tokens` → anthropic 缓存字段映射（当前完全没映射）。
- `_anthropic_usage_to_responses`（1074）、`AnthropicToResponses` skeleton（1235）、`responses_to_anthropic_response`（1763）补 `cache_write`（`cache_creation_input_tokens`↔`cache_write_tokens`）映射（当前只映射了 read）。
- 附带修一个既存保真 bug：`_anthropic_usage_to_responses`（1070-1077）把 anthropic `input_tokens`(剩余)直接当 responses `input_tokens`，且 `total_tokens=in+out` 丢了 cached——反向转换后 responses 的 input_tokens 偏小。若要保真，应把 cached 加回使其符合 responses"含命中"语义。**此项与统计层减法方向相反（转发要加回、统计要减掉），务必分清，避免实施时混淆。**

**建议**：Part B 主体（B.1-B.5，覆盖 claude/codex 活跃流量）先上；B.6 转换器保真作为后续独立小改动，避免一次改动面过大。

### 历史数据分界处理（核心兼容）

事实：openai/kimi 历史 `usage_in` 是旧语义(含命中)，无历史 cache_read 可重算，**精确迁移不可能**。且新请求 `record` 会把新剩余量累加到同 combo 的旧总量上 → 混合垃圾。三档方案：

- **推荐（个人工具，量极小）**：上线前 ① `cp .claude_model_proxy_totals.json .claude_model_proxy_totals.json.pre-cache-split.20260724`（只读归档，可查）；② 活跃账本里**仅把 openai/kimi 两个历史 combo 的 `usage_in` 就地清零**（`total` + `days` 桶各一处，共几处），其余字段（requests/ok/fail/usage_out）保留；③ `_load` 默认结构升 `version:3` 并加 `"cache_split_since":"2026-07-24"` 元字段留痕。理由：`usage_in` 是唯一语义变的字段，清零后新数据从 0 累加剩余量，口径干净；requests 等未变语义字段保留，请求数不失真。副作用：openai/kimi 这俩 combo 的 `usage_in` 短期与其 requests 数不完全对应（上线前请求的 usage_in 被清），但历史量仅 151+574 token、13 请求，无观测损失，且副本可查。
- **claude/ds combo 完全不动**：其 `usage_in` 一直是剩余量语义，本次不变，历史连续累加。**主流量（占 totals 99%+ usage_in）零迁移、零风险**——这是分界方案影响面极小的根本原因。
- 备选（若你很在意历史严谨）：中方案=把 openai/kimi 历史 combo 整条挪进 `legacy_precache` 归档区块（CLI 不聚合它）；重方案=整份账本 checkpoint 归档后新建空账本重开（丢上线前全部 total 视图）。二者复杂度更高，个人工具不推荐，列出供选。

### 线上安全落地机制

model_proxy 启动机制（本次核实 `model_proxy_cli.sh` `cmd_on`/`cmd_off`）：`nohup python3 model_proxy.py &` 后台常驻，监听 18889，**无 PID 文件**（靠 `lsof` 端口探测），非 systemd/launchd。重启 = `off` + `on`。代码在 vault 的 git repo 内。

#### 1. 副本开发测试（用户提的"文件副本"思路：可行，但精确做法是"整目录副本 + 独立端口 + 独立账本"）

用户思路方向正确，但"只复制要改的文件"有坑：改动跨 `core/server.py`+`core/translate.py`+`model_proxy_cli.sh`，且测试实例若跑起来会读写 `.claude_model_proxy_totals.json`——**若副本仍指向线上账本会污染线上数据**。精确步骤：

```bash
# 1. 整目录副本到临时区(与线上物理隔离)
cp -R /Users/vincentwang/Documents/NoteVault/tools/model_proxy /tmp/model_proxy_dev
# 2. 副本用独立账本(别碰线上): 删副本里的 totals, 让它自建空账本
rm -f /tmp/model_proxy_dev/.claude_model_proxy_totals.json
# 3. 在副本里开发改动(server.py/translate.py/cli)
# 4. 跑单测(见下节)——纯离线, 不起服务, 不发请求, 最安全
cd /tmp/model_proxy_dev && python3 -m pytest tests/ -q
# 5. (可选)起测试实例, 独立端口 18899, 不影响线上 18889
MODEL_PROXY_PORT=18899 MODEL_PROXY_CONFIG=/tmp/model_proxy_dev/config/model_proxy_config.json \
  nohup python3 /tmp/model_proxy_dev/model_proxy.py >> /tmp/model_proxy_dev/.log 2>&1 &
#    对 18899 发少量真实/构造请求验证, 验完 kill 该实例
# 6. 验证通过后, 把改动应用回线上目录(用 git 管理, 见回滚), off+on 重启线上
```

**要点**：单测(步骤4)是主要验证手段,纯离线零风险零成本;测试实例(步骤5)仅在需要端到端验证时起,务必独立端口+独立账本+独立 config 路径。

#### 2. 回滚方案(git + env 开关双保险)

- **代码回滚(git)**:改动**前**先 `git add -A && git commit`(基线快照)。出问题 `git checkout -- tools/model_proxy/core/server.py tools/model_proxy/core/translate.py tools/model_proxy/model_proxy_cli.sh` 回退,`off`+`on` 重启。git 本身就是文件版本管理,无需手工留 `.bak`。
- **数据回滚**:上线前的 `.pre-cache-split.20260724` 账本副本。若新逻辑写坏了 totals(如负数/异常大),`cp` 副本覆盖回去 + 重启。
- **功能开关(env)**:见下,出问题可不改代码、不 git 回退,直接关开关重启退回旧统计。

#### 3. 功能开关:**建议做**(复用现成 env 模式,成本极低)

复用现有 `MODEL_PROXY_REASONING_DEBUG`(server.py:236)的 env 开关模式,新增 `MODEL_PROXY_CACHE_STATS`(默认 `on`)。`off` 时:`_extract_cache_tokens` 不采集、`_reconcile_input` 不减法(usage_in 走旧逻辑)、cache 三字段记 0。开关**只影响今后新请求的采集**,已写坏的数据仍需账本副本回滚。

评估:个人工具本可省,但"线上运行中"约束下,env 开关让"新逻辑算出异常值"时**无需 git 回退、无需改代码,只 `export MODEL_PROXY_CACHE_STATS=off` + 重启**即回退到旧统计口径(usage_in 记原始总量),成本约 10 行、复用成熟模式,值得做。**注意**:开关只守 B(缓存/减法),A1(删 reasoning)是纯删除不设开关(无回退价值)。

#### 4. 测试验证(优先 mock 单测,再真实抽验)

- **mock 单测(主)**:扩展现有 `tests/test_translate.py`(测 `_extract_cache_tokens`)、`tests/test_usage_totals.py`(测账本三字段累加)、`tests/test_passthrough_sniff.py`(测旁路减法)。构造四类供应商真实 usage json:
  - claude(anthropic):`{"input_tokens":100,"cache_read_input_tokens":900,"cache_creation_input_tokens":50,"output_tokens":..}` → 断言 usage_in=100(**不减**), cache_read=900, cache_write=50, cache_probed+1。
  - openai(responses):`{"input_tokens":1000,"input_tokens_details":{"cached_tokens":800},"output_tokens":..}` → 断言 usage_in=200(**1000-800**), cache_read=800, cache_probed+1。
  - kimi(chat):`{"prompt_tokens":1000,"prompt_tokens_details":{"cached_tokens":600},...}` → usage_in=400, cache_read=600。
  - ds(anthropic 网关):同 claude 格式;若上游不填缓存字段 → cache_read/write=None → cache_probed 不+1 → n/a。
  - 边界:cached>input_tokens 的异常数据 → usage_in=max(0,..)=0 不为负。
- **真实抽验(次)**:claude(构造 >1024 token 重复前缀触发缓存写+读)与 codex(openai) 各发一条真实请求(这俩是活跃流量),核对 totals 对应 combo 三字段合理、`usage_in+cache_read+cache_write ≈ 完整输入`。ds 顺带核实上游是否真回 anthropic 缓存字段(决定显数值还是 n/a)。kimi 无活跃流量,mock 即可,不必真实发。
- **命中率抽验**:claude 命中场景 stats 显 `hit%` 合理;kimi/无数据组显 `n/a`。

### 上线组织(A1 + B 一次还是两次)

**推荐:一次上线,B 的减法用 env 开关守住。** 理由:A1 与 B 改的是同一批文件的同几处(`_acc`/`record`/`_zero_combo`/`_archive_if_needed`/`VAL_FIELDS`),分两次要动两遍同一批文件、两次重启、两次账本兼容处理,反增操作面。风险差异:A1 是纯删除(低风险),B 的减法是唯一有"算错方向致负/异常"风险的部分——用 `MODEL_PROXY_CACHE_STATS` 开关把 B 单独兜住,出问题关 B 不影响 A1。

**若求极稳可分两步**:先上 A1(验证账本减字段兼容、CLI 无 reasoning),再上 B(带减法+历史分界)。列为备选。

## 风险与权衡

- **减法方向错致负/异常(B 最大风险)**:`_reconcile_input` 只对 responses/chat 减、anthropic 不减,`max(0,)` 兜底。用 mock 单测覆盖四协议 + 越界数据,env 开关兜底。**实施务必派 reviewer 或 `/code-review` 专项核对"哪些协议减、哪些不减"的分支正确性**。
- **多点一致性耦合(实施风险)**:A1 删 `usage_reasoning` + B 加三字段,需在 `_acc`/`_zero_combo`/`record`/`_archive_if_needed`/CLI `VAL_FIELDS` **五处同步**,漏一处→漏累加/KeyError。CLI 侧遍历 `VAL_FIELDS` 自动生效,server 侧三处手写逐字段——**派 implementer(非 runner),交付后 reviewer 核对五处字段集一致**。
- **历史 openai/kimi usage_in 不可精确迁移(真实限制,如实说明)**:无历史 cache_read 可重算,只能时间分界+清零。影响量极小(151+574 token)。claude/ds 主流量语义不变、零迁移。
- **cache_write 于 responses/chat 是否含于 input_tokens 待实测**:OpenAI 自动缓存多数只暴露 cached_tokens(read),写入不单列。保守按含处理+`max(0,)`。实测后若确认无 write 字段,减法实际只减 cached。
- **ds/glm 缓存字段待实测**:anthropic 网关是否透传 `cache_read_input_tokens`/`cache_creation_input_tokens` 决定 ds/glm 显数值还是 n/a。`cache_probed` 机制天然兜住(不填即 n/a)。真实抽验时确认。
- **转发不受影响(结构保证)**:主流量 PASSTHROUGH 原样透传字节,统计减法只改 `_acc` 数值,碰不到转发。此为代码结构天然保证,非额外防护。
- **重启必需**:改动上线需 `off`+`on` 重启进程才让新 `_zero_combo`/`record`/减法生效(旧进程内存 dict 结构不变)。重启瞬间(约数秒)代理不可用,Claude Code/Codex 会短暂失败——建议选空闲时段重启,或接受一次重试。
- **迁移代价提示(理想路径)**:本方案走 B-理想式口径统一,代价即上述"历史 usage_in 语义变更 + 分界清零 + 需实测多个上游缓存字段填充实况"。用户已明确要口径一致,故不因代价改设计,此处仅供知情。
- **前序文档处置(建议,用户确认后再改)**:本方案取代 [[2026-07-24-usage-stats-reasoning-merge-cache-split]] 的 **Part 2**(其推 B-务实,本方案改 B-理想式减法);其 **Part 1 A1 仍有效、本方案沿用**。建议该文档 status→superseded 并注明"Part 1 A1 由本方案继承、Part 2 被本方案取代"。[[2026-07-24-usage-stats-reasoning-column-converge]] 保持 superseded。**status 改动请用户确认后再由 runner/architect 执行,本方案不擅改。**

## 验证方式

1. **单测(离线,主)**:`cd /tmp/model_proxy_dev && python3 -m pytest tests/ -q` 全绿;新增用例覆盖四协议 `_extract_cache_tokens` + `_reconcile_input`(claude 不减/openai kimi 减/越界 max0)+ 账本三字段累加。
2. **A1 回归**:`bash model_proxy_cli.sh stats`(及 today/month/维度/过滤)输出**无任何 reasoning 字样**;发经 responses/chat 的请求,下游收到的转换响应**仍带** `output_tokens_details.reasoning_tokens`(A1 保真未退)。
3. **B 减法(真实抽验)**:codex 发一条含缓存命中的请求 → totals 对应 combo `usage_in` 显著小于原始 `input_tokens`,且 `usage_in+cache_read+cache_write ≈ 上游 input_tokens`;claude 发含缓存写+读请求 → cache_read/cache_write 非 0、usage_in 不含缓存(与请求前后 usage_in 增量核对)。
4. **N-A 区分**:claude 未命中显 `cache_r=0`(cache_probed>0,真 0);kimi/无数据组显 `cache=n/a`(cache_probed=0)。
5. **命中率**:claude 命中场景 stats 显合理 `hit%`;无缓存组显 `n/a`。
6. **历史分界**:上线后 `stats total` 里 claude/ds combo `usage_in` 连续(未被动);openai/kimi combo `usage_in` 从分界后重新累加剩余量;`.pre-cache-split.20260724` 副本存在可查;账本 `version:3`、含 `cache_split_since`。
7. **功能开关**:`export MODEL_PROXY_CACHE_STATS=off` 重启后,新请求 usage_in 记原始总量(不减)、cache 三字段记 0——验证可退回旧口径。
8. **回滚演练**:`git checkout` 三文件 + off/on,`stats` 正常运行(旧逻辑),证明回滚路径通。
9. **一致性**:`_zero_combo`/`record`/`_archive_if_needed`/`_acc`/CLI `VAL_FIELDS` 五处字段集一致(无 KeyError、无漏累加)。

## 关联

- 取代(Part 2):[[2026-07-24-usage-stats-reasoning-merge-cache-split]](Part 1 A1 由本方案继承,Part 2 B-务实被本方案 B-理想式减法取代;建议 status→superseded)
- 更早废弃:[[2026-07-24-usage-stats-reasoning-column-converge]](保持 superseded)
- 前序保真成果(继承):[[2026-07-23-usage-reasoning-extraction-unify]]、账本 schema [[2026-07-23-usage-totals-ledger]]、增量写 [[2026-07-23-usage-totals-incremental-write]]
- 代码:[[core/server.py]] `_acc`(825-828)、`_reconcile_input`(拟新增)、`UsageTotalsStore`(`_zero_combo` 116 / `record` 168-197 / `_archive_if_needed` 199-222)、四 mode(1160-1284)、流式旁路 `_sniff_passthrough_usage`(1615-1642)、`_TRANSLATOR_TABLE`(466-472)、env 开关模式(236)
- 提取 helper:[[tools/model_proxy/core/translate.py]] `_extract_reasoning_tokens`(1040,保留)、拟新增 `_extract_cache_tokens`;缓存映射点(1063-1078、1235、1763-1768、543)
- 展示:[[model_proxy_cli.sh]] `cmd_stats`(388-588,`VAL_FIELDS` 401、period 行 536-551、`print_groups` 556-563)、`cmd_on/cmd_off`(327-378)、`print_help`(18-67)
- 配置/账本:`config/model_proxy_config.json`、`.claude_model_proxy_totals.json`(拟升 version:3 + cache_split_since)
- 测试:`tests/test_translate.py`、`tests/test_usage_totals.py`、`tests/test_passthrough_sniff.py`;样本 `samples/*.txt`
- README:`tools/model_proxy/README.md`(stats 段 + 缓存列/命中率/env 开关说明)
