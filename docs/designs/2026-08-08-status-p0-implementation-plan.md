---
type: design-decision
status: pending
target: "[[tools/model_proxy]]"
tags:
  - architect
  - model_proxy
  - cli
  - status
modified: 2026-08-08 22:10:00
created: 2026-08-08 22:10:00
---

# status P0 档实施方案（零 server 改动，CLI 侧落地）

> [务实] 路径：把 [[2026-08-08-status-content-redesign]] §6 P0 档落成可执行实施清单。
> 范围已由用户拍板：① 做 P0；② degraded 阈值 fail%>30% 且样本≥5；③ STATUS preset 缩成计数行（明细全归菜单）。
> 核实基准：2026-08-08 master 工作区，代理在跑（port 18889）；config = 25 supplies / 6 routes / 4 strategies；账本 v3，today 桶 30 combos。

## 1. 背景与问题

分析文档已定内容方向（运行态优先、异常优先、配置收敛为计数）。本文档回答"怎么改"：每项改动落到文件:行、数据来源、新输出样例、停机降级与测试。**P0 严格零 server 改动**，已逐项核实可行（§2）。

## 2. 核实结果（与分析文档 / 派单假设的对照）

| # | 核实项 | 结论 | 与假设是否相符 |
|---|---|---|---|
| 1 | 锁文件 | `/tmp/claude_model_proxy.lock` 内容确为 pid（`core/server.py:2364` `lock_fd.write(str(os.getpid()))`，实测内容 `74845`）。README 提的 `/tmp/claude_model_proxy.pid` **实测不存在**（/tmp 下只有 .lock 与 _ensure.log） | 基本相符。但实施**改用 lsof 取 pid**（见 §3.1 理由），锁文件仅作记录 |
| 2 | `ps -o lstart=,etime=` | macOS 可用，实测 `ps -o lstart=,etime= -p 74845` → `Sat Aug  8 19:53:05 2026     01:37:09` | 相符 |
| 3 | 账本 today 桶 | combo 键是**字符串** `supply=X\|route=Y\|strategy=Z`（`server.py:211-217`），值是 dict（requests/ok/fail/...），可按 supply 聚合。解析逻辑同 `cmd_stats` 的 `parse_combo_key`（`model_proxy_cli.sh:483-488`） | 相符 |
| 4 | cooldown 段 | server JSON `cooldown` = `{supply_id: 剩余秒 float}`，仅含冷却中的（`CooldownStore.snapshot` `server.py:553-563`） | 相符 |
| 5 | sidecar 离线读 | `_format_status_offline` 已实例化 `SessionOverridesSidecar`（`_format_ops.py:312-313`）；`core/commands.py` import 链纯 stdlib（copy/json/logging/os/re/tempfile/threading/time/datetime/pathlib/typing），已核实 | 相符 |
| 6 | orphan 计算 | config 静态可算。实测当前 orphan = `ds-flash-sankuai-3339, glm-51-sankuai-3339`；无 dangling 引用。意外发现：`eval-kimi`/`eval-dsp` 两个 route **缺 opus/haiku 档**且被同名 strategy 引用（疑为预期配置，见 §6 风险 5） | 相符 + 新发现 |
| 7 | STATUS preset 现状 | `format_supplies(preset="STATUS")` 4 列平铺（`_format_ops.py:126-142`）；`format_routes` 无 preset（:157-217）；`format_strategies(style="status")` 双行含 override（:241-258）；`_format_status_from_json`（:267-295）五段拼装 | 相符 |
| 8 | server JSON 字段 | `_handle_status`（`server.py:1946-1976`）返回 supplies/routes/strategies(含 sidecar_overrides_count)/cooldown/default_cooldown_seconds。P0 所需全部就位，**零 server 改动成立，无数据缺口** | 相符 |

**结论：P0 无需升级任何项到 P1。** 唯一进程态数据（pid/uptime/started）经 lsof+ps 离线推导可得。

## 3. 方案设计

### 3.1 `model_proxy_cli.sh` — `cmd_status`（:127-148）

