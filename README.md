# model_proxy

## 这是什么

本地多协议 AI 模型代理，端口 18889（默认，可配）。同时支持 Claude Code（Anthropic
`/v1/messages`）、codex-cli（OpenAI Responses `/v1/responses`）等多个 SDK 接入，并可跨协议
互相访问对方生态的模型——例如在 Claude Code 里实际调用 GPT，在 codex 里实际调用 Claude。

与 `tools/proxy.py`（纯 Anthropic 生态的 appkey/profile 轮转代理，端口 18888，Claude Code
单一协议生产运行）完全独立并行，互不依赖、互不干扰，可同时保留。

## 目录结构

```
tools/model_proxy/
├── model_proxy.py                     # 入口（thin wrapper，转发到 core.server.main）
├── model_proxy_config.json            # 实际配置（600 权限，不纳入 git 跟踪）
├── model_proxy_config.example.json    # 配置样例（不含真实凭证，纳入 git 跟踪）
├── model_proxy_cli.sh                 # 控制脚本
├── _config_ops.py                     # supply/route/strategy 的增删改实现（被 cli 调用）
├── _install_ops.py                    # install 子命令实现（四个 SDK 接入）
├── core/                              # 核心实现包
│   ├── server.py                      # 主体：HTTP server、路由决策、转发编排、控制 API
│   ├── translate.py                   # 多协议结构转换器（§1 Anthropic⇄Chat / §2 Responses⇄Anthropic / §3 Anthropic⇄Responses）；reasoning 强度处理已外迁到 core.reasoning.*
│   └── reasoning/                     # effort/thinking 强度处理领域层
│       ├── ladder.py                  # canonical 强度全序 + budget↔canonical 换算
│       ├── capability.py              # ReasoningCapability + align()（唯一钳位点）
│       └── codecs.py / registry.py    # 各协议 decode/encode/选语法
├── tests/                              # 单测
├── docs/                               # 规格/蓝图文档
└── samples/                            # 实测样本（网关真实响应，供规格核对字段用）
```

## 配置怎么写

配置文件路径默认 `tools/model_proxy/model_proxy_config.json`（600 权限，已加入
`tools/model_proxy/.gitignore`，不被 git 跟踪；含真实 appkey/token，不要手动纳入版本控制）。
可用环境变量 `MODEL_PROXY_CONFIG` 覆盖路径。本目录 `model_proxy_config.example.json` 是不含
真实凭证的样例，继续跟踪。核心是三段式结构：**supplies**（供给单元）+ **routes**（家族模板）
+ **strategies**（token→家族绑定）。

### supplies：一个供给单元 = 一个上游端点

```json
{
  "id": "gw-claude",
  "url": "https://aigc.sankuai.com/v1/anthropic",
  "protocol": "anthropic",
  "appkey": "<APPKEY_PLACEHOLDER>",
  "target_model": "claude-sonnet-4",
  "cooldown_seconds": 300
}
```

- `id`：唯一标识，routes 里按 id 引用。
- `url` + `protocol`：上游端点地址与协议（`anthropic` / `chat` / `responses`）。
- `appkey`：鉴权用 Bearer token，注入到转发请求。
- `target_model`：实际下发给上游的模型名（客户端请求里的 model 字段会被替换成这个）。
- `cooldown_seconds`（可选）：该 supply 触发失败后的冷却时长，不填则用顶层
  `default_cooldown_seconds`。
- `reasoning_capability`（可选）：该 supply 真实支持的 effort 档位能力描述。不配置时用代码
  内置默认 5 档（`effort_enum: ["none","low","medium","high","xhigh"]` + `off_alias: "none"`）。
  配置后按该 supply 覆盖，不影响其他 supply。字段：
  - `effort_enum`：该 supply 真实支持的 effort 档位有序列表（低→高）。
  - `off_alias`（可选）：显式关闭思考落到的目标档；不填则若 `effort_enum` 含
    `none`/`off` 落到该档，否则不塞字段。
  - canonical `MAX` 档不设专属别名，统一按 `effort_enum` 就近钳位规则钳到该枚举最高档。
  - budget 分档断点（Anthropic `thinking.budget_tokens` 语义）是全局固定常量
    （`ladder.py` 的 `_BUDGET_ANCHORS`），与上游厂商无关，不支持 per-supply 自定义。

  示例（某供应商只支持 5 档中的 `none/low/medium/high`，无 `xhigh`）：

  ```json
  {
    "id": "glm-sankuai",
    "url": "https://aigc.sankuai.com/v1/anthropic",
    "protocol": "anthropic",
    "appkey": "<APPKEY_PLACEHOLDER>",
    "target_model": "glm-5.2",
    "reasoning_capability": {
      "effort_enum": ["none", "low", "medium", "high"]
    }
  }
  ```

  档位不确定时可用 `supply probe <id>` 向上游探测（探测结果不保证准确，供人工核实，
  详见「怎么控制」）。

