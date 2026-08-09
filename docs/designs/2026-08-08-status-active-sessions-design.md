---
type: design-decision
status: done
target: "[[tools/model_proxy]]"
tags:
  - architect
  - model_proxy
  - cli
  - status
modified: 2026-08-08 23:00:00
created: 2026-08-08 23:00:00
---

# status 新增"活跃 session 链路健康"展示设计

> **注**：已落地（2026-08-08，含用户拍板的链路分布视图格式）。

> [务实] 路径。接续 [[2026-08-08-status-p0-implementation-plan]]（已实施），在 status 健康仪表盘加一段：
> **当前活跃 session（30min 内有请求）各自的链路是否正常**。
> 核实基准：2026-08-08 22:40 master 工作区，代理在跑（port 18889）；日志 5090 行 / 4.4h（≈1150 行/h），ACCESS 1090 行。

## 1. 背景与问题

status P0 已有进程行/health 行/异常清单/config 计数行，回答"系统级"健康。用户还要"session 级"视角：每个活跃会话当前走的链是否正常。数据全部在 ACCESS 日志行里，**CLI 端解析日志即可，零 server 改动**（与 P0 同思路）。

## 2. 核实结果（以代码为准）

| # | 核实项 | 结论 |
|---|---|---|
| 1 | ACCESS 行格式 | `server.py:1125-1135`，`_forward_logged` finally 统一 emit：`ACCESS ms= status= source= route= tier= supply= failover= attempts= usage_in= usage_out= token= session= route_failover= builtin= budget_retried= budget_truncated= stop_reason= final_error=`，行前缀 `YYYY-MM-DD HH:MM:SS,mmm req_id=<id>`（naive 本地时区，logging asctime 默认）。**strategy 字段收集了但未输出**，session 行无法展示 strategy，不需要 |
| 2 | session 提取 | `extract_session_key`（`server.py:643-666`）：`metadata.user_id` 是 JSON 字符串，取 `session_id`。非 CC 客户端（codex 等）无此字段 → `session=` 空串。实测当前日志：3 个 uuid session + 33 行空 session + 1 行怪异值 `session=s`（按字面 id 处理即可） |
| 3 | `_trim_log` | `server.py:54-66`，**仅启动时**截断到末 5000 行，运行期不滚动、只增。CLI 启动也是 `nohup >> "$LOG_FILE"`（`model_proxy_cli.sh:334`），非 ACCESS 行（WARNING/nohup 输出）交织其中，解析须容忍 |
| 4 | 30min 窗口 vs 5000 行 | 实测 1150 行/h ⇒ 30min ≈ 575 行，5000 行 ≈ 4.3h。窗口覆盖率有 8 倍余量；WARNING 风暴极端场景用截断检测+提示兜底（§3.2） |
| 5 | `_handle_status` | `server.py:1954-1984`，返回 supplies/routes/strategies/cooldown/default_cooldown_seconds，无请求历史。server 若提供 session 数据需新增内存态，见 §3.5 可选档 |
| 6 | builtin 请求 | `$route` 内建命令：`builtin=route`、`supply=(builtin)`（`server.py:1818-1819`），不产生上游调用。门控要求 source=anthropic + session 非空，故 builtin 行必带 session |
| 7 | 解析陷阱 | `route_failover=` 含子串 `failover=`——必须按空格分词后精确匹配 key，不能子串 grep（实测当前日志 failover=1 仅 3 行，子串法会虚增一倍） |
| 8 | 字段实测分布 | status：200×1058、501×30、401×2、502×1；status=0 未出现（`_acc["status"]` 初值 0，请求中途崩才可能出现，判定规则按 `!="200"` 即 fail 覆盖）；final_error 如 `unsupported_source=chat_target=anthropic`；route_failover 全 0；builtin 全空 |
| 9 | CLI 挂点 | `LOG_FILE` 定义于 `model_proxy_cli.sh:11`；`cmd_status` 在线路径 :124-173，`_format_ops.py status-format` 调用在 :172 |

## 3. 方案设计

### 3.1 状态判定规则（每 session，30min 窗口内请求）

窗口内 ACCESS 行按 session 分组聚合（builtin=route 行**计入活跃、不计入统计**）：

- `n`（非 builtin 请求数）、`fail`（status≠"200" 的行数）、`fo`（failover+route_failover 求和）
- 最近一次非 builtin 请求：ts/status/route/tier/supply/final_error/req_id
- 状态三档：
  - **FAIL**：最近一次 status≠200（链路当前断）
  - **warn**：最近 200，但窗口内有 fail 或 fo>0（断过已恢复/靠 failover 扛住）
  - **ok**：全 200 且无 failover
