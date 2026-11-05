---
created: 2026-11-04 17:02:40
modified: 2026-11-04 17:02:40
type: design-decision
status: confirmed
target: /Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104
tags:
  - architect
  - model-proxy
  - bugfix
branch: fix/tool-config-retry-20261104
base_commit: ca2b15975cdc6bd5ba1461237917ac8b11719997
---
# 工具参数关联、配置序列化保真与新增 guard 撤除

## 1. 背景与问题

修复两处已独立复现的数据关联/保真错误，撤除近期 Responses ID/store guard；仅在隔离 worktree 工作，保留既有流终态修复。

### 1.1 授权与范围

用户原话：
- “工具参数错配、配置序列化改值、兼容重试出站体，设计方案，然后落地，在worktree改不要动线上”
- “稍等下，加一个，把之前修复 ID重写 和 store 撤掉掉吧，看起来不是这个问题”
- 顶层澄清撤除两项、取消兼容重试出站体修复后：“好的，先修这两个，回头再来看如何处理 这个这个”。

最终范围只有：①撤除 ID/store 两项新增 guard；②工具参数关联修复；③配置序列化保真。保留 `293eae3` 的终态行为以及其他不属于 guard 的既有行为。

不包括：新 ID/store 兼容策略、`store=false` 改值、raw_body 优化、通用 retry/fallback 重构、非法 arguments JSON 降级为 `{}` 等其他审核问题、真实配置恢复、部署。

本阶段唯一写入是本 draft；用户确认前不派实施。两条设计路径在本定向修复内无实质差异，不扩展架构。没有必须用户裁决的关键语义阻塞。

### 1.2 工作边界

下文 `W` 仅作为阅读简称，指绝对根：

`/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104`

源码访问和实际文件操作须使用该外部根下的绝对路径。分支、起点如 frontmatter；设计前已确认 HEAD 与起点一致、工作区干净。未读取/复制线上真实配置，也未读取真实日志。

禁止修改线上目录；不执行 model_proxy CLI/API、不联网、不启动服务、不 merge/restart/reload。本记录内的测试命令是**用户确认后**的实施验收指令，不是本阶段执行结果。

### 1.3 独立核查结果

以下四项是通过只读源码及内存合成样例确认，不是采信 reviewer 原结论。Python 使用 `-B`，未生成脚本、缓存或业务文件。

| 问题 | 起点位置 | 合成反例的实际结果 |
|---|---|---|
| Chat 工具块生命周期 | `core/translate.py:1349–1376` | 工具 0 首片 → 工具 1 首片 → 工具 0 后片；输出 `stop(0), start(1), delta(0)`，旧块停止后仍接收参数 |
| Responses 工具关联 | `core/translate.py:2709–2717,2762–2765` | added A(index=0)、added B(index=1)，随后 A 的 `item_id/output_index` 参数增量被发到 Anthropic index=1，即 B |
| 删除字符串内空格 | `_config_ops.py:189–190`，其他对象压行分支同类 | 完整 supply 的 `target_model="model  variant"` 写出后读回为 `"modelvariant"` |
| 拆分真实 ID | `_config_ops.py:170–184` | 已解析的 `tiers.opus=["s,1","s2"]` 写出后成为 `["s","1","s2"]` |

扩展字段/键序改变会令正则失配而展开对象，也已由正则条件和现有格式测试核查；它不是改值的修复手段。

## 2. 方案设计

### 2.1 精确改动范围

