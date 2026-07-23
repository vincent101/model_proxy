---
type: design-decision
date: 2026-07-22
status: implemented
target: "[[core/server.py]]"
tags: [architect, model_proxy, logging, token-usage]
---

# model_proxy 访问日志与耗时追踪

## 背景与问题

model_proxy（本地多协议代理，18889，个人使用）现有日志为 **WARNING-only**：
`core/server.py` 用 root logger（`logging.basicConfig(level=WARNING)`）把 20 余处
异常/降级路径写入 `.claude_model_proxy.log`，启动时 `_trim_log` 截断保留最后 1000 行。
另有 `MODEL_PROXY_REASONING_DEBUG=1` 开关把本模块 logger 调到 DEBUG 记 reasoning 链路。

**现状定位（判断，非一概而论）**：WARNING-only 作为「错误追踪」对个人工具是**合理且够用**
的——所有异常/降级分支（no route、unknown tier、translate failed、cooldown+failover、
stream interrupted 等）都留了痕。它的盲区只有一个：**成功请求零留痕**。HTTP 200 且未触发任何
WARNING 分支的请求，事后无法复盘「命中哪个 supply、端到端耗时多少、是否走过 failover」。
上一轮端到端测试已暴露此痛点（性能排查需外部 curl 测量）。

结论：不是「日志缺失」，是**差一层 access 日志**。补这一层即可，不需要 access log 之外的任何
重型基建（不上 ELK/Prometheus/OpenTelemetry，不引第三方库，遵守「仅标准库」约束）。

## 方案设计

务实路径。两路径无实质差异，不分裂。全部改动集中在 `core/server.py` 与 `model_proxy_cli.sh`。

### 1. 新增专用 access logger（server.py 约 54-59 行 basicConfig 之后）

不改 root logger 级别（避免误收 INFO 噪声），新建具名 logger，**共用同一 FileHandler、写同一
文件**（运维仍只看一个文件）：

```python
# 现有 basicConfig 保留不动（root 仍 WARNING）
log = logging.getLogger(__name__)

# 新增：access logger，单独 INFO，复用同一日志文件，不向 root 传播
_access_handler = logging.FileHandler(LOG_FILE)
_access_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
access_log = logging.getLogger("model_proxy.access")
access_log.setLevel(logging.INFO)
access_log.addHandler(_access_handler)
access_log.propagate = False
```

单行 key=value 纯文本（与现有 `%`-style 拼接风格一致，grep/awk 友好；不用 JSON——个人工具
grep 足够，混 JSON 反割裂现有日志）。固定前缀 `ACCESS` 便于与 WARNING 行区分：

```
2026-07-22 16:45:01,123 ACCESS ms=842 status=200 source=anthropic route=default_route tier=sonnet supply=gpt5_chat failover=0 attempts=1 usage_in=1204 usage_out=356 usage_reasoning=128 token=ab12
```

（`usage_*` 三字段为下文「§6 token 用量」新增，占位默认 0；其余字段不变。）

### 2. 耗时打点 + 字段收集（server.py，`_forward` 约 629 行起）

**粒度取舍：记一条覆盖整个请求生命周期的 access 日志**（不是每次 supply 尝试各记一条）。
理由：用户视角关心「端到端多久 / 最终谁服务 / 是否 failover」；failover 的逐次 supply+status
明细，**现有 WARNING（`cooldown+failover: supply=.. status=..`，约 893/908/920 行）已逐次记了**，
两者天然互补，无需为明细再新增日志。`_write_streaming_response` 是同步读完整个上游流才返回，
故总耗时含流式传输时间，latency 更真实。

`_forward` 有十余个分散 `return` 出口（错误早退 + 四种 mode 成功分支），无单一收口点。
**用「实例字段收集 + do_* 层 try/finally 统一 emit」模式**，不在每个 return 前散点插入：

**a) 在 do_GET/do_POST 转发分支包一层**（约 609-619 行）。把对 `_forward` 的调用改为经过
一个统一入口，例如新增 `_forward_logged`：

