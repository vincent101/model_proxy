---
created: 2026-07-24 17:05:00
type: design-decision
date: 2026-07-24
status: draft
target: "[[model_proxy_cli.sh]]"
tags: [architect, model_proxy, token-usage, reasoning, stats, cli]
---

# model_proxy stats 统计口径收敛：reasoning 从默认列降级为按需明细

## 背景与问题

`model_proxy_cli.sh stats` 当前对每个 supply/route/strategy 组合并列打印 `in / out / reasoning`
三列（`VAL_FIELDS` 含 `usage_reasoning`，cli 第 401、549-551、562-563 行）。

问题：reasoning 是"协议是否额外暴露明细拆分字段"的差异，**不是计费口径差异**——所有供应商
reasoning 都已算在 output_tokens 内，从不是 output 之外的额外量。Anthropic 协议不暴露该明细，
故 `route=claude|strategy=cc` 各 supply 的 `usage_reasoning` 恒为 0（实测 totals.json：sonnet/opus/
ds-pro 全 0），而 openai/kimi 路由有非零值（kimi 918、openai 933）。三列并排展示，在跨供应商聚合
对比场景下造成"Claude 没用思考 / 该列跨 route 可比"的误导。

**目标：默认聚合/对比视图收敛为只有 in/out 两个 token 维度；reasoning 明细降级为"单供应商成本分析"
时按需查看的次要信息，并在展示与文档中澄清其语义。**

### 现状核查结论（已逐处验证，与派单描述一致）

1. `core/translate.py:1040` `_extract_reasoning_tokens(usage)`：多路径读 anthropic
   (`output_tokens_details.thinking_tokens/reasoning_tokens`、顶层 `thinking_tokens`)、chat
   (`completion_tokens_details.reasoning_tokens`)、responses
   (`output_tokens_details.reasoning_tokens`)，全 `or {}` 防 null。**这是 07-23 刚 implemented 的
   全链路提取修复的核心产物**（见 [[2026-07-23-usage-reasoning-extraction-unify]]，status=implemented）。
2. `core/server.py` 四种转发模式（1169-1284）各自把 `_acc["usage_reasoning"]` 从响应/adapter 解析出来：
   PASSTHROUGH 走 `pt._extract_reasoning_tokens`（1176）；ANTHROPIC_TO_CHAT/RESPONSES 非流式读
   `output_tokens_details.reasoning_tokens`（1209、1242）、流式取 adapter `usage_tuple()[2]`
   （1190、1222）；RESPONSES_TO_ANTHROPIC 同理（1258、1279）。
3. `_acc["usage_reasoning"]` 经 `UsageTotalsStore.record` 累加进
   `.claude_model_proxy_totals.json` 的 `combos[key].usage_reasoning`（schema 见
   [[2026-07-23-usage-totals-ledger]]，`version:2`）。
4. cli `cmd_stats`（388-588）用 python3 内联读账本，`VAL_FIELDS` 六字段含 `usage_reasoning`，
   period 行与 `print_groups` 各行都打印 reasoning。**已确认 `usage_out` 是完整口径 output（含
   reasoning），无重复计费/漏计；`usage_reasoning` 只是从中抽出的占比明细。**

### 一条必须守住的红线

**translate.py 的 reasoning 提取能力与 adapter `usage_tuple()` 第三位一律不动。** 那是"协议转换
保真"——跨协议转换时把上游 reasoning 明细如实透传给下游消费者，是转换器的正确性义务，与"统计
展示口径"是两层不同的事。07-23 刚花力气修好的 4 处提取 bug + helper 收敛，本方案不得回退。本次
只处理"统计的存储/展示口径"，不碰"转换的字段保真"。

## 方案设计

### 路径说明

本任务无 `[务实]`/`[理想]` 标记。评估后两条路径产出基本一致——**都指向"展示层收敛、存储层保真"**
（理由见风险节"为何理想路径也不选 B"），故直接给方案、不反问。下面推荐方案 A+，并把彻底收敛的
选项 B 作为备选完整列出供知情选择。

