---
created: 2026-07-17 18:52:18
version: 0.9
---
# model_proxy

**Version: 0.9**

## 1. 这是什么

本地多协议 AI 模型代理，端口 18889（默认，可配）。同时支持 Claude Code（Anthropic
`/v1/messages`）、codex-cli（OpenAI Responses `/v1/responses`）等多个 SDK 接入，并可跨协议
互相访问对方生态的模型——例如在 Claude Code 里实际调用 GPT，在 codex 里实际调用 Claude。

## 2. Quick Start

四步让 Claude Code 通过本代理跑起来。

**① 配置**

```bash
cp tools/model_proxy/config/model_proxy_config.example.json \
   tools/model_proxy/config/model_proxy_config.json
chmod 600 tools/model_proxy/config/model_proxy_config.json
```

编辑 `config/model_proxy_config.json`，把所有 `<APPKEY_PLACEHOLDER>` 替换为真实 appkey（每个 supply 一条），`<ADMIN_TOKEN_PLACEHOLDER>` 替换为自定义控制 API 鉴权 token（Quick Start 不涉及控制 API，可暂时填任意值）。example.json 已含一条可用 strategy（`client_token: "cc"`,绑 `claude` 家族），配好 appkey 即可直接启动。配置字段完整说明见 [CONFIG.md](docs/CONFIG.md)。

**② 启动代理**
```bash
tools/model_proxy/model_proxy_cli.sh on
```
预期输出：
```
Starting model_proxy.py on port 18889...
model_proxy started (pid <PID>), ready (N * 0.5s)
```
（已在监听则输出 `model_proxy already running on port 18889`。）

**③ 接入 Claude Code**
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

**④ 重启 Claude Code 生效并验证**
重启 Claude Code 后，用 `logs` 看是否有转发记录：
```bash
tools/model_proxy/model_proxy_cli.sh logs
```
出现形如 `ACCESS ms=... status=200 source=anthropic route=claude tier=... supply=... token=...`
的行即接入成功。想看运行状态用 `model_proxy_cli.sh status`。

日常挂载：`tools/model_proxy/hooker/ensure_model_proxy.sh` 已注册到 SessionStart hook，
每次开 Claude Code 会话自动拉起，一般无需手动 `on`（详见「启动与停止」）。

（注：`install` 会先跑 `ensure_session_hook` 确保 SessionStart 里存在正确 hook 条目，此点在
「启动与停止」「接入各 SDK（install）」说明，Quick Start 不展开。）

## 3. 配置结构概述

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
`route_pool`（数组）绑多个 route，按 CC 会话 session_id 做一致性哈希分配，两者字段互斥。

session override 的唯一来源是独立 sidecar 文件 `config/session_overrides.json`，通过 `$route` 命令
或直接编辑该文件维护，不再是 strategy 的字段。

- **supplies**：每条 = 一个上游端点（url + 协议 + appkey + target_model + 可选能力）。
- **routes**：家族模板，固定 opus/sonnet/haiku 三档，每档一个按优先级排列的 supply id 列表，
  取第一个未冷却的；route 本身不含 token，可被多条 strategy 复用。
- **strategies**：把某个 client_token 绑定到某个 route——可以绑单个 route（`route_id`），也可以
  绑一个 route_pool 做 session 级分配（`route_pool` + `dispatch`）；运行时切换单值写法用
  `switch` 改 route_id。

请求匹配时反向走这条链：token → strategy → route → tier → supply（见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)「三阶段匹配」）。

字段明细、protocol 推断、route_pool 分配算法、session override sidecar 见 [CONFIG.md](docs/CONFIG.md)。

## 4. 请求处理流程

```
客户端请求 → ① 鉴权识别 → ② 协议识别 → ③ 三阶段匹配 → ④ effort 映射 → ⑤ 出站转换转发 → 真实上游
```

- ① 提取 client_token（Bearer 优先，回退 x-api-key）
- ② 识别入站协议（path 尾缀 → body 特征兜底）
- ③ token → strategy → route 候选列表 → tier → supply（含跨 route 兜底）
- ④ decode → remap → encode → syntax_adapt（相对排名映射）
- ⑤ (source,target) 组合决定 PASSTHROUGH 或转换；失败且 failover=on 则冷却+换 supply

关键点：入站阶段代理不关心客户端把 base_url 后面拼了什么 path，出站只用配置的 `supply.url`。

完整链路图与各环节细节见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 5. CLI 命令速查

用 `tools/model_proxy/model_proxy_cli.sh`：