| 文件绝对路径 | 改动点与目标 |
|---|---|
| `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/core/translate.py` | 仅两个面向 Anthropic 输出的 adapter：局部调用台账、单活动输出块、待发分片/后续块缓存、done/终态排空；保留非流式函数及反向 adapter |
| `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/_config_ops.py` | 替换 `compact_config_json` 及其九组专用正则；维持 `atomic_write` 原子替换/权限/末尾换行；更新本文件中已失效的正则键序注释，不改 CRUD 输入规则 |
| `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/core/server.py` | 仅删除 ID/store guard 的 import、两段 400 重试、局部状态与专属 ACCESS/告警字段；不修改出站序列化或流式 writer |
| `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/tests/test_tool_stream_association.py` | 新增：合成事件关联、生命周期、串行缓存、非流式对拍 |
| `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/tests/test_responses_guard_removal.py` | 新增：假上游驱动，证明 ID/store guard 不再注入请求或额外同 supply 重试 |
| `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/tests/test_config_compact_format.py` | 更新旧正则外观断言，新增任意 JSON 字符串/嵌套/扩展字段保真与新排版断言 |
| `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/tests/test_config_ops.py` | 补纯 helper/临时配置回归：CSV 合法输入不变、原子写入只保留传入当前对象、不恢复旧数据 |
| `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/tests/test_route.py` | 补含逗号 ID 的已解析列表引用/选择回归，证明不以格式化过程改变路由目标 |
| `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/README.md` | 仅补新增测试/设计记录的轻量产物索引；不改功能、部署或版本描述 |

删除四个绝对路径：
- `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/core/responses_id_guard.py`
- `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/core/responses_store_guard.py`
- `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/tests/test_responses_id_guard.py`
- `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/tests/test_responses_store_guard.py`

现有 `tests/test_translate.py`、`tests/test_protocol_terminal_handler.py` 等用于回归，不要求改写。既有设计记录、真实/示例配置、CLI、运行时路径文件均不修改。

### 2.2 工具关联：局部台账 + 单活动块 + 后续缓存

#### 选择与边界

项目规格 `docs/designs/model_proxy_translate_spec.md` §4.1 承认工具分片可能交错，§3.5/§4.2 却用“新工具出现就停止旧块”；这一局部规则不能同时满足后续旧工具 delta。解决点是两个 adapter 的关联/输出生命周期，不是请求、路由或全局协议框架。

| 候选 | 结论 |
|---|---|
| 直接同时打开多个 Anthropic 工具块 | 不采用：破坏本项目既有单活动块/单调 index 契约，不依赖下游接受任意交错 |
| 所有工具乃至整条响应等终态再发 | 不采用为默认：不必要地牺牲首个工具、已有文本的流式进度 |
| 保持最早未完成工具打开，缓存后续调用，按完成顺序门槛串行排空 | **采用**：首工具仍可流式；后续工具延迟但分片不丢、不串，不回到已 stop 的块 |

仅在现有两个 adapter 内增加台账/小型待发队列；可复用事件构造函数和同文件小型私有操作，不新增模块、公共框架或配置开关。缓存不仅保存后续工具：若未完成工具之后出现文本/thinking，也按首次逻辑出现的位置暂存，不能靠停止工具来抢发后续块。首工具前的文本/thinking 和无工具路径维持原行为。

#### 数据与输出不变量

每个逻辑工具保存：上游关联键、既有客户端 tool ID/name、原样 arguments 分片列表、参数 done/工具 done 状态、待发/已打开/已停止状态。首次出现登记输出次序；**实际发 start 时**才分配 Anthropic index。

- 任一时刻最多一个 Anthropic 输出块打开；start index 连续单调递增。
- 每个工具恰好一次 start/stop；正常完成时每个非空原始分片恰好发一次，块 stop 后不能再有 delta。
- 上游关联键用于定位逻辑调用，输出 `cur_index/cur_type` 只描述当前已发布块，不能用于推断 delta 属谁。
- 已发布的 ID/name 不随块切换而改变；参数不解析、不规范化、不按内容去重。
- 后续块可先完成，但不得越过更早未完成块；排空已完成块后，可以打开下一个未完成块并继续流式。
- 文本/thinking 的原分组/停止语义仅按队列延迟执行，不设计新的 reasoning 映射。
- 保持现有 stop_reason、usage、工具名恢复与 ID 兜底。区分“最后逻辑输入类型”与“当前物理输出类型”，避免排空顺序意外改变现有 stop_reason；不顺手改混合文本/工具的历史判定。

#### Chat → Anthropic

