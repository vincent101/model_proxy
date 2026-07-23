---
type: design-decision
date: 2026-07-23
status: draft
target: "[[core/server.py]]"
tags: [architect, model_proxy, token-usage, persistence]
---

# model_proxy 累计用量账本（独立于日志截断）

> 修订 v2（2026-07-23）：粒度由「按月」改为「按天 + 天/月两种汇总」；不再新增 `totals` 命令，
> **改为重写现有 `stats`**（账本为主数据源 + 日志补 max 耗时）；时区明确按 **UTC+8 固定偏移**切天/月
> 边界。
> 修订 v3（2026-07-23）：分组结构由「三套独立预聚合桶（by_supply/by_route/by_strategy）」改为
> **单一 `combos` 字典，key 为 supply×route×strategy 组合键**，支持任意维度组合切片；`stats` 查询改为
> 对组合键做「投影+聚合」。按天分桶 / 400 天窗口+月归档 / UTC+8 / 原子写+锁 三项机制沿用不变。

## 背景与问题

现有 access 日志（见 [[2026-07-22-access-log-and-latency]]）每请求写一行 `ACCESS ...` 到
`.claude_model_proxy.log`，`stats` 命令现场 awk 聚合日志得出 count/avg ms/max ms/按 supply 成功率
与累计 token。

**问题：日志保留窗口与统计可信历史窗口被耦合死。** `_trim_log`（`core/server.py:41`）在**进程启动时**
把日志截断到最后 5000 行。日志一旦被截，早期 ACCESS 行永久丢失，`stats` 也就永久算不出被截段的
token/请求量。用户无法回答「7 月总共 / 7 月 23 日当天花了多少 token」这类长期问题。

**目标：新增一个独立累计账本文件，只增不截、不受日志截断与进程重启影响；支持按天/按月 × 任意维度
组合（supply/route/strategy 及其组合）汇总。** 务实路径，个人工具，不上 sqlite。

## 方案设计

务实路径。改动集中在 `core/server.py`（新增账本存储 + `_forward` 补 strategy 字段 + finally 调用）、
`model_proxy_cli.sh`（**重写** `cmd_stats`）、`.gitignore`（忽略账本文件）。

### 0. 时区：UTC+8 固定偏移（明确，无歧义）

天/月边界一律按 **UTC+8** 划分，**不依赖系统时区、不用 naive `datetime.now()`**。

**用固定偏移 `timezone(timedelta(hours=8))`，不用 `zoneinfo`。** 理由：项目「仅标准库」约束下，
`zoneinfo.ZoneInfo("Asia/Shanghai")` 在部分环境（如 macOS / 精简 Linux）需系统 tz 数据库或第三方
`tzdata` 包，缺失即抛 `ZoneInfoNotFoundError`；而中国全年无夏令时，UTC+8 是**恒定偏移**，固定
offset 表达完全准确且零依赖、无运行环境时区漂移风险。写法（server.py L0 基座区，与 cli 各一处）：

```python
from datetime import datetime, timezone, timedelta
_CST = timezone(timedelta(hours=8))          # UTC+8，中国标准时间，固定偏移
def _cst_now() -> datetime:
    return datetime.now(_CST)                 # 显式带时区，绝不用 naive datetime.now()
# 天 key：_cst_now().strftime("%Y-%m-%d")；月 key：前者取前 7 位 "%Y-%m"
```

cli 侧（python3 内联）同法：`datetime.now(timezone(timedelta(hours=8)))`。这样无论进程宿主系统时区
设成什么，天/月边界永远按东 8 区算。

### 1. 数据模型：按天分桶，桶内用 supply×route×strategy 组合键（重算量级）

**每天桶内改为「supply×route×strategy 组合键 → 累加值」的单一 `combos` 字典**（不再存三套独立
预聚合，见 §2、§4）。组合键实际基数（**已读 `config/model_proxy_config.json` 实测**）：

