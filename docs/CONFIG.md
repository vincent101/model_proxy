> 本文是 [README.md](../README.md) 的深入展开。日常配置与使用见 README。

# 配置字段明细

## 1. 配置路径与权限

相对 model_proxy 项目根，配置文件默认路径为 `config/model_proxy_config.json`（Mac 保持 600 权限，已加入
`.gitignore`，不被 git 跟踪；含真实 appkey/admin_token，不要手动纳入版本控制）。可用环境变量
`MODEL_PROXY_CONFIG` 覆盖路径。`config/model_proxy_config.example.json` 是不含真实凭证的
样例，纳入跟踪。

session override 存放在独立 sidecar 文件 `config/session_overrides.json`（与主 config 同目录，
600 权限，已加入 `.gitignore`），代理独占写，同样走受保护提交（私有暂存 + 原子替换 + 后验）。

运行数据（日志、账本、锁、PID、CLI 暂存与备份）默认集中在项目根 `.runtime/`，见
`config/runtime_paths.json`。该目录由 CLI 独占创建并校验私有权限，已加入 `.gitignore`；
不再使用 `/tmp` 或盘根。配置写入的安全备份也放在 `.runtime/backups/`（文件名带
`.bak.<随机后缀>`），不在被编辑文件的旁边生成。唯一例外：用户显式选择的外部配置目标，
若其派生的 sidecar 与项目暂存跨卷，才在该目标父目录做一次经确认的私有短期暂存。

### 1.1 reload 的失败语义（2026-11-22）

热重载**先校验、通过才提交**。配置读得到但校验不过时，内存继续用旧配置，
返回固定错误码而不是静默生效：

| 结果 | HTTP | `error` | 说明 |
|---|---|---|---|
| 成功 | 200 | — | 新配置生效，cooldown 已清 |
| 校验拒绝 | 422 | `config_rejected` | 旧配置继续跑；按提示修好后重试即可 |
| 解析失败 | 422 | `config_parse_error` | JSON 不合法 |
| 读盘/未知 | 500 | `config_reload_error` 等 | 可能瞬时，可重试 |

同一份被拒文件，请求热路径**不会**反复重读（按文件标记去重）；文件一改，下一次
请求或显式 `reload` 自然重试。CLI 会把这些错误码原样打印：

```
$ model_proxy_cli.sh reload
Error: reload: reload_failed; loaded=no, cooldown_cleared=yes, error=config_rejected.
```

手动 `reload` 进入认证 handler 后即使加载失败也会清 cooldown（那是进程状态，
与被拒配置无关）；后续请求触发的自动重载不会清。

## 2. supplies 字段

一个供给单元 = 一个上游端点。

四种形态，按 `protocol` 区分（配置样例里各有一条）：

| 形态 | `protocol` | 必填 | 走向 |
|---|---|---|---|
| 标准 HTTP-Anthropic | `anthropic` | `url` + `appkey`/`appkey_file` + `target_model` | legacy HTTP 路径，线协议 anthropic |
| 标准 HTTP-Responses | `responses` | 同上 | legacy HTTP 路径，线协议 responses |
| 标准 HTTP-Chat | `chat` | 同上 | 仅作出站（入站 Chat 返 501） |
| `catpaw-cli` | `catpaw-cli` | 只填 `target_model`（+ 可选 `reasoning_capability`） | 模块内置端点/凭据/header/计费块，线协议 anthropic |
| `catpaw-ide` | `catpaw-ide` | 只填 `target_model`（+ 可选 `reasoning_capability`） | 模块内置，另需桥动态库，线协议 anthropic |

`protocol` 同时承担「选处理模块」与「声明线协议」两个职责，因此不存在
`protocol: catpaw-cli` + 其它线协议的矛盾写法；细节见 §6。

```json
{
  "id": "claude-sonnet-k0",
  "url": "https://aigc.sankuai.com/v1/anthropic/v1/messages",
  "protocol": "anthropic",
  "appkey": "<APPKEY_PLACEHOLDER>",
  "target_model": "claude-sonnet-5",
  "reasoning_capability": {
    "effort_enum": ["low", "medium", "high", "xhigh", "max"]
  }
}
```

