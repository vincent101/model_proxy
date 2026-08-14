> 本文是 [README.md](../README.md) 的深入展开。日常配置与使用见 README。

# 配置字段明细

## 1. 配置路径与权限

配置文件默认路径 `tools/model_proxy/config/model_proxy_config.json`（600 权限，已加入
`.gitignore`，不被 git 跟踪；含真实 appkey/admin_token，不要手动纳入版本控制）。可用环境变量
`MODEL_PROXY_CONFIG` 覆盖路径。`config/model_proxy_config.example.json` 是不含真实凭证的
样例，纳入跟踪。

session override 存放在独立 sidecar 文件 `config/session_overrides.json`（与主 config 同目录，
600 权限，已加入 `.gitignore`），代理独占写。

## 2. supplies 字段

一个供给单元 = 一个上游端点。

```json
{
  "id": "claude-sonnet-k0",
  "url": "https://aigc.sankuai.com/v1/anthropic/v1/messages",
  "protocol": "anthropic",
  "appkey": "<APPKEY_PLACEHOLDER>",
  "target_model": "claude-sonnet-5",
  "cooldown_seconds": 300,
  "reasoning_capability": {
    "effort_enum": ["low", "medium", "high", "xhigh", "max"]
  }
}
```

- `id`：唯一标识，routes 里按 id 引用。
- `url`：完整终态请求端点，代理不做任何拼接，直接原样作为出站请求 URL（只会在其后拼接
  经净化的原始 query，剔除 `beta` 参数）。
- `protocol`（可选）：`anthropic` / `chat` / `responses`。缺省时从 `url` 尾缀自动推断（详见
  下方「protocol 推断规则」）。
- `appkey`：鉴权用 Bearer token，注入到转发请求。
- `target_model`：实际下发给上游的模型名（客户端请求里的 model 字段会被替换成这个）。
- `cooldown_seconds`（可选）：该 supply 触发失败后的冷却时长，不填则用顶层
  `default_cooldown_seconds`。
- `reasoning_capability`（可选）：该 supply（target 侧，真实上游）支持的 effort 档位能力
  描述，字段完整语义见 [REASONING.md](REASONING.md)「effort_enum 字段语义」。不配置时用默认 5 档
  （`["off","low","medium","high","xhigh"]`），按 supply 独立生效，不影响其他 supply。

> 注意：Anthropic 协议上游用 `thinking.type=disabled` 表达"关闭思考"，不使用 `off`/`none`
> 档位；因此 anthropic supply 的 `effort_enum` 通常不含 `off`（关闭走 disabled 指令，不占一档）。
> Chat/Responses 协议域用 `reasoning_effort=none` / `reasoning.effort=none` 表达关闭（"none"
> 是该域关闭词的协议事实）。档名词表的唯一权威在 `ladder._NAME_TO_CANONICAL`，codec 层零词表：
> supply `effort_enum` 声明的档名即 wire 档名，代理照配置直发，不做写死字典二次过滤。

### supplies 字段速查表

| 字段 | 类型 | 必填 | 语义 | 默认值 |
|---|---|---|---|---|
| `id` | string | 必填 | 唯一标识，routes 里按 id 引用 | — |
| `url` | string | 必填 | 完整终态请求端点，代理不做拼接，只在其后拼接净化后的原始 query | — |
| `protocol` | string | 可选 | `anthropic`/`chat`/`responses`，缺省从 url 尾缀推断，推断不出报 500 | 从 url 推断 |
| `appkey` | string | 必填 | 鉴权用 Bearer token，注入转发请求 | — |
| `target_model` | string | 必填 | 实际下发给上游的模型名 | — |
| `cooldown_seconds` | number | 可选 | 触发失败后的冷却时长 | 顶层 `default_cooldown_seconds` |
| `reasoning_capability.effort_enum` | string[] | 可选 | target 侧真实支持的 effort 档位有序列表 | 默认 5 档 `off/low/medium/high/xhigh` |
| `reasoning_capability.off_alias` | string | 可选 | 关闭动作具体落到哪个档位 | 含 off 则落 off，否则不设（走 STRIP） |

## 3. routes 字段

家族模板 = 一个 route id + 三档（opus/sonnet/haiku）各自的 supplies 列表。