- supplies **17**、routes **5**、strategies **2**。笛卡尔积理论上限 17×5×2 = **170**。
- **实际约束极强**：strategy 仅 2 个、各绑死 1 个 route（`cc→claude`、`codex→openai`），故实际只出现
  2 个 (strategy, route) 对。`claude` route 挂 4 个候选 supply、`openai` route 挂 3 个。
- **实际每天出现的组合数 ≈ cc×claude×{≤4 supply} + codex×openai×{≤3 supply} ≈ 最多 7 个**，
  加早退 `(none)` 边缘约 8~10 个/天。**实际基数是个位数~十几，远小于笛卡尔积 170。**

单天桶按 ~10 个组合键、每键约 6 个整数估，序列化后约 **1~2 KB**（与 v2 三套预聚合量级相当——因实际
组合数少，组合键并未把桶显著撑大）。文件增长量级重算：

| 时间跨度 | 天桶数 | 账本文件大小（估） |
|---|---|---|
| 1 年 | 365 | ~0.4~0.7 MB |
| 3 年 | ~1100 | ~1~2 MB |
| 10 年 | ~3650 | ~4~7 MB |

**结论：组合键量级成立、增长有界。** 仍加**明细保留窗口 + 月度归档**封死增长：

- **明细天桶只保留最近 N 天**（`KEEP_DAYS = 400`，覆盖一年多，够按天回溯）。
- 落盘时超窗旧天桶**先按组合键汇总进 `months_archive` 月归档节点（同为 `combos` 结构）再从 `days`
  删除**。`months_archive` 以 `YYYY-MM` 为 key、**永久保留**（每月 1 条、十年 120 条，KB 级）。
- 另存 `total`（全历史组合键汇总）作总账与交叉校验锚点。

**日粒度明细有界（≤N 天）、月归档缓增、总账恒定 1 条**。按天查读 `days[该天].combos`；按月查先读
`months_archive[该月].combos`，未归档则汇总该月各天桶 `combos`——两来源二选一，不双算。

文件：`tools/model_proxy/.claude_model_proxy_totals.json`（与 `LOG_FILE` 同目录）。结构：

```json
{
  "version": 2,
  "since": "2026-07-23",
  "keep_days": 400,
  "total":          { "requests":0,"ok":0,"fail":0,"sum_ms":0, "combos": {"...": {...}} },
  "months_archive": { "2025-01": { "requests":0,"ok":0,"fail":0,"sum_ms":0, "combos": {"...": {...}} } },
  "days": {
    "2026-07-23": {
      "requests":0, "ok":0, "fail":0, "sum_ms":0,
      "combos": {
        "supply=claude-sonnet-sankuai-0956|route=claude|strategy=cc":
            { "requests":0,"ok":0,"fail":0,"usage_in":0,"usage_out":0,"usage_reasoning":0 },
        "supply=openai-sol-sankuai-0956|route=openai|strategy=codex":
            { "requests":0,"ok":0,"fail":0,"usage_in":0,"usage_out":0,"usage_reasoning":0 }
      }
    }
  }
}
```

**组合键格式：扁平拼接字符串 `supply=<s>|route=<r>|strategy=<t>`，放在 `combos` 扁平 dict 里。**
每段 `字段=值` 自描述、分隔符 `|`（三个值均为 config 内标识符，实测不含 `|`/`=`，无歧义）。字段顺序
固定 supply→route→strategy，便于查询侧 `split("|")` 再 `split("=",1)` 解析。空维度值统一填 `(none)`
（如早退未匹配 route/strategy → `supply=(none)|route=(none)|strategy=cc`）。

字段口径：桶顶层 `requests/ok/fail/sum_ms` 是该桶总计（免遍历 combos 求总，也作交叉校验）；`combos`
每键 value 同结构累加 `requests/ok/fail/usage_in/out/reasoning`；`ok`/`fail` 按 `status==200` 分；
`sum_ms` 只存桶顶层（组合键粒度 avg 意义弱，combos 内不存 sum_ms，减字段）；**max 不存**，由日志补（§3）。