```python
def do_GET(self):
    if self.path.startswith(_CONTROL_PATH_PREFIX):
        self._dispatch_control("GET")
    else:
        self._forward_logged("GET")   # do_POST/PUT/DELETE/PATCH 同改

def _forward_logged(self, method):
    self._acc = {"status": 0, "source": "", "route": "", "tier": "",
                 "supply": "", "failover": 0, "attempts": 0, "token": "",
                 "usage_in": 0, "usage_out": 0, "usage_reasoning": 0}
    t0 = time.monotonic()
    try:
        self._forward(method)
    finally:
        a = self._acc
        access_log.info(
            "ACCESS ms=%d status=%s source=%s route=%s tier=%s supply=%s "
            "failover=%s attempts=%s usage_in=%s usage_out=%s usage_reasoning=%s token=%s",
            int((time.monotonic() - t0) * 1000), a["status"], a["source"],
            a["route"], a["tier"], a["supply"], a["failover"], a["attempts"],
            a["usage_in"], a["usage_out"], a["usage_reasoning"], a["token"])
```
（`time` 模块已 import，约 19 行。usage 三字段的填充见 §6。）

**b) `_forward` 内在已有位置顺手填 `self._acc`**（不新增逻辑分支，只赋值）：
- 约 641 行拿到 token 后：`self._acc["token"] = token[-4:] if token else ""`
- 约 653 行 source 识别后：`self._acc["source"] = source`
- 约 659 行 route、667 行 tier 解析后：`self._acc["route"] = route.get("id")` / `["tier"] = tier`
  （route 为 None 的早退分支 route 保持空，可接受）
- while 循环内选中 supply 后（约 719 行）：`self._acc["supply"] = supply_id`；
  每进一次循环体 `self._acc["attempts"] += 1`
- 每次 failover `continue` 前（约 895/910/922 行，已有 WARNING 处）：`self._acc["failover"] = 1`

**c) status 收集**：四个 `_write_*` 写回函数入口各加一行
`if hasattr(self, "_acc"): self._acc["status"] = status`
（`_write_buffered_response` 约 1121、`_write_streaming_response` 约 1096，两个转换流式写回
`_write_translated_stream` / `_write_responses_stream` / `_write_translated_stream_from_responses`
入口同加；转换流式无显式 status 参数的，固定填 200，因为它们只在成功路径调用）。
`hasattr` 守卫是因为 `_dispatch_control` 路径不设 `_acc`，控制端点不记 access（见下）。

### 3. 控制端点（/status、/reload）

**不记 access 日志**。它们是低频运维动作、无 supply/tier 语义，记了只是噪声。维持现状。

### 4. `_trim_log` keep 1000 → 5000（server.py 约 41 行默认参数 + 53 行调用）

access 行短（约 150 字节，含 usage 三字段后略增），5000 行约 0.7MB，可接受，覆盖窗口从
「约 1 天」拉到「约一周」量级。**不引入滚动**（RotatingFileHandler）：进程长跑期间仍只在启动时
截断——个人工具重启频繁，启动截断已够，不值得为长跑无限增长上滚动机制的复杂度。README 第 282
行「保留最后 1000 行」同步改 5000。

### 5. cli 加 `logs` / `stats` 子命令（model_proxy_cli.sh，约 367 行 case 分支 + 17 行 print_help）

纯 bash+awk，无第三方依赖，兑现「耗时可追溯」痛点的直接入口：

- `logs [N]`：`grep ' ACCESS ' "$LOG_FILE" | tail -n "${2:-30}"`（默认最近 30 条 access 行）
- `stats`：awk 聚合最近全部 ACCESS 行——count、avg ms、max ms、按 supply 分组的
  请求数与非-200 占比（成功率），**外加按 supply/model 分组的 usage_in/usage_out/usage_reasoning
  累加总量**（见 §6.4）。约 20 行 awk，解析 `ms=` `status=` `supply=` `usage_in=` 等字段即可。

优先级：先落 1-4（access 日志本体），5 为锦上添花但推荐做——否则用户得自己记 awk 命令，
体验差；做了它，「最近 N 条平均耗时 / 按 supply 成功率 / 累计 token」一条命令出。

### 6. token 用量统计（本轮追问新增，复用 §1-5 的 `_acc` + 同一条日志）