```json
{
  "id": "claude",
  "tiers": {
    "opus": ["claude-opus-k0"],
    "sonnet": ["claude-sonnet-k0"],
    "haiku": ["claude-haiku-k0"]
  },
  "failover": "on"
}
```

- `id`：家族唯一标识，strategies 里按 id 引用。route 本身不含 client_token，只是一个可复用
  的家族模板。
- `tiers`：固定三档 `opus`/`sonnet`/`haiku`，每档一个按优先级排列的 supply id 列表，取第一个
  「未冷却」的。
- `failover`：`on` 时上游返回 401/403/429/5xx 会把当前 supply 打入冷却并换同档下一个再试；
  `off` 时失败直接返回给客户端，不重试。failover 是 route（家族）级开关，不细分到 tier。

**多档共享同一组真实上游**：不是每个家族都天然有三种不同能力的模型。某家族只有两种真实模型时，
多个 tier 可以直接填同一组 supply id 列表，不必引入额外抽象层。例如 deepseek 家族只有
`pro`/`flash` 两种真实模型，`opus`/`sonnet` 都打 `pro`，`haiku` 打 `flash`：

```json
{
  "id": "deepseek",
  "tiers": {
    "opus":   ["ds-pro-k1", "ds-pro-k0"],
    "sonnet": ["ds-pro-k1", "ds-pro-k0"],
    "haiku":  ["ds-flash-k1", "ds-flash-k0"]
  },
  "failover": "on"
}
```

供给单元命名建议按真实能力取名（如 `ds-pro-k0`/`ds-flash-k0`），不要用 `opus`/`sonnet`
这类档位词——档位是请求侧的分类，不代表上游真有对应数量的独立模型。

### routes 字段速查表

| 字段 | 类型 | 必填 | 语义 | 默认值 |
|---|---|---|---|---|
| `id` | string | 必填 | 家族唯一标识，strategies 里按 id 引用 | — |
| `tiers.opus` / `tiers.sonnet` / `tiers.haiku` | string[] | 必填（三档固定） | 每档一个按优先级排列的 supply id 列表 | — |
| `failover` | `"on"`/`"off"` | 必填 | on：失败换同档下一 supply；off：失败直接返回 | — |

## 4. strategies 字段

```json
{
  "client_token": "cc",
  "route_id": "claude",
  "tiers_source_capability": {
    "opus":   {"effort_enum": ["low", "medium", "high", "xhigh", "max"]},
    "sonnet": {"effort_enum": ["low", "medium", "high", "max"]},
    "haiku":  {"effort_enum": ["low", "medium", "high", "max"]}
  },
  "note": "默认 Claude 家族（Claude Code SDK）"
}
```

- `client_token`：客户端请求鉴权头里的 token，代理据此找到对应 strategy（提取规则见
  [ARCHITECTURE.md](ARCHITECTURE.md)「入站鉴权识别」，`Authorization: Bearer <token>` 与
  `x-api-key: <token>` 两种写法均支持）。
- `route_id`：该 token 绑定的 route 家族 id，必须是 routes 里存在的 id。运行时切换家族用
  `switch <token> <route_id>` 改这个字段。**与 `route_pool` 二选一、互斥**（见下方「route_pool
  哈希分配」）。
- `tiers_source_capability`（可选）：该 client_token 各 tier 的 source 侧 reasoning 能力声明，
  结构与 supply 的 `reasoning_capability` 同构、解析逻辑复用同一套；某 tier 未声明或整条
  strategy 无此字段则回退默认 5 档。字段语义、为何挂 strategy、为何人工填详见
  [REASONING.md](REASONING.md)。
- `note`：可选备注。
- 禁用一个 token 直接删除其 strategy 记录即可（无 enabled 开关）。

### strategies 字段速查表