### 三个选项对比

| | 选项 A+（推荐，展示层收敛+按需明细） | 选项 B（累加器层收敛） | 选项 C |
|---|---|---|---|
| translate.py | 不动 | 不动 | — |
| server.py `_acc`/record | 不动 | 停写 usage_reasoning | — |
| totals.json schema | 不动（保留 usage_reasoning） | 去掉 usage_reasoning 字段 | — |
| cli 默认视图 | 去 reasoning 列（只 in/out） | 去 reasoning 列 | — |
| cli 明细入口 | `--verbose` 才显示 reasoning + 语义说明 | 无（数据源已删） | — |
| 历史兼容 | 零迁移（schema 不变） | 需处理孤儿字段/可选迁移脚本 | — |
| 单供应商 reasoning 占比能力 | 保留（`--verbose`） | **永久丧失** | — |
| 与 07-23 成果关系 | 保全并真正兑现其价值 | 提取修完却在存储层砍掉，逻辑不自洽 | — |
| 改动量 | 1 文件（cli）+ README | cli + server.py（+可选迁移脚本）+ README | — |

选项 C（其他分层）：考虑过"存储层保留、展示层加一个 `total_output`（=out，语义即含 reasoning）
派生视图"等，但对个人工具属过度设计，无实际收益，不推荐。

**推荐选项 A+。** 核心判断：误导发生在"展示"（跨 route 并列 + claude 恒 0），不在"存储"。只要
默认视图不并列 reasoning，误导即消除；数据留在存储层，单供应商成本分析时按需取用。选项 B 在存储层
抹掉客观信息，等于因某个聚合视图不需要就删数据源，架构上是退步，且把 07-23 刚打通的 openai/kimi
reasoning 统计链路在存储侧又关掉。

### 选项 A+ 具体改动（仅 `model_proxy_cli.sh` + README）

改动全部落在 `cmd_stats` 内联 python（388-588）+ `print_help`（50-59）+ README stats 段。
server.py / translate.py / totals.json **零改动**。

**1. period 行去 reasoning（默认），`--verbose` 才追加。** 现 549-551 行无条件打印
`usage_reasoning`。改为：默认只打印 `usage_in / usage_out`；解析出 `--verbose`（或 `-v`）时才在行尾
追加 ` usage_reasoning=<k>`。

**2. `print_groups` 各行去 reasoning（默认），`--verbose` 才追加。** 现 562-563 行末尾固定
`reasoning=<k>`。改为默认只 `in= out=`；`--verbose` 时行尾追加 ` reasoning=<k>`。

**3. 参数解析新增 `--verbose`/`-v` 开关（正交，不占位）。** 在 `dim_args` 解析循环（501-512）
之前先剔除 `--verbose`/`-v` token，置 `verbose=True`；其余时间选择器/维度/过滤逻辑完全不变。
`--verbose` 可与任意时间/维度/过滤组合（如 `stats today supply=openai-... --verbose`）。

**4. `--verbose` 展示 reasoning 时附一行语义澄清。** 仅在 `verbose and 有 reasoning 展示` 时，
在输出末尾（max_ms 行之前或之后）打印固定说明：

```
note: reasoning 是 output_tokens 的子集(非额外计费)；Anthropic 协议不暴露该明细，
      route=claude 恒为 0，此列仅供单供应商(openai/responses/chat)成本占比分析，跨 route 不可比。
```

**5. `VAL_FIELDS` 保持六字段不变**（`aggregate`/`merge` 仍累加 usage_reasoning，数据照常可得），
只是 print 层按 verbose 决定是否输出该列。