改 `OpenAIToAnthropicStreamAdapter.__init__/_handle_tool_calls_delta/feed/finalize`：

1. 用 `tool_calls[].index` 定位独立记录，不能用数组当前位置；index=0 必须正常处理。ID 仍取首片的 `id` 或既有 `gen_toolu_id()`，name 仍按现有映射恢复。
2. 新工具只登记记录，**不等于**旧工具结束。活动工具的后片继续原块；其他工具的后片进自己的列表。
3. Chat 没有逐工具 done；因此所有工具保持可接收参数，直到现有 `finalize()` 成功收尾点。`finish_reason` 继续先记录，再吸收末尾 usage；不新增更早收尾。
4. 成功 finalize：停止活动块 → 按登记次序发后续块的 start/原始 delta/stop → 原有 message_delta/message_stop。最后一片与 finish 在同帧时先处理片段。
5. 缺 finish、上游 error 保持原失败语义，释放待发数据，不能补成功终态。重复 finalize 不重发；已 finalized/failed 后不再出参数。
6. 不增加按 call ID 前缀重写或去重。不同 index 即不同调用，即使客户端 ID 重复也不合并；这不是新重复 ID guard。缺失/冲突 index 的不可关联事件返回明确翻译错误，不猜测“当前工具”。

`openai_index_to_anthropic_index` 如保留，仅作为实际发布后的派生映射；新增台账是生命周期真相源，不能让旧 index 映射绕过 stopped 检查。

#### Responses → Anthropic

改 `ResponsesToAnthropicStreamAdapter.__init__/feed/_emit_finish/_emit_error/finalize`：

1. `response.output_item.added` 的 `item.id` 与 `output_index` 建立到**同一逻辑记录**的映射；`item.call_id` 只用于输出 tool_use.id，不能代替 item.id 或当作全局唯一关联键。
2. arguments delta/done、function_call 的 output_item.done：有 item_id 用 item 映射，有 output_index 用 index 映射；两者都有时必须一致。未知 item_id 不能因为只有一个当前块就被错配给它；index=0 不能被当成缺省。
3. 带明确标识但 added 尚未到的 delta 暂存，后续 added 补齐后绑定并按原顺序输出；响应终态仍未绑定则明确失败，不丢事件后伪装成功。重复一致的 added 不再开新块；冲突绑定明确失败。
4. 无 item_id/output_index 的非标准 arguments 事件不得猜“当前工具”：若只有一个已登记工具，可暂存并在终态确认整条响应只有该调用时归入它；后续出现第二个工具则关联失败。没有候选工具则明确失败。暂存点之后同一调用的分片不能越过它提前输出。
5. `function_call_arguments.done` 标记参数封口，不重复发其完整 arguments 快照；工具块等待 `output_item.done` 关闭，或在 completed/incomplete 成功收尾时关闭。output_item.done 的完整 arguments 也不重放；不增加“只有 done 就恢复参数”的新行为。
6. 工具 A 的 delta 迟于工具 B 的 added，但早于 A 自己的 done：仍归 A，不能丢弃。A 的 arguments.done 或 output_item.done 后又收到非空 delta，且响应尚未终态：明确关联/生命周期错误，不给 B、不重开 A、不静默吞掉。
7. output_item.done 只完成其对应工具，不能完成整条响应；重复 done 不双 stop。completed/incomplete 验证可绑定性、封口并排空剩余块后，才走原成功/截断终态。done 缺失而 completed 已到是可收尾情形，不要求新增上游事件。
8. failed/error/意外 EOF 保持既有失败路径；completed 之后的冗余尾事件继续沿用当前终态保护，不另立更严格的尾流策略。

新增关联失败使用 `TranslationError` 的现有错误事件通道，reason 可统一为 `tool_association_error`。adapter 必须设置失败态、输出一次 error、释放缓存且不输出 message_stop。尤其不可在 Chat 已置 `_finalized=True` 后仅抛异常：server 当前保护会抑制补发 error；此处要由 adapter 返回一次 error。Responses 复用 `_emit_error`。不改 server 流式 writer 来兜这个局部问题。

