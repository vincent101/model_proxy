# model_proxy 施工蓝图（总装图）

> 用途：把 model_proxy "整体怎么搭起来、分几个文件、模块怎么调、先写哪块、每块怎么验证"讲清楚，交给 implementer 照着分模块实现。
>
> **本文不重复协议字段映射细节**——那些查合并后的双向规格
> `tools/model_proxy/docs/model_proxy_translate_spec.md`：
> - 正向（Anthropic↔Chat Completions）：见该规格 Part 1（下称"正向规格"）
> - 反向（Responses↔Anthropic）：见该规格 Part 2（下称"反向规格"）
>
> 本文只讲：文件结构、模块划分与接口签名、实施顺序、每阶段验证、风险、交接说明。

---

## 0. 硬约束（施工前先钉死）

1. **绝对不碰线上 `tools/proxy.py`**（端口 18888 生产运行中）。model_proxy 全新独立文件，可**拷贝**其成熟机制到新文件，**不 import 旧文件**。
2. **新端口 18889**（`MODEL_PROXY_PORT`，默认 18889）；**新配置** `~/.claude/model_proxy_config.json`；**新进程锁** `/tmp/claude_model_proxy.lock`；**新日志** `tools/model_proxy/.claude_model_proxy.log`。与 18888 完全隔离并行跑。
3. **纯标准库**：`json / hashlib / secrets / urllib / http.server / threading / fcntl / time`。不引第三方。
4. **验证隔离**：所有 curl 打 `127.0.0.1:18889`，绝不动 18888。切换由用户最后手动完成，本工程只到"18889 可用"为止。

---

## 1. 文件结构

```
tools/
├── proxy.py                          # 【线上·只读·禁改】仅作参考来源
└── model_proxy/
    ├── model_proxy.py                       # 主文件：基座+配置+cooldown+路由+转发+写回+控制API+main
    ├── model_proxy_translate.py             # 正向转换器（模块 A/B/C/D）+ 共享 SSE/工具辅助
    ├── model_proxy_translate_reverse.py     # 反向转换器（模块 A'/B'/C'/D'）+ Responses SSE 辅助
    ├── model_proxy_config.example.json      # 新 schema 配置样例（供用户填 appkey）
    ├── test_model_proxy_translate.py        # 正向转换器单测（脱网络）
    └── test_model_proxy_translate_reverse.py# 反向转换器单测（脱网络）
```

**为什么这样切**：
- 转换器**独立成文件**，纯 dict→dict / 状态机，可脱离 HTTP 单测（两份规格 §6.4 都要求）。正反向分两个文件，各自对齐一份规格，互不 import。
- 分发/路由/cooldown 是小纯函数 + 内存状态，放主文件 L2 段落，不单独成文件。
- 主文件只 `import model_proxy_translate` 和 `model_proxy_translate_reverse`，反向文件**不** import 正向文件（反向规格里"引用正向规格"是指文档引用，代码里 Anthropic 侧结构各自实现）。

**拷贝 vs 新写标注**（详见第 2 节每模块）：

| 来源 | 拷贝改造自 proxy.py | 全新写 |
|---|---|---|
| model_proxy.py | 日志/_trim_log、ConfigStore(mtime热重载骨架)、进程锁、ThreadingHTTPServer骨架、appkey注入、`_write_streaming_response`/`_write_buffered_response`/`_send_json`、剥离query参数、thinking方言适配四函数 | CooldownStore、L2路由全部、`_forward`编排（重写不继承旧大泥球）、`_write_translated_stream`/`_write_responses_stream`、error_body_for_source、控制API内容 |
| model_proxy_translate.py | — | 全部 |
| model_proxy_translate_reverse.py | — | 全部 |

---

## 2. 模块划分与接口签名

> 类型标注仅示意（都是 `dict`/`bytes`/`str`/`list`，标准库）。签名可微调，但**协议字段映射必须照两份规格，不得自创**。