- 仅 builtin 请求的 session：`n=0`，状态 ok，行尾注 `（仅 $route)`

### 3.2 解析方式（CLI 端，`_format_ops.py`）

新增函数（全部 stdlib）：

| 函数 | 职责 |
|---|---|
| `parse_access_line(line) -> dict \| None` | ts（前 23 字符 `strptime "%Y-%m-%d %H:%M:%S,%f"`）+ 前缀 req_id + `" ACCESS "` 后按空格分词、`split("=",1)` 精确 key 匹配。任一步失败返回 None（容忍截尾半行/stacktrace/nohup 行） |
| `load_active_sessions(log_path, *, now=None, window_minutes=30, tail_bytes=2*1024*1024) -> dict` | tail 读文件末 `tail_bytes`（二进制 seek，丢弃首条残行）→ 逐行 parse → `ts >= now-30min` 过滤 → 按 session 分组聚合 §3.1 字段。返回 `{"sessions": {...}, "truncated": bool, "log_missing": bool}` |
| `_format_active_sessions(result) -> list[str]` | 渲染 §3.3 布局 |
| `_format_status_from_json` | 签名加 `log_path`，health 行后插入 session 段 |

**时区口径**：日志 asctime 是 naive 本地时间，比较用 `datetime.now()`（naive 本地），自洽；与账本 `_cst_now` 无关（那是天桶口径）。

**截断检测**：文件 append-only、ts 单调 ⇒ 截断可能当且仅当 `文件大小 > tail_bytes 且 buffer 内首条可解析行 ts 已在窗口内`（说明窗口起点可能在 buffer 之外）。此时 header 加 `（窗口数据可能被截断）`。tail_bytes=2MB ≈ 8h+ 余量，正常不会触发。

**解析成本**：2MB / 万行级 regex，<50ms，相对 fork python3 本身可忽略。

### 3.3 展示形态（基于 2026-08-08 22:41 真实日志渲染）

位置：health 行之后、degraded 段之前。**在线时恒展示**（用户明确要看活跃 session 列表，不适用"只列问题"原则）：

```
health: cooldown 0/25 · degraded 0 · overrides 1

active sessions (30min): 3  (1 FAIL · 2 ok)
  (none)    FAIL n=2 fail=2  22:20 501  nation1/opus/kimi-k3-sankuai-3339
            err: unsupported_source=chat_target=anthropic  req=18eeccfa
  2896beec  ok   n=87 fail=0  22:41 200  nation1/opus/kimi-k3-sankuai-3339
  4eb3be5f  ok   n=37 fail=0  22:21 200  nation2/opus/kimi-k3-sankuai-2330
```

含 warn 的合成样例：

```
  1a09afa5  warn n=15 fail=2 fo=1  22:31 200  nation2/sonnet/glm-52-sankuai-3339
```

行格式（全部 ≤80 列，已逐行验证宽度）：

```
  {id:<8}  {state:<4} n={n} fail={f}[ fo={fo}]  {HH:MM} {status}  {route}/{tier}/{supply}
```

- **id**：session uuid 取前 8 位（仿 req_id 短 id 惯例）；空串 session 聚合为一桶，显示 `(none)`（与账本 supply=(none) 口径一致）
- **fo**：仅 >0 时显示
- **err 续行**：仅 FAIL 行，缩进对齐，`final_error` + `req=短id`（供 `logs req=xxx` 追查）
- **排序**：FAIL → warn → ok，同档按最近请求时间倒序（异常优先，与仪表盘原则一致）
- **header**：`active sessions (30min): N  (a ok · b warn · c FAIL)`，零计数档省略；截断时追加提示
- **零活跃**：`active sessions (30min): 无活跃请求`（一行，回答"现在没有链路在用"）
- **日志缺失**：`active sessions (30min): 无数据（日志文件缺失）`
- 上限 20 行，超出 `  ... 另有 N 个 session`
- 信息密度取舍：末次延迟 `ms` 不上主行（加上即超 80 列）；链路判定以 status/fail/failover 为准，延迟深挖走 `logs`

### 3.4 CLI 改动（`model_proxy_cli.sh`）

仅在线路径 :172 调用加第三参：

```bash
echo "$out" | python3 "$SCRIPT_DIR/_format_ops.py" status-format "$CONFIG_FILE" "$TOTALS_FILE" "$LOG_FILE"
```