#### 流式/非流式一致性

非流式 `openai_to_anthropic_response`、`responses_to_anthropic_response` 不修改。对合法合成 arguments，按调用 ID/name 重建流式工具内容，与对应非流式输入逐调用对拍；通常串行文本/工具场景同时比较块序与 stop_reason。交错场景保留首次逻辑出现次序，不新增按 ID 重排策略。

不承诺修复原来混合类型非标准序列的所有流式/非流式差异。非法 JSON、JSON 标量 arguments、空字符串等继续沿用现有两条路径各自行为；流式测试只断言原始分片保真，不能为了对拍把它们改成 `{}`。

### 2.3 配置：对象级序列化，不对 JSON 文本做业务解析

#### 契约核查

- `docs/CONFIG.md` supplies 表：`id`、`target_model` 是 string；routes 表：`tiers.opus/sonnet/haiku` 是按优先级排序的 string[]。`route_pool` 是对象数组，单个 `route_id` 是 string。
- `core/server.py` 的 supply/route map、`select_supply_list/select_supply` 使用已解析列表元素做精确引用；加载侧没有把数组内的字符串再按逗号拆分。未知扩展字段不会在加载时统一删除。
- `_config_ops.py:917–919` 的 `_split_supply_ids` 是**交互输入 CSV**；route_add/edit 的逗号输入是既有合法 CLI 契约。supply_add 的单个 ID 输入只检查非空/重复，没有禁止 ID 内部逗号。

因此序列化层必须保留 `["s,1","s2"]` 和单值 `"r,1"`；不能将“CLI 的 CSV 语法”套到实际 string 字段。保留 `_split_supply_ids` 和 effort 的交互 CSV 规则，不新增转义输入语法或 schema 校验。既有 CSV 本来不能无歧义录入单个含逗号 ID，本次不扩展 CLI；已在 JSON 内正确表达的 ID、以及编辑时留空保留的列表不能再被格式化破坏。非法 schema 的 scalar tier 若传入 serializer，也只保真写出，不自动改成数组。

#### 实现规则

移除九组 `_..._ARRAY/_..._OBJECT` 格式专用正则及文本 `.split(',')/.replace('  ', '')`；`re` 仍供其他探测函数使用，不能删除其 import。

`compact_config_json(obj)` 保持函数名/调用点：
- JSON-native dict 根，按原键序逐字段输出；根键由 json.dumps 转义，缩进 2 空格，每字段一行。
- 非空根数组字段：`[`/`]` 分行，每个元素缩进 4 空格、**完整 JSON 值单行**；根空数组为 `[]`。
- 根对象字段及数组元素内部的对象/数组全部紧凑；统一用 `json.dumps(value, ensure_ascii=False, separators=(',', ': '))`。不手写字符串转义，不依字段白名单或键序匹配。
- 空根对象为 `{}`；非 dict 根保持原 json.dumps(indent=2, ensure_ascii=False) 接受范围，不扩大配置 schema。
- serializer 不附末尾换行；atomic_write 仍仅补一个换行、同目录临时文件 + os.replace、chmod 600/原异常清理。

排版示意（非真实配置）：

```json
{
  "supplies": [
    {"id": "s,1","target_model": "model  variant","extension": {"empty": {},"items": []}},
    {"id": "s2","target_model": "模型\\variant"}
  ],
  "routes": [
    {"id": "r,1","tiers": {"opus": ["s,1","s2"]},"failover": "on"}
  ],
  "extension": {"note": "a,b  中文","nested": [[],{}]}
}
```

硬不变量：对 JSON-native 配置树，`json.loads(compact_config_json(cfg)) == cfg`，并检查原 cfg 未被原地修改、键序/数组顺序不变。输入中的内部空格、逗号、引号、反斜杠、Unicode 及未知字段均保留。

允许的文本差异：route_pool 内部冒号统一带一个排版空格，扩展对象不再因正则失配展开。旧测试中“无 effort_enum 必须完整 indent=2”“扩展字段回退多行”不再作为目标，改成稳定排版与保真断言。