### L0 基座（model_proxy.py 顶部）—— 拷贝 proxy.py

```python
LOG_FILE = Path(__file__).parent / ".claude_model_proxy.log"   # 改名
def _trim_log(path, keep=1000) -> None: ...                  # 拷贝
# logging.basicConfig(...) 拷贝
_DEFAULT_CONFIG_PATH = Path.home()/".claude"/"model_proxy_config.json"  # 改名
_LOCK_FILE = Path("/tmp/claude_model_proxy.lock")              # 改名
```

### L1 配置：ConfigStore —— 拷贝骨架 + 换 getter

拷贝 proxy.py 的 `ConfigStore`（mtime 热重载、`maybe_reload` 双重检查、`_reload_locked` 失败保留旧配置、`reload()`），**只换 getter 适配新 schema**：

```python
class ConfigStore:
    def __init__(self, config_path=None, on_reload=None): ...   # 拷贝
    def maybe_reload(self) -> bool: ...                          # 拷贝原样
    def reload(self) -> None: ...                                # 拷贝原样
    # —— 新 getter ——
    def get_supplies(self) -> list[dict]: ...        # config["supplies"]
    def get_supply_map(self) -> dict[str, dict]: ... # {supply["id"]: supply}
    def get_routes(self) -> list[dict]: ...          # config["routes"]（有序）
    def get_admin_token(self) -> str: ...
    def get_default_cooldown(self) -> int: ...       # config.get("default_cooldown_seconds", 300)
```

> ⚠️ 已于本次重构废弃 tier 分档设计，改为 client_model 精确匹配，详见 README.md。
> 以下 `model_tier` 相关描述为历史设计原文，保留不改，实际字段名与语义以 README.md 为准。

**新配置 schema**（一次性新写，无旧配置降解兼容）：

```json
{
  "admin_token": "xxx",
  "default_cooldown_seconds": 300,
  "supplies": [
    {
      "id": "gw-claude",
      "url": "https://aigc.sankuai.com/v1/anthropic",
      "protocol": "anthropic",
      "appkey": "<key>",
      "target_model": "claude-sonnet-4",
      "reasoning": true,
      "cooldown_seconds": 300
    },
    {
      "id": "gw-gpt-native",
      "url": "https://aigc.sankuai.com/v1/openai/native",
      "protocol": "chat",
      "appkey": "<key>",
      "target_model": "gpt-4o",
      "reasoning": true
    }
  ],
  "routes": [
    {
      "match": {"client_token": "cc-token-1", "model_tier": "sonnet"},
      "supplies": ["gw-claude", "gw-gpt-native"],
      "failover": "on"
    }
  ]
}
```

字段语义：
- `supply.protocol` ∈ `"anthropic" | "chat" | "responses"`——**上游**说的是哪种协议（决定 target）。
- `supply.target_model`：把入站 model 改写成上游真实 model 名（等价旧 model_map，但绑到 supply）。缺省则原样透传。
- `supply.reasoning`：该上游 model 是否 reasoning 模型（传给转换器的 `model_is_reasoning`）。
- `supply.cooldown_seconds`：本 supply 冷却时长；缺省用顶层 `default_cooldown_seconds`。
- `route.match.client_token`：入站 Authorization Bearer 值；`model_tier` 可选（缺省则该 route 匹配该 token 的所有 model）。
- `route.supplies`：**有序** supply id 列表，failover 按此序尝试。
- `route.failover`：`"on"` 允许跨 supply 故障转移，`"off"` 只用第一个。

### L1 状态：CooldownStore —— 全新（错误信号驱动，纯内存，不写盘）

```python
class CooldownStore:
    def __init__(self):
        self._until: dict[str, float] = {}   # supply_id -> cooldown_until(epoch秒)
        self._lock = threading.Lock()
    def is_cooling(self, supply_id: str) -> bool: ...        # now < until
    def cooldown(self, supply_id: str, seconds: int) -> None: ...  # until = now + seconds
    def clear(self, supply_id: str) -> None: ...            # 手动清除（控制API用）
    def snapshot(self) -> dict[str, float]: ...             # supply_id -> 剩余秒（status展示）
```