### routes：家族模板 = 一个 route id + 三档（opus/sonnet/haiku）各自的 supplies 列表

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

- `id`：家族唯一标识，strategies 里按 id 引用。route 本身**不含 client_token**，只是一个
  可复用的家族模板。
- `tiers`：固定三档 `opus`/`sonnet`/`haiku`，每档一个按优先级排列的 supply id 列表，取第一个
  「未冷却」的。
- `failover`：`on` 时上游返回 401/403/429/5xx 会把当前 supply 打入冷却并换同档下一个再试；
  `off` 时失败直接返回给客户端，不重试。failover 是 route（家族）级开关，不细分到 tier。

**多档共享同一组真实上游**：不是每个家族都天然有三种不同能力的模型——比如某家族只有
「强/快」两种真实模型，硬套三档反而要在 `supplies` 里复制冗余条目。这种情况下多个 tier
可以直接填同一组 supply id 列表，不需要引入额外抽象层。例如 deepseek 家族只有 `pro`/`flash`
两种真实模型，`opus` 和 `sonnet` 都打 `pro`，`haiku` 单独打 `flash`：

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

供给单元命名建议按**真实能力**取名（如 `ds-pro-k0`/`ds-flash-k0`），不要用 `opus`/`sonnet`
这类档位词——档位是请求侧的分类，不代表上游真的有对应数量的独立模型。

### strategies：client_token → route_id 绑定（运行时可切换）

```json
{ "client_token": "cc", "route_id": "claude", "note": "默认 Claude 家族" }
```

- `client_token`：客户端请求 `Authorization: Bearer <token>` 里的 token，model_proxy 据此
  找到对应 strategy。
- `route_id`：该 token 绑定到哪个 route 家族，必须是 routes 里存在的 id。
- `note`：可选备注。
- 禁用一个 token 直接删除其 strategy 记录即可（无 enabled 开关）。

**三阶段匹配流程**：请求进来后 ① 用 `client_token` 查 strategies 拿到 `route_id`，再用
`route_id` 拿到 route 家族；② 把请求体 `model` 字段**精确查表**映射成 tier 名
（`claude-opus`→opus / `claude-sonnet`→sonnet / `claude-haiku`→haiku，仅这三个精确值，
非子串猜测）；③ 从 route 的 `tiers` 里按 tier 取出该档 supplies 列表交给 failover 选择。

`model` 字段不是上述三个预设值之一时，选路直接 400 失败，不兜底。`settings.json` 里的
`ANTHROPIC_DEFAULT_OPUS_MODEL`/`ANTHROPIC_DEFAULT_SONNET_MODEL`/
`ANTHROPIC_DEFAULT_HAIKU_MODEL` 三个值固定填 `claude-opus`/`claude-sonnet`/`claude-haiku`；
运行时切换家族用 `model_proxy_cli.sh switch <token> <route_id>` 改 strategy 绑定，不动
model 标签。

### 顶层字段

- `admin_token`：控制 API 鉴权，供 `X-Proxy-Admin-Token` 请求头校验。
- `default_cooldown_seconds`：supply 未单独配置 `cooldown_seconds` 时的默认冷却时长。

完整样例见 `model_proxy_config.example.json`。

### effort 跨模型钳位

客户端发的 effort 意图（`none`/`low`/`medium`/`high`/`xhigh`，强度依次递增，及 Anthropic 的
`max`）未必落在目标 supply 真实支持的 `effort_enum` 里。按强度就近钳位：

- 强度高于枚举最高档 → 钳到最高档（尽量给到该 supply 能提供的最强档）。
- 强度低于枚举最低档 → 钳到最低档。
- 恰好命中枚举内某档 → 精确命中，直传。
- `max`（Anthropic 特殊字面值，canonical 序数恒大于任何枚举最高档）→ 与其他档位一样走统一
  钳位规则，结果等价于钳到该枚举最高档，不设专属别名。