| 命令 | 说明 |
|------|------|
| `status` | 运行态健康总览：health 计数 / degraded supplies / active sessions / cooldown 明细 / config 计数；离线只报进程态 + config mtime |
| `reload` | 触发配置热重载（无条件清空所有 cooldown） |
| `supply` | 打印 list 后进入交互菜单 [a]dd/[e]dit/[d]el/[t]est/[q]uit |
| `route` | 打印 list 后进入交互菜单 [a]dd/[e]dit/[d]el/[q]uit |
| `strategy` | 打印 list 后进入交互菜单 [a]dd/[e]dit/[d]el/[q]uit |
| `switch <token> <route_id>` | 改 strategy.route_id 后 reload；仅支持单值写法，route_pool 会被拒绝 |
| `install` | 交互式列出四个 SDK + 本机检测状态，选择安装 |
| `on` | 启动 model_proxy.py（已在监听则跳过） |
| `off` | 停止 model_proxy.py（严格按脚本绝对路径匹配进程） |
| `logs [N]` | 显示最近 N 条 ACCESS 访问日志（默认 30 条）；支持 `req=<id>` / `level=ERROR` / `event=cooldown` 过滤 |
| `stats [时间]` | 读独立累计账本，按 supply/route/strategy 三维度各投影一段；支持 today/month/YYYY-MM-DD/YYYY-MM/全历史 |
| `--help` / `-h` | 显示帮助 |

- **菜单规则**：`supply`/`route`/`strategy` 只能通过一级入口进入交互菜单（先打印 list，再选操作，可回菜单
  继续或 `q`/回车退出）；不再支持子命令直达（如 `supply add`）。所有写操作原子写盘
  （tempfile + os.replace）后自动触发 reload。
- **switch 限制**：仅适用于单值 `route_id` 写法的 strategy；对已配置
  `route_pool` 的 strategy 会拒绝执行（`route_id` 与 `route_pool` 互斥），需直接编辑配置文件
  调整。
- **非交互（stdin 非 TTY）环境**：调用 `supply`/`route`/`strategy` 时，先打印一次 list，然后
  检测到非 TTY 即直接退出，不进交互菜单，不会阻塞在 `read` 上等待永远不会到来的输入。
- **strategy 录入**：`tiers_source_capability` 逐 tier 人工问答录入（source 侧无可探测真实上游，详见 [CONFIG.md](docs/CONFIG.md)「strategy 录入流程」）。
- **热重载**：`ConfigStore` 按 mtime 比对，每次转发请求都 `maybe_reload()`，改配置落盘即在下一个
  请求生效；也可用 `reload` 主动强制重载（无条件清空所有冷却）。配置文件解析失败时保留旧配置并记 warning 日志，不崩溃。
- supply test 归因+探测、stats 账本 schema 见 [CONFIG.md](docs/CONFIG.md)。
- 日志写 `.model_proxy.log`（启动时截断保留末 5000 行），累计用量账本写 `.model_proxy_totals.json`（路径配置见 `config/runtime_paths.json`）。日志级别、ACCESS 字段、translate 限流、token 统计等运维内容见
  `docs/designs/2026-07-22-access-log-and-latency.md`、
  `docs/designs/2026-07-23-usage-totals-ledger.md`、
  `docs/designs/2026-08-08-log-optimization-plan.md`。

## 6. 会话内命令：`$route`

`$route` 是在 Claude Code 对话里直接打的 in-band 指令，代理拦截后不转发上游，自己合成响应。

### 语法
```
$route <id>     把当前 session 钉到 <id>，下一条消息起生效
$route          查询当前 session 生效的 route 及其来源
$route reset    清除 override，落回自动哈希分配
```

### 示例
```
$route glm      → session pinned to route=glm (next message)
$route          → current route=claude (source=auto_hash)
$route reset    → override cleared, back to auto-hash
```

### 生效语义
1. 下一条消息起生效：当前请求被拦截不打上游
2. 只对 source=="anthropic" 生效：codex 侧发送会被当普通文本转发上游（无害）
3. 旧式单值 route_id 写法不支持 set/reset：strategy 须先迁移到 route_pool 写法

匹配规则、7天清理、sidecar 格式见[设计文档](docs/designs/2026-08-04-in-band-route-command-design.md)。

## 7. SDK 接入

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

## 8. 已知限制

- model_proxy 已独立为 git repo（远端 `https://github.com/vincent101/model_proxy.git`），可独立 clone 使用。
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
  转换在 content block 组装完成后判定「无任何 text/tool block 且 reasoning_content 非空」，把思考内容整段（加前缀
  `[模型仅返回思考过程，未生成正式回答]`）填入返回的 text block，避免客户端收到空 `content`；
  `stop_reason` 不变（仍反映真实截断原因）。可用 `core/translate.py` 模块级常量
  `_ENABLE_REASONING_FALLBACK`（默认 True）整体关闭。
  - 与之互斥（2026-08-07 ①b-chat 镜像补齐）：content 非空时，`reasoning_content` 会镜像为 anthropic `thinking` block 置前（而非丢弃），content 本身是 text block；两路径严格互斥不双写。
