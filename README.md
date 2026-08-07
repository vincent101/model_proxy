---
created: 2026-07-17 18:52:18
---
# model_proxy

## 1. 这是什么

本地多协议 AI 模型代理，端口 18889（默认，可配）。同时支持 Claude Code（Anthropic
`/v1/messages`）、codex-cli（OpenAI Responses `/v1/responses`）等多个 SDK 接入，并可跨协议
互相访问对方生态的模型——例如在 Claude Code 里实际调用 GPT，在 codex 里实际调用 Claude。

v1（`tools/proxy.py`，端口 18888）已于 2026-07-24 下线，代码归档于
`tools/model_proxy/history_versions/proxy-v1-archived-20260724.tar.gz`。

## 2. Quick Start

三步让 Claude Code 通过本代理跑起来（假设已配好 `config/model_proxy_config.json`，
至少含一条 `client_token` 的 strategy，未配则先看「核心概念：三段式配置」「接入各 SDK
（install）」）。

**① 启动代理**
```bash
tools/model_proxy/model_proxy_cli.sh on
```
预期输出：
```
Starting model_proxy.py on port 18889...
model_proxy started (pid <PID>), ready (N * 0.5s)
```
（已在监听则输出 `model_proxy already running on port 18889`。）

**② 接入 Claude Code**
```bash
tools/model_proxy/model_proxy_cli.sh install
```
交互式列出四个 SDK 与本机检测状态，选 `claude` 对应序号（如 `0`），
再从协议匹配的候选 client_token 里选一个。确认 diff 后写入
`~/.claude/settings.json` 的 `env`：
```
ANTHROPIC_BASE_URL=http://localhost:18889/
ANTHROPIC_AUTH_TOKEN=<你选的 client_token>
ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus
ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet
ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku
```
（写入前会先备份原文件到 `settings.json.bak.<时间戳>`。）

**③ 重启 Claude Code 生效并验证**
重启 Claude Code 后，用 `logs` 看是否有转发记录：
```bash
tools/model_proxy/model_proxy_cli.sh logs
```
出现形如 `ACCESS ms=... status=200 source=anthropic route=claude tier=... supply=... token=...`
的行即接入成功。想看运行状态与配置概览用 `model_proxy_cli.sh status`。

日常挂载：`tools/model_proxy/hooker/ensure_model_proxy.sh` 已注册到 SessionStart hook，
每次开 Claude Code 会话自动拉起，一般无需手动 `on`（详见「启动与停止」）。

（注：`install` 会先跑 `ensure_session_hook` 确保 SessionStart 里存在正确 hook 条目，此点在
「启动与停止」「接入各 SDK（install）」说明，Quick Start 不展开。）

## 3. 核心概念：三段式配置

配置文件默认路径 `tools/model_proxy/config/model_proxy_config.json`（600 权限，已加入
`.gitignore`，不被 git 跟踪；含真实 appkey/admin_token，不要手动纳入版本控制）。可用环境变量
`MODEL_PROXY_CONFIG` 覆盖路径。`config/model_proxy_config.example.json` 是不含真实凭证的
样例，纳入跟踪。

### 3.1 三段关系总览

配置由三种可复用单元组成，按 id 层层引用：

```
[strategies]                  [routes]                       [supplies]

client_token: "cc"            id: "claude"                   id: "claude-sonnet-k0"
route_id: "claude"  --------> tiers:                          url / protocol
tiers_source_                   opus:   [id, ...]  --------->  appkey / target_model
  capability (optional)         sonnet: [id, ...]   (by id)    reasoning_capability
                                 haiku:  [id, ...]              (optional)
                               failover: on / off

一个 client_token               家族模板，不含 token，              一个 id = 一个上游端点，
对应一条 strategy               可被多条 strategy 复用               多个 route 可共享同一个 supply
```

上面是单值 `route_id` 写法（一个 client_token 固定绑一个 route）。strategies 也可以改用
`route_pool`（数组）绑多个 route，按 CC 会话 session_id 做一致性哈希分配，两者字段互斥，
详见 3.4 节：

```
[strategies]
client_token: "cc"
route_pool: [{route_id:"claude",weight:2}, {route_id:"nation",weight:1}]  --▶ 按 session 分配到其中一个 route
```
session override 的唯一来源是独立 sidecar 文件 `config/session_overrides.json`，通过 `$route` 命令
或直接编辑该文件维护，不再是 strategy 的字段（详见 3.4 节）。

- **supplies**：每条 = 一个上游端点（url + 协议 + appkey + target_model + 可选能力）。
- **routes**：家族模板，固定 opus/sonnet/haiku 三档，每档一个按优先级排列的 supply id 列表，
  取第一个未冷却的；route 本身不含 token，可被多条 strategy 复用。
- **strategies**：把某个 client_token 绑定到某个 route——可以绑单个 route（`route_id`），也可以
  绑一个 route_pool 做 session 级分配（`route_pool` + `dispatch`）；运行时切换单值写法用
  `switch` 改 route_id。

请求匹配时反向走这条链：token → strategy → route → tier → supply（见「三阶段匹配」）。