- 枚举跳档（如 `["low","xhigh"]`）时客户端发的中间强度未精确命中 → 取强度序上最接近的一档；
  两侧距离相等则取更高档（偏保守，保留更多思考质量）。

按整体强度序就近钳位，不会出现「发更强意图反而映射到更弱档位」的强度倒挂。

## 怎么启动

```bash
tools/model_proxy/model_proxy_cli.sh on
```

或手动：

```bash
MODEL_PROXY_PORT=18889 python3 tools/model_proxy/model_proxy.py &
```

端口默认 18889，可用 `MODEL_PROXY_PORT` 环境变量覆盖。日志写到本目录
`.claude_model_proxy.log`（启动时自动截断保留最后 1000 行），进程锁在
`/tmp/claude_model_proxy.lock`（防止同一时刻起多个实例）。不接 SessionStart hook，
不会随 Claude Code 会话自动拉起，需要手动管理生命周期。

## 怎么控制（CLI）

用 `tools/model_proxy/model_proxy_cli.sh`（已在仓库里可直接执行）：

```bash
model_proxy_cli.sh status                            # 运行状态 + supplies/routes/cooldown 概览
model_proxy_cli.sh reload                             # 触发配置热重载（无条件清空所有 cooldown）

model_proxy_cli.sh supply                              # 不带子命令：打印 list 后进入交互菜单
                                                        #   [a]dd/[e]dit/[d]el/[p]robe/[q]uit
model_proxy_cli.sh supply list                         # 列出所有 supply（appkey 脱敏尾4位、cooldown）
model_proxy_cli.sh supply add                          # 交互式新增 supply（同步探测 effort，写配置后 reload）
model_proxy_cli.sh supply edit <id>                    # 交互式编辑 supply（含改 appkey、可选重新探测 effort）
model_proxy_cli.sh supply del <id>                     # 删除 supply（二次确认，被 route 引用则拒绝）
model_proxy_cli.sh supply probe <id>                   # 只跑 effort 探测，接受则回写 reasoning_capability

model_proxy_cli.sh route                                # 不带子命令：打印 list 后进入交互菜单
                                                        #   [a]dd/[e]dit/[d]el/[q]uit
model_proxy_cli.sh route list                          # 列出所有 route（家族模板：opus/sonnet/haiku 三档 + failover）
model_proxy_cli.sh route add                           # 交互式新增 route 家族模板（写配置后 reload）
model_proxy_cli.sh route edit <id>                     # 交互式编辑 route 的 tiers/failover
model_proxy_cli.sh route del <id>                      # 删除 route（二次确认，被 strategy 引用则拒绝）

model_proxy_cli.sh strategy                             # 不带子命令：打印 list 后进入交互菜单
                                                        #   [a]dd/[e]dit/[d]el/[q]uit
model_proxy_cli.sh strategy list                       # 列出所有 client_token -> route_id 绑定
model_proxy_cli.sh strategy add                        # 交互式新增 strategy 绑定（写配置后 reload）
model_proxy_cli.sh strategy edit <token>                # 交互式编辑 strategy 的 route_id/note
model_proxy_cli.sh strategy del <token>                 # 删除 strategy（二次确认，无下游引用检查）

model_proxy_cli.sh switch <client_token> <route_id>     # 切换某 token 绑定的 route 家族（改 strategy.route_id 后 reload）
model_proxy_cli.sh install                              # 交互式列出四个 SDK + 本机检测状态，选择安装
model_proxy_cli.sh install --list                       # 只列出四个 SDK 检测状态，不安装
model_proxy_cli.sh on                                   # 启动 model_proxy.py（已在监听则跳过）
model_proxy_cli.sh off                                  # 停止 model_proxy.py（严格按脚本绝对路径匹配，绝不影响 v1 的 proxy.py）
model_proxy_cli.sh --help / -h                          # 显示此帮助
```

- `supply`/`route`/`strategy` 三者不带子命令时进入交互菜单（先打印 list，再选操作，
  操作完可回菜单继续或 `q`/回车退出）；带子命令（list/add/edit/del/probe）时兼容旧用法，
  直接执行、不进菜单，脚本化调用不受影响。原子写盘后自动触发 reload。