**核心判断：token 用量不另起机制，塞进已设计的 ACCESS 那一行**（新增 `usage_in`/`usage_out`/
`usage_reasoning` 三字段）。理由：这是个人工具、用量不大、无实时配额需求；`_acc` 字典 + do_* 层
统一 emit 的骨架已建好，加三个 key 即可，零额外基建。跨请求累计交给 §5 的 `stats` awk 聚合，
**不建独立 usage 文件/DB**——日志本身就是数据源，grep/awk 足够。

#### 6.1 可行性分层结论（按 mode × 流式 分别定）

数据来源已核实：各协议响应体本身都带 usage，转换路径已解析出来但用完即丢；PASSTHROUGH 完全不解析。

| mode / 场景 | 能否拿到 | 代价 | 决策 |
|---|---|---|---|
| 转换非流式（ANTHROPIC_TO_CHAT / ANTHROPIC_TO_RESPONSES / RESPONSES_TO_ANTHROPIC） | 能，精确 | 近零：`json.loads` 已做完，最终 body dict（`anthropic_resp`/`responses_resp`）里就含 usage，只多一行提取 | **做** |
| 转换流式（同上三种） | 能，精确 | 近零：adapter 实例流程中已累加 usage，写回函数结束后实例仍在，多读一次字段 | **做** |
| PASSTHROUGH 非流式 | 需额外 `json.loads(resp_body)` | 单次解析已 read 完的 body（chat/messages 响应通常几 KB~几十 KB），微秒~百微秒级，对本地个人代理可忽略 | **做**（值得，见 6.3） |
| PASSTHROUGH 流式 | 需额外逐事件解析 SSE | 高：现在是纯字节 8192 转发不碰内容，要抓 usage 得逐 chunk 拆 SSE、找末尾 usage 事件，破坏「零解析透传」特性且逻辑复杂 | **不做**（该行 usage_* 记 0，见 6.5） |

**明确回答用户「能不能统计到 token 用量」**：能，且绝大多数场景近零成本。唯一放弃的是
PASSTHROUGH 流式（anthropic→anthropic、responses→responses 的流式请求）——成本不对等，
不为「全覆盖」在低价值场景强行拆流。这类请求 access 行照样记（ms/status/supply 齐全），仅
usage_* 为 0，`stats` 聚合时它们不贡献 token 数（会略偏低，可接受，见风险）。

#### 6.2 转换模式的提取点（精确到函数与变量）

统一约定：新增一个小 helper 在 `_forward_logged` 无关处，直接在各写回调用点旁赋值 `self._acc`。
字段命名注意——**adapter 的 usage 属性名不统一**（已核实），提取时按 adapter 类型取对应属性：

**非流式**（server.py 约 941-1020 行三个转换分支，`_write_buffered_response(200, ...)` 之前）：
- ANTHROPIC_TO_CHAT（约 961 行前）：`anthropic_resp["usage"]` 里取
  `input_tokens`/`output_tokens`（`translate.py` 约 528-531 `openai_to_anthropic_response` 产出，
  无 reasoning 明细，`usage_reasoning` 记 0）。
- ANTHROPIC_TO_RESPONSES（约 987 行前）：`anthropic_resp["usage"]` 同上（输出侧是 Anthropic）。
- RESPONSES_TO_ANTHROPIC（约 1017 行前）：`responses_resp["usage"]`（`translate.py` 约 1020-1026
  `_anthropic_usage_to_responses` 产出）取 `input_tokens`/`output_tokens`，reasoning 从
  `usage["output_tokens_details"]["reasoning_tokens"]` 取。

  统一写法示例（放在对应 `_write_buffered_response(200,...)` 前一行）：
  ```python
  _u = anthropic_resp.get("usage") or {}   # RESPONSES_TO_ANTHROPIC 用 responses_resp
  self._acc["usage_in"] = _u.get("input_tokens", 0)
  self._acc["usage_out"] = _u.get("output_tokens", 0)
  self._acc["usage_reasoning"] = (_u.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
  ```

**流式**（adapter 处理完后，即三个转换流式写回函数返回后，在 server.py 约 944-945、969-970、
996-999 行 `self._write_*` 调用**之后**读 adapter 实例）：
- `OpenAIToAnthropicStreamAdapter`（ANTHROPIC_TO_CHAT）：属性 `adapter.input_tokens` /
  `adapter.output_tokens`，无 reasoning 累加器 → `usage_reasoning=0`。
