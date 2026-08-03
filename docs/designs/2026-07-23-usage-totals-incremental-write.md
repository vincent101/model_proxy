---
created: 2026-07-23 21:23:29
type: design-decision
date: 2026-07-23
status: draft
target: "[[core/server.py]]"
tags: [architect, model_proxy, performance, persistence]
---

# 累计账本每请求全量重写：量化评估与增量写方案（务实）

> 针对 [[2026-07-23-model-proxy-full-audit]] M2 项。结论先行：**按实测数据，当前全量重写在整个
> 现实数据生命周期内对个人工具都够用，本质是一个收益存疑的性能优化。推荐 A（不改）。** 若用户在意
> SSD 写放大或预期显著提升用量，备选 B（增量热/冷双文件）给出了完整可落地设计。

## 背景与问题

`UsageTotalsStore.record()`（`core/server.py:168-197`）每次请求结束在锁内做全量落盘：

```python
self._archive_if_needed()
_atomic_write_json(self._path, self._data)   # L197：把整个 data（total+months_archive+全部days桶）dump
```

`_atomic_write_json`（L91-106）= `mkstemp` + `json.dump(indent=2)` + `os.replace`，整文件替换。账本按天分桶、
`KEEP_DAYS=400` 天明细窗口，随运行时间增长天桶累积。**实际每请求只改动「今天」这一个天桶 + `total` 的少量
字段**（`record` L183 的 `for bucket in (day_bucket, total_bucket)` 只碰这两个桶），却每次重写整个文件。

`record` 挂在 `_forward_logged` 的 `finally`（L843-846），**响应已写完之后**执行，异常被 try 兜住不冒泡。

## 量化评估（本机实测，非估算）

实测手法：构造不同规模账本，各测 `mkstemp+json.dump+os.replace` 全流程 50 次取均值（本机 macOS/SSD）。
combos 取实测真实量级 **10 个/桶**（方案文档 §1 已证实际组合数个位数~十几，远小于笛卡尔积 170）。

| 规模 | 天桶数 | 归档月桶 | 文件大小(indent=2) | 单次原子写 |
|---|---|---|---|---|
| **当前实测** | 1 | 0 | 1.7 KB（14 请求/3 combo） | ~0.3 ms |
| 1 年满窗 | 365 | 12 | **954 KB** | **17.7 ms** |
| 400 天满窗（+2 年归档） | 400 | 24 | **1.07 MB** | **~20 ms** |
| 400 天满窗（+10 年归档、combo 翻倍） | 400 | 120 | **2.56 MB** | **~46 ms** |
| 极端：每天笛卡尔 170 组合（不会发生） | 400 | 120 | 21 MB | 386 ms |

**校正原账本方案的体积估算**：原文档估「400 天 ≈ 0.4~0.7 MB」偏低约一半。实测每天桶（10 combo、indent=2
缩进 + 约 60 字符组合键字符串）约 **2.6 KB**，400 桶就 **~1 MB**。原估算按「单桶 1~2 KB」偏保守。

**增长是否有界**（关键判断）：
- `days`：`_archive_if_needed`（L199-222）`while len(days) > KEEP_DAYS` 封死在 ≤400 桶 ≈ **1 MB 硬上限**。
- `months_archive`：**永久保留**，每月 +1 桶（约 2.6 KB）。**缓慢增长但速率极低**——1 年 +~30 KB，10 年
  +~300 KB，100 年才到 ~5~7 MB。对个人工具等同「有效有界」。
- `total`：恒定 1 桶。

**严重度诚实评估——这更接近过度设计的优化，不是必须修的隐患：**

1. **无用户可见延迟**：`record` 在响应写完后的 `finally` 里跑，20~46 ms 全落在后台收口，客户端零感知。
2. **锁串行化几乎不发生**：`record` 持锁做全量 dump 会串行化并发 `record`，但本工具 **QPS≪1**、请求间隔常达
   数十秒，并发 `record` 实际不出现。