### 3.2 supplies：供给单元

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
  描述，字段完整语义见「reasoning 强度映射（深入）」§ 6.1。不配置时用默认 5 档
  （`["off","low","medium","high","xhigh"]`），按 supply 独立生效，不影响其他 supply。

字段完整明细见附录 A：配置字段速查表。

> 注意：Anthropic 协议上游用 `thinking.type=disabled` 表达"关闭思考"，不使用 `off`/`none`
> 档位；因此 anthropic supply 的 `effort_enum` 通常不含 `off`（关闭走 disabled 指令，不占一档）。
> Chat/Responses 协议域用 `reasoning_effort=none` / `reasoning.effort=none` 表达关闭（"none"
> 是该域关闭词的协议事实）。档名词表的唯一权威在 `ladder._NAME_TO_CANONICAL`，codec 层零词表：
> supply `effort_enum` 声明的档名即 wire 档名，代理照配置直发，不做写死字典二次过滤。

#### protocol 推断规则

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

### 3.3 routes：家族模板

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

字段完整明细见附录 A：配置字段速查表。

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

### 3.4 strategies：client_token → route 绑定

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

- `client_token`：客户端请求鉴权头里的 token，代理据此找到对应 strategy（提取规则见「入站
  鉴权识别（client_token 提取）」，`Authorization: Bearer <token>` 与 `x-api-key: <token>`
  两种写法均支持）。
- `route_id`：该 token 绑定的 route 家族 id，必须是 routes 里存在的 id。运行时切换家族用
  `switch <token> <route_id>` 改这个字段。**与 `route_pool` 二选一、互斥**（见下方「按 session
  分配到多个 route（route_pool）」）。
- `tiers_source_capability`（可选）：该 client_token 各 tier 的 source 侧 reasoning 能力声明，
  结构与 supply 的 `reasoning_capability` 同构、解析逻辑复用同一套；某 tier 未声明或整条
  strategy 无此字段则回退默认 5 档。字段语义、为何挂 strategy、为何人工填详见「reasoning
  强度映射（深入）」§ 6.1 / § 6.2。
- `note`：可选备注。
- 禁用一个 token 直接删除其 strategy 记录即可（无 enabled 开关）。

字段完整明细见附录 A：配置字段速查表。

#### 按 session 分配到多个 route（route_pool）

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
（以上结构改写自生产真实配置，appkey/admin_token 等凭证字段已脱敏省略。）

session override 不再写在 strategy 里，而是存放在独立 sidecar 文件
`config/session_overrides.json`，结构为 `{"<client_token>": {"<session_id>": {"route_id": "...", "last_seen": "...", "created": "..."}}}`，
通过 `$route` 命令或直接编辑该文件维护。

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

## 4. 请求处理流程

### 4.1 端到端链路总览

一个客户端请求进入代理到发往真实上游，经过下面这条链路。各环节的细节见对应子节。

```
客户端请求
  │  Authorization: Bearer <token>  或  x-api-key: <token>
  │  POST <任意 path>   body: {"model": "claude-sonnet", ...}
  ▼
┌────────────────────────────────────────────────────────────────────┐
│ ① 入站鉴权识别 extract_client_token         （见「入站鉴权识别」） │
│     Bearer 优先→回退 x-api-key→都无则空 token→401                  │
│     → client_token                                                 │
├────────────────────────────────────────────────────────────────────┤
│ ② 入站协议识别 detect_source                （见「入站协议识别」） │
│     path 尾缀（大小写不敏感）→ body 特征兜底 → unknown             │
│     → source ∈ {anthropic, responses, chat, unknown}               │
├────────────────────────────────────────────────────────────────────┤
│ ③ 三阶段匹配                                  （见「三阶段匹配」） │
│     a. client_token ──查 strategies──▶ route_id 直选                │
│        或 route_pool ──session_hash/override──▶ route 候选列表    │
│     b. body.model ──精确查表──▶ tier(opus/sonnet/haiku)            │
│     c. 候选 route 逐个：route.tiers[tier] ──▶ supplies 列表         │
│        ──同route内failover──▶ supply；该候选全挂──▶ 换下一候选     │
│        route（route_failover）                                     │
│     （查不到 strategy→401 / tier 非预设→400 / 候选耗尽仍无可用     │
│     supply→503）                                                    │
├────────────────────────────────────────────────────────────────────┤
│ ④ effort 映射                          （见「effort 跨模型映射」） │
│     decode(source) → remap(source_cap, target_cap)                 │
│       → abstract_encode → syntax_adapt(target)                     │
│     supply.protocol 决定 target 协议（见「protocol 推断规则」）    │
├────────────────────────────────────────────────────────────────────┤
│ ⑤ 出站转换 / 转发                                                  │
│     (source,target) 组合 → PASSTHROUGH 或 转换（core/translate）   │
│     出站 URL = supply.url + 净化 query（丢客户端 path、剔 beta）   │
│     出站头双发 Authorization: Bearer <appkey> + x-api-key          │
│     失败(401/403/429/5xx)且 failover=on → 冷却+换同档下一 supply   │
└────────────────────────────────────────────────────────────────────┘
  ▼
真实上游（supply.url / supply.target_model）
```