- `AnthropicToResponsesStreamAdapter`（ANTHROPIC_TO_RESPONSES）：属性 `adapter.usage_in` /
  `adapter.usage_out` / `adapter.usage_reasoning`。
- `AnthropicToResponsesStreamAdapter` 的反向即 RESPONSES_TO_ANTHROPIC 用的
  `_write_translated_stream_from_responses`（其 adapter 是上游 Responses→Anthropic 的那个，
  约 1750-1751 行 `self.input_tokens`/`self.output_tokens`，无 reasoning 累加器 → 0）。

  因属性名不统一，建议在 adapter 上加一个统一方法 `usage_tuple()`（返回
  `(in, out, reasoning)`）三处 adapter 类各实现一版，`_forward` 里统一调用——**这属 translate.py
  的小改动，避免 server.py 里散落 hasattr 判类型**。若不想动 translate.py，则在 server.py 用
  `getattr(adapter, "usage_in", None) or getattr(adapter, "input_tokens", 0)` 兜底取值，二选一，
  推荐前者（干净、可测）。示例（各写回调用后）：
  ```python
  self._acc["usage_in"], self._acc["usage_out"], self._acc["usage_reasoning"] = adapter.usage_tuple()
  ```

#### 6.3 PASSTHROUGH 非流式的提取

server.py 约 934-938 行，现为 `resp_body = resp.read()` 后直接 buffered 写回。加一段防御式解析：
```python
resp_body = resp.read()
try:
    _pu = (json.loads(resp_body) or {}).get("usage") or {}
    # anthropic 侧: input_tokens/output_tokens; chat/openai 侧: prompt_tokens/completion_tokens
    self._acc["usage_in"] = _pu.get("input_tokens", _pu.get("prompt_tokens", 0)) or 0
    self._acc["usage_out"] = _pu.get("output_tokens", _pu.get("completion_tokens", 0)) or 0
    self._acc["usage_reasoning"] = (_pu.get("output_tokens_details") or {}).get("reasoning_tokens", 0) or 0
except Exception:
    pass   # 解析失败不影响透传主流程，usage 记 0
self._write_buffered_response(resp_status, list(resp.getheaders()), resp_body)
```
解析对象是已在内存的 body、且包在 try 里绝不影响透传正确性；对本地代理延迟影响可忽略。**做**。

#### 6.4 累计统计：交给 `stats` awk，不建独立文件

日志行已含 `usage_in/out/reasoning` + `supply`，`stats` 子命令 awk 顺带累加即可输出
「按 supply 分组的累计 in/out/reasoning token」。**与 §5 的 stats 合并成同一个命令**，不重复建设，
不引入 JSON/CSV 累计文件（个人工具、日志即数据源，重启截断丢历史可接受；真要长期留存，后续
再另议归档，不在本轮范围）。

> **后续追加（2026-07-23）**：用户明确提出「日志截断会丢失长期统计」的痛点，本节「不建独立累计
> 文件」的结论已被专门方案 [[2026-07-23-usage-totals-ledger]] 补足——新增一个按月分桶、只增不截、
> 独立于日志截断与进程重启的累计账本 JSON + `totals` 命令。`stats`（短期诊断、含 max ms、受日志
> 窗口限制）与 `totals`（长期账本、永不丢）并存分工。详见该文档。

#### 6.5 成本折算（token × 单价）：不做

**明确判断：不做成本折算。** 理由：不同上游（各 supply 对应不同网关/模型）计价规则差异大、
价格会变、还分输入/输出/缓存/reasoning 多档，维护一张价格表的成本远高于「个人看个大概 token 量」
的收益，且极易过期误导。只统计 token 数，不折算金额。若用户日后明确要，再在 config 给 supply
加可选 `price_in`/`price_out` 字段，stats 里做一次乘法——但现在不预埋。

## 7. PASSTHROUGH 流式 usage 补做（修订 §6.1 表末行「不做」的决策）