> 与旧 StateStore 的本质区别：**不记账、不轮转游标、不写盘**。冷却完全由上游错误信号驱动。ThreadingHTTPServer 多线程，故必须加锁。

### L2 路由决策 —— 全新（纯函数 + 编排）

```python
# 入站 source 协议识别（反向规格 §6.2）
def detect_source(path: str, body: dict) -> str: ...
    # path 尾 /v1/messages → "anthropic"；/v1/responses → "responses"；
    # /chat/completions → "chat"；否则看 body 特征；都不中 → "unknown"

# model → tier（简单前缀/映射；实现时按实际 model 名规则定，可先粗粒度）
def resolve_model_tier(model: str) -> str: ...
    # 例：含 "opus"→"opus"，"sonnet"→"sonnet"，"haiku"→"haiku"，其余→"default"

# 匹配 route：client_token 相等 且 (match 无 model_tier 或 tier 命中)
def match_route(routes: list, client_token: str, model_tier: str) -> dict | None: ...

# 从 route.supplies 有序取第一个「未冷却」的 supply
def select_supply(route: dict, supply_map: dict, cooldown: CooldownStore) -> dict | None: ...

# target 协议 = supply["protocol"]（直接取）
def detect_target(supply: dict) -> str: ...

# 四组合分发（反向规格 §6.2 决策表）
PASSTHROUGH, FORWARD, REVERSE, UNSUPPORTED = "passthrough","forward","reverse","unsupported"
def pick_translator(source: str, target: str) -> str: ...
    # (anthropic,anthropic)|(responses,responses) → PASSTHROUGH
    # (anthropic,chat) → FORWARD ；(responses,anthropic) → REVERSE ；其余 → UNSUPPORTED
```

**四组合总表**（钉死，实现时对照）：

| # | source（客户端） | supply.protocol（上游） | mode | 转换器 |
|---|---|---|---|---|
| 1 | anthropic（claudecode /v1/messages） | anthropic | PASSTHROUGH | 无（字节透传）+ thinking 方言适配 |
| 2 | responses（codex /v1/responses） | responses | PASSTHROUGH | 无（字节透传） |
| 3 | anthropic | chat | FORWARD | model_proxy_translate（A/B/C/D） |
| 4 | responses | anthropic | REVERSE | model_proxy_translate_reverse（A'/B'/C'/D'） |
| 其余 | — | — | UNSUPPORTED | 返回 source 协议合法 error |

### L3 转换分发 —— 在 `_forward` 里按 mode 分支调对应转换器（见 L4）

### L4 转发核心 + 写回（model_proxy.py）

`_forward` 编排（全新重写，不继承旧 `_forward` 大泥球）：

```python
def _forward(self, method: str):
    cs.maybe_reload()
    # 1. 读 body、Bearer token、request_model
    # 2. source = detect_source(path, body)
    # 3. route = match_route(routes, token, resolve_model_tier(model))；无 → error_body_for_source
    # 4. failover 循环：
    #    while True:
    #      supply = select_supply(route, supply_map, cooldown)
    #      if supply is None: 回写"全部冷却/失败" error（按 source）; return
    #      target = detect_target(supply)
    #      mode = pick_translator(source, target)
    #      改写 model（supply.target_model）、构造 target_url、注入 appkey
    #      按 mode 发上游并写回（见下）
    #      若上游返回冷却信号(429/403/5xx) 且 route.failover=on:
    #          cooldown.cooldown(supply.id, supply冷却秒); 从候选里排除该 supply; continue
    #      否则：写回结果; return
```

发上游 + 出站鉴权（拷贝 proxy.py 的 urllib 用法 + query 剥离）：