- `id`：唯一标识，routes 里按 id 引用。
- `url`：完整终态请求端点，代理不做任何拼接，直接原样作为出站请求 URL（只会在其后拼接
  经净化的原始 query，剔除 `beta` 参数）。
- `protocol`（可选）：`anthropic` / `chat` / `responses` / `catpaw-cli` / `catpaw-ide`。
  缺省时从 `url` 尾缀自动推断（详见下方「protocol 推断规则」）。
- `appkey`：鉴权用 Bearer token，注入到转发请求。
- `target_model`：实际下发给上游的模型名（客户端请求里的 model 字段会被替换成这个）。
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
| `url` | string | 必填（`catpaw-cli`/`catpaw-ide` 不需要） | 完整终态请求端点，代理不做拼接，只在其后拼接净化后的原始 query | — |
| `protocol` | string | 可选 | **既是线协议也是处理模块选择**：`anthropic`/`chat`/`responses` 走原有 HTTP 路径（缺省从 url 尾缀推断）；`catpaw-cli`/`catpaw-ide` 交由对应模块，线协议恒为 anthropic。未知值报错 | 从 url 推断 |
| `appkey` | string | 与 appkey_file 二选一 | 鉴权用 Bearer token（静态），注入转发请求 | — |
| `appkey_file` | string | 与 appkey 二选一 | 鉴权值的文件来源：每请求读取（~ 展开），逐行匹配 `AUTHORIZATION:` 前缀取值（mcopilot-cli 登录态）。读取失败该请求 500 且不冷却 | — |
| `target_model` | string | 必填 | 实际下发给上游的模型名 | — |
| `extra_headers` | object | 可选 | 出站 header 注入，优先级最高（覆盖客户端透传值与内置注入）；禁止 `authorization`/`x-api-key`/`content-length` 键 | `{}` |
| `system_inject` | string | 可选 | 注入计费标识等 system 块（前置合并进客户端 system，幂等）；仅 `protocol: anthropic` 的 supply 可配 | — |
| `reasoning_capability.effort_enum` | string[] | 可选 | target 侧真实支持的 effort 档位有序列表 | 默认 5 档 `off/low/medium/high/xhigh` |
| `reasoning_capability.off_alias` | string | 可选 | 关闭动作具体落到哪个档位 | 含 off 则落 off，否则不设（走 STRIP） |
| `credential_file` | string | 可选覆盖 | 凭据文件路径（`~` 展开），由 `mtsso_keeper` 产出与刷新。每请求读取；缺失或 `expire` 已过则 500 且不冷却 | 模块默认 + `provider_settings.json` 段 |
| `bridge_lib` | string | 可选覆盖 | 桥动态库路径（`~` 展开）；惰性加载并校验 ABI 版本，仅配置 IDE supply 时才加载 | 按平台默认 + `provider_settings.json` 段 |

mcli（CatPaw）的 `appkey_file`、`extra_headers`、`system_inject` 组合见项目内
[配置样例](../config/model_proxy_config.example.json)的 `glm-53-mcli-example`；使用前须确认本机登录态有效。字段和校验限制以上表为准。

#### 两个 CatPaw provider

`protocol: catpaw-cli` / `catpaw-ide` 时，端点、鉴权、header、system 注入全部内置在模块里，
supply **只声明差异**：

```json
{"id": "claude-opus-mcli-3339", "protocol": "catpaw-cli",
 "target_model": "claude-opus-5.5",
 "reasoning_capability": {"effort_enum": ["low", "medium", "high", "xhigh", "max"]}}

{"id": "catpaw-ide-sol", "protocol": "catpaw-ide",
 "target_model": "gpt-6.2-sol",
 "reasoning_capability": {"effort_enum": ["low", "medium", "high", "xhigh", "max"]}}
```

两类 provider 的 supply 都只留 4 个字段：**路径不进 supply**（见下节）。

内置内容（`core/providers/catpaw_cli.py`）：