**本节修订 §6.1 里「PASSTHROUGH 流式 → 不做（记 0）」这一格。** 用户明确要求这个场景也统计到
token。重新核实代码后结论改为：**做**。前序判「不做」的理由（「要逐 chunk 拆 SSE、破坏零解析
透传、成本不对等」）在「先转发后嗅探 + 字节预筛 + try 隔离 + 复用已验证的切块 helper」四个前提下
不再成立——透传行为一字节不改，嗅探是纯旁路，CPU/内存开销可忽略，跨 chunk 边界由 `\n\n` 切块
天然规避。§6.1 表末行、§6「明确推荐」里「PASSTHROUGH 流式放弃」、风险段「PASSTHROUGH 流式 usage
记 0」及验证段第 7 条对应项，均以本节为准。

### 7.1 方案选型：选项 A（边转发边嗅探），否决选项 B

- **选项 A（采纳）**：`_write_streaming_response` 的 `resp.read(8192)` 循环里，每个 chunk
  **先无条件原样转发**（原有 chunked 写出逻辑一字不改），转发之后再把该 chunk 追加进一个嗅探
  buffer，用 `\n\n` 切出完整 SSE 事件块，对块提取 usage。转发在前、嗅探在后且整段包 `try`，保证
  嗅探任何异常都不影响透传正确性与实时性（客户端拿到的字节流与现在完全一致，不多等一个 chunk）。
- **选项 B（否决）**：流结束后一次性解析。PASSTHROUGH 是边读边转发，「流结束后」要拿到内容只能
  额外把**全量字节**留在内存（长回答几十 KB~数 MB），比 A 的「只留当前未凑齐的残余块」内存差得多，
  且拿到的 usage 与 A 完全一样、并不更简单。滑动窗口变体（只留末尾 N KB）引入「N 取多大」的脆弱
  假设（trailing delta 多时会把 usage 挤出窗口），不如 A 的 `\n\n` 切块稳。**否决 B。**

### 7.2 三重风险的化解（性能 / 内存 / 正确性）

- **转发延迟 = 0**：先写后嗅探，嗅探不阻塞、不改变写出时序，客户端感知不到任何卡顿。绝不为「凑齐
  一个完整 SSE 事件」而推迟转发——转发的是原始 chunk 字节，与是否凑齐事件无关。
- **内存 O(单个最大事件)**：切块用 `block, buf = buf.split(b"\n\n", 1)`，已切出的完整块**立即丢弃**
  （只取其 usage，不留内容），buffer 始终只含「当前尚未凑齐 `\n\n` 的那一小段残余」。usage 事件本身
  仅几十字节，单个 delta 事件几百字节，不会无限增长。
- **CPU：字节预筛避免无谓 json.loads**。anthropic 流有成百上千个 `content_block_delta` 块，若每块
  都 `json.loads` 纯为找 usage 是浪费。**在 `json.loads` 前先做字节级子串预筛**：块里不含
  `b"message_delta"`（anthropic）/ `b"response.completed"`（responses）就直接跳过
  （`bytes.__contains__` 是 Boyer-Moore，极快）。绝大多数 delta 块预筛即弃，只有极少数目标块才解析。
- **跨 chunk 边界不丢不误判**：usage 事件被切成两半跨越两个 chunk，靠「buffer 累积 + 只在切出完整
  `\n\n` 块时才解析」根治——这正是 `_write_responses_stream` / `_write_translated_stream_from_responses`
  已在用、已验证的模式，直接沿用。

### 7.3 两协议 usage 出现规律（已读 translate.py 三个 adapter 核实，非猜测）

| 协议 | usage 所在事件 | 字段路径 | 出现次数 / 取值策略 |
|---|---|---|---|
| anthropic SSE（anthropic→anthropic） | `message_delta` | `data["usage"]` 里 `output_tokens` / `input_tokens` | 携最终 usage 的 `message_delta` 在流尾 `message_stop` 前；`message_start.usage` 为空占位。**覆盖式取最后一个**（同 `AnthropicToResponsesStreamAdapter.feed` 的 `message_delta` 分支，translate.py 约 1280-1289）。anthropic 流式 `message_delta.usage` 通常不含 reasoning 明细 → `usage_reasoning` 多为 0（有值则带出，无则 0，不臆造） |
| responses SSE（responses→responses） | `response.completed` | `data["response"]["usage"]` 里 `input_tokens` / `output_tokens` / `output_tokens_details.reasoning_tokens` | 流尾出现一次，**覆盖式取最后一个**（同 `ResponsesToAnthropicStreamAdapter.feed` 的 `response.completed` 分支，translate.py 约 1907-1913）。usage 结构更全，reasoning 可直接取到 |