### 2. 为何选扁平组合键（而非三层嵌套 / 而非细粒度条目）

三种候选：

| 方案 | 写入（命中+累加） | 单维度汇总查询 | JSON 可读性 | 增长 |
|---|---|---|---|---|
| **A. 扁平组合键**（`combos[{key}]`，推荐） | 拼一次 key、dict 直取累加，O(1) | 遍历 combos，`split` 出目标维度值匹配即累加，O(组合数≈10) | 每行一个组合，key 自描述，一眼看懂 | O(实际组合数)，个位数~十几，有界 |
| B. 三层嵌套 `{supply:{route:{strategy:{...}}}}` | 三层 `setdefault` 逐层下钻累加，O(1) 但代码啰嗦 | 单维度汇总要按维度决定遍历哪几层（看 supply 要跨所有 route/strategy 叶子递归求和），**不同维度遍历路径不同、逻辑不对称** | 深嵌套，人读要层层展开 | 同 A |
| C. 细粒度原始条目（每请求 append 一条） | append 一条 | 遍历全部条目 group by | 无法阅读 | **每请求一条 = 无界，重蹈日志覆辙** |

**推荐 A（扁平组合键）。** 逐条对上用户三个考量：
- **(a) 写入**：拼一个 key 字符串、`combos.setdefault(key, 零值).累加`，O(1)，比 B 的三层下钻代码更简。
- **(b) 任意单维度/多维度汇总**：遍历 `combos`（每桶仅约 10 条），对每个 key `split` 出三段维度值，
  按查询条件（如 supply==X 不限 route/strategy）匹配则把该 value 累加进结果——**同一套投影逻辑覆盖
  任意维度/任意维度组合**（见 §3），无需像 B 那样每个维度写不同遍历路径。这正是「支持任意维度组合
  切片」诉求的最直接落地：所有切片都是「组合键上按条件过滤后聚合」的统一操作。
- **(c) 可读性**：扁平 dict 每行一个自描述组合键，用户直接打开文件即可看懂「哪个 supply 在哪个 route
  哪个 strategy 下用了多少」，胜过深嵌套。
- 增长：与实际组合数绑定（个位数~十几），非请求数，有界。**否决 C**（无界，违背设计初衷；细粒度「谁
  何时用多少」需求 access 日志已覆盖）。**否决 B**（查询逻辑按维度分裂、嵌套难读，无收益）。

**维度取值来源（已核实 `_acc` 现状，不凑合用 token 尾4）：**
- supply 段 = `_acc["supply"]`（现已有，`server.py:784`）。
- route 段 = `_acc["route"]`（现已有，存的就是 `route.get("id")` 原始 route_id，`server.py:728`）。
- strategy 段 = **需新增** `_acc["strategy"]`。现状 `_acc` 里**没有** strategy 标识，只有 `_acc["token"]`
  =token 尾4位（`server.py:702`，仅供日志人工对齐，**不可**作维度）。strategy 稳定明文标识是
  **`client_token`**（config `strategies[].client_token`，如 `"cc"`/`"codex"`，非密钥尾4）。
  **在 `_forward` 的 `resolve_strategy` 之后（`server.py:720` 后）加一行**：
  ```python
  self._acc["strategy"] = strategy.get("client_token", "") if strategy else ""
  ```
  并在 `_forward_logged` 初始化 `_acc`（`server.py:669`）加默认 `"strategy": ""`。空维度值统一归
  `(none)`。

> 说明：`_acc["strategy"]` 仅供账本组合键，**不改 ACCESS 日志行格式**（避免影响已 implemented 的
> access-log 方案与其 awk）。

### 4. 是否仍保留三套单维度预聚合：不保留，只存组合键（单一数据源）

**结论：不再维护 by_supply/by_route/by_strategy 三套单维度预聚合，只存组合键 `combos`。** 任意单维度
汇总在查询时从组合键投影聚合得出。