| 项 | catpaw-cli | catpaw-ide |
|---|---|---|
| 端点 | `https://mcli.sankuai.com/v1/messages` | 模块内部构造 |
| 凭据默认路径 | `~/.config/mcopilot-cli/.config.yaml` | `~/.mtsso_keeper/catpaw_ide_session.json` |
| 桥库默认路径 | — | `~/.local/lib/libcatpaw_bridge.{dylib,dll,so}`（按平台） |
| `extra_headers` | `{"X-Working-Dir": "/"}` | — |
| `system_inject` | 计费 header（跟随 Claude Code 版本） | — |
| 鉴权 | Bearer + `x-api-key` | OIDC 四要素，桥内部处理 |

### 2.1 provider 本机设置（`config/provider_settings.json`）

凭据/桥库这类**跟机器和平台绑定**的路径，放在本机设置文件里，不动 supply、
不动主配置；文件缺失或某个键缺失即回落模块默认值（fail-open）。

```json
{
  "catpaw-cli": { "credential_file": "~/.config/mcopilot-cli/.config.yaml" },
  "catpaw-ide": {
    "credential_file": "~/.mtsso_keeper/catpaw_ide_session.json",
    "bridge_lib": "~/.local/lib/libcatpaw_bridge.dylib"
  }
}
```

- 样例：`config/provider_settings.example.json`；实际文件 `config/provider_settings.json`
  不入 Git（机器相关），首次需要改路径时复制样例再改。
- 解析优先级：**supply 字段 > `provider_settings.json` 段 > 模块默认值**。
  多账号/多桥库并存时才在单条 supply 上写覆盖。
- 按 `(mtime, size)` 缓存，改文件后下一次请求即生效，**不需要重启**（路径变更与主配置
  reload 解耦）。
- Windows：`bridge_lib` 默认解析成 `libcatpaw_bridge.dll`；换机时一般只需改这一行。

#### 桥库从哪来

`libcatpaw_bridge` 是 Go 编出的 **c-shared 动态库**（代码产物，与鉴权无关：
凭据运行时由 keeper 提供）。c-shared **不能交叉编译**，必须由目标平台的 C
工具链产出，因此走 CI：

```
Actions → build-catpaw-bridge → Run workflow
  windows/amd64 → libcatpaw_bridge.dll
  darwin/arm64  → libcatpaw_bridge.dylib
```

下载对应 artifact 后放到 `bridge_lib` 指向的位置（默认 `~/.local/lib/`），
或在 `provider_settings.json` 里指到任意路径。本地手工构建：

```bash
cd core/providers/catpaw_ide/go
go build -buildmode=c-shared -o ~/.local/lib/libcatpaw_bridge.dylib ./cmd/native
```

**正式构建不得带 `pocfake` 标签**（会把上游换成假 transport）。

**provider supply 若携带 `url`/`appkey`/`appkey_file`/`extra_headers`/`system_inject`
一律报错**——写了说明配置有误，不静默忽略。

两个 provider 的凭据都由 `mtsso_keeper` 维护，本代理**只读、不刷新**；凭据缺失或过期返回
500 且不进 cooldown（凭据问题不是上游故障，写冷却会误伤 failover）。

## 3. routes 字段

家族模板 = 一个 route id + 按需配置的模型档位及其 supplies 列表；服务可识别 fable、opus、sonnet、haiku。

```json
{
  "id": "claude",
  "tiers": {
    "fable": ["claude-fable-k0"],
    "opus": ["claude-opus-k0"],
    "sonnet": ["claude-sonnet-k0"],
    "haiku": ["claude-haiku-k0"]
  },
  "failover": "on"
}
```

- `id`：家族唯一标识，strategies 里按 id 引用。route 本身不含 client_token，只是一个可复用
  的家族模板。
- `tiers`：可按需配置 `fable`/`opus`/`sonnet`/`haiku`，每档为按优先级排列的 supply id 列表。使用某档前须为其配置可用供给；缺档时 pool 可能尝试下一 route。
- `failover`：`on` 时，同 route 内按顶层 `cooldown_rules` 的首条匹配规则冷却并尝试同档下一可用 supply；未匹配的错误不因此切换。`off` 不执行此类同 route 切换。