**在线路径**改动：
1. pid 来源：复用已有的 `lsof -i :"$MODEL_PROXY_PORT" -sTCP:LISTEN -t`（:133）捕获 pid，而非读锁文件。理由：① cmd_status 本就调 lsof 判活，同一调用顺手取 pid，零额外 fork；② lsof 拿到的是"正在监听该端口的真实进程"，无锁文件 stale 风险（进程被杀后 .lock 残留旧 pid）；③ server 有 flock 互斥（`server.py:2356-2363`），同端口多实例不可能。锁文件仅文档记录，不作数据源。
2. 启动时间/uptime：`ps -o lstart= -p "$pid"`、`ps -o etime= -p "$pid"`（两次调用避免空格解析）；ps 失败（进程恰好退出）则省略对应片段，不报错。
3. config mtime：`stat -f %Sm -t "%m-%d %H:%M" "$CONFIG_FILE"`。
4. 首行（bash 打印，信息全在 bash 侧）：
   `model_proxy: running on port 18889  pid 74845  up 01:37:09  (started 08-08 19:53, config mtime 08-08 20:01)`
   started 从 lstart 裁剪 `awk '{print $3,$4}'` 级处理即可，不追求重排日期。
5. 调用改为：`echo "$out" | python3 "$SCRIPT_DIR/_format_ops.py" status-format "$CONFIG_FILE" "$TOTALS_FILE"`（原 :147 只传 stdin，现补两个路径参数）。

**离线路径**改动（:136-138）：
- 首行保持 `model_proxy: NOT running on port X`。
- 调用改为 `python3 _format_ops.py status-offline "$CONFIG_FILE" "$TOTALS_FILE"`，退出码 1 保持。

**help 文案**（:22）同步更新为新结构描述。

### 3.2 `_format_ops.py` — 新增数据函数（全部 stdlib）

| 函数 | 职责 | 数据来源 |
|---|---|---|
| `_parse_combo_key(key) -> dict` | `supply=X\|route=Y\|strategy=Z` 拆成 dict（与 cmd_stats `parse_combo_key` 同逻辑，7 行；cmd_stats 是 heredoc 内嵌 python 无法 import，P0 接受这一份小重复并注释互指） | — |
| `load_supply_health(totals_path) -> dict[str, dict]` | 读账本 CST today 桶（时区口径与 server `_cst_now` 一致），combos 按 supply 聚合 `{requests, ok, fail}`。文件缺失/JSON 损坏 → 返回 `{}`（降级不报错） | `.claude_model_proxy_totals.json` |
| `compute_config_anomalies(cfg) -> dict` | 遍历 `routes[].tiers` 收集被引用 supply id，与 `supplies[].id` diff 得 orphans；收集空 tier 得 missing_tiers；tiers 中不在 supplies 的 id + strategies 引用不存在的 route_id 得 dangling_refs | config dict |
| `find_damaged_routes(cfg, bad_supplies) -> list[str]` | tier 内含 degraded∪cooling supply 的 route，输出 `nation1: opus 档 9907 degraded` 式描述 | config + 前两步结果 |

阈值常量置顶：`DEGRADED_MIN_REQUESTS = 5`、`DEGRADED_FAIL_PCT = 30.0`。

### 3.3 `_format_ops.py` — `_format_status_from_json`（:267-295）重写

签名改为 `(data, config_path, totals_path)`，新布局（**基于 2026-08-08 真实数据渲染**）：

```
health: cooldown 0/25 · degraded 3 · overrides 1 · orphan 2

degraded supplies (today fail%>30%, n>=5):
  kimi-k3-sankuai-3672   fail 80.0% (12/15)
  kimi-k3-sankuai-9907   fail 68.2% (15/22)
  kimi-k3-sankuai-4200   fail 60.0% (15/25)

unmatched: 233 req 今日全失败（supply=(none)，未匹配 strategy/route，多为 401）

damaged routes:
  nation1   opus 档 kimi-k3-sankuai-9907 degraded
  nation2   opus 档 kimi-k3-sankuai-4200, kimi-k3-sankuai-3672 degraded

config notices:
  orphan supplies: ds-flash-sankuai-3339, glm-51-sankuai-3339
  缺档: eval-kimi 缺 opus/haiku；eval-dsp 缺 opus/haiku（若属预期请忽略）

config: 25 supplies / 6 routes / 4 strategies · default_cooldown=60s
       （明细: supply / route / strategy 菜单 list；今日明细: stats today supply）
```