关键点：入站阶段代理不关心客户端把 base_url 后面拼了什么 path（③ 用 body.model
选 tier，出站 ⑤ 只用配置的 `supply.url`），所以各 SDK 的 path 拼接差异不影响转发目标。

### 4.2 入站鉴权识别（client_token 提取）

`client_token` 从入站请求头提取（`extract_client_token`），兼容两种生态各自的标准鉴权写法：

- `Authorization: Bearer <token>`（OpenAI Chat Completions/Responses API 标准方式）
- `x-api-key: <token>`（Anthropic 原生 API 标准方式，无 Bearer 前缀）

优先级：`Authorization: Bearer` 优先，缺失或非 Bearer 前缀（如 `Basic ...`）则回退
`x-api-key`；两者都提供且值不同时取 `Authorization`（这里 `client_token` 只是路由查表键，
无密钥校验语义，不因此报错，与出站转发时同一 appkey 双发 `Authorization`+`x-api-key` 保持
对称）；都没有则视为空 token，查不到任何 strategy，401。另有两条边界处理：`Bearer` scheme
大小写不敏感（RFC 6750）、两种写法取到的值都会 `strip` 首尾空白。

只支持这两种写法，不识别 Azure OpenAI 风格的 `api-key`（无 `x-` 前缀）等其他生态专属写法——
当前接入的客户端未见这类需求，避免为不存在的场景增加解析面。

### 4.3 入站协议识别（detect_source）

入站 source 协议由 `detect_source` 按客户端请求 path 尾缀判断（大小写不敏感）：
`/v1/messages`→anthropic、`/v1/responses`→responses、`/chat/completions`→chat；三者互斥
的尾缀都不命中时，退而看请求体特征（`input` 字段→responses；`messages`+`max_tokens`/
`system`→anthropic；仅 `messages`→chat）；仍不中则 `unknown`，走该组合的兜底/501。

客户端把 `base_url` 配到本代理后，无论自己在其后拼了什么路径（根路径、`/v1/messages`、
`/chat/completions`，甚至多余的斜杠如 `//chat/completions`）都不影响识别；出站转发时统一
丢弃客户端 path，只用配置好的 `supply.url` + 净化后的 query 拼真实上游请求（见
`_sanitize_forward_query`），代理自己决定往上游发什么路径，与客户端怎么拼无关。

### 4.4 三阶段匹配

请求进来后：

1. 用 `client_token` 查 strategies 拿到该 strategy 的 route 候选列表（`extract_route_candidates`）：
   - 旧写法 `route_id`（单值）：候选列表只有这一个 route（或该 id 不存在则候选为空）。
   - 新写法 `route_pool`（多值）：按 sidecar 的 session override 优先匹配、否则按
     session_key 一致性哈希，排出一个有序候选列表（详见 3.4 节「按 session 分配到多个
     route（route_pool）」）。
   - 候选列表为空 → 401（no strategy/route matched）。
2. 把请求体 `model` 字段精确查表映射成 tier 名（`claude-opus`→opus / `claude-sonnet`→
   sonnet / `claude-haiku`→haiku，仅这三个精确值，非子串猜测）。tier 解析只与 `model` 有关，
   与候选哪个 route 无关。
3. 按候选列表顺序逐个尝试 route：取该 route 的 `tiers[tier]` supplies 列表，交给同 route 内
   failover 逐个选未冷却的 supply；若该候选 route 缺 tier 配置或该 tier 下所有 supply 都不可用，
   换下一个候选 route 重试（记 `route_failover=1`），直到某候选可用或候选耗尽。

`model` 字段不是上述三个预设值之一时，选路直接 400 失败，不兜底。`settings.json` 里的
`ANTHROPIC_DEFAULT_OPUS_MODEL`/`_SONNET_MODEL`/`_HAIKU_MODEL` 固定填
`claude-opus`/`claude-sonnet`/`claude-haiku`；切换单值写法的家族用 `switch`，不动 model 标签。

### 4.5 effort 跨模型映射

链路：`decode(source) → remap(source_cap, target_cap) → abstract_encode → syntax_adapt(target)`。

核心思想：客户端的档位选择是相对自己表面模型的排名，会按比例映射到真实上游的档位排名，而不是
把档名钉死在全局绝对值上。

完整公式、边界条件与单调性详见「reasoning 强度映射（深入）」§ 6.3。

### 4.6 内建命令层：`$route` in-band 指令

设计文档：[[docs/designs/2026-08-04-in-band-route-command-design]]（已 confirmed）。

代理在对话内识别一种特殊「指令消息」，拦截后不转发上游，自己合成响应——把「翻
ACCESS 日志抄 session_id → 改配置 → 存盘」这个手工流程变成对话里打一行指令。

#### 语法

```
$route <id>     把当前 session 钉到 <id>，下一条消息起生效
$route          查询当前 session 生效的 route 及其来源
$route reset    清除 override，落回自动哈希分配
```