权衡（个人工具、查询频率极低，非高频 dashboard）：
- 保留三套单维度预聚合的唯一收益是「单维度查询 O(1) 不用遍历」。但每桶组合键仅约 10 条，遍历+过滤
  聚合是微秒级——**为不存在的查询性能问题，牺牲写入简单性（每请求要多累加三套）与单一数据源原则
  （四份数据要保持一致，多一份就多一处可能不一致的 bug 面）不值得。**
- 组合键是**唯一真相源**，所有维度视图都从它派生，无跨字典一致性风险。
- 写入只需累加一处（`combos[key]` + 桶顶层总计），比三套预聚合更简。

故 §3 所有单维度/多维度查询一律走「组合键投影聚合」，账本不冗余任何单维度预聚合。

### 3. 重写 `stats`：账本组合键投影聚合 + 日志补 max，单命令多维查询

**不新增 `totals` 命令。重写 `model_proxy_cli.sh` 的 `cmd_stats`**：主体读账本 JSON（对 `combos`
做投影聚合），同时顺手 grep 日志算账本没存的 `max ms`（同命令合并展示）。

**命令参数设计：**

```
stats                              # 全历史 total，默认三维各自汇总各列一段
stats today                        # 今天（UTC+8）
stats 2026-07-23                   # 指定某天
stats 2026-07                      # 指定某月（YYYY-MM）
stats month                        # 本月（UTC+8）
stats <时间> supply                # 只按 supply 维度汇总（不分 route/strategy）
stats <时间> route                 # 只按 route 维度汇总
stats <时间> strategy              # 只按 strategy 维度汇总
stats <时间> supply=<X>            # 过滤：只统计该 supply（跨其所有 route/strategy），并展开
stats <时间> supply=<X> route=<Y>  # 多维过滤：同时命中 supply=X 且 route=Y 的组合汇总
```

- **第 1 参 `$2`（时间选择器）**：省略=全历史 `total`；`today`/`month`=当天/本月；`YYYY-MM-DD`=某天
  （10 位两 `-`）；`YYYY-MM`=某月（7 位一 `-`）。取到对应桶的 `combos`（月粒度：已归档读
  `months_archive`，否则汇总该月各天 `combos`）。
- **第 2 参起（维度选择/过滤）**：两类，可组合——
  - 裸维度名 `supply`|`route`|`strategy`：**投影**到该维度，即把 `combos` 各键的该维度值作分组键、
    其余维度合并累加，输出该维度各值的汇总。
  - `字段=值` 形式（`supply=X`/`route=Y`/`strategy=Z`，可给多个）：**过滤**，只保留组合键中对应字段
    等于该值的条目。多个 `字段=值` 取交集（AND）。
  - 二者可叠加：`stats today route=claude supply` = 先过滤 route=claude 的组合，再按 supply 投影汇总。
  - 都省略：默认对三个维度各做一次投影，各列一段（等价于旧的三套预聚合视图）。

**核心查询逻辑（投影+聚合，伪代码）：**

```
combos = 选定时间桶的 combos           # {key: {requests,ok,fail,usage_in,out,reasoning}}
filters = 解析出的 {字段:值} 过滤条件   # 可空
proj    = 解析出的投影维度名或 None     # supply/route/strategy 之一，或 None

acc = {}   # 分组结果：{分组值: 累加计数}
for key, v in combos.items():
    dims = parse(key)                  # {"supply":..,"route":..,"strategy":..}
    if any(dims[f] != val for f,val in filters): continue    # 过滤：不满足即跳过
    gkey = dims[proj] if proj else "(all)"                   # 投影维度值作分组键
    acc[gkey] += v                     # 逐字段累加 requests/ok/fail/usage_*
print acc（按 requests 降序）
# 总计行 = 桶顶层 requests/ok/fail/sum_ms（若无过滤直接用桶顶层；有过滤则由 acc 求和得出）
```