| 字段 | 类型 | 必填 | 语义 | 默认值 |
|---|---|---|---|---|
| `client_token` | string | 必填 | 客户端鉴权 token，代理据此查找 strategy | — |
| `route_id` | string | 与 `route_pool` 二选一 | 绑定的单个 route 家族 id；与 `route_pool` 互斥（同时出现写入侧拒绝，运行时容错按 route_pool 处理并打 warning） | — |
| `route_pool` | `{route_id, weight}[]` | 与 `route_id` 二选一 | 按 session 一致性哈希分配的候选 route 列表；`weight` 可选，默认 1，非正整数静默视为 1 | — |
| `dispatch.session_overrides` | — | 已移除 | session override 已迁移到独立 sidecar 文件 `config/session_overrides.json`，不再是 strategy 字段。通过 `$route` 命令或直接编辑 sidecar 维护 | — |
| `dispatch.type` | string | 可选 | 预留分配策略扩展位，当前代码不读取该字段，只有一种哈希分配算法 | 不生效 |
| `dispatch.fallback` | string | 可选 | 预留字段，当前代码不读取，session_key 缺失时行为固定为「route_pool 首项」，写该字段不改变实际行为 | 不生效 |
| `tiers_source_capability.<opus\|sonnet\|haiku>.effort_enum` | string[] | 可选 | 该 tier 的 source 侧 effort 档位有序列表 | 默认 5 档 |
| `tiers_source_capability.<opus\|sonnet\|haiku>.off_alias` | string | 可选 | 同 supply 语义 | 同 supply 缺省规则 |
| `note` | string | 可选 | 备注 | — |

## 5. 顶层字段

| 字段 | 类型 | 必填 | 语义 |
|---|---|---|---|
| `admin_token` | string | 必填 | 控制 API 鉴权，供 `X-Proxy-Admin-Token` 请求头校验 |
| `default_cooldown_seconds` | number | 必填 | supply 未单独配置 `cooldown_seconds` 时的默认冷却时长 |
| `upstream_timeout_seconds` | number | 可选 | 上游请求超时（秒），缺省 1800（30min，对齐 API_TIMEOUT_MS） |
| `budget_retry` | object | 可选 | ④b 输出预算自动放大重试：`{"enabled": true, "max_retries": 5}`，缺省全开；封顶 131072 硬编码不暴露；无 per-supply 维度 |

完整样例见 `config/model_proxy_config.example.json`。

## 6. protocol 推断规则

`supply["protocol"]` 是可选字段，唯一权威解析逻辑在
`core/reasoning/registry.py::resolve_protocol`，优先级：

1. 显式 `supply["protocol"]`（须是下表合法值之一，否则报错）。
2. 未显式配置时，从 `supply["url"]` 的路径尾缀推断（三者互斥，无公共后缀，可靠判断）：

   | url 尾缀 | protocol |
   |---|---|
   | `/v1/messages` | `anthropic` |
   | `/v1/responses` | `responses` |
   | `/chat/completions` | `chat` |

3. 两者都推断不出（url 尾缀不属于上表、且未显式配置 protocol）→ 直接抛错，代理返回明确的
   500 错误响应，不做任何猜测性兜底——避免请求悄悄走错转换分支产生难排查的错误响应。

## 7. route_pool 哈希分配

一个 client_token 除了绑单个 route（`route_id`），也可以绑一个 `route_pool`（多个候选
route），按 Claude Code 会话 session_id 做一致性哈希分配——同一会话稳定落到同一个 route，不同
会话打散到 route_pool 内多个 route，用于摊开配额/让不同会话用到不同后端组合。

```json
{
  "client_token": "cc",
  "route_pool": [
    {"route_id": "claude", "weight": 1}
  ],
  "note": "默认 Claude 家族（Claude Code SDK）"
}
```

- **`route_pool`**（数组，`{route_id, weight}`）：与 `route_id` 二选一、互斥。同时配置两者会被
  `_config_ops.py` 的 `strategy add`/`edit` 拒绝写入；若绕过 CLI 直接改配置文件导致两者同时
  存在，运行时（`extract_route_candidates`）会忽略 `route_id`、按 `route_pool` 处理并打
  warning 日志，不会中断请求。
  - `weight`（可选，默认 1）：参与一致性哈希的权重。非正整数会被静默视为 1（不报错、不把该
    route 排除在外）。
  - `route_pool` 里引用了不存在的 `route_id` 会被跳过并打 warning 日志，不拖垒整条 strategy；
    若全部条目都非法，该 strategy 无可用候选，请求 401（no strategy/route matched）。
- **`dispatch.session_overrides`**（已移除）：session override 的存储已迁移到独立 sidecar 文件
  `config/session_overrides.json`，不再作为 strategy 的字段。通过 `$route` 命令或直接编辑
  sidecar 文件维护，优先级高于自动哈希分配。**override 的 route_id 只需存在于顶层 `routes`
  定义里，不要求也出现在这条 strategy 的 `route_pool` 列表内**——这是有意允许的「例外指定」，
  用于临时把某个会话导到 route_pool 之外的 route 做调试/隔离。