**匹配规则很严格（宁可漏识别，也不可误识别）**：取最后一条 `role=user` 消息的
最后一个 text 块，剥离 Claudian 追加的 `<current_note>` 等六种上下文标签后，必须
**单行** + 首 token **精确等于** `$route`（大小写敏感）+ **token 数 ≤ 2**。任一条件
不满足，一律照常转发（fail-open），绝不吞用户消息、绝不误判。

**前缀可达性属持续性外部依赖**：`$` 前缀经实测在 Claude Code CLI 与 Claudian 两个
客户端均无拦截（`!`/`#` 均被证伪，见设计文档 §1）。但客户端后续升级可能新增拦截逻辑，
或 Claudian 若启用 codex provider 且技能名/描述含 `route` 子串，会导致 `$route` 的
Enter 被下拉框吞掉（当前因 codex provider 未启用而无风险，见设计文档 §1.3 注）。这条
可达性不是一次性验证完就永久成立，日后语法失效时先检查客户端是否有变化。

#### 生效语义与作用范围

- 只对 `source == "anthropic"` 生效；codex 侧（Responses 协议）首版不支持，发送
  `$route` 会被当普通文本转发上游（无害）。
- 当前请求本身被拦截、不打上游，因此**「下一条消息起生效」是唯一语义**——回执会
  明确写这一点。
- `<id>` 允许指向 `route_pool` 之外的顶层 `routes` 定义（沿用既有 session override
  的「例外指定」语义，见 §3.4）；`<id>` 不存在时报错并列出全部可用 id。
- **`client_token` 对应的 strategy 若仍是旧式单值 `route_id` 写法（无 `route_pool`）**，
  `$route <id>`/`reset` 会被拒绝（不写盘）：这种旧写法的路由选择不读取
  session override，写入 override 不会产生任何效果，若允许写入会造成「回执说
  切换成功、实际路由纹丝不动」的假成功。需先把该 strategy 迁移到 `route_pool`
  写法才能使用 `$route` 切换/reset（查询命令不受此限制）。
- 命令的匹配、写盘、清理都只作用于**代理自身路由/观测状态的本地文件**——命令层
  明确禁止执行外部命令、读写 sidecar/主 config 以外的文件、代理请求转发、任何网络
  动作（`core/commands.py`/`core/server.py` 内有对应代码注释）。

#### 落盘：sidecar 文件

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

#### 7 天静默清理

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

#### 查询命令的价值

`$route`（无参）是纯读命令，不触发清理，零副作用。用户平时完全看不到自己被分到
哪个 route（哈希分配是静默的），这条命令是排查「为什么消息打到了某个奇怪的后端」
的主要手段，回执包含当前生效 route、来源（sidecar / 自动哈希）、
可用 route id 列表、该 strategy 下 override 总条数。

#### 扩展性

代理内部是一层通用「命令名 → handler」注册表（`core/commands.py::COMMAND_HANDLERS`），
首版只注册 `$route` 一个命令。日后若要加 `$status`（查看 supply/冷却状态）等命令，
只需注册一个新 handler，不需要改动拦截点/响应合成/ACCESS 记录的骨架逻辑。

## 5. 运维与控制

### 5.1 启动与停止

```bash
tools/model_proxy/model_proxy_cli.sh on
```

或手动：

```bash
MODEL_PROXY_PORT=18889 python3 tools/model_proxy/model_proxy.py &
```

端口默认 18889，可用 `MODEL_PROXY_PORT` 环境变量覆盖。日志写到本目录
`.claude_model_proxy.log`（启动时自动截断保留最后 5000 行），进程锁在
`/tmp/claude_model_proxy.lock`（防止同时起多个实例）。

`tools/model_proxy/hooker/ensure_model_proxy.sh` 已注册到 `.claude/settings.json` 的
`hooks.SessionStart`，随 Claude Code 会话启动自动拉起（幂等：已运行则直接退出，未运行则启动
并等待就绪最多 5 秒；PID 文件 `/tmp/claude_model_proxy.pid`、锁 `/tmp/claude_model_proxy_start.lock`）。
v1 代理（18888）已于 2026-07-24 下线归档，不再涉及并行关系。这条 hook 的路径正确性由 `install` 流程负责
维护——`install` 每次运行都会检测 `SessionStart` 里是否存在一条正确指向当前 model_proxy 实际
安装位置的 hook 条目，缺失/路径错误（如目录被移动过）时清理旧条目并预览确认后补齐，不需要
手动同步维护这条硬编码路径。

停止用 `model_proxy_cli.sh off`：只按本脚本同目录下 `model_proxy.py` 的绝对路径精确匹配进程，
并额外反查监听该端口、命令行含 `model_proxy.py` 的 PID 兜底（v1 代理已于 2026-07-24 下线归档，
不再存在该进程）。

### 5.2 日志与观测