**6. `print_help` 的 stats 段**（50-59）补一行 `--verbose` 说明 + reasoning 语义一句话：
```
stats [时间] [维度/过滤...] [--verbose]
  ...(原有)...
  --verbose/-v  额外展示 reasoning 明细列(output 子集,非额外计费;claude route 恒 0,跨 route 不可比)
```

**7. README stats 段**同步：默认视图只 in/out；`--verbose` 看 reasoning 占比 + 上述语义澄清。

改动量与分工建议（仅供后续执行参考，不代表本 architect 决定分工）：单文件、无跨文件/无数据正确性
风险，但 `--verbose` 需在 period 行 + `print_groups` + 说明行 + help + README 五处一致生效（漏一处
则默认视图残留 reasoning 或 verbose 不生效），属**轻度一致性耦合 + 中等 diff**。倾向 implementer
稳妥；若走 runner 则建议交付后 `/code-review` 或派 reviewer 过一眼 verbose 各分支一致性。

### 决策点 3（语义澄清）落点

不管选哪个改动深度，语义澄清都做，且落两处：
- **运行时**：`--verbose` 展示 reasoning 时打印上面第 4 点的 `note:` 行（选项 B 无 reasoning 展示则
  无需运行时说明，只在文档留一句"已按计费口径收敛为 in/out"）。
- **文档**：`print_help` + README 各一句"reasoning 是 output 子集、非额外计费、claude route 恒 0、
  跨 route 不可比"。

### 决策点 4（粒度区别对待）落点

- **跨协议聚合/对比视图**（默认 `stats` / `stats <维度投影>` / 跨 supply 各段）：只 in/out，收敛。
- **单供应商成本分析入口**：`stats today supply=<某 openai/kimi supply> --verbose`——过滤到单
  supply 后加 `--verbose` 即得该 supply 的 reasoning 占比。**不新增独立 reasoning 子命令**（个人
  工具，克制；`--verbose` 正交开关已足够覆盖）。

### 备选：选项 B 的迁移策略（若用户坚持 schema 极简收敛）

若用户出于存储洁癖坚持去掉 usage_reasoning 字段，迁移要点如下（不推荐，代价见风险节）：
1. `server.py` record 累加处停止写 usage_reasoning；四种模式仍可保留解析（或一并删 `_acc`
   赋值），adapter `usage_tuple()` 仍返回三元组，server.py 接收后**丢弃第三位即可，translate.py
   不动**。
2. `cli VAL_FIELDS` 去掉 usage_reasoning → `merge_bucket_into`/`aggregate` 不再累加该字段。
3. **历史文件天然向后兼容**：老 totals.json 里残留的 usage_reasoning 是孤儿字段，`v.get(f,0)`
   遍历新 VAL_FIELDS 时不会读它、也不报错；随后续写入/归档重写逐步消失。
4. **一次性迁移脚本非必须**（个人工具不值得）；如要彻底清，可写脚本遍历 `total`/`days`/
   `months_archive` 各 combos 删 usage_reasoning 键，`_atomic_write_json` 回写。
5. 版本号可不升（字段减法向后兼容）；若想显式标记可升 `version:3`，但会牵出 load 兼容分支，个人
   工具不建议。

## 风险与权衡

- **为何理想路径也不选 B**：理想追求架构合理与单一真相源。"存储层记客观事实（reasoning 明细在
  chat/responses 协议里客观存在）+ 展示层按可比性裁剪"本身就是更合理的分层；B 在存储层抹掉客观
  信息属退步，且与 07-23 理想路径文档"全链路提取完备"的结论直接冲突。故务实/理想在此任务收敛为
  同一方案 A+，无需反问用户选路径。
- **选项 B 的不可逆代价**：一旦停写并（可选）清历史，openai/kimi 已积累的 reasoning 值（kimi 918、
  openai 933）永久丧失展示途径；将来想做单供应商 reasoning 成本分析需重新加回链路。A+ 无此风险。