- **session_key 怎么来的**：从 CC 请求体 `metadata.user_id` 字段（一个 JSON 字符串）里解析出
  `session_id`（`extract_session_key`）。同一个 CC 会话（含它派生的 Task 子agent 请求，子agent
  复用父会话 session_id）全程稳定不变，不同会话不同。
- **自动分配算法**：一致性哈希——`md5(session_key) % 权重总和` 定位到 `route_pool` 里的主选
  route，其余候选按权重区间顺序（从主选处整体旋转）跟在后面作为兜底候选。同一 session_key
  多次计算结果恒定；不同 session_key 会打散到不同 route。当前只实现这一种分配算法
  （`dispatch.type` 是预留扩展位，代码里目前不读取该字段，无论写什么都按同一套哈希逻辑跑）。
- **session_key 缺失时的行为**（未取到 session_id，如非 CC 客户端请求）：固定回退到
  `route_pool` 首项作为主选，其余按 `route_pool` 原顺序跟随作为兜底候选。这一行为当前是
  硬编码，`dispatch.fallback` 是预留字段，代码里不读取，写它不会改变实际行为。
- **route 全挂时跨 route 兜底**：当前候选 route 下该 tier 的所有 supply 都不可用（缺 tier 配置，
  或全部冷却/失败）时，自动换 `route_pool` 里按哈希排出的下一个候选 route 重试，直到某个候选
  可用，或全部候选耗尽后最终返回 503。发生这种跨 route 切换时，ACCESS 日志记
  `route_failover=1`（区别于同一个 route 内部换 supply 的 `failover=1`）。单候选（旧单值
  `route_id` 写法）时这条外层循环只跑一轮，不产生 `route_failover`，行为与改动前完全一致。
- **ACCESS 日志新增字段**：`session=<session_id或空>`（该请求解析出的 session_key，取不到为
  空串）、`route_failover=<0或1>`（是否发生了跨 route 兜底）。

## 8. session override sidecar（含 7 天清理）

override 写入独立文件 `config/session_overrides.json`（与主 config 同目录，600
权限，已加入 `.gitignore`，不被 git 跟踪），**代理独占写**，主 config 不含
session override 字段：

```jsonc
{
  "cc": {
    "3f2a9c1e-...": {"route_id": "nation", "last_seen": "2026-08-06T10:00:00Z", "created": "2026-08-05T19:00:00Z"}
  }
}
```

顶层按 `client_token` 分组，与每条 strategy 一一对应。session override 的唯一来源
就是 sidecar 文件，不再有主 config 基线 + sidecar 合并的双来源机制。人工可直接编辑
该 sidecar 文件（支持旧式纯字符串和新式 dict 两种写法，代理读取时均兼容）。

sidecar 文件缺失视为 `{}`（首次运行的正常状态，不报错）；内容非法 JSON 时保留上一次
成功加载的内存值并打 warning，不中断请求（与主 config 的既有容错口径一致）。

### 7 天静默清理

sidecar 里的 override 若连续 **7 天**没有被任何请求命中（`last_seen` 判据），会在
下一次 `$route <id>` 或 `$route reset` 执行成功时被顺带清理（清理与本次变更是同一次
原子写，不产生中间态）。要点：

- **只清 sidecar**——sidecar 是唯一存储，主 config 不含此字段。
- **无 `last_seen` 的条目不参与清理**（含人工手改 sidecar 时写成的旧式纯字符串
  value、以及手工塞进 sidecar 的条目）——不能把「没有时间戳」当作「已过期」。
- **当前正在执行命令的 session 永不被清理**。
- **不静默删除**：清理了哪些条目会在 `$route` 的回执里列出（数量 + session 短 id），
  发现误删可立刻 `$route <id>` 恢复（代价有界且可逆，这是采用较宽松阈值的前提）。
- 阈值曾评估过 48 小时，但实测现网 5 条手工 override 里有 3 条内部最大空档超过 48
  小时（仍在活跃使用），会被误删；改用 7 天后候选仅剩 3 个，风险可控。
- `last_seen` 采用**内存记账 + 随写操作落盘**：命中 override 的普通请求只更新内存
  （无写盘 IO），只有 `$route` 写操作才会把内存值刷入 sidecar 并落盘。代价是进程
  重启会丢失「上次重启以来的活跃记录」，最坏导致一次可恢复的误删。

