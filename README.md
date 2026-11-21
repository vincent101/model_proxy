---
created: 2026-07-17 18:52:18
version: 0.15
---
# model_proxy

本地模型代理：按客户端 token、会话和模型档位选择上游，提供协议转换、故障切换和用量统计。版本见 [VERSION](VERSION)。

**五种协议组合**（行是客户端入站，列是上游）：

| 入站 \ 上游 | Anthropic | OpenAI Chat | OpenAI Responses |
|---|---|---|---|
| Anthropic `/v1/messages` | 同协议 | 转换 | 转换 |
| OpenAI `/v1/responses` | 转换 | 501 | 同协议 |
| Chat（不支持入站） | 501 | 501 | 501 |

支持项有流式和非流式实现，但转换不保证语义等价或所有功能组合可用；同协议也会改写模型、推理字段及部分签名或 item ID，并非字节透传。Mac 有实际使用证据，但不是所有上游与功能的验收；**Windows 仅有虚拟 Win32/fake 等测试，未原生验收，只供测试，不用于生产**。

[首次跑通](#首次跑通一次请求) · [接入客户端](#接入其他客户端或手动配置) · [选路与会话](#换上游模型档位或当前会话) · [运维排错](#查看状态更新配置处理失败) · [远程使用](#从另一台电脑使用) · [深入文档](#深入文档)

## 首次跑通一次请求

### 1. 选定项目根和配置

需要 Python 3.10+、可交互的终端和已安装的 Claude Code。以下所有命令都在**要操作的 model_proxy 项目根**执行。clone、worktree、vault 主仓各有自己的根；在 vault 根进入主仓可用 `cd tools/model_proxy`，不要误把其他安装当作本次目标。

Mac：

```bash
cd "目标 model_proxy 项目根目录"
```

Windows **CMD**：

```cmd
cd /d "目标 model_proxy 项目根目录"
```

先检查环境变量 `MODEL_PROXY_CONFIG` 是否指向其他配置，以及目标端口是否被其他安装占用；身在 worktree 不代表一定使用 worktree 的配置或实例。默认配置为 `config/model_proxy_config.json`。下面仅适用于**新安装且未设置其他配置路径**，已有配置不要覆盖。

Mac：

```bash
if [ ! -e config/model_proxy_config.json ]; then
  umask 077
  cp -n config/model_proxy_config.example.json config/model_proxy_config.json && chmod 600 config/model_proxy_config.json
else
  echo "配置已存在，未覆盖"
fi
```

Windows **CMD**：

```cmd
if exist config\model_proxy_config.json (echo 配置已存在，未覆盖) else (copy /-Y config\model_proxy_config.example.json config\model_proxy_config.json)
```

编辑新配置：

1. 示例是 `listen.host: "0.0.0.0"` 加空 `allow_hosts`，**原样会拒绝启动**。本机使用改成 `127.0.0.1`；远程使用见[下文](#从另一台电脑使用)。
2. 为要使用的 supply 填入真实上游完整端点、`target_model` 和 `appkey` 或 `appkey_file`；示例不是可用凭据。示例 `nation1` 的 `sonnet` 还引用 mcli：不用 mcli 时先从该 route 的 tier 列表移除引用，再删除其 supply。使用 mcli 时需有效登录态；`appkey_file`、`extra_headers`、`system_inject` 示例在配置样例中，不应假设本机已有登录态。

   mcli 供应也可改写成 `protocol: catpaw-cli`——端点、`appkey_file`、`X-Working-Dir`、计费 system 块全部内置到模块，supply 只留 `id`/`protocol`/`target_model`/`reasoning_capability`。CatPaw IDE 另用 `protocol: catpaw-ide`，supply 同样是这 4 个字段——凭据与桥库路径由模块默认值给出（换机器改 `config/provider_settings.json`，样例见 `config/provider_settings.example.json`；桥动态库由 `build-catpaw-bridge` workflow 按平台产出，见 [CONFIG](docs/CONFIG.md#桥库从哪来)）。两者的凭据都由 `mtsso_keeper` 维护，本代理只读不刷新。字段与内置值见 [CONFIG](docs/CONFIG.md#两个-catpaw-provider)。
3. 设置相互独立的随机强 `admin_token` 和 strategy `client_token`，核对 routes、tiers 和 supplies 的引用。示例 strategy 使用 `route_pool`，不是单值 `route_id`。
4. 密钥和配置不得提交或分享。Mac 保持私有文件权限；Windows 私有 ACL 检查失败时不要绕过或反复重试：记录并脱敏错误，参考 [Windows 路径与 ACL 限制](docs/designs/2026-11-16-model-proxy-Windows路径ACL默认目录兼容方案.md)（草稿，非已验收修复步骤），必要时请管理员协助核对路径和权限。Windows 尚未原生验收。

### 2. 启动、接线并验证

首次初始化可能要求确认安装来源、旧启动者和账本；在交互终端阅读提示。`on` 只有启动成功或核验到**本安装实例**才算成功；端口占用或身份不明时不要擅停其他实例。

如在 `install` 中选择 Codex，先备份 `~/.codex/config.toml`：安装会改其顶层 `model`、`model_provider` 和 `model_proxy` provider 的地址、协议与 token；审阅预览后再确认。

Mac：

```bash
./model_proxy_cli.sh on
./model_proxy_cli.sh install
./model_proxy_cli.sh status
```

Windows **CMD 测试**：

```cmd
model_proxy_cli.bat on
model_proxy_cli.bat install
model_proxy_cli.bat status
```

`install` 是接线，不安装客户端程序。选择 Claude、对应 client token，审阅脱敏预览并确认；无候选时见[筛选与手配](#接入其他客户端或手动配置)。完全退出并重启 Claude Code，发送一条最小消息并等待完整回答，然后查同一请求：

```bash
./model_proxy_cli.sh logs
```

```cmd
model_proxy_cli.bat logs
```

对照 ACCESS 的 `source`、`route`、`tier`、`supply`、`final_error` 和流终态，确认是预期线路且请求完整。`status` 或单独一个 HTTP 200 都不足以证明上游成功。

## 接入其他客户端或手动配置

`~` 表示用户目录，Windows 通常对应 `%USERPROFILE%`（`~` 不是 CMD 路径语法）。`install` 检测配置目录，不证明客户端程序已安装或版本兼容；写入前会备份并要求确认，备份默认在 `.runtime/backups/`。手配前请自行备份，**合并字段而非覆盖整文件**。

| 客户端 | 配置与默认 base URL（端口 18889） | 当前安装行为 |
|---|---|---|
| Claude Code | `~/.claude/settings.json`；`http://localhost:18889/` | 文件已存在才更新 env；替换地址/token，OPUS/SONNET/HAIKU 已有值保留、缺失才补，FABLE 手加；另处理 `~/.claude.json` onboarding。缺文件提示手配。 |
| Codex | `~/.codex/config.toml`；`http://localhost:18889/v1` | 有配置目录才编辑 TOML，会更改顶层 `model`、`model_provider` 及 provider 的地址、协议和 token；先备份。`wire_api="responses"`，不追加 `/responses`。Python 3.10 需已安装 `tomli`，否则写前失败，不会自动安装。可选 catalog 会联网取 prompt；失败不阻断主接线、保留既有 catalog 配置。 |
| Hermes | `~/.hermes/config.yaml` 和 `.env`；`http://localhost:18889/` | 始终打印片段，手工合并；用 `anthropic_messages`，不要采用打印文案中 `chat_completions` 的建议，本代理不支持 Chat 入站。 |
| OpenClaw | `~/.openclaw/openclaw.json`；`http://localhost:18889/` | 已有标准 JSON 才写/替换 `models.providers.model_proxy`；该节点自定义字段不保证保留。缺文件或 JSON5 解析失败时手配。 |

**候选筛选不是协议能力判断**：安装器取 strategy 的首个有效 route、其中首个可解析 tier/supply 的**上游协议**；Claude 筛 Anthropic，Codex 筛 Responses，Hermes/OpenClaw 不过滤。它不核验全部档位、pool 或后备供给，可能隐藏实际可转换的线路。无候选时未必有完整手配片段：若已有可用跨协议绑定，用其 token 手配；若确无有效绑定，使用 `strategy` 一级菜单或手工补绑定。不要使用程序旧提示中的 `strategy add`。

### Claude / Codex 手动接线

替换 `<PORT>` 和 `<CLIENT_TOKEN>`：端口为实际监听端口，token 是已绑定 strategy 的客户端凭证，不是上游 appkey。客户端配置中的 token 明文存盘，须限制访问。

Claude：将以下字段合并进 `~/.claude/settings.json`；确认所选 route 包含相应 tier。FABLE 仅在需要且 route 已配置时加入：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:<PORT>/",
    "ANTHROPIC_AUTH_TOKEN": "<CLIENT_TOKEN>",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku",
    "ANTHROPIC_DEFAULT_FABLE_MODEL": "claude-fable"
  }
}
```

另在 `~/.claude.json` 合并 `"hasCompletedOnboarding": true`，不是写进 settings。自动安装保留已有模型变量，因此也要核对其值。重启 Claude，发消息并检查 ACCESS。

Codex：合并进 `~/.codex/config.toml`；顶层字段放在表之前，已有 provider 表则编辑而非重复创建。选择线路中可用的固定模型标签。

```toml
model = "claude-sonnet"
model_provider = "model_proxy"

[model_providers.model_proxy]
name = "model_proxy"
base_url = "http://localhost:<PORT>/v1"
wire_api = "responses"
experimental_bearer_token = "<CLIENT_TOKEN>"
```

`name` 是手配字段，不表示安装器自动添加。重启 Codex，发最小消息并检查 ACCESS 的 `source=responses` 和选路、终态。

### Hermes / OpenClaw 手动接线

Hermes 将以下合并到 `~/.hermes/config.yaml`，在 `~/.hermes/.env` 设置 `HERMES_MODEL_PROXY_KEY=<CLIENT_TOKEN>`：

```yaml
custom_providers:
  - name: model_proxy
    base_url: http://localhost:<PORT>/
    key_env: HERMES_MODEL_PROXY_KEY
    api_mode: anthropic_messages
model:
  provider: "custom:model_proxy"
```

OpenClaw 将以下节点合并到 `~/.openclaw/openclaw.json` 的 `models.providers`：

```json
{
  "model_proxy": {
    "baseUrl": "http://localhost:<PORT>/",
    "apiKey": "<CLIENT_TOKEN>",
    "api": "anthropic"
  }
}
```

两者还须在客户端选择线路支持的 tier 标签；片段不代表所有客户端功能已验收。重启客户端并通过最小消息及 ACCESS 验证。

## 换上游、模型档位或当前会话

```text
client_token → strategy → route / route_pool → 请求 model 对应 tier
             → 有序 supply → supply.target_model / protocol / 完整 url → 上游
```

例如虚构的 Anthropic 消息带 `claude-sonnet` 和某 client token：匹配 strategy 后选 route 的 `sonnet` 供给列表，可选中 Responses supply，再以其真实 `target_model` 和端点发送转换后的请求。入站协议由请求路径及 body 识别；普通出站使用 supply 的完整 URL 和净化后的 query，不是任意 path 都无关。模型只精确识别 `claude-opus`、`claude-sonnet`、`claude-haiku`、`claude-fable`，不是任意上游模型名；上游模型由 supply 替换。

客户端凭据 `client_token` 匹配 strategy；`admin_token` 用于控制 API；上游 `appkey`/`appkey_file` 仅供出站，**不要发给客户端**。入站取 Bearer，未取到时回退 `x-api-key`。

### 添加或修改线路

在交互终端按 **supply → route → strategy** 顺序创建普通三档线路，菜单各自用 `[a]dd` 等选择。Mac / Windows CMD 分别执行：

```bash
./model_proxy_cli.sh supply
./model_proxy_cli.sh route
./model_proxy_cli.sh strategy
```

```cmd
model_proxy_cli.bat supply
model_proxy_cli.bat route
model_proxy_cli.bat strategy
```

这些是一级菜单，没有 `supply add` / `strategy add` 等直达命令；非 TTY 只列一次后退出。supply 的 `[t]est` 会请求真实上游，effort 探测需人工核实，不是无副作用的只读检查。`strategy` 菜单重新录入 `tiers_source_capability` 会重建该字段，可能丢失 fable 或其他额外档；先备份并手工保留，或避免在菜单中重新录入。

**操作 route 前先备份**：服务支持 Fable，但 route 的新增/编辑菜单只处理 opus、sonnet、haiku；`[e]dit` 会重建 `tiers`，丢失已有 fable 或其他额外 tier。带 Fable 的 route 应备份后手工合并配置，不要用该编辑菜单。删除 route 时，菜单只检查单值 `route_id`，**不会检查 `route_pool`**；删除前须人工检查并移除所有 pool 引用。

**密钥风险**：`switch` 会将 client token 暴露在进程参数、可能保留在命令历史中，CLI 还会回显 token；双引号也不能阻止 Shell 的 `$` 或 Windows CMD 的 `%` 等展开。不要把真实 token 直接写入可复制的命令。

可用 `strategy` 交互菜单修改整个单值绑定，但菜单仍可能显示 token，请在可信终端操作。`switch` 和菜单均不能编辑 `route_pool`；示例 strategy 正是 pool，修改 pool 须先备份再编辑主配置，`route_id` 与 `route_pool` 互斥。只想切换 pool 的当前会话可用下方 `$route`，但它不修改整个 strategy，且需要可提取的会话标识。

### 只切换当前会话

以下是发到**对话中的独立用户消息**，不是终端命令：

```text
$route             查询当前 route
$route <route_id>  指定当前会话的首选 route，下一条消息生效
$route reset       清除固定值，下一条恢复自动分配
```

设置/reset 只适用于 pool strategy；单值 strategy 可查询但不能切换。可指定顶层 `routes` 中已有的 route，不必在 pool 内；这是首选而非禁止兜底，缺 tier 或供给耗尽时仍可能使用 pool 中其他 route。override 写在主配置同目录的 `session_overrides.json`。需要有效 strategy、可提取的会话标识和有效消息列表；Claude Code 是典型场景，Responses 也有拦截分支，但须能提取会话标识且 `input` 为列表，不能保证所有 Codex 会话可用。无标识时 pool 以首个有效候选为主选；标识变化不保证固定同一线路。

### 调整 effort

source 能力按客户端 token 配在 strategy，target 能力配在 supply；映射按双方档位的**相对排名**，不保证同名强度不变。例如 source `[low, medium, high]`、target `[low, high]` 时，source 的 `medium` 映射到 target 的 `high`。source 能力需人工录入，target 探测也可能不准；增加 target 档位可能改变已有映射，修改后应整体核对。[REASONING](docs/REASONING.md) 解释算法，但其中 `strategy add/edit` 指一级菜单操作，不是直达命令。

## 查看状态、更新配置、处理失败

在项目根调用对应入口；下表是子命令，不是独立程序。Mac 前缀 `./model_proxy_cli.sh`，Windows CMD 前缀 `model_proxy_cli.bat`。

| 子命令 | 用途 |
|---|---|
| `on` / `off` / `restart` | 启动 / 停止 / 重启已核验实例 |
| `status` / `reload` | 查看状态 / 强制加载配置并报告结果 |
| `logs [N] [field=value]` | 默认最近 30 条 ACCESS，可按请求、级别或事件过滤 |
| `stats [today\|month\|YYYY-MM-DD\|YYYY-MM]` | 独立累计账本；不传为全历史，日/月按 UTC+8 |
| `supply` / `route` / `strategy` | 配置交互菜单 |
| `switch <client_token> <route_id>` | 切换单值 strategy；pool 不支持 |
| `install` / `--help` | 客户端接线 / 帮助 |

常用查询，Mac：

```bash
./model_proxy_cli.sh logs 50
./model_proxy_cli.sh logs req=abc12345
./model_proxy_cli.sh logs level=ERROR
./model_proxy_cli.sh logs event=cooldown
./model_proxy_cli.sh stats today
./model_proxy_cli.sh stats month
```

Windows **CMD**：

```cmd
model_proxy_cli.bat logs 50
model_proxy_cli.bat logs req=abc12345
model_proxy_cli.bat logs level=ERROR
model_proxy_cli.bat logs event=cooldown
model_proxy_cli.bat stats today
model_proxy_cli.bat stats month
```

1. **保存是否生效？** 保护性保存成功不等于 reload 成功，要检查结果。后续请求自动重载不会清 cooldown；手动 reload 进入认证 handler 后即使加载失败也会尝试清 cooldown，未进入或断线不能假定成功。轮换 `admin_token` 时磁盘与内存可能暂时不一致导致 401，不要关闭鉴权。
2. **是否需要重启？** `reload` 不换代码，也不更新启动时的 `listen`/`allow_hosts`；代码或监听变化须由使用者安排 `restart`。`off`/`restart` 不排空请求，可能截断流、漏记未持久化用量，并清内存学习态。
3. **检查同 route 切换条件**：核对顶层 `cooldown_rules` 的首条匹配、`route.failover` 和可用供给；HTTP 错误或 `URLError` 未匹配时不自动切换，空 `errorcode` 列表不是兜底。
4. **检查 pool 与正文错误**：pool 可在缺 tier、供给耗尽时尝试其他 route，但并非所有错误都遍历。非流式正文错误须被识别且匹配冷却规则才可能重试；结合 [正文错误机制](docs/designs/2026-10-10-正文错误识别与failover衔接方案.md) 和日志定位。
5. **检查流是否已提交**：一旦提交，本次请求不能换供给；部分正文错误收口后冷却，仅影响后续请求。预算放大只在特定非流式截断条件下发生；[仅思考内容兜底](docs/impl-reports/2026-08-07-chat-reasoning-mirror-batch3.md)不等于正式回答。

日志默认 `.runtime/model_proxy.log`，启动时保留末 5000 行；账本独立存于 `.runtime/model_proxy_totals.json`，其他路径见 [runtime_paths.json](config/runtime_paths.json)。日志、账本、sidecar、备份可能含敏感信息，分享前脱敏并限制访问。旧项目根 `.model_proxy_totals.json` **不会自动迁移**；v2→v3 是已选账本的 schema 升级，不是路径迁移。需承接时必须先停稳旧进程并备份旧账本，再迁移；新路径已有账本不要覆盖，迁移后核对统计；顺序见 [CONFIG 账本说明](docs/CONFIG.md)。

## 从另一台电脑使用

仅在可信 LAN 使用。服务端没有 `listen` 时才回退到 `127.0.0.1`、有效 `MODEL_PROXY_PORT` 或默认 18889；显式 `listen` 的 host/port 必须完整合法，配置端口优先，非法环境端口也会失败。

1. 在服务端确定地址：Mac 用 `scutil --get LocalHostName`，取其小写 `.local` 名；可在 DHCP 保留一个固定 IP 作为回退。将实际名称和 IP 替换下列占位符，合并到配置顶层；非 loopback 监听必须有合法、非空 `allow_hosts`，条目端口须与监听端口一致。

```json
{
  "listen": {"host": "0.0.0.0", "port": 18889},
  "allow_hosts": ["server.local:18889", "192.168.1.10:18889"]
}
```

2. 检查防火墙和网络可达范围，由使用者安排重启目标实例：Mac `./model_proxy_cli.sh restart`；Windows CMD `model_proxy_cli.bat restart`。远端客户端修改前述接入配置的主机/端口、保留自己的 client token；Codex 仍用 `/v1`，不要复制上游 appkey。
3. 在远端检查 TCP（Mac 终端或 Windows PowerShell，替换主机名）：

```bash
nc -vz "server.local" 18889
```

```powershell
Test-NetConnection -ComputerName "server.local" -Port 18889
```

TCP 可达不等于模型可用：重启远端客户端、发送最小消息，在服务端对照该请求 ACCESS 的选路和终态。

**安全边界**：`allow_hosts` 只检查可伪造的 HTTP Host、缓解 DNS rebinding，不是客户端鉴权或来源 IP 白名单；HTTP 无 TLS，凭证可能被网络观察。保护 token、限制网络可达面，禁止通过端口转发、UPnP 或 DMZ 暴露公网。

本机 SessionStart 可通过 `hooker/ensure_model_proxy.sh`（Windows 对应 `.bat`）委托 CLI `on`；`install` 仅识别标准 vault 布局并经确认时维护 hook，独立 clone/worktree 不向上猜 vault，首次交互确认仍须本人完成。运行中崩溃不会自动自愈；服务端睡眠、关机或离网不可达，远端请求不会触发其 hook，须在服务端恢复。

其他 MCP 服务须独立部署和管理；用户环境的可选说明见 [CONFIG](docs/CONFIG.md#用户环境可选-mcp-服务)。

## 深入文档

- [CONFIG](docs/CONFIG.md)：字段、顶层冷却规则、pool、sidecar 和账本；route 支持四档，CLI 菜单仅处理三档。
- [ARCHITECTURE](docs/ARCHITECTURE.md)：协议识别、选路与请求处理链路。
- [REASONING](docs/REASONING.md)：effort 相对排名和调试。
- [协议转换规格](docs/designs/model_proxy_translate_spec.md)：字段映射；[CHANGELOG](CHANGELOG.md) 和 [设计记录](docs/designs/)：历史背景。