日志除 WARNING 级别（异常/降级路径，如 no route、cooldown+failover、stream interrupted 等）外，
还有一条 INFO 级别的 `ACCESS` 访问日志：每个转发请求（不含 `/model_proxy/*` 控制端点）结束时
记一条，覆盖整个请求生命周期，字段为
`ms status source route tier supply failover attempts usage_in usage_out token session route_failover builtin`
（`ms` 为端到端耗时毫秒，`token` 为客户端 token 尾4位，`session` 为该请求解析出的 session_id
取不到为空串，`route_failover` 为 0/1 标记本次请求是否发生了「pin route 全挂后跨 route 兜底」，
区别于同 route 内换 supply 的 `failover`；`builtin` 为空表示普通转发请求，非空（当前只有
`route`）表示该请求被内建命令层拦截、未打上游，此时 `supply=(builtin)`、`route=` 记录命令
操作/查询后的生效 route，见「内建命令层」一节）。两者共用同一文件，用固定前缀 `ACCESS` 区分。

token 用量统计：转换模式（Anthropic↔Chat/Responses，流式+非流式）、PASSTHROUGH 非流式、
以及 PASSTHROUGH 流式（anthropic→anthropic、responses→responses 的流式请求）均会提取
`usage_in`/`usage_out` 填入 access 行。reasoning token 不再单独统计（原因见下）——协议转换时
仍如实把上游 reasoning 明细透传给下游消费者（`core/translate.py` 的 `_extract_reasoning_tokens()`
与各 adapter 的 `usage_tuple()` 未变），只是代理自身的统计观测链路不再追踪这一维度，详见
`docs/designs/2026-07-24-model-proxy-reasoning统计移除安全上线.md`。PASSTHROUGH 流式采用「转发在前、
旁路嗅探在后」策略（`_write_streaming_response` 转发 chunk 后累积进本地 buffer，按 `\n\n`
切出完整 SSE 事件块，从 anthropic 的 `message_delta` 或 responses 的 `response.completed`
事件里覆盖式提取 usage），不改变、不阻塞原有转发时序，异常整体隔离不影响透传正确性。
不做 token 成本折算，只统计数量。

累计用量账本：ACCESS 日志会在进程启动时被 `_trim_log` 截断到最后 5000 行，早期行永久丢失，无法
回答「本月/某天累计用了多少 token」这类长期问题。为此另建一个独立账本文件
`.claude_model_proxy_totals.json`（与日志文件同目录），每请求在 `_forward_logged` 收口处同步累加、
原子写盘：按天分桶，桶内以 `supply×route×strategy` 组合键（形如
`supply=<s>|route=<r>|strategy=<t>`，strategy 段是 client_token 明文如 `cc`/`codex`）累加
`requests`/`ok`/`fail`/`usage_in`/`usage_out`，另存 `total` 全历史汇总。账本
**只增不截**，不受进程重启与日志截断影响。天分桶只保留最近 `KEEP_DAYS=400` 天，超窗旧天桶汇总进
`months_archive` 月归档节点（永久保留）。天/月边界固定按 UTC+8 划分（`timezone(timedelta(hours=8))`，
不依赖系统时区）。账本供 `stats` 命令查询（见「CLI 命令参考」），与 ACCESS 日志完全独立。账本结构
细节见设计记录 `docs/designs/2026-07-23-usage-totals-ledger.md`。

### 5.3 配置热重载

`ConfigStore` 按 mtime 比对，每次转发请求都 `maybe_reload()`，改配置落盘即在下一个
请求生效，无需重启进程；也可用 `model_proxy_cli.sh reload` 主动强制重载。两者区别：手动 reload
会无条件清空所有 supply 的冷却，mtime 自动 reload 不动冷却状态。配置文件解析失败（JSON 非法等）时
保留旧配置并记 warning 日志，不崩溃、不清空配置。

### 5.4 reasoning debug 旁路日志

默认关闭；启动前 `export MODEL_PROXY_REASONING_DEBUG=1` 可打开，把
"客户端意图 → 相对映射结果"逐请求写进日志，只影响本模块 logger（不支持热切换，改后需重启进程）。

### 5.5 CLI 命令参考

用 `tools/model_proxy/model_proxy_cli.sh`：

```bash
model_proxy_cli.sh status                            # 运行状态 + supplies/routes/strategies/cooldown 概览
model_proxy_cli.sh reload                            # 触发配置热重载（无条件清空所有 cooldown）

model_proxy_cli.sh supply                            # 打印 list 后进入交互菜单 [a]dd/[e]dit/[d]el/[t]est/[q]uit
model_proxy_cli.sh route                             # 打印 list 后进入交互菜单 [a]dd/[e]dit/[d]el/[q]uit
model_proxy_cli.sh strategy                          # 打印 list 后进入交互菜单 [a]dd/[e]dit/[d]el/[q]uit

model_proxy_cli.sh switch <client_token> <route_id>  # 切换某 token 绑定的 route 家族（改 route_id 后 reload）
model_proxy_cli.sh install                           # 交互式列出四个 SDK + 本机检测状态，选择安装
model_proxy_cli.sh on                                # 启动 model_proxy.py（已在监听则跳过）
model_proxy_cli.sh off                               # 停止 model_proxy.py（严格按脚本绝对路径匹配进程）

model_proxy_cli.sh logs [N]                          # 显示最近 N 条 ACCESS 访问日志（默认 30 条）
model_proxy_cli.sh stats [时间] [维度/过滤...]        # 读独立累计账本，按 supply/route/strategy 任意
                                                      # 维度组合切片，支持 today/month/YYYY-MM-DD/YYYY-MM/全历史
model_proxy_cli.sh --help / -h                       # 显示帮助
```