不从备份补值、不修复已损坏历史数据、不写当前缺失的真实 config。所有 CRUD 仍从调用方指定的**当前配置对象**写出；本次修复只防止今后 serializer 改值。

### 2.4 Guard 撤除：按行为拆除，不能机械 revert

已审查 `ca2b159^ → ca2b159`、`ee2ad29^ → ee2ad29` 的精确 diff，并追溯 `293eae3^ → 293eae3` 中 ID guard 的最初引入。

| 来源 | 撤除行为/清理点 | 不撤的边界 |
|---|---|---|
| `ca2b159`，3 文件 | 删除 store 模块及专属单测；删除 server import、`_responses_store_retried`、400 文案识别后 `store=false→true` 同 supply continue、`responses_store_retry` ACCESS 键/格式占位/参数/告警 | 正常 store 透传；客户端 false/true/缺省/null 不注入、不改值；既有语法/预算/thinking 重试不改 |
| `ee2ad29`，3 文件 | 删除批量 function_call/custom_tool_call item.id 稳定 hash 重写、成对 function_call/output call_id 重写、RewriteResult 及计数日志；连同整个新增 ID guard 模块/单测撤掉 | 既有协议转换的 tool_use.id ↔ call_id/tool_result 映射、ID 缺省生成与工具名恢复不撤 |
| `293eae3` 中仅 ID guard 首版 hunks | 删除 import、`_responses_id_retried`、`responses_id_rewrite` ACCESS 键/占位/参数，以及 server 中完整 ID 400 分支；不能回退成单条 ID 重写 | **保留流终态修复**：三个 adapter 的 terminal_state()、server 统一读取、OPEN→invalid/unexpected_eof、已确认终态后的冗余 DONE 容忍及终态测试 |

起点清理锚点：server imports 38–45，ACCESS 初始化 1886–1889、格式/参数 1929/1941，两个 retried 状态 2235–2236，完整两段 guard 分支 2584–2621。行号仅供起点定位，实施以符号/行为为准。

这两个 guard 提交均**没有新增配置字段或持久化状态文件**，无需历史配置迁移/删除字段。`293eae3` 的 `log_upstream_error_body` 示例字段与 getter/脱敏日志、Responses session 提取和命令解析不是 guard 配置，保留且不读取真实错误体来验证。

删除专属观测时同步调整日志占位与参数，避免 ACCESS 参数错位；通用 attempts、failover、response_committed、stream_integrity 等继续保留。删除全部 guard 引用后删除模块/旧正向单测，补反向“没有注入”的单测，不恢复旧 guard。

撤除后这两类 400 直接走既有 HTTP 错误/cooldown/failover 路径：failover=off 一次请求后返回；failover=on 且配置覆盖 400 时允许原有换 supply，但不存在这两项专属同 supply 补救。不得借此修改通用 retry 或 raw_body。

### 2.5 定向推演

以下修后结果是〔推算〕，不是测试通过声明；修前四个反例是〔实测：离线合成〕。

1. **关联链与反例**：A(index=0) 带 `{"a":`，B(index=7) 带 `{"b":`，A 后片 `1}`，B 后片 `2}`。A 打开，B 暂存，A 后片仍发 A；封口 A 后发 B，最终 A=`{"a":1}`、B=`{"b":2}`。红：任何 A 片出现在 B，或 stop(A) 后还有 delta(A)；绿：两调用各片恰一次、每块 start/stop 一对。反例“按当前物理块路由”正是已复现错配，故队列不能取代身份映射。
2. **Responses done 衔接**：added A、added B、delta A、arguments.done A、output_item.done A、delta B、completed；arguments.done 不重放快照，item.done 只结束 A，B 的块在 A stop 后开始，completed 才结束 message。若 arguments.done A 后再 delta A，则错误，不属于可猜测的 B 参数；响应 completed 后尾事件则保留既有保护。
3. **保真与引用链**：supply.id=`s,1`、target_model=`model  variant`，route 数组引用同一 ID。对象序列化读回逐值相同，select_supply 精确命中 `s,1`。红：拆成两个 supply 或空格丢失；绿：深等、选中同一对象。反例“仅去掉 JSON 文本空白”会同时去掉字符串空白，已经实测推翻。
4. **撤除边界**：仅 revert ca/ee 会回到 `293eae3` 内单条 ID guard，违反“无 guard 注入”；revert 整个 293 又丢终态修复。按 guard 符号清除与终态回归分别验证，两个目标不冲突。