## 9. strategy 录入流程

`strategy add`/`edit` 录入 `tiers_source_capability` 时逐 tier 人工问答（source 侧无可探测
的真实上游，只能人工填，原因见 [REASONING.md](REASONING.md)「source 能力为何挂 strategy、为何人工填」）：输入逗号分隔列表→对应
`effort_enum`；输入 `-`→空列表 `[]`（该 tier 无思考能力）；留空→add 时不写该 tier 键（走默认
5 档）、edit 时保留原值。默认值绝不物化写入配置。

## 10. supply test 归因与探测

`supply` 交互菜单 `[t]est`：整合原 `supply probe`/`supply test` 为单一交互入口，全流程只发
一次上游请求。先做连通性测试：把结果细分为 DNS解析失败/连接超时/连接拒绝/其他网络异常/鉴权
失败（401/403）/模型配置错误（404 或 400+模型相关关键词）/连通正常（REACHABLE）/未知
（429/5xx等）几类，分别打印明确归因，帮助判断问题具体出在网络、appkey 还是 target_model；
不连通则打印归因直接结束，不写入 `reasoning_capability`。只有判定连通正常（REACHABLE）才
继续 effort 探测：向 supply 上游发一个已知非法的 effort 值（`__probe_invalid__`），按
protocol 构造探测请求（anthropic 走 `output_config.effort`、chat 走 `reasoning_effort`、
responses 走 `reasoning.effort`），从报错响应里用宽松正则尝试提取"Supported values are: ..."
之类枚举并展示（响应原文完整打印，不截断），最后统一询问是否接受写入 `reasoning_capability`。
解析结果不保证准确（供应商报错格式差异大），需人工核实后再接受；探测无结论时会提示用户自行
查阅目标模型官方文档确认支持的分档，再进入人工输入环节手动填。`supply add`/`edit`（重新探测
环节）内部同样先跑这次连通性测试：若判定 REACHABLE，直接复用该次响应做 effort 档位解析，
不再发第二次探测请求；否则打印归因并跳过（`add` 时会二次确认是否仍要保存该 supply）。

## 11. stats 账本 schema

累计用量账本（schema v3）：ACCESS 日志会在进程启动时被 `_trim_log` 截断到最后 5000 行，早期行
永久丢失，无法回答「本月/某天累计用了多少 token」这类长期问题。为此另建一个独立账本文件
`.model_proxy_totals.json`（与日志文件同目录），每请求在 `_forward_logged` 收口处同步累加、
原子写盘：按天分桶，桶内以 `supply×route×strategy` 组合键（形如
`supply=<s>|route=<r>|strategy=<t>`，strategy 段是 client_token 明文如 `cc`/`codex`）累加
`requests`/`ok`/`fail`/`usage_in`/`usage_out`/`max_ms`/`attempts`/`attempt_fail`，另存 `total`
全历史汇总。`max_ms`（OPT-10）入账本，CLI stats 的 max_ms 从账本取，不再依赖日志窗口；
`attempts`/`attempt_fail`（OPT-10）为 attempt 级计数——failover 中间失败计入对应 supply（仅盖
3 处 failover continue，budget 重试按 §5a 决策不记账）。该字段只落账本 JSON，CLI stats 暂不投影。账本 **只增不截**，
不受进程重启与日志截断影响。天分桶只保留最近 `KEEP_DAYS=400` 天，超窗旧天桶汇总进 `months_archive`
月归档节点（永久保留）。天/月边界固定按 UTC+8 划分（`timezone(timedelta(hours=8))`，不依赖系统时区）。
schema 升级（v2→v3）在 `_load` 启动时自动迁移：旧桶 combo 补 `attempts=0`/`attempt_fail=0`、
bucket 补 `max_ms=0`（断档但保真，不虚高），既有值保留，记 `usage_totals.migrated` INFO 日志。
**迁移与重启顺序**：先停旧进程→起新进程（启动时迁移），避免旧进程写旧 schema 覆盖新进程已迁移的文件。
账本供 `stats` 命令查询（见 README「CLI 命令速查」），与 ACCESS 日志完全独立。账本结构细节见设计记录
`docs/designs/2026-07-23-usage-totals-ledger.md` + `docs/designs/2026-08-08-log-optimization-plan.md`。