**是否 early-stop**：**不设**。字节预筛已把非目标块降到近零成本，再引入「拿到就停」状态反而对
anthropic 有取到中间 `message_delta` 值的风险（若上游发多个）。统一「每个目标块覆盖式更新，取最后
一次」，最稳，CPU 也够低。

### 7.4 复用边界：切块 helper 复用，usage 提取用轻量专用逻辑

- **复用**：`_parse_anthropic_sse_block`（静态方法，约 1411 行）——它对 anthropic（`event:`+`data:`）
  和 responses（单行 `data:`，靠 event 缺失时 `data.type` 兜底）两种块都能解析，直接复用。`\n\n` 切块
  的 `buf += data; while b"\n\n" in buf` 模式照抄现有转换流式写回。
- **不复用 adapter**：translate.py 的三个 adapter 是「协议转换」用的重状态机（要 `feed` 逐事件驱动、
  维护 content block / item 序列），PASSTHROUGH 只需「认出目标事件 + 取 usage 字段」，硬套 adapter 得
  实例化整个状态机、破坏透传轻量性，得不偿失。故 usage 提取写一个 server.py 内的**轻量专用 helper**
  （约 15 行），按 source 分支取字段。**不提到 translate.py。**

### 7.5 具体实施位置（precise to function/line）

**a) 改 `_write_streaming_response`（server.py 约 1175-1200）**，新增 `source` 参数并在循环内嗅探：

```python
def _write_streaming_response(self, status, headers, resp, source="") -> None:
    if hasattr(self, "_acc"):
        self._acc["status"] = status
    self.send_response(status)
    for hname, hval in headers:
        if hname.lower() in self._SKIP_RESP_HEADERS:
            continue
        self.send_header(hname, hval)
    self.send_header("Transfer-Encoding", "chunked")
    self.end_headers()
    sniff_buf = b""                      # 新增：usage 嗅探 buffer
    try:
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            # —— 转发在前、无条件（行为一字未改）——
            self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
            # —— 嗅探在后、纯旁路，异常绝不影响转发 ——
            try:
                sniff_buf += chunk
                while b"\n\n" in sniff_buf:
                    block, sniff_buf = sniff_buf.split(b"\n\n", 1)
                    self._sniff_passthrough_usage(block, source)
            except Exception:
                pass
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        resp.close()
```

（残余块处理：usage 事件后上游通常还有 `message_stop`/结尾空行，`\n\n` 会切出目标块，末尾残余仅是
最后一个不完整片段、不含 usage。为防上游尾部无空行，建议 break 后对 `sniff_buf.strip()` 再补一次
`_sniff_passthrough_usage`（一行，helper 幂等，加不加都不影响正确性）。)

**b) 新增轻量 helper（server.py，放在 `_parse_anthropic_sse_block` 附近，约 1411 行前）**：

```python
def _sniff_passthrough_usage(self, block: bytes, source: str) -> None:
    """PASSTHROUGH 流式旁路：从一个完整 SSE 块里嗅探 usage，覆盖式写入 self._acc。
    字节预筛跳过绝大多数无关块，只对目标块做 json 解析。异常由调用方 try 兜住。
    """
    if source == "anthropic":
        if b"message_delta" not in block:          # 字节预筛
            return
        ev_type, data = self._parse_anthropic_sse_block(block)
        if ev_type != "message_delta" or not isinstance(data, dict):
            return
        u = data.get("usage") or {}
        if u.get("output_tokens") is not None:
            self._acc["usage_out"] = u.get("output_tokens") or 0
        if u.get("input_tokens") is not None:
            self._acc["usage_in"] = u.get("input_tokens") or 0
        r = pt._extract_reasoning_tokens(u)         # 复用 translate.py，兼容 thinking_tokens 别名
        if r:
            self._acc["usage_reasoning"] = r
    elif source == "responses":
        if b"response.completed" not in block:      # 字节预筛
            return
        ev_type, data = self._parse_anthropic_sse_block(block)
        if ev_type != "response.completed" or not isinstance(data, dict):
            return
        u = (data.get("response") or {}).get("usage") or {}
        self._acc["usage_in"] = u.get("input_tokens", 0) or 0
        self._acc["usage_out"] = u.get("output_tokens", 0) or 0
        self._acc["usage_reasoning"] = pt._extract_reasoning_tokens(u)
```