3. **SSD 写放大可忽略**：1 年后每请求重写 ~1 MB。即便每天 100 请求 = 100 MB/天 = **36 GB/年**；SSD 典型
   TBW 数百 TB，按 150 TB 算需 **~4000 年**写坏。物理损耗无实际意义。
4. **`KEEP_DAYS=400` 已封死主要增长源**（`days`），`months_archive` 百年才几 MB。

即：全量重写理论上是 `O(账本大小)` 每请求，但在本工具的**整个现实数据生命周期内**稳定在毫秒~几十毫秒的
**后台**开销，不构成任何用户可感知问题。审查报告说它「最先出问题」是相对其它模块的排序，绝对值上并不是问题。

## 方案设计（对比与推荐）

### 推荐：A. 不改（如实采纳「够用」结论）

不动代码。理由即上「严重度评估」四条：无可见延迟、无并发压力、写放大可忽略、增长有效有界。这是符合务实
原则的合法结论——不为「理论上更优」引入不必要复杂度。

代价：账本变大后（1 年 ~20 ms、10 年 ~46 ms）每请求后台仍全量重写，SSD 有一份「无意义但无害」的写放大。

### 备选：B. 增量热/冷双文件（若在意写放大 / 预期扩容）

把「每请求变的热数据」与「只在跨天/归档时变的冷数据」拆成两个物理文件，热文件每请求写、冷文件极少写。

- **热文件** `.claude_model_proxy_totals.hot.json`：只放 `total` + **今天这一个天桶**。每请求写它，
  体积恒定 ~5 KB、**~0.46 ms**（实测）。`total` 是独立累加量（每请求 +1，不依赖冷数据），放热文件自洽。
- **冷文件** `.claude_model_proxy_totals.cold.json`：放 **历史天桶（不含今天）+ `months_archive`**。
  仅在两种时机写：① 跨天（今天 key 变了，把昨天桶移入冷文件的 `days`）；② 归档（冷 `days` 超窗汇总进
  `months_archive`）。低 QPS 下每天至多写一次全冷文件。
- **读取（`stats`）**：合并 hot+cold——`total` 取热文件；某天查先看热文件今天桶、否则冷文件 `days`；某月
  查合冷文件 `months_archive` 或汇总冷 `days`；全历史取热文件 `total`。

**跨天迁移时序（幂等，防双算）**：新一天首个请求发现 `today_key != hot.day_key` →
① 先把 hot 里的旧天桶**赋值覆盖**写入 `cold.days[oldkey]`（搬运非累加，故重复迁移幂等）→
② 落盘冷文件（顺带 `_archive_if_needed`）→ ③ 成功后 hot 重置为新天桶 + 保留累加后的 `total` → 落盘热文件。
若冷写成功、热写失败：下次重试仍用「覆盖」写冷，`cold.days[oldkey]` 不会被累加两次，安全。

**收益**：每请求写盘 20~46 ms → **~0.46 ms**（40~100 倍），SSD 写放大同比例消除。仍保持「每请求同步落盘」
的崩溃一致性（不丢已完成请求）。

**代价（务实提示，供知情决策）**：
- 数据模型从单文件拆双文件，`record` 加跨天迁移分支，`_archive_if_needed` 迁到冷文件路径。
- `model_proxy_cli.sh` 的 `cmd_stats`（现读单文件，见 [[2026-07-23-usage-totals-ledger]] §3）要改为**读合并
  两文件**，投影/过滤逻辑不变但数据源要拼。
- `test_usage_totals.py`（519 行）现有断言基于单文件结构，需改造 + 补跨天迁移幂等、双文件读合并、崩溃在
  两文件间半迁移的一致性单测。
- 首次运行需从旧单文件 `.claude_model_proxy_totals.json` 迁移一次（或直接空起步，账本无强历史包袱）。
- 实现复杂度中等，正确性耦合点是「跨天迁移时序 + 双文件读合并」，属需谨慎处，落地应派 implementer + reviewer。

### 否决：C. 攒批 / 定时 flush

