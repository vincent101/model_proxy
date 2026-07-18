# model_proxy

## 这是什么

多协议 AI 模型代理，同时支持 Claude Code（Anthropic `/v1/messages` 协议）和 codex-cli
（OpenAI Responses `/v1/responses` 协议）接入，并可跨协议互相访问对方生态的模型——例如
在 Claude Code 里实际调用 GPT，在 codex 里实际调用 Claude。

与 v1（`tools/proxy.py`）的关系：v1 是纯 Anthropic 生态的 appkey/profile 轮转代理，服务
Claude Code 单一协议，在端口 **18888** 生产运行。v2（本目录 `model_proxy.py`）是新一代多协议
代理，支持协议互转，在端口 **18889**（默认，可配）**实验性**运行。两者当前完全独立并行，
互不依赖、互不干扰，可同时保留。

## 目录结构

```
tools/model_proxy/
├── model_proxy.py                     # 入口（thin wrapper，转发到 core.server.main）
├── model_proxy_config.example.json    # 配置样例
├── model_proxy_cli.sh                 # 手动控制脚本
├── core/                              # 核心实现包
│   ├── __init__.py
│   ├── server.py                      # 主体：HTTP server、路由决策、转发编排、控制 API
│   └── translate.py                   # 双向协议转换器（正向 Anthropic↔Chat + 反向 Responses↔Anthropic）
├── tests/                             # 单测
│   ├── __init__.py
│   ├── test_route.py                  # 三阶段路由匹配单测（resolve_route/resolve_tier/select_supply_list/select_supply）
│   └── test_translate.py              # 双向协议转换器合并单测
├── docs/                              # 规格/蓝图文档
│   ├── model_proxy_buildplan.md       # 施工蓝图（模块划分、实施顺序、风险点）
│   └── model_proxy_translate_spec.md  # 双向协议转换规格（Part 1 正向 / Part 2 反向）
└── samples/                           # 实测样本（网关真实响应，供规格核对字段用）
    ├── anthropic_stream_samples.txt
    └── responses_api_samples.txt
```

## 配置怎么写

配置文件路径固定为 `~/.claude/model_proxy_config.json`（不随代码迁移，本目录只提供
`model_proxy_config.example.json` 作为样例，不含真实凭证）。核心是三段式结构：
**supplies**（供给单元）+ **routes**（家族模板）+ **strategies**（token→家族绑定）。

### supplies：一个供给单元 = 一个上游端点

```json
{
  "id": "gw-claude",
  "url": "https://aigc.sankuai.com/v1/anthropic",
  "protocol": "anthropic",
  "appkey": "<APPKEY_PLACEHOLDER>",
  "target_model": "claude-sonnet-4",
  "reasoning": true,
  "cooldown_seconds": 300
}
```

- `id`：唯一标识，routes 里按 id 引用。
- `url` + `protocol`：上游端点地址与协议（`anthropic` / `chat` / `responses`）。
- `appkey`：鉴权用 Bearer token，注入到转发请求。
- `target_model`：实际下发给上游的模型名（客户端请求里的 model 字段会被替换成这个）。
- `reasoning`：该模型是否支持推理/thinking，影响是否走 thinking 方言适配或 effort 映射。
- `cooldown_seconds`（可选）：该 supply 触发失败后的冷却时长，不填则用顶层
  `default_cooldown_seconds`。

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

### strategies：client_token → route_id 绑定（运行时可切换）

```json
{ "client_token": "cc", "route_id": "claude", "note": "默认 Claude 家族" }
```

- `client_token`：客户端请求 `Authorization: Bearer <token>` 里的 token，model_proxy 据此
  找到对应 strategy。
- `route_id`：该 token 绑定到哪个 route 家族，必须是 routes 里存在的 id。
- `note`：可选备注。
- 禁用一个 token 直接删除其 strategy 记录即可（无 enabled 开关）。

**匹配流程（三阶段）**：请求进来后 ① 用 `client_token` 查 strategies 拿到 `route_id`，再用
`route_id` 拿到 route 家族；② 把请求体 `model` 字段**精确查表**映射成 tier 名
（`claude-opus`→opus / `claude-sonnet`→sonnet / `claude-haiku`→haiku，仅这三个精确值，
非子串猜测）；③ 从 route 的 `tiers` 里按 tier 取出该档 supplies 列表交给 failover 选择。

> **注意**：新架构下 `model` 字段不是上述三个预设值之一时，选路直接 **400 失败，不再有兜底降级**。
> `settings.json` 里的 `ANTHROPIC_DEFAULT_OPUS_MODEL`/`ANTHROPIC_DEFAULT_SONNET_MODEL`/
> `ANTHROPIC_DEFAULT_HAIKU_MODEL` 三个值现在是固定档位标签（分别填 `claude-opus`/`claude-sonnet`/
> `claude-haiku`），**不需要改**；运行时切换家族用 `model_proxy_cli.sh switch <token> <route_id>`
> 改 strategy 绑定，不动 model 标签。

顶层还有 `admin_token`（控制 API 鉴权）和 `default_cooldown_seconds`（默认冷却时长）。
完整样例见 `model_proxy_config.example.json`。

## 怎么启动

手动启动（不随 Claude Code 会话自动拉起，这点和 v1 不同）：

```bash
MODEL_PROXY_PORT=18889 python3 tools/model_proxy/model_proxy.py &
```