各段规则：
- **health 行**：cooldown 数 = `len(data["cooldown"])`；总 supply 数 = `len(data["supplies"])`；degraded 数 = `load_supply_health` 中 fail%>30 且 requests≥5 且 id≠`(none)` 的个数；overrides 数 = Σ `strategies[].sidecar_overrides_count`（server JSON 已有，server.py:1967）；orphan 数 = `compute_config_anomalies`。**全 0 时 health 行即"系统健康"，后续异常段整段不打印**（无消息即好消息）。
- **degraded 段**：按 fail% 降序；`(none)` 排除出 degraded，fail>0 时单列 `unmatched:` 行（分析文档 §7.3 口径）。
- **cooldown 段**：有则列 `supply 剩余秒`（现逻辑上移），无则并入 health 行（cooldown 0 已表达），不单独打印。
- **damaged routes 段**：仅运行态受损（tier 命中 degraded∪cooling supply）。
- **config notices 段**：orphan、缺 tier、dangling 引用，提示级措辞。缺 tier 列入此段而非 damaged routes——`eval-kimi`/`eval-dsp` 疑为预期的单档 eval 路由，放 damaged 区会误报（见 §6 风险 5）。
- **config 计数行**：计数取 server JSON 长度；`default_cooldown_seconds` 并入此行（原独立行 :293 删除）。
- **supplies/routes/strategies 三段平铺全部删除**（原 :270-284）。

### 3.4 `_format_ops.py` — `_format_status_offline`（:298-324）重写 + 停机降级

签名补 `totals_path`。代理未运行时：

```
health: cooldown (代理未运行) · degraded (代理未运行) · overrides 1 · orphan 2

config notices:
  orphan supplies: ds-flash-sankuai-3339, glm-51-sankuai-3339
  缺档: eval-kimi 缺 opus/haiku；eval-dsp 缺 opus/haiku（若属预期请忽略）

config: 25 supplies / 6 routes / 4 strategies · default_cooldown=60s
       （明细: supply / route / strategy 菜单 list）
```

- overrides 数：sidecar 静态可读（现 :312-317 逻辑改为求和）。
- orphan/缺 tier/dangling：config 静态可算，照常展示。
- cooldown/degraded：显 `(代理未运行)`。**不读账本**——停机时账本 today 桶仍是历史值，展示会误导为"当前状态"；查历史用 `stats today`。
- 退出码 1 由 cmd_status 保持。

### 3.5 STATUS preset 下线（用户已拍板"缩成计数行"）

- `format_supplies`（:114-154）：删 `preset="STATUS"` 分支（:126-142），只留 MENU；preset 参数可简化为无参（保留参数形以免牵连 `_config_ops.py` 调用方——需 grep 确认调用面后定）。
- `format_strategies`（:220-260）：删 `style="status"` 分支（:241-258），只留 menu。
- `format_routes`：无 preset，不动。
- `normalize_supply`/`mask_appkey`/`strategy_route_desc`：MENU 分支与 `_config_ops.py` 仍在用，保留。

### 3.6 测试改动面（`tests/test_format_ops.py`，367 行）

- 删/改：`test_status_preset_max_width_le_80`（:99-114）、format_strategies style="status" 用例（:41-89）、`test_full_status_format`（:333-）、`test_status_format_with_cooldown`（:350-）、status-offline 三段断言（:175-224）。
- 新增：`load_supply_health`（tmp 账本 fixture：聚合正确、文件缺失返回 {}、JSON 损坏返回 {}、`(none)` 不混入 degraded）；`compute_config_anomalies`（orphan/缺 tier/dangling 各一）；`_format_status_from_json` 新布局断言（health 行计数、degraded 排序与阈值边界 requests=5/fail%=30、全 0 时无异常段）；`_format_status_offline` 降级断言。