结构不变量仅核两个现有入口：Chat 按 index、Responses 按 item/index 关联，都先定位逻辑调用，再走单活动输出；队列排空不反写上游 ID、不改请求/配置，不增加跨模块反馈循环。无需全局框架重划。

## 3. 风险与权衡

- **延迟/缓存**：Chat 的后续工具最坏等 finalize；Responses 的后续工具等更早工具 item.done，缺少该事件则等 completed/incomplete。首工具和无工具快路径不额外等终态。仅缓存待发片段/块、发后释放，空间随待发数据增长；不新增产品限额/缓存落盘策略。
- **非法关联流**：明确标识的迟到 delta 暂存后绑定；冲突、无法绑定、工具 done 后续片显式失败，不能默默丢事件再成功。没有凭当前块猜归属的“兼容模式”。
- **生命周期耦合**：缓存排空、thinking/text 切换、finalize 失败与错误事件去重必须一起实现/验证。仅在两个 adapter 内局部调整，其他 adapter 与 server writer 不重做。
- **排版差异**：外观局部统一，旧正则格式断言需更新；深等是首要门槛，不能用放宽测试掩盖数据变更。
- **撤除影响**：原 guard 曾尝试补救的上游 400 将重新暴露给既有错误策略。这是授权撤除的结果，不是本次需要增加新补救逻辑的理由。
- **证据上限**：已确认本地代码错误及合成反例，未证明任何线上现象的唯一根因；不承诺修复所有历史故障。

## 4. 验证方式与交付

### 4.1 新增回归清单

**工具关联测试**（新 `test_tool_stream_association.py`，纯 unittest/内存事件）：
- Chat 两工具同帧首片、后续多轮交错、非连续 index=0/7、三个不同 call ID；按 index 分组精确拼接，重复内容片不能被去重。
- 首工具仍在 feed 阶段产生 delta，后续工具不提前 start；text→tool 与 tool 后插入 text/thinking 的串行保序；工具切换不出现 stop 后 delta。
- 缺 ID 的既有兜底只生成一次；不同 index/相同 client ID 不合并、不修复重复 ID。
- finish 与最后参数同帧、末尾 usage、重复 finalize、error/无 finish；只发一次终态，保留 terminal_state 与 usage。
- Responses 双 item ID/不同 output_index/多个 call ID 交错；item_id-only、index-only、两者一致、两者冲突、index=0。
- 明确标识 delta 先于 added，后续补齐正常绑定；未知 ID 不落给当前工具；未绑定到终态明确 error。
- 无标识单工具暂存后确认；暂存后出现第二工具明确 error；不提前把不确定片段发给某工具。
- A 后片迟于 B added 但早于 A done 正常保留；A 参数/item done 后非空 delta 明确 error。
- arguments.done + output_item.done + completed 不重复快照/stop；重复 done、后工具先 done、缺 done 到 completed；incomplete 保留原始部分参数；failed/EOF 不产生成功 message_stop。
- completed 后冗余 done/delta、原有 DONE 尾流保护不另改；合法工具结果与两个现有非流式函数逐调用对拍。
- 特殊参数字符串含双空格、逗号、反斜杠、引号、Unicode、嵌套空对象/数组；非法 JSON 只检查原分片透传，不新增 `{}` 降级。

所有事件收集结果经过同一个测试侧生命周期检查器：维护唯一 active index，断言 start/delta/stop 配对、index 单调、正常结束无活动块、每调用参数精确匹配；不是只比较最后拼出来的 JSON。