- `supply`/`route`/`strategy` 只能通过一级入口进入交互菜单（先打印 list，再选操作，可回菜单
  继续或 `q`/回车退出）；不再支持子命令直达（如 `supply add`）。所有写操作原子写盘
  （tempfile + os.replace）后自动触发 reload。
- `switch <client_token> <route_id>` 仅适用于单值 `route_id` 写法的 strategy；对已配置
  `route_pool` 的 strategy 会拒绝执行（`route_id` 与 `route_pool` 互斥），需直接编辑配置文件
  调整。`strategy list`/交互菜单打印 route 归属时两种写法均兼容显示（单值直接打 route_id，
  route_pool 打成 `pool[route_id:weight,...]` 形式）。
- **非交互（stdin 非 TTY）环境**：调用 `supply`/`route`/`strategy` 时，先打印一次 list，然后
  检测到非 TTY 即直接退出，不进交互菜单，不会阻塞在 `read` 上等待永远不会到来的输入。
- `strategy add`/`edit` 录入 `tiers_source_capability` 时逐 tier 人工问答（source 侧无可探测
  的真实上游，只能人工填，原因见「reasoning 强度映射（深入）」§ 6.2）：输入逗号分隔列表→对应
  `effort_enum`；输入 `-`→空列表 `[]`（该 tier 无思考能力）；留空→add 时不写该 tier 键（走默认
  5 档）、edit 时保留原值。默认值绝不物化写入配置。
- `supply` 交互菜单 `[t]est`：整合原 `supply probe`/`supply test` 为单一交互入口，全流程只发
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
- `stats` 读独立账本文件 `.claude_model_proxy_totals.json`（按天分桶、supply×route×strategy
  组合键累加），与 ACCESS 日志完全独立、不受进程启动时 `_trim_log` 截断影响，长期累计不丢。
  支持按 `supply`/`route`/`strategy` 任意维度组合切片（投影和/或过滤），按天/月/全历史查询
  （`stats` / `stats today` / `stats month` / `stats 2026-07-23` / `stats 2026-07` /
  `stats today supply` / `stats today route=claude supply` 等）。`max ms` 仍来自 ACCESS 日志
  现场聚合（受日志窗口限制，输出中标注「近日志窗口内，非账本口径」）。

## 6. reasoning 强度映射（深入）

reasoning 语义在本 README 里只在本节深讲，其余各处只留一句话指针指向本节。

### 6.1 reasoning_capability 字段语义

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

**`off_alias`**（可选）：不是"是否支持关闭"的二元开关，而是"关闭动作具体落到哪个档位"的
可选配置。缺省规则：`effort_enum` 含 `off` 则关闭落到 `off`（进而走 DISABLED），否则不设
`off_alias`（关闭走 STRIP）。约束两条，违反则被拒、回退为不设 `off_alias`：

1. `off_alias` 不得高于 `effort_enum` 里最低的真实思考档（否则"关闭"会比"低强度思考"更强，
   破坏强度单调性）。
2. 当 target 无真实思考档（真实思考档序列为空，即 `[]` 或 `["off"]`）时，`off_alias` 不被
   消费——任何思考/关闭意图统一走 STRIP。

**档名词表**（`effort_enum`/`off_alias` 里的字符串，协议无关规范名，大小写不敏感）：
`off`（等价 `none`）/ `minimal` / `low` / `medium` / `high` / `xhigh` / `max`。未识别的档名
被忽略（容错，不报错）。`off` 与 `max` 都是强度全序的正式成员，无任何协议层专属特殊分支，走
跟 `low`/`medium`/`high` 完全一样的相对映射路径。词表唯一权威在
`core/reasoning/ladder.py::_NAME_TO_CANONICAL`，codec 层零词表：encode 直接把 canonical
枚举名小写作为 wire 档名发出（`effort_enum` 声明什么就发什么），decode 用同一全表识别入站
档名（含 `max`/`minimal`），映射约束统一收在 remap。

budget 分档断点（Anthropic `thinking.budget_tokens` 语义）是全局固定常量，与上游厂商无关，
不支持 per-supply 自定义。

档位不确定时可在 `supply` 交互菜单里选 `[t]est` 向上游探测（结果不保证准确，供人工核实，
见「CLI 命令参考」）；无法确定时应保持不配置（走默认 5 档兜底），不要凭猜测填窄档——窄档会
真实改变该 supply 收到的思考强度分布。

### 6.2 source 能力为何挂 strategy、为何人工填