`main()` 的 status-format 同步收第 4 个参数（与 CLI 一起改，不留兼容分支）。**离线路径不动、不展示 session 段**——代理未运行无活跃链路，读历史日志会把死会话误显为活跃（与 P0 "停机不读账本" 同理）。help 文案 :22 附近补一句段说明。

### 3.5 server 端可选档（不推荐现在做）

做法：server 内存 deque（maxlen≈2000）在 `_forward_logged` finally 里追加 `(ts, session, status, route, tier, supply, failover, final_error, req_id)`，`_handle_status` 聚合 30min 窗口随 JSON 返回。约 40-60 行。

对比 CLI 档：唯一增益是窗口不受日志截断影响、免解析；代价是要**重启代理生效**（打断在跑的会话）、新增内存态。当前日志截断余量 8 倍（§2 表 #4），CLI 档完全够用。**结论：CLI 档落地；server 档仅在未来日志截断/解析出问题再升级。**

## 4. 风险与权衡

1. **(none) 桶可能常驻 FAIL**：codex 等 chat 协议客户端打到 anthropic-only route 产生 501（实测 30min 内 2 条）。显示是对的——它是真实失败流量；err 行已给原因，用户自判。不接进 health 行计数（那是 supply 级口径）。
2. **短 id 碰撞**：uuid 前 8 位理论可撞，纯展示用途不处理；真撞了 `logs` 全量可查。
3. **日志格式漂移**：ACCESS 格式若改（加字段无害，改/删字段有影响）需同步 parser；tests 有 fixture 兜底。
4. **窗口边界请求**：session 最近一次请求恰在 30min 外 → 该 session 整桶消失，符合"活跃"定义，不做渐隐。
5. **`session=s` 类怪异值**：按字面 id 处理（前 8 位截断后原样），不特殊化。
6. **status=0 边界**：未实测出现，但 `_acc["status"]` 初值 0，请求中途异常理论上会留 status=0 行；`!="200"` 判 fail 天然覆盖。

## 5. 测试方案（`tests/test_format_ops.py`）

fixture 时间戳用 `datetime.now() ± timedelta` 动态生成（不可用硬编码日期，否则窗口过滤过期即失效）：

1. `parse_access_line`：正常行解析全字段 / 非 ACCESS 行 → None / 坏时间戳 → None / 缺尾字段容忍 / `route_failover` 不误命中 `failover`（§2 表 #7）。
2. `load_active_sessions`（tmp 日志文件）：窗口过滤（31min 前行排除）；分组聚合 n/fail/fo；空 session → `(none)`；builtin 计活跃不计 n；文件缺失 → `log_missing`；小 `tail_bytes` 构造截断标志位。
3. 状态判定：末行非 200 → FAIL；窗口有 fail 但末行 200 → warn；fo>0 全 200 → warn；全 200 → ok；仅 builtin → ok + 仅 $route 注记。
4. 渲染：FAIL 优先排序、err 续行含 req_id、零活跃行、截断提示、每行 `display_width ≤ 80` 断言（沿用现有 display_width 工具）。
5. `_format_status_from_json` 端到端：session 段位于 health 行之后、异常段之前；server JSON + config + 账本 + 日志四 fixture 联调。

## 6. 验证方式

```bash
cd tools/model_proxy
python3 -m unittest discover tests      # 全绿
./model_proxy_cli.sh status              # 对照 §3.3 样例结构
# 交叉核对：手工聚合日志尾 30min，与 status 输出的 n/fail/末行状态一致
grep ' ACCESS ' .claude_model_proxy.log | tail -50
./model_proxy_cli.sh off && ./model_proxy_cli.sh status   # 降级：无 session 段，exit 1
./model_proxy_cli.sh on
```

人工核对点：活跃 session 数与 `grep -o 'session=[^ ]*' | sort -u`（30min 内）一致；FAIL 行的 req= 用 `logs req=<id>` 能捞到对应错误。

## 7. 实施顺序（供 implementer）

1. `_format_ops.py`：`parse_access_line` / `load_active_sessions` / `_format_active_sessions` + `_format_status_from_json` 加参 + `main()` 收参（纯 python，可独立单测）。
2. `tests/test_format_ops.py`：§5 全部用例，跑绿。
3. `model_proxy_cli.sh`：cmd_status :172 加 `"$LOG_FILE"` + help 文案。
4. 手工验证 §6 全部命令。

## 关联

- [[2026-08-08-status-p0-implementation-plan]]（本段挂进的仪表盘结构来源）
- [[2026-08-08-status-content-redesign]]（运行态优先/异常优先原则来源）
- [[2026-07-22-access-log-and-latency]]（ACCESS 日志格式设计）