**是否支持多维查询**：**支持，但克制**——通过 `字段=值` 过滤 + 单维度投影的组合覆盖用户「任意维度
组合切片」诉求（如「某 strategy 在某 route 下的用量」= `strategy=cc route=claude`；「某 supply 不分
route」= `supply=X`）。**不引入更花哨的语法**（不做 group-by 多字段笛卡尔展开、不做时间范围区间），
个人工具够用即止。参数解析用简单规则：含 `=` 判为过滤条件，裸 `supply/route/strategy` 判为投影维度。

- **max ms 合并**：命令末尾对 `.claude_model_proxy.log` 做一次 `grep ' ACCESS ' | awk` 求 `max ms`
  （受日志窗口约束，标注「(近日志窗口内)」），追加一行。账本给全量累计的 requests/ok/fail/avg ms/token，
  日志补窗口内 max ms，一屏看全。

**输出格式（示例，`stats 2026-07 supply`）：**

```
period: 2026-07 (UTC+8)   requests=1240  ok=1231  fail=9  avg_ms=1830  usage_in=982k usage_out=310k usage_reasoning=44k
by supply:
  claude-sonnet-sankuai-0956   requests=440  ok=433  fail=7  in=362k out=120k reasoning=14k
  openai-sol-sankuai-0956      requests=800  ok=798  fail=2  in=620k out=190k reasoning=30k
max_ms=42150  (近日志窗口内，非账本口径)
```

`stats today route=claude supply`（过滤 route=claude 后按 supply 投影）则 period 行下只列 claude
route 内各 supply 汇总。**实现**：`cmd_stats` 用 python3 内联读 `$TOTALS_FILE`（沿用 `cmd_status`
的 python3 内联解析手法），按上述逻辑选桶→过滤→投影聚合→打印，结尾 bash `grep+awk` 补 max ms。
账本文件不存在时提示「no stats yet」。

### 5. 写入机制/并发/重启（沿用不变）

**每请求同步在锁内累加内存 dict 并原子落盘。**
- **挂载点**：`_forward_logged` 的 `finally`（`server.py:677`）、`access_log.info(...)` 之后，加
  `usage_totals.record(a, ms)`（外兜 try，记账异常绝不冒泡影响请求/日志）。
- **record 累加逻辑**：锁内——取当天 key 的天桶（无则建）；拼组合键
  `f"supply={a['supply'] or '(none)'}|route={a['route'] or '(none)'}|strategy={a['strategy'] or '(none)'}"`；
  对「天桶顶层 + 天桶.combos[组合键] + total 顶层 + total.combos[组合键]」四处累加
  requests/ok/fail/usage_*（顶层另累 sum_ms）；再做归档检查；`_atomic_write_json` 落盘。
- **IO/流式**：finally 在 `_forward` return 后执行，不在流式数据通路；QPS≪1，几 MB JSON 原子写毫秒级，
  可忽略。**不做攒批/定时 flush**。
- **并发**：module 级 `threading.Lock` 把「读内存→累加→归档→原子写」包成临界区，无丢更新（与
  `ConfigStore`/`CooldownStore` 一致；已有 `/tmp/claude_model_proxy.lock` 进程级互斥保证单进程写）。
- **重启不清空**：启动 `load` 一次进内存，只读不删；数据只增不截。
- **归档触发**：`record` 落盘前若 `len(days) > keep_days`，把超窗最旧天桶的 `combos` 按键汇总进
  `months_archive[对应月].combos`（及其顶层）后从 `days` 删除。O(超窗天数)、每天至多 1 次、锁内完成。

### 6. 数据一致性与可恢复性（沿用不变）