**配置测试**（扩展现有文件）：
- strings：`model  variant`、`s,1`、`r,1`、带引号/反斜杠/换行转义/Unicode 的 key/value；effort/tiers 元素内含逗号和空格不拆。
- 空根对象、根/嵌套空数组、根空数组字段、混合值数组、nested object/array、未知顶层/supply/route/strategy 扩展字段、任意键序：深等、不改输入、不无意义展开。
- 根字段 2 空格、根数组元素 4 空格完整对象单行、嵌套紧凑、末尾仅一个换行；重复序列化稳定。
- atomic_write 到临时合成文件，load_config 读回深等/权限语义；明确传入“当前新值”，不从旧备份覆盖。
- `_split_supply_ids("s1, s2")` 等既有 CSV 输入完全不变；helper 留空保留原列表的回归使用 mock input/临时配置，不执行 CLI 程序、不触发 reload。
- 读回含逗号 ID 的列表后，用现有 select_supply 函数精确命中该 ID；scalar 字段 serializer 不擅自升级为列表。

**撤除及终态测试**（新 `test_responses_guard_removal.py` + 既有回归）：
- 复用 `tests/test_thinking_strip_retry.py` 中 FakeConfig/FakeCooldown/SimpleNamespace/BytesIO/urlopen 队列思路，但只用自行构造的 Responses 请求/错误，不依真实配置、日志或 API。
- 含多个 function_call/custom_tool_call item.id、非标准 call_id 及对应 function_call_output；首轮假 400 前后捕获的请求保持全部 ID，不调用改写、无额外同 supply 重发。
- store=false/true/缺省/null + reasoning item；假 store 400 后不写 true、不删除历史、不注入缺省字段。
- failover=off 只一次假上游调用；failover=on + 400 cooldown 策略按原规则转另一 supply，下一次捕获请求的 ID/store 仍原样。通用 attempts/终态字段保留，guard 专属 ACCESS 键与告警均不存在。
- 正常 passthrough store 透传、既有转换 tool_use.id/call_id/tool_result 配对不变。
- 运行既有 `test_protocol_terminal_handler`：三个 adapter 统一 terminal_state、OPEN/EOF、成功/截断/失败、确认终态后冗余 DONE、失败不伪造 message_stop。同时回归 thinking/预算重试，证明不是误撤其他重试。

### 4.2 分阶段实施顺序

1. **确认与起点**：顶层先向用户确认本 draft；确认后交 `implementer-high`。核对指定分支/HEAD；允许本记录成为新增未跟踪文件，不接触真实 config。阶段性提交均只在 worktree，不 merge。
2. **建立红例**：落两份新回归及配置保真断言，证明在起点失败。旧 guard 正向测试此时仍可存在；不运行线上/录制日志重放。
3. **撤 guard**：先新增反向证据，再清除所有注入点/观测/模块/旧测试；跑 guard-removal + 终态 + 原 thinking/budget 回归，避免保留旧版 ID guard或误撤 293。
4. **修配置**：对象级 serializer → 更新与新排版冲突的旧断言 → config/route helper 回归。不改任何真实配置文件。
5. **修工具流**：先 Chat 调用台账/缓存/收尾，再 Responses 双键关联/done/缓存/失败路径；用同一生命周期检查器验收两者，不新增通用协议层。
6. **合并离线验收与复核**：执行下述允许集，提交改动清单/测试数/失败与排除项。顶层可按仓库规范派 reviewer 复核实施；本设计阶段不自行派 agent。

### 4.3 验证命令（仅供确认后的实施者）