**菜单安全**：route 菜单的新增和编辑只处理 opus/sonnet/haiku；编辑会重建 `tiers`，丢失已有 fable 或其他额外档位。先备份，带 fable 的 route 请手工合并配置。删除 route 的检查不包含 `route_pool`，删除前须人工检查并移除 pool 引用。

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
| `tiers.fable` / `tiers.opus` / `tiers.sonnet` / `tiers.haiku` | string[] | 按使用的档位配置 | 每档按优先级排列的 supply id 列表 | — |
| `failover` | `"on"`/`"off"` | 必填 | on：错误命中冷却规则时尝试同档下一供给；off：不执行此类切换 | — |

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
  `switch <token> <route_id>` 改这个字段，但会暴露 token 于进程参数、可能的命令历史及 CLI 回显；也可用 `strategy` 交互菜单，菜单仍可能显示 token。**与 `route_pool` 二选一、互斥**（见下方「route_pool
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
| `tiers_source_capability.<fable\|opus\|sonnet\|haiku>.effort_enum` | string[] | 可选 | 该 tier 的 source 侧 effort 档位有序列表 | 默认 5 档 |
| `tiers_source_capability.<fable\|opus\|sonnet\|haiku>.off_alias` | string | 可选 | 同 supply 语义 | 同 supply 缺省规则 |
| `note` | string | 可选 | 备注 | — |

## 5. 顶层字段

| 字段 | 类型 | 必填 | 语义 |
|---|---|---|---|
| `admin_token` | string | 必填 | 控制 API 鉴权，供 `X-Proxy-Admin-Token` 请求头校验 |
| `cooldown_rules` | array | 可选 | 按错误码首条匹配的冷却规则；未配置或未匹配不按此规则切换供给 |
| `upstream_timeout_seconds` | number | 可选 | 上游请求超时（秒），缺省 1800（30min，对齐 API_TIMEOUT_MS） |
| `budget_retry` | object | 可选 | ④b 输出预算自动放大重试：`{"enabled": true, "max_retries": 5}`，缺省全开；封顶 131072 硬编码不暴露；无 per-supply 维度 |

冷却规则写在顶层，供所有 supply 共用；例如：

```json
{
  "cooldown_rules": [
    {"errorcode": [429, 503], "cooldown_seconds": 60},
    {"errorcode": ["URLError"], "cooldown_seconds": 60}
  ]
}
```

`errorcode` 为非空数组（HTTP 状态码或非空字符串），`cooldown_seconds` 为正整数；按顺序首条匹配，空数组不是兜底。正文错误须先由 `body_error_rules` 识别，其错误码也须匹配冷却规则才能触发相应冷却和切换。**正文识别对 `200` 正常响应与上游非 2xx 错误体同样生效**：非 2xx 命中规则时优先按规则里的虚拟 `errorcode` 取冷却时长，未命中才回落 HTTP 状态码——例如把「模型未在账号目录」这类确定性错误配成 6 小时冷却，避免按 400 的 5 秒冷却反复重试同一坏供给。实际切换还取决于 route 的 `failover` 和可用供给。供给级 `cooldown_seconds` 和旧顶层 `default_cooldown_seconds` 不参与当前规则。

完整样例见 [配置样例](../config/model_proxy_config.example.json)。

## 6. protocol 推断规则

`supply["protocol"]` 是可选字段，承担**两个职责**：声明上游线协议，以及（对 CatPaw）
选择处理模块。解析分两层：

**第一层：选处理模块**（`core/providers/__init__.py::resolve_handler`）

| 取值 | 处理模块 | 上游线协议 |
|---|---|---|
| `anthropic` / `chat` / `responses` | legacy HTTP 路径 | 同名 |
| `catpaw-cli` | CatPaw CLI 模块 | `anthropic`（内置） |
| `catpaw-ide` | CatPaw IDE 模块 | `anthropic`（内置） |
| 未配置 | legacy，交由第二层推断 | — |
| 其他 | **报错** | — |

这样 `protocol` 与线协议**不可能矛盾**（不存在 `provider: catpaw-cli` + `protocol: chat`
这种写法），因此不需要额外的互斥校验。

**第二层：legacy 的线协议**（`core/reasoning/registry.py::resolve_protocol`）

1. 显式 `supply["protocol"]`（合法值见上表 legacy 三项，否则报错）。
2. 未显式配置时，从 `supply["url"]` 的路径尾缀推断（三者互斥，无公共后缀，可靠判断）：

   | url 尾缀 | protocol |
   |---|---|
   | `/v1/messages` | `anthropic` |
   | `/v1/responses` | `responses` |
   | `/chat/completions` | `chat` |