（reasoning 统一用 `pt._extract_reasoning_tokens(u)`（translate.py 约 1002 行已有），它读
`output_tokens_details.reasoning_tokens` 并兼容 `thinking_tokens` 别名，比手写取字段更稳。)

**c) 改调用点（server.py 约 977 行，PASSTHROUGH `is_stream` 分支）**，把 `source` 传进去：

```python
if is_stream:
    self._write_streaming_response(
        resp_status, list(resp.getheaders()), resp, source)
```

`source` 在 `_forward` 作用域内已有（约 690 行 `source = detect_source(...)`）。

**与 `_acc` 对接**：helper 直接写 `self._acc["usage_in"/"usage_out"/"usage_reasoning"]`，
`_forward_logged` 的 `finally` 照原样 emit，无需改 emit 逻辑。默认值 0（`_forward_logged` 初始化）
保证嗅探不到时（如断流未到 usage 事件）落回 0，与转换流式断流行为一致。

### 明确推荐

- access 日志：**做**，专用 INFO logger + 同文件 + key=value 单行 + 前缀 ACCESS。
- 耗时粒度：**整请求一条**，failover 明细复用现有 WARNING。
- 文件：**共用一个**，不分离。
- `_trim_log`：**提到 5000**，不上滚动。
- cli `logs`/`stats`：**做**（第二优先级），stats 同时出耗时/成功率/累计 token。
- request_id：**不引入**（见风险）。
- **token 用量：做，塞进同一条 ACCESS 行的 `usage_*` 三字段**，复用 `_acc`；转换模式（流式+非流式）
  与 PASSTHROUGH 非流式都提取；**PASSTHROUGH 流式改为「做」（本轮修订，见 §7），旁路嗅探 SSE
  usage 事件，透传行为不变。**
- **不做成本折算**，只统计 token 数。
- 流式 adapter 建议加统一 `usage_tuple()` 方法（translate.py 小改），避免 server.py 里判类型。

## 风险与权衡

- **request_id 不引入**：本可给每请求生成短 rid 串联 access 与 WARNING 行，但个人工具并发极低，
  靠时间戳 + token_tail 已能人工对齐两类日志；引入 rid 需改动 20 余处 WARNING 调用点（否则只
  access 带 rid、无法串联，等于白引），务实路径下收益 < 改动成本。若日后并发上升再补。
- **成功/失败共用 status 字段判定成功率**：`stats` 用 `status==200` 近似成功；转换流式响应固定
  填 200，若流式中途 upstream 断（`stream interrupted` WARNING 分支）status 仍显示 200——
  成功率会偏乐观。可接受：真出流式中断，WARNING 已记，交叉看即可。
- **PASSTHROUGH 流式 usage（§7 已改为做）**：改用旁路嗅探后不再记 0。残留风险仅两点：①上游若在
  `message_delta`/`response.completed` 之前就断流，嗅探不到 usage → 落回默认 0（与转换流式断流的
  部分值行为一致，可接受）；②嗅探解析被 `try` 全兜，任何异常只导致该请求 usage 记 0，绝不影响透传
  字节正确性。
- **流式中途断流时 adapter usage 可能不完整**：断流走 finalize，usage 累加器可能只累到断点，
  读到的是部分值——与 access 行 status（仍 200）一致地偏乐观，同上可接受。
- **adapter usage 属性名不统一**（`input_tokens`/`output_tokens` vs `usage_in`/`usage_out`/
  `usage_reasoning`，已核实）：若不加统一 `usage_tuple()`，server.py 提取处需按 adapter 类型
  分别取属性，易漏易错。推荐加 `usage_tuple()`（translate.py 三个 adapter 各实现），server.py
  单点调用——多改 translate.py 三处小方法，换 server.py 干净可测。
- **`_acc` 实例字段 + ThreadingHTTPServer**：每个请求是独立 handler 实例，`self._acc` 天然隔离，
  无线程安全问题。access_log 的 FileHandler 自带锁，多线程写同文件安全。