```python
# 复用 proxy.py: 剥离 ?beta=true；skip headers {host,content-length,authorization,x-api-key}
# 注入 fwd_headers["Authorization"]=f"Bearer {appkey}"；fwd_headers["x-api-key"]=appkey
# urllib.request.urlopen(req, timeout=600)；HTTPError 取 code/headers/body
```

写回函数：

```python
# —— 拷贝 proxy.py ——
def _write_streaming_response(self, status, headers, resp) -> None: ...   # 透传：8192字节 chunked（组合1/2）
def _write_buffered_response(self, status, headers, body) -> None: ...     # 缓冲响应（错误/非流式）
def _send_json(self, status, body) -> None: ...

# —— 全新：正向流式（组合3）——
def _write_translated_stream(self, status, headers, upstream_resp, adapter) -> None: ...
    # 正向规格 §3.6：逐行读 OpenAI SSE；data:[DONE]→adapter.finalize()；
    # 每 chunk→adapter.feed(chunk)→list[event dict]→sse_event_bytes→chunked 写出

# —— 全新：反向流式（组合4）——
def _write_responses_stream(self, status, headers, upstream_resp, adapter) -> None: ...
    # 反向规格 §3.6：按 "\n\n" 切 Anthropic SSE，解析 (event_type, data)；
    # adapter.feed(event_type, data)→list[event dict]→responses_sse_bytes→chunked 写出
    # 无 [DONE]，message_stop 触发 response.completed
```

组合 3/4 非流式：读全上游 body → 调模块 B / B' → `_write_buffered_response`（Content-Type 按 source）。

### 错误处理 —— 全新

```python
def error_body_for_source(source: str, http_status: int, message: str) -> bytes: ...
    # source=="anthropic" → 正向规格 §5.1 Anthropic error：{"type":"error","error":{"type":<映射>,"message":...}}
    # source=="responses" → 反向规格 §5.1 Responses error：{"error":{"message":...,"type":<映射>,"code":null,"param":null}}
    # source 未知 → 通用 {"error":{"message":...}}
    # http_status 决定 error.type：4xx→invalid_request_error，5xx→api_error/server_error
```

**上游 HTTP 错误也要按 source 协议包裹**（正向 §5.2 / 反向 §5.2）：不能把 OpenAI/Anthropic 原始 error 结构直接透传给不同协议的客户端。

### thinking 方言适配 —— 拷贝 proxy.py，仅用于组合1

从 proxy.py 拷贝 `_get_thinking_fmt / _set_thinking_fmt / _parse_thinking_error / _apply_thinking_fmt` 及 `_THINKING_FMT_CACHE`。**只在 mode==PASSTHROUGH 且 source==anthropic（组合1，上游 Anthropic）时启用**：预转换查缓存 + 400 自适应重试一次。组合 3/4 走 reasoning_effort/effort 映射，**不触发** thinking 重试。

### 控制 API + 进程锁 + HTTP server 骨架 —— 拷贝骨架 + 换内容

```python
_CONTROL_PATH_PREFIX = "/model_proxy"     # 改前缀，避免与 18888 的 /proxy 混淆
# 拷贝：do_GET/do_POST 分派、_dispatch_control 鉴权（X-Proxy-Admin-Token）、handle_error/log_message
# 新内容：
#   GET  /model_proxy/status         → 回显 supplies/routes + cooldown 剩余秒（cooldown.snapshot()）
#   POST /model_proxy/reload         → cs.reload()
#   POST /model_proxy/supply/<id>/cooldown/clear → cooldown.clear(id)（手动解冷）
# main(): 拷贝进程锁(改锁文件名) + ThreadingHTTPServer(改端口) + 挂 cooldown_store
```

---

## 3. 实施顺序（分阶段，每阶段一个可验证里程碑）

依赖图：`阶段0 → 阶段1 → {阶段2, 阶段3 可并行} → 阶段4 → 阶段5`