3. 两者都推断不出（url 尾缀不属于上表、且未显式配置 protocol）→ 直接抛错，代理返回明确的
   500 错误响应，不做任何猜测性兜底——避免请求悄悄走错转换分支产生难排查的错误响应。

`catpaw-*` 两个取值不参与第二层推断：它们的端点与线协议由模块决定，supply 上也不再有 `url`。

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
  `core/cli/_config_ops.py` 的 `strategy add`/`edit` 拒绝写入；若绕过 CLI 直接改配置文件导致两者同时
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

`strategy` 一级菜单的 `[a]dd`/`[e]dit` 可录入 `tiers_source_capability`；source 侧无真实上游可探测，须人工填写，原因见 [REASONING.md](REASONING.md)。菜单只询问 opus/sonnet/haiku：输入逗号分隔列表→对应 `effort_enum`；输入 `-`→空列表 `[]`；留空→add 不写该 tier、edit 保留该三档原值。默认值不物化写入配置。

服务也可读取 fable 的 source 能力，但菜单重新录入能力时会重建字段，可能丢掉原有 fable 或其他额外档位；此类配置先备份并手工合并，不要在菜单中重新录入。

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
不再发第二次探测请求；否则打印归因并跳过（`add` 时会二次确认是否仍要保存该 supply）。此外，
连通性判定为 REACHABLE（2xx 或 400 探测值被拒）后会叠加顶层 `body_error_rules` 正文识别（与
failover 侧同一对识别函数）：正文命中规则即降级判失败（`body_error`，附"正文摘要"行，取提取后
text 截 100 字符），并跳过 effort 探测与 `reasoning_capability` 写入；未配置规则或未命中则
行为与不叠加时完全一致（fail-open）。

## 11. stats 账本 schema

累计用量账本（schema v3）：ACCESS 日志会在进程启动时被 `_trim_log` 截断到最后 5000 行，早期行
永久丢失，无法回答「本月/某天累计用了多少 token」这类长期问题。为此另建一个独立账本文件
`.runtime/model_proxy_totals.json`（默认与日志同目录，路径以 `config/runtime_paths.json`
为准），每请求在 `_forward_logged` 收口处同步累加、经私有暂存原子替换并做后验复核：按天分桶，桶内以 `supply×route×strategy` 组合键（形如
`supply=<s>|route=<r>|strategy=<t>`，strategy 段是 client_token 明文如 `cc`/`codex`）累加
`requests`/`ok`/`fail`/`usage_in`/`usage_out`/`max_ms`/`attempts`/`attempt_fail`，另存 `total`
全历史汇总。`max_ms`（OPT-10）入账本，CLI stats 的 max_ms 从账本取，不再依赖日志窗口；
`attempts`/`attempt_fail`（OPT-10）为 attempt 级计数——failover 中间失败计入对应 supply（仅盖
3 处 failover continue，budget 重试按 §5a 决策不记账）。该字段只落账本 JSON，CLI stats 暂不投影。
持续选用同一账本且文件未丢失时，已持久化的累计数据不会因正常重启或日志截断而清零。
天分桶只保留最近 `KEEP_DAYS=400` 天，超窗旧天桶汇总进 `months_archive`
月归档节点（永久保留）。天/月边界固定按 UTC+8 划分（`timezone(timedelta(hours=8))`，不依赖系统时区）。
schema 升级（v2→v3）在 `_load` 启动时自动迁移**已选路径上的账本到内存**：旧桶 combo 补 `attempts=0`/`attempt_fail=0`、
bucket 补 `max_ms=0`（断档但保真，不虚高），既有值保留，记 `usage_totals.migrated` INFO 日志；正常记账并成功持久化后才写入磁盘。
这只是 schema 迁移，不会自动把项目根旧账本 `.model_proxy_totals.json` 迁移到新默认路径
`.runtime/model_proxy_totals.json`；使用新默认路径不会自动承接旧路径上的历史。
**迁移与重启顺序**：先停旧进程并备份旧账本；如需承接旧路径账本，在新路径尚无账本时单独迁移，
再启动新进程加载已选账本。新路径已有账本时不要直接覆盖；不能以启动时 schema 迁移替代路径迁移。
即使无需路径迁移，也须先停旧进程再启动新进程，避免旧进程用旧 schema 覆盖已升级的文件。
账本供 `stats` 命令查询（见 README「CLI 命令速查」），与 ACCESS 日志完全独立。账本结构细节见设计记录
`docs/designs/2026-07-23-usage-totals-ledger.md` + `docs/designs/2026-08-08-log-optimization-plan.md`。

