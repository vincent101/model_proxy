> 本文是 [README.md](../README.md) 的深入展开。日常配置与使用见 README。

# 架构与请求处理

## 1. 入站鉴权识别

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

## 2. 入站协议识别

入站 source 协议由 `detect_source` 按客户端请求 path 尾缀判断（大小写不敏感）：
`/v1/messages`→anthropic、`/v1/responses`→responses、`/chat/completions`→chat；三者互斥
的尾缀都不命中时，退而看请求体特征（`input` 字段→responses；`messages`+`max_tokens`/
`system`→anthropic；仅 `messages`→chat）；仍不中则 `unknown`，走该组合的兜底/501。

客户端把 `base_url` 配到本代理后，无论自己在其后拼了什么路径（根路径、`/v1/messages`、
`/chat/completions`，甚至多余的斜杠如 `//chat/completions`）都不影响识别；出站转发时统一
丢弃客户端 path，只用配置好的 `supply.url` + 净化后的 query 拼真实上游请求（见
`_sanitize_forward_query`），代理自己决定往上游发什么路径，与客户端怎么拼无关。

## 3. 三阶段匹配（含跨 route 兜底）

请求进来后：

1. 用 `client_token` 查 strategies 拿到该 strategy 的 route 候选列表（`extract_route_candidates`）：
   - 旧写法 `route_id`（单值）：候选列表只有这一个 route（或该 id 不存在则候选为空）。
   - 新写法 `route_pool`（多值）：按 sidecar 的 session override 优先匹配、否则按
     session_key 一致性哈希，排出一个有序候选列表（详见 [CONFIG.md](CONFIG.md)「route_pool
     哈希分配」）。
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

## 4. effort 映射链路

链路：`decode(source) → remap(source_cap, target_cap) → abstract_encode → syntax_adapt(target)`。

核心思想：客户端的档位选择是相对自己表面模型的排名，会按比例映射到真实上游的档位排名，而不是
把档名钉死在全局绝对值上。

完整公式、边界条件与单调性详见 [REASONING.md](REASONING.md)「effort 跨模型映射算法」。

## 5. 出站转换与转发

一个客户端请求进入代理到发往真实上游，经过下面这条链路。

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
│ ④ effort 映射                          （见「effort 映射链路」）   │
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

## 6. 启动与停止

```bash
tools/model_proxy/model_proxy_cli.sh on
```

或手动：

```bash
MODEL_PROXY_PORT=18889 python3 tools/model_proxy/model_proxy.py &
```

端口默认 18889，可用 `MODEL_PROXY_PORT` 环境变量覆盖。日志写到本目录
`.model_proxy.log`（启动时自动截断保留最后 5000 行），进程锁在
`/tmp/model_proxy.lock`（防止同时起多个实例）。路径配置见
`config/runtime_paths.json`。

`tools/model_proxy/hooker/ensure_model_proxy.sh` 已注册到 `.claude/settings.json` 的
`hooks.SessionStart`，随 Claude Code 会话启动自动拉起（幂等：已运行则直接退出，未运行则启动
并等待就绪最多 5 秒；PID 文件 `/tmp/model_proxy.pid`、锁 `/tmp/model_proxy_start.lock`）。
这条 hook 的路径正确性由 `install` 流程负责
维护——`install` 每次运行都会检测 `SessionStart` 里是否存在一条正确指向当前 model_proxy 实际
安装位置的 hook 条目，缺失/路径错误（如目录被移动过）时清理旧条目并预览确认后补齐，不需要
手动同步维护这条硬编码路径。

停止用 `model_proxy_cli.sh off`：只按本脚本同目录下 `model_proxy.py` 的绝对路径精确匹配进程，
并额外反查监听该端口、命令行含 `model_proxy.py` 的 PID 兜底。

### HTTP 协议层行为（2026-08-20 起）

`ModelProxyHandler.protocol_version = "HTTP/1.1"` + `timeout = 30`——修复此前 HTTP/1.0 响应
头 + chunked 传输体的非标组合导致的 codex（Rust hyper）流式读取断连；HTTP/1.1 要求响应具备
`Content-Length` 或 chunked 终止符，全部响应路径已满足。另：三个转换流式方法的
`except BrokenPipeError` 分支均补调 `adapter.finalize()`（幂等收尾），客户端断连时上游
适配器状态机正常收口。

> 日志级别、ACCESS 字段、translate 限流、token 统计等运维内容见
> `docs/designs/2026-07-22-access-log-and-latency.md`、
> `docs/designs/2026-07-23-usage-totals-ledger.md`、
> `docs/designs/2026-08-08-log-optimization-plan.md`。