```bash
cd /Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104

git -C /Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104 diff --check

# 预期退出码 1（无匹配）。新撤除测试中的反向断言文字不算业务引用。
rg -n 'responses_id_guard|responses_store_guard|_responses_id_retried|_responses_store_retried|responses_id_rewrite|responses_id_retry|responses_store_retry' /Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/core

PYTHONDONTWRITEBYTECODE=1 MODEL_PROXY_CONFIG=/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/tests/__missing_real_config__.json python3 -B - <<'PY'
import os, sys, unittest
from unittest.mock import patch
os.environ.pop('CONFIG_OPS_RELOAD_MARKER', None)
modules = [
    'tests.test_tool_stream_association', 'tests.test_responses_guard_removal',
    'tests.test_config_compact_format', 'tests.test_config_ops', 'tests.test_route',
    'tests.test_translate', 'tests.test_protocol_terminal_handler',
    'tests.test_thinking_strip_retry', 'tests.test_budget_retry', 'tests.test_body_error',
]
suite = unittest.TestLoader().loadTestsFromNames(modules)
def flatten(s):
    for t in s:
        if isinstance(t, unittest.TestSuite):
            yield from flatten(t)
        else:
            yield t
excluded = 'tests.test_translate.TestARStream.test_ar_reasoning_stream_from_glm_sample'
tests = [t for t in flatten(suite) if t.id() != excluded]
print('Excluded recorded SSE fixture test:', excluded)
with patch('socket.socket', side_effect=AssertionError('offline tests: sockets forbidden')), \
     patch('urllib.request.urlopen', side_effect=AssertionError('offline tests: real HTTP forbidden')):
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(tests))
sys.exit(not result.wasSuccessful())
PY
```

外层网络禁用负责 fail-closed；既有测试的内层 fake urlopen 可覆盖它，仅操作合成 BytesIO。新增测试也必须 mock 探测/HTTP 入口。临时文件只放临时目录且为合成配置，不碰真实配置/日志/备份。

明确排除现有读取录制 SSE 的上述单测；新增纯合成 reasoning 混合用例补该处相关覆盖。此处不要求无差别 discover 全套测试，以免带入录制样本、CLI/服务类用例；不能把“允许集通过”表述为全部测试通过。

人工/确定性复核：比较三个 `terminal_state()` 与 293 的原语义；检查 server diff 仅 guard 删除/日志参数同步；检查所有非流式转换函数、raw_body/send_body 分支、既有配置/旧设计记录无 diff；确认四个 guard 文件确已删除、新撤除测试可正常 import；分支未 merge。

### 4.4 交付物规范

归属：沿用本 worktree `tests/test_*.py` 的标准库 unittest 与 `docs/designs/` 约定；引用现有 README、配置紧凑设计记录和转换规格，仅限说明本次局部修订，不订正或 supersede 旧记录。无新目录、第三方依赖、CLI 入口、运行产物。

新增目录树（锚定绝对根）：

```text
/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/
├── docs/designs/2026-11-04-tool-config-fix-and-guard-revert.md
└── tests/
    ├── test_tool_stream_association.py
    └── test_responses_guard_removal.py
```

命名/清单/消费者：
- `2026-11-04-...md`：本 draft 决策记录；消费者为顶层用户确认、implementer-high 与实施复核者。
- `test_tool_stream_association.py`：工具参数关联和串行块生命周期；消费者为 unittest/实施验收。
- `test_responses_guard_removal.py`：无 guard 注入/重试的反向回归；消费者为 unittest/实施验收。
- README 仅登记上述路径索引；既有测试承载扩展回归，不额外落合成 fixture、一次性脚本或报告文件。

实施交付摘要需包含：基线与分支、改动/删除文件清单、允许集命令与实际测试数、明确排除项、保留终态证据、没有读取真实配置/日志或调用真实 API/服务的声明；不宣称已部署。设计记录状态仍为 draft，变更状态须由顶层另行核验授权。

## 关联

以下均为指定 worktree 中的只读依据，不读取其引用的线上日志/外部资料：
- `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/docs/designs/model_proxy_translate_spec.md`（§3.5/§4 的交错生命周期局部缺口）
- `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/docs/designs/2026-08-07-config-compact-format.md`（旧排版意图；其正则方案不能保障本次保真目标）
- `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/docs/CONFIG.md`（supply/route 字段真实契约）
- `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/tests/test_protocol_terminal_handler.py`（保留终态回归）
- `/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/tests/test_thinking_strip_retry.py`（假上游测试模式，不撤其既有修复）