## 12. 安装与运行身份记录

`.runtime/` 下的 `install.json` / `active.json` / `last_good_paths.json` 是「这个安装、这个实例」的身份凭据：服务启动链路靠它们判断
「还是不是同一份安装、上次那个进程是否已经退出」。

### 12.1 macOS：schema 4（卷 UUID + inode）

早期记录用 `{device, inode}` 描述安装根。`st_dev` 是内核的**挂载编号**，重启后会重排，
于是每次重启都被判成「安装已变」而拒绝启动。schema 4 改为
`{kind: "macos_volume_uuid", uuid, inode}`：

- `uuid` 取卷的持久 UUID，跨重启不变；
- `inode` 仍指向目录本身，防止「同卷内换了目录」；
- 旧记录能就地升级（`LEGACY_SAME_BOOT`），跨启动的旧记录需一次交互重绑定
  （`LEGACY_OTHER_BOOT`）。

### 12.2 谁判身份、谁不判

| 链路 | 是否判安装身份 | 依据 |
|---|---|---|
| `on` / 服务启动 | **判** | 记录结构 + 卷 UUID + inode + 双锁活体 |
| `status` / `off` | **不判** | 记录结构 + 真实进程 + 端口归属 + 锁活体 |

`status`/`off` 不采集卷 UUID、也不比对记录里的 inode，因此身份异常不会让服务
查不到或停不掉（停止前仍会尽力核实端口由记录进程持有，核实不了时告警但不阻断）。

卷 UUID 取不到时**启动链 fail-closed**（拒绝启动、不写新记录）；凭据路径的 `reload`
同样拒绝。

### 12.3 升级与备份

升级在服务启动且双锁持有时自动进行。升级前的记录备份到
`.runtime/backups/identity-v2-<UTC>/`：0600、只增不改、不覆盖既有备份。

### 12.4 降级（必须按顺序）

1. `off` 停服务（不要只是 `kill`）。
2. 用上一步的备份**原样恢复**这三份记录，保持 0600。
3. 再切回旧版本。

注意事项：

- 备份可能是迁移中断留下的 **2/4 混合**（旧版本读不了其中的 schema 4 记录）；
- **全新安装没有迁移备份**，只能重新初始化；
- 跨启动或跨卷恢复的 schema 2 记录在旧版本仍可能报 `installation_changed`，属预期；
- **不要手工改 `schema` 字段**——旧版本会按记录不可用拒绝。

### 12.5 Windows

Windows 不使用这套卷身份：启动路径只校验 schema 3 的 `active.json`（PID、
`process_start`、可执行文件路径拼写），不持久化卷身份，因此「跨重启 device 漂移」
在 Windows 上不成立。

## 用户环境可选 MCP 服务

仅适用于用户自己的 vault 环境：open-websearch 和 friday-websearch 是独立部署的可选服务，不由 model_proxy CLI 管理，也不是独立 clone 的依赖。其示例地址分别为 `http://server.local:3000/mcp`、`http://server.local:3001/mcp`；`server.local` 只是占位符，按实际部署替换。vault 根下 `.claude/skills/websearch-router/runtime/ensure_websearch.sh` 是用户环境中的恢复入口，存在与可用性须自行确认。

客户端可将以下片段合并到已有 `.mcp.json`，不要覆盖其他服务：

```json
{
  "mcpServers": {
    "open-websearch": {"type": "http", "url": "http://server.local:3000/mcp"},
    "friday-websearch": {"type": "http", "url": "http://server.local:3001/mcp"}
  }
}
```

两服务的绑定地址、鉴权、TLS 和网络访问须分别评估；model_proxy 的 Host 检查不保护它们，不能据此假定 LAN 或公网访问安全。