### 阶段 0：骨架能启动能读配置
- **做什么**：L0 基座 + ConfigStore（新 schema）+ 进程锁 + 日志 + ThreadingHTTPServer(18889) + 控制 API 骨架（先只实现 `/model_proxy/status` 回显 supplies/routes）。写 `model_proxy_config.example.json`。
- **依赖**：无。
- **完成标志**：进程能起、能读新配置、`/model_proxy/status` 返回配置回显。
- **验证**：
  ```bash
  cp tools/model_proxy/model_proxy_config.example.json ~/.claude/model_proxy_config.json   # 填测试 appkey
  MODEL_PROXY_PORT=18889 python3 tools/model_proxy/model_proxy.py &
  curl -s -H "X-Proxy-Admin-Token: xxx" http://127.0.0.1:18889/model_proxy/status | python3 -m json.tool
  # 确认 18888 仍在：curl -s http://127.0.0.1:18888/... 不受影响
  ```
- **工作量**：小（大半拷贝）。**风险**：低。

### 阶段 1：纯透传路由 + cooldown + failover（组合 1、2）
- **做什么**：CooldownStore + L2 路由全部（detect_source/resolve_model_tier/match_route/select_supply/detect_target/pick_translator）+ `_forward` 编排 + 发上游 + appkey 注入 + 透传写回（`_write_streaming_response`/`_write_buffered_response`）+ error_body_for_source。**只接 PASSTHROUGH 分支**（FORWARD/REVERSE 先返回"未实现" error 占位）。
- **依赖**：阶段 0。
- **完成标志**：claudecode 指向 18889 能透传到网关 Anthropic 端点拿到响应（组合1，含流式）；codex 指向 18889 能透传到 Responses 端点（组合2）；配置里第一个 supply 用必然失败的假 appkey 时，429/403 触发 cooldown 并 failover 到第二个 supply。
- **验证**：
  ```bash
  # 组合1 透传（非流式）
  curl -s http://127.0.0.1:18889/v1/messages -H "Authorization: Bearer cc-token-1" \
    -H "Content-Type: application/json" \
    -d '{"model":"claude-sonnet-4","max_tokens":64,"messages":[{"role":"user","content":"说\"ok\""}]}'
  # 组合1 流式：加 "stream":true，确认收到 event: message_start ... message_stop
  # 组合2 透传：POST /v1/responses（codex 格式 body），确认拿到 Responses 响应
  # failover：route.supplies=[坏key supply, 好key supply]，发请求应最终成功；
  #   curl status 看坏 supply 进入 cooldown（剩余秒 > 0）
  ```
- **工作量**：中。**风险**：中（failover 循环边界、cooldown 加锁、透传流式与旧一致）。

### 阶段 2：正向协议转换（组合 3：anthropic→chat）
- **做什么**：写 `model_proxy_translate.py`（模块 A/B/C/D + 辅助，**照正向规格 §1–§4、§6**）。写 `test_model_proxy_translate.py`（正向规格 §6.4 用例）。接入 `_forward` 的 FORWARD 分支：非流式→模块B→buffered 写回；流式→`_write_translated_stream(AnthropicStreamAdapter)`。
- **依赖**：阶段 1（复用路由/转发/写回框架）。
- **完成标志**：单测全绿；claudecode 发 /v1/messages、命中 protocol=chat 的 supply，能转 Chat Completions 发 native 端点，响应转回 Anthropic；流式 SSE 正常；工具调用（含流式重组）正常。
- **验证**：
  ```bash
  python3 tools/model_proxy/test_model_proxy_translate.py            # 先跑单测（脱网络）
  # 非流式：curl /v1/messages（token 命中走 chat 的 route），确认返回合法 Anthropic JSON
  # 流式：加 stream:true + tools，确认 content_block_start/input_json_delta/content_block_stop 序列正确
  ```
- **工作量**：大（流式状态机 C+D 是已知难点）。**风险**：高（SSE 状态机、工具分片重组）。