每 N 请求或每 N 秒 flush。**否决**：① 崩溃（kill -9）丢最后一批未落盘请求，破坏当前「每请求同步落盘、
崩溃最多丢当前未完成请求」的一致性优点（原方案 §6 的核心保证）；② **QPS≪1 时请求间隔常 > flush 周期，
定时 flush 近乎每请求触发一次，收益为 0**——攒批只对高 QPS 有意义，非本工具场景。

### 否决：D. 紧凑 JSON（去 indent）

`json.dump(separators=(',',':'))` 去缩进。**实测收益差**：满窗 1.07 MB → 672 KB（体积 -37%），但耗时
23 ms → 20 ms（**仅 -15%**）——瓶颈在 `mkstemp`/`os.replace` 系统调用而非序列化，去缩进省不到时间。且
牺牲账本可读性（原方案 §2(c) 明确把「打开文件即可人读 combos」列为设计目标）。性价比低，不做。

### 否决：E. 脏标记 + 定时后台 flush

同 C 的收益陷阱：低 QPS 下请求间隔 > flush 周期，等于没减少写盘次数。仅高 QPS 有意义，否决。

## 风险与权衡

- **推荐 A 的风险**：无代码风险；唯一「代价」是可忽略的后台开销与写放大，已量化证明无实际影响。
- **选 B 的风险**：正确性耦合在跨天迁移与双文件读合并，漏改一处会双算或读漏；`cmd_stats` 与全部账本单测
  需连带改。属「有耦合改动」，必须 architect 出细化实施方案后派 implementer + reviewer，不能当机械改动铺。
- **需用户决策**：本质是「要不要为一个不影响使用的后台开销，投入中等改造成本」。数据支持不改；改与否取决于
  用户对写放大的洁癖程度与未来是否会显著抬高用量（如把本地代理改成多用户/高频服务——但那已超出个人工具
  定位，届时该重新评估是否上真正的存储引擎，而非现在预投资）。
- **不建议现在改的额外理由**：账本方案（[[2026-07-23-usage-totals-ledger]]）与配套 `stats`、388 项测试刚
  落地稳定，B 会连带改动 `cmd_stats` 和大量单测，在无实际性能痛点时引入回归面，不划算。

## 验证方式

- **评估复现（不改代码即可核对本文数据）**：
  ```bash
  # 观察当前账本实际大小与增长
  ls -la tools/model_proxy/.claude_model_proxy_totals.json
  python3 -c "import json,os;p='tools/model_proxy/.claude_model_proxy_totals.json';d=json.load(open(p));print('bytes',os.path.getsize(p),'days',len(d['days']),'archive',len(d['months_archive']),'total_req',d['total']['requests'])"
  ```
  用本文「量化评估」节的构造脚本可复现满窗 1 MB / ~20 ms、10 年 2.5 MB / ~46 ms。
- **若采纳 B，验收点**：① 连发跨零点两天请求 → 昨天桶落冷文件、今天桶在热文件、`total` 连续累加；
  ② 跨天迁移中途 kill -9 再启动 → 无双算（`stats` 该天数值 = 手工累加，冷 `days[oldkey]` 只一份）；
  ③ `stats today/某天/某月/全历史/各维度投影过滤` 输出与改造前单文件版逐项一致；④ `KEEP_DAYS` 调小造归档
  → 冷文件归档不丢不重；⑤ 全部账本单测改造后绿 + 新增跨天迁移幂等/双文件读合并/半迁移一致性单测；
  ⑥ 每请求热文件写盘实测回落到亚毫秒级。

## 关联

- 前序审查：[[2026-07-23-model-proxy-full-audit]]（M2 项，本文是对其的量化落地评估）
- 账本原始设计：[[2026-07-23-usage-totals-ledger]]（单文件结构、`record`/归档/`stats` 现状，B 方案在其上拆分）
- [[core/server.py]] `UsageTotalsStore`（119-222）/ `record`（168-197）/ `_atomic_write_json`（91-106）/
  `_archive_if_needed`（199-222）/ `_forward_logged` finally（834-846）
- [[_config_ops.py]] `atomic_write`（42-58，原子写模式来源）