端口默认 18889，可用 `MODEL_PROXY_PORT` 环境变量覆盖。日志写到本目录
`.claude_model_proxy.log`（启动时自动截断保留最后 1000 行），进程锁在
`/tmp/claude_model_proxy.lock`（防止同一时刻起多个实例）。

**当前不接 SessionStart hook**：v2 还未经真实流量充分验证，不会像 v1 那样在每次
Claude Code 会话启动时自动拉起，需要手动管理生命周期。

## 怎么控制

用 `tools/model_proxy/model_proxy_cli.sh`（先 `chmod +x`，已在仓库里可直接执行）：

```bash
tools/model_proxy/model_proxy_cli.sh status               # 查看运行状态 + supplies/routes/cooldown
tools/model_proxy/model_proxy_cli.sh reload               # 触发配置热重载
tools/model_proxy/model_proxy_cli.sh clear-cooldown <id>  # 手动清除某 supply 的冷却（幂等）
tools/model_proxy/model_proxy_cli.sh supply list          # 列出所有 supply
tools/model_proxy/model_proxy_cli.sh supply add           # 交互式新增 supply
tools/model_proxy/model_proxy_cli.sh supply rotate-appkey <id> <key>  # 替换 appkey 并解冷
tools/model_proxy/model_proxy_cli.sh route list           # 列出所有 route 家族模板
tools/model_proxy/model_proxy_cli.sh route add            # 交互式新增 route 家族模板
tools/model_proxy/model_proxy_cli.sh strategy list        # 列出所有 token→家族 绑定
tools/model_proxy/model_proxy_cli.sh strategy add         # 交互式新增 strategy 绑定
tools/model_proxy/model_proxy_cli.sh switch <token> <route_id>  # 切换某 token 绑定的家族
tools/model_proxy/model_proxy_cli.sh migrate              # 选一个 strategy 的 token 写入 settings.json
tools/model_proxy/model_proxy_cli.sh on                   # 启动（已在监听则跳过）
tools/model_proxy/model_proxy_cli.sh off                  # 停止
```

- `supply`/`route`/`strategy` 子命令支持交互式增改配置（原子写盘后自动 reload），
  `switch <token> <route_id>` 用参数式改某 token 的 route 家族绑定，`migrate` 可从
  已有 strategies 里选一个 client_token 写入 `~/.claude/settings.json`。详细用法见 `--help`。

- 端口默认 18889，同样支持 `MODEL_PROXY_PORT` 环境变量覆盖。
- `admin_token` 从 `~/.claude/model_proxy_config.json` 读取，用于控制 API 的
  `X-Proxy-Admin-Token` 鉴权头。
- `off` 只按本脚本同目录下 `model_proxy.py` 的绝对路径精确匹配进程，不会影响 v1 的
  `tools/proxy.py`（18888 生产进程）——两者进程 fingerprint 完全不同，已实测验证隔离。

## v1 ↔ v2 怎么切换/回退

客户端配置里指向哪个端口/哪个 token 决定用哪套代理：

- **Claude Code**：改 `~/.claude/settings.json` 里 `env.ANTHROPIC_BASE_URL`（v1 通常是
  `http://localhost:18888/`，切到 v2 改成 `http://localhost:18889/`）及对应
  `ANTHROPIC_AUTH_TOKEN`（需匹配 v2 配置里某条 strategy 的 `client_token`）。
- **codex-cli**：改其配置里 Responses API 的 base_url/token，同理指向 18889。

切回 v1 只需把 base_url 改回 18888，两个 proxy 进程互不干扰，可以同时保留运行，随时切换
不需要互相停止。

## 当前状态

**实验性，未经大规模真实使用验证。**

已完成的能力：
- 四种协议组合的转发/转换：
  1. anthropic → anthropic（PASSTHROUGH，字节透传 + thinking 方言适配）
  2. responses → responses（PASSTHROUGH，字节透传）
  3. anthropic → chat（FORWARD，经 `core/translate.py` 正向部分转换）
  4. responses → anthropic（REVERSE，经 `core/translate.py` 反向部分转换）
- cross-supply failover：上游 401/403/429/5xx 触发对应 supply 冷却并按 route 顺序切换到下一个
  supply（不限协议，跨供给单元）。
- thinking 方言适配（仅组合1）：识别网关对 `thinking.type` 的 400 拒绝，缓存并转换为对方接受
  的格式重试。
- 错误路径加固：UNSUPPORTED 组合、上游 4xx/5xx、流式中途中断均按客户端协议包裹成合法的
  error 响应/事件，不会让客户端挂死。

已知限制：
- 没有自动重启/自愈 hook，进程崩溃不会自动拉起，需要手动 `on`。
- **配置支持热重载，不需要重启进程**：`ConfigStore` 用 mtime 比对（每次请求经过
  `_forward` 都会 `maybe_reload()`），也可用 `model_proxy_cli.sh reload` 主动触发一次强制
  重载（`ConfigStore.reload()`）。若配置文件解析失败（JSON 非法等），会保留旧配置并记录
  warning 日志，不会导致进程崩溃或配置被清空。
- 未接入自动化测试覆盖真实上游网络调用（转换器单测均为脱网络单测，`_forward` 转发编排本身
  未做端到端自动化测试，依赖手动 curl 验证）。