- **崩溃丢数据**：每请求原子落盘 → 崩溃最多丢当前未完成请求。已完成请求账本始终一致。
- **原子写**：**复用 `_config_ops.atomic_write` 的 mkstemp+os.replace 模式**（`_config_ops.py:42-58`），
  在 `server.py` 内自写 `_atomic_write_json(path, obj)`（约 8 行）——不跨包 import（依赖方向不当，且
  `atomic_write` 形参/`indent`/`chmod` 耦合 config 语义）。被杀最多留 `.tmp` 残件，正式账本绝不半截。
- **文件损坏恢复**：`load` 时 `json.loads` 失败 → WARNING、坏文件重命名备份
  `.claude_model_proxy_totals.json.corrupt.<ts>`（留取证）、空账本起步。
- **version**：`version:2`。读到旧结构或缺字段→空起步（无迁移器，账本刚上线无历史包袱）。

### 7. 具体实施位置（precise to function/line）

**server.py：**
1. L0 基座区（access logger 后，约 69 行后）：`from datetime import datetime, timezone, timedelta`
   （若未 import）；`_CST`/`_cst_now()`（§0）；`TOTALS_FILE`、`KEEP_DAYS=400`、`_atomic_write_json`；
   `UsageTotalsStore` 类（`__init__` load+损坏恢复；`record(acc, ms)` 锁内组合键累加+归档+原子写，
   见 §5）；module 单例 `usage_totals = UsageTotalsStore(TOTALS_FILE)`。
2. `_forward_logged` 初始化 `_acc`（`server.py:669`）加 `"strategy": ""`。
3. `_forward` 的 `resolve_strategy` 之后（`server.py:720` 后）加
   `self._acc["strategy"] = strategy.get("client_token", "") if strategy else ""`。
4. `_forward_logged` 的 `finally`（`server.py:677-684`）：`ms` 提为变量复用，emit access 后加
   `try: usage_totals.record(a, ms) except Exception: pass`。

**model_proxy_cli.sh：**
5. 头部变量区（`LOG_FILE=` 旁，约 11 行）加 `TOTALS_FILE="$SCRIPT_DIR/.claude_model_proxy_totals.json"`。
6. **重写** `cmd_stats`（`model_proxy_cli.sh:378-408`）：python3 内联读 `$TOTALS_FILE`，按 §3 选桶→
   过滤→投影聚合→打印 + 结尾 `grep+awk` 补 max ms。
7. 主 `case` 的 `stats)`（`model_proxy_cli.sh:445`）改为透传全部余参：`cmd_stats "${@:2}"`。
8. `print_help`（约 49 行）更新 `stats` 说明为组合键多维/多粒度用法。

**.gitignore：** 加 `.claude_model_proxy_totals.json` 与 `.claude_model_proxy_totals.json.corrupt.*`。

**README：** `stats` 段更新为「读独立账本、组合键任意维度切片（supply/route/strategy 及组合）× 天/月/
全历史，独立于日志截断，max ms 由日志补」。

## 风险与权衡

- **组合键分隔符**：三段值均为 config 内标识符（supply id / route id / client_token），实测不含 `|`/`=`，
  拼接无歧义。若日后用户把 client_token 设成含 `|` 的怪值，解析会错——可接受（个人工具、可控），
  implementer 可选在拼 key 前对三值做 `assert "|" not in v` 或简单转义，非必须。
- **归档边界一致性**：月汇总两来源（在窗→汇总 days.combos；已归档→读 months_archive.combos），必须
  「一旦归档就从 days 删除」+「查询先查 archive，命中即用否则汇总 days」二选一，杜绝双算。最需
  implementer 谨慎处，建议补跨边界单测。
- **KEEP_DAYS=400 是明细回溯上限**：超 400 天按天查无明细（只剩月归档汇总）。个人工具足够。
- **strategy 维度依赖 client_token 稳定性**：改了 client_token 则旧值/新值成两个组合键（历史归旧、新增
  归新），符合直觉，可接受。