**为什么 source 能力挂在 strategy（按 client_token）下、而不是按表面模型名声明**：表面模型名
（`claude-opus`/`claude-sonnet`/`claude-haiku`）只是客户端请求体里的 tier 选择器字符串，会被
多个 SDK 共享——codex-cli 也固定发 `model="claude-sonnet"`（见「接入各 SDK（install）」的
install 逻辑），跟 Claude Code 发的 `claude-sonnet` 同名，但两者是不同的客户端接入、理应能配
不同档位声明。真正代表"哪个客户端接入"的身份是 `client_token`（一个 token 一条 strategy，
`install` 命令把它写入某个具体 SDK 的配置文件），所以 source 能力挂在 strategy 下，既绑定
客户端身份，又保留同一客户端下不同 tier 的档位差异。

**source 能力为什么是人工填、不像 supply 那样自动探测**：source 侧是"客户端表面模型自己有哪些
档位"，没有一个可发探测请求的真实上游端点（表面模型只是请求体里的字符串标签），因此
`strategy add`/`strategy edit` 对 `tiers_source_capability` 采用逐 tier 人工问答录入，不做
自动探测。target 侧（supply）才有真实上游可 `probe`。

### 6.3 effort 跨模型映射算法

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

## 7. 接入各 SDK（install）

`install` 命令按 SDK 协议从 strategies 里过滤出协议匹配的 client_token，交互式选择后：已检测到该
SDK 配置目录则备份原文件后按其格式写入；未检测到则打印配置片段供手动粘贴。只读 strategies，不改
token→route 绑定。

**detect_installed 口径**：install 里的"已装/未装"判定口径是"配置目录存在即视为已装"（不要求
配置文件本身存在）。

支持四个 SDK：

- **claude**（Claude Code，Anthropic 协议）：写 `~/.claude/settings.json` 的
  `env.ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`，并补齐三个档位环境变量
  （`ANTHROPIC_DEFAULT_OPUS_MODEL`/`_SONNET_MODEL`/`_HAIKU_MODEL` 固定填
  `claude-opus`/`claude-sonnet`/`claude-haiku`）。
- **codex**（codex-cli，Responses 协议）：写 `~/.codex/config.toml` 的
  `[model_providers.model_proxy]` 段（`base_url`/`wire_api="responses"`/`env_key`）及顶层
  `model`/`model_provider`；appkey 走环境变量 `MODEL_PROXY_CODEX_TOKEN` 注入，不写入配置文件。
  codex 的 base_url 拼到 `/v1` 层级、由 `wire_api="responses"` 让 codex 自拼 `/responses`
  后缀——此拼法依据本项目 `detect_source` 对 `/v1/responses` 尾缀的识别反推，**未逐字核对
  codex 官方 config.toml 文档字段名**。实际接入若报 404/400，请核对 codex 官方文档调整
  base_url 层级。
- **hermes**（协议按 `api_mode` 决定）：标准库无 yaml 解析器，为避免破坏现有文件结构，统一打印
  `custom_providers` 配置片段供手动粘贴到 `~/.hermes/config.yaml`。
- **openclaw**（协议按 `api` 决定）：写 `~/.openclaw/openclaw.json` 的
  `models.providers.<name>`；现有文件用了 json5 专属语法（标准库 json 解析失败）时降级为打印
  片段，不强行写入。

四个 SDK 各自按其协议（claude=anthropic，codex=responses，hermes/openclaw 协议可选由用户在候选
token 里选定）过滤候选 client_token；无匹配协议的 token 时提示先用 `strategy add` 新增对应绑定。
检测到多个匹配 token 时交互式列出供选择。

**install base_url 期望形态表**：

| SDK | 写入的 base_url | 说明 |
|---|---|---|
| claude | `http://localhost:18889/` | 写入 `ANTHROPIC_BASE_URL` |
| codex | `http://localhost:18889/v1` | 拼到 `/v1` 层级，`wire_api="responses"` 由 codex 自拼 `/responses` |
| hermes | `http://localhost:18889/` | 打印片段供手动粘贴（标准库无 yaml writer） |
| openclaw | `http://localhost:18889/` | 写入 `providers.<name>.baseUrl`；json5 专属语法则降级打印 |

（端口随 `MODEL_PROXY_PORT` 变化，上表以默认 18889 为例。）

## 8. 当前状态 / 已知限制

- 已支持五种协议组合的转发/转换：anthropic→anthropic、responses→responses（均字节透传，含
  thinking 方言自适应）、anthropic→chat、anthropic→responses、responses→anthropic（经
  `core/translate.py` 转换）。其余组合返回 501。
- cross-supply failover：上游 401/403/429/5xx 触发对应 supply 冷却并按 route 顺序切换到下一个
  supply（不限协议，跨供给单元）。
- thinking/effort 方言自适应：Anthropic 有 `enabled`/`adaptive` 双变体，默认 `adaptive`；识别
  网关对 reasoning 语法的 400 拒绝后切换到对方接受的格式重试并缓存 48 小时。重试只重跑协议内 wire
  语法适配，不重算强度映射结果。Chat/Responses 单变体，无此重试。