- **两个 FileHandler 指向同一文件**：Python logging 各 handler 各自持有文件句柄、各自加锁，
  同进程内交叉写同一文件是安全的（不会互相截断）；只是 `_trim_log` 在启动时（handler 创建前）
  执行，不受影响。
- 迁移/落地代价：改动集中在单文件顺手赋值 + 一个 wrapper + cli 两个 bash 子命令 + translate.py
  三个 adapter 各加一个 `usage_tuple()`，无数据结构变更、无配置变更、无第三方依赖，向后兼容
  （现有 WARNING 行为不变，现有响应 body 不变）。

## 验证方式

1. 启动代理，发一个正常请求（curl `/v1/messages` 命中某 supply），确认
   `.claude_model_proxy.log` 新增一条 `ACCESS ms=.. status=200 supply=..` 且 ms 与外部
   `curl -w %{time_total}` 量级一致。
2. 构造 failover：把首选 supply appkey 改错触发 5xx，确认 access 行 `failover=1 attempts>=2`
   且 supply 为最终命中者，同时旧 WARNING `cooldown+failover` 逐次明细仍在。
3. 错误早退：发无效 token，确认 access 行 `status=401`（route/tier 为空可接受）。
4. 控制端点：`model_proxy_cli.sh status`，确认**不**产生 ACCESS 行。
5. `model_proxy_cli.sh logs 5` 输出最近 5 条 access；`stats` 输出 count/avg ms/按 supply 成功率
   /按 supply 累计 token。
6. 重启进程，确认 `_trim_log` 按 5000 行截断（造 >5000 行日志后重启核对行数）。
7. **token 用量校验**：
   - 转换非流式（如客户端 anthropic → 上游 chat）：发一条已知长度的请求，确认 access 行
     `usage_in`/`usage_out` 与上游响应体 usage 一致（可对照客户端收到的响应 usage）。
   - 转换流式：同上，确认 access 行 usage 与流式收尾 `message_delta.usage` 一致。
   - PASSTHROUGH 非流式（anthropic→anthropic）：确认 usage 从透传 body 正确解出、非 0。
   - PASSTHROUGH 流式（§7 新增，anthropic→anthropic）：发一条流式请求，确认 access 行
     `usage_in`/`usage_out` 非 0 且与末尾 `message_delta.usage` 一致；再用 responses→responses
     流式确认 usage 从 `response.completed` 解出。**关键：逐字节比对客户端收到的 SSE 流与不开嗅探时
     完全一致（透传未被破坏）**——可对同一请求分别抓包比对，或校验客户端能正常解析完整响应。
   - PASSTHROUGH 流式断流：中途 kill 上游，确认 access 行 `usage_*=0`（未到 usage 事件），透传已发
     部分字节不受影响。
   - reasoning：发一个带 thinking/reasoning 的请求（RESPONSES_TO_ANTHROPIC），确认
     `usage_reasoning>0`。
8. 现有单测（tests/）全绿，确认改动未破坏转发/转换逻辑；若加 `usage_tuple()`，补三个 adapter
   的 usage 读取单测。

## 关联

- [[core/server.py]] `_forward` / `_forward_logged` / `do_GET` / `_write_*` / `_trim_log` /
  PASSTHROUGH 非流式分支（约 980-996）/ 三个转换分支（约 999-1062）/ `_write_streaming_response`
  （约 1175）/ 新增 `_sniff_passthrough_usage`（§7）/ `_parse_anthropic_sse_block`（约 1411）/
  PASSTHROUGH is_stream 调用点（约 977）
- [[core/translate.py]] `openai_to_anthropic_response`（约 520-531）/ `_anthropic_usage_to_responses`
  （约 1013-1026）/ `_extract_reasoning_tokens`（约 994-1007）/ 三个流式 adapter usage 累加器
  （`OpenAIToAnthropicStreamAdapter` 约 580/667、`AnthropicToResponsesStreamAdapter` 约 1145-1147/
  1272-1281、反向 adapter 约 1750-1751）
- [[model_proxy_cli.sh]] case 分支 / print_help
- 后续设计：[[2026-07-23-usage-totals-ledger]]（独立累计账本，补足 §6.4「不建独立累计文件」的痛点）
- 前序设计：[[2026-07-22-install-manage-sessionstart-hook]]