### 阶段 3：反向协议转换（组合 4：responses→anthropic）
- **做什么**：写 `model_proxy_translate_reverse.py`（模块 A'/B'/C'/D' + 辅助，**照反向规格 §1–§4、§6**）。写 `test_model_proxy_translate_reverse.py`（反向规格 §6.4 用例）。接入 `_forward` 的 REVERSE 分支：非流式→B'→buffered 写回；流式→`_write_responses_stream(ResponsesStreamAdapter)`。
- **依赖**：阶段 1（与阶段 2 独立，可并行开发）。
- **完成标志**：单测全绿；codex 发 /v1/responses、命中 protocol=anthropic 的 supply，转 Anthropic 发网关，响应转回 Responses；流式 `sequence_number` 从 0 连续递增、以 `response.completed` 收尾；工具调用正常。
- **验证**：
  ```bash
  python3 tools/model_proxy/test_model_proxy_translate_reverse.py    # 先跑单测
  # 非流式：curl /v1/responses（走 anthropic 上游的 route），确认返回照样本1/3 结构的 Responses JSON
  # 流式：确认 data: 单行、无 event: 行、无 [DONE]、seq 连续
  ```
- **工作量**：大（input items 重分组 + Responses 流式事件多）。**风险**：高（同阶段2 + 反向 input 分组、reasoning 丢弃）。

### 阶段 4：加固（错误路径 + 边界降级 + thinking 接入）
- **做什么**：UNSUPPORTED 组合返回对应 source 的合法 error；上游 4xx/5xx 按 source 包裹（正/反向 §5.2）；流式中途出错发对应 error 事件（正向 §5.1 `event: error` / 反向 §5.1 `response.failed`）；接入 thinking 方言适配到组合1透传路径；落实两份 §5 降级项（cache_control/thinking 块/托管工具/图片 tool_result/max·xhigh effort 等）；控制 API 补 `/model_proxy/supply/<id>/cooldown/clear`。
- **依赖**：阶段 1/2/3。
- **完成标志**：任何异常路径都返回合法 error（客户端不挂死）；组合1 thinking 请求走通。
- **验证**：构造坏 body、UNSUPPORTED 组合（如 responses→chat 的 route）、上游 400、流式中途断——逐一确认返回结构合法。
- **工作量**：中。**风险**：中。

### 阶段 5：切换准备与观测（收尾）
- **做什么**：`/model_proxy/status` 展示 cooldown 剩余秒；review 日志噪声；写一段"如何把客户端从 18888 切到 18889"的说明（改客户端 base_url，不改 18888）。
- **依赖**：阶段 4。
- **完成标志**：用户可照说明手动切换，随时可切回 18888。
- **工作量**：小。**风险**：低。

---

## 4. 每阶段验证方法（隔离要点）

- **一律打 18889**，禁止对 18888 发任何写操作；每阶段验证前后各 `curl 18888/proxy/status`（旧控制 API）确认线上未受影响。
- **假 appkey supply** 是验证 failover/cooldown 的关键手段：配一个必然 401/403 的 supply 放在 route.supplies 首位，真 key 放次位，验证"首位冷却→failover 到次位成功"。
- **转换器先脱网络单测再接线**：阶段 2/3 必须先 `python3 test_*.py` 全绿，再接 `_forward`。录制的 chunk/event 序列直接抄两份规格 §6.4 的重点用例编号（正向 5 类流式用例、反向 7 类）。
- **流式核对**：用 `curl -N`（不缓冲）观察 SSE 逐事件到达；正向核对 Anthropic 事件序列（正向 §3.1），反向核对 `data:` 单行 + seq 连续 + `response.completed` 收尾（反向 §3.1）。
- **进程互斥**：新锁文件 `/tmp/claude_model_proxy.lock`，与 18888 的 `/tmp/claude_proxy.lock` 不同名，两进程可同时跑。

---

## 5. 风险与关键决策点