- chat 协议空回答兜底：上游 chat 协议模型（如 kimi-k3）强制思考、无关闭档，当 `max_tokens` 太小导致
  输出预算全耗在 `reasoning_content`（思考过程）、正式回答 `content` 挤不出字时，`ANTHROPIC_TO_CHAT`
  转换（非流式 `openai_to_anthropic_response` / 流式 `OpenAIToAnthropicStreamAdapter`）在 content
  block 组装完成后判定「无任何 text/tool block 且 reasoning_content 非空」，把思考内容整段（加前缀
  `[模型仅返回思考过程，未生成正式回答]`）填入返回的 text block，避免客户端收到空 `content`；
  `stop_reason` 不变（仍反映真实截断原因）。可用 `core/translate.py` 模块级常量
  `_ENABLE_REASONING_FALLBACK`（默认 True）整体关闭。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic
    `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic
    `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic
    `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic
    `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为
    anthropic `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic
    `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic
    `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic
    `thinking` block 置前（而非丢弃），content 本身是 text/tool block；两条路径严格互斥、不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic
    `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic
    `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
- reasoning 强度映射：source/target 各自声明的档位能力做相对排名映射，非绝对锚定，详见
  「reasoning 强度映射（深入）」§ 6。
- codex install 写入的 base_url 层级未逐字核对 codex 官方文档，实际接入报 404/400 时需按官方
  文档调整（详见「接入各 SDK（install）」）。
- 错误路径加固：不支持的协议组合、上游 4xx/5xx、流式中途中断均按客户端协议包裹成合法的 error
  响应/事件，不会让客户端挂死。
- 只在 Claude Code 会话启动时（SessionStart hook）拉起一次，会话运行期间进程崩溃不会自动
  重启/自愈，需手动 `on`。
- 反向（responses→anthropic、chat→anthropic）的 reasoning→thinking 回传：2026-08-07 已补齐（①b
  + ①b-chat 镜像），上游 reasoning 内容块会回传为 anthropic thinking block（th_chars>0）。**已知
  限制**：产出的 thinking block 没有 `signature` 字段（正向转换不保留 signature_delta，反向无来源）
  ——对只读评估无影响，对会把 thinking 回传上游的多轮客户端（Claude Code）是已知限制。
- 未接入自动化测试覆盖真实上游网络调用（转换器单测均为脱网络单测，转发编排本身未做端到端自动化
  测试，依赖手动 curl 验证）。
- effort 探测（`supply test`）解析结果不保证准确，仅供人工审阅参考。

## 附录 A：配置字段速查表

### supplies 字段

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

### routes 字段

| 字段 | 类型 | 必填 | 语义 | 默认值 |
|---|---|---|---|---|
| `id` | string | 必填 | 家族唯一标识，strategies 里按 id 引用 | — |
| `tiers.opus` / `tiers.sonnet` / `tiers.haiku` | string[] | 必填（三档固定） | 每档一个按优先级排列的 supply id 列表 | — |
| `failover` | `"on"`/`"off"` | 必填 | on：失败换同档下一 supply；off：失败直接返回 | — |

### strategies 字段

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

### 顶层字段

| 字段 | 类型 | 必填 | 语义 |
|---|---|---|---|
| `admin_token` | string | 必填 | 控制 API 鉴权，供 `X-Proxy-Admin-Token` 请求头校验 |
| `default_cooldown_seconds` | number | 必填 | supply 未单独配置 `cooldown_seconds` 时的默认冷却时长 |

完整样例见 `config/model_proxy_config.example.json`。

## 附录 B：目录结构

```
tools/model_proxy/
├── model_proxy.py                     # 入口（thin wrapper，转发到 core.server.main）
├── config/
│   ├── model_proxy_config.json        # 实际配置（600 权限，不纳入 git 跟踪）
│   └── model_proxy_config.example.json # 配置样例（不含真实凭证，纳入 git 跟踪）
├── model_proxy_cli.sh                 # 控制脚本
├── _config_ops.py                     # supply/route/strategy 的增删改实现（被 cli 调用）
├── _install_ops.py                    # install 子命令实现（四个 SDK 接入）
├── core/                              # 核心实现包
│   ├── server.py                      # 主体：HTTP server、路由决策、转发编排、控制 API
│   ├── translate.py                   # 多协议结构转换器（Anthropic⇄Chat / Responses⇄Anthropic / Anthropic⇄Responses）；reasoning 强度处理外迁到 core.reasoning.*
│   └── reasoning/                     # effort/thinking 强度处理领域层
│       ├── ladder.py                  # canonical 强度全序 + budget↔canonical 换算
│       ├── capability.py              # ModelReasoningCapability + remap()（相对排名映射，唯一映射点）+ abstract_encode()
│       ├── codecs.py                  # 各协议 decode / syntax_adapt（协议内 wire 语法适配）
│       └── registry.py                # protocol → codec 单例 + apply_fields
├── tests/                              # 单测
├── docs/                              # 文档
│   ├── model_proxy_translate_spec.md  # 协议转换活规格
│   ├── designs/               # 当期设计记录（如入站鉴权/access日志/SessionStart hook）
│   └── archive/                       # 历史设计记录归档
└── samples/                            # 实测样本（网关真实响应，供规格核对字段用）
```