- **`--verbose` 一致性**：verbose 必须在 period 行 + print_groups + note 行 + help + README 五处
  一致生效，漏一处即默认视图残留 reasoning 或 verbose 空转。这是本方案唯一需谨慎的耦合点，建议
  实施后专项核对五处。
- **`--verbose` 与过滤/投影组合**：`--verbose` 是正交全局开关，剔除 token 后不影响既有时间/维度/
  过滤解析；需确认它不会被误当作维度名或过滤条件（解析循环里先剔除即可）。
- **note 语义行的展示位置**：放 max_ms 行附近即可；注意仅 `verbose and 该视图确有 reasoning 列`
  时打印，避免非 verbose 场景冒出无关说明。
- **totals.json 正被实时写入**：账本随代理运行持续累加（核查期间 requests 从 2192→2195）。方案 A+
  不动 schema/写入逻辑，无并发/一致性影响；实施时也无需停服。

（选项 A+ 无迁移代价：schema 不变、零迁移、可随时上线；此段"迁移代价提示"仅对备选 B 有意义，已并入
上方 B 迁移策略与本节风险。）

## 验证方式

选项 A+：
1. `bash model_proxy_cli.sh stats`（默认全历史三维投影）→ period 行与各组行**只出现 in/out，无
   reasoning**；输出无 `reasoning=`/`usage_reasoning=` 字样。
2. `stats --verbose` → period 行与各组行**追加** reasoning 列；末尾出现 `note:` 语义澄清行；
   `route=claude` 各 supply 的 reasoning 显示 0，openai/kimi 组合显示实测非 0（对齐 totals.json：
   kimi 918、openai 933）。
3. `stats today supply=<openai/kimi supply> --verbose` → 单供应商成本视图正确展示该 supply
   reasoning 占比 + note 行。
4. `--verbose`/`-v` 与 `today`/`month`/`YYYY-MM-DD`/`YYYY-MM`/维度投影/过滤各组合均正常，且
   verbose 不被误解析为维度/过滤（如 `stats today route=claude supply --verbose` 仍按 route=claude
   过滤+supply 投影，仅多出 reasoning 列与 note）。
5. `stats --help` / README 的 stats 段含 `--verbose` 用法与 reasoning 语义澄清一句话。
6. 回归：totals.json 未改；server.py/translate.py 未改；`git diff` 仅涉 `model_proxy_cli.sh` 与
   README；现有单测（若覆盖 cmd_stats）全绿或人工核对上述输出。
7. 数据未丢：改动后 `--verbose` 仍能读出历史 usage_reasoning 值（证明只是展示条件化，非删数据）。

选项 B（若选）追加验证：`VAL_FIELDS` 去字段后读老 totals.json 不报错、默认视图正确；孤儿字段
usage_reasoning 不参与聚合；（若跑迁移脚本）回写后账本合法且各桶无 usage_reasoning 键。

## 关联

- 展示层（本方案唯一改动点）：[[model_proxy_cli.sh]] `cmd_stats`（388-588，`VAL_FIELDS` 401、
  period 行 549-551、`print_groups` 562-563、参数解析 501-512）、`print_help`（50-59）
- 累加/记账（本方案不动，仅引用）：[[core/server.py]] 四模式 usage 解析（1169-1284）、
  `UsageTotalsStore.record`
- 转换保真（红线，一律不动）：[[tools/model_proxy/core/translate.py]] `_extract_reasoning_tokens`
  （1040）与各 adapter `usage_tuple()`
- 前序（07-23，status=implemented，本方案在其之上、不回退）：
  [[2026-07-23-usage-reasoning-extraction-unify]]（全链路 reasoning 提取修复与 helper 收敛）
- 账本 schema 来源：[[2026-07-23-usage-totals-ledger]]（`combos[key].usage_reasoning`、`version:2`）
- 账本文件：`tools/model_proxy/.claude_model_proxy_totals.json`
- README：`tools/model_proxy/README.md`（stats 段）
