> 本文是 [README.md](../README.md) 的深入展开。日常配置与使用见 README。

# reasoning 强度映射（深入）

## 1. effort_enum 四种写法 / STRIP / DISABLED

`effort_enum` 和 `off_alias` 是同一套语义，target 侧（supply 下 `reasoning_capability`）与
source 侧（strategy 下 `tiers_source_capability` 的每个 tier entry）共用，解析逻辑完全一致。

**`effort_enum`**：该模型真实支持的 effort 档位有序列表（低→高，档名见下表）。四种写法对应
四种行为：

| 配置 | 去掉 off 后的真实思考档序列 | 客户端发思考意图时 | 客户端发关闭意图时 |
|---|---|---|---|
| 不写 `effort_enum` | 默认 5 档 `off/low/medium/high/xhigh` | 相对映射到默认序列 | 走 STRIP（默认无 off_alias）|
| `["off","low","medium"]`（含真实档）| `low/medium`（非空）| 相对映射到 `low/medium` | 走 DISABLED |
| `["off"]` | 空 | 走 STRIP | 走 STRIP |
| `[]`（显式空列表）| 空 | 走 STRIP | 走 STRIP |

- **STRIP**：完全清理客户端原始的 `thinking`/`output_config`/`reasoning`/`reasoning_effort`
  字段，不给上游发任何思考相关内容。用于 target 完全无真实思考档的场景（不把思考字段被动
  透传给一个已知不支持思考的上游）。
- **DISABLED**：主动发送该协议的关闭指令（anthropic 发 `{"thinking":{"type":"disabled"}}`、
  chat 发 `{"reasoning_effort":"none"}`、responses 发 `{"reasoning":{"effort":"none"}}`）。
  用于 target 有真实思考档、同时能表达"关闭"的场景。
- `["off"]` 与 `[]` 在"去掉 off 后真实思考档序列为空"这一点上强制等价，都走 STRIP，
  即便配了 `off_alias` 也不消费。
- 空列表 `[]` 与"不写 `effort_enum`"是两种不同语义：前者显式声明该 supply 不支持任何思考
  档位（一律 STRIP），后者回退默认 5 档参与相对映射。

## 2. off_alias 约束

**`off_alias`**（可选）：不是"是否支持关闭"的二元开关，而是"关闭动作具体落到哪个档位"的
可选配置。缺省规则：`effort_enum` 含 `off` 则关闭落到 `off`（进而走 DISABLED），否则不设
`off_alias`（关闭走 STRIP）。约束两条，违反则被拒、回退为不设 `off_alias`：

1. `off_alias` 不得高于 `effort_enum` 里最低的真实思考档（否则"关闭"会比"低强度思考"更强，
   破坏强度单调性）。
2. 当 target 无真实思考档（真实思考档序列为空，即 `[]` 或 `["off"]`）时，`off_alias` 不被
   消费——任何思考/关闭意图统一走 STRIP。

## 3. 档名词表

`effort_enum`/`off_alias` 里的字符串，协议无关规范名，大小写不敏感）：
`off`（等价 `none`）/ `minimal` / `low` / `medium` / `high` / `xhigh` / `max`。未识别的档名
被忽略（容错，不报错）。`off` 与 `max` 都是强度全序的正式成员，无任何协议层专属特殊分支，走
跟 `low`/`medium`/`high` 完全一样的相对映射路径。词表唯一权威在
`core/reasoning/ladder.py::_NAME_TO_CANONICAL`，codec 层零词表：encode 直接把 canonical
枚举名小写作为 wire 档名发出（`effort_enum` 声明什么就发什么），decode 用同一全表识别入站
档名（含 `max`/`minimal`），映射约束统一收在 remap。

budget 分档断点（Anthropic `thinking.budget_tokens` 语义）是全局固定常量，与上游厂商无关，
不支持 per-supply 自定义。

档位不确定时可在 `supply` 交互菜单里选 `[t]est` 向上游探测（结果不保证准确，供人工核实，
见 [CONFIG.md](CONFIG.md)「supply test 归因与探测」）；无法确定时应保持不配置（走默认 5 档兜底），不要凭猜测填窄档——窄档会
真实改变该 supply 收到的思考强度分布。

## 4. budget 断点

budget 分档断点（Anthropic `thinking.budget_tokens` 语义）是全局固定常量，与上游厂商无关，
不支持 per-supply 自定义。

## 5. source 能力为何挂 strategy

**为什么 source 能力挂在 strategy（按 client_token）下、而不是按表面模型名声明**：表面模型名
（`claude-opus`/`claude-sonnet`/`claude-haiku`）只是客户端请求体里的 tier 选择器字符串，会被
多个 SDK 共享——codex-cli 也固定发 `model="claude-sonnet"`（见 README「SDK 接入」的
install 逻辑），跟 Claude Code 发的 `claude-sonnet` 同名，但两者是不同的客户端接入、理应能配
不同档位声明。真正代表"哪个客户端接入"的身份是 `client_token`（一个 token 一条 strategy，
`install` 命令把它写入某个具体 SDK 的配置文件），所以 source 能力挂在 strategy 下，既绑定
客户端身份，又保留同一客户端下不同 tier 的档位差异。

**source 能力为什么是人工填、不像 supply 那样自动探测**：source 侧是"客户端表面模型自己有哪些
档位"，没有一个可发探测请求的真实上游端点（表面模型只是请求体里的字符串标签），因此
`strategy add`/`strategy edit` 对 `tiers_source_capability` 采用逐 tier 人工问答录入，不做
自动探测。target 侧（supply）才有真实上游可 `probe`。

## 6. 映射算法（排名比例公式 / 边界 / 单调 / off 吸收态）

链路：`decode(source 协议) → remap(source 能力, target 能力) → abstract_encode → syntax_adapt(target 协议)`。

**核心思想**：客户端的档位选择是相对自己表面模型的**排名**，会按比例映射到真实上游的档位排名，
而不是把档名钉死在全局绝对值上。"low"的真实含义是"该表面模型自己范围里排名最低的一档"，映射到
target 时按排名比例找 target 序列里对应位置的档。

- source 序列长度 m、target 序列长度 n：客户端意图在 source 序列里的排名 `i`（0-indexed），按
  `floor(i/(m-1) * (n-1) + 0.5)` 四舍五入映射到 target 序列排名 `j`，取 `target[j]`。
  - `n == 1`（target 只一档思考能力）：所有思考意图统一落到该档。
  - `m == 1`（source 只声明一档思考能力）：落到 target 序列的**中位档** `(n-1)//2`。
  - source 或 target 未声明能力：回退默认 5 档序列参与映射，行为退化为就近钳位。
- 映射结果单调不减：客户端意图越强，映射到 target 的档位绝不变弱（前提是配置遵守
  `off_alias` 约束）。
- `off`（关闭）是吸收态，**不参与**排名比例计算，单独走一条规则：target 有真实思考档则落到
  `off_alias`（进而 DISABLED），target 无真实思考档或未设 `off_alias` 则走 STRIP。

想深入了解相对映射的完整算法推导、单调性证明与决策记录，见
`docs/archive/reasoning_relative_remap_redesign.md`。

## 7. debug 旁路日志

默认关闭；启动前 `export MODEL_PROXY_REASONING_DEBUG=1` 可打开，把
"客户端意图 → 相对映射结果"逐请求写进日志，只影响本模块 logger（不支持热切换，改后需重启进程）。