- `supply probe <id>`：向该 supply 上游直接发一个已知非法的 effort 值
  （`__probe_invalid__`），按 protocol 构造对应探测请求（anthropic 走 `output_config.effort`、
  chat 走 `reasoning_effort`、responses 走 `reasoning.effort`），从报错响应里用宽松正则
  尝试提取"Supported values are: ..."之类枚举并打印，询问是否接受写入
  `reasoning_capability`。解析结果不保证准确（供应商报错格式差异大，部分响应体会被截断），
  需人工核实后再接受。
- 端口默认 18889，同样支持 `MODEL_PROXY_PORT` 环境变量覆盖。
- `admin_token` 从配置文件读取，用于控制 API 的 `X-Proxy-Admin-Token` 鉴权头。
- `off` 只按本脚本同目录下 `model_proxy.py` 的绝对路径精确匹配进程，不会影响 v1 的
  `tools/proxy.py`（18888 生产进程）——两者进程 fingerprint 完全不同，已实测验证隔离。

## 接入各SDK (install)

`install` 命令按 SDK 协议从 strategies 里过滤出协议匹配的 client_token，交互式选择后：
已检测到该 SDK 配置目录则备份原文件后按其格式写入；未检测到则打印配置片段供手动粘贴。
只读 strategies，不改 token→route 绑定关系。

支持四个 SDK：

- **claude**（Claude Code，Anthropic 协议）：写 `~/.claude/settings.json` 的
  `env.ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`，并补齐三个档位环境变量
  （`ANTHROPIC_DEFAULT_OPUS_MODEL`/`_SONNET_MODEL`/`_HAIKU_MODEL` 固定填
  `claude-opus`/`claude-sonnet`/`claude-haiku`）。
- **codex**（codex-cli，Responses 协议）：写 `~/.codex/config.toml` 的
  `[model_providers.model_proxy]` 段（`base_url`/`wire_api="responses"`/`env_key`）及顶层
  `model`/`model_provider`；appkey 走环境变量 `MODEL_PROXY_CODEX_TOKEN` 注入，不写入配置文件。
- **hermes**（协议可选，按 `api_mode` 决定）：标准库无 yaml 解析器，为避免破坏现有文件结构，
  统一打印 `custom_providers` 配置片段供手动粘贴到 `~/.hermes/config.yaml`。
- **openclaw**（协议可选，按 `api` 决定）：写 `~/.openclaw/openclaw.json` 的
  `models.providers.<name>`；若现有文件用了 json5 专属语法（标准库 json 解析失败）则降级为
  打印片段，不强行写入。

四个 SDK 各自按其协议（claude=anthropic，codex=responses，hermes/openclaw 协议可选由用户
在候选 token 里选定）过滤候选 client_token；无匹配协议的 token 时会提示先用 `strategy add`
新增对应绑定。检测到多个匹配 token 时交互式列出供选择。

## 当前状态/已知限制

- 已支持四种协议组合的转发/转换：anthropic→anthropic、responses→responses（均字节透传，
  含 thinking 方言自适应）、anthropic→chat、responses→anthropic（经 `core/translate.py` 转换）。
- cross-supply failover：上游 401/403/429/5xx 触发对应 supply 冷却并按 route 顺序切换到下一个
  supply（不限协议，跨供给单元）。
- thinking/effort 方言自适应：识别网关对 reasoning 语法的 400 拒绝，缓存并转换为对方接受
  的格式重试。
- 错误路径加固：不支持的协议组合、上游 4xx/5xx、流式中途中断均按客户端协议包裹成合法的
  error 响应/事件，不会让客户端挂死。
- 配置支持热重载，不需要重启进程：`ConfigStore` 用 mtime 比对（每次转发请求都会
  `maybe_reload()`），也可用 `model_proxy_cli.sh reload` 主动触发一次强制重载并清空 cooldown。
  若配置文件解析失败（JSON 非法等），保留旧配置并记录 warning 日志，不会导致进程崩溃或
  配置被清空。
- 没有自动重启/自愈 hook，进程崩溃不会自动拉起，需要手动 `on`。
- 未接入自动化测试覆盖真实上游网络调用（转换器单测均为脱网络单测，转发编排本身未做端到端
  自动化测试，依赖手动 curl 验证）。
- effort 探测（`supply probe`）解析结果不保证准确，仅供人工审阅参考。
</content>