- **投影聚合每次遍历 combos**：每桶约 10 条，微秒级；全历史 `total.combos` 同量级。查询频率低，无压力。
- **max ms 与账本口径不一致**：max 来自日志（有窗口），账本其余全量累计，输出已显式标注。
- **不改 ACCESS 日志行**：strategy 只进账本组合键、不进日志行，已 implemented 的 access-log/awk 不受影响。
- 迁移/落地代价：新文件首次运行自动创建；`_acc` 加一字段+一处赋值；`cmd_stats` 重写（bash+python3
  内联，无第三方依赖）；无 config 变更、向后兼容（响应/转发/access 日志全不变）。

## 验证方式

1. 冷启动无账本 → 发一个正常请求 → 生成 `.claude_model_proxy_totals.json`（`version:2`，含 `total`/
   `days["今天UTC+8"]`），该天桶 `combos` 有对应组合键
   `supply=..|route=..|strategy=..`，strategy 段是 client_token（如 `cc`）**非** token 尾4；组合键
   value 与该请求 ACCESS 行一致；天桶顶层 `requests==1`。
2. 混发多请求（不同 strategy→route→supply、混成功失败）→ 天桶顶层与 `total` 顶层逐项等于手工累加；
   `combos` 各键 value 之和 == 桶顶层（交叉校验）；每键 `ok+fail==requests`。
3. **时区**：宿主系统时区临时改成非东 8 区（如 UTC）重启后发请求 → 落桶仍按 UTC+8 日期。
4. **多维查询**：`stats`（全历史三维各段）、`stats today`、`stats 2026-07-23`、`stats 2026-07`（=各天
   桶之和）、`stats 2026-07 supply`（按 supply 投影）、`stats today supply=<X>`（过滤单 supply）、
   `stats today route=claude supply`（过滤 route 后按 supply 投影）、`stats today strategy=cc route=claude`
   （多维过滤）各自输出正确；末尾 `max_ms` 行存在且标注日志窗口。
5. **投影一致性**：同一时间桶，`stats <t> supply`/`route`/`strategy` 三种投影的总 requests 相等（同批
   请求三种切分）；任一投影各分组 requests 之和 == 桶顶层 requests。
6. **归档不双算**：`KEEP_DAYS` 临时调小（如 2）造 >2 天数据触发归档 → 最旧天桶 combos 汇总进
   `months_archive` 且从 `days` 删除；`stats 该月` 归档前后数值一致（跨边界不丢不重）。
7. **重启不清空**：账本有数据后重启再发 1 请求 → `total.requests` 在原基础上 +1，非从头。
8. **与日志截断解耦**：日志造 >5000 行后重启触发 `_trim_log` → 账本数值不受影响。
9. **原子性/损坏恢复**：账本改成非法 JSON 后启动 → WARNING + 生成 `.corrupt.<ts>` + 空起步不崩；
   运行中 `kill -9` → 账本仍是合法 JSON，最多丢当前未完成请求。
10. **并发**：`xargs -P` 起并发 curl → `total.requests` 精确等于请求数（锁内累加无丢更新）。
11. 现有单测全绿；`logs`/access 日志/转发/转换行为不变；补：组合键 `record` 累加单测、投影+过滤查询
    单测、归档边界一致性单测、`_cst_now`（UTC+8 跨零点）单测。

## 关联

- 前序：[[2026-07-22-access-log-and-latency]]（access 日志 + `_acc` + `_forward_logged` + 旧 `stats`，
  本方案复用其 `_acc` 字段与 finally 收口点，并**重写**其 `stats`）
- [[core/server.py]] `_forward`（strategy 赋值约 720 后）/ `_forward_logged`（669 初始化、677 finally）
  / 新增 `UsageTotalsStore`+`_atomic_write_json`+`_cst_now`+`TOTALS_FILE`（L0 约 69 后）/ `_trim_log`（41）
- [[_config_ops.py]] `atomic_write`（42-58，原子写模式来源）
- [[model_proxy_cli.sh]] 重写 `cmd_stats`（378-408）/ case `stats)`（445）/ `print_help`（49）/
  头部 `TOTALS_FILE`（11）