- 注入文案改写（2026-08-09 起）：anthropic source 请求转发前扫描全量 user 消息，精确匹配 claude CLI
  `thinking_only_retry` 注入的 nudge 文案则改写为更明确的「harness 自动重试、非用户空消息」表述，
  fail-open（不匹配原样透传）；命中时 ACCESS 日志记 `nudge_rewritten=1`，设计见
  `docs/designs/2026-08-09-cli-thinking-only-nudge文案proxy改写.md`。
- reasoning 强度映射：source/target 各自声明的档位能力做相对排名映射，非绝对锚定，详见
  [REASONING.md](docs/REASONING.md)。
- 输出预算自动放大重试（④b，2026-08-08 起）：非流式响应若「达到输出预算上限且正文缺失」
  （anthropic `stop_reason=max_tokens` / chat `finish_reason=length` / responses
  `status=incomplete`+`reason=max_output_tokens`，且只有 thinking 无 text/tool），代理在
  **原始上游响应**上判定后自动把预算 ×2 重发（封顶 131072、最多 5 次，同 supply 重选、
  不冷却、不计 failover；爬升途中 failover 换 supply 时放大值被继承）。首轮永远按客户端
  给定值原样发出（代理不主动改客户端预算），只在截断真实发生后反应式放大；字段名分协议
  （`max_tokens`/`max_completion_tokens`/`max_output_tokens`）。可由顶层 `budget_retry`
  块配置或整体关闭。反向（responses→anthropic）客户端不传 max_tokens 时缺省按请求是否
  产生 thinking 分档：16384 / 4096。
  - **已知限制：仅非流式生效**。流式响应字节即时下发客户端、发出后无法回追，流式只在收口
    处检测记 `budget_truncated` 日志不重试——流式场景仍需调用侧给足起步预算（流式检测覆盖
    anthropic 透传与 chat 方向；responses 协议流式 adapter 未持有 incomplete 状态，不检测）。
- codex install 写入的 base_url 层级未逐字核对 codex 官方文档，实际接入报 404/400 时需按官方
  文档调整（详见「SDK 接入」）。
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

## 附录 A: 目录结构

```
tools/model_proxy/
├── model_proxy.py                     # 入口（thin wrapper，转发到 core.server.main）
├── config/
│   ├── model_proxy_config.json        # 实际配置（600 权限，不纳入 git 跟踪）
│   ├── model_proxy_config.example.json # 配置样例（不含真实凭证，纳入 git 跟踪）
│   ├── runtime_paths.json             # 运行时路径常量（log/totals/lock/pid 等）
│   └── session_overrides.json         # session override sidecar（$route 写入，600 权限）
├── model_proxy_cli.sh                 # 控制脚本
├── _config_ops.py                     # supply/route/strategy 的增删改实现（被 cli 调用）
├── _install_ops.py                    # install 子命令实现（四个 SDK 接入）
├── core/                              # 核心实现包
│   ├── server.py                      # 主体：HTTP server、路由决策、转发编排、控制 API
│   ├── translate.py                   # 多协议结构转换器（Anthropic⇄Chat / Responses⇄Anthropic / Anthropic⇄Responses）
│   └── reasoning/                     # effort/thinking 强度处理领域层
│       ├── ladder.py                  # canonical 强度全序 + budget↔canonical 换算
│       ├── capability.py              # ModelReasoningCapability + remap()（相对排名映射，唯一映射点）+ abstract_encode()
│       ├── codecs.py                  # 各协议 decode / syntax_adapt（协议内 wire 语法适配）
│       └── registry.py                # protocol → codec 单例 + apply_fields
├── tests/                              # 单测
├── docs/                              # 文档
│   ├── CONFIG.md                      # 配置字段明细/protocol推断/route_pool/session override/supply test/stats账本
│   ├── ARCHITECTURE.md                # 入站鉴权/协议识别/三阶段匹配/effort映射/出站转换/启停
│   ├── REASONING.md                   # effort_enum语义/映射算法/off_alias/档名词/debug旁路
│   ├── designs/               # 当期设计记录（含 model_proxy_translate_spec.md 协议转换活规格）
│   └── archive/                       # 历史设计记录归档
└── samples/                            # 实测样本（网关真实响应，供规格核对字段用）
```

## 附录B: 文档导航

| 文档 | 内容 | 路径 |
|---|---|---|
| CONFIG.md | 配置字段明细/protocol推断/route_pool/session override/supply test/stats账本 | docs/CONFIG.md |
| ARCHITECTURE.md | 入站鉴权/协议识别/三阶段匹配/effort映射/出站转换/启停 | docs/ARCHITECTURE.md |
| REASONING.md | effort_enum语义/映射算法/off_alias/档名词/debug旁路 | docs/REASONING.md |
| 协议转换规格 | Anthropic⇄Chat/Responses⇄Anthropic 字段映射 | docs/designs/model_proxy_translate_spec.md |
| 设计记录 | 当期设计决策 | docs/designs/ |
| 历史归档 | 已完成/已替代设计 | docs/archive/ |