## 4. 验证方式

```bash
cd tools/model_proxy
python3 -m unittest discover tests            # 全绿
./model_proxy_cli.sh status                    # 在线：对照 §3.3 样例结构；数字与下条交叉验证
./model_proxy_cli.sh stats today supply        # degraded 三行的 req/fail 与 status 一致
ps -o lstart=,etime= -p $(lsof -i :18889 -sTCP:LISTEN -t)  # 进程行 up/started 一致
./model_proxy_cli.sh off && ./model_proxy_cli.sh status    # 降级路径：exit 1，health 行显 (代理未运行)，config 段照常
./model_proxy_cli.sh on                        # 恢复
./model_proxy_cli.sh supply                    # 菜单 list 输出不变（MENU preset 回归）
```

人工核对点：健康时（手工清账本或选无异常日）status ≤8 行；health 行各计数与 `stats today supply`、config 文件逐项一致。

## 5. 实施顺序（供 implementer）

1. `_format_ops.py`：加数据函数 + 重写两个 format 入口 + 删 STATUS/status 分支（纯 python，可独立单测）。
2. `tests/test_format_ops.py`：同步改写，跑到全绿。
3. `model_proxy_cli.sh`：cmd_status 两路径 + help 文案。
4. 手工验证 §4 全部命令。

依赖：步骤 1-2 闭环后再动 3（bash 侧只是传参与打印进程行）。

## 6. 风险与权衡

1. **ps 跨平台**：`lstart`/`etime` 在 macOS(BSD) 与 GNU ps 均支持（etime 近 POSIX，lstart 为 GNU/BSD 共有扩展）；本工具实际部署 macOS，风险低。兜底：ps 失败只打 port+pid。
2. **账本读取**：54KB JSON 每次 status 解析，与 fork python3 开销同级；损坏时降级为 health 行 degraded 显 `(账本不可用)`，不报错退出。账本涨 MB 级需再评估。
3. **degraded 误报**：today 桶是累计口径，跨时段已恢复的 supply 会被误标（分析文档 §7.1 已述，用户已接受阈值）；行内展示 `fail x/y` 供运维自判，P2 滑窗才是彻底解。
4. **STATUS preset 删除的调用面**：`format_supplies(preset="STATUS")` 当前仅 `_format_status_from_json`/`_format_status_offline` 使用；实施时 grep `preset="STATUS"`、`style="status"` 全仓确认无漏（`_config_ops.py` 只用 MENU/menu 分支，已初查）。
5. **缺 tier 提示误报 eval 路由**：`eval-kimi`/`eval-dsp` 仅有 sonnet 档且被同名 strategy 引用——若是预期配置，每次 status 都会看到这条 notice。**已按提示级措辞（"若属预期请忽略"）处理并归入 config notices 而非 damaged routes；若用户确认 eval 路由确为预期，可在实施时加 eval- 前缀豁免或接受常驻提示——此点请用户拍板。**
6. **cmd_stats 与 _format_ops 的 parse_combo_key 重复**：7 行小重复，heredoc 结构导致无法单源化，注释互指即可；未来账本键格式变更需两处同改（已有 tests/test_usage_totals.py 兜底键格式）。
7. **`status config` 逃生门**（分析文档 §5.3）：不在本次 P0 范围，留作可选附加项——实现极廉价（status-format 加分支调 MENU preset 函数，约 10 行），若用户想要可顺手并入步骤 1。

## 关联

- [[2026-08-08-status-content-redesign]]（分析文档，本方案的目标结构来源，§6 P0 档）
- [[2026-08-08-cli-status-help-improvement-plan]]（格式层改进；STATUS preset 下线后 MENU preset 继续供菜单）
- [[2026-08-06-session-overrides-single-storage]]（overrides 计数来源 sidecar）