### 5.1 最易出错处
1. **流式 SSE 状态机（正向 C+D / 反向 C'+D'）**——头号难点。块索引单调递增、切块时先 stop 旧块再 start 新块、工具分片重组。**对策**：先脱网络单测跑通规格 §6.4 全部用例再接线；`partial_json`/`arguments` 片段**原样透传不拼接**（两份规格都强调）。
2. **正向工具分片重组（模块D）**：OpenAI 按 `tool_calls[].index` 分片、id/name 只在首片给，要维护 `openai_index→anthropic_index` 映射（正向 §4）。反向相对简单（Anthropic 块串行，无需映射，反向 §4.3）。
3. **cooldown 并发**：ThreadingHTTPServer 多线程，CooldownStore 必须加锁；failover 循环里"排除已试 supply"要用请求内局部集合，别改全局。
4. **透传流式 vs 转换流式用不同写回函数**：组合1/2 走字节 chunked（`_write_streaming_response`），组合3/4 走逐事件（`_write_translated_stream`/`_write_responses_stream`），别混。
5. **error 按 source 协议返回**：分发层要全程记住 source，选错 error 格式会让客户端解析失败。

### 5.2 施工前需实测确认（停下问用户或先抓样本）
1. **反向下游 Anthropic 端点真实流式格式**：反向规格 §3 假设网关 Anthropic 端点吐的 SSE 与正向规格 §3.1/§3.2 一致。**开工阶段3前先抓一次真实样本核对**（事件类型、`event:`/`data:` 双行、ping 是否出现、usage 落在哪个事件）。若与假设不符，先回报。
2. **native chat 端点 `stream_options.include_usage` 是否被网关支持**（正向 §1.7）：不支持则 output_tokens 回填 0，不阻断，但需确认不报 400。
3. **model→tier 的实际映射规则**：`resolve_model_tier` 的粒度取决于实际下发的 model 名，开工阶段1时按真实 model 名定；不确定就先粗粒度（能区分 route 即可）。

### 5.3 implementer 应停下回报的点
- 抓到的反向 Anthropic 流式样本与正向规格 §3 假设**不一致**时。
- 两份规格**未覆盖**的字段/场景（如 codex 发了规格没列的 input item type、网关返回规格没描述的结构）——**不要自己发明映射**，停下问。
- 需要新增第 5 种组合（如 responses→chat）时——当前范围只做 4 组合，扩展属方案变更。

---

## 6. 给 implementer 的交接说明

1. **严禁碰 `tools/proxy.py`**（18888 生产运行）。只读它做参考；所有代码写进 `model_proxy*.py`。拷贝其机制时是**复制到新文件**，不 import。
2. **端口/配置/锁/日志全用 v2 命名**（18889 / `model_proxy_config.json` / `claude_model_proxy.lock` / `.claude_model_proxy.log`），确保与 18888 并行不冲突。
3. **协议字段映射一律查合并规格** `model_proxy_translate_spec.md`（正向见 Part 1、反向见 Part 2），本蓝图第 2 节的签名只是骨架，字段级细节以规格为准。**遇到规格没覆盖的情况，停下问，不要自创映射。**
4. **按阶段推进，每阶段先跑验证再进下一阶段**。阶段 2/3 的转换器**必须先脱网络单测全绿**（规格 §6.4 用例）再接 `_forward`。别写完一大坨再验证。
5. **阶段 2 和阶段 3 相互独立**，可并行；但都依赖阶段 1 的路由/转发/写回框架先落地。
6. **流式路径**：透传（组合1/2）用字节 chunked；转换（组合3/4）用逐事件状态机写回——两套写回函数不要混用。
7. **error 全程按 source 协议返回**（正向 §5 / 反向 §5），异常绝不让客户端挂死。
8. **开工阶段 3 前，先抓一次网关 Anthropic 端点真实流式样本**核对反向规格 §3 的假设（见 §5.2）。
9. **cooldown 是内存态、错误信号驱动**：不写盘、不轮转游标；上游 429/403/5xx → 冷却该 supply → failover 下一个。CooldownStore 记得加锁。
